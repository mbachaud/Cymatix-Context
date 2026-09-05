"""A5 attack 1: independent position map + head-chunk monopoly under score order."""
import json, sqlite3, sys, time, math
from collections import Counter, defaultdict
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

BED = r"F:/tmp/erb_blob_v09x.db"
HERE = Path(r"F:/Projects/cymatix-context/.claude/worktrees/wave-1-semantic-ranking-5f1dab/benchmarks/dogfood/erb")
OUTDIR = Path(r"C:/Users/max/AppData/Local/Temp/claude/F--Projects-cymatix-context--claude-worktrees-wave-1-semantic-ranking-5f1dab/c81df3a0-0a0c-4160-81ee-e68db3b02372/scratchpad")
mine = json.load(open(HERE/"receipts/interloper_mine_erb947k_2026-08-31.json"))
misses = mine["misses"]

con = sqlite3.connect(f"file:{BED}?mode=ro", uri=True)
con.execute("PRAGMA query_only=ON")

# INDEPENDENT ROUTE: build per-source ordered list keyed by source_id string,
# storing (gene_id, len, created_at). Not the array/index trick the probe used.
t=time.perf_counter()
docs = defaultdict(list)
info = {}
cur = con.execute("SELECT gene_id, source_id, length(content), json_extract(epigenetics,'$.created_at') FROM genes ORDER BY rowid")
for gid, sid, ln, cre in cur:
    docs[sid].append((gid, ln, cre))
print("scan", round(time.perf_counter()-t,2), "s;", len(docs), "docs", flush=True)

pos = {}          # gid -> (pos, n, len)
created_ties = 0  # multi-chunk docs where ALL created_at identical
created_varies = 0
created_strict_inc = 0
for sid, rows in docs.items():
    n = len(rows)
    cs = [r[2] for r in rows]
    if n > 1:
        if len(set(cs)) == 1: created_ties += 1
        else:
            created_varies += 1
            if all(cs[i] < cs[i+1] for i in range(n-1)): created_strict_inc += 1
    for p,(gid, ln, cre) in enumerate(rows):
        pos[gid] = (p, n, ln)

def posclass(p, n):
    if n == 1: return "only"
    if p == 0: return "first"
    if p == n-1: return "last"
    return "middle"

out = {}
out["A2_created_at_validator"] = {
    "n_multi_chunk_docs": created_ties + created_varies,
    "n_multi_all_created_at_IDENTICAL": created_ties,
    "n_multi_created_at_varies": created_varies,
    "pct_multi_with_identical_created_at": round(100.0*created_ties/(created_ties+created_varies), 3),
    "n_varying_that_strictly_increase_along_rowid": created_strict_inc,
    "reading": ("If ~all multi-chunk docs share ONE created_at, the probe's 'cre < last_created' "
                "monotonicity check can never fire and 'ASSUMPTION VALIDATED' is vacuous."),
}

# A3: independent corroboration of rowid==chunk order via the length signature
last_at_cap = 0; nonlast_at_cap = 0; n_last = 0; n_nonlast = 0
tail_is_shortest = 0; n_multi = 0
for sid, rows in docs.items():
    n = len(rows)
    if n == 1: continue
    n_multi += 1
    lens = [r[1] for r in rows]
    if lens[-1] == min(lens): tail_is_shortest += 1
    for i,L in enumerate(lens):
        if i == n-1:
            n_last += 1; last_at_cap += (L >= 4000)
        else:
            n_nonlast += 1; nonlast_at_cap += (L >= 4000)
out["A3_rowid_order_independent_corroboration"] = {
    "n_multi_chunk_docs": n_multi,
    "pct_docs_where_MAX_rowid_chunk_is_the_shortest": round(100.0*tail_is_shortest/n_multi, 3),
    "pct_nonlast_at_cap": round(100.0*nonlast_at_cap/n_nonlast, 3),
    "pct_last_at_cap": round(100.0*last_at_cap/n_last, 3),
    "reading": ("A fixed-4000 no-overlap chunker emits full chunks then a remainder. If rowid order "
                "were arbitrary within a doc the remainder would land uniformly, so pct_last_at_cap "
                "would be ~pct_nonlast_at_cap. It is not -- rowid order IS chunk order, corroborated "
                "WITHOUT created_at."),
}

