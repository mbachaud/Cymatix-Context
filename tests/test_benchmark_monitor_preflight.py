"""Unit tests for benchmark_monitor.py preflight gating.

Pin two behaviors:

  1. ASK_PROXY=0 retrieval-only benches never touch Ollama, so the Ollama
     reachability / benchmark-model-loaded / unauthorized-models checks
     must be skipped when ``ask_proxy=False``. The previous unconditional
     gating made retrieval-quality runs impossible whenever Ollama was
     down (which is the normal state when measuring /context only).

  2. The /health timeout used in preflight is configurable and defaults
     to 15s (raised from 5s). A freshly hot-swapped genome via
     ``POST /admin/swap-db`` returns 200 on the swap but the immediately
     following /health on the warming store can exceed 5s. 15s rides
     out the cache-warm without false-positive aborts.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

# Make benchmarks/ importable without packaging it
BENCH_DIR = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH_DIR))

from benchmark_monitor import BenchmarkMonitor  # noqa: E402


def _make_monitor(
    tmp_path: Path,
    *,
    ask_proxy: bool = True,
    health_timeout_s: float | None = None,
    **kwargs,
) -> BenchmarkMonitor:
    """Construct a monitor with a minimal on-disk genome fixture."""
    genome_path = tmp_path / "fake_genome.db"
    genome_path.write_bytes(b"")  # only needs to exist for the genome-snapshot gate
    incremental = tmp_path / "incremental.jsonl"
    incremental.write_text("")
    extra: dict = {}
    if health_timeout_s is not None:
        extra["health_timeout_s"] = health_timeout_s
    return BenchmarkMonitor(
        benchmark_model="qwen3:4b",
        incremental_output_path=str(incremental),
        total_needles=10,
        genome_snapshot_path=str(genome_path),
        ask_proxy=ask_proxy,
        **extra,
        **kwargs,
    )


def _stub_helix_only(monitor: BenchmarkMonitor) -> dict:
    """Replace monitor._client.get: helix /health → 200, everything else → ConnectError.

    Returns a capture dict; ``health_timeouts`` records the ``timeout=`` kwarg
    passed to each /health call.
    """
    captured: dict = {"health_timeouts": [], "urls": []}

    def fake_get(url, **kwargs):  # noqa: ANN001
        captured["urls"].append(url)
        if "/health" in url:
            captured["health_timeouts"].append(kwargs.get("timeout"))
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"genes": 0, "ribosome": "ok"}
            return resp
        raise httpx.ConnectError(f"unreachable: {url}")

    monitor._client.get = fake_get  # type: ignore[method-assign]
    return captured


def test_preflight_skips_ollama_when_ask_proxy_false(tmp_path: Path) -> None:
    """ask_proxy=False: unreachable Ollama must not fail preflight."""
    monitor = _make_monitor(tmp_path, ask_proxy=False)
    _stub_helix_only(monitor)

    assert monitor.preflight() is True


def test_preflight_skips_model_loaded_check_when_ask_proxy_false(tmp_path: Path) -> None:
    """ask_proxy=False: bench model not loaded in Ollama must not fail preflight.

    Even with Ollama reachable but reporting zero loaded models, preflight passes.
    """
    monitor = _make_monitor(tmp_path, ask_proxy=False)

    def fake_get(url, **kwargs):  # noqa: ANN001
        if "/health" in url:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"genes": 0, "ribosome": "ok"}
            return resp
        if "/api/tags" in url or "/api/ps" in url:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"models": []}
            return resp
        raise AssertionError(f"unexpected URL: {url}")

    monitor._client.get = fake_get  # type: ignore[method-assign]

    assert monitor.preflight() is True


def test_preflight_skips_unauthorized_models_when_ask_proxy_false(tmp_path: Path) -> None:
    """ask_proxy=False: a stray model loaded in Ollama must not fail preflight.

    Retrieval-only runs don't care what's resident in VRAM — they never hit Ollama.
    """
    monitor = _make_monitor(tmp_path, ask_proxy=False)

    def fake_get(url, **kwargs):  # noqa: ANN001
        if "/health" in url:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"genes": 0, "ribosome": "ok"}
            return resp
        if "/api/tags" in url or "/api/ps" in url:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "models": [{"name": "llama3.1:70b"}]  # unauthorized stray
            }
            return resp
        raise AssertionError(f"unexpected URL: {url}")

    monitor._client.get = fake_get  # type: ignore[method-assign]

    assert monitor.preflight() is True


def test_preflight_still_gates_ollama_when_ask_proxy_true(tmp_path: Path) -> None:
    """ask_proxy=True (default): unreachable Ollama still fails preflight."""
    monitor = _make_monitor(tmp_path, ask_proxy=True)
    _stub_helix_only(monitor)

    assert monitor.preflight() is False


def test_preflight_still_gates_missing_model_when_ask_proxy_true(tmp_path: Path) -> None:
    """ask_proxy=True (default): benchmark model not loaded fails preflight."""
    monitor = _make_monitor(tmp_path, ask_proxy=True)

    def fake_get(url, **kwargs):  # noqa: ANN001
        if "/health" in url:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"genes": 0, "ribosome": "ok"}
            return resp
        if "/api/tags" in url or "/api/ps" in url:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"models": []}  # bench model not resident
            return resp
        raise AssertionError(f"unexpected URL: {url}")

    monitor._client.get = fake_get  # type: ignore[method-assign]

    assert monitor.preflight() is False


def test_health_timeout_is_configurable(tmp_path: Path) -> None:
    """The /health timeout used in preflight respects health_timeout_s."""
    monitor = _make_monitor(tmp_path, ask_proxy=False, health_timeout_s=22.0)
    captured = _stub_helix_only(monitor)

    monitor.preflight()

    assert 22.0 in captured["health_timeouts"], (
        f"expected 22.0s timeout on /health call, got {captured['health_timeouts']}"
    )


def test_health_timeout_default_is_15s(tmp_path: Path) -> None:
    """Default health_timeout_s is 15s (raised from 5s for post-swap cache-warm)."""
    monitor = _make_monitor(tmp_path, ask_proxy=False)  # no explicit timeout
    captured = _stub_helix_only(monitor)

    monitor.preflight()

    assert 15.0 in captured["health_timeouts"], (
        f"expected default 15.0s timeout, got {captured['health_timeouts']}"
    )


def test_ask_proxy_defaults_to_true(tmp_path: Path) -> None:
    """Default ask_proxy=True preserves the historical preflight gating semantics."""
    monitor = _make_monitor(tmp_path)
    assert monitor.ask_proxy is True
