import sys, io
sys.stdout.reconfigure(encoding='utf-8')
p = r'F:/Projects/cymatix-context/.claude/worktrees/wave-1-semantic-ranking-5f1dab/benchmarks/dogfood/erb/probe_generator_leakage.py'
s = open(p, encoding='utf-8').read()
orig = s

def rep(a, b):
    global s
    assert a in s, "NOT FOUND: " + a[:90]
    assert s.count(a) == 1, "AMBIGUOUS: " + a[:90]
    s = s.replace(a, b)

rep(
'''  B. SIBLING non-gold chunks: other chunks of the SAME source documents as
     gold, minus the gold chunks themselves.  Topic-matched, so this isolates
     "the query was copied out of THIS chunk" from "the query is about this
     document".  Count-matched to gold_n, plus max over up to 30 siblings.''',
'''  B. SIBLING non-gold chunks: other chunks of the SAME source documents as
     gold, minus the gold chunks themselves.  *** THIS CONTROL IS VACUOUS ON
     THIS BED AND IS RETAINED ONLY AS THE DISCLOSED DEFECT. ***  ERB gold is
     document-complete -- resolve_erb_needles.py maps each upstream dsid to a
     source path and takes EVERY gene at that source_id -- so the non-gold
     sibling set is empty for all 470 needles (measured: n_sibling_pool == 0,
     470/470).  It returns a constant 0 and carries no information.
  B2. DIRECTORY-NEIGHBOUR chunks (the repair for B): chunks of OTHER documents
     sitting in the SAME parent directory as a gold document.  Same source
     type, same folder, different document -- topic-matched and non-vacuous.
     Count-matched to gold_n, plus max over up to 30 draws.''')

rep('N_SIBLING_CAP = 30\n',
    'N_SIBLING_CAP = 30\nN_DIRNB_CAP = 30\nDIR_COLLECT_CAP = 300  # max gene_ids retained per gold parent directory during the scan\n')

rep('''    "Otherwise PASS: the phrase signal on this bed is not a generator "
    "artifact at the population level."
)''',
'''    "Otherwise PASS: the phrase signal on this bed is not a generator "
    "artifact at the population level."
)

# The stated criterion is preserved verbatim above.  Arm (b)'s named control
# turned out to be structurally vacuous on this bed (see CRITERION_DEFECT and
# control_B_vacuity in the receipt), so arm (b) is ALSO evaluated against the
# repaired directory-neighbour control B2 and both readings are reported.
CRITERION_DEFECT = (
    "Arm (b) of the pre-registered criterion named control B (non-gold sibling "
    "chunks of the gold source documents). That control is EMPTY on this bed: "
    "ERB gold is document-complete (scripts/resolve_erb_needles.py resolves an "
    "upstream dsid to a source path and takes every gene at that source_id), so "
    "there are no non-gold siblings -- n_sibling_pool == 0 on 470/470 needles "
    "and control B returns a constant 0. Arm (b) therefore reads 'MET' against a "
    "vacuous baseline and is not evidence of anything. It is disclosed rather "
    "than quietly rewritten, and arm (b) is re-evaluated against a repaired "
    "topic-matched control B2 (directory-neighbour documents). The verdict is an "
    "AND over arms (a) and (b), and arm (a) is decided by a wide margin, so the "
    "defect does not change the verdict under either reading -- verified in "
    "verdict_robustness_to_criterion_defect."
)''')

rep('''    want_src = set(gold_src.values())
''',
'''    want_src = set(gold_src.values())

    def parent_dir(path: str) -> str:
        i = max(path.rfind(chr(92)), path.rfind("/"))
        return path[:i] if i > 0 else path

    want_dir = {parent_dir(x) for x in want_src}
''')

rep('''    all_ids: list[str] = []
    sib: dict[str, list[str]] = defaultdict(list)
    for gid, sid in con.execute("SELECT gene_id, source_id FROM genes"):
        all_ids.append(gid)
        if sid in want_src:
            sib[sid].append(gid)''',
'''    all_ids: list[str] = []
    sib: dict[str, list[str]] = defaultdict(list)
    dirnb: dict[str, list[str]] = defaultdict(list)
    for gid, sid in con.execute("SELECT gene_id, source_id FROM genes"):
        all_ids.append(gid)
        if sid in want_src:
            sib[sid].append(gid)
        d_ = parent_dir(sid)
        if d_ in want_dir:
            b_ = dirnb[d_]
            if len(b_) < DIR_COLLECT_CAP:
                b_.append(gid)''')

