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


# ── app factory: /ready gating ──────────────────────────────────────


def _app(registry=None, **kw):
    """Daemon app on fakes.

    The window defaults to 1 ms here purely to keep the suite fast — the
    worker waits out its window on every drained batch, and startup runs a
    probe bundle plus 15 warm-rate encodes. The real 8 ms default and its env
    knob are asserted explicitly below.
    """
    from cymatix_context.encoder_daemon import create_encoder_app

    kw.setdefault("batch_window_ms", 1.0)
    return create_encoder_app(registry if registry is not None else make_registry(), **kw)


def test_batch_window_defaults_to_the_client_env_knob():
    from fastapi.testclient import TestClient
    from cymatix_context.encoder_daemon import create_encoder_app

    with TestClient(create_encoder_app(make_registry())) as client:
        assert client.get("/health").json()["capacity"]["batch_window_ms"] == 8.0


def test_batch_window_honors_cymatix_encoder_batch_window_ms(monkeypatch):
    from fastapi.testclient import TestClient
    from cymatix_context.encoder_daemon import create_encoder_app

    monkeypatch.setenv("CYMATIX_ENCODER_BATCH_WINDOW_MS", "3")
    with TestClient(create_encoder_app(make_registry())) as client:
        assert client.get("/health").json()["capacity"]["batch_window_ms"] == 3.0


def test_ready_is_503_before_startup_runs():
    """Clients gate on /ready; before lifespan startup nothing is loaded."""
    from fastapi.testclient import TestClient

    client = TestClient(_app())  # no context manager => no lifespan
    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert isinstance(body["state"], str) and body["state"]


def test_ready_is_200_after_startup():
    from fastapi.testclient import TestClient

    with TestClient(_app()) as client:
        resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["spawn_token"] is None
    assert isinstance(body["probe_ms"], float)
    assert body["probe_ms"] >= 0.0


def test_ready_echoes_the_spawn_token(monkeypatch):
    """Windows venv shims re-exec python; pid is not authoritative identity."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CYMATIX_ENCODER_SPAWN_TOKEN", "tok-abc123")
    with TestClient(_app()) as client:
        assert client.get("/ready").json()["spawn_token"] == "tok-abc123"
        assert client.get("/health").json()["spawn_token"] == "tok-abc123"


def test_ready_state_can_be_flipped_directly_by_tests():
    from fastapi.testclient import TestClient

    app = _app()
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        app.state.ready_state.mark("reloading")
        resp = client.get("/ready")
        assert resp.status_code == 503
        assert resp.json()["state"] == "reloading"


# ── /health ─────────────────────────────────────────────────────────


def test_health_is_200_even_before_ready():
    from fastapi.testclient import TestClient

    client = TestClient(_app())  # no lifespan
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["ready"] is False
    assert isinstance(body["capacity"], dict)


def test_health_reports_this_process_pid():
    import os
    from fastapi.testclient import TestClient

    client = TestClient(_app())
    assert client.get("/health").json()["pid"] == os.getpid()


def test_health_capacity_has_the_self_report_shape_after_ready():
    from fastapi.testclient import TestClient

    with TestClient(_app()) as client:
        cap = client.get("/health").json()["capacity"]

    # Shape only — never absolute numbers (rates come from probing fakes).
    for key in (
        "devices",
        "warm_encodes_per_s",
        "bundles_per_s",
        "max_batch",
        "batch_window_ms",
        "probe_ms",
        "torch_version",
        "vram_total_gb",
        "vram_free_gb",
        "system_ram_gb",
    ):
        assert key in cap, f"capacity report missing {key!r}: {sorted(cap)}"
    assert sorted(cap["devices"]) == ["dense", "sema", "splade"]
    assert sorted(cap["warm_encodes_per_s"]) == ["dense", "sema", "splade"]
    assert sorted(cap["bundles_per_s"]) == ["interleaved", "pipelined"]
    assert cap["max_batch"] == {"dense": 16, "splade": 8, "sema": 64}
    for family, rate in cap["warm_encodes_per_s"].items():
        assert isinstance(rate, float) and rate > 0.0, f"{family}: {rate!r}"
    for mode, rate in cap["bundles_per_s"].items():
        assert isinstance(rate, float) and rate > 0.0, f"{mode}: {rate!r}"


def test_capacity_report_is_logged_once_at_the_ready_flip(caplog):
    import logging

    from fastapi.testclient import TestClient

    with caplog.at_level(logging.INFO, logger="cymatix.encoder_daemon"):
        with TestClient(_app()):
            pass
    ready_logs = [r for r in caplog.records if "capacity" in r.message.lower()]
    assert len(ready_logs) == 1, (
        f"expected one capacity log line; got {[r.message for r in caplog.records]}"
    )


def test_capacity_report_is_json_serializable_with_no_inf_or_nan():
    """Starlette's JSONResponse uses allow_nan=False — inf rates would 500."""
    import json
    import math

    from fastapi.testclient import TestClient

    with TestClient(_app()) as client:
        cap = client.get("/health").json()["capacity"]
    json.dumps(cap, allow_nan=False)
    for section in ("warm_encodes_per_s", "bundles_per_s"):
        for value in cap[section].values():
            assert value is None or math.isfinite(value)


