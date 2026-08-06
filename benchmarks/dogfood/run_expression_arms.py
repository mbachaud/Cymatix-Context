"""run_expression_arms.py — expression-path arms over the dogfood bed.

The rank ladders measure scoring; this measures what a caller actually
receives and what a consumer does with it. Per needle it runs the FULL
``build_context`` pipeline and records four things the rank harness cannot
see:

* **expressed delivery** — is a gold gene in ``expressed_gene_ids`` (the
  2026-07-30 probe showed defaults express ~3 genes against a rank-12
  window, so rank-based recall@12 overstates real delivery)
* **literal survival** — the needle's ``anchor`` / ``accept`` values
  surviving splice into ``expressed_context`` (a compressor that drops the
  pinned literal converts delivery into nothing)
* **achieved compression** — ``compression_ratio`` + expressed tokens, per
  ``splice_aggressiveness`` / decoder mode
* **consumer accuracy** — optional: hand ``ribosome_prompt`` +
  ``expressed_context`` + the question to a local Ollama model and grade
  the answer against ``accept``. Reported overall AND conditional on the
  value being present in context — the "if gold is present the model
  answers correctly ~80%" claim, measured.

Query-expansion arms (``qexp_*``) wire ``OllamaBackend`` directly onto the
manager. Honesty note for the receipt: since the explicit-opt-in change,
``[ribosome] backend="ollama"`` is NOT config-selectable (only litellm /
deberta construct a live ribosome; context_manager.py:1006-1086) — these
arms measure the shipped OllamaBackend class wired by the harness, which
also avoids LiteLLMBackend's 300ms min-interval throttle that would
pollute local latency numbers.

Usage::

    python benchmarks/dogfood/run_expression_arms.py --arms defaults,qexp_e4b
    python benchmarks/dogfood/run_expression_arms.py --limit 3   # smoke
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

ARMS: dict[str, dict] = {
    "defaults":       {},
    "broad":          {"call_kwargs": {"decoder_override": "broad"}},
    "maxg18":         {"overrides": {"budget.max_genes_per_turn": 18},
                       "call_kwargs": {"max_genes": 18}},
    "splice06":       {"overrides": {"budget.splice_aggressiveness": 0.6}},
    "splice09":       {"overrides": {"budget.splice_aggressiveness": 0.9}},
    "qexp_e2b":       {"qexp_model": "gemma4:e2b"},
    "qexp_e4b":       {"qexp_model": "gemma4:e4b"},
    "cons_e2b":       {"consumer_model": "gemma4:e2b"},
    "cons_e4b":       {"consumer_model": "gemma4:e4b"},
    "cons_e4b_broad": {"consumer_model": "gemma4:e4b",
                       "call_kwargs": {"decoder_override": "broad"}},
}
SCORE_K = 50  # rank recorded within top-50 scored, ladder-comparable


def _set_dotted(cfg, path: str, value) -> bool:
    section, _, key = path.partition(".")
    obj = getattr(cfg, section, None)
    if obj is None or not hasattr(obj, key):
        return False
    setattr(obj, key, value)
    return True


def _pct(vals: list[float], p: float) -> float:
    import math
    s = sorted(vals)
    return s[min(len(s) - 1, math.ceil(p * (len(s) - 1)))]


def _consumer_answer(backend, cw, query: str) -> tuple[str, float]:
    prompt = (f"<expressed_context>\n{cw.expressed_context}\n</expressed_context>\n\n"
              f"Question: {query}\nAnswer concisely with the specific value.")
    t0 = time.time()
    raw = backend.complete(prompt, system=cw.ribosome_prompt, temperature=0.0)
    return raw, (time.time() - t0) * 1000


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--genome", default="genomes/dogfood/genome.db")
    ap.add_argument("--gold", default="benchmarks/dogfood/gene_ids/gold_by_needle.json")
    ap.add_argument("--resolved", default="benchmarks/dogfood/needles_resolved.json")
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="benchmarks/dogfood/expression_arms.json")
    args = ap.parse_args()

    gold_by_needle = {k: set(v) for k, v in json.loads(
        (REPO_ROOT / args.gold).read_text(encoding="utf-8")).items()}
    resolved = json.loads((REPO_ROOT / args.resolved).read_text(encoding="utf-8"))
    needles = resolved["needles"][: args.limit] if args.limit else resolved["needles"]

    genome = str(REPO_ROOT / args.genome)
    os.environ["CYMATIX_GENOME_PATH"] = genome
    os.environ["CYMATIX_DISABLE_LEARN"] = "1"

    from cymatix_context.config import load_config
    from cymatix_context.context_manager import CymatixContextManager
    from cymatix_context.ribosome import OllamaBackend, Ribosome

    arm_names = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arm_names if a not in ARMS]
    if unknown:
        print(f"ERROR: unknown arms {unknown}; known: {list(ARMS)}", file=sys.stderr)
        return 2

    # Bed identity must come from the SAME gene_ids dir as the gold file —
    # reading the default dir mislabels receipts for alternate beds (bug
    # found on the 2026-07-31 tagger-bed run, receipt patched post-hoc).
    summary_path = (REPO_ROOT / args.gold).parent / "summary.json"
    identity = (json.loads(summary_path.read_text(encoding="utf-8"))
                .get("identity_sha256") if summary_path.exists() else None)
    print(f"expression arms: {arm_names} x {len(needles)} needles, "
          f"bed {identity[:16] if identity else 'UNKNOWN'}\n")

    results = []
    for arm_name in arm_names:
        arm = ARMS[arm_name]
        cfg = load_config()
        cfg.genome.path = genome
        unapplied = [p for p, v in arm.get("overrides", {}).items()
                     if not _set_dotted(cfg, p, v)]
        if unapplied:
            print(f"  SKIP {arm_name}: unknown config fields {unapplied}", file=sys.stderr)
            results.append({"arm": arm_name, "skipped": True,
                            "reason": f"unknown: {unapplied}"})
            continue

        manager = CymatixContextManager(cfg)

        if arm.get("qexp_model"):
            # Wire the shipped OllamaBackend directly (see module docstring).
            cfg.ribosome.enabled = True
            cfg.ribosome.query_expansion_enabled = True
            manager.ribosome = Ribosome(
                backend=OllamaBackend(model=arm["qexp_model"], timeout=60.0,
                                      keep_alive="30m", warmup=True),
                encoder=manager.encoder,
                splice_aggressiveness=cfg.budget.splice_aggressiveness,
            )

        consumer = None
        if arm.get("consumer_model"):
            consumer = OllamaBackend(model=arm["consumer_model"], timeout=120.0,
                                     keep_alive="30m", warmup=True)

        call_kwargs = dict(arm.get("call_kwargs", {}))
        # Untimed warmup: encoder load + (for qexp arms) the LRU-miss LLM
        # call on a throwaway query so timed needles pay steady-state cost.
        try:
            manager.build_context("warmup probe query", read_only=True,
                                  ignore_delivered=True, **call_kwargs)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {arm_name} warmup: {exc}", file=sys.stderr)

        rows = []
        for nd in needles:
            gold = gold_by_needle.get(nd["name"], set())
            accepts = [str(a) for a in (nd.get("accept") or [nd["expected"]])]
            t0 = time.time()
            try:
                cw = manager.build_context(nd["query"], read_only=True,
                                           ignore_delivered=True, **call_kwargs)
            except Exception as exc:  # noqa: BLE001 - a failing query is a datapoint
                rows.append({"name": nd["name"], "error": repr(exc)})
                continue
            latency_ms = (time.time() - t0) * 1000

            raw = manager.genome.last_query_scores or {}
            ordered = [g for g, _ in sorted(raw.items(), key=lambda kv: kv[1],
                                            reverse=True)][:SCORE_K]
            rank = next((i for i, g in enumerate(ordered, 1) if g in gold), None)
            expressed = set(cw.expressed_gene_ids)
            ctx = cw.expressed_context
            row = {
                "name": nd["name"],
                "latency_ms": round(latency_ms, 1),
                "rank": rank,
                "expressed_genes": len(expressed),
                "expressed_tokens": cw.total_estimated_tokens,
                "compression_ratio": round(cw.compression_ratio, 3),
                "gold_expressed": bool(gold & expressed),
                "anchor_survived": nd["anchor"] in ctx,
                "accept_in_context": any(a in ctx for a in accepts),
            }
            if consumer is not None:
                try:
                    answer, c_ms = _consumer_answer(consumer, cw, nd["query"])
                    row["consumer_latency_ms"] = round(c_ms, 1)
                    row["consumer_correct"] = any(a.lower() in answer.lower()
                                                  for a in accepts)
                    row["consumer_answer_head"] = answer.strip()[:160]
                except Exception as exc:  # noqa: BLE001
                    row["consumer_error"] = repr(exc)
            rows.append(row)

        ok = [r for r in rows if "error" not in r]
        n = len(ok) or 1
        lat = [r["latency_ms"] for r in ok]

        def rate(key: str, subset=None) -> float | None:
            pool = subset if subset is not None else ok
            if not pool:
                return None
            return round(sum(1 for r in pool if r.get(key)) / len(pool), 4)

        roll = {
            "needles": len(rows),
            "errors": len(rows) - len(ok),
            "recall@12_by_rank": round(
                sum(1 for r in ok if r["rank"] and r["rank"] <= 12) / n, 4),
            "gold_expressed_rate": rate("gold_expressed"),
            "accept_in_context_rate": rate("accept_in_context"),
            "anchor_survival_when_gold_expressed": rate(
                "anchor_survived", [r for r in ok if r["gold_expressed"]]),
            "median_expressed_genes": statistics.median(
                [r["expressed_genes"] for r in ok]) if ok else None,
            "median_expressed_tokens": statistics.median(
                [r["expressed_tokens"] for r in ok]) if ok else None,
            "mean_compression_ratio": round(statistics.mean(
                [r["compression_ratio"] for r in ok]), 3) if ok else None,
            "median_latency_ms": round(statistics.median(lat), 1) if lat else None,
            "p95_latency_ms": round(_pct(lat, 0.95), 1) if lat else None,
        }
        graded = [r for r in ok if "consumer_correct" in r]
        if graded:
            with_val = [r for r in graded if r["accept_in_context"]]
            without = [r for r in graded if not r["accept_in_context"]]
            roll["consumer_accuracy"] = rate("consumer_correct", graded)
            roll["consumer_accuracy_when_value_delivered"] = rate(
                "consumer_correct", with_val)
            roll["consumer_accuracy_when_value_absent"] = rate(
                "consumer_correct", without)
            roll["consumer_median_latency_ms"] = round(statistics.median(
                [r["consumer_latency_ms"] for r in graded]), 1)

        results.append({"arm": arm_name, "spec": {k: v for k, v in arm.items()},
                        "rollup": roll, "per_needle": rows})
        print(f"  {arm_name:16s} gold_expr={roll['gold_expressed_rate']} "
              f"accept_in_ctx={roll['accept_in_context_rate']} "
              f"genes~{roll['median_expressed_genes']} "
              f"tok~{roll['median_expressed_tokens']} "
              f"lat~{roll['median_latency_ms']}ms"
              + (f"  consumer={roll.get('consumer_accuracy')}"
                 f" (delivered={roll.get('consumer_accuracy_when_value_delivered')})"
                 if graded else ""), flush=True)

    payload = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "genome": genome,
        "bed_identity_sha256": identity,
        "score_k": SCORE_K,
        "note": ("full build_context expression path; gold by gene_id set "
                 "membership; qexp arms wire OllamaBackend directly (not "
                 "config-selectable since explicit-opt-in); no OTel this session"),
        "arms": results,
    }
    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
