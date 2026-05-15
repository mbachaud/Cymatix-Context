"""ShardRouter — federated query across category shard .db files.

Task 2 of phase-2 sharding (docs/specs/2026-04-17-knowledge store-sharding-plan.md).

Owns main.db (routing + fingerprint_index) and a lazy cache of KnowledgeStore
instances for each category shard. On query, picks candidate shards from
fingerprint_index, fans out the query to each, merges results by score,
returns the top-K.

V1 design decisions:
- **Router fusion**: each shard runs its full query_genes (existing
  fusion math untouched), router merges by raw score. FTS BM25 is
  corpus-local, which means cross-shard scores are not perfectly
  calibrated — accepted for V1, revisit if round-trip validation
  shows regression. (See spec §"Open decisions".)
- **FTS5 placement**: unchanged. Each shard keeps its own FTS; main.db
  has only the fingerprint_index for shard selection.
- **Shard selection**: LIKE scan over JSON-encoded domains/entities in
  fingerprint_index. V2 can replace with a bloom prefilter or inverted
  index once shard count justifies it.

Not in this task:
- Ingest-time routing (Task 6)
- HelixContextManager integration behind HELIX_USE_SHARDS flag (Task 7)
- Cross-shard FTS bloom prefilter (deferred)
"""

from __future__ import annotations

import logging
import math
import os
import threading
from typing import Dict, List, Optional, Tuple

from .genome import Genome
from .schemas import Gene
from .shard_schema import list_shards, open_main_db

log = logging.getLogger(__name__)


# ── Cross-shard BM25 IDF normalization (#118) ────────────────────────
#
# BM25 IDF is corpus-local: each shard computes IDF over its own docs
# only, so a term that's rare globally but common in one shard gets a
# small IDF locally — and the BM25 contribution that flows into
# Genome.last_query_scores is artificially compressed for that shard.
# When the router merges across shards, those compressed scores compete
# unfairly with shards where the term is rare locally and IDF is large.
#
# The shard-correction multiplier ``m_shard`` rescales a shard's
# scores by the ratio of cumulative global IDF to cumulative local IDF
# (weighted by global IDF so rare terms dominate, mirroring BM25's
# own weighting). Clipped to a bounded range to keep extreme single-term
# l_idf≈0 cases from blowing up the merge.
#
# Note this is an APPROXIMATION — BM25's per-document score is a sum
# over (IDF · TF-normalized) terms, not strictly proportional to the
# IDF aggregate. The approximation is good enough for cross-shard
# RANKING (we only need monotone shifts), as confirmed empirically on
# the medium-sharded bench (#118).
IDF_EPS = 1e-3                # floor for local IDF to avoid divide-by-zero
IDF_CLIP_LO = 0.5             # clip range for m_shard
IDF_CLIP_HI = 3.0


# ── Cross-shard co-activation expansion (#120) ───────────────────────
#
# Blob mode pulls docs reachable via 1-hop ``harmonic_links`` edges from
# the BM25 candidate set into the result (``KnowledgeStore._expand_coactivated``
# → ``storage.co_activation.expand_coactivated``). That is how a
# README.md / CLAUDE.md doc surfaces even when its direct keyword match
# is weak — it is linked from a more-specific doc that DOES match.
#
# Sharded mode's ``harmonic_links`` table is intra-shard only, and the
# router has no cross-shard expansion pass, so a gold doc reachable only
# via a harmonic link from a doc in a DIFFERENT shard never surfaces.
# The pass below is the sharded equivalent: after the IDF-corrected RRF
# merge produces a top-K candidate set, each candidate's harmonic links
# are read from its owning shard, the linked gene_ids are resolved to
# their shards via ``fingerprint_index``, and the linked docs are merged
# in at a discounted score so they can be re-ranked into the result.
#
# Mirrors blob's semantics: 1-hop only, a small per-source neighbour cap
# (blob uses 5), and linked docs that are already in the candidate set
# are not re-scored down (highest score wins on collision).
COACT_LINK_BOOST = 0.5        # linked-doc score = boost × source-doc score
COACT_MAX_LINKS_PER_DOC = 5   # 1-hop fan-out cap per candidate (blob: [:5])


