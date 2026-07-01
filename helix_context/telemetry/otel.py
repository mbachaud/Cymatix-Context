"""
OpenTelemetry setup for helix-context.

Makes the retrieval pipeline observable: traces per /context span, metric
histograms per tier, counters for CWoLa bucket accumulation, gauges for
lifecycle tier + graph density. Everything degrades gracefully if the
opentelemetry packages aren't installed — helix still runs, just blind.

Usage:
    from helix_context.telemetry import setup_telemetry, meter
    app = FastAPI()
    setup_telemetry(app, service_name="helix-context")
    h = meter.create_histogram("helix_tier_contribution", unit="score")
    h.record(5.3, attributes={"tier": "pki", "shape": "project_key"})

Environment:
    HELIX_OTEL_ENABLED       - "1" to turn on, default "0"
    HELIX_OTEL_ENDPOINT      - OTLP gRPC endpoint, default "localhost:4317"
    HELIX_OTEL_INSECURE      - "1" for plain gRPC, default "1" (dev-local)
    HELIX_OTEL_SAMPLER_RATIO - trace sampler 0.0-1.0, default 1.0
    HELIX_OTEL_REDACT_QUERY  - "1" to hash query strings in spans, default "1"
    HELIX_OTEL_LOGS_ENABLED  - "1" to ship Python log records to OTel
                               (collector → Loki), default "1". Set "0"
                               to keep traces+metrics on while suppressing
                               log shipment — useful under Loki disk
                               pressure or for PII-sensitive deployments.
    HELIX_OTEL_LOGS_LEVEL    - Minimum log level forwarded to OTel: one
                               of DEBUG / INFO / WARNING / ERROR /
                               CRITICAL. Default "INFO". Tunes log
                               volume without disabling traces/metrics.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import socket
from typing import Any, Optional

log = logging.getLogger("helix.telemetry")


# Graceful no-op stand-ins so callers can always
# `from helix_context.telemetry import tracer, meter` without a try/except.
class _NoopSpan:
    def __enter__(self): return self
    def __exit__(self, *a): return None
    def set_attribute(self, *a, **kw): pass
    def set_status(self, *a, **kw): pass
    def record_exception(self, *a, **kw): pass
    def add_event(self, *a, **kw): pass


class _NoopTracer:
    def start_as_current_span(self, *a, **kw): return _NoopSpan()
    def start_span(self, *a, **kw): return _NoopSpan()


class _NoopInstrument:
    def record(self, *a, **kw): pass
    def add(self, *a, **kw): pass
    def set(self, *a, **kw): pass


class _NoopMeter:
    def create_histogram(self, *a, **kw): return _NoopInstrument()
    def create_counter(self, *a, **kw): return _NoopInstrument()
    def create_up_down_counter(self, *a, **kw): return _NoopInstrument()
    def create_observable_gauge(self, *a, **kw): return _NoopInstrument()
    def create_gauge(self, *a, **kw): return _NoopInstrument()


tracer: Any = _NoopTracer()
meter: Any = _NoopMeter()
_initialised = False


def _redact_query(q: str) -> str:
    """Hash query text + keep first 50 chars — default privacy mode."""
    if not q:
        return ""
    digest = hashlib.sha256(q.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{q[:50]}[hash:{digest}]" if os.environ.get(
        "HELIX_OTEL_REDACT_QUERY", "1"
    ) != "0" else q


def _instrument_fastapi(app: Any) -> None:
    """Run FastAPIInstrumentor on a single app, logging on failure.

    Kept outside the global-init guard so every app passed to
    setup_telemetry() gets instrumented, not just the first one.
    """
    if app is None:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor().instrument_app(app)
    except ImportError:
        log.warning("opentelemetry-instrumentation-fastapi missing — "
                    "FastAPI routes will not be auto-traced")
    except Exception:
        log.warning("FastAPI auto-instrumentation failed", exc_info=True)


class _LoggerNameInjector(logging.Filter):
    """Promote the Python logger name to an OTel LogRecord attribute.

    OTel's default LoggingHandler maps the Python logger name to the
    InstrumentationScope.name, which the collector → Loki bridge does
    NOT promote to a Loki stream label by default. The
    helix-overview "Proxy call log" panel queries `{logger="helix.proxy"}`,
    so we surface the logger name as a record attribute and add the
    `loki.attribute.labels` resource hint below so Loki's OTLP ingest
    promotes it to a stream label.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        # `logger` is consumed by Loki via the loki.attribute.labels
        # resource hint set on the LoggerProvider resource.
        if not hasattr(record, "logger") or record.logger is None:
            record.logger = record.name
        return True


_LOG_LEVEL_NAMES = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _resolve_logs_level(raw: Optional[str]) -> int:
    """Map HELIX_OTEL_LOGS_LEVEL → numeric level, defaulting to INFO on
    unrecognised values."""
    if not raw:
        return logging.INFO
    name = raw.strip().upper()
    if name in _LOG_LEVEL_NAMES:
        return getattr(logging, name)
    log.warning(
        "HELIX_OTEL_LOGS_LEVEL=%r is not one of %s — falling back to INFO",
        raw, sorted(_LOG_LEVEL_NAMES),
    )
    return logging.INFO


