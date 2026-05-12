"""`helix diag <target>` — diagnostic introspection.

v1 only ships `corpus`. Future targets (`genome`, `index`, ...) plug
into the same argparse subparser.
"""
from __future__ import annotations

import argparse
from typing import Any, Dict

from . import output
from helix_context.api import open_session


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helix diag",
        description="Diagnostic introspection.",
    )
    sub = parser.add_subparsers(dest="target", metavar="<target>", required=True)
    corpus = sub.add_parser("corpus", help="Corpus shape: gene count, tier mix, staleness.")
    corpus.add_argument("--json", action="store_true", help="Machine-readable output.")
    return parser


def _corpus_payload(stats) -> Dict[str, Any]:
    """Project a StatsResult into the operator-facing corpus shape."""
    health = stats.metadata.get("health") if isinstance(stats.metadata, dict) else None
    return {
        "total_genes": stats.total_genes,
        "total_codons": stats.total_codons,
        "tier_distribution": {
            "open": stats.chromatin_open,
            "euchromatin": stats.chromatin_eu,
            "heterochromatin": stats.chromatin_hetero,
        },
        "compression_ratio": stats.compression_ratio,
        "staleness": health if isinstance(health, dict) else None,
    }


def _render_text(payload: Dict[str, Any]) -> list[str]:
    td = payload["tier_distribution"]
    lines = [
        f"total_genes: {payload['total_genes']}",
        f"total_codons: {payload['total_codons']}",
        f"compression_ratio: {payload['compression_ratio']}",
        "tier_distribution:",
        f"  open: {td['open']}",
        f"  euchromatin: {td['euchromatin']}",
        f"  heterochromatin: {td['heterochromatin']}",
    ]
    stale = payload.get("staleness")
    if isinstance(stale, dict):
        lines.append("staleness:")
        for k, v in stale.items():
            lines.append(f"  {k}: {v}")
    else:
        lines.append("staleness: (unavailable)")
    return lines


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        sess = open_session()
        stats = sess.stats()
    except Exception as exc:
        err = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if args.json:
            output.print_json(err)
        else:
            output.eprint(err["error"])
        return output.EXIT_ERROR

    payload = _corpus_payload(stats)
    if args.json:
        output.print_json(payload)
    else:
        output.print_lines(_render_text(payload))
    return output.EXIT_OK
