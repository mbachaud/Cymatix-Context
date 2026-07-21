"""Smoke tests for cymatix_context.genai_telemetry.

All tests run without an active OTel SDK — the module must be no-op safe.
Run from repo root: ``python -m pytest tests/test_genai_telemetry.py -v``
"""
from __future__ import annotations

import logging

import pytest

from cymatix_context.telemetry.genai_telemetry import (
    PRICE_TABLE,
    emit_proxy_log_line,
    estimate_cost_usd,
    extract_anthropic_usage,
    extract_openai_usage,
    infer_provider,
    llm_span,
    prompt_hash,
    record_cache_outcome,
    record_response,
)


# ── llm_span no-op safety ─────────────────────────────────────────────

def test_llm_span_no_op_without_otel():
    """llm_span must not raise when OTel SDK is absent (noop tracer path)."""
    with llm_span(operation="chat", provider="ollama", model="qwen3:8b") as span:
        assert span is not None
        record_response(
            span,
            response_model="qwen3:8b",
            provider="ollama",
            request_model="qwen3:8b",
            operation="chat",
            input_tokens=10,
            output_tokens=5,
        )


def test_llm_span_with_request_attributes():
    with llm_span(
        operation="embeddings",
        provider="local.sentence_transformers",
        model="BAAI/bge-m3",
        request_attributes={"temperature": 0.0, "stream": False},
        helix_attributes={"helix.pipeline.stage": "extract"},
    ) as span:
        assert span is not None


def test_llm_span_records_exception_and_reraises():
    with pytest.raises(ValueError, match="boom"):
        with llm_span(operation="chat", provider="test", model="test"):
            raise ValueError("boom")


# ── extract_openai_usage ──────────────────────────────────────────────

def test_extract_openai_usage_full_nested_shape():
    """Parses the OpenAI nested shape including cached + reasoning tokens."""
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 30},
        "completion_tokens_details": {"reasoning_tokens": 10},
    }
    result = extract_openai_usage(usage)
    assert result["input_tokens"] == 100
    assert result["output_tokens"] == 50
    assert result["cached_input_tokens"] == 30
    assert result["reasoning_output_tokens"] == 10


def test_extract_openai_usage_missing_details():
    usage = {"prompt_tokens": 42, "completion_tokens": 7}
    result = extract_openai_usage(usage)
    assert result == {
        "input_tokens": 42,
        "output_tokens": 7,
        "cached_input_tokens": 0,
        "reasoning_output_tokens": 0,
    }


def test_extract_openai_usage_none_input():
    result = extract_openai_usage(None)
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0


def test_extract_openai_usage_non_dict_details_handled():
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "prompt_tokens_details": "invalid"}
    result = extract_openai_usage(usage)
    assert result["cached_input_tokens"] == 0


# ── extract_anthropic_usage ───────────────────────────────────────────

class _AnthropicUsage:
    def __init__(self, *, input_tokens=0, output_tokens=0, cache_read_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


def test_extract_anthropic_usage_reads_cache_read():
    usage = _AnthropicUsage(input_tokens=200, output_tokens=80, cache_read_input_tokens=150)
    result = extract_anthropic_usage(usage)
    assert result == {"input_tokens": 200, "output_tokens": 80, "cached_input_tokens": 150}


def test_extract_anthropic_usage_none():
    result = extract_anthropic_usage(None)
    assert result == {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}


# ── estimate_cost_usd ─────────────────────────────────────────────────

def test_estimate_cost_usd_unknown_model_returns_zero():
    cost = estimate_cost_usd(
        provider="unknown",
        model="nonexistent-model-xyz",
        input_tokens=1000,
        output_tokens=500,
    )
    assert cost == 0.0


def test_estimate_cost_usd_claude_opus():
    """1M input tokens at $15/M = $15."""
    cost = estimate_cost_usd(
        provider="anthropic",
        model="claude-opus-4-7",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert cost > 0.0
    assert abs(cost - 15.0) < 0.01


def test_estimate_cost_usd_cached_cheaper_than_input():
    cost_cached = estimate_cost_usd(
        provider="anthropic",
        model="claude-opus-4-7",
        cached_input_tokens=1_000_000,
    )
    cost_input = estimate_cost_usd(
        provider="anthropic",
        model="claude-opus-4-7",
        input_tokens=1_000_000,
    )
    assert cost_cached < cost_input


def test_estimate_cost_usd_local_model_zero():
    cost = estimate_cost_usd(provider="ollama", model="qwen3:8b",
                             input_tokens=10_000, output_tokens=10_000)
    assert cost == 0.0


def test_price_table_known_models_have_complete_entries():
    """Every model in PRICE_TABLE has input/output/cached keys."""
    for model, prices in PRICE_TABLE.items():
        for k in ("input", "output", "cached"):
            assert k in prices, f"{model} missing {k} price"
            assert prices[k] >= 0


# ── prompt_hash ───────────────────────────────────────────────────────

def test_prompt_hash_consistent():
    assert prompt_hash("hello world") == prompt_hash("hello world")


def test_prompt_hash_different_texts():
    assert prompt_hash("foo") != prompt_hash("bar")


def test_prompt_hash_empty():
    assert prompt_hash("") == ""


def test_prompt_hash_default_length_16():
    assert len(prompt_hash("test")) == 16


# ── record_cache_outcome ──────────────────────────────────────────────

@pytest.mark.parametrize("outcome", ["hit", "miss", "partial", "bogus"])
def test_record_cache_outcome_safe(outcome):
    record_cache_outcome(outcome)  # must not raise


# ── emit_proxy_log_line ───────────────────────────────────────────────

def test_emit_proxy_log_line_info_path(caplog):
    with caplog.at_level(logging.INFO, logger="helix.proxy"):
        emit_proxy_log_line(
            request_id="test-req-id",
            trace_id="abc123",
            model="gpt-4o",
            provider="openai",
            prompt_hash_value="abcd1234abcd1234",
            tokens_in=100,
            tokens_out=50,
            total_ms=250.0,
            finish_reason="stop",
            cache_outcome="miss",
            context_block="none",
        )
    assert any("proxy.call" in r.message and "test-req-id" in r.message for r in caplog.records)


def test_emit_proxy_log_line_error_path_uses_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="helix.proxy"):
        emit_proxy_log_line(
            request_id="err-req",
            trace_id=None,
            model="claude-sonnet-4-6",
            provider="anthropic",
            prompt_hash_value="",
            tokens_in=0,
            tokens_out=0,
            total_ms=0.0,
            error="upstream_timeout",
        )
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ── infer_provider ────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://api.anthropic.com/v1", "anthropic"),
    ("http://localhost:11434", "ollama"),
    ("http://127.0.0.1:11434/api/generate", "ollama"),
    ("https://api.openai.com/v1", "openai"),
    ("https://generativelanguage.googleapis.com/v1", "google"),
    (None, "unknown"),
    ("", "unknown"),
    ("https://my-custom-llm.example.com/v1", "openai-compat"),
])
def test_infer_provider(url, expected):
    assert infer_provider(url) == expected


