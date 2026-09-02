# METHOD ATTACKS on P4.
import json, re, sqlite3, sys, random
from collections import Counter, defaultdict
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
ERB = Path(r"F:/Projects/cymatix-context/.claude/worktrees/wave-1-semantic-ranking-5f1dab/benchmarks/dogfood/erb")
R = ERB/"receipts"
rc = json.loads((R/"generator_leakage_2026-09-01.json").read_text("utf-8"))
per = {r["needle"]: r for r in rc["per_needle"]}
mine = json.loads((R/"interloper_mine_erb947k_2026-08-31.json").read_text("utf-8"))
base = json.loads((R/"ladder_v09x_w24_min_delivered_2026-08-28.json").read_text("utf-8"))
ledger = json.loads((R/"bucket_ledger_2026-09-01.json").read_text("utf-8"))
gold = json.loads((ERB/"gold_by_needle_v09x.json").read_text("utf-8"))

# --- A1: population alignment of control C -------------------------------
base_miss = {r["needle"] for r in base["arms"][0]["per_query"] if not r["delivered_gold"]}
mine_miss = {m["needle"] for m in mine["misses"]}
print("A1 baseline misses=%d mine misses=%d  symmetric_diff=%d" % (len(base_miss), len(mine_miss), len(base_miss ^ mine_miss)))
# are any delivered ids actually gold? (control C claims all non-gold)
leak = 0; leak_n = 0
for m in mine["misses"]:
    gs = set(gold[m["needle"]])
    k = sum(1 for d in m["delivered"] if d["gene_id"] in gs)
    if k: leak += k; leak_n += 1
print("A1 delivered chunks that ARE gold: %d over %d needles (must be 0)" % (leak, leak_n))
dc = Counter(len(m["delivered"]) for m in mine["misses"])
print("A1 delivered-count distribution:", dict(dc))

# --- A2: length confound --------------------------------------------------
TOK = re.compile(r"[a-z0-9]+")
con = sqlite3.connect("file:F:/tmp/erb_blob_v09x.db?mode=ro", uri=True)
need = set()
for r in rc["per_needle"]: pass
# rebuild the exact control id sets by re-running the seeded selection is costly;
# instead sample lengths from: gold, delivered(ctlC), and uniform-random genes.
gold_ids = sorted({g for v in gold.values() for g in v})
dele_ids = sorted({d["gene_id"] for m in mine["misses"] for d in m["delivered"]})
rng = random.Random(4242)
tot = 947531
rand_rowids = sorted(rng.randrange(1, tot+1) for _ in range(3000))
def lens(ids):
    out=[]
    for i in range(0,len(ids),400):
        ch=ids[i:i+400]
        for (c,) in con.execute("SELECT content FROM genes WHERE gene_id IN (%s)"%",".join("?"*len(ch)),ch):
            out.append(len(TOK.findall((c or "").lower())))
    return out
gl = lens(gold_ids); dl = lens(dele_ids)
rl=[]
for (c,) in con.execute("SELECT content FROM genes LIMIT 3000 OFFSET 400000"): rl.append(len(TOK.findall((c or "").lower())))
def q(x,p):
    s=sorted(x); i=p*(len(s)-1); lo=int(i); hi=min(lo+1,len(s)-1); return s[lo]+(s[hi]-s[lo])*(i-lo)
for nm,x in (("gold",gl),("ctlC delivered",dl),("bed sample(3k)",rl)):
    print("A2 %-16s n=%5d  p25=%6.0f med=%6.0f p75=%6.0f mean=%7.1f" % (nm,len(x),q(x,.25),q(x,.5),q(x,.75),sum(x)/len(x)))

# --- A3: control B2 pool emptiness / cap bias -----------------------------
pools = [per[n]["n_dirnb_pool"] for n in per]
print("A3 dirnb pool: zero=%d  <5=%d  ==30=%d  median=%.0f" % (
    sum(1 for p in pools if p==0), sum(1 for p in pools if p<5), sum(1 for p in pools if p==30), q(pools,.5)))
