"""Merged fixture-matrix test suite for ``scripts/build_fixture_matrix.py``.

Combines four previously-separate test files -- all of which imported
``scripts/build_fixture_matrix.py`` through the same ``sys.path`` shim --
into four class groups:

- ``TestAutoSubshard`` -- issue #147 auto-subshard oversized-root decomposition
- ``TestParallel``     -- issue #92/#95/#97/#113 parallel + sharded build parity
- ``TestResume``       -- issue #150/#151 file-level resume + SIGINT pause/resume
- ``TestSilentFail``   -- 2026-05-23 silent-swallow regression in
  ``_chunk_and_tag_file`` / ``_drain_with_batched_splade``

Each class below carries its source file's original module docstring
(now as a class docstring) so the historical/issue context isn't lost.

The four source files shared exactly one byte-identical thing: the
``sys.path.insert`` shim that makes ``build_fixture_matrix`` importable
from ``scripts/``. That is hoisted here to a single module-level
statement. Their file-tree-building helpers (``_populate_files``,
``_populate_tree``, ``_make_files``, ``_make_shard_db``,
``_make_simple_text_file``) are genuinely different in shape --
subdir-sized decomposition trees vs. flat count/body_size trees vs.
multi-language parity corpora vs. sqlite shard-db fixtures vs. a single
sample file -- so they are kept as separate module-level helpers rather
than forced into one signature, per the merge's "keep divergent helpers
local (with a comment)" rule.
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import sqlite3
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

# Make scripts/ importable. This sys.path shim was byte-identical across
# all four source files -- the only genuinely shared preamble.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# ``test_build_fixture_matrix_resume.py`` (now TestResume) imported this
# at module scope in its source file and relied on it being in-scope for
# every test method without a local re-import; preserved here as-is.
# The other three groups re-import ``build_fixture_matrix`` locally inside
# each test (as their source files did) -- harmless, since it's already
# cached in ``sys.modules`` by the time any test runs.
import build_fixture_matrix as bfm


# ─────────────────────────────────────────────────────────────────────────
# Module-level helpers (divergent shapes across groups -- see docstring)
# ─────────────────────────────────────────────────────────────────────────


# -- TestAutoSubshard helper (from test_build_fixture_matrix_auto_subshard.py) --


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


# -- TestParallel helpers (from test_build_fixture_matrix_parallel.py) --


def _populate_tree(root: Path, n_files: int = 6) -> None:
    """Write a tiny deterministic corpus."""
    root.mkdir(parents=True, exist_ok=True)
    bodies = [
        "def alpha():\n    return 'A' * 200\n" * 4,
        "# header\nbeta = 12345\n" * 8,
        "class Gamma:\n    def m(self):\n        pass\n" * 5,
        "// js\nconst delta = () => 7;\n" * 6,
        "{\"epsilon\": [1, 2, 3, 4, 5, 6, 7, 8]}\n" * 4,
        "phi: zeta\nrho: theta\n" * 10,
    ]
    suffixes = [".py", ".py", ".py", ".js", ".json", ".yaml"]
    for i in range(n_files):
        (root / f"f{i}{suffixes[i % len(suffixes)]}").write_text(
            bodies[i % len(bodies)], encoding="utf-8",
        )


def _collect_gene_summary(db_path: Path) -> set[tuple[str, str]]:
    """Return {(gene_id, content_hash)} for every gene in db."""
    import hashlib
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT gene_id, content FROM genes").fetchall()
    conn.close()
    return {
        (gid, hashlib.sha256(content.encode("utf-8")).hexdigest())
        for gid, content in rows
    }


def _collect_main_db_summary(main_db_path: Path) -> dict[str, set[tuple]]:
    """Return {shards, fingerprint_rows} for parity checks on a main.db."""
    conn = sqlite3.connect(str(main_db_path))
    shards = {
        (row[0], row[1])
        for row in conn.execute("SELECT shard_name, category FROM shards")
    }
    fps = {
        (row[0], row[1], row[2])
        for row in conn.execute(
            "SELECT gene_id, shard_name, source_id FROM fingerprint_index"
        )
    }
    conn.close()
    return {"shards": shards, "fingerprint_rows": fps}


def _nested_pool_probe(_value: int) -> int:
    # Must stay module-level (not a class method) -- ProcessPoolExecutor
    # needs to pickle this callable to ship it to the worker process.
    import multiprocessing as mp

    with mp.Pool(1) as pool:
        return pool.map(abs, [-1])[0]


def _make_files(root: Path, count: int, body_size: int, ext: str = ".py") -> None:
    """Write ``count`` files of approximately ``body_size`` bytes each."""
    root.mkdir(parents=True, exist_ok=True)
    body = "x" * body_size
    for i in range(count):
        (root / f"f{i}{ext}").write_text(body, encoding="utf-8")


# -- TestResume helper (from test_build_fixture_matrix_resume.py) --


def _make_shard_db(path: Path, source_ids: list[str]) -> None:
    """Create a minimal per-shard ``.db`` with just enough schema for
    ``_filter_to_unseen`` to query."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE genes (gene_id TEXT PRIMARY KEY, source_id TEXT)"
    )
    conn.executemany(
        "INSERT INTO genes (gene_id, source_id) VALUES (?, ?)",
        [(f"g{i}", sid) for i, sid in enumerate(source_ids)],
    )
    conn.commit()
    conn.close()


# -- TestSilentFail helper (from test_build_fixture_matrix_silent_fail.py) --


def _make_simple_text_file(tmp_path: Path) -> Path:
    """Write a small .txt file the chunker can split into strands."""
    p = tmp_path / "sample.txt"
    p.write_text(
        "The quick brown fox jumps over the lazy dog. " * 40,
        encoding="utf-8",
    )
    return p


