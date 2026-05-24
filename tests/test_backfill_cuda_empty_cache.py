"""Regression for the CUDA-allocator rate-decay pathology observed
during the 500K and xl-sharded build attempts on 2026-05-23/24.

Symptom (Factorio shard at xl --mode sharded, 2026-05-24 12:00-13:04):
- ingest at 22-30 g/s (healthy)
- dense backfill starts at 22 g/s
- after ~3,500 rows: 3.8 g/s
- after ~4,000 rows: 1.2 g/s (and dropping)
- GPU: 12,048 / 12,288 MiB used (99 %) — PyTorch's caching allocator
  has accumulated fragmented memory across ~60 `encode_batch` calls

Same pattern at 500K scale (slack shard): 27 g/s → 0.3 g/s over 11 h.

Fix: periodically call ``torch.cuda.empty_cache()`` inside the batch
loop so the allocator's freed-but-cached memory is returned to the
GPU's global pool, preventing fragmentation buildup.

These tests pin:

1. ``empty_cache`` is called at the expected cadence (every
   ``cuda_empty_cache_every`` batches, default once per ~1 K rows).
2. The cadence is configurable (parameter + env-var-friendly default).
3. The call is no-op-safe on a CPU-only or torch-missing environment
   (i.e., the backfill does not crash without CUDA).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


# --- helpers ------------------------------------------------------------


def _make_seeded_db(tmp_path, n_genes: int, content_each: str = "alpha beta gamma"):
    """Build a tiny SQLite genome with `n_genes` rows of NULL
    embedding_dense_v2 ready to be backfilled."""
    p = tmp_path / "seed.db"
    conn = sqlite3.connect(str(p))
    # Minimal genes-table shape that satisfies _ensure_v2_schema's
    # partial-index DDL (needs `chromatin` column) and the backfill
    # SELECT/UPDATE. embedding_dense_v2 starts NULL so all rows are
    # eligible.
    conn.execute(
        "CREATE TABLE genes ("
        "gene_id TEXT PRIMARY KEY, content TEXT, "
        "chromatin INTEGER DEFAULT 0, "
        "embedding_dense_v2 BLOB"
        ")"
    )
    for i in range(n_genes):
        conn.execute(
            "INSERT INTO genes (gene_id, content, chromatin) VALUES (?, ?, ?)",
            (f"g{i:04d}", content_each, 0),
        )
    conn.commit()
    conn.close()
    return p


class _FakeCodec:
    """Bytes-correct fake codec — returns deterministic dim-length vectors
    so the backfill's per-row UPDATE path runs to completion."""

    def __init__(self, dim: int = 1024):
        self.dim = dim
        self.encode_batch_calls = 0

    def encode_batch(self, texts, task="passage"):
        self.encode_batch_calls += 1
        # Each row gets the same unit vector — exact value doesn't matter,
        # only that vec_to_blob accepts it.
        return [[1.0 / self.dim**0.5] * self.dim for _ in texts]


# --- tests --------------------------------------------------------------


def test_backfill_calls_cuda_empty_cache_at_expected_cadence(tmp_path):
    """When CUDA is available, ``backfill_dense_db`` should call
    ``torch.cuda.empty_cache()`` once per ``cuda_empty_cache_every``
    batches. With default cadence 16 batches and batch=4 (64 rows in
    real default but 4 makes the test tight), processing 16 batches
    triggers exactly one empty_cache call."""
    import backfill_bgem3_v2 as bb

    db = _make_seeded_db(tmp_path, n_genes=64)  # 64 rows / batch=4 = 16 batches
    codec = _FakeCodec(dim=1024)

    fake_cuda = MagicMock()
    fake_cuda.is_available.return_value = True

    fake_torch = MagicMock()
    fake_torch.cuda = fake_cuda

    with patch.dict(sys.modules, {"torch": fake_torch}):
        bb.backfill_dense_db(
            str(db),
            dim=1024,
            batch=4,
            codec=codec,
            log_fn=lambda _: None,
            cuda_empty_cache_every=16,
        )

    # 64 rows / 4 batch = 16 encode_batch calls
    assert codec.encode_batch_calls == 16
    # one empty_cache trigger per 16 batches
    assert fake_cuda.empty_cache.call_count == 1, (
        f"expected empty_cache called once per 16 batches, "
        f"got {fake_cuda.empty_cache.call_count}"
    )


