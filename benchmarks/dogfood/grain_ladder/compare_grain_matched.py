"""Matched-grain ERB comparison: c580 vs c4000, same packing policy (classcap).

Both beds: tagger v2, lean writes, workers 6, same 500k corpus. Needles are
joined by QUERY TEXT (each bed has its own resolved bank). Reports delivered
gold, paired wins/losses, miss breakdown, chars/packet and query p50.

Usage: python -P compare_grain_matched.py --a <c580 receipt> --a-resolved <json>
         --b <c4000 receipt> --b-resolved <json> --out <json>
"""
import argparse
import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from summarize import miss_breakdown  # noqa: E402


def by_query(receipt, resolved):
    q = {n["name"]: n["query"] for n in json.loads(Path(resolved).read_text(encoding="utf-8"))["needles"]}
    d = json.loads(Path(receipt).read_text(encoding="utf-8"))
    return d, {q[r["needle"]]: r for r in d["arms"][0]["per_query"] if r["needle"] in q}


def main():
    ap = argparse.ArgumentParser()
    for k in ("a", "a-resolved", "b", "b-resolved", "out"):
        ap.add_argument(f"--{k}", required=True)
    a = ap.parse_args()
    da, A = by_query(a.a, a.a_resolved)
    db, B = by_query(a.b, a.b_resolved)
    common = sorted(set(A) & set(B))

    def cell(d, R):
        rs = [R[q] for q in common]
        return {"receipt_git_sha": d.get("git_sha"), "config": d.get("config"), "k": d["k"],
                "scoring_epoch": (d.get("env") or {}).get("CYMATIX_SCORING_EPOCH"),
                "delivered_gold": round(sum(r.get("delivered_gold", 0) for r in rs) / len(rs), 4),
                "chars_mean": round(st.mean(r["delivered_chars"] for r in rs)),
                "wall_p50_ms": round(st.median(r["wall_ms"] for r in rs if r.get("wall_ms")), 1),
                "misses": miss_breakdown(rs, d["k"])}

    out = {"join": "query text", "n_common": len(common),
           "n_a_only": len(set(A) - set(B)), "n_b_only": len(set(B) - set(A)),
           "a": {"receipt": a.a, **cell(da, A)}, "b": {"receipt": a.b, **cell(db, B)},
           "paired_a_vs_b": {
               "wins": sum(1 for q in common if A[q].get("delivered_gold") and not B[q].get("delivered_gold")),
               "losses": sum(1 for q in common if B[q].get("delivered_gold") and not A[q].get("delivered_gold"))}}
    Path(a.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