# A4/A-KEY: head-chunk monopoly under SCORE order (map_top) vs ASSEMBLY order (delivered)
def cls_of(gid):
    v = pos.get(gid)
    return posclass(v[0], v[1]) if v else None

seat_cls = Counter(); map_cls = Counter()
map1_cls = Counter(); seat1_cls = Counter()
map_rank_cls = defaultdict(Counter)
n_missing = 0
seat_by_rank = defaultdict(Counter)
for m in misses:
    for i, d in enumerate(m["delivered"]):
        c = cls_of(d["gene_id"])
        if c is None: n_missing += 1; continue
        seat_cls[c]+=1; seat_by_rank[i+1][c]+=1
        if i==0: seat1_cls[c]+=1
    for i, d in enumerate(m["map_top"]):
        c = cls_of(d["gene_id"])
        if c is None: n_missing += 1; continue
        map_cls[c]+=1; map_rank_cls[i+1][c]+=1
        if i==0: map1_cls[c]+=1

out["A_KEY_score_order_vs_assembly_order"] = {
    "n_gene_ids_not_found_in_bed": n_missing,
    "assembly_order_seat1_posclass": dict(seat1_cls),
    "score_order_maprank1_posclass": dict(map1_cls),
    "all_delivered_seats_posclass": dict(seat_cls),
    "all_map_top15_posclass": dict(map_cls),
    "map_rank_1_to_15_posclass": {str(k): dict(v) for k,v in sorted(map_rank_cls.items())},
    "seat_rank_1_to_12_posclass": {str(k): dict(v) for k,v in sorted(seat_by_rank.items())},
}

# conditional null for "zero middles": condition on each winner's own parent doc depth
def cond_null(gids, label):
    ns = []
    n_atcap = 0
    p_no_middle = 1.0
    exp_mid = 0.0
    depth = Counter()
    for g in gids:
        v = pos.get(g)
        if v is None: continue
        p,n,L = v
        if L < 4000: continue          # restrict to at-cap, where first/middle are both possible
        n_atcap += 1
        depth[n]+=1
        # under "length only": uniform over the (n-1) at-cap (non-tail) chunks of this doc
        pm = (n-2)/(n-1) if n >= 2 else 0.0
        exp_mid += pm
        p_no_middle *= (1.0-pm)
    return {
        "label": label,
        "n_at_cap": n_atcap,
        "parent_depth_hist": {str(k): v for k,v in sorted(depth.items())},
        "corpus_marginal_expected_middles": round(n_atcap*0.2646, 1),
        "CONDITIONAL_expected_middles": round(exp_mid, 2),
        "p_zero_middles_under_conditional_null": p_no_middle,
        "observed_middles": sum(1 for g in gids if pos.get(g) and pos[g][2]>=4000 and posclass(*pos[g][:2])=="middle"),
    }

out["A4_conditional_null"] = {
    "assembly_seat1": cond_null([m["delivered"][0]["gene_id"] for m in misses if m["delivered"]], "delivered[0]"),
    "score_rank1": cond_null([m["map_top"][0]["gene_id"] for m in misses if m["map_top"]], "map_top[0]"),
    "all_seats": cond_null([d["gene_id"] for m in misses for d in m["delivered"]], "all 1674 seats"),
    "all_map_top15": cond_null([d["gene_id"] for m in misses for d in m["map_top"]], "all map_top15"),
    "all_gold_chunks_note": "gold population handled in attack2",
}
json.dump(out, open(OUTDIR/"attack1.json","w"), indent=1)
print(json.dumps(out, indent=1)[:6000])
