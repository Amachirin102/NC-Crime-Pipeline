import pandas as pd
import pytest

from civicpipe.normalize.text import normalize_address, normalize_agency, normalize_person_name
from civicpipe.resolve.evaluate import evaluate
from civicpipe.resolve.jurisdiction import resolve_one
from civicpipe.resolve.person import link
from civicpipe.resolve.synth import generate


@pytest.mark.parametrize("raw,canon,method", [
    ("CHARLOTTE", "CHARLOTTE", "exact"),
    ("CHARLOTE", "CHARLOTTE", "fuzzy"),
    ("CHAROLOTTE", "CHARLOTTE", "fuzzy"),
    ("CHARLOTTE, NC 28211", "CHARLOTTE", "exact"),
    ("CHARLOTTE6090", "CHARLOTTE", "exact"),
    ("MATHEWS", "MATTHEWS", "fuzzy"),
    ("MECKLENBURG", "MECKLENBURG COUNTY (UNINCORPORATED)", "exact"),
    ("28215", "CHARLOTTE", "zip"),
    ("CHAROLETTE", "CHARLOTTE", "fuzzy"),
    ("CHARLOTTE, NORTH CAROLINA, UNITED STATES", "CHARLOTTE", "exact"),
    ("CHARLOTTEJAVASCRIPT:VOID PT_SU", "CHARLOTTE", "prefix"),
    ("MIDLAND", None, "unresolved"),   # real town, but in Cabarrus County
    ("J", None, "unresolved"),
    ("28227", None, "unresolved"),     # split ZIP: refuse to guess
])
def test_jurisdiction(raw, canon, method):
    r = resolve_one(raw)
    assert (r.canonical, r.method) == (canon, method)


def test_agency_normalization_converges():
    variants = ["Charlotte-Mecklenburg Police Dept.", "CHARLOTTE MECKLENBURG PD", "CMPD",
                "Charlotte Mecklenburg Police Department"]
    assert len({normalize_agency(v) for v in variants}) == 1
    assert normalize_agency("Mecklenburg Co. Sheriff's Office") == normalize_agency("MECKLENBURG COUNTY SO")


def test_address_normalization():
    assert normalize_address("123 North Tryon Street, Apt. 4B") == ("123 N TRYON ST", "4B")
    assert normalize_address("123 N TRYON ST #4B") == ("123 N TRYON ST", "4B")
    assert normalize_address("8400 E Independence Blvd")[0] == "8400 E INDEPENDENCE BV"


def test_suffix_kept_separate():
    assert normalize_person_name("Robert Barnhardt Jr.") == ("ROBERT BARNHARDT", "JR")


def _pair(a_rows, b_rows):
    cols = ["rec_id", "first_name", "last_name", "dob", "address", "zip"]
    return pd.DataFrame(a_rows, columns=cols), pd.DataFrame(b_rows, columns=cols)


def test_nickname_and_transposed_dob_link():
    a, b = _pair([["A1", "William", "Honeycutt", "1980-03-07", "12 Park Rd", "28209"]],
                 [["B1", "Billy", "Honeycutt", "1980-07-03", "12 PARK ROAD", "28209"]])
    links, _, _ = link(a, b, threshold=0.65)
    assert links[["rec_id_a", "rec_id_b"]].values.tolist() == [["A1", "B1"]]


def test_jr_sr_at_same_address_do_not_link():
    a, b = _pair([["A1", "Robert", "Barnhardt Sr", "1961-05-02", "400 Selwyn Av", "28209"]],
                 [["B1", "Robert", "Barnhardt Jr", "1988-05-02", "400 SELWYN AVENUE", "28209"]])
    links, _, _ = link(a, b, threshold=0.65)
    assert links.empty


def test_swapped_multi_token_names_link():
    a, b = _pair([["A1", "Keisha", "Van Buren", "1990-03-15", "2547 Park Bv", "28203"]],
                 [["B1", "Van Buren", "Keisha", "1990-03-15", "2547 PARK BOULEVARD", "28203"]])
    links, _, _ = link(a, b, threshold=0.65)
    assert len(links) == 1


@pytest.mark.slow
def test_benchmark_floor():
    """Regression guard: a change that drops held-out F1 below this fails CI."""
    a, b = generate(600, seed=2026)
    r = evaluate(a, b, fixed_threshold=0.65)
    assert r["at_threshold"]["f1"] >= 0.97
    assert r["at_threshold"]["precision"] >= 0.99
    assert r["blocking"]["pair_completeness"] >= 0.99
    assert r["at_threshold"]["f1"] > r["baseline_exact"]["f1"] + 0.3
