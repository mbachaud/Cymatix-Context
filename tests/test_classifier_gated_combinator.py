"""Issue #255: classifier-gated rerank combinator — per-class map.

The desk test (docs/research/2026-07-10-rerank-combinator-desktest.md) plus the
2026-07-11 semantic-arm re-run found the winning combinator is
CORPUS-DEPENDENT: the rerank additives are load-bearing on literal beds
(xl gd_id 0.62; eps_band costs −0.10), while eps_band/off win the semantic 10k
ERB bed (gd_id 0.384 vs 0.376, median gold rank 10→6). So there is NO global
default flip — the supported design is a *per-query-class* combinator map,
selected by the stage-0 rule-based query classifier. It shipped INERT (empty
map == the global-combinator path, byte-for-byte) and GRADUATED 2026-07-16 on
the knob-graduation receipt (PR #293,
docs/research/2026-07-16-knob-graduation-receipts.md /
issues/255#issuecomment-5005983077): the proposed map
``{multi_hop: eps_band, default: eps_band}`` replicated flat delivery
(gold_delivered byte-identical) with the median gold rank halved on both
semantic beds (10k 10→5, 50k 12→6), lift confined to the mapped classes, and
byte-identical unmapped (xl literal) rows — so it is now the shipped default.
An explicit empty map (``{}``) in TOML still restores the pre-graduation
byte-identical global-combinator behavior.

Test families:
  1. config default: the shipped default routes multi_hop/default -> eps_band
     (the graduation); an EXPLICIT empty map is still byte-identical to the
     global-combinator path (the pre-graduation escape hatch),
  2. resolver: fake classifier class -> combinator (+ not-in-map / empty /
     disabled-classifier fallbacks all resolve to "use the global"),
  3. load-time validation: unknown class key OR unknown combinator value is a
     hard config error at load (direct construct + TOML), valid maps construct,
  4. store-level per-query override threading (the override reaches
     combine_rerank; None is byte-identical to the global),
  5. sharded threading (the override rides the router fan-out to each shard's
     own query_docs),
  6. end-to-end wiring through HelixContextManager.build_context (the classifier
     class actually selects the store's per-query combinator; disabled
     classifier and not-in-map classes fall back to the global; the shipped
     default map routes multi_hop/default queries to eps_band).

Every store constructed here passes ``fusion_mode="rrf"`` EXPLICITLY — the
combinator only runs on the RRF finalization path.

Design record: docs/research/2026-07-09-scoring-combinator-exploration.md +
the #255 desk-test / semantic-arm verdicts + the 2026-07-16 graduation receipt.
"""
from __future__ import annotations

import json
import tempfile
import textwrap
from pathlib import Path

import pytest

from helix_context.config import (
    BudgetConfig,
    ClassifierConfig,
    GenomeConfig,
    HelixConfig,
    RetrievalConfig,
    RibosomeConfig,
    load_config,
)
from helix_context.context_manager import HelixContextManager
from helix_context.knowledge_store import KnowledgeStore
from helix_context.retrieval.query_classifier import (
    VALID_QUERY_CLASSES,
    classify_query,
)
from helix_context.retrieval.rerank_combinators import (
    VALID_COMBINATORS,
    resolve_class_combinator,
)
from helix_context.shard_router import ShardRouter
from helix_context.shard_schema import (
    init_main_db,
    open_main_db,
    register_shard,
    upsert_fingerprint,
)
from helix_context.sharding import ShardedGenomeAdapter

# Reuse the single-source rerank-firing corpora + store harness.
from tests.test_rerank_combinators import _defect1_corpus
from tests.test_retrieval_invariance import _new_store


# ═══ 1. config default ═════════════════════════════════════════════════


def test_shipped_default_routes_multi_hop_and_default_to_eps_band():
    """GRADUATED 2026-07-16 (PR #293 receipt, issues/255#issuecomment-5005983077):
    the shipped default now maps multi_hop and default -> eps_band; the other
    three classes (arithmetic, factual, procedural) are still unmapped and
    fall back to the global combinator."""
    rc = RetrievalConfig()
    assert rc.rerank_combinator_by_class == {
        "multi_hop": "eps_band", "default": "eps_band",
    }
    assert resolve_class_combinator(rc.rerank_combinator_by_class, "multi_hop") == "eps_band"
    assert resolve_class_combinator(rc.rerank_combinator_by_class, "default") == "eps_band"
    for cls in ("arithmetic", "factual", "procedural"):
        assert resolve_class_combinator(rc.rerank_combinator_by_class, cls) is None


