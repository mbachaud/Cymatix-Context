"""Upstream query classifier for the injection router.

Pure-function regex/keyword scan. No I/O, no model calls, infallible by
construction. See docs/specs/2026-04-29-query-classifier-injection-router-design.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional


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
# present (de-duplicated so a math expression with three `+` is still 1 signal).
_OPERATOR_CHARS = ("+", "-", "*", "/", "%")

# Single-word numeric/quantity keywords (lowercase, word-boundary matched).
_NUMERIC_KEYWORDS = ("calculate", "total", "sum")

# Multi-word keywords (lowercase substring match).
_MULTIWORD_KEYWORDS = ("critical path",)


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


_DEFAULT = ClassifierResult(cls="default")


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

        return _DEFAULT
    except Exception:
        return ClassifierResult(cls="default", reason="classifier_error")
