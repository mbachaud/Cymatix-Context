"""
Tests for scripts/release.py (the beta-branch release helper).

Every test runs against temp copies of pyproject.toml,
cymatix_context/__init__.py and CHANGELOG.md; nothing touches the real
tree, the network, or git (`check` is exercised with --skip-git only).
Covers:
  - pre: X.Y.Z -> X.Y.(Z+1)bN in both version files (or the --version
    given), one pre-release note directly under "## Unreleased", dry run
    by default, --write applies, refuses a base that is already released,
    refuses an empty "## Unreleased" the way `final` does
  - final: bump, "## Unreleased" rolled into "## X.Y.Z (date)" with a fresh
    empty "## Unreleased" above it, older sections untouched
  - tag-message: drafted from the matching section, Unreleased fallback
    for a pre-release version, --body-only drops the leading vX.Y.Z line
  - check: version drift and an empty Unreleased section fail
  - newline style of each file is preserved (CRLF working copies)
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "release.py"

PYPROJECT = """[build-system]
requires = ["hatchling"]

[project]
name = "cymatix-context"
version = "0.9.1"
description = "x"

[tool.other]
version = "not-the-package-version"
"""

INIT = '''"""Package."""

# Single source of truth is pyproject.toml.
__version__ = "0.9.1"

import os
'''

CHANGELOG = """# Changelog

## Unreleased

**Summary paragraph for the next release.**

