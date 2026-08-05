"""Encoder daemon transport — stdlib-only HTTP client (Fork 1 slice 1).

Companion to ``cymatix_context.encoder_daemon`` (Task 2, not yet built) and
the ``RemoteCodec`` classes that substitute at the dense/SPLADE/SEMA seams
(Task 3, not yet built). See
``docs/design/2026-08-05-fork1-slice1-contract.md`` for the binding API
contract this module is written against.

This module owns exactly three concerns, and nothing else:

1. URL resolution — ``active_url()`` resolves env ``CYMATIX_ENCODER_URL``
   (truthy, stripped; empty counts as unset) over the module-level
   ``configure()`` slot (set by the manager from
   ``cfg.encoder_daemon.url``) over ``""`` (off).
2. Transport — ``EncoderClient`` is a stdlib-``urllib`` HTTP client for the
   daemon's ``/encode/dense``, ``/encode/splade``, ``/encode/sema``,
   ``/encode/bundle``, and ``/ready`` endpoints. Every request carries an
   explicit ``timeout=``; JSON decode errors and transport failures raise
   ``EncoderClientError`` uniformly so callers (the Task 3 RemoteCodec
   classes) can catch one exception type.
3. Circuit breaker — a process-lifetime breaker (``record_failure`` /
   ``record_success`` / ``should_attempt``) so a daemon outage produces
   exactly one warning per process and callers fall back to in-process
   encoding instead of failing every query. ``reset_for_test()`` clears all
   three concerns' module state for test isolation.

Deliberately stdlib-only (urllib, threading, json, time, logging) — this
module must be importable, and constructible, without torch/transformers/
sentence_transformers ever loading in the client process. It never
constructs a real codec; that decision (and the in-process fallback) is
Task 3's job.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

log = logging.getLogger("cymatix.encoder_client")


class EncoderClientError(Exception):
    """Raised on transport failure or an unparsable/malformed daemon response."""


# ── Env-tunable knob constants — read at call time, never at import ──
#
# All four knobs are read fresh on every call (never cached at import) so a
# test's monkeypatch.setenv takes effect immediately and a long-lived daemon
# process can have its cooldown/timeout tuned without a restart.

_TIMEOUT_ENV = "CYMATIX_ENCODER_TIMEOUT_S"
_TIMEOUT_DEFAULT = 10.0

_READY_TIMEOUT_ENV = "CYMATIX_ENCODER_READY_TIMEOUT_S"
_READY_TIMEOUT_DEFAULT = 15.0

_RETRY_COOLDOWN_ENV = "CYMATIX_ENCODER_RETRY_COOLDOWN_S"
_RETRY_COOLDOWN_DEFAULT = 30.0

_BATCH_WINDOW_ENV = "CYMATIX_ENCODER_BATCH_WINDOW_MS"
_BATCH_WINDOW_DEFAULT = 8.0  # Consumed by the daemon (Task 2); parsed here so both sides share one helper.


def _env_float(name: str, default: float) -> float:
    """Read an env-tunable float knob; malformed/unset values fall back to default."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning("%s=%r is not a float; using default %s", name, raw, default)
        return default


def default_timeout_s() -> float:
    """CYMATIX_ENCODER_TIMEOUT_S — per-request transport timeout (default 10.0)."""
    return _env_float(_TIMEOUT_ENV, _TIMEOUT_DEFAULT)


def ready_timeout_s() -> float:
    """CYMATIX_ENCODER_READY_TIMEOUT_S — wait_ready budget (default 15.0)."""
    return _env_float(_READY_TIMEOUT_ENV, _READY_TIMEOUT_DEFAULT)


def retry_cooldown_s() -> float:
    """CYMATIX_ENCODER_RETRY_COOLDOWN_S — circuit-breaker reopen delay (default 30.0)."""
    return _env_float(_RETRY_COOLDOWN_ENV, _RETRY_COOLDOWN_DEFAULT)


def batch_window_ms() -> float:
    """CYMATIX_ENCODER_BATCH_WINDOW_MS — daemon micro-batch window (default 8.0)."""
    return _env_float(_BATCH_WINDOW_ENV, _BATCH_WINDOW_DEFAULT)


# ── URL resolution slot ────────────────────────────────────────────

_configured_url: str = ""


def configure(url: str) -> None:
    """Set the manager-provided URL slot (from cfg.encoder_daemon.url).

    Task 3 wires the caller: CymatixContextManager.__init__ calls this once
    at construction with ``config.encoder_daemon.url``. Stripped so a
    whitespace-only value (e.g. from a misconfigured env/toml round-trip)
    is normalized to "" here too — active_url()'s truthiness check on the
    configured slot must see "" for the "off" case, not a truthy
    whitespace string that would flip the daemon on unintentionally.
    """
    global _configured_url
    _configured_url = (url or "").strip()


def configured_url() -> str:
    """The URL last passed to configure(), or "" if never configured."""
    return _configured_url


