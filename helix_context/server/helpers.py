"""Shared helpers for the Helix Context server package.

Contains module-level utility functions, background tasks, and proxy
helpers that are used across multiple route modules.  Extracted from the
monolithic ``server.py`` -- NO logic changes, pure code motion.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
import socket
from typing import Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..accel import json_loads
from ..config import HelixConfig
from ..context_manager import HelixContextManager
from ..scoring.know_calibration import load_calibration_from_toml
from ..scoring.know_decision import (
    _agree_from_tier_contributions,
    decide_know_or_miss,
    _is_code_shaped,
)
from ..schemas import KnowBlock, MissBlock

log = logging.getLogger("helix.server")

_CHECKPOINT_INTERVAL = 60  # seconds between background WAL checkpoints
_REGISTRY_SWEEP_INTERVAL = 60  # seconds between session registry status sweeps
_WAL_GAUGE_INTERVAL = 30  # seconds between WAL-size gauge emissions

# Module-level stash for paused compressor backends. Maps id(backend) ->
# original complete() method. Not persisted -- lost on server restart,
# which is fine because restart defaults to un-paused.
_paused_ribosomes: Dict[int, object] = {}

# Cardinality cap for the `agent` telemetry label. The known-agents
# allowlist below covers our shipped MCP-host handles; anything else gets
# folded into "other" to prevent a label-cardinality blow-up if a caller
# starts shipping per-pid handles or freeform strings. Operators who
# really want a custom label can extend this set via HELIX_AGENT_ALLOW.
_KNOWN_AGENTS_DEFAULT = frozenset({
    "laude", "raude", "taude", "gemini", "codex", "claude", "manual",
})


# ── Identity / attribution helpers ──────────────────────────────────


def _local_timezone() -> Optional[str]:
    """Resolve the local IANA timezone name for attribution.

    Order of precedence:
      1. ``HELIX_TZ`` env var (e.g., 'America/Los_Angeles', 'Europe/Berlin')
         -- the only path that guarantees an IANA name on Windows, where
         the OS exposes display names like 'Pacific Standard Time'.
      2. ``tzlocal.get_localzone_name()`` if the package is installed.
         Cross-platform, always returns IANA. Soft-import so we don't
         add a hard dependency.
      3. Stdlib ``datetime.now().astimezone().tzname()`` -- returns
         abbreviation on Linux/Mac (e.g., 'PDT') and display name on
         Windows ('Pacific Daylight Time'). Not IANA, but better than
         nothing for forensic value.
      4. ``time.tzname[time.daylight]`` -- last-ditch from the time module.
      5. ``'UTC'`` -- final fallback so attribution writes don't fail.

    Returns the resolved name as a string. Caller can normalize at
    query time if needed (Windows display names map cleanly to IANA via
    a small lookup table).

    The IANA name is a label for a DST rule set, NOT a location. It
    tells us the longitude band + DST policy the device is on, not the
    user's city or actual coordinates. See docs/FEDERATION_LOCAL.md
    "What timezone capture actually tells us" for the honest framing.
    """
    if tz := os.environ.get("HELIX_TZ"):
        return tz.strip()[:64] or None
    try:
        from tzlocal import get_localzone_name  # type: ignore
        name = get_localzone_name()
        if name:
            return str(name)[:64]
    except Exception:
        pass
    try:
        import datetime as _dt
        name = _dt.datetime.now().astimezone().tzname()
        if name:
            return name[:64]
    except Exception:
        pass
    try:
        import time as _time
        name = _time.tzname[1 if _time.daylight else 0]
        if name:
            return name[:64]
    except Exception:
        pass
    return "UTC"


def _local_attribution_defaults() -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Resolve OS-level 4-layer identity for trust-on-first-use attribution.

    Returns (user_handle, device, org, agent_handle):

        org           HELIX_ORG env       || 'local'
        device        HELIX_DEVICE env    || HELIX_PARTY env (legacy)
                                          || socket.gethostname()
        user_handle   HELIX_USER env      || HELIX_AGENT env (legacy fallback
                                             -- when only HELIX_AGENT is set,
                                             treat it as the human handle so
                                             pre-4-layer setups don't break)
                                          || getpass.getuser()
        agent_handle  HELIX_AGENT env     || None (no AI agent -- manual ingest)

    The legacy/back-compat note: pre-4-layer code overloaded HELIX_AGENT
    as the "handle" of whoever was acting (could be human or AI). When
    HELIX_USER is now also set, we honor the new split. When only
    HELIX_AGENT is set without HELIX_USER, we keep treating it as the
    handle (preserves the prior commit's behaviour) AND also surface it
    as agent_handle so the agents table picks it up.

    Any field may be None -- /ingest tolerates None on every axis and
    falls through to writing whichever subset resolved cleanly.

    See docs/FEDERATION_LOCAL.md for the full design.
    """
    # Org (top layer)
    org = os.environ.get("HELIX_ORG") or "local"

    # Device (PC) -- accept HELIX_DEVICE preferentially, fall back to
    # legacy HELIX_PARTY, then hostname.
    try:
        device = (
            os.environ.get("HELIX_DEVICE")
            or os.environ.get("HELIX_PARTY")
            or socket.gethostname()
        )
    except Exception:
        device = None

    # Agent (AI persona) -- explicit only. None means "manual / no agent".
    agent_handle = os.environ.get("HELIX_AGENT") or None

    # User (human) -- HELIX_USER wins; otherwise we have to pick one of
    # HELIX_AGENT (legacy back-compat) or OS user. Logic: if HELIX_USER
    # is set, use it. Else if HELIX_AGENT is set AND HELIX_USER is not,
    # the user must be the OS account that started the process (we can't
    # tell from env alone). Use OS user.
    try:
        user_handle = os.environ.get("HELIX_USER") or getpass.getuser()
    except Exception:
        user_handle = None

    # Normalize: lowercase, strip whitespace, sanity-cap length
    def _norm(v):
        if not v:
            return None
        s = str(v).strip().lower()[:64]
        return s or None

    return _norm(user_handle), _norm(device), _norm(org), _norm(agent_handle)