# ── Intra-shard doc-type ranking boost (#121) ────────────────────────
#
# Sharded retrieval ranks each shard's candidates with that shard's own
# corpus-local statistics. High-level summary docs (README.md,
# CLAUDE.md, INDEX.md) state answers in conceptual terms; within a
# shard they are routinely out-ranked by more-specific files that
# mention the query terms more densely. The IDF correction (above)
# fixes cross-shard *scale* mismatch but not this *intra-shard rank*
# gap — so a README that holds the answer can sit at intra-shard rank
# ~7 and never clear the cross-shard merge truncation to top-K.
#
# ``DOC_TYPE_BOOST`` is a small multiplicative bump applied to the
# IDF-corrected score of candidates whose source path basename matches
# one of ``DOC_TYPE_BOOST_BASENAMES``. It is deliberately small: it
# only re-orders candidates that are already near-tied. A specific
# implementation file that genuinely out-scores a README by a wide
# margin keeps its lead — a 15% bump cannot overtake a 2× score gap —
# so the boost cannot regress queries whose gold IS a deep file (the
# explicit risk called out in #121's approach (a)).
#
# Applied ONLY on the genuine cross-shard merge path (≥2 shards),
# mirroring the IDF correction's gate. The single-shard fast path stays
# byte-identical to a bare ``Genome.query_docs`` call, and blob mode
# never constructs a ShardRouter at all — so blob behavior is unchanged
# by construction.
DOC_TYPE_BOOST = 1.15
DOC_TYPE_BOOST_BASENAMES = frozenset({
    "readme.md",
    "claude.md",
    "index.md",
})


def _doc_type_boost_for(source_id: Optional[str]) -> float:
    """Return the doc-type score multiplier for a candidate's source path.

    A path whose basename (case-insensitively) is one of
    ``DOC_TYPE_BOOST_BASENAMES`` gets ``DOC_TYPE_BOOST``; everything
    else gets ``1.0`` (identity — no change). Handles both ``/`` and
    ``\\`` separators so Windows-ingested paths match too. A missing
    or empty ``source_id`` gets the identity multiplier.

    Used by :meth:`ShardRouter.query_genes` on the cross-shard merge
    path only.
    """
    if not source_id:
        return 1.0
    # Basename only — split on both separators so a path ingested on
    # Windows (back-slashes) is treated the same as a POSIX path.
    basename = source_id.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    if basename in DOC_TYPE_BOOST_BASENAMES:
        return DOC_TYPE_BOOST
    return 1.0


