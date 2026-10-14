"""BGE-M3 dense encoder (Step 4, 2026-05-08; Stage 2 promoted dim=1024 default 2026-05-08).

Wraps BAAI/bge-m3 via sentence-transformers (preferred) or FlagEmbedding.
Asymmetric: query task prepends an instruction prefix; passage task is bare.
Matryoshka truncation: output is sliced to `dim` and L2-renormalized.

Stage 2: default dim is now 1024 (full BGE-M3). Truncation is permitted only
at sanctioned Matryoshka breakpoints (BGE-M3 published: 1024 / 768 / 512). Other
dims log a one-time warn — sub-256 truncation collapsed random-pair cosine to
~0.6 in practice (the dim=256 collapse), which is why Stage 2 reverted to
full-dim recall.
"""
from __future__ import annotations
import logging
import os
import threading

import numpy as np

from . import encoder_client

log = logging.getLogger("cymatix.bgem3")

_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# BGE-M3 published Matryoshka breakpoints. Other dims technically work but
# were never released as official truncations; we log a warn so anyone who
# tries dim=256 again sees the calibration risk.
_SANCTIONED_DIMS = frozenset({1024, 768, 512})

# EmbeddingGemma 2 (google/embeddinggemma-2, opt-in via ``[retrieval]
# dense_model``): 768-d native, MRL breakpoints 768 / 512 / 256 / 128. It is
# trained with task prefixes on BOTH sides (query and document), loads its
# vision/audio encoders unless told not to, and must not run in float16
# (activations overflow to NaN), so it gets its own load and prompt path.
_GEMMA_QUERY_PREFIX = "task: search result | query: "
_GEMMA_DOC_PREFIX = "title: none | text: "
_GEMMA_SANCTIONED_DIMS = frozenset({768, 512, 256, 128})


def is_embeddinggemma(model_name: str) -> bool:
    return "embeddinggemma" in (model_name or "").lower()


# Floors for google/embeddinggemma-2: the checkpoint declares
# sentence-transformers>=6.1.0, its model type (embedding_gemma2) is unknown to
# older transformers releases, and its processor imports torchvision even for a
# text-only load. Installed by the ``gemma`` extra.
_GEMMA_FLOORS = {"sentence-transformers": "6.1.0", "transformers": "5.19.0", "torchvision": None}


def check_embeddinggemma_stack() -> None:
    """Raise a clear error when the installed stack cannot load EmbeddingGemma 2."""
    from importlib.metadata import PackageNotFoundError, version

    from packaging.version import Version

    problems = []
    for pkg, floor in _GEMMA_FLOORS.items():
        try:
            have = version(pkg)
        except PackageNotFoundError:
            problems.append(f"{pkg} is not installed")
            continue
        if floor and Version(have) < Version(floor):
            problems.append(f"{pkg} {have} < {floor}")
    if problems:
        raise RuntimeError(
            "EmbeddingGemma 2 needs a newer model stack (" + "; ".join(problems)
            + '). Install it with: pip install "cymatix-context[gemma]"'
        )

# Passage encode length cap. BGE-M3 has max_length=512 tokens; ~2k chars is a
# safe upper bound. Both the inline ingest path (knowledge_store.upsert_doc /
# context_manager.ingest) and the offline backfill script bound passages to
# this so the two encodings stay byte-identical. See PR-1 of the 2026-05-16
# Tier-0 plan.
#
# #207 dense fast-follow (2026-07-10): configurable via
# ``[ingestion] dense_passage_char_cap`` (default 2000, byte-identical to this
# module constant). This constant remains the default anchor when a caller has
# no loaded config (e.g. a bare ``BGEM3Codec`` used outside the pipeline);
# every in-pipeline caller (context_manager, knowledge_store,
# scripts/backfill_bgem3_v2.py) threads the config value instead of reading
# this constant directly.
PASSAGE_CHAR_CAP = 2000


