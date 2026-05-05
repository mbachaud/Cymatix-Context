"""macOS MPS smoke test — runs on darwin only, and only outside GHA.

Loads a tiny cross-encoder tokenizer + model and does a 2-pair forward
pass on mps to catch MPS-branch regressions. Skipped on non-darwin
platforms (Linux / Windows) AND on GitHub Actions runners — see spec
``docs/specs/2026-05-04-hardware-detection-design.md`` §3 verification
posture: the GHA macos-14 runner's MPS shared pool can't accommodate
even small transformer forward passes (OOMs at ~1 GiB peak on every
attention-head model we tried, including a 22 MB MiniLM, even with
PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0). Re-enabling on CI is tracked as
a follow-up that needs a real Apple-Silicon dev rig to validate first.
Until then, the test scaffold is preserved so a maintainer with a Mac
can run it locally.
"""

from __future__ import annotations

import os
import sys

import pytest


def _mps_available() -> bool:
    try:
        import torch
    except ImportError:
        return False
    return (
        sys.platform == "darwin"
        and torch.backends.mps.is_available()
        and torch.backends.mps.is_built()
    )


def _on_gha() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


requires_mps_runtime = pytest.mark.skipif(
    not _mps_available() or _on_gha(),
    reason=(
        "Requires darwin + MPS-capable hardware AND not running on GHA "
        "(GHA macos-14 MPS shared pool OOMs even on tiny models — see "
        "spec §3 verification posture; tracked for re-enable when a real "
        "Mac dev rig is available)"
    ),
)


@requires_mps_runtime
@pytest.mark.requires_mps
def test_cross_encoder_two_pair_forward_pass_on_mps():
    """Load a small cross-encoder, run a 2-pair forward pass on mps, assert
    output shape == (2,). Catches MPS-branch regressions before they reach
    users."""
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    # MiniLM cross-encoder — BERT-style attention, ~22 MB. Initial plan
    # picked nli-deberta-v3-xsmall for spec §8.2 "deberta tokenizer"
    # faithfulness, but the GHA macos-14 runner's MPS shared pool is too
    # small for deberta-v2's disentangled attention (OOM at ~1 GiB peak
    # even with PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0). MiniLM exercises
    # the same MPS wiring on the available runner. Trade-off documented
    # in spec §3 verification posture.
    model_id = "cross-encoder/ms-marco-MiniLM-L-6-v2"  # ~22 MB, single-output
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForSequenceClassification.from_pretrained(model_id)
    model = model.to("mps")
    model.train(False)  # nn.Module inference mode — same effect as .eval()

    pairs_a = ["What is helix-context?", "How does the picker work?"]
    pairs_b = ["A retrieval system.", "It walks CUDA, ROCm, MPS, then CPU."]

    enc = tokenizer(
        pairs_a, pairs_b,
        padding=True, truncation=True, max_length=128, return_tensors="pt",
    ).to("mps")

    with torch.no_grad():
        out = model(**enc).logits

    # nli-deberta-v3-xsmall outputs (batch, 3) for the 3 NLI classes.
    # Allow (2, N) for any N >= 1 to remain robust if the model is
    # swapped for a single-output cross-encoder later.
    assert out.dim() == 2 and out.shape[0] == 2 and out.shape[1] >= 1, (
        f"Expected logits shape (2, N>=1); got {tuple(out.shape)!r}"
    )
    # Round-trip back to CPU to catch silent fallback / NaN-on-MPS issues
    cpu_logits = out.cpu()
    assert not torch.isnan(cpu_logits).any(), "NaN in MPS logits — dtype/op mismatch?"
