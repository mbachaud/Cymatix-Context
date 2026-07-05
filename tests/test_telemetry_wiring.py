"""Unit tests for the 2026-07-01 hallucination-visibility telemetry wiring.

Covers the roadmap §3 instruments that make the know/miss + abstain +
freshness-demotion surfaces visible (the sub-10% hallucination target's
observability layer).

Seam: pre-seed ``helix_context.telemetry.otel._instruments`` with fakes —
the lazy getters return the seeded instrument instead of creating one, so
emissions are captured without an OTel SDK or collector.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from helix_context.telemetry import otel


class FakeInstrument:
    def __init__(self):
        self.calls = []

    def add(self, value, attributes=None):
        self.calls.append(("add", value, dict(attributes or {})))

    def record(self, value, attributes=None):
        self.calls.append(("record", value, dict(attributes or {})))

    def set(self, value, attributes=None):
        self.calls.append(("set", value, dict(attributes or {})))


_SEED_KEYS = (
    "know_decision", "know_confidence", "abstain", "freshness_demotion",
    "session_elided", "session_tokens_saved", "splice_ratio",
    "dense_cosine", "shard_fanout", "shard_discrimination", "budget_tier",
)


@pytest.fixture
def fakes(monkeypatch):
    seeded = {}
    for key in _SEED_KEYS:
        seeded[key] = FakeInstrument()
        monkeypatch.setitem(otel._instruments, key, seeded[key])
    return seeded


def _window(status="abstain", genes=0):
    return SimpleNamespace(
        context_health=SimpleNamespace(status=status, genes_expressed=genes),
        metadata={},
    )


# ── know/miss discriminator ──────────────────────────────────────────

def test_miss_abstain_emits_counter(fakes):
    from helix_context.scoring.know_decision import decide_know_or_miss

    out = decide_know_or_miss(
        _window("abstain"),
        query="q",
        top_score=1.0,
        score_gap=0.0,
        lexical_dense_agree=False,
        coordinate_confidence=0.0,
    )
    assert out.reason == "abstain"
    # #209 phase-1 convention: abstain is its own outcome, not a miss.
    assert fakes["know_decision"].calls == [
        ("add", 1, {"outcome": "abstain", "reason": "abstain"}),
    ]
    # Confidence histogram records know outcomes only.
    assert fakes["know_confidence"].calls == []


def test_no_promoter_match_emits_reason(fakes):
    from helix_context.scoring.know_decision import decide_know_or_miss

    out = decide_know_or_miss(
        _window("healthy", genes=0),
        query="q",
        top_score=0.0,
        score_gap=0.0,
        lexical_dense_agree=False,
        coordinate_confidence=0.0,
    )
    assert out.reason == "no_promoter_match"
    assert fakes["know_decision"].calls == [
        ("add", 1, {"outcome": "miss", "reason": "no_promoter_match"}),
    ]


def test_know_emits_confidence_histogram(fakes):
    from helix_context.scoring.know_decision import (
        KnowCalibration,
        decide_know_or_miss,
    )

    out = decide_know_or_miss(
        _window("healthy", genes=3),
        query="what port does helix use",
        top_score=10.0,
        score_gap=5.0,
        lexical_dense_agree=True,
        coordinate_confidence=1.0,
        calibration=KnowCalibration(emit_floor=0.0),
    )
    assert hasattr(out, "confidence"), f"expected KnowBlock, got {out!r}"
    # #209 phase-1 convention: reason is "none" for know outcomes.
    assert fakes["know_decision"].calls == [
        ("add", 1, {"outcome": "know", "reason": "none"}),
    ]
    recs = fakes["know_confidence"].calls
    assert len(recs) == 1
    assert recs[0][0] == "record"
    assert recs[0][1] == pytest.approx(out.confidence)


# ── ABSTAIN gate trigger attribution ─────────────────────────────────

def test_abstain_gate_additive_floor_and_ratio(fakes):
    from helix_context.config import AbstainClassFloors
    from helix_context.pipeline.tier_logic import apply_budget_tiers

    genes = [SimpleNamespace(gene_id=f"g{i}") for i in range(6)]
    scores = {f"g{i}": 1.0 for i in range(6)}  # flat + weak → abstain
    res = apply_budget_tiers(
        genes, scores, AbstainClassFloors(),
        abstain_enabled=True, fusion_mode="additive",
    )
    assert res.abstain is True
    assert fakes["abstain"].calls == [
        ("add", 1, {"gate": "floor_and_ratio", "fusion_mode": "additive"}),
    ]
    # The legacy tier label still fires alongside.
    assert ("add", 1, {"tier": "abstain"}) in fakes["budget_tier"].calls


def test_abstain_gate_rrf_ratio_only(fakes):
    from helix_context.config import AbstainClassFloors
    from helix_context.pipeline.tier_logic import apply_budget_tiers

    genes = [SimpleNamespace(gene_id=f"g{i}") for i in range(6)]
    scores = {f"g{i}": 0.25 for i in range(6)}  # all tied → norm ratio 0
    res = apply_budget_tiers(
        genes, scores, AbstainClassFloors(),
        abstain_enabled=True, fusion_mode="rrf",
    )
    assert res.abstain is True
    assert fakes["abstain"].calls == [
        ("add", 1, {"gate": "ratio_only", "fusion_mode": "rrf"}),
    ]


def test_no_abstain_no_emit(fakes):
    from helix_context.config import AbstainClassFloors
    from helix_context.pipeline.tier_logic import apply_budget_tiers

    genes = [SimpleNamespace(gene_id=f"g{i}") for i in range(6)]
    # Strong, separated top → no abstain.
    scores = {"g0": 9.0, "g1": 1.0, "g2": 1.0, "g3": 1.0, "g4": 1.0, "g5": 1.0}
    res = apply_budget_tiers(
        genes, scores, AbstainClassFloors(),
        abstain_enabled=True, fusion_mode="additive",
    )
    assert res.abstain is False
    assert fakes["abstain"].calls == []


# ── freshness demotions ──────────────────────────────────────────────

def test_freshness_missing_emits(fakes, tmp_path):
    from helix_context.retrieval.freshness import revalidate_and_mark

    gene = SimpleNamespace(
        gene_id="g1",
        source_id=str(tmp_path / "does-not-exist.md"),
        last_verified_at=123.0,
    )
    genome = SimpleNamespace(mark_verified=lambda *a, **k: None)
    status = revalidate_and_mark(
        genome, gene, mtime_cache={}, now_ts=1000.0, read_only=True,
    )
    assert status == "missing"
    assert fakes["freshness_demotion"].calls == [
        ("add", 1, {"status": "missing"}),
    ]


def test_freshness_fresh_not_emitted(fakes, tmp_path):
    from helix_context.retrieval.freshness import revalidate_and_mark

    src = tmp_path / "src.md"
    src.write_text("content")
    gene = SimpleNamespace(
        gene_id="g1",
        source_id=str(src),
        last_verified_at=time.time() + 3600,  # verified after mtime → fresh
    )
    genome = SimpleNamespace(mark_verified=lambda *a, **k: None)
    status = revalidate_and_mark(
        genome, gene, mtime_cache={}, now_ts=time.time(), read_only=True,
    )
    assert status == "fresh"
    assert fakes["freshness_demotion"].calls == []


class _Cur:
    def __init__(self, row):
        self._row = row

    def execute(self, sql, params):
        return self

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _Cur(self._row)


def test_superseded_emits(fakes):
    from helix_context.retrieval.freshness import check_superseded

    genome = SimpleNamespace(read_conn=_Conn(("g2", "src-2")))
    out = check_superseded(genome, SimpleNamespace(gene_id="g1"))
    assert out == "src-2"
    assert fakes["freshness_demotion"].calls == [
        ("add", 1, {"status": "superseded"}),
    ]


def test_not_superseded_no_emit(fakes):
    from helix_context.retrieval.freshness import check_superseded

    genome = SimpleNamespace(read_conn=_Conn(None))
    out = check_superseded(genome, SimpleNamespace(gene_id="g1"))
    assert out is None
    assert fakes["freshness_demotion"].calls == []


# ── getter registration smoke ────────────────────────────────────────

# Union of the getter lists formerly smoke-tested in three places:
# this file (14 hallucination-visibility getters), test_telemetry_phase1
# (6-getter #209 subset, all already here), and test_telemetry_pipeline
# (3 per-stage pipeline getters).
_GETTER_NAMES = (
    "know_decision_counter", "know_confidence_histogram",
    "abstain_counter", "freshness_demotion_counter",
    "session_elided_counter", "session_tokens_saved_counter",
    "splice_ratio_histogram", "dense_cosine_histogram",
    "shard_fanout_histogram", "shard_discrimination_histogram",
    "pki_candidates_histogram", "pki_pairs_skipped_counter",
    "fingerprint_filtered_counter", "ingest_vram_gauge",
    # per-stage pipeline telemetry (feat/per-stage-telemetry)
    "pipeline_stage_histogram", "ribosome_call_histogram",
    "genome_signal_histogram",
)


@pytest.mark.parametrize("name", _GETTER_NAMES)
def test_all_new_getters_resolve(name):
    """Consolidated getter-registry smoke over _GETTER_NAMES: every lazy
    getter is re-exported through helix_context.telemetry, returns an
    instrument (noop or real), and caches it — repeated calls return the
    same object."""
    import helix_context.telemetry as tel

    getter = getattr(tel, name)
    inst = getter()
    assert hasattr(inst, "add") or hasattr(inst, "record"), name
    assert getter() is inst, f"{name} not cached"
