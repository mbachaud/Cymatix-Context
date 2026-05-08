r"""
Gemini API vs Local Model — Needle-in-a-Haystack comparison harness.

Mirrors bench_sweep.py exactly, but targets the Gemini REST API instead
of a local Ollama model.  Context retrieval is still fetched from the
local Helix proxy so the comparison is clean: same genome, same
compressed context, different downstream model.

Usage:
    # Single pass (quick sanity-check):
    GEMINI_API_KEY=... python benchmarks/bench_gemini.py

    # 20-pass statistical run:
    GEMINI_API_KEY=... N=20 python benchmarks/bench_gemini.py

    # Different model:
    GEMINI_API_KEY=... MODEL=gemini-2.5-pro python benchmarks/bench_gemini.py

Comparison workflow:
    python benchmarks/bench_sweep.py          # produces sweep_results.json
    GEMINI_API_KEY=... N=20 python benchmarks/bench_gemini.py   # produces gemini_results.json
    # then diff by hand or add compare_gemini_vs_local.py

Environment:
    GEMINI_API_KEY  — required
    MODEL           — Gemini model ID, default gemini-2.5-flash
    N               — number of passes per needle, default 1 (use 20 for variance)
    HELIX_URL       — default http://127.0.0.1:11437
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx


def _resolve_auth() -> tuple[str, str]:
    """Return (auth_value, header_name) for the Gemini API.

    Priority:
      1. GEMINI_API_KEY env var  → key-based auth (?key=...)
      2. ~/.gemini/oauth_creds.json access_token → Bearer token
    """
    key = os.environ.get("GEMINI_API_KEY", "")
    if key:
        return key, "api_key"

    creds_path = Path.home() / ".gemini" / "oauth_creds.json"
    if creds_path.exists():
        try:
            creds = json.loads(creds_path.read_text())
            token = creds.get("access_token", "")
            if token:
                return token, "bearer"
        except Exception:
            pass

    print(
        "ERROR: No Gemini credentials found.\n"
        "  Set GEMINI_API_KEY=... or run `gemini /auth` in the Gemini CLI.",
        file=sys.stderr,
    )
    sys.exit(1)

HELIX_URL = os.environ.get("HELIX_URL", "http://127.0.0.1:11437")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
MODEL = os.environ.get("MODEL", "gemini-2.5-flash")
N_RUNS = int(os.environ.get("N", "1"))

NEEDLES = [
    {
        "name": "helix_port",
        "query": "What port does the Helix proxy server listen on?",
        "expected": "11437",
        "accept": ["11437"],
    },
    {
        "name": "scorerift_threshold",
        "query": "What is the divergence threshold that triggers alerts in ScoreRift?",
        "expected": "0.15",
        "accept": ["0.15", ".15"],
    },
    {
        "name": "biged_skills_count",
        "query": "How many skills does the BigEd fleet have?",
        "expected": "125",
        "accept": ["125", "129"],
    },
    {
        "name": "bookkeeper_monetary",
        "query": "What type should be used for monetary values in BookKeeper instead of float?",
        "expected": "Decimal",
        "accept": ["decimal", "Decimal"],
    },
    {
        "name": "helix_pipeline_steps",
        "query": "How many steps are in the Helix expression pipeline?",
        "expected": "6",
        "accept": ["6", "six"],
    },
    {
        "name": "biged_rust_binary_size",
        "query": "What is the binary size of the Rust BigEd build in MB?",
        "expected": "11",
        "accept": ["11", "11mb", "11 mb"],
    },
    {
        "name": "genome_compression_target",
        "query": "What is the target compression ratio for Helix Context?",
        "expected": "5x",
        "accept": ["5x", "5:1", "5 to 1"],
    },
    {
        "name": "scorerift_preset_dimensions",
        "query": "How many dimensions does the Python preset in ScoreRift check?",
        "expected": "8",
        "accept": ["8", "eight"],
    },
    {
        "name": "helix_ribosome_budget",
        "query": "How many tokens are allocated for the ribosome decoder prompt?",
        "expected": "3000",
        "accept": ["3000", "3k", "3,000"],
    },
    {
        "name": "biged_default_model",
        "query": "What is the default local model used by BigEd for conductor tasks?",
        "expected": "qwen3",
        "accept": ["qwen3", "qwen3:4b", "qwen"],
    },
]


def fetch_context(client: httpx.Client, query: str) -> tuple[str, float]:
    """Fetch Helix-compressed context for a query. Returns (content, latency_s)."""
    t0 = time.time()
    try:
        resp = client.post(f"{HELIX_URL}/context", json={
            "query": query,
            "decoder_mode": "none",
        }, timeout=30)
    except Exception as exc:
        return f"[helix error: {exc}]", time.time() - t0

    latency = time.time() - t0
    if resp.status_code != 200:
        return f"[helix HTTP {resp.status_code}]", latency

    data = resp.json()
    entry = data[0] if data else {}
    return entry.get("content", ""), latency


def ask_gemini(
    client: httpx.Client,
    auth_value: str,
    auth_type: str,
    query: str,
    context: str,
) -> tuple[str, float]:
    """Send query + Helix context to Gemini. Returns (answer_text, latency_s)."""
    system_msg = (
        "You are a precise assistant. Answer using only the provided context. "
        "Be concise — give the exact value or term requested."
    )
    user_content = f"{context}\n\n---\n\n{query}" if context else query

    if auth_type == "api_key":
        url = f"{GEMINI_BASE}/chat/completions?key={auth_value}"
        headers = {"Content-Type": "application/json"}
    else:
        url = f"{GEMINI_BASE}/chat/completions"
        headers = {
            "Authorization": f"Bearer {auth_value}",
            "Content-Type": "application/json",
        }

    t0 = time.time()
    try:
        resp = client.post(
            url,
            headers=headers,
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_content},
                ],
                "max_tokens": 256,
                "temperature": 0.0,
            },
            timeout=60,
        )
    except Exception as exc:
        return f"[gemini error: {exc}]", time.time() - t0

    latency = time.time() - t0
    if resp.status_code != 200:
        return f"[gemini HTTP {resp.status_code}: {resp.text[:200]}]", latency

    choices = resp.json().get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", ""), latency
    return "", latency


def run_needle(
    client: httpx.Client,
    auth_value: str,
    auth_type: str,
    needle: dict,
) -> dict:
    context, ctx_latency = fetch_context(client, needle["query"])

    accept = needle.get("accept", [needle["expected"]])
    found_in_context = any(a.lower() in context.lower() for a in accept)

    answer, api_latency = ask_gemini(client, auth_value, auth_type, needle["query"], context)
    answer_correct = any(a.lower() in answer.lower() for a in accept)

    return {
        "found_in_context": found_in_context,
        "answer_correct": answer_correct,
        "context_latency_s": round(ctx_latency, 3),
        "api_latency_s": round(api_latency, 3),
        "answer_preview": answer[:300] if answer else "",
    }


def main() -> None:
    auth_value, auth_type = _resolve_auth()

    client = httpx.Client(timeout=120)

    # Verify Helix is up
    try:
        stats = client.get(f"{HELIX_URL}/stats", timeout=10).json()
        genome_genes = stats["total_genes"]
        print(f"Genome: {genome_genes} genes, {stats['compression_ratio']:.1f}x")
    except Exception as exc:
        print(f"Cannot reach Helix at {HELIX_URL}: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Model:   {MODEL}")
    print(f"Needles: {len(NEEDLES)}")
    print(f"Passes:  {N_RUNS}")
    print()

    # Accumulate per-needle results across runs
    needle_records: list[list[dict]] = [[] for _ in NEEDLES]

    for run_idx in range(N_RUNS):
        if N_RUNS > 1:
            print(f"─── Run {run_idx + 1}/{N_RUNS} ───")

        for needle_idx, needle in enumerate(NEEDLES):
            r = run_needle(client, auth_value, auth_type, needle)
            needle_records[needle_idx].append(r)

            icon_ctx = "+" if r["found_in_context"] else "-"
            icon_ans = "+" if r["answer_correct"] else "-"
            print(
                f"  ctx[{icon_ctx}] ans[{icon_ans}]  "
                f"ctx={r['context_latency_s']:.2f}s  api={r['api_latency_s']:.2f}s  "
                f"{needle['name']}"
            )

            # Be kind to the API between calls
            if needle_idx < len(NEEDLES) - 1:
                time.sleep(0.3)

        if run_idx < N_RUNS - 1:
            print()
            time.sleep(1.0)

    # Aggregate
    needle_summaries = []
    total_ctx_hits = 0
    total_ans_hits = 0
    total_api_latency = 0.0
    total_queries = len(NEEDLES) * N_RUNS

    for needle, records in zip(NEEDLES, needle_records):
        ctx_hits = sum(1 for r in records if r["found_in_context"])
        ans_hits = sum(1 for r in records if r["answer_correct"])
        avg_api_lat = sum(r["api_latency_s"] for r in records) / len(records)

        total_ctx_hits += ctx_hits
        total_ans_hits += ans_hits
        total_api_latency += sum(r["api_latency_s"] for r in records)

        needle_summaries.append({
            **{k: v for k, v in needle.items()},
            "ctx_hits": ctx_hits,
            "ans_hits": ans_hits,
            "n_runs": N_RUNS,
            "ctx_rate": round(ctx_hits / N_RUNS, 3),
            "ans_rate": round(ans_hits / N_RUNS, 3),
            "avg_api_latency_s": round(avg_api_lat, 3),
            "runs": records,
        })

    avg_latency = total_api_latency / total_queries

    # Summary table
    print()
    print("=" * 78)
    print(f"{'Needle':<35} {'Ctx':>5} {'Ans':>5} {'AvgLat':>8}")
    print("-" * 78)
    for ns in needle_summaries:
        ctx_str = f"{ns['ctx_hits']}/{N_RUNS}"
        ans_str = f"{ns['ans_hits']}/{N_RUNS}"
        print(f"  {ns['name']:<33} {ctx_str:>5} {ans_str:>5} {ns['avg_api_latency_s']:>7.2f}s")
    print("=" * 78)
    print(
        f"TOTAL  ctx={total_ctx_hits}/{total_queries} ({100*total_ctx_hits/total_queries:.0f}%)  "
        f"ans={total_ans_hits}/{total_queries} ({100*total_ans_hits/total_queries:.0f}%)  "
        f"avg_lat={avg_latency:.2f}s"
    )
    print(f"Model: {MODEL} | Genome: {genome_genes} genes | N_RUNS={N_RUNS}")

    output = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": MODEL,
        "model_type": "api",
        "n_runs": N_RUNS,
        "genome_genes": genome_genes,
        "needles_count": len(NEEDLES),
        "total_queries": total_queries,
        "context_retrieval": f"{total_ctx_hits}/{total_queries}",
        "answer_accuracy": f"{total_ans_hits}/{total_queries}",
        "ctx_rate": round(total_ctx_hits / total_queries, 3),
        "ans_rate": round(total_ans_hits / total_queries, 3),
        "avg_api_latency_s": round(avg_latency, 3),
        "needles": needle_summaries,
    }

    out_path = Path(__file__).parent / "gemini_results.json"
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\nResults → {out_path}")


if __name__ == "__main__":
    main()
