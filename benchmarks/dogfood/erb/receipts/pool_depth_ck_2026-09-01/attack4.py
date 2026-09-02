"""A5 attack 4: independent recompute of P3's headline + best-gold variant + chance-level control."""
import json, re, sqlite3, sys, math, random
from collections import defaultdict
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
_T = re.compile(r"[a-z0-9_/\-]+")


def terms(t):
    return {x for x in _T.findall(t.lower()) if len(x) > 2}


AT_CAP_MIN = 3990


def band(n):
    if n >= AT_CAP_MIN:
        return "at_cap_3990+"
    return "%d-%d" % ((n // 1000) * 1000, (n // 1000) * 1000 + 1000)


def sign_test(w, l):
    n = w + l
    if n == 0:
        return None
    k = min(w, l)
    p = 0.0
    for i in range(0, k + 1):
        p += math.comb(n, i)
    p = 2.0 * p / (2.0 ** n)
    return min(1.0, p)


con = sqlite3.connect("file:%s?mode=ro" % BED, uri=True)
con.execute("PRAGMA query_only=ON")
res = {}

# ---- chance-level control pool: random AT-CAP corpus chunks ----
rng = random.Random(20260901)
maxrow = con.execute("SELECT MAX(rowid) FROM genes").fetchone()[0]
pool = []
tries = 0
while len(pool) < 3000 and tries < 12000:
    tries += 1
    r = con.execute("SELECT content FROM genes WHERE rowid=?", (rng.randrange(1, maxrow + 1),)).fetchone()
    if r and r[0] and len(r[0]) >= AT_CAP_MIN:
        pool.append(terms(r[0]))
print("chance pool at-cap chunks:", len(pool), "from", tries, "draws", flush=True)

cache = {}


def doc(gid):
    if gid not in cache:
        r = con.execute("SELECT content FROM genes WHERE gene_id=?", (gid,)).fetchone()
        c = (r[0] if r else "") or ""
        cache[gid] = (terms(c), len(c))
    return cache[gid]


recs = []
for m in misses:
    d, e = extract_query_signals(m["query"])
    ks = sorted(set(d) | set(e))
    gset = sorted(gold_by.get(m["needle"], []))
    gcov = {}
    for g in gset:
        t, L = doc(g)
        gcov[g] = (sum(1 for x in ks if x in t), L)
    gf = m["gold_first"]["gene_id"]
    if gf not in gcov:
        continue
    best = max(gcov, key=lambda g: (gcov[g][0], g))
    wins = []
    for w in m["delivered"]:
        t, L = doc(w["gene_id"])
        wins.append((sum(1 for x in ks if x in t), L))
    chance = [sum(1 for x in ks if x in tset) for tset in rng.sample(pool, 40)]
    recs.append({
        "needle": m["needle"], "n_kw": len(ks),
        "gf": gcov[gf], "best": gcov[best], "wins": wins,
        "chance_mean": sum(chance) / len(chance),
        "gold_all": list(gcov.values()),
    })
print("recs", len(recs), flush=True)

# reproduce the mine's kw_cov exactly?
mm = sum(1 for m, r in zip(misses, recs) if m["gold_first"]["kw_cov"] == r["gf"][0])
res["reproduction"] = {"n": len(recs), "gold_first_kw_cov_matches_mine": mm}


def paired(goldkey, matcher, label):
    w = l = t = 0
    ds = []
    for r in recs:
        gcov, glen = r[goldkey]
        sel = [x for x in r["wins"] if matcher(x[1], glen)]
        if not sel:
            continue
        wm = sum(x[0] for x in sel) / len(sel)
        ds.append(wm - gcov)
        if wm > gcov:
            w += 1
        elif wm < gcov:
            l += 1
        else:
            t += 1
    return {"label": label, "n_pairs": w + l + t, "winner_beats": w, "ties": t, "gold_beats": l,
            "mean_delta_kw_cov": round(sum(ds) / len(ds), 3) if ds else None,
            "sign_test_p_two_sided": round(sign_test(w, l), 8) if (w + l) else None}


same_band = lambda wl, gl: band(wl) == band(gl)
both_cap = lambda wl, gl: wl >= AT_CAP_MIN and gl >= AT_CAP_MIN
tight = lambda wl, gl: abs(wl - gl) <= 250
allm = lambda wl, gl: True

res["P3_headline_independent_recompute"] = {
    "gold_first_arbitrary": {
        "unmatched": paired("gf", allm, "unmatched"),
        "band_matched_HEADLINE": paired("gf", same_band, "band-matched (published +1.084, 59/2/31, p=0.00416511)"),
        "both_at_cap": paired("gf", both_cap, "both at cap (published +1.191, p=0.01865726)"),
        "tight_250": paired("gf", tight, "tight +/-250 (published +1.169)"),
    },
    "BEST_gold_chunk": {
        "unmatched": paired("best", allm, "unmatched"),
        "band_matched": paired("best", same_band, "band-matched"),
        "both_at_cap": paired("best", both_cap, "both at cap"),
        "tight_250": paired("best", tight, "tight +/-250"),
    },
}

# ---- chance-level control: is kw_cov discriminative at all at cap? ----
gf_cap = [r["gf"][0] for r in recs if r["gf"][1] >= AT_CAP_MIN]
best_cap = [r["best"][0] for r in recs if r["best"][1] >= AT_CAP_MIN]
win_cap = [x[0] for r in recs for x in r["wins"] if x[1] >= AT_CAP_MIN]
chance = [r["chance_mean"] for r in recs]
nkw = [r["n_kw"] for r in recs]
res["chance_level_control"] = {
    "note": ("P3 never computed a corpus kw_cov baseline (its corpus sample carries n_distinct and "
             "avg_tf only). Without one there is no scale for 'winner 10.54 vs gold 9.24'. This "
             "control scores 40 RANDOM at-cap corpus chunks against each miss's own query."),
    "mean_n_kw_per_query": round(sum(nkw) / len(nkw), 2),
    "mean_kw_cov_random_at_cap_chunk": round(sum(chance) / len(chance), 3),
    "mean_kw_cov_gold_first_at_cap": round(sum(gf_cap) / len(gf_cap), 3),
    "mean_kw_cov_BEST_gold_at_cap": round(sum(best_cap) / len(best_cap), 3),
    "mean_kw_cov_winner_seats_at_cap": round(sum(win_cap) / len(win_cap), 3),
    "winner_minus_chance": round(sum(win_cap) / len(win_cap) - sum(chance) / len(chance), 3),
    "gold_first_minus_chance": round(sum(gf_cap) / len(gf_cap) - sum(chance) / len(chance), 3),
    "published_winner_minus_gold_at_cap": 1.191,
}
json.dump(res, open(OUT / "attack4.json", "w"), indent=1)
print(json.dumps(res, indent=1))
