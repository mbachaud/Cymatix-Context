"""verify_a1_method_basis.py -- METHOD AND BASIS verification of ARM 1 (pool_depth_forensics_2026-09-01.json).
Reads ONLY the capture sqlite (mode=ro), the checkpoint jsonl, the receipt rows, the basis ladder/mine/ledger and the
needle/gold maps. Never opens the bed. Part 1 = identity checks, headline recompute, the 8 le_48 needles, term duplication.
Part 2 = boundary cases, paired latency by segment, ladder drift, control reproduction, absent_at_1000 attribution.
Run: python benchmarks/dogfood/erb/verify_a1_method_basis.py  (each part is standalone; part 2 follows the ---- PART 2 marker)
"""
# ---- PART 1
import json, sqlite3, statistics, sys, os, random
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding='utf-8')
ROOT = 'F:/Projects/cymatix-context/.claude/worktrees/wave-1-semantic-ranking-5f1dab/benchmarks/dogfood/erb'
R = json.load(open(ROOT + '/receipts/pool_depth_forensics_2026-09-01.json', encoding='utf-8'))
rows = R['rows']
by = {r['needle']: r for r in rows}
print('n rows', len(rows), Counter(r['cohort'] for r in rows), Counter(r['bucket'] for r in rows))


def le(x, n):
    return x is not None and x <= n


# ---- A. identity checks
m1 = [(r['needle'], r['bucket'], r['deep_rank'], r['bm25_proxy_rank']) for r in rows
      if (r['deep_rank'] is not None) != le(r['bm25_proxy_rank'], 1000)]
m2 = [(r['needle'], r['bucket'], r['shipped_rank'], r['bm25_proxy_rank']) for r in rows
      if (r['shipped_rank'] is not None) != le(r['bm25_proxy_rank'], 50)]
print('A1 found_at_1000 <=> bm25_proxy<=1000 mismatches:', len(m1), m1[:20])
print('A2 shipped_found <=> bm25_proxy<=50 mismatches:', len(m2), m2[:40])
print('A3 deep map sizes:', Counter(r['deep_map_size'] for r in rows).most_common(8))
print('A4 shipped map sizes:', sorted(Counter(r['shipped_map_size'] for r in rows).items()))

# ---- B. recompute headline
pa = [r for r in rows if r['bucket'] == 'pool_absent']


def hist(rs):
    h = Counter()
    for x in rs:
        h['absent' if x is None else 'le48' if x <= 48 else 'r49_200' if x <= 200 else 'r201_1000'] += 1
    return dict(h)


print('B pool_absent n', len(pa), 'found', sum(r['deep_rank'] is not None for r in pa), hist([r['deep_rank'] for r in pa]))
print('B sem', sum(1 for r in pa if r['question_type'] == 'semantic'),
      sum(1 for r in pa if r['question_type'] == 'semantic' and r['deep_rank'] is not None))
print('B bm25 attribution pool_absent', Counter(
    'absent' if r['bm25_proxy_rank'] is None else 'top50' if r['bm25_proxy_rank'] <= 50
    else '51_1000' if r['bm25_proxy_rank'] <= 1000 else '1001_20000' for r in pa))
found = [r['deep_rank'] for r in pa if r['deep_rank'] is not None]
print('B deep_rank median/max', statistics.median(found), max(found))
misses = [r for r in rows if r['cohort'] == 'miss']
hc = [r for r in rows if r['cohort'] == 'hit_control']
print('B deep delivered misses', sum(r['deep_delivered_gold'] for r in misses),
      'hit ctl kept', sum(r['deep_delivered_gold'] for r in hc),
      'lost', [(r['needle'], r['question_type'], r['shipped_rank'], r['deep_rank'], r['deep_delivered_gold_rank']) for r in hc if r['deep_delivered_gold'] == 0])
