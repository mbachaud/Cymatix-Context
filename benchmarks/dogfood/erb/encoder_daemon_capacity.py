"""Encoder daemon capacity probe (Fork 1 slice 1) — bundles/s at N clients.

Spawns ``python -m cymatix_context.encoder_daemon`` as a bare subprocess and
hammers its ``/encode/bundle`` endpoint with 1/2/4/8 concurrent clients,
recording a receipt that ``benchmarks/dogfood/check_encoder_receipts.py``
(``--kind encoder_capacity``) gates against a bundles/s floor.

Rerank family (#341): after each level's bundle phase fully joins (wall_s
already captured), a SEPARATE barrier-synced phase has every client POST
``--rerank-pairs`` (default 50) texts to ``/encode/rerank`` once. This keeps
bundle wall-clock — and therefore ``bundles_per_s`` and its 3.5 CPU / 30 GPU
bars — completely unpolluted by rerank load; rerank throughput
(``rerank_pairs_per_s``) is reported informationally only, and is a
sequential-latency proxy (sum of each client's own latency, NOT the
n_clients-concurrent phase's wall-clock) — do not compare it against
``bundles_per_s`` as if the two were measured the same way. Rerank errors
fold into the level's existing ``errors`` field, so the checker's binding
``errors_zero`` gate covers rerank failures with no checker change.

This is NOT ``BenchServer`` (``benchmarks/bench_orchestrator.py``) — that
class spawns uvicorn for the whole backend ASGI app against a fixture DB.
The encoder daemon is a single stateless process with no DB and no fixture,
so this module copies exactly the idioms that still apply — spawn-token
identity on the readiness poll, ``taskkill /T /F`` process-tree cleanup,
``connect_ex``-based wait-for-port-free, ``CREATE_NO_WINDOW`` — rather than
importing BenchServer and fighting its uvicorn/fixture assumptions.

Receipt JSON (``kind: "encoder_capacity"``)::

    {
      "kind": "encoder_capacity",
      "profile_label": "cpu" | "gpu" | ...,
      "python": "<interpreter used to spawn the daemon>",
      "port": 11439,
      "torch_version": "...",       # from GET /health .capacity.torch_version
      "capacity": {...},            # the daemon's full /health .capacity block
      "spawn_token_verified": true,
      "levels": [
        {"n_clients": 1, "bundles_total": 50, "errors": 0, "wall_s": 14.3,
         "bundles_per_s": 3.5, "latency_p50_ms": 280.1, "latency_p95_ms": 310.4,
         "latency_max_ms": 340.2,
         # rerank_* keys (#341) present only when --rerank-pairs > 0:
         "rerank_scores_total": 50,
         "rerank_pairs_per_s": 210.4,  # sequential-latency proxy, not phase wall-clock
         "rerank_latency_p50_ms": 118.2, "rerank_latency_p95_ms": 140.6},
        ...
      ],
      "generated_at": "2026-08-05T12:00:00+00:00",
      "env": {"CYMATIX_ENCODER_BATCH_WINDOW_MS": "8"}   # only if set
    }

Windows rules throughout: ``CREATE_NO_WINDOW`` on the daemon subprocess,
``taskkill /T /F`` cleanup (rc 128 is benign — process already gone),
explicit ``timeout=`` on every HTTP call.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# benchmarks/dogfood/erb/encoder_daemon_capacity.py -> repo root is parents[3].
REPO_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_PORT = 11439
DEFAULT_CLIENTS = "1,2,4,8"
DEFAULT_BUNDLES_PER_CLIENT = 50
DEFAULT_WARMUP_BUNDLES = 5
DEFAULT_RERANK_PAIRS = 50
DEFAULT_READY_TIMEOUT_S = 180.0

PORT_FREE_TIMEOUT_S = 15.0
PORT_FREE_POLL_S = 0.25
READY_POLL_S = 0.5
READY_REQUEST_TIMEOUT_S = 5.0
WARMUP_REQUEST_TIMEOUT_S = 30.0
LEVEL_REQUEST_TIMEOUT_S = 60.0

SPAWN_TOKEN_ENV = "CYMATIX_ENCODER_SPAWN_TOKEN"

# Windows: prevent console-window flash on subprocess spawn (per CLAUDE.md
# global "Subprocess Safety (Windows)" rule) — same constant as
# bench_orchestrator.py's ``_NO_WINDOW``.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ProbeError(RuntimeError):
    """Fatal probe failure — printed and turned into exit code 1."""


# ── pure aggregation (unit-tested; no network, no subprocess) ────────


def _nearest_rank_pct(sorted_values: Sequence[float], p: float) -> Optional[float]:
    """Nearest-rank percentile over an already-sorted sequence, or ``None`` if empty."""
    n = len(sorted_values)
    if n == 0:
        return None
    idx = min(n - 1, max(0, round(p * (n - 1))))
    return round(sorted_values[idx], 3)


def aggregate_level(
    n_clients: int,
    per_request_ms: Sequence[float],
    errors: int,
    wall_s: float,
    *,
    rerank_ms: Optional[Sequence[float]] = None,
    rerank_errors: int = 0,
    rerank_pairs_per_request: int = 0,
) -> Dict[str, Any]:
    """Raw per-request latencies (ms, successful requests only) -> level stats.

    Pure function factored out of ``run_level`` specifically so it can be
    unit-tested without spawning anything (the capacity probe itself gets no
    non-live test — see ``tests/test_check_encoder_receipts.py``).

    ``bundles_total`` counts every attempted BUNDLE request (success + error).
    ``bundles_per_s = bundles_total / wall_s``; a non-positive ``wall_s``
    (e.g. every request failed before any timing could elapse) degrades to
    ``0.0`` rather than raising or producing inf/nan. Latency percentiles are
    computed only over the successful timings; with none, all three latency
    fields report ``None``.

    ``rerank_ms``/``rerank_errors``/``rerank_pairs_per_request`` (#341) carry
    the rerank family's SEPARATE timed phase (run strictly after the bundle
    phase — see ``run_level``), so they never touch ``bundles_total`` /
    ``bundles_per_s`` / ``wall_s`` above. ``rerank_ms is None`` (the default)
    omits every ``rerank_*`` key from the row, keeping old callers/receipts
    byte-identical. When provided, ``rerank_errors`` folds into the level's
    existing ``errors`` field so the binding ``errors_zero`` checker gate
    covers rerank failures with no checker change — but ``bundles_total``
    stays a pure bundle-phase count.
    """
    bundles_total = len(per_request_ms) + int(errors)
    bundles_per_s = round(bundles_total / wall_s, 4) if wall_s > 0 else 0.0

    sorted_ms = sorted(per_request_ms)

    row: Dict[str, Any] = {
        "n_clients": n_clients,
        "bundles_total": bundles_total,
        "errors": int(errors) + int(rerank_errors),
        "wall_s": round(wall_s, 4),
        "bundles_per_s": bundles_per_s,
        "latency_p50_ms": _nearest_rank_pct(sorted_ms, 0.50),
        "latency_p95_ms": _nearest_rank_pct(sorted_ms, 0.95),
        "latency_max_ms": round(sorted_ms[-1], 3) if sorted_ms else None,
    }

    if rerank_ms is not None:
        sorted_rerank_ms = sorted(rerank_ms)
        rerank_success = len(sorted_rerank_ms)
        rerank_scores_total = rerank_success * int(rerank_pairs_per_request)
        # Sequential-latency proxy, NOT phase wall-clock: sums each client's
        # own latency rather than timing the n_clients-concurrent phase, so
        # this understates true concurrent throughput by roughly a factor of
        # n_clients — informational only, never compare it against bundles_per_s.
        rerank_total_s = sum(sorted_rerank_ms) / 1000.0
        rerank_pairs_per_s = (
            round(rerank_scores_total / rerank_total_s, 4) if rerank_total_s > 0 else 0.0
        )
        row["rerank_scores_total"] = rerank_scores_total
        row["rerank_pairs_per_s"] = rerank_pairs_per_s
        row["rerank_latency_p50_ms"] = _nearest_rank_pct(sorted_rerank_ms, 0.50)
        row["rerank_latency_p95_ms"] = _nearest_rank_pct(sorted_rerank_ms, 0.95)

    return row


def _client_text(client_idx: int, req_idx: int) -> str:
    """~200-char deterministic text, distinct per (client, request)."""
    seed = f"client={client_idx} request={req_idx}"
    filler = (
        "cymatix encoder daemon capacity probe payload padding text used to "
        "reach a realistic bundle length for the dense/splade/sema encode "
        "path exercised by this level "
    )
    text = f"{seed} {filler}"
    if len(text) < 200:
        text = (text * 3)[:200]
    return text[:200]


# ── HTTP (stdlib urllib, explicit timeouts throughout) ────────────────


def _get_json(url: str, timeout: float) -> Tuple[int, Any]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except Exception:
            body = None
        return exc.code, body
    except Exception as exc:
        raise ProbeError(f"GET {url} failed: {exc}") from exc


def _post_json(url: str, payload: dict, timeout: float) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ── port + process management (copies BenchServer's idioms) ──────────


def _wait_port_free(host: str, port: int, timeout_s: float = PORT_FREE_TIMEOUT_S) -> None:
    """Block until ``(host, port)`` refuses connections — see BenchServer._wait_port_free."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            if sock.connect_ex((host, port)) != 0:
                return
        time.sleep(PORT_FREE_POLL_S)
    raise ProbeError(
        f"port {host}:{port} still occupied after {timeout_s}s; refusing to "
        "spawn/report on a doomed bind"
    )


