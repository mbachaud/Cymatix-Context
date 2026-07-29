"""#323: `cymatix ingest <dir> --recursive` must not walk vendored trees.

The bug: `_collect_files` filtered on extension only, so a recursive ingest of
a normal Python project indexed its own virtualenv. Measured on a real run,
5,331 of the first 5,337 files ingested were `.venv/Lib/site-packages`.

These tests pin the walk, not the ingest — the walk is where the defect lived.
"""

from __future__ import annotations

from pathlib import Path

from cymatix_context.cli.cmd_ingest import (
    _DEFAULT_EXCLUDE_DIRS,
    _collect_files,
)

EXTS = (".py", ".md")


def _mk(root: Path, rel: str, body: str = "x = 1\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _project(tmp_path: Path) -> Path:
    """A project laid out the way the bug was found: real code beside a venv."""
    root = tmp_path / "proj"
    _mk(root, "main.py")
    _mk(root, "README.md", "# readme\n")
    _mk(root, "pkg/core.py")
    # the trees that must never be walked
    _mk(root, ".venv/Lib/site-packages/requests/api.py")
    _mk(root, ".venv/Lib/site-packages/numpy/core.py")
    _mk(root, "node_modules/left-pad/index.js")
    _mk(root, "node_modules/left-pad/readme.md")
    _mk(root, "__pycache__/main.cpython-312.py")
    _mk(root, "dist/bundle.py")
    _mk(root, "build/lib/thing.py")
    _mk(root, ".git/hooks/pre-commit.py")
    _mk(root, ".mypy_cache/3.12/main.py")
    return root


def test_recursive_walk_skips_vendored_and_generated_trees(tmp_path):
    root = _project(tmp_path)
    found = {p.relative_to(root).as_posix()
             for p in _collect_files(root, recursive=True, exts=EXTS,
                                     exclude_dirs=_DEFAULT_EXCLUDE_DIRS)}
    assert found == {"main.py", "README.md", "pkg/core.py"}


def test_no_site_packages_leaks_under_any_default(tmp_path):
    """The specific regression: not one site-packages file may survive."""
    root = _project(tmp_path)
    files = _collect_files(root, recursive=True, exts=EXTS,
                           exclude_dirs=_DEFAULT_EXCLUDE_DIRS)
    leaked = [p for p in files
              if "site-packages" in p.as_posix() or ".venv" in p.as_posix()]
    assert leaked == [], f"virtualenv files leaked into the ingest set: {leaked}"


def test_extra_exclude_dir_is_additive(tmp_path):
    root = _project(tmp_path)
    _mk(root, "fixtures/big.py")
    exclude = set(_DEFAULT_EXCLUDE_DIRS) | {"fixtures"}
    found = {p.relative_to(root).as_posix()
             for p in _collect_files(root, recursive=True, exts=EXTS,
                                     exclude_dirs=exclude)}
    assert "fixtures/big.py" not in found
    # the defaults still apply alongside the extra one
    assert not any(f.startswith(".venv") for f in found)
    assert found == {"main.py", "README.md", "pkg/core.py"}


def test_opting_out_walks_everything(tmp_path):
    """--no-default-excludes is an escape hatch, so it must actually escape."""
    root = _project(tmp_path)
    found = {p.relative_to(root).as_posix()
             for p in _collect_files(root, recursive=True, exts=EXTS,
                                     exclude_dirs=set())}
    assert ".venv/Lib/site-packages/requests/api.py" in found
    assert "dist/bundle.py" in found


def test_non_recursive_is_unchanged(tmp_path):
    """Top-level-only ingest never descended anyway; keep it that way."""
    root = _project(tmp_path)
    found = {p.relative_to(root).as_posix()
             for p in _collect_files(root, recursive=False, exts=EXTS,
                                     exclude_dirs=_DEFAULT_EXCLUDE_DIRS)}
    assert found == {"main.py", "README.md"}


def test_single_file_target_still_honors_extension_filter(tmp_path):
    """A file argument bypasses the walk entirely — pre-existing contract."""
    root = tmp_path / "proj"
    py = _mk(root, "main.py")
    binary = _mk(root, "thing.exe", "MZ\n")
    assert _collect_files(py, recursive=True, exts=EXTS,
                          exclude_dirs=_DEFAULT_EXCLUDE_DIRS) == [py]
    assert _collect_files(binary, recursive=True, exts=EXTS,
                          exclude_dirs=_DEFAULT_EXCLUDE_DIRS) == []


def test_a_file_inside_an_excluded_dir_can_still_be_named_directly(tmp_path):
    """Exclusions govern the walk, not an explicit target. Someone pointing at
    a vendored file on purpose gets it."""
    root = _project(tmp_path)
    target = root / ".venv/Lib/site-packages/requests/api.py"
    assert _collect_files(target, recursive=True, exts=EXTS,
                          exclude_dirs=_DEFAULT_EXCLUDE_DIRS) == [target]


def test_egg_info_dirs_are_skipped_by_suffix(tmp_path):
    root = _project(tmp_path)
    _mk(root, "mypkg.egg-info/PKG-INFO.py")
    found = {p.relative_to(root).as_posix()
             for p in _collect_files(root, recursive=True, exts=EXTS,
                                     exclude_dirs=_DEFAULT_EXCLUDE_DIRS)}
    assert not any("egg-info" in f for f in found)


def test_exclusion_is_case_insensitive(tmp_path):
    """Windows and macOS default to case-insensitive filesystems, so a
    `.VENV` or `Node_Modules` must be caught too."""
    root = tmp_path / "proj"
    _mk(root, "main.py")
    _mk(root, ".VENV/Lib/site-packages/pkg.py")
    _mk(root, "Node_Modules/thing/index.py")
    found = {p.relative_to(root).as_posix()
             for p in _collect_files(root, recursive=True, exts=EXTS,
                                     exclude_dirs=_DEFAULT_EXCLUDE_DIRS)}
    assert found == {"main.py"}


def test_walk_is_sorted_and_deterministic(tmp_path):
    root = _project(tmp_path)
    a = _collect_files(root, recursive=True, exts=EXTS,
                       exclude_dirs=_DEFAULT_EXCLUDE_DIRS)
    b = _collect_files(root, recursive=True, exts=EXTS,
                       exclude_dirs=_DEFAULT_EXCLUDE_DIRS)
    assert a == b == sorted(a)
