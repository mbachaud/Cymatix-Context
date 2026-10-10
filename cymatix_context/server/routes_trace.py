"""Packet-trace read endpoints (issue #493, Phase 1).

``GET /trace/recent`` and ``GET /trace/{packet_id}``. Both answer from the
local JSONL written by ``telemetry/trace.py``; with tracing disabled they
return an empty list / 404. Both carry the ``[server] admin_token`` guard
(inert while the token is empty): ``full``-level records hold raw query
and chunk text.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .helpers import build_admin_auth


class ActionBody(BaseModel):
    session_id: Optional[str] = None
    tool_name: str
    args: Any = None
    packet_id: Optional[str] = None


def setup_trace_routes(app: FastAPI, config, **_kw) -> None:
    _admin_auth = [Depends(build_admin_auth(config))]

    def _tracer():
        t = getattr(app.state, "packet_tracer", None)
        return t if t is not None and t.enabled else None

    @app.post("/trace/action", dependencies=_admin_auth)
    async def trace_action(body: ActionBody, request: Request):
        tracer = _tracer()
        if tracer is None:
            return JSONResponse({"error": "tracing disabled"}, status_code=404)
        from ..telemetry.trace import parse_traceparent
        rec = tracer.emit_action(
            body.session_id, body.tool_name, body.args, packet_id=body.packet_id,
            traceparent=parse_traceparent(request.headers.get("traceparent")),
        )
        if rec is None:
            return JSONResponse({"error": "action not recorded"}, status_code=500)
        return {"packet_id": rec["packet_id"], "orphan": rec["orphan"]}

    # Registered before the {packet_id} route so "recent" is not captured by it.
    @app.get("/trace/recent", dependencies=_admin_auth)
    async def trace_recent(session_id: Optional[str] = None, limit: int = 20,
                           event: Optional[str] = None):
        tracer = _tracer()
        if tracer is None:
            return []
        return tracer.recent(session_id=session_id, limit=limit, event=event)

    @app.get("/trace/{packet_id}", dependencies=_admin_auth)
    async def trace_get(packet_id: str):
        tracer = _tracer()
        rec = tracer.get(packet_id) if tracer is not None else None
        if rec is None:
            return JSONResponse({"error": "trace not found"}, status_code=404)
        return rec
