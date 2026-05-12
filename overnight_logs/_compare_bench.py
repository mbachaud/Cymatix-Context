"""Bench comparison helper — load two needle JSONs, print summary deltas.

Usage:
  python _compare_bench.py baseline.json candidate.json [label_base] [label_cand]
"""
import json
import sys
from pathlib import Path


def load(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    if len(sys.argv) < 3:
        print("usage: _compare_bench.py baseline.json candidate.json [label_b] [label_c]")
        sys.exit(1)
    base_path = sys.argv[1]
    cand_path = sys.argv[2]
    label_b = sys.argv[3] if len(sys.argv) > 3 else "baseline"
    label_c = sys.argv[4] if len(sys.argv) > 4 else "candidate"

    b = load(base_path)
    c = load(cand_path)
    bs = b["summary"]
    cs = c["summary"]

    def fmt_pct(x):
        return f"{x*100:.2f}%"

    print(f"=== Bench compare: {label_b} vs {label_c} ===")
    print(f"  N: {bs['n']} / {cs['n']}")
    print(f"  Model: {b.get('model')} / {c.get('model')}")
    print(f"  Axis: {b.get('axis')} / {c.get('axis')}")
    print()
    print("Headline metrics:")
    print(f"  retrieval_rate:       {fmt_pct(bs['retrieval_rate'])}  ->  {fmt_pct(cs['retrieval_rate'])}  "
          f"(delta = {(cs['retrieval_rate']-bs['retrieval_rate'])*100:+.2f} pp)")
    print(f"  answer_accuracy_rate: {fmt_pct(bs['answer_accuracy_rate'])}  ->  {fmt_pct(cs['answer_accuracy_rate'])}  "
          f"(delta = {(cs['answer_accuracy_rate']-bs['answer_accuracy_rate'])*100:+.2f} pp)")
    print(f"  errors:               {bs['errors']}  ->  {cs['errors']}")
    print()
    bl = bs["latency"]
    cl = cs["latency"]
    print("Latency:")
    print(f"  context_p50_s:        {bl['context_p50_s']}  ->  {cl['context_p50_s']}  "
          f"(delta = {cl['context_p50_s']-bl['context_p50_s']:+.3f}s)")
    print(f"  context_p95_s:        {bl['context_p95_s']}  ->  {cl['context_p95_s']}  "
          f"(delta = {cl['context_p95_s']-bl['context_p95_s']:+.3f}s)")
    print(f"  proxy_p50_s:          {bl['proxy_p50_s']}  ->  {cl['proxy_p50_s']}")
    print(f"  proxy_p95_s:          {bl['proxy_p95_s']}  ->  {cl['proxy_p95_s']}")
    print()
    bt = bs.get("tokens", {})
    ct = cs.get("tokens", {})
    if bt and ct:
        print("Tokens:")
        print(f"  avg_injected:         {bt.get('avg_injected')}  ->  {ct.get('avg_injected')}")
        print(f"  avg_budget:           {bt.get('avg_budget')}  ->  {ct.get('avg_budget')}")
        print(f"  avg_budget_util:      {bt.get('avg_budget_utilization')}  ->  {ct.get('avg_budget_utilization')}")
        print(f"  avg_compression:      {bt.get('avg_compression_ratio')}  ->  {ct.get('avg_compression_ratio')}")
        print(f"  avg_genes_expressed:  {bt.get('avg_genes_expressed')}  ->  {ct.get('avg_genes_expressed')}")
        print(f"  total_injected:       {bt.get('total_injected')}  ->  {ct.get('total_injected')}")
        print()
    print("By category (retrieval_rate):")
    bcat = bs.get("by_category", {})
    ccat = cs.get("by_category", {})
    cats = sorted(set(bcat) | set(ccat))
    for cat in cats:
        b_r = bcat.get(cat, {}).get("retrieval_rate", 0)
        c_r = ccat.get(cat, {}).get("retrieval_rate", 0)
        n = bcat.get(cat, {}).get("n", ccat.get(cat, {}).get("n", 0))
        print(f"  {cat:<20} n={n:>4}  {fmt_pct(b_r)}  ->  {fmt_pct(c_r)}  ({(c_r-b_r)*100:+.2f} pp)")
    print()
    print("Failure modes:")
    bfm = bs.get("failure_modes", {})
    cfm = cs.get("failure_modes", {})
    keys = sorted(set(bfm) | set(cfm))
    for k in keys:
        print(f"  {k:<20} {bfm.get(k, 0):>5}  ->  {cfm.get(k, 0):>5}")
    print()

    # Verdict
    delta_pp = (cs["retrieval_rate"] - bs["retrieval_rate"]) * 100
    abs_delta = abs(delta_pp)
    p95_delta = cl["context_p95_s"] - bl["context_p95_s"]
    if abs_delta <= 2.0:
        verdict = "PASS"
    elif abs_delta <= 3.0:
        verdict = "BORDERLINE"
    else:
        verdict = "FAIL"
    print(f"Gate (|retrieval delta| <= 2.0 pp):  delta = {delta_pp:+.2f} pp  -> {verdict}")
    print(f"Context p95 delta: {p95_delta:+.3f}s (informational)")


if __name__ == "__main__":
    main()
