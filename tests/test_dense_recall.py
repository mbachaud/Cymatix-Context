"""Stage 2 (2026-05-08): dense recall as first-class tier.

Spec: ``docs/specs/2026-05-08-stage-2-dense-recall.md`` §9 test plan.

These tests cover the six cases in the spec:

1. ``test_dense_recall_finds_needle_outside_top12_lexical`` — recall pulls a
   needle that lex misses.
2. ``test_query_genes_ann_pool_size_independent_of_max_genes`` — pool >> cut.
3. ``test_codec_full_1024_dim_on_encode`` — full-dim, norm≈1, random-pair
   cosine guard against dim=256 collapse.
4. ``test_v2_blob_roundtrip_matches_json`` — fp32 BLOB ↔ JSON list round-trip.
5. ``test_backfill_v2_idempotent`` — second run is a no-op.
6. ``test_dense_matrix_invalidation_after_insert`` — cache invalidates.

The codec is mocked with deterministic hash-based vectors so the suite
runs without BGE-M3 weights. ``test_codec_full_1024_dim_on_encode`` mocks
the underlying sentence-transformers model.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from helix_context.backends.bgem3_codec import BGEM3Codec
from helix_context.genome import Genome
from helix_context.schemas import (
    ChromatinState, EpigeneticMarkers, Gene, PromoterTags,
)

# The deterministic fake BGE-M3 codec lives in tests/conftest.py — it is the
# single shared definition (the `_stub_dense_codec` autouse fixture installs
# the same class for every non-live test). `_hash_vec` / `_FakeCodec` remain
# as local aliases so this file's many call sites stay unchanged.
from tests.conftest import FakeBGEM3Codec as _FakeCodec
from tests.conftest import hash_vec as _hash_vec


def _make_gene(content: str, *, domains=None, entities=None, gene_id=None) -> Gene:
    return Gene(
        gene_id=gene_id or Genome.make_gene_id(content),
        content=content,
        complement="",
        codons=[],
        promoter=PromoterTags(domains=domains or [], entities=entities or []),
        epigenetics=EpigeneticMarkers(),
        chromatin=ChromatinState.OPEN,
        is_fragment=False,
    )


def _populate_v2(genome: Genome, gene_id: str, vec: np.ndarray) -> None:
    """Write a v2 BLOB directly bypassing the codec."""
    blob = vec.astype("<f4").tobytes()
    genome.conn.execute(
        "UPDATE genes SET embedding_dense_v2 = ? WHERE gene_id = ?",
        (sqlite3.Binary(blob), gene_id),
    )
    genome.conn.commit()
    genome._invalidate_dense_matrix()


@pytest.fixture
def dense_genome():
    """In-memory genome with dense_embedding_enabled=True."""
    g = Genome(
        path=":memory:",
        dense_embedding_enabled=True,
        dense_embedding_dim=1024,
        dense_pool_size=500,
    )
    _orig_upsert = g.upsert_doc
    def _ungated(gene, apply_gate=False):
        return _orig_upsert(gene, apply_gate=apply_gate)
    g.upsert_doc = _ungated  # canonical (R3 Stage C)
    g.upsert_gene = _ungated  # legacy alias
    yield g
    g.close()


# ── 1. Needle outside lex top-12 — dense recall finds it ─────────────


def test_dense_recall_finds_needle_outside_top12_lexical(dense_genome):
    """Spec §9 case 1: needle gene shares no surface tokens with the query
    but its dense vector matches. Lex top-12 misses it; dense_recall finds it.
    """
    g = dense_genome

    # Seed corpus: 30 distractors that DO share lex tokens with query.
    for i in range(30):
        gene = _make_gene(
            f"distractor query token alpha entry {i}",
            domains=["alpha"],
        )
        g.upsert_gene(gene)

    # The needle: lex tokens differ entirely. Dense recall has to find it.
    needle = _make_gene(
        "wholly unrelated surface form for the needle gene body",
        domains=["needle"],
        gene_id="needle-001",
    )
    g.upsert_gene(needle)

    # Stage v2 BLOB so dense recall has data to scan. The query encoder will
    # produce a vector that matches the needle (via _FakeCodec query_target).
    needle_vec = _hash_vec("matches needle target", 1024)
    _populate_v2(g, "needle-001", needle_vec)
    # Distractors get random non-matching vectors.
    for row in g.conn.execute("SELECT gene_id FROM genes WHERE gene_id != 'needle-001'").fetchall():
        v = _hash_vec(row[0] + "-distractor-vec", 1024)
        _populate_v2(g, row[0], v)

    g._dense_codec = _FakeCodec(dim=1024, query_target="matches needle target")

    # Lex-only query — needle should NOT be in top-12.
    lex_only = g.query_genes(domains=["alpha"], entities=[], max_genes=12)
    assert all(gene.gene_id != "needle-001" for gene in lex_only), (
        "needle should not appear in lex-only top-12"
    )

    # Dense recall — needle should appear in the k=500 pool.
    hits = g.query_genes_dense_recall("alpha query that shares no surface tokens", k=500)
    hit_ids = [gid for gid, _ in hits]
    assert "needle-001" in hit_ids, f"needle should appear in dense recall; got {hit_ids[:5]}"
    # And it should be at the top because the FakeCodec maps query -> needle.
    assert hit_ids[0] == "needle-001", f"needle should rank #1; got {hit_ids[:3]}"


# ── 2. pool_size independent of max_genes ────────────────────────────


def test_query_genes_ann_pool_size_independent_of_max_genes(dense_genome):
    """Spec §9 case 2: union pool reaches ≥ 100 with pool_size=500 even
    when max_genes=12. Final return ≤ 12.
    """
    g = dense_genome

    # Populate 150 genes — enough to exceed the 100-gene union threshold.
    for i in range(150):
        gene = _make_gene(
            f"alpha gene number {i} body content",
            domains=["alpha"],
            gene_id=f"g-{i:04d}",
        )
        g.upsert_gene(gene)
        v = _hash_vec(f"vec for g-{i:04d}", 1024)
        _populate_v2(g, f"g-{i:04d}", v)

    g._dense_codec = _FakeCodec(dim=1024)

    # Instrumentation: count the union size that query_genes_ann constructs.
    # We tap the underlying methods to observe the pool before the final cut.
    original_lex = g.query_genes
    lex_pool_sizes: list[int] = []
    def lex_spy(domains, entities, **kw):
        result = original_lex(domains, entities, **kw)
        lex_pool_sizes.append(len(result))
        return result
    g.query_docs = lex_spy   # canonical (R3 Stage C); internal code calls this
    g.query_genes = lex_spy  # legacy alias

    original_dense = g.query_docs_dense_recall
    dense_pool_sizes: list[int] = []
    def dense_spy(*a, **kw):
        result = original_dense(*a, **kw)
        dense_pool_sizes.append(len(result))
        return result
    g.query_docs_dense_recall = dense_spy   # canonical (R3 Stage C)
    g.query_genes_dense_recall = dense_spy  # legacy alias

    out = g.query_docs_ann(
        "alpha query",
        domains=["alpha"], entities=[],
        max_genes=12,
        pool_size=500,
    )

    # ``query_docs_ann`` builds its lex pool with exactly one
    # ``query_docs`` call.
    assert len(lex_pool_sizes) == 1
    # Dense recall now fires at least once. Tier-0 PR-3 (2026-05-16)
    # decoupled dense recall from ``fusion_mode``, so ``query_docs``
    # itself runs dense recall internally (it is a hybrid retriever in
    # both additive and RRF mode). ``query_docs_ann`` therefore triggers
    # dense recall twice: once nested inside its ``query_docs`` lex-pool
    # call, and once directly for the union pool. Pre-PR-3 the additive
    # ``query_docs`` skipped dense recall entirely, so this was exactly
    # one — that count encoded the now-superseded gated behavior, not
    # the spec's pool-size contract.
    assert len(dense_pool_sizes) >= 1
    # The union (lex_pool ∪ dense_pool) must have reached at least 100.
    # ``query_docs_ann``'s own dense pool is the LAST recorded call (the
    # directly-issued one); the lex pool is the single ``query_docs``
    # call. Both draw on the full 150-gene corpus here.
    assert lex_pool_sizes[0] + dense_pool_sizes[-1] >= 100, (
        f"union pool too small: lex={lex_pool_sizes[0]} "
        f"dense={dense_pool_sizes[-1]}"
    )
    # Final cut respects max_genes.
    assert len(out) <= 12, f"max_genes cap violated: {len(out)}"


# ── 3. Codec: full 1024-dim on encode, norm≈1, random-pair cosine < 0.10 ─


def test_codec_full_1024_dim_on_encode():
    """Spec §9 case 3 + §12 acceptance criterion: regression guard against
    the dim=256 collapse. Codec returns 1024-dim, L2-normalised, and 1k
    random-pair cosine has mean < 0.10.
    """
    codec = BGEM3Codec(dim=1024)

    # Mock the underlying model so we don't pull BGE-M3 weights.
    # The BGE-M3 model itself outputs 1024-dim normalised vectors; here we
    # simulate that with random gaussian vectors that get truncated +
    # renormalised exactly as in production.
    rng = np.random.default_rng(42)
    mock_model = MagicMock()
    def fake_encode(text, normalize_embeddings=True, show_progress_bar=False):
        # Keep the text-dependent variation — different inputs -> different vecs.
        seed = int.from_bytes(hashlib.sha256(str(text).encode()).digest()[:8], "little")
        local_rng = np.random.default_rng(seed)
        v = local_rng.standard_normal(2048).astype(np.float32)
        n = np.linalg.norm(v)
        if n > 0:
            v /= n
        return v
    mock_model.encode.side_effect = fake_encode
    codec._model = mock_model
    codec._backend = "sentence_transformers"

    # ── 3a. encoded length is 1024 ──
    vec = codec.encode("hello world", task="passage")
    assert len(vec) == 1024, f"expected 1024-dim, got {len(vec)}"

    # ── 3b. norm ≈ 1.0 ──
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    assert abs(norm - 1.0) < 1e-3, f"expected unit norm, got {norm:.6f}"

    # ── 3c. 1k random-pair cosine has mean < 0.10 ──
    # Two independent random gaussians on the unit hypersphere have
    # E[cosine] = 0 with std ~ 1/sqrt(dim). At dim=1024 the empirical mean
    # over 1k pairs is well under 0.10. dim=256 collapsed to ~0.6 because
    # the truncation broke orthogonality.
    pair_sims = []
    for i in range(1000):
        a = codec.encode(f"random text alpha {i}", task="passage")
        b = codec.encode(f"random text beta  {i}", task="passage")
        pair_sims.append(float(np.dot(np.asarray(a), np.asarray(b))))
    mean_sim = float(np.mean(pair_sims))
    assert mean_sim < 0.10, (
        f"random-pair cosine mean {mean_sim:.4f} is the dim=256 collapse signature"
    )


# ── 4. v2 BLOB ↔ JSON round-trip ─────────────────────────────────────


def test_v2_blob_roundtrip_matches_json():
    """Spec §9 case 4: encode same passage, write JSON via legacy path AND
    BLOB via v2 path, decode both, assert numpy.allclose within fp32 epsilon.
    """
    vec = _hash_vec("round-trip-passage", 1024)

    # Legacy JSON path: list[float] -> json.dumps -> json.loads -> np.array.
    json_text = json.dumps(vec.tolist())
    decoded_json = np.asarray(json.loads(json_text), dtype=np.float32)

    # v2 BLOB path: fp32 little-endian bytes -> np.frombuffer.
    blob = vec.astype("<f4").tobytes()
    decoded_blob = np.frombuffer(blob, dtype="<f4").astype(np.float32, copy=True)

    assert decoded_json.shape == decoded_blob.shape == (1024,)
    assert np.allclose(decoded_json, decoded_blob, atol=1e-7), (
        "JSON and BLOB round-trips diverged beyond fp32 epsilon"
    )


# ── 5. backfill is idempotent ────────────────────────────────────────


def test_backfill_v2_idempotent(tmp_path):
    """Spec §9 case 5: run backfill twice; second pass is a no-op.

    Smoke test against a tiny fixture DB — never against the production
    genome.db. We patch BGEM3Codec to a fake so we don't pull BGE-M3.
    """
    db_path = tmp_path / "fixture.db"
    g = Genome(path=str(db_path), dense_embedding_enabled=True, dense_embedding_dim=1024)
    for i in range(5):
        gene = _make_gene(f"idempotent fixture gene {i}", gene_id=f"idemp-{i}")
        g.upsert_gene(gene, apply_gate=False)
    g.close()

    # Patch the codec import inside the backfill script so it returns a fake.
    fake = _FakeCodec(dim=1024)
    with patch("helix_context.backends.bgem3_codec.BGEM3Codec", lambda dim, **kw: fake):
        # Run twice with --limit so the script doesn't try to phone home.
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "backfill_bgem3_v2.py"
        # Run via the script's main() directly to share the patch.
        sys.path.insert(0, str(repo_root / "scripts"))
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("backfill_bgem3_v2", script)
            mod = importlib.util.module_from_spec(spec)
            # First run — populates everything. Pin --dim 1024 so the test
            # is independent of whatever the local helix.toml says.
            sys.argv = ["backfill_bgem3_v2.py", str(db_path), "--dim", "1024"]
            spec.loader.exec_module(mod)
            mod.main()
        finally:
            sys.path.pop(0)

    # Capture the BLOBs after the first run.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows_before = {
        r["gene_id"]: bytes(r["embedding_dense_v2"]) if r["embedding_dense_v2"] else None
        for r in conn.execute("SELECT gene_id, embedding_dense_v2 FROM genes").fetchall()
    }
    conn.close()
    populated_after_first = sum(1 for v in rows_before.values() if v is not None)
    assert populated_after_first == 5, f"first run populated {populated_after_first}/5"

    # Second run — should skip everyone.
    with patch("helix_context.backends.bgem3_codec.BGEM3Codec", lambda dim, **kw: fake):
        sys.path.insert(0, str(repo_root / "scripts"))
        try:
            import importlib.util
            spec2 = importlib.util.spec_from_file_location("backfill_bgem3_v2_2", script)
            mod2 = importlib.util.module_from_spec(spec2)
            sys.argv = ["backfill_bgem3_v2.py", str(db_path), "--dim", "1024"]
            spec2.loader.exec_module(mod2)
            mod2.main()
        finally:
            sys.path.pop(0)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows_after = {
        r["gene_id"]: bytes(r["embedding_dense_v2"]) if r["embedding_dense_v2"] else None
        for r in conn.execute("SELECT gene_id, embedding_dense_v2 FROM genes").fetchall()
    }
    conn.close()

    assert rows_before == rows_after, "second backfill run modified rows"


# ── 6. matrix invalidation after insert ─────────────────────────────


def test_dense_matrix_invalidation_after_insert(dense_genome):
    """Spec §9 case 6: after a new upsert the matrix rebuilds with the new row.
    """
    g = dense_genome
    g._dense_codec = _FakeCodec(dim=1024)

    # Seed two genes + their v2 BLOBs.
    for i in range(2):
        gene = _make_gene(f"initial gene {i}", gene_id=f"init-{i}")
        g.upsert_gene(gene)
        _populate_v2(g, f"init-{i}", _hash_vec(f"vec init {i}", 1024))

    matrix1, ids1 = g._ensure_dense_matrix()
    assert matrix1 is not None
    n1 = matrix1.shape[0]

    # Insert a new gene + populate v2 — should invalidate.
    new_gene = _make_gene("late arrival gene", gene_id="late-001")
    g.upsert_gene(new_gene)
    _populate_v2(g, "late-001", _hash_vec("late vec", 1024))

    matrix2, ids2 = g._ensure_dense_matrix()
    assert matrix2 is not None
    assert matrix2.shape[0] == n1 + 1, f"matrix did not grow after insert: {n1} -> {matrix2.shape[0]}"
    assert "late-001" in ids2


# ── 7. fallback when v2 coverage is empty ───────────────────────────


def test_dense_recall_empty_v2_returns_empty_with_warn(dense_genome, caplog):
    """Spec §4 fallback: if v2 coverage is empty, dense recall returns []
    and logs a one-time warn. Caller degrades to lex-only.
    """
    g = dense_genome
    # Seed genes but DO NOT populate v2.
    for i in range(3):
        g.upsert_gene(_make_gene(f"unbackfilled {i}", gene_id=f"u-{i}"))
    g._dense_codec = _FakeCodec(dim=1024)

    import logging
    with caplog.at_level(logging.WARNING):
        out = g.query_genes_dense_recall("any query", k=500)
    assert out == []
    assert any("embedding_dense_v2 coverage" in rec.message for rec in caplog.records), (
        "expected one-time fallback warn"
    )

    # Calling again should still return [] but should NOT re-warn.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        out2 = g.query_genes_dense_recall("any query", k=500)
    assert out2 == []
    assert not any("embedding_dense_v2 coverage" in rec.message for rec in caplog.records), (
        "fallback warn should be one-time"
    )


# ── Tier-0 PR-3: dense recall decoupled from fusion_mode ─────────────
#
# Plan: docs/reviews/2026-05-16-deep-review/00-tier0-implementation-plan.md
# §PR-3, Decision 1 = Option B. Pre-PR-3, query_docs only ran dense
# recall under fusion_mode == "rrf"; the default additive mode never
# touched the BGE-M3 vectors. PR-3 changes the gate to
# dense_embedding_enabled alone and merges dense hits into gene_scores
# in additive mode with a real (cosine * dense_additive_weight) weight,
# not the 1e-9 epsilon the RRF path uses for set-membership only.


def _additive_dense_genome(dense_additive_weight: float = 4.0) -> Genome:
    """In-memory genome: dense ON, additive fusion (the shipped default)."""
    g = Genome(
        path=":memory:",
        dense_embedding_enabled=True,
        dense_embedding_dim=1024,
        dense_pool_size=500,
        fusion_mode="additive",
        dense_additive_weight=dense_additive_weight,
    )
    _orig = g.upsert_doc
    def _ungated(gene, apply_gate=False):
        return _orig(gene, apply_gate=apply_gate)
    g.upsert_doc = _ungated
    g.upsert_gene = _ungated
    return g


def test_query_docs_additive_mode_merges_dense_with_real_weight():
    """PR-3 Decision 1 Option B: in additive mode a dense-surfaced gene
    lands in gene_scores with contribution == cosine * dense_additive_weight
    — a real BM25-comparable weight, not the 1e-9 epsilon.

    The needle shares no surface tokens with the query, so its only path
    into gene_scores is the dense merge; tier_contrib['dense'] therefore
    equals the merged contribution exactly.
    """
    weight = 4.0
    g = _additive_dense_genome(dense_additive_weight=weight)
    try:
        # Distractors share the query's lex token; needle does not.
        for i in range(8):
            g.upsert_gene(_make_gene(
                f"alpha distractor body {i}", domains=["alpha"],
                gene_id=f"dist-{i:03d}",
            ))
        g.upsert_gene(_make_gene(
            "wholly disjoint surface tokens for the needle body",
            domains=["needle"], gene_id="needle-add",
        ))
        # Needle's v2 vector == what the query encoder will produce.
        target = "additive dense target vector"
        _populate_v2(g, "needle-add", _hash_vec(target, 1024))
        for row in g.conn.execute(
            "SELECT gene_id FROM genes WHERE gene_id != 'needle-add'"
        ).fetchall():
            _populate_v2(g, row[0], _hash_vec(row[0] + "-noise", 1024))

        g._dense_codec = _FakeCodec(dim=1024, query_target=target)

        docs = g.query_docs(domains=["alpha"], entities=[], max_genes=12)
        ids = {d.gene_id for d in docs}
        assert "needle-add" in ids, (
            f"dense-only needle should surface in additive mode; got {sorted(ids)}"
        )

        # gene_scores carries a real, non-epsilon contribution.
        scores = g.last_query_scores
        assert "needle-add" in scores
        assert scores["needle-add"] > 1e-3, (
            f"additive dense merge must be a real weight, not epsilon; "
            f"got {scores['needle-add']}"
        )
        # The needle is token-disjoint from the query, so 'dense' is its
        # only tier; its recorded contribution == cosine * weight, and the
        # FakeCodec maps query->needle so cosine == 1.0.
        dense_contrib = g.last_tier_contributions["needle-add"]["dense"]
        assert dense_contrib == pytest.approx(1.0 * weight, abs=1e-4), (
            f"dense contribution should be cosine*dense_additive_weight; "
            f"got {dense_contrib}"
        )
    finally:
        g.close()


def test_dense_additive_weight_scales_the_contribution():
    """PR-3: the dense_additive_weight knob is honoured — a higher weight
    yields a proportionally larger gene_scores contribution.
    """
    target = "weight-scaling dense target"

    def _run(weight: float) -> float:
        g = _additive_dense_genome(dense_additive_weight=weight)
        try:
            g.upsert_gene(_make_gene(
                "alpha lexical anchor doc", domains=["alpha"], gene_id="anchor",
            ))
            g.upsert_gene(_make_gene(
                "token-disjoint needle body", domains=["needle"],
                gene_id="needle-w",
            ))
            _populate_v2(g, "needle-w", _hash_vec(target, 1024))
            _populate_v2(g, "anchor", _hash_vec("anchor-noise", 1024))
            g._dense_codec = _FakeCodec(dim=1024, query_target=target)
            g.query_docs(domains=["alpha"], entities=[], max_genes=12)
            return g.last_tier_contributions["needle-w"]["dense"]

        finally:
            g.close()

    low = _run(2.0)
    high = _run(8.0)
    # cosine is identical (FakeCodec query->needle == 1.0) so the only
    # variable is the weight: 8.0 / 2.0 == 4x.
    assert high == pytest.approx(low * 4.0, rel=1e-4), (
        f"dense_additive_weight should scale the contribution linearly; "
        f"low(w=2)={low} high(w=8)={high}"
    )


def test_query_docs_rrf_mode_dense_still_fuser_tier():
    """PR-3 Decision 1 Option B: RRF mode keeps the pre-PR-3 behaviour —
    dense hits enter via the Fuser tier and dense-only genes get the
    1e-9 set-membership epsilon in gene_scores (ordering comes from the
    Fuser, not the epsilon).
    """
    g = Genome(
        path=":memory:",
        dense_embedding_enabled=True,
        dense_embedding_dim=1024,
        dense_pool_size=500,
        fusion_mode="rrf",
    )
    _orig = g.upsert_doc
    g.upsert_doc = g.upsert_gene = lambda gene, apply_gate=False: _orig(
        gene, apply_gate=apply_gate
    )
    try:
        for i in range(6):
            g.upsert_gene(_make_gene(
                f"alpha body {i}", domains=["alpha"], gene_id=f"a-{i}",
            ))
        g.upsert_gene(_make_gene(
            "token-disjoint needle for rrf", domains=["needle"],
            gene_id="needle-rrf",
        ))
        target = "rrf dense target"
        _populate_v2(g, "needle-rrf", _hash_vec(target, 1024))
        for row in g.conn.execute(
            "SELECT gene_id FROM genes WHERE gene_id != 'needle-rrf'"
        ).fetchall():
            _populate_v2(g, row[0], _hash_vec(row[0] + "-noise", 1024))
        g._dense_codec = _FakeCodec(dim=1024, query_target=target)

        docs = g.query_docs(domains=["alpha"], entities=[], max_genes=12)
        assert any(d.gene_id == "needle-rrf" for d in docs), (
            "dense-only needle should still surface under RRF fusion"
        )
        # Telemetry records the RAW cosine for the dense tier under RRF
        # (spec §6), NOT a weighted score — this is the additive-vs-RRF
        # contract difference.
        dense_contrib = g.last_tier_contributions["needle-rrf"]["dense"]
        assert dense_contrib == pytest.approx(1.0, abs=1e-4), (
            f"RRF dense tier_contrib is raw cosine (~1.0); got {dense_contrib}"
        )
    finally:
        g.close()


def test_query_docs_additive_dense_null_v2_degrades_to_lexical():
    """PR-3: with dense ON + additive mode but NULL embedding_dense_v2,
    query_docs degrades to lexical-only — no crash — and the one-time
    coverage WARN fires (dense recall returns []).
    """
    import logging
    g = _additive_dense_genome()
    try:
        for i in range(5):
            g.upsert_gene(_make_gene(
                f"alpha lexical doc {i}", domains=["alpha"], gene_id=f"lex-{i}",
            ))
        # Deliberately do NOT populate embedding_dense_v2.
        g._dense_codec = _FakeCodec(dim=1024)

        caplog_records: list = []
        handler = logging.Handler()
        handler.emit = lambda rec: caplog_records.append(rec)
        ks_log = logging.getLogger("helix_context.knowledge_store")
        ks_log.addHandler(handler)
        try:
            docs = g.query_docs(domains=["alpha"], entities=[], max_genes=12)
        finally:
            ks_log.removeHandler(handler)

        # Lexical path still returns the alpha docs.
        assert len(docs) == 5, f"expected lexical-only fallback, got {len(docs)}"
        # One-time coverage WARN fired from query_docs_dense_recall.
        assert any(
            "embedding_dense_v2 coverage" in rec.getMessage()
            for rec in caplog_records
        ), "expected the one-time empty-coverage WARN under additive mode"
    finally:
        g.close()


def test_dense_additive_weight_default_and_plumbing():
    """PR-3: dense_additive_weight defaults to 4.0 on a bare genome and
    is stored as _dense_additive_weight; an explicit value overrides it.
    """
    g_default = Genome(path=":memory:")
    g_custom = Genome(path=":memory:", dense_additive_weight=6.5)
    try:
        assert g_default._dense_additive_weight == pytest.approx(4.0)
        assert g_custom._dense_additive_weight == pytest.approx(6.5)
    finally:
        g_default.close()
        g_custom.close()


# ── 8. live — real BGE-M3 dense recall through query_docs (additive) ─


@pytest.mark.live
def test_live_additive_dense_recall_through_query_docs():
    """PR-3 live path: with the real BGE-M3 codec, a semantically-related
    but lexically-disjoint document is surfaced by query_docs in additive
    mode. Skipped automatically when the model is unavailable.
    """
    g = Genome(
        path=":memory:",
        dense_embedding_enabled=True,
        dense_embed_on_ingest=True,
        dense_embedding_dim=1024,
        fusion_mode="additive",
    )
    try:
        try:
            g._get_dense_codec()  # force the real load up front
        except Exception as exc:  # noqa: BLE001 — model download/import failure
            pytest.skip(f"BGE-M3 model unavailable: {exc}")

        # Lexical distractors share the query tag; the needle does not —
        # its only route into the candidate set is real dense similarity.
        for i in range(6):
            g.upsert_doc(_make_gene(
                f"unrelated filler passage number {i}", domains=["topic"],
                gene_id=f"live-dist-{i}",
            ), apply_gate=False)
        g.upsert_doc(_make_gene(
            "a database stores rows on disk and answers SQL queries",
            domains=["storage"], gene_id="live-needle",
        ), apply_gate=False)

        docs = g.query_docs(domains=["topic"], entities=[], max_genes=12)
        # The needle is token-disjoint from the "topic" tag; if it shows
        # up, real dense recall merged it into the additive accumulator.
        assert any(d.gene_id == "live-needle" for d in docs) or \
            "live-needle" in g.last_query_scores, (
            "real BGE-M3 dense recall should surface the semantically "
            "related needle in additive mode"
        )
    finally:
        g.close()
