"""Regression tests for the 2026-10-24 expiry and future release transitions."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import data
import http_cache
import source_validation as validation
from quality import assess_metrics
from test_publication_freshness import RATIOS, incident_metrics
from test_reliability import complete_metrics, response


def calendar_metrics(now="2026-10-24T12:00:00Z", scheduled="2026-10-29"):
    raw = complete_metrics(now[:10])
    raw["observation_checked_at"] = now
    for key, value in {"publication_release_id": 263, "publication_next_release": scheduled,
                       "publication_calendar_checked_at": now, "publication_checked_at": now,
                       "publication_observation_end": "2026-01-01T00:00:00Z",
                       "publication_updated_at": "2026-06-25T13:01:00Z", "publication_frequency": "Q",
                       "date": pd.Timestamp("2026-01-01")}.items():
        raw.loc[raw.series_id.isin(RATIOS), key] = value
    raw.loc[raw.series_id == RATIOS[0], "current"] = 122.59387
    raw.loc[raw.series_id == RATIOS[1], "current"] = 98.71050
    return raw


class ReleaseCalendarTests(unittest.TestCase):
    def test_daily_expiry_is_replaced_by_recent_official_calendar_evidence(self):
        for now, scheduled in (("2026-10-24T12:00:00Z", "2026-10-29"),
                               ("2026-10-25T12:00:00Z", "2026-10-29"),
                               ("2026-11-01T12:00:00Z", "2026-11-25"),
                               ("2026-12-01T12:00:00Z", "2026-12-23")):
            with self.subTest(now=now):
                raw = calendar_metrics(now, scheduled)
                checked = assess_metrics(raw, now=now)
                self.assertTrue(checked.eligible.all())
                ratios = checked[checked.series_id.isin(RATIOS)]
                self.assertEqual(set(ratios.freshness_basis), {"verified_release_calendar"})
                self.assertEqual(ratios.current.tolist(), [122.59387, 98.71050])
                self.assertEqual(set(ratios.date), {pd.Timestamp("2026-01-01")})
                self.assertEqual(data.score_coverage(data.bucket_scores(checked), data.CURRENT_STRESS_BUCKETS), 1)
                pd.testing.assert_frame_equal(checked, assess_metrics(checked, now=now))

    def test_fresh_evidence_cannot_rescue_invalid_or_unscored_values(self):
        for key, value in (("risk_score", np.nan), ("risk_score", 101), ("current", np.inf)):
            raw = calendar_metrics()
            raw.loc[raw.series_id == RATIOS[0], key] = value
            self.assertFalse(assess_metrics(raw, now="2026-10-24T12:00:00Z").iloc[0].eligible)

    def test_calendar_cannot_be_wrong_expired_future_or_unreasonably_distant(self):
        for key, value in (("publication_release_id", 999), ("publication_next_release", "2027-12-31"),
                           ("publication_next_release", "2026-10-23"),
                           ("publication_calendar_checked_at", "2026-10-22T12:00:00Z"),
                           ("publication_calendar_checked_at", "2026-10-24T12:00:01Z"),
                           ("publication_observation_end", "2026-04-01")):
            raw = calendar_metrics()
            raw.loc[raw.series_id.isin(RATIOS), key] = value
            self.assertFalse(assess_metrics(raw, now="2026-10-24T12:00:00Z").iloc[0].eligible)

    def test_stale_reason_survives_reassessment_instead_of_becoming_unscored(self):
        first = assess_metrics(incident_metrics(False, checked="2026-10-24T12:00:00Z"), now="2026-10-24T12:00:00Z")
        self.assertEqual(first.iloc[0].quality, "stale")
        pd.testing.assert_frame_equal(first, assess_metrics(first, now="2026-10-24T12:00:00Z"))

    def test_release_delivery_window_uses_us_central_time_and_expires(self):
        raw = calendar_metrics("2027-02-25T12:00:00Z", "2027-02-25")
        before = assess_metrics(raw, now="2027-02-27T05:59:59Z")
        self.assertTrue(before.iloc[0].eligible)
        self.assertEqual(before.iloc[0].freshness_limit_at, pd.Timestamp("2027-02-27T06:00:00Z"))
        self.assertFalse(assess_metrics(raw, now="2027-02-27T06:00:00Z").iloc[0].eligible)

    def test_calendar_expiry_and_one_year_period_bound_are_enforced(self):
        raw = calendar_metrics()
        self.assertFalse(assess_metrics(raw, now="2026-10-26T12:00:00Z").iloc[0].eligible)
        old = calendar_metrics("2027-04-01T12:00:00Z", "2027-04-15")
        self.assertFalse(assess_metrics(old, now="2027-04-01T12:00:00Z").iloc[0].eligible)

    def test_calendar_parser_rejects_wrong_identity_duplicates_and_partial_results(self):
        row = {"release_id": 263, "date": "2026-10-29"}
        good = {"count": 1, "offset": 0, "release_dates": [row]}
        self.assertEqual(validation.fred_release_dates(good, 263)[0], pd.Timestamp("2026-10-29", tz="UTC"))
        for payload in ({**good, "count": 2}, {**good, "count": True}, {**good, "offset": False}, {**good, "offset": 1},
                        {**good, "release_dates": [{**row, "release_id": 999}]},
                        {**good, "release_dates": [{**row, "date": "invalid"}]},
                        {**good, "count": 2, "release_dates": [row, row]}):
            with self.assertRaises((data.DataUnavailable, ValueError)):
                validation.fred_release_dates(payload, 263)
        with self.assertRaises(data.DataUnavailable):
            validation.fred_release({"releases": [{"id": 999}]}, 263)

    def test_two_ratios_share_calendar_cache_without_request_on_every_export(self):
        body = json.dumps({"count": 1, "offset": 0, "release_dates": [{"release_id": 263, "date": "2026-10-29"}]}).encode()
        url = "https://api.stlouisfed.org/fred/release/dates"
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                "DEBT_RISK_RADAR_CACHE_DIR": folder, "DEBT_RISK_RADAR_READ_ONLY": "0"}), \
                patch("http_cache.requests.get", return_value=response(body=body)) as http, \
                patch("http_cache.time.time", return_value=1000):
            for _ in range(2):
                http_cache.get_json_document(url, ttl=21600, max_age=172800,
                    validator=lambda value: validation.fred_release_dates(value, 263), params={"release_id": 263})
            with patch("http_cache.time.time", return_value=1900):
                http_cache.get_json_document(url, ttl=21600, max_age=172800,
                    validator=lambda value: validation.fred_release_dates(value, 263), params={"release_id": 263})
            http.assert_called_once()

    def test_expanded_limits_keep_confirmed_quarters_without_calendar_during_outage(self):
        now = "2026-10-24T12:00:00Z"
        raw = calendar_metrics(now)
        raw = raw.drop(columns=["publication_next_release", "publication_calendar_checked_at", "publication_release_id"])
        checked = assess_metrics(raw, now=now)
        self.assertTrue(checked.eligible.all())
        self.assertEqual(checked.iloc[0].quality, "official_delayed")
        self.assertEqual(checked.iloc[0].freshness_basis, "verified_quarterly_publication")
        self.assertEqual(checked.iloc[0].freshness_limit_at, pd.Timestamp("2027-02-21T00:00:00Z"))


if __name__ == "__main__":
    unittest.main()
