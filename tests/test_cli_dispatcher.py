"""Tests for the top-level `helix` CLI dispatcher (no subcommand work yet)."""
from __future__ import annotations

import io
import contextlib

import pytest

from helix_context.cli import main


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_no_args_prints_help_and_returns_nonzero():
    rc, out, err = _run([])
    assert rc != 0, "running `helix` with no subcommand should be an error"
    combined = (out + err).lower()
    assert "usage:" in combined
    assert "helix" in combined


def test_help_flag_exits_zero():
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    # argparse exits 0 on --help
    assert exc.value.code == 0


def test_unknown_subcommand_returns_two():
    with pytest.raises(SystemExit) as exc:
        main(["does-not-exist"])
    # argparse exits 2 on unknown choice
    assert exc.value.code == 2
