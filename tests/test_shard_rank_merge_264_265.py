"""Rank-merge comparability fixes for sharded retrieval (#264, #265).

Both defects share one root cause (issue #275): the cross-shard merge applies
ADDITIVE-scale arithmetic to per-shard scores that are now RRF-scale, because
production per-shard ``Genome``s default to ``fusion_mode="rrf"``.

* #264 — the #121 doc-type boost (``corrected *= 1.15``) was calibrated on
  additive/BM25 margins. Under RRF the intra-shard margins compress to ~1.6%,
  so the fixed multiply becomes decisive on nearly every pair and flips a
  keyword-dense implementation file below a README. Fixed behind a
  DEFAULT-INERT ``doc_type_boost_mode`` knob (``additive`` = shipped, ``off``,
  ``rank``).
* #265 — the #182 global-IDF lexical splice (``raw - old_local_lex +
  new_global_lex``) mixes BM25-magnitude (0–6) lexical sub-scores into ``raw``.
  Undefined when ``raw`` is RRF-scale. Hard-guarded (capability guard, not a
  knob) to additive-mode shards; inert-with-a-warning under per-shard RRF.

Self-contained; two real on-disk shard fixtures, no server/GPU/network.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

import pytest

from cymatix_context.config import RetrievalConfig
from cymatix_context.genome import Genome
from cymatix_context.schemas import (
    ChromatinState,
    EpigeneticMarkers,
    Gene,
    PromoterTags,
)
from cymatix_context.shard_router import (
    DOC_TYPE_BOOST,
    ShardRouter,
)
from cymatix_context.shard_schema import (
    init_main_db,
    open_main_db,
    register_shard,
    upsert_fingerprint,
)

_GIDF = "HELIX_SHARD_GLOBAL_IDF"


def _mk_gene(content: str, domains: list, entities: list, source: str) -> Gene:
    return Gene(
        gene_id="",
        content=content,
        complement=content[:50],
        codons=[],
        promoter=PromoterTags(domains=domains, entities=entities, sequence_index=0),
        epigenetics=EpigeneticMarkers(),
        chromatin=ChromatinState.OPEN,
        is_fragment=False,
        source_id=source,
    )


# ── #264 fixture: README vs keyword-dense impl in the same shard ─────────
@pytest.fixture
def doc_type_setup():
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    main_path = str(root / "main.db")
    a = str(root / "a.db")
    b = str(root / "b.db")

    ga = Genome(a)
    readme_id = ga.upsert_gene(
        _mk_gene(
            "Acme Rust build overview. The release binary is around 4 MB.",
            ["acme"], ["rust"], "projects/acme-rs/README.md",
        ),
        apply_gate=False,
    )
    impl_id = ga.upsert_gene(
        _mk_gene(
            "binary binary binary size size build target binary measured "
            "binary size binary footprint binary size binary",
            ["acme"], ["binary"], "projects/acme-rs/src/binary_size_report.rs",
        ),
        apply_gate=False,
    )
    ga.conn.close()
    if ga._reader:
        ga._reader.close()

    gb = Genome(b)
    other_id = gb.upsert_gene(
        _mk_gene(
            "Unrelated second shard. Auth tokens and JWT sessions.",
            ["acme"], ["binary"], "other/notes.md",
        ),
        apply_gate=False,
    )
    gb.conn.close()
    if gb._reader:
        gb._reader.close()

    m = open_main_db(main_path)
    init_main_db(m)
    register_shard(m, "shard_a", "reference", a, gene_count=2)
    register_shard(m, "shard_b", "participant", b, gene_count=1)
    for gid, src, ents in (
        (readme_id, "projects/acme-rs/README.md", ["rust"]),
        (impl_id, "projects/acme-rs/src/binary_size_report.rs", ["binary"]),
    ):
        upsert_fingerprint(
            m, gene_id=gid, shard_name="shard_a", source_id=src,
            domains_json=json.dumps(["acme"]),
            entities_json=json.dumps(ents), key_values_json="[]",
        )
    upsert_fingerprint(
        m, gene_id=other_id, shard_name="shard_b", source_id="other/notes.md",
        domains_json=json.dumps(["acme"]),
        entities_json=json.dumps(["binary"]), key_values_json="[]",
    )
    m.close()
    yield {"main_path": main_path, "readme_id": readme_id, "impl_id": impl_id}
    td.cleanup()


def _ranked(router):
    res = router.query_genes(domains=["acme"], entities=["binary"], max_genes=10)
    return [g.gene_id for g in res], dict(router.last_query_scores)


# ── #264 config + validation ────────────────────────────────────────────
def test_doc_type_boost_mode_default_is_additive():
    assert RetrievalConfig().doc_type_boost_mode == "additive"


def test_doc_type_boost_mode_invalid_config_raises():
    with pytest.raises(ValueError, match="doc_type_boost_mode"):
        RetrievalConfig(doc_type_boost_mode="magnitude")


def test_doc_type_boost_mode_toml_roundtrip(tmp_path):
    from cymatix_context.config import load_config
    toml = tmp_path / "helix.toml"
    toml.write_text('[retrieval]\ndoc_type_boost_mode = "off"\n', encoding="utf-8")
    c = load_config(str(toml))
    assert c.retrieval.doc_type_boost_mode == "off"


def test_router_invalid_mode_raises(doc_type_setup):
    with pytest.raises(ValueError, match="doc_type_boost_mode"):
        ShardRouter(doc_type_setup["main_path"], doc_type_boost_mode="nope")


# ── #264 default byte-identity (golden) ─────────────────────────────────
def test_default_mode_reproduces_shipped_boost(doc_type_setup):
    """DEFAULT ("additive") multiplies the README's corrected score by exactly
    DOC_TYPE_BOOST vs the "off" run — the shipped #121 behaviour, unchanged."""
    r_def = ShardRouter(doc_type_setup["main_path"])  # default additive
    r_off = ShardRouter(doc_type_setup["main_path"], doc_type_boost_mode="off")
    try:
        _, sc_def = _ranked(r_def)
        _, sc_off = _ranked(r_off)
    finally:
        r_def.close()
        r_off.close()
    rid = doc_type_setup["readme_id"]
    iid = doc_type_setup["impl_id"]
    assert sc_def[rid] == pytest.approx(sc_off[rid] * DOC_TYPE_BOOST, rel=1e-6)
    # impl (not a doc-type) is byte-identical between the two modes.
    assert sc_def[iid] == pytest.approx(sc_off[iid], rel=1e-12)


