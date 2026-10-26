"""Issue #493 latency profiling: packet trace records say where time went.

timing_ms.stages (per-request pipeline stage ms), timing_ms.signals (the
store's per-signal ms), and a top-level ``pool`` object (candidates scored per
tier + pool_total). All reused from existing timers; nothing new on the
request path.

The corpora here carry explicit ``domains`` tags: untagged genes never match
(the CPU tagger needs spaCy), which would leave the retrieval path unexercised.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from cymatix_context.config import TraceConfig
from cymatix_context.context_manager import CymatixContextManager
from cymatix_context.telemetry import trace as tr
from cymatix_context.telemetry import trace_export as te
from tests.conftest import make_client, make_cymatix_config, make_gene
from tests.test_ladder_trace import ladder
from tests.test_packet_trace import _wait_lines, _clean_env  # noqa: F401

SCHEMA = Path(__file__).resolve().parent.parent / "docs/specs/packet-trace-v0.schema.json"
QUERIES = [
    "zebracrossing authentication flamingo",
    "quasar telemetry pelican rotation",
    "marmalade checkpoint walrus ledger",
]
TAGS = [
    ["zebracrossing", "authentication", "flamingo"],
    ["quasar", "telemetry", "pelican", "rotation"],
    ["marmalade", "checkpoint", "walrus", "ledger"],
]


def _seed(genome):
    for i, (q, tags) in enumerate(zip(QUERIES, TAGS)):
        genome.upsert_gene(make_gene(q + " notes are rotated nightly.", domains=tags,
                                     gene_id=f"tim{i:013d}"))


def _manager():
    mgr = CymatixContextManager(make_cymatix_config())
    _seed(mgr.genome)
    return mgr


def _run(mgr, tracer=None):
    out = []
    for q in QUERIES:
        w = mgr.build_context(q, read_only=True, ignore_delivered=True, max_genes=4)
        out.append((list(w.expressed_gene_ids or []), dict(mgr.genome.last_query_scores or {})))
        if tracer is not None:
            ladder.emit_ladder_trace(tracer, mgr.genome, mgr.config, w, q, session_id="t")
    return out


def _check_timing(rec):
    t = rec["timing_ms"]
    assert "total" in t
    stages = t["stages"]
    assert {"classify", "extract", "express"} <= set(stages), stages
    assert all(isinstance(v, (int, float)) and v >= 0 for v in stages.values())
    sig = t["signals"]
    assert sig and all(v >= 0 for v in sig.values())
    assert {"tag_exact", "tag_prefix", "fts5"} & set(sig), sig
    pool = rec["pool"]
    assert isinstance(pool["pool_total"], int) and pool["pool_total"] >= 1
    assert all(isinstance(v, int) and v >= 0 for v in pool.values())
    assert any(k != "pool_total" for k in pool), pool  # per-tier counts present
    for v in list(stages.values()) + list(sig.values()):
        assert round(v, 2) == v  # rounded to 0.01 ms


def test_http_context_record_has_stages_signals_pool(tmp_path):
    cfg = make_cymatix_config(trace=TraceConfig(
        path=str(tmp_path / "traces"), enabled=True, level="metadata"))
    c = make_client(cfg)
    _seed(c.app.state.cymatix.genome)
    c.post("/context", json={"query": QUERIES[0]})
    rec = json.loads(_wait_lines(tmp_path / "traces" / "packets.jsonl", 1)[0])
    _check_timing(rec)


def test_ladder_record_has_stages_signals_pool(tmp_path):
    tracer = ladder.build_ladder_tracer(tmp_path / "tr", "metadata")
    _run(_manager(), tracer)
    ladder.finalize_ladder_trace(tracer, tmp_path / "tr", "metadata")
    lines = (tmp_path / "tr" / "packets.jsonl").read_text(encoding="utf-8").splitlines()
    recs = [json.loads(l) for l in lines if l]
    assert len(recs) == len(QUERIES)
    for r in recs:
        _check_timing(r)


def test_schema_validates_new_fields():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text("utf-8"))
    v = jsonschema.Draft202012Validator(schema)
    with tempfile.TemporaryDirectory() as d:
        hasher = tr.KeyedHasher(Path(d))
        ev = tr.build_packet_event(
            packet_id="01M4ZZZZZZZZZZZZZZZZZZZZZZ", pipeline_request_id="ab",
            session_id=None, query="q w", chunks=[], verdict={"kind": None},
            timing_ms={"total": 1.0}, stage_ms={"express": 3.14159},
            signal_ms={"fts5": 1.239}, pool={"fts5": 4, "pool_total": 9},
            replay={}, level="metadata", hasher=hasher, ts=1.0)
        v.validate(ev)
        assert ev["timing_ms"]["stages"] == {"express": 3.14}
        assert ev["timing_ms"]["signals"] == {"fts5": 1.24}
        assert ev["pool"] == {"fts5": 4, "pool_total": 9}
        bad = json.loads(json.dumps(ev))
        bad["pool"]["fts5"] = "x"
        with pytest.raises(jsonschema.ValidationError):
            v.validate(bad)
        plain = tr.build_packet_event(
            packet_id="01M4ZZZZZZZZZZZZZZZZZZZZZZ", pipeline_request_id=None,
            session_id=None, query="q", chunks=[], verdict={"kind": None},
            timing_ms={"total": 1.0}, replay={}, level="metadata", hasher=hasher)
        assert "pool" not in plain and plain["timing_ms"] == {"total": 1.0}
        v.validate(plain)


def test_export_keeps_timing_and_pool():
    for k in ("timing_ms", "timing_ms.stages", "timing_ms.signals", "pool"):
        assert te.FIELD_DECISIONS[k] == "keep"
    out = te._transform(
        {"timing_ms": {"total": 1.0, "stages": {"express": 2.0}},
         "pool": {"pool_total": 3}, "chunks": []}, b"k")
    assert out["timing_ms"]["stages"] == {"express": 2.0}
    assert out["pool"] == {"pool_total": 3}


def test_window_carries_request_scoped_timings_not_serialized():
    mgr = _manager()
    w = mgr.build_context(QUERIES[0], read_only=True, ignore_delivered=True, max_genes=4)
    assert w.stage_ms and all(v >= 0 for v in w.stage_ms.values())
    assert w.signal_ms and w.pool_total >= 1
    dumped = w.model_dump()
    for k in ("stage_ms", "signal_ms", "pool_total"):
        assert k not in dumped


def test_ranking_identical_across_runs_and_with_tracing(tmp_path):
    off = _run(_manager())
    assert any(ids for ids, _ in off), "corpus must actually retrieve"
    tracer = ladder.build_ladder_tracer(tmp_path / "tr", "metadata")
    on = _run(_manager(), tracer)
    ladder.finalize_ladder_trace(tracer, tmp_path / "tr", "metadata")
    assert on == off


def test_stage_timings_are_per_request_dicts():
    mgr = _manager()
    a = mgr.build_context(QUERIES[0], read_only=True, ignore_delivered=True, max_genes=4)
    b = mgr.build_context(QUERIES[1], read_only=True, ignore_delivered=True, max_genes=4)
    assert a.stage_ms is not b.stage_ms
