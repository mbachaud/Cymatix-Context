"""`--help` text in bench scripts must be ASCII so it cannot crash.

`python benchmarks/bench_*.py --help` makes argparse encode the module
docstring (the parser ``description``) and every ``help=`` string to the
console encoding. On a Windows cp1252 console a non-cp1252 character
(e.g. U+2192 RIGHTWARDS ARROW, U+2208 ELEMENT OF) raises
UnicodeEncodeError before help is shown. Keeping that text pure ASCII
keeps ``--help`` working on every locale.

Scope is exactly what argparse encodes: the module docstring and
``add_argument(help=...)`` strings of bench scripts that use argparse.
Comments, function docstrings, and runtime output (e.g. heatmap glyphs)
are intentionally out of scope -- argparse never encodes them.
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


def _help_strings(tree: ast.Module) -> list[str]:
    """Every constant `help=` string passed to an add_argument-style call."""
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if (kw.arg == "help"
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)):
                    out.append(kw.value.value)
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
        for help_text in _help_strings(tree):
            help_bad = _non_ascii(help_text)
            if help_bad:
                offenders.append(f"{path.name}: help= -> {help_bad}")
    assert not offenders, (
        "non-ASCII in argparse --help text (crashes --help on cp1252):\n  "
        + "\n  ".join(offenders)
    )
