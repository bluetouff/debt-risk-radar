"""
Machine-readable export for Debt Risk Radar.

Run with:
    python latest_export.py --output /var/www/debt-risk-radar/latest.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from catalog import (
    ACTIVE_SOURCE_HOSTS, BUCKET_LABELS, CURRENT_BUCKET_WEIGHTS, CURRENT_STRESS_BUCKETS,
    METHODOLOGY_DESCRIPTION, METHODOLOGY_ID, METHODOLOGY_VERSION,
    STRESS_LEVEL, WATCH_LEVEL, STRUCTURAL_BUCKETS,
)
from quality import assess_metrics, FRESHNESS_POLICY_VERSION, PUBLICATION_WARNING_DAYS
from http_cache import collection_status

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
    fetch_treasury_debt,
    fetch_world_bank,
    fred_metrics,
    current_stress_score,
    score_label,
    score_coverage,
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


def source_revision() -> str | None:
    """Report only the release marker installed by the verified activation."""
    try:
        revision = Path(__file__).with_name("DEPLOYED_SHA").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return revision if re.fullmatch(r"[a-f0-9]{40}", revision) else None


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
        "quality": str(row.get("quality", "unknown")),
        "quality_detail": str(row.get("quality_detail", "")),
        "eligible": bool(row.get("eligible", False)),
        "frequency": str(row.get("frequency", "unknown")),
        "observation_age_days": json_value(row.get("observation_age_days")),
        "max_age_days": json_value(row.get("max_age_days")),
        "freshness_basis": row.get("freshness_basis", "observation_age"),
        "freshness_expires_at": json_value(row.get("freshness_expires_at")),
        "freshness_limit_at": json_value(row.get("freshness_limit_at")),
        **{key: json_value(row.get(key)) for key in (
            "observation_checked_at", "publication_observation_end", "publication_updated_at",
            "publication_checked_at", "publication_frequency")},
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
    issues = treasury_issues + fred_issues + wb_issues + bis_issues + cbo_issues

    metrics = combine_metrics(
        treasury_daily_metrics(treasury_df),
        fred_metrics(fred_data),
        world_bank_metrics(wb_df),
        bis_credit_metrics(bis_df),
        cbo_projection_metrics(cbo_df),
    )
    buckets = bucket_scores(metrics)
    return metrics, buckets, issues


def build_latest_payload(metrics: pd.DataFrame, buckets: pd.DataFrame, issues: list[DataIssue],
                         collection: dict | None = None) -> dict:
    generated_at = datetime.now(timezone.utc)
    metrics = assess_metrics(metrics, now=generated_at)
    buckets = bucket_scores(metrics)
    overall = current_stress_score(buckets)
    current_coverage = score_coverage(buckets, expected_buckets=CURRENT_STRESS_BUCKETS)
    structural_buckets = buckets[buckets["bucket"].isin(STRUCTURAL_BUCKETS)] if not buckets.empty else pd.DataFrame()
    source_rows = []
    if not metrics.empty:
        for source, sub in metrics.groupby("source"):
            observed = sub[sub["frequency"] != "projection"]
            source_rows.append(
                {
                    "source": str(source),
                    "metrics": int(sub["eligible"].sum()),
                    "expected_metrics": len(sub),
                    "latest_date": json_date(observed["date"].max()),
                    "oldest_date": json_date(observed["date"].min()),
                    "projection_horizon": json_date(sub.loc[sub["frequency"] == "projection", "date"].max()),
                    "max_risk": json_value(sub["risk_score"].max()),
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
                    "current_weight": CURRENT_BUCKET_WEIGHTS.get(str(row["bucket"]), 0.0),
                    "metrics": int(row["n"]),
                    "expected_metrics": int(row["expected"]),
                    "coverage": json_value(row["coverage"]),
                    "score_role": "structural" if str(row["bucket"]) in STRUCTURAL_BUCKETS else "current_stress",
                    "included_in_overall": str(row["bucket"]) not in STRUCTURAL_BUCKETS and pd.notna(overall),
                }
            )

    top_rows = []
    if not metrics.empty:
        current_metrics = metrics[~metrics["bucket"].isin(STRUCTURAL_BUCKETS) & metrics["eligible"]]
        top_metrics = current_metrics.sort_values("risk_score", ascending=False).head(LATEST_JSON_TOP_SIGNALS)
        top_rows = [metric_record(row) for _, row in top_metrics.iterrows()]

    structural_rows = []
    if not structural_buckets.empty:
        for _, row in structural_buckets.sort_values("score", ascending=False).iterrows():
            score = float(row["score"]) if pd.notna(row["score"]) else np.nan
            structural_rows.append(
                {
                    "bucket": str(row["bucket"]),
                    "label": BUCKET_LABELS.get(str(row["bucket"]), str(row["bucket"])),
                    "score": json_value(score),
                    "status": score_label(score),
                    "metrics": int(row["n"]),
                    "note": "Long-term structural projection excluded from current stress score.",
                }
            )

    valid_until = generated_at + pd.Timedelta(seconds=2 * AUTO_REFRESH_SECONDS)
    if pd.notna(overall):
        deadlines = metrics.loc[metrics["bucket"].isin(CURRENT_STRESS_BUCKETS), "freshness_expires_at"].dropna()
        if not deadlines.empty:
            valid_until = min(valid_until, deadlines.min())
    delayed = metrics.loc[metrics["quality"] == "official_delayed", "series_id"].tolist()
    return {
        "schema_version": "1.2",
        "source_sha": source_revision(),
        "methodology": {
            "id": METHODOLOGY_ID,
            "version": METHODOLOGY_VERSION,
            "description": METHODOLOGY_DESCRIPTION,
            "current_bucket_weights": dict(CURRENT_BUCKET_WEIGHTS),
            "retired_buckets": ["market_prices"],
            "comparable_with_previous_method": False,
        },
        "name": "Debt Risk Radar",
        "description": "Machine-readable snapshot of the public US debt risk dashboard.",
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "valid_until": valid_until.isoformat().replace("+00:00", "Z"),
        "collection": collection if collection is not None else {"status": "unknown", "providers": []},
        "public_url": "https://debt.l0g.fr/",
        "latest_json_url": "https://debt.l0g.fr/latest.json",
        "scope": {
            "country": DEFAULT_COUNTRY,
            "focus": "US sovereign debt, fiscal projections, private credit, liquidity and market stress.",
            "market_data": "US yields and credit spreads via FRED. No ETF price collection or substitution. A server-side FRED key is required.",
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
            "current_stress": json_value(float(overall)) if pd.notna(overall) else None,
            "status": score_label(overall),
            "methodology": METHODOLOGY_DESCRIPTION,
            "expected_signals": int(metrics["bucket"].isin(CURRENT_STRESS_BUCKETS).sum()),
            "eligible_signals": int((metrics["bucket"].isin(CURRENT_STRESS_BUCKETS) & metrics["eligible"]).sum()),
            "coverage": json_value(current_coverage),
            "coverage_note": "Weighted coverage of eligible expected signals. Current stress is unavailable unless coverage is complete; no neutral imputation.",
            "excluded_buckets": sorted(STRUCTURAL_BUCKETS),
            "buckets": bucket_rows,
            "structural": structural_rows,
        },
        "top_signals": top_rows,
        "signals": [metric_record(row) for _, row in metrics.iterrows()],
        "quality": {
            "policy_version": FRESHNESS_POLICY_VERSION,
            "status": "degraded" if issues or not metrics["eligible"].all() else "official-delayed" if delayed else "ok",
            "expected_signals": len(metrics),
            "eligible_signals": int(metrics["eligible"].sum()),
            "unavailable_signals": metrics.loc[~metrics["eligible"], "series_id"].tolist(),
            "delayed_signals": delayed,
            "expiring_signals": [
                {"series_id": row["series_id"], "limit_at": json_value(row["freshness_limit_at"])}
                for _, row in metrics.iterrows()
                if row["eligible"] and pd.notna(row["freshness_limit_at"])
                and generated_at < row["freshness_limit_at"] <= generated_at + pd.Timedelta(days=PUBLICATION_WARNING_DAYS)
                and row["frequency"] == "quarterly_period_start"
            ],
            "note": "Official-delayed quarterly signals require recent matching FRED publication metadata and bounded publication/period ages. Other observation-age tolerances do not prove latest-publication status. CBO February 2026 vintage is pinned.",
        },
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
    payload = build_latest_payload(metrics, buckets, issues, collection_status(ACTIVE_SOURCE_HOSTS))
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
                "methodology_version": METHODOLOGY_VERSION,
                "current_stress": payload["score"]["current_stress"],
                "status": payload["score"]["status"],
                "top_signals": len(payload["top_signals"]),
                "sources": len(payload["sources"]),
                "issues": len(payload["issues"]),
                "quality": payload["quality"]["status"],
                "coverage": payload["score"]["coverage"],
                "eligible_signals": payload["quality"]["eligible_signals"],
                "unavailable_signals": payload["quality"]["unavailable_signals"],
                "delayed_signals": payload["quality"]["delayed_signals"],
                "expiring_signals": payload["quality"]["expiring_signals"],
                "collection": payload["collection"],
            },
            sort_keys=True,
        )
    )
    return 0 if payload["score"]["current_stress"] is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
