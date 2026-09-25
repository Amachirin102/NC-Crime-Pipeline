"""Canonical incident schema and header mapping that survives schema drift.

Every source (ArcGIS API, agency CSV exports, PDF tables) is mapped onto one
canonical set of column names before anything downstream touches it. Headers
are matched by normalized alias, never by position, so a reordered or renamed
column does not silently shift data into the wrong field.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import pandas as pd

# canonical name -> known aliases (compared after _norm_header)
INCIDENT_ALIASES: dict[str, list[str]] = {
    "incident_id": ["incident_report_id", "incident_id", "report_id", "case_number",
                    "case_no", "case", "report_number", "incident_number", "complaint_no"],
    "agency": ["agency", "agency_name", "reporting_agency", "department", "ori_name"],
    "date_reported": ["date_reported", "reported_date", "report_date", "reported_on",
                      "date_of_report", "reportdate"],
    "date_occurred": ["date_incident_began", "date_occurred", "occurred_date",
                      "offense_date", "incident_date", "date_of_offense", "from_date"],
    "offense_code": ["highest_nibrs_code", "nibrs_code", "offense_code", "ucr_code",
                     "nibrs", "code"],
    "offense_desc": ["highest_nibrs_description", "nibrs_description", "offense_description",
                     "offense", "crime_type", "description", "charge_description"],
    "address": ["location", "address", "street_address", "block_address", "incident_address"],
    "city": ["city", "municipality", "jurisdiction", "town"],
    "zip": ["zip", "zip_code", "zipcode", "postal_code"],
    "division": ["cmpd_patrol_division", "patrol_division", "division", "district", "beat"],
    "clearance_status": ["clearance_status", "status", "case_status", "disposition"],
    "clearance_date": ["clearance_date", "cleared_date", "date_cleared", "disposition_date"],
    "lat": ["latitude_public", "latitude", "lat", "y"],
    "lon": ["longitude_public", "longitude", "lon", "long", "lng", "x"],
}

REQUIRED = ["incident_id", "date_reported", "offense_code"]
DATE_COLUMNS = ["date_reported", "date_occurred", "clearance_date"]


def _norm_header(h: str) -> str:
    h = str(h).strip().lower()
    h = re.sub(r"[^a-z0-9]+", "_", h)
    return h.strip("_")


_ALIAS_INDEX = {_norm_header(a): canon for canon, aliases in INCIDENT_ALIASES.items() for a in aliases}


@dataclass
class MappingReport:
    mapped: dict[str, str] = field(default_factory=dict)      # source header -> canonical
    unmapped: list[str] = field(default_factory=list)         # source headers we ignored
    missing_required: list[str] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)       # 2+ source cols -> 1 canonical

    @property
    def ok(self) -> bool:
        return not self.missing_required and not self.collisions


def map_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, MappingReport]:
    """Rename source columns onto the canonical schema. Unknown columns are dropped
    but reported, so a new upstream field shows up in the audit log instead of vanishing."""
    report = MappingReport()
    rename: dict[str, str] = {}
    for col in df.columns:
        canon = _ALIAS_INDEX.get(_norm_header(col))
        if canon is None:
            report.unmapped.append(col)
        elif canon in rename.values():
            report.collisions.append(f"{col} -> {canon}")
        else:
            rename[col] = canon
    report.mapped = rename
    out = df[list(rename)].rename(columns=rename)
    report.missing_required = [c for c in REQUIRED if c not in out.columns]
    return out, report


def schema_fingerprint(columns) -> str:
    """Order-insensitive hash of a source's raw header set. A changed fingerprint
    between runs means the upstream schema moved and a human should look."""
    norm = sorted(_norm_header(c) for c in columns)
    return hashlib.sha256("|".join(norm).encode()).hexdigest()[:12]


# ---- NIBRS offense grouping -------------------------------------------------
# Group A "crimes against persons" subset commonly reported as violent crime,
# aligned with the FBI's violent-crime definition (murder/nonnegligent
# manslaughter, rape, robbery, aggravated assault).
VIOLENT = {"09A", "11A", "11B", "11C", "120", "13A"}
PROPERTY = {"200", "220", "23A", "23B", "23C", "23D", "23E", "23F", "23G", "23H", "240"}
NON_CRIMINAL_PREFIX = "8"  # CMPD 800-series codes are non-criminal (missing person, etc.)


def offense_group(code: str | None) -> str:
    if code is None or pd.isna(code):
        return "unknown"
    c = str(code).strip().upper()
    if c in VIOLENT:
        return "violent"
    if c in PROPERTY:
        return "property"
    if re.fullmatch(r"8\d\d", c):
        return "non_criminal"
    return "other_criminal"