def vec_to_blob(vec, dim: int) -> bytes:
    """Pack a float vector as a raw little-endian fp32 BLOB of ``dim*4`` bytes.

    This is the single canonical encoding for the ``genes.embedding_dense_v2``
    column. Both ``scripts/backfill_bgem3_v2.py`` and
    ``knowledge_store.upsert_doc`` call it so the inline-ingest write and the
    offline-backfill write cannot drift — a genome built through the ingest
    path must satisfy the backfill script's ``length(blob) == dim*4``
    idempotency skip-clause.
    """
    arr = np.asarray(vec, dtype="<f4")
    if arr.ndim != 1 or arr.shape[0] != dim:
        raise ValueError(
            f"vector dim {getattr(arr, 'shape', None)} != expected ({dim},)"
        )
    return arr.tobytes(order="C")


def _vram_release_interval() -> int:
    """Release torch's CUDA caching-allocator cache every N batched encodes (0 = never).

    torch's CUDA allocator keeps a separate cached block per distinct input shape,
    so a long-lived process that batch-encodes many differently-sized passages — the
    daemon ``/ingest`` route, ``scripts/backfill_bgem3_v2.py``, a 100k+-file genome
    build — climbs to the card's VRAM ceiling and then spills to shared system memory
    (measured: ~11.7 GB / 95% of a 12 GB 3080 Ti on a single-worker dense ingest of one
    mid-size repo, vs a ~6 GB plateau once the cache is released periodically). A
    periodic ``empty_cache()`` returns only *unused* cached blocks, so emitted vectors
    are byte-identical and only a cheap ``cudaFree`` is paid every N documents. CPU
    encoding has no CUDA cache and is unaffected. Tune/disable via the env var.
    """
    try:
        return max(0, int(os.environ.get("CYMATIX_DENSE_VRAM_RELEASE_EVERY", "256")))
    except ValueError:
        return 256


