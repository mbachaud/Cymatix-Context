"""`helix neighbors` — top-k SEMA neighbors for a query.

Read-only, cheap. Mirrors the ``/debug/neighbors`` HTTP endpoint and
the ``helix_neighbors`` MCP tool. Returns an empty list (exit 0) when
the SEMA codec is unavailable or no embeddings are populated — the
``--json`` shape includes a ``count: 0`` so consumers don't have to
distinguish "no codec" from "no matches" structurally; check
``helix status`` or ``helix diag corpus`` for that.
"""
from __future__ import annotations

import argparse
from typing import Any, Dict, List

from . import output
from cymatix_context.api import open_session


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helix neighbors",
        description="Top-k SEMA neighbors for a query (semantic graph walk).",
    )
    parser.add_argument("text", help="The query string.")
    parser.add_argument(
        "--k", type=int, default=10,
        help="Number of neighbors to return (default: 10).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Machine-readable JSON.",
    )
    return parser


def _payload(query: str, k: int, neighbors: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "query": query,
        "k": k,
        "neighbors": neighbors,
        "count": len(neighbors),
    }


def _render_text(payload: Dict[str, Any]) -> list[str]:
    neighbors = payload.get("neighbors", []) or []
    if not neighbors:
        return [
            f"query: {payload['query']}",
            "neighbors: (none — SEMA codec missing or no embeddings yet)",
        ]
    lines = [f"query: {payload['query']}", f"k: {payload['k']}", "neighbors:"]
    for n in neighbors:
        path = n.get("path") or "(no path)"
        preview = (n.get("preview") or "").replace("\n", " ")
        lines.append(
            f"  [{n.get('sema_cos_sim', 0.0):.4f}] "
            f"{n.get('gene_id', '?')} — {path}"
        )
        if preview:
            lines.append(f"    {preview}")
    return lines


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        sess = open_session()
        neighbors = sess.neighbors(args.text, k=args.k)
    except Exception as exc:
        err = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if args.json:
            output.print_json(err)
        else:
            output.eprint(err["error"])
        return output.EXIT_ERROR

    payload = _payload(args.text, args.k, neighbors)
    if args.json:
        output.print_json(payload)
    else:
        output.print_lines(_render_text(payload))
    return output.EXIT_OK