print('B hit ctl shipped delivered', sum(r['shipped_delivered_gold'] for r in hc),
      'deep found', sum(r['deep_rank'] is not None for r in hc),
      'deep rank median', statistics.median([r['deep_rank'] for r in hc]),
      'shipped rank median', statistics.median([r['shipped_rank'] for r in hc]))
print('B misses shipped delivered', sum(r['shipped_delivered_gold'] for r in misses))
print('B misses rescued by deep (needle,bucket,shipped_rank,deep_rank):',
      [(r['needle'], r['bucket'], r['shipped_rank'], r['deep_rank']) for r in misses if r['deep_delivered_gold']])
pairs = [(r['bm25_proxy_rank'], r['deep_rank']) for r in pa if r['deep_rank'] is not None]
print('B pool_absent found: (bm25,deep) sorted by bm25:', sorted(pairs)[:12], '...', sorted(pairs)[-6:])
print('B  deep<bm25', sum(1 for b, d in pairs if d < b), 'deep>bm25', sum(1 for b, d in pairs if d > b),
      'eq', sum(1 for b, d in pairs if d == b), 'median(deep-bm25)', statistics.median([d - b for b, d in pairs]))
for lab, grp in (('near_band', [r for r in rows if r['bucket'] == 'near_band']),
                 ('seat_capped', [r for r in rows if r['bucket'] == 'seat_capped']), ('hit', hc)):
    d = [r['deep_rank'] - r['shipped_rank'] for r in grp if r['deep_rank'] is not None and r['shipped_rank'] is not None]
    print('B rank shift deep-shipped', lab, 'n', len(d), 'median', statistics.median(d), 'min', min(d), 'max', max(d),
          'n_worse', sum(x > 0 for x in d), 'n_better', sum(x < 0 for x in d))

# ---- H/C. capture
con = sqlite3.connect('file:' + ROOT + '/receipts/pool_depth_capture_2026-09-01.sqlite?mode=ro', uri=True)
print('H capture per-arm:', con.execute('select arm,count(*),count(distinct needle),min(rank),max(rank) from pool group by arm').fetchall())
print('H capture map sizes deep:', con.execute("select mx,count(*) from (select needle,max(rank) mx from pool where arm='deep1000' group by needle) group by mx order by mx limit 12").fetchall())
print('H capture map sizes shipped:', con.execute("select mx,count(*) from (select needle,max(rank) mx from pool where arm='shipped' group by needle) group by mx order by mx").fetchall())
gold = json.load(open(ROOT + '/gold_by_needle_v09x.json', encoding='utf-8'))
bad = 0
for arm, needle, gid, isg in con.execute('select arm,needle,gene_id,is_gold from pool'):
    if (gid in set(gold.get(needle, []))) != bool(isg):
        bad += 1
print('H is_gold flag inconsistencies:', bad)
pa_names = [r['needle'] for r in pa]
q = ','.join('?' * len(pa_names))
print('H pool_absent gold present in SHIPPED capture pool:', con.execute(f"select count(distinct needle) from pool where arm='shipped' and is_gold=1 and needle in ({q})", pa_names).fetchone())
print('H pool_absent gold present in DEEP capture pool:', con.execute(f"select count(distinct needle) from pool where arm='deep1000' and is_gold=1 and needle in ({q})", pa_names).fetchone())
# consistency: capture deep gold rank vs receipt deep_rank
mm = 0
for r in rows:
    g = con.execute("select min(rank) from pool where arm='deep1000' and needle=? and is_gold=1", (r['needle'],)).fetchone()[0]
    if g != r['deep_rank']:
        mm += 1