def _attach_otlp_logging_handler(
    *,
    endpoint: str,
    insecure: bool,
    resource: Any,
    LoggerProvider: Any,
    BatchLogRecordProcessor: Any,
    OTLPLogExporter: Any,
    LoggingHandler: Any,
    set_logger_provider: Any,
) -> None:
    """Wire the Python logging root → OTel LogRecord → OTLP → collector →
    Loki. Safe to call once per process; subsequent calls are no-ops.

    Re-uses the same OTLP endpoint as the trace/metric exporters so a
    single collector receiver handles all three signals.

    Honors two env vars:

    - ``HELIX_OTEL_LOGS_ENABLED`` (default ``"1"``): set ``"0"`` to skip
      log shipping entirely while keeping traces + metrics enabled.
    - ``HELIX_OTEL_LOGS_LEVEL`` (default ``"INFO"``): minimum level
      forwarded to OTel. DEBUG / INFO / WARNING / ERROR / CRITICAL.
    """
    if os.environ.get("HELIX_OTEL_LOGS_ENABLED", "1") != "1":
        log.info(
            "OTel log shipping disabled "
            "(HELIX_OTEL_LOGS_ENABLED=0) — traces + metrics still on, "
            "Loki Logs panel will stay empty",
        )
        return
    # Idempotency: bail BEFORE constructing a new LoggerProvider so a
    # second call doesn't orphan the prior BatchLogRecordProcessor's
    # background export thread by overwriting the global provider.
    root = logging.getLogger()
    if any(isinstance(h, LoggingHandler) for h in root.handlers):
        return
    level = _resolve_logs_level(os.environ.get("HELIX_OTEL_LOGS_LEVEL"))
    try:
        # Resource hint: tells Loki's OTLP ingest to promote `logger`
        # (and `service.name`, which Loki promotes by default but we
        # name it explicitly for clarity) from a record attribute to a
        # stream label so `{logger="helix.proxy"}` queries work.
        merged_resource = resource.merge(
            type(resource).create({"loki.attribute.labels": "logger"})
        )
        logger_provider = LoggerProvider(resource=merged_resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=endpoint, insecure=insecure)
            )
        )
        set_logger_provider(logger_provider)

        otel_handler = LoggingHandler(
            level=level, logger_provider=logger_provider,
        )
        otel_handler.addFilter(_LoggerNameInjector())
        root.addHandler(otel_handler)
    except Exception:
        log.warning(
            "Could not attach OTLP logging handler — Loki log panel "
            "will stay empty",
            exc_info=True,
        )


def setup_telemetry(
    app: Any = None,
    service_name: str = "helix-context",
    service_version: str = "0.4.0b",
) -> bool:
    """Initialize OTel tracer + meter providers + FastAPI auto-instrumentation.

    Returns True if telemetry was turned on, False if it was skipped (not
    enabled, or the opentelemetry packages are missing). Safe to call
    multiple times — the global SDK init runs only once, but FastAPI
    auto-instrumentation is applied to every app passed in.
    """
    global tracer, meter, _initialised
    if _initialised:
        # SDK already set up — but each new app still needs instrumentation,
        # otherwise routes added after the first call are never auto-traced.
        _instrument_fastapi(app)
        return True
    if os.environ.get("HELIX_OTEL_ENABLED", "0") != "1":
        log.info("OTel disabled (set HELIX_OTEL_ENABLED=1 to turn on)")
        return False
    try:
        from opentelemetry import trace, metrics
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import (
            TraceIdRatioBased, ALWAYS_ON, ParentBased,
        )
        from opentelemetry.sdk.metrics import (
            Counter, Histogram, ObservableCounter, ObservableGauge,
            ObservableUpDownCounter, UpDownCounter, MeterProvider,
        )
        from opentelemetry.sdk.metrics.export import (
            AggregationTemporality, PeriodicExportingMetricReader,
        )
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter,
        )
    except ImportError:
        log.warning(
            "OTel packages not installed — "
            "`pip install opentelemetry-distro opentelemetry-exporter-otlp "
            "opentelemetry-instrumentation-fastapi`"
        )
        return False

    endpoint = os.environ.get("HELIX_OTEL_ENDPOINT", "localhost:4317")
    insecure = os.environ.get("HELIX_OTEL_INSECURE", "1") == "1"
    try:
        ratio = float(os.environ.get("HELIX_OTEL_SAMPLER_RATIO", "1.0"))
    except ValueError:
        ratio = 1.0

    resource = Resource.create({
        SERVICE_NAME: service_name,
        SERVICE_VERSION: service_version,
        # COMPUTERNAME is Windows-only; fall back to socket.gethostname()
        # so POSIX deployments don't tag every span as "unknown".
        "deployment.host": os.environ.get("COMPUTERNAME") or socket.gethostname(),
    })

    sampler = ParentBased(ALWAYS_ON if ratio >= 1.0 else TraceIdRatioBased(ratio))
    tracer_provider = TracerProvider(resource=resource, sampler=sampler)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint, insecure=insecure)
        )
    )
    trace.set_tracer_provider(tracer_provider)
    tracer = trace.get_tracer(service_name, service_version)

    # Explicit CUMULATIVE temporality on every instrument type. Diagnosed
    # 2026-04-14: without this, the Python SDK was exporting with
    # ever-changing start_timestamps, which the OTel collector's
    # prometheusexporter interprets as incompatible delta data and drops
    # silently (logs "Misaligned starting timestamps" warnings on every
    # batch). Gauges survived because they report absolute values, but
    # counters and histograms (context_latency, tier_fired,
    # tier_contribution_score, cwola_bucket) never made it to Prometheus.
    # Cumulative is what Prometheus natively understands, so this is both
    # correct-by-construction and matches the collector's expectation.
    cumulative = {
        Counter: AggregationTemporality.CUMULATIVE,
        UpDownCounter: AggregationTemporality.CUMULATIVE,
        Histogram: AggregationTemporality.CUMULATIVE,
        ObservableCounter: AggregationTemporality.CUMULATIVE,
        ObservableUpDownCounter: AggregationTemporality.CUMULATIVE,
        ObservableGauge: AggregationTemporality.CUMULATIVE,
    }
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(
            endpoint=endpoint,
            insecure=insecure,
            preferred_temporality=cumulative,
        ),
        export_interval_millis=15_000,
    )
    meter_provider = MeterProvider(
        resource=resource, metric_readers=[metric_reader],
    )
    metrics.set_meter_provider(meter_provider)
    meter = metrics.get_meter(service_name, service_version)

    # Logs → OTLP → collector → Loki. Without this, helix emits zero log
    # records via OTel and the helix-overview "Proxy call log" panel
    # stays empty. Attached at INFO so uvicorn access logs flow through.
    _attach_otlp_logging_handler(
        endpoint=endpoint, insecure=insecure, resource=resource,
        LoggerProvider=LoggerProvider,
        BatchLogRecordProcessor=BatchLogRecordProcessor,
        OTLPLogExporter=OTLPLogExporter,
        LoggingHandler=LoggingHandler,
        set_logger_provider=set_logger_provider,
    )

    _initialised = True
    # Auto-instrument FastAPI if an app was provided. Wraps every route
    # in a span; free latency + status metric per endpoint.
    _instrument_fastapi(app)
    # Promoted to WARNING so the confirmation is visible even when the root
    # logger is at the default WARNING level (uvicorn's --log-level only
    # affects uvicorn's own loggers; helix.* loggers are not auto-promoted).
    # Without this, operators can't confirm OTel is actually on.
    log.warning("OTel telemetry ON, endpoint=%s insecure=%s sampler=%.2f",
                endpoint, insecure, ratio)
    return True


