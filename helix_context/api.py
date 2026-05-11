"""
Helix Context — shared-lib API boundary.

DESIGN SKETCH (2026-05-11). The function bodies delegate to
``HelixContextManager``; this module is the *interface contract* the
three surfaces (MCP, CLI, FastAPI) all import from. Implementation
bodies will be filled in once the daemon protocol and session-aware
bench shape are locked.

Why this file exists
--------------------
Helix is moving to a three-surface architecture. All three surfaces
import from this module so behavior is single-sourced:

::

    helix-cli (subcommand)         ─┐
    helix-mcp (mcp tool)           ─┼──► helix_context.api ──► HelixContextManager
    helix.serve (FastAPI / daemon) ─┘

- **MCP** — ambient agent tool. One rich call: ``helix.query(text, k)``.
  Plus ``helix.help()`` advertising the CLI escape hatch.
- **CLI** — agent autonomy + human ops. Multi-stage, label/drift,
  diagnostics. The agent reaches for it when it needs to walk the
  genome.
- **FastAPI** — cross-device transport + telemetry surface. Existing
  endpoints stay; the new ones (e.g. ``/v2/query``) become thin
  wrappers over ``api.query``.

Sessions (v1 = stub; v2 adds adaptive caps)
-------------------------------------------
A ``HelixSession`` always carries a ``session_id`` so telemetry can
group calls. In v1 (cold-start CLI shipping first) the session is a
pure tagging construct: the adaptive-cap heuristic, daemon-tracked
state, and per-session profile evolution are all **deferred to v2**
(see ``docs/architecture/HELIX_DAEMON_DESIGN.md`` — currently parked
behind initial-benchmarking results).

Module-level convenience functions (``api.query(...)``, etc.) wrap a
one-shot session for callers that don't want to manage lifecycle.

Read-only by default
--------------------
``query()`` does NOT trigger background replication. Pass
``learn=True`` to opt into the write-back-to-genome path. This makes
the CLI safe to call repeatedly (an agent loop will not silently
mutate the genome by reading from it).

Surfaces should NOT import from ``helix_context.context_manager``
directly. If a method is missing on this boundary, add it here first.

See:
  * ``docs/architecture/HELIX_DAEMON_DESIGN.md`` — daemon spec; deferred to v1.x
  * ``docs/benchmarks/SESSION_AWARE_BENCH_DESIGN.md`` — multi-turn walk bench
  * ``helix_context/context_manager.py`` — implementation behind the delegating calls
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import HelixConfig
from .schemas import (
    ContextItem,
    ContextPacket,
    ContextWindow,
    Gene,
    KnowBlock,
    MissBlock,
    RefreshTarget,
)


# ── Result types ──────────────────────────────────────────────────────
#
# Most queries return existing pydantic models from ``schemas.py``.
# A handful of CLI-shaped results need lighter dataclasses so the JSON
# output is unambiguous without pydantic's nested-model envelopes.


@dataclass
class QueryResult:
    """The headline shape returned by ``HelixSession.query``.

    A ``ContextWindow`` is the full pipeline output; ``QueryResult``
    is the agent-facing projection: the bytes the agent will read,
    plus the structured know/miss verdict, the document IDs that
    contributed (for follow-up walks), and a one-string
    ``decision_reason`` that explains *why* the verdict was returned.
    """
    expressed_context: str
    document_ids: List[str]            # public name; underlying type is still Gene IDs
    know: Optional[KnowBlock] = None
    miss: Optional[MissBlock] = None
    estimated_tokens: int = 0
    # One-string explanation of why the verdict landed where it did.
    # Populated by the pipeline (e.g. "no trusted candidate crossed
    # threshold", "top-1 dominated by score gap"). Without this field
    # the bench loop stays muddy — agents and humans both can't
    # diagnose verdict outcomes from the bytes alone.
    decision_reason: str = ""
    # Synthesized agent next-step hint composed from miss.escalate_to
    # + miss.refresh_targets, or "answer_from_evidence" on know.
    next_action: str = ""
    # The raw ContextWindow for callers that want everything
    # (FastAPI passthrough, debug CLI). Excluded from default JSON
    # serialization — see ``to_agent_json``.
    raw: Optional[ContextWindow] = None

    @property
    def verdict(self) -> str:
        """``"know"`` | ``"miss"`` | ``"unknown"`` (when neither block was set)."""
        if self.know is not None:
            return "know"
        if self.miss is not None:
            return "miss"
        return "unknown"

    def to_agent_json(self) -> Dict[str, Any]:
        """Compact JSON view for agent consumption (CLI ``--json``,
        MCP tool return). Drops ``raw`` to keep the wire small. Uses
        the boring public vocabulary (verdict / evidence / next_action)
        rather than the internal one (gene / genome / chromatin)."""
        out: Dict[str, Any] = {
            "verdict": self.verdict,
            "expressed_context": self.expressed_context,
            "evidence": self.document_ids,
            "estimated_tokens": self.estimated_tokens,
            "decision_reason": self.decision_reason,
            "next_action": self.next_action,
        }
        if self.know is not None:
            out["know"] = self.know.model_dump()
        if self.miss is not None:
            out["miss"] = self.miss.model_dump()
        return out


@dataclass
class IngestResult:
    """Returned from ``HelixSession.ingest``."""
    gene_ids: List[str]
    chunks: int
    bytes_written: int = 0


@dataclass
class StatsResult:
    """Lightweight stats projection — ``HelixSession.stats``.

    Mirror of the ``GET /stats`` endpoint output. Kept narrow on
    purpose: anything richer goes through the FastAPI surface.
    """
    total_genes: int
    total_codons: int
    chromatin_open: int
    chromatin_eu: int
    chromatin_hetero: int
    compression_ratio: float
    metadata: Dict[str, Any] = field(default_factory=dict)


# ── Sessions ──────────────────────────────────────────────────────────


class HelixSession:
    """Stateful handle to a helix-context instance.

    One session per logical agent conversation. Carries the session_id
    that telemetry stamps on every emitted span and the adaptive tier
    caps that evolve as the agent walks the genome.

    Lifecycle:
      * Constructed by ``open_session()`` (preferred) or directly when
        the caller already owns a configured ``HelixContextManager``.
      * ``close()`` flushes pending replication, releases handles, and
        emits the final session-summary span.
      * Can be used as a context manager (``with open_session() as s:``).

    Thread-safety:
      * Sessions are NOT thread-safe. One session per agent / per
        worker. Concurrent calls on the same session may interleave
        adaptive-cap state updates incorrectly.
      * Multiple sessions on the same ``HelixContextManager`` are safe;
        the manager itself handles SQLite-level concurrency.
    """

    def __init__(
        self,
        manager: "Any",  # Forward ref — HelixContextManager — avoids circular import
        *,
        session_id: Optional[str] = None,
        adaptive_caps: bool = False,  # v1: stub. v2: see HELIX_DAEMON_DESIGN.md
    ) -> None:
        self._manager = manager
        self.session_id: str = session_id or self._generate_session_id()
        # v1: adaptive_caps is a no-op flag. The AdaptiveCap heuristic
        # is part of the deferred daemon work (see HELIX_DAEMON_DESIGN.md).
        # We keep the flag + history hook so the v2 wiring is purely
        # additive (no API breakage).
        self.adaptive_caps = adaptive_caps
        self._call_history: List[Dict[str, Any]] = []
        self._closed = False

    # ── Core agent surface (v1) ──────────────────────────────────────

    def query(
        self,
        text: str,
        *,
        k: Optional[int] = None,
        decoder_mode: Optional[str] = None,
        downstream_model: Optional[str] = None,
        include_cold: Optional[bool] = None,
        caller_model_class: str = "generic",
        ignore_delivered: bool = False,
        learn: bool = False,
    ) -> QueryResult:
        """Run the helix retrieval pipeline for ``text``.

        **Read-only by default.** No background replication is
        triggered unless ``learn=True``. This makes the CLI safe to
        call repeatedly; the agent will not silently mutate the
        genome by reading from it.

        Args:
            text: The query string.
            k: Cap on returned documents. ``None`` honors the static
               config (per-session adaptive caps deferred to v2).
            decoder_mode: Override classifier-picked decoder mode.
               One of {"navigation", "answer", "synthesis", ...}.
               ``None`` = let the classifier pick.
            downstream_model: Hint for MoE/small-model detection at the
               compression layer. Pass-through to ``build_context``.
            include_cold: Per-call override for cold-tier retrieval.
            caller_model_class: Render-branch selector (Stage 5).
               One of {"generic", "small_moe", "frontier"}.
            ignore_delivered: Skip the "already delivered this session"
               filter — useful when the agent re-queries to get the
               same document with a fresh splice.
            learn: When ``True``, the pipeline writes the query (and
               eventually the response, via ``HelixSession.learn``)
               back into the genome. Defaults to ``False`` so the CLI
               default is non-mutating.

        Returns:
            ``QueryResult`` with the expressed bytes, contributing
            document IDs, the know/miss verdict, and a one-string
            ``decision_reason`` that explains why the verdict landed.
        """
        cw: ContextWindow = self._manager.build_context(
            query=text,
            downstream_model=downstream_model,
            include_cold=include_cold,
            session_id=self.session_id,
            ignore_delivered=ignore_delivered,
            read_only=not learn,
            decoder_override=decoder_mode,
            caller_model_class=caller_model_class,
        )
        # ContextWindow.metadata carries know/miss when populated by
        # the route layer. The api boundary surfaces them as first-
        # class fields on QueryResult so the CLI/MCP can branch
        # without dict-poking.
        meta = cw.metadata or {}
        know = meta.get("know") if isinstance(meta.get("know"), KnowBlock) else None
        miss = meta.get("miss") if isinstance(meta.get("miss"), MissBlock) else None
        decision_reason = str(meta.get("decision_reason") or "")
        next_action = self._synthesize_next_action(know, miss, meta)
        result = QueryResult(
            expressed_context=cw.expressed_context,
            document_ids=list(cw.expressed_gene_ids),
            know=know,
            miss=miss,
            estimated_tokens=cw.total_estimated_tokens,
            decision_reason=decision_reason,
            next_action=next_action,
            raw=cw,
        )
        if self.adaptive_caps:
            self._record_call_for_adaptation(text, result)
        return result

    @staticmethod
    def _synthesize_next_action(
        know: Optional[KnowBlock],
        miss: Optional[MissBlock],
        meta: Dict[str, Any],
    ) -> str:
        """Compose a single-string agent hint from the verdict block.

        On ``know`` → "answer_from_evidence".
        On ``miss`` → join escalate_to / refresh_targets into one hint.
        Otherwise → empty string (the agent gets bytes, no directive).
        """
        if know is not None:
            if getattr(know, "soft_stale", False):
                return "answer_from_evidence_then_refresh"
            return "answer_from_evidence"
        if miss is not None:
            if miss.refresh_targets:
                return f"refresh:{','.join(miss.refresh_targets[:3])}"
            if miss.escalate_to:
                return f"escalate:{','.join(miss.escalate_to)}"
            return "abstain"
        return ""

    def ingest(
        self,
        content: str,
        *,
        content_type: str = "text",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> IngestResult:
        """Add ``content`` to the genome. See ``HelixContextManager.ingest``."""
        gene_ids = self._manager.ingest(
            content=content,
            content_type=content_type,
            metadata=metadata,
        )
        return IngestResult(
            gene_ids=list(gene_ids),
            chunks=len(gene_ids),
            bytes_written=len(content.encode("utf-8")),
        )

    def stats(self) -> StatsResult:
        """Lightweight stats. Full surface lives at ``/stats`` HTTP."""
        raw = self._manager.stats() or {}
        return StatsResult(
            total_genes=int(raw.get("total_genes", 0) or 0),
            total_codons=int(raw.get("total_codons", 0) or 0),
            chromatin_open=int(raw.get("chromatin_open", 0) or 0),
            chromatin_eu=int(raw.get("chromatin_euchromatin", 0) or 0),
            chromatin_hetero=int(raw.get("chromatin_heterochromatin", 0) or 0),
            compression_ratio=float(raw.get("compression_ratio", 0.0) or 0.0),
            metadata={
                k: v
                for k, v in raw.items()
                if k not in {
                    "total_genes", "total_codons",
                    "chromatin_open", "chromatin_euchromatin",
                    "chromatin_heterochromatin", "compression_ratio",
                }
            },
        )

    # ── Walk-aware surface (v1) ──────────────────────────────────────
    #
    # These power the "agent walks the corpus" workflow: query for
    # candidates, drill into a specific document, fetch related ones,
    # re-query with a refined understanding. Public method names use
    # the boring vocabulary (document/related/corpus); the underlying
    # types stay ``Gene`` until the schema-level rename lands.

    def document(self, document_id: str) -> Optional[Gene]:
        """Fetch a single document by id. Returns ``None`` if not found."""
        # TODO: delegate to manager.genome.get_gene(document_id) when
        # the read-path API is wired in v1 implementation.
        raise NotImplementedError("Wire to Genome.get_gene in v1 implementation")

    def related(
        self,
        document_id: str,
        *,
        depth: int = 1,
        relation: Optional[str] = None,
    ) -> List[Gene]:
        """Return co-activated / structurally-related documents.

        Powers the agent's graph walk: 'show me documents connected to
        the one you just gave me'. The ``relation`` filter narrows to
        a specific NLRelation or StructuralRelation.
        """
        raise NotImplementedError(
            "Wire to genome.list_neighbors / typed_co_activated in v1"
        )

    def packet(self, query: str, *, task_type: str = "default") -> ContextPacket:
        """Agent-safe verified/stale_risk/refresh_targets bundle.

        The packet shape (see ``schemas.ContextPacket``) is the richer
        sibling to ``query``: same retrieval pipeline, different
        framing. Use when the agent needs structured freshness signals
        before it acts on retrieved context.
        """
        raise NotImplementedError(
            "Delegate to the existing /context/packet builder in v1"
        )

    # ── Replication surface (v1.1) ──────────────────────────────────

    def learn(self, query: str, response: str, *, timeout_s: float = 15.0) -> Optional[str]:
        """Pack a query+response exchange back into the genome.

        Background replication; returns the new gene_id when synchronous,
        or ``None`` when the call is queued.
        """
        return self._manager.learn(query=query, response=response, timeout_s=timeout_s)

    def consolidate(self) -> List[str]:
        """Rewrite stale gene bodies from their source fingerprints.

        Returns the list of gene_ids that were rewritten. Idempotent.
        """
        return self._manager.consolidate_session()

    # ── Session lifecycle ────────────────────────────────────────────

    def reset(self) -> None:
        """Clear per-session state without closing. Useful when a
        single CLI process serves many logical sessions."""
        self._call_history.clear()
        self._manager.reset_session_state()

    def close(self) -> None:
        """Flush pending replication, release per-session handles."""
        if self._closed:
            return
        self._closed = True
        # Daemon-owned managers are NOT closed here; only the
        # session-local state is released. Cold-start callers go
        # through close_manager() to also tear down the manager.

    def __enter__(self) -> "HelixSession":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ── Internals ────────────────────────────────────────────────────

    @staticmethod
    def _generate_session_id() -> str:
        import uuid
        return f"sess-{uuid.uuid4().hex[:16]}"

    def _record_call_for_adaptation(self, text: str, result: QueryResult) -> None:
        """No-op in v1 — hook reserved for v2 daemon adaptive caps.

        When the daemon ships, this method will append to the call
        history that drives the AdaptiveCap heuristic in
        ``docs/architecture/HELIX_DAEMON_DESIGN.md``. Until then we
        keep the call site so v2 wiring is purely additive (no API
        breakage). Triggered only when ``adaptive_caps=True`` was
        passed to the session (default False in v1).
        """
        self._call_history.append({
            "query": text,
            "document_ids": result.document_ids,
            "tokens": result.estimated_tokens,
            "had_know": result.know is not None,
            "had_miss": result.miss is not None,
        })
        if len(self._call_history) > 32:
            del self._call_history[: len(self._call_history) - 32]


# ── Module-level convenience ──────────────────────────────────────────


_DEFAULT_MANAGER: Optional[Any] = None  # cached one-shot manager for module-level calls


def open_session(
    *,
    config: Optional[HelixConfig] = None,
    session_id: Optional[str] = None,
    adaptive_caps: bool = True,
) -> HelixSession:
    """Construct a session backed by a fresh or cached manager.

    Surface notes:
      * **Daemon mode**: the daemon process holds long-lived sessions.
        Cold-start clients go through this constructor to spin up a
        local manager.
      * **Embedded use** (FastAPI, tests, scripts): same path. The
        manager is reused across calls in the same process.
    """
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        from .context_manager import HelixContextManager  # late import
        cfg = config or HelixConfig()
        _DEFAULT_MANAGER = HelixContextManager(config=cfg)
    return HelixSession(
        manager=_DEFAULT_MANAGER,
        session_id=session_id,
        adaptive_caps=adaptive_caps,
    )


def close_manager() -> None:
    """Tear down the cached one-shot manager. Long-lived processes
    (daemon, FastAPI server) own their manager directly; this is for
    cold-start CLI cleanup."""
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is not None:
        try:
            _DEFAULT_MANAGER.close()
        except Exception:
            pass
        _DEFAULT_MANAGER = None


def query(text: str, *, k: Optional[int] = None, **kwargs: Any) -> QueryResult:
    """One-shot module-level query. Opens (or reuses) a session and
    returns the result. Suitable for ``python -c "from helix_context
    import api; print(api.query('...').expressed_context)"`` and for
    quick MCP wrappers that don't need to manage session lifecycle."""
    sess = open_session()
    return sess.query(text, k=k, **kwargs)


