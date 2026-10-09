"""Label composition for the vendor + host badges on the launcher dashboard.

The session registry stores ``agent_kind`` (what the agent says it is, e.g.
"claude-code") and ``mcp_host`` (the host it runs in, e.g. "vscode"). The
dashboard renders the pair as a single chip, e.g. "claude-code + vscode".

There is deliberately no name map here: the values are shown exactly as the
agent announced them, so a tool this code has never heard of appears the
first time it connects, with no release needed.

The literal string "unknown" is treated as missing on the host axis because
``mcp_server.py`` defaults ``CYMATIX_MCP_HOST`` to "unknown" when the host
doesn't set it.
"""
from __future__ import annotations

from typing import Optional


def vendor_pretty(value: Optional[str]) -> Optional[str]:
    return value or None


def host_pretty(value: Optional[str]) -> Optional[str]:
    if not value or value == "unknown":
        return None
    return value


def compose_label(
    agent_kind: Optional[str],
    mcp_host: Optional[str],
) -> Optional[str]:
    """Combine vendor + host into a single dashboard chip label.

    Returns ``None`` when both axes are absent so the template can
    skip rendering the chip entirely.
    """
    v = vendor_pretty(agent_kind)
    h = host_pretty(mcp_host)
    # A client that sends agent_kind=mcp_host=<x> still gets a single chip.
    if v and h and v.lower() == h.lower():
        return v
    if v and h:
        return f"{v} + {h}"
    return v or h
