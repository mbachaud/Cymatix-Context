"""Issue #493: the replay block (index fingerprint + git sha) must never be
computed on the request path. Stale-while-revalidate via one background worker."""

from __future__ import annotations

import hashlib
import threading
import time

from cymatix_context.telemetry import trace as tr

SLEEP_S = 0.2


class _Cur:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _SlowConn:
    def __init__(self, owner):
        self._owner = owner

    def execute(self, sql, *a):
        with self._owner.lock:
            self._owner.calls += 1
            self._owner.in_flight += 1
            self._owner.max_in_flight = max(self._owner.max_in_flight, self._owner.in_flight)
        try:
            time.sleep(self._owner.delay)
        finally:
            with self._owner.lock:
                self._owner.in_flight -= 1
        return _Cur((42, 77))


class _FakeGenome:
    def __init__(self, delay=SLEEP_S):
        self.delay = delay
        self.lock = threading.Lock()
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.read_conn = _SlowConn(self)


class _Cfg:
    def model_dump(self, **kw):
        return {"a": 1}


def _tracer(tmp_path):
    return tr.PacketTracer(
        {"enabled": True, "level": "metadata", "path": str(tmp_path),
         "hash_chain": False, "rotate_bytes": 10_000_000, "sampler_ratio": 1.0},
    )


def _poll(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _expected():
    return hashlib.sha256(b"42:77").hexdigest()[:32]


def test_expired_ttl_emit_does_not_block_and_refresh_lands(tmp_path):
    t = _tracer(tmp_path)
    g = _FakeGenome()
    try:
        t.replay_info(g, _Cfg(), ttl_s=0.05)
        assert _poll(lambda: t.replay_info(g, _Cfg(), ttl_s=1e9).get("doc_count") == 42)
        time.sleep(0.1)  # ttl expired
        t0 = time.perf_counter()
        t.replay_info(g, _Cfg(), ttl_s=0.05)
        assert (time.perf_counter() - t0) < 0.02
        calls_before = g.calls
        assert _poll(lambda: g.calls > calls_before)
    finally:
        t.close()


def test_single_flight_under_concurrent_calls(tmp_path):
    t = _tracer(tmp_path)
    g = _FakeGenome()
    try:
        threads = [threading.Thread(target=t.replay_info, args=(g, _Cfg(), 0.0)) for _ in range(20)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert _poll(lambda: g.calls >= 1)
        time.sleep(SLEEP_S * 2)
        assert g.max_in_flight == 1
    finally:
        t.close()


def test_values_match_synchronous_computation(tmp_path):
    t = _tracer(tmp_path)
    g = _FakeGenome(delay=0)
    try:
        t.replay_info(g, _Cfg())
        assert _poll(lambda: t.replay_info(g, _Cfg()).get("doc_count") == 42)
        info = t.replay_info(g, _Cfg())
        old = tr._index_fingerprint(g)
        assert info["index_fingerprint"] == old["index_fingerprint"] == _expected()
        assert info["doc_count"] == old["doc_count"]
        assert info["git_sha"] == tr._git_sha()
        assert info["config_hash"] == tr.config_hash(_Cfg())
        assert set(info) >= {"index_fingerprint", "doc_count", "config_hash",
                             "cymatix_version", "git_sha"}
    finally:
        t.close()


def test_first_call_does_not_block_and_has_placeholder(tmp_path):
    t = _tracer(tmp_path)
    g = _FakeGenome()
    try:
        t0 = time.perf_counter()
        info = t.replay_info(g, _Cfg())
        assert (time.perf_counter() - t0) < 0.02
        assert info.get("index_fingerprint") in (None, _expected())
        assert info.get("doc_count") in (None, 42)
        assert "cymatix_version" in info
    finally:
        t.close()


def test_close_returns_promptly_during_refresh(tmp_path):
    t = _tracer(tmp_path)
    g = _FakeGenome(delay=2.0)
    t.replay_info(g, _Cfg())
    assert _poll(lambda: g.in_flight == 1)
    t0 = time.perf_counter()
    t.close()
    assert (time.perf_counter() - t0) < 0.5