# ── Instruments bind to the CURRENT otel meter (staleness regression) ─
#
# setup_telemetry() reassigns otel.meter after SDK init. The module must
# resolve otel.meter at instrument-creation time, not freeze the pre-setup
# no-op meter at import time — otherwise every helix_genai_* panel renders
# empty forever (the #209 defect class).

class _RecordingInstrument:
    def __init__(self):
        self.points = []

    def record(self, value, attributes=None):
        self.points.append(("record", value, dict(attributes or {})))

    def add(self, value, attributes=None):
        self.points.append(("add", value, dict(attributes or {})))


class _RecordingMeter:
    def __init__(self):
        self.created = {}

    def _make(self, name, **kw):
        inst = _RecordingInstrument()
        self.created[name] = inst
        return inst

    create_histogram = _make
    create_counter = _make


@pytest.fixture
def recording_meter(monkeypatch):
    """Swap otel.meter for a recorder and clear the instrument cache."""
    from cymatix_context.telemetry import genai_telemetry as gt
    from cymatix_context.telemetry import otel

    meter = _RecordingMeter()
    monkeypatch.setattr(otel, "meter", meter)
    monkeypatch.setattr(gt, "_instruments", {})
    yield meter


def test_instruments_use_meter_assigned_after_import(recording_meter):
    """Reassigning otel.meter (as setup_telemetry does) must take effect."""
    record_cache_outcome("hit")
    assert "helix_context_cache_outcome_total" in recording_meter.created
    inst = recording_meter.created["helix_context_cache_outcome_total"]
    assert inst.points == [("add", 1, {"outcome": "hit"})]


def test_record_response_emits_all_metric_families(recording_meter):
    with llm_span(operation="chat", provider="openai", model="gpt-4o") as span:
        record_response(
            span,
            response_model="gpt-4o",
            finish_reasons=["stop"],
            input_tokens=100,
            output_tokens=50,
            cached_input_tokens=30,
            reasoning_output_tokens=10,
            time_to_first_chunk_s=0.25,
            provider="openai",
            request_model="gpt-4o",
            operation="chat",
        )
    assert set(recording_meter.created) == {
        "helix_genai_client_token_usage",
        "helix_genai_time_to_first_chunk_seconds",
        "helix_genai_cost_usd",
        "helix_genai_finish_reasons_total",
    }
    usage = recording_meter.created["helix_genai_client_token_usage"]
    token_types = {p[2]["gen_ai.token.type"] for p in usage.points}
    assert token_types == {"input", "output", "cached", "reasoning"}


# ── Dashboard contract: every metric the GenAI dashboard queries must
#    be emitted by this module (no phantom panels — #209 deliverable 2).

