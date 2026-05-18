"""Admin, health, stats, debug, bridge, and replication routes.

Extracted from the monolithic server.py -- NO logic changes.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import helpers as _helpers
from .helpers import (
    _merge_tier_contributions,
    _paused_ribosomes,
)

log = logging.getLogger("helix.server")


def setup_admin_routes(app: FastAPI, helix, config, registry, bridge, **_kw) -> None:
    """Register admin, health, stats, debug, bridge, and replication routes."""
    from ..accel import json_dumps, json_loads

    # -- Stats endpoint ------------------------------------------------

    @app.get("/stats")
    async def stats_endpoint():
        # Refresh OTel gauges
        try:
            from ..telemetry import emit_gauges_snapshot
            emit_gauges_snapshot(helix.genome)
        except Exception:
            log.debug("telemetry gauges snapshot failed", exc_info=True)
        return helix.stats()

    # -- Resonance introspection endpoint -------------------------------

    @app.get("/debug/resonance")
    async def resonance_endpoint(query: str, k: int = 10, downsample: int = 64):
        import json as _json

        try:
            genome = helix.genome
            codec = getattr(helix, "_sema_codec", None)

            from ..scoring.cymatics import query_spectrum, cached_doc_spectrum, resonance_score
            q_spec = query_spectrum(query)
            q_sema = codec.encode(query) if codec is not None else None

            neighbors: list = []
            if q_sema is not None:
                rows = genome.read_conn.execute(
                    "SELECT gene_id, embedding FROM genes "
                    "WHERE embedding IS NOT NULL AND chromatin < 2 "
                    "LIMIT 20000"
                ).fetchall()
                scored = []
                for r in rows:
                    try:
                        vec = _json.loads(r["embedding"])
                    except Exception:
                        continue
                    sim = codec.similarity(q_sema, vec)
                    scored.append((sim, r["gene_id"]))
                scored.sort(key=lambda x: x[0], reverse=True)
                top = scored[:k]

                for sim, gid in top:
                    g = genome.get_doc(gid)
                    if g is None:
                        continue
                    try:
                        g_spec = cached_doc_spectrum(g)
                        cym_sim = resonance_score(q_spec, g_spec)
                    except Exception:
                        cym_sim = 0.0
                    path = None
                    if g.promoter and g.promoter.metadata:
                        path = g.promoter.metadata.get("path")
                    chrom = 0
                    if g.epigenetics:
                        chrom = getattr(g.epigenetics, "chromatin", 0)
                    neighbors.append({
                        "gene_id": gid,
                        "sema_cos_sim": round(float(sim), 4),
                        "cymatic_cos_sim": round(float(cym_sim), 4),
                        "path": path,
                        "preview": (g.content or "")[:120],
                        "chromatin": chrom,
                    })

            edges: list = []
            if neighbors:
                ids = [n["gene_id"] for n in neighbors]
                placeholders = ",".join("?" * len(ids))
                edge_rows = genome.read_conn.execute(
                    f"SELECT gene_id_a, gene_id_b, weight, source FROM harmonic_links "
                    f"WHERE gene_id_a IN ({placeholders}) AND gene_id_b IN ({placeholders})",
                    (*ids, *ids),
                ).fetchall()
                for r in edge_rows:
                    edges.append({
                        "from": r[0], "to": r[1],
                        "weight": round(float(r[2]), 4),
                        "source": r[3],
                    })

            def _downsample(spec, n):
                if len(spec) <= n:
                    return [round(float(x), 4) for x in spec]
                step = len(spec) / n
                out = []
                for i in range(n):
                    lo = int(i * step)
                    hi = int((i + 1) * step)
                    chunk = spec[lo:hi] or [spec[lo]]
                    out.append(round(sum(chunk) / len(chunk), 4))
                return out

            return {
                "query": query,
                "query_sema": [round(float(x), 4) for x in q_sema] if q_sema is not None else None,
                "query_spectrum": _downsample(q_spec, downsample),
                "spectrum_bins": downsample,
                "spectrum_bins_raw": len(q_spec),
                "neighbors": neighbors,
                "edges": edges,
                "edge_count": len(edges),
                "k": k,
                "sema_available": codec is not None,
            }
        except Exception:
            log.error(
                "/debug/resonance failed for query=%r", query, exc_info=True
            )
            raise HTTPException(status_code=500, detail="Internal error")

    # -- Debug: single-document fetch ---

    @app.get("/genes/{gene_id}")
    async def gene_get_endpoint(gene_id: str):
        """Fetch a single document by ID."""
        try:
            gene = helix.genome.get_doc(gene_id)
        except Exception as exc:
            log.warning("/genes/%s failed: %s", gene_id, exc, exc_info=True)
            return JSONResponse(
                {"error": f"Gene lookup failed: {exc}"}, status_code=500,
            )
        if gene is None:
            return JSONResponse(
                {"error": f"Unknown gene_id: {gene_id}"}, status_code=404,
            )
        return gene.model_dump()

    # -- Debug: lightweight SEMA neighbors ---

    @app.get("/debug/neighbors")
    async def neighbors_endpoint(query: str, k: int = 10):
        """Top-k SEMA neighbors for ``query``."""
        import json as _json

        try:
            rows = helix.genome.read_conn.execute(
                "SELECT gene_id, embedding FROM genes "
                "WHERE embedding IS NOT NULL AND chromatin < 2 "
                "LIMIT 20000"
            ).fetchall()
            if not rows:
                return {
                    "query": query,
                    "k": k,
                    "count": 0,
                    "neighbors": [],
                }
            codec = getattr(helix, "_sema_codec", None)
            if codec is None:
                return JSONResponse(
                    {
                        "error": "SEMA codec not available",
                        "hint": "Ingest must have populated embeddings first.",
                    },
                    status_code=503,
                )
            q_sema = codec.encode(query)
            scored: list = []
            for r in rows:
                try:
                    vec = _json.loads(r["embedding"])
                except Exception:
                    continue
                sim = codec.similarity(q_sema, vec)
                scored.append((sim, r["gene_id"]))
            scored.sort(key=lambda x: x[0], reverse=True)
            top = scored[:k]

            neighbors: list = []
            for sim, gid in top:
                g = helix.genome.get_doc(gid)
                if g is None:
                    continue
                path = None
                if g.promoter and g.promoter.metadata:
                    path = g.promoter.metadata.get("path")
                neighbors.append({
                    "gene_id": gid,
                    "sema_cos_sim": round(float(sim), 4),
                    "preview": (g.content or "")[:160],
                    "path": path,
                })
            return {
                "query": query,
                "k": k,
                "neighbors": neighbors,
                "count": len(neighbors),
            }
        except Exception:
            log.error("/debug/neighbors failed", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal error")

    # -- Debug: context-pipeline dry run (no splice) ---

    @app.get("/debug/preview")
    async def preview_endpoint(
        query: str,
        max_genes: int = 12,
        profile: str = "balanced",
        score_floor: float = 0.0,
    ):
        """Dry-run the retrieval pipeline up to candidate selection."""
        profile = str(profile or "balanced").strip().lower()
        if profile not in {"fast", "balanced", "quality"}:
            return JSONResponse(
                {"error": "Invalid profile", "allowed": ["fast", "balanced", "quality"]},
                status_code=400,
            )

        if score_floor < 0:
            return JSONResponse(
                {"error": "score_floor must be >= 0"},
                status_code=400,
            )

        if score_floor > 0:
            eval_budget = min(max(max_genes * 3, 50), 200)
        else:
            eval_budget = max_genes

        expand_query = profile in {"balanced", "quality"}
        use_harmonic = profile == "quality"
        use_sr = profile == "quality"
        use_cymatics = profile == "quality"
        use_harmonic_bin = profile == "quality"
        use_tcm = True

        try:
            expanded_query, domains, entities = helix._prepare_query_signals(
                query,
                session_context=None,
                expand_query=expand_query,
            )
            candidates = helix._retrieve(
                domains=domains,
                entities=entities,
                max_genes=eval_budget,
                query_text=query,
                use_harmonic=use_harmonic,
                use_sr=use_sr,
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
        except Exception:
            log.error("/debug/preview failed", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal error")

        scores = dict(helix.genome.last_query_scores or {})
        merged_tiers = _merge_tier_contributions(
            getattr(helix.genome, "last_tier_contributions", {}) or {},
            refiner_contrib,
        )

        def _final_score(gene_id: str) -> float:
            tcm_bonus = refiner_contrib.get(gene_id, {}).get("tcm", 0.0)
            return float(scores.get(gene_id, 0.0) + tcm_bonus)

        evaluated_total = len(candidates)
        above_floor = [g for g in candidates if _final_score(g.gene_id) >= score_floor]
        above_floor_total = len(above_floor)
        truncated = above_floor[:max_genes]
        returned = len(truncated)
        filtered_by_floor = evaluated_total - above_floor_total
        truncated_by_cap = above_floor_total - returned

        result = []
        for rank, g in enumerate(truncated):
            path = None
            if g.promoter and g.promoter.metadata:
                path = g.promoter.metadata.get("path")
            result.append({
                "rank": rank,
                "gene_id": g.gene_id,
                "score": round(_final_score(g.gene_id), 4),
                "preview": (g.content or "")[:160],
                "path": path,
                "domains": list(g.promoter.domains) if g.promoter else [],
                "entities": list(g.promoter.entities) if g.promoter else [],
                "chromatin": int(getattr(g, "chromatin", 0) or 0),
                "tier_contributions": {
                    k: round(float(v), 4)
                    for k, v in sorted(merged_tiers.get(g.gene_id, {}).items())
                },
            })

        if returned == 0 and evaluated_total > 0 and score_floor > 0:
            response_hint = (
                f"All {evaluated_total} evaluated candidates fell below "
                f"score_floor={score_floor}; consider lowering it or "
                f"refining the query."
            )
        elif truncated_by_cap > 0:
            response_hint = (
                f"{truncated_by_cap} additional candidates cleared the floor "
                f"but were truncated by max_genes={max_genes}; raise "
                f"max_genes to see more."
            )
        elif filtered_by_floor > 0:
            response_hint = (
                f"{filtered_by_floor} evaluated candidates fell below "
                f"score_floor={score_floor}."
            )
        else:
            response_hint = "No filtering or truncation applied."

        return {
            "query": query,
            "profile": profile,
            "extracted": {
                "expanded_query": expanded_query,
                "domains": list(domains),
                "entities": list(entities),
            },
            "candidates": result,
            "fingerprints": result,
            "count": len(result),
            "max_genes": max_genes,
            "score_floor": score_floor,
            "evaluated_total": evaluated_total,
            "above_floor_total": above_floor_total,
            "returned": returned,
            "filtered_by_floor": filtered_by_floor,
            "truncated_by_cap": truncated_by_cap,
            "response_hint": response_hint,
            "note": "Splice step skipped; these are pre-splice candidates.",
        }

    # -- Health history endpoint ---

    @app.get("/health/history")
    async def health_history_endpoint(limit: int = 50):
        return helix.genome.health_history(limit=limit)

    # -- Token metrics endpoint ---

    @app.get("/metrics/tokens")
    async def metrics_tokens_endpoint():
        """Session + lifetime token counters."""
        try:
            return helix.token_counter.snapshot()
        except Exception as exc:
            log.warning("Token metrics snapshot failed: %s", exc, exc_info=True)
            return JSONResponse(
                {"error": f"Token snapshot failed: {exc}"},
                status_code=500,
            )

    # -- Health endpoint ---

    @app.get("/health")
    async def health_endpoint():
        ribosome_disabled = getattr(helix.ribosome.backend, "is_disabled_backend", False)
        ribosome_model = "disabled" if ribosome_disabled else "unknown"
        if not ribosome_disabled and hasattr(helix.ribosome, "backend") and hasattr(helix.ribosome.backend, "model"):
            ribosome_model = helix.ribosome.backend.model
        elif not ribosome_disabled and hasattr(helix.ribosome, "ollama_ribosome"):
            ribosome_model = f"deberta+{helix.ribosome.ollama_ribosome.backend.model}"

        genome_ready = True
        total_genes = 0
        try:
            total_genes = helix.genome.stats()["total_genes"]
        except Exception:
            genome_ready = False
            log.warning("/health genome stats failed", exc_info=True)

        # Late-bind via the package module so monkeypatch in tests
        # (``monkeypatch.setattr(server_mod, "_probe_upstream", ...)``)
        # takes effect.
        import helix_context.server as _srv
        _probe_fn = getattr(_srv, "_probe_upstream", _helpers._probe_upstream)
        upstream_probe = await asyncio.to_thread(
            _probe_fn, config.server.upstream
        )
        upstream_reachable = bool(upstream_probe.get("reachable"))
        status = "ok" if genome_ready and upstream_reachable else "degraded"

        if status == "ok":
            message = "Helix and its upstream model server answered readiness checks."
        elif not genome_ready and not upstream_reachable:
            message = "Genome stats failed and the upstream model server is unreachable."
        elif not genome_ready:
            message = "Genome stats failed; inspect the local knowledge store."
        else:
            message = "Upstream model server is unreachable; final chat proxy calls will fail."

        from ..hardware import get_hardware
        hw_info = get_hardware()
        low_vram = bool(
            hw_info.vram_total_gb is not None
            and hw_info.vram_total_gb < config.hardware.low_vram_threshold_gb
        )
        hardware_block = {
            "device": hw_info.device,
            "device_name": hw_info.device_name,
            "requested_device": hw_info.requested_device,
            "fallback_active": hw_info.fallback_reason is not None,
            "fallback_reason": hw_info.fallback_reason,
            "vram_total_gb": hw_info.vram_total_gb,
            "system_ram_gb": hw_info.system_ram_gb,
            "low_vram_warning": low_vram,
        }

        calibration_block = {
            "ann_threshold_mode": config.retrieval.ann_threshold_mode,
            "abstain_mode": config.abstain.mode,
            "abstain_classes": sorted(config.abstain.per_class.keys()),
        }
        try:
            ann_meta = helix.genome.get_calibration_provenance()
        except Exception:
            ann_meta = None
            log.debug("/health calibration provenance read failed", exc_info=True)
        if ann_meta is not None:
            calibration_block["ann_threshold"] = ann_meta

        configured_backend = (
            config.ribosome.normalized_backend if config.ribosome.enabled else None
        )
        return {
            "status": status,
            "message": message,
            # OS pid of the process answering this request. Lets callers
            # (e.g. the bench orchestrator's _wait_healthy) confirm they
            # are talking to the process they just spawned, not a stale
            # server that lost a port-bind race. See issue #127.
            "pid": os.getpid(),
            "ribosome": ribosome_model,
            "ribosome_backend": config.ribosome.effective_backend,
            "ribosome_configured_backend": configured_backend,
            "ribosome_cost_class": config.ribosome.cost_class,
            "genes": total_genes,
            "upstream": config.server.upstream,
            "upstream_reachable": upstream_reachable,
            "hardware": hardware_block,
            "calibration": calibration_block,
            "checks": {
                "genome_ready": genome_ready,
                "upstream_ready": upstream_reachable,
            },
        }

    @app.get("/replicas")
    async def replicas_endpoint():
        if helix._replication_mgr is None:
            return {"enabled": False, "replicas": []}
        return {"enabled": True, **helix._replication_mgr.status()}

    @app.post("/replicas/sync")
    async def replicas_sync_endpoint():
        if helix._replication_mgr is None:
            return {"synced": 0, "error": "replication not configured"}
        synced = helix._replication_mgr.sync_now()
        return {"synced": synced}

    # ---- Admin: knowledge store management ----

    @app.post("/admin/refresh")
    async def admin_refresh():
        """Reopen knowledge store connection to see external changes."""
        helix.genome.refresh()
        helix.genome._invalidate_dense_matrix(force=True)
        new_count = helix.genome.stats()["total_genes"]
        return {"refreshed": True, "genes": new_count}

    @app.post("/admin/vacuum")
    async def admin_vacuum():
        """Reclaim free pages from the knowledge store database."""
        try:
            result = helix.genome.vacuum()
            return {"ok": True, **result}
        except Exception as exc:
            log.warning("VACUUM failed: %s", exc, exc_info=True)
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=500,
            )

    @app.post("/admin/kv-backfill")
    async def admin_kv_backfill():
        """Run CPU regex KV extraction on documents missing key_values."""
        import re as _re
        cur = helix.genome.conn.cursor()
        rows = cur.execute(
            "SELECT gene_id, content FROM genes "
            "WHERE key_values IS NULL OR key_values = '[]' OR key_values = 'null'"
        ).fetchall()
        if not rows:
            return {"backfilled": 0, "total": helix.genome.stats()["total_genes"]}

        patterns = [
            _re.compile(r'^\s*([A-Za-z_]\w*)\s*=\s*["\']([^"\'\n]{1,100})["\']', _re.MULTILINE),
            _re.compile(r'^\s*([A-Za-z_]\w*)\s*=\s*(\d+(?:\.\d+)?)\s*$', _re.MULTILINE),
            _re.compile(r'"([a-z_]\w*)":\s*["\']?([^,}"\'\n]{1,80})["\']?'),
            _re.compile(r'(?:\*\*|[-*])\s*([A-Za-z ]{2,30})(?:\*\*)?:\s*(.{1,80})'),
        ]
        updated = 0
        for row in rows:
            content = row["content"][:3000]
            kvs = set()
            for pat in patterns:
                for match in pat.finditer(content):
                    g = match.groups()
                    if len(g) == 2 and g[0] and g[1]:
                        kvs.add(f"{g[0].strip()[:40]}={g[1].strip()[:80]}")
            cur.execute(
                "UPDATE genes SET key_values = ? WHERE gene_id = ?",
                (json_dumps(sorted(kvs)[:15]), row["gene_id"]),
            )
            updated += 1
        helix.genome.conn.commit()
        return {"backfilled": updated, "total": helix.genome.stats()["total_genes"]}

    @app.post("/admin/compact")
    async def admin_compact(dry_run: bool = False, density_threshold: float = 0.3, access_threshold: int = 5):
        """Run compaction sweep."""
        result = helix.genome.compact_genome(
            density_threshold=density_threshold,
            access_threshold=access_threshold,
            dry_run=dry_run,
        )
        return result

    @app.post("/admin/checkpoint")
    async def admin_checkpoint(mode: str = "PASSIVE"):
        """Force a WAL checkpoint."""
        helix.genome.checkpoint(mode)
        return {"checkpointed": True, "mode": mode}

    @app.post("/admin/ribosome/pause")
    async def admin_ribosome_pause():
        """Disable the compressor's LLM calls without unloading or restarting."""
        backend = helix.ribosome.backend
        backend_id = id(backend)
        if backend_id in _paused_ribosomes:
            return {
                "paused": True,
                "already": True,
                "model": getattr(backend, "model", "unknown"),
            }

        _paused_ribosomes[backend_id] = backend.complete

        def _raise_paused(*args, **kwargs):
            raise RuntimeError(
                "Ribosome paused by /admin/ribosome/pause -- "
                "learn() fallback path engaged"
            )

        backend.complete = _raise_paused
        log.info(
            "Ribosome backend paused (model=%s). LLM calls will raise.",
            getattr(backend, "model", "unknown"),
        )
        return {
            "paused": True,
            "model": getattr(backend, "model", "unknown"),
            "hint": (
                "LLM calls will raise. learn() builds minimal genes from "
                "raw exchange. Resume with POST /admin/ribosome/resume."
            ),
        }

    @app.post("/admin/ribosome/resume")
    async def admin_ribosome_resume():
        """Restore the compressor backend after /admin/ribosome/pause."""
        backend = helix.ribosome.backend
        backend_id = id(backend)
        if backend_id not in _paused_ribosomes:
            return {"resumed": False, "reason": "not paused"}

        backend.complete = _paused_ribosomes.pop(backend_id)
        log.info(
            "Ribosome backend resumed (model=%s)",
            getattr(backend, "model", "unknown"),
        )
        return {
            "resumed": True,
            "model": getattr(backend, "model", "unknown"),
        }

    @app.get("/admin/ribosome/status")
    async def admin_ribosome_status():
        """Check whether the compressor is currently paused."""
        backend = helix.ribosome.backend
        return {
            "paused": id(backend) in _paused_ribosomes,
            "model": getattr(backend, "model", "unknown"),
            "backend_type": type(backend).__name__,
        }

    @app.post("/admin/shutdown")
    async def admin_shutdown(request: Request):
        """Graceful shutdown."""
        import os as _os
        import signal as _signal

        try:
            data = await request.json()
        except Exception:
            data = {}
        actor = data.get("actor") or "unknown"
        reason = data.get("reason") or "manual shutdown"

        try:
            bridge.write_signal("server_state", {
                "state": "stopped",
                "actor": actor,
                "reason": reason,
                "pid": _os.getpid(),
                "expected_downtime_s": 0,
                "phase": "shutting_down",
            })
        except Exception:
            log.warning("Shutdown: failed to stamp signal", exc_info=True)

        log.info("Shutdown requested by %s: %s", actor, reason)

        try:
            _os.kill(_os.getpid(), _signal.SIGINT)
        except Exception:
            log.warning("SIGINT on self failed", exc_info=True)

        return {
            "shutting_down": True,
            "actor": actor,
            "reason": reason,
            "hint": "Poll GET /stats -- connection refused means shutdown complete.",
        }

    @app.post("/admin/announce_restart")
    async def admin_announce_restart(request: Request):
        """Announce an intentional server restart to other sessions."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"error": "Invalid JSON body"},
                status_code=400,
            )

        reason = body.get("reason")
        actor = body.get("actor")
        if not reason or not actor:
            return JSONResponse(
                {"error": "Both 'reason' and 'actor' are required"},
                status_code=400,
            )

        expected_downtime_s = int(body.get("expected_downtime_s", 30))

        try:
            import os as _os
            bridge.announce_restart(
                reason=reason,
                actor=actor,
                expected_downtime_s=expected_downtime_s,
                pid=_os.getpid(),
            )
        except Exception as exc:
            log.warning("announce_restart failed: %s", exc, exc_info=True)
            return JSONResponse(
                {"error": f"Announce failed: {exc}"},
                status_code=500,
            )

        log.info(
            "Restart announced by %s: %s (expected_downtime=%ds)",
            actor, reason, expected_downtime_s,
        )
        return {
            "announced": True,
            "actor": actor,
            "reason": reason,
            "expected_downtime_s": expected_downtime_s,
            "hint": "Sleep ~750ms before killing the server to let observers see the signal.",
        }

    @app.get("/admin/components")
    async def admin_components():
        """Return the list of active subsystems with running/idle status."""
        import time as _time
        idle_threshold_s = 60.0
        age = _time.time() - getattr(helix, "_last_activity_ts", 0.0)
        active_status = "running" if age < idle_threshold_s else "idle"
        ribosome_paused = id(helix.ribosome.backend) in _paused_ribosomes
        ribosome_disabled = getattr(helix.ribosome.backend, "is_disabled_backend", False)

        components = []

        if not ribosome_paused and not ribosome_disabled:
            ribosome_backend = "unknown"
            if hasattr(helix.ribosome, "backend") and hasattr(helix.ribosome.backend, "model"):
                ribosome_backend = helix.ribosome.backend.model
            components.append({
                "name": "ribosome",
                "kind": "decoder",
                "status": active_status,
                "backend": ribosome_backend,
            })

        if getattr(helix, "_sema_codec", None) is not None:
            components.append({
                "name": "sema",
                "kind": "encoder",
                "status": active_status,
            })

        if getattr(helix, "_cpu_tagger", None) is not None:
            components.append({
                "name": "cpu_tagger",
                "kind": "encoder",
                "status": active_status,
            })

        if getattr(helix.genome, "_splade_enabled", False):
            components.append({
                "name": "splade",
                "kind": "encoder",
                "status": active_status,
            })

        if getattr(helix.genome, "_entity_graph_enabled", False):
            components.append({
                "name": "entity_graph",
                "kind": "encoder",
                "status": active_status,
            })

        try:
            from ..encoding.headroom_bridge import is_headroom_available
            if is_headroom_available():
                components.append({
                    "name": "headroom",
                    "kind": "decoder",
                    "status": active_status,
                })
        except Exception:
            pass

        return {
            "components": components,
            "count": len(components),
            "last_activity_s_ago": round(age, 1),
            "idle_threshold_s": idle_threshold_s,
        }

    @app.post("/admin/sema/rebuild")
    async def admin_sema_rebuild():
        """Force-rebuild the SEMA vector cache."""
        helix.genome.invalidate_sema_cache()
        helix.genome._build_sema_cache()
        cache = helix.genome._sema_cache
        return {
            "rebuilt": True,
            "vectors": len(cache["gene_ids"]) if cache else 0,
            "memory_kb": (cache["matrix"].nbytes // 1024) if cache else 0,
        }

    @app.post("/admin/reload")
    async def admin_reload():
        """Hot-reload server runtime state without killing the process."""
        changes = {}

        # 1. Reload config from helix.toml
        try:
            from ..config import load_config
            new_config = load_config()
            old_budget = helix.config.budget.max_genes_per_turn
            new_budget = new_config.budget.max_genes_per_turn
            helix.config = new_config
            if old_budget != new_budget:
                changes["max_genes_per_turn"] = {"old": old_budget, "new": new_budget}
            else:
                changes["config"] = "reloaded (no visible changes)"
        except Exception as exc:
            changes["config_error"] = str(exc)[:200]

        # 2. Refresh knowledge store snapshot
        try:
            helix.genome.refresh()
            total = helix.genome.stats().get("total_genes", 0)
            changes["genome_genes"] = total
        except Exception as exc:
            changes["genome_error"] = str(exc)[:200]

        # 3. Rebuild SEMA vector cache
        try:
            helix.genome.invalidate_sema_cache()
            helix.genome._build_sema_cache()
            cache = helix.genome._sema_cache
            if cache:
                changes["sema_vectors"] = len(cache["gene_ids"])
        except Exception as exc:
            changes["sema_error"] = str(exc)[:200]

        # 4. Clear last_query_scores
        helix.genome.last_query_scores = {}

        log.info("Admin reload complete: %s", changes)
        return {"reloaded": True, "changes": changes}

    # ---- Admin: hot-swap knowledge store .db file ----

    @app.post("/admin/swap-db")
    async def admin_swap_db(request: Request):
        """Hot-swap the knowledge store .db file without restarting.

        Body: { "path": "genomes/bench/oauth/small.db", "read_only": false }
        """
        import os as _os
        import time as _time

        data = await request.json()
        path = data.get("path", "")
        read_only = bool(data.get("read_only", False))

        if not path or not _os.path.exists(path):
            return JSONResponse(
                {"error": f"path not found: {path}"}, status_code=400,
            )

        t0 = _time.time()
        old_path = str(helix.genome.path)

        try:
            from ..sharding import open_read_source

            new_store = open_read_source(
                genome_path=path,
                synonym_map=config.synonym_map,
                sema_codec=getattr(helix, "_sema_codec", None),
                splade_enabled=config.ingestion.splade_enabled,
                entity_graph=config.ingestion.entity_graph,
                sr_enabled=config.retrieval.sr_enabled,
                sr_gamma=config.retrieval.sr_gamma,
                sr_k_steps=config.retrieval.sr_k_steps,
                sr_weight=config.retrieval.sr_weight,
                sr_cap=config.retrieval.sr_cap,
                seeded_edges_enabled=config.retrieval.seeded_edges_enabled,
                # Tier-0 fix (2026-05-18): forward the dense / ANN / fusion
                # retrieval knobs. Without these a hot-swapped fixture reverts
                # to the KnowledgeStore defaults (dense_embedding_enabled=False)
                # and dense recall silently goes dark — the boot path
                # (context_manager.open_read_source) passes them, but this
                # swap path had drifted out of sync.
                dense_embedding_enabled=config.retrieval.dense_embedding_enabled,
                dense_embedding_dim=config.retrieval.dense_embedding_dim,
                ann_similarity_threshold=config.retrieval.ann_similarity_threshold,
                ann_threshold_min_genes=config.retrieval.ann_threshold_min_genes,
                ann_threshold_max_genes=config.retrieval.ann_threshold_max_genes,
                ann_threshold_mode=config.retrieval.ann_threshold_mode,
                ann_threshold_sigma_multiplier=config.retrieval.ann_threshold_sigma_multiplier,
                dense_pool_size=config.retrieval.dense_pool_size,
                fusion_mode=config.retrieval.fusion_mode,
                rrf_k=config.retrieval.rrf_k,
                dense_weight=config.retrieval.dense_weight,
                dense_additive_weight=config.retrieval.dense_additive_weight,
                dense_additive_min_cosine=config.retrieval.dense_additive_min_cosine,
            )
            new_store.read_only = read_only

            # Rebuild caches on the new store
            new_store.invalidate_sema_cache()
            new_store._build_sema_cache()

            # Atomic swap
            old_store = helix.genome
            helix.genome = new_store
            helix.genome.last_query_scores = {}

            # Tier-0 fix (2026-05-17): repoint the session Registry at the
            # new store. The Registry captures a genome reference at app
            # construction (app.py: Registry(helix.genome)) and uses
            # genome.conn directly for every read/write — including the
            # background sweep task. Without this repoint, after a swap the
            # Registry still holds the OLD store, which old_store.close()
            # below then closes; the next _background_registry_sweep tick
            # raises "sqlite3.ProgrammingError: Cannot operate on a closed
            # database". Must run BEFORE old_store.close() so a sweep that
            # fires mid-swap never observes a closed connection.
            try:
                registry.genome = new_store
            except Exception:
                log.warning("swap-db: failed to repoint registry genome", exc_info=True)

            # Tier-0 follow-up #4 (2026-05-17): repoint the VaultManager at
            # the new store too. Like the Registry above, VaultManager
            # captures helix.genome at construction (app.py:
            # VaultManager(genome=helix.genome)); its pruner thread runs
            # refresh_stale_view(genome=self.genome) on a timer, so without
            # this repoint a post-swap prune cycle would hit the closed old
            # store — the same closed-database failure. Vault is opt-in, so
            # this only bites when vault.enabled=true. Runs BEFORE close().
            try:
                request.app.state.vault.genome = new_store
            except Exception:
                log.warning("swap-db: failed to repoint vault genome", exc_info=True)

            # Close old store (best-effort)
            try:
                old_store.close()
            except Exception:
                log.warning("swap-db: failed to close old store", exc_info=True)

            elapsed_ms = round((_time.time() - t0) * 1000, 1)
            genes = new_store.stats().get("total_genes", 0)

            log.info(
                "swap-db: %s -> %s (%d genes, read_only=%s, %.1fms)",
                old_path, path, genes, read_only, elapsed_ms,
            )
            return {
                "swapped": True,
                "old_path": old_path,
                "new_path": str(path),
                "read_only": read_only,
                "genes": genes,
                "elapsed_ms": elapsed_ms,
            }
        except Exception as exc:
            log.warning("swap-db failed: %s", exc, exc_info=True)
            return JSONResponse(
                {"error": f"swap failed: {exc}"}, status_code=500,
            )

    # ---- Bridge: shared memory between AI assistants ----

    @app.get("/bridge/status")
    async def bridge_status():
        def _collect_status() -> Dict[str, object]:
            signals = bridge.list_signals()
            inbox_count = (
                len(list(bridge.inbox.iterdir())) if bridge.inbox.exists() else 0
            )
            return {
                "shared_dir": str(bridge.shared_dir),
                "inbox_pending": inbox_count,
                "signals": signals,
            }

        return await asyncio.to_thread(_collect_status)

    @app.post("/bridge/collect")
    async def bridge_collect():
        """Collect inbox files and ingest into knowledge store."""
        items = await asyncio.to_thread(bridge.collect_inbox)
        gene_ids: list = []
        for item in items:
            try:
                ids = await helix.ingest_async(
                    item["content"],
                    content_type="text",
                    metadata={"path": f"__bridge_{item['source']}__"},
                )
                gene_ids.extend(ids)
            except Exception:
                log.warning("Bridge ingest failed for %s", item["path"], exc_info=True)

        # Update shared context
        try:
            stats_snapshot = helix.stats()
            await asyncio.to_thread(bridge.update_shared_context, stats_snapshot)
        except Exception:
            log.warning("Bridge shared-context update failed", exc_info=True)
        return {"collected": len(items), "genes_created": len(gene_ids)}

    @app.post("/bridge/signal")
    async def bridge_signal(request: Request):
        body = await request.json()
        name = body.get("name", "unnamed")
        data = body.get("data", {})
        bridge.write_signal(name, data)
        return {"ok": True, "signal": name}
