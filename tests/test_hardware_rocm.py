"""Opt-in real-hardware ROCm test.

Skipped by default. Set ``HELIX_TEST_ROCM=1`` and run on a Linux host with
ROCm-built torch (``torch.version.hip is not None``) + AMD GPU passthrough
to exercise the full picker against real hardware. See spec
``docs/specs/2026-05-04-hardware-detection-design.md`` §8.3 for rationale.
"""

from __future__ import annotations

import os

import pytest

from helix_context import hardware

requires_rocm = pytest.mark.skipif(
    os.environ.get("HELIX_TEST_ROCM") != "1",
    reason="Set HELIX_TEST_ROCM=1 on a ROCm-capable host to enable",
)


@pytest.fixture(autouse=True)
def _reset_hardware_cache():
    hardware.reset_for_test()
    yield
    hardware.reset_for_test()


@requires_rocm
@pytest.mark.requires_rocm
def test_rocm_auto_picker_lands_on_rocm():
    """On a ROCm-capable host with HELIX_TEST_ROCM=1, auto-mode resolves
    to a rocm:N device with positive vram_total_gb and no fallback."""
    info = hardware.get_hardware()
    assert info.device_type == "rocm", f"Expected rocm, got {info.device_type!r}"
    assert info.device.startswith("rocm:"), f"Expected rocm:N, got {info.device!r}"
    assert info.vram_total_gb is not None and info.vram_total_gb > 0, (
        f"vram_total_gb={info.vram_total_gb!r}; expected positive float on real hardware"
    )
    assert info.fallback_reason is None, (
        f"Unexpected fallback: {info.fallback_reason!r}"
    )


@requires_rocm
@pytest.mark.requires_rocm
def test_rocm_recommended_batch_size_is_positive():
    """recommended_batch_size returns a positive int for known models on rocm."""
    info = hardware.get_hardware()
    assert info.device_type == "rocm"
    for model in ("rerank", "splice", "splade", "nli"):
        bs = hardware.recommended_batch_size(model)
        assert isinstance(bs, int) and bs > 0, (
            f"recommended_batch_size({model!r})={bs!r}; expected positive int"
        )
