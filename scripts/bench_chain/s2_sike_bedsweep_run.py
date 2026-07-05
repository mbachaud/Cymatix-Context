r"""s2_sike_bedsweep_run.py -- run the fixed SIKE curated needle set against
one DISTRACTOR bed, across a local ollama ladder + one Claude Sonnet rung.
Issue #221 (chain stage S2 helper).

Design is grounded in three existing harnesses:
  * benchmarks/bench_needle.py -- the SIKE needle list (NEEDLES) and the
    load-bearing gold-delivery scorer (check_gold_delivery / find_needle).
    We import and reuse its retrieval scoring verbatim so recall numbers are
    directly comparable to the standalone harness. bench_needle drives its
    answer model via the HELIX_MODEL env var and hits HELIX_URL /context +
    /v1/chat/completions -- so an ollama rung is just "set HELIX_MODEL and run
    find_needle".
  * scripts/bench_claude_matrix.py -- the claude -p convention: json output,
    per-question --max-budget-usd cost cap, sonnet default. We reuse
    score_answer's {-1,0,+1} trinary and the ABSTAIN markers.
  * benchmarks/bench_orchestrator.py -- NOT imported here (the .ps1 owns the
    BenchServer lifecycle so a bed failure can't wedge the whole sweep); this
    script assumes a server is already serving the bed at --helix-url and just
    runs the needle battery.

Consumers (rungs):
  * local ollama models discovered at runtime (`ollama list`), passed in via
    --ollama-models (comma-separated). Each is driven THROUGH helix's proxy
    (/v1/chat/completions) exactly as bench_needle does, so the context the
    local model sees is helix-assembled.
  * one Claude rung using Sonnet via `claude -p` (retrieval context injected
    as a system prompt the same way bench_enterprise_rag does), hard-capped at
    --claude-max-usd per query.

Retrieval recall is measured ONCE per bed (model-independent: it is a property
of /context, not of the answering model) using bench_needle.find_needle's
gold-delivery fields. Per-consumer correctness is measured by re-answering each
needle with that consumer and word-boundary accept-matching.

OUTPUT (single JSON, relocatable by the .ps1):
  benchmarks/results/sike_bedsweep_<bed>_<ts>.json

Stdlib + repo imports + httpx (already a repo dep, used by bench_needle).
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
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_BENCH_DIR = _REPO_ROOT / "benchmarks"
if str(_BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCH_DIR))

import httpx  # noqa: E402  (repo dependency; bench_needle imports it too)

# Reuse the canonical needle list + retrieval scorer.
import bench_needle  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Abstain markers + trinary scorer copied from scripts/bench_claude_matrix.py
# so Claude-rung scoring matches the existing matrix runner exactly.
_ABSTAIN_MARKERS = [
    r"\bi (?:don't|cannot|can't|am unable to|do not) (?:find|know|determine)\b",
    r"\bno (?:relevant )?information\b",
    r"\bnot (?:available|found|present)\b",
    r"\binsufficient (?:context|information|data)\b",
    r"\bunable to (?:find|determine|answer)\b",
    r"\bcouldn't find\b",
    r"\bi don't know\b",
]
_ABSTAIN_RE = re.compile("|".join(_ABSTAIN_MARKERS), re.IGNORECASE)


def _score_answer(answer_text: str, accept) -> dict:
    """{-1,0,+1} trinary, identical policy to bench_claude_matrix.score_answer."""
    text = (answer_text or "").strip()
    if not text:
        return {"score": 0, "reason": "empty"}
    for a in accept:
        if re.search(rf"\b{re.escape(a)}\b", text, re.IGNORECASE):
            return {"score": 1, "reason": f"accept-match:{a}", "matched_token": a}
    if _ABSTAIN_RE.search(text):
        return {"score": 0, "reason": "abstain"}
    return {"score": -1, "reason": "no-match-and-confident"}


# ---------------------------------------------------------------------------
# Claude rung (Sonnet) -- context-injected, cost-capped
# ---------------------------------------------------------------------------

_SYS_INJECTED = (
    "You are answering a single factual question about an internal knowledge "
    "base. Use ONLY the context provided below. If the answer is not in the "
    "context, say exactly: I don't know.\n\n<CONTEXT>\n{ctx}\n</CONTEXT>"
)


def _fetch_context(helix_url: str, query: str,
                   timeout_s: float = 30.0) -> tuple[str, bool]:
    """Fetch helix /context text for a query (decoder off, retrieval only).

    Returns (context_text, fetch_ok). fetch_ok=False distinguishes a
    fetch failure from a genuinely-empty retrieval so Claude-rung rows
    don't score infrastructure failures as abstentions (review
    2026-07-05). ignore_delivered matches bench_needle.find_needle.
    """
    try:
        resp = httpx.post(
            f"{helix_url}/context",
            json={"query": query, "decoder_mode": "none",
                  "ignore_delivered": True},
            timeout=timeout_s,
        )
    except Exception:
        return "", False
    if resp.status_code != 200:
        return "", False
    raw = resp.json()
    data = raw[0] if isinstance(raw, list) and raw else (
        raw if isinstance(raw, dict) else {}
    )
    ctx = data.get("content", "") or data.get("context", "") or ""
    return ctx[:12000], True


def _run_claude_sonnet(query: str, ctx_text: str, model: str,
                       max_usd: float, timeout_s: int = 180) -> dict:
    """claude -p, Sonnet, context as an appended system prompt, cost-capped.

    Mirrors scripts/bench_claude_matrix.run_claude (json output, budget cap)
    and bench_enterprise_rag.run_claude (context via --append-system-prompt-file
    + an empty --mcp-config so no local MCP is consulted).
    """
    clean_cwd = Path(tempfile.gettempdir()) / "helix-bench-clean-cwd"
    clean_cwd.mkdir(parents=True, exist_ok=True)
    empty_cfg = clean_cwd / "_empty_mcp.json"
    if not empty_cfg.exists():
        empty_cfg.write_text('{"mcpServers":{}}', encoding="utf-8")

    sys_prompt = _SYS_INJECTED.format(ctx=ctx_text) if ctx_text else (
        "You are answering a single factual question about an internal "
        "knowledge base. If you don't have the specific information, say "
        "exactly: I don't know. Do not make up details."
    )
    sp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8")
    sp.write(sys_prompt)
    sp.close()
    try:
        cmd = [
            "claude", "-p",
            "--model", model,
            "--tools", "",
            "--strict-mcp-config",
            "--mcp-config", str(empty_cfg),
            "--append-system-prompt-file", sp.name,
            "--max-budget-usd", str(max_usd),
            "--output-format", "json",
            "--", query,
        ]
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=str(clean_cwd),
                timeout=timeout_s, creationflags=NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "answer": "", "elapsed_s": timeout_s,
                    "cost_usd": None}
        elapsed = time.perf_counter() - t0
        if proc.returncode != 0:
            return {"status": "exit_nonzero", "rc": proc.returncode,
                    "stderr": (proc.stderr or "")[-300:], "answer": "",
                    "elapsed_s": elapsed, "cost_usd": None}
        try:
            data = json.loads(proc.stdout)
        except Exception:
            return {"status": "json_error",
                    "stdout_tail": (proc.stdout or "")[-300:],
                    "answer": "", "elapsed_s": elapsed, "cost_usd": None}
        return {
            "status": "ok",
            "answer": data.get("result", "") or "",
            "cost_usd": float(data.get("total_cost_usd", 0) or 0),
            "elapsed_s": elapsed,
        }
    finally:
        try:
            os.unlink(sp.name)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Ollama rung -- driven THROUGH the helix proxy, exactly like bench_needle
# ---------------------------------------------------------------------------

def _run_ollama_via_proxy(helix_url: str, query: str, model: str,
                          timeout_s: float = 660.0) -> dict:
    """Answer via helix /v1/chat/completions with the given ollama model.

    This is the same call bench_needle.find_needle makes for answer accuracy
    (HELIX_MODEL) -- context is helix-assembled and injected by the proxy.

    timeout_s must exceed the proxy's upstream timeout (600s via
    HELIX_SERVER_UPSTREAM_TIMEOUT in s2_sike_bed_sweep.ps1) so the server
    side decides and the bench records a real HTTP status instead of a
    client-side ReadTimeout (2026-07-05: the 180s default upstream
    timeout censored ~25-30 percent of the big-gemma needles as http_error).
    """
    t0 = time.perf_counter()
    try:
        resp = httpx.post(
            f"{helix_url}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": query}],
                "stream": False,
                "options": {"temperature": 0, "num_predict": 256},
            },
            timeout=timeout_s,
        )
    except Exception as exc:
        return {"status": "error", "error": str(exc), "answer": "",
                "elapsed_s": time.perf_counter() - t0}
    elapsed = time.perf_counter() - t0
    if resp.status_code != 200:
        return {"status": "http_error", "http": resp.status_code,
                "error": (resp.text or "")[:200],
                "answer": "", "elapsed_s": elapsed}
    choices = resp.json().get("choices", [])
    answer = choices[0].get("message", {}).get("content", "") if choices else ""
    return {"status": "ok", "answer": answer, "elapsed_s": elapsed,
            "cost_usd": 0.0}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bed", required=True,
                    help="Bed label for the output filename, e.g. 'xl'.")
    ap.add_argument("--helix-url", default=os.environ.get(
        "HELIX_URL", "http://127.0.0.1:11437"))
    ap.add_argument("--ollama-models", default="",
                    help="Comma-separated ollama model tags (local ladder).")
    ap.add_argument("--claude-model", default="sonnet",
                    help="claude -p --model for the Claude rung (Sonnet class).")
    ap.add_argument("--claude-max-usd", type=float, default=0.15,
                    help="Hard per-query cost cap for the Claude rung.")
    ap.add_argument("--skip-claude", action="store_true",
                    help="Skip the Claude rung (local ladder only).")
    ap.add_argument("--out", required=True, help="Output JSON path.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap needles (0 = all; smoke only).")
    args = ap.parse_args(argv)

    ollama_models = [m.strip() for m in args.ollama_models.split(",") if m.strip()]
    needles = list(bench_needle.NEEDLES)
    if args.limit:
        needles = needles[: args.limit]

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result: dict = {
        "benchmark": "sike_bedsweep",
        "issue": "#221",
        "bed": args.bed,
        "helix_url": args.helix_url,
        "timestamp": ts,
        "n_needles": len(needles),
        "consumers": {"ollama": ollama_models,
                      "claude": (None if args.skip_claude else args.claude_model)},
        "retrieval": {},
        "per_consumer": {},
        "errors": [],
    }

    # Server sanity + gene count.
    bench_needle.HELIX_URL = args.helix_url  # so find_needle hits this server
    try:
        stats = httpx.get(f"{args.helix_url}/stats", timeout=15).json()
        result["genome_genes"] = stats.get("total_genes")
        result["compression_ratio"] = stats.get("compression_ratio")
    except Exception as exc:
        result["errors"].append(f"/stats unreachable: {exc}")
        _write(result, args.out)
        return 2

    # Pass-1 client timeout must exceed the proxy's upstream timeout
    # (600s via HELIX_SERVER_UPSTREAM_TIMEOUT in the .ps1): find_needle's
    # answer step drives a local model through the proxy, and a client
    # that fires first turns a slow generation into a dropped recall row
    # (silent denominator shrink — review 2026-07-05).
    client = httpx.Client(timeout=660)

    # ------------------------------------------------------------------
    # Pass 1: retrieval recall (model-independent) via find_needle. This
    # also captures the default-model answer accuracy as a free byproduct,
    # but we only keep the retrieval fields here.
    # ------------------------------------------------------------------
    recall_rows = []
    ks = (1, 3, 5)
    recall_counts = {f"recall@{k}": 0 for k in ks}
    gold_delivered = 0
    body_has_answer = 0
    for nd in needles:
        try:
            r = bench_needle.find_needle(client, nd)
        except Exception as exc:
            result["errors"].append(f"find_needle({nd['name']}): {exc}")
            continue
        # find_needle exposes gold_delivered (gold source in delivered top-K)
        # and found_in_context (a delivered body carries the answer). We do
        # not have a per-rank position from find_needle, so recall@k here
        # collapses to gold_delivered within the harness's delivered set
        # (max_genes cap). Report it as recall@K_delivered for honesty.
        gd = bool(r.get("gold_delivered"))
        bh = bool(r.get("found_in_context"))
        if gd:
            gold_delivered += 1
            for k in ks:
                recall_counts[f"recall@{k}"] += 1
        if bh:
            body_has_answer += 1
        recall_rows.append({
            "name": r.get("name"),
            "gold_delivered": gd,
            "body_has_answer": bh,
            "n_gold_blocks": r.get("n_gold_blocks"),
            "n_delivered_blocks": r.get("n_delivered_blocks"),
            "resolution_confidence": r.get("resolution_confidence"),
            "context_latency_s": r.get("context_latency_s"),
        })

    n = max(len(recall_rows), 1)
    result["retrieval"] = {
        "note": ("recall@k here == gold_source_in_delivered_topK; find_needle "
                 "does not expose per-rank position, so k<=max_genes collapse. "
                 "gold_delivered_rate is the primary retrieval metric."),
        "gold_delivered": gold_delivered,
        "gold_delivered_rate": round(gold_delivered / n, 4),
        "body_has_answer": body_has_answer,
        "body_has_answer_rate": round(body_has_answer / n, 4),
        "per_needle": recall_rows,
    }
    for k in ks:
        result["retrieval"][f"recall@{k}_rate"] = round(
            recall_counts[f"recall@{k}"] / n, 4)

    # ------------------------------------------------------------------
    # Pass 2: per-consumer correctness. Each consumer re-answers every
    # needle; retrieval is identical (same bed), so this isolates the
    # answering model.
    # ------------------------------------------------------------------
    def _run_consumer(kind: str, model: str) -> dict:
        rows = []
        correct = abstain = wrong = errors = 0
        cost = 0.0
        consecutive_errors = 0
        rung_aborted = False
        for nd in needles:
            accept = nd.get("accept", [nd.get("expected", "")])
            q = nd["query"]
            if kind == "ollama":
                a = _run_ollama_via_proxy(args.helix_url, q, model)
            else:  # claude
                ctx, ctx_ok = _fetch_context(args.helix_url, q)
                a = _run_claude_sonnet(q, ctx, model, args.claude_max_usd)
                a["ctx_chars"] = len(ctx)
                if not ctx_ok:
                    a["ctx_fetch_failed"] = True
            ans = a.get("answer", "")
            sc = _score_answer(ans, accept)
            if a.get("status") != "ok":
                errors += 1
                consecutive_errors += 1
            else:
                consecutive_errors = 0
                if sc["score"] == 1:
                    correct += 1
                elif sc["score"] == 0:
                    abstain += 1
                else:
                    wrong += 1
            c = a.get("cost_usd")
            if isinstance(c, (int, float)):
                cost += c
            row = {
                "name": nd["name"], "status": a.get("status"),
                "score": sc["score"], "score_reason": sc["reason"],
                "answer_preview": ans[:200],
                "cost_usd": c, "elapsed_s": round(a.get("elapsed_s", 0), 2),
            }
            # Diagnostic fields for non-ok rows (2026-07-05: the 180s
            # upstream-timeout diagnosis needed the HTTP code; it was
            # captured by _run_ollama_via_proxy but never persisted).
            # Claude-rung failures carry rc/stderr/stdout_tail instead.
            for k in ("http", "rc", "ctx_chars"):
                if a.get(k) is not None:
                    row[k] = a.get(k)
            for k in ("error", "stderr", "stdout_tail"):
                if a.get(k):
                    row[k] = str(a.get(k))[:200]
            if a.get("ctx_fetch_failed"):
                row["ctx_fetch_failed"] = True
            rows.append(row)
            # Circuit breaker (review 2026-07-05): a wedged rung at the
            # 600s upstream timeout would otherwise burn 50 x ~600s =
            # ~8.3h producing identical error rows. Coverage already
            # penalizes the skipped needles (denominator is all needles).
            if consecutive_errors >= 5:
                rung_aborted = True
                break
        answered = correct + wrong
        summary = {
            "model": model,
            "correct": correct, "abstain": abstain, "wrong": wrong,
            "errors": errors,
            "answered": answered,
            "correctness_among_answered": round(correct / answered, 4)
            if answered else 0.0,
            "hallucination_rate": round(wrong / answered, 4) if answered else 0.0,
            # Denominator is the FULL needle set, not len(recall_rows):
            # Pass-1 drops must not inflate Pass-2 coverage (review
            # 2026-07-05; coverage could previously exceed 1.0).
            "coverage": round(answered / max(len(needles), 1), 4),
            "total_cost_usd": round(cost, 4),
            "per_needle": rows,
        }
        if rung_aborted:
            summary["rung_aborted"] = True
            summary["rung_aborted_after"] = len(rows)
        return summary

    for m in ollama_models:
        try:
            result["per_consumer"][f"ollama:{m}"] = _run_consumer("ollama", m)
        except Exception as exc:
            result["errors"].append(f"ollama consumer {m} failed: {exc}")

    if not args.skip_claude:
        try:
            result["per_consumer"][f"claude:{args.claude_model}"] = _run_consumer(
                "claude", args.claude_model)
        except Exception as exc:
            result["errors"].append(f"claude consumer failed: {exc}")

    client.close()
    _write(result, args.out)
    print("bed={} genes={} gold_delivered_rate={} consumers={}".format(
        args.bed, result.get("genome_genes"),
        result["retrieval"].get("gold_delivered_rate"),
        list(result["per_consumer"].keys())))
    return 0


def _write(result: dict, out_path: str) -> None:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("-> {}".format(p))


if __name__ == "__main__":
    raise SystemExit(main())
