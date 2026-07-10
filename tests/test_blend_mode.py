"""Issue #255 / audit §4 item 5: post-fusion BLEND-layer mode knob.

The blend layer (``helix_context/scoring/blend.py``) runs three post-fusion
refiners — cymatics (``* 0.5``), harmonic_bin (``* 1.5``), TCM
(``BONUS_WEIGHT = 0.3``) — that mutate ``genome.last_query_scores`` on an
unspecified score scale (audit §2d, class d) and contaminate the ``[know]``
logistic that reads the mutated map. ``[retrieval] blend_mode`` exposes three
modes; ``"legacy"`` (default) is BYTE-IDENTICAL to the shipped additive blend.

Test families:
  1. ``_scale_relative_multiplier`` unit correctness (the exact mapping),
  2. legacy byte-identity (golden == the additive formula master runs),
  3. off leaves ``last_query_scores`` untouched + candidates pure-fused,
     and the non-scoring truncation side effect still runs,
  4. scale_relative is order-preserving under uniform rescale of inputs
     (the invariance the additive form breaks — pinned as a contrast),
  5. config threading (default + TOML -> load_config) and ValueError on
     an unknown mode (pure function + manager construction).

The refiner internals are monkeypatched to deterministic bonuses so the three
modes are compared on identical signal — the knob, not the spectra, is the
unit under test.

Design record: docs/research/2026-07-08-scoring-invariance-audit.md §4 item 5;
docs/research/2026-07-10-rerank-combinator-desktest.md §5 (the off-cell floor).
"""
from __future__ import annotations

import textwrap

import pytest

from helix_context.config import RetrievalConfig, load_config
from helix_context.scoring.blend import (
    VALID_BLEND_MODES,
    _BLEND_SCALE_REF,
    _scale_relative_multiplier,
    apply_candidate_refiners,
)


# ── lightweight fakes ─────────────────────────────────────────────────


class _FakeGene:
    """Minimal candidate: the blend layer only reads ``gene_id``."""

    __slots__ = ("gene_id",)

    def __init__(self, gene_id: str):
        self.gene_id = gene_id

    def __repr__(self):  # pragma: no cover - debug aid
        return f"_FakeGene({self.gene_id!r})"


class _FakeGenome:
    """Carries the mutable ``last_query_scores`` map the blend reads/writes."""

    def __init__(self, scores):
        self.last_query_scores = dict(scores)


def _patch_cymatics(monkeypatch, flux_map):
    """Drive the cymatics refiner with a controlled per-gene flux.

    ``bonus = flux_map[gene_id] * 0.5`` — the blend imports these names from
    ``helix_context.scoring.cymatics`` inside the call, so patching the module
    attributes is picked up at call time.
    """
    import helix_context.scoring.cymatics as cym

    monkeypatch.setattr(cym, "query_spectrum",
                        lambda *a, **k: "QSPEC", raising=True)
    monkeypatch.setattr(cym, "build_weight_vector",
                        lambda *a, **k: "WEIGHTS", raising=True)
    monkeypatch.setattr(cym, "cached_doc_spectrum",
                        lambda doc, **k: doc.gene_id, raising=True)
    monkeypatch.setattr(cym, "flux_score_dispatch",
                        lambda q, g_spec, w, metric: flux_map[g_spec],
                        raising=True)


def _cymatics_only(genome, candidates, *, blend_mode, max_genes=32):
    """Run the blend with only the cymatics refiner active."""
    return apply_candidate_refiners(
        "q",
        candidates,
        max_genes,
        genome=genome,
        cymatics_enabled=True,
        use_cymatics=True,
        use_harmonic_bin=False,   # needs the ray_trace graph; off for isolation
        use_tcm=False,
        tcm_session=None,
        blend_mode=blend_mode,
    )


# ═══ 1. the scale_relative mapping ════════════════════════════════════


def test_scale_relative_multiplier_exact_mapping():
    # max-cap bonus -> C / S_REF boost on top of 1.0.
    assert _scale_relative_multiplier(0.5, 0.5) == pytest.approx(1.0 + 0.5 / _BLEND_SCALE_REF)
    assert _scale_relative_multiplier(1.5, 1.5) == pytest.approx(1.0 + 1.5 / _BLEND_SCALE_REF)
    assert _scale_relative_multiplier(0.3, 0.3) == pytest.approx(1.0 + 0.3 / _BLEND_SCALE_REF)
    # in-range bonus b reduces to the clean form (1 + b / S_REF) (C cancels).
    assert _scale_relative_multiplier(0.25, 0.5) == pytest.approx(1.0 + 0.25 / _BLEND_SCALE_REF)