print('H capture deep gold rank != receipt deep_rank:', mm)
eight = [r for r in pa if r['deep_rank'] is not None and r['deep_rank'] <= 48]
print('C the le_48 pool_absent needles:', len(eight))
for r in sorted(eight, key=lambda r: r['deep_rank']):
    n = r['needle']
    ship = [g for (g,) in con.execute("select gene_id from pool where arm='shipped' and needle=? order by rank", (n,))]
    deep = [(g, s, ig) for g, s, ig in con.execute("select gene_id,score,is_gold from pool where arm='deep1000' and needle=? order by rank", (n,))]
    deep_ids = [g for g, _, _ in deep]
    gset = set(gold.get(n, []))
    gold_in_ship = [g for g in ship if g in gset]
    gold_score = [s for g, s, _ in deep if g in gset]
    head48 = deep_ids[:48]
    overlap = len(set(head48) & set(ship))
    ship_in_deep_ranks = [deep_ids.index(g) + 1 if g in deep_ids else None for g in ship[:12]]
    s48 = deep[47][1] if len(deep) > 47 else None
    smin = con.execute("select min(score) from pool where arm='shipped' and needle=?", (n,)).fetchone()[0]
    print(f"  {n} qt={r['question_type']} shipped_map={r['shipped_map_size']} bm25_proxy={r['bm25_proxy_rank']} deep_rank={r['deep_rank']} gold_ranks={r['deep_gold_ranks']} gold_in_shipped={len(gold_in_ship)} gold_deep_scores={[round(x, 5) for x in gold_score]} deep@1={round(deep[0][1], 5)} deep@48={None if s48 is None else round(s48, 5)} shipped_min={round(smin, 5)} overlap(deep_top48,shipped_map)={overlap}/{len(ship)} shipped_top12->deep_ranks={ship_in_deep_ranks} terms_n={r['query_terms_n']}")

# ---- G. term duplicates
dups = 0
ndl = 0
for needle, t in con.execute('select needle,query_terms from terms'):
    ts = json.loads(t)
    ndl += 1
    if len(ts) != len(set(ts)):
        dups += 1
print('G terms rows', ndl, 'needles with duplicate terms', dups)
ex = con.execute("select needle,query_terms from terms where needle=?", (eight[0]['needle'],)).fetchone()
print('G example terms', ex[0], ex[1][:400])


# ---- D. paired latency from ckpt
def load_ck(p):
    seg = 0
    out = {}
    for line in open(p, encoding='utf-8'):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get('__segment__'):
            seg += 1
            continue
        rec['seg'] = seg
        out[rec['needle']] = rec
    return out


dk = load_ck(ROOT + '/receipts/pool_depth_ck_2026-09-01/cache/deep1000.jsonl')
sk = load_ck(ROOT + '/receipts/pool_depth_ck_2026-09-01/cache/shipped.jsonl')
print('D ckpt n deep', len(dk), 'shipped', len(sk), 'deep segs', Counter(v['seg'] for v in dk.values()))
pd = [(dk[n]['wall_ms'] - sk[n]['wall_ms'], dk[n]['seg']) for n in dk if n in sk]
print('D paired delta deep-shipped ms: median', round(statistics.median([d for d, _ in pd]), 1), 'mean', round(statistics.fmean([d for d, _ in pd]), 1), 'n', len(pd))
for s in (1, 2):
    ds = [d for d, g in pd if g == s]
    dw = [dk[n]['wall_ms'] for n in dk if dk[n]['seg'] == s]
    sw = [sk[n]['wall_ms'] for n in dk if dk[n]['seg'] == s and n in sk]
    print(f'D  deep seg{s}: n={len(ds)} deep p50={round(statistics.median(dw), 1)} shipped-same-needles p50={round(statistics.median(sw), 1)} paired-delta median={round(statistics.median(ds), 1)} ratio={round(statistics.median(dw) / statistics.median(sw), 3)}')
