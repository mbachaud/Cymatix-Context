"""Phase 2 of issue #493 -- pure eval checks over packet trace records.

Pure functions: no I/O, no LLM calls. Inputs are packet-event dicts as
written by ``trace.build_packet_event`` (schema v0, see
``docs/specs/packet-trace-v0.md``):

- ``packet["chunks"]``: ordered list of ``{"chunk_id", "doc_id", "rank", ...}``.
  Delivered order is list order; ``chunk_id`` is the gene id.
- ``packet["verdict"]``: ``{"kind": "know" | "miss" | None, "reason"?,
  "confidence"?}``. ``abstain`` is not a separate kind; it is treated as miss.

Action events are a Phase 3 schema that does not exist yet. ``over_delivery``
assumes each action event carries ``referenced_chunk_ids``: a list of chunk
ids the action used (ids, not text). Replace the key if Phase 3 names it
differently.

Answer records (for ``check_know_miss``) are dicts with ``abstained: bool``.
"""

from __future__ import annotations

import random
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence


def check_delivery(packet: Dict[str, Any], gold_chunk_ids: Iterable[str]) -> Dict[str, Any]:
    """Return ``{"hit": bool, "rank": int | None}``.

    ``rank`` is the 1-based position in delivered order (list position of
    ``packet["chunks"]``) of the first chunk whose ``chunk_id`` is gold. The
    stored ``rank`` field is not used, so the check does not depend on how
    the pipeline numbered its output.
    """
    gold = set(gold_chunk_ids)
    for pos, chunk in enumerate(packet.get("chunks") or [], start=1):
        if chunk.get("chunk_id") in gold:
            return {"hit": True, "rank": pos}
    return {"hit": False, "rank": None}


def check_know_miss(packet: Dict[str, Any], answer_record: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``{"compliant": bool, "reason": str}`` for the know/miss contract.

    - verdict ``know`` -> compliant.
    - verdict ``miss`` and the answer abstained -> compliant (abstain is miss).
    - verdict ``miss`` and a confident answer -> noncompliant ``answered_on_miss``.
    - no verdict (``kind`` absent or None) -> noncompliant ``no_verdict``: the
      contract was not emitted, so it cannot be checked as compliant.
    """
    kind = (packet.get("verdict") or {}).get("kind")
    if kind == "know":
        return {"compliant": True, "reason": "know"}
    if kind == "miss":
        if answer_record.get("abstained"):
            return {"compliant": True, "reason": "miss_abstained"}
        return {"compliant": False, "reason": "answered_on_miss"}
    return {"compliant": False, "reason": "no_verdict"}


def over_delivery(packet: Dict[str, Any],
                  action_events: Optional[List[Dict[str, Any]]]) -> Optional[float]:
    """Fraction of delivered chunk ids not referenced by any action.

    Returns ``None`` when ``action_events`` is None or empty (no evidence of
    use, so no ratio). Returns ``0.0`` when nothing was delivered. Otherwise
    the value is ``|delivered - referenced| / |delivered|`` over unique ids,
    where ``referenced`` is the union of each action's
    ``referenced_chunk_ids`` (assumed key, see module docstring).
    """
    if not action_events:
        return None
    delivered = {c.get("chunk_id") for c in (packet.get("chunks") or [])}
    delivered.discard(None)
    if not delivered:
        return 0.0
    referenced: set = set()
    for ev in action_events:
        referenced.update(ev.get("referenced_chunk_ids") or [])
    unused = delivered - referenced
    return len(unused) / len(delivered)


def classify_residue(results: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Route per-query results into ``clear_pass`` / ``clear_fail`` / ``ambiguous``.

    Each result is a dict with ``"delivery"`` (output of ``check_delivery``,
    needs ``hit``) and ``"know_miss"`` (output of ``check_know_miss``, needs
    ``compliant``). Routing:

    - ``clear_pass``: gold delivered (``hit``) and know/miss compliant.
    - ``clear_fail``: gold not delivered (``hit`` false) and know/miss
      noncompliant.
    - ``ambiguous``: every other combination (the two signals disagree).
      These need the judge.

    The lists hold the original result dicts, in input order.
    """
    out: Dict[str, List[Dict[str, Any]]] = {"clear_pass": [], "clear_fail": [], "ambiguous": []}
    for r in results:
        hit = bool((r.get("delivery") or {}).get("hit"))
        compliant = bool((r.get("know_miss") or {}).get("compliant"))
        if hit and compliant:
            out["clear_pass"].append(r)
        elif (not hit) and (not compliant):
            out["clear_fail"].append(r)
        else:
            out["ambiguous"].append(r)
    return out


def sample_for_judge(ids: Sequence[str], ratio: float, seed: int) -> List[str]:
    """Deterministic sample of ``round(ratio * len(ids))`` ids.

    The input is sorted first, so the result does not depend on input order.
    Uses a private ``random.Random(seed)``. The returned list is sorted.
    """
    if not 0.0 <= ratio <= 1.0:
        raise ValueError(f"ratio must be in [0, 1], got {ratio!r}")
    pool = sorted(ids)
    k = round(ratio * len(pool))
    rng = random.Random(seed)
    return sorted(rng.sample(pool, k))


def raw_agreement(a: Sequence[Any], b: Sequence[Any]) -> float:
    """Fraction of positions where ``a[i] == b[i]``."""
    _check_pair(a, b)
    same = sum(1 for x, y in zip(a, b) if x == y)
    return same / len(a)


def cohens_kappa(a: Sequence[Any], b: Sequence[Any]) -> float:
    """Cohen's kappa for two raters over the same items.

    ``kappa = (po - pe) / (1 - pe)``, where ``po`` is raw agreement and ``pe``
    the chance agreement from each rater's marginal label frequencies. When
    ``pe == 1`` (both raters use one identical label) the result is 1.0.
    """
    _check_pair(a, b)
    n = len(a)
    po = raw_agreement(a, b)
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[label] / n) * (cb[label] / n) for label in set(ca) | set(cb))
    if pe == 1.0:
        return 1.0
    return (po - pe) / (1.0 - pe)


def _check_pair(a: Sequence[Any], b: Sequence[Any]) -> None:
    if len(a) != len(b):
        raise ValueError(f"length mismatch: {len(a)} != {len(b)}")
    if not a:
        raise ValueError("inputs must be non-empty")