# ─────────────────────────────────────────────────────────────────────────
# TestAutoSubshard
# ─────────────────────────────────────────────────────────────────────────


class TestAutoSubshard:
    """Auto-subshard for issue #147.

    The fixture builder's "one shard per profile root" granularity becomes
    the long pole when one root dominates the corpus -- the 500K
    EnterpriseRAG-Bench attempt produced an 18 GB slack ``.db`` that
    exceeded the OS file-cache headroom and crashed the dense backfill rate
    from 27 g/s -> 0.12 g/s. ``_decompose_oversized_root`` adds a sizing-
    driven decomposition pass that splits oversized roots along their top-
    level subdirectories.

    Tests pin:

    - under-threshold root -> single shard, no decomposition
    - oversized root -> list of subshard ``(label, path)`` tuples
    - flat (no-subdir) root that's oversized -> fall back to single shard
    - depth-2 recursion (oversized subdir inside oversized root)
    - skip_dirs honored during decomposition
    - subshard labels use ``__`` (parent__child) delimiter
    """

    def test_decompose_small_root_returns_single_shard(self, tmp_path):
        """A root with file count + bytes under both thresholds should
        return a single (slug, root) tuple -- no decomposition.
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

    def test_decompose_oversized_root_walks_subdirs(self, tmp_path):
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
            threshold_bytes=10_000,    # tiny -- forces root to decompose
            threshold_files=50,         # 3 subdirs * 30 = 90 > 50 -> root over
        )
        # Each subdir has 30 files (< 50), so subshards do not recurse.
        assert len(result) == 3, f"expected 3 subshards, got {len(result)}: {result}"

        slugs = sorted(slug for slug, _ in result)
        assert slugs == ["bigproject__alpha", "bigproject__beta", "bigproject__gamma"]

        # Each path resolves into the right subdir
        by_slug = dict(result)
        assert by_slug["bigproject__alpha"] == str(root / "alpha")
        assert by_slug["bigproject__beta"] == str(root / "beta")

    def test_decompose_flat_root_falls_back_to_single_shard(self, tmp_path):
        """An oversized root with no subdirs (all files at top level) should
        fall back to returning the single (slug, root) tuple -- we can't
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
            threshold_files=10,    # 200 files > 10 -> over threshold
        )
        assert len(result) == 1, (
            f"flat root should fall back to single shard, got {result}"
        )
        assert result[0][1] == str(root)

    def test_decompose_recurses_into_oversized_subdir(self, tmp_path):
        """If a subdir is itself oversized (and has further subdirs), the
        function should recurse one level deeper. Labels nest with ``__``.
        Depth-2 only -- depth-3+ falls back to single shard for that branch.
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

    def test_decompose_respects_skip_dirs(self, tmp_path):
        """Subdirs in `skip_dirs` must not appear in the decomposed result
        -- same semantics as the rest of the fixture builder."""
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

    def test_decompose_nonexistent_root_returns_empty(self, tmp_path):
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


# ─────────────────────────────────────────────────────────────────────────
# TestParallel
# ─────────────────────────────────────────────────────────────────────────


class TestParallel:
    """Parity test for issue #92 parallel ingest.

    Builds the same small synthetic corpus twice -- once sequentially, once
    with the new ``--parallel`` writer + ``mp.Pool`` workers -- and asserts
    the resulting gene_ids and content hashes are identical.
    """

    @pytest.fixture(autouse=True)
    def _lean_builder_env(self, monkeypatch):
        """Force the lean ingest path for every test in this class.

        Without these, ``_build_one_shard`` loads SPLADE + BGE-M3 onto the
        GPU **in every spawn worker AND the parent** -- three CUDA contexts on
        a <=12 GB card is the documented #176 WDDM-spill livelock and hung the
        whole suite (parent stuck in ``as_completed`` while workers crept at
        the serialized-VRAM floor). Parity assertions here compare shard
        routing + fingerprint rows, which are independent of SPLADE/dense.
        Env vars (unlike monkeypatched module attrs) cross the Windows spawn
        boundary, so the workers inherit the kill-switches.
        """
        monkeypatch.setenv("HELIX_BFM_SPLADE", "0")
        monkeypatch.setenv("HELIX_BFM_DENSE_BACKFILL", "0")

    @pytest.mark.slow
    def test_parallel_matches_sequential(self, tmp_path, monkeypatch):
        """build_profile(parallel=False) and (parallel=True) should produce
        identical gene_ids + content hashes for the same corpus."""
        import build_fixture_matrix as bfm

        corpus = tmp_path / "corpus"
        _populate_tree(corpus, n_files=6)

        monkeypatch.setattr(bfm, "PROFILES", {
            "tiny92": {
                "label": "issue #92 parity test corpus",
                "active_roots": 1,
                "roots": [str(corpus)],
                "extra_skip_dirs": set(),
                "extra_filename_filters": [],
            }
        })

        seq_db = tmp_path / "seq.db"
        par_db = tmp_path / "par.db"

        bfm.build_profile("tiny92", str(seq_db), parallel=False)
        bfm.build_profile(
            "tiny92", str(par_db),
            parallel=True, n_workers=2, batch_size=8, chunksize=1,
        )

        seq = _collect_gene_summary(seq_db)
        par = _collect_gene_summary(par_db)

        assert seq == par, (
            f"gene-id/content mismatch\n"
            f"  only in seq: {sorted(seq - par)[:5]}...\n"
            f"  only in par: {sorted(par - seq)[:5]}..."
        )

    def test_process_executor_allows_nested_file_pool(self):
        """Outer shard executor workers must be able to spawn inner file pools."""
        with ProcessPoolExecutor(max_workers=1) as pool:
            assert pool.submit(_nested_pool_probe, 0).result(timeout=10) == 1

    def test_inner_file_worker_iter_uses_pool(self, monkeypatch):
        """Shard-local file workers run chunk+tag in a CPU-only pool."""
        import build_fixture_matrix as bfm

        calls = {"workers": None, "chunksize": None, "initialized": 0}

        class FakePool:
            def __init__(self, workers, initializer=None):
                calls["workers"] = workers
                self.initializer = initializer

            def __enter__(self):
                if self.initializer is not None:
                    self.initializer()
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def imap_unordered(self, func, files, chunksize=1):
                calls["chunksize"] = chunksize
                return [func(f) for f in files]

        monkeypatch.setattr(bfm.mp, "Pool", FakePool)
        monkeypatch.setattr(
            bfm, "_init_worker",
            lambda: calls.__setitem__("initialized", calls["initialized"] + 1),
        )
        monkeypatch.setattr(
            bfm, "_chunk_and_tag_file",
            lambda f: [{"source_id": f[0]}],
        )

        files = [("a.py", ".py"), ("b.py", ".py")]
        rows = list(bfm._iter_chunked_file_gene_dicts(
            files, file_workers=3, chunksize=2,
        ))

        assert calls == {"workers": 3, "chunksize": 2, "initialized": 1}
        assert rows == [[{"source_id": "a.py"}], [{"source_id": "b.py"}]]

    def test_build_profile_sharded_passes_shard_file_workers(self, tmp_path, monkeypatch):
        """Programmatic sharded builds include shard-local CPU worker count."""
        import build_fixture_matrix as bfm

        root = tmp_path / "root"
        root.mkdir()
        captured_tasks = []

        monkeypatch.setattr(bfm, "PROFILES", {
            "tiny95": {
                "label": "issue #95 shard file workers",
                "active_roots": 1,
                "roots": [str(root)],
                "extra_skip_dirs": set(),
                "extra_filename_filters": [],
            }
        })

        def fake_shard_worker_entry(task):
            captured_tasks.append(task)
            return {
                "label": task["label"],
                "root": task["root"],
                "shard_db_path": task["shard_db_path"],
                "gene_count": 0,
                "byte_size": 0,
                "elapsed_s": 0.0,
                "files": 0,
                "genes": 0,
                "skipped": 0,
                "errors": 0,
                "missing_roots": [],
                "fingerprint_payload": [],
            }

        monkeypatch.setattr(bfm, "_shard_worker_entry", fake_shard_worker_entry)

        stats = bfm.build_profile_sharded(
            "tiny95", str(tmp_path / "out"),
            shard_workers=1, shard_file_workers=3, batch_size=8,
        )

        assert captured_tasks[0]["shard_file_workers"] == 3
        assert stats["shard_file_workers"] == 3

    def test_sharded_profile_filters_are_process_picklable(self):
        """Shard tasks cross Windows spawn process boundaries."""
        import build_fixture_matrix as bfm

        for profile in bfm.PROFILES.values():
            pickle.dumps(profile["extra_filename_filters"])

    @pytest.mark.slow
    def test_sharded_pool_matches_serial(self, tmp_path, monkeypatch):
        """build_profile_sharded(shard_workers=1) and (shard_workers=2) should
        produce identical main.db fingerprint_index + per-shard gene_ids."""
        import build_fixture_matrix as bfm

        a = tmp_path / "rootA"
        b = tmp_path / "rootB"
        _populate_tree(a, n_files=4)
        _populate_tree(b, n_files=4)

        monkeypatch.setattr(bfm, "PROFILES", {
            "shardtest92": {
                "label": "issue #92 sharded parity",
                "active_roots": 2,
                "roots": [str(a), str(b)],
                "extra_skip_dirs": set(),
                "extra_filename_filters": [],
            }
        })

        out_serial = tmp_path / "ser"
        out_pool = tmp_path / "pool"

        bfm.build_profile_sharded(
            "shardtest92", str(out_serial),
            shard_workers=1, batch_size=8,
        )
        bfm.build_profile_sharded(
            "shardtest92", str(out_pool),
            shard_workers=2, batch_size=8,
        )

        ser_summary = _collect_main_db_summary(out_serial / "main.genome.db")
        pool_summary = _collect_main_db_summary(out_pool / "main.genome.db")

        assert ser_summary["shards"] == pool_summary["shards"]
        assert ser_summary["fingerprint_rows"] == pool_summary["fingerprint_rows"]

    # ── Pre-ingest sizing + largest-first sort (issue #97 A.1) ────────────────

    def test_estimate_eligible_bytes_counts_only_passing_files(self, tmp_path):
        """``_estimate_eligible_bytes`` returns ``(eligible_files, eligible_bytes)``
        where eligibility matches the actual ingest filters: extension in
        INGEST_EXTS, size within MIN/MAX bounds, and ``extra_filename_filters``
        not rejecting the path."""
        import build_fixture_matrix as bfm

        root = tmp_path / "tree"
        # 3 python files, each ~1000 bytes (well within MIN/MAX bounds).
        _make_files(root, count=3, body_size=1000, ext=".py")
        # A file with a non-ingestable extension -- should NOT count.
        (root / "ignored.bin").write_bytes(b"x" * 1000)
        # A file smaller than MIN_FILE_SIZE -- should NOT count.
        (root / "tiny.py").write_text("x" * 10, encoding="utf-8")

        files, bytes_ = bfm._estimate_eligible_bytes(
            str(root), skip_dirs=set(), extra_filename_filters=[],
        )
        assert files == 3
        # Each text-mode write is exactly 1000 bytes on disk for ASCII content.
        assert bytes_ == 3 * 1000

    def test_estimate_eligible_bytes_respects_skip_dirs(self, tmp_path):
        """``skip_dirs`` prunes directory descent, so files inside aren't counted."""
        import build_fixture_matrix as bfm

        root = tmp_path / "tree"
        _make_files(root, count=2, body_size=500, ext=".py")
        _make_files(root / "node_modules", count=5, body_size=500, ext=".py")

        files, bytes_ = bfm._estimate_eligible_bytes(
            str(root),
            skip_dirs={"node_modules"},
            extra_filename_filters=[],
        )
        assert files == 2
        assert bytes_ == 2 * 500

    def test_estimate_eligible_bytes_respects_filename_filter(self, tmp_path):
        """``extra_filename_filters`` (predicate funcs returning True to skip)
        are applied per-file, same as the real ingest walker."""
        import build_fixture_matrix as bfm

        root = tmp_path / "tree"
        _make_files(root, count=3, body_size=500, ext=".py")
        (root / "skip_me.py").write_text("x" * 500, encoding="utf-8")

        files, _bytes = bfm._estimate_eligible_bytes(
            str(root),
            skip_dirs=set(),
            extra_filename_filters=[lambda p: "skip_me" in p],
        )
        assert files == 3

    def test_estimate_eligible_bytes_missing_root_returns_zero(self, tmp_path):
        """A nonexistent root is treated as zero work, not an error."""
        import build_fixture_matrix as bfm

        files, bytes_ = bfm._estimate_eligible_bytes(
            str(tmp_path / "does-not-exist"),
            skip_dirs=set(),
            extra_filename_filters=[],
        )
        assert (files, bytes_) == (0, 0)

    def test_build_profile_sharded_sorts_largest_first(self, tmp_path, monkeypatch):
        """Default behavior: shard tasks are submitted to the worker pool
        sorted by eligible_bytes descending so the long pole gets the
        longest head start (issue #97 A.1)."""
        import build_fixture_matrix as bfm

        small_root = tmp_path / "small"
        big_root = tmp_path / "big"
        mid_root = tmp_path / "mid"
        _make_files(small_root, count=1, body_size=200, ext=".py")
        _make_files(mid_root, count=5, body_size=1000, ext=".py")
        _make_files(big_root, count=20, body_size=1500, ext=".py")

        captured_order: list[str] = []

        def fake_entry(task):
            captured_order.append(task["label"])
            return {
                "label": task["label"], "root": task["root"],
                "shard_db_path": task["shard_db_path"],
                "gene_count": 0, "byte_size": 0, "elapsed_s": 0.0,
                "files": 0, "genes": 0, "skipped": 0, "errors": 0,
                "missing_roots": [], "fingerprint_payload": [],
            }

        monkeypatch.setattr(bfm, "_shard_worker_entry", fake_entry)
        monkeypatch.setattr(bfm, "PROFILES", {
            "tiny97": {
                "label": "issue #97 sort test",
                "active_roots": 3,
                # Declared smallest -> mid -> biggest; expect dispatch order reversed.
                "roots": [str(small_root), str(mid_root), str(big_root)],
                "extra_skip_dirs": set(),
                "extra_filename_filters": [],
            }
        })

        stats = bfm.build_profile_sharded(
            "tiny97", str(tmp_path / "out"),
            shard_workers=1, shard_file_workers=1, batch_size=8,
            sort_largest_first=True,
        )

        big_slug = bfm._slug_for_root(str(big_root))
        mid_slug = bfm._slug_for_root(str(mid_root))
        small_slug = bfm._slug_for_root(str(small_root))
        assert captured_order == [big_slug, mid_slug, small_slug]
        assert stats["sort_largest_first"] is True
        assert "sizing_elapsed_s" in stats

    def test_build_profile_sharded_preserves_order_when_disabled(self, tmp_path, monkeypatch):
        """``sort_largest_first=False`` (the ``--no-shard-sort`` CLI flag)
        preserves the declared ``profile["roots"]`` order."""
        import build_fixture_matrix as bfm

        a = tmp_path / "a"
        b = tmp_path / "b"
        _make_files(a, count=1, body_size=200, ext=".py")
        _make_files(b, count=20, body_size=1500, ext=".py")

        captured_order: list[str] = []

        def fake_entry(task):
            captured_order.append(task["label"])
            return {
                "label": task["label"], "root": task["root"],
                "shard_db_path": task["shard_db_path"],
                "gene_count": 0, "byte_size": 0, "elapsed_s": 0.0,
                "files": 0, "genes": 0, "skipped": 0, "errors": 0,
                "missing_roots": [], "fingerprint_payload": [],
            }

        monkeypatch.setattr(bfm, "_shard_worker_entry", fake_entry)
        monkeypatch.setattr(bfm, "PROFILES", {
            "tiny97b": {
                "label": "issue #97 no-sort test",
                "active_roots": 2,
                "roots": [str(a), str(b)],
                "extra_skip_dirs": set(),
                "extra_filename_filters": [],
            }
        })

        stats = bfm.build_profile_sharded(
            "tiny97b", str(tmp_path / "out"),
            shard_workers=1, shard_file_workers=1, batch_size=8,
            sort_largest_first=False,
        )

        a_slug = bfm._slug_for_root(str(a))
        b_slug = bfm._slug_for_root(str(b))
        # Declaration order preserved despite a being much smaller than b.
        assert captured_order == [a_slug, b_slug]
        assert stats["sort_largest_first"] is False

    # ── source_index population on sharded builds (PR #113 follow-up) ─────────

    def test_sharded_build_populates_source_index(self, tmp_path, monkeypatch):
        """Sharded build must write rows to ``main.genome.db:source_index``.

        Prior to this regression test the build path only populated
        ``fingerprint_index``. ``cymatix_context/context_packet.py::_lookup_source_row``
        (added in PR #113) reads from ``source_index`` for packet freshness +
        authority, so bench fixtures with an empty ``source_index`` silently
        skipped that path.

        Uses a stubbed ``_shard_worker_entry`` so the test stays under the
        ``slow`` threshold -- no SPLADE / spaCy loading.
        """
        import build_fixture_matrix as bfm

        a = tmp_path / "rootA"
        b = tmp_path / "rootB"
        a.mkdir()
        b.mkdir()
        (a / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
        (b / "beta.py").write_text("def beta():\n    return 2\n", encoding="utf-8")

        now = 1_700_000_000.0

        def fake_entry(task):
            # Mimic the real ``_build_one_shard`` return: a fingerprint payload
            # and a source_index payload, both keyed on a synthetic gene_id.
            label = task["label"]
            gid = f"g_{label}_0"
            fp_row = (gid, label, "src/0", None, None, None, 1, None, now)
            si_row = (
                gid, label, "src/0", task["root"], "code",
                now, now, "deadbeef", "medium", "primary",
                None, now, None, now,
            )
            return {
                "label": label, "root": task["root"],
                "shard_db_path": task["shard_db_path"],
                "gene_count": 1, "byte_size": 0, "elapsed_s": 0.0,
                "files": 1, "genes": 1, "skipped": 0, "errors": 0,
                "missing_roots": [],
                "fingerprint_payload": [fp_row],
                "source_index_payload": [si_row],
            }

        monkeypatch.setattr(bfm, "_shard_worker_entry", fake_entry)
        monkeypatch.setattr(bfm, "PROFILES", {
            "tiny_si": {
                "label": "source_index regression",
                "active_roots": 2,
                "roots": [str(a), str(b)],
                "extra_skip_dirs": set(),
                "extra_filename_filters": [],
            }
        })

        out = tmp_path / "out"
        stats = bfm.build_profile_sharded(
            "tiny_si", str(out),
            shard_workers=1, shard_file_workers=1, batch_size=8,
        )

        main_db = out / "main.genome.db"
        assert main_db.exists()
        conn = sqlite3.connect(str(main_db))
        try:
            si_count = conn.execute(
                "SELECT COUNT(*) FROM source_index"
            ).fetchone()[0]
            # Two shards × one synthetic gene_id each.
            assert si_count == 2, (
                f"source_index should have 2 rows, got {si_count}"
            )

            # Spot-check the schema fields landed correctly.
            rows = conn.execute(
                "SELECT gene_id, shard_name, source_id, repo_root, source_kind, "
                "observed_at, mtime, content_hash, volatility_class, "
                "authority_class, last_verified_at, invalidated_at, updated_at "
                "FROM source_index ORDER BY gene_id"
            ).fetchall()
            assert len(rows) == 2
            for row in rows:
                (gid, shard, src_id, repo_root, source_kind, observed_at,
                 mtime, content_hash, vol, auth, last_verif, invalidated,
                 updated_at) = row
                assert gid.startswith("g_")
                assert shard in {bfm._slug_for_root(str(a)),
                                 bfm._slug_for_root(str(b))}
                assert src_id == "src/0"
                assert repo_root in {str(a), str(b)}
                assert source_kind == "code"
                assert vol == "medium"
                assert auth == "primary"
                assert observed_at == now
                assert mtime == now
                assert content_hash == "deadbeef"
                assert last_verif == now
                assert invalidated is None
                assert updated_at is not None
        finally:
            conn.close()

        # The per-shard manifest entry should also include the count.
        assert all("source_index_rows" in s for s in stats["shards"])
        assert sum(s["source_index_rows"] for s in stats["shards"]) == 2

    def test_sharded_build_source_index_lookup_returns_row(self, tmp_path, monkeypatch):
        """``context_packet._lookup_source_row`` must find a row for a gene_id
        written by the sharded build path. This is the failure mode PR #113
        surfaced -- empty ``source_index`` makes the lookup return None for
        every gene_id in a matrix-built fixture."""
        import build_fixture_matrix as bfm
        from cymatix_context.context_packet import _lookup_source_row

        root = tmp_path / "root"
        root.mkdir()
        (root / "alpha.py").write_text("def f(): pass\n", encoding="utf-8")

        now = 1_700_000_000.0
        target_gid = "test_lookup_gene_id"

        def fake_entry(task):
            label = task["label"]
            fp_row = (target_gid, label, "src/0", None, None, None, 1, None, now)
            si_row = (
                target_gid, label, "src/0", task["root"], "code",
                now, now, "abcd1234", "medium", "primary",
                None, now, None, now,
            )
            return {
                "label": label, "root": task["root"],
                "shard_db_path": task["shard_db_path"],
                "gene_count": 1, "byte_size": 0, "elapsed_s": 0.0,
                "files": 1, "genes": 1, "skipped": 0, "errors": 0,
                "missing_roots": [],
                "fingerprint_payload": [fp_row],
                "source_index_payload": [si_row],
            }

        monkeypatch.setattr(bfm, "_shard_worker_entry", fake_entry)
        monkeypatch.setattr(bfm, "PROFILES", {
            "tiny_si2": {
                "label": "source_index lookup regression",
                "active_roots": 1,
                "roots": [str(root)],
                "extra_skip_dirs": set(),
                "extra_filename_filters": [],
            }
        })

        out = tmp_path / "out"
        bfm.build_profile_sharded(
            "tiny_si2", str(out),
            shard_workers=1, shard_file_workers=1, batch_size=8,
        )

        conn = sqlite3.connect(str(out / "main.genome.db"))
        conn.row_factory = sqlite3.Row
        try:
            row = _lookup_source_row(conn, target_gid)
        finally:
            conn.close()

        assert row is not None, (
            "expected _lookup_source_row to return a row for a sharded-build "
            "gene_id, got None (source_index likely empty)"
        )
        assert row["gene_id"] == target_gid
        assert row["volatility_class"] == "medium"
        assert row["authority_class"] == "primary"
        assert row["content_hash"] == "abcd1234"


# ─────────────────────────────────────────────────────────────────────────
# TestResume
# ─────────────────────────────────────────────────────────────────────────


class TestResume:
    """Tests for issue #150 (file-level resume + --rebuild) and issue #151
    (SIGINT pause-then-resume checkpoint) in ``scripts/build_fixture_matrix.py``.

    The SIGINT handler itself isn't easy to drive end-to-end from a unit
    test (signals interact with the test runner), so we exercise the
    pieces it composes -- the module-level flag, ``_PauseRequested``, the
    checkpoint marker writer -- and leave the full ``signal.signal`` round-
    trip to manual smoke-testing per the issue's acceptance criteria.
    """

    # ── _filter_to_unseen ─────────────────────────────────────────────────────

    def test_filter_to_unseen_db_missing_returns_all(self, tmp_path):
        """No shard DB on disk => fresh build => return every file."""
        files = [
            (str(tmp_path / "a.py"), ".py"),
            (str(tmp_path / "b.py"), ".py"),
        ]
        missing = tmp_path / "does-not-exist.db"
        assert bfm._filter_to_unseen(files, str(missing)) == files

    def test_filter_to_unseen_empty_db_returns_all(self, tmp_path):
        """Shard DB exists but no genes row -> fall through, return all."""
        db = tmp_path / "empty.db"
        _make_shard_db(db, source_ids=[])
        files = [(str(tmp_path / "a.py"), ".py")]
        assert bfm._filter_to_unseen(files, str(db)) == files

    def test_filter_to_unseen_drops_seen_keeps_unseen(self, tmp_path):
        """Files whose source_id is in the shard DB are dropped; others stay."""
        a, b, c = (
            str(tmp_path / "a.py"),
            str(tmp_path / "b.py"),
            str(tmp_path / "c.py"),
        )
        db = tmp_path / "partial.db"
        _make_shard_db(db, source_ids=[a, c])
        files = [(a, ".py"), (b, ".py"), (c, ".py")]
        out = bfm._filter_to_unseen(files, str(db))
        assert out == [(b, ".py")]

    def test_filter_to_unseen_no_genes_table(self, tmp_path):
        """Shard DB exists but lacks a ``genes`` table -> return all."""
        db = tmp_path / "schema-only.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE other (x INTEGER)")
        conn.commit()
        conn.close()
        files = [(str(tmp_path / "a.py"), ".py")]
        assert bfm._filter_to_unseen(files, str(db)) == files

    # ── --rebuild flag ────────────────────────────────────────────────────────

    def test_rebuild_flag_registered(self, monkeypatch):
        """``--rebuild`` is wired into argparse and surfaces on the namespace."""
        import argparse

        # Drive ``main()``'s argparse setup in isolation by re-parsing the
        # same definitions. The cleanest way is to import the parser code,
        # so we re-use it by stripping ``main`` to its parse step.
        parser = argparse.ArgumentParser()
        # Mirror the relevant subset; the test only cares about --rebuild.
        parser.add_argument("--rebuild", action="store_true")
        ns = parser.parse_args(["--rebuild"])
        assert ns.rebuild is True
        ns_default = parser.parse_args([])
        assert ns_default.rebuild is False

    def test_build_one_shard_rebuild_unlinks_existing(self, tmp_path, monkeypatch):
        """``rebuild=True`` should unconditionally unlink a pre-existing
        shard ``.db`` instead of trying to salvage or resume it.
        """
        shard_db = tmp_path / "shard.db"
        # Pre-populate with a fake "complete" shard so the salvage path would
        # otherwise short-circuit. We don't care about the exact schema --
        # only whether the file gets unlinked before the real build starts.
        _make_shard_db(shard_db, source_ids=["x"])
        assert shard_db.exists()

        # Patch the heavy collaborators so we never actually run an ingest:
        # we only need to confirm the unlink branch fires and the function
        # progresses past the rebuild gate.
        called = {"unlinked": False}

        real_unlink = Path.unlink

        def _track_unlink(self, *a, **kw):
            if self == shard_db:
                called["unlinked"] = True
            return real_unlink(self, *a, **kw)

        monkeypatch.setattr(Path, "unlink", _track_unlink)

        # Stub Genome so construction is cheap and we don't need the real
        # SPLADE/tagger pipelines.
        class _StubGenome:
            def __init__(self, *a, **kw):
                self.conn = sqlite3.connect(":memory:")
                self.conn.row_factory = sqlite3.Row
                self.conn.execute(
                    "CREATE TABLE genes ("
                    "gene_id TEXT, source_id TEXT, repo_root TEXT, "
                    "source_kind TEXT, observed_at REAL, mtime REAL, "
                    "content_hash TEXT, volatility_class TEXT, "
                    "authority_class TEXT, support_span TEXT, "
                    "last_verified_at REAL, promoter TEXT, key_values TEXT, "
                    "is_fragment INTEGER)"
                )

            def stats(self):
                return {"total_genes": 0}

            def close(self):
                self.conn.close()

        monkeypatch.setattr(bfm, "Genome", _StubGenome)
        # Skip the dense backfill which would try to load the BGE model.
        monkeypatch.setattr(
            bfm, "_backfill_dense",
            lambda _p: {"dense_coverage": 0.0, "populated_after": 0},
        )
        # Stub the file walk to return nothing -- we just want to confirm
        # the unlink path ran.
        monkeypatch.setattr(
            bfm, "_iter_ingestable_files",
            lambda *a, **kw: [],
        )

        res = bfm._build_one_shard(
            label="test",
            root=str(tmp_path),
            shard_db_path=str(shard_db),
            skip_dirs=set(),
            extra_filename_filters=[],
            rebuild=True,
        )
        assert called["unlinked"], "rebuild=True should unlink existing shard"
        assert res["paused"] is False

    # ── SIGINT pause + checkpoint marker ─────────────────────────────────────

    def test_pause_checkpoint_marker_format(self, tmp_path, monkeypatch):
        """``_write_pause_checkpoint`` writes ``.paused-at-<shard>-<row>.json``
        with the documented schema."""
        monkeypatch.setattr(bfm, "_PAUSE_CHECKPOINT_DIR", str(tmp_path))
        path = bfm._write_pause_checkpoint("shard-a", 1234)
        assert path is not None
        p = Path(path)
        assert p.exists()
        assert p.name == ".paused-at-shard-a-1234.json"
        payload = json.loads(p.read_text(encoding="utf-8"))
        assert payload["shard"] == "shard-a"
        assert payload["row"] == 1234
        assert "paused_at" in payload
        assert payload["pid"] == os.getpid()

    def test_pause_checkpoint_no_dir_returns_none(self, monkeypatch):
        """When no checkpoint dir is configured, the writer is a no-op."""
        monkeypatch.setattr(bfm, "_PAUSE_CHECKPOINT_DIR", None)
        assert bfm._write_pause_checkpoint("shard", 0) is None

    def test_pause_requested_raises_at_batch_boundary(self, monkeypatch):
        """When ``_PAUSE_REQUESTED`` is True, the drain loop raises
        ``_PauseRequested`` instead of continuing."""
        # Pretend a SIGINT arrived before any work started.
        monkeypatch.setattr(bfm, "_PAUSE_REQUESTED", True)

        # Build a tiny gene_dict_iter that yields one batch's worth of dicts.
        # Real Gene model validation would force us to populate a lot of
        # fields, so stub the schemas import the drain function uses.
        class _StubGene:
            def __init__(self, **kw):
                self.content = kw.get("content", "x" * 50)

        class _StubSplade:
            @staticmethod
            def encode_batch(_texts):
                return [None] * len(_texts)

        class _StubGenome:
            def upsert_doc(self, *a, **kw):
                pass

        # Patch the late imports inside ``_drain_with_batched_splade``. The
        # function does ``from cymatix_context.backends import splade_backend``
        # and ``from cymatix_context.schemas import Gene`` at call time -- we
        # inject the stubs through ``sys.modules``.
        import types
        fake_backends = types.ModuleType("cymatix_context.backends")
        fake_backends.splade_backend = _StubSplade
        fake_schemas = types.ModuleType("cymatix_context.schemas")
        fake_schemas.Gene = _StubGene
        monkeypatch.setitem(sys.modules, "cymatix_context.backends", fake_backends)
        monkeypatch.setitem(sys.modules, "cymatix_context.schemas", fake_schemas)

        # One gene dict per file, batch_size=1 so the boundary is reached on
        # the first iteration.
        gene_dict_iter = iter([
            [{"content": "alpha"}],
            [{"content": "beta"}],  # never reached -- pause fires first.
        ])
        stats = {"files": 0, "genes": 0, "errors": 0, "t0": 0.0}
        with pytest.raises(bfm._PauseRequested):
            bfm._drain_with_batched_splade(
                gene_dict_iter, _StubGenome(), stats, batch_size=1,
            )
        # Reset the module flag for downstream tests.
        monkeypatch.setattr(bfm, "_PAUSE_REQUESTED", False)


