"""Unit tests for helix_context.nli_backend (mocked tokenizer + model).

Parallel to tests/test_deberta_backend.py — the chunked-batch test pins that
NLIClassifier.classify_batch consumes recommended_batch_size('nli') from the
hardware module instead of one-shot tokenizing all pairs. The init test pins
that device=None defers to the hardware singleton.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import torch

from helix_context import hardware
from helix_context.schemas import NLRelation


@pytest.fixture(autouse=True)
def _reset_hardware_cache():
    hardware.reset_for_test()
    yield
    hardware.reset_for_test()


def _override_hardware(monkeypatch, *, nli_batch=None):
    """Force hardware singleton onto cpu with a specific nli batch override."""
    overrides = {}
    if nli_batch is not None:
        overrides["nli"] = nli_batch
    info = hardware.HardwareInfo(
        device="cpu",
        device_type="cpu",
        device_name="test",
        vram_total_gb=None,
        vram_free_gb=None,
        cpu_arch="x86_64",
        cpu_brand="test",
        system_ram_gb=16.0,
        requested_device="auto",
        fallback_reason=None,
        batch_size_overrides=overrides,
    )
    monkeypatch.setattr(hardware, "_detect", lambda: info)


class _FakeEncodings(dict):
    """Dict that supports .to(device) the way HF BatchEncoding does."""

    def to(self, device):  # noqa: D401 - mimic HF BatchEncoding
        return self


def _make_tokenizer_mock(chunk_log):
    """Tokenizer mock whose return value tracks the chunk size for the model.

    Each call records len(texts_a) so the model mock knows how many rows of
    7-class logits to emit.
    """
    tok = MagicMock()

    def _call(texts_a, texts_b, **kwargs):
        chunk_len = len(texts_a)
        chunk_log.append(chunk_len)
        enc = _FakeEncodings(input_ids=torch.zeros(chunk_len, 1, dtype=torch.long))
        return enc

    tok.side_effect = _call
    return tok


def _make_model_mock(chunk_log):
    """Model mock that emits (chunk_len, 7) logits matching the 7 NLI classes."""
    model = MagicMock()

    def _call(**kwargs):
        chunk_len = chunk_log[-1]
        # 7 classes: ENTAILMENT, REVERSE_ENTAILMENT, EQUIVALENCE,
        # ALTERNATION, NEGATION, COVER, INDEPENDENCE.
        out = MagicMock()
        out.logits = torch.zeros(chunk_len, 7)
        return out

    model.side_effect = _call
    return model


def test_nli_classify_batch_chunks_in_recommended_batch_size(monkeypatch):
    """Force nli batch=16 via override; feed 50 pairs; expect ceil(50/16)=4
    tokenizer calls (16 + 16 + 16 + 2)."""
    _override_hardware(monkeypatch, nli_batch=16)

    from helix_context.nli_backend import NLIClassifier

    clf = NLIClassifier.__new__(NLIClassifier)  # bypass __init__
    clf._device = torch.device("cpu")

    chunk_log: list[int] = []
    clf._tokenizer = _make_tokenizer_mock(chunk_log)
    clf._model = _make_model_mock(chunk_log)

    pairs = [(f"text_a_{i}", f"text_b_{i}") for i in range(50)]
    results = clf.classify_batch(pairs)

    assert clf._tokenizer.call_count == 4
    assert chunk_log == [16, 16, 16, 2]
    # Result length matches input length, and items unpack into (NLRelation, float).
    assert len(results) == 50
    rel, conf = results[0]
    assert isinstance(rel, NLRelation)
    assert isinstance(conf, float)


def test_nli_classify_batch_empty_returns_empty(monkeypatch):
    """Empty input must short-circuit before consulting hardware."""
    _override_hardware(monkeypatch, nli_batch=16)

    from helix_context.nli_backend import NLIClassifier

    clf = NLIClassifier.__new__(NLIClassifier)
    clf._device = torch.device("cpu")
    clf._tokenizer = MagicMock()
    clf._model = MagicMock()

    assert clf.classify_batch([]) == []
    assert clf._tokenizer.call_count == 0
    assert clf._model.call_count == 0


def test_nli_init_consults_get_hardware_when_device_none(monkeypatch):
    """Passing device=None must defer to hardware.get_hardware().device.

    We monkeypatch transformers loaders to return inert mocks and verify the
    resulting self._device matches the hardware singleton. Skips when
    transformers isn't installed (the chunking tests above bypass __init__).
    """
    pytest.importorskip("transformers")

    _override_hardware(monkeypatch, nli_batch=8)

    import helix_context.nli_backend as nb

    monkeypatch.setattr(
        "transformers.AutoTokenizer.from_pretrained",
        lambda *a, **kw: MagicMock(),
    )

    fake_model = MagicMock()
    fake_model.to = lambda dev: fake_model
    fake_model.train = lambda flag: None
    monkeypatch.setattr(
        "transformers.AutoModelForSequenceClassification.from_pretrained",
        lambda *a, **kw: fake_model,
    )

    clf = nb.NLIClassifier(model_path="x", device=None)
    assert str(clf._device) == "cpu"
