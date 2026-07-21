"""Shared output helpers + exit-code constants for the helix CLI.

Every subcommand returns one of these integers from main(). Keeps the
contract testable without parsing stderr.
"""
from __future__ import annotations

import json
import sys
from typing import Any

# Exit codes (see docs/superpowers/plans/2026-05-11-helix-cli-v1.md).
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BAD_ARGS = 2          # argparse hands this out itself
EXIT_STATUS_FAIL = 3       # `helix status` only
EXIT_DEFERRED = 4          # `helix serve` and any other "not yet" subcommand


def print_json(obj: Any) -> None:
    """Stable, machine-readable JSON output to stdout.

    sort_keys=True so the bench walker can hash deterministic output.
    """
    sys.stdout.write(json.dumps(obj, indent=2, sort_keys=True, default=str))
    sys.stdout.write("\n")


def print_lines(lines: list[str]) -> None:
    """TTY-friendly newline-joined output."""
    sys.stdout.write("\n".join(lines))
    sys.stdout.write("\n")


def eprint(msg: str) -> None:
    """Single-line error to stderr (no trailing newline-on-newline)."""
    sys.stderr.write(msg.rstrip() + "\n")
