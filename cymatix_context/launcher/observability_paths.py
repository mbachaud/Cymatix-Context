"""Path resolution for native observability state, configs, binaries.

Single source of truth for "where does X live" — used by:
  - observability_render.py to render container paths into rendered configs
  - observability_supervisor.py to spawn binaries with the right --data-dir
  - the install script to land the binaries
  - the tray "Open log directory" menu

State dir uses platformdirs.user_data_dir, so:
  Windows: %LOCALAPPDATA%\\helix-context\\observability\\<service>\\
  Linux:   ~/.local/share/helix-context/observability/<service>/
  macOS:   ~/Library/Application Support/helix-context/observability/<service>/

Binaries live in the repo at tools/native-otel/<service>/<exe>, NOT in
state-dir, so the install script can hash-verify them and so a uninstall
is `rm -rf tools/native-otel/<service>`.
"""

from __future__ import annotations

import sys
from pathlib import Path


_APP_NAME = "helix-context"
_BINARY_LAYOUT = {
    "collector": ("collector", "otelcol-contrib"),
    "prometheus": ("prometheus", "prometheus"),
    "tempo": ("tempo", "tempo"),
    "loki": ("loki", "loki"),
    "grafana": ("grafana", "bin/grafana-server"),
}


# ── Manifest constants ──────────────────────────────────────────────
# Single source of truth for the 5-service / 5-config pair used by:
#   - observability_supervisor (start-order, verify_configs, verify_binaries)
#   - observability_render (_RULES output names)
#   - launcher.app._observability_install_complete
# Keeping these here (rather than in supervisor) lets the install-complete
# check on app.py read them without importing the supervisor module, which
# pulls in pywin32 + threading scaffolding we don't want at app-import time.
ALL_SERVICES: tuple[str, ...] = (
    "collector",
    "prometheus",
    "tempo",
    "loki",
    "grafana",
)

ALL_CONFIG_FILES: tuple[str, ...] = (
    "otel-collector-config.yaml",
    "prometheus.yml",
    "tempo.yaml",
    "loki-config.yaml",
    "datasources.yml",
    "dashboards.yml",
)


def _user_data_dir() -> Path:
    """Wrap platformdirs.user_data_dir; isolated for monkeypatching in tests."""
    try:
        from platformdirs import user_data_dir
    except ImportError as exc:
        raise RuntimeError(
            "platformdirs is required. "
            "Install with: pip install cymatix-context[launcher]"
        ) from exc
    # appauthor=False on Windows omits the appauthor folder; without it,
    # platformdirs reuses appname for both, producing a doubled
    # ...\helix-context\helix-context\... segment. Spec §5 documents the
    # single-segment form, so opt out.
    return Path(user_data_dir(_APP_NAME, appauthor=False))


def _repo_root() -> Path:
    # observability_paths.py lives at cymatix_context/launcher/observability_paths.py
    # so root is two parents up.
    return Path(__file__).resolve().parent.parent.parent


def state_dir(create: bool = False) -> Path:
    """Return the per-user observability state directory."""
    p = _user_data_dir() / "observability"
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def service_state_dir(service: str, create: bool = False) -> Path:
    """Return the per-service state directory under state_dir()."""
    p = state_dir() / service
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir(create: bool = False) -> Path:
    """Return the directory holding rotated <service>.log files.

    Co-located with state_dir() so the user can `Open log directory` from
    the tray and see everything in one place.
    """
    return state_dir(create=create)


def configs_dir(create: bool = False) -> Path:
    """Return tools/native-otel/configs in the repo (rendered configs)."""
    p = _repo_root() / "tools" / "native-otel" / "configs"
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def binary_path(service: str) -> Path:
    """Return the absolute path to the binary for <service>.

    service ∈ {"collector", "prometheus", "tempo", "loki", "grafana"}.
    """
    if service not in _BINARY_LAYOUT:
        raise ValueError(f"unknown service {service!r}")
    folder, exe_rel = _BINARY_LAYOUT[service]
    if sys.platform == "win32":
        # Append .exe to the leaf name only.
        head, _, leaf = exe_rel.rpartition("/")
        exe_rel = (head + "/" if head else "") + leaf + ".exe"
    return _repo_root() / "tools" / "native-otel" / folder / exe_rel
