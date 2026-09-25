"""Contract-driven validation: every row gets checked, every exception gets a
row in an exceptions table (rule, severity, record key, offending value), and
batch gates decide whether the run is allowed to publish.

The point is that bad data is *caught and explained* before it reaches a
chart a journalist will cite - not silently dropped, not silently published.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yaml


@dataclass
class AuditResult:
    exceptions: pd.DataFrame
    gates: list[dict] = field(default_factory=list)
    quarantined_keys: set = field(default_factory=set)
    rows_checked: int = 0

    @property
    def passed(self) -> bool:
        return all(g["status"] != "FAIL" for g in self.gates)

    def summary(self) -> pd.DataFrame:
        if self.exceptions.empty:
            return pd.DataFrame(columns=["rule", "severity", "rows", "pct"])
        s = self.exceptions.groupby(["rule", "severity"]).size().rename("rows").reset_index()
        s["pct"] = (100 * s["rows"] / max(self.rows_checked, 1)).round(3)
        return s.sort_values(["severity", "rows"], ascending=[True, False])


def load_contract(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text())


def _exc(mask: pd.Series, df: pd.DataFrame, key: str, rule: str, sev: str, col: str | None) -> pd.DataFrame:
    hit = df.loc[mask]
    return pd.DataFrame({
        "record_key": hit[key].astype(str) if key in hit else hit.index.astype(str),
        "rule": rule, "severity": sev, "column": col,
        "value": hit[col].astype(str) if col and col in hit else "",
    })


# ---- cross-field rules referenced by name from the contract ----------------
def _occurred_after_reported(df):
    return df["date_occurred"].notna() & (df["date_occurred"] > df["date_reported"]), "date_occurred"


def _cleared_before_reported(df):
    if "clearance_date" not in df:
        return pd.Series(False, index=df.index), None
    return df["clearance_date"].notna() & (df["clearance_date"].dt.normalize() < df["date_reported"].dt.normalize()), "clearance_date"


def _id_date_mismatch(df):
    prefix = pd.to_datetime(df["incident_id"].astype(str).str[:8], format="%Y%m%d", errors="coerce")
    return prefix.notna() & (prefix != df["date_reported"].dt.normalize()), "incident_id"


def _not_local_midnight(df):
    d = df["date_reported"]
    return d.notna() & (d != d.dt.normalize()), "date_reported"


def _jurisdiction_unresolved(df):
    if "jurisdiction" not in df:
        return pd.Series(False, index=df.index), None
    return df["jurisdiction"].isna() & df["city"].notna(), "city"


ROW_RULES = {
    "occurred_after_reported": _occurred_after_reported,
    "cleared_before_reported": _cleared_before_reported,
    "id_date_mismatch": _id_date_mismatch,
    "not_local_midnight": _not_local_midnight,
    "jurisdiction_unresolved": _jurisdiction_unresolved,
}


def validate(df: pd.DataFrame, contract: dict, today: date | None = None) -> AuditResult:
    today = pd.Timestamp(today or date.today())
    key = contract.get("key", "incident_id")
    parts = []

    for col, spec in contract["columns"].items():
        if col not in df.columns:
            if spec.get("required"):
                parts.append(pd.DataFrame([{"record_key": "*", "rule": "missing_column",
                                            "severity": "ERROR", "column": col, "value": ""}]))
            continue
        s = df[col]
        if spec.get("required"):
            parts.append(_exc(s.isna(), df, key, "required_null", "ERROR", col))
        if spec.get("regex"):
            bad = s.notna() & ~s.astype(str).str.fullmatch(spec["regex"])
            parts.append(_exc(bad, df, key, "format", spec.get("regex_severity", "ERROR"), col))
        if spec.get("type") == "date":
            if spec.get("not_future"):
                parts.append(_exc(s.notna() & (s > today + pd.Timedelta(days=1)), df, key, "future_date", "ERROR", col))
            if spec.get("min"):
                parts.append(_exc(s.notna() & (s < pd.Timestamp(spec["min"])), df, key, "implausibly_old",
                                  spec.get("min_severity", "ERROR"), col))
        if spec.get("type") == "float" and ("min" in spec or "max" in spec):
            v = pd.to_numeric(s, errors="coerce")
            bad = v.notna() & ((v < spec.get("min", -np.inf)) | (v > spec.get("max", np.inf)))
            parts.append(_exc(bad, df, key, "out_of_range", spec.get("range_severity", "ERROR"), col))

    if key in df.columns:
        dup = df[key].notna() & df[key].duplicated(keep=False)
        parts.append(_exc(dup, df, key, "duplicate_key", "ERROR", key))

    for rule in contract.get("row_rules", []):
        fn = ROW_RULES[rule["name"]]
        try:
            mask, col = fn(df)
        except KeyError:
            continue  # rule's inputs absent; missing required columns are caught above
        parts.append(_exc(mask, df, key, rule["name"], rule["severity"], col))

    exc = pd.concat([p for p in parts if not p.empty], ignore_index=True) if any(not p.empty for p in parts) \
        else pd.DataFrame(columns=["record_key", "rule", "severity", "column", "value"])
    q = set(exc.loc[exc.severity == "ERROR", "record_key"]) - {"*"}
    res = AuditResult(exceptions=exc, quarantined_keys=q, rows_checked=len(df))

    g = contract.get("batch_gates", {})
    rate = len(q) / max(len(df), 1)
    res.gates.append({"gate": "error_rate", "status": "FAIL" if rate > g.get("max_error_rate", 0) else "PASS",
                      "detail": f"{len(q):,} of {len(df):,} rows quarantined ({rate:.3%}); budget {g.get('max_error_rate', 0):.2%}"})
    if (exc["rule"] == "missing_column").any():
        res.gates.append({"gate": "required_columns", "status": "FAIL",
                          "detail": ", ".join(exc.loc[exc.rule == "missing_column", "column"])})
    return res


def batch_gates(res: AuditResult, contract: dict, manifest: dict | None, df: pd.DataFrame) -> AuditResult:
    """Gates that need run context (manifest from the pull), not just rows."""
    g = contract.get("batch_gates", {})
    if manifest:
        if g.get("require_reconciled_count"):
            ok = manifest["server_count"] == manifest["rows_fetched"]
            res.gates.append({"gate": "reconciled_count", "status": "PASS" if ok else "FAIL",
                              "detail": f"server {manifest['server_count']:,} vs fetched {manifest['rows_fetched']:,}"})
        fetched = pd.Timestamp(manifest["fetched_at"]).tz_localize(None)
        newest = df["date_reported"].max()
        lag = (fetched - newest).days
        res.gates.append({"gate": "freshness", "status": "PASS" if lag <= g.get("max_staleness_days", 10) else "FAIL",
                          "detail": f"newest record {newest:%Y-%m-%d}, {lag} days before pull"})
        exp = g.get("expected_fingerprint")
        if exp:
            same = exp == manifest["schema_fingerprint"]
            res.gates.append({"gate": "schema_fingerprint", "status": "PASS" if same else "WARN",
                              "detail": f"expected {exp}, got {manifest['schema_fingerprint']}"})
    return res


# ---- deadline / timeliness rules -------------------------------------------
def settled_months(months: pd.Series, as_of: date, settle_days: int) -> pd.Series:
    """True for months that ended at least `settle_days` before `as_of`."""
    end = (months.dt.to_timestamp(how="end")).dt.normalize()
    return (pd.Timestamp(as_of) - end).dt.days >= settle_days


def file_deadline_check(period: str, received: date, due_day: int) -> dict:
    """Agency file for `period` (YYYY-MM) is due on `due_day` of the following month."""
    p = pd.Period(period, "M")
    due = (p + 1).to_timestamp().replace(day=due_day).date()
    late = (received - due).days
    return {"period": period, "due": due.isoformat(), "received": received.isoformat(),
            "days_late": max(late, 0), "status": "LATE" if late > 0 else "ON_TIME"}


def volume_anomalies(monthly: pd.Series, trailing: int = 12, z: float = 3.5) -> pd.DataFrame:
    """Robust z-score of each month's *per-day* rate vs the trailing window's
    median/MAD. Catches a half-loaded month or a duplicated load before it
    becomes a 'spike' in print.

    Per-day, not raw count: on raw counts every February looks like a 10% drop.
    (The first version used raw counts and flagged Feb 2026 at z=-4.4; the
    per-day rate showed it in line with January.)"""
    days = pd.Series(monthly.index.days_in_month, index=monthly.index)
    rate = monthly / days
    rows = []
    for i in range(trailing, len(rate)):
        win = rate.iloc[i - trailing:i]
        med = win.median()
        mad = (win - med).abs().median() or 1.0
        rz = 0.6745 * (rate.iloc[i] - med) / mad
        rows.append({"month": str(rate.index[i]), "count": int(monthly.iloc[i]),
                     "per_day": round(float(rate.iloc[i]), 1), "trailing_median_per_day": round(float(med), 1),
                     "robust_z": round(float(rz), 2), "flag": abs(rz) > z})
    return pd.DataFrame(rows)


def daily_gaps(dates: pd.Series, start: pd.Timestamp, end: pd.Timestamp, min_per_day: int = 1) -> list[str]:
    """Days in [start, end] with fewer than `min_per_day` records. For a city
    this size a zero-report day is a load failure, not a quiet day."""
    per_day = dates.dt.normalize().value_counts()
    full = pd.date_range(start.normalize(), end.normalize(), freq="D")
    return [d.strftime("%Y-%m-%d") for d in full if per_day.get(d, 0) < min_per_day]
