"""Plugin discovery and hook isolation.

``discover_plugins`` never raises: every failure is captured on the returned
``LoadedPlugin`` so one broken add-on cannot affect core or other plugins.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Literal, Optional, TypeVar

import cymatix_context
from cymatix_context.config import PluginsConfig

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "cymatix.plugins"

T = TypeVar("T")


@dataclass
class LoadedPlugin:
    name: str
    version: str
    source: Literal["entry_point", "directory"]
    signed: bool
    status: Literal["loaded", "disabled", "skipped"]
    reason: Optional[str]
    obj: Optional[Any]  # CymatixPlugin when status == "loaded"


def _entry_points() -> List[Any]:
    from importlib.metadata import entry_points

    return list(entry_points(group=ENTRY_POINT_GROUP))


def _load_entry_point(ep: Any, cfg: PluginsConfig) -> LoadedPlugin:
    name = getattr(ep, "name", "?")

    def make(status, reason, version="?", obj=None):
        return LoadedPlugin(name, version, "entry_point", False, status, reason, obj)

    if name in cfg.disabled:
        return make("skipped", "disabled in [plugins]")
    try:
        obj = ep.load()()
    except Exception as exc:  # noqa: BLE001 - isolation boundary: any plugin failure
        log.warning("plugin %s failed to load: %r", name, exc)
        return make("disabled", repr(exc))
    reported = str(getattr(obj, "name", name))
    if reported != name:
        log.warning(
            "plugin entry point %r reports name %r; using the entry-point name", name, reported
        )
    version = str(getattr(obj, "version", "?"))
    spec = getattr(obj, "requires_core", None)
    try:
        from packaging.specifiers import SpecifierSet

        ok = SpecifierSet(str(spec)).contains(cymatix_context.__version__, prereleases=True)
    except Exception as exc:  # noqa: BLE001 - malformed/missing specifier
        return make("skipped", f"invalid requires_core {spec!r}: {exc!r}", version)
    if not ok:
        return make(
            "skipped",
            f"requires_core {spec} does not match core {cymatix_context.__version__}",
            version,
        )
    return make("loaded", None, version, obj)


def discover_plugins(
    cfg: PluginsConfig,
    *,
    entry_points: Optional[Iterable[Any]] = None,
    plugin_dir: Optional[Path] = None,
    frozen: Optional[bool] = None,
) -> List[LoadedPlugin]:
    """Discover plugins from installed entry points.

    ``plugin_dir`` and ``frozen`` are the seam for the plugin-directory source
    (not implemented yet); they are accepted so callers need not change.
    """
    if not cfg.enabled:
        return []
    eps = _entry_points() if entry_points is None else list(entry_points)
    found = [_load_entry_point(ep, cfg) for ep in eps]
    # Directory source: appended here once implemented.
    return found


def call_hook(lp: LoadedPlugin, hook: str, *args: Any, default: T) -> T:
    """Call ``lp.obj.<hook>(*args)``; return ``default`` if absent, not loaded,
    or the hook raises (the plugin is then disabled for this process)."""
    if lp.status != "loaded" or lp.obj is None:
        return default
    fn = getattr(lp.obj, hook, None)
    if not callable(fn):
        return default
    try:
        return fn(*args)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - isolation boundary
        log.warning("plugin %s hook %s failed", lp.name, hook, exc_info=True)
        lp.status = "disabled"
        lp.reason = repr(exc)
        return default


_CACHE: Optional[List[LoadedPlugin]] = None


def get_plugins() -> List[LoadedPlugin]:
    """Process-wide cached discovery using the loaded config."""
    global _CACHE
    if _CACHE is None:
        from cymatix_context.config import load_config

        _CACHE = discover_plugins(load_config().plugins)
    return _CACHE


def reset_plugins_cache() -> None:
    global _CACHE
    _CACHE = None
