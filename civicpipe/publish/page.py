"""Render the public page: one self-contained HTML file (no external requests),
safe to drop into a newsroom CMS or GitHub Pages."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

TEMPLATE = Path(__file__).with_name("template.html")


def render_page(monthly: pd.DataFrame, yoy: dict, audit_summary: pd.DataFrame, gates: list[dict],
                manifest: dict, settle_days: int, unfounded: int, unfounded_pct: float,
                out: Path) -> Path:
    data = {
        "months": [{"month": str(r.month), "violent": int(r.violent), "property": int(r.property),
                    "settled": bool(r.settled)} for r in monthly.itertuples()],
        "yoy": yoy,
        "gates": gates,
        "exceptions": audit_summary.to_dict(orient="records"),
        "fetched_at": manifest["fetched_at"],
        "rows": manifest["rows_fetched"],
        "newest": str(manifest.get("max_date_reported", ""))[:10],
        "settle_days": settle_days,
        "unfounded": unfounded,
        "unfounded_pct": unfounded_pct,
    }
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", json.dumps(data))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out
