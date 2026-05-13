"""Ingest and consolidation routes: /ingest, /consolidate.

Extracted from the monolithic server.py -- NO logic changes.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .helpers import (
    _local_attribution_defaults,
    _local_timezone,
    _normalize_identity_token,
)

log = logging.getLogger("helix.server")


def setup_ingest_routes(app: FastAPI, helix, config, registry, **_kw) -> None:
    """Register ingest routes on *app*."""

    @app.post("/ingest")
    async def ingest_endpoint(request: Request):
        import time as _time
        helix._last_activity_ts = _time.time()

        data = await request.json()
        content = data.get("content", "")
        content_type = data.get("content_type", "text")
        metadata = data.get("metadata")
        participant_id = data.get("participant_id")
        party_id = data.get("party_id")
        org_id = data.get("org_id")
        agent_id = data.get("agent_id")
        participant_handle = _normalize_identity_token(data.get("participant_handle"))
        agent_handle_override = _normalize_identity_token(data.get("agent_handle"))
        agent_kind = data.get("agent_kind")  # e.g. "claude-code", "gemini"
        local_federation = data.get("local_federation", True)
        authored_tz = data.get("authored_tz") or _local_timezone()

        # Validate content BEFORE federation writes.
        if not content or not content.strip():
            return JSONResponse(
                {"error": "No content provided"},
                status_code=400,
            )

        # Reject binary content declared as text.
        if "\x00" in content:
            return JSONResponse(
                {
                    "error": (
                        "content contains NULL bytes (binary payload declared as "
                        "text). Base64-encode binary content before POSTing."
                    ),
                },
                status_code=400,
            )

        if local_federation and not participant_id:
            user_handle, default_device, default_org, agent_handle = (
                _local_attribution_defaults()
            )
            effective_user = participant_handle or user_handle
            effective_party = _normalize_identity_token(party_id) or default_device
            effective_org = _normalize_identity_token(org_id) or default_org
            effective_agent = agent_handle_override or agent_handle
            try:
                # 4-layer find-or-create chain.
                if effective_org:
                    org_id = registry.local_org(effective_org)
                if effective_user and effective_party:
                    participant_id = registry.local_participant(
                        handle=effective_user,
                        party_id=effective_party,
                        org_id=org_id,
                        timezone=authored_tz,
                    )
                    if not party_id:
                        party_id = effective_party
                # Agent layer is optional
                if effective_agent and participant_id and not agent_id:
                    agent_id = registry.local_agent(
                        handle=effective_agent,
                        participant_id=participant_id,
                        kind=agent_kind,
                    )
            except Exception:
                log.warning(
                    "OS-level federation failed (user=%s device=%s org=%s agent=%s)",
                    effective_user, effective_party, effective_org, effective_agent,
                    exc_info=True,
                )

        try:
            gene_ids = await helix.ingest_async(content, content_type, metadata)
        except Exception as exc:
            log.warning("Ingest failed: %s", exc, exc_info=True)
            return JSONResponse(
                {"error": f"Ingest failed: {exc}", "gene_ids": [], "count": 0},
                status_code=422,
            )

        # Attribution -- additive, never fails the ingest.
        attributed = 0
        if participant_id or party_id:
            for gid in gene_ids:
                try:
                    result = registry.attribute_gene(
                        gene_id=gid,
                        participant_id=participant_id,
                        party_id=party_id,
                        org_id=org_id,
                        agent_id=agent_id,
                        authored_tz=authored_tz,
                    )
                    if result is not None:
                        attributed += 1
                except Exception:
                    log.warning(
                        "Attribution write failed for gene %s",
                        gid, exc_info=True,
                    )

        response = {"gene_ids": gene_ids, "count": len(gene_ids)}
        if participant_id or party_id:
            response["attributed"] = attributed
        return response

    @app.post("/consolidate")
    async def consolidate_endpoint():
        """Trigger session memory consolidation."""
        try:
            gene_ids = await helix.consolidate_session_async()
            return {
                "facts_extracted": len(gene_ids),
                "gene_ids": gene_ids,
            }
        except Exception as exc:
            log.warning("Consolidation endpoint failed: %s", exc, exc_info=True)
            return JSONResponse(
                {"error": f"Consolidation failed: {exc}", "facts_extracted": 0, "gene_ids": []},
                status_code=500,
            )
