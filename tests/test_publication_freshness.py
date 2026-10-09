"""Offline reproduction of the 2026-10-09 quarterly-age boundary incident."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

os.environ["DEBT_RISK_RADAR_DISABLE_STREAMLIT_CACHE"] = "1"

import numpy as np
import pandas as pd

import data
import http_cache
import latest_export
from quality import assess_metrics
from test_reliability import complete_metrics, response


NOW = "2026-10-09T05:20:00Z"
RATIOS = ("GFDEGDQ188S", "FYGFGDQ188S")


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 9, 5, 20, tzinfo=timezone.utc).astimezone(tz)


def incident_metrics(confirmed=True, checked=NOW):
    raw = complete_metrics("2026-10-09")
    for series_id, value in zip(RATIOS, (122.59387, 98.71050)):
        mask = raw.series_id == series_id
        raw.loc[mask, "date"] = pd.Timestamp("2026-01-01")
        raw.loc[mask, "current"] = value
        if confirmed:
            for key, value in {
                "publication_frequency": "Q", "publication_observation_end": "2026-01-01T00:00:00Z",
                "publication_updated_at": "2026-06-25T13:01:00Z",
                "publication_checked_at": checked, "observation_checked_at": checked,
            }.items():
                raw.loc[mask, key] = value
    return raw


class PublicationFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_incident_reproduced_without_confirmation(self):
        checked = assess_metrics(incident_metrics(False), now=NOW)
        self.assertEqual(checked.query("quality == 'stale'").series_id.tolist(), list(RATIOS))
        buckets = data.bucket_scores(checked)
        self.assertAlmostEqual(data.score_coverage(buckets, data.CURRENT_STRESS_BUCKETS), 0.872093023255814)
        self.assertTrue(np.isnan(data.current_stress_score(buckets)))

    def test_confirmed_publication_keeps_period_value_weights_and_explicit_delay(self):
        raw = incident_metrics()
        checked = assess_metrics(raw, now=NOW)
        self.assertTrue(checked.eligible.all())
        ratios = checked[checked.series_id.isin(RATIOS)]
        self.assertEqual(set(ratios.quality), {"official_delayed"})
        self.assertEqual(set(ratios.freshness_basis), {"verified_quarterly_publication"})
        self.assertEqual(set(ratios.date), {pd.Timestamp("2026-01-01")})
        self.assertEqual(set(ratios.observation_age_days), {281})
        self.assertEqual(ratios.current.tolist(), [122.59387, 98.71050])
        self.assertEqual(data.current_stress_score(data.bucket_scores(checked)), 50)

    def test_confirmation_cannot_override_other_invalid_data_or_extend_daily_series(self):
        for field, value in (("current", np.inf), ("risk_score", np.nan), ("risk_score", 101)):
            raw = incident_metrics()
            raw.loc[raw.series_id == RATIOS[0], field] = value
            self.assertFalse(assess_metrics(raw, now=NOW).query("series_id == @RATIOS[0]").iloc[0].eligible)
        raw = incident_metrics()
        raw.loc[raw.series_id == "DGS10", "date"] = pd.Timestamp("2026-09-01")
        self.assertFalse(assess_metrics(raw, now=NOW).query("series_id == 'DGS10'").iloc[0].eligible)

    def test_mismatched_missing_old_or_future_evidence_fails_closed(self):
        cases = (("publication_observation_end", "2026-04-01"), ("publication_frequency", "M"),
                 ("publication_updated_at", None), ("publication_updated_at", "2026-10-10"),
                 ("publication_updated_at", "2026-01-02"), ("publication_updated_at", "2026-06-01"),
                 ("publication_checked_at", "2026-10-08T05:20:00Z"),
                 ("publication_checked_at", "2026-10-09T05:21:00Z"),
                 ("observation_checked_at", "2026-10-08T23:20:00Z"),
                 ("observation_checked_at", "2026-10-10T05:20:00Z"),
                 ("observation_checked_at", "2026-06-24T05:20:00Z"))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                raw = incident_metrics()
                raw.loc[raw.series_id == RATIOS[0], field] = value
                row = assess_metrics(raw, now=NOW).query("series_id == 'GFDEGDQ188S'").iloc[0]
                self.assertEqual(row.quality, "stale")
                self.assertFalse(row.eligible)

    def test_revision_cannot_keep_an_old_quarter_alive(self):
        raw = incident_metrics()
        mask = raw.series_id.isin(RATIOS)
        raw.loc[mask, "date"] = pd.Timestamp("2025-10-01")
        raw.loc[mask, "publication_observation_end"] = "2025-10-01T00:00:00Z"
        raw.loc[mask, "publication_updated_at"] = "2026-10-08T00:00:00Z"
        self.assertFalse(assess_metrics(raw, now=NOW).loc[mask, "eligible"].any())

    def test_midnight_without_confirmation_is_a_real_deadline(self):
        raw = incident_metrics(False)
        before = assess_metrics(raw, now="2026-10-08T23:59:59Z")
        after = assess_metrics(raw, now="2026-10-09T00:00:00Z")
        mask = raw.series_id.isin(RATIOS)
        self.assertTrue(before.loc[mask, "eligible"].all())
        self.assertFalse(after.loc[mask, "eligible"].any())

    def test_pre_midnight_evidence_cannot_certify_beyond_its_own_expiry(self):
        raw = incident_metrics(checked="2026-10-08T18:00:00Z")
        checked = assess_metrics(raw, now="2026-10-08T23:59:59Z")
        row = checked.query("series_id == 'GFDEGDQ188S'").iloc[0]
        self.assertTrue(row.eligible)
        self.assertEqual(row.freshness_expires_at, pd.Timestamp("2026-10-09T00:00:00Z"))
        after = assess_metrics(raw, now="2026-10-09T00:00:00Z")
        self.assertFalse(after.query("series_id == 'GFDEGDQ188S'").iloc[0].eligible)

    def test_reassessing_a_delayed_metric_preserves_eligibility_and_evidence(self):
        first = assess_metrics(incident_metrics(), now=NOW)
        second = assess_metrics(first, now=NOW)
        pd.testing.assert_frame_equal(first, second)

    def test_publication_exception_also_expires_with_fresh_http_responses(self):
        for day, eligible in (("2026-10-23", True), ("2026-10-24", False)):
            now = day + "T05:20:00Z"
            checked = assess_metrics(incident_metrics(checked=now), now=now)
            self.assertEqual(checked.query("series_id == 'GFDEGDQ188S'").iloc[0].eligible, eligible)

    def test_export_preserves_delayed_status_evidence_and_expiry(self):
        with patch("latest_export.datetime", FixedDateTime):
            result = latest_export.build_latest_payload(incident_metrics(checked="2026-10-08T23:25:00Z"), pd.DataFrame(), [])
        self.assertEqual(result["quality"]["status"], "official-delayed")
        self.assertEqual(result["quality"]["delayed_signals"], list(RATIOS))
        self.assertEqual(result["score"]["eligible_signals"], 31)
        self.assertEqual(result["valid_until"], "2026-10-09T05:25:00Z")
        self.assertEqual(result["signals"][0]["date"], "2026-01-01")
        json.dumps(result, allow_nan=False)

    def test_export_warns_before_the_next_hard_age_limit(self):
        raw = incident_metrics()
        raw.loc[raw.series_id.isin(RATIOS), "publication_updated_at"] = "2026-06-24T13:01:00Z"
        with patch("latest_export.datetime", FixedDateTime):
            result = latest_export.build_latest_payload(raw, pd.DataFrame(), [])
        self.assertEqual([row["series_id"] for row in result["quality"]["expiring_signals"]], list(RATIOS))
        self.assertEqual(result["quality"]["expiring_signals"][0]["limit_at"], "2026-10-23T00:00:00+00:00")

    def test_observation_reader_does_not_fetch_metadata_for_recent_daily_data(self):
        payload = {"count": 1, "observations": [{"date": "2026-10-08", "value": "4.2"}]}
        with patch("data.fred_key_available", return_value=True), patch("data._streamlit_secret", return_value=None), \
                patch("data.iter_fred_catalog", return_value=[("rates_market", "DGS10", {})]), \
                patch("data.get_json_document", return_value=(payload, pd.Timestamp(NOW).timestamp())) as get, \
                patch("data._fred_publication", side_effect=AssertionError("Unnecessary metadata request")):
            series, issues = data.fetch_fred_series()
        self.assertEqual(issues, [])
        self.assertEqual(series["DGS10"].iloc[0], 4.2)
        get.assert_called_once()

    def test_fred_quarterly_metadata_reaches_score_and_export(self):
        dates = pd.date_range("2020-01-01", "2026-01-01", freq="QS")
        payload = {"count": len(dates), "observations": [
            {"date": date.date().isoformat(), "value": str(100 + i)} for i, date in enumerate(dates)]}
        metadata = {"seriess": [{"id": RATIOS[0], "frequency_short": "Q", "observation_end": "2026-01-01",
                                "last_updated": "2026-06-25 08:01:00-05"}]}
        fetched = pd.Timestamp(NOW).timestamp()
        with patch("data.fred_key_available", return_value=True), patch("data._streamlit_secret", return_value=None), \
                patch("data.iter_fred_catalog", return_value=[("fiscal", RATIOS[0], data.FRED_SERIES["fiscal"][RATIOS[0]])]), \
                patch("data.get_json_document", side_effect=[(payload, fetched), (metadata, fetched)]) as get:
            series, issues = data.fetch_fred_series()
            rows = data.fred_metrics(series)
        self.assertEqual(issues, [])
        self.assertEqual(get.call_count, 2)
        checked = assess_metrics(rows, now=NOW).query("series_id == 'GFDEGDQ188S'").iloc[0]
        self.assertEqual(checked.quality, "official_delayed")
        self.assertTrue(np.isfinite(checked.risk_score))

    def test_invalid_fred_duplicate_observations_are_rejected(self):
        payload = {"count": 2, "observations": [{"date": "2026-10-08", "value": "4.2"}] * 2}
        with patch("data.fred_key_available", return_value=True), patch("data._streamlit_secret", return_value=None), \
                patch("data.iter_fred_catalog", return_value=[("rates_market", "DGS10", {})]), \
                patch("data.get_json_document", return_value=(payload, pd.Timestamp(NOW).timestamp())):
            series, issues = data.fetch_fred_series()
        self.assertEqual(series, {})
        self.assertIn("duplicate", issues[0].detail)

    def test_cache_hit_preserves_original_retrieval_time(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "DEBT_RISK_RADAR_CACHE_DIR": folder, "DEBT_RISK_RADAR_READ_ONLY": "0",
        }), patch("http_cache.requests.get", return_value=response()) as http, \
                patch("http_cache.time.time", return_value=1000):
            first = http_cache.get_json_document("https://api.stlouisfed.org/fred/series", ttl=86400)
            with patch("http_cache.time.time", return_value=2000), \
                    patch.dict(os.environ, {"DEBT_RISK_RADAR_READ_ONLY": "1"}):
                self.assertEqual(http_cache.get_json_document("https://api.stlouisfed.org/fred/series", ttl=86400), first)
            self.assertEqual(first[1], 1000)
            http.assert_called_once()

    def test_metadata_parser_rejects_wrong_identity_and_newer_uncollected_period(self):
        series = pd.Series([122.59387], index=pd.to_datetime(["2026-01-01"]))
        meta = dict(id=RATIOS[0], frequency_short="Q", observation_end="2026-01-01", last_updated="2026-06-25 08:01:00-05")
        for field, value in (("id", "GDP"), ("frequency_short", "M"), ("observation_end", "2026-04-01"), ("last_updated", "invalid")):
            with self.subTest(field=field), patch("data.get_json_document", return_value=({"seriess": [dict(meta, **{field: value})]}, 1_791_520_000)):
                with self.assertRaises(data.DataUnavailable):
                    data._fred_publication(RATIOS[0], series, None)


if __name__ == "__main__":
    unittest.main()
