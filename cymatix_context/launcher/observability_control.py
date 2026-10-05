"""Start and stop the observability sidecar on demand.

The tray launcher starts the stack at boot. The desktop app's headless
launcher does not, so its dashboard offers an Enable / Stop button backed by
this controller.

Enabling follows the same order the tray uses at boot: bring the stack up,
and only then point the backend at it (``CYMATIX_OTEL_ENABLED``) by
restarting the backend. A backend that dials a collector which is not up
wedges its exporter, so a failed start never restarts the backend.

The controller does not touch the stack directly: the launcher passes in
the build/start/restart callables (and the install/opt-out checks), which
keeps this module free of the tray's wiring and testable with fakes.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("cymatix.launcher.observability_control")

OTEL_ENV = "CYMATIX_OTEL_ENABLED"

STATUS_UNAVAILABLE = "unavailable"      # operator opted out (CYMATIX_OBSERVABILITY=0)
STATUS_NOT_INSTALLED = "not_installed"  # binaries / configs missing
STATUS_STOPPED = "stopped"
STATUS_STARTING = "starting"
STATUS_RUNNING = "running"
STATUS_ERROR = "error"


class NotInstalled(Exception):
    """Enable was asked for but the stack cannot run on this machine."""


def _spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="cymatix-observability", daemon=True).start()


class ObservabilityControl:
    def __init__(
        self,
        *,
        build: Callable[[], Any],
        start: Callable[[Any], None],
        restart_backend: Callable[[], None],
        is_installed: Callable[[], bool],
        is_opted_out: Callable[[], bool],
        run_async: Callable[[Callable[[], None]], None] = _spawn,
    ) -> None:
        self._build = build
        self._start = start
        self._restart_backend = restart_backend
        self._is_installed = is_installed
        self._is_opted_out = is_opted_out
        self._run_async = run_async
        self._lock = threading.Lock()
        self._sup: Optional[Any] = None
        self._status = STATUS_STOPPED
        self._error: Optional[str] = None
        self._exported_env = False

    # ── state ──────────────────────────────────────────────────────

    def _effective_status(self) -> str:
        if self._is_opted_out():
            return STATUS_UNAVAILABLE
        if self._status in (STATUS_STOPPED, STATUS_ERROR) and not self._is_installed():
            return STATUS_NOT_INSTALLED
        return self._status

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            status = self._effective_status()
            error = self._error
            sup = self._sup
        services = []
        if sup is not None:
            try:
                services = [{"name": n, "status": s}
                            for n, s in sorted(sup.all_statuses().items())]
            except Exception:
                log.debug("observability: status read failed", exc_info=True)
        return {"status": status, "error": error, "services": services}

    # ── actions ────────────────────────────────────────────────────

    def enable(self) -> None:
        with self._lock:
            status = self._effective_status()
            if status in (STATUS_UNAVAILABLE, STATUS_NOT_INSTALLED):
                raise NotInstalled(status)
            if status in (STATUS_STARTING, STATUS_RUNNING):
                return
            self._status = STATUS_STARTING
            self._error = None
        self._run_async(self._do_enable)

    def _do_enable(self) -> None:
        sup = None
        try:
            sup = self._build()
            if sup is None:
                raise RuntimeError("the observability stack could not be built")
            before = os.environ.get(OTEL_ENV, "").strip()
            self._start(sup)
            exported = not before and os.environ.get(OTEL_ENV, "").strip() == "1"
            # Only now that the stack is up does the backend learn about it.
            self._restart_backend()
            with self._lock:
                self._sup = sup
                self._exported_env = exported
                self._status = STATUS_RUNNING
        except Exception as exc:
            log.warning("observability enable failed", exc_info=True)
            if sup is not None:
                try:
                    sup.shutdown()
                except Exception:
                    log.debug("observability: cleanup after failed start", exc_info=True)
            with self._lock:
                self._sup = None
                self._status = STATUS_ERROR
                self._error = str(exc) or exc.__class__.__name__

    def disable(self) -> None:
        with self._lock:
            if self._sup is None:
                return
        self._run_async(self._do_disable)

    def _do_disable(self) -> None:
        with self._lock:
            sup, exported = self._sup, self._exported_env
            self._sup = None
            self._exported_env = False
            self._status = STATUS_STOPPED
            self._error = None
        try:
            if sup is not None:
                sup.shutdown()
        except Exception:
            log.warning("observability: shutdown failed", exc_info=True)
        if exported:
            os.environ.pop(OTEL_ENV, None)
        try:
            self._restart_backend()
        except Exception:
            log.warning("observability: backend restart after stop failed", exc_info=True)

    def shutdown(self) -> None:
        """Launcher exit: stop the stack, leave the backend alone."""
        with self._lock:
            sup, self._sup = self._sup, None
        if sup is not None:
            try:
                sup.shutdown()
            except Exception:
                log.warning("observability: shutdown failed", exc_info=True)
