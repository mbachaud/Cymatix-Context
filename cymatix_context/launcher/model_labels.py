"""Pretty-label module for model_id strings reported via helix_announce.

Maps known model identifiers to canonical display form for the dashboard
tooltip. Unknown IDs echo verbatim — no fabrication. The map grows as
agents announce new IDs. There is no allowlist gate on what an agent
can report; the registry stores whatever the agent says, and this
module is a display-only convenience.
"""
from __future__ import annotations

from typing import Optional


_MODEL_MAP = {
    # Anthropic
    "claude-opus-4-7": "Claude Opus 4.7",
    "claude-opus-4-7-1m": "Claude Opus 4.7 (1M context)",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-haiku-4-5": "Claude Haiku 4.5",
    # OpenAI
    "gpt-5": "GPT-5",
    # Google
    "gemini-2-5-pro": "Gemini 2.5 Pro",
}


def model_pretty(value: Optional[str]) -> Optional[str]:
    """Map a known model_id to its display form, or echo verbatim if
    unknown. None / empty input returns None."""
    if not value:
        return None
    return _MODEL_MAP.get(value, value)
