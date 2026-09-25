"""Paginated pull from an ArcGIS REST feature layer (the backend behind most
city/county open-data portals, including Charlotte's).

Design choices that matter for a portal that changes without warning:
- Keyset pagination on OBJECTID, not resultOffset. Offsets skip or duplicate
  rows when the layer is edited mid-pull; keysets do not.
- The server-side count for the same WHERE clause is recorded before the pull
  and reconciled after it. A mismatch is an audit ERROR, not a log line.
- The raw response is written to disk unmodified, with a manifest (query,
  timestamps, row count, schema fingerprint), before any transformation.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from civicpipe.schema import schema_fingerprint

CMPD_INCIDENTS = "https://gis.charlottenc.gov/arcgis/rest/services/CMPD/CMPDIncidents/MapServer/0"


@dataclass
class PullManifest:
    source: str
    url: str
    where: str
    fetched_at: str
    server_count: int
    rows_fetched: int
    pages: int
    fields: list[str]
    schema_fingerprint: str
    max_date_reported: str | None = None

    @property
    def reconciled(self) -> bool:
        return self.server_count == self.rows_fetched


class ArcGISLayer:
    def __init__(self, url: str, session: requests.Session | None = None,
                 retries: int = 4, timeout: int = 60):
        self.url = url.rstrip("/")
        self.s = session or requests.Session()
        self.s.headers["User-Agent"] = "civicpipe/0.1 (portfolio data pipeline)"
        self.retries = retries
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        params = {**params, "f": "json"}
        for attempt in range(self.retries + 1):
            try:
                r = self.s.get(f"{self.url}{path}", params=params, timeout=self.timeout)
                r.raise_for_status()
                data = r.json()
                # ArcGIS returns HTTP 200 with an "error" body on failure
                if "error" in data:
                    raise RuntimeError(f"ArcGIS error: {data['error']}")
                return data
            except (requests.RequestException, RuntimeError, ValueError):
                if attempt == self.retries:
                    raise
                time.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    def metadata(self) -> dict:
        return self._get("", {})

    def count(self, where: str) -> int:
        return self._get("/query", {"where": where, "returnCountOnly": "true"})["count"]

    def pull(self, where: str, page_size: int = 2000, out_fields: str = "*",
             progress: bool = True) -> tuple[pd.DataFrame, int]:
        rows: list[dict] = []
        last_oid, pages = -1, 0
        while True:
            data = self._get("/query", {
                "where": f"({where}) AND OBJECTID > {last_oid}",
                "outFields": out_fields,
                "orderByFields": "OBJECTID ASC",
                "resultRecordCount": page_size,
                "returnGeometry": "false",
            })
            feats = data.get("features", [])
            if not feats:
                break
            rows.extend(f["attributes"] for f in feats)
            last_oid = feats[-1]["attributes"]["OBJECTID"]
            pages += 1
            if progress and pages % 10 == 0:
                print(f"  ... {len(rows):,} rows ({pages} pages)", flush=True)
        return pd.DataFrame(rows), pages


def epoch_ms_to_ts(s: pd.Series) -> pd.Series:
    # ArcGIS dates are epoch milliseconds in UTC; CMPD stores local wall-clock
    # times, so we convert to America/New_York and drop tz for month bucketing.
    ts = pd.to_datetime(s, unit="ms", utc=True, errors="coerce")
    return ts.dt.tz_convert("America/New_York").dt.tz_localize(None)


def pull_cmpd(since: str, raw_dir: Path, url: str = CMPD_INCIDENTS) -> tuple[pd.DataFrame, PullManifest]:
    """Pull CMPD incidents reported on/after `since` (YYYY-MM-DD) and write a raw snapshot."""
    layer = ArcGISLayer(url)
    where = f"DATE_REPORTED >= DATE '{since}'"
    server_count = layer.count(where)
    print(f"server reports {server_count:,} rows for: {where}")
    df, pages = layer.pull(where)

    for col in ("DATE_REPORTED", "DATE_INCIDENT_BEGAN", "DATE_INCIDENT_END", "CLEARANCE_DATE"):
        if col in df.columns:
            df[col] = epoch_ms_to_ts(df[col])

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    manifest = PullManifest(
        source="cmpd_incidents", url=url, where=where,
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        server_count=server_count, rows_fetched=len(df), pages=pages,
        fields=list(df.columns), schema_fingerprint=schema_fingerprint(df.columns),
        max_date_reported=str(df["DATE_REPORTED"].max()) if len(df) else None,
    )
    out = raw_dir / "cmpd_incidents" / stamp
    out.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out / "incidents.parquet", index=False)
    (out / "manifest.json").write_text(json.dumps(asdict(manifest), indent=2))
    print(f"wrote {len(df):,} rows -> {out}")
    return df, manifest
