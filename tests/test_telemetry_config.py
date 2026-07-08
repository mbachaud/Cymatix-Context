"""[telemetry] toml section + HELIX_OTEL_* env precedence.

Contract (2026-07-08, feat/telemetry-toml-defaults):

    HELIX_OTEL_* env var  >  [telemetry] in helix.toml  >  dataclass default

- Env vars ALWAYS win over toml, in both directions: an explicit
  HELIX_OTEL_ENABLED=0 must silence a toml ``enabled = true`` (the
  launcher's auto-export relies on this asymmetry — see
  tests/test_launcher_otel_export.py).
- An env var set to the empty string counts as unset (falls to toml).
- Env parse semantics are the historical ones from otel.py: ENABLED /
  INSECURE / LOGS_ENABLED are on iff "1"; REDACT_QUERY is on unless "0";
  SAMPLER_RATIO falls through on non-float garbage.
- ``load_config`` itself stays env-free for [telemetry]: resolution
  happens in otel.resolve_telemetry_settings() at setup time, so the
  default-honesty comparator (tests/test_config_default_honesty.py)
  never sees env-dependent values.
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import MagicMock

import pytest

from helix_context.config import TelemetryConfig, load_config
from helix_context.telemetry import otel as otel_mod
from helix_context.telemetry.otel import (
    _TELEMETRY_DEFAULTS,
    resolve_telemetry_settings,
    setup_telemetry,
)

_OTEL_ENV = (
    "HELIX_OTEL_ENABLED",
    "HELIX_OTEL_ENDPOINT",
    "HELIX_OTEL_INSECURE",
    "HELIX_OTEL_SAMPLER_RATIO",
    "HELIX_OTEL_REDACT_QUERY",
    "HELIX_OTEL_LOGS_ENABLED",
    "HELIX_OTEL_LOGS_LEVEL",
)


@pytest.fixture(autouse=True)
def _clean_otel_state(monkeypatch):
    """Neutralize the developer shell + module globals for every test."""
    for name in _OTEL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(otel_mod, "_initialised", False)
    monkeypatch.setattr(otel_mod, "_settings", dict(_TELEMETRY_DEFAULTS))


# ── dataclass + loader ────────────────────────────────────────────────


def test_telemetry_config_defaults():
    t = TelemetryConfig()
    assert t.enabled is False
    assert t.endpoint == "localhost:4317"
    assert t.insecure is True
    assert t.sampler_ratio == 1.0
    assert t.redact_query is True
    assert t.logs_enabled is True
    assert t.logs_level == "INFO"


def test_load_config_parses_telemetry_section(tmp_path):
    cfg_file = tmp_path / "helix.toml"
    cfg_file.write_text(
        "[telemetry]\n"
        "enabled = true\n"
        'endpoint = "otelhost:9999"\n'
        "insecure = false\n"
        "sampler_ratio = 0.25\n"
        "redact_query = false\n"
        "logs_enabled = false\n"
        'logs_level = "WARNING"\n',
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_file))
    assert cfg.telemetry.enabled is True
    assert cfg.telemetry.endpoint == "otelhost:9999"
    assert cfg.telemetry.insecure is False
    assert cfg.telemetry.sampler_ratio == 0.25
    assert cfg.telemetry.redact_query is False
    assert cfg.telemetry.logs_enabled is False
    assert cfg.telemetry.logs_level == "WARNING"


def test_load_config_without_telemetry_section_uses_defaults(tmp_path):
    cfg_file = tmp_path / "helix.toml"
    cfg_file.write_text("[server]\nport = 11437\n", encoding="utf-8")
    cfg = load_config(str(cfg_file))
    assert cfg.telemetry == TelemetryConfig()


def test_load_config_warns_on_unknown_telemetry_keys(tmp_path, caplog):
    cfg_file = tmp_path / "helix.toml"
    cfg_file.write_text("[telemetry]\nenabledd = true\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="helix_context.config"):
        load_config(str(cfg_file))
    assert "Unknown keys in [telemetry]" in caplog.text


# ── resolve_telemetry_settings precedence ─────────────────────────────


def test_resolve_defaults_when_no_env_no_toml():
    s = resolve_telemetry_settings(None)
    assert s == {
        "enabled": False,
        "endpoint": "localhost:4317",
        "insecure": True,
        "sampler_ratio": 1.0,
        "redact_query": True,
        "logs_enabled": True,
        "logs_level": "INFO",
    }


def test_resolve_toml_beats_default():
    s = resolve_telemetry_settings(
        TelemetryConfig(
            enabled=True,
            endpoint="tomlhost:4317",
            insecure=False,
            sampler_ratio=0.5,
            redact_query=False,
            logs_enabled=False,
            logs_level="ERROR",
        )
    )
    assert s["enabled"] is True
    assert s["endpoint"] == "tomlhost:4317"
    assert s["insecure"] is False
    assert s["sampler_ratio"] == 0.5
    assert s["redact_query"] is False
    assert s["logs_enabled"] is False
    assert s["logs_level"] == "ERROR"


def test_resolve_env_beats_toml(monkeypatch):
    monkeypatch.setenv("HELIX_OTEL_ENABLED", "1")
    monkeypatch.setenv("HELIX_OTEL_ENDPOINT", "envhost:1111")
    monkeypatch.setenv("HELIX_OTEL_SAMPLER_RATIO", "0.1")
    s = resolve_telemetry_settings(
        TelemetryConfig(enabled=False, endpoint="tomlhost:2222", sampler_ratio=0.9)
    )
    assert s["enabled"] is True
    assert s["endpoint"] == "envhost:1111"
    assert s["sampler_ratio"] == 0.1


def test_resolve_explicit_env_off_beats_toml_on(monkeypatch):
    monkeypatch.setenv("HELIX_OTEL_ENABLED", "0")
    s = resolve_telemetry_settings(TelemetryConfig(enabled=True))
    assert s["enabled"] is False


def test_resolve_empty_env_counts_as_unset(monkeypatch):
    monkeypatch.setenv("HELIX_OTEL_ENABLED", "")
    monkeypatch.setenv("HELIX_OTEL_ENDPOINT", "")
    s = resolve_telemetry_settings(TelemetryConfig(enabled=True, endpoint="tomlhost:2222"))
    assert s["enabled"] is True
    assert s["endpoint"] == "tomlhost:2222"


def test_resolve_bad_sampler_ratio_env_falls_back_to_toml(monkeypatch):
    monkeypatch.setenv("HELIX_OTEL_SAMPLER_RATIO", "not-a-float")
    s = resolve_telemetry_settings(TelemetryConfig(sampler_ratio=0.3))
    assert s["sampler_ratio"] == 0.3


def test_resolve_legacy_env_bool_semantics(monkeypatch):
    """ENABLED/INSECURE/LOGS_ENABLED are on iff '1'; REDACT off iff '0'."""
    monkeypatch.setenv("HELIX_OTEL_ENABLED", "true")  # historically NOT on
    monkeypatch.setenv("HELIX_OTEL_INSECURE", "0")
    monkeypatch.setenv("HELIX_OTEL_REDACT_QUERY", "0")
    monkeypatch.setenv("HELIX_OTEL_LOGS_ENABLED", "no")
    s = resolve_telemetry_settings(None)
    assert s["enabled"] is False
    assert s["insecure"] is False
    assert s["redact_query"] is False
    assert s["logs_enabled"] is False


# ── setup_telemetry consumes the resolved config ──────────────────────


def test_setup_telemetry_disabled_by_default(caplog):
    with caplog.at_level(logging.INFO, logger="helix.telemetry"):
        assert setup_telemetry(app=None, config=TelemetryConfig()) is False
    assert "OTel disabled" in caplog.text


def test_setup_telemetry_toml_only_enablement_passes_gate(monkeypatch, caplog):
    """[telemetry] enabled=true with NO env vars must reach the SDK import.

    The opentelemetry import is stubbed to fail so the test never
    initialises a real exporter; reaching the packages-missing branch
    proves the gate consumed the toml value rather than the env default.
    """
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    with caplog.at_level(logging.WARNING, logger="helix.telemetry"):
        assert setup_telemetry(app=None, config=TelemetryConfig(enabled=True)) is False
    assert "OTel packages not installed" in caplog.text


def test_setup_telemetry_env_off_beats_toml_on(monkeypatch, caplog):
    monkeypatch.setenv("HELIX_OTEL_ENABLED", "0")
    with caplog.at_level(logging.INFO, logger="helix.telemetry"):
        assert setup_telemetry(app=None, config=TelemetryConfig(enabled=True)) is False
    assert "OTel disabled" in caplog.text


def test_setup_telemetry_env_on_beats_toml_off(monkeypatch, caplog):
    monkeypatch.setenv("HELIX_OTEL_ENABLED", "1")
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    with caplog.at_level(logging.WARNING, logger="helix.telemetry"):
        assert setup_telemetry(app=None, config=TelemetryConfig(enabled=False)) is False
    assert "OTel packages not installed" in caplog.text


def test_setup_telemetry_recall_updates_settings_when_initialised(monkeypatch):
    """A second setup_telemetry call after SDK init must still publish the
    (possibly different) config's runtime knobs to _settings — otherwise a
    create_app(config) following a legacy env-only init silently drops the
    operator's [telemetry] redact_query / logs settings."""
    monkeypatch.setattr(otel_mod, "_initialised", True)
    assert (
        setup_telemetry(app=None, config=TelemetryConfig(redact_query=False))
        is True
    )
    assert otel_mod._settings["redact_query"] is False


