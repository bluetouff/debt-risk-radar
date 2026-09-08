"""Per-signal completeness and conservative observation-age checks.

Age limits are monitoring tolerances, not provider publication commitments.
FRED quarterly dates identify period starts; BIS dates identify period ends.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from catalog import CBO_DATASETS, FRED_SERIES, MASSIVE_MARKET_SERIES, WORLD_BANK_INDICATORS


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
    for series_id, meta in MASSIVE_MARKET_SERIES.items():
        add("market_prices", series_id, meta, 10, "daily")
    for series_id in ("HYG/LQD", "TLT/SHY", "SPY/TLT", "HYG 30d realized vol"):
        add("market_prices", series_id, {"name": series_id, "source": "Massive Market Data"}, 10, "daily")
    return expected


def assess_metrics(metrics: pd.DataFrame, now=None) -> pd.DataFrame:
    today = pd.Timestamp(now if now is not None else pd.Timestamp.now(tz="UTC"))
    today = today.tz_localize(None).normalize() if today.tzinfo else today.normalize()
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
        elif meta["max_age_days"] is not None and age > meta["max_age_days"]:
            status, detail = "stale", "Observation older than this frequency's tolerance."
        elif bucket == "market_prices" and row["current"] <= 0 and series_id != "HYG 30d realized vol":
            status, detail = "invalid", "Non-positive market price or ratio."
        elif not np.isfinite(row["risk_score"]):
            status, detail = "unscored", "Insufficient valid history or zero variance."
        elif not 0 <= row["risk_score"] <= 100 or not np.isfinite(row["weight"]) or row["weight"] <= 0:
            status, detail = "invalid", "Invalid score or weight."
        elif bucket == "cbo_projection":
            status, detail = "projection", "Pinned CBO February 2026 vintage; date is the projection horizon."
        row.update(quality=status, quality_detail=detail, observation_age_days=age if bucket != "cbo_projection" else None,
                   max_age_days=meta["max_age_days"], frequency=meta["frequency"],
                   eligible=status in {"ok", "projection"})
        if not row["eligible"]:
            row["risk_score"] = np.nan
        rows.append(row)
    result = pd.DataFrame(rows)
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    return result
