"""Enable/Stop observability from the dashboard (the Electron shell's headless
launcher never starts the sidecar on its own; the tray does)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("jinja2", reason="launcher extra not installed")
from fastapi.testclient import TestClient

from cymatix_context.launcher.app import create_app
from cymatix_context.launcher.observability_control import (
    NotInstalled,
    ObservabilityControl,
)
from tests.test_launcher_dashboard_wiring import FakeCollector, FakeSupervisor


class FakeObs:
    def __init__(self):
        self.shutdowns = 0

    def all_statuses(self):
        return {"collector": "green", "prometheus": "green"}

    def shutdown(self):
        self.shutdowns += 1


class Calls:
    def __init__(self):
        self.built = 0
        self.started = 0
        self.restarts = 0
        self.obs = FakeObs()


def _control(calls: Calls, *, installed=True, opted_out=False, build_error=None, export=False):
    def build():
        calls.built += 1
        if build_error:
            raise build_error
        return calls.obs

    def start(sup):
        calls.started += 1
        if export:
            os.environ["CYMATIX_OTEL_ENABLED"] = "1"

    def restart():
        calls.restarts += 1

    return ObservabilityControl(
        build=build, start=start, restart_backend=restart,
        is_installed=lambda: installed, is_opted_out=lambda: opted_out,
        run_async=lambda fn: fn(),                     # run inline in tests
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CYMATIX_OTEL_ENABLED", raising=False)


def test_starts_stopped_when_installed():
    assert _control(Calls()).snapshot()["status"] == "stopped"


def test_not_installed_refuses_to_enable():
    c = _control(Calls(), installed=False)
    assert c.snapshot()["status"] == "not_installed"
    with pytest.raises(NotInstalled):
        c.enable()


def test_opt_out_is_unavailable():
    c = _control(Calls(), opted_out=True)
    assert c.snapshot()["status"] == "unavailable"
    with pytest.raises(NotInstalled):
        c.enable()


def test_enable_builds_starts_and_restarts_the_backend_once():
    calls = Calls()
    c = _control(calls)
    c.enable()
    c.enable()                                          # idempotent
    snap = c.snapshot()
    assert snap["status"] == "running"
    assert (calls.built, calls.started, calls.restarts) == (1, 1, 1)
    assert snap["services"] == [{"name": "collector", "status": "green"},
                                {"name": "prometheus", "status": "green"}]


def test_shutdown_during_a_start_does_not_leave_the_stack_running():
    calls, pending = Calls(), []
    c = ObservabilityControl(
        build=lambda: calls.obs, start=lambda sup: None,
        restart_backend=lambda: setattr(calls, "restarts", calls.restarts + 1),
        is_installed=lambda: True, is_opted_out=lambda: False,
        run_async=pending.append,                       # the start thread has not run yet
    )
    c.enable()
    assert c.snapshot()["status"] == "starting"
    c.shutdown()                                        # launcher exits mid-start
    pending[0]()                                        # the start thread now runs
    assert calls.obs.shutdowns == 1                     # the stack it built is torn down
    assert calls.restarts == 0
    assert c.snapshot()["status"] == "stopped"


def test_the_real_start_wiring_reports_a_failed_stack_as_an_error(monkeypatch):
    """_start_observability_stack swallows start_all() errors for the tray; the
    desktop control must not, or a dead stack shows as running and the backend
    restarts toward a collector that is not there."""
    from cymatix_context.launcher import app as app_mod

    class DeadStack(FakeObs):
        def start_all(self):
            raise RuntimeError("port 4317 busy")

    restarts = []
    sup = SimpleNamespace(is_running=lambda: True, restart=lambda reason="": restarts.append(reason))
    monkeypatch.setattr(app_mod, "_maybe_build_observability", lambda: (DeadStack(), False))
    monkeypatch.setattr(app_mod, "_observability_install_complete", lambda: True)
    monkeypatch.setattr(app_mod, "_should_skip_observability", lambda: False)
    ctl = app_mod._build_observability_control(sup)
    ctl._run_async = lambda fn: fn()
    ctl.enable()
    snap = ctl.snapshot()
    assert snap["status"] == "error" and "4317" in snap["error"]
    assert restarts == []


def test_a_stack_whose_collector_never_binds_is_an_error(monkeypatch):
    from cymatix_context.launcher import app as app_mod
    import cymatix_context.launcher.observability_health as health

    class Quiet(FakeObs):
        def start_all(self):
            pass

    sup = SimpleNamespace(is_running=lambda: True, restart=lambda reason="": None)
    monkeypatch.setattr(app_mod, "_maybe_build_observability", lambda: (Quiet(), False))
    monkeypatch.setattr(app_mod, "_observability_install_complete", lambda: True)
    monkeypatch.setattr(app_mod, "_should_skip_observability", lambda: False)
    monkeypatch.setattr(health, "is_port_bound", lambda host, port: False)
    ctl = app_mod._build_observability_control(sup)
    ctl._run_async = lambda fn: fn()
    ctl.enable()
    assert ctl.snapshot()["status"] == "error"


def test_a_failed_start_reports_the_error_and_can_retry():
    calls = Calls()
    c = _control(calls, build_error=RuntimeError("port busy"))
    c.enable()
    snap = c.snapshot()
    assert snap["status"] == "error" and "port busy" in snap["error"]
    assert calls.restarts == 0                          # never point the backend at a dead collector


def test_disable_stops_the_stack_clears_the_env_and_restarts():
    calls = Calls()
    c = _control(calls, export=True)
    c.enable()
    assert os.environ.get("CYMATIX_OTEL_ENABLED") == "1"
    c.disable()
    assert calls.obs.shutdowns == 1
    assert "CYMATIX_OTEL_ENABLED" not in os.environ
    assert calls.restarts == 2                          # once to start exporting, once to stop
    assert c.snapshot()["status"] == "stopped"


def test_an_env_the_user_set_is_left_alone(monkeypatch):
    monkeypatch.setenv("CYMATIX_OTEL_ENABLED", "1")
    calls = Calls()
    c = _control(calls)                                 # start() does not export here
    c.enable()
    c.disable()
    assert os.environ["CYMATIX_OTEL_ENABLED"] == "1"


def test_shutdown_stops_the_stack_without_restarting_the_backend():
    calls = Calls()
    c = _control(calls)
    c.enable()
    c.shutdown()
    assert calls.obs.shutdowns == 1 and calls.restarts == 1


# ── through the launcher app ────────────────────────────────────────────


def _client(control):
    app = create_app(store=SimpleNamespace(), supervisor=FakeSupervisor(),
                     collector=FakeCollector(), observability_control=control)
    return TestClient(app)


def test_panel_offers_enable_when_stopped():
    with _client(_control(Calls())) as c:
        html = c.get("/api/state/panels").text
    assert 'data-action="obs-enable"' in html
    assert 'data-action="obs-disable"' not in html


def test_panel_offers_stop_when_running():
    ctl = _control(Calls())
    ctl.enable()
    with _client(ctl) as c:
        html = c.get("/api/state/panels").text
    assert 'data-action="obs-disable"' in html
    assert "obs-dot--green" in html


def test_panel_explains_a_missing_install_without_a_button():
    with _client(_control(Calls(), installed=False)) as c:
        html = c.get("/api/state/panels").text
    assert "install-native-observability" in html
    assert 'data-action="obs-enable"' not in html


def test_enable_route_and_not_installed_conflict():
    calls = Calls()
    with _client(_control(calls)) as c:
        r = c.post("/api/control/observability/enable")
        assert r.status_code == 202 and r.json()["status"] == "running"
    with _client(_control(Calls(), installed=False)) as c:
        assert c.post("/api/control/observability/enable").status_code == 409


def test_routes_404_without_a_controller():
    with _client(None) as c:
        assert c.post("/api/control/observability/enable").status_code == 404
    with _client(_control(Calls())) as c:
        assert c.post("/api/control/observability/bogus").status_code == 404
