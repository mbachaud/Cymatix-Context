"""Issue #255 (PR-2): post-fusion rerank combinator knob.

The RRF finalization used to be a single inline ``fused + rerank_additive``
add (DEFECT-1: an authority bonus of +2.0 is ~40x the fused RRF budget, so
the bonus becomes the ranking — ``docs/research/2026-07-08-scoring-invariance-audit.md``
§3). PR-2 factors the combination into a pure module
(``cymatix_context/retrieval/rerank_combinators.py``) behind a knob whose default
(``"additive"``) is BYTE-IDENTICAL to the shipped block, plus three bench-gated
alternatives (``fused_tier`` / ``eps_band`` / ``off``).

Test families:
  1. additive byte-identity (pure golden + store integration — the DEFECT-1
     inversion still fires under the default knob),
  2. fused_tier commensurability (== a real ``Fuser.add_tier`` run),
  3. eps_band reorders-only-within-band + determinism + degenerate cases,
  4. off ignores rerank entirely,
  5. ValueError on an unknown combinator (construction + module),
  6. config threading (TOML -> load_config -> open_read_source -> store attrs),
  7. debug hooks (``last_fused_scores`` / ``last_rerank_additive`` populated,
     ``final == fused + rerank`` per-gene under the additive combinator).

Every store constructed here passes ``fusion_mode=`` EXPLICITLY — a parallel
PR (#256) is flipping the ctor default, so relying on it would be fragile.

Design record: docs/research/2026-07-09-scoring-combinator-exploration.md.
"""
from __future__ import annotations

import inspect
import itertools
import textwrap

import pytest

from cymatix_context.config import RetrievalConfig, load_config
from cymatix_context.knowledge_store import KnowledgeStore
from cymatix_context.retrieval.fusion import DEFAULT_RRF_K, Fuser
from cymatix_context.retrieval.rerank_combinators import (
    VALID_COMBINATORS,
    combine_rerank,
)

# Store-level fixtures reuse the invariance-audit harness (which itself reuses
# the additive-weight-plumbing harness). Importing the private helpers keeps a
# single source of truth for the rerank-firing corpora.
from tests.test_retrieval_invariance import (
    _content,
    _doc,
    _fillers,
    _invariance_corpus,
    _new_store,
    _run,
)


# ── shared pure-map fixtures ──────────────────────────────────────────
_K = DEFAULT_RRF_K
_KW = dict(k=_K, tier_weight=1.0, delta=0.05, limit=12)


def _defect1_corpus():
    """Doc A wins every fused tier; doc B only carries the +2.0 source
    authority bonus. Mirrors test_defect1_authority_bonus_dominates_rrf_ordering.
    """
    return [
        _doc("auth_a", _content(3), ["wq1", "wq2", "wq3", "alpha"]),
        _doc("auth_b", _content(1), ["wr1", "wr2", "wr3", "alpha"],
             source_id="notes/alpha_setup.md"),
    ] + _fillers(4, start=200)


# ═══ 1. additive — byte-identical to the old inline formula ═══════════


def test_additive_matches_old_inline_formula_exactly():
    """The additive combinator reproduces the pre-PR-2 block:
        final[g] = fused.get(g, 0.0) + rerank.get(g, 0.0)   (over eligible)
        ranked   = sorted(final, key=(-final[g], g))[:limit]
    """
    fused = {"a": 0.10, "b": 0.05, "c": 0.20, "d": 0.0}
    rerank = {"a": 2.0, "c": 0.5}  # authority-scale bonuses (DEFECT-1)

    expected_final = {g: fused.get(g, 0.0) + rerank.get(g, 0.0) for g in fused}
    expected_ranked = sorted(
        expected_final, key=lambda g: (-expected_final[g], g)
    )[:12]

    final, ranked = combine_rerank("additive", fused, rerank, {}, **_KW)

    assert final == expected_final          # bitwise dict equality
    assert ranked == expected_ranked


def test_additive_respects_limit():
    fused = {chr(ord("a") + i): 0.1 * (10 - i) for i in range(10)}
    _final, ranked = combine_rerank(
        "additive", fused, {}, {}, k=_K, tier_weight=1.0, delta=0.05, limit=3
    )
    assert ranked == ["a", "b", "c"]


def test_additive_default_knob_still_inverts_like_master():
    """Store integration: under fusion_mode='rrf' with the DEFAULT knob the
    DEFECT-1 inversion is preserved bit-for-bit — B (worse fused, +2.0
    authority) still outranks A. This is exactly what
    test_defect1_authority_bonus_dominates_rrf_ordering pins; re-running it
    here proves the refactor is byte-identical on the default path.
    """
    g = _new_store(_defect1_corpus(), fusion_mode="rrf")  # default = "additive"
    try:
        ranked, scores, contrib = _run(g)
    finally:
        g.close()

    assert contrib["auth_b"].get("authority") == 2.0
    assert "authority" not in contrib.get("auth_a", {})
    # The inversion still happens: B outranks A despite strictly worse fused.
    assert ranked.index("auth_b") < ranked.index("auth_a")
    assert scores["auth_b"] > 10 * scores["auth_a"]


