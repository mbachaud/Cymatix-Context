"""`--help` text in bench scripts must be ASCII so it cannot crash.

`python benchmarks/bench_*.py --help` makes argparse encode the module
docstring (the parser ``description``) and every ``help=`` string to the
console encoding. On a Windows cp1252 console a non-cp1252 character
(e.g. U+2192 RIGHTWARDS ARROW, U+2208 ELEMENT OF) raises
UnicodeEncodeError before help is shown. Keeping that text pure ASCII
keeps ``--help`` working on every locale.

Scope is exactly what argparse encodes: the module docstring, the parser
``description=``/``epilog=`` strings, and every ``add_argument(help=...)``
string of bench scripts that use argparse. Comments, function docstrings,
and runtime output (e.g. heatmap glyphs) are intentionally out of scope --
argparse never encodes them.
"""

from __future__ import annotations

import ast
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[1] / "benchmarks"


def _argparse_bench_files() -> list[Path]:
    """Bench scripts that build an argparse parser (so they have --help)."""
    return [
        p for p in sorted(BENCH_DIR.glob("bench_*.py"))
        if "argparse" in p.read_text(encoding="utf-8")
    ]


# argparse encodes these keyword-arg strings to the console encoding when
# `--help` runs: `help=` (per add_argument), plus `description=` and
# `epilog=` (per ArgumentParser). All three must be ASCII.
_HELP_KWARGS = ("help", "description", "epilog")


def _help_strings(tree: ast.Module) -> list[tuple[str, str]]:
    """Every constant `help=`/`description=`/`epilog=` string passed to an
    argparse-style call, as ``(kwarg_name, value)`` pairs."""
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if (kw.arg in _HELP_KWARGS
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)):
                    out.append((kw.arg, kw.value.value))
    return out


def _non_ascii(text: str) -> list[str]:
    return sorted({c for c in text if ord(c) > 127})


def test_argparse_bench_files_are_discovered():
    """Guard against the glob silently matching nothing (vacuous pass)."""
    files = _argparse_bench_files()
    assert files, "no argparse bench scripts found under benchmarks/"
    assert any(p.name == "bench_claude_matrix.py" for p in files)


def test_argparse_help_text_is_ascii():
    """Module docstring + every `help=` string of every argparse bench
    script must be ASCII, so `--help` cannot raise UnicodeEncodeError on a
    non-UTF-8 console."""
    offenders: list[str] = []
    for path in _argparse_bench_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        doc_bad = _non_ascii(ast.get_docstring(tree) or "")
        if doc_bad:
            offenders.append(f"{path.name}: docstring -> {doc_bad}")
        for kwarg, help_text in _help_strings(tree):
            help_bad = _non_ascii(help_text)
            if help_bad:
                offenders.append(f"{path.name}: {kwarg}= -> {help_bad}")
    assert not offenders, (
        "non-ASCII in argparse --help text (crashes --help on cp1252):\n  "
        + "\n  ".join(offenders)
    )
