"""Backward-compat shim -- real module at cymatix_context.mcp.mcp_server."""
import sys
from cymatix_context.mcp import mcp_server as _real

sys.modules[__name__] = _real

if __name__ == "__main__":
    # ``python -m helix_context.mcp_server`` runs this shim with
    # __name__ == "__main__", so the real module's own __main__ guard
    # never fires — dispatch explicitly or the documented invocation
    # exits 0 without ever starting the MCP stdio loop.
    _real.main()
