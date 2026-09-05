"""Independent recompute of P1 headline reach, by a DIFFERENT route.

Route used by the probe:   FTS5 'MATCH ? AND rowid = ?' membership per phrase.
Route used here:           pull the gold rows' fts-source TEXT out of the bed,
                           tokenize in pure Python, and test each candidate
                           phrase as a contiguous token subsequence.
Only DF (for the <=5000 cap) still comes from count(*) on genes_fts, and only
for phrases that already matched gold in Python.
"""
import json, os, re, sqlite3, sys, time, collections
sys.stdout.reconfigure(encoding="utf-8")

ERB = "F:/Projects/cymatix-context/.claude/worktrees/wave-1-semantic-ranking-5f1dab/benchmarks/dogfood/erb"
sys.path.insert(0, ERB)
import probe_phrase_reach as P

R = json.load(open(os.path.join(ERB, "receipts", "phrase_reach_2026-09-01.json"), encoding="utf-8"))
needles = json.load(open(P.NEEDLES_PATH, encoding="utf-8"))["needles"]
gold = json.load(open(P.GOLD_PATH, encoding="utf-8"))
query_of = {n["name"]: n["query"] for n in needles}
per = {p["needle"]: p for p in R["per_needle"]}
miss = sorted([n for n, p in per.items() if p["arm_status"] == "miss"])
sample = sorted(R["hit_control"]["sample"])
hits_all = sorted([n for n, p in per.items() if p["arm_status"] == "hit"])

con = sqlite3.connect("file:F:/tmp/erb_blob_v09x.db?mode=ro", uri=True)
TOK = re.compile(r"[^0-9a-z]+")
dfc = {}
def DF(p):
    if p not in dfc:
        dfc[p] = con.execute("SELECT count(*) FROM genes_fts WHERE genes_fts MATCH ?",
                             (P.fts_phrase(p),)).fetchone()[0]
    return dfc[p]

def toks(s):
    return [t for t in TOK.split((s or "").lower()) if t]

def sub(hay_index, hay, gram):
    # contiguous subsequence test using first-token postings
    n = len(gram)
    for i in hay_index.get(gram[0], ()):
        if hay[i:i+n] == gram:
            return True
    return False

def run(names, label):
    t0 = time.time()
    out = {}
    for j, name in enumerate(names, 1):
        gids = gold.get(name, [])
        docs_full, docs_body = [], []
        for gid in gids:
            row = con.execute(
                "SELECT rowid, content, complement FROM genes_fts_source WHERE rowid = "
                "(SELECT rowid FROM genes WHERE gene_id = ?)", (gid,)).fetchone()
            body = con.execute("SELECT content, complement FROM genes WHERE gene_id = ?", (gid,)).fetchone()
            if row:
                docs_full.append(toks((row[1] or "") + " \x00 " + (row[2] or "")))
            if body:
                docs_body.append(toks((body[0] or "") + " \x00 " + (body[1] or "")))
        idx_full = []
        for d in docs_full:
            ix = collections.defaultdict(list)
            for i, t in enumerate(d):
                ix[t].append(i)
            idx_full.append(ix)
        idx_body = []
        for d in docs_body:
            ix = collections.defaultdict(list)
            for i, t in enumerate(d):
                ix[t].append(i)
            idx_body.append(ix)
        phrases = P.ngrams_reading_b(P.tokenize(query_of[name]))
        best_full = None; best_body = None; any_full = None
        for p in phrases:
            g = p.split(" ")
            hit_full = any(sub(idx_full[i], docs_full[i], g) for i in range(len(docs_full)))
            if not hit_full:
                continue
            d = DF(p)
            if any_full is None or d < any_full:
                any_full = d
            if d <= P.DF_CAP and (best_full is None or d < best_full):
                best_full = d
            hit_body = any(sub(idx_body[i], docs_body[i], g) for i in range(len(docs_body)))
            if hit_body and d <= P.DF_CAP and (best_body is None or d < best_body):
                best_body = d
        out[name] = {"reach_elig": best_full is not None, "reach_df": best_full,
                     "reach_any": any_full is not None, "reach_body_only_elig": best_body is not None,
                     "n_gold_docs": len(docs_full)}
        if j % 25 == 0:
            sys.stderr.write("  %s %d/%d %.0fs\n" % (label, j, len(names), time.time()-t0)); sys.stderr.flush()
    return out

target = sys.argv[1]
names = {"miss": miss, "sample": sample, "hits": hits_all}[target]
res = run(names, target)
json.dump(res, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "indep_%s.json" % target), "w"), indent=0)
n = len(names)
re_ = sum(1 for v in res.values() if v["reach_elig"])
ra = sum(1 for v in res.values() if v["reach_any"])
rb = sum(1 for v in res.values() if v["reach_body_only_elig"])
print("%s n=%d indep reach_eligible=%d (%.4f)  reach_any=%d (%.4f)  body-only-eligible=%d (%.4f)"
      % (target, n, re_, re_/n, ra, ra/n, rb, rb/n))
# agreement with the probe
agree = sum(1 for k in names if res[k]["reach_elig"] == per[k]["arm_b"]["reached_eligible"])
dis = [k for k in names if res[k]["reach_elig"] != per[k]["arm_b"]["reached_eligible"]]
print("agreement with probe on reach_eligible: %d/%d ; disagreements: %s" % (agree, n, dis))
dfdis = [(k, res[k]["reach_df"], per[k]["arm_b"]["reach_df"]) for k in names
         if res[k]["reach_elig"] and per[k]["arm_b"]["reached_eligible"]
         and res[k]["reach_df"] != per[k]["arm_b"]["reach_df"]]
print("reach_df mismatches: %d  e.g. %s" % (len(dfdis), dfdis[:8]))
