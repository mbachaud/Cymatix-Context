"""Regression tests for POST /admin/refresh.

Issue #62 / Stage 2 spec (``docs/specs/2026-05-08-stage-2-dense-recall.md`` §4):

    "An explicit refresh tick on /admin/refresh also calls
    _invalidate_dense_matrix(force=True)."

Prior to the fix, ``admin_refresh`` only reopened the genome snapshot via
``Genome.refresh()`` and never invalidated the in-memory dense matrix cache.
Out-of-process inserts (e.g., the backfill script writing ``embedding_dense_v2``
BLOBs directly) were silently invisible to dense recall until the helix process
restarted.

The fix wires ``helix.genome._invalidate_dense_matrix(force=True)`` into the
handler. This test pins that contract.
"""
from __future__ import annotations

import hashlib
import random
import sqlite3
import struct

import numpy as np
import pytest

from helix_context.config import GenomeConfig, RetrievalConfig
from helix_context.genome import Genome
from helix_context.schemas import (
    ChromatinState,
    EpigeneticMarkers,
    Gene,
    PromoterTags,
)

from tests.conftest import make_client, make_helix_config


def _hash_vec(text: str, dim: int) -> np.ndarray:
    """Deterministic L2-normalised fp32 vector (mirrors test_dense_recall)."""
    out = np.zeros(dim, dtype=np.float32)
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(seed[:8], "little"))
    for i in range(dim):
        out[i] = rng.gauss(0.0, 1.0)
    n = np.linalg.norm(out)
    if n > 0:
        out /= n
    return out


def _make_gene(content: str, *, gene_id: str | None = None) -> Gene:
    return Gene(
        gene_id=gene_id or Genome.make_gene_id(content),
        content=content,
        complement="",
        codons=[],
        promoter=PromoterTags(domains=["alpha"], entities=[]),
        epigenetics=EpigeneticMarkers(),
        chromatin=ChromatinState.OPEN,
        is_fragment=False,
    )


def _populate_v2(genome: Genome, gene_id: str, vec: np.ndarray) -> None:
    """Write a v2 BLOB directly (bypasses the codec)."""
    blob = vec.astype("<f4").tobytes()
    genome.conn.execute(
        "UPDATE genes SET embedding_dense_v2 = ? WHERE gene_id = ?",
        (sqlite3.Binary(blob), gene_id),
    )
    genome.conn.commit()


@pytest.fixture
def dense_client(tmp_path):
    """TestClient backed by a genome with dense recall enabled.

    Uses a file-backed sqlite so the WAL refresh path in
    ``Genome.refresh()`` exercises the real reopen-on-bad-state branch.
    """
    db_path = tmp_path / "admin-refresh.db"
    config = make_helix_config(
        genome=GenomeConfig(path=str(db_path), cold_start_threshold=5),
        retrieval=RetrievalConfig(
            dense_embedding_enabled=True,
            dense_embedding_dim=1024,
        ),
    )
    with make_client(config=config) as client:
        yield client


def test_admin_refresh_invalidates_dense_matrix(dense_client):
    """Issue #62: ``/admin/refresh`` must drop the in-memory dense matrix.

    Materialize the dense matrix, POST /admin/refresh, confirm the cached
    matrix is cleared so the next dense recall rebuilds from the new
    snapshot (and therefore sees out-of-process inserts).
    """
    helix = dense_client.app.state.helix
    genome = helix.genome

    # Seed two genes with v2 BLOBs so _ensure_dense_matrix returns a matrix.
    for i in range(2):
        gene = _make_gene(f"refresh fixture gene {i}", gene_id=f"refresh-{i}")
        genome.upsert_gene(gene, apply_gate=False)
        _populate_v2(genome, f"refresh-{i}", _hash_vec(f"vec {i}", 1024))

    # Materialize the dense matrix. After this, the cached matrix is hot.
    matrix, ids = genome._ensure_dense_matrix()
    assert matrix is not None, "dense matrix should build with v2 BLOBs present"
    assert genome._dense_matrix is not None
    assert genome._dense_matrix_ids is not None

    # Hit the endpoint.
    resp = dense_client.post("/admin/refresh")
    assert resp.status_code == 200
    body = resp.json()
    assert body["refreshed"] is True
    assert "genes" in body

    # The fix: matrix cache is dropped — next dense recall will rebuild from
    # the freshly-reopened snapshot.
    assert genome._dense_matrix is None, (
        "Issue #62: /admin/refresh must invalidate the dense matrix cache "
        "(was non-None after refresh)"
    )
    assert genome._dense_matrix_ids is None


def test_admin_refresh_surfaces_out_of_process_inserts(dense_client, tmp_path):
    """End-to-end: an out-of-process insert is visible after /admin/refresh.

    Mirrors the operator workflow from the issue: a backfill script writes
    rows to the genome via a sidecar sqlite connection, then the operator
    POSTs /admin/refresh. The dense matrix should rebuild and include the
    newly-inserted row on the next ``_ensure_dense_matrix()`` call.
    """
    helix = dense_client.app.state.helix
    genome = helix.genome

    # Seed one gene through the live connection.
    seeded = _make_gene("seeded via api", gene_id="seed-0")
    genome.upsert_gene(seeded, apply_gate=False)
    _populate_v2(genome, "seed-0", _hash_vec("seed vec", 1024))

    matrix1, ids1 = genome._ensure_dense_matrix()
    assert matrix1 is not None
    n1 = matrix1.shape[0]
    assert "seed-0" in ids1

    # Simulate an out-of-process insert: open a separate sqlite connection,
    # add a row, close it. The live helix snapshot does NOT see it yet.
    side = sqlite3.connect(genome.path, timeout=30)
    side.execute("PRAGMA journal_mode=WAL")
    side.execute(
        "INSERT INTO genes (gene_id, content, complement, chromatin, "
        "compression_tier, is_fragment, embedding_dense_v2) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "external-1",
            "external insert body",
            "",
            int(ChromatinState.OPEN),
            0,
            0,
            sqlite3.Binary(_hash_vec("external vec", 1024).astype("<f4").tobytes()),
        ),
    )
    side.commit()
    side.close()

    # POST /admin/refresh — must reopen the snapshot AND invalidate the cache.
    resp = dense_client.post("/admin/refresh")
    assert resp.status_code == 200

    # Cache was invalidated, so the next call rebuilds from the fresh snapshot
    # and picks up the external row.
    matrix2, ids2 = genome._ensure_dense_matrix()
    assert matrix2 is not None
    assert matrix2.shape[0] == n1 + 1, (
        f"out-of-process insert invisible after /admin/refresh: "
        f"matrix grew {n1} -> {matrix2.shape[0]}"
    )
    assert "external-1" in ids2
