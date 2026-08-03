"""W2.3 Phase A — writer-lock sweep + W2.5 background-loop offload.

Every code path that WRITES on the shared ``KnowledgeStore.conn``
(check_same_thread=False) must hold the store's ``_write_lock`` for its
execute+commit section. Unlocked, two concurrent writers either steal each
other's implicit transaction ("cannot commit - no transaction is active")
or hard-deadlock inside the statement on py3.14 sqlite3 — both reproduced
live in the slice-2 probe (log_health was the first flipped member; see
tests/test_perf_instrumentation.py::test_concurrent_log_health_no_commit_steal).

Three test families:

  1. Concurrent hammer tests (template: the log_health hammer) for the
     swept sites — daemon threads, join(timeout), liveness assert BEFORE
     the error assert, guarded close.
  2. A lock-coverage meta-test: a spy wrapped around the store's RLock
     asserts each swept method acquires it at least once — keeps the
     sweep from regressing silently.
  3. W2.5 (background-loop half): the three server background loops
     (_background_checkpoint / _background_wal_gauge /
     _background_registry_sweep) must run their blocking bodies via
     asyncio.to_thread, never directly on the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import types

import pytest

try:
    import pytest_asyncio  # noqa: F401
    _PYTEST_ASYNCIO_AVAILABLE = True
except ImportError:
    _PYTEST_ASYNCIO_AVAILABLE = False

from cymatix_context.genome import Genome
from cymatix_context.identity import session_delivery
from cymatix_context.identity.registry import Registry

from tests.conftest import make_gene


# ── Helpers ──────────────────────────────────────────────────────────


def _seed(g: Genome, n: int, prefix: str) -> list[str]:
    ids = []
    for i in range(n):
        gid = f"{prefix}-{i:03d}"
        g.upsert_doc(make_gene(
            f"{prefix} body text {i}", domains=["sweep"],
            entities=[f"ent{i % 4}", "shared"], gene_id=gid,
        ), apply_gate=False)
        ids.append(gid)
    return ids


def _hammer_pair(fn_a, fn_b, timeout: float = 60.0):
    """Run two loop bodies on daemon threads; return (threads, errors).

    daemon=True: if a wedge reproduces, the threads must fail the liveness
    assert — not block interpreter shutdown and hang the whole run.
    """
    errors: list = []

    def _wrap(fn):
        def run():
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        return run

    ts = [threading.Thread(target=_wrap(fn_a), daemon=True),
          threading.Thread(target=_wrap(fn_b), daemon=True)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=timeout)
    return ts, errors


def _log_health_loop(g: Genome, n: int = 200):
    def run():
        for i in range(n):
            g.log_health(query=f"q{i}", ellipticity=0.5, coverage=0.5,
                         density=0.5, freshness=0.5, genes_expressed=1,
                         genes_available=1, status="healthy")
    return run


# ── 1. Concurrent hammer tests ───────────────────────────────────────


@pytest.mark.concurrency
def test_concurrent_touch_genes_vs_log_health(tmp_path):
    """touch_genes is a SELECT→UPDATE*→commit transaction on the shared
    writer — unlocked it wedges against the (locked) log_health committer."""
    g = Genome(path=str(tmp_path / "tg.db"))
    ids = _seed(g, 12, "tg")

    def toucher():
        for i in range(200):
            g.touch_genes(ids[i % 6:(i % 6) + 4])

    ts, errors = _hammer_pair(toucher, _log_health_loop(g))
    try:
        assert not any(t.is_alive() for t in ts), (
            "touch_genes vs log_health wedged (shared-writer deadlock)"
        )
        assert not errors, f"commit steal reproduced: {errors[:2]!r}"
    finally:
        if not any(t.is_alive() for t in ts):
            g.close()  # close() on a wedged writer conn would hang too


@pytest.mark.concurrency
def test_concurrent_link_coactivated_vs_upsert_doc(tmp_path):
    g = Genome(path=str(tmp_path / "lc.db"))
    ids = _seed(g, 10, "lc")

    def linker():
        for i in range(150):
            g.link_coactivated(ids[i % 4:(i % 4) + 3])

    def upserter():
        for i in range(150):
            g.upsert_doc(make_gene(
                f"fresh upsert body {i}", domains=["sweep"],
                gene_id=f"lc-new-{i:03d}",
            ), apply_gate=False)

    ts, errors = _hammer_pair(linker, upserter)
    try:
        assert not any(t.is_alive() for t in ts), (
            "link_coactivated vs upsert_doc wedged (shared-writer deadlock)"
        )
        assert not errors, f"commit steal reproduced: {errors[:2]!r}"
    finally:
        if not any(t.is_alive() for t in ts):
            g.close()


@pytest.mark.concurrency
def test_concurrent_log_delivery_vs_log_health(tmp_path):
    """session_delivery.log_delivery is a module fn (INSERT+commit) on the
    store conn; the lock lives at the call site (context_manager._assemble
    delivery_log block). This hammer mirrors that call-site pattern."""
    g = Genome(path=str(tmp_path / "sd.db"))
    _seed(g, 3, "sd")

    def deliverer():
        _wl = getattr(g, "_write_lock", None)
        for i in range(200):
            with (_wl if _wl is not None else contextlib.nullcontext()):
                session_delivery.log_delivery(
                    g.conn,
                    session_id="hammer-sess",
                    gene_id=f"sd-{i % 3:03d}",
                    content_hash=f"h{i}",
                    mode="full",
                )

    ts, errors = _hammer_pair(deliverer, _log_health_loop(g))
    try:
        assert not any(t.is_alive() for t in ts), (
            "log_delivery vs log_health wedged (shared-writer deadlock)"
        )
        assert not errors, f"commit steal reproduced: {errors[:2]!r}"
    finally:
        if not any(t.is_alive() for t in ts):
            g.close()


@pytest.mark.concurrency
def test_concurrent_checkpoint_vs_upsert_doc(tmp_path):
    """PRAGMA wal_checkpoint writes on the shared writer connection —
    unlocked it interleaves with an in-flight upsert transaction."""
    g = Genome(path=str(tmp_path / "cp.db"))
    _seed(g, 3, "cp")

    def checkpointer():
        for i in range(100):
            g.checkpoint("PASSIVE" if i % 2 else "TRUNCATE")

    def upserter():
        for i in range(100):
            g.upsert_doc(make_gene(
                f"checkpointed body {i}", domains=["sweep"],
                gene_id=f"cp-new-{i:03d}",
            ), apply_gate=False)

    ts, errors = _hammer_pair(checkpointer, upserter)
    try:
        assert not any(t.is_alive() for t in ts), (
            "checkpoint vs upsert_doc wedged (shared-writer deadlock)"
        )
        assert not errors, f"commit steal reproduced: {errors[:2]!r}"
    finally:
        if not any(t.is_alive() for t in ts):
            g.close()


@pytest.mark.concurrency
def test_concurrent_registry_heartbeat_vs_log_health(tmp_path):
    """Registry commits on the store conn (~12 sites); heartbeat is the
    hottest. Post-sweep it acquires the store's _write_lock internally."""
    g = Genome(path=str(tmp_path / "rg.db"))
    registry = Registry(g)
    p = registry.register_participant(party_id="hammer@local", handle="hammer")

    def heartbeater():
        for _ in range(200):
            registry.heartbeat(p.participant_id)

    ts, errors = _hammer_pair(heartbeater, _log_health_loop(g))
    try:
        assert not any(t.is_alive() for t in ts), (
            "registry.heartbeat vs log_health wedged (shared-writer deadlock)"
        )
        assert not errors, f"commit steal reproduced: {errors[:2]!r}"
    finally:
        if not any(t.is_alive() for t in ts):
            g.close()