rep('    per_needle_sib: dict[str, list[str]] = {}\n',
    '    per_needle_sib: dict[str, list[str]] = {}\n    per_needle_dirnb: dict[str, list[str]] = {}\n')

rep('''        if len(pool) > N_SIBLING_CAP:
            pool = rng.sample(pool, N_SIBLING_CAP)
        per_needle_sib[name] = pool''',
'''        if len(pool) > N_SIBLING_CAP:
            pool = rng.sample(pool, N_SIBLING_CAP)
        per_needle_sib[name] = pool
        # control B2 -- other documents in the same parent directory
        dpool = []
        for gid in g:
            for s2 in dirnb.get(parent_dir(gold_src.get(gid, "")), ()):
                if s2 not in gset:
                    dpool.append(s2)
        dpool = list(dict.fromkeys(dpool))
        if len(dpool) > N_DIRNB_CAP:
            dpool = rng.sample(dpool, N_DIRNB_CAP)
        per_needle_dirnb[name] = dpool''')

rep('''    for v in per_needle_sib.values():
        need.update(v)''',
'''    for v in per_needle_sib.values():
        need.update(v)
    for v in per_needle_dirnb.values():
        need.update(v)''')

rep('''        sb = per_needle_sib[name]
        b_m_r, b_m_c, b_s = stat(sb, matched=gold_n)
        b_all_r, b_all_c, _ = stat(sb)
''',
'''        sb = per_needle_sib[name]
        b_m_r, b_m_c, b_s = stat(sb, matched=gold_n)
        b_all_r, b_all_c, _ = stat(sb)

        db = per_needle_dirnb[name]
        d_m_r, d_m_c, d_s = stat(db, matched=gold_n)
        d_all_r, d_all_c, _ = stat(db)
''')

rep('''            "ctlB_sibling_matched_content_str": b_s,''',
'''            "ctlB_sibling_matched_content_str": b_s,
            "n_dirnb_pool": len(db),
            "ctlB2_dirnb_matched_raw": d_m_r,
            "ctlB2_dirnb_matched_content": d_m_c,
            "ctlB2_dirnb_all_raw": d_all_r,
            "ctlB2_dirnb_all_content": d_all_c,
            "ctlB2_dirnb_matched_content_str": d_s,''')

rep('''            "ctlA_random_matched_raw": quart([r["ctlA_random_matched_raw"] for r in sub]),
            "ctlB_sibling_matched_raw": quart([r["ctlB_sibling_matched_raw"] for r in sub]),
        }''',
'''            "ctlA_random_matched_raw": quart([r["ctlA_random_matched_raw"] for r in sub]),
            "ctlB_sibling_matched_raw": quart([r["ctlB_sibling_matched_raw"] for r in sub]),
            "ctlB2_dirnb_matched_content": quart(
                [r["ctlB2_dirnb_matched_content"] for r in sub]
            ),
            "ctlB2_dirnb_all_content": quart([r["ctlB2_dirnb_all_content"] for r in sub]),
            "ctlB2_dirnb_matched_raw": quart([r["ctlB2_dirnb_matched_raw"] for r in sub]),
            "n_dirnb_pool": quart([r["n_dirnb_pool"] for r in sub]),
            "gold_beats_ctlB2_matched_content": sum(
                1 for r in sub if r["gold_lcn_content"] > r["ctlB2_dirnb_matched_content"]
            ),
            "gold_ties_ctlB2_matched_content": sum(
                1 for r in sub if r["gold_lcn_content"] == r["ctlB2_dirnb_matched_content"]
            ),
            "gold_loses_ctlB2_matched_content": sum(
                1 for r in sub if r["gold_lcn_content"] < r["ctlB2_dirnb_matched_content"]
            ),
        }''')

rep('''        out["gap_gold_minus_ctlB_matched_content_median"] = round(
            out["gold_content"]["median"] - out["ctlB_sibling_matched_content"]["median"], 3
        )''',
'''        out["gap_gold_minus_ctlB_matched_content_median"] = round(
            out["gold_content"]["median"] - out["ctlB_sibling_matched_content"]["median"], 3
        )
        out["gap_gold_minus_ctlB2_matched_content_median"] = round(
            out["gold_content"]["median"] - out["ctlB2_dirnb_matched_content"]["median"], 3
        )''')

