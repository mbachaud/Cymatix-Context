"""Launcher → backend OTel env coupling.

Out of the box the launcher auto-starts the full Grafana/Tempo/Loki/
Prometheus stack (HELIX_OBSERVABILITY defaults "1") while the backend
emitted nothing (HELIX_OTEL_ENABLED defaults off) — an empty Grafana by
default. The fix: when the launcher starts (or adopts an external)
observability stack AND the collector's OTLP port is actually accepting
connections, it exports HELIX_OTEL_ENABLED=1 into its own environment
BEFORE spawning the helix child, so the child inherits it
(HelixSupervisor.start passes env through).

Two hard guards:
- An EXPLICIT user HELIX_OTEL_ENABLED — on or off — is never overridden.
- No export unless :4317 is reachable. ObservabilitySupervisor.start_all
  does NOT raise on the dominant failure mode (service spawned but never
  ready → STATUS_RED → normal return), and a backend dialing a dead
  collector wedges its gRPC channel, so the port probe is the gate.

The endpoint is deliberately NOT exported: the backend's own resolution
chain (env > [telemetry] toml > default) already lands on localhost:4317
by default, and synthesizing an env endpoint would stomp an explicit
user [telemetry] endpoint customization (env outranks toml).
"""

from __future__ import annotations

import os

import pytest

from helix_context.launcher import observability_health
from helix_context.launcher.app import (
    _export_otel_env_for_backend,
    _start_observability_stack,
)

_ENV_KEYS = ("HELIX_OTEL_ENABLED", "HELIX_OTEL_ENDPOINT")


@pytest.fixture(autouse=True)
def _clean_env():
    """Full save/restore isolation.

    The functions under test mutate os.environ DIRECTLY (that is the
    feature — children must inherit), so monkeypatch.delenv alone would
    leak the exported value into the rest of the test session (e.g.
    flipping real telemetry on for later create_app calls).
    """
    saved = {k: os.environ.pop(k) for k in _ENV_KEYS if k in os.environ}
    yield
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _collector_port_up(monkeypatch):
    """Default every test to 'collector OTLP port is reachable'."""
    monkeypatch.setattr(observability_health, "is_port_bound", lambda host, port: True)


# ── _export_otel_env_for_backend ──────────────────────────────────────


def test_export_sets_enabled_when_unset():
    _export_otel_env_for_backend()
    assert os.environ["HELIX_OTEL_ENABLED"] == "1"


def test_export_does_not_set_endpoint():
    """Endpoint resolution is left to env > toml > default in the backend;
    a synthesized env endpoint would override an explicit user
    [telemetry] endpoint in helix.toml."""
    _export_otel_env_for_backend()
    assert "HELIX_OTEL_ENDPOINT" not in os.environ


def test_export_respects_explicit_off(monkeypatch):
    monkeypatch.setenv("HELIX_OTEL_ENABLED", "0")
    _export_otel_env_for_backend()
    assert os.environ["HELIX_OTEL_ENABLED"] == "0"


def test_export_respects_explicit_on(monkeypatch):
    monkeypatch.setenv("HELIX_OTEL_ENABLED", "1")
    monkeypatch.setenv("HELIX_OTEL_ENDPOINT", "otherhost:9317")
    _export_otel_env_for_backend()
    assert os.environ["HELIX_OTEL_ENABLED"] == "1"
    assert os.environ["HELIX_OTEL_ENDPOINT"] == "otherhost:9317"


def test_export_preserves_user_endpoint(monkeypatch):
    monkeypatch.setenv("HELIX_OTEL_ENDPOINT", "customhost:9317")
    _export_otel_env_for_backend()
    assert os.environ["HELIX_OTEL_ENABLED"] == "1"
    assert os.environ["HELIX_OTEL_ENDPOINT"] == "customhost:9317"


def test_export_treats_blank_enabled_as_unset(monkeypatch):
    monkeypatch.setenv("HELIX_OTEL_ENABLED", "  ")
    _export_otel_env_for_backend()
    assert os.environ["HELIX_OTEL_ENABLED"] == "1"


def test_export_skipped_when_collector_port_down(monkeypatch):
    """start_all() returning normally does NOT prove the collector is up
    (readiness timeout → STATUS_RED → normal return; or _maybe_external
    triggered by a squatter on :8889 alone). The port probe is the gate."""
    monkeypatch.setattr(
        observability_health, "is_port_bound", lambda host, port: False
    )
    _export_otel_env_for_backend()
    assert "HELIX_OTEL_ENABLED" not in os.environ


def test_export_probes_the_collector_otlp_port(monkeypatch):
    probed = []

    def _probe(host, port):
        probed.append((host, port))
        return True

    monkeypatch.setattr(observability_health, "is_port_bound", _probe)
    _export_otel_env_for_backend()
    assert probed == [("127.0.0.1", observability_health.SERVICE_PORTS["collector"][0])]


# ── _start_observability_stack wiring ─────────────────────────────────


class _FakeSupervisor:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.start_calls = 0

    def start_all(self):
        self.start_calls += 1
        if self.fail:
            raise RuntimeError("boom")


def test_start_stack_exports_env_on_success():
    sup = _FakeSupervisor()
    _start_observability_stack(sup)
    assert sup.start_calls == 1
    assert os.environ["HELIX_OTEL_ENABLED"] == "1"


def test_start_stack_does_not_export_on_failure():
    """Raising start_all (missing binaries/configs) must be swallowed and
    must not export."""
    sup = _FakeSupervisor(fail=True)
    _start_observability_stack(sup)  # must swallow, not raise
    assert "HELIX_OTEL_ENABLED" not in os.environ


def test_start_stack_none_supervisor_is_noop():
    _start_observability_stack(None)
    assert "HELIX_OTEL_ENABLED" not in os.environ


def test_start_stack_success_still_respects_explicit_off(monkeypatch):
    monkeypatch.setenv("HELIX_OTEL_ENABLED", "0")
    _start_observability_stack(_FakeSupervisor())
    assert os.environ["HELIX_OTEL_ENABLED"] == "0"


def test_start_stack_success_but_dead_collector_does_not_export(monkeypatch):
    monkeypatch.setattr(
        observability_health, "is_port_bound", lambda host, port: False
    )
    _start_observability_stack(_FakeSupervisor())
    assert "HELIX_OTEL_ENABLED" not in os.environ
