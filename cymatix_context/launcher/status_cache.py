"""
Host status cache: one bounded refresh worker behind the launcher polls.

The dashboard asks for host readiness on every poll, through three routes
(`/`, `/api/state/panels`, `/api/state`). The underlying observation reads
native host configuration files and probes a loopback endpoint, so it is
far too slow to run once per request and far too slow to make a caller
wait for it.

This module holds the timing and failure semantics for that observation:

  * one admitted refresh at a time, shared by every caller (single flight);
  * a finite caller wait, after which the caller is served the previous
    observation marked stale, or `unavailable` when there is none;
  * a completion time TTL for freshness and a cooldown after a failure, so
    a broken observation is not retried on every poll;
  * a generation fence, so a retired execution context or a refresh that
    ran past the deadline can never publish into the current snapshot;
  * approved snapshots only. The cache stores what the projector returns
    and nothing else: no raw report, no error text, no context identity.

What it deliberately does not do: cancel a blocked read. The deadline
fences publication and marks the refresh `timed_out`; the worker slot
stays occupied until the read actually returns, and no replacement worker
and no queue is admitted while it is stuck. That bounds the work and the
waiting without pretending to bound a file or socket operation.

The projector and the reader are injected. The reader returns whatever the
caller's collector produces, the projector turns it into the approved
field allowlist or returns None for an invalid report; neither contract
lives here, so this module never imports the status collector and cannot
grow into a cache framework.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import math
import threading
import time
from concurrent.futures import Future
from datetime import datetime, timezone
from functools import partial
from collections.abc import Mapping
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("cymatix.launcher.status_cache")

# Engineering choices for a local panel behind a two second poll, not
# measured thresholds and not maintainer specified numbers.
STATUS_FRESH_TTL_S: float = 5.0
STATUS_FAILURE_COOLDOWN_S: float = 5.0
STATUS_CALLER_WAIT_S: float = 0.25
STATUS_REFRESH_DEADLINE_S: float = 25.0

# Fixed text. The panel says which machine the observation describes
# without naming the account, the home directory or the workspace path.
OBSERVATION_SCOPE: str = "Local launcher execution account and workspace"

# Shown whenever there is no approved guidance to show: on an unavailable
# observation, and as the fallback for guidance the projector does not
# recognise. Instructions only.
GENERIC_NEXT_ACTION: str = "Run cymatix-status in this workspace for the full local diagnosis."

FRESHNESS_STATES: tuple = ("fresh", "stale", "unavailable")
REFRESH_STATES: tuple = ("idle", "refreshing", "failed", "timed_out", "invalid")

# Every key the cache publishes. The collector adds `launcher` on top of
# this, from supervisor evidence that must not age with the host TTL.
SNAPSHOT_KEYS: tuple = (
    "observation_scope",
    "freshness",
    "observed_at",
    "last_success_at",
    "last_attempt_at",
    "refresh_state",
    "age_s",
    "fresh_for_s",
    "host",
    "server",
    "mcp",
    "skill",
    "configured_ready",
    "guided_ready",
    "next_action",
)


def utc_iso(epoch: Any) -> Optional[str]:
    """Render an epoch second count as a UTC timestamp, or None."""

    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        return None
    if not math.isfinite(float(epoch)):
        return None
    stamp = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _nonnegative(value: float) -> float:
    """A finite, nonnegative, rounded second count for serialisation."""

    if not math.isfinite(value):
        return 0.0
    return round(max(0.0, value), 3)


def _error_label(exc: BaseException) -> str:
    """The exception type name and nothing else.

    A refresh failure is diagnosed from a native host configuration file
    and a loopback probe, so its message and its traceback can carry a
    path, a URL or a token. The panel already drops all three; the log
    line has to drop them too, or the projection is the only clean copy.
    """

    return type(exc).__name__


def _spawn_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="cymatix-launcher-status", daemon=True).start()


def _approved_only(snapshot: Mapping) -> Dict[str, Any]:
    """Copy across the approved keys and drop everything else.

    The field allowlist lives in the projector, which is injected. This
    is the second lock on the same door: whatever a projector returns,
    the cache publishes `SNAPSHOT_KEYS` and no other key.
    """

    return {
        key: copy.deepcopy(snapshot[key]) for key in SNAPSHOT_KEYS if key in snapshot
    }


def unavailable_snapshot() -> Dict[str, Any]:
    """The snapshot shown when no approved observation exists at all."""

    return {
        "observation_scope": OBSERVATION_SCOPE,
        "freshness": "unavailable",
        "observed_at": None,
        "last_success_at": None,
        "last_attempt_at": None,
        "refresh_state": "idle",
        "age_s": None,
        "fresh_for_s": None,
        "host": None,
        "server": None,
        "mcp": None,
        "skill": None,
        "configured_ready": None,
        "guided_ready": None,
        "next_action": GENERIC_NEXT_ACTION,
    }


class StatusCache:
    """Single flight, bounded wait, TTL cache of one approved observation.

    `reader` produces the raw report; `projector` returns the approved
    projection of that report, or None when the report does not satisfy
    the schema. Only the projection is ever stored or returned.

    `context_factory` names the execution context the observation belongs
    to. Its value is hashed immediately and never serialised or logged; a
    changed context retires the stored evidence rather than inheriting it.
    """

    def __init__(
        self,
        *,
        reader: Callable[[], Any],
        projector: Callable[[Any], Optional[Dict[str, Any]]],
        context_factory: Optional[Callable[[], str]] = None,
        ttl_s: float = STATUS_FRESH_TTL_S,
        cooldown_s: float = STATUS_FAILURE_COOLDOWN_S,
        caller_wait_s: float = STATUS_CALLER_WAIT_S,
        deadline_s: float = STATUS_REFRESH_DEADLINE_S,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        spawn: Callable[[Callable[[], None]], None] = _spawn_thread,
    ) -> None:
        self._reader = reader
        self._projector = projector
        self._context_factory = context_factory or (lambda: "default")
        self.ttl_s = ttl_s
        self.cooldown_s = cooldown_s
        self.caller_wait_s = caller_wait_s
        self.deadline_s = deadline_s
        self._clock = clock
        self._wall_clock = wall_clock
        self._spawn = spawn

        self._lock = threading.Lock()
        self._generation = 0
        self._context: Optional[str] = None
        self._snapshot: Optional[Dict[str, Any]] = None
        self._observed_monotonic: Optional[float] = None
        self._observed_at: Optional[str] = None
        self._last_success_at: Optional[str] = None
        self._last_attempt_at: Optional[str] = None
        self._refresh_state: str = "idle"
        self._cooldown_until: Optional[float] = None
        self._inflight: Optional[Future] = None
        # Set when a worker is admitted and cleared only when it
        # settles, so a retired worker still reports as running.
        self._worker_started: Optional[float] = None
        self._worker_busy = False

    # -- public surface ------------------------------------------------

    def wall_now(self) -> Optional[str]:
        """Current UTC timestamp from this cache's clock, for the caller.

        The supervisor overlay stamps its own sample with this, so panel
        times all come from one clock and a test can fix them.
        """
        return utc_iso(self._wall_clock())

    def get(self) -> Dict[str, Any]:
        """Return the approved snapshot, refreshing it when it is due.

        Never raises for a failed observation and never waits longer than
        the caller budget. The returned mapping is a private deep copy.
        """
        context = self._digest(self._context_factory())
        waiter: Optional[Future] = None
        work: Optional[Callable[[], None]] = None
        generation = 0

        with self._lock:
            if context != self._context:
                self._retire_locked(context)
            now = self._clock()
            if self._is_fresh_locked(now):
                return self._render_locked(now)
            if self._inflight is not None:
                started = self._worker_started
                if started is None or now - started <= self.deadline_s:
                    waiter = self._inflight
            elif self._worker_busy:
                # A retired or overdue worker still holds the only slot.
                # Admitting a replacement here is exactly the unbounded
                # growth the single worker bound exists to prevent.
                pass
            elif self._cooldown_until is not None and now < self._cooldown_until:
                pass
            else:
                waiter = Future()
                generation = self._generation
                self._inflight = waiter
                self._worker_started = now
                self._worker_busy = True
                self._last_attempt_at = utc_iso(self._wall_clock())
                self._refresh_state = "refreshing"
                work = partial(self._run_refresh, waiter, generation, now)

        # Spawning and waiting happen outside the bookkeeping lock, so a
        # slow observation never blocks a poll for a different route.
        if work is not None:
            try:
                self._spawn(work)
            except BaseException as exc:
                # A worker that could not start still holds the only
                # slot. Release it here or the panel never refreshes
                # again.
                log.warning("Host status refresh could not start: %s", _error_label(exc))
                self._settle(waiter, generation, "failed", None)
                raise
        if waiter is not None:
            self._await(waiter)

        with self._lock:
            return self._render_locked(self._clock())

    def clear(self) -> None:
        """Retire the stored observation; in flight work is fenced out.

        The waiters on an in flight refresh are still resolved by its own
        worker, and that worker keeps the single slot until it exits.
        """
        with self._lock:
            self._retire_locked(self._context)

    # -- refresh -------------------------------------------------------

    def _run_refresh(self, future: Future, generation: int, started: float) -> None:
        # `started` is the admission instant, the same one the caller
        # facing deadline runs from. Measuring the fence from the
        # worker's own first instruction instead would put thread start
        # latency inside the fence and outside the display, so a refresh
        # the panel had already reported as timed out could still land.
        try:
            try:
                projected = self._projector(self._reader())
            except Exception as exc:
                log.warning("Host status refresh failed: %s", _error_label(exc))
                outcome, payload = "failed", None
            else:
                if isinstance(projected, dict):
                    outcome, payload = "ok", projected
                else:
                    log.warning("Host status refresh produced an invalid report")
                    outcome, payload = "invalid", None
            if outcome == "ok" and self._clock() - started > self.deadline_s:
                # Late result: the caller was told this refresh timed out,
                # so publishing it now would resurrect fenced evidence.
                outcome, payload = "timed_out", None
            self._settle(future, generation, outcome, payload)
        except BaseException:
            # A BaseException escaping the reader must still release the
            # worker slot and resolve every waiter. It is re raised, not
            # reported as a successful observation.
            self._settle(future, generation, "failed", None)
            raise

    def _settle(
        self,
        future: Future,
        generation: int,
        outcome: str,
        payload: Optional[Dict[str, Any]],
    ) -> None:
        rendered: Optional[Dict[str, Any]] = None
        with self._lock:
            self._worker_busy = False
            self._worker_started = None
            if self._inflight is future:
                self._inflight = None
            if generation == self._generation:
                now = self._clock()
                if outcome == "ok" and payload is not None:
                    stamp = utc_iso(self._wall_clock())
                    self._snapshot = copy.deepcopy(payload)
                    self._observed_monotonic = now
                    self._observed_at = stamp
                    self._last_success_at = stamp
                    self._refresh_state = "idle"
                    self._cooldown_until = None
                else:
                    self._refresh_state = outcome
                    self._cooldown_until = now + self.cooldown_s
                rendered = self._render_locked(now)
        if not future.done():
            future.set_result(rendered)

    def _await(self, future: Future) -> None:
        try:
            future.result(timeout=self.caller_wait_s)
        except Exception:
            # Includes the wait timeout. The caller is served whatever
            # evidence exists now; the refresh keeps running.
            pass

    # -- bookkeeping, all called with the lock held ---------------------

    def _retire_locked(self, context: Optional[str]) -> None:
        self._generation += 1
        self._context = context
        self._snapshot = None
        self._observed_monotonic = None
        self._observed_at = None
        self._last_success_at = None
        self._last_attempt_at = None
        self._refresh_state = "idle"
        self._cooldown_until = None
        # The worker itself is not retired here: it keeps the only slot
        # until it exits, and `_display_state_locked` keeps saying so.
        self._inflight = None

    def _is_fresh_locked(self, now: float) -> bool:
        if self._snapshot is None or self._observed_monotonic is None:
            return False
        return now - self._observed_monotonic < self.ttl_s

    def _render_locked(self, now: float) -> Dict[str, Any]:
        snapshot = unavailable_snapshot()
        if self._snapshot is not None and self._observed_monotonic is not None:
            age = now - self._observed_monotonic
            fresh = age < self.ttl_s
            snapshot.update(_approved_only(self._snapshot))
            snapshot["freshness"] = "fresh" if fresh else "stale"
            snapshot["age_s"] = _nonnegative(age)
            # A clock that jumped backwards must not invent extra
            # freshness: the lifetime is clamped to the TTL.
            snapshot["fresh_for_s"] = (
                min(_nonnegative(self.ttl_s - age), _nonnegative(self.ttl_s))
                if fresh
                else 0.0
            )
        # Fixed text, always. A projector cannot turn the scope line
        # into a path by returning one.
        snapshot["observation_scope"] = OBSERVATION_SCOPE
        snapshot["observed_at"] = self._observed_at
        snapshot["last_success_at"] = self._last_success_at
        snapshot["last_attempt_at"] = self._last_attempt_at
        snapshot["refresh_state"] = self._display_state_locked(now)
        return snapshot

    def _display_state_locked(self, now: float) -> str:
        # An admitted worker is reported whether or not its future is
        # still current. A `clear` or a context change retires the
        # evidence, not the worker, and the panel must not read `idle`
        # while the only refresh slot is occupied.
        if self._worker_busy:
            started = self._worker_started
            if started is not None and now - started > self.deadline_s:
                return "timed_out"
            return "refreshing"
        return self._refresh_state

    @staticmethod
    def _digest(context: Any) -> str:
        """Hash the context identity so it cannot leak through this object."""

        return hashlib.sha256(str(context).encode("utf-8", "replace")).hexdigest()
