# INDEPENDENT recompute of the P4 headline: median gold content-LCN over 470 needles.
# Different LCN algorithm (difflib.SequenceMatcher.find_longest_match), different
# tokenizer route, different DB access pattern, independent median.
import json, re, sqlite3, statistics, sys
from difflib import SequenceMatcher
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

ERB = Path(r"F:/Projects/cymatix-context/.claude/worktrees/wave-1-semantic-ranking-5f1dab/benchmarks/dogfood/erb")
needles = json.loads((ERB/"needles_resolved_v09x_full.json").read_text("utf-8"))["needles"]
gold = json.loads((ERB/"gold_by_needle_v09x.json").read_text("utf-8"))
rcpt = json.loads((ERB/"receipts/generator_leakage_2026-09-01.json").read_text("utf-8"))
per = {r["needle"]: r for r in rcpt["per_needle"]}

SW = set("""a about above after again against all am an and any are as at be because been
before being below between both but by can cannot could did do does doing don down
during each few for from further had has have having he her here hers herself him
himself his how i if in into is it its itself just me more most my myself no nor not
now of off on once only or other others our ours ourselves out over own same she
should so some such than that the their theirs them themselves then there these they
this those through to too under until up us very was we were what when where which
while who whom why will with would you your yours yourself yourselves s t don t
did does was were be been being do doing get got give given make made use used
using also may might must shall need let new one two per via vs""".split())

# alternative: NLTK-style minimal english stoplist (different list, robustness check)
SW_MIN = set("""a an the and or but if of at by for with about into to from in on is are was were
be been being it its this that these those as not no do does did have has had will would can could
what when where which who whom why how""".split())

TOK = re.compile(r"[a-z0-9]+")
def toks(s): return TOK.findall(s.lower())

con = sqlite3.connect("file:F:/tmp/erb_blob_v09x.db?mode=ro", uri=True)
want = sorted({g for n in needles for g in gold[n["name"]]})
txt = {}
for i in range(0, len(want), 400):
    ch = want[i:i+400]
    for gid, c in con.execute("SELECT gene_id, content FROM genes WHERE gene_id IN (%s)" % ",".join("?"*len(ch)), ch):
        txt[gid] = c or ""
con.close()
print("gold ids requested:", len(want), "fetched:", len(txt), "missing:", len(set(want)-set(txt)))
print("gold chunks with EMPTY content:", sum(1 for v in txt.values() if not toks(v)))

def lcn_difflib(a, b):
    # longest common contiguous block, computed by a completely different algorithm
    if not a or not b: return 0
    sm = SequenceMatcher(None, a, b, autojunk=False)
    m = sm.find_longest_match(0, len(a), 0, len(b))
    return m.size

rows = []
for n in needles:
    name = n["name"]
    qr = toks(n["query"])
    qc = [t for t in qr if t not in SW]
    qm = [t for t in qr if t not in SW_MIN]
    br = bc = bm = 0
    for g in dict.fromkeys(gold[name]):
        ct = toks(txt.get(g, ""))
        br = max(br, lcn_difflib(qr, ct))
        bc = max(bc, lcn_difflib(qc, [t for t in ct if t not in SW]))
        bm = max(bm, lcn_difflib(qm, [t for t in ct if t not in SW_MIN]))
    rows.append((name, br, bc, bm))

def med(xs):
    s = sorted(xs); n = len(s); idx = 0.5*(n-1); lo = int(idx); hi = min(lo+1, n-1)
    return s[lo] + (s[hi]-s[lo])*(idx-lo)

raw = [r[1] for r in rows]; cont = [r[2] for r in rows]; minsw = [r[3] for r in rows]
print()
print("INDEPENDENT (difflib): gold RAW    median=%.1f mean=%.3f max=%d" % (med(raw), sum(raw)/len(raw), max(raw)))
print("INDEPENDENT (difflib): gold CONTENT median=%.1f mean=%.3f max=%d" % (med(cont), sum(cont)/len(cont), max(cont)))
print("INDEPENDENT alt-stoplist CONTENT   median=%.1f mean=%.3f max=%d" % (med(minsw), sum(minsw)/len(minsw), max(minsw)))
print("pct content>=4: %.1f%%  >=6: %.1f%%  raw>=8: %.1f%%" % (
    100*sum(1 for x in cont if x>=4)/len(cont), 100*sum(1 for x in cont if x>=6)/len(cont),
    100*sum(1 for x in raw if x>=8)/len(raw)))
print("pct alt-stoplist>=4: %.1f%%" % (100*sum(1 for x in minsw if x>=4)/len(minsw)))

# agreement with the script's per-needle values
dr = [(nm, br, per[nm]["gold_lcn_raw"]) for nm, br, bc, bm in rows if br != per[nm]["gold_lcn_raw"]]
dc = [(nm, bc, per[nm]["gold_lcn_content"]) for nm, br, bc, bm in rows if bc != per[nm]["gold_lcn_content"]]
print("\nper-needle disagreements vs script: raw=%d content=%d" % (len(dr), len(dc)))
for x in dr[:8]: print("  RAW", x)
for x in dc[:8]: print("  CON", x)
