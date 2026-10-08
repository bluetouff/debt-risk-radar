"""Bounded HTTPS reads, persistent cache and provider-wide request budgets."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests


class DataUnavailable(Exception):
    """An upstream result cannot safely be used as current data."""


PROVIDER_LABELS = {
    "api.stlouisfed.org": "FRED",
    "api.fiscaldata.treasury.gov": "US Treasury Fiscal Data",
    "api.worldbank.org": "World Bank",
    "data.bis.org": "BIS Data Portal",
    "raw.githubusercontent.com": "CBO Open Data",
    "api.massive.com": "Massive Market Data",
}
ALLOWED_HOSTS = frozenset(PROVIDER_LABELS)
FAILURE_REASONS = frozenset({"rate_limit", "authorization", "http_error", "network_error", "invalid_response"})
logger = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
FAILURE_COOLDOWN = 15 * 60
AUTH_COOLDOWN = 6 * 3600
MAX_RATE_LIMIT_COOLDOWN = 6 * 3600
MASSIVE_REQUEST_INTERVAL = 65.0
CACHE_REFRESH_WINDOW = 30 * 60


def read_only() -> bool:
    return os.environ.get("DEBT_RISK_RADAR_READ_ONLY") == "1"


def cache_path() -> Path:
    return Path(os.environ.get(
        "DEBT_RISK_RADAR_CACHE_DIR", str(Path.home() / ".cache" / "debt-risk-radar")
    )) / "http-v1.sqlite3"


def _connect() -> sqlite3.Connection:
    path = cache_path()
    if read_only():
        return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path, timeout=60)
    os.chmod(path, 0o600)
    db.execute("CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, fetched REAL, body BLOB)")
    db.execute("CREATE TABLE IF NOT EXISTS providers (host TEXT PRIMARY KEY, last_request REAL, blocked_until REAL)")
    # Keep the existing tables compatible with older collectors during rollback.
    db.execute("CREATE TABLE IF NOT EXISTS rate_limits (host TEXT PRIMARY KEY, failures INTEGER NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS provider_failures (host TEXT PRIMARY KEY, reason TEXT NOT NULL)")
    db.commit()
    return db


def collection_status(active_hosts: frozenset[str] | None = None) -> dict:
    """Read only public-safe pause metadata; never create a cache or make a request."""
    try:
        with closing(sqlite3.connect(cache_path().resolve().as_uri() + "?mode=ro", uri=True, timeout=60)) as db:
            now = time.time()
            rows = db.execute("SELECT host, last_request, blocked_until FROM providers WHERE blocked_until > ?", (now,)).fetchall()
            has_reasons = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_failures'").fetchone()
            reasons = dict(db.execute("SELECT host, reason FROM provider_failures")) if has_reasons else {}
        providers = []
        for host, attempted, blocked in sorted(rows):
            if active_hosts is not None and host not in active_hosts:
                continue
            if host not in PROVIDER_LABELS or not all(math.isfinite(value) for value in (attempted, blocked)):
                continue
            reason = reasons.get(host)
            providers.append({
                "source": PROVIDER_LABELS[host],
                "reason": reason if reason in FAILURE_REASONS else "upstream_failure",
                "last_attempt_at": datetime.fromtimestamp(attempted, timezone.utc).isoformat().replace("+00:00", "Z"),
                "retry_at": datetime.fromtimestamp(blocked, timezone.utc).isoformat().replace("+00:00", "Z"),
            })
        return {"status": "paused" if providers else "ok", "providers": providers}
    except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
        return {"status": "unknown", "providers": []}


def _retry_after(value: str | None, now: float) -> float:
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            dt = parsedate_to_datetime(value)
            delay = dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp() - now
        except (ValueError, TypeError, OverflowError):
            delay = FAILURE_COOLDOWN
    if not 0 <= delay < float("inf"):
        delay = FAILURE_COOLDOWN
    return max(FAILURE_COOLDOWN, delay)


def get_bytes(url: str, *, params: dict | None = None, headers: dict | None = None,
              ttl: int, cache_key: str | None = None) -> bytes:
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS
            or parts.port not in (None, 443) or parts.username or parts.password
            or parts.query or parts.fragment):
        raise DataUnavailable("Source URL is not an allowed HTTPS endpoint.")
    if parts.hostname == "raw.githubusercontent.com" and not parts.path.startswith("/US-CBO/cbo-data/"):
        raise DataUnavailable("Only the official CBO data repository is allowed.")
    public_params = {k: v for k, v in (params or {}).items() if k.lower() != "api_key"}
    identity = [url if cache_key is None else parts.hostname + ":" + cache_key, public_params]
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    try:
        with closing(_connect()) as db:
            # The write transaction serializes cold misses across exporter processes.
            if not read_only():
                db.execute("BEGIN IMMEDIATE")
            now = time.time()
            cached = db.execute("SELECT fetched, body FROM responses WHERE key=?", (key,)).fetchone()
            fresh = cached is not None and 0 <= now - cached[0] < ttl
            # Renew before the next timer tick without extending the readers' TTL.
            refresh_age = ttl - min(CACHE_REFRESH_WINDOW, ttl / 10)
            if fresh and (read_only() or now - cached[0] < refresh_age):
                return bytes(cached[1])
            if read_only():
                raise DataUnavailable("Source cache missing or expired; awaiting scheduled collection.")

            host = parts.hostname
            state = db.execute("SELECT last_request, blocked_until FROM providers WHERE host=?", (host,)).fetchone()
            if state and state[1] > now:
                if fresh:
                    return bytes(cached[1])
                raise DataUnavailable("Provider temporarily paused after an upstream failure or rate limit.")
            interval = MASSIVE_REQUEST_INTERVAL if host == "api.massive.com" else 1.0
            if state:
                time.sleep(max(0, min(interval, state[0] + interval - now)))
            error = None
            failure_reason = "network_error"
            body = None
            blocked_until = 0.0
            try:
                with requests.get(url, params=params, headers=headers, timeout=(10, 30),
                                  allow_redirects=False, stream=True) as response:
                    if response.status_code != 200:
                        failure_reason = "authorization" if response.status_code in (401, 403) else "http_error"
                        cooldown = AUTH_COOLDOWN if response.status_code in (401, 403) else FAILURE_COOLDOWN
                        if response.status_code == 429:
                            failure_reason = "rate_limit"
                            prior = db.execute("SELECT failures FROM rate_limits WHERE host=?", (host,)).fetchone()
                            failures = min(6, max(0, prior[0] if prior else 0) + 1)
                            backoff = min(MAX_RATE_LIMIT_COOLDOWN, FAILURE_COOLDOWN * 2 ** (failures - 1))
                            cooldown = max(backoff, _retry_after(response.headers.get("Retry-After"), time.time()))
                            db.execute("INSERT OR REPLACE INTO rate_limits (host, failures) VALUES (?, ?)", (host, failures))
                        blocked_until = time.time() + cooldown
                        if response.status_code == 429:
                            raise DataUnavailable(f"HTTP 429; provider requests paused for at least {cooldown / 60:g} minutes.")
                        raise DataUnavailable(f"HTTP {response.status_code}; provider requests paused.")
                    failure_reason = "invalid_response"
                    chunks = []
                    size = 0
                    started = time.monotonic()
                    for chunk in response.iter_content(64 * 1024):
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES or time.monotonic() - started > 45:
                            raise DataUnavailable("Upstream response exceeded size or duration limit.")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    secrets = [str(v) for k, v in (params or {}).items() if k.lower() == "api_key" and v]
                    authorization = (headers or {}).get("Authorization", "")
                    if authorization.startswith("Bearer ") and authorization != "Bearer None":
                        secrets.append(authorization[7:])
                    if any(secret.encode() in body for secret in secrets if secret):
                        raise DataUnavailable("Upstream response unexpectedly contains credentials; discarded.")
            except (requests.RequestException, DataUnavailable) as exc:
                error = str(exc) if isinstance(exc, DataUnavailable) else "Upstream request failed; provider requests paused."
                if isinstance(exc, requests.RequestException):
                    failure_reason = "network_error"
                blocked_until = max(blocked_until, time.time() + FAILURE_COOLDOWN)
            db.execute("INSERT OR REPLACE INTO providers VALUES (?, ?, ?)", (host, time.time(), blocked_until))
            if error is None:
                db.execute("DELETE FROM rate_limits WHERE host=?", (host,))
                db.execute("DELETE FROM provider_failures WHERE host=?", (host,))
                db.execute("INSERT OR REPLACE INTO responses VALUES (?, ?, ?)", (key, time.time(), body))
            else:
                db.execute("INSERT OR REPLACE INTO provider_failures VALUES (?, ?)", (host, failure_reason))
            db.commit()
            if error:
                # A failed renewal must not discard a still-valid response. Recheck
                # after the request: a timeout may have crossed its hard expiry.
                still_fresh = cached is not None and 0 <= time.time() - cached[0] < ttl
                logger.warning("Provider refresh deferred: source=%s reason=%s retry_at_epoch=%s cache_valid=%s",
                               PROVIDER_LABELS[host], failure_reason,
                               blocked_until, still_fresh)
                if still_fresh:
                    return bytes(cached[1])
                raise DataUnavailable(error)
            return body
    except (sqlite3.Error, OSError) as exc:
        raise DataUnavailable("Persistent source cache unavailable; no uncached request attempted.") from exc


def get_json(url: str, **kwargs):
    try:
        return json.loads(get_bytes(url, **kwargs))
    except (ValueError, UnicodeError) as exc:
        raise DataUnavailable("Invalid upstream JSON payload.") from exc
