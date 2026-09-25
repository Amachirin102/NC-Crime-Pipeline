"""Person/address record linkage across two sources whose identifiers don't line up.

Pipeline: normalize -> block -> compare -> score -> threshold -> cluster.

- Blocking keeps the comparison count tractable (n*m pairs is infeasible at
  scale) by only comparing records that share a cheap key. Multiple blocking
  passes are unioned so one corrupted field can't hide a true match.
- Comparison produces interpretable per-field similarities, not one opaque
  score, so every match decision can be explained to a reviewer.
- Scoring is a weighted sum with explicit hard rules for the traps that
  similarity alone gets wrong (JR/SR, same name + different DOB).
"""
from __future__ import annotations

from dataclasses import dataclass

import jellyfish
import pandas as pd
from rapidfuzz.distance import JaroWinkler
from rapidfuzz import fuzz

from civicpipe.normalize.text import normalize_address, normalize_person_name

NICKNAMES = {
    "BILL": "WILLIAM", "BILLY": "WILLIAM", "WILL": "WILLIAM", "LIAM": "WILLIAM",
    "BOB": "ROBERT", "BOBBY": "ROBERT", "ROB": "ROBERT", "JIM": "JAMES", "JIMMY": "JAMES",
    "JAMIE": "JAMES", "MIKE": "MICHAEL", "MIKEY": "MICHAEL", "KATE": "KATHERINE",
    "KATIE": "KATHERINE", "KATHY": "KATHERINE", "CATHY": "CATHERINE", "LIZ": "ELIZABETH",
    "BETH": "ELIZABETH", "BETTY": "ELIZABETH", "TONY": "ANTHONY", "CHRIS": "CHRISTOPHER",
    "DAN": "DANIEL", "DANNY": "DANIEL", "DAVE": "DAVID", "JOE": "JOSEPH", "JOEY": "JOSEPH",
    "TOM": "THOMAS", "TOMMY": "THOMAS", "RICK": "RICHARD", "RICH": "RICHARD", "DICK": "RICHARD",
    "SUE": "SUSAN", "SUSIE": "SUSAN", "JEN": "JENNIFER", "JENNY": "JENNIFER", "PATTY": "PATRICIA",
    "TRISH": "PATRICIA", "PEGGY": "MARGARET", "MEG": "MARGARET", "MAGGIE": "MARGARET",
    "STEVE": "STEVEN", "MATT": "MATTHEW", "ANDY": "ANDREW", "DREW": "ANDREW", "ALEX": "ALEXANDER",
    "SAM": "SAMUEL", "BEN": "BENJAMIN", "NICK": "NICHOLAS", "GREG": "GREGORY", "TED": "EDWARD",
    "ED": "EDWARD", "EDDIE": "EDWARD", "LARRY": "LAWRENCE", "JERRY": "GERALD", "TERRY": "TERRENCE",
    "DEBBIE": "DEBORAH", "DEB": "DEBORAH", "BARB": "BARBARA", "CINDY": "CYNTHIA", "MANDY": "AMANDA",
}

WEIGHTS = {"last": 0.25, "first": 0.20, "dob": 0.30, "addr": 0.20, "zip": 0.05}


def prep(df: pd.DataFrame) -> pd.DataFrame:
    """Expects columns: rec_id, first_name, last_name, dob (YYYY-MM-DD str), address, zip."""
    out = df.copy()
    first_sfx = out["first_name"].map(normalize_person_name)
    last_sfx = out["last_name"].map(normalize_person_name)
    out["n_last"] = last_sfx.str[0]
    # a suffix can land in either field when a system swaps first/last
    out["n_suffix"] = [l or f for f, l in zip(first_sfx.str[1], last_sfx.str[1])]
    out["n_first_full"] = first_sfx.str[0]
    out["n_first"] = out["n_first_full"].str.split().str[0].fillna("")
    out["n_first_canon"] = out["n_first"].map(lambda f: NICKNAMES.get(f, f))
    addr = out["address"].map(normalize_address)
    out["n_street"] = addr.str[0]
    out["n_unit"] = addr.str[1]
    out["n_zip"] = out["zip"].astype(str).str[:5]
    out["n_dob"] = out["dob"].astype(str)
    out["k_soundex_last"] = out["n_last"].map(lambda s: jellyfish.soundex(s) if s else "")
    out["k_dob_year"] = out["n_dob"].str[:4]
    out["k_first_initial_zip"] = out["n_first_canon"].str[:1] + out["n_zip"]
    out["k_street_num"] = out["n_street"].str.extract(r"^(\d+)", expand=False).fillna("") + out["n_zip"]
    # order-insensitive name key: survives first/last swaps and address changes
    out["k_name_set"] = [" ".join(sorted((jellyfish.soundex(f) if f else "", jellyfish.soundex(l) if l else "")))
                         for f, l in zip(out["n_first_canon"], out["n_last"])]
    return out


