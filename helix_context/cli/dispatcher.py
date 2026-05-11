"""Top-level argparse dispatcher for `helix <subcommand>`.

Subcommands are registered via add_parser; each one points at a
``cmd_*.run(args)`` callable that returns an integer exit code.

This module deliberately does NOT import the heavy retrieval stack at
module-load time — subcommand modules import their dependencies lazily
inside ``run()`` so `helix --help` stays sub-100ms cold-start.
"""
from __future__ import annotations

import argparse
import sys
from typing import Callable, Optional

from . import output


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helix",
        description=(
            "Helix Context CLI — cold-start retrieval over a local genome. "
            "See `helix <subcommand> --help` for details."
        ),
    )
    sub = parser.add_subparsers(dest="subcommand", metavar="<subcommand>")

    # Each cmd module owns its own argparse population; we only declare
    # the subcommand stub here so --help lists it.
    sub.add_parser(
        "query",
        help="Run one retrieval pipeline pass over the genome and print the result.",
    )
    sub.add_parser(
        "ingest",
        help="Add a file (or directory of files) to the genome.",
    )
    sub.add_parser(
        "status",
        help="Check whether the genome + config + (optional) server are healthy.",
    )
    sub.add_parser(
        "diag",
        help="Diagnostic introspection (e.g. `helix diag corpus`).",
    )
    sub.add_parser(
        "config",
        help="Inspect effective configuration (`helix config show`).",
    )
    sub.add_parser(
        "serve",
        help="(DEFERRED in v1; see HELIX_DAEMON_DESIGN.md)",
    )

    return parser


def _resolve(name: str) -> Optional[Callable[[list[str]], int]]:
    """Lazy-load the cmd module so unused subcommands don't pay import cost."""
    if name == "query":
        from . import cmd_query
        return cmd_query.run
    if name == "ingest":
        from . import cmd_ingest
        return cmd_ingest.run
    if name == "status":
        from . import cmd_status
        return cmd_status.run
    if name == "diag":
        from . import cmd_diag
        return cmd_diag.run
    if name == "config":
        from . import cmd_config
        return cmd_config.run
    if name == "serve":
        from . import cmd_serve
        return cmd_serve.run
    return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = argv if argv is not None else None
    # `helix` with no args → print help to stderr, return 1.
    if not args:
        parser.print_help(file=sys.stderr)
        return output.EXIT_ERROR
    # `helix --help` / `helix -h` → argparse default (exit 0).
    if args[0] in ("--help", "-h"):
        parser.parse_args(args)  # this SystemExit(0)s
        return output.EXIT_OK    # unreachable, kept for type clarity

    sub_name, sub_argv = args[0], args[1:]
    runner = _resolve(sub_name)
    if runner is None:
        # Force argparse to do the "invalid choice" error formatting for us
        # so the message matches what users expect.
        parser.parse_args(args)
        return output.EXIT_BAD_ARGS  # unreachable

    return runner(sub_argv)
