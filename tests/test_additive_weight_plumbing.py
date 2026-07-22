"""Issue #202: the documented ``[retrieval]`` tier weights must bind in
ADDITIVE fusion mode (the default), not just under RRF.

Before the fix, ``fts5_weight``, ``splade_weight``, ``tag_exact_weight``,
``tag_prefix_weight``, ``sema_cold_weight``, ``lex_anchor_weight``,
``harmonic_weight`` and ``entity_graph_weight`` were consumed ONLY via
``fuser.add_tier()`` — dead knobs under ``fusion_mode="additive"`` whose
tier scores were inline literals.  The warm ΣĒMA boost (``sim * 2.0``)
had no knob at all; the fix adds ``sema_boost_weight`` (default 2.0).

Three test families:

1. **Golden byte-identity** — a 10-document corpus that fires every
   additive tier; final scores and per-tier contributions must equal,
   bit-for-bit, the numbers captured on the PRE-FIX tree (commit
   266e9aa, the additive literals).  Regenerate after a deliberate
   ranking change with::

       python -c "import sys; sys.path.insert(0, '.'); \
from tests.test_additive_weight_plumbing import print_golden; print_golden()"

   Caveat: the fts5 contributions embed raw SQLite BM25 magnitudes and
   the sema tiers go through numpy float32 dot products.  Both are
   deterministic for a fixed corpus and chosen so no reduction-order
   ambiguity exists (single-overlap unit vectors), but a future SQLite
   bm25 change would require a regen (the additive snapshot test in
   ``test_fusion_rrf.py`` would trip too).

2. **Knob moves its tier** — setting a weight scales exactly that
   tier's contribution (asserted against a same-process default run,
   so no embedded floats are involved).

3. **Zero weight kills the tier** — contribution drops to 0.0 (or the
   tier stops firing entirely for gated tiers like lex_anchor).
"""
from __future__ import annotations

import contextlib
import math

import pytest

from cymatix_context.genome import Genome
from cymatix_context.schemas import (
    ChromatinState, EpigeneticMarkers, Gene, PromoterTags,
)

# ─── Query + corpus fixtures ─────────────────────────────────────────

QUERY_DOMAINS = ["alpha"]
QUERY_ENTITIES = ["epsilon"]
MAX_GENES = 12  # limit = 24 → cold-tier gate len(gene_scores) < 12 holds


class FakeSemaCodec:
    """Deterministic stand-in for backends.sema.SemaCodec.

    ``encode`` returns a fixed unit vector; ``nearest`` reimplements the
    real codec's cosine contract in pure python (no numpy reductions →
    bit-stable across BLAS implementations).
    """

    QUERY_VEC = [1.0] + [0.0] * 19

    def encode(self, text: str):
        return list(self.QUERY_VEC)

    def nearest(self, query_vec, candidates, k=5):
        def cos(a, b):
            num = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(y * y for y in b))
            if na < 1e-8 or nb < 1e-8:
                return 0.0
            return num / (na * nb)

        scored = [(gid, cos(query_vec, vec)) for gid, vec in candidates]
        scored.sort(key=lambda t: (-t[1], t[0]))
        return scored[:k]


def _vec(first: float, second: float = 0.0):
    """20-dim vector overlapping the query vec on dim 0 only.

    Cosine vs QUERY_VEC == first / ||v|| — a single multiply, no
    summation-order ambiguity in the numpy cold-tier matmul.
    """
    v = [0.0] * 20
    v[0] = first
    v[1] = second
    return v


def _gene(gid, content, domains, entities=(), embedding=None) -> Gene:
    return Gene(
        gene_id=gid,
        content=content,
        complement="",
        codons=[],
        promoter=PromoterTags(domains=list(domains), entities=list(entities)),
        epigenetics=EpigeneticMarkers(),
        chromatin=ChromatinState.OPEN,
        is_fragment=False,
        embedding=embedding,
    )


