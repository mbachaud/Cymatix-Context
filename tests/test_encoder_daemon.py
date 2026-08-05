"""Tests for cymatix_context.encoder_daemon.

Fork 1 slice 1, Task 2: the SERVER side of the encoder daemon — an
``EncoderRegistry`` owning the three encode callables, a per-family
micro-batcher (one queue + one worker thread each), the HTTP surface from
``docs/design/2026-08-05-fork1-slice1-contract.md``, ``/ready`` gating with
spawn-token echo, and the capacity self-report.

Non-live discipline: no real model is ever constructed and no subprocess or
uvicorn is ever spawned. The daemon app is driven in-process through
``fastapi.testclient.TestClient`` with fakes injected into the registry;
``torch``/``transformers`` are never imported by anything under test here
(the production ``EncoderRegistry.load`` path is deliberately not exercised).

Concurrency tests assert CALL COUNTS, never wall-clock milliseconds — the
window-forced-high coalescing proofs are about how many backend calls the
batcher made, not how long it took.
"""

from __future__ import annotations

import threading
import time

import pytest

from tests.conftest import FakeBGEM3Codec, hash_vec


# ── test isolation ──────────────────────────────────────────────────
#
# Same pattern as tests/test_encoder_client.py (seam notes §7c): clear the
# process-lifetime encoder singletons other suites populate, and unset the
# daemon env vars so a dev machine that exports them cannot change what
# these tests observe. A new test file that skips these clears fails only
# in full-suite runs.


@pytest.fixture(autouse=True)
def _pristine_encoder_daemon(monkeypatch):
    from cymatix_context.backends import bgem3_codec, splade_backend, encoder_client

    bgem3_codec._GLOBAL_CODECS.clear()
    monkeypatch.setattr(splade_backend, "_model", None, raising=False)
    monkeypatch.setattr(splade_backend, "_tokenizer", None, raising=False)
    monkeypatch.setattr(splade_backend, "_device", None, raising=False)
    monkeypatch.delenv("CYMATIX_ENCODER_URL", raising=False)
    monkeypatch.delenv("CYMATIX_ENCODER_SPAWN_TOKEN", raising=False)
    encoder_client.reset_for_test()
    yield
    encoder_client.reset_for_test()


# ── fakes (seam notes §7d) ──────────────────────────────────────────


def fake_splade(text: str, top_k: int = 128, **kw) -> dict:
    """Stand-in for splade_backend.encode — one sentinel term, fixed weight."""
    return {"sentinel": 9.99}


class CountingSplade:
    """fake_splade + a per-item call counter (per-item-loop parity proof)."""

    def __init__(self):
        self.calls = 0
        self.texts: list = []
        self.top_ks: list = []
        self.lock = threading.Lock()

    def __call__(self, text: str, top_k: int = 128, **kw) -> dict:
        with self.lock:
            self.calls += 1
            self.texts.append(text)
            self.top_ks.append(top_k)
        return {"sentinel": 9.99}


class CountingSemaCodec:
    """20-dim SEMA stand-in with the LazySemaCodec-ish surface + counters."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", device=None):
        self.model_name = model_name
        self.device = device
        self.encode_calls = 0
        self.batch_calls = 0
        self.texts: list = []
        self.lock = threading.Lock()

    def encode(self, text: str):
        with self.lock:
            self.encode_calls += 1
            self.texts.append(text)
        return [0.0] * 20

    def encode_batch(self, texts, batch_size: int = 64):
        with self.lock:
            self.batch_calls += 1
        return [[0.0] * 20 for _ in texts]

    @property
    def projection_matrix(self):
        return None

    @property
    def embed_dim(self) -> int:
        return 384


def make_registry(dense=None, splade=None, sema=None, **kw):
    """An EncoderRegistry wired to fakes — the injected-registry test path."""
    from cymatix_context.encoder_daemon import EncoderRegistry

    return EncoderRegistry(
        dense_codec=dense if dense is not None else FakeBGEM3Codec(dim=8),
        splade_encode=splade if splade is not None else fake_splade,
        sema_codec=sema if sema is not None else CountingSemaCodec(),
        dense_dim=kw.pop("dense_dim", 8),
        dense_model_name=kw.pop("dense_model_name", "fake-bge-m3"),
        **kw,
    )


# ── EncoderRegistry ─────────────────────────────────────────────────


def test_registry_dense_encode_batch_delegates_to_codec():
    dense = FakeBGEM3Codec(dim=8)
    reg = make_registry(dense=dense)
    out = reg.dense_encode_batch(["alpha", "beta"], "passage")
    assert out == [hash_vec("alpha", 8).tolist(), hash_vec("beta", 8).tolist()]
    assert dense.batch_calls == 1


def test_registry_dense_encode_batch_forwards_task_to_the_codec():
    """The query prefix is the codec's job — it must see the task we got."""

    class _Recorder:
        def __init__(self):
            self.seen = []

        def encode_batch(self, texts, task="passage"):
            self.seen.append((tuple(texts), task))
            return [hash_vec(t, 8).tolist() for t in texts]

    rec = _Recorder()
    reg = make_registry(dense=rec)
    reg.dense_encode_batch(["anything"], "query")
    assert rec.seen == [(("anything",), "query")]


