"""Backward-compat shim -- real module at cymatix_context.mcp.mcp_server."""
import sys
from . import mcp as _mcp_pkg  # noqa: F401 — ensure parent package loaded
from .mcp import mcp_server as _real

sys.modules[__name__] = _real

if __name__ == "__main__":
    # ``python -m cymatix_context.mcp_server`` runs this shim with
    # __name__ == "__main__", so the real module's own __main__ guard
    # never fires — dispatch explicitly or the documented invocation
    # exits 0 without ever starting the MCP stdio loop. Plain imports
    # skip this branch, keeping the shim side-effect-free beyond the
    # sys.modules alias.
    _real.main()