# ═══ 2. fused_tier — rank contribution, commensurate by construction ══


def test_fused_tier_contribution_equals_real_fuser_run():
    """Each rerank class's contribution equals a genuine Fuser.add_tier over
    the same (gid, boost) pairs — no exchange rate, rank absorbs the scale.
    """
    fused = {"a": 0.10, "b": 0.20, "c": 0.05}
    rerank_by_class = {
        "authority": {"a": 2.0, "b": 0.5},
        "party_attr": {"c": 0.5},
    }
    # The flat `rerank` map is IGNORED by fused_tier — pass a decoy to prove it.
    decoy_flat = {"a": 999.0, "b": 999.0, "c": 999.0}

    final, _ranked = combine_rerank(
        "fused_tier", fused, decoy_flat, rerank_by_class, **_KW
    )

    expected = dict(fused)
    for cls, boosts in rerank_by_class.items():
        f = Fuser(k=_K)
        f.add_tier(cls, [(g, v) for g, v in boosts.items() if v > 0.0], weight=1.0)
        for g, contrib in f.all_scores().items():
            expected[g] = expected.get(g, 0.0) + contrib

    assert final == expected
    # decoy flat rerank never leaked in:
    assert final["a"] < 1.0


def test_fused_tier_max_contribution_is_tier_weight_over_k_plus_one():
    fused = {"a": 0.10, "b": 0.20}
    tw = 3.0
    # a has the larger authority boost -> rank 1 -> contribution tw/(k+1).
    rbc = {"authority": {"a": 2.0, "b": 0.5}}
    final, _ = combine_rerank(
        "fused_tier", fused, {}, rbc, k=_K, tier_weight=tw, delta=0.05, limit=12
    )
    contrib_a = final["a"] - fused["a"]
    assert contrib_a == pytest.approx(tw / (_K + 1))
    # b is rank 2 -> strictly smaller contribution.
    contrib_b = final["b"] - fused["b"]
    assert contrib_b == pytest.approx(tw / (_K + 2))
    assert contrib_a > contrib_b


def test_fused_tier_ignores_non_positive_boosts():
    fused = {"a": 0.10, "b": 0.20}
    rbc = {"authority": {"a": 0.0, "b": -1.0}}  # neither is a positive boost
    final, _ = combine_rerank("fused_tier", fused, {}, rbc, **_KW)
    assert final == fused  # no class member ranked -> pure fused


# ═══ 3. eps_band — relative tie band; reorders only within a band ═════


def test_eps_band_reorders_within_band_only():
    # a (1.00) leads; band threshold = 1.00*(1-0.05)=0.95 -> b (0.97) is in
    # band, c (0.50) is not. b carries a rerank bonus so it jumps a in-band;
    # c can never cross the band.
    fused = {"a": 1.00, "b": 0.97, "c": 0.50}
    rerank = {"b": 5.0}
    final, ranked = combine_rerank("eps_band", fused, rerank, {}, **_KW)
    assert final == fused                     # scores stay pure fused
    assert ranked == ["b", "a", "c"]


def test_eps_band_outside_band_doc_never_crosses():
    # mid (0.90) falls just below hi's band (thr 0.95); lo (0.10) is far
    # below. A huge rerank on lo must NOT lift it across the bands.
    fused = {"hi": 1.00, "mid": 0.90, "lo": 0.10}
    rerank = {"lo": 100.0}
    _final, ranked = combine_rerank("eps_band", fused, rerank, {}, **_KW)
    assert ranked == ["hi", "mid", "lo"]


def test_eps_band_deterministic_under_input_permutation():
    base = {"a": 1.00, "b": 0.98, "c": 0.97, "d": 0.50}
    rerank = {"b": 1.0, "c": 2.0}
    results = set()
    for perm in itertools.permutations(base.items()):
        fused = dict(perm)
        _final, ranked = combine_rerank("eps_band", fused, rerank, {}, **_KW)
        results.add(tuple(ranked))
    # a,b,c share the top band (all >= 0.95); within it order by rerank desc:
    # c(2) > b(1) > a(0); d is its own band.
    assert results == {("c", "b", "a", "d")}


def test_eps_band_all_zero_fused_is_degenerate_singletons():
    fused = {"a": 0.0, "b": 0.0, "c": 0.0}
    rerank = {"a": 5.0}  # ignored: non-positive leaders form singleton bands
    final, ranked = combine_rerank("eps_band", fused, rerank, {}, **_KW)
    assert final == fused
    assert ranked == ["a", "b", "c"]          # pure fused (gene_id asc on ties)


