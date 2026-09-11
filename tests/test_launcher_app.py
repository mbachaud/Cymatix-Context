"""
Tests for cymatix_context.launcher.app — FastAPI endpoints with mocked
supervisor + collector. No real cymatix process is spawned.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from cymatix_context.config import HeadroomConfig, CymatixConfig, ServerConfig
from cymatix_context.launcher.app import create_app
from cymatix_context.launcher.supervisor import (
    AlreadyRunning,
    NotRunning,
    ShutdownTimeout,
    SupervisorError,
)


@pytest.fixture
def fake_store():
    store = MagicMock()
    store.state.cymatix_pid = None
    store.state.last_restart_reason = None
    store.state.last_restart_at = None
    return store


@pytest.fixture
def fake_supervisor(fake_store, tmp_path):
    sup = MagicMock()
    sup.store = fake_store
    sup.store.path = tmp_path / "state.json"
    sup.cymatix_host = "127.0.0.1"
    sup.cymatix_port = 11437
    sup.cymatix_log_path = tmp_path / "cymatix.log"
    sup.is_running.return_value = False
    sup.get_pid.return_value = None
    sup.get_uptime_s.return_value = None
    sup.adopt.return_value = False
    sup.find_orphan_cymatix.return_value = None
    sup.get_last_error.return_value = None
    sup.owns_process.return_value = False
    return sup


@pytest.fixture
def fake_collector():
    collector = MagicMock()
    collector.collect.return_value = {
        "cymatix": {
            "running": False,
            "host": "127.0.0.1",
            "port": 11437,
        }
    }
    return collector


@pytest.fixture
def client(fake_store, fake_supervisor, fake_collector):
    app = create_app(store=fake_store, supervisor=fake_supervisor, collector=fake_collector)
    with TestClient(app) as c:
        yield c


class TestDashboardHTML:
    def test_root_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        # Page must contain the brand and the empty-state message
        assert "Cymatix Launcher" in resp.text
        assert "Cymatix is stopped" in resp.text

    def test_root_renders_running_state(self, client, fake_supervisor, fake_collector):
        fake_supervisor.is_running.return_value = True
        fake_supervisor.get_pid.return_value = 12345
        fake_collector.collect.return_value = {
            "cymatix": {"running": True, "pid": 12345, "port": 11437},
            "genes": {
                "total": 8000,
                "raw_chars": 47_000_000,
                "compressed_chars": 17_500_000,
                "compression_ratio": 2.69,
            },
        }
        resp = client.get("/")
        assert resp.status_code == 200
        assert "8,000" in resp.text or "8000" in resp.text


class TestApiState:
    def test_api_state_returns_collector_payload(self, client, fake_collector):
        fake_collector.collect.return_value = {"cymatix": {"running": False, "port": 11437}}
        resp = client.get("/api/state")
        assert resp.status_code == 200
        # v0.7.0: /api/state additionally carries the launcher-side
        # observability snapshot (None when no sidecar is wired in).
        assert resp.json() == {
            "cymatix": {
                "running": False,
                "port": 11437,
                "start_pending": False,
            },
            "observability": None,
            "needs_db_selection": False,
            "bench": None,
        }


class TestLauncherOwnership:
    def test_shutdown_does_not_stop_adopted_cymatix(self, fake_store, fake_supervisor, fake_collector):
        fake_supervisor.adopt.return_value = True
        fake_supervisor.is_running.return_value = True
        fake_supervisor.owns_process.return_value = False

        app = create_app(store=fake_store, supervisor=fake_supervisor, collector=fake_collector)
        with TestClient(app):
            pass

        fake_supervisor.stop.assert_not_called()

    def test_shutdown_stops_owned_cymatix(self, fake_store, fake_supervisor, fake_collector):
        fake_supervisor.adopt.return_value = False
        fake_supervisor.is_running.return_value = True
        fake_supervisor.owns_process.return_value = True

        app = create_app(store=fake_store, supervisor=fake_supervisor, collector=fake_collector)
        with TestClient(app):
            pass

        fake_supervisor.stop.assert_called_once()


class TestPanelsPartial:
    def test_panels_partial_returns_html(self, client):
        resp = client.get("/api/state/panels")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        # Empty state when cymatix down
        assert "Cymatix is stopped" in resp.text

    def test_panels_partial_browser_navigation_redirects_to_dashboard(self, client):
        resp = client.get(
            "/api/state/panels",
            headers={"sec-fetch-mode": "navigate"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

    def test_panels_partial_renders_degraded_message(self, client, fake_collector):
        fake_collector.collect.return_value = {
            "cymatix": {
                "running": True,
                "availability": "degraded",
                "next_action": "Restart it from the launcher UI.",
                "health_message": "Upstream model server is unreachable.",
                "port": 11437,
            }
        }
        resp = client.get("/api/state/panels")
        assert resp.status_code == 200
        assert "Upstream model server is unreachable." in resp.text

    def test_panels_partial_renders_disconnected_agents(self, client, fake_collector):
        fake_collector.collect.return_value = {
            "cymatix": {
                "running": True,
                "availability": "available",
                "port": 11437,
            },
            "disconnected_agents": {
                "count": 1,
                "entries": [
                    {
                        "handle": "raude",
                        "participant_id_short": "abc12345",
                        "participant_id": "abc12345-full",
                        "status": "stale",
                        "last_seen_s_ago": 3600,
                        "identifier": "swift_wing21",
                    }
                ],
            },
        }
        resp = client.get("/api/state/panels")
        assert resp.status_code == 200
        assert "Disconnected Agents" in resp.text
        assert "abc12345-full" in resp.text


class TestControlStart:
    def test_start_success(self, client, fake_supervisor):
        fake_supervisor.start.return_value = 99999
        fake_supervisor.last_start_pending = False
        resp = client.post("/api/control/start")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "pid": 99999, "started_pending": False}

    def test_start_already_running_returns_409(self, client, fake_supervisor):
        fake_supervisor.start.side_effect = AlreadyRunning("already")
        resp = client.post("/api/control/start")
        assert resp.status_code == 409

    def test_start_pending_returns_202_with_started_pending_field(
        self, client, fake_supervisor
    ):
        """Regression for #72: PR #68 made supervisor.start() return pid
        on /stats timeout (proc left running for tray's next poll). REST
        callers must see a distinct alive-but-not-ready signal instead of
        the success-shaped 200."""
        fake_supervisor.start.return_value = 99999
        fake_supervisor.last_start_pending = True
        resp = client.post("/api/control/start")
        assert resp.status_code == 202
        body = resp.json()
        assert body["ok"] is True
        assert body["pid"] == 99999
        assert body["started_pending"] is True
        assert "did not answer" in body["message"]

    def test_start_supervisor_error_returns_500(self, client, fake_supervisor):
        """Non-timeout supervisor failures (port collision, psutil missing,
        etc) still surface as 500 — only /stats timeouts are 202."""
        fake_supervisor.start.side_effect = SupervisorError("port collision")
        resp = client.post("/api/control/start")
        assert resp.status_code == 500


class TestControlStop:
    def test_stop_success(self, client, fake_supervisor):
        fake_supervisor.stop.return_value = None
        resp = client.post("/api/control/stop")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_stop_not_running_returns_409(self, client, fake_supervisor):
        fake_supervisor.stop.side_effect = NotRunning("not running")
        resp = client.post("/api/control/stop")
        assert resp.status_code == 409

    def test_stop_shutdown_timeout_returns_408(self, client, fake_supervisor):
        fake_supervisor.stop.side_effect = ShutdownTimeout("port stuck")
        resp = client.post("/api/control/stop")
        assert resp.status_code == 408


class TestControlRestart:
    def test_restart_success(self, client, fake_supervisor):
        fake_supervisor.restart.return_value = 88888
        fake_supervisor.last_start_pending = False
        resp = client.post("/api/control/restart")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "pid": 88888, "started_pending": False}

    def test_restart_pending_returns_202_with_started_pending_field(
        self, client, fake_supervisor
    ):
        """Same alive-but-not-ready surface as /api/control/start (#72)."""
        fake_supervisor.restart.return_value = 88888
        fake_supervisor.last_start_pending = True
        resp = client.post("/api/control/restart")
        assert resp.status_code == 202
        body = resp.json()
        assert body["ok"] is True
        assert body["pid"] == 88888
        assert body["started_pending"] is True
        assert "did not answer" in body["message"]


class TestNativeFailFast:
    def test_main_exits_1_when_native_without_pywebview(self, monkeypatch):
        """--native must fail loudly when pywebview isn't available, not silently exit."""
        from cymatix_context.launcher import app as app_mod

        monkeypatch.setattr(app_mod, "_check_native_available", lambda: False)
        rc = app_mod.main(["--native", "--no-browser", "--no-autostart"])
        assert rc == 1

    def test_check_native_available_returns_bool(self):
        from cymatix_context.launcher.app import _check_native_available
        assert isinstance(_check_native_available(), bool)


class TestMaybeBuildHeadroom:
    def test_adopts_running_headroom_even_when_disabled(self, fake_store, monkeypatch):
        from cymatix_context.launcher import app as app_mod
        from cymatix_context import config as config_mod

        class FakeHeadroomSupervisor:
            def __init__(self, store, host, port, mode):
                self.store = store
                self.host = host
                self.port = port
                self.mode = mode
                self.start_calls = 0

            def adopt(self):
                return True

            def start(self):
                self.start_calls += 1
                return 4242

        cfg = CymatixConfig(
            headroom=HeadroomConfig(
                enabled=False,
                autostart=True,
                host="127.0.0.1",
                port=8787,
                mode="token",
                dashboard_path="/dashboard",
            )
        )

        monkeypatch.setattr(app_mod, "is_headroom_installed", lambda: True)
        monkeypatch.setattr(app_mod, "HeadroomSupervisor", FakeHeadroomSupervisor)
        monkeypatch.setattr(config_mod, "load_config", lambda: cfg)

        headroom, dashboard_url = app_mod._maybe_build_headroom(fake_store)

        assert headroom is not None
        assert dashboard_url == "http://127.0.0.1:8787/dashboard"
        assert headroom.start_calls == 0

    def test_disabled_headroom_without_running_proxy_stays_hidden(
        self,
        fake_store,
        monkeypatch,
    ):
        from cymatix_context.launcher import app as app_mod
        from cymatix_context import config as config_mod

        instances = []

        class FakeHeadroomSupervisor:
            def __init__(self, store, host, port, mode):
                self.store = store
                self.host = host
                self.port = port
                self.mode = mode
                self.start_calls = 0
                instances.append(self)

            def adopt(self):
                return False

            def start(self):
                self.start_calls += 1
                return 4242

        cfg = CymatixConfig(
            headroom=HeadroomConfig(
                enabled=False,
                autostart=True,
                host="127.0.0.1",
                port=8787,
                mode="token",
                dashboard_path="/dashboard",
            )
        )

        monkeypatch.setattr(app_mod, "is_headroom_installed", lambda: True)
        monkeypatch.setattr(app_mod, "HeadroomSupervisor", FakeHeadroomSupervisor)
        monkeypatch.setattr(config_mod, "load_config", lambda: cfg)

        headroom, dashboard_url = app_mod._maybe_build_headroom(
            fake_store,
            autostart_override=True,
        )

        assert headroom is None
        assert dashboard_url is None
        assert len(instances) == 1
        assert instances[0].start_calls == 0


class TestHeadroomAutoRoute:
    def test_remote_upstream_does_not_route_when_route_upstream_disabled(self, monkeypatch):
        """Default config (route_upstream=False) must NOT rewrite the
        upstream even for a remote target. Pre-fix this routed silently
        and pointed cymatix at a dead :8787 when Headroom wasn't installed."""
        from cymatix_context.launcher import app as app_mod

        cfg = CymatixConfig(
            server=ServerConfig(upstream="https://api.openai.com/v1"),
            headroom=HeadroomConfig(host="127.0.0.1", port=8787),
        )
        assert cfg.headroom.route_upstream is False  # contract guard

        monkeypatch.delenv("CYMATIX_SERVER_UPSTREAM", raising=False)
        monkeypatch.delenv("OPENAI_TARGET_API_URL", raising=False)
        # auto_override=None means "defer to config" — the precedence path under
        # test (CYMATIX_HEADROOM_ROUTE_UPSTREAM_AUTO unset at the call site).
        routed = app_mod._configure_cymatix_upstream_routing(cfg, auto_override=None)

        assert routed is False
        assert "CYMATIX_SERVER_UPSTREAM" not in os.environ
        assert "OPENAI_TARGET_API_URL" not in os.environ

    def test_remote_upstream_routes_when_route_upstream_opted_in(self, monkeypatch):
        """Explicit opt-in via [headroom] route_upstream = true keeps the
        original auto-route behavior for operators who do want it."""
        from cymatix_context.launcher import app as app_mod

        cfg = CymatixConfig(
            server=ServerConfig(upstream="https://api.openai.com/v1"),
            headroom=HeadroomConfig(host="127.0.0.1", port=8787, route_upstream=True),
        )

        monkeypatch.delenv("CYMATIX_SERVER_UPSTREAM", raising=False)
        monkeypatch.delenv("OPENAI_TARGET_API_URL", raising=False)
        routed = app_mod._configure_cymatix_upstream_routing(cfg, auto_override=None)

        assert routed is True
        assert os.environ["CYMATIX_SERVER_UPSTREAM"] == "http://127.0.0.1:8787"
        assert os.environ["OPENAI_TARGET_API_URL"] == "https://api.openai.com/v1"

    def test_env_var_false_forces_off_even_when_config_opted_in(self, monkeypatch):
        """CYMATIX_HEADROOM_ROUTE_UPSTREAM_AUTO=0 is the per-launch kill
        switch: must override route_upstream=True. Useful when the proxy
        is misbehaving and the operator wants cymatix direct for one session."""
        from cymatix_context.launcher import app as app_mod

        cfg = CymatixConfig(
            server=ServerConfig(upstream="https://api.openai.com/v1"),
            headroom=HeadroomConfig(host="127.0.0.1", port=8787, route_upstream=True),
        )

        monkeypatch.delenv("CYMATIX_SERVER_UPSTREAM", raising=False)
        monkeypatch.delenv("OPENAI_TARGET_API_URL", raising=False)
        # auto_override=False simulates CYMATIX_HEADROOM_ROUTE_UPSTREAM_AUTO=0
        routed = app_mod._configure_cymatix_upstream_routing(cfg, auto_override=False)

        assert routed is False
        assert "CYMATIX_SERVER_UPSTREAM" not in os.environ
        assert "OPENAI_TARGET_API_URL" not in os.environ

    def test_env_var_true_forces_on_even_when_config_disabled(self, monkeypatch):
        """CYMATIX_HEADROOM_ROUTE_UPSTREAM_AUTO=1 is also a per-launch
        override: must turn routing on even when route_upstream=False
        in config. Symmetric to the kill-switch test."""
        from cymatix_context.launcher import app as app_mod

        cfg = CymatixConfig(
            server=ServerConfig(upstream="https://api.openai.com/v1"),
            headroom=HeadroomConfig(host="127.0.0.1", port=8787),  # route_upstream defaults False
        )

        monkeypatch.delenv("CYMATIX_SERVER_UPSTREAM", raising=False)
        monkeypatch.delenv("OPENAI_TARGET_API_URL", raising=False)
        routed = app_mod._configure_cymatix_upstream_routing(cfg, auto_override=True)

        assert routed is True
        assert os.environ["CYMATIX_SERVER_UPSTREAM"] == "http://127.0.0.1:8787"

    def test_local_ollama_upstream_stays_direct(self, monkeypatch):
        """Loopback upstream is never rewritten, even with route_upstream=True
        (Headroom proxy hop doesn't buy anything for a localhost model server)."""
        from cymatix_context.launcher import app as app_mod

        cfg = CymatixConfig(
            server=ServerConfig(upstream="http://localhost:11434"),
            headroom=HeadroomConfig(host="127.0.0.1", port=8787, route_upstream=True),
        )

        monkeypatch.setenv("CYMATIX_SERVER_UPSTREAM", "http://127.0.0.1:8787")
        monkeypatch.setenv("OPENAI_TARGET_API_URL", "https://api.openai.com/v1")

        routed = app_mod._configure_cymatix_upstream_routing(cfg, auto_override=True)

        assert routed is False
        assert "CYMATIX_SERVER_UPSTREAM" not in os.environ
        assert "OPENAI_TARGET_API_URL" not in os.environ


@pytest.mark.parametrize("env_value, expects_skip", [
    ("0", True),
    ("false", True),
    ("False", True),     # mixed case
    ("FALSE", True),     # all caps
    ("no", True),
    ("NO", True),
    ("off", True),
    ("Off", True),
    ("1", False),        # default opt-IN
    ("true", False),
    ("yes", False),
    ("anything", False), # unrecognized → opt-IN per spec semantics
    ("", False),         # empty → default opt-IN
])
def test_observability_env_opt_out(monkeypatch, env_value, expects_skip):
    """CYMATIX_OBSERVABILITY parsing is case-insensitive across recognised
    opt-out tokens; everything else (including unknown strings) falls
    through to the default opt-IN behaviour.

    Cleanup A: assertion is on the (supervisor, install_pending) tuple
    returned by _maybe_build_observability — the previous module-level
    _OBS_INSTALL_PENDING global has been removed."""
    monkeypatch.setenv("CYMATIX_OBSERVABILITY", env_value)
    # Stub install-complete so the opt-IN branches actually return a
    # supervisor rather than skipping due to missing binaries/configs.
    monkeypatch.setattr(
        "cymatix_context.launcher.app._observability_install_complete",
        lambda: True,
    )
    from cymatix_context.launcher.app import _maybe_build_observability
    sup, install_pending = _maybe_build_observability()
    if expects_skip:
        assert sup is None, (
            f"CYMATIX_OBSERVABILITY={env_value!r} should opt out, "
            f"but a supervisor was built"
        )
        # Opt-out path never marks install-pending; the user explicitly
        # disabled observability, so don't pester them with an install
        # balloon.
        assert install_pending is False
    else:
        assert sup is not None, (
            f"CYMATIX_OBSERVABILITY={env_value!r} should opt in, "
            f"but no supervisor was built"
        )
        # Opt-IN with install complete → no install balloon needed.
        assert install_pending is False


def test_observability_enabled_when_unset(monkeypatch, tmp_path):
    """Default — env unset → returns a supervisor (or None if configs
    haven't been rendered yet; either way, not silently disabled)."""
    monkeypatch.delenv("CYMATIX_OBSERVABILITY", raising=False)
    monkeypatch.setattr(
        "cymatix_context.launcher.app._observability_install_complete",
        lambda: True,
    )
    from cymatix_context.launcher.app import _maybe_build_observability
    sup, install_pending = _maybe_build_observability()
    assert sup is not None
    assert install_pending is False


def test_observability_skipped_when_install_incomplete(monkeypatch):
    """Install incomplete → supervisor not built, install_pending=True
    so the tray-startup block schedules the install-needed balloon.

    Cleanup A: previously this was tracked through a module-level
    _OBS_INSTALL_PENDING global + setter. The helper now returns the
    flag in the tuple so the caller doesn't depend on global state."""
    monkeypatch.delenv("CYMATIX_OBSERVABILITY", raising=False)
    monkeypatch.setattr(
        "cymatix_context.launcher.app._observability_install_complete",
        lambda: False,
    )
    from cymatix_context.launcher.app import _maybe_build_observability
    sup, install_pending = _maybe_build_observability()
    assert sup is None
    assert install_pending is True


# ---------------------------------------------------------------------
# Host status panel: the three existing routes, one shared projection.
#
# These drive a real StateCollector and a real StatusCache over a fake
# reader, so the projection under test is the shipped allowlist and the
# HTML under test is the shipped template. The fake report carries a
# secret sentinel in every field the allowlist is supposed to drop:
# native config paths, the server URL, its payload, its parse error, its
# error text, the inspected path list and an environment map. If any of
# them reaches the page or the JSON, these fail.
# ---------------------------------------------------------------------

import re

from cymatix_context.launcher.collector import StateCollector, project_host_status
from cymatix_context.launcher.status_cache import (
    GENERIC_NEXT_ACTION,
    OBSERVATION_SCOPE,
    SNAPSHOT_KEYS,
    StatusCache,
)

SENTINEL = "s3cr3t-do-not-render-3f9a"

APPROVED_HOST_STATUS_KEYS = set(SNAPSHOT_KEYS) | {"launcher"}
APPROVED_GROUP_KEYS = {
    "host": {"selection", "profile"},
    "server": {"transport", "health", "source", "configured_url_match"},
    "mcp": {"configuration", "activation", "live"},
    "skill": {"installation", "activation"},
    "launcher": {"state", "source", "observed_at"},
}

# Every route this app is allowed to serve. The host status panel is
# delivered through the three that already existed, so this list must
# not grow; it is the no-new-route check.
EXPECTED_ROUTES = {
    "/",
    "/api/control/bench/start",
    "/api/control/bench/stop",
    "/api/control/restart",
    "/api/control/start",
    "/api/control/stop",
    "/api/genome/create",
    "/api/genome/select",
    "/api/genomes",
    "/api/state",
    "/api/state/panels",
    "/docs",
    "/docs/oauth2-redirect",
    "/openapi.json",
    "/redoc",
    "/static",
}

_DIMENSION = re.compile(
    r"<dt>(?P<label>[^<]+)</dt>\s*"
    r'<dd class="host-status-value[^"]*">(?P<value>[^<]*)</dd>'
)


def _dimensions(html: str) -> dict:
    """Label to rendered value for every host status row in the page."""

    return {
        m.group("label").strip(): m.group("value").strip()
        for m in _DIMENSION.finditer(html)
    }


def _sentinel_report(**overrides):
    """A schema valid report whose every unapproved field is a secret."""

    report = {
        "host": {
            "selection": "claude-code",
            "profile": "Claude Code",
            "detail": SENTINEL,
            "config_path": "/home/" + SENTINEL + "/.claude.json",
        },
        "server": {
            "transport": "reachable",
            "health": "healthy",
            "source": "configured",
            "configured_url_match": True,
            "url": "http://127.0.0.1:11437/?token=" + SENTINEL,
            "payload": {"api_key": SENTINEL},
            "parse_error": "cannot parse " + SENTINEL,
            "error": "connection refused for " + SENTINEL,
        },
        "mcp": {
            "configuration": "canonical",
            "activation": "enabled",
            "live": "connected",
            "path": "/home/" + SENTINEL + "/.mcp.json",
            "detail": SENTINEL,
        },
        "skill": {
            "installation": "present",
            "activation": "enabled",
            "path": "/home/" + SENTINEL + "/skills",
        },
        "configured_ready": True,
        "guided_ready": True,
        "next_action": "Use cymatix_context for repo questions.",
        "inspected_paths": ["/home/" + SENTINEL + "/.codex/config.toml"],
        "launcher": {"url": "http://127.0.0.1:11438/" + SENTINEL, "state": "running"},
        "env": {"CYMATIX_API_KEY": SENTINEL},
    }
    report.update(overrides)
    return report


def _status_cache(reader, calls=None):
    """A real cache whose refresh runs inline, so the test is ordered."""

    def counted():
        if calls is not None:
            calls.append(1)
        return reader()

    return StatusCache(
        reader=counted,
        projector=project_host_status,
        context_factory=lambda: "route-test-context",
        spawn=lambda work: work(),
    )


@pytest.fixture
def status_calls():
    return []


@pytest.fixture
def host_status_client(fake_store, fake_supervisor, status_calls, monkeypatch, tmp_path):
    """create_app over a real collector with a fake host status reader."""

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    def build(reader, *, running=False):
        fake_supervisor.is_running.return_value = running
        collector = StateCollector(
            supervisor=fake_supervisor,
            status_cache=_status_cache(reader, status_calls),
        )
        app = create_app(
            store=fake_store, supervisor=fake_supervisor, collector=collector
        )
        return app, TestClient(app)

    return build


class TestHostStatusProjectionOnTheExistingRoutes:
    def test_json_carries_only_the_approved_projection(self, host_status_client):
        _, client = host_status_client(_sentinel_report)
        with client as c:
            payload = c.get("/api/state").json()["host_status"]

        assert set(payload) == APPROVED_HOST_STATUS_KEYS
        for group, keys in APPROVED_GROUP_KEYS.items():
            assert set(payload[group]) == keys, group
        assert payload["observation_scope"] == OBSERVATION_SCOPE
        assert payload["configured_ready"] is True
        assert payload["guided_ready"] is True
        assert payload["launcher"]["source"] == "supervisor"

    def test_no_sentinel_reaches_any_of_the_three_responses(self, host_status_client):
        _, client = host_status_client(_sentinel_report)
        with client as c:
            bodies = [
                c.get("/").text,
                c.get("/api/state/panels").text,
                c.get("/api/state").text,
            ]

        for body in bodies:
            assert SENTINEL not in body
            assert ".mcp.json" not in body
            assert "api_key" not in body
            assert "connection refused" not in body
            assert "inspected_paths" not in body
            # The cymatix port is elsewhere on the page from the old
            # aggregate, so scope the URL check to the new panel.
            panel = body.split("data-host-status", 1)[-1]
            assert "http://" not in panel

    def test_upstream_error_text_never_reaches_a_response(self, host_status_client):
        """An unreachable server is a successful observation, not a leak."""

        report = _sentinel_report(
            server={
                "transport": "unreachable",
                "health": "unknown",
                "source": "default",
                "configured_url_match": None,
                "error": "HTTPConnectionPool refused: " + SENTINEL,
                "parse_error": SENTINEL,
                "payload": {"stack": SENTINEL},
            },
            configured_ready=False,
            guided_ready=False,
            next_action="Start or repair the Cymatix server configured for this MCP entry.",
        )
        _, client = host_status_client(lambda: report)
        with client as c:
            html = c.get("/api/state/panels").text
            payload = c.get("/api/state").json()["host_status"]

        assert SENTINEL not in html
        assert "HTTPConnectionPool" not in html
        assert payload["server"] == {
            "transport": "unreachable",
            "health": "unknown",
            "source": "default",
            "configured_url_match": None,
        }
        rows = _dimensions(html)
        assert rows["Server transport"] == "Unreachable"
        assert rows["Server health"] == "Unknown"
        assert rows["Configured URL match"] == "Unknown"

    def test_all_three_routes_share_one_refresh(self, host_status_client, status_calls):
        _, client = host_status_client(_sentinel_report)
        with client as c:
            c.get("/")
            c.get("/api/state/panels")
            c.get("/api/state")

        assert len(status_calls) == 1

    def test_no_new_route_is_registered(self, host_status_client):
        app, _ = host_status_client(_sentinel_report)
        assert {route.path for route in app.routes} == EXPECTED_ROUTES


class TestHostStatusRendering:
    def test_every_dimension_renders_independently(self, host_status_client):
        report = _sentinel_report(
            server={
                "transport": "reachable",
                "health": "unknown",
                "source": "default",
                "configured_url_match": False,
            },
            mcp={"configuration": "noncanonical", "activation": "unknown", "live": "unknown"},
            skill={"installation": "present", "activation": "disabled"},
            configured_ready=False,
            guided_ready=False,
            next_action="Add or repair the canonical cymatix-context MCP entry for this host.",
        )
        _, client = host_status_client(lambda: report)
        with client as c:
            rows = _dimensions(c.get("/").text)

        # Reachable is not healthy, configured is not activated,
        # installed is not enabled, and none of them is readiness.
        assert rows["Server transport"] == "Reachable"
        assert rows["Server health"] == "Unknown"
        assert rows["Server URL source"] == "Default"
        assert rows["Configured URL match"] == "Differs"
        assert rows["MCP entry"] == "Noncanonical"
        assert rows["MCP activation"] == "Unknown"
        assert rows["MCP live session"] == "Unknown"
        assert rows["Skill installation"] == "Present"
        assert rows["Skill activation"] == "Disabled"
        assert rows["Configured readiness"] == "Not ready"
        assert rows["Guided readiness"] == "Not ready"

    def test_mcp_live_is_labelled_registry_evidence(self, host_status_client):
        _, client = host_status_client(_sentinel_report)
        with client as c:
            html = c.get("/").text

        live = html.split("MCP live session", 1)[1].split("</div>", 1)[0]
        assert "Registry evidence only" in live
        assert "authenticated" not in live.lower()

    def test_ambiguous_and_unknown_host_selection_stay_distinct(self, host_status_client):
        ambiguous = _sentinel_report(
            host={"selection": "ambiguous", "profile": None},
            configured_ready=None,
            guided_ready=None,
            next_action=(
                "Multiple host configs contain cymatix-context; pass --host explicitly."
            ),
        )
        unknown = _sentinel_report(
            host={"selection": "unknown", "profile": None},
            configured_ready=False,
            guided_ready=False,
            next_action="Pass --host or add one canonical cymatix-context MCP entry.",
        )
        _, client = host_status_client(lambda: ambiguous)
        with client as c:
            assert _dimensions(c.get("/").text)["Host"] == "Ambiguous"
        _, client = host_status_client(lambda: unknown)
        with client as c:
            assert _dimensions(c.get("/").text)["Host"] == "Unknown"

    def test_null_readiness_renders_unknown_never_false(self, host_status_client):
        report = _sentinel_report(
            server={
                "transport": "reachable",
                "health": "unknown",
                "source": "configured",
                "configured_url_match": True,
            },
            configured_ready=None,
            guided_ready=None,
            next_action="Start or repair the Cymatix server configured for this MCP entry.",
        )
        _, client = host_status_client(lambda: report)
        with client as c:
            html = c.get("/").text
            payload = c.get("/api/state").json()["host_status"]

        rows = _dimensions(html)
        assert rows["Configured readiness"] == "Unknown"
        assert rows["Guided readiness"] == "Unknown"
        assert "Unknown is never false" in html
        assert payload["configured_ready"] is None
        assert payload["guided_ready"] is None

    def test_supervisor_launcher_state_is_its_own_dimension(self, host_status_client):
        _, client = host_status_client(_sentinel_report, running=True)
        with client as c:
            html = c.get("/").text
            payload = c.get("/api/state").json()["host_status"]

        rows = _dimensions(html)
        assert rows["Supervised child"] == "Running"
        assert rows["Child evidence from"] == "supervisor"
        assert payload["launcher"]["state"] == "running"
        assert payload["launcher"]["observed_at"] is not None

    def test_component_is_present_when_the_child_is_stopped(self, host_status_client):
        _, client = host_status_client(_sentinel_report, running=False)
        with client as c:
            root = c.get("/").text
            partial = c.get("/api/state/panels").text

        for body in (root, partial):
            assert "data-host-status" in body
            assert "Host status" in body
            assert _dimensions(body)["Supervised child"] == "Stopped"
            assert _dimensions(body)["Configured readiness"] == "Ready"
        # The stopped-child empty state still renders alongside it.
        assert "Cymatix is stopped" in root

    def test_unavailable_observation_is_distinct_from_not_ready(self, host_status_client):
        def explode():
            raise RuntimeError("native config read failed for " + SENTINEL)

        _, client = host_status_client(explode)
        with client as c:
            html = c.get("/").text
            payload = c.get("/api/state").json()["host_status"]

        assert SENTINEL not in html
        assert 'data-freshness="unavailable"' in html
        assert "No observation available yet" in html
        rows = _dimensions(html)
        assert rows["Configured readiness"] == "Unknown"
        assert rows["Server health"] == "Unknown"
        assert rows["MCP entry"] == "Unknown"
        # The supervised child is sampled separately, so it is still real.
        assert rows["Supervised child"] == "Stopped"
        assert payload["freshness"] == "unavailable"
        assert payload["host"] is None
        assert payload["next_action"] == GENERIC_NEXT_ACTION

    def test_freshness_attributes_are_serialised_for_presentation_aging(
        self, host_status_client
    ):
        _, client = host_status_client(_sentinel_report)
        with client as c:
            html = c.get("/").text
            payload = c.get("/api/state").json()["host_status"]

        assert 'data-freshness="fresh"' in html
        assert "data-fresh-for-s=" in html
        assert "data-age-s=" in html
        assert payload["fresh_for_s"] is not None
        assert payload["age_s"] is not None
        assert "aged past its" in html


class TestHostStatusEscapingAndGuidance:
    def test_unapproved_guidance_is_replaced_not_escaped(self, host_status_client):
        report = _sentinel_report(
            next_action="<b>paste this key: " + SENTINEL + "</b>",
        )
        _, client = host_status_client(lambda: report)
        with client as c:
            html = c.get("/").text
            payload = c.get("/api/state").json()["host_status"]

        assert SENTINEL not in html
        assert "&lt;b&gt;" not in html
        assert payload["next_action"] == GENERIC_NEXT_ACTION
        assert GENERIC_NEXT_ACTION in html

    def test_markup_metacharacters_in_text_are_escaped_never_executed(
        self, fake_store, fake_supervisor
    ):
        """The template escapes even text the allowlist let through.

        The approved vocabulary has no markup in it today, which is
        exactly why this drives a crafted projection straight into the
        template: the render must not depend on that staying true.
        """
        marked = '<img src=x onerror="alert(1)">&'
        collector = MagicMock()
        collector.collect.return_value = {
            "cymatix": {"running": False, "port": 11437},
            "host_status": {
                "observation_scope": marked,
                "freshness": "fresh",
                "refresh_state": "idle",
                "observed_at": "2026-09-11T06:12:44Z",
                "last_success_at": "2026-09-11T06:12:44Z",
                "last_attempt_at": "2026-09-11T06:12:44Z",
                "age_s": 0.4,
                "fresh_for_s": 4.6,
                "host": {"selection": "claude-code", "profile": marked},
                "server": {
                    "transport": "reachable",
                    "health": "healthy",
                    "source": marked,
                    "configured_url_match": True,
                },
                "mcp": {
                    "configuration": "canonical",
                    "activation": "enabled",
                    "live": "connected",
                },
                "skill": {"installation": "present", "activation": "enabled"},
                "launcher": {"state": "running", "source": marked, "observed_at": None},
                "configured_ready": True,
                "guided_ready": True,
                "next_action": marked,
            },
        }
        app = create_app(
            store=fake_store, supervisor=fake_supervisor, collector=collector
        )
        with TestClient(app) as client:
            html = client.get("/").text

        assert "<img src=x" not in html
        assert 'onerror="' not in html
        assert "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;&amp;" in html

    def test_next_action_render_is_bounded(self, fake_store, fake_supervisor):
        long_text = "A" * 4000
        collector = MagicMock()
        collector.collect.return_value = {
            "cymatix": {"running": False, "port": 11437},
            "host_status": {
                "observation_scope": OBSERVATION_SCOPE,
                "freshness": "fresh",
                "refresh_state": "idle",
                "observed_at": None,
                "last_success_at": None,
                "last_attempt_at": None,
                "age_s": None,
                "fresh_for_s": None,
                "host": None,
                "server": None,
                "mcp": None,
                "skill": None,
                "launcher": {"state": "stopped", "source": "supervisor", "observed_at": None},
                "configured_ready": None,
                "guided_ready": None,
                "next_action": long_text,
            },
        }
        app = create_app(
            store=fake_store, supervisor=fake_supervisor, collector=collector
        )
        with TestClient(app) as client:
            html = client.get("/").text

        assert "A" * 500 in html
        assert "A" * 501 not in html


class TestHostStatusBrowserContract:
    """What the served static files are allowed to do for this panel.

    There is no JS runtime in this suite, so these pin the contract for
    launcher.js: bound the fetches that already exist,
    show staleness in words, age the observation, and add no second
    loop and no second endpoint.
    """

    def _asset(self, name: str) -> str:
        from cymatix_context.launcher.app import STATIC_DIR

        return (STATIC_DIR / name).read_text(encoding="utf-8")

    def test_panel_fetches_are_finite_and_always_cleaned_up(self):
        js = self._asset("launcher.js")
        assert "AbortController" in js
        assert "controller.abort()" in js
        assert js.count("clearTimeout(timer)") == 1
        assert "finally {" in js

    def test_no_second_poll_loop_and_no_new_endpoint(self):
        js = self._asset("launcher.js")
        # Two intervals, both of which predate this panel: the dashboard
        # poll and the first-boot database modal poll. The host status
        # panel rides the first one and adds neither a third timer nor
        # an endpoint of its own.
        assert js.count("setInterval(") == 2
        assert "setInterval(pollDbModal" in js
        assert "host-status" not in js.replace("data-host-status", "")
        assert "/api/status" not in js
        polled = set(re.findall(r"await fetchBounded\(\s*\n?\s*([^,]+),", js))
        assert polled == {"pollUrl", '"/api/state"'}

    def test_stale_state_is_visible_in_words(self):
        css = self._asset("launcher.css")
        assert '.panels[data-stale="true"]::before' in css
        assert "Live updates are not arriving" in css

    def test_expired_observation_has_its_own_neutral_presentation(self):
        css = self._asset("launcher.css")
        assert '.panel--host-status[data-observation="expired"] .host-status-aged' in css
        assert '.panel--host-status[data-freshness="stale"] .host-status-value' in css
