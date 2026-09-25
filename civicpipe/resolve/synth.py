"""Seeded synthetic benchmark for person/address linkage.

No real people. Two sources are generated from one set of true identities:
source A mimics court filings, source B mimics registered-vehicle-owner
records. Each B record carries the corruption that was applied to it, so
errors can be broken down by *why* a match was hard, not just counted.

The hard negatives are the ones that bite in production:
- generational pairs: same name + same address, JR vs SR, DOBs ~25y apart
- household members: same last name + address, different first name and DOB
- common-name collisions: same first + last name, same ZIP, different person
"""
from __future__ import annotations

import random
import string

import pandas as pd

from civicpipe.resolve.person import NICKNAMES

FIRST = ["JAMES", "ROBERT", "MICHAEL", "WILLIAM", "DAVID", "RICHARD", "JOSEPH", "THOMAS",
         "CHRISTOPHER", "DANIEL", "ANTHONY", "MATTHEW", "ANDREW", "STEVEN", "GREGORY", "EDWARD",
         "MARY", "PATRICIA", "JENNIFER", "ELIZABETH", "SUSAN", "MARGARET", "KATHERINE", "DEBORAH",
         "BARBARA", "CYNTHIA", "AMANDA", "LATOYA", "KEISHA", "DESHAWN", "MARCUS", "TERRENCE",
         "JOSE", "LUIS", "MARIA", "CARMEN", "ALEJANDRO", "NGUYEN", "PRIYA", "ANIL", "OLUWASEUN",
         "SHANICE", "TYRONE", "BRITTANY", "HUNTER", "CODY", "SAVANNAH", "DAKOTA", "XAVIER", "IMANI"]
LAST = ["SMITH", "JOHNSON", "WILLIAMS", "BROWN", "JONES", "GARCIA", "MILLER", "DAVIS", "RODRIGUEZ",
        "MARTINEZ", "HERNANDEZ", "LOPEZ", "WILSON", "ANDERSON", "THOMAS", "TAYLOR", "MOORE",
        "JACKSON", "MARTIN", "LEE", "THOMPSON", "WHITE", "HARRIS", "CLARK", "LEWIS", "ROBINSON",
        "WALKER", "YOUNG", "ALLEN", "KING", "WRIGHT", "SCOTT", "MCALLISTER", "O'BRIEN",
        "DE LA CRUZ", "VAN BUREN", "SMITH-JONES", "NGUYEN", "PATEL", "OKAFOR", "WASHINGTON",
        "BARNHARDT", "YOUNGBLOOD", "FAIRCLOTH", "HONEYCUTT", "LOCKLEAR", "OXENDINE", "BLACKWELDER"]
STREETS = ["TRYON", "PROVIDENCE", "SHARON AMITY", "CENTRAL", "INDEPENDENCE", "BEATTIES FORD",
           "ALBEMARLE", "MONROE", "PARK", "SUGAR CREEK", "WILKINSON", "FREEDOM", "NATIONS FORD",
           "ARROWOOD", "MALLARD CREEK", "HARRIS", "RANDOLPH", "QUEENS", "SELWYN", "FAIRVIEW"]
SUFFIX_LONG = {"ST": "STREET", "RD": "ROAD", "AV": "AVENUE", "BV": "BOULEVARD", "DR": "DRIVE", "LN": "LANE"}
ZIPS = ["28202", "28203", "28205", "28206", "28208", "28209", "28210", "28211", "28212",
        "28213", "28215", "28216", "28217", "28226", "28262", "28269", "28273", "28277"]
REV_NICK = {}
for nick, full in NICKNAMES.items():
    REV_NICK.setdefault(full, []).append(nick)


def _typo(s: str, rng: random.Random) -> str:
    if len(s) < 3:
        return s
    i = rng.randrange(1, len(s) - 1)
    op = rng.choice(["sub", "del", "swap", "dup"])
    if op == "sub":
        return s[:i] + rng.choice(string.ascii_uppercase) + s[i + 1:]
    if op == "del":
        return s[:i] + s[i + 1:]
    if op == "swap":
        return s[:i] + s[i + 1] + s[i] + s[i + 2:]
    return s[:i] + s[i] + s[i:]


def _identity(rng: random.Random, pid: int) -> dict:
    y = rng.randint(1945, 2004)
    return {
        "pid": pid, "first_name": rng.choice(FIRST), "last_name": rng.choice(LAST),
        "dob": f"{y}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
        "num": rng.randint(100, 9999), "street": rng.choice(STREETS),
        "sfx": rng.choice(list(SUFFIX_LONG)), "unit": rng.choice([None] * 3 + [str(rng.randint(1, 40))]),
        "zip": rng.choice(ZIPS), "gen": "",
    }


