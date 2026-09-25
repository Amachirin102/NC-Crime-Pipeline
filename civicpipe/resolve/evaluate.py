"""Accuracy harness for person linkage: blocking quality, precision/recall/F1
across thresholds, an exact-match baseline, and recall by corruption type."""
from __future__ import annotations

import pandas as pd

from civicpipe.resolve.person import candidate_pairs, one_to_one, prep, score_pairs


def truth_pairs(a: pd.DataFrame, b: pd.DataFrame) -> set[tuple[str, str]]:
    m = a[["rec_id", "pid"]].merge(b[["rec_id", "pid"]], on="pid", suffixes=("_a", "_b"))
    return set(zip(m.rec_id_a, m.rec_id_b))


def prf(pred: set, truth: set) -> dict:
    tp = len(pred & truth)
    p = tp / len(pred) if pred else 0.0
    r = tp / len(truth) if truth else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4),
            "tp": tp, "fp": len(pred) - tp, "fn": len(truth) - tp}


def exact_baseline(a: pd.DataFrame, b: pd.DataFrame) -> set:
    """What a naive join on normalized first+last+DOB gets you."""
    pa, pb = prep(a), prep(b)
    m = pa.merge(pb, on=["n_first", "n_last", "n_dob"], suffixes=("_a", "_b"))
    return set(zip(m.rec_id_a, m.rec_id_b))


def conservative_threshold(sweep: pd.DataFrame, min_precision: float = 0.995,
                           recall_slack: float = 0.005) -> float:
    """Highest threshold that keeps precision >= min_precision and gives up at most
    `recall_slack` recall vs. the best threshold.

    Not argmax-F1: a false merge (two people treated as one - e.g. a debt sent to
    collections against the wrong person) costs far more than a missed link,
    which just lands in a manual-review queue. When F1 is flat across a range,
    take the strictest end of it."""
    ok = sweep[sweep.precision >= min_precision]
    if ok.empty:
        return float(sweep.threshold.max())
    ok = ok[ok.recall >= ok.recall.max() - recall_slack]
    return float(ok.threshold.max())


def evaluate(a: pd.DataFrame, b: pd.DataFrame, thresholds=(0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90),
             fixed_threshold: float | None = None) -> dict:
    """Pass fixed_threshold (chosen on a dev set) when scoring a held-out test set,
    so the reported numbers are not tuned on the data they are reported on."""
    truth = truth_pairs(a, b)
    pa, pb = prep(a), prep(b)
    pairs = candidate_pairs(pa, pb)
    cand = set(zip(pairs.rec_id_a, pairs.rec_id_b))
    scored = score_pairs(pa, pb, pairs)

    sweep = []
    for t in thresholds:
        linked = one_to_one(scored, t)
        pred = set(zip(linked.rec_id_a, linked.rec_id_b)) if len(linked) else set()
        sweep.append({"threshold": t, **prf(pred, truth)})
    sweep_df = pd.DataFrame(sweep)
    best_t = fixed_threshold if fixed_threshold is not None else conservative_threshold(sweep_df)

    linked = one_to_one(scored, best_t)
    pred = set(zip(linked.rec_id_a, linked.rec_id_b))

    # recall by corruption type (on true pairs)
    bt = b.set_index("rec_id")["corruption"]
    kind = lambda c: c if "+" not in c else "multiple"
    by_kind = pd.DataFrame([{"corruption": kind(bt[rb]), "found": (ra, rb) in pred} for ra, rb in truth])
    by_kind = by_kind.groupby("corruption").found.agg(["size", "mean"]).rename(
        columns={"size": "true_pairs", "mean": "recall"}).sort_values("true_pairs", ascending=False)

    fp = scored.merge(pd.DataFrame(list(pred - truth), columns=["rec_id_a", "rec_id_b"]))
    fn = pd.DataFrame(list(truth - pred), columns=["rec_id_a", "rec_id_b"]).merge(
        scored, how="left")  # score is NaN when blocking never proposed the pair
    return {
        "records": {"a": len(a), "b": len(b), "true_pairs": len(truth)},
        "blocking": {"candidate_pairs": len(pairs),
                     "all_pairs": len(a) * len(b),
                     "reduction_ratio": round(1 - len(pairs) / (len(a) * len(b)), 5),
                     "pair_completeness": round(len(cand & truth) / len(truth), 4)},
        "baseline_exact": prf(exact_baseline(a, b), truth),
        "sweep": sweep_df,
        "chosen_threshold": best_t,
        "at_threshold": prf(pred, truth),
        "recall_by_corruption": by_kind,
        "false_positives": fp,
        "false_negatives": fn,
    }
