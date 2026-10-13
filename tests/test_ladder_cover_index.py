"""Deterministic covering-index state for the ERB ablation ladder.

``idx_genes_id_chromatin`` is built in the background on a writable open of a
large store (PR #510). A background build during a measured run perturbs the
latency numbers, so the ladder pins the choice explicitly:

* ``--cover-index off`` (default): ``CYMATIX_COVER_INDEX_AUTOBUILD=0`` is set
  before any store opens; no builder thread starts.
* ``--cover-index prebuild``: the migration runs synchronously on the bed
  before the first query; its wall time is recorded in the receipt.

The synthetic store here is small, so the 50k-row hint threshold is patched
down (the same technique as tests/test_cover_index_autobuild.py). The autobuild
gate itself is unchanged.
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from cymatix_context.knowledge_store import KnowledgeStore
from cymatix_context.storage import ddl

from tests.test_tier_sql_rank_identical import _make

ROOT = Path(__file__).resolve().parents[1]
IDX = "idx_genes_id_chromatin"
ENV = ddl.COVER_INDEX_AUTOBUILD_ENV


def _load_ladder():
    spec = importlib.util.spec_from_file_location(
        "ablation_ladder", ROOT / "benchmarks" / "dogfood" / "erb" / "ablation_ladder.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _has_index(db: Path) -> bool:
    c = sqlite3.connect(str(db))
    try:
        return c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (IDX,)
        ).fetchone() is not None
    finally:
        c.close()


@pytest.fixture(autouse=True)
def _small_threshold(monkeypatch):
    monkeypatch.setattr(ddl, "COVER_INDEX_HINT_MIN_ROWS", 5)
    # Setting then letting monkeypatch restore keeps the ladder's os.environ
    # write from leaking into other tests.
    monkeypatch.setenv(ENV, "1")
    monkeypatch.delenv(ENV, raising=False)


def _seed(tmp_path) -> Path:
    s = _make(tmp_path)
    s.conn.commit()
    s.close()
    return tmp_path / "g.db"


def test_ladder_cli_exposes_cover_index_flag():
    src = (ROOT / "benchmarks" / "dogfood" / "erb" / "ablation_ladder.py").read_text(
        encoding="utf-8")
    assert '"--cover-index"' in src
    assert 'choices=COVER_INDEX_MODES' in src
    assert 'COVER_INDEX_MODES = ("off", "prebuild")' in src
    assert 'default="off"' in src


def test_cover_index_off_pins_env_and_starts_no_builder(tmp_path, monkeypatch):
    lad = _load_ladder()
    db = _seed(tmp_path)
    assert not _has_index(db)

    rec = lad.apply_cover_index_mode("off", str(db))

    import os
    assert os.environ[ENV] == "0"
    assert rec["mode"] == "off"
    assert rec["index_present_at_start"] is False
    assert rec["prebuild_elapsed_s"] is None
    assert rec["autobuild_env"] == "0"

    store = KnowledgeStore(str(db))
    try:
        assert store._cover_index_builder is None
    finally:
        store.close()
    assert not _has_index(db)


def test_cover_index_prebuild_creates_index_before_first_query(tmp_path, monkeypatch):
    lad = _load_ladder()
    db = _seed(tmp_path)
    assert not _has_index(db)

    rec = lad.apply_cover_index_mode("prebuild", str(db))

    assert rec["mode"] == "prebuild"
    assert rec["prebuild_status"] == "created"
    assert isinstance(rec["prebuild_elapsed_s"], float)
    assert rec["prebuild_elapsed_s"] >= 0.0
    assert rec["index_present_at_start"] is True
    assert _has_index(db)

    # Index already present: the writable open starts no background build.
    store = KnowledgeStore(str(db))
    try:
        assert store._cover_index_builder is None
    finally:
        store.close()


def test_cover_index_rejects_unknown_mode(tmp_path):
    lad = _load_ladder()
    db = _seed(tmp_path)
    with pytest.raises(ValueError):
        lad.apply_cover_index_mode("auto", str(db))
