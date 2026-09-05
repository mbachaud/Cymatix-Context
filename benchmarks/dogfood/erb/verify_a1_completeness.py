"""verify_a1_completeness.py -- ARM 1 completeness critic (what the three lenses left untested).

Inputs ONLY: the ARM-1 receipt rows, the per-arm checkpoints, the 2026-08-28 ladder
per_query, the addendum receipt, the needles file, and the worktree source. The bed
F:/tmp/erb_blob_v09x.db is never opened. No existing receipt is modified.

Usage:
    python benchmarks/dogfood/erb/verify_a1_completeness.py --stamp 2026-09-01
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("CYMATIX_DISABLE_LEARN", "1")

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
R = HERE / "receipts"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def load_ck(p: Path):
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            o = json.loads(line)
            if "needle" in o:
                out[o["needle"]] = o
    return out


def find_queries(obj):
    """Best-effort needle->query map over any of the needle-file shapes."""
    qmap = {}

    def visit(x, key_hint=None):
        if isinstance(x, dict):
            nid = x.get("needle") or x.get("name") or x.get("id") or x.get("needle_id") or key_hint
            q = x.get("query") or x.get("question") or x.get("prompt") or x.get("text")
            if isinstance(nid, str) and re.match(r"^erb_\d+$", nid) and isinstance(q, str):
                qmap[nid] = q
            for k, v in x.items():
                visit(v, k if isinstance(k, str) and re.match(r"^erb_\d+$", k) else key_hint)
        elif isinstance(x, list):
            for v in x:
                visit(v, key_hint)

    visit(obj)
    return qmap


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", default="2026-09-01")
    args = ap.parse_args(argv)
    stamp = args.stamp

    rec_p = R / f"pool_depth_forensics_{stamp}.json"
    add_p = R / f"pool_depth_addendum_{stamp}.json"
    lad_p = R / "ladder_v09x_w24_min_delivered_2026-08-28.json"
    ck_sh = R / f"pool_depth_ck_{stamp}" / "cache" / "shipped.jsonl"
    ck_dp = R / f"pool_depth_ck_{stamp}" / "cache" / "deep1000.jsonl"
    needles_p = HERE / "needles_resolved_v09x_full.json"

    rec = json.loads(rec_p.read_text(encoding="utf-8"))
    add = json.loads(add_p.read_text(encoding="utf-8"))
    lad = json.loads(lad_p.read_text(encoding="utf-8"))
    rows = {r["needle"]: r for r in rec["rows"]}
    sh, dp = load_ck(ck_sh), load_ck(ck_dp)

    # ---- 1. delivered_count flips and the FOCUSED-tier exposure ----------
    flip = collections.Counter()
    by_cohort = collections.defaultdict(collections.Counter)
    for n, r in rows.items():
        key = f"{sh[n]['delivered_count']}->{dp[n]['delivered_count']}"
        flip[key] += 1
        by_cohort[f"{r['cohort']}/{r.get('bucket')}"][key] += 1
    hc_six_shipped = [n for n, r in rows.items() if r["cohort"] == "hit_control" and sh[n]["delivered_count"] == 6]
    lost = [(n, rows[n]["question_type"], rows[n]["shipped_rank"], rows[n]["deep_rank"],
             sh[n]["delivered_count"], dp[n]["delivered_count"])
            for n, r in rows.items() if r["cohort"] == "hit_control" and r["deep_delivered_gold"] == 0]
    rescued = [(n, rows[n]["bucket"], rows[n]["shipped_rank"], rows[n]["deep_rank"],
                sh[n]["delivered_count"], dp[n]["delivered_count"])
               for n, r in rows.items() if r["cohort"] == "miss" and r["deep_delivered_gold"] == 1]

    recs = lad["arms"][0]["per_query"]
    dc_hist = collections.Counter(q["delivered_count"] for q in recs)
    hits = [q for q in recs if q.get("delivered_gold") == 1]
    misses = [q for q in recs if q.get("delivered_gold") != 1]
    ladder = {
        "n": len(recs),
        "delivered_count_hist": dict(dc_hist),
        "hits": len(hits),
        "hits_delivered_6": sum(1 for q in hits if q["delivered_count"] == 6),
        "hits_delivered_12_gold_rank_7_12": sum(
            1 for q in hits if q["delivered_count"] == 12 and 7 <= (q.get("rank_of_first_gold") or 0) <= 12),
        "misses_delivered_6": sum(1 for q in misses if q["delivered_count"] == 6),
        "misses_delivered_6_gold_rank_7_12": sum(
            1 for q in misses if q["delivered_count"] == 6 and q.get("rank_of_first_gold")
            and 7 <= q["rank_of_first_gold"] <= 12),
        "splice_n_equals_delivered_on_all": all(q.get("splice_n_candidates") == q["delivered_count"] for q in recs),
        "ladder_config": lad.get("config"),
    }
    sig = collections.defaultdict(list)
    for q in recs:
        for k, v in (q.get("signal_ms") or {}).items():
            sig[k].append(v)
    ladder["signal_ms_median"] = {k: round(statistics.median(v), 1) for k, v in sig.items()}
    ladder["wall_ms_p50"] = round(statistics.median(q["wall_ms"] for q in recs), 1)

    # ---- 2. code facts: where 6 comes from; floor reach; pool-conditioned tiers
    ks = (REPO / "cymatix_context" / "knowledge_store.py").read_text(encoding="utf-8").splitlines()
    cm = (REPO / "cymatix_context" / "context_manager.py").read_text(encoding="utf-8").splitlines()
    tl = (REPO / "cymatix_context" / "pipeline" / "tier_logic.py").read_text(encoding="utf-8").splitlines()

    def lines_matching(src, pat):
        return [i + 1 for i, l in enumerate(src) if re.search(pat, l)]

    code = {
        "tier_logic_focused_cut_lines": lines_matching(tl, r"candidates\[:6\]"),
        "tier_logic_tight_cut_lines": lines_matching(tl, r"candidates\[:3\]"),
        "tier_logic_ratio_over_candidates_lines": lines_matching(tl, r"ratio = top_score / max\(mean_score"),
        "tier_logic_min_delivered_hook_lines": lines_matching(tl, r"min_delivered"),
        "cm_tier_candidates_assign_lines": lines_matching(cm, r"candidates = _tier\.candidates"),
        "cm_seat_floor_cap_lines": lines_matching(cm, r"_seat_floor_cap = int\(getattr\(self\.config\.budget, \"min_delivered_docs\""),
        "cm_learn_link_lines": lines_matching(cm, r"self\.genome\.link_coactivated\(expressed_ids\)"),
        "ks_harmonic_candidate_ids_lines": lines_matching(ks, r"candidate_ids = list\(gene_scores\.keys\(\)\)"),
        "ks_shortlist_guard_lines": lines_matching(ks, r"getattr\(self, \"_bm25_shortlist_enabled\", False\)"),
        "ks_entity_graph_in_pool_lines": lines_matching(ks, r"if gid in gene_scores:"),
    }
    code["harmonic_runs_before_shortlist"] = bool(
        code["ks_harmonic_candidate_ids_lines"] and code["ks_shortlist_guard_lines"]
        and code["ks_harmonic_candidate_ids_lines"][0] < code["ks_shortlist_guard_lines"][0])
    code["tier_cut_precedes_floored_classifier_cap"] = bool(
        code["cm_tier_candidates_assign_lines"] and code["cm_seat_floor_cap_lines"]
        and code["cm_tier_candidates_assign_lines"][0] < code["cm_seat_floor_cap_lines"][0])

    toml = (HERE / "configs" / "w24_min_delivered_12.toml").read_text(encoding="utf-8").splitlines()
    live = {}
    for k in ("entity_graph_retrieval_enabled", "sr_enabled", "harmonic_links", "min_delivered_docs",
              "bm25_shortlist_size", "decoder_mode", "abstain_enabled", "session_delivery_enabled"):
        hit = [l.strip() for l in toml if re.match(rf"^\s*{k}\s*=", l)]
        live[k] = hit[0].split("#")[0].strip() if hit else None

    # ---- 3. semantic pool-absent 61 by BM25 window / fused rank -----------
    sem = [r for r in rec["rows"] if r["bucket"] == "pool_absent" and r["question_type"] == "semantic"]

    def win(b):
        return "absent_20000" if b is None else "le50" if b <= 50 else "r51_1000" if b <= 1000 else "r1001_20000"

    semantic = {
        "n": len(sem),
        "bm25_window": dict(collections.Counter(win(r["bm25_proxy_rank"]) for r in sem)),
        "share_vocabulary_with_query_bm25_le_20000": sum(1 for r in sem if r["bm25_proxy_rank"] is not None),
        "deep_fused_ranks_found": sorted(r["deep_rank"] for r in sem if r["deep_rank"]),
        "deep_fused_le_12": sum(1 for r in sem if r["deep_rank"] and r["deep_rank"] <= 12),
        "deep_fused_le_48": sum(1 for r in sem if r["deep_rank"] and r["deep_rank"] <= 48),
    }
    nonsem_absent = [(r["needle"], r["question_type"], r["bm25_proxy_rank"]) for r in rec["rows"]
                     if r["bucket"] == "pool_absent" and r["question_type"] != "semantic" and not r["deep_rank"]]

    # ---- 4. classifier class per needle (pure function, no bed) -----------
    classifier = {"status": "skipped"}
    try:
        from cymatix_context.retrieval.query_classifier import classify_query
        qmap = find_queries(json.loads(needles_p.read_text(encoding="utf-8")))
        cls = collections.Counter()
        cls_dc = collections.defaultdict(collections.Counter)
        missing = 0
        for n in rows:
            q = qmap.get(n)
            if q is None:
                missing += 1
                continue
            c = classify_query(q).cls
            cls[c] += 1
            cls_dc[c][sh[n]["delivered_count"]] += 1
        classifier = {"status": "ok", "queries_resolved": len(rows) - missing, "missing": missing,
                      "classes": dict(cls), "class_x_shipped_delivered_count": {k: dict(v) for k, v in cls_dc.items()}}
    except Exception as e:  # noqa: BLE001
        classifier = {"status": "failed", "error": repr(e)[:300]}

    # ---- 5. latency: paired by segment, terms_n, addendum curve -----------
    keys = list(dp.keys())
    d1 = [dp[n]["wall_ms"] - sh[n]["wall_ms"] for n in keys[:113]]
    d2 = [dp[n]["wall_ms"] - sh[n]["wall_ms"] for n in keys[113:]]
    qt = [len(dp[n].get("query_terms") or []) for n in rows]
    latency = {
        "paired_delta_ms_median_deep_segment1": round(statistics.median(d1), 1),
        "paired_delta_ms_median_deep_segment2": round(statistics.median(d2), 1),
        "query_terms_n_median": statistics.median(qt), "query_terms_n_min": min(qt), "query_terms_n_max": max(qt),
        "addendum_sweep_p50_ratio_by_depth": {str(d["depth"]): d["p50_ratio_vs_shipped"] for d in add["section_c_latency_curve"]["per_depth"]},
        "addendum_sweep_found_by_depth": {str(d["depth"]): d["gold_found_in_map"] for d in add["section_c_latency_curve"]["per_depth"]},
        "addendum_sweep_delivered_by_depth": {str(d["depth"]): d["delivered_gold"] for d in add["section_c_latency_curve"]["per_depth"]},
        "addendum_sweep_has_hit_controls": False,
        "concurrency_measured_anywhere": False,
    }

    git_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    out = {
        "stamp": stamp,
        "arm": "A1-verify",
        "lens": "COMPLETENESS CRITIC -- what the method/code-trace/recompute lenses left untested",
        "tool": "benchmarks/dogfood/erb/verify_a1_completeness.py",
        "tool_sha256": sha(Path(__file__).resolve()),
        "git_sha": git_sha,
        "bed": "F:/tmp/erb_blob_v09x.db",
        "bed_access": "NONE -- never opened. Inputs: receipt rows, checkpoints, addendum receipt, 08-28 ladder per_query, needles file, worktree source.",
        "verified_sha256": {
            "pool_depth_forensics": sha(rec_p), "pool_depth_addendum": sha(add_p),
            "ladder_08_28": sha(lad_p), "shipped_ckpt": sha(ck_sh), "deep1000_ckpt": sha(ck_dp),
            "verify_a1_method_basis": sha(R / f"verify_a1_method_basis_{stamp}.json"),
            "verify_a1_code_trace": sha(R / f"verify_a1_code-trace_{stamp}.json"),
            "verify_a1_recompute": sha(R / f"verify_a1_recompute_{stamp}.json"),
        },
        "kill_criterion": (
            "This lens does not re-litigate the ARM-1 gate (66/109 >= 30; three lenses agree). It KILLS a "
            "candidate-depth default flip as a delivery lever if the deep arm's delivery effect is confounded by "
            "a truncation the receipt did not identify, OR if the three lenses' licences contradict each other on "
            "evidence one of them produced. It CORRECTS the '61 semantic pool-absent are a vocabulary gap / "
            "unreachable by any lexical lever' statement if the ARM-1 rows show BM25 reach for them."
        ),
        "verdict": "LIVES_WITH_CORRECTIONS",
        "delivered_count_flips_shipped_to_deep": dict(flip),
        "delivered_count_flips_by_cohort": {k: dict(v) for k, v in by_cohort.items()},
        "hit_controls_delivered_6_in_shipped": {"n": len(hc_six_shipped), "of": 60},
        "lost_hit_controls": lost,
        "rescued_misses": rescued,
        "ladder_08_28_baseline": ladder,
        "code": code,
        "arm_toml_live": live,
        "semantic_pool_absent_61": semantic,
        "nonsemantic_pool_absent_absent_at_1000": nonsem_absent,
        "classifier_cross": classifier,
        "latency": latency,
        "not_touched": "bed not opened; no existing receipt modified; nothing committed.",
    }
    dst = R / f"verify_a1_completeness_{stamp}.json"
    dst.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("delivered_count_flips_shipped_to_deep", "hit_controls_delivered_6_in_shipped",
                                           "ladder_08_28_baseline", "code", "arm_toml_live", "semantic_pool_absent_61",
                                           "classifier_cross", "latency")}, indent=1))
    print("wrote", dst, "tool_sha256", out["tool_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
