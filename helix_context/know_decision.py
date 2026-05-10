"""Stage 6 know/miss discriminator — single source of truth.

Spec: docs/specs/2026-05-08-stage-6-know-miss-blocks.md §5, §4 (escalate),
§8 (gene_id_match beacon).

The /context route used to answer "did we find anything?" by smuggling a
prose marker into ``expressed_context``. Stage 6 elevates that decision
to a structured KnowBlock | MissBlock at the top of the response. This
module is the sole place that decision is computed; both /context and
/context/packet route through ``decide_know_or_miss`` so the contract
stays consistent.

Discriminator order (§5):
    1. health.status == "abstain"     -> MissBlock(reason="abstain")
    2. health.status == "denatured"   -> MissBlock(reason="denatured")
    3. genes_expressed == 0           -> MissBlock(reason="no_promoter_match")
    4. confidence < emit_floor        -> MissBlock(reason="sparse")
    5. else                           -> KnowBlock(...)

# STAGE-7-EXT: Stage 7 inserts three new branches between (3) and (4):
#   - check_superseded(genome, top1)    -> MissBlock(reason="superseded")
#   - revalidate_source(top1) == "stale" -> MissBlock(reason="stale")
#   - cold_tier_only_match               -> MissBlock(reason="cold")
#  All carry refresh_targets, NOT escalate_to. The current 5-step order
#  is preserved; Stage 7 inserts 3a/3b/3c so the prefix stays stable.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional, Sequence

from .accel import extract_query_signals
from .know_calibration import (
    KnowCalibration,
    compute_confidence,
)
from .schemas import (
    ESCALATE_TARGETS,
    KnowBlock,
    MISS_REASONS,
    MissBlock,
)

if TYPE_CHECKING:
    from .schemas import ContextWindow, Gene

log = logging.getLogger("helix.know_decision")


# ─────────────────────────────────────────────────────────────────────
# Code-shape detection (§4 rule 1)
# ─────────────────────────────────────────────────────────────────────

# Identifier dotted access (`module.fn`, `Class.method`). Matches a
# leading word + `.` + word-leader. Pre-compiled at import time.
_CODE_DOT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_]")

# Programming-keyword markers. Whitespace-bounded so "definition" does
# not trip "def".
_CODE_KEYWORDS_RE = re.compile(
    r"(?:^|\s)(?:def|class|import|function|fn|let|var|const)\s",
    re.IGNORECASE,
)

# Filename-extension shapes. Keep tight on purpose: ".py", ".ts" etc.
# at a token boundary; not matching ".com" / ".org" hostnames.
_CODE_FILEEXT_RE = re.compile(
    r"\b\w[\w\-]*\.(?:py|ts|tsx|js|jsx|go|rs|md|toml|yaml|yml|json|sql|sh|c|cpp|h|hpp|java|rb|php|lua|kt|swift|cs|fs|m|r|jl|nim|zig|dart)\b",
    re.IGNORECASE,
)


def _is_code_shaped(query: str) -> bool:
    """Return True if the query looks like code/identifier/path syntax.

    Three independent signals; any one of them flips the gate. Order
    matches §4 rule 1 of the spec.
    """
    if not query:
        return False
    if _CODE_DOT_RE.search(query):
        return True
    if _CODE_KEYWORDS_RE.search(query):
        return True
    if _CODE_FILEEXT_RE.search(query):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────
# Escalation routing (§4)
# ─────────────────────────────────────────────────────────────────────

def _pick_escalation(query: str, reason: str) -> list[str]:
    """Pick the escalate_to list for a MissBlock, ordered by §4 rules.

    First match wins; results are deduped while preserving order.
    Always returns at least one tool (the spec promises non-empty).
    """
    out: list[str] = []

    # Rule 1: code-shaped query — local search and RAG handle it.
    if _is_code_shaped(query):
        out = ["grep", "rag"]
    else:
        # Inspect query signals for entity-shape vs short-ambiguous.
        domains, entities = extract_query_signals(query or "")
        n_entities = len(entities)
        n_tokens = len((query or "").split())

        # Rule 2: entity-shape + no_promoter_match → broaden search.
        if n_entities >= 1 and reason == "no_promoter_match":
            out = ["rag", "web"]
        # Rule 3: denatured corpus → distrust RAG, prefer local + ask.
        elif reason == "denatured":
            out = ["grep", "ask_human"]
        # Rule 4: abstain on a stub query → ambiguity.
        elif reason == "abstain" and n_tokens <= 3:
            out = ["ask_human", "rag"]
        # Rule 5: default fallback.
        else:
            out = ["rag"]

    # Dedup preserving order. Filter to known targets so a future rule
    # change can't smuggle an unknown tool through.
    seen: set[str] = set()
    deduped: list[str] = []
    for tool in out:
        if tool in seen:
            continue
        if tool not in ESCALATE_TARGETS:
            continue
        seen.add(tool)
        deduped.append(tool)
    if not deduped:
        deduped = ["rag"]
    return deduped


# ─────────────────────────────────────────────────────────────────────
# Beacon: gene_id_match (§8)
# ─────────────────────────────────────────────────────────────────────

# Folder-token noise that historically over-fires when path-grain match
# is allowed. Length filter (>= 4) catches most of these, but we still
# block the canonical short-noise list defensively. # STAGE-7-EXT note:
# Stage 7 may want to also gate against a generic stop-list, e.g.
# {"main", "test", "init"} — keep this list narrow today.
_PATH_BEACON_BLOCK = frozenset({"src", "lib", "app", "bin", "var", "tmp", "out"})

# Minimum token length for a path-token (folder) match to count.
_PATH_BEACON_MIN_LEN = 4


def _gene_id_beacon(query: str, top_gene: "Optional[Gene]") -> Optional[str]:
    """Return a token from the query that exactly matches the top-1 gene's
    filename or path tokens; None otherwise.

    Rules (§8):
      1. Exact, case-insensitive equality only. No prefix, no substring.
      2. Filename match wins over path match.
      3. Path-token match requires the matched token's length >= 4, and
         the token must not be in _PATH_BEACON_BLOCK.

    The asymmetric cost of false-positives drives the strictness: a
    wrong beacon makes the frontier model lock in a wrong answer; a
    missing beacon merely lowers KnowBlock.confidence and the agent
    still gets the gene.
    """
    if not query or top_gene is None:
        return None

    source_id = getattr(top_gene, "source_id", None)
    if not source_id:
        return None

    # Lazy import to keep this module from circular-importing genome.
    from .genome import file_tokens, path_tokens

    domains, entities = extract_query_signals(query)
    # Lowercase for case-insensitive equality.
    query_tokens = {t.lower() for t in (domains + entities) if t}
    if not query_tokens:
        return None

    f_toks = file_tokens(source_id)
    p_toks = path_tokens(source_id)

    # Deterministic ordering: longest match first (compound tokens like
    # "context_manager" beat their sub-tokens), then alphabetical for
    # ties. Set-iteration order is non-deterministic across processes;
    # tests would otherwise flake.
    sorted_query = sorted(query_tokens, key=lambda t: (-len(t), t))

    # Filename match takes precedence — most common true-positive shape.
    for q in sorted_query:
        if q in f_toks:
            return q

    # Path-token match: gated by minimum length AND blocklist.
    for q in sorted_query:
        if (
            q in p_toks
            and len(q) >= _PATH_BEACON_MIN_LEN
            and q not in _PATH_BEACON_BLOCK
        ):
            return q

    return None


# ─────────────────────────────────────────────────────────────────────
# Lexical-dense agreement (top-K intersection, signal for confidence)
# ─────────────────────────────────────────────────────────────────────

# Tier-name clustering for ``lexical_dense_agree`` derivation from
# ``genome.last_tier_contributions``. Tier names come from
# helix_context/genome.py — see search results in spec build for the
# canonical list. ``promoter`` is not in either cluster: it's the
# initial filter, not a ranker.
_LEXICAL_TIERS: frozenset[str] = frozenset({
    "tag_exact",
    "tag_prefix",
    "fts5",
    "pki",
})

_DENSE_TIERS: frozenset[str] = frozenset({
    "splade",
    "sema_boost",
    "sema_cold",
})


def _lexical_dense_agree(
    lex_top_k: Sequence[str],
    dense_top_k: Sequence[str],
    *,
    k: int = 3,
) -> bool:
    """True if the lexical and dense rankers agree on at least one of
    their top-K gene_ids.

    ``lex_top_k`` and ``dense_top_k`` may be shorter than ``k`` — we
    intersect on whatever's there.

    Caller (context_manager / context_packet) is responsible for
    pulling the per-ranker top-K out of metadata. This helper exists
    so the contract is one place.
    """
    a = list(lex_top_k or [])[:k]
    b = list(dense_top_k or [])[:k]
    return bool(set(a) & set(b))


def _agree_from_tier_contributions(
    tier_contributions: dict | None,
    *,
    k: int = 3,
) -> bool:
    """Compute lexical_dense_agree from ``genome.last_tier_contributions``.

    ``tier_contributions`` is the dict ``{gene_id: {tier_name: score}}``
    surfaced by Genome._express. We synthesize a per-gene lexical
    score = sum over _LEXICAL_TIERS, and a per-gene dense score = sum
    over _DENSE_TIERS; pick top-K of each by score; check intersection.

    Returns False on any malformed input — the discriminator treats
    this as "no agreement signal", which is the safe direction (won't
    falsely boost KnowBlock.confidence).
    """
    if not tier_contributions or not isinstance(tier_contributions, dict):
        return False
    lex_scores: list[tuple[str, float]] = []
    dense_scores: list[tuple[str, float]] = []
    for gid, tier_map in tier_contributions.items():
        if not isinstance(tier_map, dict):
            continue
        lex = sum(
            float(tier_map.get(t, 0.0))
            for t in _LEXICAL_TIERS
        )
        dense = sum(
            float(tier_map.get(t, 0.0))
            for t in _DENSE_TIERS
        )
        if lex > 0:
            lex_scores.append((gid, lex))
        if dense > 0:
            dense_scores.append((gid, dense))
    lex_scores.sort(key=lambda kv: kv[1], reverse=True)
    dense_scores.sort(key=lambda kv: kv[1], reverse=True)
    lex_top = [gid for gid, _ in lex_scores[:k]]
    dense_top = [gid for gid, _ in dense_scores[:k]]
    return _lexical_dense_agree(lex_top, dense_top, k=k)


# ─────────────────────────────────────────────────────────────────────
# Discriminator
# ─────────────────────────────────────────────────────────────────────

def decide_know_or_miss(
    window: "ContextWindow",
    *,
    query: str,
    top_score: float,
    score_gap: float,
    lexical_dense_agree: bool,
    coordinate_confidence: float,
    top_gene: "Optional[Gene]" = None,
    ratio: Optional[float] = None,
    calibration: Optional[KnowCalibration] = None,
    # Stage 7 (spec §3, §7, §8) additions — all keyword-only so older
    # callers (tests, /context wrapper) keep working without churn.
    freshness_min: Optional[float] = None,
    freshness_status: Optional[str] = None,
    successor_source_id: Optional[str] = None,
    cold_refresh_targets: Optional[Sequence[str]] = None,
) -> KnowBlock | MissBlock:
    """Single source of truth for the know/miss split.

    Args:
        window: the ContextWindow about to be returned to the caller.
        query: the original query string (for escalation + beacon).
        top_score: rank-1 score from the retriever.
        score_gap: top1 - top2 score gap.
        lexical_dense_agree: see _lexical_dense_agree().
        coordinate_confidence: blend of folder + file-grain match.
        top_gene: the rank-1 Gene, used only for the gene_id_match beacon.
        ratio: top/2nd score ratio if pre-computed; defaults derived from
            ``window.metadata["ratio"]``, then 0.0.
        calibration: override; defaults to KnowCalibration() (= helix.toml
            defaults if loaded by the route; module defaults otherwise).
        freshness_min: Stage 7 — min decay across expressed candidates;
            plumbed through to ``compute_confidence`` for β5 application.
            ``None`` is "unknown" (legacy rows / no candidates).
        freshness_status: Stage 7 (spec §5) — caller's freshness verdict
            on top-1, one of "fresh" | "stale" | "missing" | "unknown".
            Only "stale" / "missing" demote to MissBlock(reason="stale").
        successor_source_id: Stage 7 (spec §7) — when not None, the
            top-1 has a Path-A successor; demote to
            MissBlock(reason="superseded") with this as refresh_target.
        cold_refresh_targets: Stage 7 (spec §6) — when non-empty AND
            we'd otherwise emit MissBlock(reason="sparse"), demote to
            MissBlock(reason="cold") with these as refresh_targets.

    Returns either a KnowBlock or a MissBlock — never both, never neither.
    """
    cal = calibration or KnowCalibration()
    health = window.context_health
    status = getattr(health, "status", "unmeasured")
    genes_expressed = int(getattr(health, "genes_expressed", 0) or 0)

    # Resolve ratio default: prefer caller-supplied, then window metadata,
    # then 0.0. Keeps the MissBlock.ratio surface comparable across
    # discriminator branches.
    eff_ratio: float
    if ratio is not None:
        eff_ratio = float(ratio)
    else:
        meta_ratio = (window.metadata or {}).get("ratio") if window.metadata else None
        eff_ratio = float(meta_ratio) if meta_ratio is not None else 0.0

    # Branch 1: ABSTAIN tier fired.
    if status == "abstain":
        return MissBlock(
            reason="abstain",
            top_score=float(top_score),
            ratio=eff_ratio,
            escalate_to=_pick_escalation(query, "abstain"),
        )

    # Branch 2: genome too inconsistent to trust.
    if status == "denatured":
        return MissBlock(
            reason="denatured",
            top_score=float(top_score),
            ratio=eff_ratio,
            escalate_to=_pick_escalation(query, "denatured"),
        )

    # Branch 3: nothing came back — promoter-tag whiff.
    if genes_expressed == 0:
        return MissBlock(
            reason="no_promoter_match",
            top_score=float(top_score),
            ratio=eff_ratio,
            escalate_to=_pick_escalation(query, "no_promoter_match"),
        )

    # Branches 3a/3b/3c — Stage 7 freshness gate (spec §7, §5, §6).
    # Order matters: superseded is the strongest signal (a NEWER gene
    # exists, so the agent can re-aim immediately), stale is next (the
    # source moved, refresh in place), cold runs after the confidence
    # floor below because it competes with reason="sparse".

    # Branch 3a: superseded — the top-1 has a successor row (spec §7,
    # Path A reverse-lookup of ``genes.supersedes``). Refresh target is
    # the successor's source_id.
    if successor_source_id:
        top_source = getattr(top_gene, "source_id", None) if top_gene else None
        # Defensive guard: if Path A returned the top-1's own source_id
        # (shouldn't happen but a malformed row could) we don't demote.
        if successor_source_id and successor_source_id != top_source:
            return MissBlock(
                reason="superseded",
                top_score=float(top_score),
                ratio=eff_ratio,
                escalate_to=[],
                refresh_targets=[str(successor_source_id)],
            )

    # Branch 3b: stale — top-1's underlying source has moved past
    # ``last_verified_at`` (spec §5). Refresh target is the top-1's
    # own source_id; the agent re-reads it and re-calls /context.
    if freshness_status in ("stale", "missing") and top_gene is not None:
        top_source = getattr(top_gene, "source_id", None)
        if top_source:
            return MissBlock(
                reason="stale",
                top_score=float(top_score),
                ratio=eff_ratio,
                escalate_to=[],
                refresh_targets=[str(top_source)],
            )

    # Branch 4: weak retrieval — confidence below floor. Stage 7
    # plumbs freshness_min through so a stale top-K shaves the
    # confidence and may push otherwise borderline retrievals under
    # emit_floor (spec §10).
    confidence = compute_confidence(
        top_score=top_score,
        score_gap=score_gap,
        lexical_dense_agree=lexical_dense_agree,
        coordinate_confidence=coordinate_confidence,
        calibration=cal,
        freshness_min=freshness_min,
    )
    if confidence < cal.emit_floor:
        # Branch 3c: cold — would-be sparse miss but the cold-tier
        # peek surfaced archived hits (spec §6). Promote to "cold"
        # so the agent gets refresh_targets instead of an
        # ask_human/rag escalation.
        if cold_refresh_targets:
            cleaned = [str(t) for t in cold_refresh_targets if t]
            if cleaned:
                return MissBlock(
                    reason="cold",
                    top_score=float(top_score),
                    ratio=eff_ratio,
                    escalate_to=[],
                    refresh_targets=cleaned,
                )
        return MissBlock(
            reason="sparse",
            top_score=float(top_score),
            ratio=eff_ratio,
            escalate_to=_pick_escalation(query, "sparse"),
        )

    # Branch 5: KnowBlock. Stage 7 — soft-stale = top-1 fresh enough
    # to act on (otherwise we'd be in branch 3b) but supporting
    # context (rank 2..K) is stale (freshness_min < 0.5). Carries
    # ``soft_stale=True`` for the route to surface
    # ``recommendation="refresh"`` (spec §9).
    soft_stale = bool(
        freshness_min is not None and freshness_min < 0.5
    )
    return KnowBlock(
        confidence=float(confidence),
        top_score=float(top_score),
        score_gap=float(score_gap),
        lexical_dense_agree=bool(lexical_dense_agree),
        gene_id_match=_gene_id_beacon(query, top_gene),
        coordinate_confidence=float(
            max(0.0, min(1.0, coordinate_confidence))
        ),
        soft_stale=soft_stale,
    )


# Public re-exports the routes consume.
__all__ = [
    "decide_know_or_miss",
    "_pick_escalation",
    "_gene_id_beacon",
    "_is_code_shaped",
    "_lexical_dense_agree",
    "_agree_from_tier_contributions",
    "MISS_REASONS",
]