def test_scale_relative_multiplier_is_bounded_and_clamped():
    assert _scale_relative_multiplier(0.0, 0.5) == 1.0        # no signal -> no-op
    assert _scale_relative_multiplier(-3.0, 0.5) == 1.0       # negatives floored
    # overshoot clamps signal_norm at 1.0 (bounded at the max-cap boost).
    assert _scale_relative_multiplier(2.0, 0.5) == pytest.approx(1.0 + 0.5 / _BLEND_SCALE_REF)
    assert _scale_relative_multiplier(0.4, 0.0) == 1.0       # degenerate cap


def test_scale_relative_preserves_signal_ratios():
    # The three signals keep their 0.5 : 1.5 : 0.3 relative magnitude.
    cy = _scale_relative_multiplier(0.5, 0.5) - 1.0
    ha = _scale_relative_multiplier(1.5, 1.5) - 1.0
    tc = _scale_relative_multiplier(0.3, 0.3) - 1.0
    assert ha == pytest.approx(3.0 * cy)
    assert cy == pytest.approx((0.5 / 0.3) * tc)


# ═══ 2. legacy byte-identity ══════════════════════════════════════════


def test_legacy_is_byte_identical_additive_blend(monkeypatch):
    """legacy reproduces master's inline formula exactly:
        scores[g] = scores.get(g, 0) + flux(g) * 0.5
    Golden is computed with the SAME arithmetic, so equality is byte-level.
    """
    base = {"a": 10.0, "b": 8.0, "c": 6.0}
    flux_map = {"a": 1.0, "b": 0.4, "c": 0.8}
    _patch_cymatics(monkeypatch, flux_map)

    genome = _FakeGenome(base)
    cands = [_FakeGene("a"), _FakeGene("b"), _FakeGene("c")]
    out, contrib = _cymatics_only(genome, cands, blend_mode="legacy")

    expected = {g: base[g] + flux_map[g] * 0.5 for g in base}
    assert genome.last_query_scores == expected            # bitwise dict equality
    # sorted desc by the blended score.
    assert [g.gene_id for g in out] == sorted(expected, key=lambda g: -expected[g])
    # refiner_contrib records the raw bonus per gene.
    for g in base:
        assert contrib[g]["cymatics"] == pytest.approx(flux_map[g] * 0.5)


def test_default_blend_mode_is_legacy_on_missing_kwarg(monkeypatch):
    """Omitting blend_mode entirely == legacy (the shipped default path)."""
    base = {"a": 5.0, "b": 5.0}
    flux_map = {"a": 1.0, "b": 0.0}
    _patch_cymatics(monkeypatch, flux_map)

    genome = _FakeGenome(base)
    cands = [_FakeGene("a"), _FakeGene("b")]
    apply_candidate_refiners(
        "q", cands, 32, genome=genome,
        cymatics_enabled=True, use_cymatics=True,
        use_harmonic_bin=False, use_tcm=False, tcm_session=None,
    )  # no blend_mode kwarg -> default "legacy"
    assert genome.last_query_scores == {"a": 5.5, "b": 5.0}


# ═══ 3. off — no blend mutation, pure fused, side effects still run ════


def test_off_leaves_last_query_scores_untouched(monkeypatch):
    base = {"a": 1.0, "b": 2.0, "c": 3.0}
    flux_map = {"a": 1.0, "b": 1.0, "c": 1.0}   # would nudge every score in legacy
    _patch_cymatics(monkeypatch, flux_map)

    genome = _FakeGenome(base)
    # fused-descending order (as retrieval delivers it).
    cands = [_FakeGene("c"), _FakeGene("b"), _FakeGene("a")]
    out, contrib = _cymatics_only(genome, cands, blend_mode="off")

    assert genome.last_query_scores == base            # untouched by the blend
    assert [g.gene_id for g in out] == ["c", "b", "a"]  # pure fused order preserved
    assert contrib == {}                                # nothing recorded under off


