"""Unit tests for bench_orchestrator.py — the issue #127 fixes.

Issue #127: a cross-mode bench restart (blob -> sharded) could fail to
free port 11437 before spawning the replacement uvicorn. The new process
lost the bind race (Errno 10048), exited, and ``_wait_healthy()`` then
talked to the *stale* blob-mode server still holding the port. Every
sharded query returned ``genes=0``.

These tests pin the four hardening fixes — they are pure unit tests:
the socket / subprocess / HTTP boundaries are mocked, no real uvicorn is
spawned.

  1. ``stop()`` kills the whole process tree and confirms teardown
     (raises if the process is somehow still alive).
  2. ``_wait_port_free()`` returns once the port refuses connections and
     raises ``BenchServerError`` on timeout.
  3. ``_wait_healthy()`` rejects a ``/health`` responder whose pid does
     not match the process just spawned.
  4. ``_guard_genes()`` raises when a non-empty fixture reports
     ``genes=0`` after a swap.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make benchmarks/ importable as a flat module path, matching the
# convention in tests/test_benchmark_monitor_preflight.py.
BENCH_DIR = Path(__file__).resolve().parents[1] / "benchmarks"
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

from bench_orchestrator import (  # noqa: E402
    BenchServer,
    BenchServerError,
    Fixture,
    load_manifest,
)


# ── helpers ───────────────────────────────────────────────────────────


def _fake_proc(pid: int = 4242, *, alive: bool = True) -> MagicMock:
    """A stand-in for ``subprocess.Popen`` with controllable liveness.

    ``poll()`` returns ``None`` while ``alive`` is True, then the exit
    code once flipped. ``wait()`` flips it to dead (simulating a
    successful kill) unless ``wait_raises`` is set.
    """
    proc = MagicMock()
    proc.pid = pid
    state = {"alive": alive, "rc": None}

    def poll():
        return None if state["alive"] else state["rc"]

    def wait(timeout=None):
        if state.get("wait_raises"):
            raise subprocess.TimeoutExpired(cmd="uvicorn", timeout=timeout)
        state["alive"] = False
        state["rc"] = 0
        return 0

    def kill():
        state["alive"] = False
        state["rc"] = -9

    proc.poll.side_effect = poll
    proc.wait.side_effect = wait
    proc.kill.side_effect = kill
    proc._state = state  # test hook
    return proc


# ── Fix 1: stop() — process-tree kill + teardown confirmation ─────────


def test_stop_noop_when_no_process() -> None:
    """stop() on a server that never started is a quiet no-op."""
    srv = BenchServer()
    srv.stop()  # must not raise


def test_stop_kills_process_tree_on_windows() -> None:
    """On Windows stop() shells out to ``taskkill /T /F`` (tree kill).

    ``proc.terminate()`` (TerminateProcess) only reaches the named pid
    and would orphan uvicorn children still holding the port.
    """
    srv = BenchServer()
    proc = _fake_proc(pid=9001)
    srv._proc = proc

    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    with patch("bench_orchestrator.os") as fake_os, \
            patch("bench_orchestrator.subprocess.run", fake_run):
        fake_os.name = "nt"
        srv.stop()

    assert fake_run.called, "expected taskkill to be invoked on Windows"
    argv = fake_run.call_args[0][0]
    assert argv[0] == "taskkill"
    assert "/T" in argv and "/F" in argv
    assert "9001" in argv, "taskkill must target the spawned pid"
    assert srv._proc is None


def test_stop_confirms_process_death() -> None:
    """After stop() the process is reaped (poll() reports an exit code)."""
    srv = BenchServer()
    proc = _fake_proc(pid=9002)
    srv._proc = proc

    with patch("bench_orchestrator.os") as fake_os, \
            patch("bench_orchestrator.subprocess.run",
                  MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))):
        fake_os.name = "nt"
        srv.stop()

    assert proc.poll() is not None, "process must be dead after stop()"


def test_stop_raises_if_process_survives() -> None:
    """A stop() that fails to kill the process raises — a live server
    must never silently be left holding the port for the next spawn."""
    srv = BenchServer()
    # Process that refuses to die: poll() always None, wait() always
    # times out, kill() is a no-op.
    proc = MagicMock()
    proc.pid = 9003
    proc.poll.return_value = None
    proc.wait.side_effect = subprocess.TimeoutExpired(cmd="uvicorn", timeout=5)
    proc.kill.return_value = None
    srv._proc = proc

    with patch("bench_orchestrator.os") as fake_os, \
            patch("bench_orchestrator.subprocess.run",
                  MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))):
        fake_os.name = "nt"
        with pytest.raises(BenchServerError, match="failed to terminate"):
            srv.stop()


def test_stop_posix_uses_sigint() -> None:
    """On POSIX stop() keeps the SIGINT-then-reap path (no taskkill)."""
    srv = BenchServer()
    proc = _fake_proc(pid=9004)
    srv._proc = proc

    fake_run = MagicMock()
    with patch("bench_orchestrator.os") as fake_os, \
            patch("bench_orchestrator.subprocess.run", fake_run):
        fake_os.name = "posix"
        srv.stop()

    assert not fake_run.called, "taskkill must not run on POSIX"
    assert proc.send_signal.called, "POSIX path must signal the process"
    assert proc.poll() is not None


# ── Fix 2: _wait_port_free() ──────────────────────────────────────────


def test_wait_port_free_returns_when_port_refuses() -> None:
    """_wait_port_free returns once connect_ex no longer returns 0
    (0 == a listener accepted == port still occupied)."""
    srv = BenchServer()
    fake_sock = MagicMock()
    # First poll: port still bound (0). Second poll: refused (non-zero).
    fake_sock.connect_ex.side_effect = [0, 111]
    fake_sock.__enter__ = MagicMock(return_value=fake_sock)
    fake_sock.__exit__ = MagicMock(return_value=False)

    with patch("bench_orchestrator.socket.socket", return_value=fake_sock), \
            patch("bench_orchestrator.time.sleep"):
        srv._wait_port_free(timeout_s=5.0)

    assert fake_sock.connect_ex.call_count == 2


def test_wait_port_free_returns_immediately_if_free() -> None:
    """If the port is already free, _wait_port_free returns on poll #1."""
    srv = BenchServer()
    fake_sock = MagicMock()
    fake_sock.connect_ex.return_value = 111  # refused == free
    fake_sock.__enter__ = MagicMock(return_value=fake_sock)
    fake_sock.__exit__ = MagicMock(return_value=False)

    with patch("bench_orchestrator.socket.socket", return_value=fake_sock), \
            patch("bench_orchestrator.time.sleep"):
        srv._wait_port_free(timeout_s=5.0)

    assert fake_sock.connect_ex.call_count == 1


