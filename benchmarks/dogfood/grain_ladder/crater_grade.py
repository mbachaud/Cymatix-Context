"""Grade crater_rephrase final answers against ERB gold answers + answer facts.

Grader (Sonnet via claude -p, no tools/MCP) labels each final answer:
  correct    states the gold answer's key facts, nothing contradicting
  partial    some key facts right, others missing or wrong
  incorrect  a substantive answer that is wrong or contradicts the facts
  not_found  the model declined / said it could not find it
Then crossed with the oracle gold-delivery flag:
  recovered_correct / recovered_wrong       gold reached the model
  lucky                                     correct without gold delivered
  hallucinated                              incorrect/partial without gold
  abstained                                 not_found
Items are graded in batches; the grader returns strict JSON.

Usage: python -P crater_grade.py --in X.jsonl --out graded.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ERB = ROOT / "benchmarks" / "dogfood" / "erb"
QUESTIONS = Path(r"F:\Projects\EnterpriseRAG-Bench-main\questions.jsonl")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SYSTEM = (
    "You are a strict grader. For each item you get a question, the reference gold "
    "answer, its key answer facts, and a candidate answer. Label the candidate: "
    "'correct' = states the key facts (wording may differ) and contradicts none; "
    "'partial' = some key facts right but others missing or wrong; 'incorrect' = a "
    "substantive answer that is wrong, unrelated, or contradicts the facts; "
    "'not_found' = the candidate declines or says the information was not found. "
    "Return ONLY a JSON array of objects {\"id\": ..., \"label\": ..., \"why\": "
    "<= 20 words}, one per item, same order."
)


def grade_batch(items: list, model: str) -> list:
    prompt = json.dumps(items, ensure_ascii=False, indent=1)
    cmd = ["claude", "-p", "--tools", "", "--strict-mcp-config", "--system-prompt", SYSTEM,
           "--model", model, "--output-format", "json"]
    for attempt in range(3):
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                           timeout=300, creationflags=NO_WINDOW)
        try:
            j = json.loads(p.stdout)
            txt = (j.get("result") or "").strip()
            txt = txt[txt.find("["): txt.rfind("]") + 1]
            out = json.loads(txt)
            if len(out) == len(items):
                return out, j.get("total_cost_usd")
        except (json.JSONDecodeError, ValueError):
            pass
        time.sleep(1.0)
    raise SystemExit(f"grader failed after 3 attempts: {p.stdout[:300]!r} {p.stderr[:300]!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--batch", type=int, default=20)
    a = ap.parse_args()
    recs = [json.loads(l) for l in Path(a.inp).read_text(encoding="utf-8").splitlines() if l.strip()]
    meta = json.loads((ERB / "needles_grain4000_meta.json").read_text(encoding="utf-8"))["needles"]
    qs = {}
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            q = json.loads(line)
            qs[q["question_id"]] = q
    items = []
    for r in recs:
        q = qs[meta[r["needle"]]["question_id"]]
        final = (r.get("final") or "").strip()
        if final.upper().startswith("ANSWER:"):
            final = final.split(":", 1)[1].strip()
        items.append({"id": r["needle"], "question": q["question"], "gold_answer": q["gold_answer"],
                      "answer_facts": q["answer_facts"], "candidate": final or "(no answer)"})
    labels, cost = {}, 0.0
    for i in range(0, len(items), a.batch):
        out, c = grade_batch(items[i:i + a.batch], a.model)
        cost += c or 0.0
        for o in out:
            labels[o["id"]] = o
        time.sleep(0.3)
    rows = []
    for r, it in zip(recs, items):
        lab = labels[r["needle"]]["label"]
        if lab == "not_found":
            outcome = "abstained"
        elif r["gold_recovered"]:
            outcome = "recovered_correct" if lab == "correct" else "recovered_wrong"
        else:
            outcome = "lucky" if lab == "correct" else "hallucinated"
        rows.append({"needle": r["needle"], "type": r["type"],
                     "miss_kind": "never_ranked" if r["receipt_rank"] is None else "ranked_cut",
                     "gold_recovered": r["gold_recovered"], "label": lab, "outcome": outcome,
                     "why": labels[r["needle"]].get("why"), "candidate": it["candidate"],
                     "gold_answer": it["gold_answer"]})
    summary = {
        "n": len(rows), "grader_model": a.model, "grader_cost_usd": round(cost, 3),
        "labels": dict(Counter(x["label"] for x in rows)),
        "outcomes": dict(Counter(x["outcome"] for x in rows)),
        "outcomes_by_type": {t: dict(Counter(x["outcome"] for x in rows if x["type"] == t))
                             for t in ("semantic", "basic")},
        "answers_correct_or_partial": sum(1 for x in rows if x["label"] in ("correct", "partial")),
    }
    Path(a.out).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
