"""Delivery metrics: what each packet actually delivered (chunks, characters)
and how long the packet took (retrieval + assembly), last and average.

The server rings the delivered counts on the ``assemble`` stage; the launcher
turns the ring into the Delivery panel (it replaces the Tokens panel, which
keeps its lifetime figure as one small line)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cymatix_context.launcher.collector import StateCollector
from tests.conftest import make_client, make_gene


# ── server: the assemble ring entry carries the delivered counts ────────


def test_assemble_ring_entry_carries_delivered_counts():
    with make_client() as c:
        gene = make_gene(content="the answer to the universe is forty two",
                         domains=["astronomy"])
        c.app.state.cymatix.genome.upsert_gene(gene, apply_gate=False)
        r = c.post("/context", json={"query": "answer universe forty two",
                                     "decoder_mode": "none"})
        assert r.status_code == 200
        events = c.get("/debug/pipeline/recent", params={"limit": 200}).json()["events"]
    assemble = [e for e in events if e["stage"] == "assemble"]
    assert assemble, "the assemble stage must ring"
    last = assemble[-1]
    assert isinstance(last["delivered_chunks"], int) and last["delivered_chunks"] >= 1
    assert isinstance(last["delivered_chars"], int) and last["delivered_chars"] > 0


# ── launcher: the panel ─────────────────────────────────────────────────


def _req(rid, ts, *, express, assemble, chunks=None, chars=None, tail=5.0):
    asm = {"request_id": rid, "stage": "assemble", "ms": assemble, "ts": ts + 0.2}
    if chunks is not None:
        asm["delivered_chunks"], asm["delivered_chars"] = chunks, chars
    return [
        {"request_id": rid, "stage": "express", "ms": express, "ts": ts + 0.1},
        asm,
        {"request_id": rid, "stage": "tail_writes", "ms": tail, "ts": ts + 0.3},
    ]


def _panel(events):
    return StateCollector._delivery_panel(object(), {"events": events, "ring_max": 128})


def test_last_and_average_over_the_ring():
    events = (_req("a", 1.0, express=100, assemble=50, chunks=10, chars=10_000)
              + _req("b", 2.0, express=200, assemble=100, chunks=12, chars=20_000))
    p = _panel(events)
    assert p["samples"] == 2
    assert p["chunks"] == {"last": 12, "avg": 11.0}
    assert p["chars"] == {"last": 20_000, "avg": 15_000.0}
    # latency = the packet stages only: tail_writes is not part of the packet
    assert p["latency_ms"]["last"] == 300.0
    assert p["latency_ms"]["avg"] == 225.0


def test_p95_uses_nearest_rank():
    events = []
    for i in range(20):
        events += _req(f"r{i}", float(i), express=float(i + 1), assemble=0.0, chunks=1, chars=1)
    p = _panel(events)
    assert p["latency_ms"]["p95"] == 19.0              # 19th of 20 sorted values


def test_newest_request_is_last_regardless_of_event_order():
    events = (_req("new", 5.0, express=1, assemble=1, chunks=3, chars=30)
              + _req("old", 1.0, express=1, assemble=1, chunks=9, chars=90))
    assert _panel(events)["chunks"]["last"] == 3


def test_an_older_engine_without_the_counts_still_reports_latency():
    p = _panel(_req("a", 1.0, express=10, assemble=5))
    assert p["chunks"] is None and p["chars"] is None
    assert p["latency_ms"]["last"] == 15.0


def test_no_events_means_no_panel():
    assert _panel([]) is None


def test_pipeline_rows_carry_the_delivered_counts():
    payload = {"events": _req("a", 1.0, express=10, assemble=5, chunks=7, chars=700),
               "ring_max": 128}
    row = StateCollector._pipeline_panel(object(), payload)["runs"][0]
    assert row["delivered_chunks"] == 7 and row["delivered_chars"] == 700


def test_collect_publishes_delivery_alongside_tokens():
    # the Tokens payload stays (lifetime line); Delivery is new
    c = StateCollector(supervisor=MagicMock())
    assert hasattr(c, "_delivery_panel")


# ── template ────────────────────────────────────────────────────────────


def test_delivery_panel_renders_and_replaces_tokens():
    pytest.importorskip("jinja2")
    from fastapi.testclient import TestClient
    from cymatix_context.launcher.app import create_app
    from tests.test_launcher_dashboard_wiring import FakeSupervisor

    events = _req("a", 1.0, express=100, assemble=50, chunks=12, chars=18_240)
    state = {
        "cymatix": {"running": True, "pid": 1, "port": 11437},
        "tokens": {"session": {"total": 5, "exact": 5, "estimated": 0},
                   "lifetime": {"total": 1_200_000, "exact": 1_200_000, "estimated": 0}},
        "delivery": StateCollector._delivery_panel(object(), {"events": events, "ring_max": 128}),
    }
    collector = MagicMock()
    collector.collect.return_value = state
    app = create_app(store=SimpleNamespace(), supervisor=FakeSupervisor(), collector=collector)
    with TestClient(app) as client:
        html = client.get("/api/state/panels").text
    assert "Chunks per packet" in html and "Packet latency" in html
    assert "18,240" in html
    assert "1,200,000" in html                          # lifetime tokens kept
    assert "panel--tokens" not in html
