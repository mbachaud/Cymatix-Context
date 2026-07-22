"""`helix config show` — print effective helix.toml + env overrides."""
from __future__ import annotations

import argparse

from .dispatcher import invoked_prog
import dataclasses
import json
import sys
from typing import Any, Dict

from . import output


def _config_to_dict(cfg: Any) -> Dict[str, Any]:
    """Convert a HelixConfig dataclass to a JSON-serializable dict."""
    return dataclasses.asdict(cfg)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{invoked_prog()} config",
        description="Inspect the effective Cymatix configuration.",
    )
    sub = parser.add_subparsers(dest="action", metavar="<action>")
    show = sub.add_parser("show", help="Print effective config (helix.toml + env).")
    show.add_argument(
        "--text",
        action="store_true",
        help="Print as flat key=value lines instead of JSON.",
    )
    show.add_argument(
        "--config",
        default=None,
        help="Path to helix.toml (default: $HELIX_CONFIG or ./helix.toml).",
    )
    return parser


def _flatten_for_text(obj: Any, prefix: str = "") -> list[str]:
    """Render nested dict/list as `dotted.key = value` lines."""
    lines: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                lines.extend(_flatten_for_text(v, prefix=key))
            else:
                # Use json.dumps for scalars AND lists — keeps booleans,
                # None, and strings unambiguous in the flattened output.
                lines.append(f"{key} = {json.dumps(v)}")
    else:
        lines.append(f"{prefix} = {json.dumps(obj)}")
    return lines


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.action != "show":
        # Help to stderr keeps stdout clean for `--json` consumers — matches
        # dispatcher.py's convention.
        parser.print_help(file=sys.stderr)
        return output.EXIT_BAD_ARGS

    # Late import — avoid loading config module when other subcommands run.
    from cymatix_context.config import load_config

    try:
        cfg = load_config(args.config)
    except Exception as exc:
        # A malformed helix.toml previously dumped a raw traceback. CI
        # consumers that pipe `helix config show --json` need structured
        # error output instead. Mirrors cmd_status._probe_config's pattern.
        err = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "next_action": "Fix the [genome]/[server]/... sections in helix.toml.",
        }
        if args.text:
            output.eprint(err["error"])
        else:
            output.print_json(err)
        return output.EXIT_ERROR

    payload = _config_to_dict(cfg)

    if args.text:
        output.print_lines(_flatten_for_text(payload))
    else:
        output.print_json(payload)
    return output.EXIT_OK
