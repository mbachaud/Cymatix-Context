"""Plugin discovery and hook isolation.

``discover_plugins`` never raises: every failure is captured on the returned
``LoadedPlugin`` so one broken add-on cannot affect core or other plugins.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Literal, Optional, Sequence, TypeVar

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
    problem = _requires_core_problem(getattr(obj, "requires_core", None))
    if problem:
        return make("skipped", problem, version)
    return make("loaded", None, version, obj)


def _requires_core_problem(spec: Any) -> Optional[str]:
    """None when ``spec`` admits this core version, else a reason string."""
    try:
        from packaging.specifiers import SpecifierSet

        ok = SpecifierSet(str(spec)).contains(cymatix_context.__version__, prereleases=True)
    except Exception as exc:  # noqa: BLE001 - malformed/missing specifier
        return f"invalid requires_core {spec!r}: {exc!r}"
    if not ok:
        return f"requires_core {spec} does not match core {cymatix_context.__version__}"
    return None


_ENTRY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*):([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)$")
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_CAPABILITIES = frozenset({"cli", "services", "routes", "panels", "menu", "status"})
_ALLOW_UNSIGNED_ENV = "CYMATIX_ALLOW_UNSIGNED_PLUGINS"


def _default_plugin_dir() -> Optional[Path]:
    try:
        import platformdirs
    except ImportError:
        return None
    return Path(platformdirs.user_data_dir("cymatix")) / "plugins"


def _manifest_problem(data: Any) -> Optional[str]:
    """Structural validation of a parsed plugin.json (no I/O, no imports)."""
    if not isinstance(data, dict):
        return "plugin.json is not an object"
    for key in ("name", "version", "requires_core", "entry", "publisher"):
        if not isinstance(data.get(key), str) or not data[key]:
            return f"plugin.json: '{key}' must be a non-empty string"
    if not _ENTRY_RE.match(data["entry"]):
        return f"plugin.json: invalid entry {data['entry']!r} (want 'module:factory')"
    caps = data.get("capabilities")
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        return "plugin.json: 'capabilities' must be a list of strings"
    unknown = sorted(set(caps) - _CAPABILITIES)
    if unknown:
        return f"plugin.json: unknown capabilities {unknown}"
    req = data.get("requires")
    if not isinstance(req, list) or not all(isinstance(r, str) for r in req):
        return "plugin.json: 'requires' must be a list of import names"
    if not isinstance(data.get("files"), dict):
        return "plugin.json: 'files' must be an object"
    return None


def _missing_requirement(requires: List[str]) -> Optional[str]:
    import importlib.util

    for mod in requires:
        if not _MODULE_RE.match(mod):
            return f"invalid requires entry {mod!r}"
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError, AttributeError):
            found = False
        if not found:
            return f"missing required module {mod!r}"
    return None


def _load_directory_plugin(
    pdir: Path,
    cfg: PluginsConfig,
    keys: Sequence[bytes],
    entry_point_names: set,
    allow_unsigned: bool,
) -> LoadedPlugin:
    from cymatix_context.plugins import signing

    ident = pdir.name

    def make(status, reason, version="?", obj=None, signed=False):
        return LoadedPlugin(ident, version, "directory", signed, status, reason, obj)

    # Identity-only checks first: nothing below is read from the plugin yet.
    if ident in cfg.disabled:
        return make("skipped", "disabled in [plugins]")
    if ident in entry_point_names:
        return make("skipped", "duplicate of entry point")
    try:
        if signing._is_link(os.lstat(pdir)):
            return make("skipped", "plugin directory is a symlink")
        manifest_bytes = (pdir / signing.MANIFEST_NAME).read_bytes()
    except OSError as exc:
        return make("skipped", f"cannot read plugin.json: {exc!r}")

    # Verification (or the explicit unsigned opt-in) completes before src/ is
    # touched. The manifest used below is decoded from the exact verified bytes.
    sig_path = pdir / signing.SIGNATURE_NAME
    signed = False
    try:
        sig = sig_path.read_bytes()
    except FileNotFoundError:
        sig = None
    except OSError as exc:
        return make("skipped", f"cannot read plugin.sig: {exc!r}")
    if sig is None:
        if not allow_unsigned:
            return make("skipped", "unsigned")
        try:
            data = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            return make("skipped", f"plugin.json is not valid JSON: {exc}")
    else:
        # A signature that is present must verify; it is never downgraded to
        # "unsigned" because the opt-in env var is set.
        ok, reason, data = signing.verify_manifest_bytes(pdir, manifest_bytes, sig, keys)
        if not ok:
            return make("skipped", reason)
        signed = True

    problem = _manifest_problem(data)
    if problem:
        return make("skipped", problem, signed=signed)
    version = data["version"]
    if data["name"] != ident:
        return make(
            "skipped",
            f"directory name {ident!r} does not match manifest name {data['name']!r}",
            version,
            signed=signed,
        )
    problem = _requires_core_problem(data["requires_core"])
    if problem:
        return make("skipped", problem, version, signed=signed)
    problem = _missing_requirement(data["requires"])
    if problem:
        return make("skipped", problem, version, signed=signed)

    src = str(pdir / signing.SRC_NAME)
    module_name, factory_path = _ENTRY_RE.match(data["entry"]).groups()
    before_modules = set(sys.modules)
    prev_dont_write = sys.dont_write_bytecode
    sys.path.insert(0, src)
    sys.dont_write_bytecode = True  # a later run must not see our own .pyc as unlisted files
    try:
        import importlib

        mod = importlib.import_module(module_name)
        mod_file = getattr(mod, "__file__", None)
        if not mod_file or os.path.commonpath(
            [os.path.realpath(mod_file), os.path.realpath(src)]
        ) != os.path.realpath(src):
            raise ImportError(f"module {module_name!r} did not resolve inside the plugin's src/")
        target: Any = mod
        for attr in factory_path.split("."):
            target = getattr(target, attr)
        obj = target()
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - isolation boundary
        log.warning("plugin %s failed to load: %r", ident, exc)
        _unload(src, before_modules)
        return make("disabled", repr(exc), version, signed=signed)
    finally:
        sys.dont_write_bytecode = prev_dont_write
    # Keep src/ on sys.path only for a plugin that loaded (lazy imports).
    reported = str(getattr(obj, "name", ident))
    if reported != ident:
        log.warning(
            "plugin directory %r reports name %r; using the directory name", ident, reported
        )
    return make("loaded", None, version, obj, signed)


def _unload(src: str, before_modules: set) -> None:
    """Undo a failed plugin load: drop src/ from sys.path and its new modules."""
    try:
        sys.path.remove(src)
    except ValueError:
        pass
    real_src = os.path.realpath(src)
    for name in set(sys.modules) - before_modules:
        f = getattr(sys.modules.get(name), "__file__", None)
        try:
            inside = bool(f) and os.path.commonpath([os.path.realpath(f), real_src]) == real_src
        except ValueError:
            inside = False
        if inside:
            sys.modules.pop(name, None)


def _discover_directory(
    cfg: PluginsConfig,
    plugin_dir: Optional[Path],
    keys: Sequence[bytes],
    entry_point_names: set,
) -> List[LoadedPlugin]:
    root = plugin_dir
    if root is None:
        root = Path(cfg.dir) if cfg.dir else _default_plugin_dir()
    if root is None:
        log.info("platformdirs not installed; skipping plugin directory source")
        return []
    try:
        entries = sorted(p for p in Path(root).iterdir() if p.is_dir())
    except OSError:
        return []  # no plugin directory: normal
    allow_unsigned = os.environ.get(_ALLOW_UNSIGNED_ENV) == "1"
    out: List[LoadedPlugin] = []
    for pdir in entries:
        try:
            out.append(_load_directory_plugin(pdir, cfg, keys, entry_point_names, allow_unsigned))
        except Exception as exc:  # noqa: BLE001 - one bad directory must not break discovery
            log.warning("plugin directory %s could not be processed: %r", pdir.name, exc)
            out.append(LoadedPlugin(pdir.name, "?", "directory", False, "skipped", repr(exc), None))
    return out


def discover_plugins(
    cfg: PluginsConfig,
    *,
    entry_points: Optional[Iterable[Any]] = None,
    plugin_dir: Optional[Path] = None,
    frozen: Optional[bool] = None,
    trusted_keys: Optional[Sequence[bytes]] = None,
) -> List[LoadedPlugin]:
    """Discover plugins from installed entry points and the plugin directory.

    Directory plugins are signature-gated in every build (``frozen`` included)
    unless ``CYMATIX_ALLOW_UNSIGNED_PLUGINS=1`` is set. ``frozen`` is accepted
    for callers that pass it; the rule does not differ by build.
    """
    if not cfg.enabled:
        return []
    eps = _entry_points() if entry_points is None else list(entry_points)
    found = [_load_entry_point(ep, cfg) for ep in eps]
    if trusted_keys is None:
        from cymatix_context.plugins.trusted_keys import TRUSTED_PUBLISHER_KEYS

        trusted_keys = TRUSTED_PUBLISHER_KEYS
    ep_names = {lp.name for lp in found}
    found.extend(_discover_directory(cfg, plugin_dir, trusted_keys, ep_names))
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
