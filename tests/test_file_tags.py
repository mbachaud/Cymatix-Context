"""Opt-in file-level tag dedup (``KnowledgeStore(file_tags=True)``).

On small-chunk beds every chunk of a file repeats the same tags in
promoter_index. With file_tags on, a (kind, tag) already indexed for an
earlier chunk of the SAME source_id is not written again: the file's first
chunk is the representative that holds the tag, later chunks keep only the
tags that are new to them. Default off == legacy writes, byte-identical.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

from cymatix_context.knowledge_store import KnowledgeStore
from tests.conftest import make_gene

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_fixture_matrix as bfm  # noqa: E402


def _chunk(i, src, domains, entities=()):
    g = make_gene(f"chunk body number {i} of {src}", domains=list(domains),
                  entities=list(entities), gene_id=f"ft_gene_{src[-1]}_{i:08d}")
    g.source_id = src
    g.promoter.sequence_index = i
    return g


def _postings(db):
    con = sqlite3.connect(db)
    try:
        return sorted(con.execute(
            "select gene_id, tag_type, tag_value from promoter_index").fetchall())
    finally:
        con.close()


def _write(tmp_path, name, **kw):
    db = str(tmp_path / name)
    ks = KnowledgeStore(path=db, synonym_map={}, **kw)
    for i, extra in enumerate(["alpha", "beta", "gamma"]):
        ks.upsert_gene(_chunk(i, "/c/a", ["shared", "fileword", extra], ["Acme"]))
    ks.upsert_gene(_chunk(0, "/c/b", ["shared", "other"], ["Acme"]))
    ks.close()
    return db


def test_default_writes_every_tag_on_every_chunk(tmp_path):
    rows = _postings(_write(tmp_path, "legacy.db"))
    assert len(rows) == 3 * 4 + 3  # 3 chunks x (3 domains + 1 entity) + b's


def test_file_tags_dedups_within_a_source(tmp_path):
    rows = _postings(_write(tmp_path, "ft.db", file_tags=True))
    a = [r for r in rows if r[0].startswith("ft_gene_a_")]
    # chunk 0 holds shared+fileword+alpha+Acme; chunks 1,2 only their own tag
    assert [r for r in a if r[0].endswith("00000000")].__len__() == 4
    assert {r[2] for r in a if r[0].endswith("00000001")} == {"beta"}
    assert {r[2] for r in a if r[0].endswith("00000002")} == {"gamma"}


def test_file_tags_does_not_dedup_across_sources(tmp_path):
    rows = _postings(_write(tmp_path, "ft2.db", file_tags=True))
    b = {r[2] for r in rows if r[0].startswith("ft_gene_b_")}
    assert b == {"shared", "other", "acme"}


def test_file_tag_hit_still_retrievable(tmp_path):
    db = _write(tmp_path, "ft3.db", file_tags=True)
    ks = KnowledgeStore(path=db, synonym_map={})
    try:
        ids = {g.gene_id for g in ks.query_docs(["fileword"], [], max_genes=10)}
    finally:
        ks.close()
    assert "ft_gene_a_00000000" in ids


def test_builder_file_tags_env(monkeypatch):
    monkeypatch.delenv("CYMATIX_BFM_FILE_TAGS", raising=False)
    assert bfm._env_flag("CYMATIX_BFM_FILE_TAGS", "0") is False
    monkeypatch.setenv("CYMATIX_BFM_FILE_TAGS", "1")
    assert bfm._env_flag("CYMATIX_BFM_FILE_TAGS", "0") is True
