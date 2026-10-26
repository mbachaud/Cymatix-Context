"""Optional System One packet judge (EXPERIMENTAL, default off).

A "System One" model takes typed questions about a state and returns
calibrated probabilities, not generated text. TypeSafe's Jev is the reference
provider (``POST {model, state, questions}`` -> ``{model, answers, usage}``).
This module asks two things of a delivered /context/packet:

* the question type, a Choice over ``QUESTION_SHEET`` (the type is passed
  back into the packet question, as in the measured configuration);
* ``packet_answers``, a Noul: does any item in the packet state the answer?

With ``rerank`` it also asks, per item, whether that item states the answer,
and reorders items inside each evidence bucket by that probability (stable,
so ties keep the cymatix order). Items are never dropped and the know/miss
contract is never touched.

The request carries only the question, the question type and each item's
source and text. Cymatix rankings are deliberately left out: the paired ERB
947k ablation measured packet know/miss AUROC 0.906 with them vs 0.900
without (delta CI [-0.015, +0.004]), so they add a request-size cost and no
measured signal. See docs/benchmarks/2026-10-23-jev-system-one-packet-judge.md.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# Question type -> the type's description (same sheet the receipts used).
QUESTION_SHEET: Dict[str, str] = {
    "config_value": "The question asks for a configured value, default, port, path or setting.",
    "code_location": "The question asks where something is implemented or which "
                     "file/function does something.",
    "mechanism": "The question asks how or why a component works.",
    "history_decision": "The question asks what changed, when, or why a decision was made.",
    "fact_lookup": "The question asks for a named fact, number, or entity not covered "
                   "by the other types.",
}

_RETRY_STATUS = (429, 529)


class SystemOneError(RuntimeError):
    """A provider call failed or the client could not be configured."""


class SystemOneClient:
    """Minimal paced HTTP client for a System One endpoint."""

    def __init__(self, url: str, model: str, api_key: str, *,
                 timeout_s: float = 30.0, min_interval_s: float = 0.3,
                 max_retries: int = 4) -> None:
        self.url = url
        self.model = model
        self._api_key = api_key
        self.timeout_s = timeout_s
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self._lock = threading.Lock()
        self._last_call = 0.0

    def ask(self, state: dict, questions: dict) -> dict:
        body = json.dumps({"model": self.model, "state": state,
                           "questions": questions}).encode("utf-8")
        for attempt in range(self.max_retries + 1):
            with self._lock:
                wait = self.min_interval_s - (time.monotonic() - self._last_call)
                if wait > 0:
                    time.sleep(wait)
                self._last_call = time.monotonic()
            req = urllib.request.Request(
                self.url, data=body, method="POST",
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                if exc.code in _RETRY_STATUS and attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise SystemOneError(f"System One HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise SystemOneError(f"System One unreachable: {exc!r}") from exc
            except ValueError as exc:  # non-JSON body
                raise SystemOneError(f"System One returned a non-JSON body: {exc}") from exc
        raise SystemOneError("System One retries exhausted")


def client_from_config(cfg) -> SystemOneClient:
    """Build a client from ``SystemOneConfig``; the key comes from the env."""
    api_key = os.environ.get(cfg.api_key_env, "")
    if not api_key:
        raise SystemOneError(f"{cfg.api_key_env} is not set")
    return SystemOneClient(cfg.url, cfg.model, api_key, timeout_s=cfg.timeout_s,
                           min_interval_s=cfg.min_interval_s)


def _qtype_questions() -> dict:
    return {"qtype": {"type": "choice",
                      "instructions": "Which type of question is this?",
                      "criteria": dict(QUESTION_SHEET)}}


def _packet_questions() -> dict:
    return {"packet_answers": {
        "type": "noul",
        "instructions": "The retrieval system delivered this packet of chunks for the "
                        "question. Does any chunk in the packet state the answer to the "
                        "question?",
        "criteria": {
            "true": "At least one chunk states the answer directly.",
            "false": "No chunk states the answer; the packet only covers the topic, "
                     "the entity's other attributes, or other entities.",
        },
    }}


def _item_questions() -> dict:
    return {"answers": {
        "type": "noul",
        "instructions": "Does the chunk state the answer to the question?",
        "criteria": {
            "true": "The answer can be read directly from the chunk.",
            "false": "The answer is absent, only implied, or only the topic is mentioned.",
        },
    }}


def _view(item: dict, max_chars: int) -> dict:
    return {"source": item.get("source_id") or "",
            "text": (item.get("content") or "")[:max_chars]}


def qualify_packet(client, query: str, buckets: Dict[str, List[dict]], *,
                   know_threshold: float, rerank: bool, max_item_chars: int) -> dict:
    """Judge a packet; with ``rerank``, reorder each bucket's items in place.

    ``buckets`` maps a bucket name (``verified``, ``stale_risk``) to its list
    of serialized ContextItem dicts. Returns the ``system_one`` block.
    """
    items = [(name, it) for name, its in buckets.items() for it in its]
    if not items:
        return {"available": True, "model": None, "question_type": None,
                "answers_p": 0.0, "verdict": "miss", "threshold": know_threshold,
                "reranked": False, "calls": 0}
    models: List[Optional[str]] = []
    calls = 0

    def ask(state, questions):
        nonlocal calls
        calls += 1
        resp = client.ask(state, questions)
        models.append(resp.get("model"))
        return resp["answers"]

    qtype = ask({"question": query}, _qtype_questions())["qtype"]["choice"]
    packet_p = float(ask({"question": query, "question_type": qtype,
                          "packet": [_view(it, max_item_chars) for _, it in items]},
                         _packet_questions())["packet_answers"]["noul"])
    block = {"available": True, "model": next((m for m in models if m), None),
             "question_type": qtype, "answers_p": packet_p,
             "verdict": "know" if packet_p >= know_threshold else "miss",
             "threshold": know_threshold, "reranked": False, "calls": calls}
    if rerank:
        # Score every item before touching any bucket, so a failed call
        # leaves the packet in its cymatix order.
        scored, probs = [], {}
        for name, it in items:
            p = float(ask({"question": query, "question_type": qtype,
                           "chunk": _view(it, max_item_chars)},
                          _item_questions())["answers"]["noul"])
            scored.append({"gene_id": it.get("gene_id"), "bucket": name, "answers_p": p})
            probs[id(it)] = p
        for its in buckets.values():
            its.sort(key=lambda it: -probs[id(it)])  # stable: ties keep cymatix order
        block.update(reranked=True, items=scored, calls=calls)
    return block


def compute_system_one_block(cfg, query: str, payload: dict) -> dict:
    """Route helper: soft-fails to ``{"available": False, "error": ...}``."""
    buckets = {k: payload[k] for k in ("verified", "stale_risk") if isinstance(payload.get(k), list)}
    try:
        client = client_from_config(cfg)
        return qualify_packet(client, query, buckets, know_threshold=cfg.know_threshold,
                              rerank=cfg.rerank_items, max_item_chars=cfg.max_item_chars)
    except SystemOneError as exc:
        log.warning("system_one judge unavailable: %s", exc)
        return {"available": False, "error": str(exc)}
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("system_one judge returned an unexpected shape: %r", exc)
        return {"available": False, "error": f"unexpected response shape: {exc!r}"}
