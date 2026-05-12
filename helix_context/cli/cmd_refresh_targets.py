"""`helix refresh-targets` — just the reread plan, no evidence items.

Useful when the caller already has content cached and only needs to
know which sources are stale enough that rereading is required before
a high-risk action completes. Same call as ``helix packet`` then
projected down to the ``refresh_targets`` list — cheap to build, no
extra retrieval pass.

Mirrors the ``/context/refresh-plan`` HTTP endpoint and the
``helix_refresh_targets`` MCP tool.
"""
from __future__ import annotations

import argparse
from typing import Any, Dict, List

from . import output
from helix_context.api import open_session


_TASK_TYPES = ("plan", "explain", "review", "edit", "debug", "ops", "quote")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helix refresh-targets",
        description=(
            "Return the reread plan for a query — refresh_targets only, "
            "no evidence items. Default task_type is 'edit'."
        ),
    )
    parser.add_argument("text", help="The query string.")
    parser.add_argument(
        "--task-type",
        choices=_TASK_TYPES,
        default="edit",
        help="Risk profile (default: edit — the usual caller).",
    )
    parser.add_argument(
        "--max-genes", type=int, default=8,
        help="Retrieval top-K cap (default: 8).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Machine-readable JSON.",
    )
    return parser


def _payload(targets) -> Dict[str, List[Dict[str, Any]]]:
    """Flatten the RefreshTarget pydantic models to dicts."""
    return {
        "refresh_targets": [t.model_dump() for t in targets],
        "count": len(targets),
    }


def _render_text(payload: Dict[str, Any]) -> list[str]:
    targets = payload.get("refresh_targets", []) or []
    if not targets:
        return ["refresh_targets: (none — packet would be clean)"]
    lines = [f"refresh_targets: {len(targets)}"]
    for t in targets:
        lines.append(
            f"  - [{t.get('priority', 0.0):.2f}] "
            f"{t.get('target_kind', '?')}:{t.get('source_id', '?')} "
            f"— {t.get('reason', '')}"
        )
    return lines


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        sess = open_session()
        targets = sess.refresh_targets(
            args.text,
            task_type=args.task_type,
            max_genes=args.max_genes,
        )
    except Exception as exc:
        err = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if args.json:
            output.print_json(err)
        else:
            output.eprint(err["error"])
        return output.EXIT_ERROR

    payload = _payload(targets)
    if args.json:
        output.print_json(payload)
    else:
        output.print_lines(_render_text(payload))
    return output.EXIT_OK
