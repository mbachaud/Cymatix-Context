"""Issue #260: rank/confidence-gated RRF — default-inert knob + byte-identity.

At true corpus scale (829K ERB blob) unconditional RRF let the dense arm's
near-random deep-rank signal (median gold rank 50,357 in a ~178K pool) demote a
gold that lexical ranked well: fused gold_delivered_id-given-pooled 0.156 (10/64)
INVERTED *below* plain lexical 0.333 (21/63) — the first sign flip in the
three-scale curve (``docs/research/2026-07-11-overnight-bench-results.md`` P7',
distilled from #93). The fix: an arm's RRF contribution counts only where that
arm's own evidence is trustworthy — gate by per-arm rank position
(``rrf_gate_top_m``, scale-free) and/or raw score floor (``rrf_gate_min_score``).

Council rule: scoring changes ship DEFAULT-INERT (byte-identical); a bench
receipt gates any default flip. So the headline guarantee tested here is that
with the gate disabled the fused scores are bit-for-bit what they are today.

Test families:
  1. Fuser gate — sentinel byte-identity, top_m prefix, min_score floor,
     composition, sentinel semantics, and the 829K inversion repro + fix.
  2. Store integration — default-inert, master-switch inertness, resolved
     gate, full-query byte-identity, validation.
  3. Config threading — RetrievalConfig defaults, load_config(TOML),
     open_read_source (solo Genome) AND the sharded router fan-out.

Reused harness: tests/test_retrieval_invariance.py (via test_rerank_combinators).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from helix_context.config import RetrievalConfig, load_config
from helix_context.genome import Genome
from helix_context.knowledge_store import KnowledgeStore
from helix_context.retrieval.fusion import DEFAULT_RRF_K, Fuser
from helix_context.shard_router import ShardRouter
from helix_context.shard_schema import (
    init_main_db,
    open_main_db,
    register_shard,
    upsert_fingerprint,
)

# Shared corpus/query harness (single source of truth for the fusion fixtures).
from tests.test_retrieval_invariance import (
    _content,
    _doc,
    _invariance_corpus,
    _new_store,
    _run,
)

_K = DEFAULT_RRF_K


def _fuse(tiers, **gate_kwargs):
    """Build a Fuser over ``tiers`` = [(name, ranked_ids, weight), ...] and
    return its full fused-score map. ``gate_kwargs`` -> Fuser(gate_top_m=...)."""
    f = Fuser(k=_K, **gate_kwargs)
    for name, ranked, weight in tiers:
        f.add_tier(name, ranked, weight)
    return f.all_scores()


# ══ 1. Fuser gate ═════════════════════════════════════════════════════


def test_gate_default_fields_are_ungated_sentinels():
    f = Fuser(k=_K)
    assert f.gate_top_m == 0
    assert f.gate_min_score == 0.0


def test_gate_sentinels_byte_identical_to_pregate():
    """Explicit (0, 0.0) must reproduce the ungated accumulation bit-for-bit
    across a multi-tier feed (one shallow lexical arm + one deep dense arm)."""
    tiers = [
        ("fts5", [("g%02d" % i, 10.0 - i) for i in range(8)], 3.0),
        ("dense", [("g%02d" % i, 1.0 - 0.01 * i) for i in range(40)], 1.0),
        ("tag_exact", [("g00", 3.0), ("g05", 3.0)], 3.0),
    ]
    base = _fuse(tiers)
    gated_off = _fuse(tiers, gate_top_m=0, gate_min_score=0.0)
    assert gated_off == base  # dict equality of floats == bitwise identity here


def test_gate_top_m_keeps_only_top_m_ranks():
    arm = [("d%02d" % i, 100.0 - i) for i in range(20)]  # d00 best .. d19 worst
    gs = _fuse([("dense", arm, 1.0)], gate_top_m=5)
    kept = set(gs)
    assert kept == {"d00", "d01", "d02", "d03", "d04"}          # ranks 1..5
    assert gs["d00"] == pytest.approx(1.0 / (_K + 1))            # rank-1 contribution intact
    assert gs["d04"] == pytest.approx(1.0 / (_K + 5))            # rank-5 contribution intact


def test_gate_top_m_larger_than_arm_is_noop():
    """A tier shallower than M is untouched — byte-identical to ungated."""
    arm = [("a", 3.0), ("b", 2.0), ("c", 1.0)]
    assert _fuse([("t", arm, 1.0)], gate_top_m=99) == _fuse([("t", arm, 1.0)])


def test_gate_min_score_keeps_only_at_or_above_floor():
    arm = [("a", 0.9), ("b", 0.7), ("c", 0.5), ("d", 0.3)]
    gs = _fuse([("dense", arm, 1.0)], gate_min_score=0.6)
    assert set(gs) == {"a", "b"}                    # 0.5 / 0.3 gated out
    assert gs["a"] == pytest.approx(1.0 / (_K + 1))
    assert gs["b"] == pytest.approx(1.0 / (_K + 2))


def test_gate_min_score_zero_is_off_sentinel_not_a_floor():
    """0.0 is the 'ungated' sentinel — it must NOT drop zero/negative scores."""
    arm = [("a", 0.5), ("b", 0.0), ("c", -0.3)]
    gs = _fuse([("t", arm, 1.0)], gate_min_score=0.0)
    assert set(gs) == {"a", "b", "c"}


def test_gate_negative_min_score_is_active():
    """A negative floor is a legitimate (truthy) threshold for negated-bm25
    arms — it gates entries strictly below it."""
    arm = [("a", 0.2), ("b", -0.3), ("c", -0.8)]
    gs = _fuse([("t", arm, 1.0)], gate_min_score=-0.5)
    assert set(gs) == {"a", "b"}                    # -0.8 < -0.5 gated out


def test_gate_top_m_and_min_score_compose_to_the_tighter_prefix():
    arm = [("s%d" % i, float(6 - i)) for i in range(6)]  # scores 6,5,4,3,2,1
    # top_m=4 alone would keep ranks 1..4 (scores 6,5,4,3); min_score=3.5 drops
    # the score-3 entry (rank 4) -> the tighter cut (3 kept) wins.
    gs = _fuse([("t", arm, 1.0)], gate_top_m=4, gate_min_score=3.5)
    assert set(gs) == {"s0", "s1", "s2"}            # scores 6,5,4
    assert "s3" not in gs                           # within top_m but below floor


def test_829k_dense_noise_inversion_repro_and_top_m_fix():
    """Reproduce the P7' 829K failure shape and show the gate fixes it.

    Lexical ranks the gold #1 and a distractor #5. The dense arm is noise at
    scale: the gold is buried deep (~rank 502) while the distractor lands at a
    modestly-better dense rank (~101), so under UNCONDITIONAL RRF dense's
    deep-rank signal boosts the distractor *over* the gold. Gating dense to its
    trustworthy top ranks removes both near-random contributions, so the fused
    order falls back to lexical and the gold is restored to #1.
    """
    gold, distractor = "GOLD", "DISTRACT"
    lex = [(gold, 5.0), ("a", 4.0), ("b", 3.0), ("c", 2.0), (distractor, 1.0)]
    # 500 descending-cosine noise docs (x000=1.0 .. x499≈0.501); distractor at
    # cosine 0.9 sorts to ~rank 101, gold at 0.2 sorts to the deep tail (~502).
    dense = [("x%03d" % i, 1.0 - 0.001 * i) for i in range(500)]
    dense.append((distractor, 0.9))
    dense.append((gold, 0.2))
    tiers = [("fts5", lex, 3.0), ("dense", dense, 1.0)]

    ungated = _fuse(tiers)
    assert ungated[distractor] > ungated[gold]      # INVERSION: non-gold wins

    gated = _fuse(tiers, gate_top_m=10)
    assert gated[gold] > gated[distractor]          # gold restored to the top
    # Mechanism: with dense gated to its top-10, neither gold (~rank 502) nor
    # distractor (~rank 101) receives a dense contribution, so each falls back
    # to its pure lexical RRF mass.
    assert gated[gold] == pytest.approx(3.0 / (_K + 1))
    assert gated[distractor] == pytest.approx(3.0 / (_K + 5))


# ══ 2. Store integration ══════════════════════════════════════════════


def test_store_default_gate_is_inert():
    store = KnowledgeStore(path=":memory:")
    try:
        assert store._rrf_gate_enabled is False
        assert store._rrf_gate_params() == (0, 0.0)
    finally:
        store.close()


def test_store_disabled_switch_forces_inert_even_with_thresholds_set():
    """The master switch dominates: enabled=False pins the ungated sentinels no
    matter what the two thresholds are, which is what guarantees inertness."""
    store = KnowledgeStore(
        path=":memory:",
        rrf_gate_enabled=False,
        rrf_gate_top_m=3,
        rrf_gate_min_score=0.9,
    )
    try:
        assert store._rrf_gate_params() == (0, 0.0)
    finally:
        store.close()


def test_store_enabled_resolves_configured_gate():
    store = KnowledgeStore(
        path=":memory:",
        rrf_gate_enabled=True,
        rrf_gate_top_m=5,
        rrf_gate_min_score=0.2,
    )
    try:
        assert store._rrf_gate_params() == (5, 0.2)
    finally:
        store.close()


def test_store_query_byte_identical_when_gate_disabled():
    """Full query path: a disabled gate (even with an aggressive top_m set)
    yields the exact same fused score map as a fully-default store."""
    g_default = _new_store(_invariance_corpus(), fusion_mode="rrf")
    g_disabled = _new_store(
        _invariance_corpus(),
        fusion_mode="rrf",
        rrf_gate_enabled=False,
        rrf_gate_top_m=1,
        rrf_gate_min_score=0.5,
    )
    try:
        _, scores_default, _ = _run(g_default)
        _, scores_disabled, _ = _run(g_disabled)
        assert scores_disabled == scores_default
    finally:
        g_default.close()
        g_disabled.close()


def test_store_query_enabled_gate_is_load_bearing():
    """Guards the byte-identity test against being vacuous: an ENABLED
    aggressive gate must actually change the fused scores on the same corpus."""
    g_default = _new_store(_invariance_corpus(), fusion_mode="rrf")
    g_gated = _new_store(
        _invariance_corpus(),
        fusion_mode="rrf",
        rrf_gate_enabled=True,
        rrf_gate_top_m=1,
    )
    try:
        _, scores_default, _ = _run(g_default)
        _, scores_gated, _ = _run(g_gated)
        assert scores_gated != scores_default
    finally:
        g_default.close()
        g_gated.close()


def test_store_construct_negative_top_m_raises():
    with pytest.raises(ValueError):
        KnowledgeStore(path=":memory:", rrf_gate_top_m=-1)


def test_gate_composes_with_278_per_query_combinator_override():
    """Composition with #278 (classifier-gated combinator): the gate acts at
    the FUSION stage (which entries feed the Fuser); #278's per-query
    ``rerank_combinator`` override acts at the POST-fusion stage. They must
    coexist without interference — the override runs cleanly under an enabled
    gate, and the gate remains load-bearing on the fused scores it produces.
    """
    kw = dict(domains=["alpha"], entities=[], max_genes=12, read_only=True,
              rerank_combinator="off")  # #278 per-query override -> pure fused
    g_gated = _new_store(
        _invariance_corpus(), fusion_mode="rrf",
        rrf_gate_enabled=True, rrf_gate_top_m=1,
    )
    g_plain = _new_store(_invariance_corpus(), fusion_mode="rrf")
    try:
        gated = g_gated.query_genes(**kw)          # runs without error
        plain = g_plain.query_genes(**kw)
        assert gated and plain                      # both return a ranking
        # Under override="off" the published scores ARE the fused scores, so a
        # difference proves the gate still bit at the fusion stage beneath the
        # per-query override — the two stages compose, no interference.
        assert dict(g_gated.last_fused_scores) != dict(g_plain.last_fused_scores)
    finally:
        g_gated.close()
        g_plain.close()


# ══ 3. Config threading ═══════════════════════════════════════════════


def test_retrieval_config_defaults_are_inert():
    rc = RetrievalConfig()
    assert rc.rrf_gate_enabled is False
    assert rc.rrf_gate_top_m == 0
    assert rc.rrf_gate_min_score == 0.0


def test_load_config_parses_gate_knobs():
    toml = (
        "[retrieval]\n"
        "rrf_gate_enabled = true\n"
        "rrf_gate_top_m = 25\n"
        "rrf_gate_min_score = 0.3\n"
    )
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "helix.toml"
        p.write_text(toml, encoding="utf-8")
        cfg = load_config(str(p))
    assert cfg.retrieval.rrf_gate_enabled is True
    assert cfg.retrieval.rrf_gate_top_m == 25
    assert cfg.retrieval.rrf_gate_min_score == 0.3


def test_load_config_absent_keys_keep_inert_defaults():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "helix.toml"
        p.write_text('[retrieval]\nfusion_mode = "rrf"\n', encoding="utf-8")
        cfg = load_config(str(p))
    assert cfg.retrieval.rrf_gate_enabled is False
    assert cfg.retrieval.rrf_gate_top_m == 0
    assert cfg.retrieval.rrf_gate_min_score == 0.0


def test_config_threads_through_open_read_source_solo():
    """TOML -> load_config -> open_read_source (solo Genome on a :memory: path)
    -> store attrs, the same seam production boots through."""
    toml = (
        "[retrieval]\n"
        "rrf_gate_enabled = true\n"
        "rrf_gate_top_m = 12\n"
        "rrf_gate_min_score = 0.15\n"
    )
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "helix.toml"
        p.write_text(toml, encoding="utf-8")
        cfg = load_config(str(p))

    from helix_context.sharding import open_read_source

    store = open_read_source(
        genome_path=":memory:",
        rrf_gate_enabled=cfg.retrieval.rrf_gate_enabled,
        rrf_gate_top_m=cfg.retrieval.rrf_gate_top_m,
        rrf_gate_min_score=cfg.retrieval.rrf_gate_min_score,
    )
    try:
        assert store._rrf_gate_params() == (12, 0.15)
    finally:
        store.close()


# ── sharded fan-out: the router forwards the gate to each per-shard store ──


@pytest.fixture
def one_shard_main():
    """main.db with a single registered shard holding one doc — enough for the
    router to lazily open a real per-shard KnowledgeStore."""
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    main_path = str(root / "main.db")
    shard_path = str(root / "shard_a.db")

    g = Genome(shard_path)
    gid = g.upsert_gene(
        _doc("shdoc", _content(3), ["w1", "w2", "w3", "alpha"]),
        apply_gate=False,
    )
    g.conn.commit()
    g.conn.close()
    if getattr(g, "_reader", None):
        g._reader.close()

    main = open_main_db(main_path)
    init_main_db(main)
    register_shard(main, "shard_a", "reference", shard_path, gene_count=1)
    upsert_fingerprint(
        main, gene_id=gid, shard_name="shard_a", source_id=None,
        domains_json=json.dumps(["w1", "w2", "w3", "alpha"]),
        entities_json="[]", key_values_json="[]",
    )
    main.close()

    yield main_path
    td.cleanup()


def test_gate_threads_to_per_shard_store(one_shard_main):
    """The sharded adapter fans the gate to every per-shard KnowledgeStore."""
    r = ShardRouter(
        one_shard_main,
        rrf_gate_enabled=True,
        rrf_gate_top_m=7,
        rrf_gate_min_score=0.25,
    )
    try:
        shard_store = r._open_shard("shard_a")
        assert shard_store._rrf_gate_params() == (7, 0.25)
    finally:
        r.close()


def test_default_router_shard_is_inert(one_shard_main):
    r = ShardRouter(one_shard_main)
    try:
        shard_store = r._open_shard("shard_a")
        assert shard_store._rrf_gate_params() == (0, 0.0)
    finally:
        r.close()
