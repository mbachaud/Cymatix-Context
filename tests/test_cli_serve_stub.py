"""`helix serve` is deferred in v1; the stub prints a clear message."""
from __future__ import annotations

import io
import contextlib

from helix_context.cli import main


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_serve_exits_four_with_deferred_message():
    rc, out, err = _run(["serve"])
    assert rc == 4
    combined = (out + err).lower()
    assert "deferred" in combined
    assert "helix-server" in combined or "uvicorn" in combined
