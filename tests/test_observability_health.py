"""Tests for helix_context.launcher.observability_health."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _bind_port_in_thread(port: int) -> threading.Event:
    """Bind 127.0.0.1:port in a daemon thread; return an Event the caller
    sets to release the port."""
    bound = threading.Event()
    release = threading.Event()

    def _worker():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        bound.set()
        release.wait(timeout=10)
        s.close()

    threading.Thread(target=_worker, daemon=True).start()
    bound.wait(timeout=2)
    return release


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ok":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a, **kw):
        pass


@pytest.fixture
def http_server():
    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), _OkHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port
    srv.shutdown()


# ── port-bind poll ────────────────────────────────────────────────────

def test_wait_for_port_returns_true_when_bound():
    from helix_context.launcher.observability_health import wait_for_port
    port = _free_port()
    release = _bind_port_in_thread(port)
    try:
        assert wait_for_port("127.0.0.1", port, timeout=2.0) is True
    finally:
        release.set()


def test_wait_for_port_returns_false_on_timeout():
    from helix_context.launcher.observability_health import wait_for_port
    port = _free_port()  # nothing bound
    t0 = time.monotonic()
    assert wait_for_port("127.0.0.1", port, timeout=0.4) is False
    elapsed = time.monotonic() - t0
    # Within 1.5x the timeout — proves we honor the deadline.
    assert elapsed < 0.4 * 1.5


# ── HTTP poll ─────────────────────────────────────────────────────────

def test_wait_for_http_ok_returns_true_on_200(http_server):
    from helix_context.launcher.observability_health import wait_for_http_ok
    assert wait_for_http_ok(
        f"http://127.0.0.1:{http_server}/ok", timeout=3.0
    ) is True


def test_wait_for_http_ok_returns_false_on_404(http_server):
    from helix_context.launcher.observability_health import wait_for_http_ok
    assert wait_for_http_ok(
        f"http://127.0.0.1:{http_server}/missing", timeout=0.4
    ) is False


def test_wait_for_http_ok_returns_false_on_unreachable():
    from helix_context.launcher.observability_health import wait_for_http_ok
    port = _free_port()  # nobody bound
    t0 = time.monotonic()
    assert wait_for_http_ok(
        f"http://127.0.0.1:{port}/anything", timeout=0.4
    ) is False
    assert time.monotonic() - t0 < 0.4 * 1.5