# ── encode endpoints ────────────────────────────────────────────────


def test_encode_dense_returns_vectors_dim_and_model():
    from fastapi.testclient import TestClient

    with TestClient(_app()) as client:
        resp = client.post("/encode/dense", json={"texts": ["alpha", "beta"], "task": "passage"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["vectors"] == [hash_vec("alpha", 8).tolist(), hash_vec("beta", 8).tolist()]
    assert body["dim"] == 8
    assert body["model"] == "fake-bge-m3"


def test_encode_dense_empty_texts_returns_empty_without_encoding():
    from fastapi.testclient import TestClient

    dense = FakeBGEM3Codec(dim=8)
    app = _app(make_registry(dense=dense))
    with TestClient(app) as client:
        baseline = dense.batch_calls
        resp = client.post("/encode/dense", json={"texts": []})
    assert resp.json()["vectors"] == []
    assert dense.batch_calls == baseline, "empty texts must not reach the codec"


def test_encode_dense_forwards_the_query_task():
    from fastapi.testclient import TestClient

    class _TaskAware:
        """encode_batch that honors task= the way the real codec's prefix does."""

        def encode_batch(self, texts, task="passage"):
            return [hash_vec(f"{task}:{t}", 8).tolist() for t in texts]

    with TestClient(_app(make_registry(dense=_TaskAware()))) as client:
        query = client.post("/encode/dense", json={"texts": ["q"], "task": "query"}).json()
        passage = client.post("/encode/dense", json={"texts": ["q"], "task": "passage"}).json()
    assert query["vectors"] == [hash_vec("query:q", 8).tolist()]
    assert passage["vectors"] == [hash_vec("passage:q", 8).tolist()]
    assert query["vectors"] != passage["vectors"]


def test_encode_dense_rejects_an_unknown_task():
    from fastapi.testclient import TestClient

    with TestClient(_app()) as client:
        resp = client.post("/encode/dense", json={"texts": ["a"], "task": "nonsense"})
    assert resp.status_code == 422


def test_encode_splade_returns_sparse_and_forwards_top_k():
    from fastapi.testclient import TestClient

    splade = CountingSplade()
    with TestClient(_app(make_registry(splade=splade))) as client:
        resp = client.post("/encode/splade", json={"texts": ["hello"], "top_k": 64})
    assert resp.status_code == 200
    assert resp.json()["sparse"] == [{"sentinel": 9.99}]
    assert splade.texts[-1] == "hello"
    assert splade.top_ks[-1] == 64


def test_encode_splade_preserves_weight_order_and_rounding():
    """Byte parity: descending-weight insertion order and round(w, 4) survive JSON."""
    from fastapi.testclient import TestClient

    ordered = {"zeta": 9.1234, "alpha": 3.5, "mid": 0.0001}

    def _splade(text, top_k=128, **kw):
        return dict(ordered)

    with TestClient(_app(make_registry(splade=_splade))) as client:
        sparse = client.post("/encode/splade", json={"texts": ["x"]}).json()["sparse"][0]
    assert list(sparse.keys()) == ["zeta", "alpha", "mid"]
    assert sparse == ordered


def test_encode_splade_default_top_k_is_128():
    from fastapi.testclient import TestClient

    splade = CountingSplade()
    with TestClient(_app(make_registry(splade=splade))) as client:
        client.post("/encode/splade", json={"texts": ["hello"]})
    assert splade.top_ks[-1] == 128


def test_encode_sema_returns_twenty_dim_vectors():
    from fastapi.testclient import TestClient

    sema = CountingSemaCodec()
    with TestClient(_app(make_registry(sema=sema))) as client:
        baseline = sema.encode_calls
        resp = client.post("/encode/sema", json={"texts": ["a", "b"]})
    assert resp.status_code == 200
    assert resp.json()["vectors"] == [[0.0] * 20, [0.0] * 20]
    assert sema.encode_calls - baseline == 2, "SEMA must run a per-item loop"


def test_encode_bundle_returns_all_three_parts_consistent_with_the_fakes():
    from fastapi.testclient import TestClient

    splade, sema = CountingSplade(), CountingSemaCodec()

    class _TaskAware:
        def encode_batch(self, texts, task="passage"):
            return [hash_vec(f"{task}:{t}", 8).tolist() for t in texts]

    app = _app(make_registry(dense=_TaskAware(), splade=splade, sema=sema))
    with TestClient(app) as client:
        resp = client.post("/encode/bundle", json={"text": "q", "task": "query", "top_k": 64})
    assert resp.status_code == 200
    body = resp.json()
    assert body["dense"] == hash_vec("query:q", 8).tolist()
    assert body["splade"] == {"sentinel": 9.99}
    assert body["sema"] == [0.0] * 20
    assert splade.top_ks[-1] == 64


def test_encode_bundle_defaults_to_query_task_and_top_k_128():
    from fastapi.testclient import TestClient

    splade = CountingSplade()

    class _TaskAware:
        def encode_batch(self, texts, task="passage"):
            return [hash_vec(f"{task}:{t}", 8).tolist() for t in texts]

    with TestClient(_app(make_registry(dense=_TaskAware(), splade=splade))) as client:
        body = client.post("/encode/bundle", json={"text": "q"}).json()
    assert body["dense"] == hash_vec("query:q", 8).tolist()
    assert splade.top_ks[-1] == 128


# ── gating + failure modes ──────────────────────────────────────────


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/encode/dense", {"texts": ["a"]}),
        ("/encode/splade", {"texts": ["a"]}),
        ("/encode/sema", {"texts": ["a"]}),
        ("/encode/bundle", {"text": "a"}),
    ],
)
def test_encode_before_ready_is_refused_with_503(path, payload):
    from fastapi.testclient import TestClient

    app = _app()
    with TestClient(app) as client:
        app.state.ready_state.mark("loading")
        resp = client.post(path, json=payload)
    assert resp.status_code == 503
    assert "ready" in resp.json()["detail"].lower()


