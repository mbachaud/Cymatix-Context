"""Phase 2: staging lanes on a snapshot of another lane's store, the runtime
config dump, and `cymatix compare` (1:1 lane receipts)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from cymatix_context.config import CymatixConfig, LaneConfig, ServerConfig, load_config
from cymatix_context.lanes import LaneConfigError, resolve_lanes, validate_lanes


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("CYMATIX_GENOME_PATH", "CYMATIX_BENCH_ENABLED", "CYMATIX_SERVER_PORT",
                "CYMATIX_LANE"):
        monkeypatch.delenv(var, raising=False)


def _wal_db(path: Path, rows: int) -> sqlite3.Connection:
    """A live WAL-mode store whose newest rows are still only in the -wal."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE genes (gene_id TEXT PRIMARY KEY, content TEXT)")
    conn.executemany("INSERT INTO genes VALUES (?, ?)",
                     [(f"g{i}", f"content {i}") for i in range(rows)])
    conn.commit()
    return conn


# ── snapshot ────────────────────────────────────────────────────────────


def test_snapshot_copies_a_live_wal_store(tmp_path):
    from cymatix_context.launcher.snapshot import snapshot_store
    src = tmp_path / "stable" / "genome.db"
    live = _wal_db(src, rows=5)  # connection stays open: rows live in -wal
    dst = tmp_path / "staging" / "genome.db"
    info = snapshot_store(src, dst)
    live.close()
    copy = sqlite3.connect(dst)
    assert copy.execute("SELECT COUNT(*) FROM genes").fetchone()[0] == 5
    copy.close()
    assert info["bytes"] > 0 and info["source"] == str(src.resolve())


def test_snapshot_replaces_an_existing_copy(tmp_path):
    from cymatix_context.launcher.snapshot import snapshot_store
    src = tmp_path / "stable" / "genome.db"
    _wal_db(src, rows=3).close()
    dst = tmp_path / "staging" / "genome.db"
    _wal_db(dst, rows=10).close()
    snapshot_store(src, dst)
    copy = sqlite3.connect(dst)
    assert copy.execute("SELECT COUNT(*) FROM genes").fetchone()[0] == 3
    copy.close()


def test_snapshot_refuses_same_file_and_missing_source(tmp_path):
    from cymatix_context.launcher.snapshot import SnapshotError, snapshot_store
    src = tmp_path / "s" / "genome.db"
    with pytest.raises(SnapshotError, match="does not exist"):
        snapshot_store(src, tmp_path / "d" / "genome.db")
    _wal_db(src, rows=1).close()
    with pytest.raises(SnapshotError, match="same file"):
        snapshot_store(src, src)


# ── lane config ─────────────────────────────────────────────────────────


def test_genome_source_parses(tmp_path):
    toml = tmp_path / "cymatix.toml"
    toml.write_text(
        '[[lanes]]\nname = "staging"\nport = 11441\n'
        'genome_path = "genomes/staging/genome.db"\ngenome_source = "snapshot:stable"\n',
        encoding="utf-8",
    )
    assert load_config(str(toml)).lanes[0].genome_source == "snapshot:stable"


def _cfg(*lanes):
    cfg = CymatixConfig()
    cfg.lanes = list(lanes)
    return cfg


def test_snapshot_source_must_be_another_known_lane():
    ok = _cfg(LaneConfig(name="staging", port=11441, genome_path="g/s/s.db",
                         genome_source="snapshot:stable"))
    validate_lanes(resolve_lanes(ok))
    for bad, match in [("snapshot:nope", "nope"), ("snapshot:staging", "itself"),
                       ("copy:stable", "genome_source")]:
        cfg = _cfg(LaneConfig(name="staging", port=11441, genome_path="g/s/s.db",
                              genome_source=bad))
        with pytest.raises(LaneConfigError, match=match):
            validate_lanes(resolve_lanes(cfg))


def test_runtime_records_snapshot_source(tmp_path):
    from cymatix_context.launcher.lane_supervisors import build_lane_runtimes
    cfg = _cfg(LaneConfig(name="staging", port=11441, genome_path="g/s/s.db",
                          genome_source="snapshot:stable"))
    cfg.genome.path = "g/main/genome.db"
    (rt,) = build_lane_runtimes(cfg, base_dir=tmp_path, state_dir=tmp_path / "st")
    assert rt.snapshot_from == (tmp_path / "g/main/genome.db").resolve()
    assert rt.store_path == (tmp_path / "g/s/s.db").resolve()