def _normalize_identity_token(value: Optional[str]) -> Optional[str]:
    """Normalize request-supplied identity tokens to local-tier shape."""
    if not value:
        return None
    normalized = str(value).strip().lower()[:64]
    return normalized or None


def _agent_allowlist() -> frozenset[str]:
    extra = os.environ.get("HELIX_AGENT_ALLOW", "")
    if not extra:
        return _KNOWN_AGENTS_DEFAULT
    extra_set = {
        t for t in (s.strip().lower() for s in extra.split(","))
        if t and t != "other" and t != "unknown"
    }
    return _KNOWN_AGENTS_DEFAULT | extra_set


def _resolve_caller_agent(request, data: dict) -> str:
    """Pick the agent label for a /context request.

    Precedence:
      1. body ``agent`` field -- explicit caller-provided handle
      2. header ``X-Helix-Agent`` -- for callers that can't shape the body
      3. env ``HELIX_AGENT`` -- per-process default set by the host bat /
         shim (start-helix-tray.bat, MCP shim, etc.)
      4. ``"unknown"`` -- last-resort label so the metric always carries
         a value (avoiding NULL-style gaps that confuse stacked-area
         dashboards).

    Returns a normalized lowercase handle. Unknown handles outside the
    allowlist collapse to ``"other"`` to keep Prometheus label
    cardinality bounded.
    """
    candidates = (
        data.get("agent") if isinstance(data, dict) else None,
        request.headers.get("x-helix-agent") if request is not None else None,
        os.environ.get("HELIX_AGENT"),
    )
    handle = None
    for c in candidates:
        normalized = _normalize_identity_token(c)
        if normalized:
            handle = normalized
            break
    if handle is None:
        return "unknown"
    return handle if handle in _agent_allowlist() else "other"


# ── Retrieval decision helpers ──────────────────────────────────────


