"""Issue #493 -- the /context trace must not re-read every delivered doc.

``_window_chunks`` used to call ``genome.get_doc`` once per delivered gene
(SELECT * + row hydration) just to get ``content`` / ``source_kind`` that
assembly already held. These tests pin: zero per-gene ``get_doc`` calls on the
traced path, and chunk fields byte-identical to the old computation.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import make_gene
from tests.test_packet_trace import DOC_TEXT, QUERY, _client, _wait_lines


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("CYMATIX_TRACE_ENABLED", "CYMATIX_TRACE_LEVEL", "CYMATIX_TRACE_PATH"):
        monkeypatch.delenv(k, raising=False)


def _deliver(c, gids, *, with_map):
    """Force delivery of ``gids`` and count get_doc calls made after the
    pipeline returned (i.e. by the trace path only)."""
    mgr = c.app.state.cymatix
    genome = mgr.genome
    genes = {}
    for i, gid in enumerate(gids):
        g = make_gene(f"{DOC_TEXT} variant number {i} " * 3, gene_id=gid)
        g.source_kind = "code" if i % 2 else "doc"
        genome.upsert_gene(g)
        genes[gid] = genome.get_doc(gid)  # as hydrated from the store
    orig = mgr.build_context_async
    calls = []

    async def patched(*a, **k):
        w = await orig(*a, **k)
        w.expressed_gene_ids = list(gids)
        w.retrieval_scores = {g: 1.0 + i for i, g in enumerate(gids)}
        if with_map:
            w.delivered_docs = dict(genes)
        real = genome.get_doc

        def counting(gid):
            calls.append(gid)
            return real(gid)

        genome.get_doc = counting
        return w

    mgr.build_context_async = patched
    return genes, calls


def _expected_chunks(genes, gids, hasher):
    out = []
    for rank, gid in enumerate(gids, 1):
        d = genes[gid]
        out.append({"chunk_id": gid, "content_hash": hasher.hash(d.content),
                    "rank": rank, "source_kind": d.source_kind})
    return out


@pytest.mark.parametrize("with_map", [True, False])
def test_traced_context_makes_no_per_gene_get_doc(tmp_path, with_map):
    gids = [f"nrf{n:013d}" for n in range(5)]
    # Baseline: get_doc calls the response path itself makes (top-gene
    # lookups for citations / know), with tracing OFF.
    base = _client(tmp_path / "off")
    _, base_calls = _deliver(base, gids, with_map=with_map)
    base.post("/context", json={"query": QUERY})

    c = _client(tmp_path, enabled=True, level="metadata")
    genes, calls = _deliver(c, gids, with_map=with_map)
    c.post("/context", json={"query": QUERY})
    rec = json.loads(_wait_lines(tmp_path / "traces" / "packets.jsonl", 1)[0])
    assert calls == base_calls, (
        f"trace re-fetched docs via get_doc: {calls} vs untraced {base_calls}")

    hasher = c.app.state.packet_tracer.hasher
    want = _expected_chunks(genes, gids, hasher)
    got = [{k: ch.get(k) for k in ("chunk_id", "content_hash", "rank", "source_kind")}
           for ch in rec["chunks"]]
    assert got == want
