"""Tolerant reader for agency CSV exports.

Agency exports break naive `pd.read_csv` in predictable ways:
- a title/preamble block above the real header ("Report generated 03/02/2026 ...")
- Windows-1252 bytes (curly quotes, 0x96 dashes) in a file labeled UTF-8
- semicolon or tab delimiters, a trailing "Total:" footer row
- headers renamed between months ("Case #" -> "Report Number")
- dates in three formats in one column

This module handles each explicitly and reports what it did, so the audit log
records *why* a file parsed, not just that it did.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from civicpipe.schema import DATE_COLUMNS, MappingReport, map_columns, schema_fingerprint

DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d %H:%M:%S",
                "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p", "%d-%b-%Y", "%b %d, %Y"]
FOOTER_RE = re.compile(r"^\s*(total|grand total|end of report|page \d+)", re.I)


@dataclass
class ParseLog:
    path: str
    encoding: str = ""
    delimiter: str = ""
    header_row: int = 0
    footer_rows_dropped: int = 0
    schema_fingerprint: str = ""
    mapping: MappingReport | None = None
    unparseable_dates: dict[str, int] = field(default_factory=dict)


def _decode(raw: bytes) -> tuple[str, str]:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise ValueError("undecodable file")


def _find_header(lines: list[str], delimiter: str, max_scan: int = 25) -> int:
    """The header is the first line whose cell count matches the modal cell
    count of the body and whose cells are mostly non-numeric."""
    counts = [len(next(csv.reader([ln], delimiter=delimiter))) for ln in lines[:200] if ln.strip()]
    modal = max(set(counts), key=counts.count) if counts else 1
    for i, ln in enumerate(lines[:max_scan]):
        cells = next(csv.reader([ln], delimiter=delimiter), [])
        if len(cells) == modal and modal > 1:
            alpha = sum(bool(re.search(r"[A-Za-z]", c)) and not re.fullmatch(r"[\d/\-: .]+", c) for c in cells)
            if alpha >= 0.6 * modal:
                return i
    return 0


def parse_dates(s: pd.Series) -> tuple[pd.Series, int]:
    """Try each known format per value; return parsed series and count of failures."""
    s = s.astype("string").str.strip()
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    remaining = s.notna() & (s != "")
    for fmt in DATE_FORMATS:
        if not remaining.any():
            break
        parsed = pd.to_datetime(s[remaining], format=fmt, errors="coerce")
        ok = parsed.notna()
        out.loc[parsed[ok].index] = parsed[ok]
        remaining.loc[parsed[ok].index] = False
    return out, int(remaining.sum())


def read_agency_csv(path: str | Path, agency: str | None = None) -> tuple[pd.DataFrame, ParseLog]:
    path = Path(path)
    text, enc = _decode(path.read_bytes())
    log = ParseLog(path=str(path), encoding=enc)

    sample = "\n".join(text.splitlines()[:50])
    try:
        log.delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        log.delimiter = ","

    lines = text.splitlines()
    log.header_row = _find_header(lines, log.delimiter)
    body = [ln for ln in lines[log.header_row:]]
    kept = [body[0]] + [ln for ln in body[1:] if ln.strip() and not FOOTER_RE.match(ln)]
    log.footer_rows_dropped = len(body) - len(kept)

    raw = pd.read_csv(io.StringIO("\n".join(kept)), sep=log.delimiter, dtype=str,
                      keep_default_na=False, skipinitialspace=True)
    raw.columns = [c.strip() for c in raw.columns]
    log.schema_fingerprint = schema_fingerprint(raw.columns)

    df, log.mapping = map_columns(raw)
    for col in DATE_COLUMNS:
        if col in df.columns:
            df[col], bad = parse_dates(df[col])
            if bad:
                log.unparseable_dates[col] = bad
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].str.strip().replace("", pd.NA)
    if agency and "agency" not in df.columns:
        df["agency"] = agency
    return df, log
