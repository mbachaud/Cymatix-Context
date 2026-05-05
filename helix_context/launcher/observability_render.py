"""Render runtime configs from deploy/otel sources for the native sidecar.

The deploy/otel YAMLs bake in Docker-Compose service-DNS hostnames
(`tempo:4317`, `http://prometheus:9090/...`, `http://loki:3100/otlp`,
`otel-collector:8889`) and Linux container paths (`/var/tempo/...`,
`/var/loki/...`, `/loki`). For the native runtime we substitute these
to `localhost:*` and `platformdirs.user_data_dir(...)` paths.

No structural changes. The diff between source and render is hostnames
+ paths only, enforced by tests/test_observability_render.py.

Usage:
    python -m helix_context.launcher.observability_render render-all
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .observability_paths import (
    ALL_CONFIG_FILES,
    binary_path,
    configs_dir,
    service_state_dir,
    state_dir,
)

log = logging.getLogger("helix.launcher.render")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _deploy_dir() -> Path:
    return _repo_root() / "deploy" / "otel"


# ── substitution rules ───────────────────────────────────────────────
# Each rule: (compiled regex, replacement fn). Replacement fn returns the
# substituted string given the match.

def _path_for(service: str, *parts: str) -> str:
    """Return a forward-slashed absolute path string under the per-service
    state dir. We use forward slashes uniformly because Tempo/Loki/Grafana
    accept them on Windows AND yaml-load doesn't choke on backslashes.
    """
    base = service_state_dir(service)
    full = base.joinpath(*parts) if parts else base
    return str(full).replace("\\", "/")


def _sub_collector(text: str) -> str:
    text = text.replace("endpoint: tempo:4317", "endpoint: localhost:4317")
    text = text.replace(
        "endpoint: http://prometheus:9090/api/v1/write",
        "endpoint: http://localhost:9090/api/v1/write",
    )
    text = text.replace(
        "endpoint: http://loki:3100/otlp",
        "endpoint: http://localhost:3100/otlp",
    )
    # Doc-comment header at top of the source file mentions Docker DNS
    # hostnames in prose ("traces -> Tempo (http://tempo:4317)"). Rewrite
    # to localhost too — pure documentation text, but keeps the rendered
    # config self-consistent with what the native runtime actually does.
    text = text.replace("http://tempo:4317", "http://localhost:4317")
    text = text.replace(
        "http://prometheus:9090/api/v1/write",
        "http://localhost:9090/api/v1/write",
    )
    text = text.replace(
        "http://loki:3100/otlp/v1/logs",
        "http://localhost:3100/otlp/v1/logs",
    )
    return text


def _sub_prometheus(text: str) -> str:
    return text.replace("'otel-collector:8889'", "'localhost:8889'")


def _sub_tempo(text: str) -> str:
    text = text.replace("/var/tempo/traces", _path_for("tempo", "traces"))
    text = text.replace("/var/tempo/wal", _path_for("tempo", "wal"))
    text = text.replace(
        "/var/tempo/generator/wal",
        _path_for("tempo", "generator", "wal"),
    )
    text = text.replace(
        "url: http://prometheus:9090/api/v1/write",
        "url: http://localhost:9090/api/v1/write",
    )
    return text


def _sub_loki(text: str) -> str:
    """Loki's source uses `/loki` as path_prefix; we redirect to
    state_dir/loki and keep the chunks_directory + rules_directory etc.
    relative to that prefix.

    Loki resolves chunks_directory and rules_directory as ABSOLUTE paths
    if they start with '/', so we substitute the full absolute path
    rather than relying on path_prefix interpolation.
    """
    text = text.replace("path_prefix: /loki", f"path_prefix: {_path_for('loki')}")
    text = text.replace(
        "chunks_directory: /loki/chunks",
        f"chunks_directory: {_path_for('loki', 'chunks')}",
    )
    text = text.replace(
        "rules_directory: /loki/rules",
        f"rules_directory: {_path_for('loki', 'rules')}",
    )
    return text


def _sub_datasources(text: str) -> str:
    text = text.replace("url: http://prometheus:9090", "url: http://localhost:9090")
    text = text.replace("url: http://tempo:3200", "url: http://localhost:3200")
    text = text.replace("url: http://loki:3100", "url: http://localhost:3100")
    return text


def _grafana_home() -> Path:
    return binary_path("grafana").parent.parent


def _grafana_dashboards_path() -> Path:
    return _grafana_home() / "conf" / "provisioning" / "dashboards-content"


def _sub_dashboards(text: str) -> str:
    return text.replace(
        "path: /var/lib/grafana/dashboards",
        f"path: {_grafana_dashboards_path().as_posix()}",
    )


_RULES = [
    # (source path relative to deploy/otel, rendered name, sub fn).
    # The rendered names below MUST equal observability_paths.ALL_CONFIG_FILES
    # (single source of truth for the rendered-config filename set).
    ("otel-collector-config.yaml", "otel-collector-config.yaml", _sub_collector),
    ("prometheus.yml", "prometheus.yml", _sub_prometheus),
    ("tempo.yaml", "tempo.yaml", _sub_tempo),
    ("loki-config.yaml", "loki-config.yaml", _sub_loki),
    (
        "grafana/provisioning/datasources/datasources.yml",
        "datasources.yml",
        _sub_datasources,
    ),
    (
        "grafana/provisioning/dashboards/dashboards.yml",
        "dashboards.yml",
        _sub_dashboards,
    ),
]
# Verify _RULES output names match the manifest at import time so any
# drift fails loudly (rather than the supervisor's _verify_configs
# discovering a mismatch only at start_all() time).
assert tuple(dst for _, dst, _ in _RULES) == ALL_CONFIG_FILES, (
    f"_RULES output names {tuple(dst for _, dst, _ in _RULES)} drifted "
    f"from manifest ALL_CONFIG_FILES {ALL_CONFIG_FILES}"
)


def _wire_grafana_provisioning() -> None:
    """Copy the rendered datasources + source dashboards into Grafana's
    conf/provisioning tree so Grafana auto-loads them at startup.

    Grafana resolves provisioning relative to its --homepath, not relative
    to a CLI flag, so the rendered datasources.yml has to physically land
    at <graf_home>/conf/provisioning/datasources/datasources.yml.

    Best-effort: skips silently if Grafana isn't installed (the config
    render runs before the binary may have been extracted in some flows).
    """
    import shutil

    graf_bin = binary_path("grafana")
    if not graf_bin.exists():
        log.info("grafana binary absent — skipping provisioning wire-up")
        return
    graf_home = _grafana_home()  # tools/native-otel/grafana

    rendered_cfg = configs_dir()
    target_ds_dir = graf_home / "conf" / "provisioning" / "datasources"
    target_ds_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rendered_cfg / "datasources.yml", target_ds_dir / "datasources.yml")

    target_dash_prov = graf_home / "conf" / "provisioning" / "dashboards"
    target_dash_prov.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rendered_cfg / "dashboards.yml", target_dash_prov / "dashboards.yml")

    src_dash = _deploy_dir() / "grafana" / "dashboards"
    if src_dash.exists():
        target_dash = _grafana_dashboards_path()
        target_dash.mkdir(parents=True, exist_ok=True)
        for f in src_dash.iterdir():
            if f.is_file():
                shutil.copy2(f, target_dash / f.name)


def render_all() -> list[Path]:
    """Render every source into configs_dir(); return list of written paths.

    Creates state_dir() (and per-service subdirs touched in the rendered
    output) so binaries can write to them at first launch. Also wires the
    rendered datasources into Grafana's provisioning tree.
    """
    out_dir = configs_dir(create=True)
    state_dir(create=True)
    written: list[Path] = []
    for src_rel, dst_name, sub_fn in _RULES:
        src = _deploy_dir() / src_rel
        if not src.exists():
            raise FileNotFoundError(f"render: source missing: {src}")
        rendered = sub_fn(src.read_text(encoding="utf-8"))
        dst = out_dir / dst_name
        dst.write_text(rendered, encoding="utf-8")
        written.append(dst)
        log.info("render: wrote %s (%d bytes)", dst, len(rendered))

    # Ensure per-service state dirs exist (binaries need to write here).
    for svc in ("prometheus", "tempo", "loki", "grafana"):
        service_state_dir(svc, create=True)

    try:
        _wire_grafana_provisioning()
    except Exception:
        log.warning("grafana provisioning wire-up failed", exc_info=True)

    return written


def _cli(argv=None) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("render-all")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    )
    if args.cmd == "render-all":
        for p in render_all():
            print(str(p))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
