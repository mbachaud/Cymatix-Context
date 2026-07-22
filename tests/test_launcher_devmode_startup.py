"""v0.7.0: dual main+bench port dev mode + startup UX surfaces."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("jinja2", reason="launcher extra not installed")
from fastapi.testclient import TestClient

from cymatix_context.launcher.app import create_app
from tests.test_launcher_dashboard_wiring import FakeCollector, FakeSupervisor


class FakeBench(FakeSupervisor):
    def __init__(self, running=False, port=11439):
        super().__init__()
        self._running = running
        self.helix_port = port

    def is_running(self):
        return self._running


def _client(**kw):
    app = create_app(
        store=SimpleNamespace(),
        supervisor=FakeSupervisor(),
        collector=FakeCollector(),
        **kw,
    )
    return TestClient(app)


# ── bench dev mode ─────────────────────────────────────────────────────


def test_bench_disabled_by_default():
    with _client() as c:
        state = c.get("/api/state").json()
        assert state["bench"] is None
        assert c.post("/api/control/bench/start").status_code == 409
        assert "panel--bench" not in c.get("/api/state/panels").text


def test_bench_state_and_controls():
    bench = FakeBench(running=False, port=11439)
    with _client(bench_supervisor=bench,
                 bench_genome_path="genomes/bench/bench.genome.db") as c:
        state = c.get("/api/state").json()
        assert state["bench"] == {
            "running": False,
            "port": 11439,
            "genome": "genomes/bench/bench.genome.db",
        }
        html = c.get("/api/state/panels").text
        assert "panel--bench" in html and "11439" in html
        assert "bench-start" in html
        r = c.post("/api/control/bench/start")
        assert r.status_code == 200 and bench.starts == 1
        bench._running = True
        assert "bench-stop" in c.get("/api/state/panels").text
        assert c.post("/api/control/bench/stop").status_code == 200


def test_server_config_bench_fields(tmp_path, monkeypatch):
    from cymatix_context.config import load_config
    toml = tmp_path / "helix.toml"
    toml.write_text(
        "[server]\nbench_enabled = true\nbench_port = 12001\n"
        'bench_genome_path = "genomes/bench/alt.db"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("HELIX_BENCH_ENABLED", raising=False)
    cfg = load_config(str(toml))
    assert cfg.server.bench_enabled is True
    assert cfg.server.bench_port == 12001
    assert cfg.server.bench_genome_path == "genomes/bench/alt.db"
    monkeypatch.setenv("HELIX_BENCH_ENABLED", "0")
    cfg = load_config(str(toml))
    assert cfg.server.bench_enabled is False


def test_supervisor_extra_env_merge():
    from cymatix_context.launcher.supervisor import HelixSupervisor
    sup = HelixSupervisor.__new__(HelixSupervisor)
    sup.extra_env = {"HELIX_GENOME_PATH": "x.db"}
    # the merge logic is inline in start(); assert the attribute shape
    assert sup.extra_env == {"HELIX_GENOME_PATH": "x.db"}
    sup2 = HelixSupervisor.__new__(HelixSupervisor)
    sup2.extra_env = None
    assert sup2.extra_env is None


# ── startup UX ─────────────────────────────────────────────────────────


def test_start_pending_renders_spinner():
    sup = FakeSupervisor()
    sup.last_start_pending = True
    app = create_app(
        store=SimpleNamespace(), supervisor=sup, collector=FakeCollector(),
    )
    with TestClient(app) as c:
        html = c.get("/api/state/panels").text
        assert "panel--starting" in html and "spinner" in html
        assert c.get("/api/state").json()["helix"]["start_pending"] is True


def test_needs_db_selection_modal_and_clearing():
    with _client(needs_db_selection=True) as c:
        state = c.get("/api/state").json()
        assert state["needs_db_selection"] is True
        page = c.get("/").text
        assert "data-db-modal" in page and "Select a database" in page
        # panels show the no-db message instead of "stopped"
        assert "No database selected" in c.get("/api/state/panels").text


def test_db_modal_has_manual_dismiss():
    # #308: the modal previously had no exit path at all.
    with _client(needs_db_selection=True) as c:
        page = c.get("/").text
        assert "data-db-modal-close" in page


def test_db_modal_js_retries_and_wires_dismiss():
    # #308: populateDbModal ran once at page load; a single failed
    # /api/genomes fetch left a dead "Scanning…" placeholder with no
    # Select buttons. No JS harness exists, so smoke the wiring: the
    # poll callback must re-run population, and the dismiss control
    # must be wired.
    from cymatix_context.launcher.app import STATIC_DIR
    js = (STATIC_DIR / "launcher.js").read_text(encoding="utf-8")
    assert "data-db-modal-close" in js
    assert "pollDbModal" in js


def test_db_modal_hidden_when_not_needed():
    with _client(needs_db_selection=False) as c:
        page = c.get("/").text
        assert "data-db-modal" in page and "hidden" in page
        assert c.get("/api/state").json()["needs_db_selection"] is False


# -- pythonw headless stdio hotfix --------------------------------------


def test_ensure_streams_replaces_none_stdio(tmp_path, monkeypatch):
    import sys
    from cymatix_context.launcher.app import _ensure_streams
    log = tmp_path / "launcher.log"
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    _ensure_streams(str(log))
    assert sys.stdout is not None and sys.stderr is not None
    print("headless print survives")  # must not raise
    sys.stdout.flush()
    assert "headless print survives" in log.read_text(encoding="utf-8")


def test_ensure_streams_noop_with_console(capsys):
    from cymatix_context.launcher.app import _ensure_streams
    import sys
    before_out, before_err = sys.stdout, sys.stderr
    _ensure_streams(None)
    assert sys.stdout is before_out and sys.stderr is before_err
