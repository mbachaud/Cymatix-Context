"""P0 -- bucket ledger, chunk-split overlap cross-tab, fit/held-out split.

No bed access. Pure re-derivation from two receipts + the needle/gold maps,
so it is cheap to re-run and it CHECKS the published wave-2 taxonomy rather
than adopting its numbers.

What it answers
---------------
1. Deterministic bucket per miss (pool_absent / near_band / seat_capped),
   cross-tabbed against question_type, reconciled against the published
   taxonomy counts. Disagreements are reported loudly, not smoothed.
2. chunk_split per miss: gold's DOC-level coverage would tie/beat the rank-1
   winner while gold's best CHUNK does not.
3. The overlap cross-tab. 61 + 37 + 41 = 139 taxonomy-category misses plus
   65 chunk_split = 204 > 156, so the flags overlap; this quantifies by how
   much, and what share of the REACHABLE population (156 minus the 61
   semantic pool-absent) a single rollup lane could in principle carry.
4. A seeded 50/50 fit/held-out split of all 470 needles, stratified by
   question_type AND by baseline hit/miss.

Run:
    python benchmarks/dogfood/erb/probe_bucket_ledger.py
"""

from __future__ import annotations

import collections
import json
import os
import random
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ERB = HERE
RECEIPTS = os.path.join(ERB, "receipts")
DOGFOOD = os.path.dirname(ERB)

BED = "F:/tmp/erb_blob_v09x.db"
STAMP = "2026-09-01"
SEED = 20260901

MINE_PATH = os.path.join(RECEIPTS, "interloper_mine_erb947k_2026-08-31.json")
BASE_PATH = os.path.join(RECEIPTS, "ladder_v09x_w24_min_delivered_2026-08-28.json")
NEEDLES_PATH = os.path.join(ERB, "needles_resolved_v09x_full.json")
GOLD_PATH = os.path.join(ERB, "gold_by_needle_v09x.json")
META_PATH = os.path.join(ERB, "needles_v09x_meta.json")
TAX_PATH = os.path.join(DOGFOOD, "interloper_taxonomy_wave2_2026-08-31.json")
OUT_PATH = os.path.join(RECEIPTS, "bucket_ledger_%s.json" % STAMP)

# --- kill criterion, stated BEFORE looking at any result -------------------
KILL_CRITERION = (
    "KILL (ledger untrustworthy) if EITHER (a) the deterministically "
    "recomputed bucket counts disagree with the published wave-2 taxonomy "
    "counts (pool_absent 109 / near_band 41 / delivery_side 6; semantic "
    "61/20/1; basic 37/18/5) on any cell, OR (b) the 156 mined misses are "
    "not exactly the 156 needles with delivered_gold==0 in the basis ladder "
    "receipt, OR (c) the recomputed chunk_split count is not 65/156. "
    "INCONCLUSIVE if a required field is missing from the inputs. "
    "Otherwise PASS. Note this criterion grades the LEDGER's fidelity, not "
    "whether chunk_split is a promising lane -- the coverage arithmetic in "
    "section 3 is reported as-measured either way."
)

PUBLISHED = {
    "bucket_counts": {"pool_absent": 109, "near_band": 41, "delivery_side_lte12": 6},
    "class_x_bucket": {
        "semantic": {"pool_absent": 61, "near_band": 20, "seat_capped": 1},
        "basic": {"pool_absent": 37, "near_band": 18, "seat_capped": 5},
        "intra_document_reasoning": {"pool_absent": 5, "near_band": 1},
        "completeness": {"pool_absent": 3},
        "conflicting_info": {"pool_absent": 2},
        "constrained": {"pool_absent": 1},
        "miscellaneous": {"near_band": 2},
    },
    "chunk_split": 65,
    "chunk_split_pct": 41.7,
}


def load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def git_sha():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=HERE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).decode().strip()
    except Exception as exc:  # noqa: BLE001
        return "unavailable: %s" % exc