# ── 2. Lock-coverage meta-test ───────────────────────────────────────


class _SpyLock:
    """Wraps the store's RLock; counts acquisitions. Context-manager and
    acquire/release protocols both delegate so nesting still works."""

    def __init__(self, real):
        self._real = real
        self.acquisitions = 0

    def __enter__(self):
        self.acquisitions += 1
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def acquire(self, *a, **kw):
        self.acquisitions += 1
        return self._real.acquire(*a, **kw)

    def release(self):
        return self._real.release()


def test_write_lock_coverage_meta(tmp_path):
    """Every swept writer method must acquire _write_lock at least once.

    Monkeypatch-spy on the store's RLock: wraps (not replaces) the real
    lock, so all serialization semantics hold while acquisitions are
    counted. New writer methods that skip the lock fail here loudly
    instead of regressing into a once-in-a-while c=2 wedge.
    """
    g = Genome(path=str(tmp_path / "meta.db"))
    ids = _seed(g, 6, "meta")
    registry = Registry(g)

    spy = _SpyLock(g._write_lock)
    g._write_lock = spy

    def check(name, fn, *args, **kwargs):
        before = spy.acquisitions
        fn(*args, **kwargs)
        assert spy.acquisitions > before, (
            f"{name} ran without acquiring _write_lock (W2.3 sweep regression)"
        )

    try:
        # KnowledgeStore writers swept in W2.3 Phase A
        check("log_health", g.log_health, query="q", ellipticity=0.5,
              coverage=0.5, density=0.5, freshness=0.5, genes_expressed=1,
              genes_available=1, status="healthy")
        check("touch_genes", g.touch_genes, ids[:3])
        check("link_coactivated", g.link_coactivated, ids[:3])
        check("store_harmonic_weights", g.store_harmonic_weights,
              [(ids[0], ids[1], 0.5)])
        check("store_relation", g.store_relation, ids[0], ids[1], 1, 0.9)
        check("store_relations_batch", g.store_relations_batch,
              [(ids[1], ids[2], 1, 0.8)])
        check("store_symbol_defs", g.store_symbol_defs,
              [("sweep_sym", ids[0], "function")])
        check("mark_verified", g.mark_verified, ids[:2], time.time())
        check("upsert_calibration", g.upsert_calibration,
              "write_lock_sweep_test", {"n": 1})
        check("_ensure_fts_vocab", g._ensure_fts_vocab)
        check("_refresh_snapshot", g._refresh_snapshot)
        check("checkpoint", g.checkpoint, "PASSIVE")
        check("rebuild_fts", g.rebuild_fts)
        check("compress_to_euchromatin", g.compress_to_euchromatin, ids[0])
        check("compress_to_heterochromatin", g.compress_to_heterochromatin,
              ids[1])
        check("compact", g.compact)
        check("compact_genome", g.compact_genome)
        check("upsert_doc", g.upsert_doc,
              make_gene("meta upsert body", gene_id="meta-new-000"),
              apply_gate=False)
        check("delete_gene", g.delete_gene, ids[2])
        check("vacuum", g.vacuum)

        # Registry writers (all commit on the store conn)
        before = spy.acquisitions
        p = registry.register_participant(party_id="meta@local", handle="meta")
        assert spy.acquisitions > before, (
            "registry.register_participant ran without acquiring _write_lock"
        )
        check("registry.heartbeat", registry.heartbeat, p.participant_id)
        check("registry.touch_heartbeat", registry.touch_heartbeat,
              p.participant_id)
        check("registry.update_announcement", registry.update_announcement,
              p.participant_id, model_id="test-model")
        check("registry.local_org", registry.local_org, "sweeporg")
        check("registry.local_agent", registry.local_agent,
              "sweep-agent", p.participant_id)
        check("registry.local_participant", registry.local_participant,
              "sweepuser", "meta@local")
        check("registry.attribute_gene", registry.attribute_gene,
              ids[3], participant_id=p.participant_id)
        check("registry.emit_hitl_event", registry.emit_hitl_event,
              p.participant_id, "permission_prompt")
        check("registry.sweep", registry.sweep)
    finally:
        g.close()