def _compute_know_or_miss_block(
    *,
    helix: "HelixContextManager",
    window,  # ContextWindow
    query: str,
):
    """Run the Stage 6 know/miss discriminator for a finished /context turn.

    Pulls together the four discriminator inputs from the manager and
    the knowledge store's per-call state, then defers the actual decision to
    ``decide_know_or_miss``. Returns ``KnowBlock | MissBlock | None`` --
    None only on hard-failure paths (the route falls back gracefully
    instead of bubbling).

    Stage 7 (2026-05-08) hooks the freshness pipeline here:
      * top-1 mtime revalidation via ``freshness.revalidate_and_mark``
      * Path-A supersession check via ``freshness.check_superseded``
      * cold-tier peek via ``HelixContextManager._cold_tier_peek``
      * ``health.freshness_min`` plumbed through to ``compute_confidence``
        for the beta5 contribution.
    """
    # Pull retrieval scores. ``last_query_scores`` is the post-fusion
    # per-document score map (gene_id -> float). Top-1 / score-gap are
    # derived from a sorted-desc view of this map.
    raw_scores = helix.genome.last_query_scores or {}
    if raw_scores:
        sorted_scores = sorted(raw_scores.values(), reverse=True)
        top_score = float(sorted_scores[0])
        score_gap = float(sorted_scores[0] - sorted_scores[1]) if len(sorted_scores) > 1 else float(sorted_scores[0])
        # Score ratio (top1 / 2nd) used by MissBlock for downstream
        # debugging -- falls back to top_score for the singleton case.
        ratio = float(sorted_scores[0] / sorted_scores[1]) if len(sorted_scores) > 1 and sorted_scores[1] > 0 else float(sorted_scores[0])
    else:
        top_score = 0.0
        score_gap = 0.0
        ratio = 0.0

    # Lexical-dense agreement from per-tier contribution map.
    tier_contrib = getattr(helix.genome, "last_tier_contributions", {}) or {}
    lex_dense_agree = _agree_from_tier_contributions(tier_contrib, k=3)

    # Coordinate confidence -- promoted to first-class in Stage 6 (section 9).
    # Lazy-import to avoid a server -> context_packet -> server cycle.
    from ..context_packet import _coordinate_confidence
    from ..genome import Gene as _GeneType  # for type discipline below

    # Re-fetch the retrieved documents by id from the knowledge store's read conn so
    # we can run path-grain coverage. Cheap (documents are 1-row reads).
    gene_ids = list(window.expressed_gene_ids or [])
    genes: list = []
    top_gene = None
    if gene_ids:
        try:
            cur = helix.genome.read_conn.cursor()
            placeholders = ",".join("?" * len(gene_ids))
            rows = cur.execute(
                f"SELECT gene_id, source_id FROM genes WHERE gene_id IN ({placeholders})",
                gene_ids,
            ).fetchall()
            row_map = {r["gene_id"]: r for r in rows}
            # Preserve the response order (same as expressed_gene_ids).
            for gid in gene_ids:
                r = row_map.get(gid)
                if r is None:
                    continue
                # Build a tiny Document-like proxy object -- _coordinate_confidence
                # and the beacon both only need ``source_id``.
                class _GeneProxy:
                    __slots__ = ("gene_id", "source_id")
                    def __init__(self, gid, sid):
                        self.gene_id = gid
                        self.source_id = sid
                genes.append(_GeneProxy(gid, r["source_id"] or ""))
            if genes:
                top_gene = genes[0]
        except Exception:
            log.debug("Stage-6 gene fetch for coordinate_confidence failed", exc_info=True)

    coord_conf = _coordinate_confidence(query, genes) if genes else 0.0

    # Calibration -- load fresh helix.toml [know] table per call (cheap;
    # tomllib parses ~kB in microseconds; falls back to defaults).
    cal = load_calibration_from_toml()

    # Stage 7 (spec section 3) -- freshness_min from the rebuilt _compute_health.
    # ``ContextHealth.freshness_min`` is Optional[float]; None falls
    # through to compute_confidence as "freshness unknown" (neutral).
    freshness_min = getattr(window.context_health, "freshness_min", None)

    # Stage 7 (spec section 5) -- top-1 source revalidation. Need a real Document
    # (not just the source-id proxy used for the beacon) to read
    # ``last_verified_at``. Fetch only top-1 to keep latency bounded
    # (~3 stat calls warm = sub-millisecond per spec section 5).
    freshness_status: Optional[str] = None
    successor_source_id: Optional[str] = None
    cold_targets: list[str] = []
    if top_gene is not None and gene_ids:
        try:
            from ..retrieval.freshness import (
                check_superseded,
                revalidate_and_mark,
            )
            from ..schemas import EpigeneticMarkers, Gene as _Gene

            top_gid = gene_ids[0]
            cur = helix.genome.read_conn.cursor()
            row = cur.execute(
                "SELECT gene_id, source_id, last_verified_at, "
                "epigenetics, supersedes "
                "FROM genes WHERE gene_id = ? LIMIT 1",
                (top_gid,),
            ).fetchone()
            if row is not None:
                # Build a minimal Document-shaped object the freshness
                # helpers can read. Constructing a full Document blows
                # up on missing columns; a SimpleNamespace-like proxy
                # with the four attributes the helpers actually use
                # is enough.
                class _FreshGeneProxy:
                    __slots__ = (
                        "gene_id", "source_id",
                        "last_verified_at", "epigenetics", "supersedes",
                    )
                    def __init__(self, gid, sid, lva, epi, sup):
                        self.gene_id = gid
                        self.source_id = sid
                        self.last_verified_at = lva
                        self.epigenetics = epi
                        self.supersedes = sup

                # Parse signals blob lazily so we have a
                # ``source_path`` attr if the JSON carried one.
                epi_obj = None
                try:
                    import json as _json
                    epi_raw = row["epigenetics"] if "epigenetics" in row.keys() else None
                    if epi_raw:
                        # DocumentSignals doesn't carry source_path
                        # in its model, but the freshness helpers fall
                        # back to source_id when source_path is absent.
                        epi_dict = _json.loads(epi_raw)
                        # Best-effort hydrate; ignore unknown keys.
                        epi_obj = EpigeneticMarkers.model_validate(
                            {k: v for k, v in epi_dict.items()
                             if k in EpigeneticMarkers.model_fields}
                        )
                except Exception:
                    epi_obj = None

                top_proxy = _FreshGeneProxy(
                    row["gene_id"],
                    row["source_id"] if "source_id" in row.keys() else None,
                    row["last_verified_at"] if "last_verified_at" in row.keys() else None,
                    epi_obj,
                    row["supersedes"] if "supersedes" in row.keys() else None,
                )

                import time as _time
                now_ts = _time.time()
                # Read-only contract -- /context is a read endpoint, so
                # mark_verified is a no-op here. The cache MAY still
                # update (in-memory; not a knowledge store write).
                try:
                    freshness_status = revalidate_and_mark(
                        helix.genome,
                        top_proxy,
                        mtime_cache=helix._mtime_cache,
                        now_ts=now_ts,
                        read_only=True,
                    )
                except Exception:
                    log.debug("Stage-7 revalidate failed", exc_info=True)
                    freshness_status = None

                try:
                    successor_source_id = check_superseded(
                        helix.genome, top_proxy,
                    )
                except Exception:
                    log.debug("Stage-7 supersession check failed", exc_info=True)
                    successor_source_id = None
        except Exception:
            log.debug("Stage-7 top-1 fetch failed", exc_info=True)

    # Cold-tier peek -- fires only when retrieved set is thin AND
    # corpus health is not abstain (spec section 6). Caches the
    # refresh_targets on the manager so the route layer can attach
    # them to the agent payload without re-querying.
    try:
        genes_expressed_n = int(
            getattr(window.context_health, "genes_expressed", 0) or 0
        )
        health_status = getattr(window.context_health, "status", "")
        if genes_expressed_n < 3 and health_status not in ("abstain", "denatured"):
            cold_targets = helix._cold_tier_peek(query, k=3, min_cosine=0.4)
            helix._last_cold_peek_targets = list(cold_targets)
    except Exception:
        log.debug("Stage-7 cold-tier peek failed", exc_info=True)
        cold_targets = []

    return decide_know_or_miss(
        window=window,
        query=query,
        top_score=top_score,
        score_gap=score_gap,
        lexical_dense_agree=lex_dense_agree,
        coordinate_confidence=coord_conf,
        top_gene=top_gene,
        ratio=ratio,
        calibration=cal,
        freshness_min=freshness_min,
        freshness_status=freshness_status,
        successor_source_id=successor_source_id,
        cold_refresh_targets=cold_targets,
    )


