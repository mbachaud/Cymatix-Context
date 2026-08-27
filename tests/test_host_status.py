"""Host-aware status contracts for ``cymatix-status``."""

from __future__ import annotations

import json

import pytest

import cymatix_context.cli.cymatix_status as status_mod
from cymatix_context.cli.cymatix_status import (
    ProbeResult,
    configured_ready,
    determine_mcp_live,
    guided_ready,
    map_launcher_state,
    map_server_health,
)
from cymatix_context.integrations.host_profiles import HostDiscovery


@pytest.mark.parametrize(
    ("configuration", "activation", "health", "expected"),
    [
        ("missing", "enabled", "healthy", False),
        ("noncanonical", "enabled", "healthy", False),
        ("invalid", "enabled", "healthy", False),
        ("canonical", "disabled", "healthy", False),
        ("canonical", "enabled", "healthy", True),
        ("canonical", "enabled", "unhealthy", False),
        ("canonical", "enabled", "unknown", False),
        ("canonical", "unknown", "healthy", None),
        ("canonical", "unknown", "unhealthy", False),
        ("canonical", "unknown", "unknown", False),
    ],
)
def test_configured_ready_truth_table(configuration, activation, health, expected):
    assert configured_ready(
        configuration=configuration,
        activation=activation,
        health=health,
    ) is expected


def test_guided_ready_separates_skill_from_mcp():
    assert guided_ready(True, "missing", "unknown") is False
    assert guided_ready(True, "present", "disabled") is False
    assert guided_ready(True, "present", "unknown") is None
    assert guided_ready(None, "present", "enabled") is None
    assert guided_ready(True, "present", "enabled") is True


def test_live_requires_exact_handle_and_host_match():
    registry = ProbeResult(
        transport="reachable",
        payload={
            "participants": [
                {"handle": "codex-agent", "mcp_host": "codex", "status": "active"}
            ]
        },
        parse_error=None,
        error=None,
    )
    assert determine_mcp_live(
        handle="codex-agent",
        configured_host="codex",
        registry=registry,
    ).state == "connected"
    assert determine_mcp_live(
        handle="codex-agent",
        configured_host="gemini-cli",
        registry=registry,
    ).state == "disconnected"
    assert determine_mcp_live(
        handle=None,
        configured_host="codex",
        registry=registry,
    ).state == "unknown"


@pytest.mark.parametrize(
    ("probe", "transport", "health", "error"),
    [
        (
            ProbeResult("unreachable", None, None, "connection refused"),
            "unreachable",
            "unknown",
            "connection refused",
        ),
        (ProbeResult("reachable", {"status": "ok"}, None, None), "reachable", "healthy", None),
        (
            ProbeResult("reachable", {"status": "degraded"}, None, None),
            "reachable",
            "unhealthy",
            None,
        ),
        (
            ProbeResult("reachable", {"error": "maintenance"}, None, "HTTP 503"),
            "reachable",
            "unhealthy",
            "HTTP 503",
        ),
        (
            ProbeResult("reachable", None, "Expecting value at line 1", None),
            "reachable",
            "unknown",
            None,
        ),
    ],
)
def test_server_health_mapping(probe, transport, health, error):
    report = map_server_health(probe, url="http://127.0.0.1:11437")
    assert (report.transport, report.health, report.error) == (
        transport,
        health,
        error,
    )
    assert report.parse_error == probe.parse_error


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        (None, "not_configured"),
        (ProbeResult("unreachable", None, None, "refused"), "unreachable"),
        (ProbeResult("reachable", {"cymatix": {"running": True}}, None, None), "running"),
        (ProbeResult("reachable", {"cymatix": {"running": False}}, None, None), "stopped"),
        (ProbeResult("reachable", {}, None, None), "unreachable"),
    ],
)
def test_launcher_state_mapping_is_independent(probe, expected):
    assert map_launcher_state(probe) == expected


