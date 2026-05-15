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
            "ORDER BY hits DESC"
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

        # Surface BOTH score families. last_query_scores becomes the
        # IDF-corrected score (which is what downstream tier_logic /
        # bench harness watch). last_tier_contributions is unchanged
        # from the source shard's per-tier breakdown.
        self.last_query_scores = {gid: corrected[gid] for gid in ranked_ids}
        self.last_tier_contributions = {
            gid: merged_tier.get(gid, {}) for gid in ranked_ids
        }
        return [merged[gid] for gid in ranked_ids if gid in merged]

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