def _taskkill_tree(pid: int) -> None:
    """Windows-only: kill ``pid`` and its whole process tree (see BenchServer)."""
    try:
        res = subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, text=True,
            creationflags=_NO_WINDOW,
            timeout=15.0,
        )
        if res.returncode != 0:
            # rc 128 == process not found; benign if it already exited.
            print(
                f"warning: taskkill /T /F /PID {pid} exited rc={res.returncode}: "
                f"{(res.stderr or res.stdout or '').strip()}",
                file=sys.stderr,
            )
    except subprocess.TimeoutExpired:
        print(f"warning: taskkill /T /F /PID {pid} timed out", file=sys.stderr)
    except Exception as exc:
        print(f"warning: taskkill /T /F /PID {pid} raised: {exc}", file=sys.stderr)


def _tail_log(log_path: Path, n: int) -> str:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"<could not read log {log_path}: {exc}>"
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def spawn_daemon(python_exe: str, port: int, log_path: Path) -> Tuple[subprocess.Popen, str, Any]:
    """Spawn the encoder daemon; fresh spawn token, PYTHONPATH-pinned, no console flash."""
    env = os.environ.copy()
    # Belt and braces: the daemon guards itself via mark_encoder_daemon_process(),
    # but a spawned process must never even see a stale CYMATIX_ENCODER_URL.
    env.pop("CYMATIX_ENCODER_URL", None)
    env["PYTHONHASHSEED"] = "0"

    spawn_token = uuid.uuid4().hex
    env[SPAWN_TOKEN_ENV] = spawn_token

    root_str = str(REPO_ROOT)
    existing = env.get("PYTHONPATH", "")
    if existing:
        # Avoid an obvious double-prepend on repeated invocations that
        # inherit an already-pinned PYTHONPATH from a parent process.
        head = existing.split(os.pathsep, 1)[0]
        if head != root_str:
            env["PYTHONPATH"] = root_str + os.pathsep + existing
    else:
        env["PYTHONPATH"] = root_str

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_path.open("ab")

    cmd = [python_exe, "-m", "cymatix_context.encoder_daemon", "--port", str(port)]
    try:
        proc = subprocess.Popen(
            cmd, env=env, cwd=root_str,
            stdout=log_fh, stderr=log_fh, stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
    except BaseException:
        # The handle is opened before the spawn, so a failing Popen (bad
        # interpreter path, exec error) would otherwise leak it: the caller
        # never receives it and so cannot close it in its own finally.
        log_fh.close()
        raise
    return proc, spawn_token, log_fh


def wait_ready(
    host: str, port: int, spawn_token: str, proc: subprocess.Popen,
    log_path: Path, timeout_s: float,
) -> Dict[str, Any]:
    """Poll GET /ready until 200 with a matching spawn_token, or fail loudly."""
    deadline = time.time() + timeout_s
    url = f"http://{host}:{port}/ready"
    last_status: Optional[int] = None

    while time.time() < deadline:
        rc = proc.poll()
        if rc is not None:
            raise ProbeError(
                f"encoder daemon process exited early (rc={rc}) before /ready; "
                f"last log lines:\n{_tail_log(log_path, 20)}"
            )
        try:
            status, body = _get_json(url, timeout=READY_REQUEST_TIMEOUT_S)
        except ProbeError:
            time.sleep(READY_POLL_S)
            continue
        last_status = status
        if status == 200:
            got_token = (body or {}).get("spawn_token")
            if got_token != spawn_token:
                raise ProbeError(
                    f"/ready answered with spawn_token={got_token!r}, expected "
                    f"{spawn_token!r} — a stale daemon is holding port {port}; "
                    "aborting rather than probing the wrong process"
                )
            return body
        time.sleep(READY_POLL_S)

    raise ProbeError(
        f"encoder daemon did not become ready within {timeout_s}s "
        f"(last /ready status={last_status}); last log lines:\n{_tail_log(log_path, 20)}"
    )


def fetch_health(host: str, port: int) -> Dict[str, Any]:
    status, body = _get_json(f"http://{host}:{port}/health", timeout=READY_REQUEST_TIMEOUT_S)
    if status != 200 or not isinstance(body, dict):
        raise ProbeError(f"GET /health returned status={status}, body={body!r}")
    return body


def warmup(host: str, port: int, n: int) -> None:
    url = f"http://{host}:{port}/encode/bundle"
    for i in range(n):
        text = _client_text(-1, i)  # client_idx=-1: distinct namespace from level texts
        _post_json(url, {"text": text, "task": "query", "top_k": 128},
                   timeout=WARMUP_REQUEST_TIMEOUT_S)


# ── the concurrency levels ────────────────────────────────────────────


def _run_rerank_phase(
    host: str, port: int, n_clients: int, rerank_pairs: int,
) -> Tuple[List[float], int]:
    """Barrier-synced rerank phase: one ``/encode/rerank`` POST per client.

    Called from ``run_level`` only AFTER the bundle phase's threads have
    already been started, joined, and had their wall-clock captured — so
    this phase can never pollute ``bundles_per_s`` (design: "rerank load
    runs as a separate timed phase after the bundle phase within each
    level"). Query/pair texts reuse ``_client_text`` with a request-index
    namespace (``-1`` for the query, ``100000+``) distinct from the bundle
    phase's ``0..bundles_per_client-1`` so nothing collides.
    """
    barrier = threading.Barrier(n_clients)
    per_client_ms: List[List[float]] = [[] for _ in range(n_clients)]
    per_client_errors = [0] * n_clients
    url = f"http://{host}:{port}/encode/rerank"

    def worker(idx: int) -> None:
        barrier.wait()
        query = _client_text(idx, -1)
        texts = [_client_text(idx, 100_000 + i) for i in range(rerank_pairs)]
        t0 = time.monotonic()
        try:
            _post_json(url, {"query": query, "texts": texts}, timeout=LEVEL_REQUEST_TIMEOUT_S)
        except Exception:
            per_client_errors[idx] += 1
        else:
            per_client_ms[idx].append((time.monotonic() - t0) * 1000.0)

    threads = [
        threading.Thread(target=worker, args=(i,), name=f"encoder-probe-rerank-{i}")
        for i in range(n_clients)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    flat_ms = [ms for lst in per_client_ms for ms in lst]
    total_errors = sum(per_client_errors)
    return flat_ms, total_errors


def run_level(
    host: str, port: int, n_clients: int, bundles_per_client: int,
    rerank_pairs: int = 0,
) -> Dict[str, Any]:
    """N barrier-synced threads POST /encode/bundle; aggregate via aggregate_level.

    When ``rerank_pairs > 0``, a SECOND barrier-synced phase (``_run_rerank_
    phase``) runs after the bundle phase has fully joined and its wall_s has
    already been captured — the bundle phase's timing is untouched by rerank
    load either way (design's "bar-preserving" requirement).
    """
    barrier = threading.Barrier(n_clients)
    per_client_ms: List[List[float]] = [[] for _ in range(n_clients)]
    per_client_errors = [0] * n_clients
    url = f"http://{host}:{port}/encode/bundle"

    def worker(idx: int) -> None:
        barrier.wait()
        for req_idx in range(bundles_per_client):
            text = _client_text(idx, req_idx)
            t0 = time.monotonic()
            try:
                _post_json(url, {"text": text, "task": "query", "top_k": 128},
                           timeout=LEVEL_REQUEST_TIMEOUT_S)
            except Exception:
                per_client_errors[idx] += 1
                continue
            per_client_ms[idx].append((time.monotonic() - t0) * 1000.0)

    threads = [
        threading.Thread(target=worker, args=(i,), name=f"encoder-probe-client-{i}")
        for i in range(n_clients)
    ]
    wall_start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall_s = time.monotonic() - wall_start

    flat_ms = [ms for lst in per_client_ms for ms in lst]
    total_errors = sum(per_client_errors)

    rerank_ms: Optional[List[float]] = None
    rerank_errors = 0
    if rerank_pairs > 0:
        rerank_ms, rerank_errors = _run_rerank_phase(host, port, n_clients, rerank_pairs)

    return aggregate_level(
        n_clients, flat_ms, total_errors, wall_s,
        rerank_ms=rerank_ms, rerank_errors=rerank_errors,
        rerank_pairs_per_request=rerank_pairs,
    )


# ── CLI ────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Encoder daemon capacity probe: bundles/s at N concurrent clients.",
    )
    ap.add_argument(
        "--python", default=sys.executable,
        help="interpreter used to SPAWN the daemon (default: this interpreter; "
             "GPU runs pass f:/tmp/venv-cuda/Scripts/python.exe)",
    )
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument(
        "--clients", default=DEFAULT_CLIENTS,
        help="comma-separated client counts (default: %(default)s)",
    )
    ap.add_argument("--bundles-per-client", type=int, default=DEFAULT_BUNDLES_PER_CLIENT)
    ap.add_argument("--warmup-bundles", type=int, default=DEFAULT_WARMUP_BUNDLES)
    ap.add_argument(
        "--rerank-pairs", type=int, default=DEFAULT_RERANK_PAIRS,
        help="texts per /encode/rerank call in the post-bundle rerank phase "
             "each client runs after its bundle loop within a level (default: "
             "%(default)s; 0 disables the rerank phase entirely).",
    )
    ap.add_argument(
        "--profile-label", default="cpu",
        help="free text recorded in the receipt, e.g. 'cpu' / 'gpu' (default: %(default)s)",
    )
    ap.add_argument("--ready-timeout", type=float, default=DEFAULT_READY_TIMEOUT_S)
    ap.add_argument(
        "--out", default=None,
        help="receipt path (default: benchmarks/dogfood/erb/receipts/"
             "encoder_daemon_capacity_<profile_label>.json)",
    )
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    host = "127.0.0.1"
    client_counts = [int(x) for x in args.clients.split(",") if x.strip()]

    out_path = Path(args.out) if args.out else (
        REPO_ROOT / "benchmarks" / "dogfood" / "erb" / "receipts"
        / f"encoder_daemon_capacity_{args.profile_label}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = out_path.with_name(out_path.stem + "_daemon.log")

    proc: Optional[subprocess.Popen] = None
    log_fh = None
    try:
        _wait_port_free(host, args.port)
        proc, spawn_token, log_fh = spawn_daemon(args.python, args.port, log_path)

        wait_ready(host, args.port, spawn_token, proc, log_path, args.ready_timeout)
        health = fetch_health(host, args.port)
        capacity = health.get("capacity") or {}

        warmup(host, args.port, args.warmup_bundles)

        levels: List[Dict[str, Any]] = []
        for n in client_counts:
            level = run_level(host, args.port, n, args.bundles_per_client, args.rerank_pairs)
            levels.append(level)
            msg = (
                f"n_clients={n} bundles_per_s={level['bundles_per_s']} "
                f"errors={level['errors']} p50_ms={level['latency_p50_ms']} "
                f"p95_ms={level['latency_p95_ms']}"
            )
            if "rerank_pairs_per_s" in level:
                msg += (
                    f" rerank_pairs_per_s={level['rerank_pairs_per_s']} "
                    f"rerank_p50_ms={level['rerank_latency_p50_ms']}"
                )
            print(msg)

        env_block: Dict[str, str] = {}
        if "CYMATIX_ENCODER_BATCH_WINDOW_MS" in os.environ:
            env_block["CYMATIX_ENCODER_BATCH_WINDOW_MS"] = os.environ["CYMATIX_ENCODER_BATCH_WINDOW_MS"]

        receipt = {
            "kind": "encoder_capacity",
            "profile_label": args.profile_label,
            "python": args.python,
            "port": args.port,
            "torch_version": capacity.get("torch_version"),
            "capacity": capacity,
            "spawn_token_verified": True,
            "levels": levels,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "env": env_block,
        }
        out_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        print(f"receipt written: {out_path}")
        return 0
    except ProbeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if log_fh is not None:
            log_fh.close()
        if proc is not None and proc.poll() is None:
            _taskkill_tree(proc.pid)
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                print(f"warning: daemon pid={proc.pid} still alive after taskkill",
                      file=sys.stderr)
        try:
            _wait_port_free(host, args.port)
            print(f"port {args.port} freed")
        except ProbeError as exc:
            print(f"warning: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
