"""civicpipe command line.

  python -m civicpipe run   [--since 2022-01-01] [--offline]   full monthly pipeline
  python -m civicpipe audit-file PATH --period 2026-08 --received 2026-09-18
  python -m civicpipe parse-pdf PATH
  python -m civicpipe er-eval                                   entity resolution benchmark
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

from civicpipe.audit import engine
from civicpipe.audit.diff import diff_snapshots
from civicpipe.schema import map_columns, offense_group

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "cmpd_incidents"
OUT = ROOT / "output"
CONTRACT = ROOT / "config" / "contracts" / "incidents.yaml"
REVIEWED = ROOT / "config" / "reviewed.yaml"
# The last pull that passed every gate, trimmed to the fields the diff tracks
# (~4.6 MB). Committed to git and overwritten only after a passing run, so CI
# always diffs against what was last *published*, not merely last attempted.
ACCEPTED = ROOT / "data" / "snapshots" / "accepted.parquet"
ACCEPTED_MANIFEST = ACCEPTED.with_name("accepted_manifest.json")


def _latest_snapshot() -> Path | None:
    snaps = sorted(p for p in RAW.glob("*") if (p / "manifest.json").exists())
    return snaps[-1] if snaps else None


def _save_accepted(df: pd.DataFrame, manifest: dict) -> None:
    from civicpipe.audit.diff import TRACKED
    ACCEPTED.parent.mkdir(parents=True, exist_ok=True)
    df[["incident_id", *TRACKED]].to_parquet(ACCEPTED, compression="zstd", index=False)
    ACCEPTED_MANIFEST.write_text(json.dumps(manifest, indent=2))


def _load(snap: Path) -> tuple[pd.DataFrame, dict]:
    raw = pd.read_parquet(snap / "incidents.parquet")
    manifest = json.loads((snap / "manifest.json").read_text())
    df, rep = map_columns(raw)
    if not rep.ok:
        sys.exit(f"schema mapping failed for {snap}: missing={rep.missing_required} collisions={rep.collisions}")
    return df, manifest


def cmd_run(args) -> int:
    from civicpipe.publish.aggregate import monthly_counts, trailing_12_change
    from civicpipe.publish.chart import trend_png
    from civicpipe.publish.page import render_page
    from civicpipe.resolve.jurisdiction import resolve_series

    if not args.offline:
        from civicpipe.ingest.arcgis import pull_cmpd
        pull_cmpd(args.since, ROOT / "data" / "raw")

    snap = _latest_snapshot()
    if snap is None:
        sys.exit("no snapshots found; run without --offline first")
    df, manifest = _load(snap)
    contract = engine.load_contract(CONTRACT)
    as_of = pd.Timestamp(manifest["fetched_at"]).date()
    OUT.mkdir(exist_ok=True)

    # 1. normalize jurisdictions (entity resolution on a real messy field)
    df["jurisdiction"], xwalk = resolve_series(df["city"])
    xwalk.to_csv(OUT / "jurisdiction_crosswalk.csv", index=False)

    # 2. validate
    res = engine.validate(df, contract, today=as_of)
    res = engine.batch_gates(res, contract, manifest, df)
    res.exceptions.to_csv(OUT / "exceptions.csv", index=False)
    summary = res.summary()

    # 3. volume anomaly check on the full monthly series
    allm = df.groupby(df["date_reported"].dt.to_period("M")).size()
    settled = engine.settled_months(pd.Series(allm.index), as_of, contract["deadlines"]["month_settle_days"])
    anomalies = engine.volume_anomalies(allm[settled.values], contract["volume"]["trailing_months"],
                                        contract["volume"]["robust_z_warn"])
    anomalies.to_csv(OUT / "volume_check.csv", index=False)
    flagged = anomalies[anomalies.flag] if len(anomalies) else anomalies
    reviewed = (yaml.safe_load(REVIEWED.read_text()) or {}).get("volume_anomaly", {}) if REVIEWED.exists() else {}
    for r in flagged.itertuples():
        note = reviewed.get(r.month)
        res.gates.append({"gate": "volume_anomaly", "status": "REVIEWED" if note else "WARN",
                          "detail": f"{r.month}: {r.per_day}/day vs trailing median {r.trailing_median_per_day} "
                                    f"(robust z={r.robust_z})" + (f". {note}" if note else ". Needs human review.")})
    if not len(flagged):
        res.gates.append({"gate": "volume_anomaly", "status": "PASS",
                          "detail": "every settled month's reports-per-day within robust z 3.5 of the trailing 12"})
    gaps = engine.daily_gaps(df["date_reported"], df["date_reported"].min(), df["date_reported"].max())
    res.gates.append({"gate": "daily_gaps", "status": "FAIL" if gaps else "PASS",
                      "detail": f"{len(gaps)} days with zero reports" + (f": {', '.join(gaps[:10])}" if gaps else "")})

    # 4. diff vs previous accepted snapshot
    diff_head = None
    prev = json.loads(ACCEPTED_MANIFEST.read_text()) if ACCEPTED_MANIFEST.exists() else None
    if prev and prev["fetched_at"] != manifest["fetched_at"]:
        old = pd.read_parquet(ACCEPTED)
        d = diff_snapshots(old, df, overlap_from=args.since)
        diff_head = d.headline()
        d.changed.to_csv(OUT / "diff_changed_fields.csv", index=False)
        d.restatements.to_csv(OUT / "diff_restatements.csv", index=False)
        # restatements are normal for this source; WARN makes them visible, it doesn't block
        res.gates.append({"gate": "restatements", "status": "WARN" if diff_head["months_restated"] else "PASS",
                          "detail": f"vs pull of {prev['fetched_at'][:10]}: +{diff_head['added']:,} / -{diff_head['removed']:,} "
                                    f"records, {diff_head['records_changed']:,} changed, "
                                    f"{diff_head['months_restated']} month-group counts moved"})
    else:
        res.gates.append({"gate": "restatements", "status": "SKIP",
                          "detail": "no earlier accepted pull to diff against yet"})

    report = {"snapshot": snap.name, "as_of": str(as_of), "passed": res.passed, "gates": res.gates,
              "exceptions": summary.to_dict(orient="records"), "diff": diff_head,
              "jurisdiction_methods": xwalk.groupby("method").rows.sum().to_dict()}
    (OUT / "audit_report.json").write_text(json.dumps(report, indent=2, default=str))
    _print_gates(res)

    if not res.passed:
        print("\nAUDIT FAILED - not publishing. See output/exceptions.csv")
        return 1

    # 5. publish (quarantined rows excluded)
    clean = df[~df["incident_id"].astype(str).isin(res.quarantined_keys)]
    settle = contract["deadlines"]["month_settle_days"]
    m = monthly_counts(clean, as_of, settle)
    m = m[m["month"] >= pd.Period(args.chart_from, "M")]
    m.assign(month=m.month.astype(str)).to_csv(OUT / "monthly_counts.csv", index=False)
    yoy = {k: trailing_12_change(m, k) for k in ("violent", "property")}

    vp = clean[clean["offense_code"].map(offense_group).isin(["violent", "property"])]
    unf = int((vp["clearance_status"].fillna("").str.upper() == "UNFOUNDED").sum())
    note = (f"Source: City of Charlotte Open Data, CMPD Incidents (pulled {as_of}). Counts are reports by date "
            f"reported, highest offense per report, unfounded reports excluded. Hollow points: preliminary.")
    trend_png(m, OUT / "cmpd_monthly_trend.png", note)
    render_page(m, yoy, summary, res.gates, manifest, settle, unf, round(100 * unf / max(len(vp), 1), 1),
                OUT / "index.html")
    _save_accepted(df, manifest)
    print(f"\npublished -> {OUT / 'index.html'}")
    print(json.dumps(yoy, indent=2))
    return 0


def _print_gates(res):
    print("\nGATES")
    for g in res.gates:
        print(f"  [{g['status']:4}] {g['gate']:20} {g['detail']}")
    print("\nEXCEPTIONS")
    print(res.summary().to_string(index=False) if len(res.exceptions) else "  none")


def cmd_audit_file(args) -> int:
    from civicpipe.ingest.csv_source import read_agency_csv
    df, log = read_agency_csv(args.path)
    print(f"parsed {len(df)} rows | encoding={log.encoding} delimiter={log.delimiter!r} "
          f"header_row={log.header_row} footer_dropped={log.footer_rows_dropped}")
    print(f"mapped: {log.mapping.mapped}\nunmapped (new upstream fields?): {log.mapping.unmapped}")
    if log.unparseable_dates:
        print(f"unparseable dates: {log.unparseable_dates}")
    contract = engine.load_contract(args.contract)
    res = engine.validate(df, contract, today=date.fromisoformat(args.received))
    if args.period:
        dl = engine.file_deadline_check(args.period, date.fromisoformat(args.received),
                                        contract["deadlines"]["file_due_day_of_following_month"])
        res.gates.append({"gate": "file_deadline", "status": "WARN" if dl["status"] == "LATE" else "PASS",
                          "detail": f"period {dl['period']} due {dl['due']}, received {dl['received']}"
                                    + (f" ({dl['days_late']} days late)" if dl["days_late"] else "")})
        in_period = df["date_reported"].dt.to_period("M") == pd.Period(args.period, "M")
        res.gates.append({"gate": "period_coverage", "status": "PASS" if in_period.all() else "WARN",
                          "detail": f"{(~in_period).sum()} rows fall outside {args.period}"})
    _print_gates(res)
    if args.out:
        res.exceptions.to_csv(args.out, index=False)
    return 0 if res.passed else 1


def cmd_parse_pdf(args) -> int:
    from civicpipe.ingest.pdf_source import parse_summary_pdf
    df, log = parse_summary_pdf(args.path)
    print(f"agency={log.agency!r} period={log.period} pages={log.pages} tables={log.tables_found}")
    print(f"header variants seen: {log.header_variants}")
    print(df.to_string(index=False))
    for e in log.exceptions:
        print(f"EXCEPTION page {e['page']}: {e['reason']}: {e['row']}")
    for w in log.warnings:
        print(f"WARNING: {w}")
    if args.out:
        df.to_csv(args.out, index=False)
    return 0 if not log.exceptions else 2


def cmd_er_eval(args) -> int:
    from civicpipe.resolve.evaluate import evaluate
    from civicpipe.resolve.synth import generate
    dev_a, dev_b = generate(args.n, seed=7)
    dev = evaluate(dev_a, dev_b)
    t = dev["chosen_threshold"]
    test_a, test_b = generate(args.n, seed=2026)
    test = evaluate(test_a, test_b, fixed_threshold=t)
    OUT.mkdir(exist_ok=True)
    out = {"threshold_chosen_on_dev": t, "dev": {k: dev[k] for k in ("records", "at_threshold")},
           "test": {k: test[k] for k in ("records", "blocking", "baseline_exact", "at_threshold")},
           "test_sweep": test["sweep"].to_dict(orient="records"),
           "test_recall_by_corruption": test["recall_by_corruption"].round(4).reset_index().to_dict(orient="records")}
    (OUT / "er_eval.json").write_text(json.dumps(out, indent=2))
    print(f"threshold chosen on dev set: {t}")
    print(f"TEST  records {test['records']}")
    print(f"      blocking  {test['blocking']}")
    print(f"      baseline  {test['baseline_exact']}")
    print(f"      linker    {test['at_threshold']}")
    print(test["sweep"].to_string(index=False))
    print(test["recall_by_corruption"].round(3).to_string())
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="civicpipe")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--since", default="2022-01-01")
    r.add_argument("--chart-from", default="2023-01")
    r.add_argument("--offline", action="store_true", help="reuse latest snapshot instead of pulling")
    r.set_defaults(fn=cmd_run)
    a = sub.add_parser("audit-file")
    a.add_argument("path")
    a.add_argument("--contract", default=str(CONTRACT))
    a.add_argument("--period")
    a.add_argument("--received", default=str(date.today()))
    a.add_argument("--out")
    a.set_defaults(fn=cmd_audit_file)
    pp = sub.add_parser("parse-pdf")
    pp.add_argument("path")
    pp.add_argument("--out")
    pp.set_defaults(fn=cmd_parse_pdf)
    e = sub.add_parser("er-eval")
    e.add_argument("--n", type=int, default=1200)
    e.set_defaults(fn=cmd_er_eval)
    args = p.parse_args(argv)
    return args.fn(args)
