"""Tests for cymatix_context.backends.encoder_client.

Fork 1 slice 1, Task 1: URL resolution, stdlib-urllib transport, and a
process-lifetime circuit breaker for the shared BGE-M3/SPLADE/SEMA encoder
daemon. See docs/design/2026-08-05-fork1-slice1-contract.md for the binding
API contract. The RemoteCodec classes that CONSUME this module are Task 3 —
nothing here constructs a real codec or touches torch/transformers.

Non-live: no real model, no real subprocess. Real sockets are allowed only
via ephemeral-port stdlib http.server.HTTPServer fixtures in daemon threads.
"""

from __future__ import annotations

import io
import json
import logging
import socket
import threading
import time
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

# ── test isolation ──────────────────────────────────────────────────
#
# Every new test file touching the encoder seams clears the process-lifetime
# singletons other suites populate (see tests/conftest.py::_stub_dense_codec
# and seam notes §7c) so this file's assertions don't depend on run order,
# and resets encoder_client's own module state so "off" tests stay
# deterministic even on a dev machine that exports CYMATIX_ENCODER_URL.


@pytest.fixture(autouse=True)
def _pristine_encoder_client(monkeypatch):
    from cymatix_context.backends import bgem3_codec, splade_backend, encoder_client

    bgem3_codec._GLOBAL_CODECS.clear()
    monkeypatch.setattr(splade_backend, "_model", None, raising=False)
    monkeypatch.setattr(splade_backend, "_tokenizer", None, raising=False)
    monkeypatch.setattr(splade_backend, "_device", None, raising=False)
    monkeypatch.delenv("CYMATIX_ENCODER_URL", raising=False)
    encoder_client.reset_for_test()
    yield
    encoder_client.reset_for_test()


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── canned-JSON daemon fixture (endpoint-shape + is_ready) ────────────


class _CannedHandler(BaseHTTPRequestHandler):
    """Serves canned JSON for /encode/* + /encode/bundle and /ready.

    Class attributes are test-controlled state: ``ready`` flips /ready
    between 200/503; ``received`` records (path, parsed-body) tuples for
    request-shape assertions.
    """

    ready = True
    received: list = []
    responses = {
        "/encode/dense": {"vectors": [[0.1, 0.2, 0.3]], "dim": 3, "model": "fake-dense"},
        "/encode/splade": {"sparse": [{"foo": 0.5, "bar": 0.25}]},
        "/encode/sema": {"vectors": [[0.1] * 20]},
        "/encode/bundle": {
            "dense": [[0.1, 0.2, 0.3]],
            "splade": {"foo": 0.5},
            "sema": [0.1] * 20,
        },
    }

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"_raw": raw.decode("utf-8", errors="replace")}
        type(self).received.append((self.path, body))
        payload = self.responses.get(self.path)
        if payload is None:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path != "/ready":
            self.send_response(404)
            self.end_headers()
            return
        if type(self).ready:
            body = json.dumps({"ready": True, "spawn_token": "tok", "probe_ms": 1.0}).encode()
            self.send_response(200)
        else:
            body = json.dumps({"ready": False, "state": "loading"}).encode()
            self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a, **kw):
        pass


class _MalformedHandler(BaseHTTPRequestHandler):
    """Returns 200 with a body that isn't valid JSON."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        data = b"not json{{{"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a, **kw):
        pass


class _EmptyObjectHandler(BaseHTTPRequestHandler):
    """Returns 200 with a valid but empty JSON object (missing expected keys)."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        data = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a, **kw):
        pass


def _start_server(handler_cls) -> tuple:
    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port


@pytest.fixture
def daemon_server():
    _CannedHandler.ready = True
    _CannedHandler.received = []
    srv, port = _start_server(_CannedHandler)
    yield port
    srv.shutdown()


@pytest.fixture
def malformed_server():
    srv, port = _start_server(_MalformedHandler)
    yield port
    srv.shutdown()


@pytest.fixture
def empty_object_server():
    srv, port = _start_server(_EmptyObjectHandler)
    yield port
    srv.shutdown()


# ── active_url() precedence ────────────────────────────────────────


def test_active_url_default_is_empty():
    from cymatix_context.backends import encoder_client
    assert encoder_client.active_url() == ""


def test_configured_url_default_is_empty():
    from cymatix_context.backends import encoder_client
    assert encoder_client.configured_url() == ""


def test_configure_sets_configured_and_active_url():
    from cymatix_context.backends import encoder_client
    encoder_client.configure("http://127.0.0.1:9999")
    assert encoder_client.configured_url() == "http://127.0.0.1:9999"
    assert encoder_client.active_url() == "http://127.0.0.1:9999"


