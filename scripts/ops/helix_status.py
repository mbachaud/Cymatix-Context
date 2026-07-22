"""Backward-compat shim -- real module at cymatix_context.cli.helix_status.

Moved into the package (bugbash BUG-3) so the ``helix-status`` console
script and `helix status`'s network probes resolve in installed
environments, not just from a repo checkout. Kept here so any operator
muscle memory of ``python scripts/ops/helix_status.py`` still works.
"""
from __future__ import annotations

from cymatix_context.cli.helix_status import (  # noqa: F401 — re-exports
    collect_status,
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
