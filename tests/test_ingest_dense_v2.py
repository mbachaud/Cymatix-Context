"""Tier-0 PR-1 (2026-05-16): BGE-M3 dense vectors written at ingest.

Plan: ``docs/reviews/2026-05-16-deep-review/00-tier0-implementation-plan.md``
§PR-1. PR-1 is the WRITE path only — it makes ``upsert_doc`` (and therefore
every ingest path) compute and persist ``genes.embedding_dense_v2``. It does
NOT enable dense retrieval (that is PR-3); these tests only assert the
column is populated, never that recall uses it.

Cases covered:

1. ``test_upsert_doc_explicit_vector_persists_blob`` — an explicit
   ``embedding_dense_v2=`` arg is packed and stored; round-trips at
   ``dim*4`` bytes.
2. ``test_upsert_doc_lazy_compute_when_knob_on`` — no dense arg +
   ``dense_embed_on_ingest=True`` ⇒ non-NULL vector.
3. ``test_upsert_doc_null_when_knob_off`` — no dense arg +
   ``dense_embed_on_ingest=False`` ⇒ NULL column.
4. ``test_fresh_genome_has_dense_v2_column`` — R10: a freshly created
   in-memory ``Genome`` has the column and round-trips a dense vector.
5. ``test_context_manager_ingest_populates_all_strands`` — a multi-strand
   ``ingest()`` ⇒ every resulting ``genes`` row has a non-NULL vector of
   the right length.
6. ``test_ingest_corpus_full_dense_coverage`` — ingest a small corpus,
   ``COUNT(embedding_dense_v2 IS NOT NULL) == COUNT(*)``.
7. ``test_pr1_genome_satisfies_backfill_skip_clause`` — a PR-1-built
   genome makes the backfill script's
   ``length(blob) != expected_bytes`` skip-clause select 0 rows.
8. ``test_vec_to_blob_shared_helper_*`` — the shared fp32 packer.
9. ``test_encode_batch_matches_encode`` — codec batch path equals the
   single-encode path.
10. ``test_live_*`` (``live``-marked) — real BGE-M3 encode + pack
    round-trip; skipped when the model is unavailable.

The codec is mocked with deterministic hash-seeded vectors so the suite
runs without BGE-M3 weights (mirrors ``tests/test_dense_recall.py``).
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from helix_context.backends.bgem3_codec import (
    PASSAGE_CHAR_CAP,
    BGEM3Codec,
    vec_to_blob,
)
from helix_context.config import HelixConfig
from helix_context.context_manager import HelixContextManager
from helix_context.genome import Genome
from helix_context.schemas import (
    ChromatinState, EpigeneticMarkers, Gene, PromoterTags,
)

# The deterministic fake BGE-M3 codec is defined once in tests/conftest.py
# (the `_stub_dense_codec` autouse fixture installs the same class for every
# non-live test). `_hash_vec` / `_FakeCodec` stay as local aliases so this
# file's existing call sites are unchanged.
from tests.conftest import FakeBGEM3Codec as _FakeCodec
from tests.conftest import hash_vec as _hash_vec

DIM = 1024


def _make_gene(content: str, *, gene_id: str | None = None) -> Gene:
    return Gene(
        gene_id=gene_id or Genome.make_gene_id(content),
        content=content,
        complement="",
        codons=[],
        promoter=PromoterTags(),
        epigenetics=EpigeneticMarkers(),
        chromatin=ChromatinState.OPEN,
        is_fragment=False,
    )


def _blob_len(genome: Genome, gene_id: str):
    row = genome.conn.execute(
        "SELECT length(embedding_dense_v2) FROM genes WHERE gene_id = ?",
        (gene_id,),
    ).fetchone()
    return row[0] if row else None


# ── 1. explicit vector persists ──────────────────────────────────────


def test_upsert_doc_explicit_vector_persists_blob():
    """An explicit ``embedding_dense_v2=`` arg is packed to a dim*4 BLOB
    and round-trips to the same float values within fp32 epsilon.
    """
    g = Genome(path=":memory:", dense_embed_on_ingest=False, dense_embedding_dim=DIM)
    try:
        vec = _hash_vec("explicit-passage").tolist()
        g.upsert_doc(
            _make_gene("explicit vector doc", gene_id="x1"),
            apply_gate=False,
            embedding_dense_v2=vec,
        )
        blob = g.conn.execute(
            "SELECT embedding_dense_v2 FROM genes WHERE gene_id='x1'"
        ).fetchone()[0]
        assert blob is not None
        assert len(blob) == DIM * 4, f"expected {DIM*4} bytes, got {len(blob)}"
        back = np.frombuffer(blob, dtype="<f4")
        assert back.shape == (DIM,)
        assert np.allclose(back, np.asarray(vec, dtype=np.float32), atol=1e-7), (
            "explicit vector did not round-trip"
        )
    finally:
        g.close()


def test_upsert_doc_explicit_vector_persists_even_with_knob_off():
    """An explicit vector is honoured regardless of dense_embed_on_ingest:
    the knob only governs *lazy* compute, not a caller-supplied vector.
    """
    g = Genome(path=":memory:", dense_embed_on_ingest=False, dense_embedding_dim=DIM)
    try:
        vec = _hash_vec("knob-off-explicit").tolist()
        g.upsert_doc(
            _make_gene("knob off but explicit", gene_id="x2"),
            apply_gate=False,
            embedding_dense_v2=vec,
        )
        assert _blob_len(g, "x2") == DIM * 4
    finally:
        g.close()


# ── 2. lazy compute when knob on ─────────────────────────────────────


def test_upsert_doc_lazy_compute_when_knob_on():
    """No dense arg + dense_embed_on_ingest=True ⇒ upsert_doc encodes the
    vector lazily via the store's codec and stores a non-NULL BLOB.
    """
    g = Genome(path=":memory:", dense_embed_on_ingest=True, dense_embedding_dim=DIM)
    fake = _FakeCodec(DIM)
    g._dense_codec = fake  # inject so no BGE-M3 weights are pulled
    try:
        g.upsert_doc(
            _make_gene("lazily encoded body", gene_id="l1"),
            apply_gate=False,
        )
        assert _blob_len(g, "l1") == DIM * 4
        assert fake.encode_calls == 1, "lazy path should call encode exactly once"
    finally:
        g.close()


# ── 3. NULL when knob off ────────────────────────────────────────────


def test_upsert_doc_null_when_knob_off():
    """No dense arg + dense_embed_on_ingest=False ⇒ column stays NULL.
    The codec must not even be consulted.
    """
    g = Genome(path=":memory:", dense_embed_on_ingest=False, dense_embedding_dim=DIM)
    fake = _FakeCodec(DIM)
    g._dense_codec = fake
    try:
        g.upsert_doc(
            _make_gene("knob off doc", gene_id="o1"),
            apply_gate=False,
        )
        val = g.conn.execute(
            "SELECT embedding_dense_v2 FROM genes WHERE gene_id='o1'"
        ).fetchone()[0]
        assert val is None, "knob-off ingest must leave embedding_dense_v2 NULL"
        assert fake.encode_calls == 0, "knob-off must not invoke the codec"
    finally:
        g.close()


def test_upsert_doc_empty_content_stays_null_even_with_knob_on():
    """Blank content has nothing to encode — column is NULL, no crash."""
    g = Genome(path=":memory:", dense_embed_on_ingest=True, dense_embedding_dim=DIM)
    g._dense_codec = _FakeCodec(DIM)
    try:
        g.upsert_doc(_make_gene("   ", gene_id="e1"), apply_gate=False)
        val = g.conn.execute(
            "SELECT embedding_dense_v2 FROM genes WHERE gene_id='e1'"
        ).fetchone()[0]
        assert val is None
    finally:
        g.close()


# ── 4. R10 — fresh genome has the column ─────────────────────────────


def test_fresh_genome_has_dense_v2_column():
    """R10: a freshly created in-memory Genome's ``genes`` table declares
    ``embedding_dense_v2`` (init_db's migration adds it) — no separate
    ALTER needed before the first dense write.
    """
    g = Genome(path=":memory:")
    try:
        cols = {
            row[1] for row in g.conn.execute("PRAGMA table_info(genes)").fetchall()
        }
        assert "embedding_dense_v2" in cols, (
            "R10 regression: fresh genome lacks embedding_dense_v2 column"
        )
    finally:
        g.close()


def test_fresh_genome_round_trips_dense_vector():
    """R10 continued: a fresh genome can write *and* read back a dense
    vector end-to-end — proves the column is a real BLOB column.
    """
    g = Genome(path=":memory:", dense_embed_on_ingest=False, dense_embedding_dim=DIM)
    try:
        vec = _hash_vec("fresh-genome-roundtrip").tolist()
        g.upsert_doc(
            _make_gene("fresh genome doc", gene_id="r1"),
            apply_gate=False,
            embedding_dense_v2=vec,
        )
        blob = g.conn.execute(
            "SELECT embedding_dense_v2 FROM genes WHERE gene_id='r1'"
        ).fetchone()[0]
        back = np.frombuffer(blob, dtype="<f4")
        assert back.shape == (DIM,)
        assert np.allclose(back, np.asarray(vec, dtype=np.float32), atol=1e-7)
    finally:
        g.close()


# ── 5. context_manager.ingest populates every strand ─────────────────


def _make_manager(*, dense_on: bool) -> HelixContextManager:
    cfg = HelixConfig()
    cfg.genome.path = ":memory:"
    cfg.ingestion.backend = "cpu"
    cfg.ingestion.dense_embed_on_ingest = dense_on
    mgr = HelixContextManager(cfg)
    mgr._dense_codec = _FakeCodec(cfg.retrieval.dense_embedding_dim)
    return mgr


def test_context_manager_ingest_populates_all_strands():
    """A multi-strand document ingested through context_manager.ingest()
    yields ``genes`` rows that ALL have a non-NULL ``embedding_dense_v2``
    of the configured dim*4 bytes — including the file-level parent doc.
    """
    mgr = _make_manager(dense_on=True)
    try:
        # Large enough to chunk into >= 2 strands (chunker cap is 4000 chars).
        big = " ".join(
            f"strand body paragraph number {i} with enough real words to "
            f"form a substantive chunk of document text."
            for i in range(400)
        )
        ids = mgr.ingest(big, content_type="text", metadata={"path": "F:/t/doc.txt"})
        assert len(ids) >= 2, f"expected a multi-strand chunking, got {len(ids)}"

        expected = mgr.config.retrieval.dense_embedding_dim * 4
        rows = mgr.genome.conn.execute(
            "SELECT gene_id, embedding_dense_v2 FROM genes"
        ).fetchall()
        assert len(rows) >= 2
        for gene_id, blob in rows:
            assert blob is not None, f"gene {gene_id} has NULL embedding_dense_v2"
            assert len(blob) == expected, (
                f"gene {gene_id}: blob len {len(blob)} != {expected}"
            )
    finally:
        mgr.genome.close()


def test_context_manager_ingest_null_when_knob_off():
    """With dense_embed_on_ingest=False the manager skips dense encoding
    entirely and every ingested row keeps a NULL column.
    """
    mgr = _make_manager(dense_on=False)
    try:
        mgr.ingest("a single short strand of text", content_type="text")
        nonnull = mgr.genome.conn.execute(
            "SELECT COUNT(*) FROM genes WHERE embedding_dense_v2 IS NOT NULL"
        ).fetchone()[0]
        assert nonnull == 0, "knob-off ingest must not write any dense vectors"
    finally:
        mgr.genome.close()


# ── 6. small-corpus full coverage ────────────────────────────────────


def test_ingest_corpus_full_dense_coverage():
    """Ingest a small corpus of separate documents; assert
    ``COUNT(embedding_dense_v2 IS NOT NULL) == COUNT(*)``.
    """
    mgr = _make_manager(dense_on=True)
    try:
        for i in range(8):
            mgr.ingest(
                f"corpus document number {i}: distinct content body for the "
                f"needle-in-a-haystack retrieval store entry {i}.",
                content_type="text",
                metadata={"path": f"F:/corpus/doc_{i}.txt"},
            )
        total = mgr.genome.conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
        nonnull = mgr.genome.conn.execute(
            "SELECT COUNT(*) FROM genes WHERE embedding_dense_v2 IS NOT NULL"
        ).fetchone()[0]
        assert total >= 8
        assert nonnull == total, (
            f"dense coverage {nonnull}/{total} — not every gene was populated"
        )
    finally:
        mgr.genome.close()


# ── 7. backfill idempotency skip-clause ──────────────────────────────


def test_pr1_genome_satisfies_backfill_skip_clause(tmp_path):
    """A genome built through the PR-1 ingest write path must make the
    backfill script's idempotency skip-clause select ZERO rows — i.e.
    re-running ``scripts/backfill_bgem3_v2.py`` over it reports 0 rows to
    process. The skip-clause is::

        WHERE embedding_dense_v2 IS NULL
           OR length(embedding_dense_v2) != ?   -- expected_bytes (dim*4)
    """
    db_path = tmp_path / "pr1_built.db"
    g = Genome(
        path=str(db_path), dense_embed_on_ingest=True, dense_embedding_dim=DIM,
    )
    g._dense_codec = _FakeCodec(DIM)
    try:
        for i in range(10):
            g.upsert_doc(
                _make_gene(f"pr1 built gene body {i}", gene_id=f"pr1-{i}"),
                apply_gate=False,
            )
    finally:
        g.close()

    expected_bytes = DIM * 4
    conn = sqlite3.connect(str(db_path))
    try:
        total = conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
        to_process = conn.execute(
            "SELECT COUNT(*) FROM genes "
            "WHERE embedding_dense_v2 IS NULL "
            "   OR length(embedding_dense_v2) != ?",
            (expected_bytes,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert total == 10
    assert to_process == 0, (
        f"backfill would re-process {to_process}/{total} rows — PR-1 encoding "
        f"does not satisfy the length(blob)==dim*4 skip-clause"
    )


# ── 8. shared fp32 packing helper ────────────────────────────────────


def test_vec_to_blob_shared_helper_length_and_roundtrip():
    """``vec_to_blob`` packs to exactly dim*4 little-endian fp32 bytes and
    ``np.frombuffer`` recovers the values within fp32 epsilon.
    """
    vec = _hash_vec("shared-helper").tolist()
    blob = vec_to_blob(vec, DIM)
    assert isinstance(blob, bytes)
    assert len(blob) == DIM * 4
    back = np.frombuffer(blob, dtype="<f4")
    assert np.allclose(back, np.asarray(vec, dtype=np.float32), atol=1e-7)


def test_vec_to_blob_rejects_wrong_dim():
    """A dim mismatch raises ValueError — guards against a half-written
    or wrong-dim row reaching the column.
    """
    with pytest.raises(ValueError):
        vec_to_blob(_hash_vec("x", 512).tolist(), DIM)


def test_vec_to_blob_matches_backfill_script_helper():
    """The backfill script's ``_vec_to_blob`` is the shared helper — the
    inline-ingest write and the offline backfill cannot drift.
    """
    import importlib.util

    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "backfill_bgem3_v2.py"
    spec = importlib.util.spec_from_file_location("backfill_bgem3_v2_id", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Same function object, not merely an equivalent implementation.
    assert mod._vec_to_blob is vec_to_blob

    vec = _hash_vec("drift-check").tolist()
    assert mod._vec_to_blob(vec, DIM) == vec_to_blob(vec, DIM)


# ── 9. codec batch path parity ───────────────────────────────────────


def test_encode_batch_matches_encode():
    """``BGEM3Codec.encode_batch`` produces the same vectors as repeated
    ``encode`` for the same texts (same truncation + renormalisation),
    so the batched ingest path is byte-equivalent to per-strand encode.
    """
    codec = BGEM3Codec(dim=64)

    # Deterministic per-text mock: identical to how encode would see it.
    def fake_single(text, normalize_embeddings=True, show_progress_bar=False):
        seed = int.from_bytes(
            hashlib.sha256(str(text).encode()).digest()[:8], "little"
        )
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(128).astype(np.float32)
        return v

    def fake_batch(texts, normalize_embeddings=True, show_progress_bar=False):
        return np.stack([fake_single(t) for t in texts])

    texts = ["alpha passage", "beta passage", "gamma passage"]

    # encode path
    from unittest.mock import MagicMock
    m1 = MagicMock()
    m1.encode.side_effect = fake_single
    codec._model = m1
    codec._backend = "sentence_transformers"
    one_by_one = [codec.encode(t, task="passage") for t in texts]

    # encode_batch path
    m2 = MagicMock()
    m2.encode.side_effect = fake_batch
    codec._model = m2
    batched = codec.encode_batch(texts, task="passage")

    assert len(batched) == len(one_by_one) == 3
    for a, b in zip(one_by_one, batched):
        assert np.allclose(
            np.asarray(a, dtype=np.float32),
            np.asarray(b, dtype=np.float32),
            atol=1e-6,
        ), "encode_batch diverged from encode"


def test_encode_batch_empty_returns_empty():
    codec = BGEM3Codec(dim=64)
    assert codec.encode_batch([], task="passage") == []


# ── 10. live — real BGE-M3 encode + pack ─────────────────────────────


@pytest.mark.live
def test_live_real_codec_encode_and_pack_roundtrip():
    """Exercise the real BGE-M3 codec: encode a passage, pack it via the
    shared helper, confirm the BLOB is dim*4 bytes and the vector is
    unit-norm. Skipped automatically when the model is unavailable.
    """
    try:
        codec = BGEM3Codec(dim=DIM)
        vec = codec.encode("helix runs an OpenAI-compatible proxy", task="passage")
    except Exception as exc:  # noqa: BLE001 — model download / import failure
        pytest.skip(f"BGE-M3 model unavailable: {exc}")

    assert len(vec) == DIM
    arr = np.asarray(vec, dtype=np.float32)
    assert abs(float(np.linalg.norm(arr)) - 1.0) < 1e-3
    blob = vec_to_blob(vec, DIM)
    assert len(blob) == DIM * 4
    assert np.allclose(np.frombuffer(blob, dtype="<f4"), arr, atol=1e-7)


@pytest.mark.live
def test_live_upsert_doc_lazy_compute_with_real_codec():
    """End-to-end: upsert_doc with the real BGE-M3 codec and the
    dense_embed_on_ingest knob on persists a real dim*4 vector.
    """
    g = Genome(path=":memory:", dense_embed_on_ingest=True, dense_embedding_dim=DIM)
    try:
        try:
            g._get_dense_codec()  # force the real load up front
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"BGE-M3 model unavailable: {exc}")
        g.upsert_doc(
            _make_gene("a real passage encoded by BGE-M3", gene_id="live-1"),
            apply_gate=False,
        )
        assert _blob_len(g, "live-1") == DIM * 4
    finally:
        g.close()
