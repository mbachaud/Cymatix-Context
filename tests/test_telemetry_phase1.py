"""#209 phase 1 telemetry tests.

Two contracts:

1. **No-op safety** — with OTel disabled (the default test environment),
   the six new instrument getters return cached no-op instruments and
   every new record path (dense cosine, shard fan-out/discrimination,
   know decision, session-elision savings, splice ratio) executes
   without raising.

2. **No phantom dashboard metrics** — every ``helix_*`` metric name
   referenced by any shipped Grafana dashboard JSON must correspond to
   an instrument actually created in ``helix_context/telemetry``,
   after applying the OTel-collector Prometheus name translation
   (unit suffix appended unless already present, ``_total`` appended
   to counters, ``_bucket``/``_sum``/``_count`` series for
   histograms). This is the regression test that kills future
   phantoms like the eight garbled names the pipeline-observatory
   dashboard shipped with (#209).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from helix_context.telemetry import otel

REPO = Path(__file__).resolve().parent.parent
DASHBOARD_DIRS = (
    REPO / "deploy" / "otel" / "grafana" / "dashboards",
    REPO / "docs" / "dashboards",
)

# Metric names emitted at runtime outside the lazy-getter registry
# (none today). Add here ONLY for dynamically-constructed names.
RUNTIME_EMITTED_WHITELIST: frozenset[str] = frozenset()

NEW_GETTERS = (
    "dense_cosine_histogram",
    "shard_fanout_histogram",
    "shard_discrimination_histogram",
    "know_decision_counter",
    "session_tokens_saved_counter",
    "splice_ratio_histogram",
)

NEW_METRIC_NAMES = (
    "helix_dense_cosine",
    "helix_shard_fanout",
    "helix_shard_discrimination",
    "helix_know_decision_total",
    "helix_session_tokens_saved_total",
    "helix_splice_ratio",
)

OBSERVATORY_PHANTOMS = (
    "helix_chroni_join_state",
    "helix_cost_concentration_ratio",
    "helix_crdt_bucket_accumulation",
    "helix_resolve_degree_distribution",
    "helix_ring_edges_by_provenance",
    "helix_rq_duration_seconds",
    "helix_tier_estimation_percent",
    "helix_tier_readable_time",
)


# ---------------------------------------------------------------------------
# Instrument registry introspection
# ---------------------------------------------------------------------------


class _RecordingMeter:
    """Captures (kind, name, unit) for every create_* call."""

    def __init__(self):
        self.created: list[tuple[str, str, str | None]] = []

    def _make(self, kind):
        def create(name, unit=None, description=None, **kw):
            self.created.append((kind, name, unit))
            return otel._NoopInstrument()
        return create

    def __getattr__(self, attr):
        if attr.startswith("create_"):
            kind = attr[len("create_"):]
            return self._make(kind)
        raise AttributeError(attr)


def _registered_instruments(monkeypatch):
    """Call every lazy getter against a recording meter."""
    rec = _RecordingMeter()
    monkeypatch.setattr(otel, "meter", rec)
    monkeypatch.setattr(otel, "_instruments", {})
    for name in dir(otel):
        if name.startswith("_"):
            continue
        if not name.endswith(("_histogram", "_counter", "_gauge")):
            continue
        getter = getattr(otel, name)
        if callable(getter):
            getter()
    assert rec.created, "no instruments registered — getter scan broke"
    return rec.created


_PROM_UNIT_MAP = {"s": "seconds", "ms": "milliseconds", "By": "bytes"}


def _prometheus_names(kind: str, name: str, unit: str | None) -> set[str]:
    """Names the OTel collector's Prometheus exporter would publish."""
    base = name
    if unit and not unit.startswith("{"):
        translated = _PROM_UNIT_MAP.get(unit, unit)
        # "1" only suffixes gauges (as _ratio); skip for simplicity —
        # no helix instrument uses it.
        if translated != "1" and not base.endswith(f"_{translated}"):
            base = f"{base}_{translated}"
    out = {base}
    if kind == "counter" and not base.endswith("_total"):
        out = {f"{base}_total"}
    elif kind == "histogram":
        out |= {f"{base}_bucket", f"{base}_sum", f"{base}_count"}
    return out


