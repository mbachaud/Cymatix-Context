#!/usr/bin/env python3
"""Idempotent codemod: cymatix_context -> cymatix_context.

Committed to the repo on purpose: the rename PR is rebased over inflight
work by re-running this script (see the Rebase Runbook in
docs/superpowers/plans/2026-07-20-cymatix-context-rename.md). Running it
twice is a no-op.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD_PKG, NEW_PKG = "cymatix_context", "cymatix_context"
# NEW_PKG first: after the move, rewrites target the new tree. The
# back-compat shim dir (a later re-created cymatix_context/) is deliberately
# NOT listed — its references to the old name are intentional.
CODE_DIRS = [NEW_PKG, "tests", "scripts", "benchmarks", "deploy"]
SKIP_PARTS = {".claude", "tools", "node_modules", ".venv", "__pycache__", ".git", "genomes"}


def move_package() -> None:
    old_dir = ROOT / OLD_PKG
    new_dir = ROOT / NEW_PKG
    if old_dir.is_dir() and not new_dir.exists():
        subprocess.run(["git", "mv", OLD_PKG, NEW_PKG], cwd=ROOT, check=True)
        print(f"moved {OLD_PKG}/ -> {NEW_PKG}/")


def rewrite_imports() -> int:
    pat = re.compile(rf"\b{OLD_PKG}\b")
    changed = 0
    for d in CODE_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for py in base.rglob("*.py"):
            if SKIP_PARTS & set(py.parts):
                continue
            text = py.read_text(encoding="utf-8")
            new = pat.sub(NEW_PKG, text)
            if new != text:
                py.write_text(new, encoding="utf-8")
                changed += 1
    return changed


if __name__ == "__main__":
    move_package()
    n = rewrite_imports()
    print(f"codemod complete: {n} files rewritten")
    sys.exit(0)
