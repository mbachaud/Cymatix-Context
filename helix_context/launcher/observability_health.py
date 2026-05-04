"""Health probes for the observability subprocesses.

Two primitives:
    - wait_for_port(host, port, timeout): TCP-connect poll until bound or timeout
    - wait_for_http_ok(url, timeout): HTTP GET poll until 2xx or timeout

Used by the supervisor to gate spawn-order phases (port-bind ready)
and to drive the 30-second health-loop tray indicator (HTTP /-/healthy etc).

Per global preference: every HTTP call has an explicit timeout=.
"""

from __future__ import annotations

import logging
import socket
import time
from typing import Optional

import httpx

log = logging.getLogger("helix.launcher.observability_health")

_POLL_INTERVAL_S = 0.5


def wait_for_port(host: str, port: int, *, timeout: float = 30.0) -> bool:
    """TCP-connect poll until bound or timeout. Never raises.

    Honors the deadline tightly: socket-connect timeout and inter-poll sleep
    are both clamped to the remaining budget so the function returns within
    ~timeout seconds even when the target is unreachable.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        sock_timeout = min(0.5, remaining)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(sock_timeout)
                s.connect((host, port))
            return True
        except (ConnectionRefusedError, OSError):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # Sleep at most half of remaining so the deadline is honored
            # even on short timeouts where a full _POLL_INTERVAL_S would
            # overshoot.
            time.sleep(min(_POLL_INTERVAL_S, remaining * 0.5))


def is_port_bound(host: str, port: int) -> bool:
    """Single-shot variant — used for the port-collision pre-flight check."""
    return wait_for_port(host, port, timeout=0.2)


def _parse_host_port(url: str) -> Optional[tuple[str, int]]:
    """Best-effort (host, port) extraction for a quick TCP pre-check."""
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
        host = u.hostname
        if host is None:
            return None
        port = u.port
        if port is None:
            port = 443 if u.scheme == "https" else 80
        return (host, int(port))
    except Exception:
        return None


def wait_for_http_ok(
    url: str,
    *,
    timeout: float = 30.0,
    expect_status: int = 200,
) -> bool:
    """HTTP GET poll until expect_status or timeout. Never raises.

    Honors the deadline tightly: the per-request httpx timeout and the
    inter-poll sleep are both clamped to the remaining budget. A fast
    TCP pre-check skips httpx entirely when the port isn't bound — on
    Windows, httpx adds ~300ms of overhead to ConnectTimeout, which
    makes short overall deadlines hard to honor without it.
    """
    deadline = time.monotonic() + timeout
    last_error: Optional[str] = None
    host_port = _parse_host_port(url)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Fast TCP pre-check — if the port isn't accepting connections,
        # skip the httpx call (which on Windows can take ~300ms over and
        # above its own timeout to surface ConnectTimeout).
        if host_port is not None:
            tcp_budget = min(0.25, remaining)
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(tcp_budget)
                    s.connect(host_port)
            except (ConnectionRefusedError, OSError) as exc:
                last_error = f"tcp: {exc}"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(_POLL_INTERVAL_S, remaining * 0.5))
                continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        req_timeout = min(2.0, remaining)
        try:
            resp = httpx.get(url, timeout=req_timeout)
            if resp.status_code == expect_status:
                return True
            last_error = f"HTTP {resp.status_code}"
        except Exception as exc:
            last_error = str(exc)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Sleep at most half of remaining so the deadline is honored
        # even on short timeouts where a full _POLL_INTERVAL_S would
        # overshoot.
        time.sleep(min(_POLL_INTERVAL_S, remaining * 0.5))
    log.debug("wait_for_http_ok timeout: %s (last=%s)", url, last_error)
    return False


# ── Per-service health-endpoint registry ──────────────────────────────
# spec §7.7 — used by the supervisor's 30s polling loop.

HEALTH_ENDPOINTS: dict[str, str] = {
    "collector":  "http://localhost:13133/",            # health_check ext default
    "prometheus": "http://localhost:9090/-/healthy",
    "tempo":      "http://localhost:3200/ready",
    "loki":       "http://localhost:3100/ready",
    "grafana":    "http://localhost:3000/api/health",
}

# Ports a healthy instance binds. Used both for the spawn-order port-bind
# poll (§7.3) and the port-collision pre-flight check (§7.2).
SERVICE_PORTS: dict[str, list[int]] = {
    "collector":  [4317, 4318, 8889],
    "prometheus": [9090],
    "tempo":      [3200],
    "loki":       [3100],
    "grafana":    [3000],
}