# ── #264 the flip case each fix resolves ────────────────────────────────
def test_default_rrf_reproduces_the_264_flip(doc_type_setup):
    """Per-shard RRF (production default) + additive boost mode flips the
    keyword-dense impl BELOW the README — the #264 defect, still the default."""
    r = ShardRouter(doc_type_setup["main_path"])  # fusion default rrf, mode additive
    try:
        ids, sc = _ranked(r)
        rid, iid = doc_type_setup["readme_id"], doc_type_setup["impl_id"]
        assert sc[rid] > sc[iid], "expected the #264 flip under the default"
        assert ids.index(rid) < ids.index(iid)
    finally:
        r.close()


def test_off_mode_fixes_the_264_flip(doc_type_setup):
    """``doc_type_boost_mode="off"`` satisfies #121's hard constraint under
    RRF: the keyword-dense impl file stays above the README."""
    r = ShardRouter(doc_type_setup["main_path"], doc_type_boost_mode="off")
    try:
        ids, sc = _ranked(r)
        rid, iid = doc_type_setup["readme_id"], doc_type_setup["impl_id"]
        assert sc[iid] > sc[rid], "off mode must not flip the impl below README"
        assert ids.index(iid) < ids.index(rid)
    finally:
        r.close()


def test_rank_mode_uses_fuser_tier_not_magnitude(doc_type_setup):
    """``rank`` mode leaves the additive-scale ``corrected`` scores unmultiplied
    (the lift is a Fuser tier, not a magnitude ×1.15) — so the published README
    score equals its unboosted value, unlike the default additive mode."""
    r_rank = ShardRouter(doc_type_setup["main_path"], doc_type_boost_mode="rank")
    r_off = ShardRouter(doc_type_setup["main_path"], doc_type_boost_mode="off")
    try:
        _, sc_rank = _ranked(r_rank)
        _, sc_off = _ranked(r_off)
    finally:
        r_rank.close()
        r_off.close()
    rid = doc_type_setup["readme_id"]
    # corrected is untouched by rank mode (no ×1.15); differs from additive.
    assert sc_rank[rid] == pytest.approx(sc_off[rid], rel=1e-9)