def test_wait_port_free_raises_on_timeout() -> None:
    """A port that stays bound past the timeout raises BenchServerError
    rather than letting the caller spawn into a doomed bind."""
    srv = BenchServer()
    fake_sock = MagicMock()
    fake_sock.connect_ex.return_value = 0  # always occupied
    fake_sock.__enter__ = MagicMock(return_value=fake_sock)
    fake_sock.__exit__ = MagicMock(return_value=False)

    # Drive a deterministic timeout: time advances past the deadline on
    # the second read.
    times = iter([1000.0, 1000.0, 9999.0, 9999.0])
    with patch("bench_orchestrator.socket.socket", return_value=fake_sock), \
            patch("bench_orchestrator.time.sleep"), \
            patch("bench_orchestrator.time.time", side_effect=lambda: next(times)):
        with pytest.raises(BenchServerError, match="still occupied"):
            srv._wait_port_free(timeout_s=15.0)


# ── Fix 3: _wait_healthy() identity check ─────────────────────────────


def test_wait_healthy_accepts_matching_pid() -> None:
    """_wait_healthy succeeds when /health reports the spawned pid."""
    srv = BenchServer()
    srv._proc = _fake_proc(pid=5555)

    with patch.object(srv, "health", return_value={"status": "ok", "pid": 5555}):
        srv._wait_healthy()  # must not raise


def test_wait_healthy_rejects_mismatched_pid() -> None:
    """A /health responder with the *wrong* pid (a stale server that won
    the bind race) must cause a loud failure, not silent success."""
    srv = BenchServer()
    srv._proc = _fake_proc(pid=5555)

    # Stale blob-mode server answering on the port reports a different pid.
    with patch.object(srv, "health", return_value={"status": "ok", "pid": 38068}):
        with pytest.raises(BenchServerError, match="stale server"):
            srv._wait_healthy()


def test_wait_healthy_tolerates_health_without_pid() -> None:
    """If /health omits ``pid`` (older server build), _wait_healthy does
    not crash — it just cannot perform the identity assertion."""
    srv = BenchServer()
    srv._proc = _fake_proc(pid=5555)

    with patch.object(srv, "health", return_value={"status": "ok"}):
        srv._wait_healthy()  # must not raise


def test_wait_healthy_raises_if_process_exits() -> None:
    """If the spawned uvicorn exits during startup, _wait_healthy raises
    (e.g. the new process lost the bind race and shut itself down)."""
    srv = BenchServer()
    proc = _fake_proc(pid=5556)
    proc._state["alive"] = False  # process already dead
    proc._state["rc"] = 1
    srv._proc = proc

    with pytest.raises(BenchServerError, match="exited during startup"):
        srv._wait_healthy()


# ── Fix 4: _guard_genes() ─────────────────────────────────────────────


