"""Cross-shard global-IDF lexical re-score (HELIX_SHARD_GLOBAL_IDF, #182).

Reproduces and fixes the within-shard mis-ranking bug on sharded
retrieval:

  Root cause: the cross-shard correction ``m_shard`` is a PER-SHARD
  SCALAR. A scalar rescales every doc in a shard equally, so it cannot
  change the relative order of two docs in the SAME shard. That order is
  set by each shard's corpus-LOCAL BM25 IDF. A gold whose distinguishing
  term is globally-rare-but-locally-common is buried under a same-shard
  incumbent, and no scalar can rescue it.

  Fix: when HELIX_SHARD_GLOBAL_IDF is truthy and ≥2 shards participate,
  re-score each candidate's BM25 lexical component per-doc with TRUE
  GLOBAL IDF (aggregated N / df over all shards) instead of the shard's
  local IDF.

Fixture design (two real on-disk shards, no GPU/server/network):

  * Query terms: ``widget`` (the gold's distinguishing term) and ``frob``.
  * ``widget`` is LOCALLY COMMON in shard A (appears in most A docs → tiny
    local IDF) but GLOBALLY RARE (shard B has none, and B is large).
  * ``frob`` is LOCALLY RARE in shard A (high local IDF) but GLOBALLY
    COMMON (filler in many B docs → tiny global IDF).
  * GOLD doc lives in shard A and is ``widget``-heavy.
  * WRONG doc lives in shard A and is ``frob``-heavy.

  ⇒ Under LOCAL IDF (flag OFF), ``frob``'s high local IDF lifts WRONG
     above GOLD inside shard A — the scalar can't reorder them.
  ⇒ Under GLOBAL IDF (flag ON), ``widget``'s high global IDF lifts GOLD
     above WRONG.

Asserts:
  * flag OFF → WRONG outranks GOLD (reproduces the bug).
  * flag ON  → GOLD outranks WRONG (global IDF fixes within-shard order).

Self-contained; runs under ``pytest --noconftest``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from cymatix_context.genome import Genome
from cymatix_context.schemas import (
    ChromatinState,
    EpigeneticMarkers,
    Gene,
    PromoterTags,
)
from cymatix_context.shard_router import ShardRouter
from cymatix_context.shard_schema import (
    init_main_db,
    open_main_db,
    register_shard,
    upsert_fingerprint,
)

_FLAG = "HELIX_SHARD_GLOBAL_IDF"


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


@pytest.fixture(autouse=True)
def _clear_flag():
    prev = os.environ.pop(_FLAG, None)
    try:
        yield
    finally:
        os.environ.pop(_FLAG, None)
        if prev is not None:
            os.environ[_FLAG] = prev


@pytest.fixture
def idf_trap_setup():
    """Two real shard .db files engineered so local vs global IDF disagree.

    The query is routed to BOTH shards via shared fingerprint terms so the
    genuine ≥2-shard cross-shard merge path runs.
    """
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    main_path = str(root / "main.db")
    shard_a_path = str(root / "shard_a.db")
    shard_b_path = str(root / "shard_b.db")

    # All docs share the routing entity "topic" so the query routes to
    # both shards regardless of the discriminating terms.
    ROUTE_ENT = "topic"

    ga = Genome(shard_a_path)
    # GOLD: widget-heavy (widget x4). widget is the distinguishing term.
    gold = _mk_gene(
        "topic widget widget widget widget gold answer payload "
        "the canonical widget specification lives here",
        domains=["topic"],
        entities=[ROUTE_ENT, "gold"],
        source="/a/gold_widget.md",
    )
    gold_id = ga.upsert_gene(gold, apply_gate=False)

    # WRONG: frob-heavy (frob x4), only one widget. frob is locally rare
    # in shard A (only this doc + nothing else has it here) → high LOCAL
    # IDF lifts WRONG above GOLD inside shard A.
    wrong = _mk_gene(
        "topic frob frob frob frob wrong incumbent widget noise "
        "frobnicator handling and frob dispatch internals",
        domains=["topic"],
        entities=[ROUTE_ENT, "wrong"],
        source="/a/wrong_frob.md",
    )
    wrong_id = ga.upsert_gene(wrong, apply_gate=False)

    # Filler docs in shard A that ALSO contain "widget" — this makes
    # "widget" LOCALLY COMMON in shard A (drives its local IDF down) while
    # it stays globally rare (shard B has none).
    for i in range(4):
        f = _mk_gene(
            f"topic widget filler entry number {i} with widget mention",
            domains=["topic"],
            entities=[ROUTE_ENT],
            source=f"/a/filler_{i}.md",
        )
        ga.upsert_gene(f, apply_gate=False)
    ga.conn.close()
    if ga._reader:
        ga._reader.close()

    # Shard B: large filler shard packed with "frob" so frob is GLOBALLY
    # COMMON (low global IDF) and the overall corpus N is large (lifting
    # widget's global IDF). None of these contain "widget".
    gb = Genome(shard_b_path)
    b_ids = []
    for i in range(24):
        f = _mk_gene(
            f"topic frob common boilerplate document {i} with frob content",
            domains=["topic"],
            entities=[ROUTE_ENT],
            source=f"/b/frob_{i}.md",
        )
        b_ids.append(gb.upsert_gene(f, apply_gate=False))
    gb.conn.close()
    if gb._reader:
        gb._reader.close()

    main = open_main_db(main_path)
    init_main_db(main)
    register_shard(main, "shard_a", "reference", shard_a_path, gene_count=6)
    register_shard(main, "shard_b", "participant", shard_b_path, gene_count=24)

    # Fingerprint GOLD + WRONG so they're addressable; route on "topic".
    upsert_fingerprint(
        main, gene_id=gold_id, shard_name="shard_a",
        source_id="/a/gold_widget.md",
        domains_json=json.dumps(["topic"]),
        entities_json=json.dumps([ROUTE_ENT, "gold"]),
        key_values_json="[]",
    )
    upsert_fingerprint(
        main, gene_id=wrong_id, shard_name="shard_a",
        source_id="/a/wrong_frob.md",
        domains_json=json.dumps(["topic"]),
        entities_json=json.dumps([ROUTE_ENT, "wrong"]),
        key_values_json="[]",
    )
    # Fingerprint at least one shard-B doc on "topic" so the router fans
    # out to shard_b too (≥2-shard merge path). The exact gene doesn't
    # matter for the assertion.
    upsert_fingerprint(
        main, gene_id=b_ids[0], shard_name="shard_b",
        source_id="/b/frob_0.md",
        domains_json=json.dumps(["topic"]),
        entities_json=json.dumps([ROUTE_ENT]),
        key_values_json="[]",
    )
    main.close()

    yield {
        "main_path": main_path,
        "gold_id": gold_id,
        "wrong_id": wrong_id,
    }
    td.cleanup()


def _rank_of(router, gene_id, query):
    """Return the 0-based rank of gene_id in the router result (or None)."""
    genes = router.query_genes(domains=query["domains"], entities=query["entities"],
                               max_genes=8)
    ids = [g.gene_id for g in genes]
    return ids.index(gene_id) if gene_id in ids else None, ids


# Query: both discriminating terms + the routing term.
_QUERY = {"domains": ["topic"], "entities": ["widget", "frob"]}


def test_both_shards_participate(idf_trap_setup):
    """Sanity: the query must route to ≥2 shards or the fix is a no-op."""
    router = ShardRouter(idf_trap_setup["main_path"])
    try:
        shards = router.route(domains=_QUERY["domains"], entities=_QUERY["entities"])
        assert len(shards) >= 2, f"expected ≥2 shards, got {shards}"
    finally:
        router.close()


def test_flag_off_reproduces_bug(idf_trap_setup):
    """Flag OFF (scalar m_shard): WRONG outranks GOLD — the bug."""
    os.environ.pop(_FLAG, None)  # explicitly OFF
    router = ShardRouter(idf_trap_setup["main_path"])
    try:
        gold_rank, ids = _rank_of(router, idf_trap_setup["gold_id"], _QUERY)
        wrong_rank, _ = _rank_of(router, idf_trap_setup["wrong_id"], _QUERY)
        assert gold_rank is not None, f"gold not admitted to pool: {ids}"
        assert wrong_rank is not None, f"wrong not admitted to pool: {ids}"
        # Bug signature: the scalar can't reorder within shard A, so the
        # frob-heavy WRONG doc (high LOCAL idf on frob) sits above GOLD.
        assert wrong_rank < gold_rank, (
            f"expected WRONG (rank {wrong_rank}) above GOLD (rank {gold_rank}) "
            f"under local-IDF scalar path; ids={ids}"
        )
    finally:
        router.close()


def test_flag_on_fixes_within_shard_order(idf_trap_setup):
    """Flag ON (per-doc global IDF): GOLD outranks WRONG — the fix."""
    os.environ[_FLAG] = "1"
    # additive-physics pin (#256): the #182 global-IDF lexical splice
    # (corrected = raw - old_local_fts5 + new_global_fts5) is only
    # scale-coherent when the per-shard store publishes additive/BM25-scale
    # scores. Production per-shard Genomes run rrf (config.retrieval.fusion_mode
    # fanned via open_read_source -> ShardRouter -> per-shard Genome), where the
    # frob-heavy WRONG doc no longer enters the merged pool at all — the fix's
    # OFF/ON contrast collapses. tracked in #265.
    router = ShardRouter(idf_trap_setup["main_path"], fusion_mode="additive")
    try:
        gold_rank, ids = _rank_of(router, idf_trap_setup["gold_id"], _QUERY)
        wrong_rank, _ = _rank_of(router, idf_trap_setup["wrong_id"], _QUERY)
        assert gold_rank is not None, f"gold not admitted to pool: {ids}"
        assert wrong_rank is not None, f"wrong not admitted to pool: {ids}"
        # Fix: widget's high GLOBAL idf lifts GOLD above WRONG even though
        # they share shard A (global IDF reorders within-shard).
        assert gold_rank < wrong_rank, (
            f"expected GOLD (rank {gold_rank}) above WRONG (rank {wrong_rank}) "
            f"under global-IDF path; ids={ids}"
        )
    finally:
        router.close()


def test_flag_truthy_variants(idf_trap_setup):
    """All truthy spellings enable the fix; junk values stay OFF."""
    gold_id = idf_trap_setup["gold_id"]
    wrong_id = idf_trap_setup["wrong_id"]

    for val in ("1", "true", "YES", "On"):
        os.environ[_FLAG] = val
        # additive-physics pin (#256): #182 global-IDF splice is additive-scale;
        # production per-shard Genomes run rrf where WRONG drops out of the pool.
        # tracked in #265.
        router = ShardRouter(idf_trap_setup["main_path"], fusion_mode="additive")
        try:
            gr, _ = _rank_of(router, gold_id, _QUERY)
            wr, _ = _rank_of(router, wrong_id, _QUERY)
            assert gr is not None and wr is not None and gr < wr, (
                f"truthy {val!r} should enable fix (gold {gr} < wrong {wr})"
            )
        finally:
            router.close()

    for val in ("0", "false", "no", "", "maybe"):
        os.environ[_FLAG] = val
        # additive-physics pin (#256): scalar-path OFF branch of the same
        # additive-scale #182 contrast. tracked in #265.
        router = ShardRouter(idf_trap_setup["main_path"], fusion_mode="additive")
        try:
            gr, _ = _rank_of(router, gold_id, _QUERY)
            wr, _ = _rank_of(router, wrong_id, _QUERY)
            assert gr is not None and wr is not None and wr < gr, (
                f"non-truthy {val!r} should stay on scalar path "
                f"(wrong {wr} < gold {gr})"
            )
        finally:
            router.close()


# ── FTS5 bm25() scale-parity (#182 root-cause guard) ──────────────────
#
# The HELIX_SHARD_GLOBAL_IDF recall regression (0.347 -> 0.168 on medium
# sharded) was a SCALE bug, not a gating bug: the manual BM25 in
# rescore_lexical_global_idf used the SMOOTHED, always-positive IDF
# ln((N-n+0.5)/(n+0.5) + 1.0), while SQLite FTS5's bm25() uses the RAW
# (unsmoothed) IDF ln((N-n+0.5)/(n+0.5)) (which can be negative). Feeding
# the splice corrected = raw - old_local_fts5 + new_global_fts5 a lexical
# sub-score on a different scale corrupted ranking.
#
# This test pins the fix: rescore_lexical_global_idf, fed each term's LOCAL
# raw IDF (so global == local), must reproduce ``bm25(genes_fts)`` for the
# SAME shard's own docs to floating-point tolerance. The genome records the
# FTS5 lexical sub-score as ``min(-rank, 6.0)`` where ``-rank`` is the
# negated bm25() sum; the rescore returns that same UNcapped positive sum,
# so ``rescore == -bm25()``.

def test_manual_bm25_matches_fts5_bm25_local(idf_trap_setup):
    """rescore_lexical_global_idf with LOCAL raw IDF == -bm25(genes_fts)."""
    import math

    # Reopen shard A read-only (it holds gold + wrong + 4 widget fillers).
    router = ShardRouter(idf_trap_setup["main_path"])
    try:
        shard = router._open_shard("shard_a")
        terms = ["widget", "frob"]
        n_local = shard.fts_doc_count()
        dfs = shard.term_doc_frequencies(terms)
        # LOCAL raw IDF (the basis FTS5's own bm25 uses) — global == local.
        # Apply FTS5's exact negative-IDF clamp (idf <= 0 -> 1e-6); ``widget``
        # here appears in EVERY shard-A doc, so its raw IDF is negative and
        # the engine clamps it — the manual must do the same to stay
        # bit-exact (this is the third leg of the #182 scale fix).
        def _fts5_idf(n_total: int, df: int) -> float:
            idf = math.log((n_total - df + 0.5) / (df + 0.5))
            return idf if idf > 0.0 else 1e-6

        local_idf = {t: _fts5_idf(n_local, dfs[t]) for t in terms}

        # Engine ranking + bm25() for the OR query across BOTH columns.
        match = " OR ".join(f'"{t}"' for t in terms)
        cur = shard.read_conn.cursor()
        rows = cur.execute(
            "SELECT gene_id, bm25(genes_fts) AS r "
            "FROM genes_fts WHERE genes_fts MATCH ? ORDER BY rank",
            (match,),
        ).fetchall()
        engine = {row["gene_id"]: float(row["r"]) for row in rows}
        assert engine, "FTS5 returned no rows for the parity probe"

        manual = shard.rescore_lexical_global_idf(
            list(engine.keys()), terms, local_idf,
        )
        assert manual, "rescore returned empty"

        worst = 0.0
        for gid, eng in engine.items():
            # rescore is the positive bm25 sum; engine bm25() is negated.
            worst = max(worst, abs(eng - (-manual[gid])))
        assert worst < 1e-6, (
            f"manual BM25 must match FTS5 bm25() (worst |delta|={worst:.3e}); "
            f"a non-zero gap means the IDF basis / tf-normalisation drifted "
            f"from the engine again (#182 scale regression)."
        )
    finally:
        router.close()
