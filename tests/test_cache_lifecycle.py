"""Offline cache lifecycle and outage simulations; every provider response is synthetic."""

import json
import io
import os
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd

os.environ["DEBT_RISK_RADAR_DISABLE_STREAMLIT_CACHE"] = "1"

import data
import http_cache
import latest_export
import source_validation
from quality import assess_metrics
from test_reliability import complete_metrics, response


def annual_payload(indicator, country="USA"):
    rows = [{"indicator": {"id": indicator}, "countryiso3code": country,
             "date": str(year), "value": float(10 + (year - 2010) / 2)} for year in range(2010, 2026)]
    return [{"page": 1, "pages": 1, "total": len(rows)}, rows]


class CacheLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now = pd.Timestamp("2026-10-08T12:00:00Z").timestamp()
        self.start = self.now
        self.calls = []
        self.failed = False
        self.payload_override = None
        self.env = patch.dict(os.environ, {"DEBT_RISK_RADAR_CACHE_DIR": self.folder.name,
            "DEBT_RISK_RADAR_READ_ONLY": "0", "DEBT_RISK_RADAR_DISABLE_STREAMLIT_CACHE": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        for mocked in (patch("http_cache.time.time", side_effect=lambda: self.now),
                       patch("http_cache.time.sleep", side_effect=self.sleep),
                       patch("http_cache.requests.get", side_effect=self.fetch),
                       patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))):
            mocked.start()
            self.addCleanup(mocked.stop)

    def sleep(self, seconds):
        self.now += seconds

    def fetch(self, url, **kwargs):
        self.calls.append((self.now, url))
        if self.failed:
            raise http_cache.requests.Timeout("test-only private request details")
        indicator = url.rsplit("/", 1)[-1]
        payload = self.payload_override if self.payload_override is not None else annual_payload(indicator)
        return response(json.dumps(payload).encode())

    def snapshot(self, frame):
        clock = self
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(clock.now, timezone.utc).astimezone(tz)
        rest = complete_metrics(pd.Timestamp(self.now, unit="s", tz="UTC").date().isoformat())
        rest = rest[rest.bucket != "world_bank"].copy()
        rest["observation_checked_at"] = pd.Timestamp(self.now, unit="s", tz="UTC").isoformat()
        metrics = pd.concat([rest, data.world_bank_metrics(frame)], ignore_index=True)
        with patch("latest_export.datetime", FrozenDateTime):
            return latest_export.build_latest_payload(metrics, pd.DataFrame(), [], http_cache.collection_status())

    def test_world_bank_timeout_across_daily_cache_expiry_preserves_valid_observations(self):
        first, issues = data.fetch_world_bank()
        self.assertEqual(issues, [])
        self.failed = True
        self.now += 2 * 86400
        reused, issues = data.fetch_world_bank()
        self.assertEqual(issues, [])
        pd.testing.assert_frame_equal(first, reused)
        payload = self.snapshot(reused)
        self.assertEqual(payload["score"]["eligible_signals"], 31)
        self.assertEqual(payload["score"]["coverage"], 1)
        self.assertIsNotNone(payload["score"]["current_stress"])
        self.assertEqual(payload["quality"]["status"], "cached")
        self.assertEqual(set(payload["quality"]["cached_signals"]), set(data.WORLD_BANK_INDICATORS))
        self.assertEqual(len(self.calls), 5)  # One failed call pauses the provider, not four retries.

    def test_seven_days_of_failure_have_bounded_attempts_and_a_real_hard_expiry(self):
        original, _ = data.fetch_world_bank()
        self.failed = True
        # 15-minute timer ticks through six days, including restarts/read-only visits.
        for tick in range(1, 6 * 96 + 1):
            self.now = self.start + tick * 900 + 10
            frame, issues = data.fetch_world_bank()
            self.assertEqual(issues, [])
            pd.testing.assert_frame_equal(original, frame)
            before = len(self.calls)
            with patch.dict(os.environ, {"DEBT_RISK_RADAR_READ_ONLY": "1"}):
                reader, _ = data.fetch_world_bank()
            self.assertEqual(len(self.calls), before)
            pd.testing.assert_frame_equal(original, reader)
        self.assertLessEqual(len(self.calls), 35)
        self.now = self.start + 7 * 86400 + 10
        expired, issues = data.fetch_world_bank()
        self.assertTrue(expired.empty)
        self.assertEqual(len(issues), 4)
        payload = self.snapshot(expired)
        self.assertIsNone(payload["score"]["current_stress"])
        self.assertEqual(payload["score"]["eligible_signals"], 27)

    def test_provider_recovers_without_resetting_cache_or_backoff(self):
        original, _ = data.fetch_world_bank()
        self.failed = True
        self.now += 2 * 86400
        data.fetch_world_bank()
        self.failed = False
        self.now += 901
        frame, issues = data.fetch_world_bank()
        self.assertEqual(issues, [])
        self.assertTrue((frame.observation_checked_at > original.observation_checked_at).all())
        self.assertEqual(self.snapshot(frame)["quality"]["status"], "ok")

    def test_invalid_success_response_does_not_poison_the_last_validated_cache(self):
        original, _ = data.fetch_world_bank()
        with closing(sqlite3.connect(http_cache.cache_path())) as db:
            before = db.execute("SELECT key,fetched,body FROM responses ORDER BY key").fetchall()
        self.payload_override = [{"page": 1, "pages": 1, "total": 1}, [{"countryiso3code": "FRA"}]]
        self.now += 86400
        frame, issues = data.fetch_world_bank()
        self.assertEqual(issues, [])
        pd.testing.assert_frame_equal(original, frame)
        with closing(sqlite3.connect(http_cache.cache_path())) as db:
            self.assertEqual(before, db.execute("SELECT key,fetched,body FROM responses ORDER BY key").fetchall())
        self.assertEqual(http_cache.collection_status()["providers"][0]["reason"], "invalid_response")

    def test_source_date_expiry_is_not_extended_by_cache_grace(self):
        frame, _ = data.fetch_world_bank()
        frame["date"] = pd.Timestamp("2020-12-31")
        rows = assess_metrics(data.world_bank_metrics(frame), now=pd.Timestamp(self.now, unit="s", tz="UTC"))
        self.assertFalse(rows.query("bucket == 'world_bank'").eligible.any())

    def test_missing_invalid_and_projection_dates_are_json_null_not_nat(self):
        payload = latest_export.build_latest_payload(pd.DataFrame(), pd.DataFrame(), [])
        self.assertNotIn('"NaT"', json.dumps(payload, allow_nan=False))

    def test_empty_wrong_country_wrong_series_paginated_and_duplicate_payloads_rejected(self):
        indicator = next(iter(data.WORLD_BANK_INDICATORS))
        for mutate in (
            lambda p: p[0].update(pages=2),
            lambda p: p[0].update(total=99),
            lambda p: p[1][0].update(countryiso3code="FRA"),
            lambda p: p[1][0].update(indicator={"id": "OTHER"}),
            lambda p: p[1][0].update(date=p[1][1]["date"]),
            lambda p: p[1][0].update(value=float("inf")),
            lambda p: p[1][0].update(date="2099"),
        ):
            payload = annual_payload(indicator)
            mutate(payload)
            with self.assertRaises((http_cache.DataUnavailable, ValueError, KeyError)):
                source_validation.world_bank(payload, "USA", indicator)

    def test_retry_after_503_is_respected_across_process_reads(self):
        url = "https://api.worldbank.org/v2/country/USA/indicator/TEST"
        with patch("http_cache.requests.get", return_value=response(status=503, headers={"Retry-After": "7200"})) as get:
            for offset in (0, 900, 3600, 7199):
                self.now = self.start + offset
                with self.assertRaises(http_cache.DataUnavailable):
                    http_cache.get_json(url, ttl=86400, max_age=7 * 86400)
            get.assert_called_once()

    def test_expiry_during_cache_validation_cannot_leak_an_expired_response(self):
        url = "https://api.worldbank.org/v2/test"
        with patch("http_cache.requests.get", return_value=response(b'{}')):
            http_cache.get_json(url, ttl=100, max_age=200)
        self.now = self.start + 199
        def slow_validation(payload):
            self.now += 2
        with patch.dict(os.environ, {"DEBT_RISK_RADAR_READ_ONLY": "1"}), self.assertRaises(http_cache.DataUnavailable):
            http_cache.get_json(url, ttl=100, max_age=200, validator=slow_validation)

    def test_invalid_json_success_preserves_cache_without_renewing_its_date(self):
        url = "https://api.worldbank.org/v2/test"
        with patch("http_cache.requests.get", return_value=response(b'{"value": 1}')):
            original = http_cache.get_json_document(url, ttl=100, max_age=86400)
        for body in (b'<html>Error</html>', b'{"value": NaN}', b'{"value": Infinity}'):
            self.now += 7200
            with patch("http_cache.requests.get", return_value=response(body)):
                self.assertEqual(original, http_cache.get_json_document(url, ttl=100, max_age=86400))

    def test_export_expiry_never_outlives_the_first_source_cache(self):
        frame, _ = data.fetch_world_bank()
        self.now = self.start + 7 * 86400 - 120
        payload = self.snapshot(frame)
        self.assertIsNotNone(payload["score"]["current_stress"])
        self.assertEqual(pd.Timestamp(payload["valid_until"]).timestamp(), self.start + 7 * 86400)
        self.assertEqual(len(payload["quality"]["cache_expiring_signals"]), 4)


