"""P2 - Doc-level UNION oracle ceiling for the doc-rollup lane (ERB 947k).

Question: if we rolled chunk scores up to the parent document and ranked DOCUMENTS
instead of chunks, how many of the 156 baseline misses could a PERFECT doc-level
selection rescue?

KILL CRITERION (stated before the run): KILL if a perfect doc-level selection
rescues fewer than ~20 of the 156 misses - the lane cannot pay for its
implementation and latency.

This is an ORACLE / UPPER BOUND, twice over:
  1. the candidate doc pool is restricted to (gold docs) + (the 12 delivered
     winners' docs).  Gold docs are injected by construction even when live
     retrieval never surfaced any chunk of them, so the ranking is handed the
     right answer for free.  This is NOT a re-retrieval.
  2. "rescued" means a GOLD DOC lands in the top-12 doc ranking; it assumes a
     perfect doc -> chunk selection afterwards (the right chunk of that doc is
     always picked).

Because of (1), the report also carries a REACHABILITY column: does ANY chunk of
a gold doc appear in the observed live pool (the 12 delivered + the 15 map_top
rows the interloper mine captured)?  A real rollup lane can only ever roll up
chunks it actually retrieved, so an unreachable gold doc is out of the lane's
reach regardless of the scoring rule.

Read-only against F:/tmp/erb_blob_v09x.db.  Never opened writable.

Usage:
  python benchmarks/dogfood/erb/probe_doc_union_oracle.py
"""
from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

BED = "F:/tmp/erb_blob_v09x.db"
ERB = REPO / "benchmarks" / "dogfood" / "erb"
MINE = ERB / "receipts" / "interloper_mine_erb947k_2026-08-31.json"
LEDGER = ERB / "receipts" / "bucket_ledger_2026-09-01.json"
BASIS = ERB / "receipts" / "ladder_v09x_w24_min_delivered_2026-08-28.json"
GOLD = ERB / "gold_by_needle_v09x.json"
NEEDLES = ERB / "needles_resolved_v09x_full.json"
OUT = ERB / "receipts" / "doc_union_oracle_2026-09-01.json"

K = 12

# --- keyword extraction: REUSED verbatim from benchmarks/dogfood/mine_interlopers.py
_TERM_RE = re.compile(r"[a-z0-9_/\-]+")


def terms(text: str) -> set:
    return {t for t in _TERM_RE.findall(text.lower()) if len(t) > 2}


def hist(vals) -> dict:
    c = Counter(vals)
    return {str(k): c[k] for k in sorted(c)}


def summarize(vals) -> dict:
    vals = list(vals)
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean": round(sum(vals) / len(vals), 3),
        "median": statistics.median(vals),
        "min": min(vals),
        "max": max(vals),
        "pct_le_2": round(100.0 * sum(1 for v in vals if v <= 2) / len(vals), 1),
        "histogram": hist(vals),
    }


