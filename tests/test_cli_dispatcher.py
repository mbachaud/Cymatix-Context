"""Tests for the top-level `helix` CLI dispatcher (no subcommand work yet)."""
from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from cymatix_context.cli import dispatcher
from cymatix_context.cli import main
from tests.conftest import run_cli as _run


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


def test_main_consults_sys_argv_when_argv_is_none(monkeypatch):
    """Regression for commit 7647b72: main() with no argument must read sys.argv[1:].

    Previously ``main()`` (no argument) bypassed the `if argv is None: argv = sys.argv[1:]`
    branch in some entry-point setups, so the installed `helix` console-script
    crashed when invoked without explicit args. We exercise the path by setting
    sys.argv to a benign ``["helix", "--help"]`` value and calling ``main()``
    with no positional argument — argparse should still see ``--help`` and
    SystemExit(0). If main() failed to consult sys.argv, it would instead see
    an empty argv and return EXIT_ERROR via the no-args branch.
    """
    import sys
    monkeypatch.setattr(sys, "argv", ["helix", "--help"])
    with pytest.raises(SystemExit) as exc:
        main()
    # argparse --help → exit 0; proves sys.argv was consulted (the no-args
    # branch would have returned EXIT_ERROR=1 without raising SystemExit).
    assert exc.value.code == 0


# ── prog derives from the invoked script name (rename cosmetic fix) ────


@pytest.mark.parametrize("argv0,expected_prog", [
    ("/usr/local/bin/cymatix", "cymatix"),
    ("/usr/local/bin/helix", "helix"),
    (r"C:\env\Scripts\cymatix.exe", "cymatix"),
    (r"C:\env\Scripts\helix.exe", "helix"),
])
def test_parser_prog_derives_from_argv0(monkeypatch, argv0, expected_prog):
    """Each console-script alias should show itself in usage/help output,
    not always claim to be `helix` (P3 finding)."""
    monkeypatch.setattr(sys, "argv", [argv0, "--help"])
    parser = dispatcher._build_parser()
    assert parser.prog == expected_prog


def test_parser_prog_falls_back_when_argv_empty(monkeypatch):
    monkeypatch.setattr(sys, "argv", [])
    parser = dispatcher._build_parser()
    assert parser.prog == "cymatix"


@pytest.mark.parametrize("script_name,expected_prog", [
    ("cymatix", "cymatix"),
    ("helix", "helix"),
])
def test_installed_console_script_prog_matches_invoked_name(script_name, expected_prog):
    """Smoke-test the real installed console scripts: `cymatix --help` must
    say `usage: cymatix`, `helix --help` must say `usage: helix`."""
    exe = shutil.which(script_name)
    if exe is None:
        pytest.skip(f"{script_name} console script not found on PATH")
    proc = subprocess.run(
        [exe, "--help"], capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    first_line = proc.stdout.splitlines()[0]
    assert first_line.startswith(f"usage: {expected_prog}"), first_line
