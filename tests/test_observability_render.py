"""Tests for observability_render — feeds each deploy/otel source YAML
through the render step and asserts the substitutions per spec §6.3 + §9.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy" / "otel"


@pytest.fixture
def rendered(tmp_path, monkeypatch):
    """Render every source YAML into tmp_path and return the dir."""
    from helix_context.launcher import observability_paths as ops
    from helix_context.launcher import observability_render as rnd

    # Redirect state_dir AND configs_dir into tmp_path so the test
    # doesn't write to the real repo or AppData.
    monkeypatch.setattr(ops, "_user_data_dir", lambda: tmp_path / "appdata")
    monkeypatch.setattr(
        rnd, "configs_dir",
        lambda create=False: (tmp_path / "configs"),
    )
    (tmp_path / "configs").mkdir(parents=True, exist_ok=True)

    rnd.render_all()
    return tmp_path / "configs"


def test_all_configs_rendered(rendered):
    """Each source has a corresponding rendered file."""
    expected = {
        "otel-collector-config.yaml",
        "prometheus.yml",
        "tempo.yaml",
        "loki-config.yaml",
        "datasources.yml",
        "dashboards.yml",
    }
    actual = {p.name for p in rendered.iterdir() if p.is_file()}
    assert expected.issubset(actual), (
        f"missing rendered files: {expected - actual}"
    )


def test_collector_hostnames_rewritten_to_localhost(rendered):
    from helix_context.launcher.observability_render import TEMPO_OTLP_PORT

    text = (rendered / "otel-collector-config.yaml").read_text()
    spec = yaml.safe_load(text)
    # tempo:4317 → localhost:<TEMPO_OTLP_PORT> (remapped off 4317 so the
    # collector's own intake on 4317 has no conflict on bare-metal localhost).
    assert spec["exporters"]["otlp/tempo"]["endpoint"] == f"localhost:{TEMPO_OTLP_PORT}"
    # http://prometheus:9090/api/v1/write → http://localhost:9090/api/v1/write
    assert spec["exporters"]["prometheusremotewrite"]["endpoint"] == \
        "http://localhost:9090/api/v1/write"
    # http://loki:3100/otlp → http://localhost:3100/otlp
    assert spec["exporters"]["otlphttp/loki"]["endpoint"] == \
        "http://localhost:3100/otlp"


def test_tempo_otlp_receiver_remapped_off_collector_port(rendered):
    """Tempo's OTLP gRPC receiver must move off 4317 in the native render
    so the supervisor's external-instance pre-flight check doesn't mistake
    a healthy tempo for a pre-existing collector and skip spawning ours.

    Regression test for the wiring bug where helix's OTel exporter saw
    `StatusCode.UNIMPLEMENTED` because tempo (which only accepts traces)
    was answering on 4317.
    """
    from helix_context.launcher.observability_render import TEMPO_OTLP_PORT

    spec = yaml.safe_load((rendered / "tempo.yaml").read_text())
    otlp_grpc = spec["distributor"]["receivers"]["otlp"]["protocols"]["grpc"]
    assert otlp_grpc["endpoint"] == f"0.0.0.0:{TEMPO_OTLP_PORT}"
    assert TEMPO_OTLP_PORT != 4317, "remapped port must not be the collector's"


def test_prometheus_scrape_target_rewritten(rendered):
    spec = yaml.safe_load((rendered / "prometheus.yml").read_text())
    # otel-collector:8889 → localhost:8889
    targets = spec["scrape_configs"][0]["static_configs"][0]["targets"]
    assert targets == ["localhost:8889"]


def test_tempo_paths_rewritten_to_state_dir(rendered, tmp_path):
    spec = yaml.safe_load((rendered / "tempo.yaml").read_text())
    appdata = tmp_path / "appdata"

    storage = spec["storage"]["trace"]
    # /var/tempo/traces → <state>/tempo/traces
    assert "/var/tempo" not in storage["local"]["path"]
    assert storage["local"]["path"].startswith(str(appdata).replace("\\", "/")) \
        or str(appdata) in storage["local"]["path"]
    assert storage["local"]["path"].endswith("tempo/traces") or \
           storage["local"]["path"].endswith("tempo\\traces")
    # /var/tempo/wal
    assert "/var/tempo" not in storage["wal"]["path"]
    # /var/tempo/generator/wal
    gen = spec["metrics_generator"]["storage"]
    assert "/var/tempo" not in gen["path"]
    # tempo's metrics_generator remote_write hostname
    rw = gen["remote_write"][0]["url"]
    assert rw == "http://localhost:9090/api/v1/write"


def test_loki_paths_rewritten(rendered, tmp_path):
    spec = yaml.safe_load((rendered / "loki-config.yaml").read_text())
    # /loki/chunks → <state>/loki/chunks
    chunks = spec["common"]["storage"]["filesystem"]["chunks_directory"]
    assert chunks.startswith(str(tmp_path / "appdata").replace("\\", "/")) \
        or str(tmp_path / "appdata") in chunks
    assert "/loki/chunks" not in chunks or chunks.endswith("loki/chunks")


def test_grafana_datasources_use_localhost(rendered):
    spec = yaml.safe_load((rendered / "datasources.yml").read_text())
    by_name = {d["name"]: d for d in spec["datasources"]}
    assert by_name["Prometheus"]["url"] == "http://localhost:9090"
    assert by_name["Tempo"]["url"] == "http://localhost:3200"
    assert by_name["Loki"]["url"] == "http://localhost:3100"


def test_grafana_dashboard_provider_uses_native_dashboard_path(rendered):
    spec = yaml.safe_load((rendered / "dashboards.yml").read_text())
    providers = spec["providers"]
    provider_path = providers[0]["options"]["path"]
    assert provider_path.replace("\\", "/").endswith(
        "grafana/conf/provisioning/dashboards-content"
    )
    assert "/var/lib/grafana/dashboards" not in provider_path


def test_no_docker_dns_hostnames_remain_in_any_render(rendered):
    """Cross-cutting check: no rendered file mentions a Docker DNS name.

    Catches accidental drift if a future config-source adds another
    container hostname that the render module didn't know about.

    NOTE: pattern requires <host>:<digit> (a real port) — bare substrings
    like "tempo:" would also match legitimate YAML keys (`otlp/tempo:`,
    `otlphttp/loki:`, `prometheus:` exporter name) which spec §6.3
    forbids touching.
    """
    docker_host_re = re.compile(
        r"\b(?:tempo|prometheus|loki|otel-collector):\d"
    )
    for f in rendered.iterdir():
        if not f.is_file() or f.name == ".gitkeep":
            continue
        text = f.read_text()
        m = docker_host_re.search(text)
        assert m is None, (
            f"{f.name}: still mentions Docker DNS hostname:port "
            f"{m.group(0)!r} after render — render module needs an "
            f"extra rule."
        )


def test_structural_diff_is_only_hostnames_and_paths(rendered):
    """Ingest source + rendered, normalize hostnames+paths to placeholders,
    assert remaining structural diff is empty. Catches accidental
    structural drift between Docker and native runtimes.

    NOTE: layered normalization (URL → bare host:port → filesystem path)
    rather than a single greedy regex. The plan body's single-regex form
    false-matched `p://` as a Windows drive letter, producing different
    norm output between source ('p://tempo:4317)' eaten as a path) and
    render ('http://localhost:4317' caught only by host-port branch) —
    so the test failed even when no real structural drift existed.
    """
    pairs = [
        ("otel-collector-config.yaml", "otel-collector-config.yaml"),
        ("prometheus.yml", "prometheus.yml"),
        ("tempo.yaml", "tempo.yaml"),
        ("loki-config.yaml", "loki-config.yaml"),
        ("grafana/provisioning/datasources/datasources.yml", "datasources.yml"),
        ("grafana/provisioning/dashboards/dashboards.yml", "dashboards.yml"),
    ]
    url_re = re.compile(
        r"https?://(?:localhost|tempo|prometheus|loki|otel-collector|grafana)"
        r"(?::\d+)?(?:/\S*)?"
    )
    hostport_re = re.compile(
        # 0.0.0.0:\d+ is included so the tempo OTLP receiver remap
        # (`0.0.0.0:4317` → `0.0.0.0:14317`) normalizes to the same
        # placeholder in source and render. The remap is intentional
        # structural diff (see observability_render.TEMPO_OTLP_PORT) —
        # the dedicated test_tempo_otlp_receiver_remapped_off_collector_port
        # asserts the new value, so this normalizer doesn't hide drift.
        r"\b(?:localhost|tempo|prometheus|loki|otel-collector|0\.0\.0\.0):\d+\b"
    )
    path_re = re.compile(
        r"(?:[A-Z]:)?[/\\](?:[\w.-]+[/\\])*"
        r"(?:tempo|loki|prometheus|grafana)(?:[/\\][\w.-]+)*"
    )

    def _norm(s: str) -> str:
        s = url_re.sub("<URL>", s)
        s = hostport_re.sub("<HOSTPORT>", s)
        s = path_re.sub("<PATH>", s)
        return s

    for src_rel, dst_name in pairs:
        src = (DEPLOY / src_rel).read_text()
        dst = (rendered / dst_name).read_text()
        src_norm = _norm(src)
        dst_norm = _norm(dst)
        assert src_norm == dst_norm, (
            f"{dst_name}: structural diff is more than hostnames+paths.\n"
            f"--- src normalized ---\n{src_norm}\n"
            f"--- dst normalized ---\n{dst_norm}"
        )