def active_url() -> str:
    """Resolve the effective daemon URL: env > configured slot > "" (off).

    Truthiness check mirrors the config-layer CYMATIX_SERVER_UPSTREAM
    convention: an empty or whitespace-only CYMATIX_ENCODER_URL counts as
    unset, not an override to "". The env value is stripped here; the
    configured slot is already stripped by configure().
    """
    env_url = os.environ.get("CYMATIX_ENCODER_URL", "").strip()
    if env_url:
        return env_url
    return _configured_url


# ── Circuit breaker (process-lifetime, one warning on first open) ────


class _Circuit:
    """Process-lifetime circuit breaker with a warn-once-per-open policy."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open_until: float = 0.0
        self._warned: bool = False

    def should_attempt(self) -> bool:
        with self._lock:
            return time.monotonic() >= self._open_until

    def record_failure(self) -> None:
        cooldown = retry_cooldown_s()
        with self._lock:
            self._open_until = time.monotonic() + cooldown
            warn = not self._warned
            self._warned = True
        if warn:
            log.warning(
                "encoder daemon unreachable — falling back to in-process "
                "encoding (retrying every %.0fs)",
                cooldown,
            )

    def record_success(self) -> None:
        with self._lock:
            self._open_until = 0.0

    def reset(self) -> None:
        with self._lock:
            self._open_until = 0.0
            self._warned = False


_circuit = _Circuit()


def should_attempt() -> bool:
    """True iff the circuit is closed (or the cooldown has elapsed)."""
    return _circuit.should_attempt()


def record_failure() -> None:
    """Open the circuit for retry_cooldown_s(); warn once per process."""
    _circuit.record_failure()


def record_success() -> None:
    """Close the circuit immediately."""
    _circuit.record_success()


def reset_for_test() -> None:
    """Clear the configured URL slot, circuit state, and warn-once flag."""
    global _configured_url
    _configured_url = ""
    _circuit.reset()


# ── Transport ──────────────────────────────────────────────────────


class EncoderClient:
    """stdlib-urllib client for one encoder daemon base URL.

    Never imports torch/transformers/sentence_transformers; every request
    carries an explicit timeout. Transport failures and malformed/missing-
    key responses both raise ``EncoderClientError`` so callers can catch one
    exception type (per contract: RemoteCodec.encode must never raise once
    constructed — the RemoteCodec catches this and falls back in-process).
    """

    def __init__(self, url: str, timeout_s: float = 10.0) -> None:
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s

    # -- POST endpoints -------------------------------------------------

    def encode_dense(self, texts: List[str], task: str = "passage") -> List[List[float]]:
        """POST /encode/dense {texts, task} -> vectors (contract table)."""
        resp = self._post_json("/encode/dense", {"texts": texts, "task": task})
        return self._require(resp, "vectors", "/encode/dense")

    def encode_splade(self, texts: List[str], top_k: int = 128) -> List[Dict[str, float]]:
        """POST /encode/splade {texts, top_k} -> sparse (contract table)."""
        resp = self._post_json("/encode/splade", {"texts": texts, "top_k": top_k})
        return self._require(resp, "sparse", "/encode/splade")

    def encode_sema(self, texts: List[str]) -> List[List[float]]:
        """POST /encode/sema {texts} -> vectors (contract table)."""
        resp = self._post_json("/encode/sema", {"texts": texts})
        return self._require(resp, "vectors", "/encode/sema")

    def encode_bundle(self, text: str, task: str = "query", top_k: int = 128) -> Dict[str, Any]:
        """POST /encode/bundle {text, task, top_k} -> {dense, splade, sema}."""
        return self._post_json("/encode/bundle", {"text": text, "task": task, "top_k": top_k})

    # -- readiness --------------------------------------------------------

    def is_ready(self, timeout_s: Optional[float] = None) -> bool:
        """GET /ready — 200 -> True; 503 or connection failure -> False.

        Never raises: readiness is a routine poll, not an error condition,
        so failures degrade to False rather than EncoderClientError.
        """
        t = self.timeout_s if timeout_s is None else timeout_s
        req = urllib.request.Request(f"{self.url}/ready", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=t) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as exc:
            log.debug("encoder daemon /ready returned HTTP %s", exc.code)
            return False
        except Exception as exc:
            log.debug("encoder daemon /ready unreachable: %s", exc)
            return False

    def wait_ready(self, budget_s: float, poll_s: float = 0.5) -> bool:
        """Poll GET /ready until ready or *budget_s* elapses (no over-run)."""
        deadline = time.monotonic() + budget_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self.is_ready(timeout_s=min(self.timeout_s, remaining)):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(poll_s, remaining))

    # -- internals --------------------------------------------------------

    def _post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.url}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read()
        except Exception as exc:
            raise EncoderClientError(f"POST {path} failed: {exc}") from exc
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EncoderClientError(f"POST {path}: invalid JSON response: {exc}") from exc
        if not isinstance(parsed, dict):
            raise EncoderClientError(
                f"POST {path}: expected a JSON object, got {type(parsed).__name__}"
            )
        return parsed

    @staticmethod
    def _require(resp: Dict[str, Any], key: str, path: str) -> Any:
        try:
            return resp[key]
        except KeyError as exc:
            raise EncoderClientError(f"{path} response missing {key!r}: {resp!r}") from exc
