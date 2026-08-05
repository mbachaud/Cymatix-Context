"""Per-layer encoder device knobs (2026-08-04 CUDA A/B enablement).

The encoder A/B needs each layer independently pinnable — GPU for fast
benchmarking, CPU as the cheaper-to-operate default — instead of the
single global [hardware] device. Four knobs, all defaulting "auto"
(= inherit the global hardware resolution, i.e. today's behavior):

    [hardware]
    dense_device  = "auto"   # BGE-M3 (was silently CPU-pinned — see below)
    splade_device = "auto"
    sema_device   = "auto"
    rerank_device = "auto"   # consumed when the pre-cap rerank lands

Bug these tests also pin: _get_dense_codec never passed a device, so
BGEM3Codec fell to its "cpu" ctor default and dense NEVER used the GPU
even under a CUDA torch with [hardware] device="cuda".
"""

from __future__ import annotations

import pytest

from cymatix_context import hardware
from cymatix_context.config import Hardware, load_config


@pytest.fixture(autouse=True)
def _reset_hardware():
    hardware.reset_for_test()
    yield
    hardware.reset_for_test()


# ── config surface ───────────────────────────────────────────────────


def test_hardware_config_layer_defaults():
    hw = Hardware()
    assert hw.dense_device == "auto"
    assert hw.splade_device == "auto"
    assert hw.sema_device == "auto"
    assert hw.rerank_device == "auto"


def test_layer_devices_load_from_toml(tmp_path):
    p = tmp_path / "cymatix.toml"
    p.write_text(
        "[hardware]\ndevice = \"cpu\"\ndense_device = \"cuda\"\n"
        "sema_device = \"cpu\"\n",
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    assert cfg.hardware.dense_device == "cuda"
    assert cfg.hardware.sema_device == "cpu"
    assert cfg.hardware.splade_device == "auto"


# ── resolution semantics ─────────────────────────────────────────────


def test_auto_layer_inherits_global_device():
    hardware.init_from_config(config_device="cpu")
    assert hardware.resolve_layer_device("dense") == "cpu"


def test_explicit_cpu_layer_is_honored():
    hardware.init_from_config(
        config_device="cpu", layer_devices={"dense": "cpu"}
    )
    assert hardware.resolve_layer_device("dense") == "cpu"


def test_unavailable_explicit_device_falls_back_to_cpu():
    """Pinning a layer to cuda on a box whose torch has no CUDA must
    degrade to cpu with a warning, never crash. (This test box runs the
    +cpu torch wheel, so cuda genuinely probes false here.)"""
    import torch

    if torch.cuda.is_available():
        pytest.skip("box has CUDA; fallback path not exercisable")
    hardware.init_from_config(
        config_device="cpu", layer_devices={"splade": "cuda"}
    )
    assert hardware.resolve_layer_device("splade") == "cpu"


def test_unknown_layer_inherits_global():
    hardware.init_from_config(config_device="cpu")
    assert hardware.resolve_layer_device("nonexistent-layer") == "cpu"


# ── consumer plumbing ────────────────────────────────────────────────


def test_dense_codec_receives_resolved_device():
    """_get_dense_codec must pass the per-layer resolved device to
    get_shared_codec (it previously passed nothing → ctor 'cpu' pin, so
    dense never used the GPU even under a CUDA torch).

    Source-text gate because the autouse conftest ``_stub_dense_codec``
    fixture replaces KnowledgeStore._get_dense_codec wholesale in every
    non-live test — the real method body is unreachable here. (Behavior
    verified live: get_shared_codec receives device='cpu' when the dense
    layer resolves to cpu.)
    """
    from pathlib import Path

    import cymatix_context.knowledge_store as ks_mod

    src = Path(ks_mod.__file__).read_text(encoding="utf-8")
    assert 'device=resolve_layer_device("dense")' in src, (
        "_get_dense_codec no longer passes the per-layer resolved device "
        "to get_shared_codec"
    )


def test_splade_and_sema_consume_layer_resolution():
    """Source gate: the splade loader and SEMA auto-branch must resolve
    through resolve_layer_device, not the raw global."""
    import inspect

    from cymatix_context.backends import sema, splade_backend

    assert "resolve_layer_device" in inspect.getsource(splade_backend)
    assert "resolve_layer_device" in inspect.getsource(sema)