# ── runtime readers honor toml when env is unset ──────────────────────


def test_redact_query_honors_toml_setting(monkeypatch):
    monkeypatch.setattr(
        otel_mod, "_settings", {**_TELEMETRY_DEFAULTS, "redact_query": False}
    )
    assert otel_mod._redact_query("secret question") == "secret question"


def test_redact_query_env_beats_settings(monkeypatch):
    monkeypatch.setattr(
        otel_mod, "_settings", {**_TELEMETRY_DEFAULTS, "redact_query": False}
    )
    monkeypatch.setenv("HELIX_OTEL_REDACT_QUERY", "1")
    assert "hash:" in otel_mod._redact_query("raw query")


def test_attach_otlp_logging_handler_skips_when_toml_disables(monkeypatch):
    """No env set; module settings carry logs_enabled=False from toml."""
    monkeypatch.setattr(
        otel_mod, "_settings", {**_TELEMETRY_DEFAULTS, "logs_enabled": False}
    )
    LoggerProvider = MagicMock()
    LoggingHandler = MagicMock()
    OTLPLogExporter = MagicMock()
    set_logger_provider = MagicMock()
    otel_mod._attach_otlp_logging_handler(
        endpoint="localhost:4317",
        insecure=True,
        resource=MagicMock(),
        LoggerProvider=LoggerProvider,
        BatchLogRecordProcessor=MagicMock(),
        OTLPLogExporter=OTLPLogExporter,
        LoggingHandler=LoggingHandler,
        set_logger_provider=set_logger_provider,
    )
    LoggerProvider.assert_not_called()
    LoggingHandler.assert_not_called()
    OTLPLogExporter.assert_not_called()
    set_logger_provider.assert_not_called()
