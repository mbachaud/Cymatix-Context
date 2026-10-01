"""Phase 5: the launcher as the desktop app's sidecar — headless mode with a
READY handshake on a free port, a per-launch token, a clean shutdown route,
and an embedded dashboard layout."""

from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import pytest

pytest.importorskip("jinja2", reason="launcher extra not installed")
from fastapi.testclient import TestClient

from cymatix_context.config import LaneConfig
from cymatix_context.launcher.app import _parse_args, bind_listen_socket, create_app
from cymatix_context.launcher.lane_supervisors import LaneRuntime
from tests.test_launcher_dashboard_wiring import FakeCollector, FakeSupervisor
from tests.test_launcher_lanes import FakeLaneSupervisor


class OwnedSupervisor(FakeLaneSupervisor):
    def __init__(self, port, owned, running=True):
        super().__init__(port, running=running)
        self._owned = owned

    def owns_process(self):
        return self._owned


def _app(**kw):
    kw.setdefault("supervisor", FakeSupervisor())
    return create_app(store=SimpleNamespace(), collector=FakeCollector(), **kw)


# ── token ───────────────────────────────────────────────────────────────


def test_no_token_means_open_as_before():
    with TestClient(_app()) as c:
        assert c.get("/api/state").status_code == 200


def test_token_guards_every_route():
    with TestClient(_app(token="s3cret")) as c:
        assert c.get("/api/state").status_code == 401
        assert c.get("/").status_code == 401
        assert c.get("/static/launcher.js").status_code == 401
        assert c.post("/api/control/start").status_code == 401
        ok = c.get("/api/state", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert c.get("/api/state", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_token_accepted_as_cookie():
    # The desktop app sets this cookie on its session so the embedded page
    # (and its fetch() calls) authenticate without the token in any URL.
    with TestClient(_app(token="s3cret")) as c:
        c.cookies.set("cymatix_launcher_token", "s3cret")
        assert c.get("/").status_code == 200
        assert c.get("/api/lanes").status_code == 200


# ── READY handshake ─────────────────────────────────────────────────────


def test_ready_line_printed_once_serving(capsys):
    with TestClient(_app(ready_info={"port": 54321})):
        pass
    lines = [l for l in capsys.readouterr().out.splitlines()
             if l.startswith("CYMATIX_LAUNCHER_READY ")]
    assert len(lines) == 1
    info = json.loads(lines[0].split(" ", 1)[1])
    assert info["port"] == 54321 and isinstance(info["pid"], int)


def test_bind_listen_socket_port_zero_picks_a_free_port():
    sock = bind_listen_socket("127.0.0.1", 0)
    try:
        port = sock.getsockname()[1]
        assert port > 0
        probe = socket.create_connection(("127.0.0.1", port), timeout=2)
        probe.close()  # already listening: connections queue until served
    finally:
        sock.close()


def test_parse_args_headless():
    args = _parse_args(["--headless", "--port", "0"])
    assert args.headless is True and args.port == 0


# ── shutdown ────────────────────────────────────────────────────────────


def test_shutdown_stops_only_owned_processes_and_exits():
    primary = OwnedSupervisor(11437, owned=True)
    owned = LaneRuntime(config=LaneConfig(name="staging", port=11441, genome_path="g"),
                        supervisor=OwnedSupervisor(11441, owned=True))
    adopted = LaneRuntime(config=LaneConfig(name="bench", port=11439, genome_path="b"),
                          supervisor=OwnedSupervisor(11439, owned=False))
    server = SimpleNamespace(should_exit=False)
    app = _app(supervisor=primary, lanes=[owned, adopted])
    app.state.uvicorn_server = server
    with TestClient(app) as c:
        body = c.post("/api/shutdown").json()
    assert body["ok"] is True
    assert sorted(body["stopped"]) == ["stable", "staging"]
    assert body["left_running"] == ["bench"]
    assert owned.supervisor.stops == 1 and adopted.supervisor.stops == 0
    assert primary.stops == 1
    assert server.should_exit is True


# ── embedded layout ─────────────────────────────────────────────────────


def test_embedded_layout_only_when_asked():
    with TestClient(_app()) as c:
        plain = c.get("/").text
        embedded = c.get("/?embedded=1").text
    assert "data-desktop-rail" not in plain and 'class="embedded"' not in plain
    assert "data-desktop-rail" in embedded and 'class="embedded"' in embedded
    for action in ("desktop-mcp-install", "desktop-copy-diagnostics", "desktop-open-logs"):
        assert action in embedded
