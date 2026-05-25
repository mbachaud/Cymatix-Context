"""Auto-subshard for issue #147.

The fixture builder's "one shard per profile root" granularity becomes
the long pole when one root dominates the corpus — the 500K
EnterpriseRAG-Bench attempt produced an 18 GB slack ``.db`` that
exceeded the OS file-cache headroom and crashed the dense backfill rate
from 27 g/s → 0.12 g/s. ``_decompose_oversized_root`` adds a sizing-
driven decomposition pass that splits oversized roots along their top-
level subdirectories.

Tests pin:

- under-threshold root → single shard, no decomposition
- oversized root → list of subshard ``(label, path)`` tuples
- flat (no-subdir) root that's oversized → fall back to single shard
- depth-2 recursion (oversized subdir inside oversized root)
- skip_dirs honored during decomposition
- subshard labels use ``__`` (parent__child) delimiter
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


# --- helpers ------------------------------------------------------------


def _populate_files(
    root: Path,
    sizes_kb_per_subdir: dict[str, int],
    *,
    file_size_bytes: int = 1024,
    files_per_subdir: int | None = None,
) -> None:
    """Make a synthetic corpus under `root`. Each subdir gets either
    `files_per_subdir` files of `file_size_bytes` each, or enough files
    to total ``sizes_kb_per_subdir[subdir] * 1024`` bytes."""
    root.mkdir(parents=True, exist_ok=True)
    for sub, kb in sizes_kb_per_subdir.items():
        sub_root = root / sub
        sub_root.mkdir(parents=True, exist_ok=True)
        if files_per_subdir is not None:
            n_files = files_per_subdir
        else:
            n_files = max(1, (kb * 1024) // file_size_bytes)
        for i in range(n_files):
            (sub_root / f"doc_{i:04d}.txt").write_bytes(b"x" * file_size_bytes)


# --- tests --------------------------------------------------------------


def test_decompose_small_root_returns_single_shard(tmp_path):
    """A root with file count + bytes under both thresholds should
    return a single (slug, root) tuple — no decomposition.
    """
    import build_fixture_matrix as bfm

    root = tmp_path / "smallproject"
    _populate_files(root, {"docs": 4}, file_size_bytes=512)  # well under any threshold

    result = bfm._decompose_oversized_root(
        str(root),
        skip_dirs=set(),
        extra_filename_filters=[],
        threshold_bytes=10_000_000,
        threshold_files=1_000,
    )
    assert len(result) == 1, f"expected single shard, got {len(result)}: {result}"
    slug, path = result[0]
    assert path == str(root)
    assert slug == "smallproject"


def test_decompose_oversized_root_walks_subdirs(tmp_path):
    """A root with file count above threshold should decompose into one
    shard per top-level subdirectory."""
    import build_fixture_matrix as bfm

    # Build a "bigproject" root with 3 subdirs, each over the per-subdir
    # file-count threshold (so each becomes its own shard, but none
    # individually triggers further recursion).
    root = tmp_path / "bigproject"
    _populate_files(
        root,
        {"alpha": 0, "beta": 0, "gamma": 0},
        files_per_subdir=30,
        file_size_bytes=128,
    )

    result = bfm._decompose_oversized_root(
        str(root),
        skip_dirs=set(),
        extra_filename_filters=[],
        threshold_bytes=10_000,    # tiny — forces root to decompose
        threshold_files=50,         # 3 subdirs * 30 = 90 > 50 → root over
    )
    # Each subdir has 30 files (< 50), so subshards do not recurse.
    assert len(result) == 3, f"expected 3 subshards, got {len(result)}: {result}"

    slugs = sorted(slug for slug, _ in result)
    assert slugs == ["bigproject__alpha", "bigproject__beta", "bigproject__gamma"]

    # Each path resolves into the right subdir
    by_slug = dict(result)
    assert by_slug["bigproject__alpha"] == str(root / "alpha")
    assert by_slug["bigproject__beta"] == str(root / "beta")


def test_decompose_flat_root_falls_back_to_single_shard(tmp_path):
    """An oversized root with no subdirs (all files at top level) should
    fall back to returning the single (slug, root) tuple — we can't
    decompose along subdirs that don't exist. A hash-based subshard
    mode could handle this later; for now it's a flat-layout fallback."""
    import build_fixture_matrix as bfm

    root = tmp_path / "flatdump"
    root.mkdir(parents=True, exist_ok=True)
    for i in range(200):
        (root / f"loose_{i:04d}.txt").write_bytes(b"x" * 512)

    result = bfm._decompose_oversized_root(
        str(root),
        skip_dirs=set(),
        extra_filename_filters=[],
        threshold_bytes=10_000,
        threshold_files=10,    # 200 files > 10 → over threshold
    )
    assert len(result) == 1, (
        f"flat root should fall back to single shard, got {result}"
    )
    assert result[0][1] == str(root)