dw = [v['wall_ms'] for v in dk.values()]
sw = [v['wall_ms'] for v in sk.values()]
print('D deep p50', statistics.median(dw), 'shipped p50', statistics.median(sw), 'deep p5/p95', sorted(dw)[int(.05 * len(dw))], sorted(dw)[int(.95 * (len(dw) - 1))], 'shipped p5/p95', sorted(sw)[int(.05 * len(sw))], sorted(sw)[int(.95 * (len(sw) - 1))])
print('D deep faster than shipped on', sum(1 for d, _ in pd if d < 0), 'of', len(pd))
order = list(dk.keys())
print('D deep seg1 first 10 wall', [round(dk[n]['wall_ms']) for n in order[:10]], ' seg2 first 10', [round(dk[n]['wall_ms']) for n in order if dk[n]['seg'] == 2][:10])
print('D shipped first 10 wall', [round(v['wall_ms']) for v in list(sk.values())[:10]])
print('D delivered_count deep', Counter(v['delivered_count'] for v in dk.values()).most_common(5), 'shipped', Counter(v['delivered_count'] for v in sk.values()).most_common(5))
# wall by cohort
for lab, grp in (('miss', misses), ('hit', hc)):
    print('D wall p50 by cohort', lab, 'shipped', statistics.median([r['shipped_wall_ms'] for r in grp]), 'deep', statistics.median([r['deep_wall_ms'] for r in grp]))

# ---- E. drift vs ladder
L = json.load(open(ROOT + '/receipts/ladder_v09x_w24_min_delivered_2026-08-28.json', encoding='utf-8'))
print('E ladder top keys', list(L.keys())[:40])
pq = None
for k in ('per_query', 'rows', 'per_needle', 'queries'):
    if k in L:
        pq = L[k]
        print('E ladder per-query key', k, type(pq).__name__, len(pq))
        break
if pq is None:
    for k, v in L.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                if isinstance(v2, list) and len(v2) >= 400:
                    pq = v2
                    print('E found per-query at', k, k2, len(v2))
                    break
                if isinstance(v2, dict):
                    for k3, v3 in v2.items():
                        if isinstance(v3, list) and len(v3) >= 400:
                            pq = v3
                            print('E found per-query at', k, k2, k3, len(v3))
                            break
                if pq:
                    break
        if pq:
            break
pqd = {}
ex = {}
if isinstance(pq, dict):
    ex = next(iter(pq.values()))
    pqd = pq
elif isinstance(pq, list) and pq and isinstance(pq[0], dict):
    ex = pq[0]
    pqd = {e.get('needle') or e.get('name'): e for e in pq}
print('E ladder entry keys', list(ex.keys())[:40])
rkey = None
for cand in ('rank_of_first_gold', 'live_rank', 'gold_rank', 'rank'):
    if cand in ex:
        rkey = cand
        break
wk = [k for k in ex if 'wall' in k or k.endswith('_ms')]
dg = [k for k in ex if 'deliver' in k]
print('E rank key', rkey, 'wall keys', wk, 'delivered keys', dg)
if pqd and rkey:
    same = diff = 0
    diffs = []
    for r in rows:
        e = pqd.get(r['needle'])
        if not e:
            continue
        lr = e.get(rkey)
        if lr == r['shipped_rank']:
            same += 1
        else:
            diff += 1
            diffs.append((r['needle'], r['bucket'], lr, r['shipped_rank']))
    print('E shipped rank == ladder', rkey, ':', same, 'differ', diff, diffs[:20])
    mkey = [k for k in ex if 'map' in k or 'pool' in k]
    print('E ladder map-size keys', mkey)
    if mkey:
        print('E ladder map size dist (216 targets)', sorted(Counter(pqd[r['needle']].get(mkey[0]) for r in rows if r['needle'] in pqd).items())[:12])
if pqd and dg:
    dkey = dg[0]
    ctl_hits = sum(1 for r in hc if pqd.get(r['needle'], {}).get(dkey) in (1, True))
    miss_hits = sum(1 for r in misses if pqd.get(r['needle'], {}).get(dkey) in (1, True))
    print('E controls that are ladder hits', ctl_hits, '/', len(hc), '; misses that are ladder hits', miss_hits)