# ── 3. W2.5: background loops run bodies via asyncio.to_thread ───────


pytestmark_async = pytest.mark.skipif(
    not _PYTEST_ASYNCIO_AVAILABLE,
    reason="async tests require pytest-asyncio (install via pip install -e .[dev])",
)


@pytestmark_async
class TestBackgroundLoopsUseToThread:
    """Each loop body must be dispatched with asyncio.to_thread — blocking
    SQLite / filesystem work must not run directly on the event loop.
    Intervals are monkeypatched on the server package (the loops re-read
    them each iteration, the established test seam — see test_registry).
    """

    async def _drive(self, coro_fn, arg, monkeypatch, interval_name):
        from cymatix_context import server as server_mod

        monkeypatch.setattr(server_mod, interval_name, 0.01)

        dispatched: list = []
        real_to_thread = asyncio.to_thread

        async def recording_to_thread(fn, *args, **kwargs):
            dispatched.append(getattr(fn, "__name__", repr(fn)))
            return await real_to_thread(fn, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", recording_to_thread)

        task = asyncio.create_task(coro_fn(arg))
        try:
            deadline = asyncio.get_event_loop().time() + 2.0
            while not dispatched and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return dispatched

    @pytest.mark.asyncio
    async def test_background_checkpoint_uses_to_thread(self, monkeypatch):
        from cymatix_context.server.helpers import _background_checkpoint

        calls: list = []
        fake = types.SimpleNamespace(genome=types.SimpleNamespace(
            checkpoint=lambda mode: calls.append(mode),
        ))
        dispatched = await self._drive(
            _background_checkpoint, fake, monkeypatch, "_CHECKPOINT_INTERVAL",
        )
        assert dispatched, "checkpoint body never dispatched via asyncio.to_thread"
        assert calls and calls[0] == "PASSIVE"

    @pytest.mark.asyncio
    async def test_background_wal_gauge_uses_to_thread(self, monkeypatch):
        from cymatix_context.server.helpers import _background_wal_gauge

        calls: list = []
        fake = types.SimpleNamespace(genome=types.SimpleNamespace(
            emit_wal_health_gauges=lambda: calls.append(True),
        ))
        dispatched = await self._drive(
            _background_wal_gauge, fake, monkeypatch, "_WAL_GAUGE_INTERVAL",
        )
        assert dispatched, "wal-gauge body never dispatched via asyncio.to_thread"
        assert calls

    @pytest.mark.asyncio
    async def test_background_registry_sweep_uses_to_thread(self, monkeypatch):
        from cymatix_context.server.helpers import _background_registry_sweep

        calls: list = []

        def fake_sweep():
            calls.append(True)
            return {"active": 0, "gone": 0, "hard_deleted": 0}

        fake = types.SimpleNamespace(sweep=fake_sweep)
        dispatched = await self._drive(
            _background_registry_sweep, fake, monkeypatch,
            "_REGISTRY_SWEEP_INTERVAL",
        )
        assert dispatched, "registry sweep never dispatched via asyncio.to_thread"
        assert calls
