"""HTTP server package -- split from monolithic server.py.

The package preserves full backward compatibility: ``from cymatix_context.server
import create_app`` (and every other previously importable name) still works
because Python resolves ``cymatix_context.server`` to this ``__init__.py`` now
that ``server`` is a package rather than a single-file module.
"""

from .app import create_app, main
from .helpers import (
    _local_timezone,
    _local_attribution_defaults,
    _normalize_identity_token,
    _resolve_caller_agent,
    _compute_know_or_miss_block,
    _compute_plr_confidence,
    _merge_tier_contributions,
    _probe_upstream,
    _background_checkpoint,
    _background_wal_gauge,
    _background_registry_sweep,
    _munge_messages,
    _stream_and_tee,
    _forward_and_replicate,
    _forward_raw,
    _register_vault_routes,
    _paused_ribosomes,
    _TraceBody,
    _agent_allowlist,
    _KNOWN_AGENTS_DEFAULT,
    _CHECKPOINT_INTERVAL,
    _REGISTRY_SWEEP_INTERVAL,
    _WAL_GAUGE_INTERVAL,
)

__all__ = [
    "create_app",
    "main",
    "_munge_messages",
    "_resolve_caller_agent",
    "_paused_ribosomes",
]
