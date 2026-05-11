"""`helix query` — run the retrieval pipeline once and print the result.

The CLI is a thin wrapper around ``helix_context.api.HelixSession.query``.
Mapping notes:
  * --tier broad   → decoder_mode="broad"     (more genes, longer)
  * --tier focused → decoder_mode="condensed" (fewer genes, tighter)
  * --tier (omit)  → no override; classifier picks

The drift between the bench spec's {broad, focused} vocabulary and the
internal {condensed, broad, dense} vocabulary is intentional in v1;
reconciliation is a v1.1 follow-up.
"""
from __future__ import annotations

import argparse
from typing import Any, Dict

from . import output
from helix_context.api import open_session


# Spec-vocab → internal-vocab mapping (see module docstring).
_TIER_TO_DECODER = {
    "broad": "broad",
    "focused": "condensed",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helix query",
        description="Run the helix retrieval pipeline for one query.",
    )
    parser.add_argument("text", help="The query string.")
    parser.add_argument(
        "--k", type=int, default=None,
        help="Cap on returned documents (default: honor static config).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Machine-readable JSON output (use for agent / bench consumption).",
    )
    parser.add_argument(
        "--tier", choices=("broad", "focused"), default=None,
        help="Walk-tier hint. broad=scout, focused=narrow. See cli.md for vocabulary.",
    )
    parser.add_argument(
        "--learn", action="store_true",
        help="Replicate the query (and eventually the response) back into the genome.",
    )
    return parser


def _render_text(payload: Dict[str, Any]) -> list[str]:
    lines = [
        f"verdict: {payload.get('verdict', 'unknown')}",
        f"evidence: {', '.join(payload.get('evidence', [])) or '(none)'}",
        f"estimated_tokens: {payload.get('estimated_tokens', 0)}",
    ]
    if payload.get("decision_reason"):
        lines.append(f"decision_reason: {payload['decision_reason']}")
    if payload.get("next_action"):
        lines.append(f"next_action: {payload['next_action']}")
    lines.append("")
    lines.append("--- expressed_context ---")
    lines.append(payload.get("expressed_context", ""))
    return lines


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    decoder_mode = _TIER_TO_DECODER.get(args.tier) if args.tier else None

    try:
        sess = open_session()
        result = sess.query(
            args.text,
            k=args.k,
            decoder_mode=decoder_mode,
            learn=args.learn,
        )
    except Exception as exc:
        err = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if args.json:
            output.print_json(err)
        else:
            output.eprint(err["error"])
        return output.EXIT_ERROR

    payload = result.to_agent_json()
    if args.json:
        output.print_json(payload)
    else:
        output.print_lines(_render_text(payload))
    return output.EXIT_OK
