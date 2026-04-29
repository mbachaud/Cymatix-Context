"""Upstream query classifier for the injection router.

Pure-function regex/keyword scan. No I/O, no model calls, infallible by
construction. See docs/specs/2026-04-29-query-classifier-injection-router-design.md.
"""

from __future__ import annotations

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
        # Rule scan lives in later tasks. For now, default-only.
        del text
        return _DEFAULT
    except Exception:
        return ClassifierResult(cls="default", reason="classifier_error")
