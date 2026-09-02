"""
Release helper for the beta-branch flow described in docs/RELEASING.md.

Stdlib only, Windows-safe (preserves each file's newline style and never
prints anything the cp1252 console cannot show). It edits exactly three
files -- pyproject.toml, cymatix_context/__init__.py and CHANGELOG.md --
and never runs git commit, git tag or git push itself: every subcommand
prints the git / gh commands the operator runs next.

Subcommands:

    check                       pyproject version == __init__ version,
                                CHANGELOG has a non-empty "## Unreleased"
                                section, git working tree is clean
    pre --n N [--version X.Y.Z] bump both version files to X.Y.ZbN (the
                                X.Y.Z it previews: --version, else the next
                                patch after a released version, else the
                                base of the current X.Y.ZbM) and add a
                                pre-release note under "## Unreleased"
    final --version X.Y.Z       bump both version files, roll
                                "## Unreleased" into "## X.Y.Z (YYYY-MM-DD)"
                                and leave a fresh empty "## Unreleased"
                                heading above it
    tag-message --version X     print the annotated-tag message drafted
                                from that CHANGELOG section

Every editing subcommand is a dry run by default and prints a unified
diff; pass --write to apply. --repo-root points at a different checkout
(the tests use temp copies).

Usage:
    python scripts/release.py check
    python scripts/release.py pre --n 1            # dry run, prints the diff
    python scripts/release.py pre --n 1 --write
    python scripts/release.py final --version 0.9.2 --write
    python scripts/release.py tag-message --version 0.9.2 > tagmsg.txt
"""

from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = "pyproject.toml"
INIT = "cymatix_context/__init__.py"
CHANGELOG = "CHANGELOG.md"
EDITED_FILES = (PYPROJECT, INIT, CHANGELOG)

UNRELEASED_HEADING = "## Unreleased"

# X.Y.Z or X.Y.ZbN (PEP 440 beta pre-release; the only pre-release form the
# flow uses).
_VERSION_RE = re.compile(r"^(?P<base>\d+\.\d+\.\d+)(?:b(?P<beta>\d+))?$")
_PYPROJECT_LINE_RE = re.compile(r'^version = "(?P<v>[^"]+)"$', re.MULTILINE)
_INIT_LINE_RE = re.compile(r'^__version__ = "(?P<v>[^"]+)"$', re.MULTILINE)
# [ \t]* rather than \s*: a \s* would swallow the newline after the heading
# and shift every offset computed from m.end().
_HEADING_RE = re.compile(r"^## (?P<title>.+?)[ \t]*$", re.MULTILINE)


class ReleaseError(Exception):
    """A precondition the operator has to fix; reported without a traceback."""


# --------------------------------------------------------------------------
# Small file helpers (newline-preserving)
# --------------------------------------------------------------------------


def _read(path: Path) -> tuple[str, str]:
    """Return (text with \\n newlines, original newline string)."""
    raw = path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), newline


def _write(path: Path, text: str, newline: str) -> None:
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))


def _diff(rel: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
    )


# --------------------------------------------------------------------------
# Version parsing and rewriting
# --------------------------------------------------------------------------


def parse_version(text: str) -> tuple[str, int | None]:
    """Split "0.9.2b1" into ("0.9.2", 1); "0.9.2" into ("0.9.2", None)."""
    m = _VERSION_RE.match(text.strip())
    if not m:
        raise ReleaseError(
            f"version {text!r} is not X.Y.Z or X.Y.ZbN (the only forms the flow uses)"
        )
    beta = m.group("beta")
    return m.group("base"), (int(beta) if beta is not None else None)


def read_pyproject_version(text: str) -> str:
    m = _PYPROJECT_LINE_RE.search(text)
    if not m:
        raise ReleaseError(f'{PYPROJECT}: no top-level version = "..." line found')
    return m.group("v")


def read_init_version(text: str) -> str:
    m = _INIT_LINE_RE.search(text)
    if not m:
        raise ReleaseError(f'{INIT}: no __version__ = "..." line found')
    return m.group("v")


def set_pyproject_version(text: str, version: str) -> str:
    read_pyproject_version(text)
    return _PYPROJECT_LINE_RE.sub(f'version = "{version}"', text, count=1)