def test_ensure_snapshot_only_copies_when_missing_or_refreshing(tmp_path):
    from cymatix_context.launcher.lane_supervisors import LaneRuntime, ensure_snapshot
    src = tmp_path / "stable" / "genome.db"
    _wal_db(src, rows=2).close()
    rt = LaneRuntime(
        config=LaneConfig(name="staging", port=11441, genome_path="x",
                          genome_source="snapshot:stable"),
        supervisor=None, snapshot_from=src,
        store_path=tmp_path / "staging" / "genome.db",
    )
    assert ensure_snapshot(rt) is not None          # missing -> copied
    assert ensure_snapshot(rt) is None              # present -> left alone
    assert ensure_snapshot(rt, refresh=True) is not None


# ── launcher snapshot route ─────────────────────────────────────────────


def test_launcher_snapshot_route_restarts_lane_on_fresh_copy(tmp_path):
    pytest.importorskip("jinja2", reason="launcher extra not installed")
    from fastapi.testclient import TestClient
    from cymatix_context.launcher.app import create_app
    from cymatix_context.launcher.lane_supervisors import LaneRuntime
    from tests.test_launcher_dashboard_wiring import FakeCollector, FakeSupervisor
    from tests.test_launcher_lanes import FakeLaneSupervisor

    src = tmp_path / "stable" / "genome.db"
    _wal_db(src, rows=4).close()
    sup = FakeLaneSupervisor(11441, running=True)
    staging = LaneRuntime(
        config=LaneConfig(name="staging", port=11441, genome_path="g",
                          genome_source="snapshot:stable"),
        supervisor=sup, snapshot_from=src,
        store_path=tmp_path / "staging" / "genome.db",
    )
    plain = LaneRuntime(config=LaneConfig(name="bench", port=11439, genome_path="b"),
                        supervisor=FakeLaneSupervisor(11439))
    app = create_app(store=SimpleNamespace(), supervisor=FakeSupervisor(),
                     collector=FakeCollector(), lanes=[staging, plain])
    with TestClient(app) as c:
        r = c.post("/api/control/lanes/staging/snapshot")
        assert r.status_code == 200, r.text
        assert sup.stops == 1 and sup.starts == 1
        assert staging.store_path.exists()
        assert c.post("/api/control/lanes/bench/snapshot").status_code == 409


def test_lanes_panel_offers_snapshot_refresh_only_for_snapshot_lanes():
    pytest.importorskip("jinja2", reason="launcher extra not installed")
    from fastapi.testclient import TestClient
    from cymatix_context.launcher.app import STATIC_DIR, create_app
    from cymatix_context.launcher.lane_supervisors import LaneRuntime
    from tests.test_launcher_dashboard_wiring import FakeCollector, FakeSupervisor
    from tests.test_launcher_lanes import FakeLaneSupervisor

    staging = LaneRuntime(
        config=LaneConfig(name="staging", port=11441, genome_path="g",
                          genome_source="snapshot:stable"),
        supervisor=FakeLaneSupervisor(11441), snapshot_from=Path("s.db"),
    )
    bench = LaneRuntime(config=LaneConfig(name="bench", port=11439, genome_path="b"),
                        supervisor=FakeLaneSupervisor(11439))
    app = create_app(store=SimpleNamespace(), supervisor=FakeSupervisor(),
                     collector=FakeCollector(), lanes=[staging, bench])
    with TestClient(app) as c:
        html = c.get("/api/state/panels").text
        lanes = {l["name"]: l for l in c.get("/api/lanes").json()["lanes"]}
    assert 'data-action="lane-snapshot" data-lane="staging"' in html
    assert 'data-action="lane-snapshot" data-lane="bench"' not in html
    assert lanes["staging"]["genome_source"] == "snapshot:stable"
    assert "lane-snapshot" in (STATIC_DIR / "launcher.js").read_text(encoding="utf-8")


# ── /admin/config-dump ──────────────────────────────────────────────────


