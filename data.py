"""
Data access and scoring for Debt Risk Radar.
"""

from __future__ import annotations

import os
import io
import zipfile
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, Iterable, List, Tuple

import numpy as np
import pandas as pd
import requests
import streamlit as st
from fredapi import Fred

from catalog import (
    BIS_BULK_FEEDS,
    BIS_COUNTRY_MAP,
    BUCKET_WEIGHTS,
    CBO_DATASETS,
    FRED_SERIES,
    MASSIVE_MARKET_SERIES,
    TREASURY_ENDPOINTS,
    WORLD_BANK_INDICATORS,
    ZSCORE_WINDOW_YEARS,
    STRUCTURAL_BUCKETS,
)


class DataUnavailable(Exception):
    """Raised when an upstream source cannot be reached or parsed."""


@dataclass(frozen=True)
class DataIssue:
    source: str
    detail: str


def cache_data(*args, **kwargs):
    if os.environ.get("DEBT_RISK_RADAR_DISABLE_STREAMLIT_CACHE") == "1":
        return lambda func: func
    return st.cache_data(*args, **kwargs)


def _secret_values() -> List[str]:
    values = []
    for name in ["FRED_API_KEY", "MASSIVE_API_KEY"]:
        value = os.environ.get(name) or _streamlit_secret(name)
        if value:
            values.append(str(value))
    return values


def _safe_error(exc: Exception | str) -> str:
    text = str(exc)
    for secret in _secret_values():
        text = text.replace(secret, "[redacted]")
    text = re.sub(
        r"(?i)(api[_-]?key|apikey|token|access_token)=([^&\s]+)",
        r"\1=[redacted]",
        text,
    )
    return text[:500]


def _streamlit_secret(name: str):
    try:
        return st.secrets.get(name, None)
    except Exception:
        return None


def fred_key_available() -> bool:
    return bool(os.environ.get("FRED_API_KEY") or _streamlit_secret("FRED_API_KEY"))


def massive_key_available() -> bool:
    return bool(os.environ.get("MASSIVE_API_KEY") or _streamlit_secret("MASSIVE_API_KEY"))


def get_fred() -> Fred:
    key = os.environ.get("FRED_API_KEY") or _streamlit_secret("FRED_API_KEY")
    if not key:
        raise DataUnavailable("FRED_API_KEY is missing.")
    return Fred(api_key=key)


def iter_fred_catalog() -> Iterable[Tuple[str, str, dict]]:
    for bucket, series_map in FRED_SERIES.items():
        for series_id, meta in series_map.items():
            yield bucket, series_id, meta


@cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_fred_series(start: str = "1990-01-01") -> Tuple[Dict[str, pd.Series], List[DataIssue]]:
    if not fred_key_available():
        return {}, [DataIssue("FRED", "FRED_API_KEY missing. FRED metrics skipped.")]

    fred = get_fred()
    data: Dict[str, pd.Series] = {}
    issues: List[DataIssue] = []

    for _, series_id, _ in iter_fred_catalog():
        try:
            series = fred.get_series(series_id, observation_start=start).dropna()
            if len(series) > 0:
                series.index = pd.to_datetime(series.index)
                data[series_id] = series.astype(float)
            else:
                issues.append(DataIssue("FRED", f"{series_id}: empty series."))
        except Exception as exc:
            issues.append(DataIssue("FRED", f"{series_id}: {_safe_error(exc)}"))

    return data, issues


def _fiscaldata_get(url: str, params: dict) -> dict:
    response = requests.get(url, params=params, timeout=30)
    if response.status_code != 200:
        raise DataUnavailable(f"HTTP {response.status_code}: {response.text[:200]}")
    payload = response.json()
    if "data" not in payload:
        raise DataUnavailable("No data field in Fiscal Data response.")
    return payload


@cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_treasury_debt(start: str = "2015-01-01") -> Tuple[pd.DataFrame, List[DataIssue]]:
    endpoint = TREASURY_ENDPOINTS["debt_to_penny"]
    params = {
        "filter": f"record_date:gte:{start}",
        "fields": ",".join(endpoint["fields"]),
        "sort": "record_date",
        "page[size]": 10000,
    }

    try:
        payload = _fiscaldata_get(endpoint["url"], params)
        df = pd.DataFrame(payload["data"])
        if df.empty:
            raise DataUnavailable("Debt to the Penny returned no rows.")
        df["record_date"] = pd.to_datetime(df["record_date"])
        for col in ["debt_held_public_amt", "intragov_hold_amt", "tot_pub_debt_out_amt"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["record_date", "tot_pub_debt_out_amt"])
        df = df.sort_values("record_date")
        return df, []
    except Exception as exc:
        return pd.DataFrame(), [DataIssue(endpoint["source"], _safe_error(exc))]


@cache_data(ttl=24 * 3600, show_spinner=False)
def fetch_world_bank(country: str = "USA") -> Tuple[pd.DataFrame, List[DataIssue]]:
    rows = []
    issues: List[DataIssue] = []

    for indicator, meta in WORLD_BANK_INDICATORS.items():
        url = f"https://api.worldbank.org/v2/country/{country}/indicator/{indicator}"
        params = {"format": "json", "per_page": 120}
        try:
            response = requests.get(url, params=params, timeout=30)
            if response.status_code != 200:
                raise DataUnavailable(f"HTTP {response.status_code}")
            payload = response.json()
            if not isinstance(payload, list) or len(payload) < 2:
                raise DataUnavailable("Unexpected World Bank payload.")
            for item in payload[1]:
                if item.get("value") is None:
                    continue
                rows.append(
                    {
                        "indicator": indicator,
                        "name": meta["name"],
                        "date": pd.to_datetime(f"{item['date']}-12-31"),
                        "value": float(item["value"]),
                        "unit": meta["unit"],
                    }
                )
        except Exception as exc:
            issues.append(DataIssue("World Bank", f"{indicator}: {_safe_error(exc)}"))

    return pd.DataFrame(rows), issues


def _quarter_to_timestamp(value: str) -> pd.Timestamp:
    return pd.Period(value, freq="Q").to_timestamp(how="end").normalize()


def _fy_to_timestamp(value: str) -> pd.Timestamp:
    year = int(str(value).replace("FY", ""))
    return pd.Timestamp(year=year, month=9, day=30)


def _download_bis_flat_csv(url: str) -> pd.DataFrame:
    response = requests.get(url, timeout=45)
    if response.status_code != 200:
        raise DataUnavailable(f"HTTP {response.status_code}: {response.text[:200]}")
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    csv_names = [name for name in archive.namelist() if name.endswith(".csv")]
    if not csv_names:
        raise DataUnavailable("BIS bulk archive does not contain a CSV file.")
    return pd.read_csv(archive.open(csv_names[0]))