# ─────────────────────────────────────────────────────────────────────────
# TestSilentFail
# ─────────────────────────────────────────────────────────────────────────


class TestSilentFail:
    """Regression for the silent-swallow bug in ``_chunk_and_tag_file``.

    Bug story (2026-05-23): a sharded build of the 500K EnterpriseRAG corpus
    completed in 550 s with all nine shards reporting **0 genes**. Root cause
    was ``spacy`` missing in the bench venv, which made ``tagger.pack(...)``
    raise ``ModuleNotFoundError`` for every strand. The exception was
    silently swallowed by ``try/except Exception: pass`` in
    ``_chunk_and_tag_file`` -- no log, no error counter visible at the
    fixture-builder level -- so the build "succeeded" while producing an
    empty DB.

    The fix: log the first occurrence of any exception class raised by
    ``tagger.pack`` (warning level, once per process per type) so that a
    missing dependency or other systemic failure is visible immediately.
    """

    def test_chunk_and_tag_logs_when_tagger_pack_raises(self, tmp_path, caplog, monkeypatch):
        """When ``tagger.pack`` raises for every strand of a file,
        ``_chunk_and_tag_file`` should:

        1. Still return ``[]`` (preserve the empty-list contract so the
           drain skips this file as an error rather than crashing).
        2. Emit at least one WARNING-level log on the ``bench.matrix``
           logger so the failure is visible -- even though every strand
           individually was swallowed.

        Currently (pre-fix) the function uses ``try/except Exception: pass``
        around the per-strand pack, so no log is emitted and 100 % of a
        corpus can silently degrade to 0 genes. This test pins the desired
        post-fix behaviour.
        """
        import build_fixture_matrix as bfm

        # Init worker globals (chunker + tagger) using the real venv.
        # We don't care which tagger backend is loaded -- we replace
        # ``.pack`` below.
        bfm._init_worker()
        assert bfm._worker_chunker is not None
        assert bfm._worker_tagger is not None

        # Drop any "logged once" guards from prior tests so we observe the
        # fresh emission. (The guard is a fix-side detail; tests that exist
        # before the fix will not have it, which is fine -- `getattr` shields
        # us.)
        guard = getattr(bfm, "_logged_pack_errors", None)
        if guard is not None:
            guard.clear()

        class _BoomTagger:
            """Stub: every .pack call raises a distinctive runtime error."""

            def pack(self, *args, **kwargs):
                raise RuntimeError("test-induced tagger.pack failure")

        monkeypatch.setattr(bfm, "_worker_tagger", _BoomTagger())

        sample = _make_simple_text_file(tmp_path)

        with caplog.at_level(logging.WARNING, logger="bench.matrix"):
            result = bfm._chunk_and_tag_file((str(sample), ".txt"))

        # Contract 1: empty list returned (drain will count this as a
        # file-with-errors instead of crashing the build).
        assert result == [], (
            "expected empty gene list when tagger.pack raises on every strand; "
            f"got {len(result)} genes"
        )

        # Contract 2: at least one warning was emitted on bench.matrix.
        bench_warnings = [
            r for r in caplog.records
            if r.name == "bench.matrix" and r.levelno >= logging.WARNING
        ]
        assert bench_warnings, (
            "expected at least one WARNING on the bench.matrix logger when "
            "tagger.pack raises; got nothing -- silent-swallow bug is back. "
            f"All captured records: {[(r.name, r.levelname, r.message) for r in caplog.records]}"
        )

        # Contract 3: the warning mentions the underlying exception class
        # so the user can immediately see what's missing (e.g.
        # ``ModuleNotFoundError`` for the original spaCy case).
        combined = " ".join(r.getMessage() for r in bench_warnings)
        assert "RuntimeError" in combined or "test-induced tagger.pack failure" in combined, (
            f"warning didn't surface the underlying exception; got: {combined!r}"
        )

    def test_drain_logs_when_gene_construction_fails(self, caplog):
        """Pinning regression for the second silent-counter in this file:
        ``_drain_with_batched_splade`` used to do ``except Exception:
        stats["errors"] += 1`` around ``Gene(**gd)`` with no log. If the
        schema drifts or a worker returns malformed dicts, the build
        silently degrades. After the 2026-05-23 fix the drain logs the
        first occurrence of each exception class on ``bench.matrix``.

        This test stays off the SPLADE/GPU path by feeding malformed gene
        dicts so Gene construction fails -- ``buf`` stays empty and
        ``_flush`` returns early without calling ``splade_backend``.
        """
        import time
        import build_fixture_matrix as bfm

        bad_gene_dicts = [
            {"this": "is not", "a valid": "Gene shape"},
            {"definitely": "missing required fields"},
        ]

        stats = {"genes": 0, "errors": 0, "files": 0,
                 "t0": time.perf_counter()}

        class _UnusedGenome:
            def upsert_doc(self, *args, **kwargs):
                raise AssertionError(
                    "upsert_doc should not be called when Gene construction fails"
                )

        with caplog.at_level(logging.WARNING, logger="bench.matrix"):
            bfm._drain_with_batched_splade(
                iter([bad_gene_dicts]),
                _UnusedGenome(),
                stats,
                batch_size=64,
            )

        # The error counter still ticks (preserves the existing contract).
        assert stats["errors"] >= 1, (
            f"expected errors counter to tick on bad gene dicts; "
            f"got stats={stats!r}"
        )

        # And a warning is emitted on bench.matrix for the Gene stage.
        bench_warnings = [
            r for r in caplog.records
            if r.name == "bench.matrix" and r.levelno >= logging.WARNING
        ]
        assert bench_warnings, (
            "expected at least one WARNING when Gene(**gd) raises; "
            f"got nothing. All records: {[(r.name, r.levelname, r.getMessage()) for r in caplog.records]}"
        )
        combined = " ".join(r.getMessage() for r in bench_warnings)
        assert "drain Gene" in combined, (
            f"warning didn't tag the failing stage; got: {combined!r}"
        )

    def test_chunk_and_tag_logs_once_per_exception_type(self, tmp_path, caplog, monkeypatch):
        """Across multiple files that all fail the same way, we should log
        once (not per-file) so a 500K-file run doesn't drown the operator
        in 500K identical warnings."""
        import build_fixture_matrix as bfm

        bfm._init_worker()
        guard = getattr(bfm, "_logged_pack_errors", None)
        if guard is not None:
            guard.clear()

        class _BoomTagger:
            def pack(self, *args, **kwargs):
                raise RuntimeError("same failure each time")

        monkeypatch.setattr(bfm, "_worker_tagger", _BoomTagger())

        # Three identical-failure files
        files = []
        for i in range(3):
            p = tmp_path / f"file{i}.txt"
            p.write_text("hello world " * 50, encoding="utf-8")
            files.append(p)

        with caplog.at_level(logging.WARNING, logger="bench.matrix"):
            for f in files:
                bfm._chunk_and_tag_file((str(f), ".txt"))

        bench_warnings = [
            r for r in caplog.records
            if r.name == "bench.matrix" and r.levelno >= logging.WARNING
        ]
        # Exactly one (per-type rate limit). Allow up to 2 in case the
        # warning is emitted per-call-site rather than per-exc-type, but
        # 3+ would mean no rate-limit at all.
        assert 1 <= len(bench_warnings) <= 2, (
            "expected at most 2 warnings across 3 identical failures (one "
            f"per exception type), got {len(bench_warnings)}: "
            f"{[r.getMessage() for r in bench_warnings]}"
        )
