"""Offline regression tests: no credentials or external API calls."""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

os.environ["DEBT_RISK_RADAR_DISABLE_STREAMLIT_CACHE"] = "1"

import numpy as np
import pandas as pd

import data
import http_cache
from catalog import CURRENT_STRESS_BUCKETS
from latest_export import build_latest_payload, write_latest_json
from quality import assess_metrics, expected_metrics


def response(body=b'{"ok": true}', status=200, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.iter_content.return_value = [body]
    return result


def complete_metrics(now="2026-09-08"):
    """Explicit synthetic test inputs, never used by runtime or public exports."""
    rows = []
    for (bucket, series_id), meta in expected_metrics().items():
        rows.append(dict(bucket=bucket, series_id=series_id, name=meta["name"],
                         source=meta["source"], unit=meta.get("unit", "%"),
                         date=pd.Timestamp("2056-09-30" if bucket == "cbo_projection" else now),
                         current=20.0, signed_z=0.0, risk_score=50.0, weight=1.0, rationale="Test fixture"))
    return pd.DataFrame(rows)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DEBT_RISK_RADAR_CACHE_DIR": self.tmp.name, "DEBT_RISK_RADAR_READ_ONLY": "0"})
        self.env.start()
        self.http = patch("http_cache.requests.get", return_value=response())
        self.get = self.http.start()
        self.url = "https://api.stlouisfed.org/fred/series/observations"

    def tearDown(self):
        self.http.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_persistent_cache_hides_credentials_and_prevents_repeated_requests(self):
        params = {"series_id": "TEST", "api_key": "test-only-credential"}
        for _ in range(3):
            self.assertEqual(http_cache.get_json(self.url, params=params, ttl=60), {"ok": True})
        self.get.assert_called_once()
        self.assertNotIn(b"test-only-credential", http_cache.cache_path().read_bytes())
        self.assertFalse(self.get.call_args.kwargs["allow_redirects"])
        self.assertEqual(http_cache.cache_path().stat().st_mode & 0o777, 0o600)

    def test_concurrent_cold_requests_fetch_once(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: http_cache.get_bytes(self.url, ttl=60), range(6)))
        self.assertEqual(len(set(results)), 1)
        self.get.assert_called_once()

    def test_read_only_cache_miss_never_accesses_network(self):
        with patch.dict(os.environ, {"DEBT_RISK_RADAR_READ_ONLY": "1"}):
            with self.assertRaises(http_cache.DataUnavailable):
                http_cache.get_bytes(self.url, ttl=60)
        self.get.assert_not_called()

    def test_expired_cache_is_not_relabelled_current(self):
        http_cache.get_bytes(self.url, ttl=60)
        with closing(sqlite3.connect(http_cache.cache_path())) as db, db:
            db.execute("UPDATE responses SET fetched=0")
        with patch.dict(os.environ, {"DEBT_RISK_RADAR_READ_ONLY": "1"}):
            with self.assertRaises(http_cache.DataUnavailable):
                http_cache.get_bytes(self.url, ttl=60)
        self.assertEqual(self.get.call_count, 1)

    def test_rate_limit_stops_other_series_and_persists_retry_after(self):
        self.get.return_value = response(status=429, headers={"Retry-After": "3600"})
        for series in ["TEST_A", "TEST_B"]:
            with self.assertRaises(http_cache.DataUnavailable):
                http_cache.get_bytes(self.url, params={"series_id": series}, ttl=60)
        self.get.assert_called_once()
        with closing(sqlite3.connect(http_cache.cache_path())) as db, db:
            blocked = db.execute("SELECT blocked_until FROM providers").fetchone()[0]
        self.assertGreater(blocked, time.time() + 3500)

    def test_retry_after_http_date(self):
        now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc).timestamp()
        self.assertEqual(http_cache._retry_after("Tue, 08 Sep 2026 14:00:00 GMT", now), 7200)

    def test_massive_calls_are_spaced_even_for_distinct_tickers(self):
        with patch("http_cache.time.sleep") as sleep:
            for ticker in ["AAA", "BBB"]:
                http_cache.get_bytes(f"https://api.massive.com/v2/{ticker}", ttl=60)
        self.assertGreater(sleep.call_args.args[0], 12)

    def test_rejects_untrusted_hosts_and_redirects_without_following(self):
        for url in ["http://api.massive.com/v2", "https://api.massive.com.evil.invalid/v2",
                    "https://127.0.0.1/", "https://user@api.massive.com/v2",
                    "https://raw.githubusercontent.com/other/repo/file"]:
            with self.assertRaises(http_cache.DataUnavailable):
                http_cache.get_bytes(url, ttl=60)
        self.get.assert_not_called()
        self.get.return_value = response(status=302, headers={"Location": "https://127.0.0.1/"})
        with self.assertRaises(http_cache.DataUnavailable):
            http_cache.get_bytes(self.url, ttl=60)
        self.get.assert_called_once()

    def test_response_size_is_bounded(self):
        with patch("http_cache.MAX_RESPONSE_BYTES", 1):
            with self.assertRaises(http_cache.DataUnavailable):
                http_cache.get_bytes(self.url, ttl=60)

    def test_provider_echoing_a_credential_is_not_cached(self):
        self.get.return_value = response(body=b'{"echo": "example-credential-value"}')
        with self.assertRaises(http_cache.DataUnavailable):
            http_cache.get_bytes(self.url, ttl=60, params={"api_key": "example-credential-value"})
        self.assertNotIn(b"example-credential-value", http_cache.cache_path().read_bytes())

    def test_network_exception_is_redacted_and_does_not_trigger_a_retry(self):
        self.get.side_effect = http_cache.requests.ConnectionError("https://example.invalid?api_key=example-sensitive")
        for _ in range(2):
            with self.assertRaises(http_cache.DataUnavailable) as result:
                http_cache.get_bytes(self.url, ttl=60)
            self.assertNotIn("example-sensitive", str(result.exception))
        self.get.assert_called_once()


