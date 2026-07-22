"""Issue #305: launcher endpoints must not block the event loop.

The db-select modal auto-dismisses by polling /api/state; when a slow
collector (sync httpx probes, 4s timeout each) ran inside an ``async def``
handler it stalled the whole loop, the poll hung, and the modal stuck
on-screen next to a "Running" header. Handlers that do blocking work must
be plain ``def`` so Starlette dispatches them to the AnyIO threadpool and
the loop keeps serving other requests meanwhile.

Each test starts a request against an endpoint whose backing call sleeps,
then measures how long a concurrent ``asyncio.sleep`` heartbeat takes on
the same loop. A blocked loop delays the heartbeat by ~BLOCK_S; a healthy
one resolves it on time.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("jinja2", reason="launcher extra not installed")
import httpx

from cymatix_context.launcher.app import create_app

BLOCK_S = 0.6          # how long the backing call stalls
HEARTBEAT_S = 0.05     # concurrent loop heartbeat
MAX_LAG_S = 0.3        # heartbeat must resolve well before BLOCK_S elapses


class SlowCollector:
    """collect() stalls like sync httpx probes against a busy backend."""

    def collect(self):
        time.sleep(BLOCK_S)
        return {"helix": {"running": False, "availability": "unavailable"}}


class SlowSupervisor:
    """Supervisor whose control calls stall (start waits on /stats)."""

    last_start_pending = False

    def adopt(self):
        return False

    def is_running(self):
        return True

    def owns_process(self):
        return False

    def start(self):
        time.sleep(BLOCK_S)
        return 1234

    def stop(self, reason=""):
        time.sleep(BLOCK_S)

    def restart(self, reason=""):
        time.sleep(BLOCK_S)
        return 1234


def _build_app():
    return create_app(
        store=SimpleNamespace(),
        supervisor=SlowSupervisor(),
        collector=SlowCollector(),
    )


def _measure_heartbeat_lag(app, method: str, path: str) -> float:
    """Fire `method path` at `app`; return the loop-heartbeat overrun."""

    async def scenario() -> float:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://launcher.test",
        ) as client:
            task = asyncio.create_task(client.request(method, path))
            t0 = time.perf_counter()
            # First loop yield hands control to the request task; if the
            # handler blocks inline, this sleep resolves ~BLOCK_S late.
            await asyncio.sleep(HEARTBEAT_S)
            lag = time.perf_counter() - t0 - HEARTBEAT_S
            resp = await task
            assert resp.status_code < 500, resp.text
            return lag

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/state"),
        ("GET", "/"),
        ("GET", "/api/state/panels"),
        ("POST", "/api/control/restart"),
    ],
)
def test_slow_collector_or_supervisor_does_not_block_loop(method, path):
    lag = _measure_heartbeat_lag(_build_app(), method, path)
    assert lag < MAX_LAG_S, (
        f"{method} {path} blocked the event loop for ~{lag:.2f}s "
        f"(backing call stalls {BLOCK_S}s; heartbeat should be unaffected)"
    )


def test_slow_genome_scan_does_not_block_loop(monkeypatch):
    from cymatix_context.launcher import genome_registry

    def slow_discover():
        time.sleep(BLOCK_S)
        return []

    monkeypatch.setattr(genome_registry, "discover_genomes", slow_discover)
    monkeypatch.setattr(
        genome_registry, "active_genome_path", lambda: Path("genome.db"),
    )
    lag = _measure_heartbeat_lag(_build_app(), "GET", "/api/genomes")
    assert lag < MAX_LAG_S, (
        f"/api/genomes blocked the event loop for ~{lag:.2f}s"
    )
