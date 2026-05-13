"""Session registry and HITL routes: /sessions/*, /hitl/*, /context/expand,
/session/{session_id}/manifest, /sessions/{handle}/recent.

Extracted from the monolithic server.py -- NO logic changes.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ..identity.registry import DEFAULT_HEARTBEAT_INTERVAL_S, DEFAULT_TTL_S

log = logging.getLogger("helix.server")


def setup_registry_routes(app: FastAPI, helix, config, registry, **_kw) -> None:
    """Register session registry and HITL routes on *app*."""

    @app.post("/sessions/register")
    async def session_register_endpoint(request: Request):
        """Register a participant under a party. Trust-on-first-use for party_id."""
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        party_id = data.get("party_id")
        handle = data.get("handle")
        # Validate identity tokens before touching the registry.
        if party_id is None or handle is None:
            raise HTTPException(
                status_code=400,
                detail="party_id and handle are required",
            )
        if not isinstance(party_id, str) or not isinstance(handle, str):
            raise HTTPException(
                status_code=400,
                detail="party_id and handle must be strings",
            )
        if "\x00" in party_id or "\x00" in handle:
            raise HTTPException(
                status_code=400,
                detail=(
                    "party_id/handle contain NULL bytes (binary payload "
                    "declared as text). Base64-encode binary content "
                    "before POSTing."
                ),
            )
        party_id_stripped = party_id.strip()
        handle_stripped = handle.strip()
        if not party_id_stripped or not handle_stripped:
            raise HTTPException(
                status_code=400,
                detail="party_id and handle must be non-empty",
            )
        if len(party_id_stripped) < 3 or len(handle_stripped) < 3:
            raise HTTPException(
                status_code=400,
                detail="party_id and handle must be at least 3 characters",
            )
        party_id = party_id_stripped
        handle = handle_stripped

        try:
            participant = registry.register_participant(
                party_id=party_id,
                handle=handle,
                workspace=data.get("workspace"),
                pid=data.get("pid"),
                capabilities=data.get("capabilities"),
                metadata=data.get("metadata"),
                display_name=data.get("display_name"),
                agent_kind=data.get("agent_kind"),
                mcp_host=data.get("mcp_host"),
                ide_detected=data.get("ide_detected"),
                ide_detection_via=data.get("ide_detection_via"),
                model_id=data.get("model_id"),
            )
        except Exception as exc:
            log.warning("Session register failed: %s", exc, exc_info=True)
            return JSONResponse(
                {"error": f"Registration failed: {exc}"},
                status_code=500,
            )

        return {
            "participant_id": participant.participant_id,
            "party_id": participant.party_id,
            "registered_at": participant.started_at,
            "heartbeat_interval_s": DEFAULT_HEARTBEAT_INTERVAL_S,
            "ttl_s": DEFAULT_TTL_S,
        }

    @app.post("/sessions/{participant_id}/announce")
    async def session_announce_endpoint(participant_id: str, request: Request):
        """Update model_id and (optionally) ide_detected on a participant."""
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        model_id = data.get("model_id")
        ide_override = data.get("ide_override")
        if model_id is not None and not isinstance(model_id, str):
            raise HTTPException(status_code=400, detail="model_id must be a string")
        if ide_override is not None and not isinstance(ide_override, str):
            raise HTTPException(status_code=400, detail="ide_override must be a string")

        try:
            registry.update_announcement(
                participant_id=participant_id,
                model_id=model_id,
                ide_override=ide_override,
            )
        except Exception as exc:
            log.warning("Announce failed: %s", exc, exc_info=True)
            return JSONResponse(
                {"error": f"Announce failed: {exc}"},
                status_code=500,
            )

        return JSONResponse({"ok": True})

    @app.post("/sessions/{participant_id}/heartbeat")
    async def session_heartbeat_endpoint(
        participant_id: str,
        request: Request,
    ):
        """Refresh last_heartbeat for a participant. Returns 404 if unknown."""
        result = registry.heartbeat(participant_id)
        if result is None:
            return JSONResponse(
                {"error": "Unknown participant_id -- please re-register"},
                status_code=404,
            )
        ttl_remaining_s, status = result

        # Optional presence-document emit.
        presence_gene_id: Optional[str] = None
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict) and body:
            try:
                presence_gene_id = registry.upsert_presence_gene(
                    participant_id,
                    handle=body.get("handle"),
                    party_id=body.get("party_id"),
                    current_focus=body.get("current_focus"),
                    blocked_on=body.get("blocked_on"),
                    in_flight=body.get("in_flight"),
                    last_commit_hash=body.get("last_commit_hash"),
                    extra_notes=body.get("notes"),
                )
            except Exception:
                log.warning(
                    "presence gene upsert failed for %s",
                    participant_id, exc_info=True,
                )

        return {
            "ok": True,
            "ttl_remaining_s": ttl_remaining_s,
            "status": status,
            "presence_gene_id": presence_gene_id,
        }

    @app.get("/sessions")
    async def session_list_endpoint(
        party_id: Optional[str] = None,
        status: str = "active",
        workspace: Optional[str] = None,
    ):
        """List participants. Filters: party_id, status, workspace prefix."""
        try:
            infos = registry.list_participants(
                party_id=party_id,
                status_filter=status,
                workspace_prefix=workspace,
            )
        except Exception as exc:
            log.warning("Session list failed: %s", exc, exc_info=True)
            return JSONResponse(
                {"error": f"List failed: {exc}"},
                status_code=500,
            )
        return {
            "participants": [info.model_dump() for info in infos],
            "count": len(infos),
        }

    @app.get("/sessions/{handle}/recent")
    async def session_recent_endpoint(
        handle: str,
        limit: int = 10,
        party_id: Optional[str] = None,
        since: Optional[float] = None,
    ):
        """Return recent documents authored by a handle, chronologically."""
        try:
            genes = registry.get_recent_by_handle(
                handle=handle,
                limit=limit,
                party_id=party_id,
                since=since,
            )
        except Exception as exc:
            log.warning("Session recent failed: %s", exc, exc_info=True)
            return JSONResponse(
                {"error": f"Recent lookup failed: {exc}"},
                status_code=500,
            )
        return {
            "handle": handle,
            "genes": genes,
            "count": len(genes),
        }

    # -- AI-Consumer Sprint 3: 1-hop neighborhood expand --

    @app.get("/context/expand")
    async def context_expand_endpoint(
        gene_id: str,
        direction: str = "forward",
        k: int = 5,
        session_id: Optional[str] = None,
    ):
        """1-hop expand from `gene_id`. See helix_context/expand.py."""
        try:
            from ..retrieval import expand as _expand
            result = _expand.expand_neighbors(
                helix.genome,
                gene_id=gene_id,
                direction=direction,
                k=max(1, min(100, int(k))),
                session_id=session_id,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            log.warning("context_expand failed: %s", exc, exc_info=True)
            return JSONResponse(
                {"error": f"Expand failed: {exc}"},
                status_code=500,
            )
        return result

    # -- AI-Consumer Sprint 2: session working-set introspection --

    @app.get("/session/{session_id}/manifest")
    async def session_manifest_endpoint(session_id: str, limit: int = 500):
        """List document deliveries recorded for a session."""
        try:
            from ..identity import session_delivery as _sd
            rows = _sd.session_manifest(
                helix.genome.conn,
                session_id=session_id,
                limit=max(1, min(5000, int(limit))),
            )
        except Exception as exc:
            log.warning("session_manifest failed: %s", exc, exc_info=True)
            return JSONResponse(
                {"error": f"Session manifest lookup failed: {exc}"},
                status_code=500,
            )
        return {
            "session_id": session_id,
            "deliveries": rows,
            "count": len(rows),
        }

    # -- HITL event endpoints --

    @app.post("/hitl/emit")
    async def hitl_emit_endpoint(request: Request):
        """Record a HITL pause event."""
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        pause_type = data.get("pause_type")
        if not pause_type:
            return JSONResponse(
                {"error": "pause_type is required"}, status_code=400,
            )

        try:
            event_id = registry.emit_hitl_event(
                participant_id=data.get("participant_id"),
                pause_type=pause_type,
                task_context=data.get("task_context"),
                resolved_without_operator=bool(
                    data.get("resolved_without_operator", False)
                ),
                chat_signals=data.get("chat_signals"),
                genome_snapshot=data.get("genome_snapshot"),
                metadata=data.get("metadata"),
                party_id=data.get("party_id"),
            )
        except Exception as exc:
            log.warning("HITL emit failed: %s", exc, exc_info=True)
            return JSONResponse(
                {"error": f"Emit failed: {exc}"}, status_code=500,
            )

        if event_id is None:
            return JSONResponse(
                {
                    "error": (
                        "Event not written -- unknown participant_id and no "
                        "party_id provided, or participant_id not registered."
                    )
                },
                status_code=400,
            )
        return {"event_id": event_id, "ok": True}

    @app.get("/hitl/recent")
    async def hitl_recent_endpoint(
        party_id: Optional[str] = None,
        participant_id: Optional[str] = None,
        pause_type: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
        limit: int = 50,
    ):
        """List recent HITL events, newest first."""
        safe_limit = max(1, min(int(limit), 500))
        try:
            events = registry.get_hitl_events(
                party_id=party_id,
                participant_id=participant_id,
                pause_type=pause_type,
                since=since,
                until=until,
                limit=safe_limit,
            )
        except Exception as exc:
            log.warning("HITL recent failed: %s", exc, exc_info=True)
            return JSONResponse(
                {"error": f"Recent lookup failed: {exc}"}, status_code=500,
            )
        return {"events": events, "count": len(events)}
