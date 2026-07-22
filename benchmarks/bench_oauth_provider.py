"""Provider OAuth focus benchmark runner.

This runner keeps Helix retrieval telemetry separate from optional provider
host paths. The ``dry_run`` provider is model-free and never invokes a
provider adapter.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from benchmarks.oauth_task_set import OAUTH_TASKS

BENCH_NAME = "oauth_provider_focus"
DEFAULT_HELIX_URL = os.environ.get("HELIX_URL", "http://127.0.0.1:11437")
DEFAULT_OUT = Path("runs/oauth_provider_focus.jsonl")
HTTP_TIMEOUT_S = 30.0
PROVIDER_TIMEOUT_S = 120.0
CLAUDE_API_OVERRIDE_ENVS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextResult:
    content: str
    latency_s: float
    injected_tokens_est: int
    compression_ratio: float


@dataclass(frozen=True)
class ProviderResult:
    answer: str
    latency_s: float
    returncode: int
    stdout_preview: str
    stderr_preview: str


def _estimate_tokens(text: str) -> int:
    return max(0, (len(text) + 3) // 4)


def verify_claude_oauth_profile() -> tuple[bool, str, dict[str, Any]]:
    present = [name for name in CLAUDE_API_OVERRIDE_ENVS if os.environ.get(name)]
    if present:
        return False, f"Claude API override env vars present: {', '.join(present)}", {}

    try:
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        log.warning("failed to verify Claude OAuth profile: %s", exc)
        return False, f"{type(exc).__name__}: {exc}", {}

    if result.returncode != 0:
        return False, f"claude auth status exited {result.returncode}", {}

    try:
        raw_status = json.loads(result.stdout or "{}")
    except Exception as exc:
        return False, f"cannot parse claude auth status: {exc}", {}

    status = {
        "authMethod": raw_status.get("authMethod"),
        "apiProvider": raw_status.get("apiProvider"),
        "subscriptionType": raw_status.get("subscriptionType"),
    }
    if raw_status.get("loggedIn") is not True:
        return False, "claude auth status reports loggedIn=false", status
    if status["authMethod"] != "claude.ai" or status["apiProvider"] != "firstParty":
        return False, f"expected claude.ai/firstParty, got {status}", status
    return True, "ok", status


def _context_content(payload: Any) -> str:
    if isinstance(payload, list):
        first = payload[0] if payload else {}
        return str(first.get("content", "")) if isinstance(first, dict) else ""

    if not isinstance(payload, dict):
        return ""

    if isinstance(payload.get("content"), str):
        return payload["content"]
    if isinstance(payload.get("context"), str):
        return payload["context"]

    for key in ("items", "context", "results"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict):
                return str(first.get("content", ""))
    return ""


def _metric(payload: Any, name: str, default: float = 0.0) -> float:
    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates.append(payload.get(name))
        agent = payload.get("agent")
        if isinstance(agent, dict):
            candidates.append(agent.get(name))
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        first = payload[0]
        candidates.append(first.get(name))
        agent = first.get("agent")
        if isinstance(agent, dict):
            candidates.append(agent.get(name))

    for value in candidates:
        if isinstance(value, int | float):
            return float(value)
    return default


def fetch_context(
    helix_url: str,
    task: dict[str, Any],
    *,
    arm: str,
    provider: str,
    pass_id: str | None,
    run_id: str | None,
) -> ContextResult:
    metadata = {
        "bench.name": BENCH_NAME,
        "bench.arm": arm,
        "bench.provider": provider,
        "bench.pass_id": pass_id,
        "bench.run_id": run_id,
        "task_id": task["id"],
    }
    body: dict[str, Any] = {
        "query": task["query"],
        "decoder_mode": "condensed",
        "clean": True,
        "read_only": True,
        "ignore_delivered": True,
        "verbose": True,
        "agent": "oauth_provider_bench",
        "session_id": f"oauth-provider-{pass_id or 'single'}-{arm}-{task['id']}",
        "metadata": metadata,
    }
    if task.get("party_id"):
        body["party_id"] = task["party_id"]

    t0 = time.time()
    try:
        resp = httpx.post(
            f"{helix_url.rstrip('/')}/context",
            json=body,
            timeout=HTTP_TIMEOUT_S,
        )
        latency_s = time.time() - t0
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        latency_s = time.time() - t0
        log.warning("failed to fetch Helix context: %s", exc)
        return ContextResult("", latency_s, 0, 0.0)

    content = _context_content(payload)
    injected_tokens = int(
        _metric(payload, "injected_tokens_est", _metric(payload, "total_tokens_est", _estimate_tokens(content)))
    )
    compression_ratio = _metric(payload, "compression_ratio", 0.0)
    return ContextResult(content, latency_s, injected_tokens, compression_ratio)


def _build_prompt(task: dict[str, str], context: str) -> str:
    if context:
        return (
            "Answer using only the provided Helix context. Be concise.\n\n"
            f"<cymatix_context>\n{context}\n</cymatix_context>\n\n"
            f"Question: {task['query']}"
        )
    return f"Answer concisely.\n\nQuestion: {task['query']}"


def call_provider(provider: str, prompt: str, model_id: str | None) -> ProviderResult:
    if provider == "claude_print":
        command = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--tools",
            "",
        ]
        if model_id:
            command.extend(["--model", model_id])
        command.append(prompt)
    elif provider == "codex_exec":
        command = [
            "codex",
            "-a",
            "never",
            "--sandbox",
            "read-only",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--cd",
            ".",
            "--json",
            prompt,
        ]
    else:
        raise ValueError(f"unsupported provider adapter: {provider}")

    t0 = time.time()
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROVIDER_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        log.warning("provider adapter failed: %s", exc)
        return ProviderResult("", time.time() - t0, 1, "", str(exc)[:500])

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    return ProviderResult(
        answer=stdout,
        latency_s=time.time() - t0,
        returncode=result.returncode,
        stdout_preview=stdout[:500],
        stderr_preview=stderr[:500],
    )


def _provider_metadata(provider: str, model: str | None) -> dict[str, Any]:
    if provider == "dry_run":
        return {
            "auth_path": "none",
            "provider": "none",
            "model_id": None,
            "host_path": "none",
        }
    if provider == "claude_print":
        return {
            "auth_path": "claude_code_oauth",
            "provider": "anthropic",
            "model_id": model or "sonnet",
            "host_path": "claude_print",
        }
    return {
        "auth_path": "chatgpt_codex_oauth",
        "provider": "openai",
        "model_id": model or os.environ.get("CODEX_MODEL", "default"),
        "host_path": "codex_exec",
    }


def _make_record(
    task: dict[str, str],
    provider: str,
    arm: str,
    context: ContextResult,
    model: str | None,
    pass_id: str | None,
    run_id: str | None,
) -> dict[str, Any]:
    return {
        "bench.name": BENCH_NAME,
        "arm": arm,
        **_provider_metadata(provider, model),
        "task_id": task["id"],
        "party_id": task.get("party_id"),
        "pass_id": pass_id,
        "run_id": run_id,
        "context_attached": arm == "context_attached",
        "provider_called": False,
        "context_latency_s": round(context.latency_s, 3),
        "injected_tokens_est": context.injected_tokens_est,
        "compression_ratio": round(context.compression_ratio, 3),
        "answer_correct": None,
    }


def run(args: argparse.Namespace) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    selected_tasks = OAUTH_TASKS[: args.limit] if args.limit is not None else OAUTH_TASKS

    for task in selected_tasks:
        if args.arm == "context_attached":
            context = fetch_context(
                args.helix_url,
                task,
                arm=args.arm,
                provider=args.provider,
                pass_id=args.pass_id,
                run_id=args.run_id,
            )
        else:
            context = ContextResult("", 0.0, 0, 0.0)

        record = _make_record(
            task,
            args.provider,
            args.arm,
            context,
            args.model,
            args.pass_id,
            args.run_id,
        )

        if args.provider != "dry_run":
            prompt = _build_prompt(task, context.content)
            provider_result = call_provider(args.provider, prompt, record["model_id"])
            record.update(
                {
                    "provider_called": True,
                    "provider_latency_s": round(provider_result.latency_s, 3),
                    "provider_returncode": provider_result.returncode,
                    "provider_stdout_preview": provider_result.stdout_preview,
                    "provider_stderr_preview": provider_result.stderr_preview,
                }
            )

        records.append(record)

    return records


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    path.write_text(text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=("dry_run", "claude_print", "codex_exec"),
        default="dry_run",
    )
    parser.add_argument(
        "--arm",
        choices=("context_attached", "context_absent"),
        default="context_attached",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--helix-url", default=DEFAULT_HELIX_URL)
    parser.add_argument("--model", default=None)
    parser.add_argument("--pass-id", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--require-claude-oauth", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be non-negative")
    if args.provider == "claude_print" and args.require_claude_oauth:
        ok, reason, status = verify_claude_oauth_profile()
        if not ok:
            parser.error(f"--require-claude-oauth failed: {reason}")
        log.info("Claude OAuth profile verified: %s", status)

    records = run(args)
    write_jsonl(args.out, records)

    provider_note = (
        "no provider call made"
        if args.provider == "dry_run"
        else f"provider path {args.provider} invoked"
    )
    print(f"{len(records)} records written, {provider_note}: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