def set_init_version(text: str, version: str) -> str:
    read_init_version(text)
    return _INIT_LINE_RE.sub(f'__version__ = "{version}"', text, count=1)


# --------------------------------------------------------------------------
# CHANGELOG parsing and rewriting
# --------------------------------------------------------------------------


def _find_section(text: str, heading: str) -> tuple[int, int, int]:
    """Return (heading_start, body_start, section_end) for a "## " heading.

    section_end is the offset of the next "## " heading or len(text).
    """
    for m in _HEADING_RE.finditer(text):
        if m.group(0).rstrip() == heading:
            nxt = _HEADING_RE.search(text, m.end())
            end = nxt.start() if nxt else len(text)
            body_start = m.end()
            if body_start < len(text) and text[body_start] == "\n":
                body_start += 1
            return m.start(), body_start, end
    raise ReleaseError(f"{CHANGELOG}: heading {heading!r} not found")


def unreleased_body(text: str) -> str:
    _, body_start, end = _find_section(text, UNRELEASED_HEADING)
    return text[body_start:end]


def section_body(text: str, version: str) -> str:
    """The body of "## <version> (...)" (or "## <version> -- ..." legacy form)."""
    for m in _HEADING_RE.finditer(text):
        title = m.group("title")
        if title == version or title.startswith(version + " "):
            nxt = _HEADING_RE.search(text, m.end())
            end = nxt.start() if nxt else len(text)
            body_start = m.end()
            if body_start < len(text) and text[body_start] == "\n":
                body_start += 1
            return text[body_start:end]
    raise ReleaseError(f"{CHANGELOG}: no section for version {version}")


def insert_prerelease_note(text: str, tag: str, version: str, date: str) -> str:
    """Add a one-line pre-release note directly under "## Unreleased"."""
    _, body_start, _ = _find_section(text, UNRELEASED_HEADING)
    note = (
        f"_Pre-release `{tag}` tagged {date} from `beta` "
        f"(`pip install --pre cymatix-context=={version}`)._\n\n"
    )
    # Skip the blank line that separates the heading from the body so the
    # note sits first, then keep exactly one blank line before old content.
    rest = text[body_start:].lstrip("\n")
    return text[:body_start] + "\n" + note + rest


def roll_unreleased(text: str, version: str, date: str) -> str:
    """Rename "## Unreleased" to "## <version> (<date>)" and add a fresh one."""
    head_start, body_start, end = _find_section(text, UNRELEASED_HEADING)
    body = text[body_start:end]
    if not body.strip():
        raise ReleaseError(f"{CHANGELOG}: '## Unreleased' is empty; nothing to release")
    body = body.strip("\n") + "\n\n"
    new_section = f"## {version} ({date})\n\n{body}"
    return (
        text[:head_start]
        + f"{UNRELEASED_HEADING}\n\n"
        + new_section
        + text[end:]
    )


# --------------------------------------------------------------------------
# Git
# --------------------------------------------------------------------------


