"""`helix serve` — DEFERRED in v1.

Per the 2026-05-11 council pass, v1 ships as cold-start CLI; the
daemon design lives in docs/architecture/HELIX_DAEMON_DESIGN.md but
is parked until benchmarking proves the thesis. This stub keeps the
subcommand visible in `helix --help` and tells the user what to do
in the meantime.
"""
from __future__ import annotations

from . import output


_MESSAGE = """\
`helix serve` is deferred in v1.

For the FastAPI proxy + retrieval HTTP surface, use the legacy entry
point or uvicorn directly:

  helix-server
  # or
  python -m uvicorn helix_context._asgi:app --host 127.0.0.1 --port 11437

The JSON-RPC daemon design lives in
docs/architecture/HELIX_DAEMON_DESIGN.md and will land in v1.x after
walk-bench numbers come in.
"""


def run(argv: list[str]) -> int:
    output.eprint(_MESSAGE)
    return output.EXIT_DEFERRED
