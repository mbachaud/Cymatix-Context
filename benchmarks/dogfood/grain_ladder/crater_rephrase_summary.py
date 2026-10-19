"""Summarize a crater_rephrase JSONL: recovery, context and model cost."""
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

recs = [json.loads(l) for l in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if l.strip()]
out = {"n": len(recs), "errors": sum(1 for r in recs for x in r["rounds"]
                                     if (x.get("model") or {}).get("error") or (x.get("model") or {}).get("is_error"))}
groups = defaultdict(list)
for r in recs:
    kind = "never_ranked" if r["receipt_rank"] is None else "ranked_cut"
    for g in ("all", r["type"], kind, f"{r['type']}/{kind}"):
        groups[g].append(r)


def cell(rs):
    rec = [r for r in rs if r["gold_recovered"]]
    cost = sum(r["cost_usd"] for r in rs)
    answered_no_gold = [r for r in rs if (r["final"] or "").upper().startswith("ANSWER")
                        and "not found" not in (r["final"] or "").lower() and not r["gold_recovered"]]
    return {
        "n": len(rs), "round0_gold_reproduced_miss": sum(1 for r in rs if not r["round0_gold"]),
        "gold_recovered": len(rec), "recovery_rate": round(len(rec) / len(rs), 3) if rs else None,
        "first_gold_round": dict(sorted(((k, sum(1 for r in rec if r["first_gold_round"] == k))
                                         for k in {r["first_gold_round"] for r in rec}))),
        "answered_without_gold": len(answered_no_gold),
        "said_not_found": sum(1 for r in rs if "not found" in (r["final"] or "").lower()),
        "searches_mean": round(st.mean(r["searches"] for r in rs), 2),
        "packet_chars_per_search": round(st.mean(x["packet_chars"] for r in rs for x in r["rounds"]
                                                 if "packet_chars" in x)),
        "context_chars_read_per_needle": round(st.mean(r["packet_chars_total"] for r in rs)),
        "model_in_tokens_per_needle": round(st.mean(r["model_in_tokens"] for r in rs)),
        "model_out_tokens_per_needle": round(st.mean(r["model_out_tokens"] for r in rs)),
        "cost_usd_total": round(cost, 2),
        "cost_per_needle": round(cost / len(rs), 3) if rs else None,
        "cost_per_recovered": round(cost / len(rec), 2) if rec else None,
    }


out["groups"] = {g: cell(rs) for g, rs in sorted(groups.items())}
Path(sys.argv[2]).write_text(json.dumps(out, indent=2), encoding="utf-8")
print(json.dumps(out, indent=1))
