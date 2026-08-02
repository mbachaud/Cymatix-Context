"""Phase-0 perf instrumentation (2026-08 latency/concurrency campaign, slice 1).

Closes the stage-timing blind spots the campaign needs before any latency
receipt can be trusted (docs ref: PERF campaign plan, A.1 item 1 / W0.2-W0.3):

  - ``_stage_timer(extra=...)`` merges a payload dict into the pipeline
    ring entry so harnesses can read sub-stage detail over HTTP with no
    OTel collector.
  - ``KnowledgeStore.query_docs`` publishes ``last_signal_timings`` — a
    per-query {signal: ms} map covering the existing seven tier signals
    plus the previously-untimed lex_anchor / hebbian / body_fetch /
    coact_expand regions.
  - ``query_docs_ann`` merges its own leg timings (ann_dense_leg /
    ann_body_fetch) into the same map.
  - ``KnowledgeStore.commit_count`` counts every COMMIT on the writer
    connection — including raw ``conn.commit()`` calls from "unlocked
    committer" call sites (Registry, session_delivery, cwola) that
    bypass ``_write_lock``.
  - ``telemetry.record_model_load()`` accumulates encoder load time into
    ``telemetry.model_load_ms()`` so cold-start receipts can separate
    model load from retrieval work.
  - End-to-end: a ``POST /context`` turn rings ``tail_writes`` /
    ``delivery_lookup`` / ``delivery_log`` / ``cwola`` stages and the
    ``express`` entry carries the ``signals`` sub-map.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Make benchmarks/dogfood importable as a flat module path (repo test
# convention — see tests/test_bench_orchestrator.py).
_BENCH_DOGFOOD = Path(__file__).resolve().parents[1] / "benchmarks" / "dogfood"
if str(_BENCH_DOGFOOD) not in sys.path:
    sys.path.insert(0, str(_BENCH_DOGFOOD))

import cymatix_context.context_manager as cm_mod
import cymatix_context.telemetry as telemetry_mod
from cymatix_context.context_manager import _stage_timer
from cymatix_context.genome import Genome

from tests.conftest import FakeBGEM3Codec, hash_vec, make_client, make_gene


# ── Helpers ──────────────────────────────────────────────────────────


class _RecordingInstrument:
    def __init__(self):
        self.observations: list[tuple[float, dict]] = []

    def record(self, value: float, attributes: dict | None = None):
        self.observations.append((value, attributes or {}))


def _populate_v2(genome: Genome, gene_id: str, vec) -> None:
    """Write a v2 dense BLOB directly, bypassing the codec (test seam)."""
    blob = vec.astype("<f4").tobytes()
    genome.conn.execute(
        "UPDATE genes SET embedding_dense_v2 = ? WHERE gene_id = ?",
        (sqlite3.Binary(blob), gene_id),
    )
    genome.conn.commit()
    genome._invalidate_dense_matrix()


def _ring_entries_since(mark: int) -> list[dict]:
    return list(cm_mod._pipeline_events)[mark:]


# ── 1. _stage_timer extra payload → ring entry ───────────────────────


def test_stage_timer_extra_merges_into_ring_entry(monkeypatch):
    """extra() keys land on the ring entry alongside request_id/stage/ms/ts."""
    monkeypatch.setattr(cm_mod, "_pipeline_stage_histogram",
                        lambda: _RecordingInstrument())
    token = cm_mod._pipeline_request_id.set("req-extra-test")
    try:
        with _stage_timer("tail_writes", extra=lambda: {"commits": 7}):
            pass
    finally:
        cm_mod._pipeline_request_id.reset(token)

    # Filter by the unique request id, not a length mark — a full deque
    # rotates instead of growing, which silently empties since-mark slices.
    entries = [e for e in cm_mod._pipeline_events
               if e.get("request_id") == "req-extra-test"]
    assert len(entries) == 1
    assert entries[0]["stage"] == "tail_writes"
    assert entries[0]["commits"] == 7
    # Reserved keys must not be clobbered by the payload.
    assert entries[0]["request_id"] == "req-extra-test"


def test_stage_timer_extra_errors_are_swallowed(monkeypatch):
    """A raising extra() must not break the pipeline or drop the entry."""
    monkeypatch.setattr(cm_mod, "_pipeline_stage_histogram",
                        lambda: _RecordingInstrument())

    def _boom():
        raise RuntimeError("extra deliberately broken")

    token = cm_mod._pipeline_request_id.set("req-extra-boom")
    try:
        with _stage_timer("tail_writes", extra=_boom):
            pass  # must not raise
    finally:
        cm_mod._pipeline_request_id.reset(token)

    entries = [e for e in cm_mod._pipeline_events
               if e.get("request_id") == "req-extra-boom"]
    assert len(entries) == 1, "entry must still ring without the extra payload"
    assert "commits" not in entries[0]


# ── 2. query_docs publishes last_signal_timings ──────────────────────


def test_query_docs_publishes_signal_timings(genome):
    for i in range(3):
        genome.upsert_gene(make_gene(
            f"alpha content body number {i}", domains=["alpha"],
            gene_id=f"sig-{i}",
        ))

    genome.query_docs(["alpha"], [], max_genes=5)

    timings = genome.last_signal_timings
    assert isinstance(timings, dict) and timings, (
        "query_docs must publish a non-empty last_signal_timings map"
    )
    # Previously-untimed regions now have first-class signals.
    for key in ("lex_anchor", "body_fetch", "coact_expand"):
        assert key in timings, f"missing signal timing: {key}"
    # Existing tier signals are mirrored into the same map.
    assert "tag_exact" in timings
    assert all(v >= 0.0 for v in timings.values())


def test_query_docs_ann_merges_leg_timings():
    g = Genome(
        path=":memory:",
        dense_embedding_enabled=True,
        dense_embedding_dim=1024,
        dense_pool_size=50,
    )
    try:
        for i in range(5):
            gid = f"lex-{i}"
            g.upsert_gene(
                make_gene(f"alpha lex body {i}", domains=["alpha"], gene_id=gid),
                apply_gate=False,
            )
            _populate_v2(g, gid, hash_vec(f"vec {gid}", 1024))
        # Dense-only needle: no lex overlap with the query domains, so it
        # reaches the union as a missing id and exercises ann_body_fetch.
        g.upsert_gene(
            make_gene("needle body text", domains=["zeta"], gene_id="needle-1"),
            apply_gate=False,
        )
        _populate_v2(g, "needle-1", hash_vec("needle target", 1024))
        g._dense_codec = FakeBGEM3Codec(dim=1024, query_target="needle target")

        g.query_docs_ann("what is the needle", domains=["alpha"], entities=[],
                         max_genes=6)

        timings = g.last_signal_timings
        assert "ann_dense_leg" in timings
        assert "ann_body_fetch" in timings
        # The inner query_docs signal map must survive the merge.
        assert "body_fetch" in timings
    finally:
        g.close()


def test_signal_timings_reset_on_raising_query(genome):
    """A query that raises (zero tier matches) must not leave the previous
    query's timing map behind — stale timings would mis-attribute the next
    ring entry."""
    from cymatix_context.knowledge_store import PromoterMismatch

    genome.upsert_gene(make_gene("alpha body", domains=["alpha"], gene_id="st-1"))
    genome.query_docs(["alpha"], [], max_genes=5)
    assert genome.last_signal_timings  # sanity: previous query populated it

    with pytest.raises(PromoterMismatch):
        genome.query_docs(["nomatchterm"], [], max_genes=5)

    assert genome.last_signal_timings == {}, (
        "raising query must clear, not retain, the prior timing map"
    )


# ── 3. commit counter on the writer connection ───────────────────────


def test_commit_count_increments_on_upsert(genome):
    before = genome.commit_count
    genome.upsert_gene(make_gene("commit counting body", domains=["cc"]))
    assert genome.commit_count > before


def test_commit_count_sees_raw_unlocked_commits(genome):
    """Raw conn.commit() from unlocked committers must be counted too —
    that is the whole point (the W0.3 receipt baselines ~5+2N commits)."""
    before = genome.commit_count
    genome.conn.execute(
        "CREATE TABLE IF NOT EXISTS _commit_probe (x INTEGER)"
    )
    genome.conn.execute("INSERT INTO _commit_probe VALUES (1)")
    genome.conn.commit()
    assert genome.commit_count == before + 1


def test_commit_count_survives_vacuum(tmp_path):
    """vacuum() rebinds self.conn — the trace hook must be re-registered
    or the counter silently freezes for the rest of the process (review
    finding, perf slice 1)."""
    g = Genome(path=str(tmp_path / "g.db"))
    try:
        g.upsert_doc(make_gene("vacuum survivor", domains=["vac"]),
                     apply_gate=False)
        g.vacuum()
        before = g.commit_count
        g.conn.execute("CREATE TABLE IF NOT EXISTS _probe2 (x INTEGER)")
        g.conn.execute("INSERT INTO _probe2 VALUES (1)")
        g.conn.commit()
        assert g.commit_count == before + 1, (
            "commit trace hook died across the vacuum reopen"
        )
    finally:
        g.close()


# ── 4. model-load recorder ───────────────────────────────────────────


def test_record_model_load_accumulates(monkeypatch):
    # Patch on the module of definition: the telemetry package star-copies
    # otel names at import, so package-level bindings are separate copies.
    from cymatix_context.telemetry import otel as otel_mod

    recorder = _RecordingInstrument()
    monkeypatch.setattr(otel_mod, "genome_signal_histogram",
                        lambda: recorder)
    monkeypatch.setattr(otel_mod, "_MODEL_LOAD_MS", {})

    otel_mod.record_model_load("dense", 1.5)
    otel_mod.record_model_load("dense", 0.5)
    otel_mod.record_model_load("splade", 0.25)

    snap = otel_mod.model_load_ms()
    assert snap["dense"] == pytest.approx(2000.0)
    assert snap["splade"] == pytest.approx(250.0)
    # Histogram observed each load under the model_load_* signal label.
    signals = [attrs.get("signal") for _, attrs in recorder.observations]
    assert signals.count("model_load_dense") == 2
    assert signals.count("model_load_splade") == 1
    # Snapshot is a copy — mutating it must not corrupt the source.
    snap["dense"] = -1
    assert telemetry_mod.model_load_ms()["dense"] == pytest.approx(2000.0)


def test_bgem3_weight_load_records_model_load(monkeypatch):
    """The real ~2 GB weight load happens lazily inside BGEM3Codec._load(),
    NOT at codec construction — model_load_ms must capture it there or
    cold-start receipts subtract nothing (review finding, perf slice 1)."""
    import types

    from cymatix_context.backends.bgem3_codec import BGEM3Codec
    from cymatix_context.telemetry import otel as otel_mod

    monkeypatch.setattr(otel_mod, "_MODEL_LOAD_MS", {})
    fake_mod = types.ModuleType("FlagEmbedding")
    fake_mod.BGEM3FlagModel = lambda name, use_fp16: object()
    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake_mod)

    codec = BGEM3Codec(dim=1024)
    codec._load()

    assert "dense" in otel_mod.model_load_ms(), (
        "BGEM3Codec._load must record the weight load under 'dense'"
    )
    # Idempotent: a second _load is the lock-free fast path, no re-record.
    n_before = otel_mod.model_load_ms()["dense"]
    codec._load()
    assert otel_mod.model_load_ms()["dense"] == n_before


def test_splade_load_records_model_load(monkeypatch):
    from cymatix_context.backends import splade_backend
    from cymatix_context.telemetry import otel as otel_mod

    monkeypatch.setattr(otel_mod, "_MODEL_LOAD_MS", {})
    monkeypatch.setattr(splade_backend, "_model", None)
    monkeypatch.setattr(splade_backend, "_tokenizer", None)
    monkeypatch.setattr(splade_backend, "_device", None)

    import transformers
    monkeypatch.setattr(transformers, "AutoTokenizer",
                        types_simple_namespace_factory())
    monkeypatch.setattr(transformers, "AutoModelForMaskedLM",
                        types_simple_namespace_factory())

    splade_backend._ensure_loaded()

    assert "splade" in otel_mod.model_load_ms(), (
        "_ensure_loaded must record the SPLADE load under 'splade'"
    )


def types_simple_namespace_factory():
    """Stand-in for AutoTokenizer/AutoModelForMaskedLM: from_pretrained
    returns an object whose .to(device) returns itself and .eval() no-ops."""
    class _Fake:
        @staticmethod
        def from_pretrained(name):
            class _M:
                def to(self, device):
                    return self

                def eval(self):
                    # torch nn.Module.eval() (inference mode), NOT builtin
                    # eval — mirrors _model.eval() in splade_backend.
                    return self
            return _M()
    return _Fake


# ── 5. _stage_receipts: bench-side ring aggregation ──────────────────


def test_stage_receipts_aggregate():
    import _stage_receipts as sr

    entries = [
        {"request_id": "r1", "stage": "express", "ms": 10.0, "ts": 1.0,
         "signals": {"fts5": 4.0, "body_fetch": 2.0},
         "model_load_ms": {"dense_codec": 100.0}},
        {"request_id": "r1", "stage": "tail_writes", "ms": 5.0, "ts": 1.1,
         "commits": 3},
        {"request_id": "r2", "stage": "express", "ms": 30.0, "ts": 2.0,
         "signals": {"fts5": 12.0},
         "model_load_ms": {"dense_codec": 100.0}},
        {"request_id": "r2", "stage": "tail_writes", "ms": 15.0, "ts": 2.1,
         "commits": 5},
    ]
    agg = sr.aggregate(entries)

    # Stage rollup uses the run_latency_scaling ceiling-index percentile:
    # p50 of [10, 30] is 30 (ceil(0.5*1) == 1 → the max at n=2).
    assert agg["stages"]["express"]["n"] == 2
    assert agg["stages"]["express"]["p50_ms"] == 30.0
    assert agg["stages"]["express"]["p95_ms"] == 30.0
    assert agg["stages"]["tail_writes"]["n"] == 2

    # Signal rollup comes from express entries' sub-maps; a signal absent
    # from one request is aggregated over the requests where it fired.
    assert agg["signals"]["fts5"]["n"] == 2
    assert agg["signals"]["fts5"]["p50_ms"] == 12.0
    assert agg["signals"]["body_fetch"]["n"] == 1

    # model_load_ms is process-cumulative: keep the last-seen snapshot.
    assert agg["model_load_ms"] == {"dense_codec": 100.0}

    # commits ride tail_writes entries.
    assert agg["commits"]["n"] == 2
    assert agg["commits"]["p50"] == 5
    assert agg["commits"]["max"] == 5


def test_stage_receipts_aggregate_empty():
    import _stage_receipts as sr

    agg = sr.aggregate([])
    assert agg["stages"] == {}
    assert agg["signals"] == {}
    assert agg["model_load_ms"] == {}
    assert agg["commits"]["n"] == 0


def test_stage_receipts_fetch_recent_parses_endpoint_shape(monkeypatch):
    """fetch_recent unwraps the {'events': [...], 'ring_max': N} envelope."""
    import _stage_receipts as sr
    import io
    import json
    import urllib.request as _url

    payload = {"events": [{"request_id": "r1", "stage": "express",
                           "ms": 1.0, "ts": 0.0}], "ring_max": 64}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(req, timeout=None):
        assert timeout is not None, "no hanging requests — timeout required"
        return _Resp(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(_url, "urlopen", _fake_urlopen)
    events = sr.fetch_recent("http://127.0.0.1:11437", limit=50)
    assert events == payload["events"]


# ── 6. launcher pipeline panel: nested stages must not double-count ──


def test_pipeline_panel_total_excludes_nested_stages():
    """delivery_lookup / delivery_log run INSIDE the assemble stage — their
    ms is already inside assemble's ms, so summing every event overstates
    the per-request total (review finding, perf slice 1)."""
    from cymatix_context.launcher.collector import StateCollector

    payload = {"events": [
        {"request_id": "r1", "stage": "express", "ms": 50.0, "ts": 1.0},
        {"request_id": "r1", "stage": "assemble", "ms": 45.0, "ts": 1.2},
        {"request_id": "r1", "stage": "delivery_lookup", "ms": 20.0, "ts": 1.1},
        {"request_id": "r1", "stage": "delivery_log", "ms": 10.0, "ts": 1.15},
    ], "ring_max": 128}

    panel = StateCollector._pipeline_panel(object(), payload)
    row = panel["runs"][0]
    assert row["total_ms"] == 95.0, (
        f"total must be express+assemble only; nested delivery stages are "
        f"already inside assemble. got {row['total_ms']}"
    )
    # The breakdown still shows the nested stages individually.
    assert row["stages"]["delivery_lookup"] == 20.0
    assert row["stages"]["delivery_log"] == 10.0


# ── 7. end-to-end: /context rings the new stages ─────────────────────


def test_context_turn_rings_new_stages():
    client = make_client()
    # Seed the store so express finds candidates and the pipeline reaches
    # assemble + tail writes (an empty genome abstains before them). Direct
    # genome pokes with explicit domains — the test-env CPU tagger yields
    # no tags, so /ingest-seeded content would miss every tier.
    g = client.app.state.cymatix.genome
    for i in range(3):
        g.upsert_doc(make_gene(
            f"splice pipeline compresses candidate fragments, variant {i}",
            domains=["splice", "pipeline"],
            gene_id=f"e2e-ring-{i}",
        ))
    # A full deque (maxlen rotation) keeps len() constant, which would
    # make a since-index mark miss every new entry — start from empty.
    cm_mod._pipeline_events.clear()
    mark = 0

    resp = client.post("/context", json={
        "query": "what does the splice step do to candidate fragments?",
        "session_id": "perf-instr-sess",
    })
    assert resp.status_code == 200

    entries = _ring_entries_since(mark)
    stages = {e["stage"] for e in entries}
    for stage in ("express", "tail_writes", "delivery_lookup",
                  "delivery_log", "cwola"):
        assert stage in stages, f"stage {stage!r} missing from ring: {stages}"

    express = [e for e in entries if e["stage"] == "express"][-1]
    assert "signals" in express, "express ring entry must carry the sub-map"
    assert "body_fetch" in express["signals"]

    # The cwola entry must correlate to the same request as express.
    cwola = [e for e in entries if e["stage"] == "cwola"][-1]
    assert cwola["request_id"] == express["request_id"]
