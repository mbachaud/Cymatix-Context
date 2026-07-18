"""
OpenTelemetry GenAI semantic-convention instrumentation for helix-context.

This module is the helix-context-side implementation of the OTel `gen_ai.*`
semantic conventions (https://opentelemetry.io/docs/specs/semconv/gen-ai/).
It is the *standard* observability surface for every LLM-touching call site
in the codebase: the proxy `/v1/chat/completions` handler, the compressor
(legacy: ribosome) backends, the local embedding/scoring backends.

It is intentionally separate from `otel.py` (which owns helix-domain
metrics like tier_contribution, chromatin_state, cwola_bucket) because the
two surfaces evolve on different cadences:
  * `otel.py` follows helix's internal data-model evolution
  * `genai_telemetry.py` follows the upstream OTel GenAI spec

Both modules share the same OTel SDK initialised by ``setup_telemetry()`` in
``otel.py``; this module is purely additive instrumentation built on
top of the same tracer + meter.

Usage at a call site::

    from helix_context.telemetry.genai_telemetry import (
        llm_span, record_response, estimate_cost_usd,
    )

    with llm_span(
        operation="chat",
        provider="ollama",
        model=model_name,
        request_attributes={"temperature": 0.0, "max_tokens": 256},
        helix_attributes={"helix.ribosome.operation": "rerank"},
    ) as span:
        first_chunk_t = None
        async for chunk in upstream_stream():
            if first_chunk_t is None and chunk.has_content:
                first_chunk_t = time.monotonic() - start_t
            ...
        record_response(
            span,
            response_model=resp.model,
            response_id=resp.id,
            finish_reasons=[resp.finish_reason],
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            cached_input_tokens=usage.get_cached(),
            reasoning_output_tokens=usage.get_reasoning(),
            time_to_first_chunk_s=first_chunk_t,
        )

Span names follow the OTel convention ``"<operation> <model>"``
(e.g. ``"chat qwen3:8b"``, ``"embeddings BAAI/bge-m3"``).

Standards reference:
    https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/
    https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-metrics/

The price table is intentionally small and lives in this module. To extend
it, edit ``PRICE_TABLE`` below. A helix.toml-driven override is a future
nice-to-have but not required for v1; unknown models simply report cost=0.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Iterable, Mapping, Optional

# Access meter/tracer as attributes of the otel module — NOT
# ``from .otel import meter, tracer``. setup_telemetry() *reassigns*
# otel.meter / otel.tracer module globals when it initialises the real
# SDK; an import-by-value here would freeze the pre-setup no-op objects
# and every helix_genai_* panel would render empty forever (the exact
# defect class #209 was filed about).
from . import otel as _otel

log = logging.getLogger("helix.genai_telemetry")

# ── Module-level instrument cache ───────────────────────────────────────
# Mirrors the pattern in otel.py: lazy-create on first access so that
# instruments reflect whatever meter is current after setup_telemetry().
_instruments: dict[str, Any] = {}


def genai_token_usage_histogram():
    """Histogram of per-call token counts.

    Spec name: ``gen_ai.client.token.usage``. We prefix with ``helix_``
    because every helix metric is namespaced (``helix_*``); the OTel spec
    name lives in the histogram description for grep-discoverability.
    Attributes: ``gen_ai.operation.name``, ``gen_ai.provider.name``,
    ``gen_ai.request.model``, ``gen_ai.response.model``,
    ``gen_ai.token.type`` ∈ {input, output, cached, reasoning}.
    """
    if "token_usage" not in _instruments:
        _instruments["token_usage"] = _otel.meter.create_histogram(
            "helix_genai_client_token_usage",
            unit="{token}",
            description="OTel gen_ai.client.token.usage — per-call token counts "
                        "split by direction (input|output|cached|reasoning).",
        )
    return _instruments["token_usage"]


def genai_ttft_histogram():
    """Histogram of time-to-first-chunk for streaming LLM responses.

    Spec name: ``gen_ai.server.time_to_first_chunk`` (we emit it client-
    side from the proxy's perspective).
    """
    if "ttft" not in _instruments:
        _instruments["ttft"] = _otel.meter.create_histogram(
            "helix_genai_time_to_first_chunk_seconds",
            unit="s",
            description="Time from request send to first content chunk for "
                        "streaming LLM responses (gen_ai.response."
                        "time_to_first_chunk).",
        )
    return _instruments["ttft"]


def genai_cost_histogram():
    """Histogram of estimated USD cost per LLM call.

    Computed from PRICE_TABLE; emits 0.0 for unpriced (e.g. local) models.
    Tracking cost as a first-class field so dashboards can expose
    cost-per-correct-answer, cost-per-minute, top-spend by model.
    """
    if "cost" not in _instruments:
        # No OTel unit annotation: the collector's Prometheus exporter
        # appends non-annotation units to the metric name, which would
        # publish helix_genai_cost_usd_USD_* and orphan the dashboard's
        # helix_genai_cost_usd_sum queries. The name already carries the
        # unit; test_telemetry_phase1 pins the translation contract.
        _instruments["cost"] = _otel.meter.create_histogram(
            "helix_genai_cost_usd",
            description="Estimated USD cost per LLM call, derived from "
                        "PRICE_TABLE. 0.0 for local/unpriced models.",
        )
    return _instruments["cost"]


def genai_finish_reasons_counter():
    if "finish_reasons" not in _instruments:
        _instruments["finish_reasons"] = _otel.meter.create_counter(
            "helix_genai_finish_reasons_total",
            description="LLM response finish reasons (stop, length, "
                        "tool_calls, content_filter, error, ...).",
        )
    return _instruments["finish_reasons"]


def cache_outcome_counter():
    """Counter for /context cache outcomes.

    Attributes: ``outcome`` ∈ {hit, miss, partial}. Top-line metric for
    proxy efficiency per the user's AI Benchmarking Reference §6.
    """
    if "cache_outcome" not in _instruments:
        _instruments["cache_outcome"] = _otel.meter.create_counter(
            "helix_context_cache_outcome_total",
            description="/context cache outcomes — hit | miss | partial.",
        )
    return _instruments["cache_outcome"]


# ── Static price table (USD per 1M tokens) ──────────────────────────────
# Last-reviewed prices are noted inline. When you update a row, also
# update the date so callers can grep how stale the table is. Keys are
# the model IDs as they appear on the OTel `gen_ai.request.model`
# attribute (which is what the upstream returns in `model` / `response.model`).
#
# Local-inference models report 0.0 — they are amortized hardware, not
# per-token spend. If you want to track local-inference cost, override
# this table from helix.toml in a follow-up.
PRICE_TABLE: dict[str, dict[str, float]] = {
    # Anthropic — checked 2026-05-10
    "claude-opus-4-7":     {"input": 15.00, "output": 75.00, "cached": 1.50},
    "claude-sonnet-4-6":   {"input":  3.00, "output": 15.00, "cached": 0.30},
    "claude-haiku-4-5":    {"input":  0.80, "output":  4.00, "cached": 0.08},
    # OpenAI — checked 2026-05-10
    "gpt-5":               {"input":  2.50, "output": 10.00, "cached": 1.25},
    "gpt-4o":              {"input":  2.50, "output": 10.00, "cached": 1.25},
    "gpt-4o-mini":         {"input":  0.15, "output":  0.60, "cached": 0.075},
    # Google — checked 2026-05-10
    "gemini-2.5-pro":      {"input":  1.25, "output":  5.00, "cached": 0.3125},
    "gemini-2.5-flash":    {"input":  0.075, "output": 0.30, "cached": 0.01875},
    # Local inference (Ollama / transformers) — no per-token cost
    "qwen3:8b":            {"input":  0.0,  "output":  0.0,  "cached": 0.0},
    "qwen3:4b":            {"input":  0.0,  "output":  0.0,  "cached": 0.0},
    "gemma4:e4b":          {"input":  0.0,  "output":  0.0,  "cached": 0.0},
    "gemma4:e2b":          {"input":  0.0,  "output":  0.0,  "cached": 0.0},
}


def estimate_cost_usd(
    *,
    provider: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_input_tokens: int = 0,
    reasoning_output_tokens: int = 0,
) -> float:
    """Look up cost for a single LLM call. Returns 0.0 for unknown models.

    Reasoning tokens are billed at the output rate for current providers
    (Anthropic, OpenAI o-series). If a provider changes that, edit here.
    """
    prices = PRICE_TABLE.get(model)
    if prices is None:
        return 0.0
    per_million = (
        input_tokens * prices["input"]
        + output_tokens * prices["output"]
        + cached_input_tokens * prices["cached"]
        + reasoning_output_tokens * prices["output"]
    )
    return per_million / 1_000_000.0


def prompt_hash(text: str, *, length: int = 16) -> str:
    """SHA256-prefix hash for grouping spans/logs by prompt without
    storing PII. ``length`` controls the hex prefix length (16 = 64 bits
    of entropy, plenty for grouping)."""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:length]


# ── Core span helpers ───────────────────────────────────────────────────

@contextmanager
def llm_span(
    *,
    operation: str,
    provider: str,
    model: str,
    request_attributes: Optional[Mapping[str, Any]] = None,
    helix_attributes: Optional[Mapping[str, Any]] = None,
):
    """Open a CLIENT-kind span for a single LLM call following OTel GenAI
    semantic conventions.

    The yielded span object accepts ``set_attribute`` calls; ``record_response``
    is the canonical way to populate response-side fields. Exceptions raised
    inside the ``with`` block are recorded on the span before re-raising.

    Args:
        operation: ``"chat"`` | ``"text_generation"`` | ``"embeddings"`` |
            ``"rerank"`` | ``"classify"``. Becomes ``gen_ai.operation.name``.
        provider: e.g. ``"ollama"``, ``"anthropic"``, ``"openai"``,
            ``"litellm"``, ``"local.transformers"``. Becomes
            ``gen_ai.provider.name``.
        model: Model ID. For cloud providers prefer the *date-suffixed*
            form (``claude-opus-4-7``, ``gpt-4o-2024-11-20``) since the
            user's AI ref doc §6 explicitly calls this out.
        request_attributes: Optional ``{temperature, top_p, top_k, max_tokens,
            seed, stream}``. Keys without the ``gen_ai.request.`` prefix are
            normalized for you.
        helix_attributes: Helix-namespace extras (``helix.ribosome.operation``,
            ``helix.pipeline.stage``, ``helix.cache_outcome``).
    """
    span_name = f"{operation} {model}".strip()
    span = _otel.tracer.start_as_current_span(span_name)
    cm = span if hasattr(span, "__enter__") else None
    if cm is not None:
        actual = cm.__enter__()
    else:
        actual = span
    try:
        actual.set_attribute("gen_ai.provider.name", provider)
        actual.set_attribute("gen_ai.operation.name", operation)
        actual.set_attribute("gen_ai.request.model", model)
        if request_attributes:
            for k, v in request_attributes.items():
                if v is None:
                    continue
                key = k if k.startswith("gen_ai.") else f"gen_ai.request.{k}"
                actual.set_attribute(key, v)
        if helix_attributes:
            for k, v in helix_attributes.items():
                if v is None:
                    continue
                actual.set_attribute(k, v)
        yield actual
    except Exception as exc:
        try:
            actual.record_exception(exc)
            actual.set_attribute("error.type", exc.__class__.__name__)
        except Exception:
            log.warning("failed to record exception on llm_span", exc_info=True)
        raise
    finally:
        if cm is not None:
            cm.__exit__(None, None, None)


def record_response(
    span: Any,
    *,
    response_model: Optional[str] = None,
    response_id: Optional[str] = None,
    finish_reasons: Optional[Iterable[str]] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cached_input_tokens: Optional[int] = None,
    reasoning_output_tokens: Optional[int] = None,
    time_to_first_chunk_s: Optional[float] = None,
    provider: Optional[str] = None,
    request_model: Optional[str] = None,
    operation: Optional[str] = None,
) -> None:
    """Populate response-side gen_ai.* attributes on the span and emit
    the matching metrics (``gen_ai.client.token.usage``, ttft, cost,
    finish_reasons counter).

    The ``provider`` / ``request_model`` / ``operation`` kwargs let the
    caller pass the same values they used in ``llm_span(...)`` so that
    histograms get full attribute sets without re-reading them from the
    span. They're optional; if omitted, metrics still record but with
    ``unknown`` provider/model labels.
    """
    if response_model:
        span.set_attribute("gen_ai.response.model", response_model)
    if response_id:
        span.set_attribute("gen_ai.response.id", response_id)
    if finish_reasons:
        finish_list = [str(r) for r in finish_reasons if r]
        if finish_list:
            span.set_attribute("gen_ai.response.finish_reasons", finish_list)
            counter = genai_finish_reasons_counter()
            for reason in finish_list:
                counter.add(1, attributes={"finish_reason": reason})

    # Common attributes for the token-usage histogram. Fall back to
    # "unknown" labels when caller didn't supply provider/model — the
    # histogram still records, dashboards will show the missing label.
    common = {
        "gen_ai.provider.name": provider or "unknown",
        "gen_ai.request.model": request_model or response_model or "unknown",
        "gen_ai.response.model": response_model or request_model or "unknown",
        "gen_ai.operation.name": operation or "unknown",
    }

    h = genai_token_usage_histogram()
    if input_tokens is not None and input_tokens >= 0:
        span.set_attribute("gen_ai.usage.input_tokens", int(input_tokens))
        h.record(int(input_tokens), attributes={**common, "gen_ai.token.type": "input"})
    if output_tokens is not None and output_tokens >= 0:
        span.set_attribute("gen_ai.usage.output_tokens", int(output_tokens))
        h.record(int(output_tokens), attributes={**common, "gen_ai.token.type": "output"})
    if cached_input_tokens is not None and cached_input_tokens > 0:
        span.set_attribute("gen_ai.usage.cached_input_tokens", int(cached_input_tokens))
        h.record(int(cached_input_tokens), attributes={**common, "gen_ai.token.type": "cached"})
    if reasoning_output_tokens is not None and reasoning_output_tokens > 0:
        span.set_attribute("gen_ai.usage.reasoning.output_tokens", int(reasoning_output_tokens))
        h.record(int(reasoning_output_tokens), attributes={**common, "gen_ai.token.type": "reasoning"})

    if time_to_first_chunk_s is not None and time_to_first_chunk_s >= 0:
        span.set_attribute("gen_ai.response.time_to_first_chunk", float(time_to_first_chunk_s))
        genai_ttft_histogram().record(float(time_to_first_chunk_s), attributes=common)

    if provider and request_model:
        cost = estimate_cost_usd(
            provider=provider,
            model=request_model,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            cached_input_tokens=int(cached_input_tokens or 0),
            reasoning_output_tokens=int(reasoning_output_tokens or 0),
        )
        if cost > 0 or request_model in PRICE_TABLE:
            span.set_attribute("gen_ai.cost.usd", cost)
            genai_cost_histogram().record(cost, attributes=common)


def record_cache_outcome(outcome: str) -> None:
    """Increment the /context cache-outcome counter.

    Args:
        outcome: One of ``"hit"`` | ``"miss"`` | ``"partial"``. Other
            values are accepted but tagged ``unknown``.
    """
    if outcome not in {"hit", "miss", "partial"}:
        outcome = "unknown"
    cache_outcome_counter().add(1, attributes={"outcome": outcome})


# ── Provider helpers ────────────────────────────────────────────────────

def infer_provider(upstream_url: Optional[str]) -> str:
    """Best-effort provider name from an upstream URL.

    Used by the proxy handler when it doesn't have explicit provider
    metadata. Defaults to ``"openai-compat"`` for unknown HTTPS endpoints.
    """
    if not upstream_url:
        return "unknown"
    url = upstream_url.lower()
    if "ollama" in url or ":11434" in url or "localhost" in url:
        return "ollama"
    if "anthropic" in url or "claude" in url:
        return "anthropic"
    if "openai" in url:
        return "openai"
    if "googleapis" in url or "gemini" in url:
        return "google"
    if "litellm" in url:
        return "litellm"
    return "openai-compat"


def extract_anthropic_usage(usage: Any) -> dict[str, int]:
    """Pull token fields from an Anthropic SDK usage object.

    Anthropic returns ``input_tokens``, ``output_tokens``, and the
    cache-related fields ``cache_creation_input_tokens`` +
    ``cache_read_input_tokens``. We map ``cache_read_input_tokens``
    onto ``cached_input_tokens`` (the OTel-canonical field).
    """
    def _g(name: str) -> int:
        v = getattr(usage, name, None) if usage is not None else 0
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0
    return {
        "input_tokens": _g("input_tokens"),
        "output_tokens": _g("output_tokens"),
        "cached_input_tokens": _g("cache_read_input_tokens"),
    }


def extract_openai_usage(usage: Any) -> dict[str, int]:
    """Pull token fields from an OpenAI-compatible usage dict.

    Reads the nested ``prompt_tokens_details.cached_tokens`` and
    ``completion_tokens_details.reasoning_tokens`` shapes that OpenAI
    introduced for prompt caching + reasoning models.
    """
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0,
                "cached_input_tokens": 0, "reasoning_output_tokens": 0}

    def _i(v: Any) -> int:
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    pt_details = usage.get("prompt_tokens_details") or {}
    ct_details = usage.get("completion_tokens_details") or {}
    return {
        "input_tokens": _i(usage.get("prompt_tokens")),
        "output_tokens": _i(usage.get("completion_tokens")),
        "cached_input_tokens": _i(pt_details.get("cached_tokens") if isinstance(pt_details, dict) else 0),
        "reasoning_output_tokens": _i(ct_details.get("reasoning_tokens") if isinstance(ct_details, dict) else 0),
    }


# ── Structured proxy log line ───────────────────────────────────────────

# Logger configured separately so OTel's logging handler (when enabled)
# routes these lines to Loki tagged with the trace context. The name is
# stable so a Loki LogQL filter can pin to ``logger="helix.proxy"``.
_proxy_log = logging.getLogger("helix.proxy")


def emit_proxy_log_line(
    *,
    request_id: str,
    trace_id: Optional[str],
    model: str,
    provider: str,
    prompt_hash_value: str,
    tokens_in: int,
    tokens_out: int,
    tokens_cached: int = 0,
    tokens_reasoning: int = 0,
    ttft_ms: Optional[float] = None,
    total_ms: float = 0.0,
    finish_reason: Optional[str] = None,
    cost_usd_estimate: float = 0.0,
    cache_outcome: Optional[str] = None,
    context_block: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Emit one structured-JSON log line per proxy request.

    The line carries everything a future analyst needs to replay the
    request without storing the prompt itself: request_id, trace_id,
    model + provider, token counts split four ways, TTFT and total
    latency, finish_reason, cost estimate, helix-cache outcome, and
    the know/miss/none classification of the injected context block.

    Sink: ``helix.proxy`` logger at INFO. With OTel's logging handler
    configured, it flows to Loki tagged with the active trace context
    so dashboards can pivot from a slow span to its log line.
    """
    payload = {
        "request_id": request_id,
        "trace_id": trace_id,
        "model": model,
        "provider": provider,
        "prompt_hash": prompt_hash_value,
        "tokens": {
            "in": tokens_in,
            "out": tokens_out,
            "cached": tokens_cached,
            "reasoning": tokens_reasoning,
        },
        "ttft_ms": ttft_ms,
        "total_ms": total_ms,
        "finish_reason": finish_reason,
        "cost_usd_estimate": cost_usd_estimate,
        "helix": {
            "cache_outcome": cache_outcome,
            "context_block": context_block,
        },
        "ts": time.time(),
    }
    if error:
        payload["error"] = error
        _proxy_log.warning("proxy.call %s", json.dumps(payload, separators=(",", ":")))
    else:
        _proxy_log.info("proxy.call %s", json.dumps(payload, separators=(",", ":")))


__all__ = [
    "PRICE_TABLE",
    "estimate_cost_usd",
    "prompt_hash",
    "llm_span",
    "record_response",
    "record_cache_outcome",
    "infer_provider",
    "extract_anthropic_usage",
    "extract_openai_usage",
    "emit_proxy_log_line",
]
