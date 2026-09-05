"""A5 attack 5: symmetric gold/winner aggregations for P3, and the P0 chunk_split reachable recompute."""
import json, re, sqlite3, sys, math, random, statistics
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
REPO = Path(r"F:/Projects/cymatix-context/.claude/worktrees/wave-1-semantic-ranking-5f1dab")
sys.path.insert(0, str(REPO))
from cymatix_context.accel import extract_query_signals

BED = r"F:/tmp/erb_blob_v09x.db"
HERE = REPO / "benchmarks/dogfood/erb"
OUT = Path(r"C:/Users/max/AppData/Local/Temp/claude/F--Projects-cymatix-context--claude-worktrees-wave-1-semantic-ranking-5f1dab/c81df3a0-0a0c-4160-81ee-e68db3b02372/scratchpad")
mine = json.load(open(HERE / "receipts/interloper_mine_erb947k_2026-08-31.json"))
ledger = json.load(open(HERE / "receipts/bucket_ledger_2026-09-01.json"))
misses = mine["misses"]
gold_by = json.load(open(HERE / "gold_by_needle_v09x.json"))
_T = re.compile(r"[a-z0-9_/\-]+")
terms = lambda t: {x for x in _T.findall(t.lower()) if len(x) > 2}
AT = 3990
band = lambda n: "cap" if n >= AT else "%d" % (n // 1000)


def sign_test(w, l):
    n = w + l
    if n == 0:
        return None
    k = min(w, l)
    return min(1.0, 2.0 * sum(math.comb(n, i) for i in range(k + 1)) / 2.0 ** n)


con = sqlite3.connect("file:%s?mode=ro" % BED, uri=True)
con.execute("PRAGMA query_only=ON")
cache = {}


def doc(g):
    if g not in cache:
        r = con.execute("SELECT content FROM genes WHERE gene_id=?", (g,)).fetchone()
        c = (r[0] if r else "") or ""
        cache[g] = (terms(c), len(c))
    return cache[g]


recs = []
for m in misses:
    d, e = extract_query_signals(m["query"])
    ks = sorted(set(d) | set(e))
    golds = []
    for g in sorted(gold_by.get(m["needle"], [])):
        t, L = doc(g)
        golds.append((sum(1 for x in ks if x in t), L, g))
    wins = []
    for w in m["delivered"]:
        t, L = doc(w["gene_id"])
        wins.append((sum(1 for x in ks if x in t), L))
    if not golds or not wins:
        continue
    recs.append({"needle": m["needle"], "golds": golds, "wins": wins,
                 "gf": m["gold_first"]["gene_id"]})
res = {}


def run(gold_agg, win_agg, matcher, label):
    w = l = t = 0
    ds = []
    for r in recs:
        gs = r["golds"]
        gv = gold_agg(gs)
        if gv is None:
            continue
        gcov, glen = gv
        sel = [x for x in r["wins"] if matcher(x[1], glen)]
        if not sel:
            continue
        wv = win_agg(sel)
        ds.append(wv - gcov)
        if wv > gcov:
            w += 1
        elif wv < gcov:
            l += 1
        else:
            t += 1
    return {"label": label, "n_pairs": w + l + t, "winner_beats": w, "ties": t, "gold_beats": l,
            "mean_delta": round(sum(ds) / len(ds), 3) if ds else None,
            "p_two_sided": round(sign_test(w, l), 6) if (w + l) else None,
            "significant_at_0.05": bool((w + l) and sign_test(w, l) < 0.05)}


g_arb = lambda gs, recmap={}: None  # placeholder replaced below
same = lambda wl, gl: band(wl) == band(gl)
cap = lambda wl, gl: wl >= AT and gl >= AT


def gold_first_of(gs, gf):
    for c, L, g in gs:
        if g == gf:
            return (c, L)
    return None


# aggregation variants
def A_arbitrary(r):
    return lambda gs: gold_first_of(gs, r["gf"])


variants = {}
for name, gagg, wagg in [
    ("gold=ARBITRARY(published) / winner=MEAN", "arb", "mean"),
    ("gold=MAX / winner=MEAN", "max", "mean"),
    ("gold=MEAN / winner=MEAN  (symmetric)", "mean", "mean"),
    ("gold=MAX / winner=MAX    (symmetric)", "max", "max"),
]:
    for mkey, matcher in (("band_matched", same), ("both_at_cap", cap)):
        w = l = t = 0
        ds = []
        for r in recs:
            gs = r["golds"]
            if gagg == "arb":
                gv = gold_first_of(gs, r["gf"])
            elif gagg == "max":
                b = max(gs, key=lambda x: (x[0], x[2]))
                gv = (b[0], b[1])
            else:
                gv = (statistics.fmean(x[0] for x in gs), statistics.fmean(x[1] for x in gs))
            if gv is None:
                continue
            gcov, glen = gv
            sel = [x for x in r["wins"] if matcher(x[1], glen)]
            if not sel:
                continue
            wv = max(x[0] for x in sel) if wagg == "max" else statistics.fmean(x[0] for x in sel)
            ds.append(wv - gcov)
            if wv > gcov:
                w += 1
            elif wv < gcov:
                l += 1
            else:
                t += 1
        p = sign_test(w, l)
        variants["%s | %s" % (name, mkey)] = {
            "n_pairs": w + l + t, "winner_beats": w, "ties": t, "gold_beats": l,
            "mean_delta_kw_cov": round(sum(ds) / len(ds), 3) if ds else None,
            "p_two_sided": round(p, 6) if p is not None else None,
            "PASSES_P3_CRITERION_delta_gt_0_and_p_lt_0.05": bool(
                ds and sum(ds) / len(ds) > 0 and p is not None and p < 0.05),
        }
res["P3_symmetric_aggregation_sweep"] = variants
res["P3_criterion_verbatim"] = json.load(open(HERE / "receipts/scope_verbosity_2026-09-01.json"))["kill_criterion"]

# ---- P0: chunk_split with BEST gold chunk, split by reachable population ----
per_miss = {x["needle"]: x for x in ledger["per_miss"]}
cs_arb = set()
cs_best = set()
for r in recs:
    pm = per_miss[r["needle"]]
    top1 = pm["top1_winner_kw_cov"]
    union = pm["gold_union_kw_cov"]
    gf = gold_first_of(r["golds"], r["gf"])[0]
    best = max(x[0] for x in r["golds"])
    if union >= top1 and gf < top1:
        cs_arb.add(r["needle"])
    if union >= top1 and best < top1:
        cs_best.add(r["needle"])
sem_pool_absent = {n for n, x in per_miss.items()
                   if x["bucket"] == "pool_absent" and x["question_type"] == "semantic"}
reach = set(per_miss) - sem_pool_absent
res["P0_chunk_split_recompute"] = {
    "published_all_156": 65,
    "recomputed_arbitrary_gold_first_all_156": len(cs_arb),
    "recomputed_BEST_gold_chunk_all_156": len(cs_best),
    "published_reachable_95": 39,
    "recomputed_arbitrary_reachable": len(cs_arb & reach),
    "recomputed_BEST_reachable": len(cs_best & reach),
    "needles_needed_for_0_80": 62,
    "published_pct_of_62_reachable": 62.9,
    "corrected_pct_of_62_reachable": round(100.0 * len(cs_best & reach) / 62, 1),
    "published_pct_of_62_all156": 104.8,
    "corrected_pct_of_62_all156": round(100.0 * len(cs_best) / 62, 1),
}
json.dump(res, open(OUT / "attack5.json", "w"), indent=1)
print(json.dumps(res, indent=1))