def redact_query(q: str) -> str:
    """Public redaction helper for code paths that want to stamp a
    privacy-safe query attribute on a span."""
    return _redact_query(q)


# ── Lazy instrument getters ──────────────────────────────────────────
# Modules import these once. They resolve to no-op instruments when
# telemetry is off, real instruments when on. Cached so meter calls
# happen at most once per process.

_instruments: dict = {}


def tier_contribution_histogram():
    if "tier_contribution" not in _instruments:
        _instruments["tier_contribution"] = meter.create_histogram(
            "helix_tier_contribution",
            unit="score",
            description="Per-tier bonus magnitude contributed to gene_scores",
        )
    return _instruments["tier_contribution"]


def context_latency_histogram():
    if "context_latency" not in _instruments:
        _instruments["context_latency"] = meter.create_histogram(
            "helix_context_latency_seconds",
            unit="s",
            description="End-to-end /context build time",
        )
    return _instruments["context_latency"]


def context_calls_by_class_counter():
    """Stage 5 (2026-05-08): per-call counter labelled by caller_model_class.

    Emits one increment per /context request with the resolved class
    (generic|small_moe|frontier). Spec §11.
    """
    if "context_calls_by_class" not in _instruments:
        _instruments["context_calls_by_class"] = meter.create_counter(
            "helix_context_calls_by_class",
            description="/context calls bucketed by caller_model_class (Stage 5)",
        )
    return _instruments["context_calls_by_class"]


def cwola_bucket_counter():
    if "cwola_bucket" not in _instruments:
        _instruments["cwola_bucket"] = meter.create_counter(
            "helix_cwola_bucket_total",
            description="CWoLa log rows by bucket (A/B/pending)",
        )
    return _instruments["cwola_bucket"]


def cwola_f_gap_gauge():
    if "cwola_f_gap" not in _instruments:
        _instruments["cwola_f_gap"] = meter.create_gauge(
            "helix_cwola_f_gap_sq",
            description="(f_A - f_B)^2 — CWoLa bucket divergence (0.16 promotes PLR)",
        )
    return _instruments["cwola_f_gap"]


def harmonic_edges_counter():
    if "harmonic_edges" not in _instruments:
        _instruments["harmonic_edges"] = meter.create_gauge(
            "helix_harmonic_edges_total",
            description="Count of harmonic_links edges by provenance source",
        )
    return _instruments["harmonic_edges"]


def chromatin_state_counter():
    if "chromatin_state" not in _instruments:
        _instruments["chromatin_state"] = meter.create_gauge(
            "helix_chromatin_state_total",
            description="Gene count by chromatin state (OPEN/EUCHROMATIN/HETEROCHROMATIN)",
        )
    return _instruments["chromatin_state"]


