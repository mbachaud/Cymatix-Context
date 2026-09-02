"""Tie-break / ordering sensitivity for the top-k selectivity curve, plus an
independent recompute of the reaching-DF bands. Membership is the pure-python
substring route (no FTS membership calls)."""
import json, os, re, sqlite3, sys, time, collections
sys.stdout.reconfigure(encoding="utf-8")
ERB="F:/Projects/cymatix-context/.claude/worktrees/wave-1-semantic-ranking-5f1dab/benchmarks/dogfood/erb"
sys.path.insert(0,ERB); import probe_phrase_reach as P
SP=os.path.dirname(os.path.abspath(__file__))
R=json.load(open(os.path.join(ERB,"receipts","phrase_reach_2026-09-01.json"),encoding="utf-8"))
needles=json.load(open(P.NEEDLES_PATH,encoding="utf-8"))["needles"]
gold=json.load(open(P.GOLD_PATH,encoding="utf-8"))
qof={n["name"]:n["query"] for n in needles}
per={p["needle"]:p for p in R["per_needle"]}
miss=sorted([n for n,p in per.items() if p["arm_status"]=="miss"])
sample=sorted(R["hit_control"]["sample"])
con=sqlite3.connect("file:F:/tmp/erb_blob_v09x.db?mode=ro",uri=True)
TOK=re.compile(r"[^0-9a-z]+")
dfc={}
def DF(p):
    if p not in dfc:
        dfc[p]=con.execute("SELECT count(*) FROM genes_fts WHERE genes_fts MATCH ?",(P.fts_phrase(p),)).fetchone()[0]
    return dfc[p]
def toks(s): return [t for t in TOK.split((s or "").lower()) if t]
KS=[1,2,3,5,10,20,50,100]
def analyse(names,label):
    rows={}
    for name in names:
        docs=[]
        for gid in gold.get(name,[]):
            r=con.execute("SELECT content,complement FROM genes WHERE gene_id=?",(gid,)).fetchone()
            if r: docs.append(toks((r[0] or "")+" \x00 "+(r[1] or "")))
        idxs=[]
        for d in docs:
            ix=collections.defaultdict(list)
            for i,t in enumerate(d): ix[t].append(i)
            idxs.append(ix)
        phrases=P.ngrams_reading_b(P.tokenize(qof[name]))
        reaching=[]
        for p in phrases:
            g=p.split(" "); n=len(g)
            ok=False
            for ix,d in zip(idxs,docs):
                for i in ix.get(g[0],()):
                    if d[i:i+n]==g: ok=True; break
                if ok: break
            if ok: reaching.append(p)
        # length-first ordering: longest phrase first, alphabetical tie-break
        order_len=sorted(phrases,key=lambda p:(-len(p.split(" ")),p))
        rank_len=next((i for i,p in enumerate(order_len,1) if p in set(reaching)),None)
        # DF of reaching phrases (small set) -> independent band recompute
        rdf=[DF(p) for p in reaching]
        elig=[d for d in rdf if 1<=d<=P.DF_CAP]
        rows[name]={"n_phrases":len(phrases),"n_reaching":len(reaching),
                    "rank_len":rank_len,"min_reach_df":min(elig) if elig else None,
                    "min_reach_df_anycap":min(rdf) if rdf else None}
    n=len(names)
    print("--- %s n=%d ---"%(label,n))
    for k in KS:
        c=sum(1 for v in rows.values() if v["rank_len"] and v["rank_len"]<=k)
        print("  length-first k=%-4d %3d  %.4f"%(k,c,c/n))
    bands=collections.OrderedDict((b,0) for b in ["<=12","13-50","51-200","201-1000","1001-5000","no_reach"])
    for v in rows.values():
        d=v["min_reach_df"]
        if d is None: bands["no_reach"]+=1
        elif d<=12: bands["<=12"]+=1
        elif d<=50: bands["13-50"]+=1
        elif d<=200: bands["51-200"]+=1
        elif d<=1000: bands["201-1000"]+=1
        else: bands["1001-5000"]+=1
    print("  reaching-DF bands:",dict(bands))
    nr=[v["n_reaching"] for v in rows.values()]
    print("  n_reaching phrases per query: median %.1f mean %.2f"%(sorted(nr)[n//2],sum(nr)/n))
    return rows
mr=analyse(miss,"MISSES")
hr=analyse(sample,"MATCHED HIT SAMPLE")
print()
print("delta (miss - hit) under length-first ordering:")
for k in KS:
    a=sum(1 for v in mr.values() if v["rank_len"] and v["rank_len"]<=k)/len(mr)
    b=sum(1 for v in hr.values() if v["rank_len"] and v["rank_len"]<=k)/len(hr)
    print("  k=%-4d miss %.4f  hit %.4f  delta %+.4f"%(k,a,b,a-b))