BLOCK_KEYS = [("k_soundex_last", "k_dob_year"), ("k_first_initial_zip",), ("k_street_num",), ("n_dob",),
              ("k_name_set",)]


def candidate_pairs(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    pairs = []
    for keys in BLOCK_KEYS:
        m = a[["rec_id", *keys]].merge(b[["rec_id", *keys]], on=list(keys), suffixes=("_a", "_b"))
        # ignore blocks keyed on an empty value
        m = m[(m[list(keys)] != "").all(axis=1)]
        pairs.append(m[["rec_id_a", "rec_id_b"]])
    return pd.concat(pairs).drop_duplicates().reset_index(drop=True)


def _dob_sim(x: str, y: str) -> float:
    if not x or not y or x == "nan" or y == "nan":
        return 0.5  # unknown, neither evidence for nor against
    if x == y:
        return 1.0
    xy, xm, xd = x[:4], x[5:7], x[8:10]
    yy, ym, yd = y[:4], y[5:7], y[8:10]
    if xy == yy and xm == yd and xd == ym:
        return 0.85  # day/month transposed - common keying error
    same = (xy == yy) + (xm == ym) + (xd == yd)
    if same == 2:
        return 0.6   # single-component typo
    return 0.0


@dataclass
class PairScore:
    last: float
    first: float
    dob: float
    addr: float
    zip: float
    suffix_conflict: bool

    @property
    def total(self) -> float:
        s = sum(getattr(self, k) * w for k, w in WEIGHTS.items())
        if self.suffix_conflict:
            s -= 0.35   # JR vs SR at the same address is a different person
        if self.dob == 0.0:
            s -= 0.15   # clearly different DOB is strong negative evidence
        return round(s, 4)


def compare(ra: pd.Series, rb: pd.Series) -> PairScore:
    first = max(JaroWinkler.similarity(ra.n_first_canon, rb.n_first_canon),
                JaroWinkler.similarity(ra.n_first, rb.n_first))
    # handle first/last swapped between systems
    swapped = min(JaroWinkler.similarity(ra.n_first_full, rb.n_last),
                  JaroWinkler.similarity(ra.n_last, rb.n_first_full))
    last = JaroWinkler.similarity(ra.n_last, rb.n_last)
    if swapped > max(first, last) and swapped > 0.92:
        first = last = swapped
    return PairScore(
        last=last,
        first=first,
        dob=_dob_sim(ra.n_dob, rb.n_dob),
        addr=fuzz.token_set_ratio(ra.n_street, rb.n_street) / 100,
        zip=float(ra.n_zip == rb.n_zip),
        suffix_conflict=bool(ra.n_suffix and rb.n_suffix and ra.n_suffix != rb.n_suffix),
    )


def score_pairs(a: pd.DataFrame, b: pd.DataFrame, pairs: pd.DataFrame) -> pd.DataFrame:
    ai, bi = a.set_index("rec_id"), b.set_index("rec_id")
    rows = []
    for ida, idb in pairs.itertuples(index=False):
        s = compare(ai.loc[ida], bi.loc[idb])
        rows.append({"rec_id_a": ida, "rec_id_b": idb, **s.__dict__, "score": s.total})
    return pd.DataFrame(rows)


def one_to_one(scored: pd.DataFrame, threshold: float, review_margin: float = 0.05) -> pd.DataFrame:
    """Greedy best-first assignment: each record links to at most one record in the
    other source. Appropriate when each source has one row per person.

    A link is flagged `needs_review` when either record had a competing candidate
    above threshold within `review_margin` of the chosen score - e.g. twins at one
    address with no DOB on file. Those go to a human, not straight to production."""
    above = scored[scored.score >= threshold].sort_values("score", ascending=False)
    kept, used_a, used_b = [], set(), set()
    for r in above.itertuples(index=False):
        if r.rec_id_a in used_a or r.rec_id_b in used_b:
            continue
        rivals = above[((above.rec_id_a == r.rec_id_a) ^ (above.rec_id_b == r.rec_id_b))]
        d = r._asdict()
        d["needs_review"] = bool((rivals.score >= r.score - review_margin).any())
        kept.append(d)
        used_a.add(r.rec_id_a)
        used_b.add(r.rec_id_b)
    cols = [*scored.columns, "needs_review"]
    return pd.DataFrame(kept, columns=cols)


def link(a_raw: pd.DataFrame, b_raw: pd.DataFrame, threshold: float = 0.80):
    a, b = prep(a_raw), prep(b_raw)
    pairs = candidate_pairs(a, b)
    scored = score_pairs(a, b, pairs)
    return one_to_one(scored, threshold), scored, pairs