if pqd and wk:
    w = [e[wk[0]] for e in pqd.values() if isinstance(e.get(wk[0]), (int, float))]
    if w:
        print('E ladder', wk[0], 'p50 all', statistics.median(w), 'n', len(w),
              '; same 216 needles p50', statistics.median([pqd[r['needle']][wk[0]] for r in rows if r['needle'] in pqd and isinstance(pqd[r['needle']].get(wk[0]), (int, float))]))
for k, v in L.items():
    if isinstance(v, (int, float, str)) and ('wall' in k or 'p50' in k or 'ms' in k or 'lat' in k):
        print('E ladder scalar', k, v)
    if isinstance(v, dict) and ('lat' in k or 'wall' in k or 'timing' in k):
        print('E ladder dict', k, json.dumps(v)[:400])

# ---- F. control composition
N = json.load(open(ROOT + '/needles_resolved_v09x_full.json', encoding='utf-8'))['needles']
qt = {n['name']: n['question_type'] for n in N}
M = json.load(open(ROOT + '/receipts/interloper_mine_erb947k_2026-08-31.json', encoding='utf-8'))
mn = [m['needle'] for m in M['misses']]
hits = sorted(set(qt) - set(mn))
print('F needles', len(N), 'misses', len(mn), 'hits', len(hits))
print('F qtype: all hits', dict(Counter(qt[h] for h in hits)))
print('F qtype: controls', dict(Counter(r['question_type'] for r in hc)))
print('F qtype: misses', dict(Counter(r['question_type'] for r in misses)))
print('F qtype: pool_absent', dict(Counter(r['question_type'] for r in pa)))


def pick(n=60, seed=20260901):
    by_type = defaultdict(list)
    for h in hits:
        by_type[qt[h]].append(h)
    total = len(hits)
    quotas = {t: len(v) * n / total for t, v in by_type.items()}
    alloc = {t: int(qq) for t, qq in quotas.items()}
    rem = n - sum(alloc.values())
    for t in sorted(quotas, key=lambda t: (-(quotas[t] - alloc[t]), t))[:rem]:
        alloc[t] += 1
    rng = random.Random(seed)
    out = []
    for t in sorted(by_type):
        pool = sorted(by_type[t])
        out.extend(rng.sample(pool, min(alloc.get(t, 0), len(pool))))
    return sorted(out)


rep = pick()
print('F reproduce controls: equal to receipt set?', set(rep) == set(r['needle'] for r in hc), len(rep))

# ---- K. absent_at_1000
ab = [r for r in pa if r['deep_rank'] is None]
print('K absent_at_1000 n', len(ab), Counter('absent20000' if r['bm25_proxy_rank'] is None else '1001_20000' if r['bm25_proxy_rank'] > 1000 else 'le1000!' for r in ab),
      'bm25 ranks of absent (sorted, first 25)', sorted(r['bm25_proxy_rank'] for r in ab if r['bm25_proxy_rank'])[:25])
print('K bm25_proxy_pool sizes', Counter(r['bm25_proxy_pool'] for r in rows).most_common(5))
print('K multi-gold pool_absent found examples', [(r['needle'], r['deep_gold_ranks']) for r in pa if r['deep_rank'] and len(r['deep_gold_ranks']) > 1][:6])
print('R receipt deep_arm_delivery', json.dumps(R['deep_arm_delivery']))
print('R wall_s_total', R['wall_s_total'])

# ---- PART 2
import json, statistics, sys, random
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding='utf-8')
ROOT = 'F:/Projects/cymatix-context/.claude/worktrees/wave-1-semantic-ranking-5f1dab/benchmarks/dogfood/erb'
R = json.load(open(ROOT + '/receipts/pool_depth_forensics_2026-09-01.json', encoding='utf-8'))
rows = R['rows']
misses = [r for r in rows if r['cohort'] == 'miss']
hc = [r for r in rows if r['cohort'] == 'hit_control']
pa = [r for r in rows if r['bucket'] == 'pool_absent']