def test_guard_genes_raises_on_zero_for_nonempty_fixture() -> None:
    """genes=0 on a fixture the manifest marks non-empty is a harness
    failure (sharded DB opened in blob mode) and must raise."""
    fx = Fixture(name="medium-sharded", db="x/main.genome.db",
                 sharded=True, expect_nonempty=True)
    with pytest.raises(BenchServerError, match="genes=0"):
        BenchServer._guard_genes(fx, {"genes": 0})


def test_guard_genes_passes_for_nonempty_result() -> None:
    """A non-empty fixture reporting real genes passes the guard."""
    fx = Fixture(name="medium-sharded", db="x/main.genome.db",
                 sharded=True, expect_nonempty=True)
    BenchServer._guard_genes(fx, {"genes": 17396})  # must not raise


def test_guard_genes_skips_when_fixture_marked_empty() -> None:
    """A fixture explicitly flagged ``expect_nonempty=False`` is exempt."""
    fx = Fixture(name="empty", db="x/empty.db", expect_nonempty=False)
    BenchServer._guard_genes(fx, {"genes": 0})  # must not raise


def test_guard_genes_treats_missing_genes_key_as_zero() -> None:
    """A swap response missing ``genes`` entirely is treated as 0 and
    fails the guard for a non-empty fixture."""
    fx = Fixture(name="medium", db="x/medium.db", expect_nonempty=True)
    with pytest.raises(BenchServerError, match="genes=0"):
        BenchServer._guard_genes(fx, {})


# ── load_manifest() — expect_nonempty derivation ──────────────────────


def test_load_manifest_defaults_expect_nonempty_true(tmp_path: Path) -> None:
    """Manifest entries without ``_genes`` metadata default to expecting
    content (the bench-matrix invariant)."""
    manifest = tmp_path / "fixtures.json"
    manifest.write_text(
        '[{"name": "small", "db": "x/small.db", "sharded": false}]',
        encoding="utf-8",
    )
    fixtures = load_manifest(manifest)
    assert len(fixtures) == 1
    assert fixtures[0].expect_nonempty is True


def test_load_manifest_honors_explicit_empty_marker(tmp_path: Path) -> None:
    """An entry with ``"_genes": 0`` is treated as a deliberately empty
    fixture (expect_nonempty=False); a positive count keeps it True."""
    manifest = tmp_path / "fixtures.json"
    manifest.write_text(
        '[{"name": "empty", "db": "x/e.db", "_genes": 0},'
        ' {"name": "medium", "db": "x/m.db", "_genes": 17324}]',
        encoding="utf-8",
    )
    fixtures = load_manifest(manifest)
    by_name = {f.name: f for f in fixtures}
    assert by_name["empty"].expect_nonempty is False
    assert by_name["medium"].expect_nonempty is True


# ── integration of the fixes inside _restart_for_fixture ──────────────


def test_restart_waits_for_port_then_spawns_in_order() -> None:
    """The cross-mode restart path must stop -> wait-for-free-port ->
    spawn, in that order, so the new uvicorn never races a held port."""
    srv = BenchServer()
    srv._proc = _fake_proc(pid=7000)

    calls: list[str] = []
    blob = Fixture(name="small", db="x/small.db", sharded=False)
    sharded = Fixture(name="medium-sharded", db="x/main.genome.db", sharded=True)
    srv._current = blob

    def rec(label):
        def _inner(*_a, **_k):
            calls.append(label)
        return _inner

    with patch.object(srv, "stop", side_effect=rec("stop")), \
            patch.object(srv, "_wait_port_free", side_effect=rec("wait_port_free")), \
            patch.object(srv, "_spawn", side_effect=rec("spawn")), \
            patch.object(srv, "_wait_healthy", side_effect=rec("wait_healthy")), \
            patch.object(srv, "_post_swap", return_value={"genes": 17396}):
        result = srv._restart_for_fixture(sharded, blob)

    assert calls == ["stop", "wait_port_free", "spawn", "wait_healthy"], calls
    assert result.mechanism == "restart"
    assert result.genes == 17396


def test_restart_fails_loudly_when_sharded_fixture_yields_zero_genes() -> None:
    """End-to-end of the #127 failure mode: the restart completes but the
    swap reports genes=0 (sharded DB opened in blob mode). The orchestrator
    must raise instead of returning a SwapResult with genes=0."""
    srv = BenchServer()
    srv._proc = _fake_proc(pid=7001)
    blob = Fixture(name="small", db="x/small.db", sharded=False)
    sharded = Fixture(name="medium-sharded", db="x/main.genome.db",
                      sharded=True, expect_nonempty=True)
    srv._current = blob

    with patch.object(srv, "stop"), \
            patch.object(srv, "_wait_port_free"), \
            patch.object(srv, "_spawn"), \
            patch.object(srv, "_wait_healthy"), \
            patch.object(srv, "_post_swap", return_value={"genes": 0}):
        with pytest.raises(BenchServerError, match="genes=0"):
            srv._restart_for_fixture(sharded, blob)