def test_eps_band_delta_zero_is_pure_fused_order():
    fused = {"a": 0.30, "b": 0.20, "c": 0.10}
    rerank = {"c": 99.0}  # would reorder if any band formed
    final, ranked = combine_rerank(
        "eps_band", fused, rerank, {}, k=_K, tier_weight=1.0, delta=0.0, limit=12
    )
    assert final == fused
    assert ranked == ["a", "b", "c"]          # δ=0 + distinct scores -> singletons


# ═══ 4. off — pure fused, rerank ignored ══════════════════════════════


def test_off_ignores_rerank_entirely():
    fused = {"a": 0.10, "b": 0.20, "c": 0.05}
    rerank = {"a": 100.0}
    rbc = {"authority": {"a": 100.0}}
    final, ranked = combine_rerank("off", fused, rerank, rbc, **_KW)
    assert final == fused
    assert ranked == ["b", "a", "c"]          # pure fused order


# ═══ 5. validation ════════════════════════════════════════════════════


def test_unknown_combinator_raises_at_construction():
    with pytest.raises(ValueError, match="rerank_combinator"):
        KnowledgeStore(
            path=":memory:", fusion_mode="rrf", rerank_combinator="bogus"
        )


def test_valid_combinators_all_construct():
    for name in VALID_COMBINATORS:
        ks = KnowledgeStore(
            path=":memory:", fusion_mode="rrf", rerank_combinator=name
        )
        try:
            assert ks._rerank_combinator == name
        finally:
            ks.close()


def test_combine_rerank_module_raises_on_unknown():
    with pytest.raises(ValueError):
        combine_rerank("nope", {}, {}, {}, **_KW)


# ═══ 6. config threading ══════════════════════════════════════════════


def test_config_defaults_are_additive_byte_identical():
    rc = RetrievalConfig()
    assert rc.rerank_combinator == "additive"
    assert rc.rerank_band_delta == 0.05
    assert rc.rerank_tier_weight == 1.0
    # Ctor default agrees with the config default (single-source guard).
    sig = inspect.signature(KnowledgeStore.__init__)
    assert sig.parameters["rerank_combinator"].default == "additive"
    assert sig.parameters["rerank_band_delta"].default == 0.05
    assert sig.parameters["rerank_tier_weight"].default == 1.0


def test_config_threads_toml_to_store_attrs(tmp_path):
    toml = tmp_path / "helix.toml"
    toml.write_text(textwrap.dedent("""
        [retrieval]
        fusion_mode = "rrf"
        rerank_combinator = "eps_band"
        rerank_band_delta = 0.02
        rerank_tier_weight = 2.5
    """), encoding="utf-8")

    cfg = load_config(str(toml))
    assert cfg.retrieval.rerank_combinator == "eps_band"
    assert cfg.retrieval.rerank_band_delta == 0.02
    assert cfg.retrieval.rerank_tier_weight == 2.5

    # Thread through the same seam production uses (open_read_source fans to
    # shards; a :memory: path resolves to a solo Genome).
    from cymatix_context.sharding import open_read_source

    store = open_read_source(
        genome_path=":memory:",
        fusion_mode=cfg.retrieval.fusion_mode,
        rerank_combinator=cfg.retrieval.rerank_combinator,
        rerank_band_delta=cfg.retrieval.rerank_band_delta,
        rerank_tier_weight=cfg.retrieval.rerank_tier_weight,
    )
    try:
        assert store._rerank_combinator == "eps_band"
        assert store._rerank_band_delta == 0.02
        assert store._rerank_tier_weight == 2.5
    finally:
        store.close()


# ═══ 7. debug hooks ═══════════════════════════════════════════════════


def test_debug_hooks_populated_and_final_equals_fused_plus_rerank():
    """Under fusion_mode='rrf' + the additive combinator, the debug hooks
    expose the eligible-restricted pre-combination maps and satisfy
    ``final == fused + rerank`` per gene.
    """
    g = _new_store(_defect1_corpus(), fusion_mode="rrf")
    try:
        g.query_genes(
            domains=["alpha"], entities=[], max_genes=12, read_only=True
        )
        fused = dict(g.last_fused_scores)
        rr = dict(g.last_rerank_additive)
        final = dict(g.last_query_scores)
    finally:
        g.close()

    assert fused and final                    # populated, not empty
    # authority (+2.0) was mirrored into the flat rerank map for auth_b.
    assert rr.get("auth_b", 0.0) == 2.0
    # All three maps are keyed on the same eligible id set.
    assert set(fused) == set(final) == set(rr)
    for gid in final:
        assert final[gid] == fused.get(gid, 0.0) + rr.get(gid, 0.0)


def test_debug_hooks_empty_under_additive_fusion_mode():
    """The additive-fusion branch never populates the RRF debug hooks, so a
    directly-constructed additive store leaves them at their init value.
    """
    g = _new_store(_invariance_corpus(), fusion_mode="additive")
    try:
        g.query_genes(
            domains=["alpha"], entities=[], max_genes=12, read_only=True
        )
        assert g.last_fused_scores == {}
        assert g.last_rerank_additive == {}
    finally:
        g.close()