def _compute_plr_confidence(
    helix: "HelixContextManager",
    config: HelixConfig,
    query: str,
    *,
    now_ts: Optional[float] = None,
) -> Optional[dict]:
    """Score the just-completed retrieval with the PLR query-quality head.

    Called after ``build_context_packet`` so ``helix.genome.last_tier_contributions``
    reflects the current query. Returns a dict suitable for JSON serialization
    (prob_B, logit, score_A, high_risk, artifact_label_set) or None when the
    fuser can't be loaded / scored.

    This is a **query-level** head. Every candidate in a given retrieval has
    the same feature vector, so callers should treat ``plr_confidence`` as a
    query-quality signal, not a per-document ranking input. See
    ``helix_context/fusion_plr.py`` docstring for the trade-off.
    """
    try:
        from ..retrieval import fusion_plr
    except ImportError:
        return None
    fuser = fusion_plr.get_fuser(config.plr.model_path)
    if fuser is None:
        return None

    # 1. Aggregate tier_totals across all documents in the current retrieval.
    #    Mirrors the CWoLa-logger aggregation at server.py ~line 898.
    tier_contrib_all = getattr(helix.genome, "last_tier_contributions", {}) or {}
    tier_totals: dict[str, float] = {}
    for contribs in tier_contrib_all.values():
        for tier, score in contribs.items():
            tier_totals[tier] = tier_totals.get(tier, 0.0) + score

    # 2. Window features -- skip when no session context is available; the
    #    fuser treats missing window entries as zero, same as training.
    window_features: Optional[dict] = None
    try:
        from ..identity import cwola
        # /context/packet doesn't currently thread a session_id through the
        # call, so we can't ask for sliding-window correlations. Leaving
        # window_features empty is the honest move; the classifier trains
        # on rows where the window extractor also degrades gracefully.
        _ = cwola  # keep the import available for future session threading
    except ImportError:
        pass

    # 3. cos(query_sema, top_candidate_sema) -- same computation as the CWoLa
    #    Phase 1 logger enrichment in server.py ~line 910.
    cos_qc: Optional[float] = None
    try:
        codec = getattr(helix, "_sema_codec", None)
        if codec is None:
            raise RuntimeError("sema codec unavailable")
        q_sema = codec.encode(query)

        top_gene_id = None
        last_scores = getattr(helix.genome, "last_query_scores", {}) or {}
        if last_scores:
            top_gene_id = max(last_scores, key=last_scores.get)
        if top_gene_id:
            gene = helix.genome.get_doc(top_gene_id)
            if gene is not None and gene.embedding and q_sema is not None:
                # inline cosine to avoid extra imports
                a, b = q_sema, gene.embedding
                if len(a) == len(b) and a:
                    import math as _math
                    dot = sum(float(x) * float(y) for x, y in zip(a, b))
                    na = _math.sqrt(sum(float(x) * float(x) for x in a))
                    nb = _math.sqrt(sum(float(y) * float(y) for y in b))
                    if na > 0 and nb > 0:
                        cos_qc = dot / (na * nb)
    except Exception:
        cos_qc = None

    out = fuser.query_confidence(tier_totals, window_features, cos_qc)
    out["high_risk"] = out["prob_B"] > config.plr.high_risk_threshold
    out["artifact_label_set"] = fuser.meta.get("label_set")
    return out