def test_backfill_empty_cache_skipped_when_cuda_unavailable(tmp_path):
    """If ``torch.cuda.is_available()`` is False (or torch is missing),
    the backfill must not call ``empty_cache`` — but it also must not
    crash."""
    import backfill_bgem3_v2 as bb

    db = _make_seeded_db(tmp_path, n_genes=32)
    codec = _FakeCodec(dim=1024)

    fake_cuda = MagicMock()
    fake_cuda.is_available.return_value = False

    fake_torch = MagicMock()
    fake_torch.cuda = fake_cuda

    with patch.dict(sys.modules, {"torch": fake_torch}):
        bb.backfill_dense_db(
            str(db), dim=1024, batch=4, codec=codec,
            log_fn=lambda _: None,
            cuda_empty_cache_every=4,
        )

    fake_cuda.empty_cache.assert_not_called()


def test_backfill_empty_cache_cadence_configurable(tmp_path):
    """The cadence is a user-tunable knob: every-N-batches. With cadence=4
    and 16 batches, empty_cache fires 4 times."""
    import backfill_bgem3_v2 as bb

    db = _make_seeded_db(tmp_path, n_genes=64)  # 64 / 4 = 16 batches
    codec = _FakeCodec(dim=1024)

    fake_cuda = MagicMock()
    fake_cuda.is_available.return_value = True

    fake_torch = MagicMock()
    fake_torch.cuda = fake_cuda

    with patch.dict(sys.modules, {"torch": fake_torch}):
        bb.backfill_dense_db(
            str(db), dim=1024, batch=4, codec=codec,
            log_fn=lambda _: None,
            cuda_empty_cache_every=4,
        )

    assert fake_cuda.empty_cache.call_count == 4


def test_get_or_create_codec_caches_across_calls():
    """Cross-shard codec caching — second sharded call must reuse the
    first call's codec instance, not create a new BGE-M3 model.

    Story (2026-05-24): xl --mode sharded ran factorio + projects +
    beamng-drive + spaceengineers2 cleanly with the per-batch
    empty_cache fix above, then dyson-sphere-program (379 genes only)
    stalled at 0.03 g/s. Diagnosis: each shard called `_backfill_dense`
    which built a fresh BGE-M3 codec → each model load added ~2-3 GB to
    GPU; by shard 5 the allocator was 11.9 / 12.0 GB. Old codecs were
    GC-eligible but the freed memory wasn't returned to GPU pool.

    Fix: cache one codec at module level, reuse across shards.
    """
    import build_fixture_matrix as bfm

    # Reset cache so the test starts clean.
    bfm._cached_codec = None

    sentinel = object()

    def _factory(dim, device):
        return sentinel

    c1 = bfm._get_or_create_codec(dim=1024, _factory=_factory)
    c2 = bfm._get_or_create_codec(dim=1024, _factory=_factory)
    assert c1 is sentinel
    assert c1 is c2, "second call must reuse the first call's codec"


def test_release_gpu_state_no_throw_when_torch_missing():
    """The cross-shard cleanup helper must be safe on a torch-missing /
    CPU-only host — must not raise."""
    import build_fixture_matrix as bfm
    import builtins
    real_import = builtins.__import__

    def boom(name, *args, **kwargs):
        if name == "torch":
            raise ImportError("simulated torch missing")
        return real_import(name, *args, **kwargs)

    from unittest.mock import patch
    with patch("builtins.__import__", side_effect=boom):
        # Must not raise.
        bfm._release_gpu_state()


def test_backfill_does_not_crash_when_torch_import_fails(tmp_path):
    """If ``import torch`` raises (e.g., torch missing entirely), the
    backfill must continue without empty_cache — this preserves the
    'graceful CPU fallback' posture documented in the codec init."""
    import backfill_bgem3_v2 as bb

    db = _make_seeded_db(tmp_path, n_genes=16)
    codec = _FakeCodec(dim=1024)

    # Force `import torch` inside the backfill to raise.
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def boom_import(name, *args, **kwargs):
        if name == "torch":
            raise ImportError("simulated torch missing")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=boom_import):
        # Must complete without raising.
        result = bb.backfill_dense_db(
            str(db), dim=1024, batch=4, codec=codec,
            log_fn=lambda _: None,
            cuda_empty_cache_every=2,
        )

    # Should have produced a valid coverage report; the backfill finished.
    assert result["rows_processed"] == 16