def genome_size_gauge():
    if "genome_size" not in _instruments:
        _instruments["genome_size"] = meter.create_gauge(
            "helix_genome_size_bytes",
            unit="By",
            description="Genome total char count — raw vs compressed",
        )
    return _instruments["genome_size"]


def tier_fired_counter():
    if "tier_fired" not in _instruments:
        _instruments["tier_fired"] = meter.create_counter(
            "helix_tier_fired_total",
            description="Retrieval tier activation events, labelled by tier",
        )
    return _instruments["tier_fired"]


def hub_concentration_gauge():
    if "hub_concentration" not in _instruments:
        _instruments["hub_concentration"] = meter.create_gauge(
            "helix_hub_concentration_ratio",
            description="harmonic_links inbound-degree top-1% mean / overall mean. "
                        "Watch for condensation transition (preferential-attachment "
                        "graphs collapse flow into hubs as N grows). Healthy ≲ ~10x; "
                        "rising trend = hub monopolization, retrieval flowing through "
                        "fewer paths than the edge count suggests.",
        )
    return _instruments["hub_concentration"]


def hub_inbound_degree_gauge():
    if "hub_inbound_degree" not in _instruments:
        _instruments["hub_inbound_degree"] = meter.create_gauge(
            "helix_hub_inbound_degree",
            description="harmonic_links inbound-degree summary statistics, labelled by stat "
                        "(max / p99 / p95 / p50 / mean). Backfill cap is 500; values "
                        "approaching that consistently mean the cap is the binding constraint.",
        )
    return _instruments["hub_inbound_degree"]


def hitl_events_counter():
    if "hitl_events" not in _instruments:
        _instruments["hitl_events"] = meter.create_counter(
            "helix_hitl_events_total",
            description="Human-In-The-Loop pause events, labelled by pause_type "
                        "(permission_request / uncertainty_check / rollback_confirm "
                        "/ other) and party. Emitted on every successful "
                        "registry.emit_hitl_event write. Pair with "
                        "helix_context_ellipticity to correlate HITL spikes with "
                        "degraded context windows.",
        )
    return _instruments["hitl_events"]


def context_ellipticity_histogram():
    if "context_ellipticity" not in _instruments:
        _instruments["context_ellipticity"] = meter.create_histogram(
            "helix_context_ellipticity",
            description="Per-query ellipticity (geometric mean of coverage, density, "
                        "freshness, and optional logical_coherence). Range 0-1. "
                        ">=0.7 classified aligned; 0.3-0.7 sparse; <0.3 denatured. "
                        "Low values across many queries = retrieval quality "
                        "degrading — candidate for runbook action.",
        )
    return _instruments["context_ellipticity"]


def context_health_status_counter():
    if "context_health_status" not in _instruments:
        _instruments["context_health_status"] = meter.create_counter(
            "helix_context_health_status_total",
            description="/context call outcomes labelled by status (aligned | "
                        "sparse | stale | denatured). Watch the ratio: "
                        "aligned-dominant genome is healthy, rising sparse or "
                        "denatured = retrieval quality drift.",
        )
    return _instruments["context_health_status"]


def budget_tier_counter():
    if "budget_tier" not in _instruments:
        _instruments["budget_tier"] = meter.create_counter(
            "helix_budget_tier_total",
            description="Dynamic budget tier selected per /context call, labelled "
                        "by tier (tight | focused | broad | abstain). Tier reflects "
                        "retrieval confidence: tight = single-gene dominance, "
                        "focused = moderate, broad = weak signal / widen the net, "
                        "abstain = below FOCUSED floor on both axes (no injection).",
        )
    return _instruments["budget_tier"]


def ribosome_info_gauge():
    """Info-metric gauge for compressor cost visibility (W2-B).

    Set once at server startup with value=1 and labels
    {backend, model, cost_class}. Standard Prometheus
    info-metric pattern -- the value is meaningless; the
    labels carry the data. Use in dashboards via:

        helix_ribosome_info{cost_class="api+paid"}

    A red stat panel keyed on cost_class="api+paid" surfaces
    paid-backend operation in the dashboard view, complementing
    the startup WARNING log line.
    """
    if "ribosome_info" not in _instruments:
        _instruments["ribosome_info"] = meter.create_gauge(
            "helix_ribosome_info",
            description="Ribosome backend info; value=1, labels "
                        "{backend, model, cost_class} carry the data.",
        )
    return _instruments["ribosome_info"]


def vault_export_histogram():
    if "vault_export" not in _instruments:
        _instruments["vault_export"] = meter.create_histogram(
            "helix_vault_export_seconds",
            unit="s",
            description="Latency of vault export operations.",
        )
    return _instruments["vault_export"]


def vault_pruner_histogram():
    if "vault_pruner" not in _instruments:
        _instruments["vault_pruner"] = meter.create_histogram(
            "helix_vault_pruner_seconds",
            unit="s",
            description="Latency of one pruner cycle.",
        )
    return _instruments["vault_pruner"]


