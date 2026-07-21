"""Tests for the pipeline span wiring (telemetry blind-spot audit 2026-07-08).

``pipeline_stage_span`` existed in ``cymatix_context.telemetry.otel`` with zero
callers; ``rrf_fused_score_histogram`` was imported by ``knowledge_store`` but
never defined. This file pins the fixes:

  - every pipeline stage opens a ``helix.pipeline.<stage>`` span during a
    seeded ``build_context`` run, nested under a
    ``helix.pipeline.build_context`` root span;
  - the persist stage (``learn``) opens its own span and histogram point
    (it runs as a background task, outside the request root);
  - all seven stages feed ``helix_pipeline_stage_seconds`` exactly once;
  - ``rrf_fused_score_histogram`` is exported, creates
    ``helix_rrf_fused_score``, records at the query_genes RRF call site;
  - the whole surface is a silent no-op with OTel disabled (the default
    test environment — ``otel.tracer``/``otel.meter`` are the noop
    stand-ins unless ``setup_telemetry`` ran).

Seams follow test_telemetry_pipeline.py (module-global monkeypatch on the
``_pipeline_stage_*`` names) and test_telemetry_phase1.py (recording meter
over the lazy-getter registry).
"""

from __future__ import annotations

from collections import Counter
from contextlib import nullcontext

import pytest

import cymatix_context.context_manager as cm_mod
from cymatix_context.config import BudgetConfig
from cymatix_context.context_manager import HelixContextManager
from cymatix_context.telemetry import otel

from tests.conftest import MockCompressorBackend, make_gene, make_helix_config


# ── Helpers ──────────────────────────────────────────────────────────


class _RecordingInstrument:
    """Histogram/counter stand-in that captures record()/add() calls."""

    def __init__(self):
        self.calls: list[tuple[float, dict]] = []

    def record(self, value, attributes=None):
        self.calls.append((value, dict(attributes or {})))

    def add(self, value, attributes=None):
        self.calls.append((value, dict(attributes or {})))


class _FakeSpan:
    """Span stand-in capturing set_attribute calls."""

    def __init__(self):
        self.attributes: dict = {}

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def record_exception(self, exc):
        pass


class _RecordingTracer:
    """Tracer stand-in logging ("start"/"end", span_name) events in order."""

    def __init__(self):
        self.events: list[tuple[str, str]] = []
        self.spans: dict[str, _FakeSpan] = {}

    def start_as_current_span(self, name, *a, **kw):
        tracer = self

        class _CM:
            def __enter__(cm_self):
                tracer.events.append(("start", name))
                span = _FakeSpan()
                tracer.spans[name] = span
                return span

            def __exit__(cm_self, *exc):
                tracer.events.append(("end", name))
                return None

        return _CM()


# ── Fixtures (test_pipeline.py shape: mock backend, in-memory genome) ─


@pytest.fixture
def helix():
    config = make_helix_config(
        budget=BudgetConfig(max_genes_per_turn=4, splice_aggressiveness=0.5),
        synonym_map={"auth": ["jwt", "login", "security"]},
    )
    mgr = HelixContextManager(config)
    mgr.ribosome.backend = MockCompressorBackend()
    yield mgr
    mgr.close()


@pytest.fixture
def seeded_helix(helix):
    genes = [
        make_gene("Authentication middleware with JWT validation",
                  domains=["auth", "security"], entities=["jwt"],
                  gene_id="auth_gene_00001"),
        make_gene("Database connection pooling and query optimization",
                  domains=["database", "performance"], entities=["postgres"],
                  gene_id="db_gene_000001"),
        make_gene("REST API rate limiting and throttling patterns",
                  domains=["api", "performance", "security"], entities=["redis"],
                  gene_id="api_gene_00001"),
    ]
    for g in genes:
        helix.genome.upsert_gene(g)
    return helix


# ── (a) per-stage spans fire during build_context ────────────────────


def test_stage_spans_open_for_each_stage(seeded_helix, monkeypatch):
    """One span per stage, in pipeline order, root first."""
    stages: list[str] = []

    def fake_span(stage, **kw):
        stages.append(stage)
        return nullcontext()

    monkeypatch.setattr(cm_mod, "_pipeline_stage_span", fake_span)

    window = seeded_helix.build_context("How does JWT auth work?")
    assert window.metadata.get("genes_expressed", 0) >= 1, (
        "seeded query must express genes — otherwise the rerank/splice/"
        "assemble stages never run and this test asserts nothing"
    )
    assert stages == [
        "build_context", "classify", "extract", "express",
        "rerank", "splice", "assemble",
    ]


def test_early_return_still_opens_root_and_early_stages(helix, monkeypatch):
    """Empty genome returns after express — root span still brackets it."""
    stages: list[str] = []

    def fake_span(stage, **kw):
        stages.append(stage)
        return nullcontext()

    monkeypatch.setattr(cm_mod, "_pipeline_stage_span", fake_span)

    window = helix.build_context("anything")
    assert "<helix:no_match" in window.expressed_context
    assert stages == ["build_context", "classify", "extract", "express"]


# ── (b) root span wraps the per-stage spans ──────────────────────────


