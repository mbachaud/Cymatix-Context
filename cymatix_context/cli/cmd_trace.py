"""`cymatix trace export` -- shareable, re-keyed export of packet traces."""
from __future__ import annotations

import argparse
import sys

from . import output
from .dispatcher import invoked_prog


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{invoked_prog()} trace",
        description="Packet trace tools (issue #493).",
    )
    sub = parser.add_subparsers(dest="action", metavar="<action>")
    exp = sub.add_parser("export", help="Export metadata-level traces with re-keyed hashes.")
    exp.add_argument("--in", dest="src", required=True,
                     help="Trace directory or a packets*.jsonl file.")
    exp.add_argument("--out", dest="out", required=True, help="Output directory.")
    return parser


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.action != "export":
        parser.print_help(file=sys.stderr)
        return output.EXIT_ERROR

    from ..telemetry import trace_export as te

    try:
        manifest = te.export_traces(args.src, args.out)
    except (te.ExportRefused, te.SecretFound, FileNotFoundError) as exc:
        print(f"trace export failed: {exc}", file=sys.stderr)
        return output.EXIT_ERROR
    print(f"exported {manifest['record_count']} record(s) to {args.out} "
          f"(consent={manifest['consent']}; parquet {manifest['parquet']})")
    return output.EXIT_OK
