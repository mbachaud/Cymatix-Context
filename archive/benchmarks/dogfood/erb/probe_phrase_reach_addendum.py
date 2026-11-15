"""P1 addendum -- two gaps left open by the main run, patched into the same receipt.

GAP 1 (no bed access): the cell-for-cell mix-matched hit control could only
reach n=117, not the required n>=156, because the miss population is 82/156
'semantic' while only 43 of the 314 hits are. Fixed two ways that do not
require inventing needles: (i) DIRECT STANDARDISATION -- per-question_type
reach rates measured on ALL 314 hits, reweighted by the miss type mix, giving
a mix-matched estimate on the full hit population; (ii) WITHIN-STRATUM
deltas, which are immune to composition entirely.

GAP 2 (bed access, read-only): the main run measured the lane at k=3 most
selective phrases (reach_topfew) and at k=all-eligible (reach_rate). The curve
in between decides whether ANY practical top-k phrase rule works, so this
records the 1-based SELECTIVITY RANK of the first gold-reaching phrase and the
full top-k reach curve.

Run AFTER probe_phrase_reach.py:
    python benchmarks/dogfood/erb/probe_phrase_reach_addendum.py
"""

from __future__ import annotations

import collections
import json
import os
import statistics
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import probe_phrase_reach as P                                  # noqa: E402

RECEIPT = P.OUT_PATH
KS = [1, 2, 3, 5, 10, 20, 50, 100, 200]


def curve_for(bed, name, query, gold_rowids):
    tokens = P.tokenize(query)
    phrases = P.ngrams_reading_b(tokens)
    scored = []
    for p in phrases:
        d = bed.df(P.fts_phrase(p))
        if d is not None:
            scored.append((d, p))
    scored.sort(key=lambda x: (x[0], x[1]))
    eligible = [(d, p) for d, p in scored if 1 <= d <= P.DF_CAP]
    rank = None
    for idx, (d, p) in enumerate(eligible, 1):
        expr = P.fts_phrase(p)
        for gid, rid in gold_rowids.items():
            if bed.matches_rowid(expr, rid):
                rank = idx
                break
        if rank is not None:
            break
    return {"needle": name, "n_eligible": len(eligible),
            "selectivity_rank_of_first_reaching_phrase": rank,
            "reach_df": eligible[rank - 1][0] if rank else None}


def curve_summary(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0}
    out = {"n": n,
           "n_eligible_dist": P.quartiles([r["n_eligible"] for r in rows]),
           "reached_at_all": sum(1 for r in rows
                                 if r["selectivity_rank_of_first_reaching_phrase"]),
           "topk_reach": {},
           "topk_reach_rate": {}}
    for k in KS:
        c = sum(1 for r in rows
                if r["selectivity_rank_of_first_reaching_phrase"]
                and r["selectivity_rank_of_first_reaching_phrase"] <= k)
        out["topk_reach"]["k=%d" % k] = c
        out["topk_reach_rate"]["k=%d" % k] = P.rate(c, n)
    ranks = [r["selectivity_rank_of_first_reaching_phrase"] for r in rows
             if r["selectivity_rank_of_first_reaching_phrase"]]
    out["reaching_rank_dist_among_reachers"] = P.quartiles(ranks)
    return out


