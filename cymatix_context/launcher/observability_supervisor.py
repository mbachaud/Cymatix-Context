"""ObservabilitySupervisor — owns the 5 native-binary subprocesses.

Lifecycle:
    1. Validate rendered configs exist (refuse otherwise — spec §7.1).
    2. Port pre-flight: skip-and-mark-external for any port already bound.
    3. Phase 1: parallel-spawn prometheus + tempo + loki.
    4. Wait for those three to bind their ports (30s timeout).
    5. Phase 2: spawn collector. Wait for ready.
    6. Phase 3: spawn grafana.
    7. Background loop: every 30s, HTTP-probe each service. Update status.
    8. shutdown(): SIGTERM-equivalent each child, wait 5s, escalate to KILL.

Cleanup guarantee (Windows): all spawned children join a Windows Job
Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE so an abnormal exit of
the launcher process kills the children at the OS level. POSIX uses
start_new_session for the same effect via process-group signalling.

Spec: docs/specs/2026-05-04-native-observability-sidecar-design.md §7.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .observability_health import (
    HEALTH_ENDPOINTS,
    SERVICE_PORTS,
    is_port_bound,
    wait_for_http_ok,
    wait_for_port,
)
from .observability_paths import (
    ALL_CONFIG_FILES,
    ALL_SERVICES as _MANIFEST_SERVICES,
    binary_path,
    configs_dir,
    logs_dir,
    service_state_dir,
    state_dir,
)

log = logging.getLogger("helix.launcher.observability")

_HEALTH_POLL_INTERVAL_S = 30.0
_TERM_GRACE_S = 5.0

# Spec §7.4 — rotate child stdout/stderr at 10 MiB, retain last 3 backups.
_LOG_ROTATE_MAX_BYTES = 10 * 1024 * 1024
_LOG_ROTATE_BACKUP_COUNT = 3


# ── Spawn-order ──────────────────────────────────────────────────────
# Phase 1 spawn together; phase 2 waits for them; collector spawns; wait;
# grafana last. Spec §7.3.
SPAWN_PHASES: List[List[str]] = [
    ["prometheus", "tempo", "loki"],
    ["collector"],
    ["grafana"],
]

# Spawn-order list (preserves the historical supervisor iteration order
# that matters for shutdown's `reversed(ALL_SERVICES)` semantics:
# grafana → collector → loki → tempo → prometheus). The set membership
# must equal the manifest in observability_paths.ALL_SERVICES — verified
# at import time so a manifest drift fails loudly instead of silently.
ALL_SERVICES: List[str] = [s for phase in SPAWN_PHASES for s in phase]
assert set(ALL_SERVICES) == set(_MANIFEST_SERVICES), (
    f"SPAWN_PHASES drift: {set(ALL_SERVICES)} vs manifest "
    f"{set(_MANIFEST_SERVICES)}"
)


# ── Status enum (kept as plain strings for tray-menu readability) ────
STATUS_GREEN = "green"        # alive + last health probe ok
STATUS_RED = "red"            # spawned but health probe failed
STATUS_EXTERNAL = "external"  # port bound by something else; we did not spawn
STATUS_PENDING = "pending"    # spawned, awaiting first health probe
STATUS_DOWN = "down"          # not yet started


class ObservabilityError(Exception):
    """Base."""


class ConfigsMissing(ObservabilityError):
    """A rendered config is absent — supervisor refuses to spawn."""


class BinariesMissing(ObservabilityError):
    """A native binary is absent — supervisor refuses to spawn."""


# ── Job Object (Windows) hooks. Real impls in win_job.py if present; ──
# we use a thin shim so test mocks can patch the names.

def _create_kill_on_close_job():
    """Create a Windows Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.

    Returns an opaque handle; raises on non-Windows or when pywin32 is
    missing. Test code patches this function to bypass the real syscall.
    """
    if sys.platform != "win32":
        raise RuntimeError("Job Objects are Windows-only")
    try:
        import win32job  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "pywin32 required for Job Object cleanup. "
            "Install with: pip install cymatix-context[launcher-tray]"
        ) from exc
    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation,
    )
    info["BasicLimitInformation"]["LimitFlags"] |= (
        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    win32job.SetInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation, info,
    )
    return job


def _assign_to_job(job, pid: int) -> None:
    """Attach pid to the Job Object. Test mocks patch this name."""
    if sys.platform != "win32":
        return
    import win32api  # type: ignore
    import win32con  # type: ignore
    import win32job  # type: ignore
    # Open by PID with rights needed for SetInformation.
    h = win32api.OpenProcess(
        win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
        False,
        pid,
    )
    try:
        win32job.AssignProcessToJobObject(job, h)
    finally:
        win32api.CloseHandle(h)


@dataclass
class _Service:
    name: str
    status: str = STATUS_DOWN
    proc: Optional[subprocess.Popen] = None
    log_path: Optional[Path] = None
    last_health_at: float = 0.0


class ObservabilitySupervisor:
    """Owns lifecycles of all five observability subprocesses."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._services: Dict[str, _Service] = {
            n: _Service(name=n) for n in ALL_SERVICES
        }
        self._job_handle = None  # Windows-only
        self._health_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # Per-service rotating log handlers + drainer threads. The parent
        # process owns the file handle (the child writes to a pipe), so
        # rotation works on Windows where renaming an open file held by
        # a child fails with ERROR_SHARING_VIOLATION. Spec §7.4.
        self._log_handlers: Dict[str, logging.handlers.RotatingFileHandler] = {}
        self._log_drainers: Dict[str, threading.Thread] = {}

    # ── public surface ────────────────────────────────────────────

    def status(self, service: str) -> str:
        with self._lock:
            return self._services[service].status

    def all_statuses(self) -> Dict[str, str]:
        with self._lock:
            return {s.name: s.status for s in self._services.values()}

    @property
    def _procs(self) -> Dict[str, Optional[subprocess.Popen]]:
        # Read-only convenience for tests / tray menu.
        return {s.name: s.proc for s in self._services.values()}

    # ── precondition checks ──────────────────────────────────────

    def _verify_configs(self) -> None:
        cfg = configs_dir()
        missing = [n for n in ALL_CONFIG_FILES if not (cfg / n).exists()]
        if missing:
            raise ConfigsMissing(
                f"Rendered configs missing: {missing}. "
                "Re-run scripts/install-native-observability.{ps1,sh}."
            )

    def _verify_binaries(self) -> None:
        missing = [s for s in ALL_SERVICES if not binary_path(s).exists()]
        if missing:
            raise BinariesMissing(
                f"Native binaries missing: {missing}. "
                "Re-run scripts/install-native-observability.{ps1,sh}."
            )

    # ── start sequence ───────────────────────────────────────────

    def start_all(self, *, phase_timeout: float = 30.0) -> None:
        """Run the spawn-order sequence per spec §7.3."""
        self._verify_configs()
        self._verify_binaries()

        # Create per-user state dirs (binaries write here).
        # Collector has no on-disk state, but creating an empty dir is harmless
        # and keeps the loop one line.
        state_dir(create=True)
        for s in ALL_SERVICES:
            service_state_dir(s, create=True)

        # Job Object on Windows.
        if sys.platform == "win32" and self._job_handle is None:
            try:
                self._job_handle = _create_kill_on_close_job()
            except Exception:
                log.warning(
                    "Could not create Job Object — falling back to atexit-only "
                    "cleanup (children may survive abnormal launcher exit)",
                    exc_info=True,
                )
                self._job_handle = None

        # Run each phase.
        for phase in SPAWN_PHASES:
            spawned: List[str] = []
            for svc in phase:
                if self._maybe_external(svc):
                    continue
                self._spawn(svc)
                spawned.append(svc)
            for svc in spawned:
                self._wait_phase_ready(svc, timeout=phase_timeout)

        self._start_health_loop()

    def _maybe_external(self, svc: str) -> bool:
        """If any of svc's ports are already bound, mark external + skip."""
        for port in SERVICE_PORTS[svc]:
            if is_port_bound("127.0.0.1", port):
                log.info(
                    "[%s] external instance detected on :%d — not spawning",
                    svc, port,
                )
                with self._lock:
                    self._services[svc].status = STATUS_EXTERNAL
                return True
        return False

    def _spawn(self, svc: str) -> None:
        cmd = self._command_for(svc)
        log.info("[%s] spawn %s", svc, " ".join(str(p) for p in cmd))

        log_path = logs_dir(create=True) / f"{svc}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        creationflags = 0
        start_new_session = False
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            start_new_session = True

        # Spec §7.4: stdout+stderr → pipe → reader-thread → RotatingFileHandler.
        # We can't reassign a running child's stdout fd from the parent, so
        # the parent owns the rotating handle and a daemon thread drains
        # the pipe. This is the only design that works on Windows where
        # renaming a file held by a child fails with ERROR_SHARING_VIOLATION.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=start_new_session,
            close_fds=True,
            bufsize=1,  # line-buffered (best-effort; binaries may override)
            env=self._service_env(svc),
        )

        # Attach to Job Object (Windows only).
        if sys.platform == "win32" and self._job_handle is not None:
            try:
                _assign_to_job(self._job_handle, proc.pid)
            except Exception:
                log.warning("[%s] Job Object assignment failed", svc, exc_info=True)

        with self._lock:
            self._services[svc].proc = proc
            self._services[svc].log_path = log_path
            self._services[svc].status = STATUS_PENDING

        # Spawn the drainer thread (daemon=True; dies with the process).
        self._start_log_drainer(svc, proc)

    # ── log rotation (spec §7.4) ─────────────────────────────────

    def _setup_log_handler(self, svc: str) -> logging.Logger:
        """Build (or fetch cached) per-service Logger backed by a
        RotatingFileHandler at logs_dir/<svc>.log, 10 MiB, 3 backups."""
        logger = logging.getLogger(f"helix.observability.{svc}")
        logger.setLevel(logging.INFO)
        logger.propagate = False  # don't bubble child stdout to root logger

        # If we already attached a handler for this svc, reuse it (restart path).
        if svc in self._log_handlers:
            return logger

        # Logger objects are global singletons; if a previous supervisor
        # instance left a stale RotatingFileHandler attached (test or
        # prod re-init), close + detach it before installing the fresh one.
        for stale in list(logger.handlers):
            try:
                logger.removeHandler(stale)
                stale.close()
            except Exception:
                pass

        log_path = logs_dir(create=True) / f"{svc}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=_LOG_ROTATE_MAX_BYTES,
            backupCount=_LOG_ROTATE_BACKUP_COUNT,
            encoding="utf-8",
        )
        # Child stdout is already framed text; don't add timestamps/levels.
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        self._log_handlers[svc] = handler
        return logger

    def _start_log_drainer(
        self, svc: str, proc: subprocess.Popen,
    ) -> threading.Thread:
        """Drain proc.stdout into the per-service rotating log.

        Returns the spawned daemon thread (tests join it; production code
        ignores it — it dies when the child closes its pipe).
        """
        logger = self._setup_log_handler(svc)

        def _drain() -> None:
            stdout = proc.stdout
            if stdout is None:
                return
            try:
                for raw in iter(stdout.readline, b""):
                    try:
                        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    except Exception:
                        # Defensive: never let a decode glitch kill the drainer.
                        line = repr(raw)
                    logger.info(line)
            except Exception:
                log.warning("[%s] log drainer error", svc, exc_info=True)
            finally:
                try:
                    stdout.close()
                except Exception:
                    pass

        thread = threading.Thread(
            target=_drain, name=f"obs-log-{svc}", daemon=True,
        )
        thread.start()
        self._log_drainers[svc] = thread
        return thread

    def _close_log_handler(self, svc: str) -> None:
        """Detach + close the per-service rotating handler.

        Called on shutdown and on restart so the file handle is released
        before respawn (Windows file-locking hygiene)."""
        handler = self._log_handlers.pop(svc, None)
        if handler is None:
            return
        logger = logging.getLogger(f"helix.observability.{svc}")
        try:
            logger.removeHandler(handler)
        except Exception:
            pass
        try:
            handler.close()
        except Exception:
            pass

    @staticmethod
    def _service_env(svc: str) -> Dict[str, str]:
        """Per-service environment for the spawned binary.

        Grafana (v0.7.0 dashboard-UX sweep): the sidecar is a local,
        single-operator surface — the tray/dashboard "open Grafana" links
        must land on a dashboard, not a login wall. Anonymous auth with
        org-role Admin is acceptable here because the listener is pinned
        to 127.0.0.1 (no LAN exposure). Operators who deliberately expose
        Grafana should configure real auth and may override any of these
        by exporting the GF_* variable themselves — existing values win.
        """
        env = dict(os.environ)
        if svc == "grafana":
            for k, v in {
                "GF_SERVER_HTTP_ADDR": "127.0.0.1",
                "GF_AUTH_ANONYMOUS_ENABLED": "true",
                "GF_AUTH_ANONYMOUS_ORG_ROLE": "Admin",
                "GF_AUTH_ANONYMOUS_ORG_NAME": "Main Org.",
                "GF_ANALYTICS_REPORTING_ENABLED": "false",
                "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
                "GF_NEWS_NEWS_FEED_ENABLED": "false",
            }.items():
                env.setdefault(k, v)
        return env

    def _command_for(self, svc: str) -> List[str]:
        bin_p = str(binary_path(svc))
        cfg = configs_dir()
        state = service_state_dir(svc, create=True)

        if svc == "collector":
            return [bin_p, f"--config={cfg / 'otel-collector-config.yaml'}"]
        if svc == "prometheus":
            return [
                bin_p,
                f"--config.file={cfg / 'prometheus.yml'}",
                # Local sidecar: never expose the TSDB beyond loopback.
                "--web.listen-address=127.0.0.1:9090",
                f"--storage.tsdb.path={state}",
                "--storage.tsdb.retention.time=14d",
                "--storage.tsdb.retention.size=4GB",
                "--web.enable-remote-write-receiver",
            ]
        if svc == "tempo":
            return [bin_p, f"-config.file={cfg / 'tempo.yaml'}"]
        if svc == "loki":
            return [bin_p, f"-config.file={cfg / 'loki-config.yaml'}"]
        if svc == "grafana":
            # Grafana finds its conf/provisioning via working dir.
            graf_home = binary_path("grafana").parent.parent  # tools/native-otel/grafana
            # Provisioning lives in repo deploy/otel/grafana/provisioning,
            # but datasources MUST be the rendered (localhost) variant.
            # The render module + bootstrap together copy:
            #   configs/datasources.yml ──► graf_home/conf/provisioning/datasources/datasources.yml
            #   deploy/otel/grafana/provisioning/dashboards/* ──► graf_home/conf/provisioning/dashboards/
            #   deploy/otel/grafana/dashboards/* ──► graf_home/conf/provisioning/dashboards-content/
            # This wiring lives in observability_render.render_all (Task 5)
            # under _wire_grafana_provisioning helper. See spec §6.3.
            return [
                bin_p,
                f"--homepath={graf_home}",
                f"--config={graf_home / 'conf' / 'defaults.ini'}",
            ]
        raise ValueError(svc)

    def _wait_phase_ready(self, svc: str, *, timeout: float) -> None:
        primary_port = SERVICE_PORTS[svc][0]
        ok = wait_for_port("127.0.0.1", primary_port, timeout=timeout)
        with self._lock:
            self._services[svc].status = STATUS_GREEN if ok else STATUS_RED
        if not ok:
            log.warning("[%s] did not bind :%d within %.1fs",
                        svc, primary_port, timeout)

    # ── health loop ──────────────────────────────────────────────

    def _start_health_loop(self) -> None:
        if self._health_thread is not None:
            return
        self._health_thread = threading.Thread(
            target=self._health_loop, name="obs-health", daemon=True,
        )
        self._health_thread.start()

    def _health_loop(self) -> None:
        while not self._stop_event.is_set():
            for svc in ALL_SERVICES:
                with self._lock:
                    status = self._services[svc].status
                if status == STATUS_EXTERNAL:
                    continue
                if status == STATUS_DOWN:
                    continue
                ok = wait_for_http_ok(
                    HEALTH_ENDPOINTS[svc], timeout=4.0,
                )
                with self._lock:
                    self._services[svc].status = (
                        STATUS_GREEN if ok else STATUS_RED
                    )
                    self._services[svc].last_health_at = time.time()
            self._stop_event.wait(timeout=_HEALTH_POLL_INTERVAL_S)

    # ── per-service control ──────────────────────────────────────

    def restart_service(self, svc: str) -> None:
        log.info("[%s] restart", svc)
        self._kill(svc)
        self._spawn(svc)
        self._wait_phase_ready(svc, timeout=30.0)

    def _kill(self, svc: str) -> None:
        with self._lock:
            proc = self._services[svc].proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except Exception:
            log.warning("[%s] terminate failed", svc, exc_info=True)
        deadline = time.monotonic() + _TERM_GRACE_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                log.warning("[%s] kill failed", svc, exc_info=True)
        with self._lock:
            self._services[svc].status = STATUS_DOWN
            self._services[svc].proc = None
        # Drainer thread will exit on its own once the child closes the
        # pipe; close + detach the rotating handler so the file is
        # released before any respawn.
        self._close_log_handler(svc)

    # ── shutdown ─────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Terminate all spawned children. Job Object would do this anyway
        on Windows when the parent dies, but this path runs on the clean
        exit + tray-Quit path so the OS has nothing to clean up."""
        log.info("ObservabilitySupervisor: shutdown")
        self._stop_event.set()
        for svc in reversed(ALL_SERVICES):
            self._kill(svc)
        # Releasing the Job Object handle triggers the kill-on-close
        # cleanup for any child we missed. Closing it is implicit when
        # the Python object is GC'd, but explicit is cheap insurance.
        if self._job_handle is not None and sys.platform == "win32":
            try:
                import win32api  # type: ignore
                win32api.CloseHandle(self._job_handle)
            except Exception:
                pass
            self._job_handle = None
