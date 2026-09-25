"""Turn validated incident rows into the monthly series the public chart shows."""
from __future__ import annotations

from datetime import date

import pandas as pd

from civicpipe.audit.engine import settled_months
from civicpipe.schema import offense_group


def monthly_counts(df: pd.DataFrame, as_of: date, settle_days: int,
                   exclude_unfounded: bool = True) -> pd.DataFrame:
    d = df.copy()
    if exclude_unfounded and "clearance_status" in d:
        d = d[d["clearance_status"].fillna("").str.upper() != "UNFOUNDED"]
    d["group"] = d["offense_code"].map(offense_group)
    d["month"] = d["date_reported"].dt.to_period("M")
    out = (d[d.group.isin(["violent", "property"])]
           .groupby(["month", "group"]).size().unstack(fill_value=0).sort_index())
    out = out.reset_index()
    # A month the data doesn't cover end-to-end is never charted: 22 days of
    # September next to 31 days of August reads as a plunge that didn't happen.
    through = df["date_reported"].max().normalize()
    out = out[out["month"].dt.to_timestamp(how="end").dt.normalize() <= through]
    out["settled"] = settled_months(out["month"], as_of, settle_days).values
    return out.reset_index(drop=True)


def trailing_12_change(m: pd.DataFrame, col: str) -> dict:
    s = m[m.settled].set_index("month")[col]
    if len(s) < 24:
        return {}
    last, prior = s.iloc[-12:], s.iloc[-24:-12]
    return {"window": f"{last.index[0]} to {last.index[-1]}",
            "prior_window": f"{prior.index[0]} to {prior.index[-1]}",
            "last_12": int(last.sum()), "prior_12": int(prior.sum()),
            "pct_change": round(100 * (last.sum() - prior.sum()) / prior.sum(), 1)}
