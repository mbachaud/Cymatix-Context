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
   ``max_batch`` items within a window (default 8 ms,
   ``CYMATIX_ENCODER_BATCH_WINDOW_MS``). **Dense** issues one
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

import logging
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .backends.encoder_client import _env_float

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
                _resolve(fut, exc=RuntimeError(
                    f"encoder daemon {self.name} produced no result for a queued item"
                ))
            if saw_stop:
                break
        self._drain_pending()

    def _drain_pending(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is _STOP:
                continue
            _, fut = item
            _resolve(fut, exc=RuntimeError(
                f"encoder daemon {self.name} batcher is shutting down"
            ))