def test_explicit_empty_map_is_still_byte_identical_and_inert():
    """The pre-graduation escape hatch: an EXPLICIT empty map (e.g. ``{}`` in
    TOML) still disables per-class routing entirely, for every class."""
    rc = RetrievalConfig(rerank_combinator_by_class={})
    assert rc.rerank_combinator_by_class == {}
    # Empty map + any class => None => the store keeps its global combinator.
    for cls in VALID_QUERY_CLASSES:
        assert resolve_class_combinator(rc.rerank_combinator_by_class, cls) is None


# ═══ 2. resolver — fake classifier class -> combinator ════════════════


def test_resolver_selects_per_class_combinator():
    mapping = {"factual": "off", "procedural": "eps_band"}
    assert resolve_class_combinator(mapping, "factual") == "off"
    assert resolve_class_combinator(mapping, "procedural") == "eps_band"


def test_resolver_class_not_in_map_falls_back_to_global():
    mapping = {"factual": "off"}
    # arithmetic isn't mapped -> None -> store uses its global combinator.
    assert resolve_class_combinator(mapping, "arithmetic") is None


def test_resolver_empty_map_is_always_global():
    assert resolve_class_combinator({}, "factual") is None


def test_resolver_none_class_is_global():
    # classifier disabled (or no class assigned) => None cls => global.
    assert resolve_class_combinator({"factual": "off"}, None) is None


def test_resolver_composes_with_the_real_classifier():
    """A real classifier output feeds the resolver: a wh-question classes
    ``factual`` and the map routes it to ``off``."""
    result = classify_query("what is the auth middleware path")
    assert result.cls == "factual"  # guards the fixture assumption
    assert resolve_class_combinator({"factual": "off"}, result.cls) == "off"


# ═══ 3. load-time validation (fail loud at load) ══════════════════════


def test_unknown_combinator_value_is_config_error_at_construction():
    with pytest.raises(ValueError, match="combinator"):
        RetrievalConfig(rerank_combinator_by_class={"factual": "bogus"})


def test_unknown_class_key_is_config_error_at_construction():
    with pytest.raises(ValueError, match="query class"):
        RetrievalConfig(rerank_combinator_by_class={"not_a_class": "off"})


def test_valid_map_constructs():
    rc = RetrievalConfig(
        rerank_combinator_by_class={"factual": "off", "procedural": "eps_band"}
    )
    assert rc.rerank_combinator_by_class == {"factual": "off", "procedural": "eps_band"}


def test_all_valid_class_x_combinator_pairs_construct():
    full = {cls: VALID_COMBINATORS[i % len(VALID_COMBINATORS)]
            for i, cls in enumerate(VALID_QUERY_CLASSES)}
    rc = RetrievalConfig(rerank_combinator_by_class=full)
    assert rc.rerank_combinator_by_class == full


def test_toml_load_threads_and_validates_map(tmp_path):
    toml = tmp_path / "helix.toml"
    toml.write_text(textwrap.dedent("""
        [retrieval]
        fusion_mode = "rrf"

        [retrieval.rerank_combinator_by_class]
        factual = "off"
        procedural = "eps_band"
    """), encoding="utf-8")
    cfg = load_config(str(toml))
    assert cfg.retrieval.rerank_combinator_by_class == {
        "factual": "off", "procedural": "eps_band",
    }


def test_toml_load_rejects_unknown_combinator(tmp_path):
    toml = tmp_path / "helix.toml"
    toml.write_text(textwrap.dedent("""
        [retrieval]
        fusion_mode = "rrf"

        [retrieval.rerank_combinator_by_class]
        factual = "bogus"
    """), encoding="utf-8")
    with pytest.raises(ValueError, match="combinator"):
        load_config(str(toml))


def test_toml_load_rejects_unknown_class(tmp_path):
    toml = tmp_path / "helix.toml"
    toml.write_text(textwrap.dedent("""
        [retrieval]
        fusion_mode = "rrf"

        [retrieval.rerank_combinator_by_class]
        nonsense = "off"
    """), encoding="utf-8")
    with pytest.raises(ValueError, match="query class"):
        load_config(str(toml))


# ═══ 4. store-level per-query override threading ══════════════════════


def _order(genes):
    return [g.gene_id for g in genes]


def test_default_no_override_still_inverts_like_global():
    """No override (None) is byte-identical to the store's global combinator:
    the DEFECT-1 authority inversion still fires (auth_b outranks auth_a)."""
    g = _new_store(_defect1_corpus(), fusion_mode="rrf")  # global == "additive"
    try:
        ids = _order(g.query_genes(
            domains=["alpha"], entities=[], max_genes=12, read_only=True,
        ))
        ids_explicit_none = _order(g.query_genes(
            domains=["alpha"], entities=[], max_genes=12, read_only=True,
            rerank_combinator=None,
        ))
    finally:
        g.close()
    assert ids.index("auth_b") < ids.index("auth_a")   # inversion preserved
    assert ids_explicit_none == ids                    # None == global, exactly