class ProviderValidationTests(unittest.TestCase):
    def test_treasury_schema_and_numeric_values(self):
        payload = {"meta": {"total-pages": 1}, "data": [{"record_date": "2026-10-01",
            "debt_held_public_amt": "20.0", "intragov_hold_amt": "10.0", "tot_pub_debt_out_amt": "30.0"}]}
        self.assertEqual(source_validation.treasury(payload).tot_pub_debt_out_amt.iloc[0], 30)
        payload["data"][0]["tot_pub_debt_out_amt"] = "NaN"
        with self.assertRaises((http_cache.DataUnavailable, ValueError)):
            source_validation.treasury(payload)

    def test_bis_both_archives_validate_us_codes_and_duplicate_periods(self):
        for kind, codes in (("CG_DTYPE:Credit gap data type", ["A", "C"]),
                            ("DSR_BORROWERS:Borrowers", ["P", "N", "H"])):
            rows = [{"BORROWERS_CTY:Borrowers' country": "US:United States", kind: code,
                     "TIME_PERIOD:Time period or range": "2026-Q1", "OBS_VALUE:Observation Value": 10}
                    for code in codes]
            def archive(records):
                out = io.BytesIO()
                with zipfile.ZipFile(out, "w") as zipped:
                    zipped.writestr("data.csv", pd.DataFrame(records).to_csv(index=False))
                return out.getvalue()
            self.assertEqual(len(source_validation.bis_archive(archive(rows))), len(codes))
            with self.assertRaises(http_cache.DataUnavailable):
                source_validation.bis_archive(archive(rows + [rows[0]]))

    def test_cbo_pinned_projections_allow_future_horizons_but_reject_missing_variables(self):
        variables = data.CBO_DATASETS["long_term_budget"]["variables"]
        body = pd.DataFrame([{"date": "FY2056", "variable": variable, "value": 20}
                             for variable in variables]).to_csv(index=False).encode()
        parsed = source_validation.cbo_csv(body, variables)
        self.assertTrue((parsed.date == pd.Timestamp("2056-09-30")).all())
        with self.assertRaises(http_cache.DataUnavailable):
            source_validation.cbo_csv(body, [*variables, "MISSING"])


if __name__ == "__main__":
    unittest.main()
