"""Method 2.0 contract and isolated UI/export tests; no provider traffic."""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

os.environ["DEBT_RISK_RADAR_DISABLE_STREAMLIT_CACHE"] = "1"

import numpy as np
import pandas as pd

import data
import http_cache
import latest_export
from catalog import ACTIVE_SOURCE_HOSTS, CURRENT_BUCKET_WEIGHTS, CURRENT_STRESS_BUCKETS
from quality import assess_metrics, expected_metrics
from test_reliability import complete_metrics

ROOT = Path(__file__).resolve().parents[1]


class InstitutionalMethodTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Network forbidden in tests")))
        self.today = pd.Timestamp.now(tz="UTC").date().isoformat()

    def test_catalog_and_explicit_normalized_weights(self):
        catalog = expected_metrics()
        self.assertEqual(len(catalog), 35)
        self.assertEqual(sum(bucket in CURRENT_STRESS_BUCKETS for bucket, _ in catalog), 31)
        self.assertEqual(len(CURRENT_STRESS_BUCKETS), 7)
        self.assertNotIn("market_prices", CURRENT_STRESS_BUCKETS)
        expected = dict(fiscal=22, rates_market=18, private_leverage=12, liquidity=10,
                        treasury_daily=10, world_bank=4, global_credit=10)
        self.assertAlmostEqual(sum(CURRENT_BUCKET_WEIGHTS.values()), 1)
        for bucket, weight in expected.items():
            self.assertAlmostEqual(CURRENT_BUCKET_WEIGHTS[bucket], weight / 86)

    def test_known_score_with_nonuniform_inputs_and_no_cbo_weight(self):
        raw = complete_metrics(self.today)
        values = dict(fiscal=10, rates_market=20, private_leverage=30, liquidity=40,
                      treasury_daily=50, world_bank=60, global_credit=70, cbo_projection=100)
        raw["risk_score"] = raw.bucket.map(values)
        score = data.current_stress_score(data.bucket_scores(assess_metrics(raw)))
        self.assertAlmostEqual(score, (10*22 + 20*18 + 30*12 + 40*10 + 50*10 + 60*4 + 70*10) / 86)

    def test_each_required_signal_still_suspends_score_when_missing(self):
        raw = complete_metrics(self.today)
        for (bucket, series_id) in expected_metrics():
            if bucket not in CURRENT_STRESS_BUCKETS:
                continue
            with self.subTest(series_id=series_id):
                assessed = assess_metrics(raw[raw.series_id != series_id])
                self.assertTrue(np.isnan(data.current_stress_score(data.bucket_scores(assessed))))

    def test_retired_rows_never_change_quality_score_or_top_signals(self):
        raw = complete_metrics(self.today)
        legacy = raw.iloc[:1].copy()
        legacy["bucket"], legacy["series_id"] = "market_prices", "SPY"
        for score in (0, 100, np.nan):
            with self.subTest(score=score):
                legacy["risk_score"] = score
                payload = latest_export.build_latest_payload(pd.concat([raw, legacy]), pd.DataFrame(), [])
                self.assertEqual(payload["quality"]["status"], "ok")
                self.assertEqual(payload["quality"]["eligible_signals"], 35)
                self.assertAlmostEqual(payload["score"]["current_stress"], 50)
                self.assertTrue(all(row["bucket"] != "market_prices" for row in payload["signals"] + payload["top_signals"]))

    def test_export_contract_versions_method_separately_from_schema(self):
        payload = latest_export.build_latest_payload(complete_metrics(self.today), pd.DataFrame(), [])
        self.assertEqual(payload["schema_version"], "1.2")
        self.assertEqual(payload["methodology"]["id"], "us-debt-institutional")
        self.assertEqual(payload["methodology"]["version"], "2.0")
        self.assertFalse(payload["methodology"]["comparable_with_previous_method"])
        self.assertEqual(payload["methodology"]["retired_buckets"], ["market_prices"])
        self.assertEqual(payload["score"]["expected_signals"], 31)
        self.assertEqual(payload["score"]["eligible_signals"], 31)
        self.assertEqual(payload["score"]["excluded_buckets"], ["cbo_projection"])
        self.assertAlmostEqual(sum(row["current_weight"] for row in payload["score"]["buckets"]), 1)
        self.assertTrue(all(row["current_weight"] == 0 for row in payload["score"]["buckets"] if row["bucket"] == "cbo_projection"))
        json.dumps(payload, allow_nan=False)

    def test_revision_marker_is_validated_without_guessing_local_head(self):
        for value, expected in (("a" * 40 + "\n", "a" * 40), ("not-a-release", None), ("b" * 41, None)):
            with self.subTest(value=value), patch("latest_export.Path.read_text", return_value=value):
                self.assertEqual(latest_export.source_revision(), expected)
        with patch("latest_export.Path.read_text", side_effect=FileNotFoundError):
            self.assertIsNone(latest_export.source_revision())

    def seed_pauses(self, directory):
        self.stack.enter_context(patch.dict(os.environ, {"DEBT_RISK_RADAR_CACHE_DIR": directory, "DEBT_RISK_RADAR_READ_ONLY": "0"}))
        with closing(http_cache._connect()) as db, db:
            db.execute("INSERT INTO providers VALUES (?, ?, ?)", ("api.massive.com", time.time(), time.time() + 21600))
            db.execute("INSERT INTO provider_failures VALUES (?, ?)", ("api.massive.com", "rate_limit"))

    def test_retired_pause_filtered_without_deleting_state_or_hiding_fred_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            self.seed_pauses(directory)
            self.assertEqual(http_cache.collection_status(ACTIVE_SOURCE_HOSTS), {"status": "ok", "providers": []})
            self.assertEqual(http_cache.collection_status()["status"], "paused")
            with closing(sqlite3.connect(http_cache.cache_path())) as db, db:
                db.execute("INSERT INTO providers VALUES (?, ?, ?)", ("api.stlouisfed.org", time.time(), time.time() + 900))
            state = http_cache.collection_status(ACTIVE_SOURCE_HOSTS)
            self.assertEqual(state["status"], "paused")
            self.assertEqual([p["source"] for p in state["providers"]], ["FRED"])
            self.assertEqual(len(http_cache.collection_status()["providers"]), 2)

    def mock_feeds(self, module, raw=None):
        """Synthetic complete metrics with empty chart data, only in the test process."""
        self.stack.enter_context(patch.dict(os.environ, {"MASSIVE_API_KEY": "test-only"}))
        self.stack.enter_context(patch("data.fetch_massive_market", side_effect=AssertionError("Retired connector called")))
        raw = complete_metrics(self.today) if raw is None else raw
        specs = (
            ("treasury_debt", "treasury_daily", ["treasury_daily"]),
            ("fred_series", "fred", ["fiscal", "rates_market", "private_leverage", "liquidity"]),
            ("world_bank", "world_bank", ["world_bank"]),
            ("bis_credit", "bis_credit", ["global_credit"]),
            ("cbo_projections", "cbo_projection", ["cbo_projection"]),
        )
        for fetch, transform, buckets in specs:
            chart_data = pd.DataFrame()
            if fetch == "fred_series":
                chart_data = {series: pd.Series([4.0, 4.1, 4.2], index=pd.date_range(end=self.today, periods=3))
                              for series in ("DGS10", "BAMLC0A0CM", "BAMLH0A0HYM2")}
            self.stack.enter_context(patch(f"{module}.fetch_{fetch}", return_value=(chart_data, [])))
            self.stack.enter_context(patch(f"{module}.{transform}_metrics", return_value=raw[raw.bucket.isin(buckets)]))

    def test_scheduled_export_makes_no_massive_call_with_key_and_old_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            self.seed_pauses(directory)
            self.mock_feeds("latest_export")
            target = Path(directory) / "latest.json"
            payload, issue = latest_export.generate_latest_json(str(target))
            self.assertIsNone(issue)
            self.assertEqual(payload["collection"]["status"], "ok")
            self.assertEqual(payload["quality"]["status"], "ok")
            self.assertEqual(json.loads(target.read_text()), payload)

    def test_dashboard_and_faq_render_without_retired_connector(self):
        from streamlit.testing.v1 import AppTest

        with tempfile.TemporaryDirectory() as directory:
            self.seed_pauses(directory)
            self.mock_feeds("data")
            app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=20)
            self.assertEqual(len(app.exception), 0)
            markup = "\n".join(item.value for item in app.markdown)
            self.assertIn("Méthode 2.0", markup)
            self.assertIn("Taux / crédit", markup)
            self.assertFalse(any("Qualité des données" in item.value for item in app.warning))
            charts = [json.loads(chart.proto.spec) for chart in app.get("plotly_chart")]
            market = next(chart for chart in charts if "FRED" in chart["layout"].get("title", {}).get("text", ""))
            self.assertEqual([trace["name"] for trace in market["data"]],
                             ["Treasury 10 ans (%)", "IG OAS (points de %)", "HY OAS (points de %)"])
            self.assertNotIn("yaxis2", market["layout"])
            self.assertEqual(market["layout"]["yaxis"]["title"]["text"], "Taux (%) / spread (points de %)")
            app.query_params["view"] = "faq"
            app.run(timeout=20)
            self.assertEqual(len(app.exception), 0)
            markup = "\n".join(item.value for item in app.markdown)
            self.assertIn("31 signaux", markup)
            self.assertIn("rompt la comparabilité", markup)

    def test_dashboard_explains_confirmed_delayed_publication_without_calling_it_unavailable(self):
        from streamlit.testing.v1 import AppTest
        from test_publication_freshness import incident_metrics, NOW

        with tempfile.TemporaryDirectory() as directory:
            self.seed_pauses(directory)
            self.mock_feeds("data", incident_metrics())
            self.stack.enter_context(patch("data.assess_metrics", side_effect=lambda rows: assess_metrics(rows, now=NOW)))
            app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=20)
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(any("Publication officielle différée" in item.value for item in app.info))
            self.assertFalse(any("Qualité des données dégradée" in item.value for item in app.warning))
            markup = "\n".join(item.value for item in app.markdown)
            self.assertIn("2026-01-01", markup)
            self.assertIn("2026-06-25", markup)

    def test_dashboard_labels_reused_cache_and_keeps_its_original_timestamp(self):
        from streamlit.testing.v1 import AppTest
        raw = complete_metrics(self.today)
        now = pd.Timestamp.now(tz="UTC")
        raw["observation_checked_at"] = now.isoformat()
        original = (now - pd.Timedelta(days=2)).isoformat()
        raw.loc[raw.bucket == "world_bank", "observation_checked_at"] = original
        with tempfile.TemporaryDirectory() as directory:
            self.seed_pauses(directory)
            self.mock_feeds("data", raw)
            app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=20)
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(any("Cache source validé" in item.value for item in app.info))
            self.assertFalse(any("Qualité des données dégradée" in item.value for item in app.warning))
            self.assertIn(original, "\n".join(item.value for item in app.markdown))


if __name__ == "__main__":
    unittest.main()
