"""Issue #214: dense pool floor + true-pair calibration cap.

The margin-over-random ANN calibration (``mu + sigma_mult*sigma`` over
RANDOM gene pairs) can land ABOVE every real query-doc cosine — measured
calibrated threshold 0.779 vs corpus max ~0.713 (golds 0.46-0.68), so
0/5000 pool docs cleared the gate by dense and 70.0% of golds on a
480-question ERB run never surfaced in top-10 (independent measurement:
67.2%). Pool membership is strictly upstream of fusion ranking. Two fixes
under test, neither requiring a model:

1. ``knowledge_store.apply_ann_gate`` — runtime floor: when fewer than
   ``dense_pool_floor_genes`` dense-scored candidates survive the threshold
   cut (but the dense leg HAD scored candidates), the top-N dense hits by
   cosine are admitted into the pool anyway. Default 8; 0 = legacy.
2. ``scripts.calibrate_thresholds.cap_threshold_with_true_pairs`` — the
   calibration-method cap: ``final = min(random_pair_bound,
   P05(true_pairs) - 0.02)``; codec unavailable -> legacy value + flag.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Make scripts/ importable (same convention as tests/test_calibration.py).
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from helix_context.config import HelixConfig, load_config
from helix_context.genome import Genome
from helix_context.knowledge_store import apply_ann_gate
from helix_context.schemas import (
    ChromatinState, EpigeneticMarkers, Gene, PromoterTags,
)
from tests.conftest import FakeBGEM3Codec, hash_vec


# ─── apply_ann_gate unit tests (pure gate, synthetic cosines) ─────────────


def _dense(n: int, start: float = 0.70, step: float = 0.01):
    """n synthetic dense hits, cosine-descending: d0=start, d1=start-step, ..."""
    return [(f"d{i}", start - i * step) for i in range(n)]


def test_threshold_above_all_floor_admits_top8():
    """The #214 shape: calibrated threshold sits above every real cosine.

    Legacy gate admits nothing (min_genes=0); the floor admits the top-8
    dense candidates by cosine.
    """
    dense = _dense(20)  # 0.70 .. 0.51 — all below 0.779
    out = apply_ann_gate(
        dense, dense,
        threshold=0.779, min_genes=0, max_genes=12,
        dense_pool_floor_genes=8,
    )
    assert out == [f"d{i}" for i in range(8)]


def test_healthy_gate_is_byte_identical_with_floor_on():
    """>= 8 dense candidates pass the threshold -> floor changes nothing."""
    dense = _dense(20, start=0.90)  # 0.90 .. 0.71; threshold 0.58 passes all
    legacy = apply_ann_gate(
        dense, dense, threshold=0.58, min_genes=1, max_genes=12,
        dense_pool_floor_genes=0,
    )
    floored = apply_ann_gate(
        dense, dense, threshold=0.58, min_genes=1, max_genes=12,
        dense_pool_floor_genes=8,
    )
    assert floored == legacy
    assert len(legacy) == 12  # max_genes cap still binds


def test_floor_zero_keeps_legacy_empty_result():
    """floor=0 is the legacy gate: nothing passes, nothing returned."""
    dense = _dense(20)
    out = apply_ann_gate(
        dense, dense, threshold=0.779, min_genes=0, max_genes=12,
        dense_pool_floor_genes=0,
    )
    assert out == []


def test_fewer_than_floor_total_candidates_all_admitted():
    """Only 3 dense candidates exist -> the floor admits all 3, not 8."""
    dense = _dense(3)
    out = apply_ann_gate(
        dense, dense, threshold=0.99, min_genes=0, max_genes=12,
        dense_pool_floor_genes=8,
    )
    assert out == ["d0", "d1", "d2"]


def test_rescued_ordering_follows_cosine_not_id():
    """Floor-rescued candidates come back in cosine order (dense_hits
    order), regardless of gene_id lexicographic order."""
    dense = [("zz", 0.70), ("aa", 0.65), ("mm", 0.60), ("qq", 0.55)]
    out = apply_ann_gate(
        dense, dense, threshold=0.9, min_genes=0, max_genes=12,
        dense_pool_floor_genes=3,
    )
    assert out == ["zz", "aa", "mm"]


def test_min_genes_rescues_stay_ahead_of_floor_appends():
    """The exact broken-calibration shape: lex pins (threshold-0.01) sort
    ABOVE every dense cosine, so the min_genes floor rescues only lex docs.
    The dense floor appends after the legacy result without displacing it.
    """
    threshold = 0.779
    dense = _dense(5)  # 0.70 .. 0.66, all below the lex pin 0.769
    scored = [("lex1", threshold - 0.01)] + dense
    out = apply_ann_gate(
        scored, dense, threshold=threshold, min_genes=1, max_genes=12,
        dense_pool_floor_genes=8,
    )
    assert out[0] == "lex1"  # legacy min_genes rescue untouched, still first
    assert out[1:] == [f"d{i}" for i in range(5)]


def test_floor_counts_dense_already_admitted_and_tops_up():
    """5 dense pass the threshold on their own; floor=8 only appends 3."""
    dense = [
        ("d0", 0.95), ("d1", 0.93), ("d2", 0.91), ("d3", 0.89), ("d4", 0.87),
        ("d5", 0.40), ("d6", 0.38), ("d7", 0.36), ("d8", 0.34), ("d9", 0.32),
    ]
    out = apply_ann_gate(
        dense, dense, threshold=0.85, min_genes=0, max_genes=12,
        dense_pool_floor_genes=8,
    )
    assert out == [f"d{i}" for i in range(8)]


def test_no_dense_candidates_floor_is_noop():
    """Dense leg scored nothing (empty corpus / no vectors): the floor must
    not invent candidates — legacy behavior exactly."""
    scored = [("lex1", 0.57), ("lex2", 0.57)]
    out = apply_ann_gate(
        scored, [], threshold=0.58, min_genes=1, max_genes=12,
        dense_pool_floor_genes=8,
    )
    assert out == ["lex1"]


# ─── Config plumbing ──────────────────────────────────────────────────────


def test_dense_pool_floor_default_is_8():
    cfg = HelixConfig()
    assert cfg.retrieval.dense_pool_floor_genes == 8


def test_dense_pool_floor_loads_from_toml(tmp_path):
    toml = tmp_path / "helix.toml"
    toml.write_text("[retrieval]\ndense_pool_floor_genes = 3\n", encoding="utf-8")
    cfg = load_config(str(toml))
    assert cfg.retrieval.dense_pool_floor_genes == 3


def test_dense_pool_floor_toml_zero_disables(tmp_path):
    toml = tmp_path / "helix.toml"
    toml.write_text("[retrieval]\ndense_pool_floor_genes = 0\n", encoding="utf-8")
    cfg = load_config(str(toml))
    assert cfg.retrieval.dense_pool_floor_genes == 0


def test_dense_pool_floor_reaches_store():
    g = Genome(path=":memory:", dense_pool_floor_genes=5)
    try:
        assert g._dense_pool_floor_genes == 5
    finally:
        g.close()


def test_dense_pool_floor_store_default():
    g = Genome(path=":memory:")
    try:
        assert g._dense_pool_floor_genes == 8
    finally:
        g.close()


# ─── Calibration cap math (pure functions, no model) ─────────────────────


def test_true_pair_cap_wins_when_random_bound_above_real_cosines():
    """The #214 shape: random-pair bound 0.779 vs true pairs 0.46..0.68 ->
    the cap (P05 - 0.02) wins and lands inside the real cosine range."""
    from scripts.calibrate_thresholds import (
        _percentile, cap_threshold_with_true_pairs,
    )
    true_pairs = [0.46 + 0.002 * i for i in range(111)]  # 0.46 .. 0.68
    out = cap_threshold_with_true_pairs(0.779, true_pairs)
    expected = _percentile(true_pairs, 5.0) - 0.02
    assert out["winning_bound"] == "true_pair_cap"
    assert out["guard_skipped"] is False
    assert out["threshold"] == pytest.approx(expected)
    assert out["threshold"] < 0.713  # below the real query-doc cosine max
    assert out["random_pair_threshold"] == pytest.approx(0.779)
    assert out["true_pair_p05"] == pytest.approx(_percentile(true_pairs, 5.0))
    assert out["true_pair_n"] == 111


def test_random_pair_bound_wins_when_already_below_true_pairs():
    """Healthy calibration: the random-pair bound sits below the true-pair
    cap -> it is kept unchanged (the guard is a cap, never a raise)."""
    from scripts.calibrate_thresholds import cap_threshold_with_true_pairs
    out = cap_threshold_with_true_pairs(0.40, [0.80, 0.82, 0.84, 0.86, 0.88])
    assert out["winning_bound"] == "random_pair"
    assert out["threshold"] == pytest.approx(0.40)
    assert out["guard_skipped"] is False


def test_missing_true_pairs_falls_back_to_legacy_with_flag():
    from scripts.calibrate_thresholds import cap_threshold_with_true_pairs
    for missing in (None, []):
        out = cap_threshold_with_true_pairs(0.779, missing)
        assert out["guard_skipped"] is True
        assert out["winning_bound"] == "random_pair"
        assert out["threshold"] == pytest.approx(0.779)
        assert out["true_pair_p05"] is None


def test_guard_skip_threads_legacy_value_into_result_and_report(monkeypatch):
    """Codec unavailable -> apply_true_pair_guard keeps the legacy value,
    flags the skip, and emit_report records both bounds."""
    import scripts.calibrate_thresholds as ct
    ann = ct.AnnCalibrationResult(
        threshold=0.779, mu=0.5, sigma=0.093, n_pairs=1000,
        dim=64, sigma_mult=3.0, seed=42, n_genes=50,
    )
    monkeypatch.setattr(
        ct, "measure_true_pair_cosines",
        lambda *a, **k: (None, "dense codec unavailable: stub"),
    )
    out = ct.apply_true_pair_guard(ann, Path("unused.db"), dim=64)
    assert out.guard_skipped is True
    assert out.threshold == pytest.approx(0.779)
    assert out.winning_bound == "random_pair"
    assert "codec unavailable" in (out.guard_skip_reason or "")

    report = ct.emit_report(
        out, ct.FloorCalibrationResult(), genome_path=Path("unused.db"),
    )
    blk = report["ann_threshold"]
    assert blk["value"] == pytest.approx(0.779)
    assert blk["random_pair_threshold"] == pytest.approx(0.779)
    assert blk["true_pair_guard"]["skipped"] is True
    assert blk["true_pair_guard"]["winning_bound"] == "random_pair"


def test_guard_caps_threshold_end_to_end_through_apply(monkeypatch):
    """Measured true pairs below the random bound -> apply_true_pair_guard
    rewrites threshold to P05-0.02 and records the winning bound."""
    import scripts.calibrate_thresholds as ct
    ann = ct.AnnCalibrationResult(
        threshold=0.779, mu=0.5, sigma=0.093, n_pairs=1000,
        dim=64, sigma_mult=3.0, seed=42, n_genes=50,
        random_pair_threshold=0.779,
    )
    true_pairs = [0.46 + 0.002 * i for i in range(111)]
    monkeypatch.setattr(
        ct, "measure_true_pair_cosines", lambda *a, **k: (true_pairs, None),
    )
    out = ct.apply_true_pair_guard(ann, Path("unused.db"), dim=64)
    assert out.guard_skipped is False
    assert out.winning_bound == "true_pair_cap"
    assert out.threshold == pytest.approx(ct._percentile(true_pairs, 5.0) - 0.02)
    assert out.random_pair_threshold == pytest.approx(0.779)
    # The snippet must carry both bounds for the operator.
    snippet = ct.emit_toml_snippet(out, ct.FloorCalibrationResult())
    assert "true-pair guard" in snippet
    assert "winning bound: true_pair_cap" in snippet


def test_query_synthesis_first_tokens():
    from scripts.calibrate_thresholds import _synthesize_query
    content = " ".join(f"tok{i}" for i in range(40))
    q = _synthesize_query(content)
    assert q == " ".join(f"tok{i}" for i in range(12))
    assert _synthesize_query("too short") is None  # < 4 tokens


# ─── Integration-lite: in-memory Genome, absurd threshold ────────────────
#
# Fixture style mirrors tests/test_dense_recall.py (FakeBGEM3Codec +
# hash_vec from tests/conftest.py; v2 BLOBs written directly).


def _make_gene(content: str, *, domains, gene_id: str) -> Gene:
    return Gene(
        gene_id=gene_id,
        content=content,
        complement="",
        codons=[],
        promoter=PromoterTags(domains=domains, entities=[]),
        epigenetics=EpigeneticMarkers(),
        chromatin=ChromatinState.OPEN,
        is_fragment=False,
    )


def _populate_v2(genome: Genome, gene_id: str, vec) -> None:
    blob = vec.astype("<f4").tobytes()
    genome.conn.execute(
        "UPDATE genes SET embedding_dense_v2 = ? WHERE gene_id = ?",
        (sqlite3.Binary(blob), gene_id),
    )
    genome.conn.commit()
    genome._invalidate_dense_matrix()


@pytest.fixture
def floor_genome():
    """In-memory genome, dense retrieval on, default floor (8)."""
    g = Genome(
        path=":memory:",
        dense_embedding_enabled=True,
        dense_embedding_dim=1024,
        dense_pool_size=500,
    )
    _orig_upsert = g.upsert_doc
    def _ungated(gene, apply_gate=False):
        return _orig_upsert(gene, apply_gate=apply_gate)
    g.upsert_doc = _ungated
    g.upsert_gene = _ungated
    yield g
    g.close()


def _seed_corpus(g: Genome) -> None:
    # 4 lexical docs matching domain "alpha"; NO dense vectors (they enter
    # the union as threshold-0.01 lex pins only).
    for i in range(4):
        g.upsert_gene(_make_gene(
            f"alpha lexical doc number {i} body text",
            domains=["alpha"], gene_id=f"lex-{i}",
        ))
    # 6 dense-only docs: no lexical overlap with the query; v2 vectors set
    # (hash_vec pairs are near-orthogonal -> cosines far below 0.99).
    for i in range(6):
        gid = f"dense-{i}"
        g.upsert_gene(_make_gene(
            f"wholly unrelated surface body {i} zzz qqq",
            domains=["other"], gene_id=gid,
        ))
        _populate_v2(g, gid, hash_vec(f"vector for {gid}", 1024))


def test_absurd_threshold_floor_on_dense_still_surfaces(floor_genome):
    """ann threshold 0.99 (above every fake cosine): with the default floor
    the dense leg still lands candidates in the retrieve() pool."""
    g = floor_genome
    _seed_corpus(g)
    g._dense_codec = FakeBGEM3Codec(dim=1024)
    g._ann_threshold = 0.99
    assert g._dense_pool_floor_genes == 8  # default ON

    out = g.query_docs_ann(
        "completely different query wording xyzzy",
        domains=["alpha"], entities=[], max_genes=12,
    )
    ids = [d.gene_id for d in out]
    dense_ids = [i for i in ids if i.startswith("dense-")]
    assert dense_ids, f"dense leg gated to zero despite floor; got {ids}"
    # Fewer than 8 dense docs exist -> all 6 must be admitted.
    assert set(dense_ids) == {f"dense-{i}" for i in range(6)}, ids
    # Rescued block ordering follows the dense cosine order.
    recall = dict(g.query_docs_dense_recall(
        "completely different query wording xyzzy", k=500,
    ))
    cosines = [recall[i] for i in dense_ids]
    assert cosines == sorted(cosines, reverse=True)


def test_absurd_threshold_floor_zero_gates_dense_out(floor_genome):
    """Same corpus, floor disabled: legacy behavior — the dense leg is gated
    out of pool construction entirely (the #214 failure mode)."""
    g = floor_genome
    _seed_corpus(g)
    g._dense_codec = FakeBGEM3Codec(dim=1024)
    g._ann_threshold = 0.99
    g._dense_pool_floor_genes = 0  # legacy gate-only

    out = g.query_docs_ann(
        "completely different query wording xyzzy",
        domains=["alpha"], entities=[], max_genes=12,
    )
    ids = [d.gene_id for d in out]
    assert not any(i.startswith("dense-") for i in ids), (
        f"floor=0 must reproduce the legacy gate; got {ids}"
    )
    # min_genes floor (default 1) still returns the top lex pin.
    assert len(out) >= 1