def test_active_url_env_wins_over_configured(monkeypatch):
    from cymatix_context.backends import encoder_client
    encoder_client.configure("http://configured:1")
    monkeypatch.setenv("CYMATIX_ENCODER_URL", "http://env:2")
    assert encoder_client.active_url() == "http://env:2"


def test_active_url_empty_env_counts_as_unset(monkeypatch):
    from cymatix_context.backends import encoder_client
    encoder_client.configure("http://configured:1")
    monkeypatch.setenv("CYMATIX_ENCODER_URL", "")
    assert encoder_client.active_url() == "http://configured:1"


def test_active_url_env_value_is_stripped(monkeypatch):
    from cymatix_context.backends import encoder_client
    monkeypatch.setenv("CYMATIX_ENCODER_URL", "  http://env:2  ")
    assert encoder_client.active_url() == "http://env:2"


def test_active_url_whitespace_only_env_counts_as_unset(monkeypatch):
    from cymatix_context.backends import encoder_client
    encoder_client.configure("http://configured:1")
    monkeypatch.setenv("CYMATIX_ENCODER_URL", "   ")
    assert encoder_client.active_url() == "http://configured:1"


def test_configure_whitespace_only_url_normalizes_to_off():
    """Regression: configure("   ") must leave the daemon OFF, not ON.

    Once Task 3 wires configure(cfg.encoder_daemon.url), a whitespace-only
    value reaching this slot (e.g. a stray env/toml round-trip) must not
    make active_url() return a truthy garbage string — that would flip
    every RemoteCodec seam on with no real daemon behind it, violating
    "off means untouched".
    """
    from cymatix_context.backends import encoder_client
    encoder_client.configure("   ")
    assert encoder_client.configured_url() == ""
    assert encoder_client.active_url() == ""


def test_configure_strips_surrounding_whitespace_from_real_url():
    from cymatix_context.backends import encoder_client
    encoder_client.configure("  http://configured:1  ")
    assert encoder_client.configured_url() == "http://configured:1"
    assert encoder_client.active_url() == "http://configured:1"


def test_reset_for_test_clears_configured_slot():
    from cymatix_context.backends import encoder_client
    encoder_client.configure("http://x:1")
    encoder_client.reset_for_test()
    assert encoder_client.configured_url() == ""
    assert encoder_client.active_url() == ""


# ── env-tunable knob constants ──────────────────────────────────────


def test_default_timeout_s_default_is_10():
    from cymatix_context.backends import encoder_client
    assert encoder_client.default_timeout_s() == 10.0


def test_ready_timeout_s_default_is_15():
    from cymatix_context.backends import encoder_client
    assert encoder_client.ready_timeout_s() == 15.0


def test_retry_cooldown_s_default_is_30():
    from cymatix_context.backends import encoder_client
    assert encoder_client.retry_cooldown_s() == 30.0


def test_batch_window_ms_default_is_8():
    from cymatix_context.backends import encoder_client
    assert encoder_client.batch_window_ms() == 8.0


@pytest.mark.parametrize(
    "env_name,accessor_name,expected_default",
    [
        ("CYMATIX_ENCODER_TIMEOUT_S", "default_timeout_s", 10.0),
        ("CYMATIX_ENCODER_READY_TIMEOUT_S", "ready_timeout_s", 15.0),
        ("CYMATIX_ENCODER_RETRY_COOLDOWN_S", "retry_cooldown_s", 30.0),
        ("CYMATIX_ENCODER_BATCH_WINDOW_MS", "batch_window_ms", 8.0),
    ],
)
def test_env_knob_is_tunable_at_call_time(monkeypatch, env_name, accessor_name, expected_default):
    from cymatix_context.backends import encoder_client
    accessor = getattr(encoder_client, accessor_name)
    assert accessor() == expected_default
    monkeypatch.setenv(env_name, "42.5")
    assert accessor() == 42.5
    monkeypatch.delenv(env_name, raising=False)
    assert accessor() == expected_default


def test_env_knob_malformed_value_falls_back_with_warning(monkeypatch, caplog):
    from cymatix_context.backends import encoder_client
    monkeypatch.setenv("CYMATIX_ENCODER_TIMEOUT_S", "not-a-number")
    with caplog.at_level(logging.WARNING, logger="cymatix.encoder_client"):
        val = encoder_client.default_timeout_s()
    assert val == 10.0
    assert any("not-a-number" in r.message for r in caplog.records)


# ── circuit breaker ──────────────────────────────────────────────────


def test_circuit_starts_closed():
    from cymatix_context.backends import encoder_client
    assert encoder_client.should_attempt() is True


