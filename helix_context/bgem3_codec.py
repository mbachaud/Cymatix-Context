"""BGE-M3 dense encoder (Step 4, 2026-05-08).

Wraps BAAI/bge-m3 via sentence-transformers (preferred) or FlagEmbedding.
Asymmetric: query task prepends an instruction prefix; passage task is bare.
Matryoshka truncation: output is sliced to `dim` and L2-renormalized.
"""
from __future__ import annotations
import logging
import numpy as np

log = logging.getLogger("helix.bgem3")

_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class BGEM3Codec:
    def __init__(self, dim: int = 256, device: str = "cpu", model_name: str = "BAAI/bge-m3"):
        self.dim = dim
        self.model_name = model_name
        self._device = device
        self._model = None

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