def vault_force_prune_counter():
    if "vault_force_prune" not in _instruments:
        _instruments["vault_force_prune"] = meter.create_counter(
            "helix_vault_force_prune_total",
            description="Pinned traces force-deleted per max_retention_hours_hard.",
        )
    return _instruments["vault_force_prune"]


def vault_file_count_gauge():
    """Imperative gauge — VaultManager.status() updates it on each call."""
    if "vault_file_count" not in _instruments:
        _instruments["vault_file_count"] = meter.create_gauge(
            "helix_vault_file_count",
            description="Files in each vault folder (per `folder` label).",
        )
    return _instruments["vault_file_count"]


# ── Per-stage pipeline telemetry (feat/per-stage-telemetry) ──────────


def pipeline_stage_histogram():
    """Histogram for per-stage /context pipeline latency.

    Attributes: {stage: str}  — e.g. "classify", "retrieve", "refine",
    "assemble". Optionally decorated with {decoder_mode: str} by the
    caller when the label is cheap to produce.
    """
    if "pipeline_stage" not in _instruments:
        _instruments["pipeline_stage"] = meter.create_histogram(
            "helix_pipeline_stage_seconds",
            unit="s",
            description="Latency of each /context pipeline stage.",
        )
    return _instruments["pipeline_stage"]


def pipeline_stage_span(stage: str, *, decoder_mode: Optional[str] = None):
    """Open a span bracketing one /context pipeline stage.

    The span name is ``helix.pipeline.<stage>`` (e.g.
    ``helix.pipeline.classify``). Use as a context manager around the
    stage's code block; pairs with ``pipeline_stage_histogram()`` so
    each stage emits BOTH a span (visible in Tempo as the per-request
    waterfall) AND a histogram point (visible in Prometheus as the
    aggregate latency distribution).

    The root span ``helix.pipeline.build_context`` should wrap the full
    /context handler so per-stage spans nest under it correctly.

    Returns the underlying tracer span; the caller can ``set_attribute``
    additional helix-namespace fields (e.g. ``helix.cache_outcome``,
    ``helix.context_block``).

    Args:
        stage: stage identifier, e.g. ``"classify"``, ``"extract"``,
            ``"express"``, ``"rerank"``, ``"splice"``, ``"assemble"``,
            ``"build_context"`` (root).
        decoder_mode: optional ``CallerModelClass`` value (``"generic"``,
            ``"small_moe"``, ``"frontier"``) to tag the stage's branch.
    """
    cm = tracer.start_as_current_span(f"helix.pipeline.{stage}")
    # The OTel tracer returns a context manager; the noop tracer
    # returns a _NoopSpan that is itself a context manager. Both
    # support the same protocol, so we wrap the entry/exit by hand
    # to be able to set attributes before yielding.
    span = cm.__enter__()
    try:
        span.set_attribute("helix.pipeline.stage", stage)
        if decoder_mode:
            span.set_attribute("helix.pipeline.decoder_mode", decoder_mode)
    except Exception:
        pass
    return _PipelineStageSpanCM(cm, span)


class _PipelineStageSpanCM:
    """Internal context-manager wrapper for ``pipeline_stage_span``.

    Exists so callers can use the natural ``with pipeline_stage_span(...)
    as span:`` form even though we needed to enter the underlying span
    eagerly to set its attributes.
    """

    def __init__(self, cm, span):
        self._cm = cm
        self._span = span

    def __enter__(self):
        return self._span

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            try:
                self._span.record_exception(exc_val)
            except Exception:
                pass
        return self._cm.__exit__(exc_type, exc_val, exc_tb)


def ribosome_call_histogram():
    """Histogram for individual compressor backend.complete() calls.

    Attributes: {backend: str, model: str, call_kind: str}
    call_kind is one of: pack | rerank | splice | replicate | unknown
    """
    if "ribosome_call" not in _instruments:
        _instruments["ribosome_call"] = meter.create_histogram(
            "helix_ribosome_call_seconds",
            unit="s",
            description="Latency of each ribosome backend.complete() call, "
                        "labelled by backend, model, and call_kind.",
        )
    return _instruments["ribosome_call"]


def genome_signal_histogram():
    """Histogram for per-signal latency inside query_genes().

    Attributes: {signal: str}  — e.g. "fts5", "splade", "sema_boost",
    "tag_exact", "tag_prefix", "pki", "harmonic", "sr".
    """
    if "genome_signal" not in _instruments:
        _instruments["genome_signal"] = meter.create_histogram(
            "helix_genome_signal_seconds",
            unit="s",
            description="Latency of each retrieval signal inside query_genes(), "
                        "labelled by signal name.",
        )
    return _instruments["genome_signal"]


def genome_wal_size_gauge():
    """Gauge for the WAL file size in bytes.

    Updated by the background WAL-health task (every 30 s). Stale when
    OTel is disabled — the noop instrument silently drops the call.
    """
    if "genome_wal_size" not in _instruments:
        _instruments["genome_wal_size"] = meter.create_gauge(
            "helix_genome_wal_size_bytes",
            unit="By",
            description="SQLite WAL file size in bytes. Spikes indicate "
                        "checkpoint pressure; sustained high values = WAL bloat.",
        )
    return _instruments["genome_wal_size"]


