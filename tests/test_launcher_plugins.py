"""Launcher wiring for plugins: /api/plugins, actions, routers, panels, services."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from cymatix_context.launcher.app import create_app
from cymatix_context.plugins.api import MenuItem, Panel
from cymatix_context.plugins.loader import LoadedPlugin

LOOPBACK = {"Host": "127.0.0.1:11438"}
ACTION = {"Host": "127.0.0.1:11438", "X-Cymatix-Action": "1"}


class FakePlugin:
    name = "demo"
    version = "1.2.3"
    requires_core = ">=0"

    def __init__(self):
        self.actions = []
        self.panel_html = "<p>hello mobile</p>"
        self.router = APIRouter()

        @self.router.get("/ping")
        def ping():
            return {"pong": True}

        @self.router.post("/echo")
        def echo():
            self.actions.append("echo")
            return {"echoed": True}

        @self.router.get("/boom")
        def boom():
            raise RuntimeError("secret traceback detail")

    def menu(self):
        return [MenuItem("pair", "Pair a phone"), MenuItem("off", "Off", enabled=False)]

    def on_action(self, action_id):
        self.actions.append(action_id)
        return {"did": action_id}

    def status(self):
        return {"level": "warn", "text": "not paired"}

    def routes(self):
        return self.router

    def panels(self):
        return [Panel("mobile", "Mobile <b>", lambda: self.panel_html)]


def _loaded(obj=None, name="demo"):
    obj = obj or FakePlugin()
    return LoadedPlugin(name, "1.2.3", "entry_point", False, "loaded", None, obj)


@pytest.fixture
def parts():
    store = MagicMock()
    sup = MagicMock()
    sup.cymatix_host = "127.0.0.1"
    sup.cymatix_port = 11437
    sup.is_running.return_value = False
    sup.owns_process.return_value = False
    sup.adopt.return_value = False
    collector = MagicMock()
    collector.collect.side_effect = lambda: {
        "cymatix": {"running": False, "host": "127.0.0.1", "port": 11437}
    }
    return store, sup, collector


def _client(parts, plugins=None, services=None, **kw):
    store, sup, collector = parts
    app = create_app(
        store=store, supervisor=sup, collector=collector,
        plugins=plugins, plugin_services=services, **kw,
    )
    return TestClient(app)


def test_api_plugins_lists_menu_and_status(parts):
    services = MagicMock()
    services.snapshot.return_value = [{"plugin": "demo", "service": "s", "running": True}]
    lp = _loaded()
    with _client(parts, [lp], services) as c:
        body = c.get("/api/plugins", headers=LOOPBACK).json()
    assert body["services"] == [{"plugin": "demo", "service": "s", "running": True}]
    (p,) = body["plugins"]
    assert p["name"] == "demo" and p["version"] == "1.2.3"
    assert p["source"] == "entry_point" and p["signed"] is False
    assert p["status"] == "loaded" and p["reason"] is None
    assert p["menu"] == [
        {"id": "pair", "label": "Pair a phone", "enabled": True},
        {"id": "off", "label": "Off", "enabled": False},
    ]
    assert p["status_info"] == {"level": "warn", "text": "not paired"}


def test_api_plugins_without_services(parts):
    with _client(parts, [_loaded()]) as c:
        assert c.get("/api/plugins", headers=LOOPBACK).json()["services"] == []


def test_action_dispatches_on_action(parts):
    obj = FakePlugin()
    with _client(parts, [_loaded(obj)]) as c:
        r = c.post("/api/plugins/demo/actions/pair", headers=ACTION)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "result": {"did": "pair"}}
    assert obj.actions == ["pair"]


def test_action_without_header_refused(parts):
    obj = FakePlugin()
    with _client(parts, [_loaded(obj)]) as c:
        r = c.post("/api/plugins/demo/actions/pair", headers=LOOPBACK)
    assert r.status_code == 403
    assert r.json() == {"error": "plugin action requires X-Cymatix-Action header"}
    assert obj.actions == []


def test_action_non_loopback_host_refused(parts):
    obj = FakePlugin()
    with _client(parts, [_loaded(obj)]) as c:
        r = c.post(
            "/api/plugins/demo/actions/pair",
            headers={"Host": "evil.example", "X-Cymatix-Action": "1"},
        )
    assert r.status_code == 403
    assert r.json() == {"error": "non-loopback host"}
    assert obj.actions == []


@pytest.mark.parametrize("host", ["localhost:1", "LOCALHOST", "[::1]:11438", "127.0.0.1"])
def test_loopback_host_spellings_accepted(parts, host):
    with _client(parts, [_loaded()]) as c:
        r = c.post(
            "/api/plugins/demo/actions/pair",
            headers={"Host": host, "X-Cymatix-Action": "1"},
        )
    assert r.status_code == 200


def test_unknown_plugin_and_action_404(parts):
    with _client(parts, [_loaded()]) as c:
        r = c.post("/api/plugins/nope/actions/pair", headers=ACTION)
        assert r.status_code == 404 and r.json() == {"error": "unknown plugin"}
        r = c.post("/api/plugins/demo/actions/nope", headers=ACTION)
        assert r.status_code == 404 and r.json() == {"error": "unknown action"}


def test_action_non_dict_result_disables_plugin(parts):
    obj = FakePlugin()
    obj.on_action = lambda action_id: "not a dict"
    lp = _loaded(obj)
    with _client(parts, [lp]) as c:
        r = c.post("/api/plugins/demo/actions/pair", headers=ACTION)
        assert r.status_code == 500 and r.json() == {"error": "plugin error"}
        assert lp.status == "disabled"
        r = c.post("/api/plugins/demo/actions/pair", headers=ACTION)
        assert r.status_code == 404 and r.json() == {"error": "unknown plugin"}


def test_action_raising_gives_500_and_disables(parts):
    obj = FakePlugin()

    def bad(action_id):
        raise RuntimeError("kaboom")

    obj.on_action = bad
    lp = _loaded(obj)
    with _client(parts, [lp]) as c:
        r = c.post("/api/plugins/demo/actions/pair", headers=ACTION)
    assert r.status_code == 500 and r.json() == {"error": "plugin error"}
    assert lp.status == "disabled"


def test_plugin_router_mounted_and_post_guarded(parts):
    obj = FakePlugin()
    with _client(parts, [_loaded(obj)]) as c:
        assert c.get("/api/plugins/demo/ping", headers=LOOPBACK).json() == {"pong": True}
        r = c.post("/api/plugins/demo/echo", headers=LOOPBACK)
        assert r.status_code == 403
        r = c.post("/api/plugins/demo/echo", headers={"Host": "evil.example", "X-Cymatix-Action": "1"})
        assert r.status_code == 403
        assert obj.actions == []
        r = c.post("/api/plugins/demo/echo", headers=ACTION)
        assert r.status_code == 200 and obj.actions == ["echo"]


def test_plugin_router_error_contained(parts):
    lp = _loaded()
    with _client(parts, [lp]) as c:
        r = c.get("/api/plugins/demo/boom", headers=LOOPBACK)
        assert r.status_code == 500
        assert r.json() == {"error": "plugin error"}
        assert "secret" not in r.text
        assert lp.status == "disabled"
        r = c.get("/api/plugins/demo/ping", headers=LOOPBACK)
        assert r.status_code == 404 and r.json() == {"error": "unknown plugin"}


def test_non_router_routes_disables_plugin(parts):
    obj = FakePlugin()
    obj.routes = lambda: {"not": "a router"}
    lp = _loaded(obj)
    with _client(parts, [lp]):
        pass
    assert lp.status == "disabled"


def test_panel_rendered_in_partial(parts):
    with _client(parts, [_loaded()]) as c:
        html = c.get("/api/state/panels").text
    assert 'id="plugin-demo-mobile"' in html
    assert "plugin-panel" in html
    assert "<p>hello mobile</p>" in html
    assert "Mobile &lt;b&gt;" in html  # title escaped


def test_panel_rendered_on_dashboard(parts):
    with _client(parts, [_loaded()]) as c:
        assert 'id="plugin-demo-mobile"' in c.get("/").text


def test_panel_render_failure_contained(parts):
    obj = FakePlugin()

    def boom():
        raise RuntimeError("render boom")

    obj.panels = lambda: [Panel("mobile", "Mobile", boom)]
    lp = _loaded(obj)
    with _client(parts, [lp]) as c:
        r = c.get("/api/state/panels")
    assert r.status_code == 200
    assert "Plugin panel unavailable" in r.text
    assert "Cymatix is stopped" in r.text  # built-in panels still render
    assert lp.status == "disabled"


def test_panel_render_non_str_is_failure(parts):
    obj = FakePlugin()
    obj.panels = lambda: [Panel("mobile", "Mobile", lambda: 42)]
    lp = _loaded(obj)
    with _client(parts, [lp]) as c:
        r = c.get("/api/state/panels")
    assert r.status_code == 200 and "Plugin panel unavailable" in r.text
    assert lp.status == "disabled"


def test_no_plugins_dashboard_unchanged(parts):
    with _client(parts, None) as c:
        base_root = c.get("/").text
        base_panels = c.get("/api/state/panels").text
    with _client(parts, []) as c:
        assert c.get("/").text == base_root
        assert c.get("/api/state/panels").text == base_panels
    assert "plugin-panel" not in base_root and "plugin-panel" not in base_panels


def test_shutdown_stops_plugin_services(parts):
    services = MagicMock()
    with _client(parts, [], services) as c:
        r = c.post("/api/shutdown")
        assert r.status_code == 200
        services.stop_all.assert_called()


def test_token_mode_still_requires_header_and_token(parts):
    obj = FakePlugin()
    with _client(parts, [_loaded(obj)], token="s3cret") as c:
        auth = {"Authorization": "Bearer s3cret"}
        r = c.post("/api/plugins/demo/actions/pair", headers={**LOOPBACK})
        assert r.status_code == 401
        r = c.post("/api/plugins/demo/actions/pair", headers={**LOOPBACK, **auth})
        assert r.status_code == 403
        r = c.post("/api/plugins/demo/actions/pair", headers={**ACTION, **auth})
        assert r.status_code == 200