rep('''    cond_b = (med_gold - med_ctlB) >= 2
    headline_fires = cond_a and cond_b''',
'''    med_ctlB2 = overall["ctlB2_dirnb_matched_content"]["median"]
    cond_b = (med_gold - med_ctlB) >= 2                 # as pre-registered (vacuous control)
    cond_b_repaired = (med_gold - med_ctlB2) >= 2       # repaired topic-matched control
    headline_fires = cond_a and cond_b
    headline_fires_repaired = cond_a and cond_b_repaired''')

rep('''    top = sorted(rows,''',
'''    verdict_robustness = {
        "arm_a_gold_content_median": med_gold,
        "arm_a_threshold": 4,
        "arm_a_met": cond_a,
        "arm_b_as_preregistered_control": "B (non-gold siblings of gold docs) -- VACUOUS, constant 0",
        "arm_b_as_preregistered_gap": round(med_gold - med_ctlB, 3),
        "arm_b_as_preregistered_met": cond_b,
        "arm_b_repaired_control": "B2 (directory-neighbour documents) -- topic-matched, non-vacuous",
        "arm_b_repaired_gap": round(med_gold - med_ctlB2, 3),
        "arm_b_repaired_met": cond_b_repaired,
        "headline_fires_as_preregistered": headline_fires,
        "headline_fires_with_repaired_arm_b": headline_fires_repaired,
        "verdict_identical_under_both_readings": headline_fires == headline_fires_repaired,
        "why": (
            "The headline criterion is an AND. Arm (a) fails by a wide margin "
            f"(gold content-LCN median {med_gold} against a threshold of 4), so "
            "neither the vacuous nor the repaired reading of arm (b) can flip the "
            "verdict. The defect is disclosed, not load-bearing."
        ),
    }

    top = sorted(rows,''')

rep('''        "kill_criterion": KILL_CRITERION,''',
'''        "kill_criterion": KILL_CRITERION,
        "criterion_defect_disclosed": CRITERION_DEFECT,
        "verdict_robustness_to_criterion_defect": verdict_robustness,
        "control_B_vacuity": {
            "n_needles_with_zero_non_gold_siblings": sum(
                1 for r in rows if r["n_sibling_pool"] == 0
            ),
            "n_needles": len(rows),
            "cause": (
                "ERB gold is document-complete. scripts/resolve_erb_needles.py maps "
                "an upstream dsid to a source path and takes EVERY gene at that "
                "source_id, so 'other chunks of the gold document' is the empty set. "
                "Spot-checked on the bed: erb_000's two gold docs have 8/8 and 2/2 "
                "chunks gold."
            ),
            "consequence": "control B is a constant 0 and is reported only as the disclosed defect; control B2 replaces it.",
        },''')

rep('''            "control_B": f"sibling chunks of the gold source documents minus gold, cap {N_SIBLING_CAP}, seed {SEED}; topic-matched",''',
'''            "control_B": f"sibling chunks of the gold source documents minus gold, cap {N_SIBLING_CAP}, seed {SEED}; VACUOUS on this bed (gold is document-complete) -- see control_B_vacuity",
            "control_B2": f"chunks of OTHER documents in the same parent directory as a gold document, cap {N_DIRNB_CAP}/needle drawn from up to {DIR_COLLECT_CAP} gene_ids retained per gold directory in rowid order, seed {SEED}; topic-matched replacement for B",''')

rep('''            f"ctlB={d['ctlB_sibling_matched_content']['median']:>4} | "''',
'''            f"ctlB2={d['ctlB2_dirnb_matched_content']['median']:>4} | "''')

rep('''    print()
    print("VERDICT:", verdict)''',
'''    print()
    print("CRITERION DEFECT: control B vacuous on "
          f"{sum(1 for r in rows if r['n_sibling_pool']==0)}/{len(rows)} needles; "
          f"arm(b) as-preregistered gap={round(med_gold-med_ctlB,3)} (met={cond_b}), "
          f"repaired via B2 gap={round(med_gold-med_ctlB2,3)} (met={cond_b_repaired}); "
          f"verdict identical under both = {headline_fires == headline_fires_repaired}")
    print()
    print("VERDICT:", verdict)''')

assert s != orig
open(p, 'w', encoding='utf-8').write(s)
print("patched ok")
