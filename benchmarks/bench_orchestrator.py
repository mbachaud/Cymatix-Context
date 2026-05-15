"""Bench orchestrator — manage uvicorn lifecycle + hot-swap across the
6-fixture matrix (4 monolithic blobs + 2 sharded) without manual server
restarts between runs.

Two transition shapes:

- **Same-mode (blob→blob, sharded→sharded)**: POST /admin/swap-db.
  Atomic, ~milliseconds, no process restart. Available since PR #91.
- **Cross-mode (blob↔sharded)**: full uvicorn restart with the
  ``HELIX_USE_SHARDS`` env var set/unset. ``open_read_source()`` in
  ``helix_context/sharding.py`` reads that env at store-construction time;
  it can't be flipped mid-process.

The orchestrator hides this distinction from the bench code: callers just
say "switch to fixture X" and the orchestrator picks the right mechanism.

Usage as a library::

    from bench_orchestrator import BenchServer, Fixture

    with BenchServer() as srv:
        for fx in fixtures:
            srv.switch(fx)
            run_my_bench(srv.url)

Usage as a CLI (loops the matrix, invokes existing bench scripts per
fixture)::

    python benchmarks/bench_orchestrator.py \\
        --manifest benchmarks/fixtures.json \\
        --bench bench_needle_1000 \\
        --out results/

The manifest is a JSON list of fixtures::

    [
      {"name": "small",  "db": "F:/.../small.db",            "sharded": false},
      {"name": "medium", "db": "F:/.../medium.db",           "sharded": false},
      {"name": "large",  "db": "F:/.../large.db",            "sharded": false},
      {"name": "xl",     "db": "F:/.../xl.db",               "sharded": false},
      {"name": "medium-sharded", "db": "F:/.../medium-sharded/main.genome.db", "sharded": true},
      {"name": "xl-sharded",     "db": "F:/.../xl-sharded/main.genome.db",     "sharded": true}
    ]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("bench.orchestrator")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11437
DEFAULT_HEALTH_TIMEOUT_S = 60.0
DEFAULT_HEALTH_POLL_S = 0.5
DEFAULT_SHUTDOWN_TIMEOUT_S = 10.0
DEFAULT_SWAP_TIMEOUT_S = 60.0
DEFAULT_REQUEST_TIMEOUT_S = 15.0

# Windows: prevent console-window flash on subprocess spawn (per CLAUDE.md
# global "Subprocess Safety (Windows)" rule).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class Fixture:
    """One row of the bench fixture matrix.

    ``db`` is the SQLite path to swap in. For sharded fixtures, this is the
    routing DB path (``.../main.genome.db``); ``open_read_source`` in
    ``helix_context/sharding.py`` detects the basename and dispatches to
    ``ShardedGenomeAdapter`` when ``HELIX_USE_SHARDS=1``.

    ``read_only`` defaults True so a bench run can't accidentally mutate
    the fixture (PR #91 ``read_only`` guard).
    """
    name: str
    db: str
    sharded: bool = False
    read_only: bool = True
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass
class SwapResult:
    """Outcome of switching to a new fixture."""
    fixture: Fixture
    mechanism: str        # "hot_swap" | "restart" | "start"
    elapsed_s: float
    genes: int
    server_pid: Optional[int]


class BenchServerError(RuntimeError):
    pass


class BenchServer(AbstractContextManager["BenchServer"]):
    """Manage a uvicorn process + transparent hot-swap across fixtures.

    Lifecycle::

        srv = BenchServer()
        srv.start(initial_fixture)   # boots uvicorn
        for fx in remaining_fixtures:
            srv.switch(fx)           # picks hot-swap or restart automatically
        srv.stop()

    Or as a context manager::

        with BenchServer() as srv:
            srv.switch(first_fixture)
            ...
        # stop() runs on __exit__ regardless of exceptions

    The server is started under the current Python interpreter so the
    venv/conda env in use carries over without explicit configuration.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        python: Optional[str] = None,
        app: str = "helix_context._asgi:app",
        health_timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S,
        shutdown_timeout_s: float = DEFAULT_SHUTDOWN_TIMEOUT_S,
        log_to: Optional[Path] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.python = python or sys.executable
        self.app = app
        self.health_timeout_s = health_timeout_s
        self.shutdown_timeout_s = shutdown_timeout_s
        self.log_to = Path(log_to) if log_to else None

        self._proc: Optional[subprocess.Popen] = None
        self._current: Optional[Fixture] = None
        self._log_fh = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def current(self) -> Optional[Fixture]:
        return self._current

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    # ── Public API ────────────────────────────────────────────────────

    def start(self, fixture: Fixture) -> SwapResult:
        """Boot uvicorn pointed at ``fixture``.

        Idempotent: if already running and the requested fixture matches
        the current mode, this delegates to ``switch`` (hot-swap path).
        """
        if self._proc is not None and self._proc.poll() is None:
            return self.switch(fixture)

        t0 = time.time()
        self._spawn(fixture)
        self._wait_healthy()

        # After spawn, set the active fixture via hot-swap so we always
        # know the active path (the initial app boot may pick whatever
        # the config file points at).
        swap = self._post_swap(fixture)
        elapsed = time.time() - t0
        self._current = fixture
        return SwapResult(
            fixture=fixture, mechanism="start",
            elapsed_s=round(elapsed, 3),
            genes=int(swap.get("genes", 0)),
            server_pid=self.pid,
        )

    def switch(self, fixture: Fixture) -> SwapResult:
        """Transition to ``fixture``. Picks hot-swap or restart.

        Cross-mode transitions (sharded ↔ blob) require restarting uvicorn
        because ``HELIX_USE_SHARDS`` is read at process startup by
        ``open_read_source``. Same-mode transitions use the atomic
        ``/admin/swap-db`` endpoint.
        """
        if self._proc is None or self._proc.poll() is not None:
            return self.start(fixture)

        prev = self._current
        if prev is not None and prev.sharded != fixture.sharded:
            return self._restart_for_fixture(fixture, prev)
        return self._hot_swap(fixture)

    def stop(self) -> None:
        """Terminate uvicorn. Best-effort; kills if graceful exit fails."""
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        if proc.poll() is not None:
            self._close_log()
            return
        try:
            if os.name == "nt":
                # CTRL_BREAK won't reach the child without
                # CREATE_NEW_PROCESS_GROUP at spawn time; terminate() maps
                # to TerminateProcess which is the right hammer on Windows.
                proc.terminate()
            else:
                proc.send_signal(signal.SIGINT)
            proc.wait(timeout=self.shutdown_timeout_s)
        except subprocess.TimeoutExpired:
            log.warning("uvicorn did not exit cleanly; killing pid=%s", proc.pid)
            proc.kill()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                log.warning("uvicorn kill timed out; abandoning pid=%s", proc.pid)
        except Exception:
            log.warning("uvicorn shutdown raised", exc_info=True)
        finally:
            self._close_log()
            self._current = None

    def health(self) -> dict[str, Any]:
        """Fetch ``/health``. Raises if unreachable."""
        return self._http("GET", "/health")

    # ── Context manager ───────────────────────────────────────────────

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ── Internals ─────────────────────────────────────────────────────

    def _spawn(self, fixture: Fixture) -> None:
        cmd = [
            self.python, "-m", "uvicorn", self.app,
            "--host", self.host, "--port", str(self.port),
        ]
        env = os.environ.copy()
        if fixture.sharded:
            env["HELIX_USE_SHARDS"] = "1"
        else:
            env.pop("HELIX_USE_SHARDS", None)
        # Pin PYTHONHASHSEED so set / dict iteration orders stay stable
        # across uvicorn re-spawns. Belt-and-suspenders defence on top of
        # the determinism fixes in helix_context (sorted expansion, lock
        # on last_query_scores, shard-name tiebreak): without it, bench
        # replays of the same query against the same fixture can drift
        # purely because the subprocess got a different hash seed.
        env.setdefault("PYTHONHASHSEED", "0")
        env.update(fixture.extra_env)

        if self.log_to:
            self.log_to.parent.mkdir(parents=True, exist_ok=True)
            self._log_fh = self.log_to.open("ab")
            stdout = stderr = self._log_fh
        else:
            stdout = stderr = subprocess.DEVNULL

        self._proc = subprocess.Popen(
            cmd, env=env, stdout=stdout, stderr=stderr,
            stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
            close_fds=(os.name != "nt"),
        )

    def _wait_healthy(self) -> None:
        deadline = time.time() + self.health_timeout_s
        last_err: Optional[Exception] = None
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                rc = self._proc.returncode
                raise BenchServerError(f"uvicorn exited during startup (rc={rc})")
            try:
                self.health()
                return
            except Exception as exc:
                last_err = exc
                time.sleep(DEFAULT_HEALTH_POLL_S)
        raise BenchServerError(
            f"uvicorn did not become healthy within {self.health_timeout_s}s "
            f"(last error: {last_err})"
        )

    def _hot_swap(self, fixture: Fixture) -> SwapResult:
        t0 = time.time()
        result = self._post_swap(fixture)
        self._current = fixture
        return SwapResult(
            fixture=fixture, mechanism="hot_swap",
            elapsed_s=round(time.time() - t0, 3),
            genes=int(result.get("genes", 0)),
            server_pid=self.pid,
        )

    def _restart_for_fixture(self, fixture: Fixture, prev: Fixture) -> SwapResult:
        log.info(
            "cross-mode transition (%s sharded=%s → %s sharded=%s); restarting uvicorn",
            prev.name, prev.sharded, fixture.name, fixture.sharded,
        )
        t0 = time.time()
        self.stop()
        self._spawn(fixture)
        self._wait_healthy()
        swap = self._post_swap(fixture)
        self._current = fixture
        return SwapResult(
            fixture=fixture, mechanism="restart",
            elapsed_s=round(time.time() - t0, 3),
            genes=int(swap.get("genes", 0)),
            server_pid=self.pid,
        )

    def _post_swap(self, fixture: Fixture) -> dict[str, Any]:
        payload = {"path": fixture.db, "read_only": fixture.read_only}
        try:
            return self._http(
                "POST", "/admin/swap-db", payload,
                timeout=DEFAULT_SWAP_TIMEOUT_S,
            )
        except Exception as exc:
            raise BenchServerError(
                f"hot-swap to {fixture.name} ({fixture.db}) failed: {exc}"
            ) from exc

    def _http(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> dict[str, Any]:
        url = f"{self.url}{path}"
        data: Optional[bytes] = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(exc)
            raise BenchServerError(f"{method} {path} → HTTP {exc.code}: {detail}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BenchServerError(f"{method} {path} → non-JSON response: {exc}") from exc

    def _close_log(self) -> None:
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None


# ── Manifest loading ─────────────────────────────────────────────────


def load_manifest(path: Path) -> list[Fixture]:
    """Parse a fixture manifest JSON file into ``Fixture`` instances.

    Each entry must have ``name`` and ``db``. Optional: ``sharded`` (bool,
    default False), ``read_only`` (bool, default True), ``env`` (dict of
    extra env vars to inject when this fixture is active).
    """
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise ValueError(f"manifest at {path} must be a JSON list of fixtures")
    fixtures = []
    for i, entry in enumerate(raw):
        if "name" not in entry or "db" not in entry:
            raise ValueError(f"fixture #{i} missing 'name' or 'db'")
        fixtures.append(Fixture(
            name=str(entry["name"]),
            db=str(entry["db"]),
            sharded=bool(entry.get("sharded", False)),
            read_only=bool(entry.get("read_only", True)),
            extra_env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
        ))
    return fixtures


# ── CLI: loop the matrix, invoke per-fixture bench ───────────────────


# Map "--bench" arg to the script under benchmarks/ that gets invoked
# per fixture. Add new entries here when wiring in another bench harness.
BENCH_SCRIPTS = {
    "bench_needle": "bench_needle.py",
    "bench_needle_1000": "bench_needle_1000.py",
}


def _run_one_bench(
    bench_script: Path,
    *,
    helix_url: str,
    fixture: Fixture,
    out_dir: Path,
    extra_args: Iterable[str] = (),
    python: str = sys.executable,
    timeout_s: Optional[float] = None,
) -> int:
    """Invoke a bench script as a subprocess, env-configured for the fixture.

    Returns the script's exit code. Output is written to
    ``out_dir/<fixture.name>.log``; bench-specific JSON output is the
    script's responsibility (set via env or args by the caller).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / f"{fixture.name}.log"
    env = os.environ.copy()
    env["HELIX_URL"] = helix_url
    # bench_needle_1000.py honors GENOME_DB for needle harvesting; the
    # orchestrator points it at the same path the server is serving so
    # harvested needles and retrieval target match.
    env["GENOME_DB"] = fixture.db
    env.setdefault("OUTPUT", str(out_dir / f"{fixture.name}_results.json"))

    cmd = [python, str(bench_script), *list(extra_args)]
    with log_path.open("ab") as fh:
        try:
            return subprocess.call(
                cmd, env=env, stdout=fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            log.warning("bench %s on fixture %s timed out", bench_script.name, fixture.name)
            return -1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path,
                        help="Path to a fixture manifest JSON file.")
    parser.add_argument("--bench", choices=sorted(BENCH_SCRIPTS),
                        default="bench_needle_1000",
                        help="Which bench to run per fixture.")
    parser.add_argument("--out", default="benchmarks/results/matrix",
                        type=Path, help="Output directory for logs + JSON results.")
    parser.add_argument("--only", default="",
                        help="Comma-separated fixture names to run (default: all).")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--health-timeout", default=DEFAULT_HEALTH_TIMEOUT_S, type=float)
    parser.add_argument("--bench-timeout", default=None, type=float,
                        help="Per-fixture bench timeout in seconds (default: no limit).")
    parser.add_argument("--read-write", action="store_true",
                        help="Disable read-only guard. Default is read-only "
                             "so benches cannot mutate the fixture DB.")
    parser.add_argument("--server-log", type=Path, default=None,
                        help="Optional path for uvicorn stdout/stderr.")
    args, extra = parser.parse_known_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    fixtures = load_manifest(args.manifest)
    only = {n.strip() for n in args.only.split(",") if n.strip()}
    if only:
        fixtures = [f for f in fixtures if f.name in only]
        if not fixtures:
            log.error("no fixtures match --only=%s", args.only)
            return 2

    if args.read_write:
        for f in fixtures:
            f.read_only = False

    bench_script = Path(__file__).parent / BENCH_SCRIPTS[args.bench]
    if not bench_script.is_file():
        log.error("bench script not found: %s", bench_script)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []

    with BenchServer(
        host=args.host,
        port=args.port,
        health_timeout_s=args.health_timeout,
        log_to=args.server_log,
    ) as srv:
        for fx in fixtures:
            log.info("→ fixture %s (db=%s sharded=%s)", fx.name, fx.db, fx.sharded)
            try:
                swap = srv.switch(fx)
            except BenchServerError as exc:
                log.error("fixture %s: switch failed: %s", fx.name, exc)
                summary.append({
                    "fixture": fx.name, "status": "switch_failed",
                    "error": str(exc),
                })
                continue
            log.info(
                "  %s in %.2fs (genes=%d, pid=%s)",
                swap.mechanism, swap.elapsed_s, swap.genes, swap.server_pid,
            )

            rc = _run_one_bench(
                bench_script,
                helix_url=srv.url,
                fixture=fx,
                out_dir=args.out,
                extra_args=extra,
                timeout_s=args.bench_timeout,
            )
            summary.append({
                "fixture": fx.name, "status": "ok" if rc == 0 else "failed",
                "rc": rc, "mechanism": swap.mechanism,
                "switch_elapsed_s": swap.elapsed_s, "genes": swap.genes,
                "results_path": str(args.out / f"{fx.name}_results.json"),
                "log_path": str(args.out / f"{fx.name}.log"),
            })

    summary_path = args.out / "matrix_summary.json"
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump({"summary": summary, "bench": args.bench}, f, indent=2)
    log.info("matrix summary: %s", summary_path)
    return 0 if all(s.get("status") == "ok" for s in summary) else 1


if __name__ == "__main__":
    raise SystemExit(main())