def test_circuit_opens_after_failure_and_blocks(monkeypatch):
    from cymatix_context.backends import encoder_client
    clock = [1_000.0]
    monkeypatch.setattr(encoder_client.time, "monotonic", lambda: clock[0])
    encoder_client.record_failure()
    assert encoder_client.should_attempt() is False


def test_circuit_reopens_after_cooldown(monkeypatch):
    from cymatix_context.backends import encoder_client
    clock = [1_000.0]
    monkeypatch.setattr(encoder_client.time, "monotonic", lambda: clock[0])
    encoder_client.record_failure()
    assert encoder_client.should_attempt() is False
    clock[0] += encoder_client.retry_cooldown_s()
    assert encoder_client.should_attempt() is True


def test_circuit_cooldown_is_env_tunable(monkeypatch):
    from cymatix_context.backends import encoder_client
    clock = [1_000.0]
    monkeypatch.setattr(encoder_client.time, "monotonic", lambda: clock[0])
    monkeypatch.setenv("CYMATIX_ENCODER_RETRY_COOLDOWN_S", "5")
    encoder_client.record_failure()
    clock[0] += 5.0
    assert encoder_client.should_attempt() is True


def test_circuit_record_success_closes_immediately(monkeypatch):
    from cymatix_context.backends import encoder_client
    clock = [1_000.0]
    monkeypatch.setattr(encoder_client.time, "monotonic", lambda: clock[0])
    encoder_client.record_failure()
    assert encoder_client.should_attempt() is False
    encoder_client.record_success()
    assert encoder_client.should_attempt() is True


def test_circuit_warns_exactly_once_across_two_failures(monkeypatch, caplog):
    from cymatix_context.backends import encoder_client
    clock = [1_000.0]
    monkeypatch.setattr(encoder_client.time, "monotonic", lambda: clock[0])
    with caplog.at_level(logging.WARNING, logger="cymatix.encoder_client"):
        encoder_client.record_failure()
        clock[0] += 1.0
        encoder_client.record_failure()
    warnings = [r for r in caplog.records if "unreachable" in r.message]
    assert len(warnings) == 1, f"expected exactly one warn-once record; got {[r.message for r in caplog.records]}"


def test_reset_for_test_clears_warn_once_flag(monkeypatch, caplog):
    from cymatix_context.backends import encoder_client
    clock = [1_000.0]
    monkeypatch.setattr(encoder_client.time, "monotonic", lambda: clock[0])
    encoder_client.record_failure()
    encoder_client.reset_for_test()
    caplog.clear()  # discard the pre-reset warning; only the post-reset one counts here
    with caplog.at_level(logging.WARNING, logger="cymatix.encoder_client"):
        encoder_client.record_failure()
    warnings = [r for r in caplog.records if "unreachable" in r.message]
    assert len(warnings) == 1


def test_reset_for_test_closes_circuit():
    from cymatix_context.backends import encoder_client
    encoder_client.record_failure()
    assert encoder_client.should_attempt() is False
    encoder_client.reset_for_test()
    assert encoder_client.should_attempt() is True


# ── EncoderClient transport: endpoint shape against a real socket ────


def test_encode_dense_posts_texts_and_task_and_parses_vectors(daemon_server):
    from cymatix_context.backends.encoder_client import EncoderClient
    client = EncoderClient(f"http://127.0.0.1:{daemon_server}")
    vectors = client.encode_dense(["hello world"], task="query")
    assert vectors == [[0.1, 0.2, 0.3]]
    path, body = _CannedHandler.received[-1]
    assert path == "/encode/dense"
    assert body == {"texts": ["hello world"], "task": "query"}


def test_encode_splade_posts_texts_and_top_k_and_parses_sparse(daemon_server):
    from cymatix_context.backends.encoder_client import EncoderClient
    client = EncoderClient(f"http://127.0.0.1:{daemon_server}")
    sparse = client.encode_splade(["hello world"], top_k=64)
    assert sparse == [{"foo": 0.5, "bar": 0.25}]
    path, body = _CannedHandler.received[-1]
    assert path == "/encode/splade"
    assert body == {"texts": ["hello world"], "top_k": 64}


def test_encode_sema_posts_texts_and_parses_vectors(daemon_server):
    from cymatix_context.backends.encoder_client import EncoderClient
    client = EncoderClient(f"http://127.0.0.1:{daemon_server}")
    vectors = client.encode_sema(["hello world"])
    assert vectors == [[0.1] * 20]
    path, body = _CannedHandler.received[-1]
    assert path == "/encode/sema"
    assert body == {"texts": ["hello world"]}


