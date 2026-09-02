import io, ast
p = 'F:/Projects/cymatix-context/.claude/worktrees/wave-1-semantic-ranking-5f1dab/benchmarks/dogfood/erb/probe_head_chunk_lane.py'
s = io.open(p, encoding='utf-8').read()

old = """    gold_mid_tail = 0
    for m in misses:"""
new = """    # -- S10  K3(b): is anything DOWNSTREAM of bm25 demoting middles? -------
    # The admission gate is FTS5's bm25 top-50. Within that admitted set the
    # tag/lex/RRF machinery re-orders. If the head-chunk monopoly had a
    # positional component that is NOT bm25, it must show up here as middles
    # losing rank relative to their bm25 rank.
    live_path = ERB / "receipts/head_lane_live_2026-09-01.json"
    s10 = {"status": "live receipt absent"}
    if live_path.exists():
        live = json.loads(live_path.read_text(encoding="utf-8"))
        drank = defaultdict(list)
        brank = defaultdict(list)
        frank = defaultdict(list)
        lane_val = defaultdict(lambda: defaultdict(list))
        n_q = 0
        n_leak = 0
        for r in live.get("per_query", []):
            if "error" in r:
                continue
            n_q += 1
            n_leak += r.get("n_eligible_not_in_fts50", 0)
            bm_rank = {g: i for i, g in enumerate(r["fts_top50"], 1)}
            for fr, g in enumerate(r["final_order"], 1):
                i = idx_of_gid.get(g)
                if i is None or g not in bm_rank:
                    continue
                pc = posclass(pos_arr[i], nch_arr[i])
                drank[pc].append(fr - bm_rank[g])
                brank[pc].append(bm_rank[g])
                frank[pc].append(fr)
                for lane, val in (r.get("tier_contrib_final", {}).get(g, {}) or {}).items():
                    lane_val[pc][lane].append(val)
        s10 = {
            "source_receipt": "benchmarks/dogfood/erb/receipts/head_lane_live_2026-09-01.json",
            "n_queries": n_q,
            "n_eligible_candidates_outside_fts5_top50": n_leak,
            "admission_gate_confirmed": n_leak == 0,
            "by_posclass": {
                pc: {
                    "n": len(drank[pc]),
                    "mean_bm25_rank_in_top50": mean(brank[pc]),
                    "mean_final_rank": mean(frank[pc]),
                    "mean_delta_final_minus_bm25_rank": mean(drank[pc]),
                } for pc in sorted(drank)
            },
            "mean_lane_score_by_posclass": {
                pc: {ln: mean(v) for ln, v in sorted(lanes.items())}
                for pc, lanes in sorted(lane_val.items())
            },
            "reading": (
                "delta = final rank minus bm25 rank inside the admitted top-50. A "
                "systematically POSITIVE delta for middles would be a positional demotion "
                "that bm25 does not explain; a delta near zero (or negative) means the "
                "post-admission machinery is position-neutral and everything the P5 "
                "observation saw was already decided by bm25 admission."
            ),
        }
    out["section_10_K3b_post_admission_reordering"] = s10

    gold_mid_tail = 0
    for m in misses:"""
assert old in s
s = s.replace(old, new)

old = """        "section_9_addressable_population")}, indent=1))"""
new = """        "section_9_addressable_population",
        "section_10_K3b_post_admission_reordering")}, indent=1))"""
assert old in s
s = s.replace(old, new)

io.open(p, 'w', encoding='utf-8').write(s)
ast.parse(s)
print('patch2 ok')
