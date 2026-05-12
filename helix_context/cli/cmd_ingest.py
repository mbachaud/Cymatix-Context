"""`helix ingest <path>` — read a file or directory into the genome."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable, List

from . import output
from helix_context.api import open_session


_DEFAULT_EXTENSIONS = (".txt", ".md", ".rst", ".py", ".ts", ".js", ".json", ".toml", ".yml", ".yaml")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helix ingest",
        description="Add a file (or directory of files) to the genome.",
    )
    parser.add_argument("path", help="Path to a file or directory.")
    parser.add_argument(
        "--recursive", "-r", action="store_true",
        help="Walk subdirectories when path is a directory.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Machine-readable output.",
    )
    parser.add_argument(
        "--ext",
        action="append",
        default=None,
        help="File extension to include (e.g. --ext .txt). Repeatable. "
             "Default: " + ", ".join(_DEFAULT_EXTENSIONS),
    )
    return parser


def _collect_files(root: Path, recursive: bool, exts: Iterable[str]) -> List[Path]:
    ext_set = {e.lower() if e.startswith(".") else "." + e.lower() for e in exts}
    if root.is_file():
        # Honor the extension filter even when the user pointed at a single file
        # — otherwise `helix ingest binary.exe` would silently ingest replacement
        # characters via the errors="replace" decode.
        return [root] if root.suffix.lower() in ext_set else []
    if not root.is_dir():
        return []
    iterator = root.rglob("*") if recursive else root.iterdir()
    return sorted(p for p in iterator if p.is_file() and p.suffix.lower() in ext_set)


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    root = Path(args.path)
    if not root.exists():
        err = {"ok": False, "error": f"path not found: {root}"}
        if args.json:
            output.print_json(err)
        else:
            output.eprint(err["error"])
        return output.EXIT_ERROR

    exts = args.ext or _DEFAULT_EXTENSIONS
    files = _collect_files(root, args.recursive, exts)
    if not files:
        msg = {
            "ok": False,
            "error": f"no matching files under {root} (extensions: {list(exts)})",
        }
        if args.json:
            output.print_json(msg)
        else:
            output.eprint(msg["error"])
        return output.EXIT_ERROR

    # Opening the session itself is a hard-stop failure — no partial
    # progress is possible if we can't even talk to the genome.
    try:
        sess = open_session()
    except Exception as exc:
        err = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "files_processed": 0,
            "gene_ids": [],
            "bytes_written": 0,
            "errors": [],
        }
        if args.json:
            output.print_json(err)
        else:
            output.eprint(err["error"])
        return output.EXIT_ERROR

    all_gene_ids: list[str] = []
    total_bytes = 0
    errors: list[dict] = []
    files_processed = 0
    for f in files:
        # Per-file try/except so one bad file (locked, permission denied,
        # pipeline error mid-batch) doesn't nuke the progress from the
        # other 999 files in the directory. KeyboardInterrupt is allowed
        # to propagate so Ctrl-C still works.
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            result = sess.ingest(content)
            all_gene_ids.extend(result.gene_ids)
            total_bytes += result.bytes_written
            files_processed += 1
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            errors.append({
                "file": str(f),
                "error": f"{type(exc).__name__}: {exc}",
            })

    payload = {
        "ok": not errors,
        "files_processed": files_processed,
        "gene_ids": all_gene_ids,
        "bytes_written": total_bytes,
        "errors": errors,
    }
    if args.json:
        output.print_json(payload)
    else:
        lines = [
            f"ingested {files_processed} file(s)",
            f"  gene_ids: {len(all_gene_ids)}",
            f"  bytes:    {total_bytes}",
        ]
        if errors:
            lines.append(f"  failed:   {len(errors)} file(s)")
            for e in errors:
                lines.append(f"    - {e['file']}: {e['error']}")
        output.print_lines(lines)
    return output.EXIT_OK if not errors else output.EXIT_ERROR