class BGEM3Codec:
    # Track which non-sanctioned dim values we've already warned for so we
    # don't spam the log on every re-instantiation in long-running processes.
    _UNSANCTIONED_DIM_WARNED: set[int] = set()

    def __init__(self, dim: int = 1024, device: str = "cpu", model_name: str = "BAAI/bge-m3"):
        self.dim = dim
        self.model_name = model_name
        self._device = device
        self._model = None
        # Number of batched encodes since construction; drives the periodic CUDA-cache
        # release that bounds VRAM during long ingest runs (see ``_maybe_release_vram``).
        self._encode_batch_calls = 0
        # Guards the lazy model load. When this codec is shared across
        # concurrent shard-fan-out workers (the A1 singleton), two threads can
        # hit ``_load`` simultaneously on first use; without the lock both
        # would build a ~2 GB model. Double-checked locking keeps it to one.
        self._load_lock = threading.Lock()
        self._load_error: Exception | None = None
        self._gemma = is_embeddinggemma(model_name)
        sanctioned = _GEMMA_SANCTIONED_DIMS if self._gemma else _SANCTIONED_DIMS
        if dim not in sanctioned and dim not in BGEM3Codec._UNSANCTIONED_DIM_WARNED:
            log.warning(
                "BGEM3Codec(dim=%d) is not a sanctioned BGE-M3 Matryoshka breakpoint "
                "(supported: 1024 / 768 / 512). Random-pair cosine may not be near 0; "
                "thresholds calibrated at 1024-dim will not transfer.",
                dim,
            )
            BGEM3Codec._UNSANCTIONED_DIM_WARNED.add(dim)

    def _load(self) -> None:
        if self._model is not None:
            return
        self._raise_if_load_failed()
        # Double-checked locking: the fast path above is lock-free once loaded;
        # the slow path serializes concurrent first-loads so a shared codec
        # (A1 singleton) under fan-out builds the ~2 GB model exactly once.
        with self._load_lock:
            if self._model is not None:
                return
            self._raise_if_load_failed()
            import time as _time
            _load_t0 = _time.monotonic()
            try:
                model = self._build_model()
            except Exception as exc:
                # Remember the failure: without this every encode call (one per
                # ingested document) re-ran model resolution and failed again.
                self._load_error = exc
                log.error(
                    "dense model %s failed to load; dense encoding is disabled for "
                    "this process (not retried, restart to try again): %s",
                    self.model_name, exc,
                )
                raise
            # Publish self._model last so the lock-free fast path never sees a
            # half-initialized model (self._backend is set before this).
            self._model = model
            # Perf slice 1 (W0.2): THIS is where the ~2 GB weight load
            # actually happens (construction is metadata-only) — record it
            # so cold-start receipts can subtract it from express.
            try:
                from ..telemetry import record_model_load
                record_model_load("dense", _time.monotonic() - _load_t0)
            except Exception:
                pass
            log.info("%s loaded (%s) dim=%d", self.model_name, self._backend, self.dim)

    def _raise_if_load_failed(self) -> None:
        if self._load_error is not None:
            raise RuntimeError(
                f"dense model {self.model_name} failed to load earlier in this process"
            ) from self._load_error

    def _build_model(self):
        if self._gemma:
            check_embeddinggemma_stack()
            import torch
            from sentence_transformers import SentenceTransformer
            bf16 = (
                self._device == "cuda"
                and torch.cuda.is_available()
                and torch.cuda.is_bf16_supported()
            )
            self._backend = "sentence_transformers"
            return SentenceTransformer(
                self.model_name, device=self._device,
                model_kwargs={"torch_dtype": torch.bfloat16 if bf16 else torch.float32},
                config_kwargs={"vision_config": None, "audio_config": None},
            )
        try:
            from FlagEmbedding import BGEM3FlagModel
            self._backend = "flagembedding"
            return BGEM3FlagModel(self.model_name, use_fp16=False)
        except ImportError:
            from sentence_transformers import SentenceTransformer
            self._backend = "sentence_transformers"
            return SentenceTransformer(self.model_name, device=self._device)

    def encode(self, text: str, task: str = "passage") -> list[float]:
        """Encode text to a self.dim-dimensional float list.

        task='query': prepends retrieval instruction prefix.
        task='passage': bare (no prefix).
        """
        self._load()
        text = self._prepare([text], task)[0]
        if self._backend == "flagembedding":
            raw = self._model.encode([text], batch_size=1, max_length=512)["dense_vecs"]
            vec = np.array(raw[0], dtype=np.float32)
        else:
            vec = np.array(
                self._model.encode(text, normalize_embeddings=True, show_progress_bar=False),
                dtype=np.float32,
            )
        # Matryoshka truncate + re-normalize
        vec = vec[: self.dim]
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.tolist()

    def encode_batch(self, texts: list[str], task: str = "passage") -> list[list[float]]:
        """Encode many texts in one model call. Same contract as ``encode``.

        Tier-0 PR-1 (2026-05-16): the ingest path encodes every strand of a
        document in a single batched call rather than one ``encode`` per
        strand. Each output row is Matryoshka-truncated to ``self.dim`` and
        L2-renormalised exactly as ``encode`` does, so a vector produced here
        is byte-identical to one produced by ``encode`` for the same text.

        task='query' prepends the retrieval instruction prefix to every text;
        task='passage' is bare.
        """
        if not texts:
            return []
        self._load()
        prepared = self._prepare(texts, task)
        if self._backend == "flagembedding":
            raw = self._model.encode(prepared, max_length=512)["dense_vecs"]
            mat = np.asarray(raw, dtype=np.float32)
        else:
            mat = np.asarray(
                self._model.encode(
                    prepared, normalize_embeddings=True, show_progress_bar=False,
                ),
                dtype=np.float32,
            )
        # #209 phase 2 / roadmap §3b-9: sample VRAM once per ingest batch
        # so dense-ingest memory pressure (the #176/#177 OOM class) is a
        # Grafana series, not a post-mortem. No-op without torch/CUDA.
        try:
            from ..telemetry import ingest_vram_gauge
            import torch  # type: ignore[import-not-found]
            if torch.cuda.is_available():
                ingest_vram_gauge().set(float(torch.cuda.memory_allocated()))
        except Exception:  # pragma: no cover
            pass
        # Matryoshka truncate + per-row L2 renormalise.
        mat = mat[:, : self.dim]
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        mat = mat / norms
        self._encode_batch_calls += 1
        self._maybe_release_vram()
        return mat.tolist()

    def _prepare(self, texts: list[str], task: str) -> list[str]:
        """Apply the model family's task prefix (BGE-M3: query only)."""
        if self._gemma:
            prefix = _GEMMA_QUERY_PREFIX if task == "query" else _GEMMA_DOC_PREFIX
            return [prefix + t for t in texts]
        if task == "query":
            return [_QUERY_PREFIX + t for t in texts]
        return list(texts)

    def _maybe_release_vram(self) -> None:
        """Periodically free torch's CUDA caching-allocator cache during batch ingest.

        See ``_vram_release_interval`` for why this is needed. No-op on CPU (no CUDA
        cache) or when ``CYMATIX_DENSE_VRAM_RELEASE_EVERY=0``. Best-effort: a failure to
        free is logged at debug and never interrupts ingest.
        """
        if self._device != "cuda":
            return
        every = _vram_release_interval()
        if not every or (self._encode_batch_calls % every):
            return
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            log.debug("torch.cuda.empty_cache() failed during dense ingest", exc_info=True)

    def similarity(self, vec_a: list[float], vec_b: list[float]) -> float:
        a = np.array(vec_a, dtype=np.float32)
        b = np.array(vec_b, dtype=np.float32)
        return float(np.dot(a, b))


