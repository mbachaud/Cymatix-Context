"""Agent-safe context packet builder.

Additive surface for the Helix index work: it reuses existing tags
retrieval, then labels results by freshness/authority so callers can
decide whether to trust, reread, or refresh before acting.
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import PurePath
from typing import Optional

from .accel import extract_query_signals
from .genome import file_tokens, path_tokens
from .schemas import ContextItem, ContextPacket, Gene, RefreshTarget

_HALF_LIFE_SECONDS = {
    "stable": 7 * 24 * 60 * 60,
    "medium": 12 * 60 * 60,
    "hot": 15 * 60,
}

_AUTHORITY_WEIGHTS = {
    "primary": 1.0,
    "derived": 0.75,
    "inferred": 0.45,
}

_TASK_RISK = {
    "plan": 0.30,
    "explain": 0.45,
    "review": 0.60,
    "edit": 0.85,
    "debug": 0.90,
    "ops": 1.00,
    "quote": 0.95,
}

_HIGH_RISK_TASKS = {"edit", "debug", "ops", "quote"}
_LITERAL_SOURCE_KINDS = {"code", "config", "db", "benchmark", "tool_output"}
_DOC_LIKE_SOURCE_KINDS = {
    "doc", "log", "session_note", "html", "pdf",
    "office", "spreadsheet", "transcript",
}
_MEDIA_SOURCE_KINDS = {"image", "audio", "video"}


def _row_value(row: sqlite3.Row | None, key: str):
    if row is None:
        return None
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _lookup_source_row(
    main_conn: sqlite3.Connection | None,
    gene_id: str,
) -> sqlite3.Row | None:
    if main_conn is None:
        return None
    try:
        return main_conn.execute(
            "SELECT * FROM source_index WHERE gene_id = ?",
            (gene_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def _effective_meta(gene: Gene, source_row: sqlite3.Row | None) -> dict:
    observed_at = _row_value(source_row, "observed_at")
    if observed_at is None:
        observed_at = gene.observed_at
    if observed_at is None and gene.epigenetics is not None:
        observed_at = gene.epigenetics.created_at

    last_verified_at = _row_value(source_row, "last_verified_at")
    if last_verified_at is None:
        last_verified_at = gene.last_verified_at
    if last_verified_at is None:
        last_verified_at = observed_at

    return {
        "source_id": _row_value(source_row, "source_id") or gene.source_id,
        "repo_root": _row_value(source_row, "repo_root") or gene.repo_root,
        "source_kind": _row_value(source_row, "source_kind") or gene.source_kind,
        "observed_at": observed_at,
        "mtime": _row_value(source_row, "mtime") or gene.mtime,
        "content_hash": _row_value(source_row, "content_hash") or gene.content_hash,
        "volatility_class": (
            _row_value(source_row, "volatility_class")
            or gene.volatility_class
            or "medium"
        ),
        "authority_class": (
            _row_value(source_row, "authority_class")
            or gene.authority_class
            or "primary"
        ),
        "support_span": _row_value(source_row, "support_span") or gene.support_span,
        "last_verified_at": last_verified_at,
        "invalidated_at": _row_value(source_row, "invalidated_at"),
    }


def _freshness_score(last_verified_at: float | None, volatility_class: str, now_ts: float) -> float:
    if last_verified_at is None:
        return 0.0
    half_life = _HALF_LIFE_SECONDS.get(volatility_class or "medium", _HALF_LIFE_SECONDS["medium"])
    age_seconds = max(0.0, now_ts - float(last_verified_at))
    return math.exp(-age_seconds / max(half_life, 1.0))


def _authority_score(authority_class: str | None) -> float:
    return _AUTHORITY_WEIGHTS.get(authority_class or "primary", 0.75)


def _specificity_score(meta: dict) -> float:
    source_kind = meta.get("source_kind")
    source_id = meta.get("source_id")
    support_span = meta.get("support_span")

    if source_kind in _LITERAL_SOURCE_KINDS and source_id:
        return 1.0
    if support_span and source_id:
        return 0.9
    if source_kind in _DOC_LIKE_SOURCE_KINDS and source_id:
        return 0.75
    if source_kind in _MEDIA_SOURCE_KINDS and source_id:
        return 0.50
    if source_kind == "user_assertion":
        return 0.45
    return 0.60


def _status_for(
    *,
    task_type: str,
    freshness_score: float,
    authority_score: float,
    invalidated_at: float | None,
    freshness_known: bool,
) -> str:
    if invalidated_at is not None:
        return "needs_refresh"

    if not freshness_known:
        return "needs_refresh" if task_type in _HIGH_RISK_TASKS else "stale_risk"

    if authority_score < 0.55:
        return "needs_refresh" if task_type in _HIGH_RISK_TASKS else "stale_risk"

    if task_type in _HIGH_RISK_TASKS:
        if freshness_score >= 0.70:
            return "verified"
        if freshness_score >= 0.35:
            return "stale_risk"
        return "needs_refresh"

    if task_type == "review":
        if freshness_score >= 0.55:
            return "verified"
        if freshness_score >= 0.20:
            return "stale_risk"
        return "needs_refresh"

    if freshness_score >= 0.35:
        return "verified"
    if freshness_score >= 0.12:
        return "stale_risk"
    return "needs_refresh"


def _action_risk_score(task_type: str, freshness_score: float, source_kind: str | None) -> float:
    base = _TASK_RISK.get(task_type, 0.50)
    exactness_penalty = 0.0
    if task_type in _HIGH_RISK_TASKS and source_kind not in _LITERAL_SOURCE_KINDS:
        exactness_penalty = 0.15
    return min(1.0, base * (1.0 - freshness_score) + exactness_penalty)


def _item_title(gene: Gene, meta: dict) -> str:
    source_id = meta.get("source_id")
    if source_id:
        name = PurePath(source_id).name
        if name:
            return name
        return source_id
    if gene.promoter.summary:
        return gene.promoter.summary[:80]
    return gene.gene_id


_DEFAULT_MAX_ITEM_CHARS = 280
# Opt-in raw cap: when callers pass include_raw=True they're signalling
# they want the full gene.content (not the compressor-compressed summary)
# for direct consumption as LLM context. 48k chars ~ 12k tokens per item
# — a single item can't exceed a typical context window by itself, but a
# packet of 8 max_genes × 48k would be 384k chars. Callers that need a
# smaller per-item cap override via `max_item_chars`.
_RAW_MAX_ITEM_CHARS = 48000


def _item_content(
    gene: Gene,
    *,
    max_chars: int = _DEFAULT_MAX_ITEM_CHARS,
    prefer_raw: bool = False,
) -> str:
    """Per-item content string for the packet.

    The default 280-char cap uses ``gene.complement`` (the compressor
    compressed summary) and truncates aggressively — appropriate when the
    packet is itself routing metadata and the LLM will re-fetch on demand.

    When ``prefer_raw=True`` the full ``gene.content`` is returned instead,
    bounded by ``max_chars``. This is the "helix_only gives me the real
    content, not a thumbnail" path — opt-in via the ``include_raw`` flag
    on ``/context/packet`` (research-review Proposal 3, 2026-04-22).
    """
    if prefer_raw:
        text = (gene.content or gene.complement or "").strip()
    else:
        text = (gene.complement or gene.content or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max(1, max_chars - 3)] + "..."


def _item_citations(gene: Gene, meta: dict) -> list[str]:
    citations = [f"gene:{gene.gene_id}"]
    if meta.get("source_id"):
        citations.insert(0, str(meta["source_id"]))
    return citations


def _coordinate_signals(query: str, genes: list[Gene]) -> tuple[float, float]:
    """Folder-grain + file-grain path overlap between query and delivered documents.

    Returns ``(folder_coverage, file_coverage)`` — both in [0, 1]. Each is
    the fraction of delivered documents whose path/file tokens intersect the
    query's significant tokens.

    - **folder_coverage** uses ``path_tokens()`` (folder + file tokens mixed).
      Coarse signal. Introduced as Step 1b-iter2 (2026-04-18).
    - **file_coverage** uses ``file_tokens()`` (basename only). Narrower
      signal; designed to catch the "same-folder-wrong-file" failure mode
      where the delivered set is all in the right project directory but
      none of the files actually contain the queried concept.
    """
    if not genes:
        return (0.0, 0.0)
    domains, entities = extract_query_signals(query)
    q_set = {t.lower() for t in (domains + entities) if t}
    if not q_set:
        return (0.0, 0.0)
    folder_hits = 0
    file_hits = 0
    for g in genes:
        sid = getattr(g, "source_id", None)
        if not sid:
            continue
        if path_tokens(sid) & q_set:
            folder_hits += 1
        if file_tokens(sid) & q_set:
            file_hits += 1
    n = len(genes)
    return (folder_hits / n, file_hits / n)


def _coordinate_confidence(query: str, genes: list[Gene]) -> float:
    """Composite folder + file grain confidence in [0, 1].

    Blends ``_coordinate_signals`` into a single number suitable for
    threshold-based downgrades. File-grain is weighted higher because
    same-folder-wrong-file is the dominant silent-miss mode (see
    2026-04-18 session-close handoff). A document whose filename tokens
    match the query is much more likely to be the right coordinate
    than one that just happens to share a project folder.
    """
    folder_cov, file_cov = _coordinate_signals(query, genes)
    return 0.4 * folder_cov + 0.6 * file_cov


_COORDINATE_CONFIDENCE_FLOOR = 0.30
_FILE_GRAIN_FLOOR = 0.15


def _apply_coordinate_confidence(
    status: str,
    task_type: str,
    coordinate_confidence: float,
    file_coverage: float = 1.0,
) -> str:
    """Downgrade status when coordinate confidence is below the floor.

    Freshness answers "is what we resolved to trustworthy?" — but if the
    resolution itself landed in the wrong region, freshness is a category
    error. Low coordinate confidence forces a refresh cue regardless of
    how fresh the delivered content is.

    File-grain acts as an independent downgrade trigger: if the delivered
    set passed folder-grain (composite ≥ floor) but no delivered file
    names mention the queried concept, we're in "same folder, wrong file"
    territory and still shouldn't trust the packet for high-risk tasks.
    """
    composite_ok = coordinate_confidence >= _COORDINATE_CONFIDENCE_FLOOR
    file_ok = file_coverage >= _FILE_GRAIN_FLOOR
    if composite_ok and file_ok:
        return status
    if task_type in _HIGH_RISK_TASKS:
        return "needs_refresh"
    if status == "verified":
        return "stale_risk"
    return status


def _build_item(
    gene: Gene,
    *,
    relevance_score: float,
    meta: dict,
    task_type: str,
    now_ts: float,
    coordinate_confidence: float = 1.0,
    file_coverage: float = 1.0,
    max_item_chars: int = _DEFAULT_MAX_ITEM_CHARS,
    prefer_raw: bool = False,
) -> tuple[ContextItem, str]:
    freshness_known = meta.get("last_verified_at") is not None
    freshness_score = _freshness_score(
        meta.get("last_verified_at"),
        meta.get("volatility_class") or "medium",
        now_ts,
    )
    authority_score = _authority_score(meta.get("authority_class"))
    specificity_score = _specificity_score(meta)
    live_truth_score = freshness_score * authority_score * specificity_score

    if meta.get("invalidated_at") is not None:
        live_truth_score *= 0.25

    status = _status_for(
        task_type=task_type,
        freshness_score=freshness_score,
        authority_score=authority_score,
        invalidated_at=meta.get("invalidated_at"),
        freshness_known=freshness_known,
    )
    status = _apply_coordinate_confidence(
        status, task_type, coordinate_confidence, file_coverage,
    )

    item = ContextItem(
        kind="gene",
        gene_id=gene.gene_id,
        title=_item_title(gene, meta),
        content=_item_content(
            gene, max_chars=max_item_chars, prefer_raw=prefer_raw,
        ),
        relevance_score=float(relevance_score),
        live_truth_score=float(live_truth_score),
        source_id=meta.get("source_id"),
        source_kind=meta.get("source_kind"),
        volatility_class=meta.get("volatility_class"),
        authority_class=meta.get("authority_class"),
        last_verified_at=meta.get("last_verified_at"),
        status=status,
        citations=_item_citations(gene, meta),
    )
    return item, status


def _refresh_target(item: ContextItem, task_type: str) -> RefreshTarget | None:
    if not item.source_id:
        return None
    if item.status == "verified":
        return None
    priority = _action_risk_score(task_type, item.live_truth_score, item.source_kind)
    reason = "stale_risk"
    if item.status == "needs_refresh":
        reason = "fresh verification required before action"
    elif item.status == "stale_risk":
        reason = "relevant evidence is aging or weakly grounded"
    return RefreshTarget(
        target_kind=item.source_kind or "source",
        source_id=item.source_id,
        reason=reason,
        priority=priority,
    )


def _query_genes(
    query: str,
    *,
    genome=None,
    router=None,
    max_genes: int = 8,
    read_only: bool = False,
) -> tuple[list[Gene], dict]:
    domains, entities = extract_query_signals(query)
    if not domains and not entities and query.strip():
        fallback = query.strip().lower()
        if len(fallback) > 2:
            domains = [fallback]

    if router is not None:
        genes = router.query_genes(
            domains=domains,
            entities=entities,
            max_genes=max_genes,
            read_only=read_only,
        )
        score_map = dict(getattr(router, "last_query_scores", {}))
        return genes, score_map

    if genome is not None:
        genes = genome.query_genes(
            domains=domains,
            entities=entities,
            max_genes=max_genes,
            read_only=read_only,
        )
        score_map = dict(getattr(genome, "last_query_scores", {}))
        return genes, score_map

    raise ValueError("build_context_packet requires a genome or router")


def build_context_packet(
    query: str,
    *,
    task_type: str = "explain",
    genome=None,
    router=None,
    main_conn: sqlite3.Connection | None = None,
    max_genes: int = 8,
    now_ts: float | None = None,
    read_only: bool = False,
    include_raw: bool = False,
    max_item_chars: int | None = None,
) -> ContextPacket:
    """Return a freshness-labeled packet for the given query.

    ``include_raw=True`` switches each item's content from the compressor-
    compressed summary to the full ``gene.content``, and bumps the per-item
    cap to ``_RAW_MAX_ITEM_CHARS`` (48k) unless ``max_item_chars`` overrides.
    This is the "helix_only ships the real content" path from the
    2026-04-22 research review (Proposal 3) — use when the packet is the
    only context source and the downstream LLM needs real bytes, not
    thumbnails.
    """
    effective_max_chars = (
        max_item_chars
        if max_item_chars is not None
        else (_RAW_MAX_ITEM_CHARS if include_raw else _DEFAULT_MAX_ITEM_CHARS)
    )
    if not query or not query.strip():
        raise ValueError("query must be non-empty")

    effective_main_conn = main_conn
    if effective_main_conn is None and router is not None:
        effective_main_conn = getattr(router, "main_conn", None)

    now_ts = float(now_ts) if now_ts is not None else 0.0
    if now_ts <= 0.0:
        import time
        now_ts = time.time()

    genes, score_map = _query_genes(
        query,
        genome=genome,
        router=router,
        max_genes=max_genes,
        read_only=read_only,
    )
    packet = ContextPacket(task_type=task_type, query=query)

    if effective_main_conn is None:
        packet.notes.append("source_index unavailable; using gene-local metadata only")

    # Step 1b-iter2 + file-grain: coordinate_confidence downgrades status
    # when retrieval lands outside the coordinate region the query names.
    # folder_cov is coarse (project/module level); file_cov is narrow
    # (basename level). The composite is a weighted blend; file_cov is
    # surfaced separately so "same folder, wrong file" still triggers
    # a downgrade even when the composite passes.
    folder_cov, file_cov = _coordinate_signals(query, genes)
    coordinate_confidence = 0.4 * folder_cov + 0.6 * file_cov

    # Stage 6 (§9): promote coordinate_confidence + file_coverage
    # from prose-in-notes to first-class packet fields. The notes
    # entries below remain (humans read them; the threshold-trigger
    # form is more readable than the raw numbers).
    packet.coordinate_confidence = float(coordinate_confidence)
    packet.file_coverage = float(file_cov)

    if coordinate_confidence < _COORDINATE_CONFIDENCE_FLOOR:
        packet.notes.append(
            f"coordinate_confidence={coordinate_confidence:.2f} below "
            f"{_COORDINATE_CONFIDENCE_FLOOR:.2f} floor "
            f"(folder={folder_cov:.2f}, file={file_cov:.2f}) — retrieval "
            "may not have located the right coordinate region"
        )
    elif file_cov < _FILE_GRAIN_FLOOR:
        packet.notes.append(
            f"file_coverage={file_cov:.2f} below {_FILE_GRAIN_FLOOR:.2f} "
            f"floor (folder={folder_cov:.2f}) — delivered set is in the "
            "right folder but no filenames match the queried concept"
        )

    for gene in genes:
        source_row = _lookup_source_row(effective_main_conn, gene.gene_id)
        meta = _effective_meta(gene, source_row)
        item, status = _build_item(
            gene,
            relevance_score=score_map.get(gene.gene_id, 0.0),
            meta=meta,
            task_type=task_type,
            now_ts=now_ts,
            coordinate_confidence=coordinate_confidence,
            file_coverage=file_cov,
            max_item_chars=effective_max_chars,
            prefer_raw=include_raw,
        )
        if status == "verified":
            packet.verified.append(item)
        elif status == "stale_risk":
            packet.stale_risk.append(item)
        else:
            packet.stale_risk.append(item)

        target = _refresh_target(item, task_type)
        if target is not None:
            packet.refresh_targets.append(target)

    packet.verified.sort(
        key=lambda item: (item.live_truth_score, item.relevance_score),
        reverse=True,
    )
    packet.stale_risk.sort(
        key=lambda item: (item.status == "needs_refresh", item.live_truth_score, item.relevance_score),
        reverse=True,
    )
    packet.refresh_targets.sort(key=lambda target: target.priority, reverse=True)

    # ── Stage 6: machine-tagged know/miss block on the packet ─────
    # The /context/packet route lifts this to the top of the response.
    # Soft-fail: any exception leaves know/miss as None and the
    # contract degrades cleanly (consumers that don't know about
    # Stage 6 see no change).
    try:
        _attach_know_or_miss(
            packet,
            query=query,
            genes=genes,
            score_map=score_map,
            coordinate_confidence=coordinate_confidence,
        )
    except Exception:
        import logging
        logging.getLogger("helix.context_packet").warning(
            "Stage-6 know/miss attach failed", exc_info=True
        )

    return packet


# ─────────────────────────────────────────────────────────────────────
# Stage 6 know/miss helper (kept here so the knowledge store read for
# tier_contributions stays close to the score_map fetch above).
# ─────────────────────────────────────────────────────────────────────

def _attach_know_or_miss(
    packet: "ContextPacket",
    *,
    query: str,
    genes: list,
    score_map: dict,
    coordinate_confidence: float,
) -> None:
    """Compute decide_know_or_miss and stash on packet.know / packet.miss.

    Mirror of the /context route logic in server._compute_know_or_miss_block
    but works directly with the locals already accumulated in
    ``build_context_packet`` instead of re-fetching from the knowledge store.
    """
    from .know_calibration import load_calibration_from_toml
    from .know_decision import (
        _agree_from_tier_contributions,
        decide_know_or_miss,
    )
    from .schemas import ContextHealth, ContextWindow, KnowBlock, MissBlock

    # Compute discriminator inputs from the post-fusion score map.
    if score_map:
        sorted_scores = sorted(score_map.values(), reverse=True)
        top_score = float(sorted_scores[0])
        score_gap = (
            float(sorted_scores[0] - sorted_scores[1])
            if len(sorted_scores) > 1
            else float(sorted_scores[0])
        )
        if len(sorted_scores) > 1 and sorted_scores[1] > 0:
            ratio = float(sorted_scores[0] / sorted_scores[1])
        else:
            ratio = float(sorted_scores[0])
    else:
        top_score = 0.0
        score_gap = 0.0
        ratio = 0.0

    # Build a shim ContextWindow so decide_know_or_miss can reuse the
    # same input shape as the /context route. Fields beyond
    # context_health.status / genes_expressed are not read by the
    # discriminator (verified by inspection of know_decision.py).
    n_genes = len(genes)
    if n_genes == 0:
        # No candidates returned: route to the no_promoter_match branch
        # of the discriminator. We use status="sparse" + genes_expressed=0
        # because (1) "denatured" is reserved for "knowledge store shape is bad"
        # which we cannot detect from the packet builder, and (2) the
        # discriminator ordering (§5) checks genes_expressed==0 ahead
        # of any sparse-confidence floor, so this routes to
        # MissBlock(reason="no_promoter_match"). The packet builder
        # does not emit ABSTAIN; that's the /context endpoint's
        # responsibility.
        status = "sparse"
    else:
        status = "aligned"
    health = ContextHealth(
        ellipticity=0.0,
        coverage=0.0,
        density=0.0,
        freshness=0.0,
        genes_available=0,
        genes_expressed=n_genes,
        status=status,
    )
    shim_window = ContextWindow(
        ribosome_prompt="",
        expressed_context="",
        context_health=health,
        metadata={"query": query, "ratio": ratio},
    )

    # Tier contributions for lex/dense agreement. The packet builder
    # does not currently capture per-tier contribs (it only takes the
    # post-fusion score_map). Best-effort: pull off the knowledge store if the
    # caller wired one through. Otherwise treat as no-agreement signal.
    tier_contrib = {}
    # documents elements may be Document objects from genome.query_genes, which
    # don't carry tier-level info. The router/genome's last_tier_-
    # contributions is the source of truth — we expect the route to
    # have set it. The packet builder does NOT currently surface the
    # knowledge store handle past _query_genes; until that's plumbed, we leave
    # this False, which is the safe direction (no false-positive
    # confidence boost).
    lex_dense_agree = _agree_from_tier_contributions(tier_contrib, k=3)

    cal = load_calibration_from_toml()
    top_gene = genes[0] if genes else None
    block = decide_know_or_miss(
        window=shim_window,
        query=query,
        top_score=top_score,
        score_gap=score_gap,
        lexical_dense_agree=lex_dense_agree,
        coordinate_confidence=coordinate_confidence,
        top_gene=top_gene,
        ratio=ratio,
        calibration=cal,
    )
    if isinstance(block, KnowBlock):
        packet.know = block
        packet.miss = None
    elif isinstance(block, MissBlock):
        packet.miss = block
        packet.know = None


def get_refresh_targets(
    query: str,
    *,
    task_type: str = "edit",
    genome=None,
    router=None,
    main_conn: sqlite3.Connection | None = None,
    max_genes: int = 8,
    now_ts: float | None = None,
    read_only: bool = False,
) -> list[RefreshTarget]:
    """Convenience helper for just the reread plan."""
    packet = build_context_packet(
        query,
        task_type=task_type,
        genome=genome,
        router=router,
        main_conn=main_conn,
        max_genes=max_genes,
        now_ts=now_ts,
        read_only=read_only,
    )
    return packet.refresh_targets
