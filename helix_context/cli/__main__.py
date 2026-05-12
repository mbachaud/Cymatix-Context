"""Enable ``python -m helix_context.cli`` as a fallback when the
pip-installed ``helix`` console script is broken or off PATH.

Documented in ``docs/clients/cli.md`` as the always-works recovery path
when the editable-install entry point points at a deleted source tree.
"""
from __future__ import annotations

import sys

from .dispatcher import main

if __name__ == "__main__":
    sys.exit(main())
