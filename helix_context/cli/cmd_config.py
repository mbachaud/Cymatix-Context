"""`helix config show` — print effective helix.toml + env overrides."""
from __future__ import annotations

import argparse
import dataclasses
import json
from typing import Any, Dict

from . import output


def _config_to_dict(cfg: Any) -> Dict[str, Any]:
    """Convert a HelixConfig dataclass to a JSON-serializable dict."""
    return dataclasses.asdict(cfg)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helix config",
        description="Inspect the effective Helix configuration.",
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
            elif isinstance(v, list):
                lines.append(f"{key} = {json.dumps(v)}")
            else:
                lines.append(f"{key} = {v}")
    else:
        lines.append(f"{prefix} = {obj}")
    return lines


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.action != "show":
        parser.print_help()
        return output.EXIT_BAD_ARGS

    # Late import — avoid loading config module when other subcommands run.
    from helix_context.config import load_config

    cfg = load_config(args.config)
    payload = _config_to_dict(cfg)

    if args.text:
        output.print_lines(_flatten_for_text(payload))
    else:
        output.print_json(payload)
    return output.EXIT_OK
