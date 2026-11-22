"""Supervision of helper processes that plugins ask the host to run.

A plugin returns ``ServiceSpec`` objects from its ``services()`` hook. The
supervisor starts each one, restarts it with capped exponential backoff when it
exits, and stops them all on shutdown. Nothing here raises into the host: a
bad ``services()`` result disables only the offending plugin.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from cymatix_context.plugins.api import ServiceSpec
from cymatix_context.plugins.loader import LoadedPlugin, call_hook

log = logging.getLogger(__name__)

BACKOFF_SECONDS = (1, 2, 4, 8, 16, 32, 60)
STABLE_AFTER_S = 60.0
TICK_INTERVAL_S = 1.0


@dataclass
class _Service:
    plugin: str
    spec: ServiceSpec
    proc: Optional[Any] = None
    started_at: float = 0.0
    restarts: int = 0
    backoff_idx: int = 0
    next_restart_at: Optional[float] = None


def _spec_problem(specs: Any) -> Optional[str]:
    """None when ``specs`` is a valid list of ServiceSpec, else a reason."""
    if not isinstance(specs, list):
        return f"services() returned {type(specs).__name__}, expected list"
    for spec in specs:
        if not isinstance(spec, ServiceSpec):
            return f"services() item {spec!r} is not a ServiceSpec"
        if not isinstance(spec.name, str) or not spec.name:
            return "service name must be a non-empty str"
        argv = spec.argv
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(a, str) for a in argv)
        ):
            return f"service {spec.name!r}: argv must be a non-empty list of str"
        env = spec.env
        if env is not None and (
            not isinstance(env, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
        ):
            return f"service {spec.name!r}: env must be None or dict[str, str]"
    return None


class PluginServiceSupervisor:
    def __init__(
        self,
        plugins: List[LoadedPlugin],
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._plugins = plugins
        self._popen = popen
        self._clock = clock
        self._sleep = sleep
        self._services: List[_Service] = []
        self._lock = threading.RLock()
        self._stopped = False
        self._stop_event = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start_all(self) -> None:
        with self._lock:
            if self._stopped:
                return
            for lp in self._plugins:
                if lp.status != "loaded":
                    continue
                specs = call_hook(lp, "services", default=[])
                if lp.status != "loaded":  # the hook raised
                    continue
                problem = _spec_problem(specs)
                if problem:
                    log.warning("plugin %s disabled: %s", lp.name, problem)
                    lp.status = "disabled"
                    lp.reason = problem
                    continue
                for spec in specs:
                    svc = _Service(lp.name, spec)
                    self._services.append(svc)
                    self._spawn(svc)

    def tick(self) -> None:
        """One supervision pass: detect exits, restart those whose backoff elapsed."""
        with self._lock:
            if self._stopped:
                return
            now = self._clock()
            for svc in self._services:
                self._observe(svc, now)
                if svc.proc is None and svc.next_restart_at is not None and now >= svc.next_restart_at:
                    svc.restarts += 1
                    self._spawn(svc)

    def run_in_thread(self) -> threading.Thread:
        """Start the production 1 s supervision loop on a daemon thread."""

        def loop() -> None:
            while not self._stop_event.wait(TICK_INTERVAL_S):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - supervision must never die
                    log.warning("plugin service tick failed", exc_info=True)

        t = threading.Thread(target=loop, name="plugin-service-supervisor", daemon=True)
        t.start()
        return t

    def stop_all(self, timeout: float = 10.0) -> None:
        with self._lock:
            self._stopped = True
            self._stop_event.set()
            live = [s for s in self._services if s.proc is not None and s.proc.poll() is None]
            for svc in live:
                try:
                    svc.proc.terminate()
                except OSError:
                    log.warning("terminate failed for %s/%s", svc.plugin, svc.spec.name, exc_info=True)
            deadline = time.monotonic() + max(0.0, timeout)
            for svc in live:
                try:
                    svc.proc.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    log.warning("killing %s/%s after %.1fs", svc.plugin, svc.spec.name, timeout)
                    try:
                        svc.proc.kill()
                        svc.proc.wait(timeout=5.0)
                    except (OSError, subprocess.TimeoutExpired):
                        log.warning("kill failed for %s/%s", svc.plugin, svc.spec.name, exc_info=True)
            for svc in self._services:
                svc.proc = None
                svc.next_restart_at = None

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            now = self._clock()
            rows = []
            for svc in self._services:
                running = svc.proc is not None and svc.proc.poll() is None
                if running:
                    nxt = None
                elif svc.next_restart_at is None:
                    nxt = None
                else:
                    nxt = max(0.0, svc.next_restart_at - now)
                rows.append(
                    {
                        "plugin": svc.plugin,
                        "service": svc.spec.name,
                        "running": running,
                        "pid": svc.proc.pid if running else None,
                        "restarts": svc.restarts,
                        "next_restart_in_s": nxt,
                    }
                )
            return rows

    # -- internals ---------------------------------------------------------

    def _spawn(self, svc: _Service) -> None:
        env = dict(os.environ)
        if svc.spec.env:
            env.update(svc.spec.env)
        now = self._clock()
        try:
            svc.proc = self._popen(
                list(svc.spec.argv),
                env=env,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, ValueError) as exc:
            log.warning("plugin %s service %s failed to start: %r", svc.plugin, svc.spec.name, exc)
            svc.proc = None
            svc.started_at = now
            self._schedule_restart(svc, now, uptime=0.0)
            return
        svc.started_at = now
        svc.next_restart_at = None
        self._observe(svc, now)  # a process that dies at once is scheduled now

    def _observe(self, svc: _Service, now: float) -> None:
        proc = svc.proc
        if proc is None:
            return
        code = proc.poll()
        uptime = now - svc.started_at
        if code is None:
            if uptime >= STABLE_AFTER_S:
                svc.backoff_idx = 0
            return
        log.warning(
            "plugin %s service %s exited with %r after %.1fs",
            svc.plugin, svc.spec.name, code, uptime,
        )
        svc.proc = None
        self._schedule_restart(svc, now, uptime)

    def _schedule_restart(self, svc: _Service, now: float, uptime: float) -> None:
        if uptime >= STABLE_AFTER_S:
            svc.backoff_idx = 0
        delay = BACKOFF_SECONDS[min(svc.backoff_idx, len(BACKOFF_SECONDS) - 1)]
        svc.backoff_idx += 1
        svc.next_restart_at = now + delay


__all__ = ["PluginServiceSupervisor", "BACKOFF_SECONDS"]