def test_per_query_override_off_reaches_combine_rerank():
    """An ``off`` override for THIS call flips to pure fused — the inversion
    disappears (auth_a outranks auth_b) — while the store's global stays
    additive (a second default call still inverts)."""
    g = _new_store(_defect1_corpus(), fusion_mode="rrf")
    try:
        off_ids = _order(g.query_genes(
            domains=["alpha"], entities=[], max_genes=12, read_only=True,
            rerank_combinator="off",
        ))
        # The override is per-call, not sticky: the next default call inverts.
        default_ids = _order(g.query_genes(
            domains=["alpha"], entities=[], max_genes=12, read_only=True,
        ))
    finally:
        g.close()
    assert off_ids.index("auth_a") < off_ids.index("auth_b")      # un-inverted
    assert default_ids.index("auth_b") < default_ids.index("auth_a")  # global intact
    assert g._rerank_combinator == "additive"                    # store never mutated


def test_per_query_override_eps_band_also_reaches_store():
    g = _new_store(_defect1_corpus(), fusion_mode="rrf")
    try:
        ids = _order(g.query_genes(
            domains=["alpha"], entities=[], max_genes=12, read_only=True,
            rerank_combinator="eps_band",
        ))
        fused = dict(g.last_fused_scores)
        final = dict(g.last_query_scores)
    finally:
        g.close()
    # eps_band keeps final scores PURE FUSED — additive would fold auth_b's
    # +2.0 authority into the score, so final == fused uniquely rules additive
    # OUT. auth_a/auth_b sit inside the default 5% band, so the in-band
    # authority still lets auth_b lead — distinct from `off` (which leaves
    # auth_a, the better fused doc, on top). Together this pins the 'eps_band'
    # override specifically reaching combine_rerank.
    assert final == fused
    assert ids.index("auth_b") < ids.index("auth_a")


# ═══ 5. sharded threading ═════════════════════════════════════════════


def _build_single_shard_main(root: Path) -> str:
    """Materialize the DEFECT-1 corpus into one on-disk shard + a main.db that
    routes ``alpha`` to it. Returns the main_path."""
    from helix_context.genome import Genome

    main_path = str(root / "main.db")
    shard_path = str(root / "shard_a.db")
    gs = Genome(shard_path)
    real_ids = {}
    for d in _defect1_corpus():
        real_ids[d.gene_id] = gs.upsert_gene(d, apply_gate=False)
    gs.conn.commit()
    gs.conn.close()
    if gs._reader:
        gs._reader.close()

    main = open_main_db(main_path)
    init_main_db(main)
    register_shard(main, "shard_a", "reference", shard_path, gene_count=6)
    for gid, real in real_ids.items():
        upsert_fingerprint(
            main, gene_id=real, shard_name="shard_a",
            source_id=f"/a/{gid}.md",
            domains_json=json.dumps(["alpha"]),
            entities_json=json.dumps(["alpha"]),
            key_values_json="[]",
        )
    main.close()
    return main_path


def test_sharded_adapter_threads_override_to_each_shard(monkeypatch):
    """The per-query combinator rides ShardedGenomeAdapter.query_docs ->
    ShardRouter.query_genes -> each shard's query_docs via the router's
    verbatim **kwargs fan-out. Spy the per-shard call and assert it saw the
    override."""
    seen: list = []
    orig = KnowledgeStore.query_docs

    def spy(self, *a, **k):
        seen.append(k.get("rerank_combinator"))
        return orig(self, *a, **k)

    monkeypatch.setattr(KnowledgeStore, "query_docs", spy)

    with tempfile.TemporaryDirectory() as td:
        main_path = _build_single_shard_main(Path(td))
        adapter = ShardedGenomeAdapter(main_path)
        try:
            adapter.query_docs(
                domains=["alpha"], entities=["alpha"], max_genes=8,
                read_only=True, rerank_combinator="off",
            )
        finally:
            adapter.close()

    assert seen, "no per-shard query_docs call was recorded"
    assert all(v == "off" for v in seen), seen


def test_sharded_default_no_override_threads_none(monkeypatch):
    """No override => the shard sees rerank_combinator=None => its own global
    (byte-identical default on the sharded path too)."""
    seen: list = []
    orig = KnowledgeStore.query_docs

    def spy(self, *a, **k):
        seen.append(k.get("rerank_combinator"))
        return orig(self, *a, **k)

    monkeypatch.setattr(KnowledgeStore, "query_docs", spy)

    with tempfile.TemporaryDirectory() as td:
        main_path = _build_single_shard_main(Path(td))
        adapter = ShardedGenomeAdapter(main_path)
        try:
            adapter.query_docs(
                domains=["alpha"], entities=["alpha"], max_genes=8,
                read_only=True,
            )
        finally:
            adapter.close()

    assert seen, "no per-shard query_docs call was recorded"
    assert all(v is None for v in seen), seen


