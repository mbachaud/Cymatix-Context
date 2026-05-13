"""Intent-based sub-query router — LLM-free decomposition path (D8, Step 3B).

Maps IntentClass values to sub-query template functions. Used by
HelixContextManager._decompose_query() when query_decomposition_enabled=False
or when no LLM backend is available.
"""
from __future__ import annotations
import re
from ..schemas import IntentClass


_TEMPLATES: dict[IntentClass, list[str]] = {
    IntentClass.MECHANISM: [
        "what triggers {subject}?",
        "what does {subject} compute or produce?",
        "what are the inputs and outputs of {subject}?",
    ],
    IntentClass.CONFIG_KNOB: [
        "what is the default value of {subject}?",
        "what does changing {subject} affect?",
        "where is {subject} configured?",
    ],
    IntentClass.DATA_STRUCTURE: [
        "what columns or fields does {subject} have?",
        "what is stored in {subject}?",
        "how is {subject} populated?",
    ],
    IntentClass.TRIGGER_CONDITION: [
        "what condition activates {subject}?",
        "what happens when {subject} fires?",
        "what prevents {subject} from triggering?",
    ],
    IntentClass.PROCESS_STEP: [
        "what is the first step of {subject}?",
        "what does each stage of {subject} produce?",
        "what are the inputs to {subject}?",
    ],
    IntentClass.FACT: [
        "what is the exact value of {subject}?",
        "where is {subject} defined?",
        "what uses {subject}?",
    ],
    IntentClass.RELATIONSHIP: [
        "how does {subject} depend on its context?",
        "what does {subject} provide to its callers?",
        "can {subject} exist independently?",
    ],
}


def sub_queries_for(query: str, intent_class: IntentClass) -> list[str]:
    """Return 3 point-fact sub-queries for a broad query given its intent class.

    Returns [query] if the class has no template or subject extraction fails.
    """
    templates = _TEMPLATES.get(intent_class)
    if not templates:
        return [query]
    m = re.search(
        r'\b(how|what|why|where|when|which)\b\s+(?:does|is|are|do|did)?\s*(.+)',
        query, re.IGNORECASE,
    )
    subject = m.group(2).rstrip("?. ") if m else query
    return [t.format(subject=subject) for t in templates[:3]]