def test_additive_fusion_single_shard_unaffected_by_mode(doc_type_setup):
    """The knob is gated to the ≥2-shard merge — a single-shard route is
    byte-identical across all three modes (blob-parity contract)."""
    scores = {}
    for mode in ("additive", "off", "rank"):
        r = ShardRouter(doc_type_setup["main_path"], doc_type_boost_mode=mode)
        try:
            # entity that routes to a single shard would be ideal; here both
            # docs live in shard_a, so restrict entities to force one shard.
            res = r.query_genes(domains=[], entities=["rust"], max_genes=10)
            scores[mode] = tuple(sorted((g.gene_id, round(s, 9)) for g, s in
                                        zip(res, [r.last_query_scores.get(g.gene_id, 0.0) for g in res])))
        finally:
            r.close()
    # "rust" only fingerprints the README in shard_a -> single-shard path.
    assert scores["additive"] == scores["off"] == scores["rank"]


# ── #265 fixture: local-vs-global IDF trap across two shards ─────────────
@pytest.fixture
def idf_trap():
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    main_path = str(root / "main.db")
    a = str(root / "a.db")
    b = str(root / "b.db")
    ROUTE = "topic"

    ga = Genome(a)
    gold_id = ga.upsert_gene(
        _mk_gene(
            "topic widget widget widget widget gold answer payload "
            "the canonical widget specification lives here",
            ["topic"], [ROUTE, "gold"], "/a/gold_widget.md",
        ),
        apply_gate=False,
    )
    wrong_id = ga.upsert_gene(
        _mk_gene(
            "topic frob frob frob frob wrong incumbent widget noise "
            "frobnicator handling and frob dispatch internals",
            ["topic"], [ROUTE, "wrong"], "/a/wrong_frob.md",
        ),
        apply_gate=False,
    )
    for i in range(4):
        ga.upsert_gene(
            _mk_gene(f"topic widget filler entry number {i} with widget mention",
                     ["topic"], [ROUTE], f"/a/filler_{i}.md"),
            apply_gate=False,
        )
    ga.conn.close()
    if ga._reader:
        ga._reader.close()

    gb = Genome(b)
    b_ids = [
        gb.upsert_gene(
            _mk_gene(f"topic frob common boilerplate document {i} with frob content",
                     ["topic"], [ROUTE], f"/b/frob_{i}.md"),
            apply_gate=False,
        )
        for i in range(24)
    ]
    gb.conn.close()
    if gb._reader:
        gb._reader.close()

    m = open_main_db(main_path)
    init_main_db(m)
    register_shard(m, "shard_a", "reference", a, gene_count=6)
    register_shard(m, "shard_b", "participant", b, gene_count=24)
    upsert_fingerprint(m, gene_id=gold_id, shard_name="shard_a",
                       source_id="/a/gold_widget.md",
                       domains_json=json.dumps(["topic"]),
                       entities_json=json.dumps([ROUTE, "gold"]),
                       key_values_json="[]")
    upsert_fingerprint(m, gene_id=wrong_id, shard_name="shard_a",
                       source_id="/a/wrong_frob.md",
                       domains_json=json.dumps(["topic"]),
                       entities_json=json.dumps([ROUTE, "wrong"]),
                       key_values_json="[]")
    upsert_fingerprint(m, gene_id=b_ids[0], shard_name="shard_b",
                       source_id="/b/frob_0.md",
                       domains_json=json.dumps(["topic"]),
                       entities_json=json.dumps([ROUTE]),
                       key_values_json="[]")
    m.close()
    yield {"main_path": main_path, "gold_id": gold_id, "wrong_id": wrong_id}
    td.cleanup()


