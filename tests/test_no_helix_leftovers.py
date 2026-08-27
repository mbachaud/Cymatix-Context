"""Clean-break contract: the ``helix`` brand is gone from every live surface.

This replaces the old ``test_cymatix_rename.py`` back-compat contract. That
file pinned the *presence* of the compatibility layer (the ``helix_context``
alias package, ``helix*`` console scripts, the ``CYMATIX_*→HELIX_*`` env
mirror, the ``helix.toml`` fallback). As of 0.8.5 all of that is **removed**,
so the contract inverts: we now assert the shims are *absent*.

A second section (``TestNoHelixInSource``) greps the live ``cymatix_context``
package for any lingering ``helix`` literal and fails on anything outside a
tiny, explicit read-compat whitelist.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO_ROOT / "cymatix_context"


# Keep the active-surface walk deterministic and deliberately narrow.  Historical
# records, benchmark receipts, and the metadata-only redirect are covered by
# their own contracts and must not be swept into this lint.
ACTIVE_DOCS = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "docs/SETUP.md",
    REPO_ROOT / "docs/TROUBLESHOOTING.md",
    *sorted((REPO_ROOT / "docs/clients").glob("*.md")),
    *sorted((REPO_ROOT / "docs/api").rglob("*.md")),
    *sorted((REPO_ROOT / "docs/ops").rglob("*.md")),
)
ACTIVE_SCRIPTS = tuple(
    path
    for root in (REPO_ROOT, REPO_ROOT / "deploy")
    for suffix in ("*.bat", "*.ps1", "*.sh")
    for path in sorted(root.glob(suffix) if root == REPO_ROOT else root.rglob(suffix))
    if path.is_file()
)
ACTIVE_SKILLS = tuple(
    path for path in sorted((REPO_ROOT / "skills").rglob("*")) if path.is_file()
)

_ACTIVE_SURFACE_WHITELIST: dict[str, tuple[str, ...]] = {
    "README.md": (
        "formerly `helix-context`",
        "`helix_context` import",
        "helix-context](#migrating",
        "old `helix_context` alias",
        "migrating from helix-context",
        "old `helix` surface",
        "`cymatix-context<0.8.5`",
        "| install | `pip install helix-context`",
        "| import | `import helix_context`",
        "| cli | `helix`, `helix-server`",
        "| config file | `helix.toml`",
        "| env vars | `helix_*`",
        "| mcp `-m` entry | `python -m helix_context.mcp_server`",
        "| mcp tools | `helix_*`",
        "| asgi target | `helix_context._asgi:app`",
    ),
    "CLAUDE.md": (
        "renamed from helix-context",
        "old `helix_context` alias package",
        "not the helix brand",
        "the `helix.toml` fallback",
    ),
    "deploy/windows/setup-cymatix.ps1": (
        "retire pre-rename 'helix' shortcuts",
        "a helix.lnk for some other install",
        'join-path $desktop "helix.lnk"',
    ),
}
_ACTIVE_SURFACE_EXPECTED_COUNTS = {
    "README.md": 15,
    "CLAUDE.md": 4,
    "deploy/windows/setup-cymatix.ps1": 3,
}


# ── (a) the helix_context alias package is gone ─────────────────────


def test_helix_context_package_is_unimportable():
    """``import helix_context`` must raise ModuleNotFoundError — the alias
    shim package was deleted in the 0.8.5 clean break."""
    for mod in [m for m in list(sys.modules) if m.split(".")[0] == "helix_context"]:
        del sys.modules[mod]
    with pytest.raises(ModuleNotFoundError):
        import helix_context  # noqa: F401


def test_helix_context_submodule_unimportable():
    """Deep imports through the old namespace fail too."""
    with pytest.raises(ModuleNotFoundError):
        import helix_context.config  # noqa: F401


# ── (b) no helix* console scripts ───────────────────────────────────


def test_no_helix_console_scripts():
    from importlib.metadata import entry_points

    names = {ep.name for ep in entry_points(group="console_scripts")}
    forbidden = {
        "helix", "helix-server", "helix-launcher", "helix-status", "helix-vault",
    }
    leaked = forbidden & names
    assert not leaked, f"deprecated helix console scripts still registered: {leaked}"


def test_cymatix_console_scripts_present():
    from importlib.metadata import entry_points

    names = {ep.name for ep in entry_points(group="console_scripts")}
    expected = {
        "cymatix", "cymatix-server", "cymatix-launcher", "cymatix-status", "cymatix-vault",
    }
    missing = expected - names
    assert not missing, f"missing cymatix console scripts: {missing}"


# ── (c) no CYMATIX_* → HELIX_* env mirror ───────────────────────────


def test_no_env_mirror_symbol():
    """The ``_mirror_env`` helper that copied CYMATIX_* → HELIX_* is gone."""
    import cymatix_context

    assert not hasattr(cymatix_context, "_mirror_env")


def test_setting_cymatix_env_does_not_create_helix_mirror():
    """Setting CYMATIX_FOO in a fresh interpreter that imports cymatix_context
    must NOT populate os.environ['HELIX_FOO'] — no mirror runs on import."""
    env = dict(os.environ)
    env["CYMATIX_MIRROR_PROBE"] = "1"
    env.pop("HELIX_MIRROR_PROBE", None)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import cymatix_context, os; "
            "print(os.environ.get('HELIX_MIRROR_PROBE', '<absent>'))",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "<absent>", (
        "importing cymatix_context mirrored CYMATIX_* → HELIX_*; "
        f"HELIX_MIRROR_PROBE={proc.stdout.strip()!r}"
    )


# ── (d) load_config does not fall back to helix.toml ────────────────


def test_load_config_ignores_helix_toml(tmp_path, monkeypatch):
    """A lone ``helix.toml`` in cwd must be ignored — the fallback was
    removed, so config resolves to defaults (not the helix.toml value)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HELIX_CONFIG", raising=False)
    monkeypatch.delenv("CYMATIX_CONFIG", raising=False)
    sentinel_port = 59999
    (tmp_path / "helix.toml").write_text(
        f"[server]\nport = {sentinel_port}\n", encoding="utf-8"
    )
    from cymatix_context.config import load_config

    cfg = load_config()
    assert cfg.server.port != sentinel_port, (
        "load_config honored helix.toml — the fallback should be gone"
    )


