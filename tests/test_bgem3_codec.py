"""BGE-M3 codec tests (Step 4, 2026-05-08)."""
import sys

import numpy as np
import pytest
from unittest.mock import MagicMock, patch
from cymatix_context.backends.bgem3_codec import BGEM3Codec
from cymatix_context.backends import bgem3_codec as _codec_mod


def _make_codec_with_mock(dim=256):
    """Return a BGEM3Codec with a mocked sentence-transformers backend."""
    codec = BGEM3Codec(dim=dim)
    mock_model = MagicMock()
    # encode() returns a numpy array of shape (dim*2,) — will be truncated to dim
    mock_model.encode.return_value = np.ones(dim * 2, dtype=np.float32) * 0.5
    codec._model = mock_model
    codec._backend = "sentence_transformers"
    return codec


def test_encode_returns_correct_dim():
    codec = _make_codec_with_mock(dim=64)
    vec = codec.encode("what port does helix use?", task="query")
    assert len(vec) == 64


def test_encode_is_normalized():
    codec = _make_codec_with_mock(dim=32)
    vec = codec.encode("some text", task="passage")
    arr = np.array(vec)
    assert abs(np.linalg.norm(arr) - 1.0) < 1e-5, "Vector must be L2-normalized"


def test_encode_query_prepends_prefix():
    codec = _make_codec_with_mock(dim=32)
    codec.encode("test query", task="query")
    call_arg = codec._model.encode.call_args[0][0]
    assert call_arg.startswith("Represent this sentence")


def test_encode_passage_no_prefix():
    codec = _make_codec_with_mock(dim=32)
    codec.encode("test passage", task="passage")
    call_arg = codec._model.encode.call_args[0][0]
    assert not call_arg.startswith("Represent")


def test_similarity_identical_vectors():
    codec = BGEM3Codec(dim=4)
    vec = [0.5, 0.5, 0.5, 0.5]
    # Normalized
    import math
    n = math.sqrt(4 * 0.25)
    vec_n = [v / n for v in vec]
    assert abs(codec.similarity(vec_n, vec_n) - 1.0) < 1e-5


def test_similarity_orthogonal_vectors():
    codec = BGEM3Codec(dim=4)
    a = [1.0, 0.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0, 0.0]
    assert abs(codec.similarity(a, b)) < 1e-5


# ── VRAM cache release (bounds CUDA caching-allocator growth during batch ingest) ──

def _make_batch_codec(dim=64, device="cpu", n_rows=3):
    """A BGEM3Codec wired to a mocked sentence-transformers backend for encode_batch."""
    codec = BGEM3Codec(dim=dim, device=device)
    mock_model = MagicMock()
    mock_model.encode.return_value = np.ones((n_rows, dim * 2), dtype=np.float32) * 0.5
    codec._model = mock_model
    codec._backend = "sentence_transformers"
    return codec


def test_vram_release_interval_default(monkeypatch):
    monkeypatch.delenv("HELIX_DENSE_VRAM_RELEASE_EVERY", raising=False)
    assert _codec_mod._vram_release_interval() == 256


def test_vram_release_interval_custom(monkeypatch):
    monkeypatch.setenv("HELIX_DENSE_VRAM_RELEASE_EVERY", "10")
    assert _codec_mod._vram_release_interval() == 10


def test_vram_release_interval_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("HELIX_DENSE_VRAM_RELEASE_EVERY", "not-an-int")
    assert _codec_mod._vram_release_interval() == 256


def test_encode_batch_counts_calls():
    codec = _make_batch_codec()
    assert codec._encode_batch_calls == 0
    codec.encode_batch(["a", "b", "c"])
    codec.encode_batch(["d"])
    assert codec._encode_batch_calls == 2


def test_cpu_device_never_releases(monkeypatch):
    """CPU encoding has no CUDA cache: empty_cache must never be reached."""
    codec = _make_batch_codec(device="cpu")
    fake_torch = MagicMock()
    monkeypatch.setenv("HELIX_DENSE_VRAM_RELEASE_EVERY", "1")
    with patch.dict(sys.modules, {"torch": fake_torch}):
        codec.encode_batch(["x"])
    fake_torch.cuda.empty_cache.assert_not_called()


def test_cuda_releases_on_interval(monkeypatch):
    """On CUDA, empty_cache fires exactly every N batched encodes, not per call."""
    codec = _make_batch_codec(device="cuda")
    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = True
    monkeypatch.setenv("HELIX_DENSE_VRAM_RELEASE_EVERY", "2")
    with patch.dict(sys.modules, {"torch": fake_torch}):
        codec.encode_batch(["a"])  # call 1: 1 % 2 != 0 -> no release
        assert fake_torch.cuda.empty_cache.call_count == 0
        codec.encode_batch(["b"])  # call 2: 2 % 2 == 0 -> release
        assert fake_torch.cuda.empty_cache.call_count == 1
        codec.encode_batch(["c"])  # call 3: no release
        codec.encode_batch(["d"])  # call 4: release
        assert fake_torch.cuda.empty_cache.call_count == 2


def test_cuda_release_disabled_with_zero(monkeypatch):
    """HELIX_DENSE_VRAM_RELEASE_EVERY=0 disables the release entirely."""
    codec = _make_batch_codec(device="cuda")
    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = True
    monkeypatch.setenv("HELIX_DENSE_VRAM_RELEASE_EVERY", "0")
    with patch.dict(sys.modules, {"torch": fake_torch}):
        for t in "abcd":
            codec.encode_batch([t])
    fake_torch.cuda.empty_cache.assert_not_called()


def test_encode_batch_vectors_unchanged_by_release(monkeypatch):
    """The release is byte-neutral: vectors with release on == vectors with it off."""
    monkeypatch.setenv("HELIX_DENSE_VRAM_RELEASE_EVERY", "0")
    off = _make_batch_codec(device="cpu").encode_batch(["a", "b", "c"])
    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = True
    monkeypatch.setenv("HELIX_DENSE_VRAM_RELEASE_EVERY", "1")
    with patch.dict(sys.modules, {"torch": fake_torch}):
        on = _make_batch_codec(device="cuda").encode_batch(["a", "b", "c"])
    assert np.allclose(np.array(off), np.array(on))
