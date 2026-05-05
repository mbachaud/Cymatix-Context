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


# ── Manifest constants (single source of truth for service+config names) ──


def test_all_services_manifest_constant_exists_and_matches_spec():
    """ALL_SERVICES tuple lives in observability_paths so supervisor +
    render + app can import a single source of truth. Order matches
    spec §7.3 spawn-order doc-prose: collector, prometheus, tempo,
    loki, grafana."""
    from helix_context.launcher.observability_paths import ALL_SERVICES
    assert isinstance(ALL_SERVICES, tuple)
    assert ALL_SERVICES == (
        "collector",
        "prometheus",
        "tempo",
        "loki",
        "grafana",
    )


def test_all_config_files_manifest_constant_exists_and_matches_spec():
    """ALL_CONFIG_FILES tuple is the rendered-config filename list
    used by the install-complete check, supervisor pre-flight verify,
    and the render module's _RULES."""
    from helix_context.launcher.observability_paths import ALL_CONFIG_FILES
    assert isinstance(ALL_CONFIG_FILES, tuple)
    assert ALL_CONFIG_FILES == (
        "otel-collector-config.yaml",
        "prometheus.yml",
        "tempo.yaml",
        "loki-config.yaml",
        "datasources.yml",
        "dashboards.yml",
    )


def test_supervisor_all_services_is_same_set_as_paths_manifest():
    """observability_supervisor.ALL_SERVICES must contain the same set of
    services as the manifest in observability_paths — the supervisor's
    list is constrained to spawn-phase order (which differs from manifest
    order) but the membership is the single source of truth."""
    from helix_context.launcher import observability_paths as paths_mod
    from helix_context.launcher import observability_supervisor as sup_mod
    assert set(sup_mod.ALL_SERVICES) == set(paths_mod.ALL_SERVICES)