def _compute_shard_idf_correction(
    query_terms: List[str],
    shard_n: Dict[str, int],
    shard_dfs: Dict[str, Dict[str, int]],
) -> Dict[str, float]:
    """Compute a per-shard score-correction multiplier for IDF mismatch.

    For each query term ``t``::

        local_idf(t, shard)  = log((N_shard - df_shard(t) + 0.5) /
                                   (df_shard(t) + 0.5) + 1.0)
        global_idf(t)        = log((Σ N_shard - Σ df_shard(t) + 0.5) /
                                   (Σ df_shard(t) + 0.5) + 1.0)

    The per-shard multiplier is the global-IDF-weighted mean of
    ``global_idf(t) / max(local_idf(t), IDF_EPS)``, clipped to
    ``[IDF_CLIP_LO, IDF_CLIP_HI]``. A shard where query terms are
    over-represented locally (small local IDF → BM25 score artificially
    compressed) gets ``m_shard > 1`` and its candidates are amplified
    in the cross-shard ranking. A shard where terms are rarer locally
    (large local IDF → BM25 score artificially inflated) gets
    ``m_shard < 1`` and its candidates are deflated.

    Used by ``ShardRouter.query_genes`` to renormalize each shard's
    ``last_query_scores`` before the cross-shard merge.

    Args:
        query_terms: list of query terms (after dedup, before per-shard
            synonym expansion — we use the unexpanded set so multipliers
            are consistent across shards).
        shard_n: ``{shard_name: doc_count}``.
        shard_dfs: ``{shard_name: {term: document_frequency_in_shard}}``.

    Returns:
        ``{shard_name: m_shard}``. Shards absent from ``shard_n`` get
        no entry. A shard whose every query term has df=0 (or whose
        terms have no global signal) gets ``m_shard = 1.0`` — falls
        back to identity transform.
    """
    if not query_terms or not shard_n:
        return {sn: 1.0 for sn in shard_n}

    # Aggregate global N and global df per term across all shards.
    total_n = sum(shard_n.values())
    global_df: Dict[str, int] = {t: 0 for t in query_terms}
    for shard_name, dfs in shard_dfs.items():
        for t, df in dfs.items():
            if t in global_df:
                global_df[t] += int(df)

    if total_n <= 0:
        return {sn: 1.0 for sn in shard_n}

    # Compute global IDF per term using BM25's standard "+0.5" smoothing.
    # A term with df=0 globally has no signal across the corpus — give
    # it the maximum smoothed IDF (treat it as a hapax) so it doesn't
    # crash the weighting; but in practice df=0 means no shard returned
    # candidates on it, so it never dominates the average.
    global_idf: Dict[str, float] = {}
    for t in query_terms:
        df_g = global_df[t]
        # Smoothed BM25 IDF: log((N - df + 0.5) / (df + 0.5) + 1)
        global_idf[t] = math.log((total_n - df_g + 0.5) / (df_g + 0.5) + 1.0)

    # Per-shard multiplier: weighted mean of global_idf / local_idf,
    # weighted by global_idf so the term with strongest cross-corpus
    # discrimination dominates. Clipped to a bounded range.
    multipliers: Dict[str, float] = {}
    for shard_name, n_shard in shard_n.items():
        dfs = shard_dfs.get(shard_name, {})
        # Only consider terms that actually fire in this shard.
        # A term with df=0 in this shard contributed nothing to BM25
        # scores from this shard, so it shouldn't drive the correction.
        weighted_sum = 0.0
        weight_total = 0.0
        for t in query_terms:
            df = int(dfs.get(t, 0))
            if df <= 0:
                continue
            # BM25-style smoothed local IDF
            local_idf = math.log(
                (n_shard - df + 0.5) / (df + 0.5) + 1.0
            )
            local_idf_safe = max(local_idf, IDF_EPS)
            g_idf = global_idf.get(t, 0.0)
            # Skip terms with no global discrimination signal.
            if g_idf <= 0.0:
                continue
            ratio = g_idf / local_idf_safe
            # Weight rare-global-term ratios more heavily — these
            # are the terms BM25 considers most discriminative.
            weighted_sum += g_idf * ratio
            weight_total += g_idf

        if weight_total <= 0.0:
            # No participating terms; identity transform.
            multipliers[shard_name] = 1.0
            continue

        m = weighted_sum / weight_total
        # Clip extreme values. Without clipping, a single very-common
        # local term (l_idf near zero) can produce m_shard >> 100×,
        # which would let the unrelated co-shard docs steamroll the
        # merge. The clip is a stability knob; tune in helix.toml if
        # later bench data wants different bounds.
        multipliers[shard_name] = max(IDF_CLIP_LO, min(IDF_CLIP_HI, m))

    return multipliers


