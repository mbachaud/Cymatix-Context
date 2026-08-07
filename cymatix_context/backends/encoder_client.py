"""Encoder daemon transport — stdlib-only HTTP client (Fork 1 slice 1).

Companion to ``cymatix_context.encoder_daemon``. See
``docs/design/2026-08-05-fork1-slice1-contract.md`` for the binding API
contract this module is written against.

This module owns exactly four concerns, and nothing else:

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
   encoding instead of failing every query. ``reset_for_test()`` clears the
   URL slot and the breaker for test isolation.
4. RemoteCodec substitutes — ``RemoteBGEM3Codec`` (installed by
   ``bgem3_codec.get_shared_codec``) and ``RemoteSemaCodec`` (installed by
   ``CymatixContextManager.__init__``). Both are drop-in duck-types for the
   codec they replace and both degrade to an in-process codec when the
   daemon is unreachable. The third seam, SPLADE, needs no class — it
   branches inside ``splade_backend.encode`` / ``encode_batch``.

Deliberately stdlib-only at import (urllib, threading, json, time, logging)
— this module must be importable, and its classes constructible, without
torch/transformers/sentence_transformers ever loading in the client process.
The real codecs are imported lazily, inside a method, only on the fallback
path.
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
# 2026-08-05 dogfood A/B receipts: 8ms cost +4pp on the c=1 latency gate
# (three sequential seam RPCs pay the idle window each) with ZERO batching
# benefit at any measured concurrency — coalescing under load comes from
# queue backpressure while the worker encodes, not from the idle window.
# 2ms: c=1 median ratio 1.117 (vs 1.154-1.157 at 8ms, gate 1.15), c=2 qps
# +8.6%, c=4 qps +12.7% — strictly better on every axis measured.
_BATCH_WINDOW_DEFAULT = 2.0  # Consumed by the daemon (Task 2); parsed here so both sides share one helper.

_PER_ITEM_TIMEOUT_ENV = "CYMATIX_ENCODER_PER_ITEM_TIMEOUT_S"
_PER_ITEM_TIMEOUT_DEFAULT = 0.25


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
    """CYMATIX_ENCODER_BATCH_WINDOW_MS — daemon micro-batch window (default 2.0).

    2026-08-05 dogfood A/B receipt: 8ms cost +4pp on the c=1 latency gate
    with zero batching benefit at any concurrency; 2ms is strictly better
    on every measured axis (c=1 latency, c=2/c=4 qps).
    """
    return _env_float(_BATCH_WINDOW_ENV, _BATCH_WINDOW_DEFAULT)


def per_item_timeout_s() -> float:
    """CYMATIX_ENCODER_PER_ITEM_TIMEOUT_S — added per batch item (default 0.25)."""
    return _env_float(_PER_ITEM_TIMEOUT_ENV, _PER_ITEM_TIMEOUT_DEFAULT)


def batch_timeout_s(n: int) -> float:
    """Per-request timeout for a whole-batch POST of *n* texts (Finding 1).

    ``default_timeout_s() + n * per_item_timeout_s()``. A whole-batch
    ``/encode/dense`` (or splade/sema) POST sent under the flat 10s default
    times out client-side once the batch is big enough — at CPU dense rate
    ~18.6 enc/s (the slowest measured local backend, see the fork1-slice1
    contract's bars) that is roughly 185 texts. A client-side timeout mid
    batch dumps every worker onto the ~2GB in-process fallback and opens the
    shared circuit for every seam — exactly the memory blow-up the daemon
    exists to prevent. 0.25s/item is ~4.6x margin over that rate.

    Deliberately NOT used for single-item ``encode()`` calls, which keep the
    flat ``default_timeout_s()`` — only the three whole-batch POST paths
    (``RemoteBGEM3Codec.encode_batch``, ``RemoteSemaCodec.encode_batch``,
    ``splade_backend.encode_batch``'s remote branch) call this.
    """
    return default_timeout_s() + n * per_item_timeout_s()


# ── URL resolution slot ────────────────────────────────────────────

_configured_url: str = ""

#: True in the encoder daemon's OWN process. The daemon builds its codecs
#: through the very seams this module substitutes, so if an operator exports
#: CYMATIX_ENCODER_URL (the documented way to turn the daemon on) and then
#: starts the daemon from that same shell, the daemon would become a client
#: of itself: startup would pay the readiness budget polling its own
#: not-yet-bound port, and after the circuit cooldown its own /encode/splade
#: handler would POST to itself, recursively, until the threadpool starved.
#: The daemon's entry points set this flag before any model is constructed.
_daemon_process: bool = False


def mark_encoder_daemon_process() -> None:
    """Declare this process the encoder daemon — forces ``active_url()`` off.

    Called by both daemon entry points (``create_encoder_app`` and
    ``main``) before the registry loads, so the daemon's own dense / SPLADE /
    SEMA construction takes the in-process path no matter what the
    environment says.
    """
    global _daemon_process
    _daemon_process = True


def is_encoder_daemon_process() -> bool:
    """True once ``mark_encoder_daemon_process()`` has run in this process."""
    return _daemon_process


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

    Always "" inside the daemon's own process (see ``_daemon_process``) —
    the daemon must never route its own encodes back through HTTP.
    """
    if _daemon_process:
        return ""
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


# ── shared one-shot readiness gate ──────────────────────────────────
#
# Process-wide, not per-codec: the daemon is one process holding all three
# models, so "is it up yet?" is one question. If each seam polled on its own,
# whichever seam encoded first during a cold start (SEMA and SPLADE both run
# ahead of dense on the ingest path) would skip the gate, hammer a loading
# daemon, and open the SHARED circuit — after which dense never reaches its
# own gate and permanently materializes a ~2 GB in-process fallback even
# though the daemon is healthy 30 s later.

_ready_probed: bool = False
_ready_result: Optional[bool] = None
_ready_gate_lock = threading.Lock()


def ready_gate(client: "EncoderClient") -> bool:
    """Poll ``GET /ready`` once per process; every seam calls this first.

    The first caller pays the ``CYMATIX_ENCODER_READY_TIMEOUT_S`` budget and
    records a failure if the daemon is not ready, which opens the circuit for
    the cooldown. Threads that lose the lock race read that same outcome —
    never a hardcoded True, or a loser would fire a live RPC at a daemon the
    winner just confirmed cold, which is precisely what this gate exists to
    prevent.

    A not-ready outcome is "spent", not a permanent verdict: retry timing
    belongs to the circuit breaker, so the final answer for a failed probe is
    ``should_attempt()`` — False for the race losers (the winner just opened
    the circuit) and True once the cooldown has elapsed, which lets the retry
    go straight to the RPC with no second readiness poll. That is what keeps
    a slow-starting daemon from disabling remote encoding for the process
    lifetime.
    """
    global _ready_probed, _ready_result
    if _ready_probed and _ready_result:
        return True
    probed_now = False
    with _ready_gate_lock:
        if not _ready_probed:
            _ready_result = client.wait_ready(budget_s=ready_timeout_s())
            _ready_probed = True
            probed_now = True
        ok = bool(_ready_result)
    if probed_now and not ok:
        # Exactly one failure recorded per process for a cold daemon: later
        # callers must not keep pushing the cooldown window forward.
        record_failure()
    if ok:
        return True
    return should_attempt()


def ready_probed() -> bool:
    """Whether the one-shot readiness gate has already run in this process."""
    return _ready_probed


def reset_for_test() -> None:
    """Clear the URL slot, daemon flag, readiness gate, and circuit state."""
    global _configured_url, _daemon_process, _ready_probed, _ready_result
    _configured_url = ""
    _daemon_process = False
    _ready_probed = False
    _ready_result = None
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

    def encode_dense(
        self, texts: List[str], task: str = "passage", timeout_s: Optional[float] = None,
    ) -> List[List[float]]:
        """POST /encode/dense {texts, task} -> vectors (contract table).

        ``timeout_s`` overrides this client's constructor-time timeout for
        this one request — batch callers pass ``batch_timeout_s(len(texts))``
        (Finding 1); omitted, it falls back to ``self.timeout_s``.
        """
        resp = self._post_json(
            "/encode/dense", {"texts": texts, "task": task}, timeout_s=timeout_s,
        )
        return self._require(resp, "vectors", "/encode/dense")

    def encode_splade(
        self, texts: List[str], top_k: int = 128, timeout_s: Optional[float] = None,
    ) -> List[Dict[str, float]]:
        """POST /encode/splade {texts, top_k} -> sparse (contract table).

        See ``encode_dense`` for the ``timeout_s`` override semantics.
        """
        resp = self._post_json(
            "/encode/splade", {"texts": texts, "top_k": top_k}, timeout_s=timeout_s,
        )
        return self._require(resp, "sparse", "/encode/splade")

    def encode_sema(
        self, texts: List[str], timeout_s: Optional[float] = None,
    ) -> List[List[float]]:
        """POST /encode/sema {texts} -> vectors (contract table).

        See ``encode_dense`` for the ``timeout_s`` override semantics.
        """
        resp = self._post_json("/encode/sema", {"texts": texts}, timeout_s=timeout_s)
        return self._require(resp, "vectors", "/encode/sema")

    def encode_rerank(
        self, query: str, texts: List[str], timeout_s: Optional[float] = None,
    ) -> List[float]:
        """POST /encode/rerank {query, texts} -> scores (contract table).

        See ``encode_dense`` for the ``timeout_s`` override semantics.
        """
        resp = self._post_json(
            "/encode/rerank", {"query": query, "texts": texts}, timeout_s=timeout_s,
        )
        return self._require(resp, "scores", "/encode/rerank")

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

    def _post_json(
        self, path: str, payload: Dict[str, Any], timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        url = f"{self.url}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        t = self.timeout_s if timeout_s is None else timeout_s
        try:
            with urllib.request.urlopen(req, timeout=t) as resp:
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


# ── RemoteCodec substitutes ─────────────────────────────────────────
#
# Drop-in duck-types for BGEM3Codec and LazySemaCodec that encode through the
# daemon and degrade to an in-process codec when it is unreachable. Neither
# class imports torch / transformers / sentence_transformers at construction —
# the real codec is imported (and its weights loaded) only on the fallback
# path, so a healthy daemon means the client process never holds the models.


class RemoteBGEM3Codec:
    """``BGEM3Codec`` drop-in that encodes through the encoder daemon.

    Substituted inside ``bgem3_codec.get_shared_codec`` when a daemon URL is
    active, under the same ``(model_name, dim, device)`` cache key — so the
    process still holds exactly one instance per key and the store, the
    manager and the shard fan-out all share it.

    ``encode`` / ``encode_batch`` **never raise** once constructed:
    ``KnowledgeStore.query_genes_dense_recall`` calls ``encode`` with no
    try/except, so a daemon outage must degrade, never 500 the query path.
    Degradation order is: circuit closed? → ``/ready`` (first use only) → RPC
    → on ANY failure, record it and re-encode with a lazily-built in-process
    ``BGEM3Codec`` held on this instance. If even that fails (no
    FlagEmbedding, no sentence-transformers) the result is an empty vector —
    every consumer already treats a wrong-length vector as "no vector"
    (the query path warns and returns no dense hits; the ingest path stores
    NULL ``embedding_dense_v2`` for a later backfill).

    Device-mismatch byte drift between daemon vectors and fallback vectors is
    an accepted, documented caveat of a mid-run daemon outage (contract,
    "Client / RemoteCodec semantics").
    """

    def __init__(
        self,
        dim: int = 1024,
        device: str = "cpu",
        model_name: str = "BAAI/bge-m3",
        url: str = "",
    ) -> None:
        self.dim = dim
        self.model_name = model_name
        # Kept for parity with BGEM3Codec (cache-key component, and the device
        # the in-process fallback is built with).
        self._device = device
        self.url = url
        self._client = EncoderClient(url, timeout_s=default_timeout_s())
        self._fallback: Optional[Any] = None
        self._fallback_lock = threading.Lock()

    @property
    def _model(self) -> Any:
        """The fallback's loaded model, or None — the /admin/components probe.

        ``routes_admin`` infers dense residency from ``holder._model is not
        None``. A remote codec holds no weights, so this reports None until an
        outage forces the in-process fallback to load — which is exactly what
        the probe should say.
        """
        return getattr(self._fallback, "_model", None)

    # -- encode -----------------------------------------------------------

    def encode(self, text: str, task: str = "passage") -> List[float]:
        vectors = self._encode_remote([text], task)
        if vectors is not None:
            return vectors[0]
        try:
            return self._local().encode(text, task=task)
        except Exception:
            log.warning(
                "in-process dense fallback failed — returning an empty vector "
                "(no dense hits this turn / NULL embedding_dense_v2)",
                exc_info=True,
            )
            return []

    def encode_batch(self, texts: List[str], task: str = "passage") -> List[List[float]]:
        if not texts:
            # Matches BGEM3Codec: no model load, and here also no network and
            # no fallback construction.
            return []
        # Finding 1: a whole-batch POST under the flat default times out
        # client-side on large batches; scale the timeout with len(texts)
        # (batch_timeout_s) rather than the single-item flat default that
        # encode() uses.
        vectors = self._encode_remote(list(texts), task, timeout_s=batch_timeout_s(len(texts)))
        if vectors is not None:
            return vectors
        try:
            return self._local().encode_batch(list(texts), task=task)
        except Exception:
            log.warning(
                "in-process dense batch fallback failed — strands stored with "
                "NULL embedding_dense_v2 (backfill later)",
                exc_info=True,
            )
            return []

    def similarity(self, vec_a: List[float], vec_b: List[float]) -> float:
        """Pure numpy dot, exactly as BGEM3Codec.similarity — never a round trip."""
        import numpy as np

        a = np.array(vec_a, dtype=np.float32)
        b = np.array(vec_b, dtype=np.float32)
        return float(np.dot(a, b))

    # -- internals --------------------------------------------------------

    def _encode_remote(
        self, texts: List[str], task: str, timeout_s: Optional[float] = None,
    ) -> Optional[List[List[float]]]:
        """Daemon-encoded vectors, or None to signal "fall back in process".

        ``timeout_s`` is None from ``encode()`` (flat client default) and
        ``batch_timeout_s(len(texts))`` from ``encode_batch()`` (Finding 1).
        """
        if not should_attempt():
            return None
        if not ready_gate(self._client):
            return None
        try:
            vectors = self._client.encode_dense(texts, task=task, timeout_s=timeout_s)
        except Exception as exc:  # EncoderClientError + any stray transport error
            record_failure()
            log.debug("remote dense encode failed (%s); encoding in process", exc)
            return None
        if not self._usable(vectors, len(texts)):
            record_failure()
            log.debug(
                "remote dense encode returned an unusable payload for dim=%d; "
                "encoding in process",
                self.dim,
            )
            return None
        record_success()
        return vectors

    def _usable(self, vectors: Any, expected: int) -> bool:
        """Shape gate: N vectors of exactly ``dim`` floats, or it is a failure.

        A daemon serving a different ``dim`` would otherwise poison
        ``vec_to_blob`` (ValueError → NULL column) and the dense query matmul
        (shape mismatch → no hits); falling back in process is strictly better.
        """
        if not isinstance(vectors, list) or len(vectors) != expected:
            return False
        return all(isinstance(v, list) and len(v) == self.dim for v in vectors)

    def _local(self) -> Any:
        """The lazily-built in-process fallback codec (one per instance).

        Constructed directly rather than through ``get_shared_codec`` — that
        factory returns *this* class while a URL is active. One fallback per
        remote instance is still one model per ``(model_name, dim, device)``
        key, because the remote instances themselves are the cached
        singletons.
        """
        codec = self._fallback
        if codec is not None:
            return codec
        with self._fallback_lock:
            if self._fallback is None:
                # Imported here, and by module-attribute lookup, so the client
                # module stays torch-free until an outage actually needs it
                # (and so tests can patch bgem3_codec.BGEM3Codec).
                from .bgem3_codec import BGEM3Codec

                self._fallback = BGEM3Codec(
                    dim=self.dim, device=self._device, model_name=self.model_name,
                )
            return self._fallback


class RemoteSemaCodec:
    """``LazySemaCodec`` drop-in that encodes ΣĒMA vectors through the daemon.

    Constructed by ``CymatixContextManager.__init__`` in place of
    ``LazySemaCodec`` when a daemon URL is active; the single manager-owned
    instance is what the store's Tier 4 / cold tier, /debug/resonance,
    /debug/neighbors, PLR confidence and CWoLa all use.

    Failure policy differs from ``LazySemaCodec`` on purpose: that class
    caches a *construction* error and re-raises it forever (right for a
    broken install), whereas a network error here is per-call — the daemon
    coming back must be picked up automatically once the circuit cooldown
    elapses. Every SEMA call site already guards with try/except, so a total
    failure (daemon down AND sentence-transformers missing) raises exactly
    where the in-process codec would have.
    """

    is_lazy_component = True

    # MiniLM's sentence-embedding width. Reported without loading anything;
    # the projection to 20 ΣĒMA dims happens daemon-side.
    _EMBED_DIM = 384

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        url: str = "",
        device: Optional[str] = None,
    ) -> None:
        self._model_name = model_name
        self._device = device
        self.url = url
        self._client = EncoderClient(url, timeout_s=default_timeout_s())
        self._local: Optional[Any] = None
        self._local_lock = threading.Lock()
        self._remote_ok = False

    # -- residency reporting (GET /admin/components) -----------------------

    @property
    def loaded(self) -> bool:
        """True once a model has actually served this codec — remote or local."""
        if self._remote_ok:
            return True
        return self._local is not None and bool(self._local.loaded)

    def peek(self) -> Optional[Any]:
        """The materialized fallback codec, or None — never triggers a load."""
        return self._local.peek() if self._local is not None else None

    def warm(self) -> "RemoteSemaCodec":
        """[hardware] lazy_encoders = false: probe the daemon, load nothing here.

        The daemon owns the weights, so "warming" is a readiness poll rather
        than a model construction — eagerly building MiniLM in this process
        would defeat the point of running the daemon at all.
        """
        if should_attempt() and not self._client.wait_ready(budget_s=ready_timeout_s()):
            record_failure()
        return self

    # -- encode -----------------------------------------------------------

    def encode(self, text: str) -> List[float]:
        vectors = self._encode_remote([text])
        if vectors is not None:
            return vectors[0]
        return self._fallback().encode(text)

    def encode_batch(self, texts: List[str], batch_size: int = 64) -> List[List[float]]:
        if not texts:
            return []
        # Finding 1: scale the timeout with len(texts) for the whole-batch
        # POST — see RemoteBGEM3Codec.encode_batch for the same rationale.
        vectors = self._encode_remote(list(texts), timeout_s=batch_timeout_s(len(texts)))
        if vectors is not None:
            return vectors
        return self._fallback().encode_batch(list(texts), batch_size=batch_size)

    def signature(self, text: str, top_k: int = 5) -> List[Any]:
        """Top-k strongest primes — SemaCodec.signature's math, run locally."""
        from .sema import PRIMES, PRIME_COUNT

        vec = self.encode(text)
        scored = [(PRIMES[i].name, vec[i]) for i in range(PRIME_COUNT)]
        scored.sort(key=lambda x: abs(x[1]), reverse=True)
        return scored[:top_k]

    def fingerprint(self, text: str) -> str:
        """Compact 5-prime fingerprint — SemaCodec.fingerprint's math, locally."""
        from .sema import PRIMES, PRIME_COUNT

        vec = self.encode(text)
        scored = [(PRIMES[i].symbol, vec[i]) for i in range(PRIME_COUNT)]
        scored.sort(key=lambda x: abs(x[1]), reverse=True)
        return " ".join(f"{sym}.{int(abs(score)*100):02d}" for sym, score in scored[:5])

    @property
    def projection_matrix(self) -> None:
        """None — the projection lives daemon-side (CountingSemaCodec precedent)."""
        return None

    @property
    def embed_dim(self) -> int:
        return self._EMBED_DIM

    # Pure numpy, no model, no RPC: pass straight through to SemaCodec's
    # staticmethods (Tier-4 hot loop). Wrapped rather than bound at class-body
    # time so importing this module never imports the sema module.
    @staticmethod
    def similarity(vec_a: List[float], vec_b: List[float]) -> float:
        from .sema import SemaCodec

        return SemaCodec.similarity(vec_a, vec_b)

    @staticmethod
    def nearest(query_vec: List[float], candidates: List[Any], k: int = 5) -> List[Any]:
        from .sema import SemaCodec

        return SemaCodec.nearest(query_vec, candidates, k=k)

    # -- internals --------------------------------------------------------

    def _encode_remote(
        self, texts: List[str], timeout_s: Optional[float] = None,
    ) -> Optional[List[List[float]]]:
        """Daemon-encoded ΣĒMA vectors, or None to signal "fall back in process".

        ``timeout_s`` is None from ``encode()`` (flat client default) and
        ``batch_timeout_s(len(texts))`` from ``encode_batch()`` (Finding 1).
        """
        if not should_attempt():
            return None
        if not ready_gate(self._client):
            return None
        try:
            vectors = self._client.encode_sema(texts, timeout_s=timeout_s)
        except Exception as exc:  # EncoderClientError + any stray transport error
            record_failure()
            log.debug("remote ΣĒMA encode failed (%s); encoding in process", exc)
            return None
        if not self._usable(vectors, len(texts)):
            record_failure()
            log.debug("remote ΣĒMA returned an unusable payload; encoding in process")
            return None
        record_success()
        self._remote_ok = True
        return vectors

    @staticmethod
    def _usable(vectors: Any, expected: int) -> bool:
        """Shape gate: every storage/read path silently drops non-20-d vectors."""
        from .sema import PRIME_COUNT

        if not isinstance(vectors, list) or len(vectors) != expected:
            return False
        return all(isinstance(v, list) and len(v) == PRIME_COUNT for v in vectors)

    def _fallback(self) -> Any:
        """The lazily-built in-process ``LazySemaCodec`` (one per instance)."""
        codec = self._local
        if codec is not None:
            return codec
        with self._local_lock:
            if self._local is None:
                from .sema import LazySemaCodec

                self._local = LazySemaCodec(
                    model_name=self._model_name, device=self._device,
                )
            return self._local