@cache_data(ttl=24 * 3600, show_spinner=False)
def fetch_bis_credit(country: str = "USA") -> Tuple[pd.DataFrame, List[DataIssue]]:
    bis_country = BIS_COUNTRY_MAP.get(country)
    if not bis_country:
        return pd.DataFrame(), [DataIssue("BIS Data Portal", f"No BIS country mapping for {country}.")]

    rows = []
    issues: List[DataIssue] = []

    try:
        gap_df = _download_bis_flat_csv(BIS_BULK_FEEDS["credit_gap"]["url"])
        gap_df = gap_df[gap_df["BORROWERS_CTY:Borrowers' country"].astype(str).str.startswith(f"{bis_country}:")]
        for dtype, metric_name, unit in [
            ("C: Credit-to-GDP gaps (actual-trend)", "Credit-to-GDP gap", "pp"),
            ("A: Credit-to-GDP ratios (actual data)", "Credit-to-GDP ratio", "% GDP"),
        ]:
            sub = gap_df[gap_df["CG_DTYPE:Credit gap data type"] == dtype].copy()
            if sub.empty:
                continue
            sub["date"] = sub["TIME_PERIOD:Time period or range"].map(_quarter_to_timestamp)
            sub["value"] = pd.to_numeric(sub["OBS_VALUE:Observation Value"], errors="coerce")
            for _, item in sub.dropna(subset=["value"]).iterrows():
                rows.append(
                    {
                        "dataset": "credit_gap",
                        "metric": metric_name,
                        "date": item["date"],
                        "value": float(item["value"]),
                        "unit": unit,
                    }
                )
    except Exception as exc:
        issues.append(DataIssue("BIS Data Portal", f"credit_gap: {_safe_error(exc)}"))

    try:
        dsr_df = _download_bis_flat_csv(BIS_BULK_FEEDS["dsr"]["url"])
        dsr_df = dsr_df[dsr_df["BORROWERS_CTY:Borrowers' country"].astype(str).str.startswith(f"{bis_country}:")]
        dsr_df["date"] = dsr_df["TIME_PERIOD:Time period or range"].map(_quarter_to_timestamp)
        dsr_df["value"] = pd.to_numeric(dsr_df["OBS_VALUE:Observation Value"], errors="coerce")
        for borrower, metric_name in [
            ("H: Households & NPISHs", "Household debt service ratio"),
            ("PNFS: Private non-financial sector", "Private non-financial debt service ratio"),
            ("NFC: Non-financial corporations", "Corporate debt service ratio"),
        ]:
            sub = dsr_df[dsr_df["DSR_BORROWERS:Borrowers"] == borrower]
            for _, item in sub.dropna(subset=["value"]).iterrows():
                rows.append(
                    {
                        "dataset": "dsr",
                        "metric": metric_name,
                        "date": item["date"],
                        "value": float(item["value"]),
                        "unit": "%",
                    }
                )
    except Exception as exc:
        issues.append(DataIssue("BIS Data Portal", f"dsr: {_safe_error(exc)}"))

    return pd.DataFrame(rows), issues


@cache_data(ttl=24 * 3600, show_spinner=False)
def fetch_cbo_projections() -> Tuple[pd.DataFrame, List[DataIssue]]:
    dataset = CBO_DATASETS["long_term_budget"]
    try:
        response = requests.get(dataset["url"], timeout=30)
        if response.status_code != 200:
            raise DataUnavailable(f"HTTP {response.status_code}: {response.text[:200]}")
        df = pd.read_csv(io.StringIO(response.text))
        df["date"] = df["date"].map(_fy_to_timestamp)
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        return df.dropna(subset=["value"]), []
    except Exception as exc:
        return pd.DataFrame(), [DataIssue(dataset["source"], _safe_error(exc))]


def _massive_session() -> requests.Session:
    key = os.environ.get("MASSIVE_API_KEY") or _streamlit_secret("MASSIVE_API_KEY")
    if not key:
        raise DataUnavailable("MASSIVE_API_KEY missing. Massive market metrics skipped.")
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {key}"
    return session


@cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_massive_market(days: int = 730) -> Tuple[Dict[str, pd.Series], List[DataIssue]]:
    if not massive_key_available():
        return {}, [DataIssue("Massive Market Data", "MASSIVE_API_KEY missing. Market price metrics skipped.")]

    base_url = (os.environ.get("MASSIVE_BASE_URL") or _streamlit_secret("MASSIVE_BASE_URL") or "https://api.massive.com").rstrip("/")
    session = _massive_session()
    end = date.today()
    start = end - timedelta(days=days)
    data: Dict[str, pd.Series] = {}
    issues: List[DataIssue] = []

    for ticker in MASSIVE_MARKET_SERIES:
        path = f"/v2/aggs/ticker/{ticker}/range/1/day/{start.isoformat()}/{end.isoformat()}"
        params = {"adjusted": "true", "limit": 5000}
        try:
            response = session.get(base_url + path, params=params, timeout=30)
            if response.status_code != 200:
                raise DataUnavailable(f"HTTP {response.status_code}: {response.text[:200]}")
            payload = response.json()
            rows = []
            for item in payload.get("results") or []:
                if item.get("t") is None or item.get("c") is None:
                    continue
                rows.append((pd.to_datetime(item["t"], unit="ms", utc=True).tz_convert(None).normalize(), float(item["c"])))
            if rows:
                series = pd.Series({ts: close for ts, close in rows}).sort_index()
                data[ticker] = series
            else:
                issues.append(DataIssue("Massive Market Data", f"{ticker}: no daily aggregate rows."))
        except Exception as exc:
            issues.append(DataIssue("Massive Market Data", f"{ticker}: {_safe_error(exc)}"))

    return data, issues


def zscore_latest(series: pd.Series, direction: str, window_years: int = ZSCORE_WINDOW_YEARS) -> dict:
    clean = series.dropna().sort_index()
    if len(clean) < 8:
        return {"z": np.nan, "signed_z": np.nan, "current": np.nan, "date": pd.NaT}

    current = clean.iloc[-1]
    current_date = clean.index[-1]
    cutoff = current_date - pd.DateOffset(years=window_years)
    window = clean.loc[cutoff:]
    if len(window) < 6 or window.std() == 0:
        z = np.nan
    else:
        z = (current - window.mean()) / window.std()
    sign = 1 if direction == "up" else -1
    return {
        "z": float(z) if pd.notna(z) else np.nan,
        "signed_z": float(sign * z) if pd.notna(z) else np.nan,
        "current": float(current),
        "date": current_date,
    }


def risk_points_from_z(signed_z: float) -> float:
    if pd.isna(signed_z):
        return np.nan
    return float(np.clip(50 + signed_z * 15, 0, 100))


def risk_points_from_level(value: float, neutral: float, stress: float, direction: str = "up") -> float:
    if pd.isna(value):
        return np.nan
    if stress == neutral:
        return np.nan
    if direction == "down":
        scaled = (neutral - value) / (neutral - stress)
    else:
        scaled = (value - neutral) / (stress - neutral)
    return float(np.clip(50 + scaled * 30, 0, 100))


