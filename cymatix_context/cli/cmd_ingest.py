"""`cymatix ingest <path>` — read a file or directory into the genome."""
from __future__ import annotations

import argparse
import os

from .dispatcher import invoked_prog
from pathlib import Path
from typing import Iterable, List

from . import output
from cymatix_context.api import open_session


_DEFAULT_EXTENSIONS = (".txt", ".md", ".rst", ".py", ".ts", ".js", ".json", ".toml", ".yml", ".yaml")

# Directories never descended into during a recursive walk (#323).
#
# Without this, `cymatix ingest <repo> --recursive` matched on extension alone
# and happily indexed the repo's own virtualenv: measured on a real run, 5,331
# of the first 5,337 files ingested were `.venv/Lib/site-packages`. That is
# corpus poisoning, not just wasted time — a store full of library internals
# out-ranks the user's own code, and the symptom looks like bad retrieval
# rather than bad ingest.
#
# These are vendored, generated, or cache trees: none of them is content a user
# means to make retrievable. `--exclude-dir` extends the set;
# `--no-default-excludes` drops it entirely for the rare deliberate case.
_DEFAULT_EXCLUDE_DIRS = frozenset({
    ".git", ".hg", ".svn",
    ".venv", "venv", "env", "virtualenv", "site-packages",
    "node_modules", "bower_components", "vendor",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".cache", ".next", ".nuxt", ".parcel-cache", ".turbo",
    "dist", "build", "target", "out", "htmlcov", ".coverage",
    ".idea", ".vscode", ".gradle", ".terraform",
})

# Extensions whose contents should be chunked as CODE (AST/structure-aware via
# the tree-sitter chunker) rather than prose paragraphs. Issue #224: the ingest
# CLI previously never set content_type, so code was chunked as prose and the
# AST/code chunker (encoding/tree_chunker.py + CodonChunker._chunk_code) was
# never reached. Inferring content_type from the file extension activates it.
_CODE_EXTENSIONS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java", ".c", ".h",
    ".cc", ".cpp", ".hpp", ".hh", ".cs", ".rb", ".php", ".lua", ".scala",
    ".kt", ".kts", ".swift", ".sh", ".bash", ".sql", ".m", ".mm",
})


def _content_type_for(path: Path) -> str:
    """Infer ingest content_type from a file's extension (#224).

    Code extensions route to the code chunker (function/class units via
    tree-sitter when available, regex fallback otherwise); everything else
    is chunked as text. Markup like .md/.rst/.json stays text on purpose.
    """
    return "code" if path.suffix.lower() in _CODE_EXTENSIONS else "text"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{invoked_prog()} ingest",
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
    parser.add_argument(
        "--exclude-dir",
        action="append",
        default=None,
        metavar="NAME",
        help="Directory name to skip during a recursive walk. Repeatable. "
             "Adds to the default set (.venv, node_modules, .git, __pycache__, "
             "dist, build, …), which is skipped unless --no-default-excludes.",
    )
    parser.add_argument(
        "--no-default-excludes", action="store_true",
        help="Walk vendored/generated trees too (.venv, node_modules, …). "
             "Off by default: ingesting a virtualenv poisons the store with "
             "third-party library source.",
    )
    parser.add_argument(
        "--okf", action="store_true",
        help="Treat path as an OKF v0.1 knowledge bundle directory "
             "(markdown + YAML frontmatter). Walks every non-reserved "
             ".md file itself; --recursive/--ext do not apply.",
    )
    parser.add_argument(
        "--bundle-id", default=None,
        help="Override the OKF bundle id (default: bundle directory name). "
             "Only with --okf.",
    )
    parser.add_argument(
        "--deterministic", action="store_true",
        help="Deterministic-ingest profile: disable SEMA/dense/SPLADE "
             "encodes at ingest so the store contains no float tensors. "
             "Embeddings are backfilled per host afterwards "
             "(scripts/backfill_bgem3_v2.py) as per-host artifacts — they "
             "are never covered by the OKF interop claim. Only with --okf.",
    )
    return parser


