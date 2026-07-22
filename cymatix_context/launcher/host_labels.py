"""Pretty-label composition for vendor + host badges on the launcher dashboard.

The session registry stores ``agent_kind`` (vendor family — "claude-code",
"codex", "gemini") and ``mcp_host`` (host capability tag — "antigravity",
"vscode", "cursor"). The dashboard renders the pair as a single chip,
e.g. "Claude Code + VS Code". Unknown values echo verbatim so a new
vendor surfaces immediately rather than being swallowed.

The literal string "unknown" is treated as missing on the host axis
because ``mcp_server.py`` defaults ``HELIX_MCP_HOST`` to "unknown" when
the host doesn't set it.
"""
from __future__ import annotations

from typing import Optional


_VENDOR_MAP = {
    "claude-code": "Claude Code",
    "claude-desktop": "Claude Desktop",
    "codex": "Codex",
    "gemini": "Gemini",
}

_HOST_MAP = {
    "claude-code": "Claude Code",
    "claude-desktop": "Claude Desktop",
    "antigravity": "Antigravity",
    "cursor": "Cursor",
    "vscode": "VS Code",
    "vscode-continue": "VS Code (Continue)",
}


def vendor_pretty(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return _VENDOR_MAP.get(value, value)


def host_pretty(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if value == "unknown":
        return None
    return _HOST_MAP.get(value, value)


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
    # Case-insensitive dedup: a vendor like "codex" maps to "Codex" via
    # _VENDOR_MAP, but the same string as a host echoes verbatim through
    # host_pretty. Case-insensitive equality catches the asymmetry so a
    # client that sends agent_kind=mcp_host=<x> still gets a single chip.
    if v and h and v.lower() == h.lower():
        return v
    if v and h:
        return f"{v} + {h}"
    return v or h
