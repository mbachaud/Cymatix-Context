"""Cross-encoder rerank scorer (#341). In-process core + (Task 9) remote branch.

Pure (query, texts) -> scores. Gene-aware adaptation lives in the store seam,
not here. Model: [retrieval] rerank_model, device: [hardware] rerank_device via
resolve_layer_device("rerank"), batch: recommended_batch_size("rerank").
"""
from __future__ import annotations
import logging, threading, time
from typing import List, Optional, Tuple

from ..hardware import resolve_layer_device, recommended_batch_size
from . import encoder_client
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

# Encoder-daemon transport (#341 Task 9), as a single ``(url, client)`` tuple
# so the rebind on a URL change is atomic — same lock-free module state as
# splade_backend._remote_client (a benign race just rebuilds a cheap stdlib
# client). ``None`` until the first remote encode; stays ``None`` forever when
# no daemon URL is configured.
_remote_client = None


def reset_for_test() -> None:
    global _scorer, _scorer_model_name, _remote_client
    with _lock:
        _scorer, _scorer_model_name = None, None
    _remote_client = None


def _remote_scores(
    query: str, texts: List[str], timeout_s: Optional[float] = None,
) -> Optional[List[float]]:
    """Daemon-scored cross-encoder scores, or ``None`` meaning "run the local body".

    Mirrors ``splade_backend._remote_sparse`` exactly: reached BEFORE
    ``_get_scorer()`` / ``import torch`` in ``score_pairs``, so a daemon
    serving this process lets rerank run on a host with no transformers
    installed at all.

    Any failure (transport, malformed payload, wrong item count, non-numeric
    score) records the failure on the shared circuit breaker and returns
    ``None``, so the caller re-scores in process. ``score_pairs`` must NEVER
    raise out of this branch — every failure degrades to the in-process
    fallback instead.
    """
    url = encoder_client.active_url()
    if not url:
        return None
    # No empty-texts guard: score_pairs already returns [] before calling this,
    # and this function's sole caller is score_pairs.
    if not encoder_client.should_attempt():
        return None

    global _remote_client
    cached = _remote_client
    if cached is None or cached[0] != url:
        client = encoder_client.EncoderClient(
            url, timeout_s=encoder_client.default_timeout_s(),
        )
        _remote_client = (url, client)
    else:
        client = cached[1]

    # Shared one-shot readiness gate: rerank joins dense/SPLADE/SEMA on the
    # same process-wide gate (see encoder_client.ready_gate's docstring) so a
    # cold daemon opens the circuit exactly once, not once per seam.
    if not encoder_client.ready_gate(client):
        return None

    try:
        scores = client.encode_rerank(query, list(texts), timeout_s=timeout_s)
    except Exception as exc:  # EncoderClientError + any stray transport error
        encoder_client.record_failure()
        log.debug("remote rerank encode failed (%s); scoring in process", exc)
        return None
    if not isinstance(scores, list) or len(scores) != len(texts):
        encoder_client.record_failure()
        log.debug("remote rerank returned an unusable payload; scoring in process")
        return None
    try:
        scores = [float(s) for s in scores]
    except (TypeError, ValueError) as exc:
        encoder_client.record_failure()
        log.debug(
            "remote rerank returned non-numeric scores (%s); scoring in process", exc,
        )
        return None
    encoder_client.record_success()
    return scores

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
    remote = _remote_scores(query, texts, timeout_s=encoder_client.batch_timeout_s(len(texts)))
    if remote is not None:
        return remote
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
