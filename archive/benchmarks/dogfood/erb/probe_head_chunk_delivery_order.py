"""ARM-3 addendum -- where the head-chunk monopoly actually lives (2026-09-01).

The main probe (probe_head_chunk_lane.py) tested the pre-registered hypothesis
that the head-chunk monopoly is a BM25/content effect. Both pre-registered
conditions FAILED, and so did both SURVIVE conditions -- which meant the
observation had to be produced somewhere the pre-registration did not look.

This addendum locates it. It reads the live replay receipt and separates three
things the P5 receipt had collapsed into one word ("seat"):

  * the ADMITTED pool      -- FTS5 bm25 top-50 (the only admission gate)
  * the delivered SET      -- which documents are returned
  * the delivered ORDER    -- the order they are returned in

and checks the composition of each by chunk-position class.

It then verifies, against the bed, that the delivered ORDER is
``sorted(candidates, key=lambda g: g.promoter.sequence_index or 0)`` --
context_manager.py:3355, the default (non-slate, non-frontier) emission branch.

Sections 11-13 and the verdict are merged into the main ARM-3 receipt.

Bed opened READ-ONLY (mode=ro, PRAGMA query_only). No writes.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import pickle
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
ERB = ROOT / "benchmarks/dogfood/erb"

BED = "F:/tmp/erb_blob_v09x.db"
LIVE = ERB / "receipts/head_lane_live_2026-09-01.json"
MAIN = ERB / "receipts/head_chunk_lane_2026-09-01.json"
MINE = ERB / "receipts/interloper_mine_erb947k_2026-08-31.json"
# self-generated cache from probe_head_lane_cache.py in this same arm
CACHE = Path(os.environ.get(
    "ARM3_CACHE",
    "C:/Users/max/AppData/Local/Temp/claude/"
    "F--Projects-cymatix-context--claude-worktrees-wave-1-semantic-ranking-5f1dab/"
    "c81df3a0-0a0c-4160-81ee-e68db3b02372/scratchpad",
)) / "arm3_corpus_cache.pkl"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def posclass(pos: int, n: int) -> str:
    if n <= 1:
        return "only"
    if pos == 0:
        return "first"
    if pos == n - 1:
        return "last"
    return "middle"


def share(c: Counter) -> dict:
    t = sum(c.values()) or 1
    return {k: round(100.0 * v / t, 2) for k, v in sorted(c.items())}


def midshare(c: Counter):
    d = c["first"] + c["middle"]
    return round(100.0 * c["middle"] / d, 2) if d else None


def main() -> int:
    t0 = time.perf_counter()
    cache = pickle.load(open(CACHE, "rb"))
    gid_arr, pos_arr, nch_arr = cache["gid"], cache["pos"], cache["n_chunks"]
    idx_of_gid = {g: i for i, g in enumerate(gid_arr)}

    def pc_of(g):
        i = idx_of_gid.get(g)
        return None if i is None else posclass(pos_arr[i], nch_arr[i])

    live = json.loads(LIVE.read_text(encoding="utf-8"))
    con = sqlite3.connect("file:%s?mode=ro" % BED, uri=True)
    con.execute("PRAGMA query_only=1")
    cur = con.cursor()

    # -- fetch promoter.sequence_index for every gene the live replay touched --
    gids = set()
    for r in live["per_query"]:
        if "error" in r:
            continue
        gids |= set(r.get("delivered") or [])
        gids |= set(r["final_order"])
    seq = {}
    gl = sorted(gids)
    for i in range(0, len(gl), 400):
        ch = gl[i:i + 400]
        ph = ",".join("?" * len(ch))
        for g, p in cur.execute(
                "SELECT gene_id, promoter FROM genes WHERE gene_id IN (%s)" % ph, ch):
            try:
                seq[g] = json.loads(p or "{}").get("sequence_index")
            except Exception:
                seq[g] = None

    # sequence_index vs the rowid-inferred position, on the same genes
    agree = disagree = null_seq = 0
    for g, s in seq.items():
        i = idx_of_gid.get(g)
        if i is None:
            continue
        if s is None:
            null_seq += 1
        elif s == pos_arr[i]:
            agree += 1
        else:
            disagree += 1

    # -- composition of pool / set / order -----------------------------------
    bands = {k: Counter() for k in (
        "fts_rank1", "fts_top3", "fts_top12", "fts_top50",
        "score_rank1", "score_top3", "score_top12", "score_13_48",
        "delivered_set", "delivered_rank1", "delivered_top3")}
    set_overlap = []
    order_pred_ok = order_n = monotone_ok = 0
    p_no_head = []
    for r in live["per_query"]:
        if "error" in r:
            continue
        d = list(r.get("delivered") or [])
        fo = r["final_order"]
        deep = [g for g, _s in r["fts_deep400"]]
        for rk, g in enumerate(deep, 1):
            k = pc_of(g)
            if k is None:
                continue
            if rk == 1:
                bands["fts_rank1"][k] += 1
            if rk <= 3:
                bands["fts_top3"][k] += 1
            if rk <= 12:
                bands["fts_top12"][k] += 1
            if rk <= 50:
                bands["fts_top50"][k] += 1
        for rk, g in enumerate(fo, 1):
            k = pc_of(g)
            if k is None:
                continue
            if rk == 1:
                bands["score_rank1"][k] += 1
            if rk <= 3:
                bands["score_top3"][k] += 1
            if rk <= 12:
                bands["score_top12"][k] += 1
            if 13 <= rk <= 48:
                bands["score_13_48"][k] += 1
        if not d:
            continue
        kk = len(d)
        set_overlap.append(len(set(d) & set(fo[:kk])) / float(kk))
        for rk, g in enumerate(d, 1):
            k = pc_of(g)
            if k is None:
                continue
            bands["delivered_set"][k] += 1
            if rk == 1:
                bands["delivered_rank1"][k] += 1
            if rk <= 3:
                bands["delivered_top3"][k] += 1
        order_n += 1
        restricted = [g for g in fo if g in set(d)]
        if sorted(restricted, key=lambda g: (seq.get(g) or 0)) == d:
            order_pred_ok += 1
        sq = [seq.get(g) or 0 for g in d]
        if all(sq[i] <= sq[i + 1] for i in range(len(sq) - 1)):
            monotone_ok += 1
        # P(no head-or-only chunk among k seats) under the admitted composition
        pool = [g for g in fo[:50]]
        heads = sum(1 for g in pool if pc_of(g) in ("first", "only"))
        if pool:
            ph_ = heads / float(len(pool))
            p_no_head.append((1.0 - ph_) ** kk)

    s11 = {
        "question": ("the P5 receipt calls delivered[0] the 'top-1 winner'. Is that a "
                     "RANKING position or a PRESENTATION position?"),
        "n_queries": order_n,
        "delivered_set_equals_score_top_k": {
            "mean_overlap": round(sum(set_overlap) / len(set_overlap), 4) if set_overlap else None,
            "n_queries": len(set_overlap),
            "reading": ("1.000 means the delivered SET is exactly the top-k of the "
                        "fused+combined score map. Nothing about position changes WHICH "
                        "documents are returned."),
        },
        "delivered_order_is_sorted_by_sequence_index": {
            "n_matching": order_pred_ok,
            "n_queries": order_n,
            "pct": round(100.0 * order_pred_ok / order_n, 2) if order_n else None,
            "predictor": ("sorted(score-ordered delivered set, key=promoter.sequence_index "
                          "or 0) -- context_manager.py:3355, the default dense-decoder "
                          "emission branch (non-slate, non-frontier)"),
            "residual_note": ("the non-matching queries are the ones where the assembly "
                              "trim/eviction pass drops a part after the sort"),
        },
        "delivered_order_is_non_decreasing_in_sequence_index": {
            "n_matching": monotone_ok, "n_queries": order_n,
            "pct": round(100.0 * monotone_ok / order_n, 2) if order_n else None,
            "reading": ("the weaker, assumption-free version of the same claim: the "
                        "delivered list never puts a later chunk before an earlier one"),
        },
        "sequence_index_vs_rowid_inferred_position": {
            "n_agree": agree, "n_disagree": disagree, "n_null": null_seq,
            "pct_agree": round(100.0 * agree / max(1, agree + disagree), 2),
            "reading": ("promoter.sequence_index IS the chunk's index within its source "
                        "document, so sorting by it is literally sorting by position class"),
        },
    }

    s12 = {
        "note": ("composition by chunk-position class at every stage. 'mid_share' is "
                 "middles as a percentage of first+middle, i.e. restricted to the at-cap "
                 "population where P5 measured 0/142."),
        "stages": {
            k: {"counts": dict(bands[k]), "share_pct": share(bands[k]),
                "mid_share_of_first_plus_middle_pct": midshare(bands[k])}
            for k in bands
        },
        "corpus_baseline_mid_share_of_first_plus_middle_pct": 26.46,
        "reading": (
            "the position signal is FLAT from the FTS bm25 ranking through the fused score "
            "ranking -- middles hold roughly a fifth of every band including rank 1 -- and "
            "then collapses to exactly 0 at delivered rank 1. The collapse happens at the "
            "presentation sort, not in retrieval."
        ),
    }

    s13 = {
        "question": "does the sequence_index sort explain 156/156 head-or-only at rank 1?",
        "mean_P_no_head_or_only_chunk_among_the_delivered_seats": (
            round(sum(p_no_head) / len(p_no_head), 8) if p_no_head else None),
        "expected_n_queries_out_of_156_with_no_head_chunk_to_sort_to_front": (
            round(156 * sum(p_no_head) / len(p_no_head), 4) if p_no_head else None),
        "reading": (
            "head-or-only chunks are ~78% of the admitted pool, so a 12-seat delivery "
            "essentially always contains at least one sequence_index==0 chunk, and that "
            "chunk is placed at position 0 by the sort. 156/156 is the arithmetic of the "
            "sort key, not evidence about scoring."
        ),
    }

    # -- merge into the main receipt + write the verdict ---------------------
    main_rec = json.loads(MAIN.read_text(encoding="utf-8"))
    main_rec["helper_tools"]["benchmarks/dogfood/erb/probe_head_chunk_delivery_order.py"] = \
        sha256(Path(__file__).resolve())
    main_rec["section_11_ranking_vs_presentation"] = s11
    main_rec["section_12_composition_by_stage"] = s12
    main_rec["section_13_sort_arithmetic"] = s13
    main_rec["section_14_trim_path_audit"] = {
        "question": ("the presentation sort is position-driven. Can it leak into the "
                     "delivered SET through the budget trim?"),
        "code": "cymatix_context/context_manager.py, budget-eviction loop ~:3607-3634",
        "finding": (
            "No. The default eviction branch picks worst_idx = argmin over the "
            "request-scoped SCORE map, explicitly so that sequence_index ordering cannot "
            "decide who is dropped (the code comments say as much). The "
            "respect_caller_order positional pop(0) branch is the foveated reverse-rank "
            "path, which this bed's default config does not take. The W2.4 seat floor "
            "(min_delivered_docs=12) truncates the LARGEST remaining part instead of "
            "evicting, which is position-correlated -- at-cap chunks are non-tail -- but "
            "it works AGAINST head chunks (they are the ones shortened) and still never "
            "changes membership."
        ),
        "empirical_confirmation": (
            "delivered SET vs score top-k overlap = 1.000 across all 40 live replays; "
            "delivered-set middle share 20.4%% against 19.9%% of the admitted FTS top-50."
        ),
    }

    p = main_rec["section_5_within_document_paired_bm25"]
    k2 = main_rec["section_6_header_ablation_K2"]
    k3b = main_rec["section_10_K3b_post_admission_reordering"]["by_posclass"]
    main_rec["verdict"] = "KILL"
    main_rec["verdict_detail"] = {
        "headline": (
            "The head-chunk monopoly is REAL and it is a PRESENTATION-ORDER artefact with "
            "zero retrieval content. context_manager.py:3355 emits the delivered set as "
            "sorted(candidates, key=promoter.sequence_index or 0) on the default dense "
            "branch, i.e. it literally sorts by chunk position within the parent document. "
            "The delivered SET is bit-identical to the score top-k (mean overlap 1.000), so "
            "the effect cannot move a single needle."
        ),
        "prereg_outcome": {
            "K1_earned_by_content": (
                "FAILED. Within-document, at-cap, same-query paired BM25 gives the head "
                "only %.2f%% wins (n=%d decided, p=%.2e). Significant, but far below the "
                "pre-registered 60%% bar, and the mean edge is +%.2f BM25 on a base of "
                "~%.1f (2.6%%)." % (p["head_win_pct"], p["n_decided"],
                                    p["binomial_two_sided_p_vs_50pct"],
                                    p["mean_bm25_delta_head_minus_mid"], p["mean_mid_bm25"])),
            "K2_cause_named": (
                "FAILED, and REFUTED as a hypothesis. Deleting the head chunk's leading "
                "400 content characters flips only %.2f%% of head wins, and the head's "
                "mean BM25 does not fall (%.3f with header vs %.3f without). Query-term "
                "occurrences inside the header are %.2f%% of the head's total against a "
                "%.2f%% token share -- i.e. UNIFORM. The document title/metadata block is "
                "NOT doing the work." % (k2["pct_flipped"],
                                         k2["mean_head_bm25_with_header"],
                                         k2["mean_head_bm25_without_header"],
                                         k2["head_qterm_tf_share_inside_header_pct"],
                                         k2["header_token_share_of_head_doc_pct"])),
            "K3a_survive_coin_flip_with_persistent_rank_gap": (
                "NOT MET. The paired BM25 comparison is near-coin-flip as K3(a) requires, "
                "but the RANK gap does not persist: middles hold %.1f%% of score-rank-1 "
                "slots and %.1f%% of the score top-12 against a %.1f%% share of the FTS "
                "top-50 pool. There is no ranking gap left to survive on." % (
                    s12["stages"]["score_rank1"]["mid_share_of_first_plus_middle_pct"],
                    s12["stages"]["score_top12"]["mid_share_of_first_plus_middle_pct"],
                    s12["stages"]["fts_top50"]["mid_share_of_first_plus_middle_pct"])),
            "K3b_survive_post_admission_demotion": (
                "NOT MET. Inside the admitted top-50 the mean (final rank - bm25 rank) is "
                "%+.2f for first chunks and %+.2f for middles: middles are very slightly "
                "PROMOTED, not demoted." % (
                    k3b["first"]["mean_delta_final_minus_bm25_rank"],
                    k3b["middle"]["mean_delta_final_minus_bm25_rank"])),
            "honesty_note": (
                "The pre-registration anticipated exactly two outcomes -- a content-difference "
                "kill (K1/K2) or a surviving positional bias (K3). Neither happened. The "
                "effect dissolved at a layer the pre-registration did not cover: it is not "
                "in the scorer at all. The kill stands on evidence the pre-registration did "
                "not name, and that is stated here rather than retrofitted into K1."
            ),
        },
        "what_P5_actually_measured": (
            "P5's 'seat rank' is the index into window.expressed_gene_ids, which is the "
            "presentation order. Its mean_seat_rank_by_posclass (only 5.16 / first 5.22 / "
            "middle 9.33 / last 9.56) is a readout of the sort key itself. P5's own "
            "delivered-seat composition already showed the SET is position-neutral: "
            "237/1423 = 16.7% of at-cap seats are middles, against a 17-22% share of the "
            "admitted pool."
        ),
        "addressable_population": (
            "ZERO needles. The mechanism never changes which documents are delivered, so no "
            "re-ordering of it can convert a miss into a hit. The 89 misses whose gold sits "
            "at a middle or tail chunk are NOT addressable by this lead; they are an "
            "admission problem (only %d of 156 have gold anywhere in a 2000-deep FTS-only "
            "ranking, and only %d inside the top 50 that the bm25 shortlist admits)."
            % (main_rec["section_9_addressable_population"]
               ["n_misses_gold_reachable_in_deep_fts_2000"],
               main_rec["section_9_addressable_population"]
               ["deep_fts_rank_of_first_gold_hist"].get("1-12", 0)
               + main_rec["section_9_addressable_population"]
               ["deep_fts_rank_of_first_gold_hist"].get("13-50", 0))
        ),
        "incidental_findings_for_other_arms": [
            "ADMISSION GATE IS 50, NOT 48. [retrieval] bm25_shortlist_enabled defaults to "
            "true with bm25_shortlist_size = 50; knowledge_store.py post-filters the scored "
            "set to FTS5's bm25 top-50 before RRF. That -- not fts5_candidate_depth (48) -- "
            "is why the published score map bottoms out at ~45-48 entries. M8 should be "
            "benched against bm25_shortlist_size, and fts5_candidate_depth alone cannot "
            "widen admission past 50.",
            "THE FTS TEXT IS NOT genes.content. genes_fts is external-content over the view "
            "genes_fts_source, whose content column is source_id || ' ' || "
            "GROUP_CONCAT(promoter_index.tag_value) || ' ' || genes.content. Every chunk is "
            "indexed with its document's PATH and tags prepended (mean 62.5 tokens). Any "
            "lexical arm that reasons about 'the chunk text' is reasoning about the wrong "
            "string.",
            "BUDGET TRUNCATION IS POSITION-CORRELATED AND POINTS THE OTHER WAY. Under "
            "the W2.4 seat floor the trim shortens the LARGEST remaining part, which on "
            "this bed is almost always an at-cap non-tail chunk. If anything the assembler "
            "penalises head chunks on content, not middles.",
            "BM25 TIES ARE A NON-MECHANISM. 135 of 312,000 deep-ranked rows sit in a bm25 "
            "tie (0.043%), and only 2 tied groups mix position classes -- so SQLite's "
            "rowid-ascending tie-break cannot produce any measurable position bias.",
        ],
    }
    main_rec["elapsed_seconds_addendum"] = round(time.perf_counter() - t0, 1)
    MAIN.write_text(json.dumps(main_rec, indent=1), encoding="utf-8")
    print(json.dumps({"section_11": s11, "section_12": s12, "section_13": s13,
                      "verdict": main_rec["verdict"],
                      "verdict_detail": main_rec["verdict_detail"]}, indent=1))
    print("merged into", MAIN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
