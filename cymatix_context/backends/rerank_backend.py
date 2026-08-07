"""Cross-encoder rerank scorer (#341). In-process core + (Task 9) remote branch.

Pure (query, texts) -> scores. Gene-aware adaptation lives in the store seam,
not here. Model: [retrieval] rerank_model, device: [hardware] rerank_device via
resolve_layer_device("rerank"), batch: recommended_batch_size("rerank").
"""
from __future__ import annotations
import logging, threading, time
from typing import List, Optional, Tuple

from ..hardware import resolve_layer_device, recommended_batch_size
# record_model_load is imported INSIDE _get_scorer, exactly like splade_backend.py:138:
#   from cymatix_context.telemetry import record_model_load
# (there is NO telemetry.otel_metrics module; a wrong module-level import here would make
# every import of rerank_backend raise, and the store's fallback would silently eat it)

log = logging.getLogger(__name__)

DEFAULT_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
_MAX_LENGTH = 256   # same truncation DeBERTaRibosome.re_rank uses; probe-cost-faithful

_lock = threading.Lock()
_scorer: Optional[Tuple[object, object, str]] = None   # (tokenizer, model, device_str)
_scorer_model_name: Optional[str] = None

def reset_for_test() -> None:
    global _scorer, _scorer_model_name
    with _lock:
        _scorer, _scorer_model_name = None, None

def _recommended_batch() -> int:
    return recommended_batch_size("rerank")

def _get_scorer(model_name: Optional[str] = None):
    global _scorer, _scorer_model_name
    name = model_name or DEFAULT_RERANK_MODEL
    with _lock:
        if _scorer is not None and _scorer_model_name == name:
            return _scorer
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        from cymatix_context.telemetry import record_model_load
        t0 = time.monotonic()
        device = resolve_layer_device("rerank")
        tok = AutoTokenizer.from_pretrained(name)
        model = AutoModelForSequenceClassification.from_pretrained(name)
        model.to(torch.device(device))
        model.eval()
        record_model_load("rerank", time.monotonic() - t0)   # SECONDS — otel.py:826 does *1000 itself
        _scorer, _scorer_model_name = (tok, model, device), name
        return _scorer

def score_pairs(query: str, texts: List[str], *, model_name: Optional[str] = None,
                batch_size: Optional[int] = None) -> List[float]:
    if not texts:
        return []
    # Task 9 inserts the remote branch here.
    tok, model, device = _get_scorer(model_name)
    import torch
    bs = batch_size or _recommended_batch()
    out: List[float] = []
    with torch.inference_mode():
        for i in range(0, len(texts), bs):
            chunk = texts[i:i + bs]
            enc = tok([query] * len(chunk), chunk, truncation=True,
                      max_length=_MAX_LENGTH, padding=True, return_tensors="pt").to(device)
            logits = model(**enc).logits
            out.extend(torch.sigmoid(logits.squeeze(-1)).cpu().tolist())
    return out
