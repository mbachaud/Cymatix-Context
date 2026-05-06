"""Env-var fingerprint chain for IDE/CLI detection at MCP-adapter startup.

Used by ``mcp_server._register_with_registry`` to populate the
``ide_detected`` and ``ide_detection_via`` columns on the participants
row without depending on each MCP host vendor to set ``HELIX_MCP_HOST``
correctly.

Only env vars set intentionally by the host process are trusted as
signals. No PPID walking, no terminal-program guessing, no inference.
When no signal matches we return ``(None, "no_match")`` and let the
agent self-report later via ``helix_announce``.

Priority chain (first match wins):
    1. HELIX_MCP_HOST explicit (and not the legacy "unknown" sentinel)
    2. VSCODE_PID
    3. CURSOR_TRACE_ID
    4. fallback → (None, "no_match")

Extending: add a new branch ABOVE the fallback. New branch must (a) read
a single, intentional env var that the host actually sets, and (b) return
the canonical short host id paired with ``"env:<VAR_NAME>"`` as the via.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple


def detect_ide() -> Tuple[Optional[str], str]:
    """Return ``(ide_value, detection_via)``.

    ``ide_value`` is a canonical short id (e.g. ``"vscode"``, ``"cursor"``)
    or ``None`` when no signal matched. ``detection_via`` always contains
    a string documenting how the result was reached, suitable for the
    tooltip's diagnostic line.
    """
    explicit = os.environ.get("HELIX_MCP_HOST", "").strip()
    if explicit and explicit != "unknown":
        return explicit, "explicit:HELIX_MCP_HOST"

    if os.environ.get("VSCODE_PID", "").strip():
        return "vscode", "env:VSCODE_PID"

    if os.environ.get("CURSOR_TRACE_ID", "").strip():
        return "cursor", "env:CURSOR_TRACE_ID"

    return None, "no_match"