def _dashboard_metric_refs():
    """helix_* tokens from every expr/query/definition in every dashboard."""
    refs: dict[str, set[str]] = {}

    def walk(node, sink):
        if isinstance(node, dict):
            for key, val in node.items():
                if key in ("expr", "query", "definition") and isinstance(val, str):
                    sink |= set(re.findall(r"\bhelix_[a-z0-9_]+", val))
                else:
                    walk(val, sink)
        elif isinstance(node, list):
            for item in node:
                walk(item, sink)

    for d in DASHBOARD_DIRS:
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.json")):
            sink: set[str] = set()
            walk(json.loads(path.read_text(encoding="utf-8")), sink)
            refs[path.name] = sink
    return refs


# ---------------------------------------------------------------------------
# 1. No-op safety with OTel disabled
# ---------------------------------------------------------------------------


def test_new_getters_return_cached_noop_instruments():
    for getter_name in NEW_GETTERS:
        getter = getattr(otel, getter_name)
        first = getter()
        assert getter() is first, f"{getter_name} not cached"


def test_new_record_paths_do_not_raise_when_otel_disabled():
    otel.dense_cosine_histogram().record(0.42, {"arm": "hot"})
    otel.dense_cosine_histogram().record(0.17, {"arm": "cold"})
    otel.shard_fanout_histogram().record(3)
    otel.shard_discrimination_histogram().record(0.5)
    otel.know_decision_counter().add(1, {"outcome": "know", "reason": "none"})
    otel.session_tokens_saved_counter().add(120)
    otel.splice_ratio_histogram().record(4.2, {"caller_model_class": "generic"})


def test_instrumented_modules_import_cleanly():
    import helix_context.context_manager  # noqa: F401
    import helix_context.knowledge_store  # noqa: F401
    import helix_context.scoring.know_decision  # noqa: F401
    import helix_context.shard_router  # noqa: F401


# ---------------------------------------------------------------------------
# 2. Call sites
# ---------------------------------------------------------------------------


class _CounterRecorder:
    def __init__(self):
        self.calls = []

    def add(self, value, attrs=None):
        self.calls.append((value, dict(attrs or {})))

    def record(self, value, attrs=None):
        self.calls.append((value, dict(attrs or {})))


def _window(status="aligned", genes_expressed=1):
    from helix_context.schemas import ContextHealth, ContextWindow
    return ContextWindow(
        ribosome_prompt="",
        expressed_context="ctx",
        context_health=ContextHealth(status=status, genes_expressed=genes_expressed),
    )


def test_know_decision_survives_broken_telemetry(monkeypatch):
    from helix_context.scoring.know_decision import decide_know_or_miss
    from helix_context.schemas import KnowBlock

    def boom():
        raise RuntimeError("telemetry down")

    monkeypatch.setattr("helix_context.telemetry.know_decision_counter", boom)
    block = decide_know_or_miss(
        _window("aligned"),
        query="q",
        top_score=1.0,
        score_gap=0.5,
        lexical_dense_agree=True,
        coordinate_confidence=1.0,
    )
    assert isinstance(block, KnowBlock)


