"""Opt-in real-hardware CUDA test.

Skipped by default. Set ``HELIX_TEST_CUDA=1`` on a host with CUDA-built
torch + NVIDIA GPU to exercise the full picker against real hardware.
Mocked CUDA tests in ``tests/test_hardware.py`` cover the wiring; this
validates ``torch.cuda.mem_get_info``, ``get_device_name``, and the probe
round-trip on actual silicon. See spec
``docs/specs/2026-05-04-hardware-detection-design.md`` §8.3.
"""

from __future__ import annotations

import os

import pytest

from helix_context import hardware

requires_real_cuda = pytest.mark.skipif(
    os.environ.get("HELIX_TEST_CUDA") != "1",
    reason="Set HELIX_TEST_CUDA=1 on a CUDA-capable host to enable",
)


@pytest.fixture(autouse=True)
def _reset_hardware_cache():
    hardware.reset_for_test()
    yield
    hardware.reset_for_test()


@requires_real_cuda
@pytest.mark.requires_real_cuda
def test_cuda_auto_picker_lands_on_cuda():
    """On a CUDA-capable host with HELIX_TEST_CUDA=1, auto-mode resolves
    to a cuda:N device with positive vram_total_gb and a non-empty device_name."""
    info = hardware.get_hardware()
    assert info.device_type == "cuda", f"Expected cuda, got {info.device_type!r}"
    assert info.device.startswith("cuda:"), f"Expected cuda:N, got {info.device!r}"
    assert info.vram_total_gb is not None and info.vram_total_gb > 0, (
        f"vram_total_gb={info.vram_total_gb!r}; expected positive float on real hardware"
    )
    assert info.device_name and isinstance(info.device_name, str), (
        f"device_name={info.device_name!r}; expected non-empty string"
    )
    assert info.fallback_reason is None, (
        f"Unexpected fallback: {info.fallback_reason!r}"
    )


@requires_real_cuda
@pytest.mark.requires_real_cuda
def test_cuda_recommended_batch_size_is_positive():
    """recommended_batch_size returns a positive int for known models on cuda."""
    info = hardware.get_hardware()
    assert info.device_type == "cuda"
    for model in ("rerank", "splice", "splade", "nli"):
        bs = hardware.recommended_batch_size(model)
        assert isinstance(bs, int) and bs > 0, (
            f"recommended_batch_size({model!r})={bs!r}; expected positive int"
        )