# ── A1: process-wide codec singleton ────────────────────────────────────
#
# Before this, every shard's KnowledgeStore lazy-built its OWN BGEM3Codec, so a
# 100-shard query loaded up to ~100 copies of the ~2 GB BGE-M3 model — the
# dominant driver of the daemon's 47 GB-on-disk → ~120 GB-resident ramp. One
# shared instance per (model_name, dim, device) loads the weights once and every
# shard reuses them. Inference (``encode``) is stateless/read-only, so sharing
# one model across concurrent fan-out workers is safe; the per-instance
# ``_load_lock`` serializes the one-time lazy load.
_GLOBAL_CODECS: dict[tuple, "BGEM3Codec"] = {}
_GLOBAL_CODEC_LOCK = threading.Lock()


def shared_dense_codec_enabled() -> bool:
    """Whether to share one BGE-M3 codec process-wide (the A1 fix).

    Default ON. Set ``CYMATIX_SHARE_DENSE_CODEC=0`` to reproduce the legacy
    per-shard-instance behavior (for an A/B on the RAM impact).
    """
    return os.environ.get("CYMATIX_SHARE_DENSE_CODEC", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def get_shared_codec(
    dim: int = 1024,
    device: str = "cpu",
    model_name: str = "BAAI/bge-m3",
    *,
    share: bool = True,
) -> "BGEM3Codec":
    """Return a process-shared ``BGEM3Codec`` keyed by (model_name, dim, device).

    ``share=False`` bypasses the cache and builds a fresh per-call instance
    (legacy behavior / A-B testing). The construction lock guards only the
    cache insert; the ~2 GB model still loads lazily on first ``encode`` and is
    guarded by the instance's own ``_load_lock``.

    Fork 1 slice 1: when an encoder-daemon URL is active the cached object is
    a ``RemoteBGEM3Codec`` instead — same cache key, same ``share=False``
    bypass, same one-instance-per-key guarantee, and the same duck-type
    (``encode`` / ``encode_batch`` / ``similarity`` / ``dim`` /
    ``model_name`` / ``_model``). With no URL configured this function is
    byte-identical to its pre-daemon behavior: same class, same cache, same
    warnings.
    """
    url = encoder_client.active_url()
    if url:
        if not share:
            return encoder_client.RemoteBGEM3Codec(
                dim=dim, device=device, model_name=model_name, url=url,
            )
        key = (model_name, dim, device)
        codec = _GLOBAL_CODECS.get(key)
        if codec is not None:
            return codec
        with _GLOBAL_CODEC_LOCK:
            codec = _GLOBAL_CODECS.get(key)
            if codec is None:
                codec = encoder_client.RemoteBGEM3Codec(
                    dim=dim, device=device, model_name=model_name, url=url,
                )
                _GLOBAL_CODECS[key] = codec
            return codec

    if not share:
        return BGEM3Codec(dim=dim, device=device, model_name=model_name)
    key = (model_name, dim, device)
    codec = _GLOBAL_CODECS.get(key)
    if codec is not None:
        return codec
    with _GLOBAL_CODEC_LOCK:
        codec = _GLOBAL_CODECS.get(key)
        if codec is None:
            codec = BGEM3Codec(dim=dim, device=device, model_name=model_name)
            _GLOBAL_CODECS[key] = codec
        return codec
