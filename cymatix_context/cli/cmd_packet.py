"""`helix packet` — agent-safe evidence bundle with freshness labels.

Wraps :meth:`cymatix_context.api.HelixSession.packet`, which itself
delegates to ``cymatix_context.context_packet.build_context_packet`` —
the same builder the FastAPI ``/context/packet`` endpoint and the
``helix_context_packet`` MCP tool use. Output shape is identical so
agents can swap surfaces (CLI ↔ MCP ↔ HTTP) without changing call
logic.

The packet is the right surface for high-risk actions (edit, ops):
it returns freshness-labeled evidence (verified / stale_risk) plus
an explicit ``refresh_targets`` reread plan, instead of raw bytes
that may be stale.
"""
from __future__ import annotations

import argparse

from .dispatcher import invoked_prog
from typing import Any, Dict

from . import output
from cymatix_context.api import open_session


# Task-type vocabulary matches the MCP tool + /context/packet endpoint.
# Listed explicitly so `--task-type foo` fails fast at argparse rather
# than silently coercing inside the builder.
_TASK_TYPES = ("plan", "explain", "review", "edit", "debug", "ops", "quote")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{invoked_prog()} packet",
        description=(
            "Build a freshness-labeled evidence packet for an agent action. "
            "Returns verified / stale_risk items plus refresh_targets."
        ),
    )
    parser.add_argument("text", help="The query string.")
    parser.add_argument(
        "--task-type",
        choices=_TASK_TYPES,
        default="explain",
        help=(
            "Risk profile (default: explain). Higher-risk types (edit, ops) "
            "apply stricter freshness + coordinate-confidence gates."
        ),
    )
    parser.add_argument(
        "--max-genes", type=int, default=8,
        help="Retrieval top-K cap (default: 8).",
    )
    parser.add_argument(
        "--include-raw", action="store_true",
        help=(
            "Return the full gene content per item instead of the "
            "compressor-compressed summary. Use when the packet is the only "
            "context source and the downstream LLM needs real bytes."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Machine-readable JSON (use for agent / bench consumption).",
    )
    return parser


def _packet_to_payload(packet) -> Dict[str, Any]:
    """Project a ``ContextPacket`` pydantic model into the agent-facing
    JSON shape. Identical structure to the ``/context/packet`` endpoint
    response so callers can swap CLI ↔ HTTP without code changes."""
    return packet.model_dump()


def _render_text(payload: Dict[str, Any]) -> list[str]:
    verified = payload.get("verified", []) or []
    stale = payload.get("stale_risk", []) or []
    refresh = payload.get("refresh_targets", []) or []
    lines = [
        f"task_type: {payload.get('task_type', '?')}",
        f"query: {payload.get('query', '')}",
        f"coordinate_confidence: {payload.get('coordinate_confidence', 0.0):.2f}",
        f"file_coverage: {payload.get('file_coverage', 0.0):.2f}",
        f"verified: {len(verified)}",
        f"stale_risk: {len(stale)}",
        f"refresh_targets: {len(refresh)}",
    ]
    know = payload.get("know")
    miss = payload.get("miss")
    if know is not None:
        lines.append(f"verdict: know (soft_stale={know.get('soft_stale', False)})")
    elif miss is not None:
        lines.append(f"verdict: miss (escalate_to={miss.get('escalate_to', [])})")
    notes = payload.get("notes") or []
    if notes:
        lines.append("notes:")
        for n in notes:
            lines.append(f"  - {n}")
    if refresh:
        lines.append("refresh_targets:")
        for t in refresh:
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
        packet = sess.packet(
            args.text,
            task_type=args.task_type,
            max_genes=args.max_genes,
            include_raw=args.include_raw,
        )
    except Exception as exc:
        err = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if args.json:
            output.print_json(err)
        else:
            output.eprint(err["error"])
        return output.EXIT_ERROR

    payload = _packet_to_payload(packet)
    if args.json:
        output.print_json(payload)
    else:
        output.print_lines(_render_text(payload))
    return output.EXIT_OK