def test_registry_dense_meta_reports_dim_and_model():
    reg = make_registry()
    assert reg.dense_meta == {"dim": 8, "model": "fake-bge-m3"}


def test_registry_splade_encode_one_forwards_top_k():
    splade = CountingSplade()
    reg = make_registry(splade=splade)
    assert reg.splade_encode_one("hello", 64) == {"sentinel": 9.99}
    assert splade.calls == 1
    assert splade.texts == ["hello"]
    assert splade.top_ks == [64]


def test_registry_sema_encode_one_delegates():
    sema = CountingSemaCodec()
    reg = make_registry(sema=sema)
    assert reg.sema_encode_one("hello") == [0.0] * 20
    assert sema.encode_calls == 1


def test_registry_loaded_flags_true_when_all_three_present():
    reg = make_registry()
    assert reg.loaded == {"dense": True, "splade": True, "sema": True}


def test_registry_loaded_flags_false_for_missing_families():
    from cymatix_context.encoder_daemon import EncoderRegistry

    reg = EncoderRegistry(dense_codec=FakeBGEM3Codec(dim=8))
    assert reg.loaded == {"dense": True, "splade": False, "sema": False}


def test_registry_warm_touches_every_model_once():
    dense, splade, sema = FakeBGEM3Codec(dim=8), CountingSplade(), CountingSemaCodec()
    reg = make_registry(dense=dense, splade=splade, sema=sema)
    reg.warm()
    assert dense.batch_calls == 1
    assert splade.calls == 1
    assert sema.encode_calls == 1


def test_registry_max_batch_defaults_are_the_daemon_defaults():
    reg = make_registry()
    assert reg.max_batch("dense") == 16
    assert reg.max_batch("splade") == 8
    assert reg.max_batch("sema") == 64


def test_registry_max_batch_honors_explicit_batch_sizes():
    reg = make_registry(batch_sizes={"dense": 3})
    assert reg.max_batch("dense") == 3
    assert reg.max_batch("splade") == 8


# ── resolve_max_batch: [hardware] override > table > daemon default ──


def _fake_hardware(monkeypatch, *, overrides=None, table=1):
    """Stub hardware.get_hardware/recommended_batch_size — no real detection."""
    import cymatix_context.hardware as hw

    class _Info:
        batch_size_overrides = dict(overrides or {})

    monkeypatch.setattr(hw, "get_hardware", lambda: _Info())
    monkeypatch.setattr(hw, "recommended_batch_size", lambda model: table)


def test_resolve_max_batch_uses_daemon_default_when_table_floors_at_one(monkeypatch):
    from cymatix_context.encoder_daemon import resolve_max_batch

    _fake_hardware(monkeypatch, table=1)
    assert resolve_max_batch("dense") == 16
    assert resolve_max_batch("sema") == 64


def test_resolve_max_batch_prefers_table_when_larger_than_default(monkeypatch):
    from cymatix_context.encoder_daemon import resolve_max_batch

    _fake_hardware(monkeypatch, table=32)
    assert resolve_max_batch("splade") == 32


def test_resolve_max_batch_hardware_override_wins_even_when_smaller(monkeypatch):
    from cymatix_context.encoder_daemon import resolve_max_batch

    _fake_hardware(monkeypatch, overrides={"dense": 4}, table=32)
    assert resolve_max_batch("dense") == 4


def test_resolve_max_batch_falls_back_to_default_when_hardware_raises(monkeypatch):
    import cymatix_context.hardware as hw
    from cymatix_context.encoder_daemon import resolve_max_batch

    def _boom():
        raise RuntimeError("no hardware here")

    monkeypatch.setattr(hw, "get_hardware", _boom)
    assert resolve_max_batch("dense") == 16