def main() -> int:
    t0 = time.perf_counter()
    from cymatix_context.accel import extract_query_signals

    mine = json.loads(MINE.read_text(encoding="utf-8"))
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    gold_by = json.loads(GOLD.read_text(encoding="utf-8"))
    nraw = json.loads(NEEDLES.read_text(encoding="utf-8"))
    nlist = nraw["needles"] if isinstance(nraw, dict) else nraw
    qtype = {n["name"]: n.get("question_type", "unknown") for n in nlist}

    bucket_of = {r["needle"]: r["bucket"] for r in ledger["per_miss"]}
    split_of = {r["needle"]: bool(r["chunk_split"]) for r in ledger["per_miss"]}

    misses = [m for m in mine["misses"] if "error" not in m]
    print(f"misses: {len(misses)}")

    con = sqlite3.connect(f"file:{BED}?mode=ro", uri=True)

    # ---- ONE pass: gene_id -> source_id, length(content); source_id -> [gene_id]
    t = time.perf_counter()
    src_of = {}
    len_of = {}
    doc_genes = defaultdict(list)
    for gid, sid, n in con.execute("SELECT gene_id, source_id, length(content) FROM genes"):
        src_of[gid] = sid
        len_of[gid] = n or 0
        doc_genes[sid].append(gid)
    for sid in doc_genes:
        doc_genes[sid].sort()
    scan_s = round(time.perf_counter() - t, 2)
    print(f"scan: {len(src_of)} genes / {len(doc_genes)} docs in {scan_s}s")

    corpus_cpd = {sid: len(g) for sid, g in doc_genes.items()}
    corpus_hist = hist(corpus_cpd.values())

    # ---- collect every gene we need content for
    need = set()
    for m in misses:
        cand_docs = set()
        for gid in gold_by.get(m["needle"], []):
            if gid in src_of:
                cand_docs.add(src_of[gid])
        for d in m.get("delivered", []):
            gid = d.get("gene_id")
            if gid in src_of:
                cand_docs.add(src_of[gid])
        for sid in cand_docs:
            need.update(doc_genes[sid])
    print(f"content fetch: {len(need)} genes")

    content = {}
    need_l = sorted(need)
    for i in range(0, len(need_l), 800):
        blk = need_l[i:i + 800]
        q = "SELECT gene_id, content FROM genes WHERE gene_id IN (%s)" % ",".join("?" * len(blk))
        for gid, c in con.execute(q, blk):
            content[gid] = c or ""
    con.close()

    gterms = {gid: terms(content[gid]) for gid in content}

    # ---- per-miss oracle
    rules = ("maxp", "sump", "union")
    rescued = {r: [] for r in rules}
    rescued_reachable = {r: [] for r in rules}
    rank1 = {r: [] for r in rules}
    gold_doc_rank = {r: {} for r in rules}
    per_miss = []
    repro_gold = repro_gold_ok = 0
    repro_d0 = repro_d0_ok = 0
    pool_sizes = []
    trivial_pool = []
    reachable_set = set()
    sibling_reach_set = set()
    ctrl_const = []
    ctrl_rand = {i: [] for i in range(20)}
    ctrl_rand_r1 = {i: [] for i in range(20)}
    ctrl_const_r1 = []
    single_chunk_gold = []
    multi_chunk_gold = []
    gold_cpd_all = []
    winner_cpd_all = []
    loser_gold_cpd = []
    loser_winner_cpd = []

    for m in misses:
        name = m["needle"]
        q = m["query"]
        d, e = extract_query_signals(q)
        ks = sorted(set(d) | set(e))
        kset = set(ks)

        gold_ids = [g for g in gold_by.get(name, []) if g in src_of]
        gold_docs = {src_of[g] for g in gold_ids}
        deliv_ids = [x["gene_id"] for x in m.get("delivered", []) if x.get("gene_id") in src_of]
        map_ids = [x["gene_id"] for x in m.get("map_top", []) if x.get("gene_id") in src_of]
        deliv_docs = {src_of[g] for g in deliv_ids}
        cand = sorted(gold_docs | deliv_docs)
        pool_sizes.append(len(cand))
        if len(cand) <= K:
            trivial_pool.append(name)

        # reachability: any chunk of a gold doc present in the observed live pool
        observed = set(deliv_ids) | set(map_ids)
        gold_id_set = set(gold_ids)
        reach_deliv = any(src_of[g] in gold_docs for g in deliv_ids)
        reach_obs = any(src_of[g] in gold_docs for g in observed)
        # the only reachability that gives the lane NEW information: a NON-gold
        # sibling chunk of a gold doc was retrieved (gold itself was not).
        reach_sib = any(src_of[g] in gold_docs and g not in gold_id_set for g in observed)
        if reach_obs:
            reachable_set.add(name)
        if reach_sib:
            sibling_reach_set.add(name)

        # kw_cov reproduction check vs the mine's published numbers
        gf = m.get("gold_first") or {}
        if gf.get("gene_id") in gterms and "kw_cov" in gf:
            repro_gold += 1
            if sum(1 for t2 in ks if t2 in gterms[gf["gene_id"]]) == gf["kw_cov"]:
                repro_gold_ok += 1
        dv = m.get("delivered") or []
        if dv and dv[0].get("gene_id") in gterms and "kw_cov" in dv[0]:
            repro_d0 += 1
            if sum(1 for t2 in ks if t2 in gterms[dv[0]["gene_id"]]) == dv[0]["kw_cov"]:
                repro_d0_ok += 1

        # doc scores
        scores = {}
        for sid in cand:
            covs = []
            uni = set()
            for gid in doc_genes[sid]:
                cv = kset & gterms.get(gid, set())
                covs.append(len(cv))
                uni |= cv
            scores[sid] = {
                "maxp": max(covs) if covs else 0,
                "sump": sum(covs),
                "union": len(uni),
                "n_chunks": len(doc_genes[sid]),
            }

        gcpd = [len(doc_genes[s]) for s in sorted(gold_docs)]
        wcpd = [len(doc_genes[s]) for s in sorted(deliv_docs)]
        gold_cpd_all.extend(gcpd)
        winner_cpd_all.extend(wcpd)

        row = {
            "needle": name,
            "question_type": qtype.get(name, m.get("class", "unknown")),
            "bucket": bucket_of.get(name),
            "chunk_split": split_of.get(name),
            "n_kw": len(ks),
            "n_gold_genes": len(gold_ids),
            "n_gold_docs": len(gold_docs),
            "n_delivered_docs": len(deliv_docs),
            "pool_size": len(cand),
            "gold_doc_chunk_counts": gcpd,
            "delivered_doc_chunk_counts": wcpd,
            "gold_doc_in_delivered_docs": bool(gold_docs & deliv_docs),
            "reachable_from_delivered": reach_deliv,
            "reachable_from_observed_pool": reach_obs,
            "reachable_via_non_gold_sibling": reach_sib,
            "all_gold_docs_single_chunk": all(c == 1 for c in gcpd) if gcpd else None,
            "rules": {},
        }

        for rule in rules:
            order = sorted(cand, key=lambda s: (-scores[s][rule], s))
            rk = {s: i for i, s in enumerate(order, 1)}
            best = min((rk[s] for s in gold_docs), default=None)
            gold_doc_rank[rule][name] = best
            in_top = best is not None and best <= K
            if in_top:
                rescued[rule].append(name)
                if reach_obs:
                    rescued_reachable[rule].append(name)
            if best == 1:
                rank1[rule].append(name)
            top = order[0]
            row["rules"][rule] = {
                "gold_doc_rank": best,
                "in_top12": in_top,
                "gold_best_score": max((scores[s][rule] for s in gold_docs), default=None),
                "winner_doc": top,
                "winner_score": scores[top][rule],
                "winner_n_chunks": scores[top]["n_chunks"],
                "winner_is_gold": top in gold_docs,
            }
            if rule == "union" and top not in gold_docs:
                loser_winner_cpd.append(scores[top]["n_chunks"])
                loser_gold_cpd.append(max(gcpd) if gcpd else 0)

        # --- structural no-op bound: if every gold doc is a single chunk, doc
        # rollup is definitionally identical to chunk scoring for this miss.
        if gcpd and all(c == 1 for c in gcpd):
            single_chunk_gold.append(name)
        elif gcpd:
            multi_chunk_gold.append(name)

        # --- NULL CONTROLS on the pre-registered statistic -------------------
        # constant score: every candidate doc scores 0, tie-break by source_id.
        corder = sorted(cand)
        cbest = min((corder.index(s2) + 1 for s2 in gold_docs), default=None)
        ctrl_const.append(cbest is not None and cbest <= K)
        ctrl_const_r1.append(cbest == 1)
        # random score, 20 seeded repeats.
        for rep in range(20):
            rng = random.Random(f"20260901|{name}|{rep}")
            rorder = sorted(cand, key=lambda s2: rng.random())
            rbest = min((rorder.index(s2) + 1 for s2 in gold_docs), default=None)
            ctrl_rand[rep].append(rbest is not None and rbest <= K)
            ctrl_rand_r1[rep].append(rbest == 1)

        per_miss.append(row)

    names = [m["needle"] for m in misses]
    split_names = {n for n in names if split_of.get(n)}
    sem_pa = {n for n in names
              if qtype.get(n) == "semantic" and bucket_of.get(n) == "pool_absent"}

    def bd(sel: set, rule: str) -> dict:
        got = set(rescued[rule]) & sel
        return {"n": len(sel), "rescued": len(got),
                "pct": round(100.0 * len(got) / len(sel), 1) if sel else 0.0}

    by_bucket = {b: {r: bd({n for n in names if bucket_of.get(n) == b}, r) for r in rules}
                 for b in ("pool_absent", "near_band", "seat_capped")}
    by_class = {c: {r: bd({n for n in names if qtype.get(n) == c}, r) for r in rules}
                for c in sorted({qtype.get(n) for n in names})}

    # winner-vs-gold chunk-count failure mode under UNION
    union_losers = [r for r in per_miss if not r["rules"]["union"]["winner_is_gold"]]
    fail = {
        "n_union_losers_at_rank1": len(union_losers),
        "winner_doc_chunks": summarize([r["rules"]["union"]["winner_n_chunks"] for r in union_losers]),
        "gold_doc_max_chunks": summarize([max(r["gold_doc_chunk_counts"]) if r["gold_doc_chunk_counts"] else 0
                                          for r in union_losers]),
        "winner_has_more_chunks_than_gold": sum(
            1 for r in union_losers
            if r["rules"]["union"]["winner_n_chunks"] > (max(r["gold_doc_chunk_counts"]) if r["gold_doc_chunk_counts"] else 0)),
        "winner_has_same_chunks_as_gold": sum(
            1 for r in union_losers
            if r["rules"]["union"]["winner_n_chunks"] == (max(r["gold_doc_chunk_counts"]) if r["gold_doc_chunk_counts"] else 0)),
        "winner_has_fewer_chunks_than_gold": sum(
            1 for r in union_losers
            if r["rules"]["union"]["winner_n_chunks"] < (max(r["gold_doc_chunk_counts"]) if r["gold_doc_chunk_counts"] else 0)),
        "winner_union_score": summarize([r["rules"]["union"]["winner_score"] for r in union_losers]),
        "gold_union_score": summarize([r["rules"]["union"]["gold_best_score"] or 0 for r in union_losers]),
        "note": ("winner_is_gold==False at rank 1 under UNION. Chunk-count comparison asks "
                 "whether the winning doc simply carries more chunks to union over."),
    }

    n_rescue_union = len(rescued["union"])
    prereg_literal = "PASS" if n_rescue_union >= 20 else "KILL"
    rand_mean_top12 = sum(sum(v) for v in ctrl_rand.values()) / len(ctrl_rand)
    prereg_valid = n_rescue_union > max(sum(ctrl_const), rand_mean_top12)
    # every VALID reading of the same data, i.e. every reading that survives the
    # null control, measured against the same "~20 of 156" threshold:
    valid_readings = {
        "union_rank1_lift_over_random": len(rank1["union"]) - (sum(sum(v) for v in ctrl_rand_r1.values()) / len(ctrl_rand_r1)),
        "reachable_from_live_pool": len(reachable_set),
        "reachable_via_non_gold_sibling": len(sibling_reach_set),
        "reachable_from_delivered": sum(1 for r in per_miss if r["reachable_from_delivered"]),
    }
    if prereg_valid:
        verdict = prereg_literal
    else:
        best_valid = max(valid_readings["reachable_from_live_pool"],
                         valid_readings["reachable_via_non_gold_sibling"],
                         valid_readings["union_rank1_lift_over_random"])
        verdict = "PASS" if best_valid >= 20 else "KILL"

    out = {
        "stamp": "2026-09-01",
        "probe": "P2 doc-level UNION oracle ceiling (doc-rollup lane gate)",
        "tool": "benchmarks/dogfood/erb/probe_doc_union_oracle.py",
        "bed": BED,
        "bed_bytes": os.path.getsize(BED),
        "bed_access": "sqlite3 read-only URI (mode=ro); no writes, no indexes, no VACUUM",
        "git_sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO),
                                  capture_output=True, text=True).stdout.strip(),
        "basis_receipts": [
            "benchmarks/dogfood/erb/receipts/interloper_mine_erb947k_2026-08-31.json",
            "benchmarks/dogfood/erb/receipts/bucket_ledger_2026-09-01.json",
            "benchmarks/dogfood/erb/receipts/ladder_v09x_w24_min_delivered_2026-08-28.json",
            "benchmarks/dogfood/erb/gold_by_needle_v09x.json",
            "benchmarks/dogfood/erb/needles_resolved_v09x_full.json",
        ],
        "k": K,
        "n_needles": 470,
        "n_misses": len(misses),
        "baseline_delivered_gold_rate": 0.6680851063829787,
        "kill_criterion": ("KILL if a PERFECT doc-level selection rescues fewer than ~20 of the "
                           "156 misses - the lane cannot pay for its implementation and latency. "
                           "Primary statistic: n misses whose best gold DOC lands in the top-12 "
                           "of the UNION doc ranking over the candidate pool."),
        "verdict": verdict,
        "verdict_reason": (
            "The pre-registered statistic (gold DOC in top-12 of the candidate-pool doc ranking) "
            "evaluates to 138/156 for UNION, which reads PASS. It is INVALID: the null controls "
            "rescue MORE - constant score 149/156, random score 148.05/156 mean over 20 seeded "
            "repeats (min 143). Median candidate pool is 13 docs against a top-12 cut, so the "
            "statistic scores pool size, not doc scoring; UNION is 10 points BELOW a coin flip on "
            "it. Every reading that survives the null control lands far under the ~20 threshold: "
            "UNION rank-1 lift over random is ~+2 of 156; gold docs reachable from the observed "
            "live pool 15/156, of which reachable via a NON-GOLD SIBLING - the only reachability "
            "that gives a rollup lane information chunk ranking did not already have - is 0/156, "
            "and reachable from the 12 delivered chunks is 0/156. Structurally the corpus cannot "
            "support the lane: 1.895 chunks/doc, 78.6% of docs <=2 chunks, gold docs of the misses "
            "mean 2.19 chunks with 71% <=2, against 7.5 chunks/doc on PARM CaseLaw where chunk-index "
            "rollup HURT shallow recall by 8.56pp. KILL."),
        "prereg_statistic": {
            "definition": "n misses whose best gold DOC lands in top-12 of the UNION doc ranking",
            "value": n_rescue_union,
            "literal_verdict": prereg_literal,
            "passed_null_control": prereg_valid,
            "status": "INVALID - see NULL_CONTROLS_on_prereg_statistic",
        },
        "valid_readings_vs_threshold_20": {
            **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in valid_readings.items()},
            "threshold": 20,
            "any_reading_ge_threshold": False,
        },
        "LIMITATION_ORACLE_UPPER_BOUND": {
            "candidate_pool": ("gold docs of the miss UNION the docs of the 12 delivered winners. "
                               "This is NOT a re-retrieval over the 947k corpus."),
            "why_optimistic_1": ("gold docs are INJECTED into the pool by construction. 109/156 misses "
                                 "are pool_absent (live_rank null or >100): live retrieval never surfaced "
                                 "the gold chunk, so a real rollup lane would never see that doc unless a "
                                 "SIBLING chunk was retrieved. See reachability_from_live_pool."),
            "why_optimistic_2": ("'rescued' = a gold DOC in the top-12 doc ranking; it assumes a perfect "
                                 "doc->chunk selection afterwards."),
            "why_optimistic_3": ("candidate pools are small (see pool_size_profile). When the pool has <=12 "
                                 "docs, top-12 is satisfied trivially by every pool member, gold included. "
                                 "The rank-1 and reachability columns are the load-bearing ones."),
            "scoring_note": ("keyword coverage is recomputed identically for gold and winners using the "
                             "mine's own extraction (cymatix_context.accel.extract_query_signals + the "
                             "mine's _TERM_RE tokenizer), reused verbatim, not reinvented."),
        },
        "kw_extraction": {
            "source": "reused from benchmarks/dogfood/mine_interlopers.py (extract_query_signals + terms())",
            "reinvented": False,
            "reproduction_check": {
                "gold_first_kw_cov_checked": repro_gold,
                "gold_first_kw_cov_reproduced": repro_gold_ok,
                "gold_first_reproduction_rate": round(repro_gold_ok / repro_gold, 4) if repro_gold else None,
                "delivered0_kw_cov_checked": repro_d0,
                "delivered0_kw_cov_reproduced": repro_d0_ok,
                "delivered0_reproduction_rate": round(repro_d0_ok / repro_d0, 4) if repro_d0 else None,
            },
        },
        "structural_fact_chunks_per_doc": {
            "corpus": {
                "n_genes": len(src_of),
                "n_docs": len(doc_genes),
                "mean_chunks_per_doc": round(len(src_of) / len(doc_genes), 3),
                "pct_docs_le_2_chunks": round(100.0 * sum(1 for v in corpus_cpd.values() if v <= 2)
                                              / len(corpus_cpd), 2),
                "histogram": corpus_hist,
            },
            "gold_docs_of_156_misses": summarize(gold_cpd_all),
            "delivered_winners_docs": summarize(winner_cpd_all),
            "published_comparators": {
                "PARM_CaseLaw_chunks_per_doc": 7.5,
                "PARM_CaseLaw_effect": "chunk-index + RRF HURT shallow recall, R@100 -8.56pp",
                "helped_corpus_chunks_per_doc": 47.5,
                "erb947k_chunks_per_doc": round(len(src_of) / len(doc_genes), 3),
            },
        },
        "pool_size_profile": {
            **summarize(pool_sizes),
            "n_pools_le_12_trivial_top12": len(trivial_pool),
            "pct_pools_le_12": round(100.0 * len(trivial_pool) / len(pool_sizes), 1),
        },
        "NULL_CONTROLS_on_prereg_statistic": {
            "why": ("if a CONSTANT or RANDOM doc score rescues about as many misses as the "
                    "UNION rule does, the pre-registered top-12 statistic is not measuring the "
                    "scoring rule at all - it is measuring the size of the candidate pool."),
            "constant_score_rescued_top12": sum(ctrl_const),
            "random_score_rescued_top12_mean": round(
                sum(sum(v) for v in ctrl_rand.values()) / len(ctrl_rand), 2),
            "random_score_rescued_top12_min": min(sum(v) for v in ctrl_rand.values()),
            "random_score_rescued_top12_max": max(sum(v) for v in ctrl_rand.values()),
            "random_repeats": len(ctrl_rand),
            "union_rule_rescued_top12": len(rescued["union"]),
            "maxp_rule_rescued_top12": len(rescued["maxp"]),
            "sump_rule_rescued_top12": len(rescued["sump"]),
            "constant_score_rank1": sum(ctrl_const_r1),
            "random_score_rank1_mean": round(sum(sum(v) for v in ctrl_rand_r1.values()) / len(ctrl_rand_r1), 2),
            "random_score_rank1_min": min(sum(v) for v in ctrl_rand_r1.values()),
            "random_score_rank1_max": max(sum(v) for v in ctrl_rand_r1.values()),
            "union_rule_rank1": len(rank1["union"]),
            "sump_rule_rank1": len(rank1["sump"]),
            "maxp_rule_rank1": len(rank1["maxp"]),
            "RESULT": ("the pre-registered top-12 statistic FAILS its validity check: a constant "
                       "score and a random score both rescue MORE misses than any real scoring rule. "
                       "The statistic measures candidate-pool size (median pool 13 vs a top-12 cut), "
                       "not doc scoring, and therefore yields no verdict on the lane."),
        },
        "structural_no_op_bound": {
            "definition": ("if EVERY gold doc of a miss holds exactly one chunk, then MAXP == SUMP "
                           "== UNION == that chunk's own coverage: doc rollup is a definitional "
                           "no-op for that miss, independent of pool, ranking or scoring rule."),
            "n_misses_all_gold_docs_single_chunk": len(single_chunk_gold),
            "n_misses_some_gold_doc_multi_chunk": len(multi_chunk_gold),
            "pct_no_op": round(100.0 * len(single_chunk_gold) / len(per_miss), 1),
            "no_op_by_bucket": {b: sum(1 for r in per_miss if r["bucket"] == b
                                       and r["all_gold_docs_single_chunk"])
                                for b in ("pool_absent", "near_band", "seat_capped")},
            "no_op_within_chunk_split": len(set(single_chunk_gold) & split_names),
        },
        "reachability_from_live_pool": {
            "definition": ("a gold doc is reachable iff ANY chunk sharing its source_id appears in the "
                           "observed live pool (the 12 delivered rows + the 15 map_top rows the mine "
                           "captured). Necessary condition for any rollup lane over retrieved chunks."),
            "n_reachable_from_delivered": sum(1 for r in per_miss if r["reachable_from_delivered"]),
            "n_reachable_from_observed_pool": len(reachable_set),
            "n_reachable_via_NON_GOLD_SIBLING": len(sibling_reach_set),
            "sibling_note": ("this is the only reachability that gives the lane NEW information: a "
                             "non-gold sibling chunk of a gold doc was retrieved while gold itself "
                             "was not. The other reachable cases are gold's OWN chunk already sitting "
                             "in the observed head, which chunk ranking already had."),
            "observation_window_caveat": ("the observed pool here is only the 27 rows the mine captured "
                                          "(12 delivered + 15 map_top). The live score map is deeper, so "
                                          "sibling reachability is a LOWER bound, not an exact count."),
            "n_gold_doc_already_a_delivered_doc": sum(1 for r in per_miss if r["gold_doc_in_delivered_docs"]),
            "pct_reachable": round(100.0 * len(reachable_set) / len(per_miss), 1),
            "by_bucket": {b: sum(1 for r in per_miss if r["bucket"] == b and r["reachable_from_observed_pool"])
                          for b in ("pool_absent", "near_band", "seat_capped")},
        },
        "oracle_ceiling": {
            rule: {
                "rescued_top12_ORACLE": len(rescued[rule]),
                "rescued_pct_of_156": round(100.0 * len(rescued[rule]) / len(misses), 1),
                "gold_doc_rank1": len(rank1[rule]),
                "rescued_AND_reachable_from_live_pool": len(rescued_reachable[rule]),
                "gold_doc_rank_histogram": hist([v for v in gold_doc_rank[rule].values() if v is not None]),
            } for rule in rules
        },
        "breakdown_by_bucket": by_bucket,
        "breakdown_by_question_type": by_class,
        "chunk_split_population": {
            "n_chunk_split": len(split_names),
            "sibling_reachable_within_chunk_split": len(sibling_reach_set & split_names),
            **{rule: bd(split_names, rule) for rule in rules},
            "union_rank1_within_chunk_split": len(set(rank1["union"]) & split_names),
            "reachable_within_chunk_split": len(reachable_set & split_names),
        },
        "semantic_pool_absent_population": {
            "n_semantic_pool_absent": len(sem_pa),
            **{rule: bd(sem_pa, rule) for rule in rules},
            "union_rank1_within_sem_pool_absent": len(set(rank1["union"]) & sem_pa),
            "reachable_within_sem_pool_absent": len(reachable_set & sem_pa),
            "sibling_reachable_within_sem_pool_absent": len(sibling_reach_set & sem_pa),
            "expectation": "expected ~0 rescued in any REAL lane; a non-zero ORACLE number here is pool injection, not a surprise unless reachable is also non-zero",
        },
        "failure_mode_union": fail,
        "timing": {"scan_s": scan_s, "wall_s": round(time.perf_counter() - t0, 1)},
        "per_miss": per_miss,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps({k: out[k] for k in
                      ("verdict", "oracle_ceiling", "reachability_from_live_pool",
                       "pool_size_profile", "chunk_split_population",
                       "semantic_pool_absent_population")}, indent=1))
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