def test_config_dump_reports_loaded_runtime_config(monkeypatch):
    from tests.conftest import make_client, make_cymatix_config
    monkeypatch.setenv("CYMATIX_LANE", "staging")
    cfg = make_cymatix_config(server=ServerConfig(
        upstream="http://localhost:11434", admin_token="tok"))
    with make_client(cfg) as c:
        assert c.get("/admin/config-dump").status_code == 401
        body = c.get("/admin/config-dump",
                     headers={"Authorization": "Bearer tok"}).json()
    assert body["lane"] == "staging"
    assert body["config"]["server"]["admin_token"] == "<redacted>"
    assert body["config"]["budget"]["max_genes_per_turn"] == 4
    assert body["genome"]["path"] == ":memory:"
    assert isinstance(body["genome"]["total_genes"], int)
    assert body["engine"]["version"]
    assert "commit" in body["engine"]


# ── cymatix compare ─────────────────────────────────────────────────────


def _packet(ids, know=True):
    return {
        "verified": [{"gene_id": g, "title": g, "content": ""} for g in ids],
        "stale_risk": [],
        "know": {"found": True, "confidence": 0.8, "top_score": 1.0,
                 "score_gap": 0.1, "lexical_dense_agree": True} if know else None,
        "miss": None if know else {"miss": True, "reason": "no_match"},
    }


def test_load_queries_formats(tmp_path):
    from cymatix_context.cli.cmd_compare import load_queries
    (tmp_path / "q.txt").write_text("alpha?\n\n# comment\nbeta?\n", encoding="utf-8")
    (tmp_path / "q.jsonl").write_text(
        json.dumps({"question": "alpha?"}) + "\n" + json.dumps({"query": "beta?"}) + "\n",
        encoding="utf-8")
    (tmp_path / "q.json").write_text(json.dumps(["alpha?", {"question": "beta?"}]),
                                     encoding="utf-8")
    for name in ("q.txt", "q.jsonl", "q.json"):
        assert load_queries(tmp_path / name) == ["alpha?", "beta?"]


def test_compare_writes_a_receipt(tmp_path, monkeypatch):
    from cymatix_context.cli import cmd_compare

    answers = {
        "http://127.0.0.1:11437": {"alpha?": ["g1", "g2", "g3"], "beta?": ["g9"]},
        "http://127.0.0.1:11441": {"alpha?": ["g1", "g3", "g4"], "beta?": ["g9"]},
    }

    def fake_post(url, body, token=None):
        base, path = url.rsplit("/context", 1)
        assert path == "/packet" and body["ignore_delivered"] is True
        return _packet(answers[base][body["query"]])

    def fake_get(url, token=None):
        return {"lane": "x", "engine": {"commit": "abc", "version": "0.10.0"},
                "genome": {"path": "p", "total_genes": 3}, "config": {}}

    monkeypatch.setattr(cmd_compare, "_post_json", fake_post)
    monkeypatch.setattr(cmd_compare, "_get_json", fake_get)
    cfg = tmp_path / "cymatix.toml"
    cfg.write_text('[[lanes]]\nname = "staging"\nport = 11441\n'
                   'genome_path = "genomes/staging/genome.db"\n', encoding="utf-8")
    monkeypatch.setenv("CYMATIX_CONFIG", str(cfg))
    (tmp_path / "q.txt").write_text("alpha?\nbeta?\n", encoding="utf-8")
    out = tmp_path / "receipt.json"

    rc = cmd_compare.run(["--lanes", "stable,staging", "--queries",
                          str(tmp_path / "q.txt"), "--out", str(out)])
    assert rc == 0
    receipt = json.loads(out.read_text(encoding="utf-8"))
    assert [l["name"] for l in receipt["lanes"]] == ["stable", "staging"]
    assert receipt["lanes"][0]["config_dump"]["engine"]["commit"] == "abc"
    assert receipt["queries_sha256"]
    alpha, beta = receipt["results"]
    assert alpha["query"] == "alpha?" and alpha["identical"] is False
    assert alpha["only_a"] == ["g2"] and alpha["only_b"] == ["g4"]
    assert alpha["jaccard"] == pytest.approx(2 / 4)
    assert beta["identical"] is True
    s = receipt["summary"]
    assert s["n"] == 2 and s["identical"] == 1 and s["top1_same"] == 2
    assert s["know_agree"] == 2


def test_compare_unknown_lane_fails_cleanly(tmp_path, monkeypatch, capsys):
    from cymatix_context.cli import cmd_compare
    (tmp_path / "q.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("CYMATIX_CONFIG", str(tmp_path / "none.toml"))
    rc = cmd_compare.run(["--lanes", "stable,ghost", "--queries", str(tmp_path / "q.txt")])
    assert rc == 2 and "ghost" in capsys.readouterr().err
