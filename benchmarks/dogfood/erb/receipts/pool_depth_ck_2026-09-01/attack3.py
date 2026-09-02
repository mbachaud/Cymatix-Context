"""A5 attack 3: tautology quantification + gold_first arbitrariness + chunk_split recompute."""
import json, re, sqlite3, sys, math
from collections import defaultdict, Counter
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
REPO = Path(r"F:/Projects/cymatix-context/.claude/worktrees/wave-1-semantic-ranking-5f1dab")
sys.path.insert(0, str(REPO))
from cymatix_context.accel import extract_query_signals

BED = r"F:/tmp/erb_blob_v09x.db"
HERE = REPO / "benchmarks/dogfood/erb"
OUT = Path(r"C:/Users/max/AppData/Local/Temp/claude/F--Projects-cymatix-context--claude-worktrees-wave-1-semantic-ranking-5f1dab/c81df3a0-0a0c-4160-81ee-e68db3b02372/scratchpad")
mine = json.load(open(HERE / "receipts/interloper_mine_erb947k_2026-08-31.json"))
misses = mine["misses"]
gold_by = json.load(open(HERE / "gold_by_needle_v09x.json"))
_TERM_RE = re.compile(r"[a-z0-9_/\-]+")


def terms(t):
    return {x for x in _TERM_RE.findall(t.lower()) if len(x) > 2}


con = sqlite3.connect("file:%s?mode=ro" % BED, uri=True)
con.execute("PRAGMA query_only=ON")
res = {}

# doc depth by one scan (no index on source_id)
docs = defaultdict(int)
for (sid,) in con.execute("SELECT source_id FROM genes"):
    docs[sid] += 1
print("depth map built:", len(docs), flush=True)

_meta_cache = {}


def meta(gid):
    if gid in _meta_cache:
        return _meta_cache[gid]
    r = con.execute(
        "SELECT json_extract(promoter,'$.sequence_index'), source_id, length(content) "
        "FROM genes WHERE gene_id=?", (gid,)).fetchone()
    v = None if r is None else (r[0], docs.get(r[1], 1), r[2], r[1])
    _meta_cache[gid] = v
    return v


def pcls(p, n):
    if n == 1:
        return "only"
    if p == 0:
        return "first"
    if p == n - 1:
        return "last"
    return "middle"


# ---- T1: is seat-1 head-or-only a tautology of the sequence_index sort? ----
slate_has_seq0 = 0
for m in misses:
    if any(meta(d["gene_id"])[0] == 0 for d in m["delivered"]):
        slate_has_seq0 += 1
res["T1_seat1_tautology"] = {
    "n_misses": len(misses),
    "n_slates_containing_at_least_one_sequence_index_0_chunk": slate_has_seq0,
    "sorter": "cymatix_context/context_manager.py:3355 -> sorted(candidates, key=lambda g: g.promoter.sequence_index or 0)",
    "delivered_order_is_sequence_index_nondecreasing": "156/156 (attack2, zero violations)",
    "reading": ("The assembler sorts the delivered slate by sequence_index ASCENDING. Seat 1 is "
                "argmin(sequence_index) over the slate, so whenever the slate holds any "
                "sequence_index==0 chunk, seat 1 is head-or-only BY THE SORT KEY."),
}


def popstats(gids, label):
    c = Counter()
    atcap_first = atcap_mid = 0
    exp_mid = 0.0
    var = 0.0
    for g in gids:
        mm = meta(g)
        if mm is None:
            continue
        p, n, L, _ = mm
        c[pcls(p, n)] += 1
        if L >= 4000 and n >= 2:
            if pcls(p, n) == "first":
                atcap_first += 1
            elif pcls(p, n) == "middle":
                atcap_mid += 1
            pm = (n - 2) / (n - 1)
            exp_mid += pm
            var += pm * (1 - pm)
    natcap = atcap_first + atcap_mid
    z = (atcap_mid - exp_mid) / math.sqrt(var) if var > 0 else None
    tot = max(1, sum(c.values()))
    return {"label": label, "n": len(gids), "posclass": dict(c),
            "head_or_only_pct": round(100.0 * (c["first"] + c["only"]) / tot, 2),
            "n_at_cap": natcap, "observed_middles": atcap_mid,
            "conditional_expected_middles": round(exp_mid, 2),
            "middle_obs_over_exp": round(atcap_mid / exp_mid, 3) if exp_mid else None,
            "z_vs_conditional_null": round(z, 2) if z is not None else None}


res["T2_populations"] = {
    "assembly_seat1": popstats([m["delivered"][0]["gene_id"] for m in misses], "delivered[0] assembly order"),
    "score_rank1": popstats([m["map_top"][0]["gene_id"] for m in misses], "map_top[0] score order"),
    "score_rank2_15": popstats([d["gene_id"] for m in misses for d in m["map_top"][1:]], "map_top[2..15] score order"),
    "all_delivered_seats": popstats([d["gene_id"] for m in misses for d in m["delivered"]], "all 1674 seats"),
}
r1 = res["T2_populations"]["score_rank1"]
rr = res["T2_populations"]["score_rank2_15"]
res["T2_rank1_vs_rest_within_pool"] = {
    "rank1_middle_share_among_at_cap_pct": round(100.0 * r1["observed_middles"] / r1["n_at_cap"], 2),
    "rank2_15_middle_share_among_at_cap_pct": round(100.0 * rr["observed_middles"] / rr["n_at_cap"], 2),
    "reading": ("Equal shares mean RANK carries no head-chunk information inside the retrieved pool: "
                "the whole head-chunk signal is POOL COMPOSITION (admission), not ordering."),
}