def genome_checkpoint_blocked_counter():
    """Counter incremented when a WAL checkpoint reports busy=1.

    A rising count means readers are holding WAL snapshots long enough
    to block TRUNCATE checkpoints. Correlates with WAL bloat (PR #32).
    """
    if "genome_checkpoint_blocked" not in _instruments:
        _instruments["genome_checkpoint_blocked"] = meter.create_counter(
            "helix_genome_checkpoint_blocked_total",
            description="Number of WAL checkpoints that returned busy=1, "
                        "meaning a reader was holding a snapshot.",
        )
    return _instruments["genome_checkpoint_blocked"]


# ── #209 phase 1: top-5 tuning-signal instruments ────────────────────


def dense_cosine_histogram():
    """Histogram of raw dense-tier cosines at their computation sites.

    Attributes: {arm: str} — "hot" for the BGE-M3 dense-recall merge in
    query_genes (both rrf and additive fusion observe the same raw
    cosine, pre-weight), "cold" for the heterochromatin ΣĒMA scan in
    query_cold_tier (above-floor hits only). Calibration data for
    dense_additive_weight / dense_additive_min_cosine (#209 / #203).
    """
    if "dense_cosine" not in _instruments:
        _instruments["dense_cosine"] = meter.create_histogram(
            "helix_dense_cosine",
            description="Raw cosine of each dense-tier hit, labelled by arm "
                        "(hot = BGE-M3 dense recall, cold = cold-tier ΣĒMA).",
        )
    return _instruments["dense_cosine"]


def shard_fanout_histogram():
    """Histogram of shards consulted per routed query (ShardRouter.query_genes).

    The #165 finding (router degeneracy: 90-100% of shards consulted)
    becomes a continuously monitored number. No attributes.
    """
    if "shard_fanout" not in _instruments:
        _instruments["shard_fanout"] = meter.create_histogram(
            "helix_shard_fanout",
            description="Number of shards consulted per ShardRouter query.",
        )
    return _instruments["shard_fanout"]


def shard_discrimination_histogram():
    """Histogram of the fraction of healthy shards hit per routed query.

    routed / known, in [0, 1]. 1.0 = the router consulted every healthy
    shard (zero discrimination — the #165 degeneracy case); lower is a
    more selective route. Acceptance metric for the AND-mode router.
    """
    if "shard_discrimination" not in _instruments:
        _instruments["shard_discrimination"] = meter.create_histogram(
            "helix_shard_discrimination",
            description="Fraction of healthy shards consulted per ShardRouter "
                        "query (routed / known, 1.0 = no discrimination).",
        )
    return _instruments["shard_discrimination"]


def know_decision_counter():
    """Counter of know/miss discriminator outcomes (decide_know_or_miss).

    Attributes: {outcome: str, reason: str} — outcome is one of
    know | miss | abstain; reason is "none" for know, the MissBlock
    reason (a member of schemas.MISS_REASONS) otherwise. Calibrates
    [know] floors/margins per corpus and feeds miss-reason-driven
    escalation (SNOW-2 arm E).
    """
    if "know_decision" not in _instruments:
        _instruments["know_decision"] = meter.create_counter(
            "helix_know_decision_total",
            description="Know/miss discriminator outcomes, labelled by outcome "
                        "(know | miss | abstain) and miss reason.",
        )
    return _instruments["know_decision"]


def session_tokens_saved_counter():
    """Counter of estimated tokens saved by session-delivery elision.

    Incremented in _assemble when an already-delivered document is
    replaced by an elision stub; the value is the estimated token delta
    (full spliced text minus stub, ~4 chars/token). Proves or falsifies
    the "~40% tokens on multi-turn" claim and prices the elision arm.
    """
    if "session_tokens_saved" not in _instruments:
        _instruments["session_tokens_saved"] = meter.create_counter(
            "helix_session_tokens_saved_total",
            description="Estimated tokens saved by session working-set elision "
                        "of already-delivered documents.",
        )
    return _instruments["session_tokens_saved"]


def splice_ratio_histogram():
    """Histogram of the per-window splice compression ratio.

    raw_chars / compressed_chars as computed in _assemble (the same
    value shipped in ContextWindow.compression_ratio and the legibility
    headers). Attributes: {caller_model_class: str} — generic |
    small_moe | frontier. Balancing signal for splice_aggressiveness:
    watch ratio drift vs abstain-rate drift while sweeping.
    """
    if "splice_ratio" not in _instruments:
        _instruments["splice_ratio"] = meter.create_histogram(
            "helix_splice_ratio",
            description="Splice compression ratio (raw_chars / compressed_chars) "
                        "per assembled context window, labelled by "
                        "caller_model_class.",
        )
    return _instruments["splice_ratio"]


def know_confidence_histogram():
    """Histogram of KnowBlock.confidence (know outcomes only).

    #209 phase 2: pairs with helix_know_decision_total — the confidence
    distribution calibrates [know] emit_floor per corpus, and judged eval
    runs map it to P(correct) (goal-gates spec 2026-07-01).
    """
    if "know_confidence" not in _instruments:
        _instruments["know_confidence"] = meter.create_histogram(
            "helix_know_confidence",
            unit="1",
            description="KnowBlock confidence distribution, know outcomes only.",
        )
    return _instruments["know_confidence"]


