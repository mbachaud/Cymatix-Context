"""Tests for ObservabilitySupervisor.

All subprocess.Popen calls are mocked — no real binaries spawn. Tests
cover spawn-order, port pre-flight, refusal-without-rendered-config,
Job Object setup on Windows, cleanup cascade.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


SERVICES = ("collector", "prometheus", "tempo", "loki", "grafana")


@pytest.fixture
def fake_paths(tmp_path, monkeypatch):
    """Redirect state + configs into tmp_path; pretend binaries + configs exist."""
    from helix_context.launcher import observability_paths as ops

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
    ):
        (cfg_dir / name).write_text("# stub\n", encoding="utf-8")

    for svc in SERVICES:
        bp = ops.binary_path(svc)
        bp.parent.mkdir(parents=True, exist_ok=True)
        bp.write_bytes(b"\x7fELF")  # placeholder
    return tmp_path


def _supervisor(fake_paths):
    from helix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )
    return ObservabilitySupervisor()


# ── refusal-without-rendered-config ─────────────────────────────────

def test_refuses_to_spawn_when_rendered_config_missing(fake_paths):
    from helix_context.launcher import observability_paths as ops
    from helix_context.launcher.observability_supervisor import (
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
    from helix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )

    def _make_proc(*a, **kw):
        m = MagicMock()
        m.pid = 22000
        m.poll.return_value = None
        return m

    with patch(
        "helix_context.launcher.observability_supervisor.is_port_bound",
        side_effect=lambda host, port: port == 9090,
    ), patch(
        "helix_context.launcher.observability_supervisor.subprocess.Popen",
        side_effect=_make_proc,
    ) as popen, patch(
        "helix_context.launcher.observability_supervisor.wait_for_port",
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
    from helix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )

    spawn_order = []
    def _record(*args, **kwargs):
        cmd = args[0]
        for svc in SERVICES:
            if any(svc in str(p) for p in cmd):
                spawn_order.append(svc)
                break
        m = MagicMock()
        m.pid = 12345
        m.poll.return_value = None
        return m

    with patch(
        "helix_context.launcher.observability_supervisor.is_port_bound",
        return_value=False,
    ), patch(
        "helix_context.launcher.observability_supervisor.subprocess.Popen",
        side_effect=_record,
    ), patch(
        "helix_context.launcher.observability_supervisor.wait_for_port",
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
    from helix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )
    procs = []
    def _make(*a, **kw):
        m = MagicMock()
        m.pid = 22000 + len(procs)
        m.poll.return_value = None
        procs.append(m)
        return m

    with patch(
        "helix_context.launcher.observability_supervisor.is_port_bound",
        return_value=False,
    ), patch(
        "helix_context.launcher.observability_supervisor.subprocess.Popen",
        side_effect=_make,
    ), patch(
        "helix_context.launcher.observability_supervisor.wait_for_port",
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
    from helix_context.launcher import observability_supervisor as os_mod

    fake_job = MagicMock()
    fake_job.handle = 0xCAFE

    with patch.object(
        os_mod,
        "_create_kill_on_close_job",
        return_value=fake_job,
    ) as create_job, patch(
        "helix_context.launcher.observability_supervisor.is_port_bound",
        return_value=False,
    ), patch(
        "helix_context.launcher.observability_supervisor.subprocess.Popen",
    ) as popen, patch(
        "helix_context.launcher.observability_supervisor._assign_to_job",
    ) as assign, patch(
        "helix_context.launcher.observability_supervisor.wait_for_port",
        return_value=True,
    ):
        m = MagicMock()
        m.pid = 9999
        m.poll.return_value = None
        popen.return_value = m

        from helix_context.launcher.observability_supervisor import (
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
    from helix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )
    captured_kwargs = []
    def _capture(*args, **kwargs):
        captured_kwargs.append(kwargs)
        m = MagicMock()
        m.pid = 7777
        m.poll.return_value = None
        return m

    with patch(
        "helix_context.launcher.observability_supervisor.is_port_bound",
        return_value=False,
    ), patch(
        "helix_context.launcher.observability_supervisor.subprocess.Popen",
        side_effect=_capture,
    ), patch(
        "helix_context.launcher.observability_supervisor.wait_for_port",
        return_value=True,
    ):
        sup = ObservabilitySupervisor()
        sup.start_all()

    for kw in captured_kwargs:
        assert kw.get("start_new_session") is True


# ── per-service restart ─────────────────────────────────────────────

def test_restart_service_kills_then_respawns(fake_paths):
    from helix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )

    procs_made = []
    def _make(*a, **kw):
        m = MagicMock()
        m.pid = 30000 + len(procs_made)
        m.poll.return_value = None
        procs_made.append(m)
        return m

    with patch(
        "helix_context.launcher.observability_supervisor.is_port_bound",
        return_value=False,
    ), patch(
        "helix_context.launcher.observability_supervisor.subprocess.Popen",
        side_effect=_make,
    ), patch(
        "helix_context.launcher.observability_supervisor.wait_for_port",
        return_value=True,
    ):
        sup = ObservabilitySupervisor()
        sup.start_all()
        prom_first = sup._procs["prometheus"]
        sup.restart_service("prometheus")
        prom_second = sup._procs["prometheus"]

    assert prom_first is not prom_second, "restart should produce a new Popen"
    assert prom_first.terminate.called or prom_first.kill.called