def treasury_daily_metrics(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    daily = df.set_index("record_date").sort_index()
    total_bn = daily["tot_pub_debt_out_amt"] / 1_000_000_000
    public_bn = daily["debt_held_public_amt"] / 1_000_000_000
    public_share = public_bn / total_bn * 100

    latest = total_bn.iloc[-1]
    d90_ref = total_bn.asof(total_bn.index[-1] - pd.DateOffset(days=90))
    d365_ref = total_bn.asof(total_bn.index[-1] - pd.DateOffset(days=365))
    growth_90d_ann = ((latest / d90_ref) - 1) * 4 * 100 if pd.notna(d90_ref) and d90_ref else np.nan
    growth_1y = ((latest / d365_ref) - 1) * 100 if pd.notna(d365_ref) and d365_ref else np.nan

    rows = []
    for metric_name, series, direction, weight, rationale in [
        ("Total public debt outstanding", total_bn, "up", 1.00, "Daily Treasury debt stock."),
        ("Debt held by the public share", public_share, "up", 0.80, "Market-facing share of Treasury debt."),
    ]:
        scored = zscore_latest(series, direction)
        rows.append(
            {
                "bucket": "treasury_daily",
                "series_id": metric_name,
                "name": metric_name,
                "unit": "USD bn" if "outstanding" in metric_name else "%",
                "date": scored["date"],
                "current": scored["current"],
                "signed_z": scored["signed_z"],
                "risk_score": risk_points_from_z(scored["signed_z"]),
                "weight": weight,
                "source": "US Treasury Fiscal Data",
                "rationale": rationale,
            }
        )

    rows.append(
        {
            "bucket": "treasury_daily",
            "series_id": "90d annualized debt growth",
            "name": "90-day annualized debt growth",
            "unit": "%",
            "date": total_bn.index[-1],
            "current": float(growth_90d_ann) if pd.notna(growth_90d_ann) else np.nan,
            "signed_z": np.nan,
            "risk_score": float(np.clip(50 + max(growth_90d_ann - 5, -5) * 3, 0, 100))
            if pd.notna(growth_90d_ann)
            else np.nan,
            "weight": 0.70,
            "source": "US Treasury Fiscal Data",
            "rationale": f"One-year growth is {growth_1y:.2f}%." if pd.notna(growth_1y) else "Short-term issuance pace.",
        }
    )
    return pd.DataFrame(rows)


def fred_metrics(all_data: Dict[str, pd.Series]) -> pd.DataFrame:
    rows = []
    for bucket, series_id, meta in iter_fred_catalog():
        if series_id not in all_data:
            continue
        scored = zscore_latest(all_data[series_id], meta["direction"])
        rows.append(
            {
                "bucket": bucket,
                "series_id": series_id,
                "name": meta["name"],
                "unit": meta["unit"],
                "date": scored["date"],
                "current": scored["current"],
                "signed_z": scored["signed_z"],
                "risk_score": risk_points_from_z(scored["signed_z"]),
                "weight": meta["weight"],
                "source": meta["source"],
                "rationale": meta["rationale"],
            }
        )
    return pd.DataFrame(rows)


def world_bank_metrics(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    rows = []
    for indicator, meta in WORLD_BANK_INDICATORS.items():
        sub = df[df["indicator"] == indicator].sort_values("date")
        if sub.empty:
            continue
        series = pd.Series(sub["value"].values, index=sub["date"])
        scored = zscore_latest(series, meta["direction"], window_years=10)
        rows.append(
            {
                "bucket": "world_bank",
                "series_id": indicator,
                "name": meta["name"],
                "unit": meta["unit"],
                "date": scored["date"],
                "current": scored["current"],
                "signed_z": scored["signed_z"],
                "risk_score": risk_points_from_z(scored["signed_z"]),
                "weight": meta["weight"],
                "source": meta["source"],
                "rationale": "Annual cross-country comparable indicator.",
            }
        )
    return pd.DataFrame(rows)


def bis_credit_metrics(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    rows = []
    for metric_name, sub in df.groupby("metric"):
        sub = sub.sort_values("date")
        series = pd.Series(sub["value"].values, index=sub["date"])
        current = float(series.iloc[-1])
        current_date = series.index[-1]
        if metric_name == "Credit-to-GDP gap":
            risk_score = risk_points_from_level(current, neutral=0.0, stress=10.0, direction="up")
            signed_z = np.nan
            rationale = "BIS early-warning credit gap; positive gaps flag private credit above trend."
            weight = 1.20
            unit = "pp"
        else:
            scored = zscore_latest(series, "up", window_years=10)
            risk_score = risk_points_from_z(scored["signed_z"])
            signed_z = scored["signed_z"]
            rationale = "BIS private-sector leverage or debt-service metric."
            weight = 0.90
            unit = str(sub["unit"].iloc[-1])
        rows.append(
            {
                "bucket": "global_credit",
                "series_id": f"BIS {metric_name}",
                "name": metric_name,
                "unit": unit,
                "date": current_date,
                "current": current,
                "signed_z": signed_z,
                "risk_score": risk_score,
                "weight": weight,
                "source": "BIS Data Portal",
                "rationale": rationale,
            }
        )
    return pd.DataFrame(rows)


def cbo_projection_metrics(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    variable_meta = CBO_DATASETS["long_term_budget"]["variables"]
    rows = []
    for variable, meta in variable_meta.items():
        sub = df[df["variable"] == variable].sort_values("date")
        if sub.empty:
            continue
        series = pd.Series(sub["value"].values, index=sub["date"])
        scored = zscore_latest(series, meta["direction"], window_years=30)
        terminal = float(series.iloc[-1])
        terminal_date = series.index[-1]
        risk_score = risk_points_from_z(scored["signed_z"])
        if variable == "lt_debt_held_by_public_gdp_share":
            risk_score = max(risk_score, risk_points_from_level(terminal, neutral=80.0, stress=150.0, direction="up"))
        elif variable == "lt_outlays_net_interest_gdp_share":
            risk_score = max(risk_score, risk_points_from_level(terminal, neutral=2.0, stress=6.0, direction="up"))
        elif variable == "lt_deficit_total_gdp_share":
            risk_score = max(risk_score, risk_points_from_level(terminal, neutral=-3.0, stress=-8.0, direction="down"))
        rows.append(
            {
                "bucket": "cbo_projection",
                "series_id": variable,
                "name": meta["name"],
                "unit": meta["unit"],
                "date": terminal_date,
                "current": terminal,
                "signed_z": scored["signed_z"],
                "risk_score": risk_score,
                "weight": meta["weight"],
                "source": "CBO Open Data",
                "rationale": "Latest CBO long-term projection vintage; terminal structural value, not a current market shock.",
            }
        )
    return pd.DataFrame(rows)


def massive_market_metrics(all_data: Dict[str, pd.Series]) -> pd.DataFrame:
    if not all_data:
        return pd.DataFrame()

    rows = []
    for ticker, series in all_data.items():
        meta = MASSIVE_MARKET_SERIES[ticker]
        returns_30d = series.pct_change(30, fill_method=None) * 100
        scored = zscore_latest(series, meta["direction"], window_years=2)
        drawdown = (series.iloc[-1] / series.rolling(252, min_periods=30).max().iloc[-1] - 1) * 100
        risk_score = risk_points_from_z(scored["signed_z"])
        if pd.notna(drawdown):
            risk_score = max(risk_score, risk_points_from_level(drawdown, neutral=0.0, stress=-20.0, direction="down"))
        rows.append(
            {
                "bucket": "market_prices",
                "series_id": ticker,
                "name": meta["name"],
                "unit": meta["unit"],
                "date": series.index[-1],
                "current": float(series.iloc[-1]),
                "signed_z": scored["signed_z"],
                "risk_score": risk_score,
                "weight": meta["weight"],
                "source": meta["source"],
                "rationale": f"{meta['rationale']} 30d return {returns_30d.iloc[-1]:+.2f}% if available.",
            }
        )

    ratio_specs = [
        ("HYG/LQD", "High yield versus investment grade price ratio", "HYG", "LQD", "down", 1.00),
        ("TLT/SHY", "Long Treasury versus short Treasury price ratio", "TLT", "SHY", "down", 0.80),
        ("SPY/TLT", "Equity versus long Treasury price ratio", "SPY", "TLT", "down", 0.50),
    ]
    for series_id, name, left, right, direction, weight in ratio_specs:
        if left not in all_data or right not in all_data:
            continue
        ratio = (all_data[left] / all_data[right]).dropna()
        if len(ratio) < 40:
            continue
        scored = zscore_latest(ratio, direction, window_years=2)
        rows.append(
            {
                "bucket": "market_prices",
                "series_id": series_id,
                "name": name,
                "unit": "ratio",
                "date": ratio.index[-1],
                "current": float(ratio.iloc[-1]),
                "signed_z": scored["signed_z"],
                "risk_score": risk_points_from_z(scored["signed_z"]),
                "weight": weight,
                "source": "Massive Market Data",
                "rationale": "Derived market ratio from Massive daily adjusted closes.",
            }
        )

    if "HYG" in all_data:
        hy_returns = all_data["HYG"].pct_change(fill_method=None)
        hy_vol = hy_returns.rolling(30, min_periods=20).std() * np.sqrt(252) * 100
        scored = zscore_latest(hy_vol.dropna(), "up", window_years=2)
        if pd.notna(scored["current"]):
            rows.append(
                {
                    "bucket": "market_prices",
                    "series_id": "HYG 30d realized vol",
                    "name": "HYG 30d realized volatility",
                    "unit": "%",
                    "date": scored["date"],
                    "current": scored["current"],
                    "signed_z": scored["signed_z"],
                    "risk_score": risk_points_from_z(scored["signed_z"]),
                    "weight": 0.70,
                    "source": "Massive Market Data",
                    "rationale": "Realized credit ETF volatility from Massive closes.",
                }
            )
    return pd.DataFrame(rows)


def combine_metrics(*frames: pd.DataFrame) -> pd.DataFrame:
    valid = [frame for frame in frames if frame is not None and not frame.empty]
    if not valid:
        return pd.DataFrame()
    df = pd.concat(valid, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df["weighted_score"] = df["risk_score"] * df["weight"]
    return df


def bucket_scores(metrics: pd.DataFrame) -> pd.DataFrame:
    if metrics.empty:
        return pd.DataFrame(columns=["bucket", "score", "weight", "n"])

    rows = []
    for bucket, sub in metrics.dropna(subset=["risk_score"]).groupby("bucket"):
        metric_score = np.average(sub["risk_score"], weights=sub["weight"])
        rows.append(
            {
                "bucket": bucket,
                "score": float(metric_score),
                "weight": BUCKET_WEIGHTS.get(bucket, 0.05),
                "n": int(len(sub)),
            }
        )
    return pd.DataFrame(rows).sort_values("score", ascending=False)


def overall_score(bucket_df: pd.DataFrame, exclude_buckets: set[str] | None = None) -> float:
    if bucket_df.empty:
        return np.nan
    scoped = bucket_df
    if exclude_buckets:
        scoped = scoped[~scoped["bucket"].isin(exclude_buckets)]
    if scoped.empty:
        return np.nan
    return float(np.average(scoped["score"], weights=scoped["weight"]))


def current_stress_score(bucket_df: pd.DataFrame) -> float:
    return overall_score(bucket_df, exclude_buckets=STRUCTURAL_BUCKETS)


def score_label(score: float) -> str:
    if pd.isna(score):
        return "No data"
    if score >= 80:
        return "Stress"
    if score >= 65:
        return "Watch"
    if score >= 50:
        return "Elevated"
    return "Calm"


def score_color(score: float) -> str:
    if pd.isna(score):
        return "#6f9b94"
    if score >= 80:
        return "#ff4d87"
    if score >= 65:
        return "#f5b13d"
    if score >= 50:
        return "#ff8a3d"
    return "#5eead4"


def latest_value(series: pd.Series) -> float:
    clean = series.dropna().sort_index()
    return float(clean.iloc[-1]) if len(clean) else np.nan


def build_debt_dynamics_projection(
    initial_debt_gdp: float,
    primary_balance_gdp: float,
    nominal_growth: float,
    effective_rate: float,
    years: int = 10,
) -> pd.DataFrame:
    """
    Project debt/GDP with the standard approximation:
    debt[t+1] = debt[t] * (1 + r) / (1 + g) - primary_balance

    primary_balance_gdp is positive for surplus and negative for deficit.
    Inputs are percentages, output is percentage of GDP.
    """
    current_year = date.today().year
    debt = initial_debt_gdp
    rows = [{"year": current_year, "debt_gdp": debt}]
    for step in range(1, years + 1):
        debt = debt * (1 + effective_rate / 100) / (1 + nominal_growth / 100) - primary_balance_gdp
        rows.append({"year": current_year + step, "debt_gdp": debt})
    return pd.DataFrame(rows)