@contextlib.contextmanager
def patched_splade():
    """Replace the SPLADE model calls with fixed, deterministic hits.

    ``encode`` (used at ingest AND for the query) returns an empty
    sparse dict; ``query_splade`` returns two raw scores chosen so one
    saturates the 20.0 normalization cap (gA) and one does not (gD).
    """
    from cymatix_context.backends import splade_backend

    old_encode = splade_backend.encode
    old_query = splade_backend.query_splade
    splade_backend.encode = lambda text, **kw: {}
    splade_backend.query_splade = (
        lambda conn, sparse, limit=20: [("gA", 30.0), ("gD", 10.0)]
    )
    try:
        yield
    finally:
        splade_backend.encode = old_encode
        splade_backend.query_splade = old_query


def build_corpus(g: Genome) -> None:
    """10 synthetic documents lighting up every additive tier.

    gA — exact tag 'alpha' + prefix + FTS + SPLADE(sat) + warm ΣĒMA +
         lex anchor + harmonic + entity_graph + authority
    gB — prefix tag 'alphabeta' + harmonic link to gA + entity_graph
    gC — exact tag 'epsilon' + FTS + lex anchor
    gD — SPLADE-only candidate (no lexical hit)
    gE/gF — cold ΣĒMA fills (sim 1.0 / cos vs query 0.7071...)
    gG — embedding below the 0.4 cold floor (control, never retrieved)
    gH/gI/gJ — inert filler (corpus size / IDF stability)
    """
    docs = [
        _gene("gA",
              "alpha configures the parser pipeline. alpha owns the retry "
              "policy and alpha gates the splice budget.",
              domains=["alpha", "parser"],
              embedding=_vec(0.75, 0.5)),
        _gene("gB",
              "the merge stage rewrites stale buffers before compaction.",
              domains=["alphabeta"]),
        _gene("gC",
              "epsilon thresholds for the scheduler retry loop.",
              domains=["epsilon"]),
        _gene("gD",
              "vector quantization keeps the shard logic compact.",
              domains=["gamma"]),
        _gene("gE",
              "the cold start path warms caches in the background.",
              domains=["delta"],
              embedding=_vec(1.0)),
        _gene("gF",
              "queue depth metrics feed the backpressure controller.",
              domains=["zeta"],
              embedding=_vec(0.5, 0.5)),
        _gene("gG",
              "log rotation happens at midnight local time.",
              domains=["eta"],
              embedding=_vec(0.125, 1.0)),
        _gene("gH", "summaries are spooled to the export bucket.",
              domains=["omega"]),
        _gene("gI", "the watchdog restarts wedged workers.",
              domains=["sigma"]),
        _gene("gJ", "tracing spans are sampled at one percent.",
              domains=["kappa"]),
    ]
    for d in docs:
        g.upsert_gene(d, apply_gate=False)

    # Tier 5 fixture: harmonic link between two lexical candidates.
    g.conn.execute(
        "INSERT INTO harmonic_links "
        "(gene_id_a, gene_id_b, weight, updated_at, source) "
        "VALUES ('gA', 'gB', 1.0, 0.0, 'co_retrieved')"
    )
    # Tier 5b fixture: entity co-occurrence rows for the query entity.
    g.conn.execute(
        "INSERT INTO entity_graph (entity, gene_id) VALUES ('epsilon', 'gA')"
    )
    g.conn.execute(
        "INSERT INTO entity_graph (entity, gene_id) VALUES ('epsilon', 'gB')"
    )
    g.conn.commit()


def make_genome(**weight_kwargs) -> Genome:
    g = Genome(
        path=":memory:",
        sema_codec=FakeSemaCodec(),
        splade_enabled=True,
        entity_graph_retrieval_enabled=True,
        **weight_kwargs,
    )
    with patched_splade():
        build_corpus(g)
    return g


def run_query(g: Genome):
    """Run the canonical query; return (ranked_ids, scores, tier_contrib)."""
    with patched_splade():
        genes = g.query_genes(
            domains=list(QUERY_DOMAINS),
            entities=list(QUERY_ENTITIES),
            max_genes=MAX_GENES,
            read_only=True,
        )
    ranked = [x.gene_id for x in genes]
    scores = dict(g.last_query_scores)
    contrib = {gid: dict(t) for gid, t in g.last_tier_contributions.items()}
    return ranked, scores, contrib


