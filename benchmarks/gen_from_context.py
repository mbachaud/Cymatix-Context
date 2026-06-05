r"""Replay a captured context.jsonl against ONE gen model -- no helix, no
re-retrieval. Pairs with capture_context.py. Output is grader-compatible
(needles.jsonl + onyx_answers.jsonl) so score_enterprise_rag_onyx.py judges it
unchanged.

Models:
  haiku | sonnet | opus      -> claude -p (OAuth, reuses bench_enterprise_rag.run_claude)
  gpt-5.4 | gpt-5.5 | gpt-*  -> OpenAI API (needs $env:OPENAI_API_KEY); --model is the
                               literal OpenAI model id.

Usage:
  python benchmarks/gen_from_context.py --context benchmarks/results/ctx_fixed_sem125.jsonl \
      --model opus --parallelism 3
  python benchmarks/gen_from_context.py --context ... --model gpt-5.5 \
      --price-in 1.25 --price-out 10   # $/Mtok, optional, to fill cost_usd for OpenAI
Then grade:
  python benchmarks/score_enterprise_rag_onyx.py --needles <run_dir>/needles.jsonl \
      --judge-model opus --parallelism 3 --label <label>
"""
from __future__ import annotations
import argparse, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx  # noqa: E402
from bench_enterprise_rag import (  # noqa: E402
    run_claude, SYSTEM_PROMPT_INJECTED, SYSTEM_PROMPT_COLD, RESULTS_ROOT,
)

CLAUDE_ALIASES = {"haiku", "sonnet", "opus"}


def run_openai(prompt: str, ctx_text: str, model_id: str,
               price_in: float, price_out: float) -> dict:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return {"status": "error", "error": "OPENAI_API_KEY unset", "answer": ""}
    sys_p = SYSTEM_PROMPT_INJECTED.format(ctx=ctx_text) if ctx_text else SYSTEM_PROMPT_COLD
    t0 = time.perf_counter()
    try:
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model_id, "temperature": 0,
                  "messages": [{"role": "system", "content": sys_p},
                               {"role": "user", "content": prompt}]},
            timeout=180,
        )
        r.raise_for_status()
        d = r.json()
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:300], "answer": "",
                "elapsed_s": time.perf_counter() - t0}
    ans = (d.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    u = d.get("usage", {})
    ti, to = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
    cost = (ti / 1e6) * price_in + (to / 1e6) * price_out
    return {"status": "ok", "answer": ans, "tokens": {"input": ti, "output": to},
            "cost_usd": round(cost, 6), "elapsed_s": time.perf_counter() - t0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", required=True, help="context.jsonl from capture_context.py")
    ap.add_argument("--model", required=True, help="haiku|sonnet|opus or an OpenAI model id (gpt-...)")
    ap.add_argument("--label", default=None)
    ap.add_argument("--parallelism", type=int, default=3)
    ap.add_argument("--price-in", type=float, default=0.0, help="$/Mtok input (OpenAI cost calc)")
    ap.add_argument("--price-out", type=float, default=0.0, help="$/Mtok output (OpenAI cost calc)")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    rows = [json.loads(l) for l in Path(args.context).read_text(encoding="utf-8").splitlines() if l.strip()]
    is_claude = args.model in CLAUDE_ALIASES
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = args.label or f"reuse_{args.model}_{stamp}"
    run_dir = RESULTS_ROOT / f"enterprise_rag_reuse_{args.model}_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    native, onyx = run_dir / "needles.jsonl", run_dir / "onyx_answers.jsonl"

    done: set[str] = set()
    if args.resume and onyx.exists():
        for l in onyx.read_text(encoding="utf-8").splitlines():
            try: done.add(json.loads(l)["question_id"])
            except Exception: pass
    todo = [r for r in rows if r["id"] not in done]
    print(f"gen model={args.model} ({'claude -p' if is_claude else 'openai'})  "
          f"{len(todo)}/{len(rows)} to gen (parallelism={args.parallelism}) -> {run_dir}")

    def gen_one(row):
        ctx, q = row.get("ctx_text", ""), row["question"]
        if is_claude:
            r = run_claude(q, ctx, args.model, None)
        else:
            r = run_openai(q, ctx, args.model, args.price_in, args.price_out)
        return row, r

    summ = {"model": args.model, "n": len(todo), "ok": 0, "err": 0, "cost_usd": 0.0,
            "started": datetime.now(timezone.utc).isoformat()}
    with native.open("a" if args.resume else "w", encoding="utf-8") as nfh, \
         onyx.open("a" if args.resume else "w", encoding="utf-8") as ofh, \
         ThreadPoolExecutor(max_workers=max(1, args.parallelism)) as pool:
        for i, (row, r) in enumerate(pool.map(gen_one, todo), 1):
            ans = r.get("answer", "") or ""
            nfh.write(json.dumps({
                "id": row["id"], "type": row["type"], "question": row["question"],
                "gold_answer": (row.get("gold_answer") or "")[:300],
                "expected_doc_ids": row.get("expected_doc_ids", []),
                "ctx": row.get("ctx", {}),
                "llm": {k: v for k, v in r.items() if k != "stderr"},
                "answer": ans[:2000], "predicted_doc_ids": row.get("predicted_doc_ids", []),
                "cost_usd": r.get("cost_usd", 0.0),
            }) + "\n"); nfh.flush()
            ofh.write(json.dumps({"question_id": row["id"], "answer": ans,
                                  "document_ids": row.get("predicted_doc_ids", [])}) + "\n"); ofh.flush()
            summ["cost_usd"] += r.get("cost_usd", 0.0) or 0.0
            summ["ok" if r.get("status") == "ok" else "err"] += 1
            if i % 25 == 0:
                print(f"  [{i}/{len(todo)}] ok={summ['ok']} err={summ['err']} cost=${summ['cost_usd']:.4f}")

    summ["cost_usd"] = round(summ["cost_usd"], 4)
    summ["finished"] = datetime.now(timezone.utc).isoformat()
    (run_dir / "summary.json").write_text(json.dumps(summ, indent=2), encoding="utf-8")
    print(f"DONE {args.model}: ok={summ['ok']} err={summ['err']} cost=${summ['cost_usd']}  -> {native}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
