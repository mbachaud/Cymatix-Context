"""MAX_PATH overflow guard for the mirrored corpus-shard layout.

The filesystem-mirroring layout (``genomes/<drive>/<source path>/<label>
.genome.db``) is self-identifying by design, but a deep source root mirrored
under a deep ``genomes_root`` can exceed Windows' classic 260-char MAX_PATH
(observed: pytest tmp roots mirrored under a pytest-tmp out dir produced
``...pool\\C\\Users\\...\\Temp\\pytest-of-...\\rootA\\roota.genome.db``).
``corpus_shard_db`` now falls back to a compact deterministic
``_overflow/<label>-<digest10>.genome.db`` path beyond the cap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cymatix_context.sharding import corpus_shard_db, corpus_shard_dir


SHORT_ROOT = "F:/Projects/helix-context"


def test_short_paths_keep_mirrored_layout(tmp_path):
    db = corpus_shard_db(SHORT_ROOT, "helix", tmp_path)
    assert db == corpus_shard_dir(SHORT_ROOT, tmp_path) / "helix.genome.db"
    assert "_overflow" not in str(db)


def test_long_paths_fall_back_to_overflow(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIX_SHARD_PATH_MAX", "120")
    deep_root = "C:/Users/max/AppData/Local/Temp/pytest-of-max/pytest-4764/" + \
        "test_sharded_pool_matches_seri0/rootA"
    db = corpus_shard_db(deep_root, "roota", tmp_path)
    assert db.parent == Path(tmp_path) / "_overflow"
    assert db.name.startswith("roota-")
    assert db.name.endswith(".genome.db")
    # label + 10-hex digest
    digest = db.name[len("roota-"):-len(".genome.db")]
    assert len(digest) == 10
    int(digest, 16)  # hex


def test_overflow_path_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIX_SHARD_PATH_MAX", "120")
    deep_root = "C:/Users/max/AppData/Local/Temp/very/deep/tree/rootA"
    a = corpus_shard_db(deep_root, "roota", tmp_path)
    b = corpus_shard_db(deep_root, "roota", tmp_path)
    assert a == b


def test_overflow_distinguishes_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIX_SHARD_PATH_MAX", "120")
    base = "C:/Users/max/AppData/Local/Temp/very/deep/tree/"
    a = corpus_shard_db(base + "rootA", "shard", tmp_path)
    b = corpus_shard_db(base + "rootB", "shard", tmp_path)
    assert a != b
    assert a.parent == b.parent  # both in _overflow


def test_cap_is_env_overridable(tmp_path, monkeypatch):
    deep_root = "C:/Users/max/AppData/Local/Temp/very/deep/tree/rootA"
    monkeypatch.setenv("HELIX_SHARD_PATH_MAX", "100000")
    db = corpus_shard_db(deep_root, "roota", tmp_path)
    assert "_overflow" not in str(db)
    monkeypatch.setenv("HELIX_SHARD_PATH_MAX", "1")
    db = corpus_shard_db(deep_root, "roota", tmp_path)
    assert "_overflow" in str(db)


def test_bad_env_value_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIX_SHARD_PATH_MAX", "not-a-number")
    db = corpus_shard_db(SHORT_ROOT, "helix", tmp_path)
    # default cap (240) — short mirrored path survives
    assert "_overflow" not in str(db) or len(str(db)) > 240
