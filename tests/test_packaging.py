"""Tests for pyproject.toml extras-matrix invariants."""

from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore


REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"


def _extras() -> dict:
    with PYPROJECT.open("rb") as f:
        spec = tomllib.load(f)
    return spec["project"]["optional-dependencies"]


def test_launcher_extra_includes_platformdirs():
    """platformdirs is required for state-dir resolution; bundled in
    [launcher] because every launcher mode (UI, native, tray) uses it.
    Lock the dep so a future drop is caught."""
    extras = _extras()
    assert any("platformdirs" in d for d in extras["launcher"]), (
        "platformdirs must be in [launcher]"
    )


def test_launcher_tray_extra_includes_pywin32_with_windows_marker():
    """pywin32 (Job Object APIs) is Windows-only. Must carry an
    environment marker so pip on Linux/macOS doesn't try to install it."""
    extras = _extras()
    pyw = [d for d in extras["launcher-tray"] if "pywin32" in d]
    assert pyw, "pywin32 must be in [launcher-tray]"
    line = pyw[0]
    assert "sys_platform" in line and "win32" in line, (
        f"pywin32 entry must carry sys_platform marker; got {line!r}"
    )


def test_all_extra_includes_platformdirs():
    """[all] (the meta-extra) keeps the extras-matrix sensible by
    including everything from individual extras."""
    extras = _extras()
    assert any("platformdirs" in d for d in extras["all"])


def test_spacy_extras_exclude_preshed_3_0_14():
    """preshed 3.0.14 breaks loading en_core_web_sm 3.8.0 on spaCy 3.8.16
    (OverflowError in Tokenizer._flush_specials). Every extra that pulls
    spaCy must exclude it, or a fresh install resolves to the broken pin."""
    extras = _extras()
    spacy_extras = [
        name for name, deps in extras.items()
        if any(d.replace(" ", "").startswith("spacy") for d in deps)
    ]
    assert {"cpu", "dev", "all"} <= set(spacy_extras)
    missing = [
        name for name in spacy_extras
        if not any(d.replace(" ", "") == "preshed!=3.0.14" for d in extras[name])
    ]
    assert not missing, f"extras pulling spaCy without preshed!=3.0.14: {missing}"
