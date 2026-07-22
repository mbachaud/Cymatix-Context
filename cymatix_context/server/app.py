"""Helix Context Server -- The cell membrane.

A FastAPI HTTP sidecar that acts as an OpenAI-compatible proxy.
Clients point their model endpoint at this server instead of Ollama directly.
Context compression happens transparently in the proxy layer.

Endpoints:
    POST /v1/chat/completions  -- proxy (primary integration)
    POST /ingest               -- manual content ingestion
    POST /context              -- Continue HTTP context provider format
    GET  /stats                -- knowledge store and compression metrics
    GET  /health               -- compressor model and document count

App factory lives here; route modules are in sibling files under
``cymatix_context/server/``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI

from ..config import HelixConfig, load_config
from ..context_manager import HelixContextManager
from ..identity.registry import Registry
from ..vault import VaultManager

from .helpers import (
    _background_checkpoint,
    _background_registry_sweep,
    _background_wal_gauge,
    _register_vault_routes,
)
from .routes_context import setup_context_routes
from .routes_ingest import setup_ingest_routes
from .routes_admin import setup_admin_routes
from .routes_registry import setup_registry_routes

log = logging.getLogger("helix.server")


def create_app(config: Optional[HelixConfig] = None) -> FastAPI:
    """Factory -- creates the FastAPI app with a HelixContextManager."""
    from ..bridge import AgentBridge

    if config is None:
        config = load_config()

    # ---- Hardware init MUST happen BEFORE any backend constructs. ----
    from ..hardware import init_from_config
    init_from_config(
        config_device=config.hardware.device,
        batch_size_overrides=config.hardware.batch_sizes,
    )

    # ---- Cost-visibility startup warning (W2-B) ----
    _cost_class = config.ribosome.cost_class
    if _cost_class == "api+paid":
        log.warning(
            "RIBOSOME PAID-API ACTIVE: backend=%s model=%s. Every "
            "ingest/replicate/rerank call hits a metered API. Flip "
            "[ribosome] enabled=false (or switch to backend=deberta) "
            "for non-metered operation.",
            config.ribosome.effective_backend,
            config.ribosome.active_model,
        )
    elif _cost_class == "disabled":
        log.info(
            "Ribosome disabled: configured enabled=%s backend=%s",
            config.ribosome.enabled,
            config.ribosome.backend,
        )
    else:
        log.info(
            "Ribosome cost_class=%s backend=%s model=%s",
            _cost_class, config.ribosome.effective_backend, config.ribosome.active_model,
        )

    helix = HelixContextManager(config)

    # W2-B: emit the compressor info-metric for dashboard visibility.
    try:
        from ..telemetry import ribosome_info_gauge
        ribosome_info_gauge().set(
            1,
            attributes={
                "backend": config.ribosome.effective_backend,
                "model": config.ribosome.active_model,
                "cost_class": _cost_class,
            },
        )
    except Exception:  # pragma: no cover - telemetry must not break startup
        pass

    # Bridge instantiated up here so the lifespan closure can capture it.
    bridge = AgentBridge()

    # Session registry -- presence + attribution.
    registry = Registry(helix.genome)

    # Vault manager -- operator-facing markdown export.
    vault = VaultManager(config=config, genome=helix.genome)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """
        Startup: stamp server_state=running so observer sessions know
            a restart completed (or this is the first launch).
        Shutdown: WAL checkpoint + stamp server_state=stopped as a
            fallback for clean shutdowns (Ctrl+C, OS shutdown).
            Does NOT run under kill -9 -- agents should call
            bridge.announce_restart BEFORE killing the process.
        """
        # Stamp "running" so observer sessions know a restart completed.
        try:
            bridge.write_signal("server_state", {
                "state": "running",
                "actor": "lifespan",
                "reason": None,
                "pid": os.getpid(),
                "expected_downtime_s": 0,
                "phase": "up",
            })
            log.info("Startup: server_state=running stamped (pid=%d)", os.getpid())
        except Exception:
            log.warning("Startup: failed to stamp server_state signal", exc_info=True)

        task = asyncio.create_task(_background_checkpoint(helix))
        sweep_task = asyncio.create_task(_background_registry_sweep(registry))
        wal_gauge_task = asyncio.create_task(_background_wal_gauge(helix))

        # Vault export -- opt-in, off if config.vault.enabled=false.
        try:
            vault.start()
        except Exception:
            log.warning("vault.start failed; continuing without vault", exc_info=True)

        yield
        task.cancel()
        sweep_task.cancel()
        wal_gauge_task.cancel()
        for _t in (task, sweep_task, wal_gauge_task):
            try:
                await _t
            except asyncio.CancelledError:
                pass
        helix.genome.checkpoint("TRUNCATE")

        try:
            vault.stop()
        except Exception:
            log.warning("vault.stop failed", exc_info=True)

        # Flush token counter so lifetime totals persist across restart.
        try:
            helix.token_counter.flush()
        except Exception:
            log.warning("Token counter flush failed during shutdown", exc_info=True)

        # Belt-and-suspenders: stamp "stopped" on clean shutdown.
        try:
            bridge.write_signal("server_state", {
                "state": "stopped",
                "actor": "lifespan",
                "reason": "clean shutdown",
                "pid": os.getpid(),
                "expected_downtime_s": 0,
                "phase": "shutting_down",
            })
        except Exception:
            log.warning("Shutdown: failed to stamp server_state signal", exc_info=True)

        log.info("Shutdown: final WAL checkpoint completed")

    from .. import __version__ as _pkg_version
    app = FastAPI(title="Cymatix Context Proxy", version=_pkg_version, lifespan=lifespan)
    app.state.helix = helix  # Expose for testing
    app.state.bridge = bridge  # Expose for testing
    app.state.registry = registry  # Expose for testing
    app.state.vault = vault

    # OpenTelemetry init (off unless HELIX_OTEL_ENABLED=1 or [telemetry]
    # enabled=true; env wins over toml — see otel.resolve_telemetry_settings).
    try:
        from ..telemetry import setup_telemetry
        setup_telemetry(app, service_name="cymatix-context", config=config.telemetry)
    except Exception:
        log.debug("OTel setup failed", exc_info=True)

    # ---- Register all route modules ----
    setup_context_routes(app, helix=helix, config=config, registry=registry)
    setup_ingest_routes(app, helix=helix, config=config, registry=registry)
    setup_registry_routes(app, helix=helix, config=config, registry=registry)
    setup_admin_routes(app, helix=helix, config=config, registry=registry, bridge=bridge)

    # Register vault endpoints (export, status, trace, pin/unpin).
    _register_vault_routes(app)

    return app


# -- Entry point -------------------------------------------------------

def main():
    config = load_config()
    app = create_app(config)
    log.info("Helix Context Proxy starting on %s:%d", config.server.host, config.server.port)
    log.info("Upstream: %s", config.server.upstream)
    uvicorn.run(app, host=config.server.host, port=config.server.port)


# Module-level app object is intentionally NOT created here.
# Importing server.py must not open a database connection -- doing so breaks
# pytest collection in any environment where the knowledge store path doesn't exist
# (e.g. git worktrees, fresh clones, CI without a test knowledge store).
#
# For uvicorn, use the dedicated entry point instead:
#   uvicorn cymatix_context._asgi:app
# See cymatix_context/_asgi.py.