# boundary: bm25 49/50 cases
print('X bm25_proxy in 49..50 and shipped_rank:', [(r['needle'], r['bucket'] or 'hit', r['bm25_proxy_rank'], r['shipped_rank'], r['shipped_map_size']) for r in rows if r['bm25_proxy_rank'] in (49, 50)])
print('X bm25_proxy in 45..48 and shipped_rank:', [(r['needle'], r['bucket'] or 'hit', r['bm25_proxy_rank'], r['shipped_rank'], r['shipped_map_size']) for r in rows if r['bm25_proxy_rank'] in (45, 46, 47, 48)])
print('X near_band shipped_rank max', max(r['shipped_rank'] for r in rows if r['bucket'] == 'near_band'), 'bm25 max', max(r['bm25_proxy_rank'] for r in rows if r['bucket'] == 'near_band'))


# ---- D. paired latency from ckpt
def load_ck(p):
    seg = 0
    out = {}
    for line in open(p, encoding='utf-8'):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get('__segment__'):
            seg += 1
            continue
        rec['seg'] = seg
        out[rec['needle']] = rec
    return out


dk = load_ck(ROOT + '/receipts/pool_depth_ck_2026-09-01/cache/deep1000.jsonl')
sk = load_ck(ROOT + '/receipts/pool_depth_ck_2026-09-01/cache/shipped.jsonl')
print('D ckpt n deep', len(dk), 'shipped', len(sk), 'deep segs', Counter(v['seg'] for v in dk.values()))
pd = [(dk[n]['wall_ms'] - sk[n]['wall_ms'], dk[n]['seg']) for n in dk if n in sk]
print('D paired delta deep-shipped ms: median', round(statistics.median([d for d, _ in pd]), 1), 'mean', round(statistics.fmean([d for d, _ in pd]), 1), 'n', len(pd))
for s in (1, 2):
    ds = [d for d, g in pd if g == s]
    dw = [dk[n]['wall_ms'] for n in dk if dk[n]['seg'] == s]
    sw = [sk[n]['wall_ms'] for n in dk if dk[n]['seg'] == s and n in sk]
    print(f'D  deep seg{s}: n={len(ds)} deep p50={round(statistics.median(dw), 1)} shipped-same-needles p50={round(statistics.median(sw), 1)} paired-delta median={round(statistics.median(ds), 1)} ratio={round(statistics.median(dw) / statistics.median(sw), 3)}')
dw = [v['wall_ms'] for v in dk.values()]
sw = [v['wall_ms'] for v in sk.values()]
print('D deep p50', statistics.median(dw), 'shipped p50', statistics.median(sw), 'deep p5/p95', sorted(dw)[int(.05 * len(dw))], sorted(dw)[int(.95 * (len(dw) - 1))], 'shipped p5/p95', sorted(sw)[int(.05 * len(sw))], sorted(sw)[int(.95 * (len(sw) - 1))])
print('D deep faster than shipped on', sum(1 for d, _ in pd if d < 0), 'of', len(pd))
order = list(dk.keys())
print('D deep seg1 first 10 wall', [round(dk[n]['wall_ms']) for n in order[:10]], ' seg2 first 10', [round(dk[n]['wall_ms']) for n in order if dk[n]['seg'] == 2][:10])
print('D shipped first 10 wall', [round(v['wall_ms']) for v in list(sk.values())[:10]])
print('D delivered_count deep', Counter(v['delivered_count'] for v in dk.values()).most_common(5), 'shipped', Counter(v['delivered_count'] for v in sk.values()).most_common(5))
for lab, grp in (('miss', misses), ('hit', hc)):
    print('D wall p50 by cohort', lab, 'shipped', statistics.median([r['shipped_wall_ms'] for r in grp]), 'deep', statistics.median([r['deep_wall_ms'] for r in grp]))