def capture():
    g = make_genome()
    try:
        return run_query(g)
    finally:
        g.close()


def print_golden():  # pragma: no cover — regen helper, run by hand
    ranked, scores, contrib = capture()
    print("_GOLDEN_RANKED =", repr(ranked))
    print("_GOLDEN_SCORES =", repr(scores))
    print("_GOLDEN_CONTRIB =", repr(contrib))


# ─── Golden numbers (captured on the PRE-FIX tree, commit 266e9aa) ───

_GOLDEN_RANKED = ['gA', 'gC', 'gB', 'gE', 'gF', 'gD']
_GOLDEN_SCORES = {'gA': 18.428866615370232, 'gC': 12.118210600885599, 'gB': 3.5, 'gD': 2.25, 'gE': 3.5, 'gF': 2.6213203072547913}
_GOLDEN_CONTRIB = {'gA': {'tag_exact': 3.0, 'tag_prefix': 1.5, 'fts5': 2.71034323891445, 'splade': 3.5, 'sema_boost': 1.2185233764557821, 'lex_anchor': 3.0, 'authority': 2.0, 'harmonic': 1.0, 'entity_graph': 0.5}, 'gC': {'tag_exact': 3.0, 'tag_prefix': 1.5, 'fts5': 2.618210600885599, 'lex_anchor': 3.0, 'authority': 2.0}, 'gB': {'tag_prefix': 1.5, 'authority': 0.5, 'harmonic': 1.0, 'entity_graph': 0.5}, 'gD': {'splade': 1.75, 'authority': 0.5}, 'gE': {'sema_cold': 3.0, 'authority': 0.5}, 'gF': {'sema_cold': 2.1213203072547913, 'authority': 0.5}}


# ─── 1. byte-identity with the pre-fix additive literals ─────────────


def test_additive_default_scores_byte_identical_to_prefix_golden():
    # additive-physics pin (#256): tests the legacy accumulator, removed v(N+2)
    g = make_genome(fusion_mode="additive")
    try:
        ranked, scores, contrib = run_query(g)
    finally:
        g.close()
    assert ranked == _GOLDEN_RANKED, (
        f"ranking diverged from pre-#202 additive literals:\n"
        f"  expected {_GOLDEN_RANKED}\n  got      {ranked}"
    )
    assert scores == _GOLDEN_SCORES, (
        "final additive scores diverged from pre-#202 literals "
        "(must be bit-identical at default weights):\n"
        f"  expected {_GOLDEN_SCORES}\n  got      {scores}"
    )
    assert contrib == _GOLDEN_CONTRIB, (
        "per-tier contributions diverged from pre-#202 literals:\n"
        f"  expected {_GOLDEN_CONTRIB}\n  got      {contrib}"
    )


def test_every_plumbed_tier_fired_in_golden_corpus():
    """Guard the fixture itself: all 9 knob-bound tiers must appear."""
    _, _, contrib = capture()
    fired = {tier for tiers in contrib.values() for tier in tiers}
    expected = {
        "tag_exact", "tag_prefix", "fts5", "splade",
        "sema_boost", "sema_cold", "lex_anchor", "harmonic",
        "entity_graph",
    }
    missing = expected - fired
    assert not missing, f"corpus no longer exercises tiers: {missing}"


def test_explicit_default_weights_byte_identical():
    """Passing the documented defaults explicitly == passing nothing."""
    g = make_genome(
        # additive-physics pin (#256): tests the legacy accumulator, removed v(N+2)
        fusion_mode="additive",
        fts5_weight=3.0,
        splade_weight=3.5,
        tag_exact_weight=3.0,
        tag_prefix_weight=1.5,
        sema_boost_weight=2.0,
        sema_cold_weight=3.0,
        lex_anchor_weight=1.5,
        harmonic_weight=1.0,
        entity_graph_weight=0.5,
    )
    try:
        ranked, scores, contrib = run_query(g)
    finally:
        g.close()
    assert ranked == _GOLDEN_RANKED
    assert scores == _GOLDEN_SCORES
    assert contrib == _GOLDEN_CONTRIB