def _merge_tier_contributions(base: dict, extra: dict) -> dict:
    """Merge per-document tier contributions without mutating inputs."""
    merged = {gid: dict(contribs) for gid, contribs in (base or {}).items()}
    for gid, contribs in (extra or {}).items():
        row = merged.setdefault(gid, {})
        for tier, score in contribs.items():
            row[tier] = row.get(tier, 0.0) + score
    return merged


def _probe_upstream(upstream_url: str, timeout_s: float = 1.0) -> Dict[str, object]:
    """Best-effort readiness probe for the configured upstream model server.

    Helix most often fronts Ollama, but we tolerate any OpenAI-compatible
    upstream by probing a few common endpoints and treating any non-5xx
    response as "reachable". This avoids false greens when the model server
    is entirely down while still handling auth-gated upstreams honestly.
    """
    base_url = (upstream_url or "").rstrip("/")
    if not base_url:
        return {
            "reachable": False,
            "detail": "No upstream URL configured.",
        }

    probes = ("/api/tags", "/v1/models", "/health")
    last_error: Optional[str] = None

    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            for probe in probes:
                try:
                    resp = client.get(f"{base_url}{probe}")
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    continue

                if resp.status_code < 500:
                    return {
                        "reachable": True,
                        "probe": probe,
                        "status_code": resp.status_code,
                    }
                last_error = f"HTTP {resp.status_code} on {probe}"
    except Exception as exc:
        last_error = f"{type(exc).__name__}: {exc}"

    return {
        "reachable": False,
        "detail": last_error or "No upstream probe succeeded.",
    }