# wall vs terms_n correlation (rough): bucket by terms
tb = defaultdict(list)
for r in rows:
    tb[min(r['query_terms_n'] // 10 * 10, 60)].append((r['shipped_wall_ms'], r['deep_wall_ms']))
print('D wall p50 by query_terms_n decile:', {k: (round(statistics.median([a for a, _ in v])), round(statistics.median([b for _, b in v])), len(v)) for k, v in sorted(tb.items())})

# ---- E. ladder
L = json.load(open(ROOT + '/receipts/ladder_v09x_w24_min_delivered_2026-08-28.json', encoding='utf-8'))
arms = L['arms']
print('E arms:', [a['arm'] for a in arms])
base = [a for a in arms if a['arm'] == 'baseline'][0]
print('E baseline keys:', [k for k in base.keys() if k != 'per_query'])
for k in ('wall_ms_p50', 'p50_ms', 'latency_p50_ms', 'wall_s', 'wall_ms_total', 'latency'):
    if k in base:
        print('E baseline', k, base[k])
pq = base['per_query']
print('E per_query n', len(pq), 'entry keys', list(pq[0].keys()))
pqd = {e.get('needle') or e.get('name'): e for e in pq}
rkey = None
for cand in ('rank_of_first_gold', 'rank_of_gold', 'live_rank', 'gold_rank', 'rank'):
    if cand in pq[0]:
        rkey = cand
        break
print('E rank key', rkey)
same = diff = 0
diffs = []
for r in rows:
    e = pqd.get(r['needle'])
    if not e:
        continue
    lr = e.get(rkey)
    if lr == r['shipped_rank']:
        same += 1
    else:
        diff += 1
        diffs.append((r['needle'], r['bucket'], lr, r['shipped_rank']))
print('E shipped rank == ladder', rkey, ':', same, 'differ', diff, diffs[:20])
dg = [k for k in pq[0] if 'deliver' in k or 'recall_hit' in k]
print('E delivered keys', dg)
if dg:
    dkey = 'delivered_gold' if 'delivered_gold' in pq[0] else dg[0]
    print('E using', dkey, 'controls that are ladder hits', sum(1 for r in hc if pqd.get(r['needle'], {}).get(dkey) in (1, True)), '/', len(hc),
          '; misses that are ladder hits', sum(1 for r in misses if pqd.get(r['needle'], {}).get(dkey) in (1, True)))
wk = [k for k in pq[0] if 'wall' in k or k.endswith('_ms')]
print('E wall keys', wk)
if wk:
    w = [e[wk[0]] for e in pq if isinstance(e.get(wk[0]), (int, float))]
    print('E ladder', wk[0], 'p50 all 470:', statistics.median(w), '; same 216 needles p50:', statistics.median([pqd[r['needle']][wk[0]] for r in rows if r['needle'] in pqd and isinstance(pqd[r['needle']].get(wk[0]), (int, float))]),
          '; p95 all:', sorted(w)[int(.95 * (len(w) - 1))])
    # paired: probe shipped wall vs ladder wall on same needles
    pdl = [r['shipped_wall_ms'] - pqd[r['needle']][wk[0]] for r in rows if r['needle'] in pqd and isinstance(pqd[r['needle']].get(wk[0]), (int, float))]
    print('E paired probe_shipped - ladder wall median', round(statistics.median(pdl), 1), 'ratio of p50s', round(statistics.median([r['shipped_wall_ms'] for r in rows]) / statistics.median([pqd[r['needle']][wk[0]] for r in rows if r['needle'] in pqd]), 3))
mk = [k for k in pq[0] if 'map' in k or 'pool' in k or 'n_scores' in k or 'scored' in k]
print('E map-ish keys', mk)
if mk:
    print('E ladder map size dist on 216:', sorted(Counter(pqd[r['needle']].get(mk[0]) for r in rows if r['needle'] in pqd).items())[:15])

# ---- F. controls
N = json.load(open(ROOT + '/needles_resolved_v09x_full.json', encoding='utf-8'))['needles']
qt = {n['name']: n['question_type'] for n in N}
M = json.load(open(ROOT + '/receipts/interloper_mine_erb947k_2026-08-31.json', encoding='utf-8'))
mn = [m['needle'] for m in M['misses']]
hits = sorted(set(qt) - set(mn))
print('F needles', len(N), 'misses', len(mn), 'hits', len(hits))
print('F qtype all hits', dict(sorted(Counter(qt[h] for h in hits).items())))
print('F qtype controls', dict(sorted(Counter(r['question_type'] for r in hc).items())))
print('F qtype misses', dict(sorted(Counter(r['question_type'] for r in misses).items())))
print('F qtype pool_absent', dict(sorted(Counter(r['question_type'] for r in pa).items())))


def pick(n=60, seed=20260901):
    by_type = defaultdict(list)
    for h in hits:
        by_type[qt[h]].append(h)
    total = len(hits)
    quotas = {t: len(v) * n / total for t, v in by_type.items()}
    alloc = {t: int(qq) for t, qq in quotas.items()}
    rem = n - sum(alloc.values())
    for t in sorted(quotas, key=lambda t: (-(quotas[t] - alloc[t]), t))[:rem]:
        alloc[t] += 1
    rng = random.Random(seed)
    out = []
    for t in sorted(by_type):
        pool = sorted(by_type[t])
        out.extend(rng.sample(pool, min(alloc.get(t, 0), len(pool))))
    return sorted(out)


rep = pick()
print('F reproduce controls equal to receipt set?', set(rep) == set(r['needle'] for r in hc), len(rep))
# hit-control shipped rank distribution vs all-hits (from ladder) to judge representativeness
if rkey:
    allhit_ranks = [pqd[h][rkey] for h in hits if h in pqd and pqd[h].get(rkey) is not None]
    ctl_ranks = [r['shipped_rank'] for r in hc]
    print('F all-hits ladder rank median/mean', statistics.median(allhit_ranks), round(statistics.fmean(allhit_ranks), 2), 'n>=6:', sum(1 for x in allhit_ranks if x >= 6), '/', len(allhit_ranks),
          '; controls shipped rank median/mean', statistics.median(ctl_ranks), round(statistics.fmean(ctl_ranks), 2), 'n>=6:', sum(1 for x in ctl_ranks if x >= 6), '/', len(ctl_ranks))
    sem_hits = [h for h in hits if qt[h] == 'semantic']
    print('F semantic hits n', len(sem_hits), 'controls semantic n', sum(1 for r in hc if r['question_type'] == 'semantic'),
          'semantic hit rank median', statistics.median([pqd[h][rkey] for h in sem_hits if pqd.get(h, {}).get(rkey) is not None]))
    # deep shift by control question type
    for t in sorted(set(r['question_type'] for r in hc)):
        g = [r for r in hc if r['question_type'] == t]
        print('F ctl type', t, 'n', len(g), 'median shift', statistics.median([r['deep_rank'] - r['shipped_rank'] for r in g]), 'lost', sum(1 for r in g if r['deep_delivered_gold'] == 0))

# ---- K
ab = [r for r in pa if r['deep_rank'] is None]
print('K absent_at_1000 n', len(ab), Counter('absent20000' if r['bm25_proxy_rank'] is None else '1001_20000' if r['bm25_proxy_rank'] > 1000 else 'le1000!' for r in ab),
      'bm25 ranks (sorted, first 25)', sorted(r['bm25_proxy_rank'] for r in ab if r['bm25_proxy_rank'])[:25])
print('K bm25_proxy_pool sizes', Counter(r['bm25_proxy_pool'] for r in rows).most_common(5))
print('K pool_absent semantic found by deep-rank bin', Counter(('semantic' if r['question_type'] == 'semantic' else 'other', 'absent' if r['deep_rank'] is None else 'le48' if r['deep_rank'] <= 48 else 'r49_200' if r['deep_rank'] <= 200 else 'r201_1000') for r in pa))
print('R deep_arm_delivery', json.dumps(R['deep_arm_delivery']))
print('R wall_s_total', R['wall_s_total'])