# ── micro-batcher ───────────────────────────────────────────────────


def _batcher(name, execute, max_batch, window_ms=50.0):
    from cymatix_context.encoder_daemon import _Batcher

    return _Batcher(name, execute, max_batch=max_batch, window_s=window_ms / 1000.0)


def test_batcher_sequential_executor_runs_one_call_per_item():
    from cymatix_context.encoder_daemon import make_sequential_executor

    splade = CountingSplade()
    b = _batcher("splade", make_sequential_executor(lambda t, k: splade(t, top_k=k)), 8)
    futures = [b.submit((f"text-{i}", 128)) for i in range(5)]
    b.start()
    try:
        assert [f.result(timeout=5) for f in futures] == [{"sentinel": 9.99}] * 5
    finally:
        b.stop()
    assert splade.calls == 5, "SPLADE micro-batches must run a sequential per-item loop"
    assert splade.texts == [f"text-{i}" for i in range(5)]


def test_batcher_dense_executor_coalesces_a_drained_batch_into_one_call():
    from cymatix_context.encoder_daemon import make_dense_executor

    dense = FakeBGEM3Codec(dim=8)
    b = _batcher("dense", make_dense_executor(dense.encode_batch), 16)
    # Enqueue before starting the worker: the whole batch is already queued
    # when the window opens, so the coalescing proof needs no sleep.
    futures = [b.submit((f"text-{i}", "passage")) for i in range(8)]
    b.start()
    try:
        results = [f.result(timeout=5) for f in futures]
    finally:
        b.stop()
    assert results == [hash_vec(f"text-{i}", 8).tolist() for i in range(8)]
    assert dense.batch_calls == 1, "8 queued dense items must ride one encode_batch call"


def test_batcher_respects_max_batch_cap():
    from cymatix_context.encoder_daemon import make_dense_executor

    seen: list = []

    def _encode_batch(texts, task="passage"):
        seen.append(len(texts))
        return [hash_vec(t, 8).tolist() for t in texts]

    b = _batcher("dense", make_dense_executor(_encode_batch), 4)
    futures = [b.submit((f"text-{i}", "passage")) for i in range(20)]
    b.start()
    try:
        for f in futures:
            f.result(timeout=5)
    finally:
        b.stop()
    assert sum(seen) == 20
    assert max(seen) == 4, f"max_batch=4 must cap every batch; saw {seen}"


def test_batcher_dense_executor_groups_by_task():
    """Mixed tasks in one drained batch must not share a single task= call."""
    from cymatix_context.encoder_daemon import make_dense_executor

    calls: list = []

    def _encode_batch(texts, task="passage"):
        calls.append((tuple(texts), task))
        return [hash_vec(f"{task}:{t}", 8).tolist() for t in texts]

    b = _batcher("dense", make_dense_executor(_encode_batch), 16)
    f_q = b.submit(("q-text", "query"))
    f_p = b.submit(("p-text", "passage"))
    b.start()
    try:
        assert f_q.result(timeout=5) == hash_vec("query:q-text", 8).tolist()
        assert f_p.result(timeout=5) == hash_vec("passage:p-text", 8).tolist()
    finally:
        b.stop()
    assert sorted(t for _, t in calls) == ["passage", "query"]


def test_batcher_worker_survives_a_sequential_encode_exception():
    from cymatix_context.encoder_daemon import make_sequential_executor

    def _encode_one(text, top_k):
        if text == "boom":
            raise RuntimeError("encode exploded")
        return {"ok": 1.0}

    b = _batcher("splade", make_sequential_executor(_encode_one), 8)
    b.start()
    try:
        bad = b.submit(("boom", 128))
        with pytest.raises(RuntimeError, match="encode exploded"):
            bad.result(timeout=5)
        good = b.submit(("fine", 128))
        assert good.result(timeout=5) == {"ok": 1.0}
        assert b.is_alive(), "worker thread must survive an encode exception"
    finally:
        b.stop()


def test_batcher_sequential_exception_does_not_poison_batch_siblings():
    from cymatix_context.encoder_daemon import make_sequential_executor

    def _encode_one(text, top_k):
        if text == "boom":
            raise RuntimeError("encode exploded")
        return {"ok": 1.0}

    b = _batcher("splade", make_sequential_executor(_encode_one), 8)
    bad = b.submit(("boom", 128))
    good = b.submit(("fine", 128))
    b.start()
    try:
        with pytest.raises(RuntimeError):
            bad.result(timeout=5)
        assert good.result(timeout=5) == {"ok": 1.0}
    finally:
        b.stop()