# ═══ 6. end-to-end wiring through build_context ═══════════════════════


def _seeded_manager(rerank_map: dict, classifier_enabled: bool = True):
    """A mock-backend, in-memory-genome manager whose retrieval runs the plain
    (dense-off) query_docs path so the combinator override is deterministic to
    observe. Seeded so build_context reaches retrieval."""
    from tests.conftest import MockCompressorBackend, make_gene

    cfg = HelixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(max_genes_per_turn=4, splice_aggressiveness=0.5),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        classifier=ClassifierConfig(enabled=classifier_enabled),
        retrieval=RetrievalConfig(
            fusion_mode="rrf",
            dense_embedding_enabled=False,   # force the plain query_docs seam
            rerank_combinator_by_class=rerank_map,
        ),
    )
    mgr = HelixContextManager(cfg)
    mgr.ribosome.backend = MockCompressorBackend()
    for i, (content, doms, ents) in enumerate([
        ("what is the auth middleware path in the server module",
         ["auth", "server"], ["middleware", "auth", "path"]),
        ("auth middleware lives in server routes and handlers",
         ["auth", "server"], ["middleware", "routes"]),
        ("hello there general notes about greetings",
         ["greeting"], ["hello"]),
        ("more hello there phrasing examples",
         ["greeting"], ["hello"]),
    ]):
        mgr.genome.upsert_gene(
            make_gene(content, domains=doms, entities=ents,
                      gene_id=f"seed_gene_{i:010d}"),
        )
    return mgr


def _spy_combinator(monkeypatch, mgr) -> list:
    seen: list = []
    orig = mgr.genome.query_docs

    def spy(*a, **k):
        seen.append(k.get("rerank_combinator"))
        return orig(*a, **k)

    monkeypatch.setattr(mgr.genome, "query_docs", spy)
    return seen


def test_build_context_factual_query_selects_mapped_combinator(monkeypatch):
    """A wh-question classes ``factual``; the map routes factual -> off, and
    the store's query_docs receives rerank_combinator='off'."""
    mgr = _seeded_manager({"factual": "off"})
    seen = _spy_combinator(monkeypatch, mgr)
    try:
        mgr.build_context("what is the auth middleware path")
    finally:
        mgr.close()
    assert seen, "retrieval never called query_docs"
    assert all(v == "off" for v in seen), seen


def test_build_context_unmapped_class_falls_back_to_global(monkeypatch):
    """``hello there`` classes ``default``, which the map does not cover, so
    the store falls back to its global combinator (override None)."""
    mgr = _seeded_manager({"factual": "off"})   # default not in map
    seen = _spy_combinator(monkeypatch, mgr)
    try:
        mgr.build_context("hello there")
    finally:
        mgr.close()
    assert seen, "retrieval never called query_docs"
    assert all(v is None for v in seen), seen


def test_build_context_classifier_disabled_falls_back_to_global(monkeypatch):
    """Classifier disabled => classifier_result is None => override None even
    though the map is populated => global combinator on every query."""
    mgr = _seeded_manager({"factual": "off"}, classifier_enabled=False)
    seen = _spy_combinator(monkeypatch, mgr)
    try:
        mgr.build_context("what is the auth middleware path")
    finally:
        mgr.close()
    assert seen, "retrieval never called query_docs"
    assert all(v is None for v in seen), seen


def test_build_context_explicit_empty_map_is_byte_identical_global(monkeypatch):
    """The pre-graduation escape hatch (an EXPLICIT empty map) never overrides:
    the store always sees None regardless of the classifier class."""
    mgr = _seeded_manager({})
    seen = _spy_combinator(monkeypatch, mgr)
    try:
        mgr.build_context("what is the auth middleware path")
        mgr.build_context("hello there")
    finally:
        mgr.close()
    assert seen, "retrieval never called query_docs"
    assert all(v is None for v in seen), seen


def test_build_context_shipped_default_routes_multi_hop_and_default_to_eps_band(monkeypatch):
    """GRADUATED 2026-07-16 (PR #293 receipt): the shipped default map routes
    multi_hop and default classes to eps_band end-to-end through
    build_context. ``auth middleware compare server routes`` classes
    multi_hop (the ``compare`` connective, no leading wh-word); ``hello
    there`` classes default (matches no other class)."""
    default_map = RetrievalConfig().rerank_combinator_by_class
    mgr = _seeded_manager(default_map)
    seen = _spy_combinator(monkeypatch, mgr)
    try:
        result = classify_query("auth middleware compare server routes")
        assert result.cls == "multi_hop"  # guards the fixture assumption
        mgr.build_context("auth middleware compare server routes")
        mgr.build_context("hello there")
    finally:
        mgr.close()
    assert seen, "retrieval never called query_docs"
    assert all(v == "eps_band" for v in seen), seen
