"""build_dogfood_genome.py — rebuild the dogfood bed from the four owned repos.

The dogfood bed is the corpus cymatix retrieves against when we eat our own
cooking: four repos we own, all of whose files we are free to redistribute
(three are public, MaxExpressKit is ours). That redistributability is the
point — it is what lets the matching gold pack ship to a collaborator, which
the previous needle set could not do (51 of its 64 gold files lived in
private sibling repos).

**Curated scope, deliberately.** The previous beds were invalidated twice by
contamination the ingest walk let in: echo genes (the store's own bench
output re-ingested as retrievable content) and worktree duplicates (the same
file ingested once per git worktree). ``EXCLUDE_DIRS`` below is the fix, and
every entry earns its place:

* ``benchmarks/results``, ``docs/research/data``, ``docs/benchmarks/data`` —
  frozen measurement payloads. Re-ingesting a result JSON makes the bed
  retrieve its own scores, which is the echo-gene defect.
* ``.worktrees``, ``_worktrees``, ``.clone`` — the worktree-dupe source.
* ``genomes``, ``.venv``, ``node_modules``, ``__pycache__``, ``.git``,
  ``dist``, ``build``, ``site-packages`` — binaries, vendored code, caches.
* ``docs/archive`` — superseded docs. They read as authoritative but are not,
  so they generate confident wrong answers.

Usage::

    python scripts/build_dogfood_genome.py                 # build
    python scripts/build_dogfood_genome.py --dry-run       # count only
    python scripts/build_dogfood_genome.py --out PATH      # alternate target

The build is destructive on its target: it removes the existing genome (and
its -wal/-shm sidecars) so the result is a function of the inputs alone and
not of whatever the file happened to contain before. Pass ``--keep`` to
ingest into an existing store instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECTS_ROOT = Path(os.environ.get("CYMATIX_PROJECTS_ROOT", "F:/Projects"))

# The four owned repos, in ingest order. ``label`` is the ``<repo>/`` prefix
# that gold_source entries and source_id paths carry.
REPOS: list[dict[str, str]] = [
    {"label": "cymatix-context", "dir": "cymatix-context"},
    {"label": "driftwatch", "dir": "driftwatch"},
    {"label": "scorerift", "dir": "scorerift"},
    {"label": "MaxExpressKit", "dir": "MaxExpressKit"},
]

EXTENSIONS = {
    ".py", ".md", ".rst", ".txt", ".toml", ".yml", ".yaml", ".json",
    ".ts", ".tsx", ".js", ".jsx", ".sh", ".ps1",
}

EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", "site-packages",
    ".worktrees", "_worktrees", ".clone", "genomes", ".idea", ".vscode",
    "htmlcov", ".tox", "egg-info", ".cache",
}

# Path fragments (posix, relative to the repo root) whose subtrees are dropped.
EXCLUDE_PREFIXES = (
    "benchmarks/results",
    "benchmarks/logs",
    "docs/research/data",
    "docs/benchmarks/data",
    "docs/archive",
    "overnight_logs",
    "cwola_export",
    # Vendored third-party benchmark corpora. aider_polyglot is a downloaded
    # exercism cache (1,240 files) and contextbench/results is scored output;
    # together they were 63% of the cymatix-context walk and would have made
    # the dogfood bed mostly other people's fixtures.
    "benchmarks/aider_polyglot",
    "benchmarks/contextbench",
    # Synthetic test scaffolding (calculator.py, essay_caching.txt, ...) —
    # authored to exercise the chunker, not to be retrieved as knowledge.
    "tests/fixtures",
)

# Individual files that are pure noise or enormous generated artifacts.
EXCLUDE_NAMES = {"package-lock.json", "poetry.lock", "uv.lock", "yarn.lock"}

MAX_BYTES = 1_000_000  # skip anything above 1 MB — generated, not authored


def _excluded(rel_posix: str) -> bool:
    if any(rel_posix.startswith(p) for p in EXCLUDE_PREFIXES):
        return True
    return Path(rel_posix).name in EXCLUDE_NAMES


def collect(repo_dir: Path) -> list[Path]:
    """Return the curated ingest set for one repo, sorted for determinism."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo_dir):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in EXCLUDE_DIRS and not d.endswith(".egg-info")
        )
        for fn in sorted(filenames):
            f = Path(dirpath) / fn
            if f.suffix.lower() not in EXTENSIONS:
                continue
            rel = f.relative_to(repo_dir).as_posix()
            if _excluded(rel):
                continue
            try:
                if f.stat().st_size > MAX_BYTES:
                    continue
            except OSError:
                continue
            out.append(f)
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="genomes/dogfood/genome.db",
                    help="target genome path (default: genomes/dogfood/genome.db)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the file counts and exit without ingesting")
    ap.add_argument("--keep", action="store_true",
                    help="ingest into the existing store instead of rebuilding it")
    ap.add_argument("--manifest", default="benchmarks/dogfood/dogfood_manifest.json",
                    help="where to write the build manifest")
    args = ap.parse_args()

    target = (REPO_ROOT / args.out).resolve()

    plan: list[tuple[dict, Path, list[Path]]] = []
    missing: list[str] = []
    for spec in REPOS:
        rd = PROJECTS_ROOT / spec["dir"]
        if not rd.is_dir():
            missing.append(str(rd))
            continue
        plan.append((spec, rd, collect(rd)))

    if missing:
        print("ERROR: missing repo(s):", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        return 2

    total = sum(len(files) for _, _, files in plan)
    print(f"Dogfood build plan  ->  {target}")
    print(f"  projects root: {PROJECTS_ROOT}")
    for spec, rd, files in plan:
        print(f"  {len(files):5d}  {spec['label']:18s}  {rd}")
    print(f"  {total:5d}  TOTAL")

    if args.dry_run:
        return 0

    if not args.keep:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(target) + suffix)
            if p.exists():
                p.unlink()
                print(f"  removed {p.name}")
    target.parent.mkdir(parents=True, exist_ok=True)

    # Ingest the curated list file-by-file through the session API.
    #
    # NOT `cymatix ingest <repo> --recursive`: that re-walks the tree itself via
    # ``_collect_files``, which is a bare ``rglob("*")`` with no directory
    # exclusions. Handing it a repo root ingests `.venv/Lib/site-packages`,
    # `node_modules` and every other vendored tree, and the curation computed
    # above is silently discarded. Measured on the first attempt: 5,331 of the
    # 5,337 files it had ingested were site-packages.
    os.environ["CYMATIX_GENOME_PATH"] = str(target)
    os.environ["CYMATIX_DISABLE_LEARN"] = "1"

    sys.path.insert(0, str(REPO_ROOT))
    from cymatix_context.api import open_session  # noqa: E402
    from cymatix_context.cli.cmd_ingest import _content_type_for  # noqa: E402
    from cymatix_context.config import load_config  # noqa: E402

    cfg = load_config()
    cfg.genome.path = str(target)

    started = time.time()
    per_repo: list[dict] = []
    sess = open_session(config=cfg)

    for spec, rd, files in plan:
        t0 = time.time()
        print(f"\n=== ingesting {spec['label']} ({len(files)} files) ===", flush=True)
        done = 0
        failed: list[str] = []
        for f in files:
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                sess.ingest(
                    content,
                    content_type=_content_type_for(f),
                    metadata={"path": str(f), "source_id": str(f)},
                )
                done += 1
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad file must not abort
                failed.append(f"{f}: {type(exc).__name__}: {exc}")
            if done % 50 == 0:
                rate = done / max(time.time() - t0, 1e-6)
                print(f"    {done}/{len(files)}  ({rate*60:.1f} files/min)", flush=True)
        dt = time.time() - t0
        print(f"    done {done}/{len(files)} in {dt:.0f}s"
              f"{f'  ({len(failed)} failed)' if failed else ''}")
        for msg in failed[:5]:
            print(f"      ! {msg}", file=sys.stderr)
        per_repo.append({
            "label": spec["label"],
            "dir": str(rd),
            "files_planned": len(files),
            "files_ingested": done,
            "files_failed": len(failed),
            "seconds": round(dt, 1),
        })

    elapsed = time.time() - started

    import sqlite3
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    genes = conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
    conn.close()

    manifest = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "target": str(target),
        "projects_root": str(PROJECTS_ROOT),
        "genes": genes,
        "files_planned_total": total,
        "seconds_total": round(elapsed, 1),
        "repos": per_repo,
        "curation": {
            "extensions": sorted(EXTENSIONS),
            "exclude_dirs": sorted(EXCLUDE_DIRS),
            "exclude_prefixes": list(EXCLUDE_PREFIXES),
            "exclude_names": sorted(EXCLUDE_NAMES),
            "max_bytes": MAX_BYTES,
        },
        "genome_sha256": sha256_file(target),
    }
    mpath = REPO_ROOT / args.manifest
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"\nBuilt {genes:,} genes in {elapsed:.0f}s")
    print(f"  genome:   {target}")
    print(f"  sha256:   {manifest['genome_sha256']}")
    print(f"  manifest: {mpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
