"""Tests for helix_context.launcher.observability_paths."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


def test_state_dir_returns_absolute_path():
    from helix_context.launcher.observability_paths import state_dir
    p = state_dir()
    assert p.is_absolute()
    # Always ends with .../observability
    assert p.name == "observability"


def test_state_dir_includes_helix_context_segment():
    """Sanity: helix-context segment appears so we don't accidentally
    hit a different app's data dir on a shared host."""
    from helix_context.launcher.observability_paths import state_dir
    p = state_dir()
    assert "helix-context" in str(p) or "helix_context" in str(p)


def test_per_service_state_dir():
    from helix_context.launcher.observability_paths import service_state_dir
    p = service_state_dir("prometheus")
    assert p.name == "prometheus"
    assert p.parent.name == "observability"


def test_logs_dir_is_under_state_dir():
    from helix_context.launcher.observability_paths import (
        logs_dir,
        state_dir,
    )
    assert logs_dir().parent == state_dir() or logs_dir() == state_dir()


def test_configs_dir_is_under_repo_tools_native_otel():
    from helix_context.launcher.observability_paths import configs_dir
    assert configs_dir().parts[-2:] == ("native-otel", "configs")


def test_binary_path_returns_per_service_path():
    from helix_context.launcher.observability_paths import binary_path
    p = binary_path("prometheus")
    assert p.parent.name == "prometheus"
    # Windows-only on this dev box; verify .exe suffix on Windows.
    if sys.platform == "win32":
        assert p.suffix == ".exe"


def test_state_dir_creates_on_request(tmp_path, monkeypatch):
    """state_dir(create=True) makes the dir if missing."""
    from helix_context.launcher import observability_paths as ops
    monkeypatch.setattr(ops, "_user_data_dir", lambda: tmp_path)
    p = ops.state_dir(create=True)
    assert p.exists()


def test_state_dir_no_doubled_helix_context_segment():
    """Spec §5 documents %LOCALAPPDATA%\\helix-context\\observability\\
    (single helix-context segment). Without appauthor=False, platformdirs
    on Windows produces ...\\helix-context\\helix-context\\... — pinning
    against that regression here."""
    from helix_context.launcher.observability_paths import state_dir
    parts = state_dir().parts
    assert parts.count("helix-context") == 1, (
        f"state_dir must contain exactly one 'helix-context' segment per "
        f"spec §5; got {state_dir()!s}"
    )