def test_root_span_named_build_context_wraps_pipeline(seeded_helix, monkeypatch):
    tracer = _RecordingTracer()
    monkeypatch.setattr(otel, "tracer", tracer)

    seeded_helix.build_context("How does JWT auth work?")

    root = "helix.pipeline.build_context"
    assert tracer.events[0] == ("start", root)
    assert tracer.events[-1] == ("end", root)

    started = {name for kind, name in tracer.events if kind == "start"}
    assert started >= {
        root,
        "helix.pipeline.classify",
        "helix.pipeline.extract",
        "helix.pipeline.express",
        "helix.pipeline.rerank",
        "helix.pipeline.splice",
        "helix.pipeline.assemble",
    }
    # Every span that started also ended (no leaked spans on any path).
    assert Counter(n for k, n in tracer.events if k == "start") == \
        Counter(n for k, n in tracer.events if k == "end")
    # pipeline_stage_span stamps the stage attribute on each span.
    assert tracer.spans[root].attributes.get("helix.pipeline.stage") == \
        "build_context"
    assert tracer.spans["helix.pipeline.splice"].attributes.get(
        "helix.pipeline.stage") == "splice"


# ── persist stage (background pack) ──────────────────────────────────


def test_persist_stage_span_and_histogram_on_learn(helix, monkeypatch):
    stages: list[str] = []

    def fake_span(stage, **kw):
        stages.append(stage)
        return nullcontext()

    recorder = _RecordingInstrument()
    monkeypatch.setattr(cm_mod, "_pipeline_stage_span", fake_span)
    monkeypatch.setattr(cm_mod, "_pipeline_stage_histogram", lambda: recorder)

    gid = helix.learn("test query", "test response")
    assert gid is not None

    assert "persist" in stages
    assert "persist" in [attrs.get("stage") for _, attrs in recorder.calls]


# ── all seven stages feed helix_pipeline_stage_seconds once each ─────


def test_all_seven_stages_recorded_exactly_once(seeded_helix, monkeypatch):
    recorder = _RecordingInstrument()
    monkeypatch.setattr(cm_mod, "_pipeline_stage_histogram", lambda: recorder)

    seeded_helix.build_context("How does JWT auth work?")
    seeded_helix.learn("test query", "test response")

    counts = Counter(attrs.get("stage") for _, attrs in recorder.calls)
    assert counts == Counter({
        "classify": 1, "extract": 1, "express": 1, "rerank": 1,
        "splice": 1, "assemble": 1, "persist": 1,
    }), f"stage histogram counts off: {dict(counts)}"


# ── (c) rrf_fused_score_histogram: export, name, call site ───────────


class _RecordingMeter:
    """Captures (kind, name) for create_* calls (test_telemetry_phase1 shape)."""

    def __init__(self):
        self.created: list[tuple[str, str]] = []

    def create_histogram(self, name, unit=None, description=None, **kw):
        self.created.append(("histogram", name))
        return otel._NoopInstrument()


def test_rrf_fused_score_histogram_importable_and_named(monkeypatch):
    from cymatix_context.telemetry import rrf_fused_score_histogram

    rec = _RecordingMeter()
    monkeypatch.setattr(otel, "meter", rec)
    monkeypatch.setattr(otel, "_instruments", {})

    inst = rrf_fused_score_histogram()
    assert ("histogram", "helix_rrf_fused_score") in rec.created
    # Lazy getter caches: repeated calls return the same instrument.
    assert rrf_fused_score_histogram() is inst


def test_rrf_fused_score_recorded_at_query_genes_call_site(tmp_path, monkeypatch):
    """query_genes under fusion_mode=rrf records one point per ranked doc."""
    from cymatix_context.genome import Genome

    genome = Genome(str(tmp_path / "genome.db"), fusion_mode="rrf")
    try:
        gene = make_gene("auth uses jwt tokens", domains=["auth"])
        genome.upsert_gene(gene, apply_gate=False)

        rec = _RecordingInstrument()
        monkeypatch.setattr(
            "cymatix_context.telemetry.rrf_fused_score_histogram", lambda: rec
        )

        genome.query_docs(domains=["auth"], entities=[])
    finally:
        genome.close()

    assert rec.calls, "no rrf fused score recorded under fusion_mode=rrf"
    values = [v for v, _ in rec.calls]
    labels = [attrs for _, attrs in rec.calls]
    assert all(isinstance(v, float) for v in values)
    # Attribute-less by design — a per-gene_id label would mint one
    # Prometheus series set per document.
    assert all(not attrs for attrs in labels)


# ── (d) no-op path: OTel disabled must not raise or change behavior ──


def test_build_context_and_learn_unchanged_with_otel_disabled(seeded_helix):
    """Default test env: otel.tracer/meter are the noop stand-ins.

    No monkeypatching — the real span/histogram wrappers run against the
    noop tracer/meter and must neither raise nor alter the window.
    """
    window = seeded_helix.build_context("How does JWT auth work?")
    assert window.metadata.get("genes_expressed", 0) >= 1
    assert "<expressed_context>" in window.expressed_context

    gid = seeded_helix.learn("Why is auth slow?", "JWT validation hits the DB.")
    assert gid is not None


def test_rrf_record_is_noop_when_otel_disabled():
    otel.rrf_fused_score_histogram().record(0.0166)


def test_pipeline_stage_span_is_noop_safe():
    """pipeline_stage_span works as a CM against the noop tracer."""
    with otel.pipeline_stage_span("classify") as span:
        span.set_attribute("helix.pipeline.stage", "classify")  # must not raise


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
