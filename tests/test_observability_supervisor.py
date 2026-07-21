"""Tests for ObservabilitySupervisor.

All subprocess.Popen calls are mocked — no real binaries spawn. Tests
cover spawn-order, port pre-flight, refusal-without-rendered-config,
Job Object setup on Windows, cleanup cascade.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _empty_stdout_proc(pid: int = 12345) -> MagicMock:
    """Build a fake Popen result whose stdout is an already-closed pipe.

    The supervisor spawns a per-service log-drainer thread that reads
    proc.stdout line-by-line and exits on b''. A bare MagicMock returns
    MagicMock for readline(), which never matches the b'' sentinel — the
    drainer would loop forever and starve the test process. Giving each
    fake proc an empty BytesIO makes the drainer exit immediately.
    """
    m = MagicMock()
    m.pid = pid
    m.poll.return_value = None
    m.stdout = io.BytesIO(b"")
    return m


SERVICES = ("collector", "prometheus", "tempo", "loki", "grafana")


@pytest.fixture
def fake_paths(tmp_path, monkeypatch):
    """Redirect state + configs into tmp_path; pretend binaries + configs exist."""
    from cymatix_context.launcher import observability_paths as ops

    monkeypatch.setattr(ops, "_user_data_dir", lambda: tmp_path / "appdata")
    monkeypatch.setattr(
        ops, "_repo_root",
        lambda: tmp_path / "repo",
    )

    # Pretend each binary + each rendered config exists.
    (tmp_path / "appdata" / "observability").mkdir(parents=True, exist_ok=True)
    cfg_dir = tmp_path / "repo" / "tools" / "native-otel" / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "otel-collector-config.yaml",
        "prometheus.yml",
        "tempo.yaml",
        "loki-config.yaml",
        "datasources.yml",
        "dashboards.yml",
    ):
        (cfg_dir / name).write_text("# stub\n", encoding="utf-8")

    for svc in SERVICES:
        bp = ops.binary_path(svc)
        bp.parent.mkdir(parents=True, exist_ok=True)
        bp.write_bytes(b"\x7fELF")  # placeholder
    return tmp_path


def _supervisor(fake_paths):
    from cymatix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )
    return ObservabilitySupervisor()


# ── refusal-without-rendered-config ─────────────────────────────────

def test_refuses_to_spawn_when_rendered_config_missing(fake_paths):
    from cymatix_context.launcher import observability_paths as ops
    from cymatix_context.launcher.observability_supervisor import (
        ConfigsMissing,
        ObservabilitySupervisor,
    )
    # Remove one rendered config.
    (ops.configs_dir() / "prometheus.yml").unlink()

    sup = ObservabilitySupervisor()
    with pytest.raises(ConfigsMissing):
        sup.start_all()


# ── port pre-flight ─────────────────────────────────────────────────

def test_port_already_bound_skips_spawn_and_marks_external(fake_paths):
    from cymatix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )

    def _make_proc(*a, **kw):
        return _empty_stdout_proc(pid=22000)

    with patch(
        "cymatix_context.launcher.observability_supervisor.is_port_bound",
        side_effect=lambda host, port: port == 9090,
    ), patch(
        "cymatix_context.launcher.observability_supervisor.subprocess.Popen",
        side_effect=_make_proc,
    ) as popen, patch(
        "cymatix_context.launcher.observability_supervisor.wait_for_port",
        return_value=True,
    ):
        sup = ObservabilitySupervisor()
        sup.start_all()
        # Each Popen call's first positional arg is the cmd list; the
        # binary path is its first element.
        spawned_cmds = [call.args[0] for call in popen.call_args_list]
        spawned_bin_paths = [str(cmd[0]) for cmd in spawned_cmds]
        # No prometheus binary in the spawn list.
        assert not any("prometheus" in p for p in spawned_bin_paths), (
            f"prometheus should be skipped when :9090 is bound; got {spawned_bin_paths}"
        )
        assert sup.status("prometheus") == "external"


# ── spawn order ─────────────────────────────────────────────────────

def test_spawn_order_phase1_then_collector_then_grafana(fake_paths):
    """Phase 1: prom+tempo+loki spawn first; collector after they're ready;
    grafana last. We assert by recording the order of Popen calls."""
    from cymatix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )

    spawn_order = []
    def _record(*args, **kwargs):
        cmd = args[0]
        for svc in SERVICES:
            if any(svc in str(p) for p in cmd):
                spawn_order.append(svc)
                break
        return _empty_stdout_proc(pid=12345)

    with patch(
        "cymatix_context.launcher.observability_supervisor.is_port_bound",
        return_value=False,
    ), patch(
        "cymatix_context.launcher.observability_supervisor.subprocess.Popen",
        side_effect=_record,
    ), patch(
        "cymatix_context.launcher.observability_supervisor.wait_for_port",
        return_value=True,
    ):
        sup = ObservabilitySupervisor()
        sup.start_all()

    # Phase 1 services come before collector; collector before grafana.
    p1 = {"prometheus", "tempo", "loki"}
    collector_idx = spawn_order.index("collector")
    grafana_idx = spawn_order.index("grafana")
    for s in p1:
        assert spawn_order.index(s) < collector_idx, (
            f"{s} must spawn before collector; got order={spawn_order}"
        )
    assert collector_idx < grafana_idx


# ── shutdown cascade ────────────────────────────────────────────────

def test_shutdown_terminates_all_children(fake_paths):
    from cymatix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )
    procs = []
    def _make(*a, **kw):
        m = _empty_stdout_proc(pid=22000 + len(procs))
        procs.append(m)
        return m

    with patch(
        "cymatix_context.launcher.observability_supervisor.is_port_bound",
        return_value=False,
    ), patch(
        "cymatix_context.launcher.observability_supervisor.subprocess.Popen",
        side_effect=_make,
    ), patch(
        "cymatix_context.launcher.observability_supervisor.wait_for_port",
        return_value=True,
    ):
        sup = ObservabilitySupervisor()
        sup.start_all()
        sup.shutdown()

    for m in procs:
        assert m.terminate.called or m.kill.called, (
            "every child must receive terminate or kill on shutdown"
        )


# ── Windows Job Object setup ────────────────────────────────────────

@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_job_object_created_on_windows(fake_paths):
    """When the supervisor starts, it should construct a Job Object with
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE so child PIDs auto-terminate when
    the parent process dies (clean OR force-killed)."""
    from cymatix_context.launcher import observability_supervisor as os_mod

    fake_job = MagicMock()
    fake_job.handle = 0xCAFE

    with patch.object(
        os_mod,
        "_create_kill_on_close_job",
        return_value=fake_job,
    ) as create_job, patch(
        "cymatix_context.launcher.observability_supervisor.is_port_bound",
        return_value=False,
    ), patch(
        "cymatix_context.launcher.observability_supervisor.subprocess.Popen",
    ) as popen, patch(
        "cymatix_context.launcher.observability_supervisor._assign_to_job",
    ) as assign, patch(
        "cymatix_context.launcher.observability_supervisor.wait_for_port",
        return_value=True,
    ):
        popen.return_value = _empty_stdout_proc(pid=9999)

        from cymatix_context.launcher.observability_supervisor import (
            ObservabilitySupervisor,
        )
        sup = ObservabilitySupervisor()
        sup.start_all()

        # Job created exactly once.
        create_job.assert_called_once()
        # Every child PID added to the job (5 services minus any externally
        # adopted; here none are external).
        assert assign.call_count == 5


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
def test_posix_uses_start_new_session(fake_paths):
    from cymatix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )
    captured_kwargs = []
    def _capture(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return _empty_stdout_proc(pid=7777)

    with patch(
        "cymatix_context.launcher.observability_supervisor.is_port_bound",
        return_value=False,
    ), patch(
        "cymatix_context.launcher.observability_supervisor.subprocess.Popen",
        side_effect=_capture,
    ), patch(
        "cymatix_context.launcher.observability_supervisor.wait_for_port",
        return_value=True,
    ):
        sup = ObservabilitySupervisor()
        sup.start_all()

    for kw in captured_kwargs:
        assert kw.get("start_new_session") is True


# ── per-service restart ─────────────────────────────────────────────

def test_restart_service_kills_then_respawns(fake_paths):
    from cymatix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )

    procs_made = []
    def _make(*a, **kw):
        m = _empty_stdout_proc(pid=30000 + len(procs_made))
        procs_made.append(m)
        return m

    with patch(
        "cymatix_context.launcher.observability_supervisor.is_port_bound",
        return_value=False,
    ), patch(
        "cymatix_context.launcher.observability_supervisor.subprocess.Popen",
        side_effect=_make,
    ), patch(
        "cymatix_context.launcher.observability_supervisor.wait_for_port",
        return_value=True,
    ):
        sup = ObservabilitySupervisor()
        sup.start_all()
        prom_first = sup._procs["prometheus"]
        sup.restart_service("prometheus")
        prom_second = sup._procs["prometheus"]

    assert prom_first is not prom_second, "restart should produce a new Popen"
    assert prom_first.terminate.called or prom_first.kill.called


# ── Task 7.5 — log rotation (spec §7.4) ─────────────────────────────
#
# Spec mandates: stdout/stderr → `<service>.log`, rotated at 10MB, last
# 3 retained. Implementation uses a per-service reader thread that
# drains the child's piped stdout into a logging.handlers.RotatingFileHandler.
# This works on Windows (where renaming an open file fails with
# ERROR_SHARING_VIOLATION) because the parent owns the file handle, not
# the child.

def test_log_rotation_triggers_at_size_threshold(fake_paths, tmp_path):
    """Push >10MB of bytes through the drainer; verify .log.1 is created
    and the active .log is reset (smaller than the threshold)."""
    import io
    import time

    from cymatix_context.launcher import observability_paths as ops
    from cymatix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )

    sup = ObservabilitySupervisor()

    # Build a fake child whose stdout produces ~11MB of line-terminated
    # bytes, then EOF (b"").
    line = (b"x" * 1023) + b"\n"          # 1024 bytes per line
    n_lines = 11 * 1024                    # 11 MiB total
    fake_stdout = io.BytesIO(line * n_lines)
    fake_proc = MagicMock()
    fake_proc.stdout = fake_stdout
    fake_proc.poll.return_value = 0  # exited cleanly

    # Drive the drainer for a real service name.
    svc = "prometheus"
    log_path = ops.logs_dir(create=True) / f"{svc}.log"

    thread = sup._start_log_drainer(svc, fake_proc)
    thread.join(timeout=30.0)
    assert not thread.is_alive(), "drainer should exit when child closes pipe"

    # Force handler flush + close so on-disk state is observable.
    sup._close_log_handler(svc)

    backup = log_path.with_name(log_path.name + ".1")
    assert backup.exists(), (
        f"expected rotated backup at {backup}; dir contents: "
        f"{list(log_path.parent.iterdir())}"
    )
    # Active log must be < threshold (a fresh post-rotation file).
    assert log_path.stat().st_size < 10 * 1024 * 1024


def test_keeps_only_three_backups(fake_paths, tmp_path):
    """Force ≥4 rotations and verify .log.4 does NOT exist
    (RotatingFileHandler with backupCount=3 must drop the oldest)."""
    import io

    from cymatix_context.launcher import observability_paths as ops
    from cymatix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )

    sup = ObservabilitySupervisor()

    # ~45 MiB → ≥4 rotations against a 10 MiB threshold.
    line = (b"y" * 1023) + b"\n"
    n_lines = 45 * 1024
    fake_stdout = io.BytesIO(line * n_lines)
    fake_proc = MagicMock()
    fake_proc.stdout = fake_stdout
    fake_proc.poll.return_value = 0

    svc = "tempo"
    log_path = ops.logs_dir(create=True) / f"{svc}.log"

    thread = sup._start_log_drainer(svc, fake_proc)
    thread.join(timeout=60.0)
    assert not thread.is_alive()

    sup._close_log_handler(svc)

    # .log.1, .log.2, .log.3 may exist; .log.4 MUST NOT.
    too_old = log_path.with_name(log_path.name + ".4")
    assert not too_old.exists(), (
        f"backup count exceeded 3; dir contents: "
        f"{sorted(p.name for p in log_path.parent.iterdir())}"
    )


def test_log_drainer_handles_child_exit(fake_paths):
    """When the child closes its stdout pipe, the drainer thread must
    exit cleanly (no infinite loop, no exception bubbling out)."""
    import io

    from cymatix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )

    sup = ObservabilitySupervisor()

    # Empty stdout → first readline returns b"" → drainer exits immediately.
    fake_proc = MagicMock()
    fake_proc.stdout = io.BytesIO(b"")
    fake_proc.poll.return_value = 0

    svc = "loki"
    thread = sup._start_log_drainer(svc, fake_proc)
    thread.join(timeout=5.0)

    assert not thread.is_alive(), "drainer must exit when stdout is closed"
    sup._close_log_handler(svc)
