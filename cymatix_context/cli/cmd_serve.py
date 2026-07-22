"""`helix serve` — DEFERRED in v1.

Per the 2026-05-11 council pass, v1 ships as cold-start CLI; the
daemon design doc has not been written yet — it is parked until
benchmarking proves the thesis. This stub keeps the
subcommand visible in `helix --help` and tells the user what to do
in the meantime.
"""
from __future__ import annotations

from . import output


_MESSAGE = """\
`helix serve` is deferred in v1.

For the FastAPI proxy + retrieval HTTP surface, use the legacy entry
point or uvicorn directly:

  cymatix-server
  # or
  python -m uvicorn cymatix_context._asgi:app --host 127.0.0.1 --port 11437

A JSON-RPC daemon may land in v1.x after walk-bench numbers come in;
no design doc exists yet.
"""


def run(argv: list[str]) -> int:
    output.eprint(_MESSAGE)
    return output.EXIT_DEFERRED