def test_decompose_recurses_into_oversized_subdir(tmp_path):
    """If a subdir is itself oversized (and has further subdirs), the
    function should recurse one level deeper. Labels nest with ``__``.
    Depth-2 only — depth-3+ falls back to single shard for that branch.
    """
    import build_fixture_matrix as bfm

    root = tmp_path / "huge"
    root.mkdir(parents=True, exist_ok=True)
    # tiny: under threshold even at root level
    (root / "tiny").mkdir()
    for i in range(5):
        (root / "tiny" / f"a_{i}.txt").write_bytes(b"x" * 128)
    # giant: over threshold, but has subdirs we can split along
    (root / "giant").mkdir()
    for sub in ["one", "two"]:
        d = root / "giant" / sub
        d.mkdir(parents=True)
        for i in range(40):
            (d / f"b_{i}.txt").write_bytes(b"x" * 128)

    result = bfm._decompose_oversized_root(
        str(root),
        skip_dirs=set(),
        extra_filename_filters=[],
        threshold_bytes=10_000,
        threshold_files=30,
    )
    slugs = sorted(slug for slug, _ in result)
    # `tiny` stays as one shard at the parent's level.
    # `giant` is oversized and decomposes into giant__one / giant__two.
    assert "huge__tiny" in slugs, f"expected huge__tiny in {slugs}"
    assert "huge__giant__one" in slugs, f"expected huge__giant__one in {slugs}"
    assert "huge__giant__two" in slugs, f"expected huge__giant__two in {slugs}"


def test_decompose_respects_skip_dirs(tmp_path):
    """Subdirs in `skip_dirs` must not appear in the decomposed result
    — same semantics as the rest of the fixture builder."""
    import build_fixture_matrix as bfm

    root = tmp_path / "withjunk"
    _populate_files(
        root,
        {"keep": 0, "node_modules": 0, ".venv": 0},
        files_per_subdir=30,
        file_size_bytes=128,
    )

    result = bfm._decompose_oversized_root(
        str(root),
        skip_dirs={"node_modules", ".venv"},
        extra_filename_filters=[],
        threshold_bytes=10_000,
        threshold_files=20,
    )
    slugs = sorted(slug for slug, _ in result)
    assert all(
        "node_modules" not in s and "venv" not in s for s in slugs
    ), f"skip_dirs leaked into subshard labels: {slugs}"


def test_decompose_nonexistent_root_returns_empty(tmp_path):
    """Mirrors the fixture builder's silent-skip behaviour for missing
    roots in the profile config (`stats["missing_roots"].append(root)`).
    """
    import build_fixture_matrix as bfm

    result = bfm._decompose_oversized_root(
        str(tmp_path / "does_not_exist"),
        skip_dirs=set(),
        extra_filename_filters=[],
        threshold_bytes=10_000,
        threshold_files=1_000,
    )
    assert result == []