def test_encode_failure_returns_500_with_detail_and_the_worker_survives():
    from fastapi.testclient import TestClient

    def _splade(text, top_k=128, **kw):
        if text == "boom":
            raise RuntimeError("splade exploded")
        return {"sentinel": 9.99}

    with TestClient(_app(make_registry(splade=_splade))) as client:
        bad = client.post("/encode/splade", json={"texts": ["boom"]})
        good = client.post("/encode/splade", json={"texts": ["fine"]})
    assert bad.status_code == 500
    assert "splade exploded" in bad.json()["detail"]
    assert good.status_code == 200, "the worker thread must survive an encode exception"
    assert good.json()["sparse"] == [{"sentinel": 9.99}]


def test_dense_encode_failure_returns_500_and_the_worker_survives():
    from fastapi.testclient import TestClient

    class _Flaky:
        def __init__(self):
            self.calls = 0

        def encode_batch(self, texts, task="passage"):
            self.calls += 1
            if any(t == "boom" for t in texts):
                raise RuntimeError("dense exploded")
            return [hash_vec(t, 8).tolist() for t in texts]

    flaky = _Flaky()
    with TestClient(_app(make_registry(dense=flaky))) as client:
        bad = client.post("/encode/dense", json={"texts": ["boom"]})
        good = client.post("/encode/dense", json={"texts": ["fine"]})
    assert bad.status_code == 500
    assert "dense exploded" in bad.json()["detail"]
    assert good.status_code == 200
    assert good.json()["vectors"] == [hash_vec("fine", 8).tolist()]


def test_request_timeout_knob_defaults_to_30s_and_is_env_tunable(monkeypatch):
    from cymatix_context.encoder_daemon import request_timeout_s

    assert request_timeout_s() == 30.0
    monkeypatch.setenv("CYMATIX_ENCODER_REQUEST_TIMEOUT_S", "0.25")
    assert request_timeout_s() == 0.25


