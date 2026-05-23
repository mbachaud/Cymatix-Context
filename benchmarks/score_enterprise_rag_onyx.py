r"""Score EnterpriseRAG bench answers with Onyx's exact metric prompts,
but via `claude -p` OAuth (Sonnet) instead of the Anthropic SDK — so no
API key is needed.

Computes per question:
  - Onyx CORRECTNESS  (wholistic LLM judge, binary aligned yes/no)
  - Onyx COMPLETENESS (% of answer_facts the candidate supports)
  - Onyx DOCUMENT RECALL  (predicted ∩ expected / expected) — deterministic
  - Onyx INVALID EXTRA DOCS (predicted not in expected) — deterministic
PLUS our trust lens:
  - HALLUCINATION = answered (non-abstain) AND correctness=no
  - ABSTAIN       = said "I don't know" / refused
  - GROUNDED      = answered AND correctness=yes

Two lenses on the same data:
  * Onyx lens: abstain counts as not-correct (leaderboard-comparable)
  * Trust lens: abstain is safe; only confident-wrong is a failure

Prompts are verbatim copies of Onyx's ANSWER_WHOLISTIC_EVALUATION_PROMPT
and INDIVIDUAL_FACT_VALIDATOR_PROMPT (src/prompts/answer_evaluation.py)
so scoring stays comparable to their leaderboard. --no-correction
equivalent: we score against the original gold set (no 3-judge doc
rewrite).

Usage:
  python benchmarks/score_enterprise_rag_onyx.py \
      --needles benchmarks/results/enterprise_rag_helix_haiku_XXX/needles.jsonl \
      --judge-model sonnet --parallelism 4 --label 10k-d1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_enterprise_rag import load_needles   # for answer_facts by id

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CLAUDE_TIMEOUT_S = 120

# --- Onyx prompts (verbatim from src/prompts/answer_evaluation.py) ----------

WHOLISTIC = """
You are a wholistic and detail-oriented answer evaluator. Given a query, a gold answer, and a candidate answer, evaluate if the candidate answer aligned with the gold answer.
Use the following metrics for evaluating the answer:
- The candidate answer must provide loosely the same information as the gold answer. The core aspects directly asked by the query must be addressed in the candidate answer and they must not conflict with the gold answer.
- If there are any specific quantities mentioned in both answers, they must match.
- The candidate answer is not required to contain all of the same details as the gold answer.
- The candidate answer must address the key parts of the query, if it is missing anything critical to the question, it is misaligned.
- The candidate answer may contain more details, richer information, or other helpful relevant information than the gold answer, this is ok.
- The candidate answer may offer up additional loosely related information that adds to the context of the answer, this is ok as long as it does not lead the user to an incorrect conclusion (compared to the gold answer).
- Do not penalize the candidate answer for stylistic differences. If the candidate answer offers follow up questions, asks additional clarifications to the user, or offers additional context, this is ok as long as it contains the necessary information to answer the question.

There is a separate check for answer completeness, this is not in scope for this evaluation. However, if there are core parts of the question being left out, this is misaligned.

## Query
```
{query}
```

## Gold Answer
```
{gold_answer}
```

## Candidate Answer
```
{candidate_answer}
```

## Output Format
Output a JSON with "reason" and "aligned" fields. The "reason" field should be a as concise as possible (max 1 sentence) explanation of why the candidate answer is aligned or misaligned with the gold answer. The "aligned" field should be a simple "yes" or "no", use only those two strings literally and nothing else.

CRITICAL: Output only a JSON object with the following fields in the order shown below (with no additional text or formatting):
{{
  "reason": "reason for the classification",
  "aligned": "yes or no"
}}
""".strip()

FACT_VALIDATOR = """
You are an answer validator. Given an answer and a statement, determine if the answer is consistent with and contains the information in the statement. The answer may contain more details or richer information than the statement but as long as it does not contradict the statement, this is valid. If there are negative statements such as "The answer must not say...", it is valid if the answer mentions the statement with caveats or qualifications. It is valid if additional context is shared for completeness however hallucinations are not allowed. Output a simple yes or no for if the answer is consistent with and contains the information in the statement.