def test_batcher_dense_batch_exception_fails_every_future_in_the_batch():
    from cymatix_context.encoder_daemon import make_dense_executor

    def _encode_batch(texts, task="passage"):
        raise RuntimeError("batch exploded")

    b = _batcher("dense", make_dense_executor(_encode_batch), 16)
    futures = [b.submit((f"t{i}", "passage")) for i in range(3)]
    b.start()
    try:
        for f in futures:
            with pytest.raises(RuntimeError, match="batch exploded"):
                f.result(timeout=5)
        assert b.is_alive(), "worker thread must survive a batch encode exception"
    finally:
        b.stop()


def test_batcher_dense_length_mismatch_fails_futures_instead_of_hanging():
    from cymatix_context.encoder_daemon import make_dense_executor

    def _encode_batch(texts, task="passage"):
        return []  # buggy backend: fewer vectors than texts

    b = _batcher("dense", make_dense_executor(_encode_batch), 16)
    fut = b.submit(("t0", "passage"))
    b.start()
    try:
        with pytest.raises(Exception):
            fut.result(timeout=5)
    finally:
        b.stop()


def test_batcher_stop_joins_the_worker_thread():
    from cymatix_context.encoder_daemon import make_sequential_executor

    b = _batcher("sema", make_sequential_executor(lambda t: [0.0] * 20), 64)
    b.start()
    assert b.is_alive()
    b.stop()
    assert not b.is_alive(), "stop() must join the worker (daemon threads still get a sentinel)"


def test_batcher_submit_after_stop_fails_fast_instead_of_hanging():
    from cymatix_context.encoder_daemon import make_sequential_executor

    b = _batcher("sema", make_sequential_executor(lambda t: [0.0] * 20), 64)
    b.start()
    b.stop()
    fut = b.submit(("late",))
    with pytest.raises(RuntimeError):
        fut.result(timeout=5)


def test_batcher_stop_drains_pending_futures_with_an_exception():
    """A never-started batcher's queued work must fail, not hang forever."""
    from cymatix_context.encoder_daemon import make_sequential_executor

    b = _batcher("sema", make_sequential_executor(lambda t: [0.0] * 20), 64)
    fut = b.submit(("queued-but-never-run",))
    b.stop()
    with pytest.raises(RuntimeError):
        fut.result(timeout=5)


@pytest.mark.concurrency
def test_batcher_is_thread_safe_under_concurrent_submit():
    """Liveness first: 8 submitting threads must all get correct results."""
    from cymatix_context.encoder_daemon import make_sequential_executor

    sema = CountingSemaCodec()
    b = _batcher("sema", make_sequential_executor(sema.encode), 64)
    b.start()
    barrier = threading.Barrier(8)
    errors: list = []
    results: list = []
    lock = threading.Lock()

    def _worker(i):
        try:
            barrier.wait(timeout=5)
            out = b.submit((f"text-{i}",)).result(timeout=10)
            with lock:
                results.append(out)
        except Exception as exc:  # collected, asserted after liveness
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,), daemon=True) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    wedged = [t for t in threads if t.is_alive()]
    try:
        assert not wedged, "submitting threads wedged — batcher deadlock"
        assert not errors, f"concurrent submit raised: {errors}"
        assert results == [[0.0] * 20] * 8
        assert sema.encode_calls == 8
    finally:
        b.stop()


@pytest.mark.concurrency
def test_batcher_stop_while_submitting_never_wedges():
    """Shutdown racing enqueue: every future resolves (result or exception)."""
    from cymatix_context.encoder_daemon import make_sequential_executor

    b = _batcher("sema", make_sequential_executor(lambda t: [0.0] * 20), 64, window_ms=1.0)
    b.start()
    futures: list = []
    lock = threading.Lock()
    stop_flag = threading.Event()

    def _submitter(i):
        while not stop_flag.is_set():
            fut = b.submit((f"text-{i}",))
            with lock:
                futures.append(fut)
            time.sleep(0.001)

    threads = [threading.Thread(target=_submitter, args=(i,), daemon=True) for i in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    b.stop()
    stop_flag.set()
    for t in threads:
        t.join(timeout=10)
    assert not [t for t in threads if t.is_alive()], "submitter threads wedged"
    with lock:
        pending = list(futures)
    assert pending, "expected the submitters to have enqueued work"
    for fut in pending:
        # Liveness: resolved one way or the other, never left hanging.
        assert fut.done() or fut.exception(timeout=5) is not None
