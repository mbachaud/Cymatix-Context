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

**rerank family (#341, added on top of the above):** a fourth family —
``backends/rerank_backend.py``'s cross-encoder — joins the registry, the
batchers (sequential per-item, like SPLADE/SEMA), and the capacity report,
but it never rides ``/encode/bundle``. ``BUNDLE_FAMILIES`` names the three
families the bundle endpoint and its ``bundles_per_s`` math still cover;
``FAMILIES = BUNDLE_FAMILIES + ("rerank",)`` is everything else (registry
load, devices, ``max_batch``, ``loaded``, ``warm_encodes_per_s``).

Determinism profile (slice 2, ``CYMATIX_ENCODER_DETERMINISTIC=1``)
------------------------------------------------------------------

``docs/benchmarks/2026-08-05-fork1-worker-sweep.md`` ("Rank stability under
parallelism") measured recall drifting ±0.05 across concurrency levels on
CUDA with overlap_fraction ≥0.975 and zero errors: batched-GEMM low-mantissa
drift across *batch shapes* flips near-cutoff ranks. The driver is the shape
of the dense micro-batch — ``(item count × padded token length)`` — which
under concurrency depends on which requests happened to be in the queue when
the worker drained it. Off by default; everything below is byte-for-byte
inert unless the env gate is set.

**What the gate pins, honestly:**

- *Item count* — **pinned**. The dense executor pads every drained
  task-group up to exactly ``max_batch`` with copies of a constant filler
  text (``dense_filler_text()``) and discards the filler rows. This is done
  with the codec's public ``encode_batch`` API only; no model library is
  forked or monkey-patched.
- *Padded token length* — **pinned only by domination, not by API**. Neither
  backend's public API accepts a fixed pad length:
  ``BGEM3FlagModel.encode`` and ``SentenceTransformer.encode`` both sort
  inputs by length, chunk them by their own internal ``batch_size``, and
  tokenize each chunk with ``padding=True`` (pad to the longest member).
  There is no ``padding="max_length"`` knob on either. So the filler is a
  deterministic ~max-length text (default 4096 chars,
  ``CYMATIX_ENCODER_FILLER_CHARS``) — roughly 2x the 2000-char
  ``dense_passage_char_cap`` callers apply — which makes it the longest
  member of every batch and therefore makes the padded length a constant.
  The pin holds while every real item is shorter than the filler; a caller
  that sends an uncapped passage longer than the filler silently returns
  that batch to today's data-dependent shape. The daemon encodes exactly
  what it receives (contract, "Text caps are applied by CALLERS"), so it
  cannot enforce this.
- *Sub-batch chunking* — pinned only while ``max_batch`` (default 16) is
  ≤ the backend's own encode ``batch_size`` (sentence-transformers: 32;
  FlagEmbedding: its own default, not verifiable here — FlagEmbedding is not
  installed in this venv). Above that the backend splits our fixed batch
  into several chunks and only the chunk holding the filler is length-pinned.
- *Flash-attention varlen packing* — **not pinned**. When
  sentence-transformers can flatten inputs (flash-attn with varlen support),
  it drops padding entirely and packs the chunk into one concatenated
  sequence whose length varies with content. Item count stays pinned; padded
  length becomes moot and unpinned. Not the default attention path here.
- *Cost* — the gate trades throughput for shape stability: a 2-item drain
  becomes a ``max_batch``-item encode of near-max-length rows. That is the
  point, and it is why the gate is off by default.

``apply_determinism_flags()`` (called at ``EncoderRegistry.load``, i.e. in
the daemon process only) additionally sets
``torch.use_deterministic_algorithms(True, warn_only=True)``, turns TF32 off
for cuBLAS matmuls (``CYMATIX_ENCODER_TF32=1`` re-enables it), and sets
``CUBLAS_WORKSPACE_CONFIG`` if the operator has not. Every step is guarded
for a CPU-only or missing torch. ``torch.backends.cudnn.allow_tf32`` is
deliberately untouched — BGE-M3 inference is cuBLAS matmul, not cuDNN.

Daemon-side vector LRU (``CYMATIX_ENCODER_VECTOR_CACHE``, default 4096
entries, 0 = off) is orthogonal to the gate: keyed
``(family, model, task, text)``, checked before enqueue and populated after
a successful encode, never on failure. A cache hit is also a determinism
win — the same text returns the same bytes it returned the first time,
regardless of what shape its batch would have had this turn.
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
from collections import OrderedDict
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

#: Families that ride the ``/encode/bundle`` fan-out and the
#: ``bundles_per_s`` (interleaved/pipelined) capacity math — three models,
#: unchanged shape since the fork-1 slice-1 contract. ``rerank`` (#341) is
#: server-side registry/capacity plumbing only; it never joins a bundle.
BUNDLE_FAMILIES: Tuple[str, ...] = ("dense", "splade", "sema")

#: Every family the daemon serves: devices/loaded/max_batch/warm rates and
#: the registry loop all iterate this. ``bundles_per_s`` and the
#: ``/encode/bundle`` payload iteration deliberately use ``BUNDLE_FAMILIES``
#: instead — see the module docstring's bundle-vs-family split.
FAMILIES: Tuple[str, ...] = BUNDLE_FAMILIES + ("rerank",)

#: Daemon-local micro-batch caps. ``hardware._BATCH_TABLE`` has no dense/sema
#: rows, so ``recommended_batch_size`` floors them at 1 — not a real cap.
DAEMON_MAX_BATCH: Dict[str, int] = {"dense": 16, "splade": 8, "sema": 64, "rerank": 16}

#: Constant text for the readiness probe bundle and the warm-rate probes.
PROBE_TEXT = "cymatix encoder daemon readiness probe"

#: Encodes per model in the warm-rate probe (capacity self-report).
WARM_PROBE_ENCODES = 5


# ── determinism profile knobs (slice 2 — see the module docstring) ───

DETERMINISTIC_ENV = "CYMATIX_ENCODER_DETERMINISTIC"

#: TF32 is OFF whenever the deterministic profile is on; this re-enables it
#: for an operator who wants deterministic algorithms without the TF32 cost.
TF32_ENV = "CYMATIX_ENCODER_TF32"

#: torch requires this before the first cuBLAS handle when
#: ``use_deterministic_algorithms`` is on; ``:4096:8`` is torch's own
#: documented value. Only set when the operator has not set one.
CUBLAS_WORKSPACE_ENV = "CUBLAS_WORKSPACE_CONFIG"
CUBLAS_WORKSPACE_DEFAULT = ":4096:8"

#: Filler size in characters. Default is ~2x the 2000-char
#: ``[ingestion] dense_passage_char_cap`` callers apply, so the filler
#: dominates every real item and the padded length becomes a constant.
FILLER_CHARS_ENV = "CYMATIX_ENCODER_FILLER_CHARS"
FILLER_CHARS_DEFAULT = 4096

#: Deterministic ASCII unit the filler text is built from. Constant for the
#: package's lifetime: changing it changes every deterministic-mode batch
#: shape (and therefore the low mantissa bits this profile exists to pin).
FILLER_UNIT = "cymatix deterministic batch shape filler token sequence "

#: Daemon-side vector LRU capacity in entries; 0 disables it entirely.
VECTOR_CACHE_ENV = "CYMATIX_ENCODER_VECTOR_CACHE"
VECTOR_CACHE_DEFAULT = 4096

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean env knob; malformed/unset values fall back to *default*.

    Same degradation contract as ``_env_float`` (shared with the client): a
    knob nobody set, and a knob somebody set to nonsense, must behave the
    same way, and the nonsense case must say so once in the log.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE_WORDS:
        return True
    if value in _FALSE_WORDS:
        return False
    log.warning("%s=%r is not a boolean; using default %s", name, raw, default)
    return default


def _env_int(name: str, default: int) -> int:
    """Read an integer env knob; malformed/unset values fall back to *default*."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        log.warning("%s=%r is not an integer; using default %s", name, raw, default)
        return default


def request_timeout_s() -> float:
    """CYMATIX_ENCODER_REQUEST_TIMEOUT_S — per-request future wait (30.0)."""
    return _env_float(REQUEST_TIMEOUT_ENV, REQUEST_TIMEOUT_DEFAULT)


def deterministic_enabled() -> bool:
    """CYMATIX_ENCODER_DETERMINISTIC — the whole slice-2 gate (default off).

    Read at call time, never cached: the dense executor consults it per
    drained batch and ``/health`` reports the live value, so an operator can
    A/B the profile against a running daemon by flipping the env of a
    restart, and a test can monkeypatch it after the app exists.
    """
    return _env_flag(DETERMINISTIC_ENV, False)


def tf32_enabled() -> bool:
    """CYMATIX_ENCODER_TF32 — TF32 matmuls under the deterministic profile.

    Default **False**: TF32's 10-bit mantissa is exactly the drift budget the
    profile is trying to remove, so turning it off is part of the profile
    rather than a separate opt-in. Set to 1 to keep TF32's speed and accept
    the wider drift.
    """
    return _env_flag(TF32_ENV, False)


def filler_chars() -> int:
    """CYMATIX_ENCODER_FILLER_CHARS — filler length in characters (4096).

    Clamped to ≥1: a 1-char filler still pins the item count, it just stops
    pinning the padded length (see the module docstring's honest limits).
    """
    return max(1, _env_int(FILLER_CHARS_ENV, FILLER_CHARS_DEFAULT))


def dense_filler_text() -> str:
    """The constant filler text used to pad a dense batch to ``max_batch``.

    Deterministic by construction (a repeated ASCII unit sliced to length),
    so two daemons on the same env produce the same batch shape, and the
    same daemon produces the same shape on every drain.
    """
    n = filler_chars()
    reps = n // len(FILLER_UNIT) + 1
    return (FILLER_UNIT * reps)[:n]


def vector_cache_size() -> int:
    """CYMATIX_ENCODER_VECTOR_CACHE — LRU capacity in entries (4096; 0 = off)."""
    return max(0, _env_int(VECTOR_CACHE_ENV, VECTOR_CACHE_DEFAULT))


def apply_determinism_flags() -> Dict[str, Any]:
    """Apply the torch-level determinism profile. Daemon process only.

    Called from ``EncoderRegistry.load`` — the last point in this process
    where torch has not yet built a CUDA context, which matters because
    ``CUBLAS_WORKSPACE_CONFIG`` is only read when cuBLAS initializes its
    handle. Fully inert unless ``CYMATIX_ENCODER_DETERMINISTIC`` is set, and
    fully guarded otherwise: no torch, a CPU-only build, or a torch version
    without one of these attributes all degrade to a warning and a partial
    profile rather than a daemon that will not start.

    Returns a record of what was actually applied (logged, and asserted by
    tests against a fake torch module).
    """
    applied: Dict[str, Any] = {
        "enabled": deterministic_enabled(),
        "cublas_workspace_config": None,
        "use_deterministic_algorithms": False,
        "allow_tf32": None,
        "torch": False,
    }
    if not applied["enabled"]:
        return applied

    if not os.environ.get(CUBLAS_WORKSPACE_ENV, "").strip():
        os.environ[CUBLAS_WORKSPACE_ENV] = CUBLAS_WORKSPACE_DEFAULT
    applied["cublas_workspace_config"] = os.environ.get(CUBLAS_WORKSPACE_ENV)

    try:
        import torch
    except Exception as exc:
        log.warning(
            "deterministic profile requested but torch is unavailable (%s); "
            "fixed-shape batching still applies",
            exc,
        )
        return applied
    applied["torch"] = True

    try:
        torch.use_deterministic_algorithms(True, warn_only=True)
        applied["use_deterministic_algorithms"] = True
    except Exception as exc:
        log.warning("torch.use_deterministic_algorithms(True) failed: %s", exc)

    allow_tf32 = tf32_enabled()
    try:
        backends = getattr(torch, "backends", None)
        cuda_backend = getattr(backends, "cuda", None)
        matmul = getattr(cuda_backend, "matmul", None)
        if matmul is None:
            # CPU-only torch build: no cuBLAS knob to turn. Not an error.
            log.debug("torch.backends.cuda.matmul unavailable; TF32 knob skipped")
        else:
            matmul.allow_tf32 = allow_tf32
            applied["allow_tf32"] = allow_tf32
    except Exception as exc:
        log.warning("setting torch.backends.cuda.matmul.allow_tf32 failed: %s", exc)

    log.info("encoder daemon determinism profile applied: %s", applied)
    return applied


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
        rerank: Optional[Callable[[str, Sequence[str]], List[float]]] = None,
        *,
        dense_dim: int = 1024,
        dense_model_name: str = "BAAI/bge-m3",
        splade_model_name: str = "",
        sema_model_name: str = "",
        rerank_model_name: str = "",
        devices: Optional[Dict[str, Optional[str]]] = None,
        batch_sizes: Optional[Dict[str, int]] = None,
    ) -> None:
        self.dense_codec = dense_codec
        self.splade_encode = splade_encode
        self.sema_codec = sema_codec
        self.rerank = rerank
        self.dense_dim = int(dense_dim)
        self.dense_model_name = dense_model_name
        # Cache-key components only (the vector LRU must not serve a vector
        # from model A to a daemon reloaded onto model B). Default "" so the
        # fake-registry test path needs no extra wiring — family already
        # separates the key spaces.
        self.splade_model_name = splade_model_name
        self.sema_model_name = sema_model_name
        self.rerank_model_name = rerank_model_name
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

    def rerank_score_batch(self, query: str, texts: Sequence[str]) -> List[float]:
        """Cross-encoder scores for (query, texts) — delegates to the injected callable."""
        return list(self.rerank(query, list(texts)))

    def rerank_score_one(self, query: str, text: str) -> float:
        """Single-pair score — the per-item batcher executor's call shape."""
        return self.rerank_score_batch(query, [text])[0]

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
            "rerank": self.rerank is not None,
        }

    def model_name(self, family: str) -> str:
        """Model identity for *family* — the vector LRU's key component."""
        return {
            "dense": self.dense_model_name,
            "splade": self.splade_model_name,
            "sema": self.sema_model_name,
            "rerank": self.rerank_model_name,
        }.get(family, "")

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
        if self.rerank is not None:
            self.rerank_score_batch(PROBE_TEXT, [PROBE_TEXT])

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

        Slice 2: ``apply_determinism_flags()`` runs FIRST — before any codec
        exists, which is the last moment ``CUBLAS_WORKSPACE_CONFIG`` can
        still be read by a not-yet-initialized cuBLAS handle. Inert unless
        ``CYMATIX_ENCODER_DETERMINISTIC`` is set.
        """
        apply_determinism_flags()

        from .backends.bgem3_codec import get_shared_codec, shared_dense_codec_enabled
        from .backends import splade_backend
        from .backends import rerank_backend
        from .backends.sema import SemaCodec
        from .hardware import resolve_layer_device

        dense_dim = int(cfg.retrieval.dense_embedding_dim)
        dense_model = str(cfg.retrieval.dense_model)
        splade_model = str(cfg.ingestion.splade_model)
        sema_model = str(cfg.ingestion.sema_model)
        rerank_model = str(cfg.retrieval.rerank_model)

        dense_codec = get_shared_codec(
            dim=dense_dim,
            model_name=dense_model,
            device=resolve_layer_device("dense"),
            share=shared_dense_codec_enabled(),
        )
        sema_codec = SemaCodec(model_name=sema_model, device=None)

        def _splade_encode(text: str, top_k: int = 128) -> Dict[str, float]:
            return splade_backend.encode(text, top_k=top_k, model_name=splade_model)

        def _rerank_score(query: str, texts: List[str]) -> List[float]:
            # score_pairs owns device resolution (resolve_layer_device("rerank"))
            # and record_model_load via its own _get_scorer — nothing here
            # duplicates that, so this closure is just the model-name binding.
            return rerank_backend.score_pairs(query, texts, model_name=rerank_model)

        return cls(
            dense_codec=dense_codec,
            splade_encode=_splade_encode,
            sema_codec=sema_codec,
            rerank=_rerank_score,
            dense_dim=dense_dim,
            dense_model_name=dense_model,
            splade_model_name=splade_model,
            sema_model_name=sema_model,
            rerank_model_name=rerank_model,
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
    *,
    max_batch: Optional[int] = None,
) -> Callable[[List[_Item]], None]:
    """Batched executor — one ``encode_batch`` call per drained batch.

    Items are grouped by ``task`` first: the query prefix is applied per-text
    by the codec from its ``task=`` argument, so mixing a query and a passage
    into one call would prefix the wrong texts. In the common case every item
    in a batch shares a task and this is exactly one call.

    With ``CYMATIX_ENCODER_DETERMINISTIC`` set and *max_batch* wired, each
    task group is padded up to exactly ``max_batch`` items with copies of
    ``dense_filler_text()`` and the filler rows are discarded — the drained
    batch's SHAPE stops depending on concurrent arrival order, which is the
    measured cause of the ±0.05 rank wobble (module docstring). Both
    backends return rows in input order (sentence-transformers restores its
    internal length sort via ``argsort``; FlagEmbedding likewise), so the
    caller rows are the first ``len(items)`` of the response. Off (the
    default) this function is byte-for-byte what it was before slice 2.
    """
    cap = None if max_batch is None else max(1, int(max_batch))

    def _pad(texts: List[str]) -> List[str]:
        """Deterministic-mode shape pin; identity in every other case."""
        if cap is None or not texts or len(texts) >= cap:
            return texts
        if not deterministic_enabled():
            return texts
        return texts + [dense_filler_text()] * (cap - len(texts))

    def _execute(batch: List[_Item]) -> None:
        by_task: Dict[str, List[Tuple[str, "Future"]]] = {}
        for payload, fut in batch:
            text, task = payload
            by_task.setdefault(task, []).append((text, fut))

        for task, items in by_task.items():
            texts = [text for text, _ in items]
            padded = _pad(texts)
            try:
                vectors = encode_batch(padded, task)
            except Exception as exc:
                log.warning("encoder daemon dense batch failed: %s", exc, exc_info=True)
                for _, fut in items:
                    _resolve(fut, exc=exc)
                continue
            if vectors is None or len(vectors) != len(padded):
                exc = RuntimeError(
                    f"dense encode_batch returned {0 if vectors is None else len(vectors)} "
                    f"vectors for {len(padded)} texts"
                )
                for _, fut in items:
                    _resolve(fut, exc=exc)
                continue
            # zip() stops at the shorter side, so the filler rows past
            # len(items) are dropped without an explicit slice.
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


# ── daemon-side vector LRU ──────────────────────────────────────────


#: Cache key: ``(family, model_name, task, text)``. ``task`` carries whatever
#: else changes the bytes for that family — the dense query/passage prefix,
#: SPLADE's ``top_k`` truncation — so one key space per family cannot collide.
_CacheKey = Tuple[str, str, str, str]


class _VectorCache:
    """Bounded, thread-safe LRU over encoded vectors / sparse dicts.

    Checked before enqueue and populated after a successful encode, so a
    repeat text never reaches a queue, never lands in a micro-batch, and
    never changes another request's batch shape. Failures are never cached:
    a daemon that briefly OOMs must retry, not memoize the error.

    Sizing is in entries, not bytes. A 1024-dim fp32 dense vector is ~8 KB as
    a Python float list, so the 4096-entry default is a ~30 MB ceiling on the
    dense family — small next to the ~3 GB of weights this process already
    holds, and the whole point of the daemon is that this is the only process
    holding them.
    """

    def __init__(self, max_size: int) -> None:
        self._max = max(0, int(max_size))
        self._lock = threading.Lock()
        self._entries: "OrderedDict[_CacheKey, Any]" = OrderedDict()
        self._hits = 0
        self._misses = 0

    @property
    def enabled(self) -> bool:
        return self._max > 0

    @property
    def max_size(self) -> int:
        return self._max

    def get(self, key: _CacheKey) -> Tuple[bool, Any]:
        """``(hit, value)`` — a sentinel-free lookup (None is a legal value)."""
        if not self._max:
            return False, None
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                self._hits += 1
                return True, self._entries[key]
            self._misses += 1
            return False, None

    def put(self, key: _CacheKey, value: Any) -> None:
        if not self._max:
            return
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> Dict[str, Any]:
        """``/health.capacity.vector_cache`` — live, not a ready-flip snapshot."""
        with self._lock:
            return {
                "enabled": self._max > 0,
                "size": len(self._entries),
                "max_size": self._max,
                "hits": self._hits,
                "misses": self._misses,
            }


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
    vector_cache: Optional[_VectorCache] = None,
) -> Dict[str, Any]:
    """The self-report logged at the ready flip and served at /health.capacity.

    ``bundles_per_s`` reports both readings the fork's bars are quoted
    against: ``interleaved`` = 1/sum(1/rate) (one worker doing all three
    encodes back to back) and ``pipelined`` = min(rate) (three stages in
    flight at once, so the slowest model sets the ceiling).

    ``deterministic`` and ``vector_cache`` are the slice-2 additions: an
    operator reading a receipt has to be able to tell which profile produced
    it, and a rank-stability claim measured against a warm cache is a
    different claim from one measured against a cold one. ``/health``
    re-reads both live on every request (the counters move after the flip).
    """
    warm = {family: _finite_rate(rates.get(family)) for family in FAMILIES}
    # bundles_per_s is quoted against /encode/bundle, which fans out to
    # BUNDLE_FAMILIES only (rerank never joins a bundle) — see the module
    # docstring's bundle-vs-family split.
    bundle_usable = [warm[family] for family in BUNDLE_FAMILIES if warm.get(family)]
    if len(bundle_usable) == len(BUNDLE_FAMILIES):
        interleaved: Optional[float] = round(1.0 / sum(1.0 / r for r in bundle_usable), 3)
        pipelined: Optional[float] = round(min(bundle_usable), 3)
    else:
        interleaved = pipelined = None

    vram_total, vram_free = _vram_gb()
    return {
        "devices": {family: registry.devices.get(family) for family in FAMILIES},
        "warm_encodes_per_s": warm,
        "bundles_per_s": {"interleaved": interleaved, "pipelined": pipelined},
        "max_batch": {family: registry.max_batch(family) for family in FAMILIES},
        "batch_window_ms": round(window_s * 1000.0, 3),
        "deterministic": deterministic_enabled(),
        "vector_cache": (vector_cache or _VectorCache(0)).stats(),
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

    def __init__(
        self,
        registry: Optional[EncoderRegistry],
        window_s: float,
        *,
        vector_cache_max: Optional[int] = None,
    ) -> None:
        self.registry = registry
        self.window_s = window_s
        self.ready_state = ReadyState()
        self.batchers: Dict[str, _Batcher] = {}
        # Capacity read once, at daemon construction (load time) — the LRU is
        # allocated here and a live resize would have to evict, so unlike the
        # per-call knobs this one is fixed for the process.
        self.vector_cache = _VectorCache(
            vector_cache_size() if vector_cache_max is None else vector_cache_max
        )
        self._startup_thread: Optional[threading.Thread] = None
        self._shutdown = threading.Event()

    # -- vector cache ---------------------------------------------------

    def cache_key(self, family: str, payload: Tuple[Any, ...]) -> Optional[_CacheKey]:
        """``(family, model, task, text)`` for *payload*, or None to bypass.

        None means "do not consult or populate the cache": the LRU is off, or
        the registry is not installed yet (no model identity to key on), or
        the payload shape is not one this daemon knows how to key.
        """
        if not self.vector_cache.enabled:
            return None
        registry = self.registry
        if registry is None:
            return None
        model = registry.model_name(family)
        try:
            if family == "dense":
                text, task = payload
                return ("dense", model, str(task), str(text))
            if family == "splade":
                text, top_k = payload
                # top_k truncates the returned term set, so it is part of the
                # bytes: a top_k=32 hit must never serve a top_k=128 request.
                return ("splade", model, f"top_k={int(top_k)}", str(text))
            if family == "sema":
                return ("sema", model, "", str(payload[0]))
            if family == "rerank":
                # Payload is (query, text): the query stands in for "task"
                # here — it is the other axis that changes the score bytes,
                # so it belongs in the key exactly like dense's task prefix.
                query, text = payload
                return ("rerank", model, str(query), str(text))
        except Exception as exc:
            log.debug("vector cache key build failed for %s: %s", family, exc)
            return None
        return None

    def submit_cached(self, family: str, payload: Tuple[Any, ...]) -> "Future":
        """Cache-checked ``submit`` — the endpoints' enqueue path.

        A hit resolves a Future immediately and never touches the queue, so a
        repeat text costs no model time and cannot perturb another request's
        micro-batch shape. A miss enqueues normally and populates the cache
        from the done-callback (which runs on the batcher worker thread — the
        LRU is thread-safe). Failures are never cached.
        """
        key = self.cache_key(family, payload)
        if key is not None:
            hit, value = self.vector_cache.get(key)
            if hit:
                fut: "Future" = Future()
                _resolve(fut, result=value)
                return fut
        future = self.batchers[family].submit(payload)
        if key is not None:
            future.add_done_callback(lambda f, k=key: self._cache_result(k, f))
        return future

    def _cache_result(self, key: _CacheKey, future: "Future") -> None:
        """Populate the LRU from a resolved future — successes only."""
        try:
            if future.cancelled() or future.exception() is not None:
                return
            self.vector_cache.put(key, future.result())
        except Exception as exc:
            # A done-callback raising would be swallowed by concurrent.futures
            # anyway; log it rather than lose it.
            log.debug("vector cache populate failed: %s", exc)

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
        capacity = build_capacity_report(
            registry, rates, self.window_s, probe_ms, self.vector_cache
        )
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
                make_dense_executor(
                    registry.dense_encode_batch, max_batch=registry.max_batch("dense")
                ),
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
            "rerank": _Batcher(
                "rerank",
                make_sequential_executor(registry.rerank_score_one),
                max_batch=registry.max_batch("rerank"),
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
            "rerank": (PROBE_TEXT, PROBE_TEXT),
        }[family]

    def _probe_bundle(self) -> None:
        """One bundle through the real micro-batch path — the ready gate.

        Deliberately the raw batcher ``submit``, not ``submit_cached``: the
        readiness proof must be a real encode on every bring-up, and the
        probe's constant text would otherwise sit in the LRU forever as a
        hit that proves nothing.
        """
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
        reason dense is worth daemonizing). rerank is measured the same way
        as dense — one ``rerank_score_batch`` call of WARM_PROBE_ENCODES
        copies of the probe pair — since ``score_pairs`` batches internally
        too; it is excluded from ``bundles_per_s`` (BUNDLE_FAMILIES only) but
        still gets its own ``warm_encodes_per_s`` row.
        """
        probes: Dict[str, Callable[[], Any]] = {
            "dense": lambda: registry.dense_encode_batch([PROBE_TEXT] * WARM_PROBE_ENCODES, "query"),
            "splade": lambda: [
                registry.splade_encode_one(PROBE_TEXT, 128) for _ in range(WARM_PROBE_ENCODES)
            ],
            "sema": lambda: [
                registry.sema_encode_one(PROBE_TEXT) for _ in range(WARM_PROBE_ENCODES)
            ],
            "rerank": lambda: registry.rerank_score_batch(
                PROBE_TEXT, [PROBE_TEXT] * WARM_PROBE_ENCODES
            ),
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


class RerankRequest(BaseModel):
    query: str
    texts: List[str]


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
    vector_cache_max: Optional[int] = None,
) -> FastAPI:
    """Build the daemon's FastAPI app.

    *registry* ``None`` is the production path: the lifespan starts a
    background thread that loads the real codecs. Tests inject an
    ``EncoderRegistry`` of fakes, which makes bring-up synchronous — every
    line of serving, batching, probing and reporting code is shared between
    the two paths.

    *batch_window_ms* overrides ``CYMATIX_ENCODER_BATCH_WINDOW_MS`` (2 ms);
    *vector_cache_max* overrides ``CYMATIX_ENCODER_VECTOR_CACHE`` (4096
    entries, 0 = off).
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
    daemon = _Daemon(
        registry,
        window_s=max(0.0, window_ms) / 1000.0,
        vector_cache_max=vector_cache_max,
    )

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
        capacity = daemon.ready_state.capacity
        if capacity:
            # The rest of the report is a ready-flip snapshot; these two move
            # afterwards (counters, and an env knob read per call), so serve
            # them live rather than shipping a stale receipt.
            capacity["vector_cache"] = daemon.vector_cache.stats()
            capacity["deterministic"] = deterministic_enabled()
        return {
            "status": "ok",
            "ready": snapshot["ready"],
            "state": snapshot["state"],
            "pid": os.getpid(),
            "spawn_token": os.environ.get(SPAWN_TOKEN_ENV),
            "capacity": capacity,
        }

    # ---- encode endpoints (thin: validate, enqueue, join) ----

    @app.post("/encode/dense")
    def encode_dense(req: DenseRequest) -> Dict[str, Any]:
        _require_ready(daemon)
        meta = daemon.registry.dense_meta
        if not req.texts:
            return {"vectors": [], **meta}
        futures = [daemon.submit_cached("dense", (text, req.task)) for text in req.texts]
        return {"vectors": _join(futures, "/encode/dense"), **meta}

    @app.post("/encode/splade")
    def encode_splade(req: SpladeRequest) -> Dict[str, Any]:
        _require_ready(daemon)
        if not req.texts:
            return {"sparse": []}
        futures = [daemon.submit_cached("splade", (text, req.top_k)) for text in req.texts]
        return {"sparse": _join(futures, "/encode/splade")}

    @app.post("/encode/sema")
    def encode_sema(req: SemaRequest) -> Dict[str, Any]:
        _require_ready(daemon)
        if not req.texts:
            return {"vectors": []}
        futures = [daemon.submit_cached("sema", (text,)) for text in req.texts]
        return {"vectors": _join(futures, "/encode/sema")}

    @app.post("/encode/bundle")
    def encode_bundle(req: BundleRequest) -> Dict[str, Any]:
        _require_ready(daemon)
        # Fan out to all three queues BEFORE joining any of them: the three
        # models then run concurrently and the bundle costs max(), not sum().
        # Same cache-checked enqueue as the single-family routes, so the
        # bundle path gets the LRU for free (a bundle whose text was seen by
        # a prior /encode/dense hits on dense and misses on the other two).
        futures = [
            daemon.submit_cached("dense", (req.text, req.task)),
            daemon.submit_cached("splade", (req.text, req.top_k)),
            daemon.submit_cached("sema", (req.text,)),
        ]
        dense, splade, sema = _join(futures, "/encode/bundle")
        return {"dense": dense, "splade": splade, "sema": sema}

    @app.post("/encode/rerank")
    def encode_rerank(req: RerankRequest) -> Dict[str, Any]:
        _require_ready(daemon)
        if not req.texts:
            return {"scores": [], "model": daemon.registry.rerank_model_name}
        futures = [daemon.submit_cached("rerank", (req.query, text)) for text in req.texts]
        return {
            "scores": _join(futures, "/encode/rerank"),
            "model": daemon.registry.rerank_model_name,
        }

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