def abstain_counter():
    """Counter of ABSTAIN gate fires with trigger attribution.

    #209 phase 2: gate is floor_and_ratio (additive: both the absolute
    floor and the ratio tripped) or ratio_only (RRF: absolute floors
    bypassed). Balancing partner of helix_splice_ratio — tuning that
    raises compression but spikes abstains is a net loss.
    """
    if "abstain" not in _instruments:
        _instruments["abstain"] = meter.create_counter(
            "helix_abstain_total",
            description="ABSTAIN gate fires, labelled by gate "
                        "(floor_and_ratio | ratio_only) and fusion_mode.",
        )
    return _instruments["abstain"]


def freshness_demotion_counter():
    """Counter of demotion-relevant freshness verdicts.

    #209 phase 2: status ∈ {stale, missing, unknown, superseded};
    "fresh" is the common case and is not emitted (volume). The
    stale-answer-suppression activity behind MissBlock stale/superseded.
    """
    if "freshness_demotion" not in _instruments:
        _instruments["freshness_demotion"] = meter.create_counter(
            "helix_freshness_demotion_total",
            description="Freshness demotion-relevant events, labelled by "
                        "status (stale | missing | unknown | superseded).",
        )
    return _instruments["freshness_demotion"]


def session_elided_counter():
    """Counter of documents replaced by an elision stub.

    #209 phase 2: the event-count companion of
    helix_session_tokens_saved_total (docs elided vs tokens saved).
    """
    if "session_elided" not in _instruments:
        _instruments["session_elided"] = meter.create_counter(
            "helix_session_elided_total",
            description="Documents elided by the session working-set register.",
        )
    return _instruments["session_elided"]


def pki_candidates_histogram():
    """Histogram of documents hit by >=1 path_key_index pair per query.

    #209 phase 2 / roadmap §3b-1: input to the R@1 AND-route program's
    collision census.
    """
    if "pki_candidates" not in _instruments:
        _instruments["pki_candidates"] = meter.create_histogram(
            "helix_pki_candidates",
            unit="genes",
            description="Documents hit by >=1 path_key_index pair per query.",
        )
    return _instruments["pki_candidates"]


def pki_pairs_skipped_counter():
    """Counter of PKI pairs skipped by the noise cutoff.

    #209 phase 2: unique (path_token, kv_key) pairs with cardinality >
    PKI_NOISE_CUTOFF per query. High values = the index doing inventory,
    not pruning (the #165 lesson).
    """
    if "pki_pairs_skipped" not in _instruments:
        _instruments["pki_pairs_skipped"] = meter.create_counter(
            "helix_pki_pairs_skipped_total",
            description="path_key_index pairs skipped by the noise cutoff.",
        )
    return _instruments["pki_pairs_skipped"]


def fingerprint_filtered_counter():
    """Counter of /fingerprint candidates dropped, labelled by cause.

    #209 phase 2 / roadmap §3b-10: cause ∈ {floor, cap} — calibrates
    score_floor / max_results for agentic navigation (SNOW-2 arms C/E).
    """
    if "fingerprint_filtered" not in _instruments:
        _instruments["fingerprint_filtered"] = meter.create_counter(
            "helix_fingerprint_filtered_total",
            description="/fingerprint candidates dropped, labelled by cause "
                        "(floor | cap).",
        )
    return _instruments["fingerprint_filtered"]


def ingest_vram_gauge():
    """Gauge of CUDA memory sampled once per dense ingest batch.

    #209 phase 2 / roadmap §3b-9: tracks the #176/#177 OOM class live
    instead of post-mortem. No-op without torch/CUDA.
    """
    if "ingest_vram" not in _instruments:
        _instruments["ingest_vram"] = meter.create_gauge(
            "helix_ingest_vram_bytes",
            unit="By",
            description="torch.cuda.memory_allocated per dense ingest batch.",
        )
    return _instruments["ingest_vram"]


