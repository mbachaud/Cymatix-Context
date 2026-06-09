"""Context retrieval routes: /context, /context/packet, /context/refresh-plan,
/v1/chat/completions, /fingerprint.

Extracted from the monolithic server.py -- NO logic changes.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .helpers import (
    _compute_know_or_miss_block,
    _compute_plr_confidence,
    _forward_raw,
    _merge_tier_contributions,
    _munge_messages,
    _resolve_caller_agent,
    _stream_and_tee,
    _forward_and_replicate,
)
from ..schemas import KnowBlock, MissBlock

log = logging.getLogger("helix.server")


def setup_context_routes(app: FastAPI, helix, config, registry, **_kw) -> None:
    """Register context retrieval routes on *app*.

    Routes are defined as closures so they capture *helix*, *config*, and
    *registry* without requiring global state.
    """
    from ..context_packet import build_context_packet, get_refresh_targets
    from ..scoring.know_calibration import load_calibration_from_toml

    # -- Proxy endpoint (primary integration) --------------------------

    @app.post("/v1/chat/completions")
    async def chat_proxy(request: Request, background_tasks: BackgroundTasks):
        body = await request.json()
        messages = body.get("messages", [])

        if not messages:
            return JSONResponse({"error": "No messages provided"}, status_code=400)

        user_query = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_query = msg.get("content", "")
                break

        if not user_query:
            # No user message -- pass through unmodified
            return await _forward_raw(body, config, helix)

        # Step 1-5: Retrieval pipeline
        downstream_model = body.get("model")
        # Budget-zone signal: sum all message content tokens so the
        # pipeline can see how full the caller's window already is.
        # Computed here (not in context_manager) because messages[] is
        # a proxy-layer concept. No-op unless HELIX_BUDGET_ZONE=1.
        try:
            from ..telemetry.metrics import estimate_tokens as _est_tokens
            _prompt_tokens = sum(
                _est_tokens(m.get("content", "") or "") for m in messages
            )
        except Exception:
            _prompt_tokens = None
        # Stage 5 (2026-05-08): /v1/chat/completions proxies whatever
        # caller_model_class the body specifies, defaulting to "generic"
        # which preserves today's behavior for Continue and other Continue-
        # compatible callers. Spec section 3.
        from ..schemas import CallerModelClass, CALLER_MODEL_CLASS_DEFAULT
        _proxy_cmc_raw = body.get("caller_model_class")
        if _proxy_cmc_raw is None:
            _proxy_caller_model_class = CALLER_MODEL_CLASS_DEFAULT
        else:
            try:
                _proxy_caller_model_class = CallerModelClass(str(_proxy_cmc_raw)).value
            except ValueError:
                _proxy_caller_model_class = CALLER_MODEL_CLASS_DEFAULT
        context_window = await helix.build_context_async(
            user_query,
            downstream_model=downstream_model,
            prompt_tokens_hint=_prompt_tokens,
            caller_model_class=_proxy_caller_model_class,
        )

        # Delta-epsilon health signal
        health = context_window.context_health
        log.info(
            "Context health: status=%s ellipticity=%.3f coverage=%.2f "
            "density=%.2f freshness=%.2f genes=%d/%d",
            health.status, health.ellipticity, health.coverage,
            health.density, health.freshness,
            health.genes_expressed, health.genes_available,
        )

        # Munge messages: inject context, apply history stripping
        body["messages"] = _munge_messages(
            messages=messages,
            expressed_context=context_window.expressed_context,
            ribosome_prompt=context_window.ribosome_prompt,
            total_genes=helix.genome.stats()["total_genes"],
            cold_start_threshold=config.genome.cold_start_threshold,
        )

        # Suppress think mode for small models -- their reasoning loops
        # consume the entire output budget without producing answers.
        # Extends to qwen3:4b for extraction-heavy workloads (benchmarks,
        # agent tool-calls) where think tokens add cost without accuracy.
        downstream_model_name = body.get("model", "").lower()
        suppress_think = (
            context_window.metadata.get("moe_mode")
            or downstream_model_name.startswith("qwen3:4b")
            or downstream_model_name.startswith("qwen3:1.7b")
            or downstream_model_name.startswith("qwen3:0.6b")
        )
        if suppress_think:
            body["temperature"] = 0
            # Inject /no_think into user message for Qwen3 think suppression
            for msg in reversed(body["messages"]):
                if msg.get("role") == "user":
                    if not msg["content"].startswith("/no_think"):
                        msg["content"] = "/no_think " + msg["content"]
                    break

        if body.get("stream", False):
            return StreamingResponse(
                _stream_and_tee(body, config, helix, user_query, background_tasks),
                media_type="text/event-stream",
            )
        else:
            return await _forward_and_replicate(body, config, helix, user_query, background_tasks)

    # -- Context endpoint (Continue HTTP context provider format) -------

    def _request_read_only(data: dict) -> bool:
        explicit = data.get("read_only")
        if explicit is not None:
            return bool(explicit)
        # Synthetic benches already use clean=true to mean "isolate this
        # query from prior benchmark state". Treating that as read-only by
        # default also prevents query-time graph writeback from polluting
        # later rows in the same run.
        return bool(data.get("clean", False))

    @app.post("/context")
    async def context_endpoint(request: Request):
        import time as _time
        t0 = _time.time()
        helix._last_activity_ts = t0

        data = await request.json()
        query = data.get("query", "")
        response_mode = str(
            data.get("response_mode", data.get("format", "continue"))
        ).strip().lower()
        decoder_override = data.get("decoder_mode")
        verbose = data.get("verbose", False)  # Agent-mode: include document citations
        # Per-request cold-tier override (C.2 of B->C, 2026-04-10)
        # None  = honor [context] cold_tier_enabled config flag
        # True  = force cold-tier ON for this request
        # False = force cold-tier OFF for this request
        include_cold = data.get("include_cold")
        if include_cold is not None:
            include_cold = bool(include_cold)

        # session_context: optional dict carrying the caller's working
        # context (active_project, active_files). Plumbed through to the
        # path_key_index tier so PKI can fire on (project, key) pairs even
        # when the user's natural query doesn't restate the project name.
        session_context = data.get("session_context")
        if session_context is not None and not isinstance(session_context, dict):
            session_context = None  # ignore malformed input

        # CWoLa label-logger identifiers.
        cwola_session_id = data.get("session_id")
        cwola_party_id = data.get("party_id")
        if cwola_session_id is None and config.session.synthetic_session_enabled:
            import hashlib as _hashlib
            client_ip = request.client.host if request.client else "unknown"
            window_s = max(1, config.session.synthetic_session_window_s)
            bucket_ts = int(t0 // window_s) * window_s
            cwola_session_id = "syn_" + _hashlib.sha1(
                f"{client_ip}:{bucket_ts}".encode("utf-8")
            ).hexdigest()[:12]
        if cwola_party_id is None:
            cwola_party_id = config.session.default_party_id

        # clean=true: reset per-session caches
        if data.get("clean", False):
            try:
                helix.reset_session_state()
            except Exception:
                log.debug("reset_session_state failed", exc_info=True)
        read_only = _request_read_only(data)

        if not query:
            return JSONResponse({"error": "No query provided"}, status_code=400)
        if response_mode not in {"continue", "packet"}:
            return JSONResponse(
                {"error": "Invalid response_mode", "allowed": ["continue", "packet"]},
                status_code=400,
            )

        if response_mode == "packet":
            try:
                max_genes = int(data.get("max_genes", config.budget.max_genes_per_turn))
            except (TypeError, ValueError):
                max_genes = config.budget.max_genes_per_turn
            max_genes = max(1, min(max_genes, 32))
            packet = build_context_packet(
                str(query),
                task_type=str(data.get("task_type", "explain") or "explain"),
                genome=helix.genome,
                max_genes=max_genes,
                now_ts=t0,
                read_only=read_only,
            )
            payload = packet.model_dump()
            payload["response_mode"] = "packet"
            return payload

        # Per-request decoder mode override
        _decoder_override = (
            decoder_override
            if decoder_override in ("full", "condensed", "minimal", "none")
            else None
        )

        # Stage 5 (2026-05-08): caller_model_class opt-in render branch.
        from ..schemas import CallerModelClass, CALLER_MODEL_CLASS_DEFAULT
        _caller_model_class_raw = data.get("caller_model_class")
        if _caller_model_class_raw is None:
            caller_model_class = CALLER_MODEL_CLASS_DEFAULT
        else:
            try:
                caller_model_class = CallerModelClass(str(_caller_model_class_raw)).value
            except ValueError:
                return JSONResponse(
                    {
                        "error": "Invalid caller_model_class",
                        "allowed": [c.value for c in CallerModelClass],
                    },
                    status_code=400,
                )

        # Budget-zone signal
        _prompt_tokens_hint = data.get("prompt_tokens")
        if _prompt_tokens_hint is not None:
            try:
                _prompt_tokens_hint = int(_prompt_tokens_hint)
            except (TypeError, ValueError):
                _prompt_tokens_hint = None

        # Sprint 2 session working-set
        _ignore_delivered = bool(data.get("ignore_delivered", False))

        # Semantic-wiring arm (PRD 2026-06-02): optional per-call query_type,
        # mirroring the /fingerprint thread. The bench injects the needle's
        # ground-truth type so the fixed pipeline can be A/B'd on /context
        # (delivery), not just /fingerprint (recall). Production callers omit
        # it; only "semantic" + HELIX_SEMANTIC_ARM=1 changes retrieval — any
        # other value (or arm off) is inert / byte-identical.
        query_type = (str(data.get("query_type", "")).strip().lower() or None)

        window = await helix.build_context_async(
            query,
            include_cold=include_cold,
            session_context=session_context,
            party_id=cwola_party_id,
            prompt_tokens_hint=_prompt_tokens_hint,
            session_id=cwola_session_id,
            ignore_delivered=_ignore_delivered,
            read_only=read_only,
            decoder_override=_decoder_override,
            caller_model_class=caller_model_class,
            query_type=query_type,
        )

        # Stage 5 section 3: echo caller_model_class on response.metadata
        if window.metadata is not None:
            window.metadata["caller_model_class"] = caller_model_class

        health = window.context_health
        latency_ms = round((_time.time() - t0) * 1000, 1)

        # Stage 6: machine-tagged know/miss block
        try:
            kmblock = _compute_know_or_miss_block(
                helix=helix,
                window=window,
                query=str(query),
            )
        except Exception:
            log.warning("Stage-6 know/miss decision failed", exc_info=True)
            kmblock = None

        # Build base response (Continue-compatible).
        response = {}
        if isinstance(kmblock, KnowBlock):
            response["know"] = kmblock.model_dump()
        elif isinstance(kmblock, MissBlock):
            response["miss"] = kmblock.model_dump()

        response.update({
            "name": "Helix Genome Context",
            "description": (
                f"{health.genes_expressed} genes expressed, "
                f"{window.compression_ratio:.1f}x compression, "
                f"health={health.status} (Δε={health.ellipticity:.2f})"
            ),
            "content": window.expressed_context,
            "context_health": health.model_dump(),
        })

        # Agent-mode fields: structured metadata for programmatic use
        try:
            # Snapshot scores under the lock so we don't see a torn view
            # of last_query_scores while another /context call is
            # mid-write (the writer holds the same lock; see
            # ShardRouter.query_genes / ShardedGenomeAdapter.query_docs).
            _lock = getattr(helix.genome, "_last_query_scores_lock", None)
            if _lock is not None:
                with _lock:
                    scores = dict(helix.genome.last_query_scores or {})
            else:
                scores = dict(helix.genome.last_query_scores or {})
            # Fetch source_id for retrieved documents for citation
            gene_ids = window.expressed_gene_ids or []
            citations = []
            if gene_ids:
                # Polymorphic citation lookup so blob and sharded backends
                # both work. Sharded mode resolves source_id + tags from
                # main.db's fingerprint_index (the genes table on main.db is
                # empty under HELIX_USE_SHARDS=1, so the prior direct SQL
                # silently returned zero rows — see issue #104).
                row_map = helix.genome.get_citation_rows(gene_ids)

                # Session registry citation enrichment
                attribution_map: dict = {}
                try:
                    attribution_map = registry.get_attributions_for_genes(gene_ids)
                except Exception:
                    log.debug("Citation attribution lookup failed", exc_info=True)

                for gid in gene_ids:
                    r = row_map.get(gid)
                    if r is None:
                        continue
                    citation = {
                        "gene_id": gid,
                        "source": r["source_id"],
                        "score": round(scores.get(gid, 0.0), 3),
                    }
                    attribution = attribution_map.get(gid)
                    if attribution:
                        citation["authored_by_party"] = attribution.get("party_id")
                        if attribution.get("handle"):
                            citation["authored_by_handle"] = attribution["handle"]
                    if verbose:
                        if r["domains"]:
                            citation["domains"] = r["domains"][:5]
                        if r["entities"]:
                            citation["entities"] = r["entities"][:5]
                    citations.append(citation)

            # Actionable recommendation for the agent.
            if isinstance(kmblock, MissBlock):
                if kmblock.reason in ("stale", "cold", "superseded"):
                    recommendation = "refresh"
                    targets_preview = ", ".join(
                        kmblock.refresh_targets[:3]
                    ) or "(none)"
                    hint = (
                        f"Genome has a {kmblock.reason} candidate. Do "
                        f"not answer from genome. Re-read "
                        f"refresh_targets and re-call /context: "
                        f"{targets_preview}"
                    )
                else:
                    recommendation = "escalate"
                    hint = (
                        "Genome has no usable signal for query. Do not "
                        "answer from genome. Use a tool from "
                        f"escalate_to: {kmblock.escalate_to}"
                    )
            elif isinstance(kmblock, KnowBlock) and getattr(kmblock, "soft_stale", False):
                recommendation = "refresh"
                hint = (
                    "Top-1 is fresh; supporting context is stale. "
                    "Answer is safe to use, plan a refresh of "
                    "lower-ranked supporting genes."
                )
            elif health.status == "aligned":
                recommendation = "trust"
                hint = "Context is well-grounded. Use directly."
            elif health.status == "sparse":
                recommendation = "verify"
                hint = "Context has gaps. Verify specific values before acting on them."
            elif health.status == "stale":
                recommendation = "refresh"
                hint = "Expressed genes are outdated. Re-ingest source files or verify from disk."
            else:  # denatured
                recommendation = "reread_raw"
                hint = "Context is unreliable. Read raw files instead of trusting the genome."

            # Stage 4 (spec section 9, issue #63) -- calibration staleness surface.
            try:
                from ..scoring.know_calibration import (
                    calibration_age_days as _cal_age_days,
                    is_calibration_stale as _is_cal_stale,
                )
                _cal_for_warn = load_calibration_from_toml()
                _cal_age = _cal_age_days(_cal_for_warn.calibrated_at)
                _cal_stale = _is_cal_stale(
                    _cal_age, _cal_for_warn.stale_after_days,
                )
            except Exception:
                log.debug(
                    "Stage 4 section 9 calibration staleness compute failed",
                    exc_info=True,
                )
                _cal_age = None
                _cal_stale = False

            _warnings: list[str] = []
            if _cal_stale:
                _warnings.append("calibration_stale")

            response["agent"] = {
                "recommendation": recommendation,
                "hint": hint,
                "citations": citations,
                "latency_ms": latency_ms,
                "total_tokens_est": window.total_estimated_tokens,
                "compression_ratio": round(window.compression_ratio, 2),
                "moe_mode": window.metadata.get("moe_mode", False),
                "budget_tier": window.metadata.get("budget_tier", "broad"),
                "budget_tokens_est": window.metadata.get("budget_tokens_est", 15000),
                # C.2 of B->C: cold-tier retrieval markers
                "cold_tier_used": getattr(helix, "_last_cold_tier_used", False),
                "cold_tier_count": getattr(helix, "_last_cold_tier_count", 0),
                # Stage 4 (2026-05-08): calibration provenance
                "ann_threshold_mode": config.retrieval.ann_threshold_mode,
                "abstain_mode": config.abstain.mode,
                # Stage 4 section 9 (issue #63) -- calibration staleness fields.
                "calibration_age_days": _cal_age,
                "calibration_stale": _cal_stale,
                "warnings": _warnings,
            }

            # Activation profile: per-tier score breakdown
            if verbose:
                try:
                    tier_contrib = getattr(helix.genome, "last_tier_contributions", {}) or {}
                    expressed_ids = set(window.expressed_gene_ids or [])
                    activation = {
                        gid: contribs
                        for gid, contribs in tier_contrib.items()
                        if gid in expressed_ids
                    }
                    tier_totals: dict = {}
                    for contribs in activation.values():
                        for tier, score in contribs.items():
                            tier_totals[tier] = tier_totals.get(tier, 0.0) + score
                    response["agent"]["tier_contributions"] = activation
                    response["agent"]["tier_totals"] = {
                        k: round(v, 3) for k, v in tier_totals.items()
                    }
                except Exception:
                    log.debug("Tier contribution surfacing failed", exc_info=True)
        except Exception:
            log.debug("Agent metadata enrichment failed", exc_info=True)

        # CWoLa label logger (STATISTICAL_FUSION sect C2)
        try:
            from ..identity import cwola
            tier_contrib_all = getattr(helix.genome, "last_tier_contributions", {}) or {}
            cwola_tier_totals: dict = {}
            for contribs in tier_contrib_all.values():
                for tier, score in contribs.items():
                    cwola_tier_totals[tier] = cwola_tier_totals.get(tier, 0.0) + score
            expressed = window.expressed_gene_ids or []
            top_gene = expressed[0] if expressed else None

            # PWPC Phase 1 enrichment
            query_sema_vec = None
            top_candidate_sema_vec = None
            try:
                codec = getattr(helix, "_sema_codec", None)
                if codec is not None:
                    query_sema_vec = codec.encode(query)
                if top_gene:
                    gene = helix.genome.get_doc(top_gene)
                    if gene is not None and gene.embedding:
                        top_candidate_sema_vec = gene.embedding
            except Exception:
                log.debug("CWoLa sema enrichment failed", exc_info=True)

            cwola.log_query(
                helix.genome.conn,
                session_id=cwola_session_id,
                party_id=cwola_party_id,
                query=query,
                tier_totals=cwola_tier_totals,
                top_gene_id=top_gene,
                ts=t0,
                query_sema=query_sema_vec,
                top_candidate_sema=top_candidate_sema_vec,
            )
            cwola.sweep_buckets(helix.genome.conn, now=_time.time())
        except Exception:
            log.debug("CWoLa log_query/sweep failed", exc_info=True)

        # OTel latency histogram
        try:
            from ..telemetry import (
                context_latency_histogram,
                context_calls_by_class_counter,
                redact_query,
            )
            agent_label = _resolve_caller_agent(request, data)
            context_latency_histogram().record(
                _time.time() - t0,
                {
                    "health": health.status,
                    "budget_tier": window.metadata.get("budget_tier", "broad"),
                    "cold_tier_used": str(getattr(helix, "_last_cold_tier_used", False)),
                    "class": caller_model_class,
                    "agent": agent_label,
                },
            )
            context_calls_by_class_counter().add(
                1, {"class": caller_model_class, "agent": agent_label},
            )
        except Exception:
            log.warning("OTel /context latency emit failed", exc_info=True)

        return [response]

    @app.post("/context/packet")
    async def context_packet_endpoint(request: Request):
        """Freshness-labeled evidence packet for agent-safe actions."""
        import time as _time

        t0 = _time.time()
        helix._last_activity_ts = t0

        data = await request.json()
        query = data.get("query", "")
        task_type = data.get("task_type", "explain")
        max_genes = data.get("max_genes", 8)
        include_raw = bool(data.get("include_raw", False))
        raw_max = data.get("max_item_chars")
        try:
            max_item_chars = int(raw_max) if raw_max is not None else None
        except (TypeError, ValueError):
            max_item_chars = None
        if data.get("clean", False):
            try:
                helix.reset_session_state()
            except Exception:
                log.debug("reset_session_state failed", exc_info=True)
        read_only = _request_read_only(data)

        if not query or not str(query).strip():
            return JSONResponse({"error": "No query provided"}, status_code=400)

        try:
            max_genes = int(max_genes)
        except (TypeError, ValueError):
            max_genes = 8
        max_genes = max(1, min(max_genes, 32))

        packet = build_context_packet(
            str(query),
            task_type=str(task_type or "explain"),
            genome=helix.genome,
            max_genes=max_genes,
            now_ts=t0,
            read_only=read_only,
            include_raw=include_raw,
            max_item_chars=max_item_chars,
        )
        packet_dict = packet.model_dump()

        # Stage 6 (section 5): lift the know/miss block
        payload: dict = {}
        if packet_dict.get("know") is not None:
            payload["know"] = packet_dict["know"]
        elif packet_dict.get("miss") is not None:
            payload["miss"] = packet_dict["miss"]
        for k, v in packet_dict.items():
            if k in ("know", "miss"):
                continue
            payload[k] = v
        payload["response_mode"] = "packet"

        # PLR query-confidence head
        live_cfg = getattr(helix, "config", config)
        if live_cfg.plr.enabled:
            try:
                plr_block = _compute_plr_confidence(
                    helix, live_cfg, str(query), now_ts=t0,
                )
                if plr_block is not None:
                    payload["plr_confidence"] = plr_block
            except Exception:
                log.warning("plr_confidence compute failed", exc_info=True)

        return payload

    @app.post("/context/refresh-plan")
    async def context_refresh_plan_endpoint(request: Request):
        """Just the refresh-before-action plan for an agent-safe task."""
        import time as _time

        t0 = _time.time()
        helix._last_activity_ts = t0

        data = await request.json()
        query = data.get("query", "")
        task_type = data.get("task_type", "edit")
        max_genes = data.get("max_genes", 8)
        if data.get("clean", False):
            try:
                helix.reset_session_state()
            except Exception:
                log.debug("reset_session_state failed", exc_info=True)
        read_only = _request_read_only(data)

        if not query or not str(query).strip():
            return JSONResponse({"error": "No query provided"}, status_code=400)

        try:
            max_genes = int(max_genes)
        except (TypeError, ValueError):
            max_genes = 8
        max_genes = max(1, min(max_genes, 32))

        targets = get_refresh_targets(
            str(query),
            task_type=str(task_type or "edit"),
            genome=helix.genome,
            max_genes=max_genes,
            now_ts=t0,
            read_only=read_only,
        )
        return {
            "query": str(query),
            "task_type": str(task_type or "edit"),
            "refresh_targets": [t.model_dump() for t in targets],
            "response_mode": "refresh_plan",
        }

    @app.post("/fingerprint")
    async def fingerprint_endpoint(request: Request):
        """Navigation-first retrieval payload with tier scores, not content."""
        import time as _time

        t0 = _time.time()
        helix._last_activity_ts = t0

        data = await request.json()
        query = data.get("query", "")
        if not query:
            return JSONResponse({"error": "No query provided"}, status_code=400)

        profile = str(
            data.get("profile") or config.context.fingerprint_mode_profile
        ).strip().lower()
        if profile not in {"fast", "balanced", "quality"}:
            return JSONResponse(
                {"error": "Invalid profile", "allowed": ["fast", "balanced", "quality"]},
                status_code=400,
            )

        include_cold = data.get("include_cold")
        if include_cold is not None:
            include_cold = bool(include_cold)

        session_context = data.get("session_context")
        if session_context is not None and not isinstance(session_context, dict):
            session_context = None

        if data.get("clean", False):
            try:
                helix.reset_session_state()
            except Exception:
                log.debug("reset_session_state failed", exc_info=True)

        try:
            max_results = int(
                data.get("max_results", config.budget.max_fingerprints_per_turn)
            )
        except (TypeError, ValueError):
            max_results = config.budget.max_fingerprints_per_turn
        max_results = max(1, min(max_results, 200))

        # Optional score_floor
        score_floor_raw = data.get("score_floor")
        if score_floor_raw is None:
            score_floor = 0.0
        else:
            try:
                score_floor = float(score_floor_raw)
            except (TypeError, ValueError):
                return JSONResponse(
                    {"error": "score_floor must be a number"},
                    status_code=400,
                )
            if score_floor < 0:
                return JSONResponse(
                    {"error": "score_floor must be >= 0"},
                    status_code=400,
                )

        # Evaluation budget
        if score_floor > 0:
            eval_budget = min(max(max_results * 3, 50), 200)
        else:
            eval_budget = max_results

        party_id = data.get("party_id")
        if party_id is None:
            party_id = config.session.default_party_id

        # Semantic-wiring arm (PRD 2026-06-02): optional per-call query_type.
        # The bench injects the needle's ground-truth type so the arm can be
        # A/B'd; production callers omit it (a runtime semantic detector is a
        # separate track). Only "semantic" + HELIX_SEMANTIC_ARM=1 changes
        # retrieval; any other value (or arm off) is inert/byte-identical.
        query_type = (str(data.get("query_type", "")).strip().lower() or None)

        expand_query = profile in {"balanced", "quality"}
        use_harmonic = profile == "quality"
        use_sr = profile == "quality"
        use_cymatics = profile == "quality"
        use_harmonic_bin = profile == "quality"
        use_tcm = True

        try:
            expanded_query, domains, entities = helix._prepare_query_signals(
                query,
                session_context=session_context,
                expand_query=expand_query,
            )
            candidates = helix._retrieve(
                domains,
                entities,
                eval_budget,
                query_text=query,
                include_cold=include_cold,
                party_id=party_id,
                use_harmonic=use_harmonic,
                use_sr=use_sr,
                query_type=query_type,
            )
            candidates, refiner_contrib = helix._apply_candidate_refiners(
                query,
                candidates,
                eval_budget,
                use_cymatics=use_cymatics,
                use_harmonic_bin=use_harmonic_bin,
                use_tcm=use_tcm,
                allow_rerank=(profile == "quality"),
            )
        except Exception as exc:
            log.warning("/fingerprint failed: %s", exc, exc_info=True)
            return JSONResponse(
                {"error": type(exc).__name__, "detail": str(exc)},
                status_code=500,
            )

        # Snapshot scores + tier contributions atomically under the
        # writer lock so the (base_scores, last_tier_contributions) pair
        # comes from the same /context call.
        _lock = getattr(helix.genome, "_last_query_scores_lock", None)
        if _lock is not None:
            with _lock:
                base_scores = dict(helix.genome.last_query_scores or {})
                _tier_contribs = dict(getattr(helix.genome, "last_tier_contributions", {}) or {})
        else:
            base_scores = dict(helix.genome.last_query_scores or {})
            _tier_contribs = dict(getattr(helix.genome, "last_tier_contributions", {}) or {})
        merged_tiers = _merge_tier_contributions(
            _tier_contribs,
            refiner_contrib,
        )

        def _final_score(gene_id: str) -> float:
            tcm_bonus = refiner_contrib.get(gene_id, {}).get("tcm", 0.0)
            return float(base_scores.get(gene_id, 0.0) + tcm_bonus)

        evaluated_total = len(candidates)
        above_floor = [g for g in candidates if _final_score(g.gene_id) >= score_floor]
        above_floor_total = len(above_floor)
        truncated = above_floor[:max_results]
        returned = len(truncated)
        filtered_by_floor = evaluated_total - above_floor_total
        truncated_by_cap = above_floor_total - returned

        attribution_map: dict = {}
        if truncated:
            try:
                attribution_map = registry.get_attributions_for_genes(
                    [g.gene_id for g in truncated]
                )
            except Exception:
                log.debug("Fingerprint attribution lookup failed", exc_info=True)

        fingerprints = []
        for rank, g in enumerate(truncated):
            path = None
            if g.promoter and g.promoter.metadata:
                path = g.promoter.metadata.get("path")
            tiers = merged_tiers.get(g.gene_id, {})
            tcm_bonus = refiner_contrib.get(g.gene_id, {}).get("tcm", 0.0)
            row = {
                "rank": rank,
                "gene_id": g.gene_id,
                "score": round(float(base_scores.get(g.gene_id, 0.0) + tcm_bonus), 4),
                "preview": (g.content or "")[:160],
                "path": path,
                "source": g.source_id or "",
                "domains": list(g.promoter.domains) if g.promoter else [],
                "entities": list(g.promoter.entities) if g.promoter else [],
                "chromatin": int(getattr(g, "chromatin", 0) or 0),
                "tier_contributions": {
                    k: round(float(v), 4) for k, v in sorted(tiers.items())
                },
            }
            attribution = attribution_map.get(g.gene_id)
            if attribution:
                row["authored_by_party"] = attribution.get("party_id")
                if attribution.get("handle"):
                    row["authored_by_handle"] = attribution["handle"]
            fingerprints.append(row)

        tier_totals: dict = {}
        for row in fingerprints:
            for tier, score in row["tier_contributions"].items():
                tier_totals[tier] = tier_totals.get(tier, 0.0) + score

        # response_hint
        if returned == 0 and evaluated_total > 0 and score_floor > 0:
            response_hint = (
                f"All {evaluated_total} evaluated candidates fell below "
                f"score_floor={score_floor}; consider lowering it or "
                f"refining the query."
            )
        elif truncated_by_cap > 0:
            response_hint = (
                f"{truncated_by_cap} additional candidates cleared the floor "
                f"but were truncated by max_results={max_results}; raise "
                f"max_results to see more."
            )
        elif filtered_by_floor > 0:
            response_hint = (
                f"{filtered_by_floor} evaluated candidates fell below "
                f"score_floor={score_floor}."
            )
        else:
            response_hint = "No filtering or truncation applied."

        latency_ms = round((_time.time() - t0) * 1000, 1)
        return {
            "mode": "fingerprint",
            "profile": profile,
            "query": query,
            "extracted": {
                "expanded_query": expanded_query,
                "domains": list(domains),
                "entities": list(entities),
            },
            "fingerprints": fingerprints,
            "count": len(fingerprints),
            "max_results": max_results,
            "score_floor": score_floor,
            "evaluated_total": evaluated_total,
            "above_floor_total": above_floor_total,
            "returned": returned,
            "filtered_by_floor": filtered_by_floor,
            "truncated_by_cap": truncated_by_cap,
            "response_hint": response_hint,
            "agent": {
                "recommendation": "triage",
                "hint": "Use tier fingerprints to decide which genes to fetch in full.",
                "latency_ms": latency_ms,
                "cold_tier_used": getattr(helix, "_last_cold_tier_used", False),
                "cold_tier_count": getattr(helix, "_last_cold_tier_count", 0),
                "tier_totals": {k: round(v, 4) for k, v in sorted(tier_totals.items())},
            },
        }