def test_off_still_truncates_to_max_genes(monkeypatch):
    """The one non-scoring side effect (truncation to max_genes) still runs."""
    base = {"c": 3.0, "b": 2.0, "a": 1.0, "e": 0.5, "d": 0.2}
    flux_map = {k: 1.0 for k in base}
    _patch_cymatics(monkeypatch, flux_map)

    genome = _FakeGenome(base)
    cands = [_FakeGene(x) for x in ("c", "b", "a", "e", "d")]
    out, _ = _cymatics_only(genome, cands, blend_mode="off", max_genes=3)

    assert [g.gene_id for g in out] == ["c", "b", "a"]  # top-3 by fused, truncated
    assert genome.last_query_scores == base             # still untouched


# ═══ 4. scale_relative order-invariance under uniform rescale ═════════


@pytest.mark.parametrize("mode,should_be_stable", [
    ("scale_relative", True),
    ("legacy", False),
])
def test_uniform_rescale_order_stability(monkeypatch, mode, should_be_stable):
    """scale_relative preserves the emitted order under uniform rescale of the
    input scores; legacy (fixed additive bonus) does NOT — it flips as the
    score scale shifts. This is the invariance that motivates the mode.
    """
    # a: max cymatics signal but slightly lower base; b: no signal, higher base.
    flux_map = {"a": 1.0, "b": 0.0}          # bonus a=0.5, b=0.0
    _patch_cymatics(monkeypatch, flux_map)

    orders = set()
    for c in (1.0, 10.0, 100.0):
        genome = _FakeGenome({"a": 1.0 * c, "b": 1.3 * c})
        cands = [_FakeGene("a"), _FakeGene("b")]
        out, _ = _cymatics_only(genome, cands, blend_mode=mode)
        orders.add(tuple(g.gene_id for g in out))

    if should_be_stable:
        assert len(orders) == 1                          # order invariant
        assert orders == {("b", "a")}                    # b's higher base wins at every scale
    else:
        assert len(orders) > 1                           # legacy flips across scales


def test_scale_relative_multiplies_rather_than_adds(monkeypatch):
    base = {"a": 10.0, "b": 20.0}
    flux_map = {"a": 1.0, "b": 0.0}          # bonus a=0.5 -> multiplier 1.05
    _patch_cymatics(monkeypatch, flux_map)

    genome = _FakeGenome(base)
    cands = [_FakeGene("a"), _FakeGene("b")]
    _cymatics_only(genome, cands, blend_mode="scale_relative")

    assert genome.last_query_scores["a"] == pytest.approx(10.0 * (1.0 + 0.5 / _BLEND_SCALE_REF))
    assert genome.last_query_scores["b"] == pytest.approx(20.0)   # zero signal -> unchanged


# ═══ 5. config threading + validation ════════════════════════════════


def test_config_default_blend_mode_is_legacy():
    assert RetrievalConfig().blend_mode == "legacy"
    assert VALID_BLEND_MODES == ("legacy", "scale_relative", "off")


def test_config_threads_blend_mode_from_toml(tmp_path):
    toml = tmp_path / "helix.toml"
    toml.write_text(textwrap.dedent("""
        [retrieval]
        blend_mode = "scale_relative"
    """), encoding="utf-8")

    cfg = load_config(str(toml))
    assert cfg.retrieval.blend_mode == "scale_relative"


def test_unknown_blend_mode_raises_in_pure_function():
    genome = _FakeGenome({"a": 1.0})
    with pytest.raises(ValueError, match="blend_mode"):
        apply_candidate_refiners(
            "q", [_FakeGene("a"), _FakeGene("b")], 32,
            genome=genome, blend_mode="bogus",
        )


def test_unknown_blend_mode_raises_at_manager_construction(tmp_path):
    """The manager validates at __init__ (fail-fast, mirrors the store's
    rerank_combinator guard) — a bad blend_mode never reaches a query."""
    from helix_context.config import load_config as _load
    from helix_context.context_manager import HelixContextManager

    toml = tmp_path / "helix.toml"
    toml.write_text(textwrap.dedent(f"""
        [genome]
        path = "{(tmp_path / 'g.db').as_posix()}"
        [retrieval]
        blend_mode = "nope"
        [ribosome]
        backend = "none"
    """), encoding="utf-8")
    cfg = _load(str(toml))
    with pytest.raises(ValueError, match="blend_mode"):
        HelixContextManager(cfg)
