"""Backward-compat shim -- real module at helix_context.mcp.mcp_server."""
import sys
from . import mcp as _mcp_pkg  # noqa: F401 — ensure parent package loaded
from .mcp import mcp_server as _real

sys.modules[__name__] = _real
