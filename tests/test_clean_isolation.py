"""Stage 1 — read-only isolation contract + axis-split harness.

Verifies that ``clean=true`` HTTP requests and ``read_only=True`` direct
calls to ``HelixContextManager.build_context`` produce zero genome
mutations. Also locks the per-axis query-template wording so the
``blind`` baseline stays byte-identical to the v2 single-axis output and
the ``located`` axis emits the dim-lock variant-4 4-axis form.

Spec: ``docs/specs/2026-05-08-stage-1-bench-axis-split.md`` §7.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from helix_context import server as server_mod
from helix_context.config import BudgetConfig, ClassifierConfig
from helix_context.context_manager import HelixContextManager
from tests.conftest import MockCompressorBackend, make_client, make_gene, make_helix_config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seeded_manager() -> HelixContextManager:
    """In-memory genome with a handful of seeded genes so build_context
    has candidates to express + can exercise the touch / coactivate /
    relations-batch tail of the pipeline.

    ``abstain_enabled=False``: this fixture's 4-gene corpus sits on a knife
    edge of the RRF-normalized abstain ratio gate (``tier_logic.py``,
    threshold 1.5) -- under ``blend_mode="legacy"`` the mis-scaled additive
    bonuses happened to inflate the ratio just over the line (~1.52), but
    under the graduated ``"scale_relative"`` default (2026-07-13, serving-
    profile receipt) the same corpus computes ~1.36 and abstains, so
    read_only=False touches nothing and this isolation test false-fails.
    This test is about the read_only mutation contract, not retrieval
    confidence, so abstain is disabled to decouple the two concerns rather
    than hand-tuning the corpus to dodge one gate's threshold.
    """
    cfg = make_helix_config(
        budget=BudgetConfig(
            max_genes_per_turn=4, splice_aggressiveness=0.5, abstain_enabled=False,
        ),
        classifier=ClassifierConfig(enabled=True),
        synonym_map={"port": ["upstream", "endpoint", "url"]},
    )
    mgr = HelixContextManager(cfg)
    mgr.ribosome.backend = MockCompressorBackend()

    seed_data = [
        ("upstream_port = 11434  # Ollama default", ["network"], ["port", "upstream"]),
        ("HELIX_PORT = 11437  # bench helix sidecar", ["network"], ["port", "helix"]),
        ("Server bound to port 8080 for development", ["network"], ["port", "server"]),
        ("Random unrelated content about file paths", ["filesystem"], ["path"]),
    ]
    for i, (content, doms, ents) in enumerate(seed_data):
        mgr.genome.upsert_gene(
            make_gene(
                content,
                domains=doms,
                entities=ents,
                gene_id=f"isolation_seed_{i:010d}",
            ),
        )
    return mgr


def _genome_state_snapshot(conn: sqlite3.Connection) -> dict:
    """Serialize all the genome state that read_only=True must NOT mutate.

    - `genes.epigenetics` JSON (touch_genes bumps access_count + last_accessed
      + decay_score + recent_accesses inside this blob).
    - `gene_relations` rowcount (store_relations_batch target).
    - `harmonic_links` rowcount (store_harmonic_weights target).
    - `genes.epigenetics.co_activated_with` (link_coactivated mutates
      this list inside the JSON blob).

    Returns a dict suitable for direct equality comparison.
    """
    cur = conn.cursor()
    snap: dict = {}
    snap["gene_count"] = cur.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
    rows = cur.execute(
        "SELECT gene_id, epigenetics FROM genes ORDER BY gene_id"
    ).fetchall()
    # Serialize epigenetics blobs; comparing the raw JSON catches any
    # mutation to access_count, last_accessed, decay_score, recent_accesses,
    # or co_activated_with without us having to enumerate fields by hand.
    snap["epigenetics"] = {row[0]: row[1] for row in rows}

    snap["gene_relations_count"] = cur.execute(
        "SELECT COUNT(*) FROM gene_relations"
    ).fetchone()[0]
    # harmonic_links table is created lazily by the cymatics migration;
    # tolerate its absence on minimal :memory: configs.
    try:
        snap["harmonic_links_count"] = cur.execute(
            "SELECT COUNT(*) FROM harmonic_links"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        snap["harmonic_links_count"] = 0
    return snap


# ---------------------------------------------------------------------------
# 1. read_only=True freezes genome state
# ---------------------------------------------------------------------------


def test_clean_true_does_not_mutate_genome():
    """Direct manager call with read_only=True must not mutate any genome
    state (touch_genes, link_coactivated, store_harmonic_weights,
    store_relations_batch are all gated). A read_only=False baseline call
    against a fresh manager must mutate at least one observed quantity.
    """
    # --- read_only=True path: state must be byte-identical pre/post
    mgr_ro = _seeded_manager()
    try:
        before_ro = _genome_state_snapshot(mgr_ro.genome.conn)
        win_ro = mgr_ro.build_context(query="What is the upstream port?", read_only=True)
        # Sanity: pipeline actually ran and expressed something.
        assert win_ro is not None
        after_ro = _genome_state_snapshot(mgr_ro.genome.conn)
        assert before_ro == after_ro, (
            "read_only=True must not mutate genome state, but observed "
            f"differences:\n  before: {before_ro}\n  after:  {after_ro}"
        )
    finally:
        mgr_ro.close()

    # --- read_only=False baseline: at least one observed quantity must change
    mgr_rw = _seeded_manager()
    try:
        before_rw = _genome_state_snapshot(mgr_rw.genome.conn)
        win_rw = mgr_rw.build_context(query="What is the upstream port?", read_only=False)
        assert win_rw is not None
        after_rw = _genome_state_snapshot(mgr_rw.genome.conn)
        # touch_genes always bumps access_count for at least one expressed
        # gene → epigenetics blobs differ.
        assert before_rw != after_rw, (
            "read_only=False (default) must mutate genome state; observed "
            "no change. Did touch_genes / link_coactivated regress?"
        )
    finally:
        mgr_rw.close()


# ---------------------------------------------------------------------------
# 2. clean=true HTTP request implies read_only=True
# ---------------------------------------------------------------------------


@pytest.fixture
def http_client():
    client = make_client(backend=MockCompressorBackend())
    app = client.app
    # Seed so build_context has something to express.
    for i, (content, doms, ents) in enumerate([
        ("upstream_port = 11434", ["network"], ["port"]),
        ("HELIX_PORT = 11437", ["network"], ["port"]),
    ]):
        app.state.helix.genome.upsert_gene(
            make_gene(
                content, domains=doms, entities=ents,
                gene_id=f"http_seed_{i:010d}",
            ),
        )
    with client:
        yield client, app


def test_clean_flag_implies_read_only(http_client):
    """POST /context with clean=true (no explicit read_only) must reach
    the manager with read_only=True. Verifies _request_read_only honors
    the spec contract at server.py:762-770."""
    client, app = http_client

    # build_context_async() forwards to build_context() positionally:
    #   build_context(query, downstream_model, include_cold, session_context,
    #                 party_id, prompt_tokens_hint, session_id,
    #                 ignore_delivered, read_only, decoder_override)
    # So `read_only` is positional arg index 8 (zero-indexed, after query).
    captured: dict = {}
    real_build = app.state.helix.build_context

    def spy(*args, **kwargs):
        # The async wrapper passes everything positionally; the synchronous
        # /context/packet route may pass kwargs. Accept both, then surface
        # read_only via either path.
        ro_pos = args[8] if len(args) > 8 else None
        captured["read_only"] = kwargs.get("read_only", ro_pos)
        return real_build(*args, **kwargs)

    with patch.object(app.state.helix, "build_context", side_effect=spy):
        resp = client.post("/context", json={"query": "upstream port", "clean": True})

    assert resp.status_code == 200, resp.text
    assert captured.get("read_only") is True, (
        f"clean=true must propagate read_only=True; observed: {captured}"
    )


# ---------------------------------------------------------------------------
# 3. response_mode="packet" branch within /context honors read_only
# ---------------------------------------------------------------------------


def test_response_mode_packet_with_clean_isolates_writes(http_client):
    """The in-handler packet branch at server.py:852 must share read_only
    plumbing with the dedicated /context/packet route, otherwise
    `clean=true` is a silent escape hatch for genome writes."""
    client, app = http_client
    before = _genome_state_snapshot(app.state.helix.genome.conn)
    resp = client.post(
        "/context",
        json={"query": "upstream port", "clean": True, "response_mode": "packet"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Sanity: we actually hit the packet branch.
    assert body.get("response_mode") == "packet"
    after = _genome_state_snapshot(app.state.helix.genome.conn)
    assert before == after, (
        "response_mode='packet' + clean=true must not mutate the genome; "
        f"diff:\n  before: {before}\n  after:  {after}"
    )


# ---------------------------------------------------------------------------
# 4. Located-axis query format
# ---------------------------------------------------------------------------


def _import_bench_module():
    """Load benchmarks/bench_needle_1000.py without requiring it to be a
    package. The bench dir is not on sys.path by default."""
    repo_root = Path(__file__).resolve().parent.parent
    bench_path = repo_root / "benchmarks" / "bench_needle_1000.py"
    spec = importlib.util.spec_from_file_location("bench_needle_1000", bench_path)
    assert spec is not None and spec.loader is not None
    # bench file has `sys.path.insert(0, ...)` for its own dim-lock import
    # — that side-effect is harmless under tests.
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_located_axis_query_format():
    """4-axis locator — project/module/filename composition, with a
    config.py source under the helix-context project."""
    bench = _import_bench_module()
    needle = {
        "key": "cold_start_threshold",
        "value": "5",
        "category": "helix",
        "source": "F:/Projects/helix-context/helix_context/config.py",
    }
    out = bench.build_query_located(needle)
    assert (
        out
        == "What is the cold start threshold value in helix-context/helix_context/config?"
    ), f"located output mismatch: {out!r}"


# ---------------------------------------------------------------------------
# 5. Blind-axis query format (legacy preserved)
# ---------------------------------------------------------------------------


def test_blind_axis_preserves_legacy_format():
    """Bare-key form — must be byte-identical to the pre-split v2 output
    for the same needle. This is the regression guard for the 13.8%
    headline."""
    bench = _import_bench_module()
    needle = {
        "key": "cold_start_threshold",
        "value": "5",
        "category": "helix",
        "source": "F:/Projects/helix-context/helix_context/config.py",
    }
    out = bench.build_query_blind(needle)
    # v2 default-branch wording (`bench_needle_1000.py:341` pre-Stage-1):
    # "threshold" matches the port/size/count/limit/threshold/budget guard,
    # so the template emits the {category} form.
    assert out == "What is the cold start threshold in the helix source?", (
        f"blind output drifted from v2 baseline: {out!r}"
    )


# ---------------------------------------------------------------------------
# 6. Token-field uniformity — no bare `injected_tokens` keys leak through
# ---------------------------------------------------------------------------


def test_token_field_uniformity():
    """Harness v3 unifies on `injected_tokens_est` / `budget_tokens_est`.
    summarize() must read the canonical key, and the tolerant fallback
    aggregates legacy rows from cross-branch JSONs without dropping data.
    """
    bench = _import_bench_module()
    # Mixed rows: canonical, legacy-only, and a row missing both.
    rows = [
        {"injected_tokens_est": 800, "budget_tokens_est": 15000},
        {"injected_tokens": 1200, "budget_tokens": 10000},  # legacy form
        {"genes_expressed": 3},  # no tokens recorded
    ]
    summary = bench.summarize(rows)
    tokens = summary["tokens"]
    # avg_injected must be > 0 (legacy row contributes via tolerant fallback).
    assert tokens["avg_injected"] > 0, (
        f"summary.tokens.avg_injected expected > 0, got {tokens['avg_injected']}; "
        "tolerant read of legacy `injected_tokens` regressed."
    )
    # Both legacy and canonical rows averaged: (800 + 1200) / 2 = 1000.
    assert tokens["avg_injected"] == pytest.approx(1000.0)
    assert tokens["avg_budget"] == pytest.approx(12500.0)

    # No row in the summary's source dicts should still rely on the bare
    # key once the harness has emitted it — verified by checking the
    # canonical-emit path: the run_needle result dict keys.
    src = (Path(__file__).resolve().parent.parent
           / "benchmarks" / "bench_needle_1000.py").read_text(encoding="utf-8")
    # The ASK_PROXY=1 emit path (the only emit path on master) must use
    # the `_est` suffix exclusively. Bare `injected_tokens` may still
    # appear inside fallback helpers and comments — that's the tolerant
    # read, not an emit. We assert the emit-side usage.
    emit_lines = [
        line for line in src.splitlines()
        if '"injected_tokens"' in line and "tokens_est" not in line
    ]
    # Any remaining emits (if any) must be inside the tolerant-read helper
    # (function names `_injected` / `_budget`) rather than top-level dict
    # construction. We allow them in the helper context only.
    assert not [
        line for line in emit_lines
        if "result.update" in line or 'result["' in line or "result['" in line
    ], f"bare `injected_tokens` still emitted at result-update time: {emit_lines}"