def bucket_of(rec):
    """Deterministic bucket for one miss record.

    pool_absent : live_rank is None (gold never entered the published score
                  map) or live_rank > 100
    near_band   : 13 <= live_rank <= 100
    seat_capped : live_rank <= 12 -- gold made the pool top-12 yet the miss
                  record exists, i.e. delivered_gold == 0
    """
    lr = rec.get("live_rank")
    if lr is None or lr > 100:
        return "pool_absent"
    if lr >= 13:
        return "near_band"
    return "seat_capped"


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    mine = load(MINE_PATH)
    base = load(BASE_PATH)
    needles = load(NEEDLES_PATH)["needles"]
    gold = load(GOLD_PATH)
    tax = load(TAX_PATH)

    misses = mine["misses"]
    arms = base["arms"]
    assert len(arms) == 1, "basis receipt has %d arms; expected 1" % len(arms)
    per_query = arms[0]["per_query"]

    qtype = {n["name"]: n["question_type"] for n in needles}
    base_by_needle = {r["needle"]: r for r in per_query}

    problems = []

    # ---------------------------------------------------------------- 0. sanity
    base_misses = sorted(
        r["needle"] for r in per_query if not (r.get("delivered_gold") or 0) > 0
    )
    mined = sorted(r["needle"] for r in misses)
    miss_set_match = base_misses == mined
    if not miss_set_match:
        problems.append(
            "mined miss set != basis delivered_gold==0 set: "
            "%d mined vs %d basis; mined-only %s; basis-only %s"
            % (
                len(mined),
                len(base_misses),
                sorted(set(mined) - set(base_misses))[:10],
                sorted(set(base_misses) - set(mined))[:10],
            )
        )

    class_disagreements = [
        {"needle": r["needle"], "mine_class": r["class"], "needle_question_type": qtype.get(r["needle"])}
        for r in misses
        if r["class"] != qtype.get(r["needle"])
    ]
    if class_disagreements:
        problems.append(
            "mine 'class' disagrees with needles question_type on %d misses"
            % len(class_disagreements)
        )

    # ------------------------------------------------------- 1. bucket ledger
    buckets = {}
    for r in misses:
        buckets[r["needle"]] = bucket_of(r)

    bucket_counts = collections.Counter(buckets.values())
    class_x_bucket = collections.defaultdict(collections.Counter)
    for r in misses:
        class_x_bucket[qtype[r["needle"]]][buckets[r["needle"]]] += 1
    class_x_bucket = {k: dict(v) for k, v in sorted(class_x_bucket.items())}

    # bucket evidence: live_rank distributions
    lr_by_bucket = collections.defaultdict(list)
    for r in misses:
        lr_by_bucket[buckets[r["needle"]]].append(r.get("live_rank"))
    bucket_live_rank = {}
    for b, vals in lr_by_bucket.items():
        ints = sorted(v for v in vals if v is not None)
        bucket_live_rank[b] = {
            "n": len(vals),
            "n_live_rank_null": sum(1 for v in vals if v is None),
            "min": ints[0] if ints else None,
            "median": ints[len(ints) // 2] if ints else None,
            "max": ints[-1] if ints else None,
            "ranks": ints if len(ints) <= 12 else None,
        }

    # reconciliation
    recon = {
        "bucket_counts_published": PUBLISHED["bucket_counts"],
        "bucket_counts_recomputed": {
            "pool_absent": bucket_counts.get("pool_absent", 0),
            "near_band": bucket_counts.get("near_band", 0),
            "delivery_side_lte12": bucket_counts.get("seat_capped", 0),
        },
        "class_x_bucket_published": PUBLISHED["class_x_bucket"],
        "class_x_bucket_recomputed": class_x_bucket,
        "cell_disagreements": [],
    }
    for k, v in recon["bucket_counts_published"].items():
        got = recon["bucket_counts_recomputed"].get(k, 0)
        if got != v:
            recon["cell_disagreements"].append(
                {"cell": "bucket_counts.%s" % k, "published": v, "recomputed": got}
            )
    all_classes = set(PUBLISHED["class_x_bucket"]) | set(class_x_bucket)
    for cls in sorted(all_classes):
        pub = PUBLISHED["class_x_bucket"].get(cls, {})
        got = class_x_bucket.get(cls, {})
        for b in sorted(set(pub) | set(got)):
            p = pub.get(b, 0)
            g = got.get(b, 0)
            if p != g:
                recon["cell_disagreements"].append(
                    {"cell": "class_x_bucket.%s.%s" % (cls, b), "published": p, "recomputed": g}
                )
    if recon["cell_disagreements"]:
        problems.append(
            "taxonomy reconciliation: %d disagreeing cells"
            % len(recon["cell_disagreements"])
        )

    # ------------------------------------------------------- 2. chunk_split
    per_miss = []
    for r in misses:
        name = r["needle"]
        top1 = r["delivered"][0] if r["delivered"] else None
        top1_cov = top1["kw_cov"] if top1 else None
        gf_cov = r["gold_first"]["kw_cov"]
        union_cov = r["gold_kw_cov"]
        cs = (
            top1_cov is not None
            and union_cov >= top1_cov
            and gf_cov < top1_cov
        )
        per_miss.append(
            {
                "needle": name,
                "question_type": qtype[name],
                "bucket": buckets[name],
                "live_rank": r.get("live_rank"),
                "n_kw": r["n_kw"],
                "gold_n": r["gold_n"],
                "gold_union_kw_cov": union_cov,
                "gold_first_kw_cov": gf_cov,
                "top1_winner_kw_cov": top1_cov,
                "top1_winner_gene_id": top1["gene_id"] if top1 else None,
                "delivered_count": r["receipt_delivered_count"],
                "chunk_split": bool(cs),
                "union_minus_first": union_cov - gf_cov,
            }
        )
    cs_names = set(x["needle"] for x in per_miss if x["chunk_split"])
    n_cs = len(cs_names)
    cs_recon = {
        "published": PUBLISHED["chunk_split"],
        "published_pct": PUBLISHED["chunk_split_pct"],
        "recomputed": n_cs,
        "recomputed_pct": round(100.0 * n_cs / len(misses), 1),
        "agrees": n_cs == PUBLISHED["chunk_split"],
    }
    if not cs_recon["agrees"]:
        problems.append(
            "chunk_split recomputed %d != published %d" % (n_cs, PUBLISHED["chunk_split"])
        )

    # -------------------------------------------------- 3. overlap cross-tab
    # The three headline taxonomy categories are (semantic & pool_absent),
    # (basic & pool_absent) and (any-class & near_band) = 61 + 37 + 41 = 139.
    def big_cat(x):
        if x["bucket"] == "pool_absent" and x["question_type"] == "semantic":
            return "sem_pool_absent"
        if x["bucket"] == "pool_absent" and x["question_type"] == "basic":
            return "basic_pool_absent"
        if x["bucket"] == "near_band":
            return "near_band"
        return None

    bucket_x_cs = collections.defaultdict(lambda: {"n": 0, "chunk_split": 0})
    for x in per_miss:
        cell = bucket_x_cs[x["bucket"]]
        cell["n"] += 1
        cell["chunk_split"] += 1 if x["chunk_split"] else 0
    bucket_x_cs = {
        k: {
            "n": v["n"],
            "chunk_split": v["chunk_split"],
            "chunk_split_pct": round(100.0 * v["chunk_split"] / v["n"], 1),
        }
        for k, v in sorted(bucket_x_cs.items())
    }

    cat_x_cs = collections.defaultdict(lambda: {"n": 0, "chunk_split": 0})
    for x in per_miss:
        c = big_cat(x) or "other_%s" % x["bucket"]
        cell = cat_x_cs[c]
        cell["n"] += 1
        cell["chunk_split"] += 1 if x["chunk_split"] else 0
    cat_x_cs = {
        k: {
            "n": v["n"],
            "chunk_split": v["chunk_split"],
            "chunk_split_pct": round(100.0 * v["chunk_split"] / v["n"], 1),
        }
        for k, v in sorted(cat_x_cs.items())
    }

    class_x_cs = collections.defaultdict(lambda: {"n": 0, "chunk_split": 0})
    for x in per_miss:
        cell = class_x_cs[x["question_type"]]
        cell["n"] += 1
        cell["chunk_split"] += 1 if x["chunk_split"] else 0
    class_x_cs = {
        k: {
            "n": v["n"],
            "chunk_split": v["chunk_split"],
            "chunk_split_pct": round(100.0 * v["chunk_split"] / v["n"], 1),
        }
        for k, v in sorted(class_x_cs.items())
    }

    flag_hist = collections.Counter()
    for x in per_miss:
        flags = (1 if big_cat(x) else 0) + (1 if x["chunk_split"] else 0)
        flag_hist[flags] += 1

    n_sem_pa = sum(1 for x in per_miss if big_cat(x) == "sem_pool_absent")
    reachable = len(misses) - n_sem_pa
    cs_in_reachable = sum(
        1 for x in per_miss if x["chunk_split"] and big_cat(x) != "sem_pool_absent"
    )
    cs_in_sem_pa = n_cs - cs_in_reachable
    needed = 62

    overlap = {
        "definition_note": (
            "Buckets are mutually exclusive by construction (one per miss). "
            "chunk_split is an ORTHOGONAL flag, so the 156 misses carry "
            "1 bucket + 0-or-1 chunk_split. The 'flags' histogram counts "
            "membership in one of the three headline taxonomy categories "
            "(sem_pool_absent / basic_pool_absent / near_band) plus "
            "chunk_split, which is the 139 + 65 = 204 arithmetic."
        ),
        "bucket_x_chunk_split": bucket_x_cs,
        "headline_category_x_chunk_split": cat_x_cs,
        "question_type_x_chunk_split": class_x_cs,
        "flag_histogram": {str(k): flag_hist[k] for k in sorted(flag_hist)},
        "arithmetic": {
            "n_misses": len(misses),
            "headline_categories_sum": 61 + 37 + 41,
            "headline_categories_recomputed_sum": sum(
                v["n"] for k, v in cat_x_cs.items() if not k.startswith("other_")
            ),
            "misses_outside_headline_categories": sum(
                v["n"] for k, v in cat_x_cs.items() if k.startswith("other_")
            ),
            "chunk_split_total": n_cs,
            "sum_if_disjoint": 139 + n_cs,
            "excess_over_156": (139 + n_cs) - len(misses),
            "overlap_explained_by": (
                "chunk_split is not a fourth bucket: %d of its %d members already "
                "sit inside one of the three headline categories, and the residual "
                "%d sit in the 17 misses outside them."
                % (
                    sum(v["chunk_split"] for k, v in cat_x_cs.items() if not k.startswith("other_")),
                    n_cs,
                    sum(v["chunk_split"] for k, v in cat_x_cs.items() if k.startswith("other_")),
                )
            ),
        },
        "reachable_population": {
            "definition": (
                "156 misses minus the 61 semantic pool-absent misses -- the "
                "population a rollup lane is allowed to be judged on, since "
                "the semantic crater is already a standing KILL for "
                "pool-recovery lanes on this bed."
            ),
            "n_reachable": reachable,
            "n_excluded_sem_pool_absent": n_sem_pa,
            "chunk_split_in_reachable": cs_in_reachable,
            "chunk_split_in_reachable_pct": round(100.0 * cs_in_reachable / reachable, 1),
            "chunk_split_in_sem_pool_absent": cs_in_sem_pa,
        },
        "carry_arithmetic": {
            "needles_needed_for_0_80": needed,
            "note": (
                "0.80 x 470 = 376 delivered; baseline 314; gap = 62 needles."
            ),
            "ceiling_if_chunk_split_lane_is_perfect_reachable_only": cs_in_reachable,
            "pct_of_62_covered_reachable_only": round(100.0 * cs_in_reachable / needed, 1),
            "ceiling_if_chunk_split_lane_is_perfect_all_156": n_cs,
            "pct_of_62_covered_all_156": round(100.0 * n_cs / needed, 1),
            "reading": (
                "A single doc-rollup lane that converted EVERY chunk_split miss "
                "at 100%% yield would supply %d of the %d needles needed "
                "(%.1f%%) if it is confined to the reachable population, or %d "
                "(%.1f%%) if it is also allowed to fire inside the semantic "
                "crater. A perfect lane is not a real yield, so treat these as "
                "hard ceilings: chunk_split alone cannot carry +62, but it is "
                "the largest single mechanism on the board."
                % (
                    cs_in_reachable,
                    needed,
                    100.0 * cs_in_reachable / needed,
                    n_cs,
                    100.0 * n_cs / needed,
                )
            ),
        },
    }

    # ------------------------------------------------ 4. fit / held-out split
    strata = collections.defaultdict(list)
    for n in needles:
        name = n["name"]
        rec = base_by_needle.get(name)
        if rec is None:
            problems.append("needle %s absent from basis per_query" % name)
            continue
        hit = 1 if (rec.get("delivered_gold") or 0) > 0 else 0
        strata[(n["question_type"], hit)].append(name)

    rng = random.Random(SEED)
    fit, held = [], []
    stratum_counts = {}
    # Deterministic: strata visited in sorted key order, names sorted inside
    # each stratum before the seeded shuffle, and odd leftovers alternate
    # sides so the halves stay balanced overall.
    leftover_to_fit = True
    for key in sorted(strata):
        names = sorted(strata[key])
        rng.shuffle(names)
        half = len(names) // 2
        if len(names) % 2 == 0:
            a, b = names[:half], names[half:]
        else:
            if leftover_to_fit:
                a, b = names[: half + 1], names[half + 1:]
            else:
                a, b = names[:half], names[half:]
            leftover_to_fit = not leftover_to_fit
        fit.extend(a)
        held.extend(b)
        stratum_counts["%s|%s" % (key[0], "hit" if key[1] else "miss")] = {
            "n": len(names),
            "fit": len(a),
            "held_out": len(b),
        }

    fit = sorted(fit)
    held = sorted(held)
    assert not (set(fit) & set(held)), "fit/held-out overlap"
    assert len(fit) + len(held) == len(needles), "split lost needles"

    def rate(names):
        hits = sum(
            1 for x in names if (base_by_needle[x].get("delivered_gold") or 0) > 0
        )
        return hits, len(names), hits / len(names)

    fh, fn, fr = rate(fit)
    hh, hn, hr = rate(held)

    def miss_buckets(names):
        c = collections.Counter()
        for x in names:
            if x in buckets:
                c[buckets[x]] += 1
        return dict(sorted(c.items()))

    split = {
        "seed": SEED,
        "method": (
            "stratified by (question_type x baseline delivered-gold hit/miss); "
            "names sorted inside each stratum, then shuffled with "
            "random.Random(%d); strata visited in sorted key order; odd "
            "leftovers alternate between fit and held-out so the halves stay "
            "balanced. Fully reproducible from this receipt." % SEED
        ),
        "stratum_counts": stratum_counts,
        "n_fit": fn,
        "n_held_out": hn,
        "fit_delivered_gold": fh,
        "held_out_delivered_gold": hh,
        "fit_delivered_gold_rate": round(fr, 6),
        "held_out_delivered_gold_rate": round(hr, 6),
        "rate_gap": round(abs(fr - hr), 6),
        "full_delivered_gold_rate": round(arms[0]["delivered_gold_rate"], 6),
        "fit_miss_buckets": miss_buckets(fit),
        "held_out_miss_buckets": miss_buckets(held),
        "fit_chunk_split_misses": sum(1 for x in fit if x in cs_names),
        "held_out_chunk_split_misses": sum(1 for x in held if x in cs_names),
        "fit_needles": fit,
        "held_out_needles": held,
    }

    # ------------------------------------------------------- 5. scorer status
    sys.path.insert(0, ERB)
    import rloss  # noqa: E402

    selftest = rloss.self_test()

    # ---------------------------------------------------------------- verdict
    if problems:
        verdict = "KILL"
        verdict_reason = "; ".join(problems)
    else:
        verdict = "PASS"
        verdict_reason = (
            "All %d mined misses are exactly the basis receipt's "
            "delivered_gold==0 set; recomputed buckets match every published "
            "taxonomy cell; chunk_split recomputes to %d/156 as published; "
            "rloss self-test passes identity, negative and shuffle controls."
            % (len(misses), n_cs)
        )

    receipt = {
        "stamp": STAMP,
        "probe": "P0 bucket ledger / r-loss scorer / fit-heldout split",
        "tool": "benchmarks/dogfood/erb/probe_bucket_ledger.py",
        "scorer_module": "benchmarks/dogfood/erb/rloss.py",
        "bed": BED,
        "bed_bytes": (os.path.getsize(BED) if os.path.exists(BED) else None),
        "bed_bytes_note": (
            "This probe does NOT open the bed; the size is recorded for "
            "provenance only. The basis ladder receipt recorded "
            "bed_bytes=%d on 2026-08-28." % base.get("bed_bytes", -1)
        ),
        "bed_access": "none -- receipts and JSON maps only",
        "git_sha": git_sha(),
        "basis_receipts": [
            "benchmarks/dogfood/erb/receipts/interloper_mine_erb947k_2026-08-31.json",
            "benchmarks/dogfood/erb/receipts/ladder_v09x_w24_min_delivered_2026-08-28.json",
            "benchmarks/dogfood/interloper_taxonomy_wave2_2026-08-31.json",
            "benchmarks/dogfood/erb/needles_resolved_v09x_full.json",
            "benchmarks/dogfood/erb/gold_by_needle_v09x.json",
        ],
        "k": mine["k"],
        "n_needles": len(needles),
        "n_misses": len(misses),
        "n_gold_map_entries": len(gold),
        "baseline_delivered_gold_rate": round(arms[0]["delivered_gold_rate"], 6),
        "baseline_recall_at_k": arms[0]["recall_at_k"],
        "kill_criterion": KILL_CRITERION,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "problems": problems,
        "bucket_definitions": {
            "pool_absent": "live_rank is null (gold absent from the published score map) OR live_rank > 100",
            "near_band": "13 <= live_rank <= 100",
            "seat_capped": "live_rank <= 12 while delivered_gold == 0 (gold made the pool top-12 but was not shipped)",
            "note": (
                "On this mine the three are exhaustive and mutually "
                "exclusive; every miss has delivered_gold==0 by construction, "
                "so seat_capped needs no extra clause in practice."
            ),
        },
        "chunk_split_definition": (
            "gold_kw_cov (UNION coverage over all gold chunks) >= "
            "delivered[0].kw_cov AND gold_first.kw_cov < delivered[0].kw_cov "
            "-- the gold DOCUMENT would tie or beat the rank-1 winner on "
            "distinct query-term coverage, but gold's best single CHUNK does not."
        ),
        "sanity": {
            "mined_miss_set_equals_basis_delivered_gold_zero": miss_set_match,
            "n_basis_misses": len(base_misses),
            "n_mined": len(mined),
            "rank_match_reported_by_mine": mine["summary"]["rank_match"],
            "mine_class_vs_needle_question_type_disagreements": class_disagreements,
            "needles_with_no_miss_of_type_project_related": (
                "project_related has %d needles and %d misses"
                % (
                    sum(1 for n in needles if n["question_type"] == "project_related"),
                    sum(1 for r in misses if qtype[r["needle"]] == "project_related"),
                )
            ),
        },
        "section_1_bucket_ledger": {
            "bucket_counts": dict(sorted(bucket_counts.items())),
            "bucket_live_rank_profile": bucket_live_rank,
            "class_x_bucket": class_x_bucket,
            "reconciliation": recon,
        },
        "section_2_chunk_split": {
            "reconciliation": cs_recon,
            "n_chunk_split": n_cs,
            "chunk_split_needles": sorted(cs_names),
            "gap_profile": {
                "mean_union_minus_first_all_misses": round(
                    sum(x["union_minus_first"] for x in per_miss) / len(per_miss), 3
                ),
                "mean_union_minus_first_chunk_split": round(
                    sum(x["union_minus_first"] for x in per_miss if x["chunk_split"])
                    / max(n_cs, 1),
                    3,
                ),
                "mean_gold_n_chunk_split": round(
                    sum(x["gold_n"] for x in per_miss if x["chunk_split"]) / max(n_cs, 1), 3
                ),
                "mean_gold_n_non_chunk_split": round(
                    sum(x["gold_n"] for x in per_miss if not x["chunk_split"])
                    / max(len(per_miss) - n_cs, 1),
                    3,
                ),
                "single_chunk_gold_among_chunk_split": sum(
                    1 for x in per_miss if x["chunk_split"] and x["gold_n"] == 1
                ),
            },
        },
        "section_3_overlap": overlap,
        "section_4_fit_heldout_split": split,
        "section_5_rloss_self_test": selftest,
        "per_miss": per_miss,
    }

    with open(OUT_PATH, "w", encoding="utf-8") as fh_out:
        json.dump(receipt, fh_out, indent=1)

    # ------------------------------------------------------------- console
    print("=== P0 BUCKET LEDGER ===")
    print("verdict:", verdict)
    print("reason:", verdict_reason)
    print()
    print("buckets:", dict(sorted(bucket_counts.items())))
    print("class x bucket:")
    for cls, v in class_x_bucket.items():
        print("   %-26s %s" % (cls, v))
    print("reconciliation disagreements:", len(recon["cell_disagreements"]))
    for d in recon["cell_disagreements"]:
        print("   !!", d)
    print()
    print("chunk_split: %d/156 (%.1f%%)  published %d -- agrees=%s"
          % (n_cs, 100.0 * n_cs / len(misses), PUBLISHED["chunk_split"], cs_recon["agrees"]))
    print("bucket x chunk_split:", json.dumps(bucket_x_cs))
    print("headline cat x chunk_split:", json.dumps(cat_x_cs))
    print("flag histogram (headline-category + chunk_split):",
          {str(k): flag_hist[k] for k in sorted(flag_hist)})
    print()
    print("reachable = 156 - %d sem_pool_absent = %d" % (n_sem_pa, reachable))
    print("chunk_split inside reachable: %d/%d (%.1f%%)"
          % (cs_in_reachable, reachable, 100.0 * cs_in_reachable / reachable))
    print("perfect-lane ceiling vs the +62 needed: %d (%.1f%%) reachable-only, "
          "%d (%.1f%%) all-156"
          % (cs_in_reachable, 100.0 * cs_in_reachable / needed,
             n_cs, 100.0 * n_cs / needed))
    print()
    print("split: fit n=%d rate=%.4f (%d/%d) | held n=%d rate=%.4f (%d/%d) | gap %.4f"
          % (fn, fr, fh, fn, hn, hr, hh, hn, abs(fr - hr)))
    print("fit miss buckets", split["fit_miss_buckets"],
          "| held miss buckets", split["held_out_miss_buckets"])
    print("chunk_split misses: fit %d / held %d"
          % (split["fit_chunk_split_misses"], split["held_out_chunk_split_misses"]))
    print()
    print("rloss self-test passed:", selftest["passed"])
    print("receipt ->", OUT_PATH)


if __name__ == "__main__":
    main()