- **feat:** something new (#1).
- **fix:** something fixed (#2).

## 0.9.1 (2026-08-30)

The previous release.

- **tagger v2** entry.

## 0.9.0 (2026-08-20)

Older.
"""


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("release_script", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_tree(tmp_path: Path, newline: str = "\n", changelog: str = CHANGELOG) -> Path:
    root = tmp_path / "repo"
    (root / "cymatix_context").mkdir(parents=True)
    for rel, text in (
        ("pyproject.toml", PYPROJECT),
        ("cymatix_context/__init__.py", INIT),
        ("CHANGELOG.md", changelog),
    ):
        (root / rel).write_bytes(text.replace("\n", newline).encode("utf-8"))
    return root


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_bytes().decode("utf-8")


class TestPre:
    def test_dry_run_prints_diff_and_changes_nothing(self, tmp_path, mod, capsys):
        root = _make_tree(tmp_path)
        rc = mod.main(["--repo-root", str(root), "pre", "--n", "1", "--date", "2026-09-01"])
        out = capsys.readouterr().out
        assert rc == 0
        assert '-version = "0.9.1"' in out and '+version = "0.9.2b1"' in out
        assert '+__version__ = "0.9.2b1"' in out
        assert "dry run" in out
        assert "git commit" in out and "--prerelease" in out
        assert _read(root, "pyproject.toml") == PYPROJECT
        assert _read(root, "CHANGELOG.md") == CHANGELOG

    def test_write_bumps_both_files_and_notes_the_tag(self, tmp_path, mod):
        root = _make_tree(tmp_path)
        rc = mod.main(["--repo-root", str(root), "pre", "--n", "2", "--date", "2026-09-01", "--write"])
        assert rc == 0
        assert 'version = "0.9.2b2"' in _read(root, "pyproject.toml")
        # The unrelated [tool.other] version line is untouched.
        assert 'version = "not-the-package-version"' in _read(root, "pyproject.toml")
        assert '__version__ = "0.9.2b2"' in _read(root, "cymatix_context/__init__.py")
        log = _read(root, "CHANGELOG.md")
        expected_head = (
            "# Changelog\n\n## Unreleased\n\n"
            "_Pre-release `v0.9.2b2` tagged 2026-09-01 from `beta` "
            "(`pip install --pre cymatix-context==0.9.2b2`)._\n\n"
            "**Summary paragraph for the next release.**\n"
        )
        assert log.startswith(expected_head)
        assert log.endswith(CHANGELOG[CHANGELOG.index("## 0.9.1"):])

    def test_second_pre_release_replaces_the_beta_suffix(self, tmp_path, mod):
        root = _make_tree(tmp_path)
        assert mod.main(["--repo-root", str(root), "pre", "--n", "1", "--write"]) == 0
        assert mod.main(["--repo-root", str(root), "pre", "--n", "2", "--write"]) == 0
        # The second cut keeps previewing 0.9.2 (base of the current 0.9.2b1),
        # it does not advance to 0.9.3.
        assert 'version = "0.9.2b2"' in _read(root, "pyproject.toml")
        assert "b1b2" not in _read(root, "pyproject.toml")

    def test_explicit_version_and_released_base_guard(self, tmp_path, mod, capsys):
        root = _make_tree(tmp_path)
        assert mod.main(["--repo-root", str(root), "pre", "--n", "1", "--version", "0.10.0", "--write"]) == 0
        assert 'version = "0.10.0b1"' in _read(root, "pyproject.toml")
        assert '__version__ = "0.10.0b1"' in _read(root, "cymatix_context/__init__.py")

        root2 = _make_tree(tmp_path / "second")
        # 0.9.1 is the released version in the fixture: 0.9.1b1 would sort
        # before it on PyPI, so the script refuses.
        assert mod.main(["--repo-root", str(root2), "pre", "--n", "1", "--version", "0.9.1"]) == 1
        assert "sort before" in capsys.readouterr().err
        assert mod.main(["--repo-root", str(root2), "pre", "--n", "1", "--version", "0.9.2b1"]) == 1
        assert "not X.Y.ZbN" in capsys.readouterr().err

    def test_preserves_crlf(self, tmp_path, mod):
        root = _make_tree(tmp_path, newline="\r\n")
        assert mod.main(["--repo-root", str(root), "pre", "--n", "1", "--write"]) == 0
        for rel in ("pyproject.toml", "cymatix_context/__init__.py", "CHANGELOG.md"):
            raw = (root / rel).read_bytes()
            assert b"\r\n" in raw
            assert b"\n" not in raw.replace(b"\r\n", b"")

    def test_rejects_n_below_one(self, tmp_path, mod, capsys):
        root = _make_tree(tmp_path)
        assert mod.main(["--repo-root", str(root), "pre", "--n", "0"]) == 1
        assert "--n must be >= 1" in capsys.readouterr().err

    def test_rejects_empty_unreleased(self, tmp_path, mod, capsys):
        # Same guard as `final`: the pre-release's tag message and GitHub
        # release notes come from "## Unreleased", so an empty section would
        # ship an empty release. The version files must stay untouched.
        empty = CHANGELOG.replace(
            CHANGELOG[CHANGELOG.index("**Summary"):CHANGELOG.index("## 0.9.1")], ""
        )
        root = _make_tree(tmp_path, changelog=empty)
        assert mod.main(["--repo-root", str(root), "pre", "--n", "1", "--write"]) == 1
        assert "empty" in capsys.readouterr().err
        assert _read(root, "pyproject.toml") == PYPROJECT
        assert _read(root, "CHANGELOG.md") == empty


class TestFinal:
    def test_rolls_unreleased_into_dated_section(self, tmp_path, mod):
        root = _make_tree(tmp_path)
        rc = mod.main(
            ["--repo-root", str(root), "final", "--version", "0.9.2", "--date", "2026-09-01", "--write"]
        )
        assert rc == 0
        assert 'version = "0.9.2"' in _read(root, "pyproject.toml")
        assert '__version__ = "0.9.2"' in _read(root, "cymatix_context/__init__.py")
        log = _read(root, "CHANGELOG.md")
        expected = (
            "# Changelog\n\n"
            "## Unreleased\n\n"
            "## 0.9.2 (2026-09-01)\n\n"
            "**Summary paragraph for the next release.**\n\n"
            "- **feat:** something new (#1).\n"
            "- **fix:** something fixed (#2).\n\n"
        ) + CHANGELOG[CHANGELOG.index("## 0.9.1"):]
        assert log == expected

    def test_final_after_pre_release_drops_the_suffix(self, tmp_path, mod):
        root = _make_tree(tmp_path)
        assert mod.main(["--repo-root", str(root), "pre", "--n", "1", "--date", "2026-09-01", "--write"]) == 0
        assert mod.main(
            ["--repo-root", str(root), "final", "--version", "0.9.2", "--date", "2026-09-02", "--write"]
        ) == 0
        assert 'version = "0.9.2"' in _read(root, "pyproject.toml")
        log = _read(root, "CHANGELOG.md")
        # The pre-release note rides into the released section as history.
        assert "## 0.9.2 (2026-09-02)\n\n_Pre-release `v0.9.2b1`" in log
        assert log.count("## Unreleased") == 1

    def test_dry_run_is_default(self, tmp_path, mod, capsys):
        root = _make_tree(tmp_path)
        assert mod.main(["--repo-root", str(root), "final", "--version", "0.9.2"]) == 0
        out = capsys.readouterr().out
        assert "+## 0.9.2 (" in out
        assert "release/v0.9.2" in out
        assert _read(root, "CHANGELOG.md") == CHANGELOG

    def test_rejects_prerelease_version_and_empty_unreleased(self, tmp_path, mod, capsys):
        root = _make_tree(tmp_path)
        assert mod.main(["--repo-root", str(root), "final", "--version", "0.9.2b1"]) == 1
        assert "use `pre`" in capsys.readouterr().err

        empty = CHANGELOG.replace(
            CHANGELOG[CHANGELOG.index("**Summary"):CHANGELOG.index("## 0.9.1")], ""
        )
        root2 = _make_tree(tmp_path / "second", changelog=empty)
        assert mod.main(["--repo-root", str(root2), "final", "--version", "0.9.2"]) == 1
        assert "empty" in capsys.readouterr().err


class TestTagMessage:
    def test_drafts_from_the_matching_section(self, tmp_path, mod, capsys):
        root = _make_tree(tmp_path)
        assert mod.main(["--repo-root", str(root), "tag-message", "--version", "0.9.1"]) == 0
        out = capsys.readouterr().out
        assert out == "v0.9.1\n\nThe previous release.\n\n- **tagger v2** entry.\n"

    def test_prerelease_falls_back_to_unreleased(self, tmp_path, mod, capsys):
        root = _make_tree(tmp_path)
        assert mod.main(["--repo-root", str(root), "tag-message", "--version", "0.9.2b1"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("v0.9.2b1\n\n**Summary paragraph")
        assert "## 0.9.1" not in out

    def test_missing_section_fails(self, tmp_path, mod, capsys):
        root = _make_tree(tmp_path)
        assert mod.main(["--repo-root", str(root), "tag-message", "--version", "0.5.0"]) == 1
        assert "no section" in capsys.readouterr().err

    def test_body_only_drops_the_title_line(self, tmp_path, mod, capsys):
        # `gh release create --notes-file` renders its own title, so the body
        # alone is what it wants; the annotated tag still gets the v-line form.
        root = _make_tree(tmp_path)
        assert mod.main(
            ["--repo-root", str(root), "tag-message", "--version", "0.9.1", "--body-only"]
        ) == 0
        body_only = capsys.readouterr().out
        assert body_only == "The previous release.\n\n- **tagger v2** entry.\n"

        assert mod.main(["--repo-root", str(root), "tag-message", "--version", "0.9.1"]) == 0
        assert capsys.readouterr().out == "v0.9.1\n\n" + body_only

    def test_body_only_works_for_a_prerelease_version(self, tmp_path, mod, capsys):
        root = _make_tree(tmp_path)
        assert mod.main(
            ["--repo-root", str(root), "tag-message", "--version", "0.9.2b1", "--body-only"]
        ) == 0
        out = capsys.readouterr().out
        assert out.startswith("**Summary paragraph")
        assert "v0.9.2b1" not in out


class TestCheck:
    def test_passes_on_consistent_tree(self, tmp_path, mod, capsys):
        root = _make_tree(tmp_path)
        assert mod.main(["--repo-root", str(root), "check", "--skip-git"]) == 0
        assert "release check OK" in capsys.readouterr().out

    def test_fails_on_version_drift(self, tmp_path, mod, capsys):
        root = _make_tree(tmp_path)
        init = root / "cymatix_context" / "__init__.py"
        init.write_text(INIT.replace('"0.9.1"', '"0.9.0"'), encoding="utf-8")
        assert mod.main(["--repo-root", str(root), "check", "--skip-git"]) == 1
        assert "version mismatch" in capsys.readouterr().out

    def test_fails_on_empty_unreleased(self, tmp_path, mod, capsys):
        empty = CHANGELOG.replace(
            CHANGELOG[CHANGELOG.index("**Summary"):CHANGELOG.index("## 0.9.1")], ""
        )
        root = _make_tree(tmp_path, changelog=empty)
        assert mod.main(["--repo-root", str(root), "check", "--skip-git"]) == 1
        assert "no content" in capsys.readouterr().out


def test_parse_version(mod):
    assert mod.parse_version("0.9.2") == ("0.9.2", None)
    assert mod.parse_version("0.9.2b3") == ("0.9.2", 3)
    with pytest.raises(mod.ReleaseError):
        mod.parse_version("v0.9.2")
    with pytest.raises(mod.ReleaseError):
        mod.parse_version("0.9.2rc1")
