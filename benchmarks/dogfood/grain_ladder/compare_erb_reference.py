"""Compare an ERB grain-policy ladder receipt against the v0.11.3 witness.

Per arm: delivered gold, miss breakdown (summarize.miss_breakdown), budget
utilization, chars per packet; paired per-needle delivered-gold wins/losses
vs the witness (shipped config: floor 12, 7000 tokens). Pairs by needle name
within the same ordered v09x bank (both runs use the identical resolved file,
so the +1 offset hazard of mixing banks does not apply).

Usage: python -P compare_erb_reference.py --arm X.json --witness W.json --out O.json
"""
import argparse
import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from summarize import miss_breakdown  # noqa: E402


def load(p):
    d = json.loads(Path(p).read_text(encoding="utf-8"))
    return d, d["arms"][0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True)
    ap.add_argument("--witness", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = {}
    hits = {}
    for name, path in (("witness_shipped", a.witness), ("c4000_charcap", a.arm)):
        d, arm = load(path)
        pq = arm["per_query"]
        hits[name] = {r["needle"]: r.get("delivered_gold", 0) for r in pq}
        chars = [r["delivered_chars"] for r in pq if "delivered_chars" in r]
        out[name] = {
            "receipt": str(path), "git_sha": d.get("git_sha"), "config": d.get("config"), "n": len(pq),
            "delivered_gold_rate": arm["delivered_gold_rate"], "recall_at_k": arm["recall_at_k"],
            "final_recall_at_k": arm["final_recall_at_k"],
            "chars_mean": round(st.mean(chars)) if chars else None,
            "misses": miss_breakdown(pq, d["k"]) if chars else None,
        }
    w, c = hits["witness_shipped"], hits["c4000_charcap"]
    out["paired_vs_witness"] = {"wins": sum(1 for n in w if c.get(n) and not w[n]),
                                "losses": sum(1 for n in w if w[n] and not c.get(n)),
                                "missing_needles": sorted(set(w) ^ set(c))[:10]}
    Path(a.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