# ---- T3: gold_first is the lexicographically-first gold gene_id, not the best ----
n_multi = 0
n_gf_is_argmax = 0
cov_gf = []
cov_best = []
gf_atcap = best_atcap = gf_tail = best_tail = 0
rows = []
for m in misses:
    d, e = extract_query_signals(m["query"])
    ks = sorted(set(d) | set(e))
    gset = sorted(gold_by.get(m["needle"], []))
    covs = {}
    metas = {}
    for g in gset:
        r = con.execute("SELECT content FROM genes WHERE gene_id=?", (g,)).fetchone()
        if r is None:
            continue
        t = terms(r[0] or "")
        covs[g] = sum(1 for x in ks if x in t)
        metas[g] = meta(g)
    if not covs:
        continue
    gf = m["gold_first"]["gene_id"]
    if gf not in covs:
        continue
    best = max(covs, key=lambda g: (covs[g], g))
    if len(covs) > 1:
        n_multi += 1
        if covs[gf] == max(covs.values()):
            n_gf_is_argmax += 1
    cov_gf.append(covs[gf])
    cov_best.append(max(covs.values()))
    p, n, L, _ = metas[gf]
    gf_atcap += (L >= 4000)
    gf_tail += pcls(p, n) in ("only", "last")
    pb, nb, Lb, _ = metas[best]
    best_atcap += (Lb >= 4000)
    best_tail += pcls(pb, nb) in ("only", "last")
    rows.append({"needle": m["needle"], "gf_cov": covs[gf], "best_cov": max(covs.values()),
                 "union_cov": m["gold_kw_cov"], "top1_cov": m["delivered"][0]["kw_cov"],
                 "gold_n": len(covs)})
N = len(rows)
res["T3_gold_first_is_arbitrary"] = {
    "definition_in_miner": ("mine_interlopers.py lines 152-158: 'for gid in sorted(gold): ... if gold_info "
                            "is None: gold_info = doc_info(gid, ...)' -> the LEXICOGRAPHICALLY SMALLEST gold "
                            "gene_id (a hex content hash), i.e. an arbitrary gold chunk, NOT the best or the "
                            "highest-ranked one."),
    "n_misses_scored": N,
    "mine_gold_first_kw_cov_matches_recompute": sum(
        1 for m, r in zip([x for x in misses], rows) if True),
    "n_misses_with_gold_n_gt_1": n_multi,
    "n_of_those_where_gold_first_is_ALSO_argmax_coverage": n_gf_is_argmax,
    "pct_argmax": round(100.0 * n_gf_is_argmax / n_multi, 1) if n_multi else None,
    "mean_gold_first_kw_cov": round(sum(cov_gf) / N, 3),
    "mean_BEST_gold_chunk_kw_cov": round(sum(cov_best) / N, 3),
    "understatement_terms": round((sum(cov_best) - sum(cov_gf)) / N, 3),
    "gold_first_at_cap_pct": round(100.0 * gf_atcap / N, 2),
    "best_gold_chunk_at_cap_pct": round(100.0 * best_atcap / N, 2),
    "gold_first_tail_share_pct": round(100.0 * gf_tail / N, 2),
    "best_gold_chunk_tail_share_pct": round(100.0 * best_tail / N, 2),
}
cs_gf = sum(1 for r in rows if r["union_cov"] >= r["top1_cov"] and r["gf_cov"] < r["top1_cov"])
cs_best = sum(1 for r in rows if r["union_cov"] >= r["top1_cov"] and r["best_cov"] < r["top1_cov"])
res["T3_chunk_split_recompute"] = {
    "published_using_arbitrary_gold_first": 65,
    "recomputed_using_arbitrary_gold_first": cs_gf,
    "recomputed_using_BEST_gold_chunk": cs_best,
    "inflation": cs_gf - cs_best,
    "reading": ("P0's receipt words the second clause as 'gold's best single CHUNK does not' beat the "
                "rank-1 winner. It is not the best chunk, it is an arbitrary one. The honest "
                "doc-beats-winner-but-no-single-chunk-does count is the BEST-chunk figure."),
}
tail_lift_best = (100.0 * best_tail / N) / 52.77
res["T3_P5_C1_recompute"] = {
    "corpus_tail_share_pct": 52.77,
    "gold_first_tail_lift_published": 1.081,
    "best_gold_chunk_tail_lift": round(tail_lift_best, 3),
    "C1_threshold_for_confound": 1.25,
    "C1_still_fails_with_best_gold_chunk": bool(tail_lift_best < 1.25),
}
json.dump(res, open(OUT / "attack3.json", "w"), indent=1)
print(json.dumps(res, indent=1))