def ingest(content: str, *, content_type: str = "text", **kwargs: Any) -> IngestResult:
    """One-shot module-level ingest. See ``query`` for lifecycle notes."""
    sess = open_session()
    return sess.ingest(content, content_type=content_type, **kwargs)


def stats() -> StatsResult:
    """One-shot module-level stats. See ``query`` for lifecycle notes."""
    sess = open_session()
    return sess.stats()


# ── What this module deliberately does NOT expose ─────────────────────
#
# Belongs on the FastAPI surface (cross-device telemetry / ops):
#   * /metrics, /health, /admin/*, /sessions/*, /hitl/*
#
# Belongs on the CLI but not in this lib (CLI-specific UX):
#   * argparse subcommand definitions, TTY-aware output formatting,
#     `helix label set`, `helix drift baseline/compare`, `helix diag genome`
#
# Belongs in the future "walk-aware" v1.1+:
#   * `walk(seed_gene_id, max_hops)` — multi-hop traversal as a
#     single call (an alternative to the agent calling
#     `gene + neighbors` repeatedly via the CLI)
#   * `score_against(query, gene_ids)` — re-rank a caller-supplied
#     set against a query (lets external retrievers fuse with helix)


__all__ = [
    "QueryResult",
    "IngestResult",
    "StatsResult",
    "HelixSession",
    "open_session",
    "close_manager",
    "query",
    "ingest",
    "stats",
]
