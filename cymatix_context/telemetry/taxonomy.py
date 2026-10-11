"""Attack/label taxonomy for packet-trace ``label`` events (issue #493, Phase 4).

Labels are recorded only. No detector in this module decides a label.

UNVERIFIED MAPPINGS: the OWASP LLM Top 10 (2025) and MITRE ATLAS ids in
``MAPPING`` are candidate mappings written from memory. Every entry is
marked ``"verified": False`` until a human reviewer checks it against the
published OWASP and ATLAS catalogues. Where the id was not known with
confidence the value is ``None`` rather than a guess.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Optional, TypedDict

INDIRECT_PROMPT_INJECTION = "indirect_prompt_injection"
CONTROL_TAG_FORGERY = "control_tag_forgery"
RBAC_LEAK = "rbac_leak"
CORPUS_POISONING = "corpus_poisoning"
DATA_EXFILTRATION = "data_exfiltration"
BENIGN = "benign"

LABELS: FrozenSet[str] = frozenset({
    INDIRECT_PROMPT_INJECTION,
    CONTROL_TAG_FORGERY,
    RBAC_LEAK,
    CORPUS_POISONING,
    DATA_EXFILTRATION,
    BENIGN,
})

LABEL_SOURCES: FrozenSet[str] = frozenset({"trace_check", "human", "judge"})


class Mapping(TypedDict):
    owasp_llm: Optional[str]
    atlas: Optional[str]
    verified: bool


# Non-benign labels only. UNVERIFIED, see module docstring.
MAPPING: Dict[str, Mapping] = {
    INDIRECT_PROMPT_INJECTION: {
        "owasp_llm": "LLM01:2025",
        "atlas": "AML.T0051.001",
        "verified": False,
    },
    CONTROL_TAG_FORGERY: {
        "owasp_llm": "LLM01:2025",
        "atlas": "AML.T0051",
        "verified": False,
    },
    RBAC_LEAK: {
        "owasp_llm": "LLM02:2025",
        "atlas": None,
        "verified": False,
    },
    CORPUS_POISONING: {
        "owasp_llm": "LLM04:2025",
        "atlas": "AML.T0070",
        "verified": False,
    },
    DATA_EXFILTRATION: {
        "owasp_llm": "LLM02:2025",
        "atlas": None,
        "verified": False,
    },
}
