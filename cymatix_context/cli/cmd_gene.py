"""`helix gene <action>` — inspect a single document.

Two actions:
  * ``get <id>``     — full document (content + tags + signals + tier
                       + embedding). Use when you need everything.
  * ``preview <id>`` — content-only snippet, capped at ``--chars`` (default
                       240). Cheaper than ``get`` when you just want to
                       eyeball whether the document is relevant.

Both wrap :meth:`cymatix_context.api.HelixSession.gene_get`, which uses
``Genome.get_gene`` under the hood. Read-only.

Vocabulary note: the subcommand is ``gene`` to match the legacy MCP
tool name (``helix_gene_get``) — the canonical engineering alias is
``helix_document_get`` per ROSETTA.md. A future ``helix document``
top-level subcommand alias can co-exist once the R4 soft-deprecation
wave lands.
"""
from __future__ import annotations

import argparse

from .dispatcher import invoked_prog
from typing import Any, Dict, Optional

from . import output
from cymatix_context.api import open_session


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{invoked_prog()} gene",
        description="Inspect a single document by ID.",
    )
    sub = parser.add_subparsers(dest="action", metavar="<action>", required=True)

    get_p = sub.add_parser("get", help="Full document (content + tags + signals + tier).")
    get_p.add_argument("gene_id", help="The document ID (e.g. gene-abc123…).")
    get_p.add_argument("--json", action="store_true", help="Machine-readable JSON.")

    prev_p = sub.add_parser("preview", help="Content-only snippet, char-capped.")
    prev_p.add_argument("gene_id", help="The document ID.")
    prev_p.add_argument(
        "--chars", type=int, default=240,
        help="Max characters to include in the preview (default: 240).",
    )
    prev_p.add_argument("--json", action="store_true", help="Machine-readable JSON.")

    return parser


def _get_payload(gene) -> Dict[str, Any]:
    return gene.model_dump()


def _preview_payload(gene, chars: int) -> Dict[str, Any]:
    content = gene.content or ""
    snippet = content[:chars]
    truncated = len(content) > chars
    path: Optional[str] = None
    if gene.promoter and gene.promoter.metadata:
        path = gene.promoter.metadata.get("path")
    return {
        "gene_id": gene.gene_id,
        "preview": snippet,
        "truncated": truncated,
        "total_chars": len(content),
        "path": path,
    }


def _render_get_text(payload: Dict[str, Any]) -> list[str]:
    promoter = payload.get("promoter") or {}
    tags_domains = promoter.get("domains") or []
    tags_entities = promoter.get("entities") or []
    lines = [
        f"gene_id: {payload.get('gene_id', '?')}",
        f"chromatin: {payload.get('chromatin', '?')}",
        f"domains: {', '.join(tags_domains) or '(none)'}",
        f"entities: {', '.join(tags_entities) or '(none)'}",
        f"content_chars: {len(payload.get('content') or '')}",
        "",
        "--- content ---",
        payload.get("content") or "",
    ]
    return lines


def _render_preview_text(payload: Dict[str, Any]) -> list[str]:
    lines = [
        f"gene_id: {payload['gene_id']}",
        f"total_chars: {payload['total_chars']} "
        f"({'truncated' if payload['truncated'] else 'complete'})",
        f"path: {payload.get('path') or '(none)'}",
        "",
        "--- preview ---",
        payload["preview"],
    ]
    return lines


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        sess = open_session()
        gene = sess.gene_get(args.gene_id)
    except Exception as exc:
        err = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if args.json:
            output.print_json(err)
        else:
            output.eprint(err["error"])
        return output.EXIT_ERROR

    if gene is None:
        err = {"ok": False, "error": f"unknown gene_id: {args.gene_id}"}
        if args.json:
            output.print_json(err)
        else:
            output.eprint(err["error"])
        return output.EXIT_ERROR

    if args.action == "get":
        payload = _get_payload(gene)
        if args.json:
            output.print_json(payload)
        else:
            output.print_lines(_render_get_text(payload))
        return output.EXIT_OK

    # preview
    payload = _preview_payload(gene, args.chars)
    if args.json:
        output.print_json(payload)
    else:
        output.print_lines(_render_preview_text(payload))
    return output.EXIT_OK