# ── Background tasks ────────────────────────────────────────────────


async def _background_checkpoint(helix: HelixContextManager) -> None:
    """Periodically flush WAL to main database file."""
    # Read the interval from the package __init__ each iteration so
    # monkeypatch in tests can override the value at runtime.
    import helix_context.server as _srv
    while True:
        await asyncio.sleep(getattr(_srv, "_CHECKPOINT_INTERVAL", _CHECKPOINT_INTERVAL))
        try:
            helix.genome.checkpoint("PASSIVE")
        except Exception:
            log.warning("Background WAL checkpoint failed", exc_info=True)


async def _background_wal_gauge(helix: HelixContextManager) -> None:
    """Emit helix_genome_wal_size_bytes every _WAL_GAUGE_INTERVAL seconds.

    Best-effort: failures are ignored so the gauge never affects uptime.
    No-op when OTel is disabled (noop instruments silently drop the call).
    """
    import helix_context.server as _srv
    while True:
        await asyncio.sleep(getattr(_srv, "_WAL_GAUGE_INTERVAL", _WAL_GAUGE_INTERVAL))
        try:
            helix.genome.emit_wal_health_gauges()
        except Exception:
            pass  # diagnostic path; never block the event loop


async def _background_registry_sweep(registry_obj) -> None:
    """Periodically sweep session registry status.

    Updates the persisted ``status`` column on participants based on
    ``last_heartbeat`` age, transitioning active -> idle -> stale -> gone
    on schedule. Hard-deletes participants whose ``gone`` state has aged
    past 7 days, NULLing their gene_attribution.participant_id while
    preserving party_id.

    The sweep is non-destructive for live data -- observers can call
    list_participants() at any time and get correct status regardless
    of when the sweep last ran (live status is recomputed from
    last_heartbeat). The sweep exists so the persisted column stays
    consistent for any code that filters by it directly.
    """
    # Read from the package module so tests can monkeypatch the interval.
    import helix_context.server as _srv
    while True:
        await asyncio.sleep(getattr(_srv, "_REGISTRY_SWEEP_INTERVAL", _REGISTRY_SWEEP_INTERVAL))
        try:
            counts = registry_obj.sweep()
            # Only log when something interesting happened
            if counts.get("hard_deleted", 0) > 0 or counts.get("gone", 0) > 0:
                log.info("Registry sweep: %s", counts)
        except Exception:
            log.warning("Background registry sweep failed", exc_info=True)


# ── Pydantic models ─────────────────────────────────────────────────


class _TraceBody(BaseModel):
    request_id: str
    trigger_reason: str
    total_latency_ms: int
    health_status: str
    stage_timing_ms: dict
    fingerprint_route: str
    foveated_ranks: str
    final_genes: list


# ── Proxy helpers (used by /v1/chat/completions) ────────────────────


def _munge_messages(
    messages: list[dict],
    expressed_context: str,
    ribosome_prompt: str,
    total_genes: int,
    cold_start_threshold: int,
) -> list[dict]:
    """
    Inject retrieved context into the system message.
    Apply history stripping based on knowledge store maturity (Fix 3).

    Fix 3 (cold-start bootstrap):
        If total_genes < threshold, retain the last 2 conversation turns
        alongside the current turn. Once the knowledge store matures, strip all
        prior turns -- the knowledge store covers them.
    """
    if not messages:
        return messages

    # Find or create system message
    system_msg = None
    other_messages = []
    for msg in messages:
        if msg.get("role") == "system" and system_msg is None:
            system_msg = dict(msg)  # copy
        else:
            other_messages.append(msg)

    if system_msg is None:
        system_msg = {"role": "system", "content": ""}

    # Append context to system message (never overwrite user's custom prompts)
    system_msg["content"] = (
        f"{system_msg['content']}\n\n{ribosome_prompt}\n\n{expressed_context}"
        if system_msg["content"].strip()
        else f"{ribosome_prompt}\n\n{expressed_context}"
    )

    result = [system_msg]

    # Current user message (always keep)
    current_turn = other_messages[-1] if other_messages else None

    if total_genes < cold_start_threshold:
        # Cold start: keep last 2 turns + current for continuity
        history_window = other_messages[-3:-1] if len(other_messages) > 2 else other_messages[:-1]
        result.extend(history_window)
    # else: strip all history -- knowledge store covers it

    if current_turn:
        result.append(current_turn)

    return result


