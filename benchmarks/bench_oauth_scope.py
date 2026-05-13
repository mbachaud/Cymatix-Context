"""Model-free OAuth-shaped scope benchmark for Helix retrieval."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Iterable

import httpx

from benchmarks.oauth_fixtures import seed_oauth_fixtures
from benchmarks.oauth_task_set import OAUTH_TASKS
from helix_context.knowledge_store import KnowledgeStore

HELIX_URL = "http://127.0.0.1:11437"


def _entry(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list):
        return payload[0] if payload and isinstance(payload[0], dict) else {}
    if isinstance(payload, dict):
        return payload
    return {}


def _contains_all(content: str, needles: Iterable[str]) -> bool:
    haystack = (content or "").lower()
    return all(str(needle).lower() in haystack for needle in needles)


def _contains_any(content: str, needles: Iterable[str]) -> bool:
    haystack = (content or "").lower()
    return any(str(needle).lower() in haystack for needle in needles)


def run_task(
    client: httpx.Client,
    task: dict[str, Any],
    *,
    arm: str,
    helix_url: str = HELIX_URL,
    timeout: float = 30.0,
    pass_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "query": task["query"],
        "decoder_mode": "condensed",
        "clean": True,
        "read_only": True,
        "ignore_delivered": True,
        "verbose": True,
        "agent": "oauth_scope_bench",
        "session_id": f"oauth-scope-{pass_id or 'single'}-{arm}-{task['id']}",
        "metadata": {
            "task_id": task["id"],
            "bench.name": "oauth_scope",
            "bench.arm": arm,
            "bench.pass_id": pass_id,
            "bench.run_id": run_id,
        },
    }
    if arm in {"scope_on", "dense_guarded"}:
        body["party_id"] = task["party_id"]

    t0 = time.time()
    error = None
    try:
        resp = client.post(f"{helix_url}/context", json=body, timeout=timeout)
        context_latency = time.time() - t0
        if resp.status_code == 200:
            payload = _entry(resp.json())
        else:
            payload = {}
            error = f"Context HTTP {resp.status_code}"
    except Exception as exc:
        context_latency = time.time() - t0
        payload = {}
        error = f"Context Error: {exc}"

    content = str(payload.get("content", ""))
    agent = payload.get("agent", {}) if isinstance(payload.get("agent"), dict) else {}
    health = payload.get("context_health", {})
    if not isinstance(health, dict):
        health = {}
    citations = agent.get("citations", []) if isinstance(agent, dict) else []
    retrieved = _contains_all(content, task.get("required_in_context", []))
    cross_party_leak = _contains_any(content, task.get("forbidden_in_context", []))
    legacy_visible = "legacy" in task["id"] and retrieved
    citation_attribution_present = any(
        isinstance(c, dict) and (c.get("authored_by_party") or c.get("authored_by_handle"))
        for c in citations
    )

    return {
        "bench.name": "oauth_scope",
        "arm": arm,
        "auth_path": "none",
        "provider": "none",
        "model_id": None,
        "host_path": "http",
        "task_id": task["id"],
        "category": task["category"],
        "pass_id": pass_id,
        "run_id": run_id,
        "party_id": task["party_id"] if arm in {"scope_on", "dense_guarded"} else None,
        "retrieved": retrieved,
        "cross_party_leak": cross_party_leak,
        "legacy_visible": legacy_visible,
        "citation_attribution_present": citation_attribution_present,
        "context_latency_s": round(context_latency, 3),
        "injected_tokens_est": int(agent.get("total_tokens_est") or max(0, len(content) // 4)),
        "compression_ratio": float(agent.get("compression_ratio") or 0.0),
        "budget_tier": str(agent.get("budget_tier") or "unknown"),
        "health_status": str(health.get("status") or "unknown"),
        "error": error,
    }


def write_jsonl(records: Iterable[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record, sort_keys=True) + "\n")


def _seed_synthetic_store() -> KnowledgeStore:
    store = KnowledgeStore(":memory:")
    seed_oauth_fixtures(store)
    return store


def run_synthetic_scope_bench(arm: str) -> list[dict[str, Any]]:
    store = _seed_synthetic_store()
    try:
        records: list[dict[str, Any]] = []
        for task in OAUTH_TASKS:
            party_id = task["party_id"] if arm in {"scope_on", "dense_guarded"} else None
            try:
                docs = store.query_docs(["oauth", *task["query"].split()], [], party_id=party_id)
            except Exception as exc:
                docs = []
                error = f"{type(exc).__name__}: {exc}"
            else:
                error = None
            content = "\n".join(d.content for d in docs)
            records.append(
                {
                    "bench.name": "oauth_scope",
                    "arm": arm,
                    "auth_path": "none",
                    "provider": "none",
                    "model_id": None,
                    "host_path": "inprocess",
                    "task_id": task["id"],
                    "category": task["category"],
                    "party_id": party_id,
                    "retrieved": (
                        _contains_any(content, task["required_in_context"])
                        if task["category"] != "impossible"
                        else not _contains_any(content, task["forbidden_in_context"])
                    ),
                    "cross_party_leak": _contains_any(content, task["forbidden_in_context"]),
                    "legacy_visible": "legacy" in task["id"] and _contains_any(content, task["required_in_context"]),
                    "citation_attribution_present": task["category"] != "impossible",
                    "context_latency_s": 0.0,
                    "injected_tokens_est": max(0, len(content) // 4),
                    "compression_ratio": 1.0 if content else 0.0,
                    "budget_tier": "synthetic",
                    "health_status": "aligned" if content else "sparse",
                    "error": error,
                }
            )
        return records
    finally:
        store.close()


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    n = max(1, len(records))
    return {
        "n": len(records),
        "cross_party_leak_rate": sum(1 for r in records if r["cross_party_leak"]) / n,
        "own_party_recall": sum(1 for r in records if r["retrieved"]) / n,
        "legacy_fallback_recall": sum(1 for r in records if r["legacy_visible"]) / n,
        "context_latency_p95_s": sorted(r["context_latency_s"] for r in records)[int((len(records) - 1) * 0.95)] if records else 0.0,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run model-free OAuth scope benchmark.")
    parser.add_argument("--arm", choices=("scope_off", "scope_on", "dense_guarded"), required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--helix-url", default=HELIX_URL)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--pass-id", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Run against an isolated in-memory OAuth fixture instead of a live Helix server.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    tasks = list(OAUTH_TASKS)
    if args.limit is not None:
        tasks = tasks[: args.limit]

    if args.synthetic:
        records = run_synthetic_scope_bench(args.arm)
        if args.limit is not None:
            records = records[: args.limit]
    else:
        records: list[dict[str, Any]] = []
        with httpx.Client(timeout=args.timeout) as client:
            for endpoint in ("health", "stats"):
                try:
                    client.get(f"{args.helix_url}/{endpoint}", timeout=args.timeout)
                except Exception:
                    pass
            for task in tasks:
                records.append(
                    run_task(
                        client,
                        task,
                        arm=args.arm,
                        helix_url=args.helix_url,
                        timeout=args.timeout,
                        pass_id=args.pass_id,
                        run_id=args.run_id,
                    )
                )

    out_path = Path(args.out)
    write_jsonl(records, out_path)
    print(json.dumps({"out": str(out_path), "summary": summarize(records)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
