"""`cymatix plugins list` — show discovered add-ons and their status."""
from __future__ import annotations

import argparse
import logging
from typing import Callable, Dict, Set

from . import output

log = logging.getLogger(__name__)

_WARNED: Set[str] = set()


def plugin_cli_commands(builtin: Set[str]) -> Dict[str, Callable[[list], int]]:
    """Commands offered by loaded plugins, excluding builtin names.

    A plugin that offers a builtin name is ignored for that command with one
    warning per name per process.
    """
    from cymatix_context.plugins import loader

    cmds: Dict[str, Callable[[list], int]] = {}
    for lp in loader.get_plugins():
        offered = loader.call_hook(lp, "cli_commands", default={}) or {}
        for name, fn in offered.items():
            if name in builtin:
                if name not in _WARNED:
                    _WARNED.add(name)
                    log.warning(
                        "plugin %s offers command %r which is a builtin; ignoring",
                        lp.name, name,
                    )
                continue
            cmds.setdefault(name, fn)
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
    rows = [
        (p.name, p.version, p.source, "yes" if p.signed else "no", p.status + (f" ({p.reason})" if p.reason else ""))
        for p in plugins
    ]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(header)]
    for r in (header, *rows):
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    return output.EXIT_OK