async def _stream_and_tee(
    body: dict,
    config: HelixConfig,
    helix: HelixContextManager,
    user_query: str,
    background_tasks,
):
    """
    Stream chunks from upstream to client while accumulating the
    full response for background persistence.

    Inspects the upstream HTTP status before the first yield; non-2xx
    responses raise HTTPException so FastAPI propagates the real status
    code + body instead of forwarding an error payload to the client
    as if it were a successful stream.
    """
    accumulated: list[str] = []
    captured_usage: Optional[dict] = None

    async with httpx.AsyncClient(timeout=config.server.upstream_timeout) as client:
        async with client.stream(
            "POST",
            f"{config.server.upstream}/v1/chat/completions",
            json=body,
        ) as resp:
            if resp.status_code >= 400:
                # Drain the body so we can forward a readable error
                # detail upstream. aread() materializes the stream.
                try:
                    err_bytes = await resp.aread()
                    err_detail = err_bytes.decode("utf-8", errors="replace")[:2000]
                except Exception:
                    log.warning(
                        "Upstream stream error body read failed",
                        exc_info=True,
                    )
                    err_detail = f"Upstream returned HTTP {resp.status_code}"
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=err_detail,
                )
            async for line in resp.aiter_lines():
                if not line:
                    yield "\n"
                    continue

                # Forward to client immediately
                yield f"{line}\n"

                # Parse SSE data for accumulation (orjson when available)
                if line.startswith("data: "):
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        continue
                    try:
                        chunk = json_loads(data_str)
                        choices = chunk.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                accumulated.append(content)
                        # Capture usage from any chunk that includes it
                        # (modern Ollama / OpenAI with stream_options.include_usage=true).
                        chunk_usage = chunk.get("usage")
                        if isinstance(chunk_usage, dict):
                            captured_usage = chunk_usage
                    except (ValueError, TypeError):
                        pass

    # Stream is complete -- fire background persistence
    full_response = "".join(accumulated)
    if full_response:
        background_tasks.add_task(helix.learn, user_query, full_response)

    # Token accounting -- prefer authoritative usage if upstream provided it,
    # else estimate from the user query + accumulated response.
    try:
        if not helix.token_counter.add_from_usage(captured_usage):
            from ..telemetry.metrics import estimate_tokens
            helix.token_counter.add(
                prompt_tokens=estimate_tokens(user_query),
                completion_tokens=estimate_tokens(full_response),
                estimated=True,
            )
    except Exception:
        log.debug("Token counter update failed (stream)", exc_info=True)


async def _forward_and_replicate(
    body: dict,
    config: HelixConfig,
    helix: HelixContextManager,
    user_query: str,
    background_tasks,
):
    """Forward non-streaming request, then persist."""
    async with httpx.AsyncClient(timeout=config.server.upstream_timeout) as client:
        resp = await client.post(
            f"{config.server.upstream}/v1/chat/completions",
            json=body,
        )
        if resp.status_code >= 400:
            # Propagate upstream status + body instead of forwarding
            # the error payload as HTTP 200 to the caller.
            try:
                err_detail = resp.text[:2000]
            except Exception:
                log.warning(
                    "Upstream error body decode failed",
                    exc_info=True,
                )
                err_detail = f"Upstream returned HTTP {resp.status_code}"
            raise HTTPException(
                status_code=resp.status_code,
                detail=err_detail,
            )
        data = resp.json()

    choices = data.get("choices", [])
    content = ""
    if choices:
        content = choices[0].get("message", {}).get("content", "")
        if content:
            background_tasks.add_task(helix.learn, user_query, content)

    # Token accounting -- exact if usage was provided, else estimated.
    try:
        if not helix.token_counter.add_from_usage(data.get("usage")):
            from ..telemetry.metrics import estimate_tokens
            helix.token_counter.add(
                prompt_tokens=estimate_tokens(user_query),
                completion_tokens=estimate_tokens(content),
                estimated=True,
            )
    except Exception:
        log.debug("Token counter update failed (non-stream)", exc_info=True)

    return JSONResponse(data)