# ─── 2. each knob moves exactly its tier ──────────────────────────────


def _contrib_for(weight_kwargs):
    g = make_genome(**weight_kwargs)
    try:
        _, scores, contrib = run_query(g)
    finally:
        g.close()
    return scores, contrib


@pytest.fixture(scope="module")
def default_run():
    return capture()


def test_fts5_weight_scales_cap(default_run):
    """Additive fts5 has no leading coefficient — the knob owns the cap
    (cap = 2.0 × fts5_weight; 2.0 × default 3.0 == legacy literal 6.0).
    gA's raw BM25 magnitude exceeds 0.5, so a 0.25 weight must clamp the
    contribution to exactly 0.5.

    Kept standalone: every other knob below is exercised by *doubling*
    the default weight and asserting the contribution doubles; fts5 is
    exercised by *shrinking* the weight to prove the cap clamps, so it
    does not fit the shared 2x table in ``test_weight_scales_tier``.
    """
    _, _, base = default_run
    assert base["gA"]["fts5"] > 0.5  # fixture guard: raw above the test cap
    _, contrib = _contrib_for({"fts5_weight": 0.25})
    assert contrib["gA"]["fts5"] == 0.5


@pytest.mark.parametrize(
    ("tier", "weight_kwarg", "new_weight", "gene_ids", "exact", "base_exact"),
    [
        pytest.param(
            "tag_exact", "tag_exact_weight", 6.0, ("gA", "gC"), {}, {},
            id="tag_exact",
        ),
        pytest.param(
            "tag_prefix", "tag_prefix_weight", 3.0, ("gB",), {}, {},
            id="tag_prefix",
        ),
        pytest.param(
            # gA saturates the 20.0 normalization: min(30,20) * 7/20 == 7.0
            "splade", "splade_weight", 7.0, ("gA", "gD"), {"gA": 7.0}, {},
            id="splade",
        ),
        pytest.param(
            "sema_boost", "sema_boost_weight", 4.0, ("gA",), {}, {},
            id="sema_boost",
        ),
        pytest.param(
            "sema_cold", "sema_cold_weight", 6.0, ("gE", "gF"), {}, {},
            id="sema_cold",
        ),
        pytest.param(
            # Cap scales with the knob too (cap = 2.0 × weight; 2.0 ×
            # default 1.5 == legacy literal 3.0), so gA's capped
            # contribution (pinned via base_exact) still obeys the
            # plain 2x rule: new cap 6.0 == 2 * 3.0.
            "lex_anchor", "lex_anchor_weight", 3.0, ("gA", "gC"), {},
            {"gA": 3.0}, id="lex_anchor",
        ),
        pytest.param(
            # Cap scales with the knob (cap = 3.0 × weight; 3.0 ×
            # default 1.0 == legacy literal 3.0).
            "harmonic", "harmonic_weight", 2.0, ("gA", "gB"), {}, {},
            id="harmonic",
        ),
        pytest.param(
            # Cap scales with the knob (cap = 4.0 × weight; 4.0 ×
            # default 0.5 == legacy literal 2.0).
            "entity_graph", "entity_graph_weight", 1.0, ("gA", "gB"), {}, {},
            id="entity_graph",
        ),
    ],
)
def test_weight_scales_tier(
    default_run, tier, weight_kwarg, new_weight, gene_ids, exact, base_exact,
):
    """Doubling a tier's weight scales exactly that tier's contribution
    for every doc where it fires, checked against a same-process default
    run (no embedded golden floats). fts5 is excluded — see
    ``test_fts5_weight_scales_cap`` above, which covers its cap-clamp
    shape instead of the plain 2x rule used here.
    """
    _, _, base = default_run
    for gid, expected_base in base_exact.items():
        assert base[gid][tier] == expected_base
    _, contrib = _contrib_for({weight_kwarg: new_weight})
    for gid in gene_ids:
        assert contrib[gid][tier] == 2 * base[gid][tier]
        if gid in exact:
            assert contrib[gid][tier] == exact[gid]