def test_shard_router_records_fanout_and_discrimination(tmp_path, monkeypatch):
    from helix_context.genome import Genome
    from helix_context.shard_schema import init_main_db, open_main_db, register_shard
    from helix_context.sharding import ShardedGenomeAdapter
    from tests.conftest import make_gene

    main_path = tmp_path / "main.genome.db"
    shard_path = tmp_path / "projects.genome.db"

    main_conn = open_main_db(str(main_path))
    init_main_db(main_conn)

    shard = Genome(str(shard_path))
    try:
        shard.upsert_gene(make_gene("auth uses jwt", domains=["auth"]), apply_gate=False)
        register_shard(
            main_conn,
            shard_name="projects",
            category="reference",
            path=str(shard_path),
            gene_count=1,
            byte_size=shard_path.stat().st_size,
        )
    finally:
        shard.close()
    main_conn.close()

    fanout = _CounterRecorder()
    discrimination = _CounterRecorder()
    monkeypatch.setattr(
        "helix_context.telemetry.shard_fanout_histogram", lambda: fanout
    )
    monkeypatch.setattr(
        "helix_context.telemetry.shard_discrimination_histogram",
        lambda: discrimination,
    )

    adapter = ShardedGenomeAdapter(str(main_path))
    try:
        # Empty terms take the route() fallback (all healthy shards):
        # fanout = 1 shard consulted, discrimination = 1/1.
        adapter.query_docs(domains=[], entities=[])
        # Terms that match no fingerprint row route to zero shards:
        # fanout = 0; discrimination 0/1 is recorded too.
        adapter.query_docs(domains=["nomatchterm"], entities=[])
    finally:
        adapter.close()

    assert [v for v, _ in fanout.calls] == [1, 0]
    assert [v for v, _ in discrimination.calls] == [1.0, 0.0]


def test_dense_cosine_recorded_on_hot_merge(tmp_path, monkeypatch):
    from helix_context.genome import Genome
    from tests.conftest import make_gene

    genome = Genome(str(tmp_path / "genome.db"))
    try:
        gene = make_gene("auth uses jwt tokens", domains=["auth"])
        genome.upsert_gene(gene, apply_gate=False)

        rec = _CounterRecorder()
        monkeypatch.setattr(
            "helix_context.telemetry.dense_cosine_histogram", lambda: rec
        )
        monkeypatch.setattr(genome, "_dense_embedding_enabled", True)
        monkeypatch.setattr(
            genome,
            "query_docs_dense_recall",
            lambda *a, **kw: [(gene.gene_id, 0.42)],
        )

        genome.query_docs(domains=["auth"], entities=[])
    finally:
        genome.close()

    assert (0.42, {"arm": "hot"}) in rec.calls


# ---------------------------------------------------------------------------
# 3. Dashboards reference only real instruments (phantom killer)
# ---------------------------------------------------------------------------


def test_dashboards_reference_only_real_instruments(monkeypatch):
    created = _registered_instruments(monkeypatch)
    known: set[str] = set(RUNTIME_EMITTED_WHITELIST)
    for kind, name, unit in created:
        known |= _prometheus_names(kind, name, unit)

    refs = _dashboard_metric_refs()
    assert refs, "no dashboard JSONs found"

    phantoms = {
        fname: sorted(n for n in names if n not in known)
        for fname, names in refs.items()
    }
    phantoms = {f: n for f, n in phantoms.items() if n}
    assert not phantoms, (
        "Dashboard(s) chart metric names with no creating instrument in "
        f"helix_context/telemetry: {phantoms}. Either add the instrument "
        "or repoint the panel at a real metric (#209)."
    )


def test_registry_covers_the_new_209_instruments(monkeypatch):
    created = {name for _, name, _ in _registered_instruments(monkeypatch)}
    for metric in NEW_METRIC_NAMES:
        assert metric in created, f"{metric} missing from telemetry registry"


def test_observatory_phantom_names_are_gone():
    for d in DASHBOARD_DIRS:
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.json")):
            body = path.read_text(encoding="utf-8")
            for phantom in OBSERVATORY_PHANTOMS:
                assert phantom not in body, f"{path.name} still references {phantom}"


def test_every_new_instrument_has_a_dashboard_panel():
    refs = _dashboard_metric_refs()
    all_refs = set().union(*refs.values()) if refs else set()
    for metric in NEW_METRIC_NAMES:
        assert any(r == metric or r.startswith(f"{metric}_") for r in all_refs), (
            f"{metric} has no panel in any shipped dashboard JSON"
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
