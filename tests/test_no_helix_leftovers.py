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
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO_ROOT / "cymatix_context"


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
    pytest.importorskip("mcp", reason="mcp SDK extra not installed")
    from cymatix_context.mcp.mcp_server import mcp

    assert mcp.name == "cymatix"


# ── source-tree guard: no stray ``helix`` literals in the package ───
#
# The only intentional ``helix`` mentions left in cymatix_context are
# read-compat carve-outs, whitelisted here by (relative path → allowed
# ASCII fragments). Every helix-bearing source line must match one of
# these fragments, and the total count is pinned so a *new* whitelisted-
# looking line still trips the guard.

_WHITELIST: dict[str, tuple[str, ...]] = {
    # Accept the legacy bundle key on import (write only the cymatix key).
    "cross_store_import.py": ("helix_format_version",),
    # Read-only fallback to a pre-rename ~/.helix launcher selection record.
    "launcher/genome_registry.py": (".helix", "helix-era"),
}
_EXPECTED_WHITELISTED_LINES = 6


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
        whitelisted = 0
        for rel, lineno, line in _iter_source_helix_lines():
            fragments = _WHITELIST.get(rel)
            if fragments and any(frag in line for frag in fragments):
                whitelisted += 1
                continue
            violations.append(f"{rel}:{lineno}: {line.strip()}")
        assert not violations, (
            "unexpected 'helix' literal(s) in cymatix_context/:\n"
            + "\n".join(violations)
        )
        assert whitelisted == _EXPECTED_WHITELISTED_LINES, (
            "whitelisted helix read-compat line count drifted: expected "
            f"{_EXPECTED_WHITELISTED_LINES}, found {whitelisted}. Update the "
            "whitelist deliberately if the read-compat surface changed."
        )
