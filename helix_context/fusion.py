"""Reciprocal Rank Fusion (RRF) accumulator.

Spec: ``docs/specs/2026-05-08-stage-3-rrf-fusion.md`` §4-§5.

Replaces the additive ``gene_scores[gid] += tier_score`` accumulator in
``Genome.query_genes()`` with rank-level fusion (Cormack 2009).

Per-tier scores are non-commensurate — FTS5 negative-bm25, BGE cosine
∈[-1,1], tags exact ∈ {0, 3.0}, harmonic ∈ {0..3}, filename_anchor
4.0 per match — so summing them lets one over-scaled tier dominate. RRF
operates on ranks, which are scale-invariant.

For each document ``d`` and each participating tier ``t``::

    score(d) = Σ_{t ∈ tiers}  weight_t · 1 / (k + rank_t(d))

where ``rank_t(d)`` is ``d``'s 1-based rank in tier ``t``'s
descending-score-ordered list. Documents not present in tier ``t`` contribute
0. ``k = 60`` is the Cormack default.

Ties: stable rank-by-(score desc, gene_id asc). Documents with bitwise-equal
float scores get adjacent ranks (NOT shared rank — this keeps RRF
monotone in input order and makes the test plan §10 deterministic).

This module is a **pure data structure**. It has no SQL, no KnowledgeStore
coupling, no telemetry side effects. The caller (``genome.py``) drives
it: each tier site computes its raw scores, calls ``add_tier(...)``, and
at the end calls ``top_k(limit)`` for the fused ranking.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence, Tuple


__all__ = ["Fuser", "DEFAULT_RRF_K"]


# Cormack 2009 default. The original IR paper used 60 across TREC tracks
# and showed insensitivity in [10, 100]. Surfaced as ``[retrieval] rrf_k``
# in helix.toml for operators who want to tune.
DEFAULT_RRF_K: int = 60


@dataclass
class Fuser:
    """Reciprocal Rank Fusion accumulator.

    Lifecycle::

        fuser = Fuser(k=60)
        fuser.add_tier("fts5", [(gid, score), ...], weight=3.0)
        fuser.add_tier("dense", [(gid, cosine), ...], weight=1.0)
        ...
        ranked = fuser.top_k(limit=12)   # [(gid, fused_score), ...]

    Tiers may be added in any order. Documents appearing in multiple tiers
    accumulate independent contributions per spec §8 ("tied-tier
    semantics: independent contribution per tier; no max, no min, no
    per-document cap").

    A tier with ``weight == 0.0`` is silently a no-op — useful for the
    ``test_rrf_with_zero_weights_disables_tier`` case in §10.
    """

    k: int = DEFAULT_RRF_K

    # gene_id -> accumulated fused score
    _scores: Dict[str, float] = field(default_factory=dict)

    # Tiers seen so far (preserved for telemetry / debug introspection;
    # NOT used in the fusion math). Each entry is the tier name, in
    # add_tier call order.
    _tiers_seen: List[str] = field(default_factory=list)

    def add_tier(
        self,
        tier_name: str,
        ranked_ids: Sequence[Tuple[str, float]],
        weight: float = 1.0,
    ) -> None:
        """Register a tier's ranked output.

        ``ranked_ids`` is an iterable of ``(gene_id, raw_score)`` pairs.
        It does NOT need to be pre-sorted — the Fuser sorts internally
        by ``(score desc, gene_id asc)`` to guarantee deterministic ranks
        even when callers feed dict-iteration order. This matters because
        Python dict iteration is insertion-ordered, but two callers
        building the same dict in different orders would otherwise
        produce different RRF ranks.

        Documents with bitwise-equal float scores get adjacent ranks broken
        by ``gene_id`` ascending (spec §4: "NOT shared rank — keeps RRF
        monotone in input order"). Tied scores DO get different ranks
        and therefore different ``1/(k+rank)`` contributions. This is
        intentional: shared ranks would create non-monotone behavior on
        zero-weight tier swaps.

        ``weight == 0.0`` short-circuits — the tier contributes nothing,
        which is the contract for ``test_rrf_with_zero_weights_disables_tier``.

        ``ranked_ids`` may be empty (tier didn't fire); this is a no-op.
        """
        if weight == 0.0:
            # Per §7: zero-weight is the operator's "disable this tier"
            # knob. Recording the tier as seen would falsely show it on
            # debug introspection, so skip the bookkeeping too.
            return
        if not ranked_ids:
            return

        # Sort by (-score, gene_id) for stable, deterministic ranks.
        # Negating the score flips the natural ascending sort. Tying on
        # gene_id ascending breaks bitwise-equal scores deterministically.
        # We materialize to a list because ``ranked_ids`` may be a
        # generator.
        sorted_pairs = sorted(
            ranked_ids,
            key=lambda pair: (-float(pair[1]), str(pair[0])),
        )

        k = self.k
        for rank, (gid, _score) in enumerate(sorted_pairs, start=1):
            # 1-based rank per Cormack 2009. RRF score contribution for
            # this document from this tier:
            contribution = weight / (k + rank)
            self._scores[gid] = self._scores.get(gid, 0.0) + contribution

        self._tiers_seen.append(tier_name)

    def top_k(self, limit: int) -> List[Tuple[str, float]]:
        """Return the top-``limit`` documents by fused score.

        Output: ``[(gene_id, fused_score), ...]`` sorted descending by
        fused score, ties broken by ``gene_id`` ascending (matches the
        ``add_tier`` tie-break so the global ordering is consistent).

        ``limit <= 0`` returns ``[]``. ``limit > len(scores)`` returns
        all scored documents.
        """
        if limit <= 0:
            return []
        if not self._scores:
            return []

        # Sort by (-fused_score, gene_id) so highest-score wins, ties
        # broken deterministically by gene_id ascending.
        ordered = sorted(
            self._scores.items(),
            key=lambda kv: (-kv[1], kv[0]),
        )
        return ordered[:limit]

    def all_scores(self) -> Dict[str, float]:
        """Return a copy of the full fused-score map.

        Used by genome.py to populate ``last_query_scores`` under the
        ``fusion_mode == "rrf"`` branch (spec §5). Returned dict is a
        copy — mutating it does not affect the Fuser.
        """
        return dict(self._scores)

    def tiers_seen(self) -> List[str]:
        """Return the ordered list of tier names that contributed.

        Used by tests (``test_rrf_telemetry_emits_raw_pre_rrf``) and the
        debug log line in ``query_genes``. Read-only snapshot.
        """
        return list(self._tiers_seen)

    def __len__(self) -> int:
        """Number of distinct documents scored across all tiers."""
        return len(self._scores)

    def __contains__(self, gene_id: str) -> bool:
        """Check whether a document received any tier contribution."""
        return gene_id in self._scores


def rank_by_score(
    pairs: Iterable[Tuple[str, float]],
) -> List[Tuple[str, int]]:
    """Helper: return ``[(gene_id, rank), ...]`` sorted by score desc.

    Convenience for callers that want to inspect ranks directly (e.g.
    benchmarks, eval harnesses). Same tie-break as ``Fuser.add_tier``
    so the ranks line up.

    Not used by ``Fuser`` itself — kept here so the rank-computation
    rule lives in one place.
    """
    sorted_pairs = sorted(
        pairs,
        key=lambda pair: (-float(pair[1]), str(pair[0])),
    )
    return [(gid, idx) for idx, (gid, _) in enumerate(sorted_pairs, start=1)]