sub=[r for r in rc["per_needle"] if r["n_dirnb_pool"]>0]
b=sum(1 for r in sub if r["gold_lcn_content"]>r["ctlB2_dirnb_matched_content"])
t=sum(1 for r in sub if r["gold_lcn_content"]==r["ctlB2_dirnb_matched_content"])
l=sum(1 for r in sub if r["gold_lcn_content"]<r["ctlB2_dirnb_matched_content"])
print("A3 gold vs B2 restricted to NON-EMPTY pools: n=%d beats=%d ties=%d loses=%d (receipt on all470: 386/82/2)"%(len(sub),b,t,l))
sub2=[r for r in rc["per_needle"] if r["n_dirnb_pool"]>=30]
b2=sum(1 for r in sub2 if r["gold_lcn_content"]>r["ctlB2_dirnb_all_content"])
t2=sum(1 for r in sub2 if r["gold_lcn_content"]==r["ctlB2_dirnb_all_content"])
l2=sum(1 for r in sub2 if r["gold_lcn_content"]<r["ctlB2_dirnb_all_content"])
print("A3 gold vs B2 ALL-30 draws, full pools only: n=%d beats=%d ties=%d loses=%d"%(len(sub2),b2,t2,l2))

# --- A4: gold_n vs matched slicing ---------------------------------------
gn=[per[n]["gold_n"] for n in per]
print("A4 gold_n: min=%d p25=%.0f med=%.0f p75=%.0f max=%d mean=%.2f"%(min(gn),q(gn,.25),q(gn,.5),q(gn,.75),max(gn),sum(gn)/len(gn)))
miss_gn=[per[m["needle"]]["gold_n"] for m in mine["misses"]]
print("A4 gold_n on the 156 misses: med=%.0f mean=%.2f  (ctlC matched slices top gold_n of 12)"%(q(miss_gn,.5),sum(miss_gn)/len(miss_gn)))

# --- A5: hit/miss + target population recompute from per_needle -----------
def stats(sel,lbl):
    s=[r for r in rc["per_needle"] if sel(r)]
    c=[r["gold_lcn_content"] for r in s]
    print("A5 %-32s n=%3d med=%.1f mean=%.3f p90=%.1f max=%d  >=4:%.1f%% >=6:%.1f%%"%(
        lbl,len(s),q(c,.5),sum(c)/len(c),q(c,.9),max(c),100*sum(1 for x in c if x>=4)/len(c),100*sum(1 for x in c if x>=6)/len(c)))
stats(lambda r:r["outcome"]=="hit","hit")
stats(lambda r:r["outcome"]=="miss","miss")
stats(lambda r:r["question_type"]=="basic" and r["bucket"]=="pool_absent","basic x pool_absent (target)")
tgt=[r for r in rc["per_needle"] if r["question_type"]=="basic" and r["bucket"]=="pool_absent"]
for key,lbl in (("ctlC_delivered_content","all12"),("ctlC_delivered_matched_content","matched")):
    b=sum(1 for r in tgt if r["gold_lcn_content"]>r[key]); t=sum(1 for r in tgt if r["gold_lcn_content"]==r[key]); l=sum(1 for r in tgt if r["gold_lcn_content"]<r[key])
    print("A5 target vs ctlC %-8s beats=%d ties=%d loses=%d (%.1f%%)"%(lbl,b,t,l,100*b/len(tgt)))
cs=[r for r in rc["per_needle"] if r["chunk_split"] is True]; ncs=[r for r in rc["per_needle"] if r["chunk_split"] is False]
print("A5 chunk_split n=%d mean=%.3f | non n=%d mean=%.3f"%(len(cs),sum(r["gold_lcn_content"] for r in cs)/len(cs),len(ncs),sum(r["gold_lcn_content"] for r in ncs)/len(ncs)))
con.close()
