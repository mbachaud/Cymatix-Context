"""Every ``cymatix_context`` import in the live scripts and harnesses resolves.

Scripts under ``scripts/``, ``benchmarks/``, ``examples/`` and ``training/``
are not imported by the test suite, so a package move (the PR #90 split, or a
later rename) can leave them failing with ``ModuleNotFoundError`` for months.
PR #540 found 13 such imports. This test reads each script's AST and checks
only its ``cymatix_context`` imports, without running the script, so it needs
none of the scripts' own optional dependencies.

``archive/`` is excluded: retired scripts are kept as history, not maintained.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_ROOTS = ("scripts", "benchmarks", "examples", "training")
PKG = "cymatix_context"


def _package_imports(path: Path) -> list[tuple[str, str | None, int]]:
    """Return (module, name-or-None, lineno) for each absolute cymatix_context import."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []  # not this test's concern; the lint job covers syntax
    found: list[tuple[str, str | None, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] == PKG:
                for alias in node.names:
                    found.append((node.module, None if alias.name == "*" else alias.name, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == PKG:
                    found.append((alias.name, None, node.lineno))
    return found


def _resolves(module: str, name: str | None) -> str | None:
    """Return None if the import resolves, else a short reason."""
    try:
        mod = importlib.import_module(module)
    except ImportError as exc:
        # A missing third-party extra (torch, spacy, ...) is not a stale path.
        missing = getattr(exc, "name", None) or ""
        if missing and not missing.startswith(PKG):
            return None
        return f"{type(exc).__name__}: {exc}"
    if name is None or hasattr(mod, name):
        return None
    try:
        importlib.import_module(f"{module}.{name}")
        return None
    except ImportError:
        return f"{module} has no attribute or submodule {name!r}"


def _live_scripts() -> list[Path]:
    files: list[Path] = []
    for root in LIVE_ROOTS:
        base = REPO_ROOT / root
        if base.is_dir():
            files.extend(sorted(base.rglob("*.py")))
    return files


def test_live_scripts_have_package_imports_to_check():
    total = sum(len(_package_imports(p)) for p in _live_scripts())
    assert total > 50, f"expected many cymatix_context imports in live scripts, found {total}"


def test_live_script_package_imports_resolve():
    failures = []
    for path in _live_scripts():
        for module, name, lineno in _package_imports(path):
            reason = _resolves(module, name)
            if reason:
                rel = path.relative_to(REPO_ROOT).as_posix()
                target = f"{module}.{name}" if name else module
                failures.append(f"{rel}:{lineno}: {target} -> {reason}")
    if failures:
        pytest.fail(
            "Live scripts import cymatix_context paths that no longer exist. "
            "Repoint them, or move the script to archive/ if it is retired:\n  "
            + "\n  ".join(failures)
        )
