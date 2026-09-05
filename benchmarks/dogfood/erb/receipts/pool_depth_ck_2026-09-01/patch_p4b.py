import sys
sys.stdout.reconfigure(encoding='utf-8')
p = r'F:/Projects/cymatix-context/.claude/worktrees/wave-1-semantic-ranking-5f1dab/benchmarks/dogfood/erb/probe_generator_leakage.py'
s = open(p, encoding='utf-8').read()
orig = s

def rep(a, b):
    global s
    assert a in s, "NOT FOUND: " + a[:90]
    assert s.count(a) == 1, "AMBIGUOUS: " + a[:90]
    s = s.replace(a, b)

# per-needle: add count-matched and top-1 variants of control C
rep('''        if name in delivered_ids:
            c_r, c_c, c_s = stat(delivered_ids[name])
            row["ctlC_delivered_raw"] = c_r
            row["ctlC_delivered_content"] = c_c
            row["ctlC_delivered_content_str"] = c_s''',
'''        if name in delivered_ids:
            dv = delivered_ids[name]
            c_r, c_c, c_s = stat(dv)
            row["ctlC_delivered_raw"] = c_r
            row["ctlC_delivered_content"] = c_c
            row["ctlC_delivered_content_str"] = c_s
            # fairness: max over 12 delivered vs max over ~4 gold biases C up.
            # matched = the top gold_n delivered (the STRONGEST interlopers, so
            # this is if anything still generous to the control).
            m_r, m_c, _ = stat(dv, matched=gold_n)
            row["ctlC_delivered_matched_raw"] = m_r
            row["ctlC_delivered_matched_content"] = m_c
            t_r, t_c, _ = stat(dv, matched=1)
            row["ctlC_delivered_top1_raw"] = t_r
            row["ctlC_delivered_top1_content"] = t_c''')

# rollup: report all three variants
rep('''        cs = [r for r in sub if "ctlC_delivered_content" in r]
        if cs:
            out["ctlC_delivered_content"] = quart([r["ctlC_delivered_content"] for r in cs])
            out["ctlC_delivered_raw"] = quart([r["ctlC_delivered_raw"] for r in cs])
            out["gold_beats_ctlC_content"] = sum(
                1 for r in cs if r["gold_lcn_content"] > r["ctlC_delivered_content"]
            )
            out["gold_ties_ctlC_content"] = sum(
                1 for r in cs if r["gold_lcn_content"] == r["ctlC_delivered_content"]
            )
            out["gold_loses_ctlC_content"] = sum(
                1 for r in cs if r["gold_lcn_content"] < r["ctlC_delivered_content"]
            )
            out["n_ctlC"] = len(cs)''',
'''        cs = [r for r in sub if "ctlC_delivered_content" in r]
        if cs:
            out["n_ctlC"] = len(cs)
            for key, lbl in (
                ("ctlC_delivered_content", "all12"),
                ("ctlC_delivered_matched_content", "matched"),
                ("ctlC_delivered_top1_content", "top1"),
            ):
                out[key] = quart([r[key] for r in cs])
                out[f"gold_vs_ctlC_{lbl}_content"] = {
                    "gold_beats": sum(1 for r in cs if r["gold_lcn_content"] > r[key]),
                    "ties": sum(1 for r in cs if r["gold_lcn_content"] == r[key]),
                    "gold_loses": sum(1 for r in cs if r["gold_lcn_content"] < r[key]),
                    "gold_beats_pct": round(
                        100.0
                        * sum(1 for r in cs if r["gold_lcn_content"] > r[key])
                        / len(cs),
                        1,
                    ),
                }
            out["ctlC_delivered_raw"] = quart([r["ctlC_delivered_raw"] for r in cs])
            # back-compat aliases for the all-12 reading
            out["gold_beats_ctlC_content"] = out["gold_vs_ctlC_all12_content"]["gold_beats"]
            out["gold_ties_ctlC_content"] = out["gold_vs_ctlC_all12_content"]["ties"]
            out["gold_loses_ctlC_content"] = out["gold_vs_ctlC_all12_content"]["gold_loses"]''')

rep('''            "control_C": "the 12 chunks the shipped pipeline actually delivered, for the 156 baseline misses only (all non-gold since delivered_gold==0). Max over the 12.",''' if '"control_C": "the 12 chunks the shipped pipeline actually delivered, for the 156 baseline misses only (all non-gold since delivered_gold==0). Max over the 12.",' in s else '''            "control_C": "the 12 chunks the shipped pipeline actually delivered, for the 156 baseline misses only (all non-gold since delivered_gold==0)",''',
'''            "control_C": "the 12 chunks the shipped pipeline actually delivered, for the 156 baseline misses only (all non-gold since delivered_gold==0). Reported three ways: max over all 12; count-matched to gold_n using the TOP gold_n delivered (strongest interlopers); and the rank-1 delivered chunk alone. Only misses have delivered ids in the interloper mine, so control C does not exist for the 314 hits -- disclosed, not imputed.",''')

assert s != orig
open(p, 'w', encoding='utf-8').write(s)
print("patched ok")
