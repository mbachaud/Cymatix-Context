"""Lean bench-bed writes: skip storage that is query-inert at shipped defaults.

* ``KnowledgeStore(pki_write=False)`` skips path_key_index rows (the PKI
  read tier is off by default since #370, yet every insert wrote ~41 rows
  per gene on ERB 580-char chunks).
* ``CYMATIX_LEAN_INDEXES=1`` skips two indexes that are left-prefixes of
  another index on the same table, so every lookup they serve is still
  index-served: idx_promoter_value (prefix of idx_promoter_cover) and
  idx_entity_graph_entity (prefix of the entity_graph primary key).

Both default to the legacy behaviour.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

from cymatix_context.knowledge_store import KnowledgeStore
from tests.conftest import make_gene

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_fixture_matrix as bfm  # noqa: E402


def _gene():
    g = make_gene("Retention policy evaluation in the control plane.", gene_id="lean_gene_00000001")
    g.source_id = r"F:\tmp\corpus\confluence\adr-017.json"
    g.key_values = ["status=published", "owner_team=engineering"]
    return g


def _rows(db, table):
    con = sqlite3.connect(db)
    try:
        return con.execute(f"select count(*) from {table}").fetchone()[0]
    finally:
        con.close()


def _indexes(db):
    con = sqlite3.connect(db)
    try:
        return {r[0] for r in con.execute("select name from sqlite_master where type='index'")}
    finally:
        con.close()


def test_pki_written_by_default(tmp_path):
    db = str(tmp_path / "a.db")
    ks = KnowledgeStore(path=db, synonym_map={})
    ks.upsert_gene(_gene())
    ks.close()
    assert _rows(db, "path_key_index") > 0


def test_pki_write_false_skips_rows(tmp_path):
    db = str(tmp_path / "b.db")
    ks = KnowledgeStore(path=db, synonym_map={}, pki_write=False)
    ks.upsert_gene(_gene())
    ks.close()
    assert _rows(db, "path_key_index") == 0
    assert _rows(db, "genes") == 1


def test_redundant_indexes_present_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CYMATIX_LEAN_INDEXES", raising=False)
    db = str(tmp_path / "c.db")
    KnowledgeStore(path=db, synonym_map={}).close()
    assert {"idx_promoter_value", "idx_entity_graph_entity"} <= _indexes(db)


def test_lean_indexes_skips_prefix_redundant_indexes(tmp_path, monkeypatch):
    monkeypatch.setenv("CYMATIX_LEAN_INDEXES", "1")
    db = str(tmp_path / "d.db")
    KnowledgeStore(path=db, synonym_map={}).close()
    idx = _indexes(db)
    assert "idx_promoter_value" not in idx and "idx_entity_graph_entity" not in idx
    assert "idx_promoter_cover" in idx and "idx_entity_graph_gene" in idx


def test_lean_indexes_lookups_stay_index_served(tmp_path, monkeypatch):
    monkeypatch.setenv("CYMATIX_LEAN_INDEXES", "1")
    db = str(tmp_path / "e.db")
    KnowledgeStore(path=db, synonym_map={}).close()
    con = sqlite3.connect(db)
    try:
        for sql in ("select gene_id from promoter_index where tag_value = 'x'",
                    "select gene_id from entity_graph where entity = 'x'"):
            plan = " ".join(r[-1] for r in con.execute("explain query plan " + sql))
            assert "USING" in plan and "INDEX" in plan, plan
    finally:
        con.close()


@pytest.mark.parametrize("val,expected", [(None, True), ("1", True), ("0", False)])
def test_builder_pki_write_env(monkeypatch, val, expected):
    if val is None:
        monkeypatch.delenv("CYMATIX_BFM_PKI_WRITE", raising=False)
    else:
        monkeypatch.setenv("CYMATIX_BFM_PKI_WRITE", val)
    assert bfm._env_flag("CYMATIX_BFM_PKI_WRITE") is expected
