from datetime import date
from pathlib import Path

import pandas as pd

from civicpipe.audit import engine
from civicpipe.audit.diff import diff_snapshots
from civicpipe.ingest.csv_source import read_agency_csv

ROOT = Path(__file__).parents[1]
FIX = ROOT / "tests" / "fixtures"
AGENCY = engine.load_contract(ROOT / "config" / "contracts" / "agency_incidents.yaml")
CMPD = engine.load_contract(ROOT / "config" / "contracts" / "incidents.yaml")


def test_planted_defects_are_all_caught():
    df, _ = read_agency_csv(FIX / "harlow_so_2026-08.csv")
    res = engine.validate(df, AGENCY, today=date(2026, 9, 18))
    got = set(zip(res.exceptions.rule, res.exceptions.record_key))
    assert ("duplicate_key", "HC-2026-0802") in got
    assert ("future_date", "HC-2026-0804") in got
    assert ("required_null", "HC-2026-0805") in got
    assert ("out_of_range", "HC-2026-0806") in got
    assert ("occurred_after_reported", "HC-2026-0803") in got
    assert res.quarantined_keys == {"HC-2026-0802", "HC-2026-0804", "HC-2026-0805"}
    assert not res.passed                              # error budget blown -> no publish


def test_clean_file_passes():
    df, _ = read_agency_csv(FIX / "pinecrest_pd_2026-09.csv")
    assert engine.validate(df, AGENCY, today=date(2026, 9, 18)).passed


def test_missing_required_column_fails_gate():
    df = pd.DataFrame({"incident_id": ["x"], "date_reported": [pd.Timestamp("2026-01-01")]})
    res = engine.validate(df, AGENCY, today=date(2026, 9, 18))
    assert any(g["gate"] == "required_columns" and g["status"] == "FAIL" for g in res.gates)


def _cmpd_rows():
    return pd.DataFrame({
        "incident_id": ["20260801-0001-00", "20260801-0002-00", "20260805-0003-00", "20260806-0004-00"],
        "date_reported": pd.to_datetime(["2026-08-01", "2026-08-02", "2026-08-05", "2026-08-06 20:00"], format="mixed"),
        "date_occurred": pd.to_datetime(["2026-07-31", "2026-08-01", "2026-08-09", "2026-08-06"]),
        "clearance_date": pd.to_datetime([None, "2026-07-01", None, None]),
        "offense_code": ["13A", "23F", "220", "240"],
        "city": ["CHARLOTTE", "CHARLOTE", "CHARLOTTE", "ZZZ"],
        "jurisdiction": ["CHARLOTTE", "CHARLOTTE", "CHARLOTTE", None],
    })


def test_cmpd_cross_field_rules():
    res = engine.validate(_cmpd_rows(), CMPD, today=date(2026, 9, 18))
    got = set(zip(res.exceptions.rule, res.exceptions.record_key))
    assert ("id_date_mismatch", "20260801-0002-00") in got       # id says 08-01, reported 08-02
    assert ("cleared_before_reported", "20260801-0002-00") in got
    assert ("occurred_after_reported", "20260805-0003-00") in got
    assert ("not_local_midnight", "20260806-0004-00") in got     # timezone bug signature
    assert ("jurisdiction_unresolved", "20260806-0004-00") in got
    assert res.passed                                            # all WARN, nothing quarantined


def test_batch_gates_reconciliation_and_freshness():
    df = _cmpd_rows()
    res = engine.validate(df, CMPD, today=date(2026, 9, 18))
    manifest = {"server_count": 5, "rows_fetched": 4, "fetched_at": "2026-09-18T12:00:00+00:00",
                "schema_fingerprint": "abc"}
    res = engine.batch_gates(res, CMPD, manifest, df)
    gates = {g["gate"]: g["status"] for g in res.gates}
    assert gates["reconciled_count"] == "FAIL"     # a page went missing
    assert gates["freshness"] == "FAIL"            # newest record 43 days old
    assert not res.passed


def test_file_deadline():
    assert engine.file_deadline_check("2026-08", date(2026, 9, 15), 15)["status"] == "ON_TIME"
    late = engine.file_deadline_check("2026-08", date(2026, 9, 18), 15)
    assert late["status"] == "LATE" and late["days_late"] == 3
    assert engine.file_deadline_check("2026-12", date(2027, 1, 10), 15)["due"] == "2027-01-15"


def test_settled_months():
    months = pd.Series(pd.period_range("2026-06", "2026-09", freq="M"))
    assert engine.settled_months(months, date(2026, 9, 24), 30).tolist() == [True, True, False, False]


def test_volume_anomaly_flags_half_loaded_month():
    s = pd.Series([1000, 1020, 980, 1010, 995, 1005, 990, 1015, 1000, 985, 1010, 1000, 480],
                  index=pd.period_range("2025-01", periods=13, freq="M"))
    out = engine.volume_anomalies(s)
    assert out.iloc[-1].flag and out.iloc[-1].robust_z < -3.5


def test_snapshot_diff_detects_restatements():
    old = pd.DataFrame({
        "incident_id": ["a", "b", "c", "d"],
        "date_reported": pd.to_datetime(["2026-07-01", "2026-07-02", "2026-07-03", "2026-08-01"]),
        "offense_code": ["23H", "13A", "220", "240"],
        "clearance_status": ["Open", "Open", "Open", "Open"],
    })
    new = pd.DataFrame({
        "incident_id": ["a", "b", "d", "e", "f"],
        "date_reported": pd.to_datetime(["2026-07-01", "2026-07-02", "2026-08-01", "2026-07-15", "2026-09-01"]),
        "offense_code": ["120", "13A", "240", "23F", "23F"],      # a reclassified theft -> robbery
        "clearance_status": ["Open", "Cleared by Arrest", "Open", "Open", "Open"],
    })
    d = diff_snapshots(old, new)
    assert d.removed.incident_id.tolist() == ["c"]
    assert d.added.incident_id.tolist() == ["e"]          # late report; "f" is just newer data
    changes = set(zip(d.changed.incident_id, d.changed.field))
    assert changes == {("a", "offense_code"), ("b", "clearance_status")}
    r = d.restatements.set_index(["month", "group"])
    assert r.loc[("2026-07", "violent"), "delta"] == 1    # robbery added by reclassification
    assert r.loc[("2026-07", "property"), "delta"] == -1  # theft reclassified, burglary removed, one late report


def test_february_is_not_an_anomaly():
    # constant 250 reports/day: raw counts dip every February, per-day rate doesn't
    idx = pd.period_range("2025-01", "2026-02", freq="M")
    s = pd.Series([250 * p.days_in_month for p in idx], index=idx)
    assert not engine.volume_anomalies(s).flag.any()


def test_daily_gaps():
    d = pd.Series(pd.to_datetime(["2026-08-01", "2026-08-02", "2026-08-04"]))
    assert engine.daily_gaps(d, d.min(), d.max()) == ["2026-08-03"]


def test_severity_follows_whether_field_is_published():
    df = _cmpd_rows()
    df.loc[0, "date_occurred"] = pd.Timestamp("1926-07-19")
    res = engine.validate(df, CMPD, today=date(2026, 9, 18))
    hit = res.exceptions[res.exceptions.rule == "implausibly_old"]
    assert hit.severity.tolist() == ["WARN"]
    assert "20260801-0001-00" not in res.quarantined_keys