def _emit_snapshot_values(
    *,
    chrom_rows: list[tuple[Any, Any]],
    edge_rows: list[tuple[Any, Any]],
    raw_chars: Optional[int],
    compressed_chars: Optional[int],
    in_degrees: list[int],
) -> None:
    """Write an aggregate knowledge store snapshot into the OTel gauges."""
    chrom_gauge = chromatin_state_counter()
    for state, n in chrom_rows:
        label = {0: "open", 1: "euchromatin", 2: "heterochromatin"}.get(
            int(state) if state is not None else 0, "unknown",
        )
        chrom_gauge.set(int(n), {"state": label})

    edges_gauge = harmonic_edges_counter()
    for source, n in edge_rows:
        edges_gauge.set(int(n), {"source": source or "unknown"})

    size_gauge = genome_size_gauge()
    if raw_chars is not None:
        size_gauge.set(int(raw_chars), {"kind": "raw"})
    if compressed_chars is not None:
        size_gauge.set(int(compressed_chars), {"kind": "compressed"})

    if in_degrees:
        in_degrees.sort()
        n = len(in_degrees)
        mean_deg = sum(in_degrees) / n
        top_1pct_count = max(1, n // 100)
        top_1pct_mean = sum(in_degrees[-top_1pct_count:]) / top_1pct_count
        ratio = top_1pct_mean / mean_deg if mean_deg > 0 else 0.0

        hub_concentration_gauge().set(float(ratio))
        deg_gauge = hub_inbound_degree_gauge()
        deg_gauge.set(float(in_degrees[-1]), {"stat": "max"})
        deg_gauge.set(float(in_degrees[int(n * 0.99) - 1]), {"stat": "p99"})
        deg_gauge.set(float(in_degrees[int(n * 0.95) - 1]), {"stat": "p95"})
        deg_gauge.set(float(in_degrees[n // 2]), {"stat": "p50"})
        deg_gauge.set(float(mean_deg), {"stat": "mean"})


def _emit_sharded_gauges_snapshot(genome) -> None:
    """Aggregate telemetry directly from registered shard DBs."""
    router = getattr(genome, "_router", None)
    main_conn = getattr(router, "main_conn", None)
    if main_conn is None:
        return

    shard_rows = main_conn.execute(
        "SELECT path FROM shards WHERE health = 'ok'"
    ).fetchall()
    chrom_counts: dict[int | None, int] = {}
    edge_counts: dict[str, int] = {}
    inbound_counts: dict[str, int] = {}
    raw_chars = 0
    compressed_chars = 0

    for row in shard_rows:
        shard_path = row["path"] if isinstance(row, sqlite3.Row) else row[0]
        if not shard_path or not os.path.exists(shard_path):
            continue

        conn = sqlite3.connect(shard_path)
        try:
            for state, count in conn.execute(
                "SELECT chromatin, COUNT(*) FROM genes GROUP BY chromatin"
            ).fetchall():
                chrom_counts[state] = chrom_counts.get(state, 0) + int(count)

            for source, count in conn.execute(
                "SELECT source, COUNT(*) FROM harmonic_links GROUP BY source"
            ).fetchall():
                label = source or "unknown"
                edge_counts[label] = edge_counts.get(label, 0) + int(count)

            row = conn.execute(
                "SELECT "
                "COALESCE(SUM(LENGTH(content)), 0) AS raw, "
                "COALESCE(SUM(LENGTH(complement)), 0) AS compressed "
                "FROM genes WHERE chromatin=0"
            ).fetchone()
            if row:
                raw_chars += int(row[0] or 0)
                compressed_chars += int(row[1] or 0)

            for gene_id_b, count in conn.execute(
                "SELECT gene_id_b, COUNT(*) FROM harmonic_links GROUP BY gene_id_b"
            ).fetchall():
                inbound_counts[gene_id_b] = inbound_counts.get(gene_id_b, 0) + int(count)
        except sqlite3.OperationalError:
            # Best-effort metrics path: a stale or mid-migration shard should
            # not spam warnings or break /stats.
            continue
        finally:
            conn.close()

    _emit_snapshot_values(
        chrom_rows=list(chrom_counts.items()),
        edge_rows=list(edge_counts.items()),
        raw_chars=raw_chars,
        compressed_chars=compressed_chars,
        in_degrees=list(inbound_counts.values()),
    )


def emit_gauges_snapshot(genome) -> None:
    """Poll-driven gauges for lifecycle tier + harmonic-edges + knowledge store size.

    Prometheus scrapes via the collector every 15s; we refresh these
    absolute-value metrics on each /stats call (cheap DB queries) so
    the dashboard gauges track live state instead of event stream.
    No-op when OTel is off — the noop instruments just drop the calls.
    """
    try:
        if getattr(genome, "_sharded_adapter", False):
            _emit_sharded_gauges_snapshot(genome)
            return

        cur = genome.read_conn.cursor()
        chrom = cur.execute(
            "SELECT chromatin, COUNT(*) FROM genes GROUP BY chromatin"
        ).fetchall()
        edges = cur.execute(
            "SELECT source, COUNT(*) FROM harmonic_links GROUP BY source"
        ).fetchall()
        row = cur.execute(
            "SELECT "
            "SUM(LENGTH(content)) AS raw, "
            "SUM(LENGTH(complement)) AS compressed "
            "FROM genes WHERE chromatin=0"
        ).fetchone()
        in_degrees = [
            int(n) for (_, n) in cur.execute(
                "SELECT gene_id_b, COUNT(*) FROM harmonic_links GROUP BY gene_id_b"
            ).fetchall()
        ]
        _emit_snapshot_values(
            chrom_rows=[(r[0], r[1]) for r in chrom],
            edge_rows=[(r[0], r[1]) for r in edges],
            raw_chars=int(row[0] or 0) if row else 0,
            compressed_chars=int(row[1] or 0) if row else 0,
            in_degrees=in_degrees,
        )
    except Exception:
        # Promoted from debug to warning: silent debug-level was hiding a
        # real failure (lifecycle tier gauge would emit, harmonic/hub/genome_size
        # would silently disappear). If you see this in normal operation,
        # the SQL inside this function raised — likely a stale read_conn
        # schema cache or a replica-vs-master path mismatch.
        log.warning("emit_gauges_snapshot failed", exc_info=True)
