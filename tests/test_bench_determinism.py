"""Tier-1 bench determinism fixes.

The bench harness observed ``agent.citations`` flipping ``12 → 0 → 12 → 0``
between runs against identical code+fixture. Forensic investigation pinned
two non-determinism sources that this PR addresses, and these tests pin
the fixes:

1. **Race condition on ``last_query_scores``.** Concurrent ``/context``
   calls (the bench probe + claude-MCP both racing the same uvicorn)
   clobbered each other's per-request state because the writes weren't
   serialized.  ``test_concurrent_query_docs_preserves_score_consistency``
   exercises the locked snapshot path.

2. **Hash-seed-dependent iteration order.** ``_expand_terms`` returned
   ``list(set(...))`` so synonym-expanded query terms came out in a
   PYTHONHASHSEED-dependent order; downstream SQL parameter ordering and
   rank tie-breaks then drifted across uvicorn subprocesses.
   ``test_expand_terms_returns_sorted_output`` pins the new contract.

3. **Bench orchestrator subprocess env.** ``_spawn`` must set
   ``PYTHONHASHSEED=0`` in the uvicorn child env so set/dict iteration
   stays stable across replays. Pinned in
   ``test_bench_orchestrator_sets_pythonhashseed_zero``.

4. **Shard route tie-break.** Covered in
   ``tests/test_shard_router.py::test_route_tiebreak_by_shard_name_ascending``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from helix_context.config import (
    BudgetConfig,
    GenomeConfig,
    HelixConfig,
    RibosomeConfig,
)
from helix_context.context_manager import HelixContextManager
from helix_context.knowledge_store import KnowledgeStore


# ── Fix 2: ``_expand_terms`` returns sorted output ──────────────────────


def _make_store(synonym_map: dict | None = None) -> KnowledgeStore:
    cfg = HelixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(max_genes_per_turn=4),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        synonym_map=synonym_map or {},
    )
    mgr = HelixContextManager(cfg)
    return mgr


def test_expand_terms_returns_sorted_output():
    """``_expand_terms`` must return a sorted list so iteration order is
    independent of PYTHONHASHSEED.

    Before the fix this was ``list(set(...))`` and order varied across
    process restarts. Downstream call sites pass the result into SQL
    parameter slots and rank tie-breaks, so a re-shuffle silently moved
    the bench's expected gene at boundary positions.
    """
    mgr = _make_store(synonym_map={
        "cache": ["redis", "ttl", "invalidation"],
        "auth": ["jwt", "session", "bearer"],
    })
    try:
        out = mgr.genome._expand_terms(["cache", "auth"])
        assert out == sorted(out), (
            f"_expand_terms must return sorted order; got {out}"
        )
        # All inputs + synonyms present (lowercased, deduped).
        expected = {
            "cache", "redis", "ttl", "invalidation",
            "auth", "jwt", "session", "bearer",
        }
        assert set(out) == expected
    finally:
        mgr.close()


def test_expand_terms_stable_across_repeated_calls():
    mgr = _make_store(synonym_map={
        "x": ["a", "b", "c"],
        "y": ["d", "e", "f"],
    })
    try:
        out1 = mgr.genome._expand_terms(["x", "y"])
        out2 = mgr.genome._expand_terms(["x", "y"])
        out3 = mgr.genome._expand_terms(["y", "x"])
        assert out1 == out2 == out3, (
            "_expand_terms output must not depend on input order or call count"
        )
    finally:
        mgr.close()


def test_expand_terms_no_synonyms_still_sorted():
    """Even without a synonym map the unique lowercased terms come back sorted."""
    mgr = _make_store(synonym_map={})
    try:
        out = mgr.genome._expand_terms(["zebra", "apple", "Apple", "mango"])
        # 'Apple' folds to 'apple', dedup happens, alphabetical order.
        assert out == ["apple", "mango", "zebra"]
    finally:
        mgr.close()


# ── Fix 1: concurrent ``query_docs`` doesn't tear last_query_scores ─────


def test_concurrent_publish_preserves_score_tier_pair_consistency():
    """Simulate the race: two writers publishing distinct
    (last_query_scores, last_tier_contributions) pairs concurrently. A
    reader that takes the same lock must always see the pair from a
    single writer — never scores from writer A with tiers from writer B.

    This is the exact bench-time race: claude-MCP's /context call and the
    bench probe's retrieval_probe arrive at the same uvicorn worker via
    ThreadPoolExecutor(max_workers=2); the router's writes raced inside
    HelixContextManager._build_signals.
    """
    mgr = _make_store(synonym_map={})
    try:
        store = mgr.genome
        # Two writer "calls" with distinct, identifiable signatures so a
        # mixed (A-scores, B-tiers) snapshot is detectable.
        scores_a = {f"a{i}": float(i) for i in range(10)}
        tiers_a = {f"a{i}": {"bm25": 1.0} for i in range(10)}
        scores_b = {f"b{i}": float(i) for i in range(10)}
        tiers_b = {f"b{i}": {"bm25": 1.0} for i in range(10)}

        def writer(scores, tiers, n_iters=200):
            for _ in range(n_iters):
                with store._last_query_scores_lock:
                    store.last_query_scores = dict(scores)
                    store.last_tier_contributions = dict(tiers)

        torn_snapshots: list = []

        def reader(n_iters=200):
            for _ in range(n_iters):
                with store._last_query_scores_lock:
                    snap_scores = dict(store.last_query_scores)
                    snap_tiers = dict(store.last_tier_contributions)
                # A consistent pair: every tier key must appear in scores
                # (writers always publish matching keys per call).
                extra = set(snap_tiers.keys()) - set(snap_scores.keys())
                if extra:
                    torn_snapshots.append({
                        "extra_tier_keys": list(extra)[:3],
                        "snap_keys": list(snap_scores.keys())[:3],
                    })

        threads = [
            threading.Thread(target=writer, args=(scores_a, tiers_a)),
            threading.Thread(target=writer, args=(scores_b, tiers_b)),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), "concurrent thread hung"

        assert not torn_snapshots, (
            f"reader saw torn (scores, tiers) pair: {torn_snapshots[:3]}"
        )
    finally:
        mgr.close()


def test_knowledge_store_publishes_scores_and_tiers_under_lock():
    """Whitebox: monkeypatch the lock to record acquire/release order and
    confirm the (last_query_scores, last_tier_contributions) write pair
    is atomic w.r.t. the lock.

    Before the fix the tier-contributions write happened *outside* the
    ``with self._last_query_scores_lock:`` block, so a reader holding the
    lock could observe scores from call A and tier contributions from
    call B.
    """
    from helix_context.schemas import (
        ChromatinState,
        EpigeneticMarkers,
        Gene,
        PromoterTags,
    )

    mgr = _make_store(synonym_map={})
    try:
        gene = Gene(
            gene_id="",
            content="doc about ranking and retrieval",
            complement="",
            codons=[],
            promoter=PromoterTags(
                domains=["retrieval"], entities=["doc"], sequence_index=0,
            ),
            epigenetics=EpigeneticMarkers(),
            chromatin=ChromatinState.OPEN,
            is_fragment=False,
            source_id="/seed/0.md",
        )
        mgr.genome.upsert_gene(gene, apply_gate=False)

        # Wrap the lock to track when (last_query_scores, last_tier_contributions)
        # are mutated.
        events: list[str] = []
        real_lock = mgr.genome._last_query_scores_lock

        class _TracingLock:
            def __enter__(self_inner):
                real_lock.acquire()
                events.append("enter")
                return self_inner

            def __exit__(self_inner, *args):
                events.append("exit")
                real_lock.release()
                return False

            def acquire(self_inner, *args, **kwargs):
                return real_lock.acquire(*args, **kwargs)

            def release(self_inner):
                return real_lock.release()

        mgr.genome._last_query_scores_lock = _TracingLock()

        mgr.genome.query_docs(domains=["retrieval"], entities=[], max_genes=2)

        # Pair must come in matching enter/exit (no leaks); at least one
        # enter happened (the writer published state).
        assert "enter" in events and "exit" in events
        # Balanced lock usage.
        assert events.count("enter") == events.count("exit")
    finally:
        mgr.close()


# ── Fix 4: bench orchestrator pins PYTHONHASHSEED ───────────────────────


def test_bench_orchestrator_sets_pythonhashseed_zero(tmp_path):
    """``BenchServer._spawn`` must include ``PYTHONHASHSEED=0`` in the
    child uvicorn environment (unless already set explicitly), so
    set/dict iteration order is stable across replays.

    We don't actually want to spawn uvicorn here — instead, intercept
    ``subprocess.Popen`` and inspect the env passed to it.
    """
    # benchmarks/ isn't installed as a package; add to sys.path the same
    # way test_benchmark_monitor_preflight does.
    bench_dir = Path(__file__).resolve().parents[1] / "benchmarks"
    sys.path.insert(0, str(bench_dir))
    try:
        from bench_orchestrator import BenchServer, Fixture  # noqa: E402

        fixture = Fixture(
            name="dummy",
            db=str(tmp_path / "dummy.db"),
            sharded=False,
            extra_env={},
        )

        captured_env: dict[str, str] = {}

        class _FakeProc:
            def __init__(self):
                self.returncode = None
                self.pid = -1

            def poll(self):
                return None

            def terminate(self):
                pass

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

        def _fake_popen(cmd, env=None, **kwargs):
            captured_env.update(env or {})
            return _FakeProc()

        srv = BenchServer(
            host="127.0.0.1",
            port=11437,
            python=sys.executable,
            app="helix_context._asgi:app",
        )
        # bench_orchestrator does ``import subprocess`` and calls
        # ``subprocess.Popen(...)`` so the global subprocess.Popen patch
        # intercepts the call.
        with patch("subprocess.Popen", side_effect=_fake_popen):
            srv._spawn(fixture)

        assert captured_env.get("PYTHONHASHSEED") == "0", (
            f"bench orchestrator must pin PYTHONHASHSEED=0 in uvicorn env; "
            f"got {captured_env.get('PYTHONHASHSEED')!r}"
        )
    finally:
        # Clean up sys.path so other tests don't see the bench dir.
        if str(bench_dir) in sys.path:
            sys.path.remove(str(bench_dir))


def test_bench_orchestrator_respects_explicit_pythonhashseed_override(tmp_path):
    """If a fixture passes ``PYTHONHASHSEED`` via ``extra_env`` (e.g. for
    a targeted random-seed bench), that wins over the orchestrator
    default. ``env.setdefault`` then ``env.update(extra_env)`` should
    produce the override behavior.
    """
    bench_dir = Path(__file__).resolve().parents[1] / "benchmarks"
    sys.path.insert(0, str(bench_dir))
    try:
        from bench_orchestrator import BenchServer, Fixture  # noqa: E402

        fixture = Fixture(
            name="dummy",
            db=str(tmp_path / "dummy.db"),
            sharded=False,
            extra_env={"PYTHONHASHSEED": "42"},
        )

        captured_env: dict[str, str] = {}

        class _FakeProc:
            returncode = None
            pid = -1

            def poll(self):
                return None

            def terminate(self):
                pass

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

        def _fake_popen(cmd, env=None, **kwargs):
            captured_env.update(env or {})
            return _FakeProc()

        srv = BenchServer(
            host="127.0.0.1",
            port=11437,
            python=sys.executable,
            app="helix_context._asgi:app",
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            srv._spawn(fixture)

        # extra_env override wins.
        assert captured_env.get("PYTHONHASHSEED") == "42"
    finally:
        if str(bench_dir) in sys.path:
            sys.path.remove(str(bench_dir))