def git_dirty_paths(root: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise ReleaseError(f"git status failed: {proc.stderr.strip() or proc.returncode}")
    return [line for line in proc.stdout.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------


def _load_tree(root: Path) -> dict[str, tuple[str, str]]:
    files = {}
    for rel in EDITED_FILES:
        path = root / rel
        if not path.is_file():
            raise ReleaseError(f"missing {rel} under {root}")
        files[rel] = _read(path)
    return files


def cmd_check(root: Path, skip_git: bool) -> int:
    files = _load_tree(root)
    problems: list[str] = []

    pv = read_pyproject_version(files[PYPROJECT][0])
    iv = read_init_version(files[INIT][0])
    if pv != iv:
        problems.append(f"version mismatch: {PYPROJECT} says {pv}, {INIT} says {iv}")
    else:
        try:
            parse_version(pv)
        except ReleaseError as exc:
            problems.append(str(exc))

    try:
        body = unreleased_body(files[CHANGELOG][0])
    except ReleaseError as exc:
        problems.append(str(exc))
    else:
        if not body.strip():
            problems.append(f"{CHANGELOG}: '## Unreleased' has no content")

    if not skip_git:
        try:
            dirty = git_dirty_paths(root)
        except (ReleaseError, OSError) as exc:
            problems.append(f"git: {exc}")
        else:
            if dirty:
                problems.append("git working tree is not clean:\n  " + "\n  ".join(dirty))

    if problems:
        print("release check FAILED")
        for p in problems:
            print(f"- {p}")
        return 1
    print(f"release check OK: version {pv}, '## Unreleased' has content"
          + ("" if skip_git else ", working tree clean"))
    return 0


def _apply(root: Path, files: dict[str, tuple[str, str]], updates: dict[str, str], write: bool) -> None:
    diff_text = ""
    for rel, new_text in updates.items():
        old_text, _ = files[rel]
        if new_text != old_text:
            diff_text += _diff(rel, old_text, new_text)
    if not diff_text:
        print("nothing to change")
        return
    if write:
        for rel, new_text in updates.items():
            _, newline = files[rel]
            _write(root / rel, new_text, newline)
        print(f"wrote: {', '.join(rel for rel in updates)}")
    else:
        sys.stdout.write(diff_text)
        print("(dry run; pass --write to apply)")


def _print_next(lines: list[str]) -> None:
    print("")
    print("next (the script never runs these itself):")
    for line in lines:
        print(f"  {line}")


def next_prerelease_base(current: str, requested: str | None) -> str:
    """Pick the X.Y.Z a pre-release is a preview of.

    A pre-release X.Y.ZbN sorts BEFORE X.Y.Z, so it must preview a version
    that is not released yet. --version wins when given; otherwise a tree
    already on X.Y.ZbM stays on X.Y.Z, and a tree on a final X.Y.Z (the
    state right after a release) moves to the next patch number. The
    operator sees the result in the dry-run diff before --write.
    """
    cur_base, cur_beta = parse_version(current)
    if requested is not None:
        base, beta = parse_version(requested)
        if beta is not None:
            raise ReleaseError("pre --version takes the final X.Y.Z it previews, not X.Y.ZbN")
        if base == cur_base and cur_beta is None:
            raise ReleaseError(
                f"{base}bN would sort before the already-released {current}; "
                "pass the next version instead"
            )
        return base
    if cur_beta is not None:
        return cur_base
    major, minor, patch = (int(x) for x in cur_base.split("."))
    return f"{major}.{minor}.{patch + 1}"


def cmd_pre(root: Path, n: int, write: bool, date: str, version_base: str | None) -> int:
    if n < 1:
        raise ReleaseError("--n must be >= 1")
    files = _load_tree(root)
    current = read_pyproject_version(files[PYPROJECT][0])
    if current != read_init_version(files[INIT][0]):
        raise ReleaseError("version files disagree; run `release.py check` first")
    base = next_prerelease_base(current, version_base)
    version = f"{base}b{n}"
    tag = f"v{version}"
    if version == current:
        raise ReleaseError(f"tree is already at {current}; pick a higher --n")

    updates = {
        PYPROJECT: set_pyproject_version(files[PYPROJECT][0], version),
        INIT: set_init_version(files[INIT][0], version),
        CHANGELOG: insert_prerelease_note(files[CHANGELOG][0], tag, version, date),
    }
    print(f"pre-release: {current} -> {version} (tag {tag})")
    _apply(root, files, updates, write)
    _print_next([
        "python -m pytest tests/test_version.py -q",
        f"git add {PYPROJECT} {INIT} {CHANGELOG}",
        f'git commit -m "release: {tag} pre-release"',
        "git push origin HEAD:refs/heads/beta",
        f"python scripts/release.py tag-message --version {version} > tagmsg.txt",
        f"git tag -a {tag} -F tagmsg.txt",
        f"git push origin {tag}",
        f'gh release create {tag} --prerelease --title "{tag}" --notes-file tagmsg.txt',
        "(publish.yml fires on the published pre-release and uploads it to PyPI)",
    ])
    return 0


def cmd_final(root: Path, version: str, write: bool, date: str) -> int:
    base, beta = parse_version(version)
    if beta is not None:
        raise ReleaseError("final --version takes X.Y.Z; use `pre` for X.Y.ZbN")
    files = _load_tree(root)
    current = read_pyproject_version(files[PYPROJECT][0])
    if current != read_init_version(files[INIT][0]):
        raise ReleaseError("version files disagree; run `release.py check` first")
    tag = f"v{version}"

    updates = {
        PYPROJECT: set_pyproject_version(files[PYPROJECT][0], version),
        INIT: set_init_version(files[INIT][0], version),
        CHANGELOG: roll_unreleased(files[CHANGELOG][0], version, date),
    }
    print(f"final release: {current} -> {version} (tag {tag}, CHANGELOG section dated {date})")
    _apply(root, files, updates, write)
    _print_next([
        "python -m pytest tests/test_version.py -q",
        f"python scripts/release.py tag-message --version {version} > tagmsg.txt",
        f"git add {PYPROJECT} {INIT} {CHANGELOG}",
        f'git commit -m "release: {tag}"',
        f"git push origin HEAD:refs/heads/release/{tag}",
        f'gh pr create --base master --head release/{tag} --title "release: {tag}" --body-file tagmsg.txt',
        "(after the PR merges with a merge commit)",
        "git fetch origin && git checkout master && git pull --ff-only",
        f"git tag -a {tag} -F tagmsg.txt",
        f"git push origin {tag}",
        f'gh release create {tag} --title "{tag}" --notes-file tagmsg.txt',
        "(publish.yml fires on the published release and uploads it to PyPI)",
    ])
    return 0


def cmd_tag_message(root: Path, version: str) -> int:
    parse_version(version)
    text, _ = _read(root / CHANGELOG)
    try:
        body = section_body(text, version)
    except ReleaseError:
        # Pre-releases have no section of their own; they ship the current
        # Unreleased content.
        _, beta = parse_version(version)
        if beta is None:
            raise
        body = unreleased_body(text)
    body = body.strip("\n")
    if not body:
        raise ReleaseError(f"{CHANGELOG}: section for {version} is empty")
    sys.stdout.write(f"v{version}\n\n{body}\n")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _today() -> str:
    return _dt.date.today().isoformat()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="release.py",
        description="Release helper for the beta-branch flow (see docs/RELEASING.md).",
    )
    ap.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="checkout to operate on (default: the repo this script lives in)",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="verify versions, CHANGELOG and a clean tree")
    p_check.add_argument("--skip-git", action="store_true", help="do not require a clean git tree")

    def add_edit_flags(p: argparse.ArgumentParser) -> None:
        g = p.add_mutually_exclusive_group()
        g.add_argument("--dry-run", action="store_true", default=True,
                       help="print a unified diff and change nothing (default)")
        g.add_argument("--write", action="store_true", help="apply the edits")
        p.add_argument("--date", default=None,
                       help="YYYY-MM-DD to stamp instead of today (tests, backdated cuts)")

    p_pre = sub.add_parser("pre", help="bump to X.Y.ZbN and note the pre-release")
    p_pre.add_argument("--n", type=int, required=True, help="beta number N in X.Y.ZbN")
    p_pre.add_argument(
        "--version",
        default=None,
        help="the final X.Y.Z this pre-release previews (default: next patch after a "
             "released version, or the base of the current X.Y.ZbM)",
    )
    add_edit_flags(p_pre)

    p_final = sub.add_parser("final", help="bump to X.Y.Z and roll the CHANGELOG")
    p_final.add_argument("--version", required=True, help="the release version X.Y.Z")
    add_edit_flags(p_final)

    p_tag = sub.add_parser("tag-message", help="print the annotated-tag message for a version")
    p_tag.add_argument("--version", required=True)
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    root = Path(args.repo_root).resolve()
    try:
        if args.command == "check":
            return cmd_check(root, skip_git=args.skip_git)
        date = getattr(args, "date", None) or _today()
        if args.command == "pre":
            return cmd_pre(root, n=args.n, write=args.write, date=date, version_base=args.version)
        if args.command == "final":
            return cmd_final(root, version=args.version, write=args.write, date=date)
        if args.command == "tag-message":
            return cmd_tag_message(root, version=args.version)
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
