"""Launcher multi-lane wiring: one supervisor per lane, generic lane
controls, and the dashboard's Lanes panel."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("jinja2", reason="launcher extra not installed")
from fastapi.testclient import TestClient

from cymatix_context.config import CymatixConfig, LaneConfig
from cymatix_context.launcher.app import create_app
from cymatix_context.launcher.lane_supervisors import LaneRuntime, build_lane_runtimes
from tests.test_launcher_dashboard_wiring import FakeCollector, FakeSupervisor


class FakeLaneSupervisor(FakeSupervisor):
    def __init__(self, port, running=False):
        super().__init__()
        self.cymatix_port = port
        self._running = running
        self.stops = 0

    def is_running(self):
        return self._running

    def start(self):
        self._running = True
        return super().start()

    def stop(self, reason=""):
        self.stops += 1
        self._running = False


def _lane(name, port, role="custom", **kw):
    return LaneRuntime(
        config=LaneConfig(name=name, port=port,
                          genome_path=f"genomes/{name}/genome.db", role=role, **kw),
        supervisor=FakeLaneSupervisor(port),
    )


def _client(lanes=None, primary=None, **kw):
    app = create_app(
        store=SimpleNamespace(),
        supervisor=primary or FakeSupervisor(),
        collector=FakeCollector(),
        lanes=lanes,
        **kw,
    )
    return TestClient(app)


# ── /api/lanes ──────────────────────────────────────────────────────────


def test_api_lanes_lists_primary_then_extra_lanes():
    staging = _lane("staging", 11441, role="staging")
    with _client(lanes=[staging]) as c:
        body = c.get("/api/lanes").json()
    assert [(l["name"], l["port"], l["role"], l["primary"]) for l in body["lanes"]] == [
        ("stable", 11437, "stable", True),
        ("staging", 11441, "staging", False),
    ]
    assert body["lanes"][1]["genome"] == "genomes/staging/genome.db"
    assert body["lanes"][1]["running"] is False
    assert body["lanes"][1]["mcp_url"] == "http://127.0.0.1:11441"


def test_api_lanes_primary_only_by_default():
    with _client() as c:
        names = [l["name"] for l in c.get("/api/lanes").json()["lanes"]]
    assert names == ["stable"]


# ── /api/control/lanes/{name}/{action} ──────────────────────────────────


def test_lane_start_stop_restart():
    staging = _lane("staging", 11441)
    sup = staging.supervisor
    with _client(lanes=[staging]) as c:
        r = c.post("/api/control/lanes/staging/start")
        assert r.status_code == 200 and sup.starts == 1
        assert c.get("/api/lanes").json()["lanes"][1]["running"] is True
        assert c.post("/api/control/lanes/staging/restart").status_code == 200
        assert sup.restarts
        assert c.post("/api/control/lanes/staging/stop").status_code == 200
        assert sup.stops == 1


def test_unknown_lane_is_404():
    with _client() as c:
        assert c.post("/api/control/lanes/nope/start").status_code == 404


def test_primary_lane_routes_to_primary_supervisor():
    primary = FakeSupervisor()
    with _client(primary=primary) as c:
        assert c.post("/api/control/lanes/stable/restart").status_code == 200
    assert primary.restarts


# ── legacy bench aliases ────────────────────────────────────────────────


def test_bench_routes_alias_the_bench_lane():
    bench = _lane("bench", 11439, role="bench")
    with _client(lanes=[bench]) as c:
        assert c.post("/api/control/bench/start").status_code == 200
        assert bench.supervisor.starts == 1
        assert c.get("/api/state").json()["bench"] == {
            "running": True, "port": 11439, "genome": "genomes/bench/genome.db",
        }


def test_legacy_bench_supervisor_kwarg_still_works():
    sup = FakeLaneSupervisor(11439)
    with _client(bench_supervisor=sup, bench_genome_path="g/b/b.db") as c:
        lanes = c.get("/api/lanes").json()["lanes"]
    assert [(l["name"], l["genome"]) for l in lanes] == [
        ("stable", CymatixConfig().genome.path), ("bench", "g/b/b.db"),
    ]


# ── dashboard ───────────────────────────────────────────────────────────


def test_lanes_panel_renders_each_extra_lane():
    staging = _lane("staging", 11441, role="staging")
    with _client(lanes=[_lane("bench", 11439, role="bench"), staging]) as c:
        html = c.get("/api/state/panels").text
    assert "panel--lanes" in html
    assert 'data-lane="bench"' in html and 'data-lane="staging"' in html
    assert ":11441" in html and 'data-action="lane-start"' in html


def test_lanes_panel_hidden_without_extra_lanes():
    with _client() as c:
        assert "panel--lanes" not in c.get("/api/state/panels").text


def test_launcher_js_wires_lane_actions():
    from cymatix_context.launcher.app import STATIC_DIR
    js = (STATIC_DIR / "launcher.js").read_text(encoding="utf-8")
    assert "/api/control/lanes/" in js and "lane-start" in js


# ── tray ────────────────────────────────────────────────────────────────


def _tray(lanes):
    pytest.importorskip("pystray")
    from cymatix_context.launcher.tray import CymatixTrayIcon
    return CymatixTrayIcon(
        supervisor=FakeSupervisor(), dashboard_url="http://127.0.0.1:11438",
        lanes=lanes,
    )


def _items(menu):
    return list(getattr(menu, "items", None) or getattr(menu, "_items", []))


def _text(item):
    text = getattr(item, "text", None)
    return text(item) if callable(text) else text


def test_tray_lanes_submenu_controls_each_lane():
    staging = _lane("staging", 11441, role="staging")
    icon = _tray([staging])
    lanes_item = next(i for i in _items(icon._build_menu()) if _text(i) == "Lanes")
    lane_item = next(i for i in _items(lanes_item.submenu)
                     if "staging" in (_text(i) or ""))
    assert ":11441" in _text(lane_item) and "stopped" in _text(lane_item)
    actions = {_text(i): i for i in _items(lane_item.submenu)}
    actions["Start"](icon)
    assert staging.supervisor.starts == 1
    actions["Stop"](icon)
    assert staging.supervisor.stops == 1


def test_tray_lanes_submenu_omitted_without_lanes():
    titles = [_text(i) for i in _items(_tray([])._build_menu())]
    assert "Lanes" not in titles


# ── supervisor construction ─────────────────────────────────────────────


def test_build_lane_runtimes_skips_primary_and_isolates_state(tmp_path):
    cfg = CymatixConfig()
    cfg.server.bench_enabled = True
    cfg.lanes = [LaneConfig(
        name="staging", port=11441, genome_path="genomes/staging/genome.db",
        engine_path=str(tmp_path / "wt"), python_executable="C:/venv/python.exe",
    )]
    runtimes = build_lane_runtimes(
        cfg, host="127.0.0.1", base_dir=tmp_path, state_dir=tmp_path / "state",
    )
    by_name = {rt.config.name: rt for rt in runtimes}
    assert list(by_name) == ["bench", "staging"]

    bench = by_name["bench"].supervisor
    # Legacy file names are kept so an already-running bench child is re-adopted.
    assert bench.store.path == tmp_path / "state" / "bench-state.json"
    assert bench.cymatix_log_path == tmp_path / "state" / "cymatix-bench.log"

    staging = by_name["staging"].supervisor
    assert staging.cymatix_port == 11441
    assert staging.python_executable == "C:/venv/python.exe"
    assert staging.store.path == tmp_path / "state" / "staging-state.json"
    assert staging.cymatix_log_path == tmp_path / "state" / "cymatix-staging.log"
    assert staging.extra_env["CYMATIX_LANE"] == "staging"
    assert staging._cwd() == str((tmp_path / "wt").resolve())


def test_supervisor_cwd_defaults_to_repo_root():
    from cymatix_context.launcher.supervisor import CymatixSupervisor
    sup = CymatixSupervisor(store=SimpleNamespace(), cymatix_log_path=Path("x.log"))
    assert sup._cwd() is None or Path(sup._cwd(), "pyproject.toml").exists()
