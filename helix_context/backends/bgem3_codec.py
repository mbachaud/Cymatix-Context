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
import numpy as np

log = logging.getLogger("helix.bgem3")

_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# BGE-M3 published Matryoshka breakpoints. Other dims technically work but
# were never released as official truncations; we log a warn so anyone who
# tries dim=256 again sees the calibration risk.
_SANCTIONED_DIMS = frozenset({1024, 768, 512})


class BGEM3Codec:
    # Track which non-sanctioned dim values we've already warned for so we
    # don't spam the log on every re-instantiation in long-running processes.
    _UNSANCTIONED_DIM_WARNED: set[int] = set()

    def __init__(self, dim: int = 1024, device: str = "cpu", model_name: str = "BAAI/bge-m3"):
        self.dim = dim
        self.model_name = model_name
        self._device = device
        self._model = None
        if dim not in _SANCTIONED_DIMS and dim not in BGEM3Codec._UNSANCTIONED_DIM_WARNED:
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
        try:
            from FlagEmbedding import BGEM3FlagModel
            self._model = BGEM3FlagModel(self.model_name, use_fp16=False)
            self._backend = "flagembedding"
        except ImportError:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name, device=self._device)
            self._backend = "sentence_transformers"
        log.info("BGE-M3 loaded (%s) dim=%d", self._backend, self.dim)

    def encode(self, text: str, task: str = "passage") -> list[float]:
        """Encode text to a self.dim-dimensional float list.

        task='query': prepends retrieval instruction prefix.
        task='passage': bare (no prefix).
        """
        self._load()
        if task == "query":
            text = _QUERY_PREFIX + text
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

    def similarity(self, vec_a: list[float], vec_b: list[float]) -> float:
        a = np.array(vec_a, dtype=np.float32)
        b = np.array(vec_b, dtype=np.float32)
        return float(np.dot(a, b))
