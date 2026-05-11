"""`helix status` — cold-start health check."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any, Dict

from . import output


def _probe_genome(path: str) -> Dict[str, Any]:
    """Open the genome read-only and count rows. Cheap; no full pipeline."""
    p = Path(path)
    if not p.exists():
        return {
            "reachable": False,
            "path": str(p),
            "error": "file_not_found",
            "next_action": f"Create the genome by ingesting content: `helix ingest <path>`. Expected at {p}.",
        }
    try:
        # Open URI-style read-only so we don't accidentally lock the file.
        uri = f"file:{p.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            row = conn.execute("SELECT COUNT(*) FROM genes").fetchone()
            gene_count = int(row[0]) if row else 0
        finally:
            conn.close()
        return {
            "reachable": True,
            "path": str(p),
            "gene_count": gene_count,
        }
    except sqlite3.DatabaseError as exc:
        return {
            "reachable": False,
            "path": str(p),
            "error": "not_a_helix_genome",
            "detail": str(exc),
            "next_action": "Delete the file and re-ingest, or point [genome] path at the correct DB.",
        }


def _probe_config(config_path):
    """Validate helix.toml loads without error."""
    from helix_context.config import load_config
    try:
        cfg = load_config(config_path)
        return {
            "valid": True,
            "path": config_path or "(defaults)",
            "genome_path": cfg.genome.path,
            "server_port": cfg.server.port,
        }
    except Exception as exc:
        return {
            "valid": False,
            "path": config_path or "(defaults)",
            "error": type(exc).__name__,
            "detail": str(exc),
            "next_action": "Fix the [genome]/[server]/... sections in helix.toml.",
        }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helix status",
        description="Check genome / config / (optional) HTTP server health.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Skip HTTP server / launcher probes (offline check only).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to helix.toml (default: $HELIX_CONFIG or ./helix.toml).",
    )
    return parser


def _render_text(report: Dict[str, Any]) -> list[str]:
    lines = []
    g = report["genome"]
    lines.append(f"Genome: {'up' if g['reachable'] else 'down'} ({g.get('path', '?')})")
    if g["reachable"]:
        lines.append(f"  gene_count: {g.get('gene_count', '?')}")
    elif g.get("next_action"):
        lines.append(f"  fix: {g['next_action']}")

    c = report["config"]
    lines.append(f"Config: {'valid' if c['valid'] else 'invalid'} ({c.get('path', '?')})")
    if not c["valid"]:
        lines.append(f"  fix: {c.get('next_action', '')}")

    s = report.get("server")
    if s is not None:
        lines.append(
            f"Server: {'up' if s.get('reachable') else 'down'} ({s.get('url', '?')})"
        )

    lines.append("")
    lines.append(f"Next action: {report['next_action']}")
    return lines


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    from helix_context.config import load_config
    try:
        cfg = load_config(args.config)
        genome_path = cfg.genome.path
    except Exception:
        genome_path = "genome.db"   # best-effort default

    config_report = _probe_config(args.config)
    genome_report = _probe_genome(genome_path)

    report: Dict[str, Any] = {
        "genome": genome_report,
        "config": config_report,
    }

    # Optional network probes — reuse the existing helix-status logic.
    if not args.no_network:
        try:
            from helix_status import collect_status
            net = collect_status()
            report["server"] = {
                "reachable": net["server"]["reachable"],
                "url": net["server"]["url"],
            }
            report["launcher"] = {
                "reachable": net["launcher"]["reachable"],
                "url": net["launcher"]["url"],
            }
        except Exception as exc:
            report["server"] = {"reachable": False, "error": str(exc)}

    # Compute aggregate next_action + return code.
    if not genome_report["reachable"]:
        report["next_action"] = genome_report.get("next_action", "Bring the genome online.")
        rc = output.EXIT_STATUS_FAIL
    elif not config_report["valid"]:
        report["next_action"] = config_report.get("next_action", "Fix helix.toml.")
        rc = output.EXIT_STATUS_FAIL
    else:
        report["next_action"] = "Healthy — try `helix query \"...\"`."
        rc = output.EXIT_OK

    if args.json:
        output.print_json(report)
    else:
        output.print_lines(_render_text(report))
    return rc
