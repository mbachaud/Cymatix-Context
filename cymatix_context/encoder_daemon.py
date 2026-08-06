"""Encoder daemon — one process owning BGE-M3 + SPLADE + SEMA (Fork 1 slice 1).

The SERVER side of the encoder seam. ``cymatix_context.backends.encoder_client``
(Task 1) is the client; the ``RemoteCodec`` classes that substitute at the
dense/SPLADE/SEMA seams are Task 3. The binding API contract — endpoint table,
byte-parity rules, micro-batcher spec, startup sequence — lives in
``docs/design/2026-08-05-fork1-slice1-contract.md``.

Three pieces:

1. ``EncoderRegistry`` — owns the three encode callables plus their metadata
   (dense dim/model, resolved devices, effective batch sizes). Production
   builds it with ``EncoderRegistry.load(cfg)``; tests inject fakes through
   the constructor. Nothing in this module imports torch/transformers/
   sentence_transformers at module scope — those imports live inside
   ``load()`` and the capacity probe, so importing this module (and running
   the non-live suite against it) never touches a model.
2. The micro-batcher — one queue + one worker thread per model family.
   Requests enqueue ``(payload, Future)``; a worker drains up to
   ``max_batch`` items within a window (default 2 ms,
   ``CYMATIX_ENCODER_BATCH_WINDOW_MS`` — 2026-08-05 dogfood A/B receipt: 8ms
   cost +4pp on the c=1 latency gate with no batching benefit at any
   concurrency; coalescing under load comes from queue backpressure while
   the worker encodes, not from the idle window). **Dense** issues one
   ``encode_batch`` call per drained batch (the only place batching is
   byte-sanctioned); **SPLADE and SEMA** drain batches but execute a
   sequential per-item loop, because ``encode_batch``'s ``padding=True``
   changes max-pool bytes versus today's single-item query path.
3. The FastAPI app (``create_encoder_app``) + ``main()`` — added alongside.

Byte-parity is the load-bearing constraint: the daemon must produce exactly
what an in-process encode would produce, so the batcher never re-rounds,
re-sorts, or re-groups anything the codecs return.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import queue
import sys
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ``_env_float`` is deliberately shared with the client rather than
# re-implemented: both sides must agree on how a malformed env knob degrades.
from .backends.encoder_client import _env_float
from .backends.encoder_client import batch_window_ms as client_batch_window_ms

log = logging.getLogger("cymatix.encoder_daemon")


# ── constants ───────────────────────────────────────────────────────

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11439  # 11437 = backend, 11491-11493 = bench harnesses.

SPAWN_TOKEN_ENV = "CYMATIX_ENCODER_SPAWN_TOKEN"

REQUEST_TIMEOUT_ENV = "CYMATIX_ENCODER_REQUEST_TIMEOUT_S"
REQUEST_TIMEOUT_DEFAULT = 30.0

#: Families served by the daemon, in bundle order.
FAMILIES: Tuple[str, ...] = ("dense", "splade", "sema")

#: Daemon-local micro-batch caps. ``hardware._BATCH_TABLE`` has no dense/sema
#: rows, so ``recommended_batch_size`` floors them at 1 — not a real cap.
DAEMON_MAX_BATCH: Dict[str, int] = {"dense": 16, "splade": 8, "sema": 64}

#: Constant text for the readiness probe bundle and the warm-rate probes.
PROBE_TEXT = "cymatix encoder daemon readiness probe"

#: Encodes per model in the warm-rate probe (capacity self-report).
WARM_PROBE_ENCODES = 5


def request_timeout_s() -> float:
    """CYMATIX_ENCODER_REQUEST_TIMEOUT_S — per-request future wait (30.0)."""
    return _env_float(REQUEST_TIMEOUT_ENV, REQUEST_TIMEOUT_DEFAULT)


def resolve_max_batch(family: str) -> int:
    """Effective micro-batch cap: [hardware] override > table > daemon default.

    ``recommended_batch_size`` already consults ``batch_size_overrides``, but
    an explicit operator override must win *verbatim* (including values below
    the daemon default), while a table hit only wins when it is larger — the
    table's floor-1 miss for dense/sema is a lookup failure, not a cap.
    Hardware detection failing (no torch, exotic host) must never stop the
    daemon from serving: fall back to the daemon default with a warning.
    """
    default = DAEMON_MAX_BATCH.get(family, 1)
    try:
        from . import hardware

        overrides = getattr(hardware.get_hardware(), "batch_size_overrides", {}) or {}
        if family in overrides:
            return max(1, int(overrides[family]))
        return max(int(hardware.recommended_batch_size(family)), default)
    except Exception as exc:
        log.warning(
            "hardware batch-size resolution failed for %r (%s); "
            "using daemon default %d",
            family, exc, default,
        )
        return default


# ── registry ────────────────────────────────────────────────────────


class EncoderRegistry:
    """The three encode callables + their metadata, in one injectable object.

    Production: ``EncoderRegistry.load(cfg)`` after ``hardware.init_from_config``
    (the startup order is load-bearing — see the contract doc). Tests: pass
    fakes to the constructor. Everything downstream (batchers, endpoints,
    capacity report) reads this object, so the fake path and the real path
    share every line of serving code.
    """

    def __init__(
        self,
        dense_codec: Any = None,
        splade_encode: Optional[Callable[..., Dict[str, float]]] = None,
        sema_codec: Any = None,
        *,
        dense_dim: int = 1024,
        dense_model_name: str = "BAAI/bge-m3",
        devices: Optional[Dict[str, Optional[str]]] = None,
        batch_sizes: Optional[Dict[str, int]] = None,
    ) -> None:
        self.dense_codec = dense_codec
        self.splade_encode = splade_encode
        self.sema_codec = sema_codec
        self.dense_dim = int(dense_dim)
        self.dense_model_name = dense_model_name
        self.devices: Dict[str, Optional[str]] = dict(devices or {})
        self.batch_sizes: Dict[str, int] = dict(batch_sizes or {})

    # -- encode surface (what the batchers call) ------------------------

    def dense_encode_batch(self, texts: Sequence[str], task: str) -> List[List[float]]:
        """One BGEM3Codec.encode_batch call — byte-identical to N encodes."""
        return self.dense_codec.encode_batch(list(texts), task=task)

    def splade_encode_one(self, text: str, top_k: int) -> Dict[str, float]:
        """Single-item splade_backend.encode (padding=False query semantics)."""
        return self.splade_encode(text, top_k=top_k)

    def sema_encode_one(self, text: str) -> List[float]:
        """Single-item SemaCodec.encode — 20 floats against the PRIMES anchors."""
        return self.sema_codec.encode(text)

    # -- metadata -------------------------------------------------------

    @property
    def dense_meta(self) -> Dict[str, Any]:
        """``{"dim", "model"}`` — the /encode/dense response metadata."""
        return {"dim": self.dense_dim, "model": self.dense_model_name}

    @property
    def loaded(self) -> Dict[str, bool]:
        return {
            "dense": self.dense_codec is not None,
            "splade": self.splade_encode is not None,
            "sema": self.sema_codec is not None,
        }

    def max_batch(self, family: str) -> int:
        """Effective cap for *family* (explicit wiring wins over resolution)."""
        if family in self.batch_sizes:
            return max(1, int(self.batch_sizes[family]))
        return DAEMON_MAX_BATCH.get(family, 1)

    def warm(self) -> None:
        """Touch every model once so weights are resident before /ready flips.

        The daemon is deliberately not lazy: paying the load here means the
        readiness probe measures steady-state rates, and the first real client
        request never eats a 2 GB weight load.
        """
        if self.dense_codec is not None:
            self.dense_encode_batch([PROBE_TEXT], "passage")
        if self.splade_encode is not None:
            self.splade_encode_one(PROBE_TEXT, 128)
        if self.sema_codec is not None:
            self.sema_encode_one(PROBE_TEXT)

    # -- production construction ----------------------------------------

    @classmethod
    def load(cls, cfg: Any) -> "EncoderRegistry":
        """Build the real codecs. NOT exercised by the non-live suite.

        Startup order (contract doc, seam notes §4): ``hardware.init_from_config``
        must already have run — ``main()`` does it before the app exists. Dense
        gets its device passed explicitly (the ctor default is "cpu"; omitting
        it silently CPU-pins BGE-M3); SPLADE and SEMA self-resolve their layer
        devices. torch/transformers/sentence_transformers enter the process
        here and nowhere else in this module.
        """
        from .backends.bgem3_codec import get_shared_codec, shared_dense_codec_enabled
        from .backends import splade_backend
        from .backends.sema import SemaCodec
        from .hardware import resolve_layer_device

        dense_dim = int(cfg.retrieval.dense_embedding_dim)
        dense_model = str(cfg.retrieval.dense_model)
        splade_model = str(cfg.ingestion.splade_model)

        dense_codec = get_shared_codec(
            dim=dense_dim,
            model_name=dense_model,
            device=resolve_layer_device("dense"),
            share=shared_dense_codec_enabled(),
        )
        sema_codec = SemaCodec(model_name=str(cfg.ingestion.sema_model), device=None)

        def _splade_encode(text: str, top_k: int = 128) -> Dict[str, float]:
            return splade_backend.encode(text, top_k=top_k, model_name=splade_model)

        return cls(
            dense_codec=dense_codec,
            splade_encode=_splade_encode,
            sema_codec=sema_codec,
            dense_dim=dense_dim,
            dense_model_name=dense_model,
            devices={family: resolve_layer_device(family) for family in FAMILIES},
            batch_sizes={family: resolve_max_batch(family) for family in FAMILIES},
        )


# ── micro-batcher ───────────────────────────────────────────────────

_STOP = object()

#: One queued unit of work: an opaque payload tuple plus the caller's future.
_Item = Tuple[Tuple[Any, ...], "Future"]


def _resolve(fut: "Future", *, result: Any = None, exc: Optional[BaseException] = None) -> None:
    """Resolve *fut* exactly once; already-resolved/cancelled futures no-op."""
    try:
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(result)
    except Exception:
        # concurrent.futures.InvalidStateError (cancelled or already set) —
        # this helper is deliberately idempotent so the worker's
        # never-leave-a-future-hanging sweep is safe to run unconditionally.
        pass


def make_sequential_executor(
    encode_one: Callable[..., Any],
) -> Callable[[List[_Item]], None]:
    """Per-item executor — the SPLADE/SEMA byte-parity path.

    A drained batch is executed as N single-item encodes, so the bytes match
    an in-process ``splade_backend.encode`` / ``SemaCodec.encode`` call
    exactly. One item raising resolves only that item's future; its batch
    siblings and the worker thread are unaffected.
    """

    def _execute(batch: List[_Item]) -> None:
        for payload, fut in batch:
            try:
                _resolve(fut, result=encode_one(*payload))
            except Exception as exc:
                log.warning("encoder daemon per-item encode failed: %s", exc, exc_info=True)
                _resolve(fut, exc=exc)

    return _execute


def make_dense_executor(
    encode_batch: Callable[..., List[List[float]]],
) -> Callable[[List[_Item]], None]:
    """Batched executor — one ``encode_batch`` call per drained batch.

    Items are grouped by ``task`` first: the query prefix is applied per-text
    by the codec from its ``task=`` argument, so mixing a query and a passage
    into one call would prefix the wrong texts. In the common case every item
    in a batch shares a task and this is exactly one call.
    """

    def _execute(batch: List[_Item]) -> None:
        by_task: Dict[str, List[Tuple[str, "Future"]]] = {}
        for payload, fut in batch:
            text, task = payload
            by_task.setdefault(task, []).append((text, fut))

        for task, items in by_task.items():
            texts = [text for text, _ in items]
            try:
                vectors = encode_batch(texts, task)
            except Exception as exc:
                log.warning("encoder daemon dense batch failed: %s", exc, exc_info=True)
                for _, fut in items:
                    _resolve(fut, exc=exc)
                continue
            if vectors is None or len(vectors) != len(texts):
                exc = RuntimeError(
                    f"dense encode_batch returned {0 if vectors is None else len(vectors)} "
                    f"vectors for {len(texts)} texts"
                )
                for _, fut in items:
                    _resolve(fut, exc=exc)
                continue
            for (_, fut), vec in zip(items, vectors):
                _resolve(fut, result=vec)

    return _execute


class _Batcher:
    """One queue + one worker thread for a model family.

    The worker blocks on the queue, then drains up to ``max_batch`` further
    items within ``window_s`` before handing the batch to *execute*. It never
    dies: an executor raising resolves the batch's futures with the exception
    and the loop continues. Shutdown is a stop sentinel plus a join — the
    thread is a daemon thread so a wedged worker can never hold the process
    open, but the sentinel means clean shutdown does not rely on that.
    """

    def __init__(
        self,
        name: str,
        execute: Callable[[List[_Item]], None],
        *,
        max_batch: int,
        window_s: float,
    ) -> None:
        self.name = name
        self._execute = execute
        self._max_batch = max(1, int(max_batch))
        self._window_s = max(0.0, float(window_s))
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stopped = threading.Event()
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run, name=f"encoder-batcher-{self.name}", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Sentinel + join, then fail anything still queued (never hang a caller)."""
        self._stopped.set()
        self._queue.put(_STOP)
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                log.warning(
                    "encoder batcher %s did not stop within %.1fs (daemon thread)",
                    self.name, timeout,
                )
        self._drain_pending()

    def is_alive(self) -> bool:
        with self._lock:
            thread = self._thread
        return bool(thread is not None and thread.is_alive())

    # -- enqueue --------------------------------------------------------

    def submit(self, payload: Tuple[Any, ...]) -> "Future":
        fut: "Future" = Future()
        if self._stopped.is_set():
            _resolve(fut, exc=RuntimeError(f"encoder daemon {self.name} batcher is stopped"))
            return fut
        self._queue.put((payload, fut))
        if self._stopped.is_set():
            # Raced a concurrent stop(): the drain may already have run, so
            # sweep again rather than leave this item's future hanging.
            self._drain_pending()
        return fut

    # -- worker ---------------------------------------------------------

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                break
            batch: List[_Item] = [item]
            saw_stop = False
            if self._max_batch > 1 and self._window_s > 0.0:
                deadline = time.monotonic() + self._window_s
                while len(batch) < self._max_batch:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        nxt = self._queue.get(timeout=remaining)
                    except queue.Empty:
                        break
                    if nxt is _STOP:
                        saw_stop = True
                        break
                    batch.append(nxt)
            try:
                self._execute(batch)
            except Exception as exc:
                # Executors are written not to raise; if one ever does, the
                # worker still survives and the batch's callers still learn.
                log.warning(
                    "encoder batcher %s executor raised: %s", self.name, exc, exc_info=True
                )
                for _, fut in batch:
                    _resolve(fut, exc=exc)
            # Belt-and-braces: a buggy executor that silently skipped an item
            # must not leave an HTTP handler blocked until its timeout.
            for _, fut in batch:
                if not fut.done():
                    _resolve(fut, exc=RuntimeError(
                        f"encoder daemon {self.name} produced no result for a queued item"
                    ))
            if saw_stop:
                break
        self._drain_pending()

    def _drain_pending(self) -> None:
        """Fail everything still queued — nothing may outlive the worker unresolved."""
        requeue_stop = False
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is _STOP:
                requeue_stop = True
                continue
            _, fut = item
            _resolve(fut, exc=RuntimeError(
                f"encoder daemon {self.name} batcher is shutting down"
            ))
        if requeue_stop and threading.current_thread() is not self._thread:
            # A submit racing stop() must not swallow the worker's exit signal
            # (the worker itself has already consumed one — it needs no more).
            self._queue.put(_STOP)


