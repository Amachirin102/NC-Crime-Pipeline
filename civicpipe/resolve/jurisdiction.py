"""Resolve free-text jurisdiction values ("CHARLOTE", "CHARLOTTE, NC 28211",
"28215", "MATHEWS") to a canonical municipality.

Resolution is tiered, and every result records *which* tier produced it, so
a reviewer can audit the fuzzy and ZIP-inferred matches separately from the
exact ones:

  1. exact      - cleaned value equals a gazetteer name or known alias
  2. prefix     - a gazetteer name followed by junk (form residue)
  3. fuzzy      - edit-distance similarity >= threshold, with a clear margin
                  over the runner-up (so a typo can't split between two towns)
  4. zip        - value is (or contains) a ZIP that maps to one municipality
  5. unresolved - left as-is and counted; never guessed. Real towns outside
                  the county (MIDLAND, FAYETTEVILLE) correctly land here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd
from rapidfuzz import fuzz, process

# Incorporated municipalities in Mecklenburg County, plus the county itself
# for unincorporated areas.
GAZETTEER = ["CHARLOTTE", "CORNELIUS", "DAVIDSON", "HUNTERSVILLE", "MATTHEWS",
             "MINT HILL", "PINEVILLE", "MECKLENBURG COUNTY (UNINCORPORATED)"]
ALIASES = {"MECKLENBURG": "MECKLENBURG COUNTY (UNINCORPORATED)", "CLT": "CHARLOTTE"}

# ZIP -> predominant municipality. ZIPs do not follow city limits, so this is
# an inference, scored lower than a name match and reported as method="zip".
# ZIPs known to be split between two towns (e.g. 28227, Charlotte/Mint Hill)
# are left out on purpose: a ZIP that could mean two places resolves to nothing.
# ASSUMPTION: review this table against county GIS before production use.
ZIP_UNIQUE = {"28031": "CORNELIUS", "28036": "DAVIDSON", "28078": "HUNTERSVILLE",
              "28105": "MATTHEWS", "28134": "PINEVILLE"}
ZIP_UNIQUE.update({z: "CHARLOTTE" for z in (
    "28202", "28203", "28204", "28205", "28206", "28207", "28208", "28209", "28210",
    "28211", "28212", "28213", "28214", "28215", "28216", "28217", "28219", "28226",
    "28244", "28262", "28269", "28273", "28277", "28278", "28280", "28282")})


@dataclass(frozen=True)
class Resolution:
    raw: str
    canonical: str | None
    method: str
    score: float


def _clean(raw: str) -> tuple[str, str | None]:
    s = str(raw or "").upper()
    zip_m = re.search(r"\b(\d{5})(?:-?\d{4})?\b", s)
    s = re.sub(r"\d+", " ", s)                    # "CHARLOTTE6090" -> "CHARLOTTE"
    s = re.sub(r"\b(NORTH CAROLINA|UNITED STATES( OF AMERICA)?|USA)\b", " ", s)
    s = re.sub(r",?\s*\bN\.?\s*C\.?\b", " ", s)   # ", NC"
    s = re.sub(r"[^A-Z ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip(), (zip_m.group(1) if zip_m else None)


def resolve_one(raw: str, threshold: float = 80.0) -> Resolution:
    name, zip5 = _clean(raw)
    if name in GAZETTEER:
        return Resolution(raw, name, "exact", 100.0)
    if name in ALIASES:
        return Resolution(raw, ALIASES[name], "exact", 100.0)
    # web-form residue glued onto a valid name: "CHARLOTTEJAVASCRIPT VOID PT SU"
    prefix = [g for g in GAZETTEER if name.startswith(g) and len(g) >= 6]
    if len(prefix) == 1:
        return Resolution(raw, prefix[0], "prefix", 80.0)
    if len(name) >= 4:
        hits = process.extract(name, GAZETTEER, scorer=fuzz.ratio, limit=2)
        top = hits[0]
        runner_up = hits[1][1] if len(hits) > 1 else 0
        # require a clear winner, not just a high score
        if top[1] >= threshold and top[1] - runner_up >= 10:
            return Resolution(raw, top[0], "fuzzy", round(top[1], 1))
    if zip5 and zip5 in ZIP_UNIQUE:
        return Resolution(raw, ZIP_UNIQUE[zip5], "zip", 70.0)
    return Resolution(raw, None, "unresolved", 0.0)


def resolve_series(values: pd.Series) -> tuple[pd.Series, pd.DataFrame]:
    """Resolve each distinct value once; return canonical series and a crosswalk
    table (raw value, canonical, method, score, rows affected)."""
    counts = values.fillna("").value_counts()
    xwalk = pd.DataFrame([resolve_one(v).__dict__ | {"rows": int(n)} for v, n in counts.items()])
    mapping = dict(zip(xwalk["raw"], xwalk["canonical"]))
    return values.fillna("").map(mapping), xwalk.sort_values("rows", ascending=False)
