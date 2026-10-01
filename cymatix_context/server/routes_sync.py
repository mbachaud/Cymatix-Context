"""Delta-sync routes: GET /sync/status, POST /sync/rescan."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import Depends, FastAPI, Request

from .helpers import build_admin_auth

log = logging.getLogger("cymatix.server")


def setup_sync_routes(app: FastAPI, config, worker: Optional[object]) -> None:
    _admin_auth = [Depends(build_admin_auth(config))]

    @app.get("/sync/status")
    async def sync_status():
        if worker is None:
            return {"enabled": False}
        return await asyncio.to_thread(worker.status)

    @app.post("/sync/rescan", dependencies=_admin_auth)
    async def sync_rescan(request: Request):
        """Run one pass now and return its report. Body (optional):
        ``{"allow_mass_delete": true}`` confirms a bulk delete that the
        mass-delete guard held back."""
        if worker is None:
            return {"enabled": False}
        allow = False
        try:
            body = await request.json()
            allow = bool(isinstance(body, dict) and body.get("allow_mass_delete"))
        except ValueError:
            pass  # no / non-JSON body: plain rescan
        report = await asyncio.to_thread(worker.run_pass, allow)
        return report.to_dict()
