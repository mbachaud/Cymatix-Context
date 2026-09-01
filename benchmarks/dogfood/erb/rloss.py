"""R-loss scorer for paired ladder-receipt arms (ERB 947k wave-3).

An "arm" here is the `per_query` list carried by every arm inside an
ablation-ladder receipt (see benchmarks/dogfood/erb/ablation_ladder.py and
the `per_query_note` on any ladder receipt). Each record is keyed by
`needle` and carries the #335 delivery basis:

    delivered_gold   -- count of gold chunks that made the delivered slate
    delivered_count  -- seats actually shipped
    delivered_gold_rank

Delivery, not pool recall, is the metric this project ships on
(`delivered_gold_rate`), so every function below scores on
`delivered_gold > 0` unless a different `basis` is requested.

The point of this module is regression accounting, not headline movement:
an arm that rescues 20 needles while quietly dropping 18 has moved the
headline by +2 and the corpus by 38. r_loss_at_k names the ones it broke.

Usage:
    from rloss import load_arm, r_loss_at_k, rescued_at_k, robustness_index

    base = load_arm("receipts/ladder_v09x_w24_min_delivered_2026-08-28.json")
    arm  = load_arm("receipts/my_arm_2026-09-01.json")
    print(r_loss_at_k(base, arm))

Self-test: `python rloss.py` runs the baseline receipt against ITSELF and
asserts r_loss == 0 and rescued == 0, then runs a negative control.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

BASIS_DELIVERED = "delivered"   # delivered_gold > 0    (delivered_gold_rate)
BASIS_POOL = "pool"             # recall_hit == 1       (recall_at_k)
BASIS_FINAL = "final"           # final_recall_hit == 1 (#341 final-order)

_BASIS_FIELDS = {
    BASIS_DELIVERED: "delivered_gold",
    BASIS_POOL: "recall_hit",
    BASIS_FINAL: "final_recall_hit",
}


def _hit(rec, basis=BASIS_DELIVERED):
    """True iff this per-query record counts as a hit on `basis`.

    A record carrying an 'error' (a failed query, kept in the vector so arm
    lengths stay equal) is never a hit. A missing/None field is not a hit
    either -- absence of evidence is scored as a miss, deliberately, so a
    receipt that forgot to emit the field cannot silently inflate an arm.
    """
    if basis not in _BASIS_FIELDS:
        raise ValueError(
            "unknown basis %r; expected one of %s" % (basis, sorted(_BASIS_FIELDS))
        )
    if rec.get("error"):
        return False
    v = rec.get(_BASIS_FIELDS[basis])
    if v is None:
        return False
    return v > 0


def _index(per_query):
    out = {}
    for rec in per_query:
        name = rec.get("needle")
        if name is None:
            raise ValueError("per_query record without a 'needle' key")
        if name in out:
            raise ValueError("duplicate needle %r in per_query" % (name,))
        out[name] = rec
    return out


def _paired(baseline_per_query, arm_per_query):
    """Index both arms; return (base_idx, arm_idx, shared_names_sorted).

    Pairing is by needle NAME, not by list position: two receipts written by
    different runs can share an order, but nothing guarantees it, and a
    silently misaligned zip is exactly the failure mode this module exists
    to prevent.
    """
    b = _index(baseline_per_query)
    a = _index(arm_per_query)
    shared = sorted(set(b) & set(a))
    return b, a, shared


def r_loss_at_k(baseline_per_query, arm_per_query, basis=BASIS_DELIVERED):
    """Needles the baseline delivered gold on and the arm does NOT.

    This is the regression ledger: the cost side of any headline win.
    """
    b, a, shared = _paired(baseline_per_query, arm_per_query)
    lost = [n for n in shared if _hit(b[n], basis) and not _hit(a[n], basis)]
    return {
        "basis": basis,
        "count": len(lost),
        "needles": lost,
        "n_paired": len(shared),
        "baseline_only": sorted(set(b) - set(a)),
        "arm_only": sorted(set(a) - set(b)),
    }


def rescued_at_k(baseline_per_query, arm_per_query, basis=BASIS_DELIVERED):
    """Needles the arm delivers gold on that the baseline missed."""
    b, a, shared = _paired(baseline_per_query, arm_per_query)
    won = [n for n in shared if not _hit(b[n], basis) and _hit(a[n], basis)]
    return {
        "basis": basis,
        "count": len(won),
        "needles": won,
        "n_paired": len(shared),
        "baseline_only": sorted(set(b) - set(a)),
        "arm_only": sorted(set(a) - set(b)),
    }


def robustness_index(baseline_per_query, arm_per_query, basis=BASIS_DELIVERED):
    """(helped - hurt) / n over the paired needle set.

    Also reports the raw rates so a caller can see whether a flat index is
    "nothing moved" (helped 0, hurt 0) or "churn cancelled out"
    (helped 18, hurt 18) -- those are very different arms.
    """
    b, a, shared = _paired(baseline_per_query, arm_per_query)
    n = len(shared)
    helped = rescued_at_k(baseline_per_query, arm_per_query, basis)["count"]
    hurt = r_loss_at_k(baseline_per_query, arm_per_query, basis)["count"]
    base_hits = sum(1 for x in shared if _hit(b[x], basis))
    arm_hits = sum(1 for x in shared if _hit(a[x], basis))
    return {
        "basis": basis,
        "n": n,
        "helped": helped,
        "hurt": hurt,
        "churn": helped + hurt,
        "robustness_index": ((helped - hurt) / n) if n else 0.0,
        "baseline_rate": (base_hits / n) if n else 0.0,
        "arm_rate": (arm_hits / n) if n else 0.0,
        "delta_rate": ((arm_hits - base_hits) / n) if n else 0.0,
    }


def load_arm(receipt_path, arm_name=None):
    """Pull one arm's per_query list out of a ladder receipt.

    `arm_name` defaults to the receipt's single arm; with >1 arm it is
    required, because guessing which arm someone meant is how a paired
    comparison quietly becomes an unpaired one.
    """
    with open(receipt_path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    arms = doc.get("arms")
    if not isinstance(arms, list) or not arms:
        raise ValueError("%s: no 'arms' list" % receipt_path)
    if arm_name is None:
        if len(arms) != 1:
            names = [x.get("arm") for x in arms]
            raise ValueError(
                "%s carries %d arms %s; pass arm_name=" % (receipt_path, len(arms), names)
            )
        chosen = arms[0]
    else:
        matches = [x for x in arms if x.get("arm") == arm_name]
        if len(matches) != 1:
            raise ValueError(
                "%s: %d arms named %r" % (receipt_path, len(matches), arm_name)
            )
        chosen = matches[0]
    pq = chosen.get("per_query")
    if not isinstance(pq, list):
        raise ValueError(
            "%s: arm %r has no per_query list" % (receipt_path, chosen.get("arm"))
        )
    return pq


def self_test():
    here = os.path.dirname(os.path.abspath(__file__))
    base_path = os.path.join(
        here, "receipts", "ladder_v09x_w24_min_delivered_2026-08-28.json"
    )
    pq = load_arm(base_path)
    results = {}
    for basis in (BASIS_DELIVERED, BASIS_POOL, BASIS_FINAL):
        rl = r_loss_at_k(pq, pq, basis)
        rs = rescued_at_k(pq, pq, basis)
        ri = robustness_index(pq, pq, basis)
        assert rl["count"] == 0, "identity r_loss != 0 on basis %s: %s" % (basis, rl)
        assert rs["count"] == 0, "identity rescued != 0 on basis %s: %s" % (basis, rs)
        assert ri["robustness_index"] == 0.0, "identity RI != 0 on basis %s" % basis
        assert ri["n"] == len(pq), "pairing dropped needles on the identity comparison"
        assert abs(ri["baseline_rate"] - ri["arm_rate"]) < 1e-12
        results[basis] = {
            "r_loss": rl["count"],
            "rescued": rs["count"],
            "robustness_index": ri["robustness_index"],
            "n_paired": ri["n"],
            "rate": round(ri["baseline_rate"], 6),
        }

    # Negative control: a scorer that returns 0 for everything also passes an
    # identity test, so the identity test alone proves nothing. Flip one hit
    # to a miss and one miss to a hit, and confirm the scorer NOTICES both.
    hit_names = [r["needle"] for r in pq if _hit(r, BASIS_DELIVERED)]
    miss_names = [r["needle"] for r in pq if not _hit(r, BASIS_DELIVERED)]
    mutated = [dict(r) for r in pq]
    broke = hit_names[0]
    fixed = miss_names[0]
    for r in mutated:
        if r["needle"] == broke:
            r["delivered_gold"] = 0
        if r["needle"] == fixed:
            r["delivered_gold"] = 1
    rl = r_loss_at_k(pq, mutated)
    rs = rescued_at_k(pq, mutated)
    ri = robustness_index(pq, mutated)
    assert rl["count"] == 1 and rl["needles"] == [broke], rl
    assert rs["count"] == 1 and rs["needles"] == [fixed], rs
    assert ri["robustness_index"] == 0.0 and ri["churn"] == 2, ri
    results["negative_control"] = {
        "broke": broke,
        "fixed": fixed,
        "r_loss": rl["count"],
        "rescued": rs["count"],
        "churn": ri["churn"],
        "robustness_index": ri["robustness_index"],
        "note": "one hit flipped to miss, one miss flipped to hit; RI is 0 but churn is 2",
    }

    # Shuffle control: pairing must be by name, not by list position.
    import random as _random

    shuffled = list(pq)
    _random.Random(20260901).shuffle(shuffled)
    ri_s = robustness_index(pq, shuffled)
    assert ri_s["churn"] == 0, "pairing is order-sensitive: %s" % ri_s
    results["shuffle_control"] = {
        "churn": ri_s["churn"],
        "note": "arm rows shuffled; churn stays 0, proving pairing is by needle name",
    }

    results["baseline_receipt"] = base_path.replace("\\", "/")
    results["passed"] = True
    return results


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    out = self_test()
    print(json.dumps(out, indent=2))
    print(
        "SELF-TEST PASS: identity r_loss==0 and rescued==0 on all three bases; "
        "negative control detected exactly 1 loss + 1 rescue; shuffle control churn 0."
    )
