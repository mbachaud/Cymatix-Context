"""Ablation-ladder packet trace (--trace-dir / --trace-level).

The ladder calls ``manager.build_context`` directly, so the HTTP route never
traces it. These tests pin: the shared chunk helper matches the route's old
construction, tracing the ladder does not change retrieval, and one packet per
query lands with a verifying hash chain. No external services.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import types
from pathlib import Path

from cymatix_context.context_manager import CymatixContextManager
from cymatix_context.telemetry import trace_chunks
from tests.conftest import make_cymatix_config, make_gene

ROOT = Path(__file__).resolve().parents[1]


def _load_ladder():
    spec = importlib.util.spec_from_file_location(
        "ablation_ladder_trace", ROOT / "benchmarks" / "dogfood" / "erb" / "ablation_ladder.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ladder = _load_ladder()

QUERIES = [
    "zebracrossing authentication flamingo",
    "quasar telemetry pelican rotation",
    "marmalade checkpoint walrus ledger",
]
TEXTS = [
    "Zebracrossing authentication flamingo tokens are rotated nightly.",
    "Quasar telemetry pelican rotation happens every quarter hour.",
    "Marmalade checkpoint walrus ledger entries are compacted weekly.",
]


DOMAINS = [
    ["zebracrossing", "authentication", "flamingo"],
    ["quasar", "telemetry", "pelican", "rotation"],
    ["marmalade", "checkpoint", "walrus", "ledger"],
]


def _manager():
    # Explicit domain tags: untagged genes never match (the CPU tagger needs
    # spaCy), which made the on/off ranking comparison vacuous.
    mgr = CymatixContextManager(make_cymatix_config())
    for i, (t, d) in enumerate(zip(TEXTS, DOMAINS)):
        mgr.genome.upsert_gene(make_gene(t, domains=d, gene_id=f"lad{i:013d}"))
    return mgr


def _run(mgr, tracer=None):
    out = []
    for q in QUERIES:
        window = mgr.build_context(q, read_only=True, ignore_delivered=True, max_genes=4)
        out.append((
            list(window.expressed_gene_ids or []),
            dict(mgr.genome.last_query_scores or {}),
        ))
        if tracer is not None:
            ladder.emit_ladder_trace(
                tracer, mgr.genome, mgr.config, window, q, session_id="ladder-test")
    return out


def test_window_chunks_helper_matches_legacy_construction():
    docs = {
        "a": types.SimpleNamespace(content="alpha", source_kind="doc"),
        "b": types.SimpleNamespace(content="beta", source_kind="code"),
    }
    window = types.SimpleNamespace(
        expressed_gene_ids=["b", "a"], retrieval_scores={"a": 2.0, "b": 1.0},
        tier_contributions={"a": {"fts": 1}}, delivered_docs=docs)
    got = trace_chunks.window_chunks(window, genome=None)
    assert got == [
        {"gene_id": "b", "text": "beta", "rank": 1, "score": 1.0,
         "source_kind": "code", "lane_contribs": None},
        {"gene_id": "a", "text": "alpha", "rank": 2, "score": 2.0,
         "source_kind": "doc", "lane_contribs": {"fts": 1}},
    ]


def test_ladder_cli_exposes_trace_flags_and_run_arm_param():
    assert "tracer" in inspect.signature(ladder.run_arm).parameters
    assert inspect.signature(ladder.run_arm).parameters["tracer"].default is None


def test_ladder_trace_does_not_change_retrieval_and_writes_one_packet_per_query(tmp_path):
    off = _run(_manager())
    assert all(ids for ids, _ in off), "corpus must retrieve for every query"

    tracer = ladder.build_ladder_tracer(tmp_path / "tr", "metadata")
    on = _run(_manager(), tracer)
    info = ladder.finalize_ladder_trace(tracer, tmp_path / "tr", "metadata")

    assert on == off
    assert info["packets_written"] == len(QUERIES)
    assert info["chain_verified"] is True
    assert info["trace_level"] == "metadata"
    lines = [
        json.loads(l)
        for l in (tmp_path / "tr" / "packets.jsonl").read_text(encoding="utf-8").splitlines()
        if l
    ]
    assert len(lines) == len(QUERIES)
    assert all(r["session_id"] == "ladder-test" for r in lines)
    assert len({r["packet_id"] for r in lines}) == len(QUERIES)