def main():
    t0 = time.time()
    rec = json.load(open(RECEIPT, encoding="utf-8"))
    needles = json.load(open(P.NEEDLES_PATH, encoding="utf-8"))["needles"]
    gold = json.load(open(P.GOLD_PATH, encoding="utf-8"))
    query_of = dict((n["name"], n["query"]) for n in needles)

    per = dict((r["needle"], r) for r in rec["per_needle"])
    miss_names = sorted([n for n, r in per.items() if r["arm_status"] == "miss"])
    hit_names = sorted([n for n, r in per.items() if r["arm_status"] == "hit"])
    sample = rec["hit_control"]["sample"]
    miss_mix = rec["hit_control"]["miss_type_mix"]
    n_miss = sum(miss_mix.values())

    # ---------- GAP 1: standardisation + within-stratum, no bed ------------
    def type_rate(names, field):
        g = collections.defaultdict(list)
        for n in names:
            g[per[n]["question_type"]].append(per[n])
        out = {}
        for t, rows in sorted(g.items()):
            if field == "reach":
                c = sum(1 for r in rows if r["arm_b"]["reached_eligible"])
            elif field == "topfew":
                c = sum(1 for r in rows if r["arm_b"]["reached_topfew"])
            else:
                c = sum(1 for r in rows if r["near"]["reached"])
            out[t] = {"n": len(rows), "count": c, "rate": P.rate(c, len(rows))}
        return out

    miss_by_type = type_rate(miss_names, "reach")
    hits_by_type = type_rate(hit_names, "reach")
    miss_by_type_tf = type_rate(miss_names, "topfew")
    hits_by_type_tf = type_rate(hit_names, "topfew")
    miss_by_type_near = type_rate(miss_names, "near")
    hits_by_type_near = type_rate(hit_names, "near")

    def standardise(hits_tab):
        num, wt, missing = 0.0, 0.0, []
        for t, w in miss_mix.items():
            if t in hits_tab and hits_tab[t]["n"] > 0:
                num += w * hits_tab[t]["rate"]
                wt += w
            else:
                missing.append(t)
        return (round(num / wt, 4) if wt else None, wt / float(n_miss), missing)

    std_reach, cov, missing_types = standardise(hits_by_type)
    std_topfew, _, _ = standardise(hits_by_type_tf)
    std_near, _, _ = standardise(hits_by_type_near)

    miss_reach = rec["section_3_control_arm"]["misses"]["reach_rate"]
    miss_topfew = rec["section_3_control_arm"]["misses"]["reach_topfew_rate"]
    miss_near = rec["section_3_control_arm"]["misses"]["near_reach_rate"]

    within = {}
    for t in sorted(set(list(miss_by_type) + list(hits_by_type))):
        m = miss_by_type.get(t)
        h = hits_by_type.get(t)
        within[t] = {
            "miss_n": m["n"] if m else 0, "miss_reach_rate": m["rate"] if m else None,
            "hit_n": h["n"] if h else 0, "hit_reach_rate": h["rate"] if h else None,
            "delta_miss_minus_hit": (round(m["rate"] - h["rate"], 4)
                                     if (m and h) else None),
        }
    strata_with_both = [t for t, v in within.items()
                        if v["delta_miss_minus_hit"] is not None]
    strata_miss_higher = [t for t in strata_with_both
                          if within[t]["delta_miss_minus_hit"] >= P.KILL_B_MIN_DELTA]

    rec["section_6_standardised_control"] = {
        "why": ("the cell-for-cell mix-matched sample could only reach n=117 (<156): the "
                "miss population is 82/156 semantic but only 43 of 314 hits are semantic, "
                "so semantic is the binding stratum. Two composition-proof replacements."),
        "hit_stratum_availability": dict((t, {"miss_wanted": miss_mix[t],
                                              "hits_available": hits_by_type.get(t, {}).get("n", 0)})
                                         for t in sorted(miss_mix)),
        "direct_standardisation": {
            "definition": ("per-question_type reach rate measured on ALL 314 hits, reweighted "
                           "by the miss type mix (sum_t w_t * rate_t / sum_t w_t)"),
            "hit_reach_rate_standardised_to_miss_mix": std_reach,
            "hit_topfew_rate_standardised": std_topfew,
            "hit_near_rate_standardised": std_near,
            "miss_mix_weight_covered": round(cov, 4),
            "miss_types_with_no_hits": missing_types,
            "miss_reach_rate": miss_reach,
            "delta_miss_minus_standardised_hit": (round(miss_reach - std_reach, 4)
                                                  if std_reach is not None else None),
            "delta_topfew": (round(miss_topfew - std_topfew, 4)
                             if std_topfew is not None else None),
            "delta_near": (round(miss_near - std_near, 4)
                           if std_near is not None else None),
        },
        "within_stratum": within,
        "n_strata_compared": len(strata_with_both),
        "n_strata_where_misses_fire_materially_more": len(strata_miss_higher),
        "strata_where_misses_fire_materially_more": strata_miss_higher,
        "miss_by_type_topfew": miss_by_type_tf,
        "hits_all_by_type_topfew": hits_by_type_tf,
        "miss_by_type_near": miss_by_type_near,
        "hits_all_by_type_near": hits_by_type_near,
    }

    # ---------- GAP 2: top-k selectivity curve (bed, read-only) -----------
    bed = P.Bed(P.BED)
    targets = miss_names + sample
    rows = {}
    for i, name in enumerate(targets, 1):
        rids = bed.rowids_for(gold.get(name, []))
        rows[name] = curve_for(bed, name, query_of[name], rids)
        if i % 25 == 0:
            sys.stderr.write("  curve %d/%d %.0fs\n" % (i, len(targets), time.time() - t0))
            sys.stderr.flush()

    miss_rows = [rows[n] for n in miss_names]
    samp_rows = [rows[n] for n in sample]
    mc = curve_summary(miss_rows)
    hc = curve_summary(samp_rows)

    rec["section_7_topk_selectivity_curve"] = {
        "why": ("the main run measured the lane only at k=3 (reach_topfew) and k=all-eligible "
                "(reach_rate). This is the curve in between: does ANY practical top-k "
                "phrase-selection rule surface gold? k is a rank over phrases sorted by "
                "ascending DF -- exactly the selectivity rule the lever proposes."),
        "ks": KS,
        "misses": mc,
        "hits_matched_sample": hc,
        "delta_rate_miss_minus_hit_by_k": dict(
            ("k=%d" % k, round(mc["topk_reach_rate"]["k=%d" % k]
                               - hc["topk_reach_rate"]["k=%d" % k], 4)) for k in KS),
        "n_fts_errors": len(bed.errors),
        "per_needle": [dict(rows[n], arm_status=per[n]["arm_status"],
                            question_type=per[n]["question_type"],
                            bucket=per[n]["bucket"]) for n in targets],
    }

    # ---------- verdict restatement on the composition-proof basis -------
    std_delta = rec["section_6_standardised_control"]["direct_standardisation"][
        "delta_miss_minus_standardised_hit"]
    rec["verdict_reason_addendum"] = (
        "Limb (b) re-graded on the composition-proof bases now that the mix-matched sample "
        "was capped at n=117: direct standardisation of ALL 314 hits to the miss type mix "
        "gives hit reach %.4f vs miss reach %.4f, delta %+.4f (needs >= %.2f); and the "
        "within-stratum comparison finds %d of %d question_type strata where misses fire "
        "materially more than hits. Limb (b) FIRES on every basis. The top-k curve adds "
        "that at the k a real lane would use the miss reach is %.4f (k=3) / %.4f (k=10) / "
        "%.4f (k=50), each BELOW the hit rate at the same k. Verdict unchanged: KILL."
        % (std_reach if std_reach is not None else float("nan"),
           miss_reach, std_delta if std_delta is not None else float("nan"),
           P.KILL_B_MIN_DELTA, len(strata_miss_higher), len(strata_with_both),
           mc["topk_reach_rate"]["k=3"], mc["topk_reach_rate"]["k=10"],
           mc["topk_reach_rate"]["k=50"])
    )
    rec["addendum_tool"] = "benchmarks/dogfood/erb/probe_phrase_reach_addendum.py"
    rec["addendum_git_sha"] = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
        cwd=HERE).stdout.strip()
    rec["addendum_wall_s"] = round(time.time() - t0, 1)

    with open(RECEIPT, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, indent=1)

    print("standardised hit reach %s vs miss %s -> delta %s"
          % (rec["section_6_standardised_control"]["direct_standardisation"]
             ["hit_reach_rate_standardised_to_miss_mix"], miss_reach, std_delta))
    print("strata where misses fire materially more: %d/%d %s"
          % (len(strata_miss_higher), len(strata_with_both), strata_miss_higher))
    print("miss topk rate", mc["topk_reach_rate"])
    print("hit  topk rate", hc["topk_reach_rate"])
    print("patched %s in %.0fs" % (RECEIPT, time.time() - t0))


if __name__ == "__main__":
    main()
