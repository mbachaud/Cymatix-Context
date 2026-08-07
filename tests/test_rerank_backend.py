"""Unit tests for cymatix_context.backends.rerank_backend (mocked tokenizer + model).

Mock idiom adapted from tests/test_deberta_backend.py:64-104 (_FakeEncodings,
tokenizer/model mock builders). Adapted, not copied: the fake tokenizer here
records each CHUNK (the actual list of candidate texts) into a shared
``calls`` list rather than just its length, so callers can inspect both the
chunk sizes (``len(c)``) and the batching shape; the fake model reads the
most recent chunk from that shared list and returns an object whose
``.logits`` is a REAL ``torch.tensor([[v] for v in logits_chunk])`` (shape
``(n, 1)``, matching ms-marco) so ``torch.sigmoid(...).squeeze`` math runs for
real instead of being mocked away.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import torch

from cymatix_context import hardware
from cymatix_context.backends import rerank_backend


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    rerank_backend.reset_for_test()
    hardware.reset_for_test()          # same autouse reset test_layer_devices uses
    yield
    rerank_backend.reset_for_test()


class _FakeEncodings(dict):
    """Dict that supports .to(device) the way HF BatchEncoding does."""

    def to(self, device):  # noqa: D401 - mimic HF BatchEncoding
        return self


def _make_tokenizer_mock(calls):
    """Tokenizer mock that records each chunk (list of texts) it is called with."""
    tok = MagicMock()

    def _call(texts_a, texts_b, **kwargs):
        calls.append(list(texts_b))
        enc = _FakeEncodings(input_ids=torch.zeros(len(texts_b), 1, dtype=torch.long))
        return enc

    tok.side_effect = _call
    return tok


def _make_model_mock(calls, logits=None):
    """Model mock whose .logits is a real (n, 1) tensor for the latest chunk.

    Defaults to zeros for the most recently recorded chunk size when no
    explicit `logits` list is supplied; otherwise slices `logits` to match
    the running offset across successive chunk calls.
    """
    model = MagicMock()
    state = {"consumed": 0}

    def _call(**kwargs):
        chunk_len = len(calls[-1])
        if logits is not None:
            start = state["consumed"]
            chunk_logits = logits[start:start + chunk_len]
            state["consumed"] = start + chunk_len
        else:
            chunk_logits = [0.0] * chunk_len
        out = MagicMock()
        out.logits = torch.tensor([[v] for v in chunk_logits])
        return out

    model.side_effect = _call
    return model


def test_score_pairs_empty_is_empty_without_load():
    with patch.object(rerank_backend, "_get_scorer", side_effect=AssertionError("must not load")):
        assert rerank_backend.score_pairs("q", []) == []


def test_score_pairs_batches_by_recommended_size(monkeypatch):
    calls = []
    fake_tok, fake_model = _make_tokenizer_mock(calls), _make_model_mock(calls)  # SHARED list — model reads chunk sizes from it
    monkeypatch.setattr(rerank_backend, "_get_scorer", lambda model_name=None: (fake_tok, fake_model, "cpu"))
    monkeypatch.setattr(rerank_backend, "_recommended_batch", lambda: 2)
    scores = rerank_backend.score_pairs("q", ["a", "b", "c"])
    assert len(scores) == 3
    assert all(isinstance(s, float) for s in scores)
    assert [len(c) for c in calls] == [2, 1]      # chunked 2 + 1


def test_scores_preserve_negative_logit_ordering(monkeypatch):
    # sigmoid is monotone: raw logits [-4.0, -1.0, -9.0] must rank b > a > c
    calls = []
    fake_tok, fake_model = _make_tokenizer_mock(calls), _make_model_mock(calls, logits=[-4.0, -1.0, -9.0])
    monkeypatch.setattr(rerank_backend, "_get_scorer", lambda model_name=None: (fake_tok, fake_model, "cpu"))
    monkeypatch.setattr(rerank_backend, "_recommended_batch", lambda: 16)
    s = rerank_backend.score_pairs("q", ["a", "b", "c"])
    assert s[1] > s[0] > s[2]


def test_loader_uses_rerank_layer_device_and_records_model_load(monkeypatch):
    seen = {}

    def _fake_resolve(layer):
        seen["layer"] = layer
        return "cpu"          # NOT `setdefault(...) or "cpu"` — that returns "rerank" as the device

    monkeypatch.setattr(rerank_backend, "resolve_layer_device", _fake_resolve)
    recorded = []
    # record_model_load takes (name, SECONDS) — telemetry/otel.py:826 multiplies by 1000 itself.
    # Import happens inside _get_scorer as `from cymatix_context.telemetry import
    # record_model_load`, so we monkeypatch the source module attribute, not a
    # rerank_backend attribute.
    monkeypatch.setattr(
        "cymatix_context.telemetry.record_model_load",
        lambda name, seconds: recorded.append((name, seconds < 100)),
    )
    transformers = pytest.importorskip("transformers")
    with patch.object(transformers.AutoTokenizer, "from_pretrained", return_value=MagicMock()), \
         patch.object(transformers.AutoModelForSequenceClassification, "from_pretrained", return_value=MagicMock()):
        rerank_backend._get_scorer()
    assert seen["layer"] == "rerank"
    assert recorded == [("rerank", True)]   # sane-seconds check catches a ms-unit regression
