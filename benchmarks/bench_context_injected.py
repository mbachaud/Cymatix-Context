r"""Context-injected bench — bypasses helix MCP entirely.

Designed in response to the 2026-05-20 finding that helix MCP never
actually loaded under the existing claude_matrix bench (claude -p doesn't
read user-level .mcp.json on Windows + the stdio handshake crashes during
_register_with_registry). The claude_matrix bench was measuring
filesystem Read/Grep over the cwd + CLAUDE.md prior-leak, not retrieval.

This script measures helix's actual retrieval-quality contribution by:

  1. Disabling ALL built-in tools (``--tools ""``) — no Read, Grep,
     Glob, Bash, etc. The LLM cannot grep the cwd.
  2. Disabling MCP entirely (no ``--mcp-config``, no MCP tools).
  3. Spawning claude -p in an empty cwd (``F:\tmp\bench-clean-cwd``) so
     no project CLAUDE.md / README.md / source code is auto-loaded.
  4. Optionally injecting context via ``--append-system-prompt``:
     - ``--mode none``    : M0 anchor. Empty context. Pure prior baseline.
     - ``--mode helix``   : M2. helix /context output injected.
     - ``--mode oracle``  : M3. First gold_source file content injected.

The three modes give the honest-baseline scale recommended by seat 3
of the 2026-05-20 council:

    M0 ≈ 5/50 (cold-start floor, verified today)
    M2 = helix retrieval-quality measurement (no MCP-call overhead)
    M3 = retrieval ceiling (perfect-retrieval upper bound)

Real helix lift = M2 − M0.  Helix's distance from perfect = M3 − M2.

Pre-conditions:
  * claude CLI on PATH (uses same OAuth path as the existing bench)
  * For ``--mode helix``: bench harness manages a helix server via
    BenchServer (same as the claude_matrix bench).
  * For ``--mode none`` and ``--mode oracle``: ``--no-server`` skips
    starting helix; the bench needs no helix-side runtime.

Usage:
  python benchmarks/bench_context_injected.py --mode none   --only medium --no-server
  python benchmarks/bench_context_injected.py --mode helix  --only medium
  python benchmarks/bench_context_injected.py --mode oracle --only medium --no-server

Output:
  benchmarks/results/injected_<mode>_<UTC-timestamp>/
    summary.json
    <profile>.jsonl  (per profile)
    run.log
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Sibling imports — bench_orchestrator + bench_claude_matrix live alongside
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_orchestrator import BenchServer, Fixture  # noqa: E402
# Reuse needles, scoring, retrieval probe, abstain regex from the existing
# bench so this script is a measurement-design variant, not a re-fork of
# the question set.
from bench_claude_matrix import (  # noqa: E402
    NEEDLES,
    score_answer as _baseline_score_answer,
    retrieval_probe,
    HELIX_URL,
    CLAUDE_TIMEOUT_S,
    CONTEXT_TIMEOUT_S,
    UNTRUSTED_PROFILES,
    MANIFEST,
    ABSTAIN_RE as _BASELINE_ABSTAIN_RE,
)

import re as _re

# Extended abstain detection — the baseline ABSTAIN_RE in
# bench_claude_matrix only catches "don't know/find/determine", but
# closed-book LLMs hedge with "I don't have any information about" much
# more often. Per the 2026-05-20 council seat-3 review on
# refusal-as-correctness, we reclassify those from -1 (wrong-confident)
# to 0 (abstain) so the hallucination_rate metric is meaningful. This is
# additive on top of the baseline regex; it does NOT modify the canonical
# ABSTAIN_RE used by claude_matrix.
_EXTENDED_ABSTAIN_PATTERNS = [
    r"\bi (?:don't|do not|cannot|can't|am unable to) have (?:any |specific |enough |reliable |the )?(?:information|context|data|knowledge|access|details)\b",
    r"\bi'?m not (?:sure|familiar|aware|certain) (?:what|which|about|of|with)\b",
    r"\bi(?:'?ll|'?d) need (?:more|additional) (?:context|information|details)\b",
    r"\b(?:could you|can you) (?:provide|clarify|specify|share)\b",
    r"\bnot (?:enough|sufficient) (?:context|information|data)\b",
    r"\bwithout (?:more |additional )?(?:context|information|details)\b",
]
_EXTENDED_ABSTAIN_RE = _re.compile("|".join(_EXTENDED_ABSTAIN_PATTERNS), _re.IGNORECASE)


def score_answer(answer_text: str, accept_substrings: list[str]) -> dict:
    """Score with extended abstain detection layered on top of baseline.

    Order: accept-match wins (correct=+1). Else baseline abstain (=0).
    Else extended abstain (=0, tagged abstain_extended). Else wrong (-1).
    """
    base = _baseline_score_answer(answer_text, accept_substrings)
    if base["score"] != -1:
        return base
    if _EXTENDED_ABSTAIN_RE.search(answer_text or ""):
        return {"score": 0, "reason": "abstain_extended"}
    return base


# ── Configuration ────────────────────────────────────────────────────────

RESULTS_ROOT = Path(r"F:\Projects\helix-context\benchmarks\results")

# Empty cwd with no CLAUDE.md / AGENTS.md / README.md anywhere up the
# tree (F:\tmp is not a git root and contains no markdown). Created
# during the 2026-05-20 leak diagnosis.
CLEAN_CWD = Path(r"F:\tmp\bench-clean-cwd")

# Gold-source files referenced by needle["gold_source"][0] resolve under
# F:\Projects (e.g. "helix-context/CLAUDE.md" → F:\Projects\helix-context\CLAUDE.md).
PROJECTS_ROOT = Path(r"F:\Projects")

# Hard cap on injected-system-prompt size so very large gold files don't
# distort token-cost comparisons or breach context-window limits.
MAX_CTX_CHARS = 60_000


# ── Mode-specific context sources ────────────────────────────────────────

def context_none(_needle: dict, _helix_url: str) -> tuple[str, dict]:
    """M0 anchor — no context provided."""
    return "", {"source": "none", "chars": 0}


def context_helix(needle: dict, helix_url: str) -> tuple[str, dict]:
    """M2 — POST /context with the needle's query and return rendered content.

    Uses a unique ``session_id`` per call (``bench-<needle>-<ns>``) so
    helix's session_delivery layer does NOT elide repeats. The bench's
    50 needles are independent factual queries and should each receive
    a fresh delivery. Without this, every needle past ~3 in a bench run
    receives only `[gene=... ↻ delivered N queries ago]` stubs instead
    of actual content (verified during 2026-05-20 M2 debugging — 5141
    chars with fresh session vs 368 chars of stubs without).
    """
    import time as _time
    session_id = f"bench-{needle['name']}-{int(_time.time_ns())}"
    t0 = time.perf_counter()
    try:
        resp = httpx.post(
            f"{helix_url}/context",
            json={
                "query": needle["query"],
                "decoder_mode": "condensed",
                "session_id": session_id,
            },
            timeout=CONTEXT_TIMEOUT_S,
        )
    except Exception as exc:
        return "", {
            "source": "helix_error",
            "error": str(exc),
            "chars": 0,
            "latency_s": time.perf_counter() - t0,
            "session_id": session_id,
        }
    latency = time.perf_counter() - t0
    if resp.status_code != 200:
        return "", {
            "source": "helix_http_error",
            "http": resp.status_code,
            "chars": 0,
            "latency_s": latency,
            "session_id": session_id,
        }
    data = resp.json()
    entry = data[0] if isinstance(data, list) and data else {}
    content = (entry.get("content") or "")[:MAX_CTX_CHARS]
    return content, {
        "source": "helix",
        "chars": len(content),
        "latency_s": latency,
        "decoder_mode": "condensed",
        "session_id": session_id,
    }


def context_oracle(needle: dict, _helix_url: str) -> tuple[str, dict]:
    """M3 — read needle["gold_source"][0] as a literal file path."""
    gs = needle.get("gold_source") or []
    if not gs:
        return "", {"source": "oracle_no_gold", "chars": 0}
    rel = gs[0]
    path = PROJECTS_ROOT / rel.replace("/", os.sep)
    if not path.is_file():
        return "", {
            "source": "oracle_missing",
            "expected_path": str(path),
            "chars": 0,
        }
    try:
        content = path.read_text(encoding="utf-8", errors="replace")[:MAX_CTX_CHARS]
    except Exception as exc:
        return "", {
            "source": "oracle_read_error",
            "expected_path": str(path),
            "error": str(exc),
            "chars": 0,
        }
    return content, {
        "source": "oracle",
        "path": str(path),
        "chars": len(content),
    }


CONTEXT_FNS = {
    "none": context_none,
    "helix": context_helix,
    "oracle": context_oracle,
    # Tool-enabled modes for the 3-arm head-to-head per seat 4's
    # X/Y/Z falsification (2026-05-20 council review). Arms B and C
    # give the LLM Read/Grep/Glob/Bash access to F:/Projects/. Arm B
    # has no helix; Arm C has helix injection on top.
    "readgrep": context_none,    # tool-enabled, no helix injection
    "both": context_helix,        # tool-enabled, helix injection on top
}

# Modes where the LLM gets default built-in tools and broad project
# access — used for head-to-head with native agentic Read/Grep.
TOOL_ENABLED_MODES = {"readgrep", "both"}


# Empty MCP config to force-disable any user/system-registered MCP
# servers across all arms. Written on first call.
EMPTY_MCP_CONFIG_PATH = Path(r"F:\tmp\bench-empty-mcp.json")


def _ensure_empty_mcp_config() -> Path:
    """Write an empty MCP-config file once. Reused across all arms."""
    if not EMPTY_MCP_CONFIG_PATH.exists():
        EMPTY_MCP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        EMPTY_MCP_CONFIG_PATH.write_text(
            json.dumps({"mcpServers": {}}), encoding="utf-8",
        )
    return EMPTY_MCP_CONFIG_PATH


# System-prompt fragment for tool-enabled modes. Tells the LLM:
#   1. tool surface and where source lives
#   2. EXPLICIT prohibition on reading bench-scoring artifacts
#   3. abstain instruction
# The prohibition is honest. We audit answer_text for citations to
# blocked paths to detect violations.
SYSTEM_PROMPT_TOOLS_GUARD = (
    "You are answering a single factual question about an internal codebase.\n\n"
    "You may use the Read, Grep, Glob, and Bash tools to find the answer in the\n"
    "F:/Projects directory tree. Project source lives under directories such as:\n"
    "  - F:/Projects/helix-context/  (excluding benchmarks/ and .claude/ — see below)\n"
    "  - F:/Projects/BookKeeper/\n"
    "  - F:/Projects/Education/biged-rs/\n"
    "  - F:/Projects/Education/fleet/\n"
    "  - F:/Projects/CosmicTasha/\n"
    "  - F:/Projects/MaxExpressKit/  (if present)\n"
    "  - F:/Projects/ScoreRift/      (if present)\n"
    "  - F:/Projects/two-brain-audit/  (if present)\n\n"
    "IMPORTANT — Do NOT read, grep, glob, or otherwise access any file under:\n"
    "  - F:/Projects/helix-context/benchmarks/\n"
    "  - F:/Projects/helix-context/.claude/\n"
    "These are test-scoring artifacts; accessing them invalidates the measurement.\n\n"
    "Answer concisely from what you find in the allowed source. If you cannot find\n"
    "the answer, respond exactly: I don't know.\n"
)


# ── claude -p invocation with tools disabled ─────────────────────────────

SYSTEM_PROMPT_INJECTED = (
    "You are answering a single factual question about an internal codebase. "
    "Use ONLY the context provided below. If the answer is not in the context, "
    "say exactly: I don't know.\n\n"
    "<CONTEXT>\n{ctx}\n</CONTEXT>"
)


import tempfile

# Windows command-line limit is 32,767 chars total. Passing 41KB system
# prompts via --append-system-prompt fails with WinError 206 ("filename
# or extension is too long"). Always route through a temp file so context
# size is bounded only by --append-system-prompt-file (no command-line
# arg). See the 2026-05-20 M3 oracle run — 6/50 medium needles failed
# this way before the fix because fleet.toml is 41KB.
def run_claude_no_tools(
    query: str,
    ctx_text: str,
    model: str,
    max_usd: float,
    log: logging.Logger,
) -> dict:
    """Run claude -p with --tools "" (no built-in tools) and optional
    file-based system prompt. No MCP config.

    Returns dict matching the existing run_claude shape so per-needle
    record building is reusable.
    """
    cmd = [
        "claude",
        "-p",
        # Disable every built-in tool. Without this, the LLM can Read /
        # Grep the cwd — even an empty one would still let it navigate up,
        # and the model may still call WebSearch on its own.
        # Putting --tools "" first AND adding -- before the prompt is the
        # only ordering that survives the variadic-arg parser quirk.
        "--tools", "",
        "--output-format", "json",
        "--no-session-persistence",
        "--permission-mode", "bypassPermissions",
        "--model", model,
        "--max-budget-usd", str(max_usd),
    ]

    sys_prompt_path: str | None = None
    if ctx_text:
        sys_prompt = SYSTEM_PROMPT_INJECTED.format(ctx=ctx_text)
        # Use NamedTemporaryFile with delete=False so the subprocess can
        # open it. We'll unlink after the run.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".sysprompt.txt",
            delete=False,
        ) as fh:
            fh.write(sys_prompt)
            sys_prompt_path = fh.name
        cmd += ["--append-system-prompt-file", sys_prompt_path]

    # End-of-flags sentinel so the positional prompt is unambiguous after
    # variadic-arg flags like --tools.
    cmd.append("--")
    cmd.append(query)

    log.info(
        "  claude -p tools=disabled mcp=default ctx_chars=%d query=%s",
        len(ctx_text), query[:60],
    )
    t0 = time.perf_counter()
    try:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CLAUDE_TIMEOUT_S,
                cwd=str(CLEAN_CWD),
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "elapsed_s": CLAUDE_TIMEOUT_S}
        except Exception as exc:
            return {
                "status": "error",
                "error": str(exc),
                "elapsed_s": time.perf_counter() - t0,
            }
    finally:
        if sys_prompt_path is not None:
            try:
                os.unlink(sys_prompt_path)
            except OSError:
                pass
    elapsed = time.perf_counter() - t0
    if proc.returncode != 0:
        return {
            "status": "exit_nonzero",
            "returncode": proc.returncode,
            "stderr": proc.stderr[:500],
            "stdout_head": proc.stdout[:500],
            "elapsed_s": elapsed,
        }
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {
            "status": "json_decode_error",
            "error": str(exc),
            "stdout_head": proc.stdout[:500],
            "elapsed_s": elapsed,
        }
    return {"status": "ok", "result": result, "elapsed_s": elapsed}


def run_claude_with_tools(
    query: str,
    ctx_text: str,
    model: str,
    max_usd: float,
    log: logging.Logger,
) -> dict:
    """Run claude -p with DEFAULT built-in tools (Read/Grep/Glob/Bash etc.)
    and broad F:/Projects access. Optional helix injection on top.

    Used by the 3-arm head-to-head — Arm B (readgrep, no helix) and Arm C
    (both, helix + tools). MCP is force-disabled via --strict-mcp-config
    so we measure native-tool-use, not other-MCP-leakage.
    """
    empty_mcp = _ensure_empty_mcp_config()

    cmd = [
        "claude",
        "-p",
        "--output-format", "json",
        "--no-session-persistence",
        "--permission-mode", "bypassPermissions",
        "--model", model,
        "--max-budget-usd", str(max_usd),
        # Force-disable ALL MCP servers (Gmail/Drive/Context7/etc.) so
        # this arm measures helix-vs-Read/Grep, not helix-vs-Context7.
        "--mcp-config", str(empty_mcp),
        "--strict-mcp-config",
        # Allow tool access into the project tree. The cwd is the empty
        # CLEAN_CWD, so CLAUDE.md auto-discovery still doesn't fire (walk
        # only goes up from cwd, not into --add-dir paths). The
        # system-prompt guard tells the LLM explicitly to skip the
        # benchmarks/ dir.
        "--add-dir", str(PROJECTS_ROOT),
    ]

    # System prompt: tool guard + optional helix injection.
    sys_prompt_parts = [SYSTEM_PROMPT_TOOLS_GUARD]
    if ctx_text:
        sys_prompt_parts.append(SYSTEM_PROMPT_INJECTED.format(ctx=ctx_text))
    sys_prompt = "\n\n".join(sys_prompt_parts)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".sysprompt.txt", delete=False,
    ) as fh:
        fh.write(sys_prompt)
        sys_prompt_path = fh.name
    cmd += ["--append-system-prompt-file", sys_prompt_path]

    cmd.append("--")
    cmd.append(query)

    log.info(
        "  claude -p tools=enabled mcp=disabled ctx_chars=%d query=%s",
        len(ctx_text), query[:60],
    )
    t0 = time.perf_counter()
    try:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CLAUDE_TIMEOUT_S,
                cwd=str(CLEAN_CWD),
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "elapsed_s": CLAUDE_TIMEOUT_S}
        except Exception as exc:
            return {
                "status": "error",
                "error": str(exc),
                "elapsed_s": time.perf_counter() - t0,
            }
    finally:
        try:
            os.unlink(sys_prompt_path)
        except OSError:
            pass

    elapsed = time.perf_counter() - t0
    if proc.returncode != 0:
        return {
            "status": "exit_nonzero",
            "returncode": proc.returncode,
            "stderr": proc.stderr[:500],
            "stdout_head": proc.stdout[:500],
            "elapsed_s": elapsed,
        }
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {
            "status": "json_decode_error",
            "error": str(exc),
            "stdout_head": proc.stdout[:500],
            "elapsed_s": elapsed,
        }
    return {"status": "ok", "result": result, "elapsed_s": elapsed}


# ── Per-needle execution ─────────────────────────────────────────────────

def run_one_needle(
    needle: dict,
    profile_key: str,
    mode: str,
    model: str,
    max_usd: float,
    helix_url: str,
    log: logging.Logger,
) -> dict:
    log.info("[%s %s] %s", profile_key, mode, needle["name"])

    # Retrieval probe — same as the existing bench, always logged for
    # comparability. The retr block tells us gold_delivered / MRR
    # independent of the LLM's behavior. For --mode none / oracle we
    # still run this against helix for cross-cell comparison; if the
    # server isn't running it will fail-soft.
    retr = retrieval_probe(
        needle["query"], needle["gold_source"], helix_url=helix_url,
    )

    # Build the context the model will see.
    ctx_fn = CONTEXT_FNS[mode]
    ctx_text, ctx_meta = ctx_fn(needle, helix_url)

    # Run the model. Tool-enabled modes (readgrep, both) get default
    # built-in tools + broad --add-dir access; other modes get --tools "".
    if mode in TOOL_ENABLED_MODES:
        claude_r = run_claude_with_tools(needle["query"], ctx_text, model, max_usd, log)
    else:
        claude_r = run_claude_no_tools(needle["query"], ctx_text, model, max_usd, log)

    answer_text = ""
    tokens: dict = {}
    cost_usd = None
    if claude_r["status"] == "ok":
        res = claude_r["result"]
        answer_text = (
            res.get("result")
            or res.get("answer")
            or res.get("content")
            or ""
        )
        if "usage" in res and isinstance(res["usage"], dict):
            for k, v in res["usage"].items():
                tokens[k] = v
        cost_usd = (
            res.get("total_cost_usd")
            or res.get("total_cost")
            or res.get("cost_usd")
        )

    score = score_answer(answer_text, needle["accept"])

    record = {
        "profile": profile_key,
        "mode": mode,
        "needle": needle["name"],
        "query": needle["query"],
        "expected": needle["expected"],
        "accept": needle["accept"],
        "gold_source": needle["gold_source"],
        "retrieval": retr,
        "context": ctx_meta,
        "claude_status": claude_r["status"],
        "claude_elapsed_s": claude_r.get("elapsed_s"),
        "answer_text": answer_text[:2000],
        "tokens": tokens,
        "cost_usd": cost_usd,
        "score": score["score"],
        "score_reason": score["reason"],
        "score_matched_token": score.get("matched_token"),
    }
    if claude_r["status"] != "ok":
        record["claude_debug"] = {
            k: v for k, v in claude_r.items() if k != "result"
        }

    in_tok = tokens.get("input_tokens", 0)
    out_tok = tokens.get("output_tokens", 0)
    cache_r = tokens.get("cache_read_input_tokens", 0)
    cache_w = tokens.get("cache_creation_input_tokens", 0)
    log.info(
        "  retr=%s ctx=%d score=%+d cost=$%s in=%d out=%d cache_r=%d cache_w=%d",
        ("#%d" % retr["gold_rank"]) if retr.get("gold_delivered") else (
            "miss" if retr.get("status") == "ok" else "skip"
        ),
        ctx_meta.get("chars", 0),
        score["score"],
        f"{cost_usd:.4f}" if cost_usd is not None else "?",
        in_tok, out_tok, cache_r, cache_w,
    )
    return record


# ── Profile runner ───────────────────────────────────────────────────────

def select_profiles(
    fixtures: dict[str, Fixture],
    only: list[str] | None,
    skip: list[str] | None,
    include_untrusted: bool,
) -> list[str]:
    keys = list(fixtures.keys())
    if only:
        keys = [k for k in keys if k in only]
    if skip:
        keys = [k for k in keys if k not in skip]
    if not include_untrusted:
        keys = [k for k in keys if k not in UNTRUSTED_PROFILES or (only and k in only)]
    return keys


def run_profile(
    fixture: Fixture | None,
    profile_key: str,
    mode: str,
    model: str,
    max_usd: float,
    helix_url: str,
    run_dir: Path,
    log: logging.Logger,
    max_needles: int | None = None,
) -> dict:
    log.info("  running profile=%s ctx_mode=%s%s",
             profile_key, mode,
             f" (helix-served from {fixture.db})" if fixture else " (no helix)")

    profile_jsonl = run_dir / f"{profile_key}.jsonl"
    records = []
    needles = NEEDLES if max_needles is None else NEEDLES[:max_needles]

    with profile_jsonl.open("w", encoding="utf-8") as fh:
        for idx, needle in enumerate(needles, 1):
            log.info("[%s %s %d/%d] %s",
                     profile_key, mode, idx, len(needles), needle["name"])
            rec = run_one_needle(
                needle, profile_key, mode, model, max_usd, helix_url, log,
            )
            records.append(rec)
            fh.write(json.dumps(rec) + "\n")
            fh.flush()

    # Per-profile summary
    correct = sum(1 for r in records if r["score"] == 1)
    abstain = sum(1 for r in records if r["score"] == 0)
    wrong = sum(1 for r in records if r["score"] == -1)
    gold_delivered = sum(1 for r in records
                          if r["retrieval"].get("gold_delivered"))
    mrrs = [
        1.0 / (r["retrieval"]["gold_rank"] + 1)
        for r in records
        if r["retrieval"].get("gold_delivered")
        and r["retrieval"].get("gold_rank") is not None
    ]
    mrr = round(sum(mrrs) / len(records), 4) if records else 0.0
    total_cost = sum(r.get("cost_usd") or 0.0 for r in records)
    ctx_chars_avg = (
        sum(r["context"].get("chars", 0) for r in records) / len(records)
        if records else 0
    )

    log.info(
        "  PROFILE %s: correct=%d abstain=%d wrong=%d "
        "retr_hit=%d/%d mrr=%.3f ctx_avg=%.0fc cost=$%.4f",
        profile_key, correct, abstain, wrong,
        gold_delivered, len(records), mrr, ctx_chars_avg, total_cost,
    )

    return {
        "needles_run": len(records),
        "answers": {
            "correct": correct,
            "abstain": abstain,
            "wrong": wrong,
            "score_sum": correct - wrong,
            "score_normalized": round((correct - wrong) / max(len(records), 1), 4),
        },
        "retrieval": {
            "gold_delivered_count": gold_delivered,
            "gold_delivered_rate": round(gold_delivered / max(len(records), 1), 4),
            "mrr": mrr,
        },
        "context": {
            "mode": mode,
            "avg_chars": round(ctx_chars_avg, 0),
        },
        "total_cost_usd": round(total_cost, 4),
    }


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=list(CONTEXT_FNS.keys()), required=True,
                        help="Context source: none|helix|oracle (M0|M2|M3)")
    parser.add_argument("--only", help="Comma-separated profiles to run")
    parser.add_argument("--skip", help="Comma-separated profiles to skip")
    parser.add_argument("--include-untrusted", action="store_true",
                        help="Include xl-sharded (corrupt fixture; #133)")
    parser.add_argument("--model", default="haiku",
                        help="claude -p --model. Default: haiku")
    parser.add_argument("--max-usd", type=float, default=0.30,
                        help="Per-question budget cap")
    parser.add_argument("--max-needles", type=int, default=None,
                        help="Optional cap on # needles per profile (smoke runs)")
    parser.add_argument("--no-server", action="store_true",
                        help="Don't start helix uvicorn. Required for mode=none "
                             "and mode=oracle (no helix needed). Mode=helix MUST "
                             "have a server — pass --external-server if you've "
                             "started one already.")
    parser.add_argument("--external-server", action="store_true",
                        help="A helix server is already running at --helix-url; "
                             "don't manage uvicorn here. Implies --no-server "
                             "(no spawn), but the retrieval probe will still hit "
                             "--helix-url.")
    parser.add_argument("--helix-url", default=HELIX_URL,
                        help=f"Helix HTTP URL (default: {HELIX_URL})")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=11437, type=int)
    parser.add_argument("--health-timeout", default=180.0, type=float)
    args = parser.parse_args()

    only = [s.strip() for s in args.only.split(",")] if args.only else None
    skip = [s.strip() for s in args.skip.split(",")] if args.skip else None

    if not MANIFEST.exists():
        print(f"ERROR: manifest not found at {MANIFEST}", file=sys.stderr)
        return 2

    # Create empty cwd if it doesn't exist; do NOT clean it (the user may
    # have written a priming CLAUDE.md there for H1-style tests). Warn if
    # markdown is present so the operator can decide.
    CLEAN_CWD.mkdir(parents=True, exist_ok=True)
    md_files = [
        p.name for p in CLEAN_CWD.iterdir()
        if p.is_file() and p.suffix.lower() in {".md", ".mdc"}
    ]
    if md_files:
        print(
            f"WARN: {CLEAN_CWD} contains markdown that claude -p may auto-load: "
            f"{md_files} — remove or move aside if you want a true clean-cwd run.",
            file=sys.stderr,
        )

    # Set up run dir + logger
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_ROOT / f"injected_{args.mode}_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "run.log"

    logger = logging.getLogger("bench.context_injected")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            datefmt="%H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt); logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt); logger.addHandler(sh)

    logger.info("RUN START run_dir=%s", run_dir)
    logger.info("mode=%s model=%s max_usd_per_question=%.2f",
                args.mode, args.model, args.max_usd)
    logger.info("clean_cwd=%s helix_url=%s", CLEAN_CWD, args.helix_url)

    # Load fixtures
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    targets = manifest["targets"]
    all_keys = list(targets.keys())

    if only:
        profile_keys = [k for k in all_keys if k in only]
    else:
        profile_keys = list(all_keys)
    if skip:
        profile_keys = [k for k in profile_keys if k not in skip]
    if not args.include_untrusted:
        profile_keys = [
            k for k in profile_keys
            if k not in UNTRUSTED_PROFILES or (only and k in only)
        ]
    if not profile_keys:
        logger.error("No profiles selected after --only/--skip filters")
        return 2
    logger.info("profiles in run: %s", profile_keys)

    # Modes that inject helix /context need a running helix server.
    needs_server = (args.mode in {"helix", "both"}) and not (args.no_server or args.external_server)

    summary = {
        "run_dir": str(run_dir),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "model": args.model,
        "max_usd_per_question": args.max_usd,
        "max_needles": args.max_needles,
        "clean_cwd": str(CLEAN_CWD),
        "helix_url": args.helix_url,
        "profiles": {},
    }

    def _run_one_profile(key: str, fixture: Fixture | None) -> None:
        try:
            summary["profiles"][key] = run_profile(
                fixture=fixture,
                profile_key=key,
                mode=args.mode,
                model=args.model,
                max_usd=args.max_usd,
                helix_url=args.helix_url,
                run_dir=run_dir,
                log=logger,
                max_needles=args.max_needles,
            )
        except Exception:
            logger.exception("profile %s failed", key)

    try:
        if needs_server:
            uvicorn_log = run_dir / "uvicorn.log"
            logger.info("Starting BenchServer (manages uvicorn) — log=%s", uvicorn_log)
            with BenchServer(
                host=args.host,
                port=args.port,
                health_timeout_s=args.health_timeout,
                log_to=uvicorn_log,
            ) as srv:
                for key in profile_keys:
                    target = targets[key]
                    logger.info("=" * 60)
                    logger.info("PROFILE: %s (mode=%s)", key, target.get("mode"))
                    if target.get("status") == "missing":
                        logger.warning("  target missing on disk, skipping")
                        continue
                    fixture = Fixture(
                        name=key,
                        db=str(target["path"]).replace("\\", "/"),
                        sharded=(target.get("mode") == "sharded"),
                        read_only=True,
                    )
                    try:
                        swap = srv.switch(fixture)
                    except Exception as exc:
                        logger.error("  switch failed: %s", exc)
                        summary["profiles"][key] = {"swap_error": str(exc)}
                        continue
                    logger.info(
                        "  %s in %.2fs (genes=%d, pid=%s)",
                        swap.mechanism, swap.elapsed_s, swap.genes, swap.server_pid,
                    )
                    _run_one_profile(key, fixture)
        else:
            # No server. mode=none or mode=oracle — helix-free path.
            for key in profile_keys:
                target = targets[key]
                logger.info("=" * 60)
                logger.info("PROFILE: %s (mode=%s, no server)",
                            key, target.get("mode"))
                if target.get("status") == "missing":
                    logger.warning("  target missing on disk, skipping")
                    continue
                _run_one_profile(key, None)
    finally:
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary_path = run_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        logger.info("=" * 60)
        logger.info("RUN DONE. summary: %s", summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