# ── readiness ───────────────────────────────────────────────────────


class ReadyState:
    """Thread-safe readiness record behind ``/ready`` and ``/health``.

    Clients gate on ``/ready``: 200 only after every model is loaded AND one
    probe bundle has round-tripped through the real micro-batch path. Anything
    earlier is a 503 carrying the current ``state`` string, so an operator
    watching a cold daemon can tell "loading weights" from "probing" from
    "failed".
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "starting"
        self._ready = False
        self._probe_ms: Optional[float] = None
        self._error: Optional[str] = None
        self._capacity: Dict[str, Any] = {}

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._ready

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def capacity(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._capacity)

    def mark(self, state: str) -> None:
        """Move to a not-ready *state* (also the test hook for flipping back)."""
        with self._lock:
            self._state = state
            self._ready = False

    def mark_ready(self, probe_ms: float, capacity: Dict[str, Any]) -> None:
        with self._lock:
            self._state = "ready"
            self._ready = True
            self._probe_ms = float(probe_ms)
            self._capacity = dict(capacity)
            self._error = None

    def mark_failed(self, error: str) -> None:
        with self._lock:
            self._state = "failed"
            self._ready = False
            self._error = error

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "ready": self._ready,
                "state": self._state,
                "probe_ms": self._probe_ms,
                "error": self._error,
            }


# ── capacity self-report ────────────────────────────────────────────


def _torch_version() -> Optional[str]:
    """torch.__version__ if torch is ALREADY imported — never imports it.

    In the daemon torch is resident the moment the codecs load, so this is
    truthful in production while keeping this module (and the non-live suite)
    torch-free.
    """
    torch = sys.modules.get("torch")
    return getattr(torch, "__version__", None) if torch is not None else None


def _vram_gb() -> Tuple[Optional[float], Optional[float]]:
    """(total, free) VRAM in GiB via torch.cuda.mem_get_info, fully guarded."""
    torch = sys.modules.get("torch")
    if torch is None:
        return None, None
    try:
        if not torch.cuda.is_available():
            return None, None
        free, total = torch.cuda.mem_get_info()
        return round(total / (1024 ** 3), 3), round(free / (1024 ** 3), 3)
    except Exception as exc:
        log.debug("VRAM probe failed: %s", exc)
        return None, None


def _system_ram_gb() -> Optional[float]:
    """System RAM in GiB via psutil (optional dependency — guarded)."""
    try:
        import psutil

        return round(psutil.virtual_memory().total / (1024 ** 3), 2)
    except Exception as exc:
        log.debug("system RAM probe failed: %s", exc)
        return None


def _finite_rate(value: Optional[float]) -> Optional[float]:
    """Round a rate for the report; inf/nan/<=0 become None.

    Starlette's JSONResponse serializes with ``allow_nan=False``, so an
    infinite rate (a fake encoder finishing in zero measurable time) would
    turn ``/health`` into a 500 — the report must never carry one.
    """
    if value is None:
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rate) or rate <= 0.0:
        return None
    return round(rate, 3)


def build_capacity_report(
    registry: EncoderRegistry,
    rates: Dict[str, Optional[float]],
    window_s: float,
    probe_ms: float,
) -> Dict[str, Any]:
    """The self-report logged at the ready flip and served at /health.capacity.

    ``bundles_per_s`` reports both readings the fork's bars are quoted
    against: ``interleaved`` = 1/sum(1/rate) (one worker doing all three
    encodes back to back) and ``pipelined`` = min(rate) (three stages in
    flight at once, so the slowest model sets the ceiling).
    """
    warm = {family: _finite_rate(rates.get(family)) for family in FAMILIES}
    usable = [r for r in warm.values() if r]
    if len(usable) == len(FAMILIES):
        interleaved: Optional[float] = round(1.0 / sum(1.0 / r for r in usable), 3)
        pipelined: Optional[float] = round(min(usable), 3)
    else:
        interleaved = pipelined = None

    vram_total, vram_free = _vram_gb()
    return {
        "devices": {family: registry.devices.get(family) for family in FAMILIES},
        "warm_encodes_per_s": warm,
        "bundles_per_s": {"interleaved": interleaved, "pipelined": pipelined},
        "max_batch": {family: registry.max_batch(family) for family in FAMILIES},
        "batch_window_ms": round(window_s * 1000.0, 3),
        "probe_ms": round(float(probe_ms), 3),
        "warm_probe_encodes": WARM_PROBE_ENCODES,
        "loaded": registry.loaded,
        "dense_model": registry.dense_model_name,
        "dense_dim": registry.dense_dim,
        "torch_version": _torch_version(),
        "vram_total_gb": vram_total,
        "vram_free_gb": vram_free,
        "system_ram_gb": _system_ram_gb(),
        "pid": os.getpid(),
    }


# ── daemon wiring (registry + batchers + readiness, one object) ──────


class _Daemon:
    """Owns the registry, the three batchers, and the readiness lifecycle."""

    def __init__(self, registry: Optional[EncoderRegistry], window_s: float) -> None:
        self.registry = registry
        self.window_s = window_s
        self.ready_state = ReadyState()
        self.batchers: Dict[str, _Batcher] = {}
        self._startup_thread: Optional[threading.Thread] = None
        self._shutdown = threading.Event()

    # -- startup --------------------------------------------------------

    def start(self) -> None:
        """Called from the FastAPI lifespan.

        Injected registry (tests): bring-up is synchronous and sub-millisecond.
        Production: a daemon thread loads ~3 GB of weights while HTTP is
        already answering, so ``/health`` is reachable throughout the load and
        ``/ready`` reports progress instead of a dead socket.
        """
        if self.registry is not None:
            self.bring_up()
            return
        self._startup_thread = threading.Thread(
            target=self._bring_up_guarded, name="encoder-daemon-startup", daemon=True
        )
        self._startup_thread.start()

    def _bring_up_guarded(self) -> None:
        try:
            self.bring_up()
        except Exception as exc:
            log.error("encoder daemon startup failed: %s", exc, exc_info=True)
            self.ready_state.mark_failed(str(exc))

    def bring_up(self) -> None:
        self.ready_state.mark("loading")
        registry = self.registry
        if registry is None:
            # Production: config is loaded here (not in main()) so the HTTP
            # surface comes up first. init_hardware() is idempotent on the
            # hardware cache — main() has normally already run it, and this
            # call is what keeps the layer pins correct for any entry point
            # that skipped main().
            from .config import load_config

            cfg = load_config()
            init_hardware(cfg)
            registry = EncoderRegistry.load(cfg)
        registry.warm()  # eager: no lazy loads behind the ready flip
        self.install(registry)

        self.ready_state.mark("probing")
        started = time.monotonic()
        self._probe_bundle()
        probe_ms = (time.monotonic() - started) * 1000.0
        rates = self._warm_rates(registry)
        capacity = build_capacity_report(registry, rates, self.window_s, probe_ms)
        self.ready_state.mark_ready(probe_ms, capacity)
        log.info("encoder daemon ready in %.1f ms — capacity: %s", probe_ms, capacity)
        if self._shutdown.is_set():
            # Raced a shutdown that started while we were loading.
            self.stop()

    def install(self, registry: EncoderRegistry) -> None:
        """Build + start one batcher per family against *registry*."""
        self.registry = registry
        self.batchers = {
            "dense": _Batcher(
                "dense",
                make_dense_executor(registry.dense_encode_batch),
                max_batch=registry.max_batch("dense"),
                window_s=self.window_s,
            ),
            "splade": _Batcher(
                "splade",
                make_sequential_executor(registry.splade_encode_one),
                max_batch=registry.max_batch("splade"),
                window_s=self.window_s,
            ),
            "sema": _Batcher(
                "sema",
                make_sequential_executor(registry.sema_encode_one),
                max_batch=registry.max_batch("sema"),
                window_s=self.window_s,
            ),
        }
        for batcher in self.batchers.values():
            batcher.start()

    # -- probes ---------------------------------------------------------

    def _payload(self, family: str) -> Tuple[Any, ...]:
        return {
            "dense": (PROBE_TEXT, "query"),
            "splade": (PROBE_TEXT, 128),
            "sema": (PROBE_TEXT,),
        }[family]

    def _probe_bundle(self) -> None:
        """One bundle through the real micro-batch path — the ready gate."""
        timeout = request_timeout_s()
        futures = [self.batchers[family].submit(self._payload(family)) for family in FAMILIES]
        for future in futures:
            future.result(timeout=timeout)

    def _warm_rates(self, registry: EncoderRegistry) -> Dict[str, Optional[float]]:
        """Per-model warm encode rate — measured DIRECTLY, not via the batcher.

        The probe *bundle* must ride the real micro-batch path (it is the
        readiness gate). The warm-*rate* probe must not: a worker waits out
        its whole window on an idle queue, so a submitted probe measures
        ``1/(window + t_encode)`` and every reported rate would be hard-capped
        at ``1/window`` — 500 enc/s at the 2 ms default, which would still
        silently understate a fast GPU. ``bundles_per_s`` is derived from these
        numbers and is what the fork's ≥3.5 CPU / ≥30 GPU bars are read
        against, so the rate must describe the model, not the batcher.

        Each family is measured the way the daemon actually serves it: SPLADE
        and SEMA as WARM_PROBE_ENCODES sequential single encodes, dense as one
        ``encode_batch`` of WARM_PROBE_ENCODES texts (batching is the whole
        reason dense is worth daemonizing).
        """
        probes: Dict[str, Callable[[], Any]] = {
            "dense": lambda: registry.dense_encode_batch([PROBE_TEXT] * WARM_PROBE_ENCODES, "query"),
            "splade": lambda: [
                registry.splade_encode_one(PROBE_TEXT, 128) for _ in range(WARM_PROBE_ENCODES)
            ],
            "sema": lambda: [
                registry.sema_encode_one(PROBE_TEXT) for _ in range(WARM_PROBE_ENCODES)
            ],
        }
        rates: Dict[str, Optional[float]] = {}
        for family in FAMILIES:
            try:
                started = time.monotonic()
                probes[family]()
                elapsed = max(time.monotonic() - started, 1e-6)
                rates[family] = WARM_PROBE_ENCODES / elapsed
            except Exception as exc:
                log.warning("warm-rate probe failed for %s: %s", family, exc)
                rates[family] = None
        return rates

    # -- shutdown -------------------------------------------------------

    def stop(self) -> None:
        self._shutdown.set()
        thread = self._startup_thread
        # `is not current_thread()`: bring_up() calls stop() on itself when a
        # shutdown lands mid-load, and CPython raises RuntimeError if a thread
        # joins itself — which would abort the recovery and leave the batcher
        # threads running.
        if (
            thread is not None
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(timeout=5.0)
        for batcher in list(self.batchers.values()):
            batcher.stop()
        self.ready_state.mark("stopped")


# ── request models ──────────────────────────────────────────────────


class DenseRequest(BaseModel):
    texts: List[str]
    task: Literal["query", "passage"] = "passage"


class SpladeRequest(BaseModel):
    texts: List[str]
    top_k: int = 128


class SemaRequest(BaseModel):
    texts: List[str]


class BundleRequest(BaseModel):
    text: str
    task: Literal["query", "passage"] = "query"
    top_k: int = 128


def _require_ready(daemon: _Daemon) -> None:
    """503 before the ready flip — clients gate on /ready and fall back."""
    snapshot = daemon.ready_state.snapshot()
    if not snapshot["ready"]:
        raise HTTPException(
            status_code=503,
            detail=f"encoder daemon not ready (state={snapshot['state']})",
        )


def _join(futures: Sequence["Future"], path: str) -> List[Any]:
    """Await *futures* under one request budget; map failures onto HTTP 500."""
    timeout = request_timeout_s()
    deadline = time.monotonic() + timeout
    results: List[Any] = []
    for future in futures:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HTTPException(
                status_code=500, detail=f"{path}: encode timed out after {timeout:.1f}s"
            )
        try:
            results.append(future.result(timeout=remaining))
        except FutureTimeoutError:
            raise HTTPException(
                status_code=500, detail=f"{path}: encode timed out after {timeout:.1f}s"
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"{path}: encode failed: {exc}")
    return results


# ── app factory ─────────────────────────────────────────────────────


def create_encoder_app(
    registry: Optional[EncoderRegistry] = None,
    *,
    batch_window_ms: Optional[float] = None,
) -> FastAPI:
    """Build the daemon's FastAPI app.

    *registry* ``None`` is the production path: the lifespan starts a
    background thread that loads the real codecs. Tests inject an
    ``EncoderRegistry`` of fakes, which makes bring-up synchronous — every
    line of serving, batching, probing and reporting code is shared between
    the two paths.

    *batch_window_ms* overrides ``CYMATIX_ENCODER_BATCH_WINDOW_MS`` (2 ms).
    """
    # This process IS the daemon: force every encoder seam back in-process
    # before a registry can load. Otherwise an operator who exports
    # CYMATIX_ENCODER_URL (the documented switch) and starts the daemon from
    # that shell gets a daemon that is a client of itself — a readiness poll
    # against its own unbound port at startup, then /encode/* handlers POSTing
    # to themselves. Set here, not only in main(), so an embedded/ASGI-factory
    # bring-up is covered too.
    from .backends.encoder_client import mark_encoder_daemon_process

    mark_encoder_daemon_process()

    window_ms = client_batch_window_ms() if batch_window_ms is None else float(batch_window_ms)
    daemon = _Daemon(registry, window_s=max(0.0, window_ms) / 1000.0)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        daemon.start()
        try:
            yield
        finally:
            daemon.stop()

    from . import __version__ as _pkg_version

    app = FastAPI(title="Cymatix Encoder Daemon", version=_pkg_version, lifespan=lifespan)
    app.state.encoder = daemon
    app.state.ready_state = daemon.ready_state

    # ---- readiness / health ----

    @app.get("/ready")
    def ready() -> JSONResponse:
        snapshot = daemon.ready_state.snapshot()
        if not snapshot["ready"]:
            return JSONResponse(
                status_code=503,
                content={"ready": False, "state": snapshot["state"], "error": snapshot["error"]},
            )
        return JSONResponse(
            status_code=200,
            content={
                "ready": True,
                "spawn_token": os.environ.get(SPAWN_TOKEN_ENV),
                "probe_ms": snapshot["probe_ms"],
            },
        )

    @app.get("/health")
    def health() -> Dict[str, Any]:
        snapshot = daemon.ready_state.snapshot()
        return {
            "status": "ok",
            "ready": snapshot["ready"],
            "state": snapshot["state"],
            "pid": os.getpid(),
            "spawn_token": os.environ.get(SPAWN_TOKEN_ENV),
            "capacity": daemon.ready_state.capacity,
        }

    # ---- encode endpoints (thin: validate, enqueue, join) ----

    @app.post("/encode/dense")
    def encode_dense(req: DenseRequest) -> Dict[str, Any]:
        _require_ready(daemon)
        meta = daemon.registry.dense_meta
        if not req.texts:
            return {"vectors": [], **meta}
        futures = [daemon.batchers["dense"].submit((text, req.task)) for text in req.texts]
        return {"vectors": _join(futures, "/encode/dense"), **meta}

    @app.post("/encode/splade")
    def encode_splade(req: SpladeRequest) -> Dict[str, Any]:
        _require_ready(daemon)
        if not req.texts:
            return {"sparse": []}
        futures = [daemon.batchers["splade"].submit((text, req.top_k)) for text in req.texts]
        return {"sparse": _join(futures, "/encode/splade")}

    @app.post("/encode/sema")
    def encode_sema(req: SemaRequest) -> Dict[str, Any]:
        _require_ready(daemon)
        if not req.texts:
            return {"vectors": []}
        futures = [daemon.batchers["sema"].submit((text,)) for text in req.texts]
        return {"vectors": _join(futures, "/encode/sema")}

    @app.post("/encode/bundle")
    def encode_bundle(req: BundleRequest) -> Dict[str, Any]:
        _require_ready(daemon)
        # Fan out to all three queues BEFORE joining any of them: the three
        # models then run concurrently and the bundle costs max(), not sum().
        futures = [
            daemon.batchers["dense"].submit((req.text, req.task)),
            daemon.batchers["splade"].submit((req.text, req.top_k)),
            daemon.batchers["sema"].submit((req.text,)),
        ]
        dense, splade, sema = _join(futures, "/encode/bundle")
        return {"dense": dense, "splade": splade, "sema": sema}

    return app


# ── entry point ─────────────────────────────────────────────────────


def init_hardware(cfg: Any) -> None:
    """Apply the four ``[hardware]`` layer knobs — MUST precede any model.

    ``hardware.init_from_config`` is idempotent *on the cache*: if anything
    calls ``get_hardware()`` first, the singleton freezes with defaults and
    the layer pins plus batch-size overrides are silently dropped (pinned by
    tests/test_hardware.py::test_init_from_config_must_run_before_get_hardware).
    Both entry points — ``main()`` and the app's production bring-up — route
    through here so the order can only be got right once.
    """
    from . import hardware

    hardware.init_from_config(
        config_device=cfg.hardware.device,
        batch_size_overrides=cfg.hardware.batch_sizes,
        layer_devices={
            "dense": cfg.hardware.dense_device,
            "splade": cfg.hardware.splade_device,
            "sema": cfg.hardware.sema_device,
            "rerank": cfg.hardware.rerank_device,
        },
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cymatix_context.encoder_daemon",
        description="Shared BGE-M3 + SPLADE + SEMA encoder daemon (localhost HTTP).",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind host (default: %(default)s)")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="bind port (default: %(default)s)"
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn log level (default: %(default)s)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Start the daemon. Hardware init runs FIRST — the order is load-bearing.

    ``hardware.init_from_config`` is idempotent *on the cache*: if anything
    calls ``get_hardware()`` before it, the singleton freezes with defaults
    and the ``[hardware]`` layer pins plus batch-size overrides are silently
    dropped. Model construction happens later, in the app's startup thread.
    """
    args = build_arg_parser().parse_args(argv)

    # Before config, hardware or app: this process must never resolve a daemon
    # URL for itself (see create_encoder_app). Set in both entry points so
    # neither ordering can leave the window open.
    from .backends.encoder_client import mark_encoder_daemon_process

    mark_encoder_daemon_process()

    from .config import load_config

    init_hardware(load_config())

    app = create_encoder_app()
    log.info("Cymatix encoder daemon starting on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()
