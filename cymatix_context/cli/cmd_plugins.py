"""`cymatix plugins list` — show discovered add-ons and their status."""
from __future__ import annotations

import argparse
import logging
from typing import Callable, Dict, Set

from . import output

log = logging.getLogger(__name__)

_WARNED: Set[str] = set()


def reset_warned() -> None:
    """Clear the warn-once state (for tests)."""
    _WARNED.clear()


def _disable(lp, reason: str) -> None:
    log.warning("plugin %s misbehaved: %s; disabling it", lp.name, reason)
    lp.status = "disabled"
    lp.reason = reason


def plugin_cli_commands(builtin: Set[str]) -> Dict[str, Callable[[list], int]]:
    """Commands offered by loaded plugins, excluding builtin names.

    A plugin that offers a builtin name is ignored for that command with one
    warning per name per process.
    """
    from cymatix_context.plugins import loader

    cmds: Dict[str, Callable[[list], int]] = {}
    owners: Dict[str, str] = {}
    clashed: Set[str] = set()
    for lp in loader.get_plugins():
        offered = loader.call_hook(lp, "cli_commands", default={})
        if offered is None:
            offered = {}
        if not isinstance(offered, dict):
            _disable(lp, f"cli_commands() returned {type(offered).__name__}, expected dict")
            continue
        bad = next((n for n, f in offered.items() if not isinstance(n, str) or not callable(f)), None)
        if bad is not None:
            _disable(lp, f"cli_commands() entry {bad!r} is not a name -> callable")
            continue
        for name, fn in offered.items():
            if name in builtin:
                if name not in _WARNED:
                    _WARNED.add(name)
                    log.warning(
                        "plugin %s offers command %r which is a builtin; ignoring",
                        lp.name, name,
                    )
                continue
            if name in clashed:
                continue
            if name in cmds:
                log.warning(
                    "plugins %s and %s both offer command %r; registering neither",
                    owners[name], lp.name, name,
                )
                del cmds[name]
                clashed.add(name)
                continue
            cmds[name] = fn
            owners[name] = lp.name
    return cmds


def run(argv: list) -> int:
    from cymatix_context.plugins import loader

    parser = argparse.ArgumentParser(prog="cymatix plugins", description="Inspect installed add-ons.")
    parser.add_argument("action", choices=["list"], help="list discovered plugins")
    parser.parse_args(argv)

    plugins = loader.get_plugins()
    # Surface builtin-name collisions even though only `list` was asked for.
    from .dispatcher import BUILTIN_COMMANDS

    plugin_cli_commands(set(BUILTIN_COMMANDS))

    header = ("name", "version", "source", "signed", "status")
    def _status(p) -> str:
        if not p.reason:
            return p.status
        reason = " ".join(str(p.reason).split())
        if len(reason) > 120:
            reason = reason[:117] + "..."
        return f"{p.status} ({reason})"

    rows = [
        (p.name, p.version, p.source, "yes" if p.signed else "no", _status(p))
        for p in plugins
    ]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(header)]
    for r in (header, *rows):
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    return output.EXIT_OK
