"""Multi-lane serving (Phase 1): N named cymatix instances, each on its own
port and knowledge store, supervised by one launcher.

The primary lane (``stable``) is always synthesized from ``[server] port`` +
``[genome] path``; ``bench`` is synthesized from the legacy ``[server]
bench_*`` knobs; ``[[lanes]]`` declares any additional lanes.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from cymatix_context.config import CymatixConfig, LaneConfig, load_config
from cymatix_context.lanes import (
    LaneConfigError,
    lane_env,
    resolve_lanes,
    validate_lanes,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "CYMATIX_GENOME_PATH", "CYMATIX_STORE_PATH", "CYMATIX_BENCH_ENABLED",
        "CYMATIX_SERVER_PORT", "CYMATIX_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)


def _write(tmp_path: Path, body: str) -> str:
    p = tmp_path / "cymatix.toml"
    p.write_text(body, encoding="utf-8")
    return str(p)


# ── config parsing ──────────────────────────────────────────────────────


def test_default_config_has_no_declared_lanes():
    assert CymatixConfig().lanes == []


def test_lanes_array_of_tables_is_parsed(tmp_path, caplog):
    path = _write(tmp_path, (
        "[[lanes]]\n"
        'name = "staging"\n'
        "port = 11441\n"
        'genome_path = "genomes/staging/genome.db"\n'
        'role = "staging"\n'
        'engine_path = "F:/worktrees/feature-x"\n'
        "autostart = false\n"
    ))
    with caplog.at_level(logging.WARNING, logger="cymatix_context.config"):
        cfg = load_config(path)
    assert cfg.lanes == [LaneConfig(
        name="staging", port=11441, genome_path="genomes/staging/genome.db",
        role="staging", engine_path="F:/worktrees/feature-x", autostart=False,
    )]
    # [[lanes]] is a list, not a table: it must neither be discarded as a
    # malformed section nor flagged as unknown.
    assert "lanes" not in caplog.text


def test_malformed_lane_entry_is_skipped_with_warning(tmp_path, caplog):
    path = _write(tmp_path, (
        "[[lanes]]\n"
        'name = "no-port"\n'
        'genome_path = "genomes/x/genome.db"\n'
        "[[lanes]]\n"
        'name = "ok"\n'
        "port = 11441\n"
        'genome_path = "genomes/ok/genome.db"\n'
    ))
    with caplog.at_level(logging.WARNING, logger="cymatix_context.config"):
        cfg = load_config(path)
    assert [lane.name for lane in cfg.lanes] == ["ok"]
    assert "no-port" in caplog.text


def test_server_port_env_override(tmp_path, monkeypatch):
    # The launcher exports CYMATIX_SERVER_PORT to lane children; it used to
    # be written but never read, so a lane's own config reported :11437.
    path = _write(tmp_path, "[server]\nport = 11437\n")
    monkeypatch.setenv("CYMATIX_SERVER_PORT", "11441")
    assert load_config(path).server.port == 11441


def test_server_port_env_garbage_is_ignored(tmp_path, monkeypatch):
    path = _write(tmp_path, "[server]\nport = 11437\n")
    monkeypatch.setenv("CYMATIX_SERVER_PORT", "not-a-port")
    assert load_config(path).server.port == 11437


# ── resolution ──────────────────────────────────────────────────────────


def test_resolve_default_is_stable_only():
    lanes = resolve_lanes(CymatixConfig())
    assert [(lane.name, lane.port, lane.role) for lane in lanes] == [
        ("stable", 11437, "stable"),
    ]
    assert lanes[0].genome_path == CymatixConfig().genome.path


def test_resolve_synthesizes_bench_from_legacy_knobs():
    cfg = CymatixConfig()
    cfg.server.bench_enabled = True
    lanes = resolve_lanes(cfg)
    assert [(lane.name, lane.port, lane.role) for lane in lanes] == [
        ("stable", 11437, "stable"),
        ("bench", 11439, "bench"),
    ]
    assert lanes[1].genome_path == cfg.server.bench_genome_path


def test_resolve_bench_forced_on_by_flag():
    lanes = resolve_lanes(CymatixConfig(), bench=True)
    assert [lane.name for lane in lanes] == ["stable", "bench"]


def test_declared_bench_lane_replaces_synthesized_one():
    cfg = CymatixConfig()
    cfg.server.bench_enabled = True
    cfg.lanes = [LaneConfig(name="bench", port=12001, genome_path="g/b/b.db")]
    lanes = resolve_lanes(cfg)
    assert [(lane.name, lane.port) for lane in lanes] == [
        ("stable", 11437), ("bench", 12001),
    ]


def test_declared_lanes_follow_primary_in_order():
    cfg = CymatixConfig()
    cfg.lanes = [
        LaneConfig(name="staging", port=11441, genome_path="g/s/s.db", role="staging"),
        LaneConfig(name="eval2", port=11442, genome_path="g/e/e.db"),
    ]
    assert [lane.name for lane in resolve_lanes(cfg)] == ["stable", "staging", "eval2"]


def test_primary_port_follows_launcher_override():
    lanes = resolve_lanes(CymatixConfig(), primary_port=12345)
    assert lanes[0].port == 12345


# ── validation ──────────────────────────────────────────────────────────


def _cfg_with(*lanes: LaneConfig) -> CymatixConfig:
    cfg = CymatixConfig()
    cfg.lanes = list(lanes)
    return cfg


def test_valid_lanes_pass():
    validate_lanes(resolve_lanes(_cfg_with(
        LaneConfig(name="staging", port=11441, genome_path="g/s/s.db"),
    )))


def test_duplicate_port_rejected():
    with pytest.raises(LaneConfigError, match="port 11437"):
        validate_lanes(resolve_lanes(_cfg_with(
            LaneConfig(name="clash", port=11437, genome_path="g/c/c.db"),
        )))


@pytest.mark.parametrize("port", [11438, 11440, 8787])
def test_reserved_port_rejected(port):
    with pytest.raises(LaneConfigError, match=f"port {port}"):
        validate_lanes(resolve_lanes(_cfg_with(
            LaneConfig(name="x", port=port, genome_path="g/x/x.db"),
        )))


def test_shared_genome_directory_rejected():
    # metrics.json lives next to the genome (context_manager.py), so two
    # lanes sharing a directory would overwrite each other's counters.
    cfg = _cfg_with(LaneConfig(
        name="twin", port=11441,
        genome_path=str(Path(CymatixConfig().genome.path).parent / "other.db"),
    ))
    with pytest.raises(LaneConfigError, match="directory"):
        validate_lanes(resolve_lanes(cfg))


def test_declaring_primary_lane_name_rejected():
    with pytest.raises(LaneConfigError, match="stable"):
        validate_lanes(resolve_lanes(_cfg_with(
            LaneConfig(name="stable", port=11441, genome_path="g/s/s.db"),
        )))


@pytest.mark.parametrize("name", ["", "has space", "../escape", "UPPER"])
def test_bad_lane_name_rejected(name):
    with pytest.raises(LaneConfigError, match="name"):
        validate_lanes(resolve_lanes(_cfg_with(
            LaneConfig(name=name, port=11441, genome_path="g/s/s.db"),
        )))


def test_duplicate_declared_names_rejected():
    with pytest.raises(LaneConfigError, match="staging"):
        validate_lanes(resolve_lanes(_cfg_with(
            LaneConfig(name="staging", port=11441, genome_path="g/a/a.db"),
            LaneConfig(name="staging", port=11442, genome_path="g/b/b.db"),
        )))


# ── child environment ───────────────────────────────────────────────────


def test_lane_env_pins_identity_and_absolute_genome(tmp_path):
    lane = LaneConfig(name="staging", port=11441, genome_path="genomes/s/s.db")
    env = lane_env(lane, base_dir=tmp_path)
    assert env["CYMATIX_LANE"] == "staging"
    assert env["CYMATIX_SERVER_PORT"] == "11441"
    # Absolute: a staging lane runs with cwd = its engine worktree, so a
    # relative path would silently resolve inside that worktree.
    assert env["CYMATIX_GENOME_PATH"] == str((tmp_path / "genomes/s/s.db").resolve())
    assert "CYMATIX_CONFIG" not in env
    assert "PYTHONPATH" not in env


def test_lane_env_engine_path_and_config(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "existing")
    engine = tmp_path / "worktree"
    lane = LaneConfig(
        name="staging", port=11441, genome_path=str(tmp_path / "s.db"),
        engine_path=str(engine), config_path="lanes/staging.toml",
    )
    env = lane_env(lane, base_dir=tmp_path)
    assert env["PYTHONPATH"] == os.pathsep.join([str(engine.resolve()), "existing"])
    assert env["CYMATIX_CONFIG"] == str((tmp_path / "lanes/staging.toml").resolve())
