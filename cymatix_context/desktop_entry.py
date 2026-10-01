"""Single entry point for the desktop app's bundled engine.

The packaged app ships one PyInstaller executable, ``cymatix-engine``, and
reaches every Python surface through it, so users need no Python install:

    cymatix-engine launcher [launcher args]   # the sidecar (--headless ...)
    cymatix-engine cli [cymatix args]         # e.g. `cli mcp install ...`
    cymatix-engine mcp                        # stdio MCP server for chat hosts

In development the same dispatch runs as
``python -m cymatix_context.desktop_entry <subcommand> ...``.
"""

from __future__ import annotations

import sys
from typing import List, Optional

_USAGE = "usage: cymatix-engine {launcher|cli|mcp} [args...]"


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sub, rest = (argv[0], argv[1:]) if argv else ("", [])
    if sub == "launcher":
        from .launcher import app as launcher_app
        return launcher_app.main(rest) or 0
    if sub == "cli":
        from .cli import dispatcher
        return dispatcher.main(rest) or 0
    if sub == "mcp":
        from .mcp import mcp_server
        mcp_server.main()
        return 0
    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