async def _forward_raw(body: dict, config: HelixConfig, helix: Optional[HelixContextManager] = None):
    """Pass request through to upstream without context injection."""
    try:
        async with httpx.AsyncClient(timeout=config.server.upstream_timeout) as client:
            resp = await client.post(
                f"{config.server.upstream}/v1/chat/completions",
                json=body,
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        log.warning("Upstream raw passthrough failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Upstream chat backend unreachable: {exc}",
        ) from exc

    try:
        if resp.status_code >= 400:
            # Mirror the guard in _forward_and_replicate: surface upstream
            # error status/body instead of forwarding an error JSON as HTTP
            # 200, and avoid raising on .json() when the body is non-JSON.
            try:
                err_detail = resp.text[:2000]
            except Exception:
                log.warning(
                    "Upstream error body decode failed (raw)",
                    exc_info=True,
                )
                err_detail = f"Upstream returned HTTP {resp.status_code}"
            log.warning(
                "Upstream /v1/chat/completions returned %d: %s",
                resp.status_code,
                err_detail,
            )
            raise HTTPException(
                status_code=resp.status_code,
                detail=err_detail,
            )
        data = resp.json()
    except ValueError as exc:
        log.warning("Upstream raw passthrough returned non-JSON", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Upstream chat backend returned a non-JSON response.",
        ) from exc

    # Token accounting if helix is wired in.
    if helix is not None:
        try:
            helix.token_counter.add_from_usage(data.get("usage"))
        except Exception:
            log.debug("Token counter update failed (raw)", exc_info=True)

    return JSONResponse(data)


# ── Vault route registration ────────────────────────────────────────


def _register_vault_routes(app: "FastAPI") -> None:
    """Register vault endpoints on the given FastAPI app instance."""
    import re
    import time as _time

    @app.post("/export/obsidian")
    async def post_export_obsidian(request: Request):
        body = await request.json()
        full = bool(body.get("full", False))
        vault = request.app.state.vault
        if full:
            return vault.full_export()
        return vault.incremental_export()

    @app.get("/vault/status")
    async def get_vault_status(request: Request):
        return request.app.state.vault.status()

    @app.post("/vault/trace")
    async def post_vault_trace(body: _TraceBody, request: Request):
        vault = request.app.state.vault
        path = vault.trace_export(**body.model_dump())
        return {"path": str(path), "request_id": body.request_id}

    @app.post("/vault/traces/{request_id}/pin")
    async def post_pin_trace(request_id: str, request: Request):
        vault = request.app.state.vault
        if not vault._started:
            return {"ok": False, "error": "vault disabled"}
        traces_dir = vault.vault_root / "_traces"
        pinned_dir = vault.vault_root / "_traces-pinned"
        pinned_dir.mkdir(exist_ok=True, mode=0o700)
        matches = list(traces_dir.glob(f"*_{request_id}_exp*.md"))
        if not matches:
            return {"ok": False, "error": f"trace {request_id} not found in _traces/"}
        src = matches[0]
        new_name = re.sub(r"_exp\d+\.md$", ".md", src.name)
        dst = pinned_dir / new_name
        src.replace(dst)
        return {"ok": True, "pinned_path": str(dst)}

    @app.post("/vault/traces/{request_id}/unpin")
    async def post_unpin_trace(request_id: str, request: Request):
        vault = request.app.state.vault
        if not vault._started:
            return {"ok": False, "error": "vault disabled"}
        pinned_dir = vault.vault_root / "_traces-pinned"
        traces_dir = vault.vault_root / "_traces"
        matches = list(pinned_dir.glob(f"*_{request_id}.md"))
        if not matches:
            return {"ok": False, "error": f"trace {request_id} not found in _traces-pinned/"}
        src = matches[0]
        retention_hours = vault.config.vault.traces.retention_hours
        expires_unix = int(_time.time() + retention_hours * 3600)
        new_name = src.stem + f"_exp{expires_unix}.md"
        dst = traces_dir / new_name
        src.replace(dst)
        return {"ok": True, "unpinned_path": str(dst)}
