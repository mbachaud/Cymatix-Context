"""Unit tests for cymatix_context.deberta_backend (mocked tokenizer + model).

Exists separately from tests/test_ribosome.py because that file tests the
Ollama-backed Ribosome class with MockBackend, not the DeBERTa cross-encoder
backend. The chunked-batch tests below pin that re_rank/splice consume
recommended_batch_size from the hardware module instead of one-shot
tokenizing all pairs.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import torch

from cymatix_context import hardware
from cymatix_context.schemas import Gene, PromoterTags


@pytest.fixture(autouse=True)
def _reset(reset_hardware_cache):
    yield


def _override_hardware(monkeypatch, *, rerank_batch=None, splice_batch=None):
    """Force hardware singleton onto cpu with specific batch overrides."""
    overrides = {}
    if rerank_batch is not None:
        overrides["rerank"] = rerank_batch
    if splice_batch is not None:
        overrides["splice"] = splice_batch
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


def _make_gene(gene_id: str, *, content: str = "hello world",
               summary: str = "summary text", domains=None, codons=None) -> Gene:
    """Build a minimal Gene that satisfies re_rank's pair-builder.

    re_rank reads g.promoter.summary and g.promoter.domains; splice reads
    g.codons. Other Gene fields fall back to schema defaults.
    """
    return Gene(
        gene_id=gene_id,
        content=content,
        complement=content[:80],
        codons=codons if codons is not None else ["c1", "c2"],
        promoter=PromoterTags(summary=summary, domains=domains or []),
    )


class _FakeEncodings(dict):
    """Dict that supports .to(device) the way HF BatchEncoding does."""

    def to(self, device):  # noqa: D401 - mimic HF BatchEncoding
        return self


def _make_tokenizer_mock(scores_per_call):
    """Tokenizer mock whose return value tracks the chunk size for the model.

    Each call returns a fresh _FakeEncodings; we record the chunk size on
    the encodings object so the model mock can produce a logits tensor of
    matching length.
    """
    tok = MagicMock()

    def _call(texts_a, texts_b, **kwargs):
        # Record chunk length so the model knows how many scores to emit.
        chunk_len = len(texts_a)
        scores_per_call.append(chunk_len)
        enc = _FakeEncodings(input_ids=torch.zeros(chunk_len, 1, dtype=torch.long))
        return enc

    tok.side_effect = _call
    return tok


def _make_model_mock(scores_per_call):
    """Model mock that emits zeros(chunk_len) so re_rank's clamp/tolist works."""
    model = MagicMock()

    def _call(**kwargs):
        # The most recent tokenizer call recorded its chunk_len; use it.
        chunk_len = scores_per_call[-1]
        out = MagicMock()
        out.logits = torch.zeros(chunk_len)
        return out

    model.side_effect = _call
    return model


def test_deberta_rerank_chunks_in_recommended_batch_size(monkeypatch):
    """Force batch=16 via override; feed 100 candidates; expect 7 tokenizer
    calls (6 full + 1 partial of 4)."""
    _override_hardware(monkeypatch, rerank_batch=16)

    from cymatix_context.backends.deberta_backend import DeBERTaRibosome

    rib = DeBERTaRibosome.__new__(DeBERTaRibosome)  # bypass __init__
    rib._device = torch.device("cpu")
    rib._rerank_pretrained = False

    chunk_log: list[int] = []
    rib._rerank_tokenizer = _make_tokenizer_mock(chunk_log)
    rib._rerank_model = _make_model_mock(chunk_log)

    candidates = [_make_gene(f"g_{i}") for i in range(100)]
    rib.re_rank("query", candidates, k=10)

    # 100 candidates / 16 batch = 6 full + 1 partial = 7 tokenizer calls.
    assert rib._rerank_tokenizer.call_count == 7
    # Chunk sizes recorded in order: six 16s + one 4.
    assert chunk_log == [16, 16, 16, 16, 16, 16, 4]


def test_deberta_splice_chunks_in_recommended_batch_size(monkeypatch):
    """Force batch=16 via splice override; feed 50 codons across 5 genes;
    expect ceil(50/16) = 4 tokenizer calls (16 + 16 + 16 + 2)."""
    _override_hardware(monkeypatch, splice_batch=16)

    from cymatix_context.backends.deberta_backend import DeBERTaRibosome

    rib = DeBERTaRibosome.__new__(DeBERTaRibosome)  # bypass __init__
    rib._device = torch.device("cpu")
    rib.splice_threshold = 0.5

    chunk_log: list[int] = []
    rib._splice_tokenizer = _make_tokenizer_mock(chunk_log)
    rib._splice_model = _make_model_mock(chunk_log)

    # 5 genes x 10 codons = 50 (query, codon) pairs.
    genes = [
        _make_gene(f"g_{gi}", codons=[f"codon_{gi}_{ci}" for ci in range(10)])
        for gi in range(5)
    ]
    rib.splice("query", genes)

    assert rib._splice_tokenizer.call_count == 4
    assert chunk_log == [16, 16, 16, 2]


def test_deberta_rerank_preserves_ordering_of_negative_logits(monkeypatch):
    """Cross-encoder logits are commonly negative (or > 1). Squashing must
    be monotone — the old clamp to [0, 1] collapsed all negatives to 0.0,
    destroying ordering among them (bugbash BUG-3)."""
    _override_hardware(monkeypatch, rerank_batch=16)

    from cymatix_context.backends.deberta_backend import DeBERTaRibosome

    rib = DeBERTaRibosome.__new__(DeBERTaRibosome)  # bypass __init__
    rib._device = torch.device("cpu")
    rib._rerank_pretrained = True  # pure score-sorted path, no position bonus

    chunk_log: list[int] = []
    rib._rerank_tokenizer = _make_tokenizer_mock(chunk_log)

    # MS MARCO-style raw logits: g2 most relevant, then g1, then g0.
    fixed_logits = torch.tensor([-4.0, -1.0, 3.0])
    model = MagicMock()

    def _call(**kwargs):
        out = MagicMock()
        out.logits = fixed_logits.clone()
        return out

    model.side_effect = _call
    rib._rerank_model = model

    candidates = [_make_gene("g0"), _make_gene("g1"), _make_gene("g2")]
    top = rib.re_rank("query", candidates, k=2)

    assert [g.gene_id for g in top] == ["g2", "g1"], (
        "negative logits collapsed — ordering not preserved"
    )


def test_deberta_init_consults_get_hardware_when_device_none(monkeypatch):
    """Passing device=None must defer to hardware.get_hardware().device.

    We don't actually load tokenizers/models — we monkeypatch the
    transformers loaders to return inert mocks and verify the resulting
    self._device matches the hardware singleton. Skips when transformers
    isn't installed (ribosome backend can still be unit-tested without it
    via the chunking tests above, which bypass __init__).
    """
    pytest.importorskip("transformers")

    _override_hardware(monkeypatch, rerank_batch=8)

    import cymatix_context.backends.deberta_backend as db

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

    rib = db.DeBERTaRibosome(
        rerank_model_path="x",
        splice_model_path="y",
        nli_model_path="z",
        device=None,
    )
    assert str(rib._device) == "cpu"