def test_genai_dashboard_queries_are_covered(recording_meter):
    import json
    import re
    from pathlib import Path

    dash_path = (
        Path(__file__).resolve().parents[1]
        / "deploy" / "otel" / "grafana" / "dashboards" / "helix-genai.json"
    )
    dash = json.loads(dash_path.read_text(encoding="utf-8"))

    exprs = []

    def _walk(panels):
        for p in panels:
            for t in p.get("targets", []):
                exprs.append(t.get("expr", ""))
            _walk(p.get("panels", []))

    _walk(dash.get("panels", []))
    assert exprs, "dashboard has no queries — file moved or malformed?"

    queried = set()
    for e in exprs:
        for name in re.findall(r"helix_[a-z0-9_]+", e):
            # Strip Prometheus histogram/counter suffixes to recover the
            # OTel instrument name (helix_genai_finish_reasons_total is
            # created WITH the _total suffix, so it is kept as-is below).
            base = re.sub(r"_(sum|count|bucket)$", "", name)
            queried.add(base)

    # Force-create every instrument the module can emit.
    record_cache_outcome("miss")
    with llm_span(operation="chat", provider="openai", model="gpt-4o") as span:
        record_response(
            span, provider="openai", request_model="gpt-4o", operation="chat",
            finish_reasons=["stop"], input_tokens=1, output_tokens=1,
            time_to_first_chunk_s=0.1,
        )
    emitted = set(recording_meter.created)

    missing = queried - emitted
    assert not missing, (
        f"dashboard queries metrics the module never emits: {sorted(missing)}"
    )


# ── Proxy wiring: _emit_genai_proxy_telemetry ─────────────────────────

def _proxy_config():
    from types import SimpleNamespace
    return SimpleNamespace(server=SimpleNamespace(upstream="http://localhost:11434"))


def test_emit_genai_proxy_telemetry_logs_proxy_line(recording_meter, caplog, monkeypatch):
    from cymatix_context.server.helpers import _emit_genai_proxy_telemetry
    from cymatix_context.telemetry import otel

    # The proxy emission is gated on setup_telemetry() having run.
    monkeypatch.setattr(otel, "_initialised", True)
    with caplog.at_level(logging.INFO, logger="helix.proxy"):
        _emit_genai_proxy_telemetry(
            body={"model": "qwen3:8b", "stream": False},
            config=_proxy_config(),
            user_query="what does the splice step do?",
            usage={"prompt_tokens": 120, "completion_tokens": 40},
            response_id="chatcmpl-123",
            response_model="qwen3:8b",
            finish_reason="stop",
            total_s=0.5,
        )
    lines = [r.message for r in caplog.records if "proxy.call" in r.message]
    assert len(lines) == 1
    assert "chatcmpl-123" in lines[0]
    # Metrics side: token usage + finish reason recorded.
    assert "helix_genai_client_token_usage" in recording_meter.created
    assert "helix_genai_finish_reasons_total" in recording_meter.created


def test_emit_genai_proxy_telemetry_never_raises(recording_meter, monkeypatch):
    """Malformed inputs must not propagate into the proxy path."""
    from cymatix_context.server.helpers import _emit_genai_proxy_telemetry
    from cymatix_context.telemetry import otel

    monkeypatch.setattr(otel, "_initialised", True)
    _emit_genai_proxy_telemetry(
        body={},
        config=object(),  # no .server.upstream — internal failure swallowed
        user_query="",
        usage="not-a-dict",
        total_s=0.0,
    )


def test_emit_genai_proxy_telemetry_silent_when_telemetry_off(recording_meter, caplog, monkeypatch):
    """Default behavior (telemetry off) must stay byte-identical: no
    proxy.call log line, no instruments created."""
    from cymatix_context.server.helpers import _emit_genai_proxy_telemetry
    from cymatix_context.telemetry import otel

    monkeypatch.setattr(otel, "_initialised", False)
    with caplog.at_level(logging.DEBUG, logger="helix.proxy"):
        _emit_genai_proxy_telemetry(
            body={"model": "qwen3:8b"},
            config=_proxy_config(),
            user_query="q",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            total_s=0.1,
        )
    assert not [r for r in caplog.records if "proxy.call" in r.message]
    assert recording_meter.created == {}


# ── Cache wiring: CachedDAL bumps the outcome counter ────────────────

def test_cached_dal_records_hit_and_miss(recording_meter, tmp_path):
    from cymatix_context.adapters.cache import CachedDAL
    from cymatix_context.adapters.dal import DAL

    p = tmp_path / "a.txt"
    p.write_text("hello", encoding="utf-8")
    cache = CachedDAL(DAL())
    cache.fetch(str(p))   # miss
    cache.fetch(str(p))   # hit
    inst = recording_meter.created["helix_context_cache_outcome_total"]
    outcomes = [pt[2]["outcome"] for pt in inst.points]
    assert outcomes == ["miss", "hit"]


def test_cached_dal_bypass_not_recorded(recording_meter, tmp_path):
    from cymatix_context.adapters.cache import CachedDAL
    from cymatix_context.adapters.dal import DAL

    p = tmp_path / "b.txt"
    p.write_text("hello", encoding="utf-8")
    cache = CachedDAL(DAL())
    cache.fetch(str(p), bypass_cache=True)
    inst = recording_meter.created.get("helix_context_cache_outcome_total")
    assert inst is None or inst.points == []