@pytest.mark.parametrize(
    ("launcher_url", "launcher_probe", "expected_state"),
    [
        (
            "http://127.0.0.1:11438",
            ProbeResult("reachable", {"cymatix": {"running": True}}, None, None),
            "running",
        ),
        (
            "http://127.0.0.1:11438",
            ProbeResult("reachable", {"cymatix": {"running": False}}, None, None),
            "stopped",
        ),
        (
            "http://127.0.0.1:11438",
            ProbeResult("unreachable", None, None, "connection refused"),
            "unreachable",
        ),
        (None, None, "not_configured"),
    ],
)
def test_launcher_state_never_gates_readiness(
    monkeypatch,
    tmp_path,
    launcher_url,
    launcher_probe,
    expected_state,
):
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        "[mcp_servers.cymatix-context]\n"
        'command = "python"\n'
        'args = ["-m", "cymatix_context.mcp_server"]\n'
        "enabled = true\n\n"
        "[mcp_servers.cymatix-context.env]\n"
        'CYMATIX_MCP_URL = "http://127.0.0.1:11999"\n'
        'CYMATIX_MCP_HANDLE = "codex-agent"\n'
        'CYMATIX_MCP_HOST = "codex"\n',
        encoding="utf-8",
    )
    skill = tmp_path / ".agents" / "skills" / "cymatix-context"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: cymatix-context\n---\n", encoding="utf-8")

    seen_urls = []

    def fake_probe(url, timeout_s=status_mod.DEFAULT_STATUS_TIMEOUT_S):
        seen_urls.append(url)
        if url.endswith("/health"):
            return ProbeResult("reachable", {"status": "ok"}, None, None)
        if url.endswith("/api/state"):
            assert launcher_probe is not None
            return launcher_probe
        if url.endswith("/sessions?status=active"):
            return ProbeResult("reachable", {"participants": []}, None, None)
        raise AssertionError(url)

    monkeypatch.setattr(status_mod, "_probe_json", fake_probe)
    result = status_mod.collect_status(
        host="codex",
        start_dir=tmp_path,
        home_dir=tmp_path,
        launcher_url=launcher_url,
    )
    assert result["launcher"]["state"] == expected_state
    assert "http://127.0.0.1:11999/health" in seen_urls
    assert result["server"]["url"] == "http://127.0.0.1:11999"
    assert result["configured_ready"] is True
    assert result["guided_ready"] is True


@pytest.mark.parametrize(
    ("selection", "configuration", "action_fragment"),
    [
        ("unknown", "missing", "Pass --host"),
        ("ambiguous", "invalid", "Multiple host configs"),
    ],
)
def test_collect_status_handles_unselected_host(
    monkeypatch,
    tmp_path,
    selection,
    configuration,
    action_fragment,
):
    monkeypatch.setattr(
        status_mod,
        "discover_profile",
        lambda **_kwargs: HostDiscovery(selection, None, (), (tmp_path,)),
    )
    seen_urls = []

    def fake_probe(url, timeout_s=status_mod.DEFAULT_STATUS_TIMEOUT_S):
        seen_urls.append(url)
        return ProbeResult("reachable", {"status": "ok"}, None, None)

    monkeypatch.setattr(status_mod, "_probe_json", fake_probe)
    result = status_mod.collect_status(start_dir=tmp_path, home_dir=tmp_path)
    assert result["host"]["selection"] == selection
    assert result["mcp"]["configuration"] == configuration
    assert result["configured_ready"] is False
    assert result["guided_ready"] is False
    assert action_fragment in result["next_action"]
    assert not any("/sessions" in url for url in seen_urls)


def test_probe_retains_http_payload_and_bounded_parse_error(monkeypatch):
    class Response:
        def read(self, _size=-1):
            return b'{"error": "maintenance"}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(status_mod.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert status_mod._probe_json("http://127.0.0.1:11437/health").payload == {
        "error": "maintenance"
    }


@pytest.mark.parametrize(
    ("configured", "expected_rc"),
    [(True, 0), (False, 1), (None, 1)],
)
def test_main_preserves_tristate_json_and_gates_exit_on_configured_ready(
    monkeypatch, capsys, configured, expected_rc
):
    seen = {}

    def fake_collect_status(**kwargs):
        seen.update(kwargs)
        return {
            "host": {"selection": "codex", "profile": "Codex", "inspected_paths": []},
            "server": {
                "url": "http://127.0.0.1:11437",
                "transport": "reachable",
                "health": "healthy",
                "payload": {"status": "ok"},
                "parse_error": None,
                "error": None,
            },
            "launcher": {"url": None, "state": "not_configured"},
            "mcp": {
                "configuration": "canonical",
                "activation": "unknown",
                "live": "unknown",
                "path": None,
                "detail": None,
            },
            "skill": {"installation": "present", "activation": "enabled", "path": "skill", "detail": None},
            "configured_ready": configured,
            "guided_ready": configured,
            "next_action": "Confirm activation.",
        }

    monkeypatch.setattr(status_mod, "collect_status", fake_collect_status)
    assert status_mod.main(["--host", "codex", "--server-url", "http://custom", "--json"]) == expected_rc
    assert seen["host"] == "codex"
    assert seen["server_url"] == "http://custom"
    assert json.loads(capsys.readouterr().out)["configured_ready"] is configured


def test_text_output_labels_each_independent_status_dimension():
    text = status_mod._render_text(
        {
            "host": {"selection": "codex", "profile": "Codex", "inspected_paths": []},
            "server": {"url": "http://server", "transport": "reachable", "health": "healthy"},
            "launcher": {"url": None, "state": "not_configured"},
            "mcp": {"configuration": "canonical", "activation": "enabled", "live": "unknown", "path": None, "detail": None},
            "skill": {"installation": "present", "activation": "enabled", "path": "skill", "detail": None},
            "configured_ready": True,
            "guided_ready": True,
            "next_action": "Use it.",
        }
    )
    for label in ("Host:", "Server:", "Launcher:", "MCP:", "Skill:", "Configured ready:", "Guided ready:"):
        assert label in text