# ─── 3. zero weight kills the tier ────────────────────────────────────


def test_zero_lex_anchor_weight_kills_tier():
    """Kept standalone: the boost gate (> 1.0) never opens at weight 0,
    so the tier is absent from ``contrib`` entirely rather than present
    at a 0.0 contribution — it does not fit the shared
    ``.get(tier, 0.0) == 0.0`` table in ``test_zero_weight_kills_tier``.
    """
    _, contrib = _contrib_for({"lex_anchor_weight": 0.0})
    # boost gate (> 1.0) never opens at weight 0 — tier never fires.
    assert "lex_anchor" not in contrib.get("gA", {})
    assert "lex_anchor" not in contrib.get("gC", {})


@pytest.mark.parametrize(
    ("tier", "weight_kwarg", "gene_ids"),
    [
        pytest.param("tag_exact", "tag_exact_weight", ("gA", "gC"), id="tag_exact"),
        pytest.param("tag_prefix", "tag_prefix_weight", ("gB",), id="tag_prefix"),
        pytest.param("fts5", "fts5_weight", ("gA", "gC"), id="fts5"),
        pytest.param("splade", "splade_weight", ("gA", "gD"), id="splade"),
        pytest.param("sema_boost", "sema_boost_weight", ("gA",), id="sema_boost"),
        pytest.param("sema_cold", "sema_cold_weight", ("gE", "gF"), id="sema_cold"),
        pytest.param("harmonic", "harmonic_weight", ("gA", "gB"), id="harmonic"),
        pytest.param(
            "entity_graph", "entity_graph_weight", ("gA", "gB"), id="entity_graph",
        ),
    ],
)
def test_zero_weight_kills_tier(tier, weight_kwarg, gene_ids):
    """Zeroing a tier's weight drops its contribution to 0.0 for every
    doc where it fires. lex_anchor is excluded — see
    ``test_zero_lex_anchor_weight_kills_tier`` above.
    """
    _, contrib = _contrib_for({weight_kwarg: 0.0})
    for gid in gene_ids:
        assert contrib.get(gid, {}).get(tier, 0.0) == 0.0


# ─── config plumbing: TOML → RetrievalConfig → Genome kwargs ─────────


def test_retrieval_config_defaults_match_additive_literals():
    """The dataclass defaults ARE the legacy additive literals (the
    leading coefficients), so untouched configs stay bit-identical."""
    from cymatix_context.config import RetrievalConfig
    cfg = RetrievalConfig()
    assert cfg.fts5_weight == 3.0          # additive cap = 2.0 × 3.0 = 6.0
    assert cfg.splade_weight == 3.5
    assert cfg.tag_exact_weight == 3.0
    assert cfg.tag_prefix_weight == 1.5
    assert cfg.sema_boost_weight == 2.0    # new knob (#202)
    assert cfg.sema_cold_weight == 3.0
    assert cfg.lex_anchor_weight == 1.5    # additive cap = 2.0 × 1.5 = 3.0
    assert cfg.harmonic_weight == 1.0      # additive cap = 3.0 × 1.0 = 3.0
    assert cfg.entity_graph_weight == 0.5  # additive cap = 4.0 × 0.5 = 2.0


def test_toml_loader_plumbs_sema_boost_weight(tmp_path):
    cfg_file = tmp_path / "helix.toml"
    cfg_file.write_text(
        "[retrieval]\nsema_boost_weight = 9.0\ntag_exact_weight = 4.5\n"
    )
    from cymatix_context.config import load_config
    cfg = load_config(str(cfg_file))
    assert cfg.retrieval.sema_boost_weight == 9.0
    assert cfg.retrieval.tag_exact_weight == 4.5
