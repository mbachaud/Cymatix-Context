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
        "packet",
        help="Build a freshness-labeled agent-safe evidence bundle.",
    )
    sub.add_parser(
        "refresh-targets",
        help="Reread plan only — refresh_targets without evidence items.",
    )
    sub.add_parser(
        "gene",
        help="Inspect a single document (`helix gene get|preview <id>`).",
    )
    sub.add_parser(
        "neighbors",
        help="Top-k SEMA neighbors for a query (semantic graph walk).",
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
    if name == "packet":
        from . import cmd_packet
        return cmd_packet.run
    if name == "refresh-targets":
        from . import cmd_refresh_targets
        return cmd_refresh_targets.run
    if name == "gene":
        from . import cmd_gene
        return cmd_gene.run
    if name == "neighbors":
        from . import cmd_neighbors
        return cmd_neighbors.run
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
    # When called as an installed entry point (no explicit argv), read from
    # sys.argv[1:] — the standard Python CLI convention.
    if argv is None:
        argv = sys.argv[1:]

    # No args → print usage to stderr and exit 1.
    if not argv:
        parser.print_help(file=sys.stderr)
        return output.EXIT_ERROR

    # Let argparse handle top-level flags (--help → SystemExit(0); unknown
    # subcommand → SystemExit(2); future --version / --verbose work as
    # declared on the parser). Only parse the first token to keep
    # subcommand-specific flags untouched.
    parsed = parser.parse_args(argv[:1])
    sub_name = parsed.subcommand
    if sub_name is None:
        parser.print_help(file=sys.stderr)
        return output.EXIT_ERROR

    runner = _resolve(sub_name)
    if runner is None:
        # Defensive — argparse should have errored above on unknown choices.
        return output.EXIT_BAD_ARGS

    return runner(argv[1:])
