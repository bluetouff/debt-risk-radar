"""Per-signal completeness and conservative observation-age checks.

Age limits are monitoring tolerances, not provider publication commitments.
FRED quarterly dates identify period starts; BIS dates identify period ends.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from catalog import CBO_DATASETS, FRED_SERIES, WORLD_BANK_INDICATORS

FRESHNESS_POLICY_VERSION = "2"
PUBLICATION_WARNING_DAYS = 14
PUBLICATION_MAX_AGE_DAYS = 120
PUBLICATION_METADATA_TTL = 24 * 3600
OBSERVATION_CACHE_TTL = 6 * 3600


def utc_timestamp(value):
    if not isinstance(value, (str, pd.Timestamp)):
        return pd.NaT
    return pd.to_datetime(value, errors="coerce", utc=True)


def quarterly_publication_deadline(row, now):
    """Bounded exception for a freshly verified latest official quarterly release.

    It is not an extension of the HTTP cache or a change of observation date.
    A revision cannot keep an old quarter eligible indefinitely.
    """
    observed = utc_timestamp(row.get("date"))
    end = utc_timestamp(row.get("publication_observation_end"))
    updated = utc_timestamp(row.get("publication_updated_at"))
    checked = utc_timestamp(row.get("publication_checked_at"))
    retrieved = utc_timestamp(row.get("observation_checked_at"))
    if any(pd.isna(value) for value in (observed, end, updated, checked, retrieved)):
        return pd.NaT
    if row.get("publication_frequency") != "Q" or end != observed:
        return pd.NaT
    period = observed.tz_localize(None).to_period("Q")
    period_end = period.end_time.normalize().tz_localize("UTC")
    if observed != period.start_time.tz_localize("UTC"):
        return pd.NaT
    if not (period_end <= updated <= min(checked, retrieved) and max(checked, retrieved) <= now):
        return pd.NaT
    if not (0 <= (now - checked).total_seconds() < PUBLICATION_METADATA_TTL
            and 0 <= (now - retrieved).total_seconds() < OBSERVATION_CACHE_TTL):
        return pd.NaT
    return min(updated.normalize() + pd.Timedelta(days=PUBLICATION_MAX_AGE_DAYS + 1),
               period_end + pd.DateOffset(months=6) + pd.Timedelta(days=31))


def expected_metrics() -> dict[tuple[str, str], dict]:
    expected = {}

    def add(bucket, series_id, meta, max_age_days, frequency):
        expected[bucket, series_id] = dict(meta, bucket=bucket, series_id=series_id,
                                          max_age_days=max_age_days, frequency=frequency)

    for bucket, series in FRED_SERIES.items():
        for series_id, meta in series.items():
            if series_id == "FYFSGDA188S":
                age, freq = 1100, "annual_period_start"
            elif bucket in {"fiscal", "private_leverage"}:
                age, freq = 280, "quarterly_period_start"
            elif series_id in {"NFCI", "STLFSI4", "WRESBAL"}:
                age, freq = 28, "weekly"
            else:
                age, freq = 10, "daily"
            add(bucket, series_id, meta, age, freq)
    for series_id in ("Total public debt outstanding", "Debt held by the public share", "90d annualized debt growth"):
        add("treasury_daily", series_id, {"name": series_id, "source": "US Treasury Fiscal Data"}, 10, "daily")
    for series_id, meta in WORLD_BANK_INDICATORS.items():
        add("world_bank", series_id, meta, 900, "annual_period_end")
    for name in ("Credit-to-GDP gap", "Credit-to-GDP ratio", "Household debt service ratio",
                 "Private non-financial debt service ratio", "Corporate debt service ratio"):
        add("global_credit", f"BIS {name}", {"name": name, "source": "BIS Data Portal"}, 300, "quarterly_period_end")
    for series_id, meta in CBO_DATASETS["long_term_budget"]["variables"].items():
        add("cbo_projection", series_id, dict(meta, source="CBO Open Data"), None, "projection")
    return expected


def assess_metrics(metrics: pd.DataFrame, now=None) -> pd.DataFrame:
    instant = pd.Timestamp(now if now is not None else pd.Timestamp.now(tz="UTC"))
    instant = instant.tz_localize("UTC") if instant.tzinfo is None else instant.tz_convert("UTC")
    today = instant.tz_localize(None).normalize()
    rows = []
    for (bucket, series_id), meta in expected_metrics().items():
        sub = metrics[(metrics["bucket"] == bucket) & (metrics["series_id"] == series_id)] if not metrics.empty else pd.DataFrame()
        row = {"bucket": bucket, "series_id": series_id, "name": meta["name"],
               "unit": meta.get("unit", ""), "source": meta["source"], "date": pd.NaT,
               "current": np.nan, "signed_z": np.nan, "risk_score": np.nan,
               "weight": meta.get("weight", 1.0), "rationale": meta.get("rationale", "")}
        if not sub.empty:
            row.update(sub.iloc[0].to_dict())
        status, detail = "ok", "Within observation-age tolerance."
        observed = pd.to_datetime(row["date"], errors="coerce", utc=True)
        age = (today - observed.tz_localize(None).normalize()).days if pd.notna(observed) else None
        expires = (observed.normalize() + pd.Timedelta(days=meta["max_age_days"] + 1)
                   if pd.notna(observed) and meta["max_age_days"] is not None else pd.NaT)
        publication_deadline = (quarterly_publication_deadline(row, instant)
                                if meta["frequency"] == "quarterly_period_start" else pd.NaT)
        basis = "observation_age"
        limit = expires
        if pd.notna(publication_deadline) and publication_deadline > expires:
            limit = publication_deadline
            expires = max(expires, min(publication_deadline,
                          utc_timestamp(row["publication_checked_at"]) + pd.Timedelta(seconds=PUBLICATION_METADATA_TTL),
                          utc_timestamp(row["observation_checked_at"]) + pd.Timedelta(seconds=OBSERVATION_CACHE_TTL)))
        if sub.empty or row.get("quality") == "missing":
            status, detail = "missing", "Expected signal unavailable."
        elif row.get("quality") == "invalid":
            status, detail = "invalid", row["quality_detail"]
        elif len(sub) != 1:
            status, detail = "invalid", "Duplicate signal identifier."
        elif pd.isna(observed) or not np.isfinite(row["current"]):
            status, detail = "invalid", "Invalid observation value or date."
        elif bucket != "cbo_projection" and age < 0:
            status, detail = "invalid", "Future observation date."
        elif not np.isfinite(row["risk_score"]):
            status, detail = "unscored", "Insufficient valid history or zero variance."
        elif not 0 <= row["risk_score"] <= 100 or not np.isfinite(row["weight"]) or row["weight"] <= 0:
            status, detail = "invalid", "Invalid score or weight."
        elif bucket == "cbo_projection":
            status, detail = "projection", "Pinned CBO February 2026 vintage; date is the projection horizon."
        elif meta["max_age_days"] is not None and age > meta["max_age_days"]:
            if pd.notna(publication_deadline) and instant < publication_deadline:
                status, basis = "official_delayed", "verified_quarterly_publication"
                detail = ("Latest quarterly observation confirmed by FRED metadata; publication delayed. "
                          "Original period retained. Bounded by publication age, period age and cache expiry.")
            else:
                status = "stale"
                detail = ("Observation-age limit exceeded; no valid recent quarterly publication confirmation."
                          if meta["frequency"] == "quarterly_period_start"
                          else "Observation older than this frequency's tolerance.")
        row.update(quality=status, quality_detail=detail, observation_age_days=age if bucket != "cbo_projection" else None,
                   max_age_days=meta["max_age_days"], frequency=meta["frequency"],
                   freshness_basis=basis, freshness_expires_at=expires, freshness_limit_at=limit,
                   eligible=status in {"ok", "projection", "official_delayed"})
        if not row["eligible"]:
            row["risk_score"] = np.nan
        rows.append(row)
    result = pd.DataFrame(rows)
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    return result
