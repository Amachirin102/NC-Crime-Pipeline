"""Normalization for names, agencies, jurisdictions, and street addresses.

Normalization is deliberately boring and deterministic: it removes variation
that carries no identity information (case, punctuation, USPS suffix
spelling) so that the fuzzy matcher only has to deal with real ambiguity.
"""
from __future__ import annotations

import re
import unicodedata

AGENCY_TOKENS = {
    "PD": "POLICE DEPARTMENT", "P D": "POLICE DEPARTMENT", "POLICE DEPT": "POLICE DEPARTMENT",
    "DEPT": "DEPARTMENT", "SO": "SHERIFFS OFFICE", "SHERIFF OFFICE": "SHERIFFS OFFICE",
    "CO": "COUNTY", "CNTY": "COUNTY", "TWP": "TOWNSHIP", "UNIV": "UNIVERSITY",
    "CMPD": "CHARLOTTE MECKLENBURG POLICE DEPARTMENT",
}
AGENCY_STOP = {"THE", "OF", "TOWN", "CITY", "VILLAGE", "NC", "NORTH", "CAROLINA"}

USPS_SUFFIX = {
    "STREET": "ST", "STR": "ST", "AVENUE": "AV", "AVE": "AV", "BOULEVARD": "BV", "BLVD": "BV",
    "DRIVE": "DR", "DRV": "DR", "ROAD": "RD", "LANE": "LN", "COURT": "CT", "CIRCLE": "CIR",
    "PLACE": "PL", "PARKWAY": "PKWY", "PKY": "PKWY", "HIGHWAY": "HWY", "TERRACE": "TER",
    "TRAIL": "TRL", "WAY": "WY", "SQUARE": "SQ", "EXPRESSWAY": "EXPY", "CROSSING": "XING",
}
# CMPD's own data uses AV and BV (not AVE/BLVD), so we normalize toward the
# dominant local convention rather than strict USPS Pub. 28.
DIRECTIONAL = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
               "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW"}
UNIT_RE = re.compile(r"(?:\b(?:APT|APARTMENT|UNIT|STE|SUITE|LOT|RM|ROOM)\b|#)\s*#?\s*([A-Z0-9-]+)\b")


def ascii_upper(s: str | None) -> str:
    if s is None:
        return ""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return s.upper()


def squash(s: str) -> str:
    s = re.sub(r"[^A-Z0-9# ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_agency(name: str | None) -> str:
    s = squash(ascii_upper(name).replace("'", "").replace("-", " "))
    for short, full in sorted(AGENCY_TOKENS.items(), key=lambda kv: -len(kv[0])):
        s = re.sub(rf"\b{short}\b", full, s)
    return " ".join(t for t in s.split() if t not in AGENCY_STOP)


def normalize_address(addr: str | None) -> tuple[str, str | None]:
    """Return (street_line, unit). Unit is split out because it is the part most
    often missing or formatted differently between two records for one place."""
    s = ascii_upper(addr).replace(".", "")
    unit = None
    if m := UNIT_RE.search(s):
        unit = m.group(1)
        s = s[:m.start()] + s[m.end():]
    s = squash(s.replace("#", " "))
    toks = [DIRECTIONAL.get(t, USPS_SUFFIX.get(t, t)) for t in s.split()]
    # ordinal words -> digits for the common cases
    ords = {"FIRST": "1ST", "SECOND": "2ND", "THIRD": "3RD", "FOURTH": "4TH", "FIFTH": "5TH"}
    toks = [ords.get(t, t) for t in toks]
    return " ".join(toks), unit


NAME_SUFFIX = {"JR", "SR", "II", "III", "IV"}  # not "V": collides with a middle initial


def normalize_person_name(name: str | None) -> tuple[str, str]:
    """Return (name_without_suffix, generational_suffix). The suffix is kept
    separately because JR vs SR is the classic same-name/different-person trap."""
    s = squash(ascii_upper(name).replace("-", " ").replace("'", ""))
    toks = s.split()
    suffix = next((t for t in toks if t in NAME_SUFFIX), "")
    return " ".join(t for t in toks if t not in NAME_SUFFIX), suffix
