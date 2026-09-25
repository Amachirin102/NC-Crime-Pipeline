"""Diff this pull against the last accepted pull.

Open-data portals restate history: incidents get reclassified (a larceny
becomes a robbery), unfounded reports get removed, clearance fields update.
If last month's chart said "412 robberies in June" and this month's data says
397, a newsroom needs to know that *before* publishing, and why.

Output: record-level changes (added / removed / field changed) and a
month x offense-group restatement table showing how much each already-
published number moved.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from civicpipe.schema import offense_group

TRACKED = ["date_reported", "offense_code", "clearance_status", "city", "address"]


@dataclass
class SnapshotDiff:
    added: pd.DataFrame
    removed: pd.DataFrame
    changed: pd.DataFrame          # one row per (record, field) change
    restatements: pd.DataFrame     # month x group: old count, new count, delta

    def headline(self) -> dict:
        return {"added": len(self.added), "removed": len(self.removed),
                "records_changed": self.changed["incident_id"].nunique() if len(self.changed) else 0,
                "field_changes": self.changed.groupby("field").size().to_dict() if len(self.changed) else {},
                "months_restated": int((self.restatements["delta"] != 0).sum()) if len(self.restatements) else 0}


def _monthly(df: pd.DataFrame) -> pd.DataFrame:
    return (df.assign(month=df["date_reported"].dt.to_period("M").astype(str),
                      group=df["offense_code"].map(offense_group))
              .groupby(["month", "group"]).size())


def diff_snapshots(old: pd.DataFrame, new: pd.DataFrame, key: str = "incident_id",
                   overlap_from: str | None = None) -> SnapshotDiff:
    """`overlap_from` limits the comparison to the date range both pulls cover,
    so a longer lookback window isn't mistaken for a flood of 'added' records."""
    if overlap_from:
        cut = pd.Timestamp(overlap_from)
        old = old[old["date_reported"] >= cut]
        new = new[new["date_reported"] >= cut]
    # records newer than the old pull's horizon aren't restatements, just new data
    horizon = old["date_reported"].max()
    new_in_scope = new[new["date_reported"] <= horizon]

    o, n = old.set_index(key), new_in_scope.set_index(key)
    added = n.loc[n.index.difference(o.index)].reset_index()
    removed = o.loc[o.index.difference(n.index)].reset_index()

    common = o.index.intersection(n.index)
    changes = []
    for f in [c for c in TRACKED if c in o.columns and c in n.columns]:
        a, b = o.loc[common, f], n.loc[common, f]
        neq = ~((a == b) | (a.isna() & b.isna()))
        for k in neq[neq].index:
            changes.append({key: k, "field": f, "old": a[k], "new": b[k]})
    changed = pd.DataFrame(changes, columns=[key, "field", "old", "new"])

    mo, mn = _monthly(old), _monthly(new_in_scope)
    rest = pd.concat({"old": mo, "new": mn}, axis=1).fillna(0).astype(int)
    rest["delta"] = rest["new"] - rest["old"]
    rest["pct"] = (100 * rest["delta"] / rest["old"].where(rest["old"] > 0)).round(2)
    return SnapshotDiff(added, removed, changed, rest.reset_index())