def _collect_files(
    root: Path,
    recursive: bool,
    exts: Iterable[str],
    exclude_dirs: Iterable[str] | None = None,
) -> List[Path]:
    ext_set = {e.lower() if e.startswith(".") else "." + e.lower() for e in exts}
    if root.is_file():
        # Honor the extension filter even when the user pointed at a single file
        # — otherwise `cymatix ingest binary.exe` would silently ingest replacement
        # characters via the errors="replace" decode.
        return [root] if root.suffix.lower() in ext_set else []
    if not root.is_dir():
        return []

    skip = {d.lower() for d in
            (_DEFAULT_EXCLUDE_DIRS if exclude_dirs is None else exclude_dirs)}

    if not recursive:
        return sorted(p for p in root.iterdir()
                      if p.is_file() and p.suffix.lower() in ext_set)

    # os.walk with in-place dirnames pruning, not rglob("*") + post-filter:
    # pruning means an excluded subtree is never descended into at all.
    # Post-filtering would still stat every file in a 40k-file virtualenv
    # before discarding it, which is the slow half of the #323 bug.
    out: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in skip and not d.lower().endswith(".egg-info")]
        for fn in filenames:
            if Path(fn).suffix.lower() in ext_set:
                out.append(Path(dirpath) / fn)
    return sorted(out)


def _run_okf(args) -> int:
    """`cymatix ingest --okf <bundle_dir>` — ingest an OKF v0.1 bundle."""
    root = Path(args.path)
    if not root.is_dir():
        err = {"ok": False, "error": f"--okf requires a bundle directory: {root}"}
        if args.json:
            output.print_json(err)
        else:
            output.eprint(err["error"])
        return output.EXIT_ERROR

    # Default = standard config: the public determinism claim is scoped to
    # the canonical digest, not to stored bytes. --deterministic opts into
    # the float-free profile (embeddings backfilled per host afterwards).
    config = None
    if args.deterministic:
        from cymatix_context.config import load_config

        config = load_config()
        config.ingestion.sema_embed_on_ingest = False
        config.ingestion.dense_embed_on_ingest = False
        config.ingestion.splade_enabled = False

    try:
        sess = open_session(config=config)
        from cymatix_context.okf import ingest_bundle

        result = ingest_bundle(sess._manager, root, bundle_id=args.bundle_id)
    except Exception as exc:
        err = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if args.json:
            output.print_json(err)
        else:
            output.eprint(err["error"])
        return output.EXIT_ERROR

    payload = {
        "ok": True,
        "bundle_id": result.bundle_id,
        "okf_version": result.okf_version,
        "digest": result.digest,
        "concepts_total": result.concepts_total,
        "concepts_ingested": result.concepts_ingested,
        "gene_ids": result.gene_ids,
        "links": {
            "captured": result.links_captured,
            "resolved": result.links_resolved,
            "dangling": result.links_dangling,
        },
        "deterministic_profile": bool(args.deterministic),
        "warnings": result.warnings,
        "skipped_files": result.skipped_files,
    }
    if args.json:
        output.print_json(payload)
    else:
        lines = [
            f"ingested OKF bundle '{result.bundle_id}'"
            + (f" (okf_version {result.okf_version})" if result.okf_version else ""),
            f"  concepts: {result.concepts_ingested}/{result.concepts_total}",
            f"  genes:    {len(result.gene_ids)}",
            f"  links:    {result.links_captured} "
            f"({result.links_resolved} resolved, {result.links_dangling} dangling)",
            f"  digest:   {result.digest}",
        ]
        if args.deterministic:
            lines.append("  profile:  deterministic-ingest (no float tensors; "
                         "backfill embeddings per host)")
        for w in result.warnings:
            lines.append(f"  warning:  {w}")
        for s in result.skipped_files:
            lines.append(f"  skipped:  {s}")
        output.print_lines(lines)
    return output.EXIT_OK


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.okf:
        return _run_okf(args)
    if args.bundle_id or args.deterministic:
        err = {"ok": False, "error": "--bundle-id/--deterministic require --okf"}
        if args.json:
            output.print_json(err)
        else:
            output.eprint(err["error"])
        return output.EXIT_ERROR

    root = Path(args.path)
    if not root.exists():
        err = {"ok": False, "error": f"path not found: {root}"}
        if args.json:
            output.print_json(err)
        else:
            output.eprint(err["error"])
        return output.EXIT_ERROR

    exts = args.ext or _DEFAULT_EXTENSIONS
    if args.no_default_excludes:
        exclude_dirs = set(args.exclude_dir or ())
    else:
        exclude_dirs = set(_DEFAULT_EXCLUDE_DIRS) | set(args.exclude_dir or ())
    files = _collect_files(root, args.recursive, exts, exclude_dirs)
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
            # #224: infer content_type from extension (code vs text) and record
            # the source path so retrieval can attribute + match gold by file.
            result = sess.ingest(
                content,
                content_type=_content_type_for(f),
                metadata={"path": str(f), "source_id": str(f)},
            )
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