class QualityTests(unittest.TestCase):
    def test_fred_json_preserves_observation_dates_and_missing_values(self):
        payload = {"count": 3, "observations": [
            {"date": "2026-09-02", "value": "4.5"},
            {"date": "2026-09-03", "value": "."},
            {"date": "2026-09-04", "value": "4.6"},
        ]}
        with patch("data.fred_key_available", return_value=True), patch("data._streamlit_secret", return_value=None), \
                patch("data.iter_fred_catalog", return_value=[("rates_market", "DGS10", {})]), \
                patch("data.get_json", return_value=payload) as get:
            series, issues = data.fetch_fred_series()
        self.assertEqual(issues, [])
        self.assertEqual(series["DGS10"].tolist(), [4.5, 4.6])
        self.assertEqual(series["DGS10"].index[-1], pd.Timestamp("2026-09-04"))
        self.assertEqual(get.call_args.kwargs["params"]["series_id"], "DGS10")

    def test_massive_daily_dates_and_duplicate_rejection(self):
        timestamp = int(pd.Timestamp("2026-09-04", tz="America/New_York").timestamp() * 1000)
        payload = {"results": [{"t": timestamp, "c": 80.0}]}
        with patch("data.massive_key_available", return_value=True), patch("data._streamlit_secret", return_value=None), \
                patch("data.MASSIVE_MARKET_SERIES", {"TLT": {}}), patch("data.get_json", return_value=payload):
            series, issues = data.fetch_massive_market()
            self.assertEqual(issues, [])
            self.assertEqual(series["TLT"].index[-1], pd.Timestamp("2026-09-04"))
            payload["results"] *= 2
            series, issues = data.fetch_massive_market()
            self.assertEqual(series, {})
            self.assertEqual(len(issues), 1)

    def test_missing_single_signal_reduces_coverage_and_suspends_overall(self):
        raw = complete_metrics()
        assessed = assess_metrics(raw[raw.series_id != "DGS10"], now="2026-09-08")
        buckets = data.bucket_scores(assessed)
        self.assertLess(data.score_coverage(buckets, CURRENT_STRESS_BUCKETS), 1)
        self.assertTrue(np.isnan(data.current_stress_score(buckets)))
        self.assertEqual(assessed.loc[assessed.series_id == "DGS10", "quality"].iloc[0], "missing")

    def test_complete_current_score_ignores_cbo_value_and_missing_cbo(self):
        raw = complete_metrics()
        raw = raw[raw.bucket != "cbo_projection"]
        self.assertEqual(data.current_stress_score(data.bucket_scores(assess_metrics(raw, now="2026-09-08"))), 50)

    def test_zero_data_never_yields_neutral_50(self):
        self.assertTrue(np.isnan(data.current_stress_score(data.bucket_scores(data.combine_metrics()))))

    def test_frequency_tolerances_and_projection_horizon(self):
        raw = complete_metrics()
        dates = {"DGS10": "2026-09-04", "NFCI": "2026-08-28", "GFDEGDQ188S": "2026-01-01",
                 "BIS Credit-to-GDP gap": "2025-12-31", "GC.XPN.INTP.RV.ZS": "2024-12-31"}
        for series, date in dates.items():
            raw.loc[raw.series_id == series, "date"] = pd.Timestamp(date)
        assessed = assess_metrics(raw, now="2026-09-08")
        self.assertTrue(assessed.eligible.all())
        self.assertTrue(assessed.loc[assessed.bucket == "cbo_projection", "observation_age_days"].isna().all())
        raw.loc[raw.series_id == "DGS10", "date"] = pd.Timestamp("2026-08-28")
        stale = assess_metrics(raw, now="2026-09-08").query("series_id == 'DGS10'").iloc[0]
        self.assertEqual(stale.quality, "stale")
        self.assertTrue(np.isnan(stale.risk_score))

    def test_nonfinite_duplicate_future_and_insufficient_history(self):
        for field, value in [("current", np.inf), ("date", pd.Timestamp("2026-09-09")), ("risk_score", -1)]:
            raw = complete_metrics()
            raw.loc[raw.series_id == "DGS10", field] = value
            self.assertEqual(assess_metrics(raw, now="2026-09-08").query("series_id == 'DGS10'").iloc[0].quality, "invalid")
        raw = complete_metrics()
        raw = pd.concat([raw, raw[raw.series_id == "DGS10"]])
        self.assertFalse(assess_metrics(raw, now="2026-09-08").query("series_id == 'DGS10'").iloc[0].eligible)
        scored = data.zscore_latest(pd.Series([1., 2.], index=pd.date_range("2026-01-01", periods=2)), "up")
        self.assertEqual(scored["current"], 2.)
        self.assertTrue(np.isnan(scored["signed_z"]))

    def test_export_is_strict_json_with_all_signals_and_null_overall(self):
        payload = build_latest_payload(pd.DataFrame(), pd.DataFrame(), [])
        self.assertIsNone(payload["score"]["current_stress"])
        self.assertEqual(payload["quality"]["status"], "degraded")
        self.assertEqual(len(payload["signals"]), len(expected_metrics()))
        self.assertEqual(payload["top_signals"], [])
        json.dumps(payload, allow_nan=False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latest.json"
            self.assertIsNone(write_latest_json(payload, str(path)))
            self.assertEqual(json.loads(path.read_text()), payload)

    def test_bis_codes_match_published_p_n_h_and_do_not_depend_on_labels(self):
        gap = pd.DataFrame({"BORROWERS_CTY:Borrowers' country": ["US: United States"] * 2,
                            "CG_DTYPE:Credit gap data type": ["A: changed label", "C: changed label"],
                            "TIME_PERIOD:Time period or range": ["2025-Q4"] * 2,
                            "OBS_VALUE:Observation Value": [140.25005140258, -11.5378]})
        dsr = pd.DataFrame({"BORROWERS_CTY:Borrowers' country": ["US: United States"] * 3,
                            "DSR_BORROWERS:Borrowers": ["P: Private", "N: Corporate", "H: Households"],
                            "TIME_PERIOD:Time period or range": ["2025-Q4"] * 3,
                            "OBS_VALUE:Observation Value": [14.1, 37.5, 8.0]})
        with patch("data._download_bis_flat_csv", side_effect=[gap, dsr]):
            rows, issues = data.fetch_bis_credit()
        self.assertEqual(len(rows), 5)
        self.assertEqual(issues, [])
        with patch("data._download_bis_flat_csv", side_effect=[gap, dsr.iloc[:1]]):
            _, issues = data.fetch_bis_credit()
        self.assertEqual(len(issues), 2)


if __name__ == "__main__":
    unittest.main()