def _addr(p: dict, long_form: bool = False, unit_style: str = "APT") -> str:
    sfx = SUFFIX_LONG[p["sfx"]] if long_form else p["sfx"]
    a = f"{p['num']} {p['street']} {sfx}"
    if p["unit"]:
        a += f" {unit_style} {p['unit']}" if unit_style != "#" else f" #{p['unit']}"
    return a


def generate(n: int = 1200, overlap: float = 0.7, seed: int = 7) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = random.Random(seed)
    people = [_identity(rng, i) for i in range(n)]

    # hard negatives, added as distinct identities
    extra = []
    for p in rng.sample(people, n // 12):          # generational pairs
        p["gen"] = "SR"
        jr = dict(p, pid=len(people) + len(extra), gen="JR",
                  dob=f"{int(p['dob'][:4]) + rng.randint(20, 32)}{p['dob'][4:]}")
        if int(jr["dob"][:4]) > 2006:
            jr["dob"] = "2005" + jr["dob"][4:]
        extra.append(jr)
    for p in rng.sample(people, n // 12):          # household members
        extra.append(dict(_identity(rng, len(people) + len(extra)), last_name=p["last_name"],
                          num=p["num"], street=p["street"], sfx=p["sfx"], zip=p["zip"], unit=p["unit"]))
    for p in rng.sample(people, n // 40):          # twins: same last name, address, and DOB
        extra.append(dict(_identity(rng, len(people) + len(extra)), last_name=p["last_name"],
                          dob=p["dob"], num=p["num"], street=p["street"], sfx=p["sfx"],
                          zip=p["zip"], unit=p["unit"]))
    for p in rng.sample(people, n // 15):          # same name, same zip, different person
        extra.append(dict(_identity(rng, len(people) + len(extra)), first_name=p["first_name"],
                          last_name=p["last_name"], zip=p["zip"]))
    people += extra

    a_rows, b_rows = [], []
    for p in people:
        in_a, in_b = rng.random() < 0.85, rng.random() < 0.85
        if rng.random() < overlap:
            in_a = in_b = True
        last = f"{p['last_name']} {p['gen']}".strip()
        if in_a:
            a_rows.append({"rec_id": f"A{len(a_rows):05d}", "pid": p["pid"], "first_name": p["first_name"],
                           "last_name": last, "dob": p["dob"], "address": _addr(p), "zip": p["zip"]})
        if in_b:
            r = {"rec_id": f"B{len(b_rows):05d}", "pid": p["pid"], "first_name": p["first_name"],
                 "last_name": last, "dob": p["dob"], "address": _addr(p, long_form=rng.random() < .5,
                 unit_style=rng.choice(["APT", "UNIT", "#"])), "zip": p["zip"], "corruption": "none"}
            kinds = rng.sample(["typo_last", "typo_first", "nickname", "dob_transpose", "dob_typo",
                                "moved", "swap_names", "drop_suffix"],
                               k=rng.choice([0, 1, 1, 1, 2]))
            for k in kinds:
                if k == "typo_last":
                    r["last_name"] = _typo(r["last_name"], rng)
                elif k == "typo_first":
                    r["first_name"] = _typo(r["first_name"], rng)
                elif k == "nickname" and p["first_name"] in REV_NICK:
                    r["first_name"] = rng.choice(REV_NICK[p["first_name"]])
                elif k == "dob_transpose" and int(p["dob"][8:10]) <= 12:
                    r["dob"] = f"{p['dob'][:4]}-{p['dob'][8:10]}-{p['dob'][5:7]}"
                elif k == "dob_typo":
                    r["dob"] = f"{p['dob'][:8]}{rng.randint(1, 28):02d}"
                elif k == "moved":
                    q = _identity(rng, -1)
                    r["address"], r["zip"] = _addr(q), q["zip"]
                elif k == "swap_names":
                    r["first_name"], r["last_name"] = r["last_name"], r["first_name"]
                elif k == "drop_suffix" and p["gen"]:
                    r["last_name"] = p["last_name"]
                else:
                    continue
                r["corruption"] = k if r["corruption"] == "none" else r["corruption"] + "+" + k
            # vehicle-owner style sources often lack DOB entirely; independent of corruption
            if r["dob"] and rng.random() < 0.20:
                r["dob"] = ""
                r["corruption"] = "no_dob" if r["corruption"] == "none" else r["corruption"] + "+no_dob"
            b_rows.append(r)

    a, b = pd.DataFrame(a_rows), pd.DataFrame(b_rows)
    return a.sample(frac=1, random_state=seed).reset_index(drop=True), \
        b.sample(frac=1, random_state=seed + 1).reset_index(drop=True)
