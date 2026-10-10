"""Pure provider parsers. A response must pass these checks before cache commit."""

import io
import re
import zipfile

import numpy as np
import pandas as pd

from http_cache import DataUnavailable


def observation_dates(values):
    dates = pd.to_datetime(values, errors="raise", utc=True)
    if dates.isna().any() or (dates > pd.Timestamp.now(tz="UTC")).any():
        raise DataUnavailable("Invalid or future observation date.")


def fred_observations(payload):
    rows = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows or payload.get("count", len(rows)) != len(rows):
        raise DataUnavailable("Incomplete FRED observations response.")
    dates = pd.to_datetime([row["date"] for row in rows], errors="raise")
    if dates.isna().any() or dates.has_duplicates or not dates.is_monotonic_increasing:
        raise DataUnavailable("Invalid or duplicate FRED observations.")
    if any(isinstance(row["value"], bool) for row in rows):
        raise DataUnavailable("Invalid FRED observation value.")
    values = pd.to_numeric(pd.Series([row["value"] for row in rows]).replace(".", np.nan), errors="raise")
    series = pd.Series(values.to_numpy(), index=dates).dropna().astype(float)
    if series.empty or not np.isfinite(series).all():
        raise DataUnavailable("Invalid or empty FRED observations.")
    observation_dates(series.index)
    return series


def fred_publication(payload, series_id):
    rows = payload.get("seriess") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise DataUnavailable("Invalid FRED series metadata.")
    meta = rows[0]
    end = pd.to_datetime(meta.get("observation_end"), errors="coerce", utc=True)
    updated = pd.to_datetime(meta.get("last_updated"), errors="coerce", utc=True)
    if meta.get("id") != series_id or meta.get("frequency_short") != "Q" or pd.isna(end) or pd.isna(updated):
        raise DataUnavailable("Invalid FRED publication identity or dates.")
    observation_dates([end, updated])
    return end, updated


def world_bank(payload, country, indicator):
    if (not isinstance(payload, list) or len(payload) != 2
            or not isinstance(payload[0], dict) or not isinstance(payload[1], list)):
        raise DataUnavailable("Unexpected World Bank payload.")
    meta, items = payload
    if int(meta.get("page", 0)) != 1 or int(meta.get("pages", 0)) != 1 or int(meta.get("total", -1)) != len(items):
        raise DataUnavailable("Incomplete World Bank response.")
    rows, seen = [], set()
    for item in items:
        if (item.get("countryiso3code") != country
                or item.get("indicator", {}).get("id") != indicator):
            raise DataUnavailable("World Bank country or indicator mismatch.")
        year = str(item.get("date", ""))
        if not re.fullmatch(r"[12][0-9]{3}", year) or year in seen:
            raise DataUnavailable("Invalid or duplicate World Bank observation year.")
        seen.add(year)
        if item.get("value") is None:
            continue
        value = item["value"]
        if isinstance(value, bool) or not np.isfinite(float(value)):
            raise DataUnavailable("Invalid World Bank observation value.")
        rows.append({"date": pd.Timestamp(f"{year}-12-31"), "value": float(value)})
    if not rows:
        raise DataUnavailable("Empty World Bank series.")
    observation_dates([row["date"] for row in rows])
    return rows


def treasury(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or not payload["data"]:
        raise DataUnavailable("No debt observations in Treasury response.")
    if int(payload.get("meta", {}).get("total-pages", 1)) != 1:
        raise DataUnavailable("Incomplete Treasury response: pagination required.")
    frame = pd.DataFrame(payload["data"])
    frame["record_date"] = pd.to_datetime(frame["record_date"], errors="raise")
    if frame.record_date.isna().any() or frame.record_date.duplicated().any():
        raise DataUnavailable("Invalid Treasury observation dates.")
    observation_dates(frame.record_date)
    columns = ["debt_held_public_amt", "intragov_hold_amt", "tot_pub_debt_out_amt"]
    for col in columns:
        if frame[col].map(lambda value: isinstance(value, bool)).any():
            raise DataUnavailable("Invalid Treasury debt amount.")
        frame[col] = pd.to_numeric(frame[col], errors="raise")
        if not np.isfinite(frame[col]).all() or (frame[col] < 0).any():
            raise DataUnavailable("Invalid Treasury debt amount.")
    if (frame.tot_pub_debt_out_amt <= 0).any():
        raise DataUnavailable("Invalid Treasury total debt.")
    return frame.sort_values("record_date")


def bis_archive(body):
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        files = [item for item in archive.infolist() if item.filename.endswith(".csv")]
        if len(files) != 1 or files[0].file_size > 100 * 1024 * 1024:
            raise DataUnavailable("BIS archive has unexpected contents or exceeds the size limit.")
        with archive.open(files[0]) as handle:
            frame = pd.read_csv(handle)
    country = "BORROWERS_CTY:Borrowers' country"
    period = "TIME_PERIOD:Time period or range"
    value = "OBS_VALUE:Observation Value"
    kind = "CG_DTYPE:Credit gap data type" if "CG_DTYPE:Credit gap data type" in frame else "DSR_BORROWERS:Borrowers"
    selected = frame[frame[country].astype(str).str.split(":", n=1).str[0].str.strip() == "US"]
    codes = selected[kind].astype(str).str.split(":", n=1).str[0].str.strip()
    required = ("A", "C") if kind.startswith("CG_") else ("P", "N", "H")
    for code in required:
        series = selected[codes == code]
        numbers = pd.to_numeric(series[value], errors="raise").dropna()
        if series.empty or series[period].duplicated().any() or numbers.empty or not np.isfinite(numbers).all():
            raise DataUnavailable("Missing, duplicate or invalid BIS US observations.")
        observation_dates(pd.PeriodIndex(series[period], freq="Q").end_time.normalize())
    return frame


def cbo_csv(body, variables):
    frame = pd.read_csv(io.BytesIO(body))
    frame["date"] = frame["date"].map(lambda x: pd.Timestamp(year=int(str(x).replace("FY", "")), month=9, day=30))
    frame["value"] = pd.to_numeric(frame["value"], errors="raise")
    for variable in variables:
        series = frame[frame.variable == variable]
        if series.empty or series.date.duplicated().any() or not np.isfinite(series.value).all():
            raise DataUnavailable("Missing, duplicate or invalid CBO projection.")
    return frame.dropna(subset=["value"])