## Answer
```
{answer}
```

## Statement
```
{statement}
```

CRITICAL: output only a simple yes if the answer is consistent with the statement or a no if the answer does not contain the information in the statement or contradicts the statement.
""".strip()

ABSTAIN_RE = re.compile(
    r"\b(?:I (?:don'?t|do not) (?:know|have)|I'm not (?:sure|aware|certain)|"
    r"I (?:can'?t|cannot) (?:find|locate|provide|determine|answer)|"
    r"no information (?:available|provided|found)|not (?:available|enough|able to)|"
    r"insufficient (?:context|information))",
    re.IGNORECASE,
)


def claude_p(prompt: str, model: str) -> str:
    """Invoke claude -p (OAuth), no tools, clean cwd. Returns raw text."""
    clean = Path(r"F:/tmp/bench-clean-cwd"); clean.mkdir(parents=True, exist_ok=True)
    empty = clean / "_empty.json"
    if not empty.exists():
        empty.write_text('{"mcpServers":{}}', encoding="utf-8")
    sp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
    sp.write(prompt); sp.close()
    try:
        # Prompt goes in the system-prompt file (dodges the 32K arg limit);
        # the positional user message is just a nudge to respond.
        cmd = ["claude", "-p", "--model", model, "--tools", "",
               "--strict-mcp-config", "--mcp-config", str(empty),
               "--append-system-prompt-file", sp.name,
               "--output-format", "json", "--",
               "Respond now following the system prompt exactly."]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=str(clean),
                              timeout=CLAUDE_TIMEOUT_S, creationflags=NO_WINDOW)
        if proc.returncode != 0:
            return ""
        data = json.loads(proc.stdout)
        return data.get("result", "") or ""
    except Exception:
        return ""
    finally:
        try: os.unlink(sp.name)
        except Exception: pass


def judge_correctness(query: str, gold: str, cand: str, model: str) -> tuple[bool | None, str, float]:
    """Returns (aligned, reason, cost). cost from claude -p json if present."""
    prompt = WHOLISTIC.format(query=query, gold_answer=gold, candidate_answer=cand)
    raw = claude_p(prompt, model)
    if not raw:
        return (None, "judge-failed", 0.0)
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        return (None, "no-json", 0.0)
    try:
        parsed = json.loads(m.group(0))
    except Exception:
        return (None, "bad-json", 0.0)
    aligned_str = str(parsed.get("aligned", ""))
    aligned = re.search(r"\byes\b", aligned_str, re.IGNORECASE) is not None
    return (aligned, str(parsed.get("reason", "")), 0.0)


def validate_fact(answer: str, statement: str, model: str) -> bool:
    raw = claude_p(FACT_VALIDATOR.format(answer=answer, statement=statement), model)
    return re.search(r"\byes\b", raw, re.IGNORECASE) is not None


def score_one(rec: dict, facts: list[str], model: str) -> dict:
    qid = rec["id"]
    ans = (rec.get("answer") or "").strip()
    gold = rec.get("gold_answer", "")
    expected = set(rec.get("expected_doc_ids") or [])
    predicted = set(rec.get("predicted_doc_ids") or [])

    # Deterministic doc metrics
    if expected:
        recall = len(predicted & expected) / len(expected) * 100
        invalid_extra = len(predicted - expected)
    else:
        recall = None
        invalid_extra = None

    is_abstain = bool(ABSTAIN_RE.search(ans)) or not ans

    # Onyx correctness (wholistic). Abstains: skip the call, correctness=no.
    if is_abstain:
        correct, reason = False, "abstain"
    else:
        c, reason, _ = judge_correctness(rec["question"], gold, ans, model)
        correct = bool(c)

    # Onyx completeness (% facts supported). Abstains: 0 (answer lacks facts).
    if not facts:
        completeness = 100.0 if not is_abstain else 0.0
    elif is_abstain:
        completeness = 0.0
    else:
        supported = 0
        for f in facts:
            if validate_fact(ans, f, model):
                supported += 1
        completeness = supported / len(facts) * 100

    # Trust lens
    if is_abstain:
        trust = "abstain"
    elif correct:
        trust = "grounded"
    else:
        trust = "hallucination"   # confident but not aligned with gold

    return {
        "id": qid, "type": rec.get("type"),
        "abstain": is_abstain, "onyx_correct": correct,
        "completeness_pct": round(completeness, 1),
        "doc_recall_pct": round(recall, 1) if recall is not None else None,
        "invalid_extra_docs": invalid_extra,
        "trust_class": trust, "correctness_reason": reason[:160],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--needles", required=True, type=Path,
                    help="Path to a bench run's needles.jsonl")
    ap.add_argument("--judge-model", default="sonnet")
    ap.add_argument("--parallelism", type=int, default=4)
    ap.add_argument("--label", default="run")
    ap.add_argument("--limit", type=int, default=None, help="Score only first N (smoke test)")
    args = ap.parse_args()

    recs = [json.loads(l) for l in args.needles.open(encoding="utf-8")]
    if args.limit:
        recs = recs[: args.limit]
    # answer_facts aren't persisted in needles.jsonl — pull from questions.jsonl
    needles = load_needles()
    facts_by_id = {n["id"]: n["answer_facts"] for n in needles}

    print(f"scoring {len(recs)} answers with judge={args.judge_model} "
          f"parallelism={args.parallelism}")

    results = []
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.parallelism) as ex:
        futs = {ex.submit(score_one, r, facts_by_id.get(r["id"], []),
                          args.judge_model): r["id"] for r in recs}
        done = 0
        for fut in as_completed(futs):
            results.append(fut.result())
            done += 1
            if done % 20 == 0:
                print(f"  {done}/{len(recs)} ... ({time.perf_counter()-t0:.0f}s)")

    n = len(results)
    onyx_correct = sum(1 for r in results if r["onyx_correct"])
    avg_completeness = sum(r["completeness_pct"] for r in results) / n
    recalls = [r["doc_recall_pct"] for r in results if r["doc_recall_pct"] is not None]
    avg_recall = sum(recalls) / len(recalls) if recalls else 0.0
    extras = [r["invalid_extra_docs"] for r in results if r["invalid_extra_docs"] is not None]
    avg_extra = sum(extras) / len(extras) if extras else 0.0
    abstain = sum(1 for r in results if r["trust_class"] == "abstain")
    grounded = sum(1 for r in results if r["trust_class"] == "grounded")
    hallucination = sum(1 for r in results if r["trust_class"] == "hallucination")

    summary = {
        "label": args.label, "n": n, "judge_model": args.judge_model,
        "needles": str(args.needles),
        "onyx_lens": {
            "correctness_pct": round(onyx_correct / n * 100, 1),
            "completeness_pct": round(avg_completeness, 1),
            "doc_recall_pct": round(avg_recall, 1),
            "avg_invalid_extra_docs": round(avg_extra, 2),
        },
        "trust_lens": {
            "grounded": grounded, "abstain": abstain, "hallucination": hallucination,
            "hallucination_pct": round(hallucination / n * 100, 1),
        },
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "rows": results,
    }
    out = args.needles.parent / f"onyx_score_{args.label}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\n=== ONYX LENS (leaderboard-comparable) ===")
    print(f"  correctness:  {summary['onyx_lens']['correctness_pct']}%")
    print(f"  completeness: {summary['onyx_lens']['completeness_pct']}%")
    print(f"  doc recall:   {summary['onyx_lens']['doc_recall_pct']}%")
    print(f"  invalid extra docs (avg): {summary['onyx_lens']['avg_invalid_extra_docs']}")
    print("=== TRUST LENS (abstain=safe) ===")
    print(f"  grounded:      {grounded}/{n}")
    print(f"  abstain:       {abstain}/{n}")
    print(f"  hallucination: {hallucination}/{n} ({summary['trust_lens']['hallucination_pct']}%)")
    print(f"\nelapsed: {summary['elapsed_s']}s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