def test_encode_bundle_posts_text_task_top_k_and_parses_whole_dict(daemon_server):
    from cymatix_context.backends.encoder_client import EncoderClient
    client = EncoderClient(f"http://127.0.0.1:{daemon_server}")
    bundle = client.encode_bundle("hello world", task="query", top_k=64)
    assert bundle == {
        "dense": [[0.1, 0.2, 0.3]],
        "splade": {"foo": 0.5},
        "sema": [0.1] * 20,
    }
    path, body = _CannedHandler.received[-1]
    assert path == "/encode/bundle"
    assert body == {"text": "hello world", "task": "query", "top_k": 64}


def test_encode_bundle_default_task_and_top_k(daemon_server):
    from cymatix_context.backends.encoder_client import EncoderClient
    client = EncoderClient(f"http://127.0.0.1:{daemon_server}")
    client.encode_bundle("hello world")
    path, body = _CannedHandler.received[-1]
    assert body == {"text": "hello world", "task": "query", "top_k": 128}


# ── EncoderClient transport: error handling ───────────────────────────


def test_encode_dense_raises_encoder_client_error_on_malformed_json(malformed_server):
    from cymatix_context.backends.encoder_client import EncoderClient, EncoderClientError
    client = EncoderClient(f"http://127.0.0.1:{malformed_server}")
    with pytest.raises(EncoderClientError):
        client.encode_dense(["hello"])


def test_encode_dense_raises_encoder_client_error_on_missing_key(empty_object_server):
    from cymatix_context.backends.encoder_client import EncoderClient, EncoderClientError
    client = EncoderClient(f"http://127.0.0.1:{empty_object_server}")
    with pytest.raises(EncoderClientError):
        client.encode_dense(["hello"])


def test_encode_dense_raises_encoder_client_error_when_unreachable():
    from cymatix_context.backends.encoder_client import EncoderClient, EncoderClientError
    port = _free_port()  # nothing bound
    client = EncoderClient(f"http://127.0.0.1:{port}", timeout_s=0.4)
    with pytest.raises(EncoderClientError):
        client.encode_dense(["hello"])


# ── EncoderClient.is_ready / wait_ready ────────────────────────────────


def test_is_ready_true_when_daemon_reports_ready(daemon_server):
    from cymatix_context.backends.encoder_client import EncoderClient
    client = EncoderClient(f"http://127.0.0.1:{daemon_server}")
    assert client.is_ready(timeout_s=2.0) is True


def test_is_ready_false_when_daemon_reports_not_ready(daemon_server):
    from cymatix_context.backends.encoder_client import EncoderClient
    _CannedHandler.ready = False
    client = EncoderClient(f"http://127.0.0.1:{daemon_server}")
    assert client.is_ready(timeout_s=2.0) is False


def test_is_ready_false_when_unreachable_within_deadline():
    from cymatix_context.backends.encoder_client import EncoderClient
    port = _free_port()  # nothing bound
    client = EncoderClient(f"http://127.0.0.1:{port}")
    t0 = time.monotonic()
    assert client.is_ready(timeout_s=0.4) is False
    elapsed = time.monotonic() - t0
    assert elapsed < 0.4 * 1.5, "is_ready must honor its timeout deadline"


def test_wait_ready_true_when_already_ready(daemon_server):
    from cymatix_context.backends.encoder_client import EncoderClient
    client = EncoderClient(f"http://127.0.0.1:{daemon_server}", timeout_s=1.0)
    assert client.wait_ready(budget_s=2.0, poll_s=0.1) is True


def test_wait_ready_false_within_budget_when_unreachable():
    from cymatix_context.backends.encoder_client import EncoderClient
    port = _free_port()  # nothing bound
    client = EncoderClient(f"http://127.0.0.1:{port}", timeout_s=0.2)
    t0 = time.monotonic()
    assert client.wait_ready(budget_s=0.5, poll_s=0.1) is False
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5 * 1.5, "wait_ready must honor its budget deadline"


# ── transport-level: every request carries an explicit timeout ────────


def test_every_request_carries_explicit_timeout(monkeypatch):
    """No hanging requests — every urlopen call must pass timeout=<not None>."""
    from cymatix_context.backends.encoder_client import EncoderClient

    calls = []

    class _Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(req, timeout=None):
        assert timeout is not None, "no hanging requests — timeout required"
        calls.append((req.full_url, timeout))
        if req.full_url.endswith("/ready"):
            return _Resp(json.dumps({"ready": True}).encode())
        return _Resp(json.dumps({
            "vectors": [[0.1]], "sparse": [{}], "dense": [], "splade": {}, "sema": [],
        }).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client = EncoderClient("http://127.0.0.1:1", timeout_s=3.0)
    client.encode_dense(["a"])
    client.encode_splade(["a"])
    client.encode_sema(["a"])
    client.encode_bundle("a")
    client.is_ready()

    assert len(calls) == 5
    assert all(t is not None for _, t in calls)
