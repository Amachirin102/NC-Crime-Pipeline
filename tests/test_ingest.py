from pathlib import Path

import pandas as pd
import pytest

from civicpipe.ingest.csv_source import parse_dates, read_agency_csv
from civicpipe.ingest.pdf_source import parse_summary_pdf
from civicpipe.schema import map_columns, offense_group, schema_fingerprint

FIX = Path(__file__).parent / "fixtures"


def test_preamble_cp1252_and_footer():
    df, log = read_agency_csv(FIX / "pinecrest_pd_2026-08.csv")
    assert log.encoding == "cp1252"
    assert log.header_row == 3
    assert log.footer_rows_dropped == 1          # "Total:" row
    assert len(df) == 8
    assert df["offense_desc"].str.contains("owner’s").any()   # curly apostrophe survived
    # mixed 08/22/2026 and 8/22/26 formats both parsed
    assert df.loc[df.incident_id == "P26-004433", "date_reported"].iloc[0] == pd.Timestamp("2026-08-22")
    assert not log.unparseable_dates


def test_renamed_and_reordered_headers_map_by_name_not_position():
    aug, _ = read_agency_csv(FIX / "pinecrest_pd_2026-08.csv")
    sep, log = read_agency_csv(FIX / "pinecrest_pd_2026-09.csv")
    assert set(aug.columns) == set(sep.columns)
    assert log.mapping.unmapped == ["Officer Unit"]           # new field surfaced, not dropped silently
    assert sep["offense_code"].tolist() == ["23C", "13B", "220", "23F"]
    assert sep["date_reported"].iloc[0] == pd.Timestamp("2026-09-02")


def test_semicolon_delimiter():
    df, log = read_agency_csv(FIX / "harlow_so_2026-08.csv")
    assert log.delimiter == ";"
    assert {"lat", "lon"} <= set(df.columns)


def test_schema_fingerprint_is_order_insensitive_but_catches_renames():
    assert schema_fingerprint(["A", "B"]) == schema_fingerprint(["b", "a"])
    assert schema_fingerprint(["A", "B"]) != schema_fingerprint(["A", "C"])


def test_mapping_reports_missing_required_and_collisions():
    df = pd.DataFrame(columns=["Case #", "Report Number", "Offense"])
    _, rep = map_columns(df)
    assert "date_reported" in rep.missing_required
    assert rep.collisions and not rep.ok


def test_parse_dates_counts_failures():
    s, bad = parse_dates(pd.Series(["2026-01-05", "01/06/2026", "Jan 07, 2026", "not a date", None]))
    assert bad == 1
    assert s.iloc[2] == pd.Timestamp("2026-01-07")


@pytest.mark.parametrize("code,group", [("13A", "violent"), ("09A", "violent"), ("23F", "property"),
                                        ("240", "property"), ("13B", "other_criminal"),
                                        ("899", "non_criminal"), (None, "unknown")])
def test_offense_group(code, group):
    assert offense_group(code) == group


def test_pdf_layout_2025():
    df, log = parse_summary_pdf(FIX / "pinecrest_summary_2025-08.pdf")
    assert log.agency == "Town of Pinecrest Police Department"
    assert log.period == "2025-08"
    assert len(df) == 6 and df["count"].sum() == log.printed_total == 50
    assert df.set_index("offense_code").loc["120", "prior_year_count"] == 0   # em dash -> 0
    assert not log.exceptions and not log.warnings


def test_pdf_layout_2026_drift_multipage_and_defects():
    df, log = parse_summary_pdf(FIX / "pinecrest_summary_2026-08.pdf")
    assert log.period == "2026-08"                     # "Aug. 2026"
    assert log.pages == 2                              # header repeated on page 2
    assert "TOTAL" not in df["offense_code"].tolist()  # total label moved columns
    by = df.set_index("offense_code")["count"]
    assert by["13A"] == 5                              # footnote marker stripped
    assert by["23C"] == 1012                           # thousands separator
    assert [e["row"][0] for e in log.exceptions] == ["290"]   # "n/a" flagged, not zeroed
    assert any("implausible jump" in w and "23C" in w for w in log.warnings)