_Q = {"domains": ["topic"], "entities": ["widget", "frob"]}


@pytest.fixture(autouse=True)
def _clear_gidf():
    prev = os.environ.pop(_GIDF, None)
    try:
        yield
    finally:
        os.environ.pop(_GIDF, None)
        if prev is not None:
            os.environ[_GIDF] = prev


def _rank_of(router, gene_id):
    genes = router.query_genes(domains=_Q["domains"], entities=_Q["entities"], max_genes=8)
    ids = [g.gene_id for g in genes]
    return (ids.index(gene_id) if gene_id in ids else None), ids


# ── #265 the capability guard ───────────────────────────────────────────
def test_global_idf_suppressed_under_per_shard_rrf(idf_trap):
    """Flag ON + per-shard RRF (production default): the splice is guarded off
    and the router behaves identically to the flag being OFF (scalar path).

    Undefined-under-RRF (#265): compare flag-ON-rrf against flag-OFF-rrf and
    require byte-identical published scores — proving the splice never ran."""
    os.environ[_GIDF] = "1"
    r_on = ShardRouter(idf_trap["main_path"])  # fusion default rrf
    on_ids = [g.gene_id for g in r_on.query_genes(**_Q, max_genes=8)]
    on_scores = dict(r_on.last_query_scores)
    r_on.close()

    os.environ.pop(_GIDF, None)
    r_off = ShardRouter(idf_trap["main_path"])
    off_ids = [g.gene_id for g in r_off.query_genes(**_Q, max_genes=8)]
    off_scores = dict(r_off.last_query_scores)
    r_off.close()

    assert on_ids == off_ids, "flag ON under RRF must not change the order"
    for gid in off_scores:
        assert on_scores[gid] == pytest.approx(off_scores[gid], rel=1e-12), (
            "global-IDF splice must be a byte-identical no-op under per-shard RRF"
        )


def test_global_idf_runs_under_additive(idf_trap):
    """The guard is fusion-mode-scoped, not a blanket disable: pinning the
    shards to additive re-enables the #182 splice (gold outranks wrong)."""
    os.environ[_GIDF] = "1"
    r = ShardRouter(idf_trap["main_path"], fusion_mode="additive")
    try:
        gr, ids = _rank_of(r, idf_trap["gold_id"])
        wr, _ = _rank_of(r, idf_trap["wrong_id"])
        assert gr is not None and wr is not None, f"both must be pooled: {ids}"
        assert gr < wr, "global-IDF splice must lift gold above wrong under additive"
    finally:
        r.close()


def test_global_idf_rrf_warns_once(idf_trap, caplog):
    """A truthy flag under per-shard RRF logs the suppression notice once."""
    import cymatix_context.shard_router as sr
    sr._GLOBAL_IDF_RRF_WARNED = False  # reset process-once latch for the test
    os.environ[_GIDF] = "1"
    with caplog.at_level(logging.WARNING):
        r = ShardRouter(idf_trap["main_path"])
        try:
            r.query_genes(**_Q, max_genes=8)
            r.query_genes(**_Q, max_genes=8)  # second call must NOT re-warn
        finally:
            r.close()
    hits = [rec for rec in caplog.records if "HELIX_SHARD_GLOBAL_IDF" in rec.message]
    assert len(hits) == 1, f"expected exactly one suppression warning, got {len(hits)}"
