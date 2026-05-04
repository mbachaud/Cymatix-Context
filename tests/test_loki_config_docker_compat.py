"""Regression: deploy/otel/loki-config.yaml is mounted by docker-compose
and uses the same path Loki's image-default expects, so the Docker
runtime's behavior is unchanged when we add an explicit config.

Spec §11.2 — locked: Loki config is shared by Docker and native runtimes.
"""

from pathlib import Path

import pytest

# pyyaml ships transitively (sentence-transformers, etc.) but isn't a
# declared dep; skip cleanly in barebones envs rather than ImportError.
yaml = pytest.importorskip("yaml")


REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "deploy" / "otel" / "docker-compose.yml"
LOKI_CFG = REPO / "deploy" / "otel" / "loki-config.yaml"


def test_loki_config_file_exists():
    assert LOKI_CFG.exists(), (
        "deploy/otel/loki-config.yaml must exist — both runtimes read it."
    )


def test_docker_compose_mounts_loki_config():
    text = COMPOSE.read_text(encoding="utf-8")
    spec = yaml.safe_load(text)
    loki = spec["services"]["loki"]
    volumes = loki.get("volumes", [])
    mount_target = "./loki-config.yaml:/etc/loki/local-config.yaml:ro"
    assert mount_target in volumes, (
        f"docker-compose loki service must mount {mount_target}; "
        f"got volumes={volumes}"
    )


def test_docker_compose_loki_command_points_at_mounted_config():
    spec = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    cmd = spec["services"]["loki"].get("command")
    assert cmd == ["-config.file=/etc/loki/local-config.yaml"], (
        f"docker-compose loki command must point at the mounted config; got {cmd!r}"
    )


def test_loki_config_parses_as_yaml_and_listens_on_3100():
    spec = yaml.safe_load(LOKI_CFG.read_text(encoding="utf-8"))
    # Loki's HTTP listen port is 3100 — must match docker-compose port mapping.
    assert spec["server"]["http_listen_port"] == 3100
