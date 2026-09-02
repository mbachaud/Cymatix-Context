"""
Mirror this repo's wiki/ directory to the GitHub wiki git repo.

GitHub wikis are themselves a git repo (a repo named `<repo>.wiki.git`
alongside the main repo). This script clones that repo to a temp dir,
replaces its tracked *.md files and assets/ directory with wiki/'s
content, commits, and pushes -- so wiki/ in this repo stays the single
source of truth and the GitHub-rendered wiki is just a mirror of it.

Usage:
    python scripts/sync_github_wiki.py [--remote URL] [--wiki-dir DIR] [--dry-run]

Network access required (git clone/push); there is no automated test for
this script for that reason -- see tests/test_build_wiki_site.py for the
tested wiki -> site render path instead.

If the wiki hasn't been initialized yet, GitHub returns 404/"repository
not found" on clone -- GitHub only creates the wiki git remote after the
first page is saved through the web UI. This script detects that case and
prints a clear message pointing at the wiki UI instead of a raw git error.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_REMOTE = "https://github.com/mbachaud/Cymatix-Context.wiki.git"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WIKI_DIR = REPO_ROOT / "wiki"

_WIKI_NOT_INITIALIZED_MSG = (
    "error: the GitHub wiki repo does not exist yet.\n"
    "GitHub only creates the wiki's git remote after the first page is saved\n"
    "through the web UI at https://github.com/mbachaud/Cymatix-Context/wiki --\n"
    "create one page there, then re-run this script."
)


def _run(cmd: list[str], cwd: Path | None = None, dry_run: bool = False) -> None:
    printable = " ".join(cmd)
    if dry_run:
        suffix = f" (cwd={cwd})" if cwd else ""
        print(f"[dry-run] would run: {printable}{suffix}")
        return
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def _looks_like_missing_repo(stderr: str) -> bool:
    lowered = stderr.lower()
    return "not found" in lowered or "repository not found" in lowered or "404" in lowered


def sync(remote: str = DEFAULT_REMOTE, wiki_dir: Path = DEFAULT_WIKI_DIR, dry_run: bool = False) -> int:
    """Clone `remote`, replace its *.md + assets/ with `wiki_dir`'s, commit, push.

    Returns a process-style exit code (0 success, 1 failure) rather than
    raising, so main() can report a clean message either way.
    """
    wiki_dir = Path(wiki_dir)
    if not wiki_dir.is_dir():
        print(f"error: wiki source directory not found: {wiki_dir}", file=sys.stderr)
        return 1

    if dry_run:
        print(f"[dry-run] would clone {remote} to a temp directory")
        print(f"[dry-run] would replace its *.md files and assets/ with the contents of {wiki_dir}")
        _run(["git", "add", "-A"], dry_run=True)
        print('[dry-run] would commit as "Sync wiki from repo wiki/" (if there are changes)')
        _run(["git", "push"], dry_run=True)
        return 0

    tmp_dir = Path(tempfile.mkdtemp(prefix="cymatix-wiki-sync-"))
    clone_dir = tmp_dir / "wiki-clone"
    try:
        clone = subprocess.run(
            ["git", "clone", remote, str(clone_dir)],
            capture_output=True,
            text=True,
        )
        if clone.returncode != 0:
            stderr = clone.stderr or ""
            if _looks_like_missing_repo(stderr):
                print(_WIKI_NOT_INITIALIZED_MSG, file=sys.stderr)
            else:
                print(f"error: git clone failed:\n{stderr}", file=sys.stderr)
            return 1

        for md in clone_dir.glob("*.md"):
            md.unlink()
        clone_assets = clone_dir / "assets"
        if clone_assets.exists():
            shutil.rmtree(clone_assets)

        for md in wiki_dir.glob("*.md"):
            shutil.copy2(md, clone_dir / md.name)
        src_assets = wiki_dir / "assets"
        if src_assets.exists():
            shutil.copytree(src_assets, clone_dir / "assets")

        subprocess.run(["git", "add", "-A"], cwd=str(clone_dir), check=True)

        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(clone_dir), capture_output=True, text=True
        )
        if not status.stdout.strip():
            print("wiki content already up to date, nothing to sync")
            return 0

        subprocess.run(
            ["git", "commit", "-m", "Sync wiki from repo wiki/"], cwd=str(clone_dir), check=True
        )
        subprocess.run(["git", "push"], cwd=str(clone_dir), check=True)
        print(f"synced {wiki_dir} -> {remote}")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--remote", default=DEFAULT_REMOTE, help="wiki git remote URL")
    ap.add_argument(
        "--wiki-dir",
        default=str(DEFAULT_WIKI_DIR),
        help=f"source wiki/ directory (default: {DEFAULT_WIKI_DIR})",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="print what would happen without touching git"
    )
    args = ap.parse_args(argv)
    return sync(remote=args.remote, wiki_dir=Path(args.wiki_dir), dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