def test_load_config_reads_cymatix_toml(tmp_path, monkeypatch):
    """The positive control: cymatix.toml is still honored in cwd."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HELIX_CONFIG", raising=False)
    monkeypatch.delenv("CYMATIX_CONFIG", raising=False)
    (tmp_path / "cymatix.toml").write_text(
        "[server]\nport = 12345\n", encoding="utf-8"
    )
    from cymatix_context.config import load_config

    cfg = load_config()
    assert cfg.server.port == 12345


# ── (e) MCP server identifies as cymatix ────────────────────────────


def test_mcp_server_identifies_as_cymatix():
    pytest.importorskip(
        "mcp.server.mcpserver",
        reason="mcp SDK extra not installed, or too old to expose "
               "mcp.server.mcpserver (mcp 2.x home of MCPServer)",
    )
    from cymatix_context.mcp.mcp_server import mcp

    assert mcp.name == "cymatix"


# ── source-tree guard: no stray ``helix`` literals in the package ───
#
# The only intentional ``helix`` mentions left in cymatix_context are
# read-compat carve-outs, whitelisted here by (relative path → allowed
# ASCII fragments). Every helix-bearing source line must match one of
# these fragments, and the path-specific counts are pinned so a *new*
# whitelisted-looking line still trips the guard.

_WHITELIST: dict[str, tuple[str, ...]] = {
    # Accept the legacy bundle key on import (write only the cymatix key).
    "cross_store_import.py": ("helix_format_version",),
    # Read-only fallback to a pre-rename ~/.helix launcher selection record.
    "launcher/genome_registry.py": (".helix", "helix-era"),
}
_EXPECTED_WHITELISTED_LINES = {
    "cross_store_import.py": 2,
    "launcher/genome_registry.py": 4,
}


def _iter_source_helix_lines():
    for path in sorted(PKG_ROOT.rglob("*.py")):
        rel = path.relative_to(PKG_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "helix" in line.lower():
                yield rel, lineno, line


class TestNoHelixInSource:
    def test_only_whitelisted_helix_references_remain(self):
        violations = []
        whitelisted_counts: dict[str, int] = {}
        for rel, lineno, line in _iter_source_helix_lines():
            fragments = _WHITELIST.get(rel)
            if fragments and any(frag in line.lower() for frag in fragments):
                whitelisted_counts[rel] = whitelisted_counts.get(rel, 0) + 1
                continue
            violations.append(f"{rel}:{lineno}: {line.strip()}")
        assert not violations, (
            "unexpected 'helix' literal(s) in cymatix_context/:\n"
            + "\n".join(violations)
        )
        assert whitelisted_counts == _EXPECTED_WHITELISTED_LINES, (
            "whitelisted helix read-compat line counts drifted: expected "
            f"{_EXPECTED_WHITELISTED_LINES}, found {whitelisted_counts}. Update the "
            "whitelist deliberately if the read-compat surface changed."
        )


def test_active_surfaces_have_only_count_pinned_migration_references():
    """Current docs, scripts, and skills are Cymatix-only except named notices."""
    violations = []
    whitelisted_counts: dict[str, int] = {}
    active_paths = (*ACTIVE_DOCS, *ACTIVE_SCRIPTS, *ACTIVE_SKILLS)
    for path in active_paths:
        rel = path.relative_to(REPO_ROOT).as_posix()
        fragments = _ACTIVE_SURFACE_WHITELIST.get(rel, ())
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "helix" not in line.lower():
                continue
            if fragments and any(fragment in line.lower() for fragment in fragments):
                whitelisted_counts[rel] = whitelisted_counts.get(rel, 0) + 1
                continue
            violations.append(f"{rel}:{lineno}: {line.strip()}")

    assert not violations, (
        "unexpected Helix reference on an active surface:\n" + "\n".join(violations)
    )
    assert whitelisted_counts == _ACTIVE_SURFACE_EXPECTED_COUNTS, (
        "active migration-reference counts drifted: expected "
        f"{_ACTIVE_SURFACE_EXPECTED_COUNTS}, found {whitelisted_counts}"
    )


_OLD_PREFIX_ADOPTION = re.compile(r"adopt[\s_-]+old[\s_-]+prefix|legacy[\s_-]+prefix", re.I)
_HELIX_ENV = re.compile(r"\bHELIX_[A-Z0-9_]*\b", re.I)
_CYMATIX_SELF_ASSIGNMENT = re.compile(
    r"\b(CYMATIX_[A-Z0-9_]+)\s*=\s*(?:%\s*([A-Z0-9_]+)\s*%|"
    r"\$\{?([A-Z0-9_]+)\}?|['\"]?\$env:([A-Z0-9_]+)['\"]?)",
    re.I,
)


def test_active_scripts_have_no_old_prefix_adoption_or_mirrors():
    violations = []
    for path in ACTIVE_SCRIPTS:
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _OLD_PREFIX_ADOPTION.search(line) or _HELIX_ENV.search(line):
                violations.append(f"{rel}:{lineno}: {line.strip()}")
                continue
            match = _CYMATIX_SELF_ASSIGNMENT.search(line)
            rhs_name = (
                next((group for group in match.groups()[1:] if group), None)
                if match
                else None
            )
            if match and match.group(1).upper() == f"CYMATIX_{rhs_name.upper()}":
                violations.append(f"{rel}:{lineno}: {line.strip()}")
    assert not violations, (
        "active scripts contain a legacy-prefix adoption/mirror surface:\n"
        + "\n".join(violations)
    )


def test_pypi_tombstone_is_a_metadata_only_cymatix_redirect():
    tombstone = REPO_ROOT / "deploy/pypi-tombstone"
    metadata = tomllib.loads((tombstone / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]
    assert project["name"] == "helix-context"
    assert any(dep.lower().startswith("cymatix-context") for dep in project["dependencies"])
    assert not project.get("scripts")
    setuptools = metadata.get("tool", {}).get("setuptools", {})
    assert setuptools.get("packages") == []
    assert setuptools.get("py-modules") == []
    assert not setuptools.get("entry-points")

    prose = (tombstone / "README.md").read_text(encoding="utf-8").lower()
    assert "current cymatix" in prose and "does not provide" in prose
    for removed_alias in ("helix_context", "helix*", "helix_*", "helix.toml"):
        assert removed_alias in prose
