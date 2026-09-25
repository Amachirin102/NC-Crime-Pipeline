"""Turn agency "monthly offense summary" PDFs into structured rows.

Small agencies often publish only a PDF: a title block (agency, reporting
period) followed by a table of offense counts that may span pages, repeat its
header on each page, carry footnote markers ("14*"), and use an em dash for
zero. The layout changes between years (columns reordered, renamed, a
"% Change" column added).

Strategy: pull metadata from page text with regexes, pull tables with
pdfplumber, locate the header row in each table by alias (never by position),
and coerce counts strictly - anything that is not a clean integer after
removing known decorations becomes an exception row, not a silent zero.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
import pdfplumber

COL_ALIASES = {
    "offense_code": ["nibrs code", "code", "nibrs", "ucr code", "offense code"],
    "offense_desc": ["offense", "offense category", "offense description", "crime", "category"],
    "count": ["month count", "current month", "count", "this month", "incidents", "total"],
    "prior_year_count": ["prior year same month", "same month last year", "prior year", "last year"],
}
_ALIAS = {a: canon for canon, al in COL_ALIASES.items() for a in al}

AGENCY_RE = re.compile(r"(?:Agency|Reporting Agency|Department)\s*:\s*(.+)", re.I)
PERIOD_RE = re.compile(r"(?:Reporting Period|Period|Month)\s*:\s*([A-Za-z]+\.?\s+\d{4}|\d{1,2}/\d{4})", re.I)
DASHES = {"—", "–", "-", ""}


@dataclass
class PdfParseLog:
    path: str
    agency: str | None = None
    period: str | None = None
    pages: int = 0
    tables_found: int = 0
    header_variants: list[list[str]] = field(default_factory=list)
    exceptions: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    printed_total: int | None = None


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower().rstrip(":")


def _to_int(cell) -> int | None:
    s = str(cell or "").strip()
    if s in DASHES:
        return 0
    s = re.sub(r"[*†‡]+$", "", s).replace(",", "")
    return int(s) if re.fullmatch(r"\d+", s) else None


def _parse_period(p: str) -> str:
    p = p.replace(".", "").strip()
    for fmt in ("%B %Y", "%b %Y", "%m/%Y"):
        try:
            return datetime.strptime(p, fmt).strftime("%Y-%m")
        except ValueError:
            pass
    raise ValueError(f"unrecognized reporting period: {p!r}")


def _reconcile(df: pd.DataFrame, log: PdfParseLog, jump_ratio: float = 10.0) -> None:
    """Cross-check the extraction against the document itself."""
    if log.printed_total is not None:
        parsed = int(df["count"].sum())
        if parsed != log.printed_total:
            log.warnings.append(f"row sum {parsed} != printed total {log.printed_total}")
    elif len(df):
        log.warnings.append("no printed total found; extraction could not be reconciled")
    # a 10x jump vs. the same month last year is far more often a bad cell
    # (merged digits, misread column) than a real change - send it to a human
    prior = df["prior_year_count"].fillna(0).clip(lower=1)
    for r in df[(df["count"] >= 20) & (df["count"] / prior >= jump_ratio)].itertuples():
        log.warnings.append(f"implausible jump for {r.offense_code} {r.offense_desc}: "
                            f"{r.prior_year_count} -> {r.count}")


def parse_summary_pdf(path: str | Path) -> tuple[pd.DataFrame, PdfParseLog]:
    path = Path(path)
    log = PdfParseLog(path=str(path))
    rows: list[dict] = []
    colmap: dict[int, str] | None = None

    with pdfplumber.open(path) as pdf:
        log.pages = len(pdf.pages)
        first_text = pdf.pages[0].extract_text() or ""
        if m := AGENCY_RE.search(first_text):
            log.agency = m.group(1).strip()
        if m := PERIOD_RE.search(first_text):
            log.period = _parse_period(m.group(1))

        for page_no, page in enumerate(pdf.pages, 1):
            for table in page.extract_tables():
                log.tables_found += 1
                for r in table:
                    cells = [_norm(c) for c in r]
                    hits = {i: _ALIAS[c] for i, c in enumerate(cells) if c in _ALIAS}
                    if "count" in hits.values() and len(hits) >= 2:
                        colmap = hits            # header row (possibly repeated per page)
                        if cells not in log.header_variants:
                            log.header_variants.append(cells)
                        continue
                    if colmap is None or not any(cells):
                        continue
                    rec = {canon: r[i] for i, canon in colmap.items() if i < len(r)}
                    # the "Total" label moves between columns across layouts
                    if any(c.startswith("total") for c in cells):
                        log.printed_total = _to_int(rec.get("count"))
                        continue
                    n = _to_int(rec.get("count"))
                    if n is None:
                        log.exceptions.append({"page": page_no, "row": r, "reason": "non-integer count"})
                        continue
                    rows.append({
                        "offense_code": (rec.get("offense_code") or "").strip().upper() or None,
                        "offense_desc": (rec.get("offense_desc") or "").strip(),
                        "count": n,
                        "prior_year_count": _to_int(rec.get("prior_year_count")),
                    })

    df = pd.DataFrame(rows, columns=["offense_code", "offense_desc", "count", "prior_year_count"])
    _reconcile(df, log)
    df.insert(0, "month", log.period)
    df.insert(0, "agency", log.agency)
    return df, log
