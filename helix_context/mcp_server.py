"""Backward-compat shim -- real module at helix_context.mcp.mcp_server.

Two responsibilities:

1. **Import compat.** Code that does ``from helix_context import mcp_server``
   or ``import helix_context.mcp_server`` continues to receive the real
   module via the ``sys.modules`` rebind.

2. **Command-line entry point.** ``python -m helix_context.mcp_server`` is
   the canonical invocation registered in users' ``.mcp.json`` files; it
   must dispatch to ``helix_context.mcp.mcp_server.main()``. Without
   this, the shim imports the real module and exits silently, and any
   MCP host (Claude Code, Cursor, Antigravity, etc.) reports
   "Connection closed" within ~2s of the spawn — the failure mode
   diagnosed during the 2026-05-20 bench debugging session.
"""
import sys
from . import mcp as _mcp_pkg  # noqa: F401 — ensure parent package loaded
from .mcp import mcp_server as _real

if __name__ == "__main__":
    # ``python -m helix_context.mcp_server`` path. Dispatch to the real
    # main() so the MCP stdio handshake can complete. mcp.run() blocks
    # for the process lifetime.
    _real.main()
else:
    # Import path. Rebind so existing ``from helix_context import mcp_server``
    # callers receive the real module transparently.
    sys.modules[__name__] = _real
