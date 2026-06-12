"""Sharder catch-all for root-level files + builder coverage assertion (#213).

Measured by an external collaborator on the 100-shard v2 Onyx corpus
(cc-exchange embedding-upgrade turn 0024, L1a): four ``slack/<epoch>-*.json``
files living DIRECTLY at the slack source root (no channel subfolder) were
never ingested into ANY shard — 4/84 never-surfaced golds missing, 0
slack-root docs indexed anywhere, a +3.2pp ceiling @200_lt on his 125-query
set. Structural cause: ``_decompose_oversized_root`` (the #147
auto-subshard) splits an oversized root along its top-level subdirectories
and silently DROPS files that live directly at the root — they match no
sub-shard prefix.

Tests pin:

- the failing-on-master repro: root-level files map to a shard after the
  fix (a dedicated ``<root-slug>__root`` catch-all entry)
- decompose-with-no-subdirs (flat root) output unchanged
- decompose with subdirs but no root-level files: no catch-all emitted
  (#147 output unchanged)
- ``__root`` shard naming/slug is stable + deterministic, with a
  collision guard for a genuine subdir named ``root``
- the catch-all task's walk picks up ONLY root-level files (no
  duplication of subdir shards)
- the builder coverage assertion: passes on a complete mapping, raises
  with the orphan list on a synthetic gap, and honours the
  ``HELIX_BFM_COVERAGE_CHECK=0`` kill-switch
- ``build_profile_sharded`` wires the catch-all task end-to-end
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

# Make scripts/ importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_fixture_matrix as bfm


# --- helpers ------------------------------------------------------------

# MIN_FILE_SIZE is 50 bytes; keep synthetic files comfortably eligible.
_BODY = b"x" * 256


def _make_slack_like_root(tmp_path: Path) -> Path:
    """Mirror the measured corpus shape: a ``slack`` root with two channel
    subfolders plus two ``<epoch>-*.json`` files DIRECTLY at the root."""
    root = tmp_path / "slack"
    for chan in ("eng-sre", "aditya-rao"):
        d = root / chan
        d.mkdir(parents=True)
        for i in range(30):
            (d / f"17183{i:03d}-msg.json").write_bytes(_BODY)
    # The orphans from issue #213: channel-less root-level exports.
    (root / "1718323200-export.json").write_bytes(_BODY)
    (root / "1718409600-export.json").write_bytes(_BODY)
    return root


def _eligible_files_under(root: Path) -> list[str]:
    """Every file in the synthetic trees is eligible (ext + size)."""
    return [
        os.path.join(dirpath, f)
        for dirpath, _dirs, files in os.walk(root)
        for f in files
    ]


def _covered_by_entries(fpath: str, entries: list[tuple[str, str]]) -> bool:
    """True iff ``fpath`` falls under at least one decomposed shard root."""
    f = os.path.normpath(fpath)
    for _slug, p in entries:
        p = os.path.normpath(p)
        if f == p or f.startswith(p + os.sep):
            return True
    return False


def _tasks_from_entries(
    entries: list[tuple[str, str]], skip_dirs: set[str],
) -> list[dict]:
    """Minimal task dicts the way ``build_profile_sharded`` constructs
    them: catch-all entries (a path that is a strict ancestor of another
    entry's path) are marked ``rootfiles_only`` with an extended
    skip-set so their walk stays at the top level."""
    catchalls = bfm._catchall_indices(entries)
    tasks = []
    for i, (label, root) in enumerate(entries):
        task = {"label": label, "root": root, "skip_dirs": skip_dirs}
        if i in catchalls:
            task["rootfiles_only"] = True
            task["skip_dirs"] = bfm._rootfiles_only_skip_dirs(root, skip_dirs)
        tasks.append(task)
    return tasks


_LOW_THRESHOLDS = dict(threshold_bytes=10_000, threshold_files=10)


# --- the #213 repro (FAILS on master) -----------------------------------


def test_root_level_files_map_to_a_shard_after_decompose(tmp_path):
    """THE bug: decompose an oversized root that has both subdirs and
    root-level files; every eligible file must map to >= 1 shard entry.

    On master the decomposition returns only the per-subdir entries, so
    the two slack-root ``<epoch>-*.json`` files match no shard prefix —
    this assertion fails there (0 slack-root docs indexed anywhere).
    """
    root = _make_slack_like_root(tmp_path)

    entries = bfm._decompose_oversized_root(
        str(root), skip_dirs=set(), extra_filename_filters=[],
        **_LOW_THRESHOLDS,
    )

    orphans = [
        f for f in _eligible_files_under(root)
        if not _covered_by_entries(f, entries)
    ]
    assert not orphans, (
        f"{len(orphans)} eligible file(s) fell through the decomposition "
        f"and map to no shard: {orphans}"
    )

    # The catch-all self-identifies: a dedicated ``<root-slug>__root``
    # entry whose path is the root itself.
    by_slug = dict(entries)
    assert "slack__root" in by_slug, f"expected slack__root in {sorted(by_slug)}"
    assert by_slug["slack__root"] == str(root)
    # Channel subshards are still present and unchanged.
    assert by_slug["slack__eng-sre"] == str(root / "eng-sre")
    assert by_slug["slack__aditya-rao"] == str(root / "aditya-rao")


# --- no-regression pins on #147 behaviour --------------------------------


def test_decompose_flat_root_unchanged(tmp_path):
    """An oversized root with NO subdirs still falls back to a single
    ``(slug, root)`` shard — no ``__root`` entry is invented for it."""
    root = tmp_path / "flatdump"
    root.mkdir()
    for i in range(40):
        (root / f"loose_{i:03d}.txt").write_bytes(_BODY)

    entries = bfm._decompose_oversized_root(
        str(root), skip_dirs=set(), extra_filename_filters=[],
        **_LOW_THRESHOLDS,
    )
    assert entries == [("flatdump", str(root))]


def test_decompose_without_rootlevel_files_emits_no_catchall(tmp_path):
    """Subdirs only, nothing directly at the root: the #147 output is
    byte-for-byte what it was before — no ``__root`` entry."""
    root = tmp_path / "bigproject"
    for sub in ("alpha", "beta"):
        d = root / sub
        d.mkdir(parents=True)
        for i in range(30):
            (d / f"doc_{i:03d}.txt").write_bytes(_BODY)

    entries = bfm._decompose_oversized_root(
        str(root), skip_dirs=set(), extra_filename_filters=[],
        **_LOW_THRESHOLDS,
    )
    assert sorted(s for s, _ in entries) == [
        "bigproject__alpha", "bigproject__beta",
    ]


# --- __root naming stability ---------------------------------------------


def test_root_catchall_slug_naming_stable(tmp_path):
    """The catch-all slug is ``<root-slug>__root``, deterministic across
    calls, and collision-guarded against a genuine subdir named ``root``."""
    root = tmp_path / "Slack Export-v2"
    (root / "general").mkdir(parents=True)
    for i in range(30):
        (root / "general" / f"m_{i:03d}.json").write_bytes(_BODY)
    (root / "1718323200-export.json").write_bytes(_BODY)

    kwargs = dict(skip_dirs=set(), extra_filename_filters=[], **_LOW_THRESHOLDS)
    first = bfm._decompose_oversized_root(str(root), **kwargs)
    second = bfm._decompose_oversized_root(str(root), **kwargs)
    assert first == second, "decomposition must be deterministic"
    by_slug = dict(first)
    assert by_slug["slack-export-v2__root"] == str(root)

    # Collision guard: a genuine subdir literally named ``root`` already
    # owns ``<parent>__root``; the catch-all must take a distinct,
    # deterministic slug instead of clobbering it.
    (root / "root").mkdir()
    for i in range(30):
        (root / "root" / f"r_{i:03d}.json").write_bytes(_BODY)
    entries = bfm._decompose_oversized_root(str(root), **kwargs)
    slugs = [s for s, _ in entries]
    assert len(slugs) == len(set(slugs)), f"duplicate shard labels: {slugs}"
    by_slug = dict(entries)
    assert by_slug["slack-export-v2__root"] == str(root / "root")
    catchall = [
        s for s, p in entries
        if p == str(root) and s.startswith("slack-export-v2__root")
    ]
    assert catchall, f"catch-all lost in collision case: {entries}"


# --- the catch-all task walks ONLY root-level files -----------------------


def test_rootfiles_only_walk_excludes_subdirs(tmp_path):
    """The ``__root`` shard must not re-ingest the subdir shards' files:
    its task walks the root with every immediate subdir skipped."""
    root = _make_slack_like_root(tmp_path)

    task_skip = bfm._rootfiles_only_skip_dirs(str(root), {"node_modules"})
    assert {"eng-sre", "aditya-rao", "node_modules"} <= task_skip

    stats = {"missing_roots": [], "skipped": 0}
    files = bfm._iter_ingestable_files(
        [str(root)], task_skip, [], stats,
    )
    names = sorted(os.path.basename(f) for f, _ext in files)
    assert names == ["1718323200-export.json", "1718409600-export.json"]


# --- builder coverage assertion (#213) ------------------------------------


def test_coverage_check_passes_on_complete_mapping(tmp_path, monkeypatch):
    """A decomposition that covers every eligible file (post-fix shape,
    including the rootfiles-only catch-all) passes silently."""
    monkeypatch.delenv("HELIX_BFM_COVERAGE_CHECK", raising=False)
    root = _make_slack_like_root(tmp_path)
    entries = bfm._decompose_oversized_root(
        str(root), skip_dirs=set(), extra_filename_filters=[],
        **_LOW_THRESHOLDS,
    )
    tasks = _tasks_from_entries(entries, set())
    # Must not raise.
    bfm._assert_shard_coverage([str(root)], tasks, set(), [])


def test_coverage_check_raises_with_orphan_list(tmp_path, caplog):
    """A synthetic gap (the master bug shape: subdir tasks only) raises
    and logs ERROR with the orphan list."""
    root = _make_slack_like_root(tmp_path)
    tasks = [
        {"label": "slack__eng-sre", "root": str(root / "eng-sre"),
         "skip_dirs": set()},
        {"label": "slack__aditya-rao", "root": str(root / "aditya-rao"),
         "skip_dirs": set()},
    ]
    with caplog.at_level(logging.ERROR, logger="build_fixture_matrix"):
        with pytest.raises(RuntimeError, match=r"coverage gap.*2 eligible"):
            bfm._assert_shard_coverage([str(root)], tasks, set(), [])
    assert any(
        "1718323200-export.json" in rec.getMessage()
        and rec.levelno == logging.ERROR
        for rec in caplog.records
    ), "ERROR log must carry the orphan list"


def test_coverage_check_env_kill_switch(tmp_path, monkeypatch):
    """``HELIX_BFM_COVERAGE_CHECK=0`` skips the check (speed valve for
    huge corpora) — the same synthetic gap no longer raises."""
    root = _make_slack_like_root(tmp_path)
    tasks = [
        {"label": "slack__eng-sre", "root": str(root / "eng-sre"),
         "skip_dirs": set()},
    ]
    monkeypatch.setenv("HELIX_BFM_COVERAGE_CHECK", "0")
    bfm._assert_shard_coverage([str(root)], tasks, set(), [])  # no raise


def test_coverage_check_default_on(tmp_path, monkeypatch):
    """Unset env means the check runs (default ON)."""
    monkeypatch.delenv("HELIX_BFM_COVERAGE_CHECK", raising=False)
    root = _make_slack_like_root(tmp_path)
    with pytest.raises(RuntimeError):
        bfm._assert_shard_coverage([str(root)], [], set(), [])


# --- build_profile_sharded wiring ------------------------------------------


def test_build_profile_sharded_wires_catchall_task(tmp_path, monkeypatch):
    """End-to-end through the builder's task construction: the catch-all
    becomes a real task (rootfiles_only + extended skip set), the sizing
    pass counts only its root-level files, and the coverage assertion
    passes on the resulting task list."""
    monkeypatch.delenv("HELIX_BFM_COVERAGE_CHECK", raising=False)
    root = _make_slack_like_root(tmp_path)
    out_dir = tmp_path / "out"

    monkeypatch.setitem(bfm.PROFILES, "tiny213", {
        "label": "issue-213 repro profile",
        "active_roots": 1,
        "roots": [str(root)],
        "extra_skip_dirs": set(),
        "extra_filename_filters": [],
    })
    # Force decomposition of the small synthetic root.
    monkeypatch.setattr(bfm, "DEFAULT_AUTO_SUBSHARD_THRESHOLD_BYTES", 10_000)
    monkeypatch.setattr(bfm, "DEFAULT_AUTO_SUBSHARD_THRESHOLD_FILES", 10)

    seen_tasks: list[dict] = []

    def _stub_worker(task: dict) -> dict:
        seen_tasks.append(task)
        return {
            "label": task["label"], "root": task["root"],
            "shard_db_path": task["shard_db_path"], "gene_count": 0,
            "byte_size": 0, "elapsed_s": 0.0, "files": 0, "genes": 0,
            "skipped": 0, "errors": 0, "missing_roots": [],
            "fingerprint_payload": [], "source_index_payload": [],
            "dense_coverage": 0.0, "dense_genes_populated": 0,
            "paused": False,
        }

    monkeypatch.setattr(bfm, "_shard_worker_entry", _stub_worker)

    totals = bfm.build_profile_sharded(
        "tiny213", str(out_dir), shard_workers=1, shard_file_workers=1,
        auto_subshard_threshold_bytes=10_000,
        auto_subshard_threshold_files=10,
    )

    by_label = {t["label"]: t for t in seen_tasks}
    assert set(by_label) == {
        "slack__eng-sre", "slack__aditya-rao", "slack__root",
    }, f"unexpected task labels: {sorted(by_label)}"

    catchall = by_label["slack__root"]
    assert catchall["root"] == str(root)
    assert catchall.get("rootfiles_only") is True
    assert {"eng-sre", "aditya-rao"} <= catchall["skip_dirs"]
    # Sizing must respect the restricted walk: 2 root-level files only.
    assert catchall["eligible_files"] == 2
    # Subdir tasks keep the shared skip set (no rootfiles_only flag).
    assert by_label["slack__eng-sre"].get("rootfiles_only") is None
    assert by_label["slack__eng-sre"]["eligible_files"] == 30

    assert totals["shard_count"] == 3
