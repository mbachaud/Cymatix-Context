"""`helix serve` is deferred in v1; the stub prints a clear message."""
from __future__ import annotations

from tests.conftest import run_cli as _run


def test_serve_exits_four_with_deferred_message():
    rc, out, err = _run(["serve"])
    assert rc == 4
    combined = (out + err).lower()
    assert "deferred" in combined
    assert "helix-server" in combined or "uvicorn" in combined
