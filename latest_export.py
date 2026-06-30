"""
Machine-readable export for Debt Risk Radar.

Run with:
    python latest_export.py --output /var/www/debt-risk-radar/latest.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from catalog import BUCKET_LABELS, STRESS_LEVEL, WATCH_LEVEL

if Path(sys.argv[0]).name == "latest_export.py":
    os.environ.setdefault("DEBT_RISK_RADAR_DISABLE_STREAMLIT_CACHE", "1")

from data import (
    DataIssue,
    bis_credit_metrics,
    bucket_scores,
    cbo_projection_metrics,
    combine_metrics,
    fetch_bis_credit,
    fetch_cbo_projections,
    fetch_fred_series,
    fetch_massive_market,
    fetch_treasury_debt,
    fetch_world_bank,
    fred_metrics,
    massive_market_metrics,
    overall_score,
    score_label,
    treasury_daily_metrics,
    world_bank_metrics,
)


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(value, minimum)
    return value


AUTO_REFRESH_SECONDS = env_int("DEBT_RISK_RADAR_AUTO_REFRESH_SECONDS", 15 * 60, minimum=60)
LATEST_JSON_PATH = os.environ.get("DEBT_RISK_RADAR_LATEST_JSON", "/var/www/debt-risk-radar/latest.json")
LATEST_JSON_TOP_SIGNALS = env_int("DEBT_RISK_RADAR_LATEST_JSON_TOP_SIGNALS", 20, minimum=1)
DEFAULT_COUNTRY = "USA"
DEFAULT_FRED_START = "1990-01-01"
DEFAULT_TREASURY_START = "2015-01-01"


def json_value(value):
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        return value if np.isfinite(value) else None
    if pd.isna(value):
        return None
    return value


def json_date(value) -> str | None:
    if value is None or pd.isna(value):
        return None
    timestamp = pd.Timestamp(value)
    return timestamp.date().isoformat()


def metric_record(row: pd.Series) -> dict:
    return {
        "bucket": str(row["bucket"]),
        "family": BUCKET_LABELS.get(str(row["bucket"]), str(row["bucket"])),
        "series_id": str(row["series_id"]),
        "name": str(row["name"]),
        "unit": str(row["unit"]),
        "date": json_date(row["date"]),
        "current": json_value(float(row["current"])) if pd.notna(row["current"]) else None,
        "signed_z": json_value(float(row["signed_z"])) if pd.notna(row["signed_z"]) else None,
        "risk_score": json_value(float(row["risk_score"])) if pd.notna(row["risk_score"]) else None,
        "source": str(row["source"]),
        "rationale": str(row["rationale"]),
    }


def load_metric_snapshot(
    country: str = DEFAULT_COUNTRY,
    fred_start: str = DEFAULT_FRED_START,
    treasury_start: str = DEFAULT_TREASURY_START,
) -> tuple[pd.DataFrame, pd.DataFrame, list[DataIssue]]:
    treasury_df, treasury_issues = fetch_treasury_debt(str(treasury_start))
    fred_data, fred_issues = fetch_fred_series(str(fred_start))
    wb_df, wb_issues = fetch_world_bank(country)
    bis_df, bis_issues = fetch_bis_credit(country)
    cbo_df, cbo_issues = fetch_cbo_projections()
    massive_data, massive_issues = fetch_massive_market()
    issues = treasury_issues + fred_issues + wb_issues + bis_issues + cbo_issues + massive_issues

    metrics = combine_metrics(
        treasury_daily_metrics(treasury_df),
        fred_metrics(fred_data),
        world_bank_metrics(wb_df),
        bis_credit_metrics(bis_df),
        cbo_projection_metrics(cbo_df),
        massive_market_metrics(massive_data),
    )
    buckets = bucket_scores(metrics)
    return metrics, buckets, issues


def build_latest_payload(metrics: pd.DataFrame, buckets: pd.DataFrame, issues: list[DataIssue]) -> dict:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    overall = overall_score(buckets)
    source_rows = []
    if not metrics.empty:
        source_audit = (
            metrics.groupby("source")
            .agg(metrics=("series_id", "count"), latest_date=("date", "max"), max_risk=("risk_score", "max"))
            .reset_index()
            .sort_values("metrics", ascending=False)
        )
        for _, row in source_audit.iterrows():
            source_rows.append(
                {
                    "source": str(row["source"]),
                    "metrics": int(row["metrics"]),
                    "latest_date": json_date(row["latest_date"]),
                    "max_risk": json_value(float(row["max_risk"])) if pd.notna(row["max_risk"]) else None,
                }
            )

    bucket_rows = []
    if not buckets.empty:
        for _, row in buckets.sort_values("score", ascending=False).iterrows():
            score = float(row["score"]) if pd.notna(row["score"]) else np.nan
            bucket_rows.append(
                {
                    "bucket": str(row["bucket"]),
                    "label": BUCKET_LABELS.get(str(row["bucket"]), str(row["bucket"])),
                    "score": json_value(score),
                    "status": score_label(score),
                    "weight": json_value(float(row["weight"])) if pd.notna(row["weight"]) else None,
                    "metrics": int(row["n"]),
                }
            )

    top_rows = []
    if not metrics.empty:
        top_metrics = metrics.sort_values("risk_score", ascending=False).head(LATEST_JSON_TOP_SIGNALS)
        top_rows = [metric_record(row) for _, row in top_metrics.iterrows()]

    return {
        "schema_version": "1.0",
        "name": "Debt Risk Radar",
        "description": "Machine-readable snapshot of the public US debt risk dashboard.",
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "public_url": "https://debt.l0g.fr/",
        "latest_json_url": "https://debt.l0g.fr/latest.json",
        "scope": {
            "country": DEFAULT_COUNTRY,
            "focus": "US sovereign debt, fiscal projections, private credit, liquidity and market stress.",
            "market_data": "FRED and Massive Market Data are used when server-side keys are configured.",
        },
        "thresholds": {
            "watch": WATCH_LEVEL,
            "stress": STRESS_LEVEL,
        },
        "refresh": {
            "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
            "source_ttl_seconds": {
                "market": 6 * 3600,
                "institutional": 24 * 3600,
            },
        },
        "score": {
            "overall": json_value(float(overall)) if pd.notna(overall) else None,
            "status": score_label(overall),
            "buckets": bucket_rows,
        },
        "top_signals": top_rows,
        "sources": source_rows,
        "issues": [{"source": issue.source, "detail": issue.detail} for issue in issues],
    }


def write_latest_json(payload: dict, output_path: str = LATEST_JSON_PATH) -> DataIssue | None:
    if not output_path:
        return None

    path = Path(output_path)
    tmp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as tmp:
            tmp_name = tmp.name
            json.dump(payload, tmp, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            tmp.write("\n")
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
        os.chmod(path, 0o644)
    except Exception:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return DataIssue("latest.json", "Public JSON export was skipped; check output path permissions.")
    return None


def generate_latest_json(output_path: str = LATEST_JSON_PATH) -> tuple[dict, DataIssue | None]:
    metrics, buckets, issues = load_metric_snapshot()
    payload = build_latest_payload(metrics, buckets, issues)
    return payload, write_latest_json(payload, output_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the public Debt Risk Radar JSON snapshot.")
    parser.add_argument("--output", default=LATEST_JSON_PATH, help="Output path for latest.json.")
    args = parser.parse_args()

    payload, issue = generate_latest_json(args.output)
    if issue:
        print(f"{issue.source}: {issue.detail}")
        return 1
    print(
        json.dumps(
            {
                "output": args.output,
                "generated_at": payload["generated_at"],
                "overall": payload["score"]["overall"],
                "status": payload["score"]["status"],
                "top_signals": len(payload["top_signals"]),
                "sources": len(payload["sources"]),
                "issues": len(payload["issues"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
