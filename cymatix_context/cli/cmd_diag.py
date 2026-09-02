"""`cymatix diag <target>` — diagnostic introspection.

Targets: `corpus` (shape of the configured store) and `bed` (build
provenance of any bed file on disk). Future targets plug into the same
argparse subparser.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .dispatcher import invoked_prog
from typing import Any, Dict

from . import output
from cymatix_context.api import open_session


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{invoked_prog()} diag",
        description="Diagnostic introspection.",
    )
    sub = parser.add_subparsers(dest="target", metavar="<target>", required=True)
    corpus = sub.add_parser("corpus", help="Corpus shape: gene count, tier mix, staleness.")
    corpus.add_argument("--json", action="store_true", help="Machine-readable output.")

    bed = sub.add_parser(
        "bed",
        help="Bed provenance: what built this store, on what code, and how it has aged.",
    )
    bed.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Bed file to inspect (default: the configured genome path).",
    )
    bed.add_argument(
        "--identity",
        action="store_true",
        help="Also compute the live gene_id digest (full scan; slow on large beds).",
    )
    bed.add_argument("--json", action="store_true", help="Machine-readable output.")
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


def _bed_payload(db_path: str, want_identity: bool) -> Dict[str, Any]:
    """Read provenance out of a bed without going through the retrieval stack."""
    import sqlite3

    from cymatix_context.storage import provenance

    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        events = provenance.read_events(conn)
        payload: Dict[str, Any] = {
            "db": str(db_path),
            "bytes": Path(db_path).stat().st_size,
            "live_gene_count": provenance.gene_count(conn),
            "live_fts_count": provenance.fts_count(conn),
            "events": events,
            "config_drift": provenance.config_drift(events),
        }
        if want_identity:
            payload["live_identity_sha256"] = provenance.identity_sha256(conn)
        return payload
    finally:
        conn.close()


def _render_bed(payload: Dict[str, Any]) -> list[str]:
    lines = [
        f"db: {payload['db']}",
        f"bytes: {payload['bytes']}",
        f"live_gene_count: {payload['live_gene_count']}",
        f"live_fts_count: {payload['live_fts_count']}",
    ]
    if "live_identity_sha256" in payload:
        lines.append(f"live_identity_sha256: {payload['live_identity_sha256']}")
    events = payload.get("events") or []
    if not events:
        lines.append(
            "events: (none - this bed predates provenance stamping; "
            "run scripts/stamp_bed_provenance.py to retro-stamp it)"
        )
        return lines
    lines.append(f"events: {len(events)}")
    for e in events:
        lines.append(
            f"  [{e['id']}] {e['at_utc']} {e['event']}"
            f" v{e['cymatix_version']}"
            f" sha={(e.get('git_sha') or '?')[:12]}"
            f"{'+dirty' if e.get('git_dirty') else ''}"
            f" ingest_c={e.get('ingest_c')}"
            f" genes={e.get('gene_count')}"
        )
        if e.get("notes"):
            lines.append(f"        note: {e['notes']}")
    drift = payload.get("config_drift") or []
    if drift:
        lines.append("config_drift:")
        for d in drift:
            lines.append(f"  {d['from_id']} -> {d['to_id']} ({d['at_utc']}):")
            for knob, ch in d["changed"].items():
                lines.append(f"    {knob}: {ch['from']} -> {ch['to']}")
    else:
        lines.append("config_drift: none")
    return lines


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.target == "bed":
        try:
            db_path = args.db
            if db_path is None:
                from cymatix_context.config import load_config

                db_path = load_config().genome.path
            if not Path(db_path).exists():
                raise FileNotFoundError(db_path)
            payload = _bed_payload(db_path, args.identity)
        except Exception as exc:
            err = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if args.json:
                output.print_json(err)
            else:
                output.eprint(err["error"])
            return output.EXIT_ERROR
        if args.json:
            output.print_json(payload)
        else:
            output.print_lines(_render_bed(payload))
        return output.EXIT_OK

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