def test_a_wedged_worker_times_out_the_request_instead_of_hanging(monkeypatch):
    """A hung encode must return an error inside the request timeout budget."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CYMATIX_ENCODER_REQUEST_TIMEOUT_S", "0.3")
    gate = threading.Event()

    def _slow_splade(text, top_k=128, **kw):
        if text == "hangs":  # the readiness probe's text must still fly through
            gate.wait(timeout=10)
        return {"sentinel": 9.99}

    try:
        with TestClient(_app(make_registry(splade=_slow_splade))) as client:
            resp = client.post("/encode/splade", json={"texts": ["hangs"]})
            assert resp.status_code == 500
            assert "timed out" in resp.json()["detail"].lower()
            gate.set()  # release the worker before shutdown joins it
    finally:
        gate.set()


# ── EncoderClient (Task 1) round-trips against the real daemon app ──


class _TestClientTransport:
    """Routes urllib requests from EncoderClient into an in-process TestClient."""

    def __init__(self, client):
        self.client = client
        self.timeouts = []

    def urlopen(self, req, timeout=None):
        import io
        import urllib.parse

        assert timeout is not None, "no hanging requests — timeout required"
        self.timeouts.append(timeout)
        path = urllib.parse.urlsplit(req.full_url).path
        if req.data is None:
            resp = self.client.get(path)
        else:
            resp = self.client.post(
                path, content=req.data, headers={"Content-Type": "application/json"}
            )

        class _Resp(io.BytesIO):
            status = resp.status_code

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        return _Resp(resp.content)


def test_encoder_client_round_trips_every_endpoint_against_the_daemon(monkeypatch):
    import urllib.request

    from fastapi.testclient import TestClient

    from cymatix_context.backends.encoder_client import EncoderClient

    splade, sema = CountingSplade(), CountingSemaCodec()
    with TestClient(_app(make_registry(splade=splade, sema=sema))) as tc:
        transport = _TestClientTransport(tc)
        monkeypatch.setattr(urllib.request, "urlopen", transport.urlopen)
        client = EncoderClient("http://127.0.0.1:11439", timeout_s=5.0)

        assert client.is_ready() is True
        assert client.encode_dense(["alpha"], task="passage") == [hash_vec("alpha", 8).tolist()]
        assert client.encode_splade(["alpha"], top_k=64) == [{"sentinel": 9.99}]
        assert client.encode_sema(["alpha"]) == [[0.0] * 20]
        bundle = client.encode_bundle("alpha", task="query", top_k=64)

    assert set(bundle) == {"dense", "splade", "sema"}
    assert bundle["splade"] == {"sentinel": 9.99}
    assert bundle["sema"] == [0.0] * 20
    assert len(bundle["dense"]) == 8
    assert all(t is not None for t in transport.timeouts)


def test_encoder_client_is_ready_is_false_while_the_daemon_is_loading(monkeypatch):
    import urllib.request

    from fastapi.testclient import TestClient

    from cymatix_context.backends.encoder_client import EncoderClient

    app = _app()
    with TestClient(app) as tc:
        app.state.ready_state.mark("loading")
        transport = _TestClientTransport(tc)
        monkeypatch.setattr(urllib.request, "urlopen", transport.urlopen)
        assert EncoderClient("http://127.0.0.1:11439", timeout_s=5.0).is_ready() is False


# ── micro-batch coalescing over HTTP ────────────────────────────────


def _hammer(client, path, payload_for, n=8):
    """N concurrent POSTs released together; returns (responses, wedged, errors)."""
    barrier = threading.Barrier(n)
    responses: list = []
    errors: list = []
    lock = threading.Lock()

    def _worker(i):
        try:
            barrier.wait(timeout=10)
            resp = client.post(path, json=payload_for(i))
            with lock:
                responses.append((i, resp))
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    wedged = [t for t in threads if t.is_alive()]
    return responses, wedged, errors


@pytest.mark.concurrency
def test_concurrent_dense_posts_coalesce_into_fewer_batch_calls():
    """Window forced high: N concurrent requests must ride < N encode_batch calls."""
    from fastapi.testclient import TestClient

    dense = FakeBGEM3Codec(dim=8)
    n = 8
    with TestClient(_app(make_registry(dense=dense), batch_window_ms=50.0)) as client:
        baseline = dense.batch_calls
        responses, wedged, errors = _hammer(
            client, "/encode/dense", lambda i: {"texts": [f"text-{i}"]}, n=n
        )

    # Liveness before correctness.
    assert not wedged, "concurrent dense POSTs wedged"
    assert not errors, f"concurrent dense POSTs raised: {errors}"
    assert len(responses) == n
    for i, resp in responses:
        assert resp.status_code == 200
        assert resp.json()["vectors"] == [hash_vec(f"text-{i}", 8).tolist()]

    calls = dense.batch_calls - baseline
    assert calls >= 1
    assert calls < n, f"micro-batcher did not coalesce: {calls} encode_batch calls for {n} requests"


@pytest.mark.concurrency
def test_concurrent_bundles_coalesce_dense_but_keep_splade_and_sema_per_item():
    """The byte-parity split, proven by call counts on one hammer run."""
    from fastapi.testclient import TestClient

    dense, splade, sema = FakeBGEM3Codec(dim=8), CountingSplade(), CountingSemaCodec()
    n = 8
    registry = make_registry(dense=dense, splade=splade, sema=sema)
    with TestClient(_app(registry, batch_window_ms=50.0)) as client:
        base_dense, base_splade, base_sema = dense.batch_calls, splade.calls, sema.encode_calls
        responses, wedged, errors = _hammer(
            client, "/encode/bundle", lambda i: {"text": f"text-{i}"}, n=n
        )

    assert not wedged, "concurrent bundle POSTs wedged"
    assert not errors, f"concurrent bundle POSTs raised: {errors}"
    assert len(responses) == n
    for _, resp in responses:
        assert resp.status_code == 200

    assert dense.batch_calls - base_dense < n, "dense must coalesce"
    assert splade.calls - base_splade == n, "SPLADE must stay one encode per item"
    assert sema.encode_calls - base_sema == n, "SEMA must stay one encode per item"


# ── Task 4: two-client hammer + first-use readiness race ────────────


@pytest.mark.concurrency
def test_two_clients_hammer_mixed_dense_and_bundle_no_cross_request_bleed():
    """Two daemon threads hammer one app with K=25 mixed /encode/dense +
    /encode/bundle requests each, distinct per-thread texts.

    This is the GIL-side tokenizer-state concern from the kickoff: dense
    requests ride a shared micro-batcher that groups items from concurrent
    callers into one ``encode_batch`` call and zips results back out — a
    mis-aligned zip would hand one caller's vector to another. Every
    response must equal ``hash_vec`` of ITS OWN text, never a sibling's.
    """
    from fastapi.testclient import TestClient

    dense = FakeBGEM3Codec(dim=8)
    splade, sema = CountingSplade(), CountingSemaCodec()
    registry = make_registry(dense=dense, splade=splade, sema=sema)
    n_clients, k = 2, 25
    barrier = threading.Barrier(n_clients)
    results: list = []
    errors: list = []
    lock = threading.Lock()

    def _hammer_client(cid, client):
        try:
            barrier.wait(timeout=10)
            for i in range(k):
                text = f"client{cid}-item{i}"
                if i % 2 == 0:
                    resp = client.post(
                        "/encode/dense", json={"texts": [text], "task": "passage"}
                    )
                    kind = "dense"
                else:
                    resp = client.post(
                        "/encode/bundle", json={"text": text, "task": "query", "top_k": 32}
                    )
                    kind = "bundle"
                with lock:
                    results.append((cid, i, kind, text, resp))
        except Exception as exc:
            with lock:
                errors.append(exc)

    with TestClient(_app(registry, batch_window_ms=50.0)) as client:
        threads = [
            threading.Thread(target=_hammer_client, args=(cid, client), daemon=True)
            for cid in range(n_clients)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        wedged = [t for t in threads if t.is_alive()]

    # Liveness before correctness.
    assert not wedged, "hammer client threads wedged"
    assert not errors, f"hammer client threads raised: {errors}"
    assert len(results) == n_clients * k

    for cid, i, kind, text, resp in results:
        assert resp.status_code == 200, (
            f"{kind} client{cid} item{i}: {resp.status_code} {resp.text}"
        )
        expected = hash_vec(text, 8).tolist()
        got = resp.json()["vectors"][0] if kind == "dense" else resp.json()["dense"]
        assert got == expected, (
            f"cross-request bleed: {kind} client{cid} item{i} ({text!r}) did not get "
            "the vector for its own text"
        )

    # Proof of real interleaving, not two threads that happened to run
    # back-to-back: within one client thread every request blocks on its own
    # response before the next is sent, so no two dense-family submissions
    # from the SAME thread can ever land in the same drained batch. Any
    # coalescing observed here can only come from genuine cross-thread
    # overlap of the two hammer threads.
    total_dense_submissions = n_clients * k
    assert 1 <= dense.batch_calls < total_dense_submissions, (
        f"expected cross-thread batch coalescing ({dense.batch_calls} encode_batch calls "
        f"for {total_dense_submissions} submissions) — this run does not prove concurrent "
        "interleaving between the two hammer threads"
    )


@pytest.mark.concurrency
def test_ready_gate_eight_thread_first_use_race_blocks_until_ready_flips(monkeypatch):
    """8-thread first-use readiness race against the client-side ready gate.

    ``encoder_client.ready_gate`` (module docstring, "shared one-shot
    readiness gate") is process-wide, not per-codec: if 8 concurrent first
    users each polled ``/ready`` on their own, whichever one lost the
    daemon-not-ready race would open the shared circuit and strand every
    seam in permanent in-process fallback even though the daemon comes up
    moments later. Only the lock-winning thread may poll; every racer must
    block on the gate itself (not skip it, not error) and observe the
    eventual ready flip.
    """
    import urllib.request

    from fastapi.testclient import TestClient

    from cymatix_context.backends import encoder_client
    from cymatix_context.backends.encoder_client import EncoderClient

    class _CountingClient(EncoderClient):
        """Counts ``wait_ready()`` invocations — the single-flight proof."""

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.wait_ready_calls = 0
            self._count_lock = threading.Lock()

        def wait_ready(self, budget_s, poll_s=0.5):
            with self._count_lock:
                self.wait_ready_calls += 1
            return super().wait_ready(budget_s, poll_s=poll_s)

    app = _app()
    with TestClient(app) as tc:
        app.state.ready_state.mark("loading")  # simulate: daemon not ready yet
        transport = _TestClientTransport(tc)
        monkeypatch.setattr(urllib.request, "urlopen", transport.urlopen)
        client = _CountingClient("http://127.0.0.1:11439", timeout_s=5.0)

        def _flip_after_delay():
            time.sleep(0.1)
            app.state.ready_state.mark_ready(probe_ms=1.0, capacity={})

        flipper = threading.Thread(target=_flip_after_delay, daemon=True)
        barrier = threading.Barrier(8)
        results: list = []
        errors: list = []
        lock = threading.Lock()

        def _racer():
            try:
                barrier.wait(timeout=5)
                ok = encoder_client.ready_gate(client)
                with lock:
                    results.append(ok)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=_racer, daemon=True) for _ in range(8)]
        flipper.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        wedged = [t for t in threads if t.is_alive()]
        flipper.join(timeout=15)

    # Liveness before correctness.
    assert not wedged, "readiness racers wedged"
    assert not errors, f"readiness racers raised: {errors}"
    assert results == [True] * 8, f"every racer must observe ready=True; got {results}"
    assert client.wait_ready_calls == 1, (
        f"only the lock-winning thread may poll wait_ready(); got {client.wait_ready_calls}"
    )
    assert encoder_client.ready_probed() is True


# ── main() / CLI ────────────────────────────────────────────────────


def test_default_port_is_11439():
    from cymatix_context.encoder_daemon import DEFAULT_HOST, DEFAULT_PORT

    assert DEFAULT_PORT == 11439
    assert DEFAULT_HOST == "127.0.0.1"


def test_arg_parser_defaults():
    from cymatix_context.encoder_daemon import build_arg_parser

    args = build_arg_parser().parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 11439
    assert args.log_level == "info"


def test_arg_parser_overrides():
    from cymatix_context.encoder_daemon import build_arg_parser

    args = build_arg_parser().parse_args(
        ["--host", "0.0.0.0", "--port", "9", "--log-level", "debug"]
    )
    assert (args.host, args.port, args.log_level) == ("0.0.0.0", 9, "debug")


def test_main_inits_hardware_before_building_the_app(monkeypatch):
    """Startup order is load-bearing: any get_hardware() before init_from_config
    freezes the singleton with defaults and silently drops the layer pins."""
    import cymatix_context.encoder_daemon as mod
    import cymatix_context.hardware as hw

    order: list = []

    monkeypatch.setattr(hw, "init_from_config", lambda **kw: order.append(("hardware", kw)))
    monkeypatch.setattr(mod, "create_encoder_app", lambda *a, **kw: order.append("app") or "APP")
    monkeypatch.setattr(mod.uvicorn, "run", lambda app, **kw: order.append(("uvicorn", app, kw)))

    mod.main([])

    assert [step[0] if isinstance(step, tuple) else step for step in order] == [
        "hardware",
        "app",
        "uvicorn",
    ]
    hw_kwargs = order[0][1]
    assert sorted(hw_kwargs["layer_devices"]) == ["dense", "rerank", "sema", "splade"]
    assert order[2][1] == "APP"
    assert order[2][2]["host"] == "127.0.0.1"
    assert order[2][2]["port"] == 11439


# ── import hygiene ──────────────────────────────────────────────────


def test_module_never_imports_torch_at_module_scope():
    """Model libraries load inside EncoderRegistry.load(), never at import."""
    import ast
    from pathlib import Path

    import cymatix_context.encoder_daemon as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    banned = {"torch", "transformers", "sentence_transformers", "FlagEmbedding"}
    top_level = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.append(node.module.split(".")[0])
    assert not (banned & set(top_level)), (
        f"module-scope model import: {sorted(banned & set(top_level))}"
    )


# ── production bring-up path (EncoderRegistry.load stubbed — no weights) ──


def _await_ready_state(client, wanted: str, budget_s: float = 15.0) -> dict:
    """Poll /ready until it reports *wanted* (liveness, not a latency assert).

    The 200 body carries no ``state`` (contract: ready/spawn_token/probe_ms),
    so "ready" is matched on the flag itself.
    """
    deadline = time.monotonic() + budget_s
    while True:
        body = client.get("/ready").json()
        if body.get("state") == wanted or (wanted == "ready" and body.get("ready")):
            return body
        if time.monotonic() >= deadline:
            return body
        time.sleep(0.005)


def test_production_bring_up_inits_hardware_before_constructing_models(monkeypatch):
    """registry=None is the production path: HTTP answers while weights load."""
    from fastapi.testclient import TestClient

    import cymatix_context.encoder_daemon as mod
    import cymatix_context.hardware as hw

    calls: list = []
    monkeypatch.setattr(hw, "init_from_config", lambda **kw: calls.append(("hardware", kw)))

    fake = make_registry()

    def _fake_load(cfg):
        calls.append("load")
        return fake

    monkeypatch.setattr(mod.EncoderRegistry, "load", staticmethod(_fake_load))

    with TestClient(mod.create_encoder_app(batch_window_ms=1.0)) as client:
        # /health is up while the background thread is still loading.
        assert client.get("/health").status_code == 200
        assert _await_ready_state(client, "ready")["ready"] is True
        assert client.get("/ready").status_code == 200

    assert [c[0] if isinstance(c, tuple) else c for c in calls] == ["hardware", "load"], (
        "hardware init must run before any model constructs — a get_hardware() "
        "first freezes the singleton with defaults and drops the layer pins"
    )
    assert sorted(calls[0][1]["layer_devices"]) == ["dense", "rerank", "sema", "splade"]


def test_production_bring_up_failure_marks_failed_and_keeps_health_up(monkeypatch):
    """A broken model install must not kill the HTTP surface — clients fall back."""
    from fastapi.testclient import TestClient

    import cymatix_context.encoder_daemon as mod
    import cymatix_context.hardware as hw

    monkeypatch.setattr(hw, "init_from_config", lambda **kw: None)

    def _boom(cfg):
        raise RuntimeError("weights missing")

    monkeypatch.setattr(mod.EncoderRegistry, "load", staticmethod(_boom))

    with TestClient(mod.create_encoder_app(batch_window_ms=1.0)) as client:
        body = _await_ready_state(client, "failed")
        assert body["ready"] is False
        assert body["state"] == "failed"
        assert "weights missing" in (body["error"] or "")
        assert client.get("/ready").status_code == 503
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["ready"] is False


def test_production_bring_up_encode_is_refused_until_ready(monkeypatch):
    """No encode may sneak in against a half-loaded registry."""
    from fastapi.testclient import TestClient

    import cymatix_context.encoder_daemon as mod
    import cymatix_context.hardware as hw

    monkeypatch.setattr(hw, "init_from_config", lambda **kw: None)
    monkeypatch.setattr(mod.EncoderRegistry, "load", staticmethod(lambda cfg: make_registry()))

    app = mod.create_encoder_app(batch_window_ms=1.0)
    with TestClient(app) as client:
        app.state.ready_state.mark("loading")
        assert client.post("/encode/dense", json={"texts": ["a"]}).status_code == 503


# ── review fixes ────────────────────────────────────────────────────


def test_shutdown_during_load_stops_batchers_without_a_self_join(monkeypatch, caplog):
    """Shutdown racing a multi-GB load: bring-up's recovery runs ON the startup
    thread, so it must not try to join itself (CPython raises RuntimeError,
    which would leave all three batcher threads running and the daemon
    reporting `failed` instead of `stopped`)."""
    import logging

    import cymatix_context.encoder_daemon as mod
    import cymatix_context.hardware as hw

    monkeypatch.setattr(hw, "init_from_config", lambda **kw: None)
    load_gate = threading.Event()

    def _slow_load(cfg):
        assert load_gate.wait(timeout=15), "load gate never opened"
        return make_registry()

    monkeypatch.setattr(mod.EncoderRegistry, "load", staticmethod(_slow_load))

    daemon = mod._Daemon(None, window_s=0.001)
    with caplog.at_level(logging.ERROR, logger="cymatix.encoder_daemon"):
        daemon.start()  # startup thread parks inside the load
        daemon._shutdown.set()  # lifespan shutdown fires mid-load (what stop() sets first)
        load_gate.set()  # ...and only then do the weights finish loading
        daemon._startup_thread.join(timeout=20)

    assert not daemon._startup_thread.is_alive(), "startup thread wedged"
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], f"bring-up recovery raised: {errors}"
    assert daemon.ready_state.state == "stopped"
    assert daemon.batchers, "batchers were never installed — test staged the wrong race"
    alive = [name for name, b in daemon.batchers.items() if b.is_alive()]
    assert not alive, f"batcher threads survived shutdown-during-load: {alive}"


def test_warm_rates_are_measured_outside_the_micro_batcher():
    """Only the probe BUNDLE rides the micro-batch path.

    A warm-rate probe submitted through a batcher measures
    ``1/(window + t_encode)``, so every reported enc/s would be hard-capped at
    ``1/window`` (125/s at the 8 ms default) — a property of the batcher, not
    of the model, and ``bundles_per_s`` (the number the fork's bars are quoted
    against) is computed from those rates. Asserted by call path, never by
    elapsed time.
    """
    from fastapi.testclient import TestClient

    from cymatix_context.encoder_daemon import WARM_PROBE_ENCODES

    def _batcher_thread() -> bool:
        return threading.current_thread().name.startswith("encoder-batcher-")

    class _PathAwareDense:
        def __init__(self):
            self.calls = []  # (n_texts, via_batcher)

        def encode_batch(self, texts, task="passage"):
            self.calls.append((len(texts), _batcher_thread()))
            return [hash_vec(t, 8).tolist() for t in texts]

    class _PathAwareSplade:
        def __init__(self):
            self.calls = []

        def __call__(self, text, top_k=128, **kw):
            self.calls.append(_batcher_thread())
            return {"sentinel": 9.99}

    class _PathAwareSema:
        def __init__(self):
            self.calls = []

        def encode(self, text):
            self.calls.append(_batcher_thread())
            return [0.0] * 20

    dense, splade, sema = _PathAwareDense(), _PathAwareSplade(), _PathAwareSema()
    registry = make_registry(dense=dense, splade=splade, sema=sema)
    # Window forced high: if the rate probe went through the batcher, this is
    # exactly the configuration that corrupts the receipt.
    with TestClient(_app(registry, batch_window_ms=50.0)):
        pass

    assert [via for _, via in dense.calls].count(True) == 1, (
        f"dense: only the probe bundle may ride the batcher; got {dense.calls}"
    )
    assert splade.calls.count(True) == 1, f"splade: {splade.calls}"
    assert sema.calls.count(True) == 1, f"sema: {sema.calls}"

    # Off-batcher work = the eager warm() touch + the WARM_PROBE_ENCODES rate probe.
    assert splade.calls.count(False) == 1 + WARM_PROBE_ENCODES
    assert sema.calls.count(False) == 1 + WARM_PROBE_ENCODES
    direct_dense = [n for n, via in dense.calls if not via]
    assert direct_dense == [1, WARM_PROBE_ENCODES], (
        "dense warm rate must be one batched call of WARM_PROBE_ENCODES texts "
        f"(that is how the daemon serves dense); got {direct_dense}"
    )


def test_warm_rates_survive_a_model_that_raises_during_the_rate_probe():
    """A flaky model must not block the ready flip — its rate reports None."""
    from fastapi.testclient import TestClient

    class _RateBomb:
        def __init__(self):
            self.calls = 0

        def encode(self, text):
            # 1 = the eager warm(), 2 = the probe bundle; the rate probe
            # (3..3+WARM_PROBE_ENCODES) is what explodes.
            self.calls += 1
            if self.calls > 2:
                raise RuntimeError("sema exploded")
            return [0.0] * 20

    with TestClient(_app(make_registry(sema=_RateBomb()))) as client:
        body = client.get("/ready").json()
        cap = client.get("/health").json()["capacity"]
    assert body["ready"] is True
    assert cap["warm_encodes_per_s"]["sema"] is None
    assert cap["bundles_per_s"] == {"interleaved": None, "pipelined": None}
