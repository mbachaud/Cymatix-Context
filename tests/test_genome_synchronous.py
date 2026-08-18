"""Issue #372: [genome] synchronous — writer PRAGMA, default NORMAL.

Before the fix no code in the package set PRAGMA synchronous, so SQLite's
FULL default applied and every ingest commit (one per strand, plus parent,
relations, and demotions) was an fsync.
"""

import pytest

from cymatix_context.config import GenomeConfig, load_config
from cymatix_context.knowledge_store import KnowledgeStore


def _level(store) -> int:
    return store.conn.execute("PRAGMA synchronous").fetchone()[0]


def test_default_is_normal(tmp_path):
    g = KnowledgeStore(path=str(tmp_path / "g.db"))
    try:
        assert _level(g) == 1  # 1 = NORMAL
        assert g._synchronous == "NORMAL"
    finally:
        g.close()


def test_explicit_full_is_honoured(tmp_path):
    g = KnowledgeStore(path=str(tmp_path / "g.db"), synchronous="FULL")
    try:
        assert _level(g) == 2  # 2 = FULL
    finally:
        g.close()


def test_invalid_value_falls_back_to_normal(tmp_path, caplog):
    g = KnowledgeStore(path=str(tmp_path / "g.db"), synchronous="TURBO")
    try:
        assert _level(g) == 1
        assert g._synchronous == "NORMAL"
    finally:
        g.close()


def test_config_default_and_toml_parse(tmp_path):
    assert GenomeConfig().synchronous == "NORMAL"
    p = tmp_path / "cymatix.toml"
    p.write_text('[genome]\nsynchronous = "FULL"\n', encoding="utf-8")
    assert load_config(str(p)).genome.synchronous == "FULL"


def test_builder_threads_the_knob(tmp_path):
    """Boot and /admin/swap-db construct stores from build_genome_kwargs —
    the knob must ride the shared builder (tests/test_swap_db_knob_sync.py
    guards the structural half; this checks the value)."""
    from cymatix_context.config import CymatixConfig
    from cymatix_context.sharding import build_genome_kwargs

    cfg = CymatixConfig(genome=GenomeConfig(path=":memory:", synchronous="FULL"))
    assert build_genome_kwargs(cfg)["synchronous"] == "FULL"
