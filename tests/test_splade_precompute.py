"""SPLADE precompute plumbing - issue #92, Phase 1.

Verifies that callers can pass a precomputed SPLADE sparse vector to
``sync_splade_index`` and ``upsert_doc`` instead of letting them call
``splade_backend.encode`` inline. Used by the parallel/shard-pool ingest
paths to batch SPLADE encoding outside the per-document upsert.
"""

from __future__ import annotations

import sqlite3

import pytest

from cymatix_context.backends import splade_backend
from cymatix_context.storage.indexes import sync_splade_index


def _fresh_splade_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    splade_backend.create_splade_table(conn)
    return conn


def test_sync_splade_index_uses_provided_sparse():
    """When splade_sparse is provided, no inline encode happens."""
    conn = _fresh_splade_db()
    provided = {"alpha": 1.5, "beta": 0.75}

    sync_splade_index(
        conn.cursor(),
        gene_id="g1",
        content="this content should be ignored",
        splade_enabled=True,
        splade_sparse=provided,
    )
    conn.commit()

    rows = conn.execute(
        "SELECT term, weight FROM splade_terms WHERE gene_id = ? ORDER BY term",
        ("g1",),
    ).fetchall()
    assert rows == [("alpha", 1.5), ("beta", 0.75)]


def test_sync_splade_index_disabled_is_noop_even_with_sparse():
    conn = _fresh_splade_db()
    sync_splade_index(
        conn.cursor(),
        gene_id="g1",
        content="x",
        splade_enabled=False,
        splade_sparse={"alpha": 1.0},
    )
    conn.commit()
    rows = conn.execute("SELECT COUNT(*) FROM splade_terms").fetchone()
    assert rows[0] == 0


def test_sync_splade_index_empty_sparse_dict_clears_existing_rows():
    """Pre-existing rows for gene_id get DELETE'd even when sparse is empty."""
    conn = _fresh_splade_db()
    conn.execute(
        "INSERT INTO splade_terms (gene_id, term, weight) VALUES (?, ?, ?)",
        ("g1", "stale", 1.0),
    )
    conn.commit()

    sync_splade_index(
        conn.cursor(),
        gene_id="g1",
        content="x",
        splade_enabled=True,
        splade_sparse={},
    )
    conn.commit()

    rows = conn.execute(
        "SELECT COUNT(*) FROM splade_terms WHERE gene_id = ?", ("g1",)
    ).fetchone()
    assert rows[0] == 0


from cymatix_context.knowledge_store import KnowledgeStore
from cymatix_context.schemas import Gene


def _make_test_gene(content: str = "hello world parallel ingest") -> Gene:
    return Gene(
        gene_id=KnowledgeStore.make_gene_id(content),
        content=content,
        complement=f"Summary: {content[:40]}",
        codons=["chunk_0"],
        source_id="test://splade-precompute",
    )


def test_upsert_doc_forwards_splade_sparse(tmp_path):
    """Pre-computed SPLADE sparse dict ends up in the splade_terms table."""
    db = tmp_path / "g.db"
    ks = KnowledgeStore(path=str(db), synonym_map={}, splade_enabled=True)
    gene = _make_test_gene()

    provided = {"semantic": 2.5, "expansion": 1.1}
    gene_id = ks.upsert_doc(gene, apply_gate=False, splade_sparse=provided)

    rows = [
        (r["term"], r["weight"])
        for r in ks.conn.execute(
            "SELECT term, weight FROM splade_terms WHERE gene_id = ? ORDER BY term",
            (gene_id,),
        )
    ]
    ks.close()

    assert sorted(rows) == [("expansion", 1.1), ("semantic", 2.5)]


def test_upsert_doc_inline_encode_when_sparse_not_provided(tmp_path, monkeypatch):
    """No splade_sparse -> falls back to splade_backend.encode."""
    db = tmp_path / "g.db"
    ks = KnowledgeStore(path=str(db), synonym_map={}, splade_enabled=True)

    sentinel = {"sentinel": 9.99}
    calls: list[str] = []

    def fake_encode(text, top_k=128, **kw):
        calls.append(text)
        return sentinel

    monkeypatch.setattr(splade_backend, "encode", fake_encode)
    gene_id = ks.upsert_doc(_make_test_gene(), apply_gate=False)

    rows = [
        (r["term"], r["weight"])
        for r in ks.conn.execute(
            "SELECT term, weight FROM splade_terms WHERE gene_id = ?", (gene_id,)
        )
    ]
    ks.close()

    assert calls, "splade_backend.encode should have been called once"
    assert rows == [("sentinel", 9.99)]