class ShardRouter:
    """Routes queries across category shard .db files.

    Owns:
        - main.db connection (fingerprint_index + shards + identity)
        - lazy dict of KnowledgeStore instances keyed by shard_name

    Exposes a subset of KnowledgeStore's API (query_genes for now; ingest
    lands in Task 6).
    """

    def __init__(self, main_path: str, **genome_kwargs):
        """Open main.db. Shard KnowledgeStores open lazily on first access.

        genome_kwargs are forwarded to each KnowledgeStore on lazy-open — keep
        them identical to what HelixContextManager passes when
        constructing a solo KnowledgeStore, so sharded + non-sharded return
        identical tiers.
        """
        self.main_path = main_path
        self.main_conn = open_main_db(main_path)
        self._genome_kwargs = genome_kwargs
        self._shards: Dict[str, Genome] = {}

        # Retrieval introspection — mirrors KnowledgeStore's interface so
        # callers can use either interchangeably.
        self.last_query_scores: Dict[str, float] = {}
        self.last_tier_contributions: Dict[str, Dict[str, float]] = {}
        # Guards last_query_scores / last_tier_contributions writes from
        # racing concurrent /context calls. KnowledgeStore has the same
        # lock on the same attributes (see knowledge_store.py:479) — the
        # router needs to match that contract so the adapter snapshot in
        # ShardedGenomeAdapter.query_docs sees a consistent pair.
        self._last_query_scores_lock = threading.Lock()

    # ── Shard lifecycle ─────────────────────────────────────────────

    def _open_shard(self, shard_name: str) -> Genome:
        """Lazy-open a KnowledgeStore against the shard .db. Cached."""
        if shard_name not in self._shards:
            row = self.main_conn.execute(
                "SELECT path FROM shards WHERE shard_name = ? AND health = 'ok'",
                (shard_name,),
            ).fetchone()
            if row is None:
                raise ValueError(f"shard not registered: {shard_name}")
            path = row["path"]
            log.info("opening shard %s at %s", shard_name, path)
            self._shards[shard_name] = Genome(path, **self._genome_kwargs)
        return self._shards[shard_name]

    def known_shards(self, category: Optional[str] = None) -> List[str]:
        """Shard names registered in main.db, optionally filtered."""
        return [r["shard_name"] for r in list_shards(self.main_conn, category)]

    # ── Routing ─────────────────────────────────────────────────────

    def route(self, domains: List[str], entities: List[str]) -> List[str]:
        """Return shard names likely to contain matches for the query.

        V1: LIKE scan against fingerprint_index.domains/entities.
        Match is loose — if any query term appears in any shard's
        fingerprint domains or entities, that shard is in. Ordered
        by match count (most matches first).

        Empty query → return every healthy shard (preserves the
        fallback path callers rely on for "return something").
        """
        terms = [t.lower() for t in (domains + entities) if t]
        if not terms:
            return self.known_shards()

        # Match either domains or entities JSON containing the term
        # quoted exactly: ["auth"] or ["JWT"]. Simple substring match
        # on JSON-encoded lists.
        like_clauses = []
        params: list = []
        for t in terms:
            like_clauses.append("(LOWER(domains) LIKE ? OR LOWER(entities) LIKE ?)")
            needle = f'%"{t}"%'
            params.extend([needle, needle])

        sql = (
            "SELECT shard_name, COUNT(*) AS hits "
            "FROM fingerprint_index "
            f"WHERE {' OR '.join(like_clauses)} "
            "GROUP BY shard_name "
            # shard_name ASC tiebreak: without it, two shards with equal
            # hit counts come back in whatever insertion/index order
            # SQLite happens to pick, which differs across rebuilds and
            # WAL checkpoints. Deterministic fan-out order matters because
            # the merge in query_genes is "first-shard-wins on ties".
            "ORDER BY hits DESC, shard_name ASC"
        )
        rows = self.main_conn.execute(sql, params).fetchall()
        return [r["shard_name"] for r in rows]

    # ── Query fan-out ───────────────────────────────────────────────

    def query_genes(
        self,
        domains: List[str],
        entities: List[str],
        max_genes: int = 8,
        party_id: Optional[str] = None,
        read_only: bool = False,
        **kwargs,
    ) -> List[Gene]:
        """Fan query across routed shards and merge with cross-shard IDF correction.

        Mirrors Genome.query_genes signature so callers can swap
        without branching. Extra kwargs (``use_harmonic``, ``cwola_weight``,
        etc.) forward verbatim to each shard's ``Genome.query_genes``; any
        kwarg a given shard rejects is silently dropped for that shard.

        Merge strategy (#118 fix):
            1. Fan candidates out via each shard's ``query_docs``.
            2. Probe each shard's FTS5 index for ``N_shard`` and per-term
               ``df_shard(t)`` over the query terms.
            3. Compute a per-shard correction multiplier
               (see :func:`_compute_shard_idf_correction`) that rescales
               each shard's scores by the global-vs-local IDF mismatch.
            4. Sort candidates by IDF-corrected score, breaking ties
               with RRF (rank-fused) so single-shard rank-1 tie chains
               still get a deterministic ordering.

        For a single-shard query (the only-shard fallback path), the
        correction multiplier collapses to identity and the routing
        behaves as if scores came directly from that shard.
        """
        shard_names = self.route(domains, entities)
        if not shard_names:
            with self._last_query_scores_lock:
                self.last_query_scores = {}
                self.last_tier_contributions = {}
            return []

        from .retrieval.fusion import Fuser, DEFAULT_RRF_K

        # Materialised candidates by gene_id (last writer wins on
        # content-hash collisions across shards — same content, same id).
        merged: Dict[str, Gene] = {}
        # Carry per-tier contributions through the merge so downstream
        # introspection (cwola log, activation profile in /context) still
        # sees them. Keep the contributions from whichever shard ranked
        # the doc best.
        merged_tier: Dict[str, Dict[str, float]] = {}
        merged_tier_source: Dict[str, int] = {}

        # Per-shard data we need for IDF normalization:
        #   - which gene_ids came from each shard, with raw scores
        #   - each shard's N (FTS doc count)
        #   - each shard's per-term df over the query terms
        # Collect these as we fan out, then compute the multiplier after.
        shard_ranked: Dict[str, List[Tuple[str, float]]] = {}
        shard_n: Dict[str, int] = {}
        shard_dfs: Dict[str, Dict[str, int]] = {}

        # Build the set of query terms used for IDF probing. Deduplicate
        # case-insensitively and drop very short terms (mirrors the
        # ``len(t) > 2`` filter used elsewhere for FTS5 queries).
        idf_terms: List[str] = []
        _idf_seen: set[str] = set()
        for t in (domains or []) + (entities or []):
            if not t:
                continue
            k = t.lower()
            if k in _idf_seen or len(k) <= 2:
                continue
            _idf_seen.add(k)
            idf_terms.append(k)

        fuser = Fuser(k=DEFAULT_RRF_K)

        # Per-shard fetch depth: request 2x more from each shard than the
        # caller's ``max_genes``. Genome.query_docs already returns
        # ``max_genes * 2`` candidates internally; doubling the routed
        # value here gives each shard 4x candidate depth (=4× max_genes
        # candidates), so a doc at intra-shard rank ~30 reaches the
        # cross-shard merge after IDF correction. Without this lift,
        # cross-shard re-ranking sees only each shard's top ~24 — and a
        # gold doc that's intra-shard rank 25+ never enters the merge.
        # The price is per-shard query cost; in benchmarks the medium
        # fixture's 6 shards stay well under 1s with this fan-out.
        per_shard_fetch = max(int(max_genes), int(max_genes) * 2)

        for shard_name in shard_names:
            try:
                shard = self._open_shard(shard_name)
            except Exception:
                log.warning("shard %s failed to open; skipping", shard_name, exc_info=True)
                continue

            try:
                genes = shard.query_docs(
                    domains=domains,
                    entities=entities,
                    max_genes=per_shard_fetch,
                    party_id=party_id,
                    read_only=read_only,
                    **kwargs,
                )
            except TypeError:
                # Kwarg mismatch with an older KnowledgeStore schema — fall back to
                # the minimal signature so a stale shard still contributes.
                log.warning(
                    "shard %s rejected kwargs %s; falling back to base signature",
                    shard_name, list(kwargs.keys()),
                )
                try:
                    genes = shard.query_docs(
                        domains=domains,
                        entities=entities,
                        max_genes=per_shard_fetch,
                        party_id=party_id,
                        read_only=read_only,
                    )
                except Exception:
                    log.warning("shard %s query failed; skipping", shard_name, exc_info=True)
                    continue
            except Exception:
                log.warning("shard %s query failed; skipping", shard_name, exc_info=True)
                continue

            # IDF probe: cheap COUNT-over-MATCH queries against this
            # shard's FTS5. Soft-fails to empty dict — that shard then
            # gets m_shard = 1.0 (identity) so its scores pass through
            # uncorrected.
            try:
                shard_n[shard_name] = int(shard.fts_doc_count())
                shard_dfs[shard_name] = dict(shard.term_doc_frequencies(idf_terms))
            except Exception:
                log.warning(
                    "shard %s IDF probe failed; falling back to identity correction",
                    shard_name, exc_info=True,
                )
                shard_n[shard_name] = 0
                shard_dfs[shard_name] = {}

            shard_scores = shard.last_query_scores
            ranked_for_shard: List[Tuple[str, float]] = []
            for doc in genes:
                raw_score = float(shard_scores.get(doc.gene_id, 0.0))
                ranked_for_shard.append((doc.gene_id, raw_score))
                if doc.gene_id not in merged:
                    merged[doc.gene_id] = doc
                if (
                    doc.gene_id not in merged_tier_source
                    or raw_score > merged_tier_source[doc.gene_id]
                ):
                    merged_tier[doc.gene_id] = dict(
                        shard.last_tier_contributions.get(doc.gene_id, {})
                    )
                    merged_tier_source[doc.gene_id] = raw_score

            shard_ranked[shard_name] = ranked_for_shard
            # Still feed the Fuser — RRF acts as the tiebreaker on the
            # corrected-score sort.
            fuser.add_tier(shard_name, ranked_for_shard, weight=1.0)

        # ── Cross-shard IDF correction ──────────────────────────────
        # Skip when only one shard participated — the corpus IS the
        # shard, so its local IDF is already global. This also keeps
        # the single-shard fallback path byte-identical to a bare
        # ``Genome.query_docs`` call composed under a router.
        if len(shard_ranked) <= 1 or not idf_terms:
            multipliers = {sn: 1.0 for sn in shard_ranked}
        else:
            multipliers = _compute_shard_idf_correction(
                idf_terms, shard_n, shard_dfs,
            )

        # Apply correction. A doc that appears in multiple shards (rare —
        # would require content-hash collision across shards) keeps its
        # highest corrected score so cross-shard duplication can't penalize.
        corrected: Dict[str, float] = {}
        for shard_name, pairs in shard_ranked.items():
            m = float(multipliers.get(shard_name, 1.0))
            for gid, raw in pairs:
                adj = raw * m
                if gid not in corrected or adj > corrected[gid]:
                    corrected[gid] = adj

        # ── Intra-shard doc-type boost (#121) ───────────────────────
        # Lift README/CLAUDE/INDEX summary docs by a small multiplier
        # so a high-level doc that holds the answer but lost the
        # intra-shard keyword-density race can still clear the merge
        # truncation. Gated to the genuine cross-shard merge (≥2
        # shards) so the single-shard fast path stays byte-identical;
        # see DOC_TYPE_BOOST commentary above. The multiplier is small
        # enough that it only re-orders near-tied candidates — a deep
        # implementation file that genuinely out-scores a README keeps
        # its lead, so no regression on implementation-file queries.
        if len(shard_ranked) > 1:
            for gid in corrected:
                doc = merged.get(gid)
                if doc is None:
                    continue
                boost = _doc_type_boost_for(getattr(doc, "source_id", None))
                if boost != 1.0:
                    corrected[gid] *= boost

        rrf_all = fuser.all_scores()

        # Primary sort key: corrected score desc.
        # Secondary tiebreaker: RRF score desc (stable for ties in
        # the corrected score, e.g., 6 shards' rank-1 hitting the cap).
        # Tertiary: gene_id asc (deterministic).
        ranked_ids_full = sorted(
            corrected,
            key=lambda gid: (
                -corrected.get(gid, 0.0),
                -rrf_all.get(gid, 0.0),
                gid,
            ),
        )
        # Match Genome.query_docs contract: return up to ``max_genes * 2``
        # candidates so the downstream assembler (splice + co-activation +
        # freshness gate) has the same depth to work with as a blob-mode
        # genome would. Without this, the router's max_genes cap deletes
        # the deeper candidates the assembler relies on for co-activated
        # pull-forward. Mirrors ``Genome.query_docs`` line ~2288 which
        # truncates to ``limit = max_genes * 2``.
        limit = max(1, int(max_genes) * 2)
        ranked_ids = ranked_ids_full[:limit]

        # ── Cross-shard co-activation expansion (#120) ──────────────
        # Pull docs reachable via 1-hop harmonic links from the top-K
        # candidates but living in a DIFFERENT shard. Mutates ``merged``
        # / ``merged_tier`` / ``corrected`` in place and returns a
        # re-sorted, re-truncated id list. Soft-fails to ``ranked_ids``
        # unchanged so a graph hiccup never perturbs the merge result.
        try:
            ranked_ids = self._expand_cross_shard_coactivation(
                ranked_ids=ranked_ids,
                shard_ranked=shard_ranked,
                corrected=corrected,
                rrf_all=rrf_all,
                merged=merged,
                merged_tier=merged_tier,
                limit=limit,
            )
        except Exception:
            log.warning(
                "cross-shard co-activation expansion failed; "
                "falling back to un-expanded merge",
                exc_info=True,
            )

        # Surface BOTH score families. last_query_scores becomes the
        # IDF-corrected score (which is what downstream tier_logic /
        # bench harness watch). last_tier_contributions is unchanged
        # from the source shard's per-tier breakdown.
        with self._last_query_scores_lock:
            self.last_query_scores = {gid: corrected[gid] for gid in ranked_ids}
            self.last_tier_contributions = {
                gid: merged_tier.get(gid, {}) for gid in ranked_ids
            }
        return [merged[gid] for gid in ranked_ids if gid in merged]

    # ── Cross-shard co-activation expansion (#120) ──────────────────

    def _expand_cross_shard_coactivation(
        self,
        *,
        ranked_ids: List[str],
        shard_ranked: Dict[str, List[Tuple[str, float]]],
        corrected: Dict[str, float],
        rrf_all: Dict[str, float],
        merged: Dict[str, Gene],
        merged_tier: Dict[str, Dict[str, float]],
        limit: int,
    ) -> List[str]:
        """Pull 1-hop harmonic-link neighbours from across shards.

        Sharded equivalent of blob's ``_expand_coactivated``: a gold doc
        whose only path into the result is a harmonic link from a doc in
        a *different* shard never surfaces, because each shard's
        ``harmonic_links`` table is intra-shard only and the merge has no
        graph pass. This method closes that gap.

        For each candidate in ``ranked_ids`` (the post-merge top-K):

        1. Read its 1-hop ``harmonic_links`` neighbours from the DB of
           the shard that ranked it best (``fetch_forward_neighbors`` —
           ``gene_id_a = candidate``, mirroring blob's edge direction).
        2. Resolve each linked gene_id to its owning shard via
           ``fingerprint_index`` (lexicographically-min ``shard_name``
           wins on cross-shard content-hash duplicates — same tie-break
           contract as ``get_citation_rows``).
        3. Score the linked doc at ``COACT_LINK_BOOST × source-doc
           corrected score`` and merge it into ``corrected`` (highest
           score wins — a linked doc already in the candidate set is
           never re-scored *down*).
        4. Materialise newly-introduced docs into ``merged`` from their
           owning shard, re-sort the union by the same key
           ``query_genes`` uses, and truncate to ``limit``.

        ``merged`` / ``merged_tier`` / ``corrected`` are mutated in
        place. Returns the re-sorted, re-truncated id list. On any error
        the caller falls back to the un-expanded ``ranked_ids``.

        Blob-mode parity: this method is reached only from the sharded
        ``ShardRouter.query_genes`` path — blob's ``KnowledgeStore``
        retrieval never calls it, so blob behaviour is unchanged.
        """
        if not ranked_ids or not shard_ranked:
            return ranked_ids

        from .retrieval.expand import fetch_forward_neighbors

        # gene_id → owning shard. A doc can appear in several shards on a
        # content-hash collision; pick the lexicographically-min shard so
        # the resolution is deterministic (matches get_citation_rows).
        gid_to_shard: Dict[str, str] = {}
        for shard_name, pairs in shard_ranked.items():
            for gid, _score in pairs:
                cur = gid_to_shard.get(gid)
                if cur is None or shard_name < cur:
                    gid_to_shard[gid] = shard_name

        # Candidate set we expand from — the post-merge top-K only, so
        # the fan-out cost is bounded by ``limit`` 1-hop reads.
        existing: set[str] = set(ranked_ids)

        # linked gene_id → best discounted score proposed for it.
        linked_scores: Dict[str, float] = {}
        for src_gid in ranked_ids:
            src_shard = gid_to_shard.get(src_gid)
            if src_shard is None:
                continue
            try:
                shard = self._open_shard(src_shard)
            except Exception:
                log.debug(
                    "co-activation: shard %s failed to open for %s",
                    src_shard, src_gid, exc_info=True,
                )
                continue
            try:
                neighbors = fetch_forward_neighbors(
                    shard.conn, src_gid, k=COACT_MAX_LINKS_PER_DOC,
                )
            except Exception:
                log.debug(
                    "co-activation: harmonic_links read failed for %s",
                    src_gid, exc_info=True,
                )
                continue
            src_score = float(corrected.get(src_gid, 0.0))
            if src_score <= 0.0:
                continue
            boosted = src_score * COACT_LINK_BOOST
            for linked_gid, _weight in neighbors:
                # Already a candidate — never re-score it down; the
                # existing (higher) merge score stands.
                if linked_gid in existing:
                    continue
                prev = linked_scores.get(linked_gid)
                if prev is None or boosted > prev:
                    linked_scores[linked_gid] = boosted

        if not linked_scores:
            return ranked_ids

        # Resolve linked gene_ids to their owning shards via main.db's
        # fingerprint_index. A linked doc with no fingerprint row (e.g.
        # an unsharded / dangling reference) is skipped.
        linked_ids = list(linked_scores.keys())
        id_ph = ",".join("?" * len(linked_ids))
        try:
            fp_rows = self.main_conn.execute(
                f"SELECT gene_id, shard_name FROM fingerprint_index "
                f"WHERE gene_id IN ({id_ph})",
                linked_ids,
            ).fetchall()
        except Exception:
            log.debug(
                "co-activation: fingerprint_index resolve failed",
                exc_info=True,
            )
            return ranked_ids

        linked_gid_to_shard: Dict[str, str] = {}
        for r in fp_rows:
            gid = r["gene_id"]
            sn = r["shard_name"]
            cur = linked_gid_to_shard.get(gid)
            if cur is None or sn < cur:
                linked_gid_to_shard[gid] = sn

        # Materialise the linked docs from their owning shards. Group by
        # shard so each shard DB is queried once.
        by_shard: Dict[str, List[str]] = {}
        for gid, sn in linked_gid_to_shard.items():
            by_shard.setdefault(sn, []).append(gid)

        promoted: set[str] = set()
        for shard_name, gids in by_shard.items():
            try:
                shard = self._open_shard(shard_name)
            except Exception:
                log.debug(
                    "co-activation: shard %s failed to open for fetch",
                    shard_name, exc_info=True,
                )
                continue
            for gid in gids:
                if gid in merged:
                    # Already materialised by the main fan-out — still
                    # eligible to be re-ranked in via its linked score.
                    promoted.add(gid)
                    continue
                try:
                    doc = shard.get_doc(gid)
                except Exception:
                    log.debug(
                        "co-activation: get_doc(%s) failed", gid,
                        exc_info=True,
                    )
                    doc = None
                if doc is None:
                    continue
                merged[gid] = doc
                merged_tier.setdefault(gid, {})["co_activation"] = (
                    linked_scores[gid]
                )
                promoted.add(gid)

        if not promoted:
            return ranked_ids

        # Fold the discounted scores into ``corrected`` (highest wins),
        # then re-sort the union with the SAME key query_genes uses and
        # re-truncate to ``limit``.
        for gid in promoted:
            adj = linked_scores.get(gid, 0.0)
            if gid not in corrected or adj > corrected[gid]:
                corrected[gid] = adj

        union_ids = list(dict.fromkeys(list(ranked_ids) + sorted(promoted)))
        union_ids.sort(
            key=lambda gid: (
                -corrected.get(gid, 0.0),
                -rrf_all.get(gid, 0.0),
                gid,
            ),
        )
        return union_ids[:limit]

    # ── Lifecycle ───────────────────────────────────────────────────

    def close(self) -> None:
        """Close all lazy-opened shard knowledge stores + main.db."""
        for shard_name, genome in self._shards.items():
            try:
                genome.conn.close()
            except Exception:
                log.warning("failed to close shard %s", shard_name, exc_info=True)
            try:
                if getattr(genome, "_reader", None):
                    genome._reader.close()
            except Exception:
                pass
        self._shards.clear()
        try:
            self.main_conn.close()
        except Exception:
            pass


# ── Feature-flag helper ──────────────────────────────────────────────


def use_shards_enabled() -> bool:
    """Read HELIX_USE_SHARDS env flag. Default OFF until Task 8 cutover."""
    return os.environ.get("HELIX_USE_SHARDS", "").strip() in ("1", "true", "yes", "on")
