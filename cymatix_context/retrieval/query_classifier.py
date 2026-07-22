"""Upstream query classifier for the injection router.

Pure-function regex/keyword scan. No I/O, no model calls, infallible by
construction. See docs/specs/2026-04-29-query-classifier-injection-router-design.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# Hard cap to bound work on pasted code/logs.
_QUERY_TRUNCATE_CHARS = 2000


@dataclass(frozen=True)
class ClassifierResult:
    cls: str
    signals_matched: List[str] = field(default_factory=list)
    signal_count: int = 0
    threshold_required: int = 0
    assembly_max_genes_cap: Optional[int] = None
    decoder_mode: Optional[str] = None
    reason: Optional[str] = None


# Operator characters that count as one signal per *distinct* operator class
# present (de-duplicated so a math retrieval with three `+` is still 1 signal).
_OPERATOR_CHARS = ("+", "-", "*", "/", "%")

# Single-word numeric/quantity keywords (lowercase, word-boundary matched).
_NUMERIC_KEYWORDS = ("calculate", "total", "sum")

# Multi-word keywords (lowercase substring match).
_MULTIWORD_KEYWORDS = ("critical path",)

_WH_WORDS = ("who", "what", "where", "when", "which")
_FACTUAL_MAX_WORDS = 15  # strict less-than

_PROCEDURAL_PATTERNS = ("how do i", "how to", "steps", "walk me through")

_MULTIHOP_CONNECTIVES = (
    "and then", "because", "after that", "compare", " vs ",
)
_MULTIHOP_BETWEEN_RE = re.compile(r"\bbetween\s+\S+\s+and\s+\S+", re.IGNORECASE)
_MULTIHOP_LONG_WORDS = 25  # strict greater-than


def _scan_arithmetic(lower: str) -> List[str]:
    """Return the list of arithmetic signal tags matched in `lower`."""
    signals: List[str] = []
    for op in _OPERATOR_CHARS:
        if op in lower:
            signals.append(f"operator:{op}")
    for kw in _NUMERIC_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", lower):
            signals.append(f"keyword:{kw}")
    for kw in _MULTIWORD_KEYWORDS:
        if kw in lower:
            signals.append(f"keyword:{kw}")
    return signals


def _arithmetic_meets_threshold(signals: List[str]) -> bool:
    """Arithmetic fires if:
    - signal_count >= 2, OR
    - >= 1 operator signal AND >= 1 numeric/quantity keyword signal
      (the strong-pair shortcut).
    """
    if len(signals) >= 2:
        return True
    has_op = any(s.startswith("operator:") for s in signals)
    has_kw = any(s.startswith("keyword:") for s in signals)
    return has_op and has_kw


def _is_factual(lower: str) -> Optional[List[str]]:
    """Return signal list if factual; None otherwise.

    Length check is **AND** — both leading wh-word AND `< 15` words required.
    """
    stripped = lower.lstrip()
    leading = stripped.split(maxsplit=1)
    if not leading:
        return None
    first = leading[0].rstrip("?.,!:;")
    if first not in _WH_WORDS:
        return None
    word_count = len(stripped.split())
    if word_count >= _FACTUAL_MAX_WORDS:
        return None
    return [f"wh:{first}", f"length:{word_count}"]


def _scan_procedural(lower: str) -> List[str]:
    return [f"keyword:{p}" for p in _PROCEDURAL_PATTERNS if p in lower]


def _scan_multi_hop(lower: str, raw: str) -> List[str]:
    signals: List[str] = []
    for c in _MULTIHOP_CONNECTIVES:
        if c in lower:
            signals.append(f"connective:{c.strip()}")
    if _MULTIHOP_BETWEEN_RE.search(raw):
        signals.append("connective:between-x-and-y")
    if len(raw.split()) > _MULTIHOP_LONG_WORDS:
        signals.append(f"length:{len(raw.split())}")
    return signals


_DEFAULT = ClassifierResult(cls="default")


# Stage 5 (2026-05-08) — caller_model_class × classifier.cls decoder_mode table.
# Spec §6 (15 cells). The `generic` column is byte-identical to today's
# hard-coded ClassifierResult.decoder_mode values (spec §7 regression table)
# and the byte-identical golden test (test 6) is the live enforcement.
#
# Outer key = classifier.cls. Inner key = caller_model_class. Value is the
# decoder_mode string, or None for "fall back to manager default" (matches
# today's _DEFAULT behavior at line 113).
DECODER_MODE_TABLE: dict[str, dict[str, Optional[str]]] = {
    "arithmetic": {"generic": "minimal",   "small_moe": "answer_slate_only",    "frontier": "minimal"},
    "factual":    {"generic": "condensed", "small_moe": "answer_slate_only",    "frontier": "condensed"},
    "procedural": {"generic": "full",      "small_moe": "condensed_with_slate", "frontier": "full"},
    "multi_hop":  {"generic": "full",      "small_moe": "condensed_with_slate", "frontier": "full"},
    "default":    {"generic": None,        "small_moe": "condensed_with_slate", "frontier": None},
}


# Issue #255 (classifier-gated combinator, 2026-07-12): the canonical set of
# classifier class labels — every ``ClassifierResult.cls`` value that
# ``classify_query`` can return, including the ``default`` no-op. Single source
# of truth for validating the ``[retrieval] rerank_combinator_by_class`` map
# keys at config load. Kept in lockstep with ``DECODER_MODE_TABLE`` (every
# routed class also owns a decoder row) by the assertion below.
VALID_QUERY_CLASSES: Tuple[str, ...] = (
    "arithmetic", "factual", "procedural", "multi_hop", "default",
)
assert set(VALID_QUERY_CLASSES) == set(DECODER_MODE_TABLE), (
    "VALID_QUERY_CLASSES drifted from DECODER_MODE_TABLE"
)


def resolve_decoder_mode(cls: str, caller_model_class: str) -> Optional[str]:
    """Resolve decoder_mode given classifier class and caller_model_class.

    Stage 5 §6 lookup. Unknown cls falls back to the ``default`` row;
    unknown caller_model_class falls back to the ``generic`` column.
    Returns ``None`` for cells where the manager should keep its configured
    default decoder prompt (e.g. generic × default).
    """
    row = DECODER_MODE_TABLE.get(cls) or DECODER_MODE_TABLE["default"]
    if caller_model_class not in row:
        caller_model_class = "generic"
    return row[caller_model_class]


def classify_query(query: Optional[str]) -> ClassifierResult:
    """Classify a query into one of the router classes.

    Always returns a ClassifierResult; never raises. On empty/None input
    or any internal exception, returns the `default` result (no-op).
    """
    if not query:
        return _DEFAULT
    try:
        text = query[:_QUERY_TRUNCATE_CHARS]
        lower = text.lower()

        # Priority 1: arithmetic
        arith_signals = _scan_arithmetic(lower)
        if _arithmetic_meets_threshold(arith_signals):
            return ClassifierResult(
                cls="arithmetic",
                signals_matched=arith_signals,
                signal_count=len(arith_signals),
                threshold_required=2,
                assembly_max_genes_cap=2,
                decoder_mode="minimal",
            )

        # Priority 2: factual
        fact_signals = _is_factual(lower)
        if fact_signals is not None:
            return ClassifierResult(
                cls="factual",
                signals_matched=fact_signals,
                signal_count=len(fact_signals),
                threshold_required=1,
                assembly_max_genes_cap=5,
                decoder_mode="condensed",
            )

        # Priority 3 vs 4: procedural before multi_hop is PROVISIONAL.
        # The ordering between these two is asserted, not benchmarked.
        # See spec section 3 — revisit after first procedural benchmark run.
        proc_signals = _scan_procedural(lower)
        if proc_signals:
            return ClassifierResult(
                cls="procedural",
                signals_matched=proc_signals,
                signal_count=len(proc_signals),
                threshold_required=1,
                assembly_max_genes_cap=6,
                decoder_mode="full",
            )

        mh_signals = _scan_multi_hop(lower, text)
        if mh_signals:
            return ClassifierResult(
                cls="multi_hop",
                signals_matched=mh_signals,
                signal_count=len(mh_signals),
                threshold_required=1,
                assembly_max_genes_cap=8,
                decoder_mode="full",
            )

        return _DEFAULT
    except Exception:
        return ClassifierResult(cls="default", reason="classifier_error")
