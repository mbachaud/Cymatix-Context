"""Bench orchestrator - manage uvicorn lifecycle + hot-swap across the
6-fixture matrix (4 monolithic blobs + 2 sharded) without manual server
restarts between runs.

Two transition shapes:

- **Same-mode (blob->blob, sharded->sharded)**: POST /admin/swap-db.
  Atomic, ~milliseconds, no process restart. Available since PR #91.
- **Cross-mode (blob<->sharded)**: full uvicorn restart with the
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
import socket
import sqlite3
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


def _repo_root() -> Optional[Path]:
    """Return the helix-context repo root, derived from this file's location.

    ``bench_orchestrator.py`` lives at ``<repo>/benchmarks/``, so the repo
    root is ``parents[1]``. We confirm by checking for ``pyproject.toml``
    AND ``helix_context/__init__.py`` (both required to call this a
    helix-context source checkout) — defends against the orchestrator
    being copied/symlinked into an unrelated tree.

    Returns ``None`` if neither marker is present so callers can fall
    back to the inherited cwd / sys.path without crashing.
    """
    try:
        candidate = Path(__file__).resolve().parents[1]
    except (IndexError, OSError):
        return None
    if (candidate / "pyproject.toml").exists() and (
        candidate / "helix_context" / "__init__.py"
    ).exists():
        return candidate
    return None


# Tables the spawned helix server must find in the fixture DB before we
# accept it as "the right helix talking to the right fixture". Issue #153:
# a stale worktree's code path queried tables the fixture's schema didn't
# include (``cwola_log``, ``session_delivery_log``, ``genes``) and the
# whole bench logged ``retr=err`` × 50 instead of failing fast. We probe
# the fixture DB from the orchestrator side so the mismatch surfaces in
# milliseconds, before the first /context call.
#
# ``genes`` is REQUIRED — every non-empty fixture must have it, the issue
# trace was triggered by its absence. ``cwola_log`` and
# ``session_delivery_log`` are RECOMMENDED — we warn rather than fail on
# their absence, because a deliberately-stripped fixture (e.g. a
# routing-only sharded DB) may legitimately omit them but the loaded
# helix code path may still try to write to them. The warning gives the
# operator a heads-up without aborting valid benches.
REQUIRED_FIXTURE_TABLES: tuple[str, ...] = ("genes",)
RECOMMENDED_FIXTURE_TABLES: tuple[str, ...] = (
    "cwola_log",
    "session_delivery_log",
)


def _resolve_helix_context_file(repo_root: Optional[Path]) -> str:
    """Best-effort: report ``helix_context.__file__`` for ``repo_root``.

    Used in the RUN START log line so the operator sees which checkout
    the spawned uvicorn will load. We don't import from a subprocess (too
    slow / pollutes our own sys.modules); we just check the on-disk path
    that PYTHONPATH+cwd will resolve to. If ``repo_root`` is None we fall
    back to whatever the current process already imported.
    """
    if repo_root is not None:
        candidate = repo_root / "helix_context" / "__init__.py"
        if candidate.exists():
            return str(candidate)
    try:
        import helix_context  # noqa: PLC0415 — lazy by design
        return getattr(helix_context, "__file__", "<unknown>") or "<unknown>"
    except Exception:
        return "<unresolvable>"


def _probe_fixture_schema(
    db_path: str,
    *,
    required: Iterable[str] = REQUIRED_FIXTURE_TABLES,
    recommended: Iterable[str] = RECOMMENDED_FIXTURE_TABLES,
) -> None:
    """Open ``db_path`` read-only and assert the required tables exist.

    Issue #153 surfaced as ``sqlite3.OperationalError: no such table: ...``
    50 times per bench because a stale-worktree helix_context queried
    tables the fixture's schema didn't include. Probing from the
    orchestrator side catches the mismatch in milliseconds — and produces
    a single clear error pointing at the wrong-helix root cause instead
    of N "retr=err" log lines.

    Required tables raise ``BenchServerError``. Recommended tables only
    log a warning — the spawn proceeds because some fixtures legitimately
    omit them (e.g. a stripped routing DB) and the operator may still
    want to bench retrieval against them.

    The probe is a no-op for a non-file path (defensive: ``:memory:`` or
    a remote URI just gets skipped rather than crashing the bench
    launcher).
    """
    p = Path(db_path)
    if not p.is_file():
        log.debug("schema probe skipped: %s is not a file", db_path)
        return
    uri = f"file:{p.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise BenchServerError(
            f"fixture {db_path} could not be opened for schema probe: {exc}"
        ) from exc
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()

    missing_required = [t for t in required if t not in names]
    if missing_required:
        raise BenchServerError(
            f"fixture {db_path} is missing required table(s) "
            f"{missing_required}: the spawned helix server would raise "
            "sqlite3.OperationalError on first /context call. Likely "
            "causes: wrong helix_context source on PYTHONPATH (issue "
            "#153), or a fixture built with a stripped schema."
        )
    missing_recommended = [t for t in recommended if t not in names]
    if missing_recommended:
        log.warning(
            "fixture %s missing recommended table(s) %s — some helix "
            "code paths will log OperationalError but the bench will "
            "proceed",
            db_path, missing_recommended,
        )

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11437
DEFAULT_HEALTH_TIMEOUT_S = 60.0
DEFAULT_HEALTH_POLL_S = 0.5
DEFAULT_SHUTDOWN_TIMEOUT_S = 10.0
DEFAULT_SWAP_TIMEOUT_S = 60.0
DEFAULT_REQUEST_TIMEOUT_S = 15.0
# How long to wait for the OS to release the listen port after a stop()
# before spawning the replacement uvicorn. On Windows the socket can
# linger (TIME_WAIT, or a child the OS hasn't fully reaped) past
# proc.wait() returning, so the next bind loses the race with Errno
# 10048. See issue #127.
DEFAULT_PORT_FREE_TIMEOUT_S = 15.0
DEFAULT_PORT_FREE_POLL_S = 0.25

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

    ``expect_nonempty`` defaults True: every fixture in the bench matrix
    has thousands of genes, so a server reporting ``genes=0`` after a
    swap/restart is always a harness failure (issue #127 — sharded DB
    opened in blob mode). Set False only for a deliberately empty
    fixture.
    """
    name: str
    db: str
    sharded: bool = False
    read_only: bool = True
    extra_env: dict[str, str] = field(default_factory=dict)
    expect_nonempty: bool = True


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
        repo_root: Optional[Path] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.python = python or sys.executable
        self.app = app
        self.health_timeout_s = health_timeout_s
        self.shutdown_timeout_s = shutdown_timeout_s
        self.log_to = Path(log_to) if log_to else None
        # Issue #153: pin the spawned uvicorn's working directory and
        # PYTHONPATH to a single helix-context source tree. Default to the
        # repo this orchestrator lives in; callers can override (e.g. to
        # bench a different worktree) by passing ``repo_root=``.
        self.repo_root: Optional[Path] = (
            Path(repo_root).resolve() if repo_root is not None else _repo_root()
        )

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
        # Issue #127: even on a cold start, a leftover uvicorn from a
        # crashed prior run can still hold the port. Confirm it is free
        # (or fail loudly) before spawning into a possible bind race.
        self._wait_port_free()
        self._spawn(fixture)
        self._wait_healthy()

        # After spawn, set the active fixture via hot-swap so we always
        # know the active path (the initial app boot may pick whatever
        # the config file points at).
        swap = self._post_swap(fixture)
        elapsed = time.time() - t0
        self._current = fixture
        self._guard_genes(fixture, swap)
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
        """Terminate uvicorn and confirm the process tree is gone.

        ``proc.terminate()`` only kills the named pid. uvicorn under
        ``python -m uvicorn`` can spawn child/worker processes; on Windows
        a surviving child keeps the listen socket bound, so the next
        ``_spawn()`` loses the bind race (Errno 10048) and the orchestrator
        ends up talking to a stale server. This kills the whole tree and
        verifies teardown before returning. See issue #127.
        """
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        if proc.poll() is not None:
            self._close_log()
            self._current = None
            return
        try:
            if os.name == "nt":
                # taskkill /T walks and kills the whole process tree;
                # /F forces it. terminate() (TerminateProcess) would only
                # reach the top-level pid and orphan any uvicorn children
                # that may still hold port %s.
                self._taskkill_tree(proc.pid)
                try:
                    proc.wait(timeout=self.shutdown_timeout_s)
                except subprocess.TimeoutExpired:
                    log.warning(
                        "uvicorn pid=%s still alive after taskkill /T /F; "
                        "falling back to proc.kill()", proc.pid,
                    )
                    proc.kill()
            else:
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=self.shutdown_timeout_s)
                except subprocess.TimeoutExpired:
                    log.warning(
                        "uvicorn did not exit on SIGINT; killing pid=%s", proc.pid,
                    )
                    proc.kill()
            # Reap so the OS releases the pid + socket handles. On POSIX
            # this prevents a zombie holding the descriptor; on Windows it
            # ensures the handle close completes before we poll the port.
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                log.warning("uvicorn kill timed out; abandoning pid=%s", proc.pid)
        except Exception:
            log.warning("uvicorn shutdown raised", exc_info=True)
        finally:
            self._close_log()
            self._current = None
        # Loud failure beats a silent stale-server attach: if the process
        # is somehow still alive, callers must not spawn a replacement.
        if proc.poll() is None:
            raise BenchServerError(
                f"failed to terminate uvicorn pid={proc.pid}; "
                "refusing to spawn a replacement into a live process"
            )

    @staticmethod
    def _taskkill_tree(pid: int) -> None:
        """Windows-only: kill ``pid`` and its whole process tree.

        Uses ``taskkill /T /F``. ``CREATE_NO_WINDOW`` keeps the helper from
        flashing a console (project Windows subprocess convention).
        """
        try:
            res = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, text=True,
                creationflags=_NO_WINDOW,
                timeout=15.0,
            )
            if res.returncode != 0:
                # rc 128 == process not found; benign if it already exited.
                log.warning(
                    "taskkill /T /F /PID %s exited rc=%s: %s",
                    pid, res.returncode, (res.stderr or res.stdout or "").strip(),
                )
        except subprocess.TimeoutExpired:
            log.warning("taskkill /T /F /PID %s timed out", pid)
        except Exception:
            log.warning("taskkill /T /F /PID %s raised", pid, exc_info=True)

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

        # Issue #153: pin the spawned uvicorn to a single helix-context
        # source tree, otherwise an editable-install / .pth shim / sibling
        # worktree can win the import race and answer with stale code that
        # doesn't match the fixture's schema (the canonical failure: a
        # vibrant-easley worktree on bench/int-5fixture took the
        # ``helix_context._asgi:app`` import and raised
        # ``no such table: cwola_log`` × 50 against the xl-sharded
        # fixture).  Prepend repo_root to PYTHONPATH (preserving any
        # caller-supplied value) and run the subprocess from there.
        spawn_cwd: Optional[str] = None
        if self.repo_root is not None:
            root_str = str(self.repo_root)
            existing = env.get("PYTHONPATH", "")
            if existing:
                # Avoid an obvious double-prepend on restart.
                head = existing.split(os.pathsep, 1)[0]
                if head != root_str:
                    env["PYTHONPATH"] = root_str + os.pathsep + existing
            else:
                env["PYTHONPATH"] = root_str
            spawn_cwd = root_str

        if self.log_to:
            self.log_to.parent.mkdir(parents=True, exist_ok=True)
            self._log_fh = self.log_to.open("ab")
            stdout = stderr = self._log_fh
        else:
            stdout = stderr = subprocess.DEVNULL

        # RUN START line: log which helix_context source the spawned
        # process will resolve, so the operator can confirm the right
        # checkout is answering instead of debugging "retr=err × 50".
        log.info(
            "RUN START fixture=%s sharded=%s db=%s "
            "repo_root=%s helix_context=%s python=%s",
            fixture.name,
            fixture.sharded,
            fixture.db,
            self.repo_root,
            _resolve_helix_context_file(self.repo_root),
            self.python,
        )

        self._proc = subprocess.Popen(
            cmd, env=env, stdout=stdout, stderr=stderr,
            stdin=subprocess.DEVNULL,
            cwd=spawn_cwd,
            creationflags=_NO_WINDOW,
            close_fds=(os.name != "nt"),
        )

    def _wait_port_free(
        self, timeout_s: float = DEFAULT_PORT_FREE_TIMEOUT_S,
    ) -> None:
        """Block until ``(host, port)`` refuses connections, then return.

        ``stop()`` returning does not guarantee the OS has released the
        listen socket — on Windows it can linger in TIME_WAIT or wait on a
        not-fully-reaped child. Spawning the replacement uvicorn before the
        port is free makes it lose the bind race (Errno 10048); the new
        process dies and ``_wait_healthy`` then talks to the *stale*
        server. So we poll until ``connect_ex`` no longer returns 0 (0 ==
        a listener accepted the connection == port still occupied).

        Raises ``BenchServerError`` on timeout rather than letting the
        caller spawn into a doomed bind. See issue #127.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                # connect_ex returns 0 only if something is *listening*.
                # Anything else (ECONNREFUSED / WSAECONNREFUSED, timeout)
                # means the port is free for us to bind.
                if sock.connect_ex((self.host, self.port)) != 0:
                    return
            time.sleep(DEFAULT_PORT_FREE_POLL_S)
        raise BenchServerError(
            f"port {self.host}:{self.port} still occupied after "
            f"{timeout_s}s; refusing to spawn uvicorn into a doomed bind "
            "(a stale server is likely still holding the socket)"
        )

    def _wait_healthy(self) -> None:
        deadline = time.time() + self.health_timeout_s
        last_err: Optional[Exception] = None
        expected_pid = self._proc.pid if self._proc is not None else None
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                rc = self._proc.returncode
                raise BenchServerError(f"uvicorn exited during startup (rc={rc})")
            try:
                payload = self.health()
            except Exception as exc:
                last_err = exc
                time.sleep(DEFAULT_HEALTH_POLL_S)
                continue
            # Identity check: a stale uvicorn that won the bind race will
            # also answer /health. If the responder's pid is not the
            # process we just spawned, fail loudly — proceeding would run
            # the whole bench against the wrong (e.g. blob-mode) server
            # and silently report genes=0. See issue #127.
            responder_pid = payload.get("pid")
            if expected_pid is not None and responder_pid is not None:
                if int(responder_pid) != int(expected_pid):
                    raise BenchServerError(
                        f"/health answered by pid={responder_pid}, but the "
                        f"spawned uvicorn is pid={expected_pid}; a stale "
                        "server is holding the port. Aborting rather than "
                        "running the bench against the wrong process."
                    )
            return
        raise BenchServerError(
            f"uvicorn did not become healthy within {self.health_timeout_s}s "
            f"(last error: {last_err})"
        )

    def _hot_swap(self, fixture: Fixture) -> SwapResult:
        t0 = time.time()
        result = self._post_swap(fixture)
        self._current = fixture
        self._guard_genes(fixture, result)
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
        # Issue #127: do not spawn until the OS has actually released the
        # listen port. Otherwise the new uvicorn loses the bind race and
        # _wait_healthy attaches to the stale (wrong-mode) server.
        self._wait_port_free()
        self._spawn(fixture)
        self._wait_healthy()
        swap = self._post_swap(fixture)
        self._current = fixture
        self._guard_genes(fixture, swap)
        return SwapResult(
            fixture=fixture, mechanism="restart",
            elapsed_s=round(time.time() - t0, 3),
            genes=int(swap.get("genes", 0)),
            server_pid=self.pid,
        )

    def _post_swap(self, fixture: Fixture) -> dict[str, Any]:
        # Issue #153: schema probe BEFORE the swap. A missing required
        # table here means the running helix code will raise
        # OperationalError on every /context call — better to abort now
        # than to log retr=err × 50 and produce an unactionable summary.
        _probe_fixture_schema(fixture.db)
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

    @staticmethod
    def _guard_genes(fixture: Fixture, swap: dict[str, Any]) -> None:
        """Fail loudly if a non-empty fixture reports ``genes=0``.

        A ``start``/``restart``/``hot_swap`` that yields zero genes for a
        fixture the manifest says has content is always a harness failure
        — the canonical case (issue #127) is a sharded routing DB opened
        in blob mode because ``HELIX_USE_SHARDS`` did not take effect, so
        ``stats()`` reads the empty local ``genes`` table. Without this
        guard the bench silently produces a 0/N result that looks like a
        retrieval regression.
        """
        if not fixture.expect_nonempty:
            return
        genes = int(swap.get("genes", 0))
        if genes == 0:
            raise BenchServerError(
                f"fixture {fixture.name} ({fixture.db}) reported genes=0 "
                "after swap, but the manifest marks it non-empty. This is a "
                "harness failure (likely a sharded DB opened in blob mode — "
                "HELIX_USE_SHARDS not in effect, or a stale server answered). "
                "Refusing to run a bench that would silently score 0/N."
            )

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

    The optional ``_genes`` metadata key (the matrix manifest records the
    expected gene count there) drives the ``expect_nonempty`` guard: an
    entry with ``"_genes": 0`` is treated as a deliberately empty fixture
    and exempt from the ``genes=0`` failure (issue #127). Any other value
    — or its absence — keeps the default non-empty expectation.
    """
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise ValueError(f"manifest at {path} must be a JSON list of fixtures")
    fixtures = []
    for i, entry in enumerate(raw):
        if "name" not in entry or "db" not in entry:
            raise ValueError(f"fixture #{i} missing 'name' or 'db'")
        # Only an explicit "_genes": 0 marks a fixture as legitimately
        # empty. Absent metadata defaults to expecting content.
        expect_nonempty = entry.get("_genes", None) != 0
        fixtures.append(Fixture(
            name=str(entry["name"]),
            db=str(entry["db"]),
            sharded=bool(entry.get("sharded", False)),
            read_only=bool(entry.get("read_only", True)),
            extra_env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
            expect_nonempty=expect_nonempty,
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
