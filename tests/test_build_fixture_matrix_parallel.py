"""Parity test for issue #92 parallel ingest.

Builds the same small synthetic corpus twice -- once sequentially, once
with the new ``--parallel`` writer + ``mp.Pool`` workers -- and asserts
the resulting gene_ids and content hashes are identical.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import pickle
import sys
import sqlite3
from pathlib import Path

import pytest

# Make scripts/ importable.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "scripts")
)


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


@pytest.mark.slow
def test_parallel_matches_sequential(tmp_path, monkeypatch):
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
    import multiprocessing as mp

    with mp.Pool(1) as pool:
        return pool.map(abs, [-1])[0]


def test_process_executor_allows_nested_file_pool():
    """Outer shard executor workers must be able to spawn inner file pools."""
    with ProcessPoolExecutor(max_workers=1) as pool:
        assert pool.submit(_nested_pool_probe, 0).result(timeout=10) == 1


def test_inner_file_worker_iter_uses_pool(monkeypatch):
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


def test_build_profile_sharded_passes_shard_file_workers(tmp_path, monkeypatch):
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


def test_sharded_profile_filters_are_process_picklable():
    """Shard tasks cross Windows spawn process boundaries."""
    import build_fixture_matrix as bfm

    for profile in bfm.PROFILES.values():
        pickle.dumps(profile["extra_filename_filters"])


@pytest.mark.slow
def test_sharded_pool_matches_serial(tmp_path, monkeypatch):
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


def _make_files(root: Path, count: int, body_size: int, ext: str = ".py") -> None:
    """Write ``count`` files of approximately ``body_size`` bytes each."""
    root.mkdir(parents=True, exist_ok=True)
    body = "x" * body_size
    for i in range(count):
        (root / f"f{i}{ext}").write_text(body, encoding="utf-8")


def test_estimate_eligible_bytes_counts_only_passing_files(tmp_path):
    """``_estimate_eligible_bytes`` returns ``(eligible_files, eligible_bytes)``
    where eligibility matches the actual ingest filters: extension in
    INGEST_EXTS, size within MIN/MAX bounds, and ``extra_filename_filters``
    not rejecting the path."""
    import build_fixture_matrix as bfm

    root = tmp_path / "tree"
    # 3 python files, each ~1000 bytes (well within MIN/MAX bounds).
    _make_files(root, count=3, body_size=1000, ext=".py")
    # A file with a non-ingestable extension — should NOT count.
    (root / "ignored.bin").write_bytes(b"x" * 1000)
    # A file smaller than MIN_FILE_SIZE — should NOT count.
    (root / "tiny.py").write_text("x" * 10, encoding="utf-8")

    files, bytes_ = bfm._estimate_eligible_bytes(
        str(root), skip_dirs=set(), extra_filename_filters=[],
    )
    assert files == 3
    # Each text-mode write is exactly 1000 bytes on disk for ASCII content.
    assert bytes_ == 3 * 1000


def test_estimate_eligible_bytes_respects_skip_dirs(tmp_path):
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


def test_estimate_eligible_bytes_respects_filename_filter(tmp_path):
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


def test_estimate_eligible_bytes_missing_root_returns_zero(tmp_path):
    """A nonexistent root is treated as zero work, not an error."""
    import build_fixture_matrix as bfm

    files, bytes_ = bfm._estimate_eligible_bytes(
        str(tmp_path / "does-not-exist"),
        skip_dirs=set(),
        extra_filename_filters=[],
    )
    assert (files, bytes_) == (0, 0)


def test_build_profile_sharded_sorts_largest_first(tmp_path, monkeypatch):
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


def test_build_profile_sharded_preserves_order_when_disabled(tmp_path, monkeypatch):
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

