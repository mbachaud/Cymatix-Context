"""BGE-M3 codec tests (Step 4, 2026-05-08)."""
import numpy as np
import pytest
from unittest.mock import MagicMock, patch
from helix_context.bgem3_codec import BGEM3Codec


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
