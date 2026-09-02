"""A5 attack 2: is delivered order == promoter.sequence_index ascending?"""
import json, sqlite3, sys
from collections import defaultdict, Counter
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
BED = r"F:/tmp/erb_blob_v09x.db"
HERE = Path(r"F:/Projects/cymatix-context/.claude/worktrees/wave-1-semantic-ranking-5f1dab/benchmarks/dogfood/erb")
mine = json.load(open(HERE/"receipts/interloper_mine_erb947k_2026-08-31.json"))
misses = mine["misses"]

want = set()
for m in misses:
    for d in m["delivered"]: want.add(d["gene_id"])
    for d in m["map_top"]: want.add(d["gene_id"])
    if m.get("gold_first"): want.add(m["gold_first"]["gene_id"])
gold_by = json.load(open(HERE/"gold_by_needle_v09x.json"))
for m in misses:
    for g in gold_by.get(m["needle"], []): want.add(g)
print("want", len(want))

con = sqlite3.connect(f"file:{BED}?mode=ro", uri=True); con.execute("PRAGMA query_only=ON")
cols = [r[1] for r in con.execute("PRAGMA table_info(genes)")]
print("genes cols:", cols)

seq = {}
promoter_raw = {}
for gid in want:
    r = con.execute("SELECT promoter, source_id FROM genes WHERE gene_id=?", (gid,)).fetchone()
    if r is None: continue
    try: p = json.loads(r[0]) if r[0] else {}
    except Exception: p = {}
    seq[gid] = p.get("sequence_index")
    promoter_raw[gid] = (p, r[1])
sample = list(seq.items())[:5]
print("sequence_index sample:", sample)
print("n with sequence_index None:", sum(1 for v in seq.values() if v is None))
print("sequence_index value hist:", Counter(seq.values()).most_common(12))

# is delivered order == sequence_index ascending (stable)?
ok = 0; bad = 0; badex = []
for m in misses:
    s = [seq.get(d["gene_id"]) for d in m["delivered"]]
    s2 = [x if x is not None else 0 for x in s]
    if all(s2[i] <= s2[i+1] for i in range(len(s2)-1)): ok += 1
    else:
        bad += 1
        if len(badex) < 3: badex.append((m["needle"], s2))
print("delivered order is sequence_index NON-DECREASING:", ok, "/", len(misses), " violations:", bad, badex)

# does sequence_index == rowid-derived chunk position?
docs = defaultdict(list)
cur = con.execute("SELECT gene_id, source_id FROM genes ORDER BY rowid")
for gid, sid in cur: docs[sid].append(gid)
pos = {}
for sid, gs in docs.items():
    for i, g in enumerate(gs): pos[g] = (i, len(gs))
agree = dis = 0; dis_ex = []
for gid, sq in seq.items():
    if gid not in pos: continue
    if sq == pos[gid][0]: agree += 1
    else:
        dis += 1
        if len(dis_ex) < 5: dis_ex.append((gid, sq, pos[gid]))
print("sequence_index == rowid-position:", agree, "disagree:", dis, dis_ex)
