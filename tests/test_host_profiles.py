"""Contract tests for read-only native host-profile discovery."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import tomllib

import pytest

from cymatix_context.integrations.host_profiles import (
    HOST_PROFILES,
    discover_host_config,
    discover_profile,
    discover_skill,
    get_profile,
    parse_host_config,
    profile_for_config_path,
    render_mcp_snippet,
)


EXPECTED_IDENTITIES = {
    "claude-code": ("Claude Code", "claude-code", "claude-code"),
    "codex": ("Codex", "codex", "codex"),
    "gemini-cli": ("Gemini CLI", "gemini", "gemini-cli"),
    "antigravity": ("Google Antigravity", "gemini", "antigravity"),
}


def _entry(profile_id: str, *, url: str = "http://127.0.0.1:11437") -> dict[str, object]:
    _display_name, _agent_kind, mcp_host = EXPECTED_IDENTITIES[profile_id]
    return {
        "command": "python",
        "args": ["-m", "cymatix_context.mcp_server"],
        "env": {
            "CYMATIX_MCP_URL": url,
            "CYMATIX_MCP_HANDLE": f"{profile_id}-agent",
            "CYMATIX_MCP_HOST": mcp_host,
            "UNRELATED_SECRET": "must-not-escape",
        },
    }


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_config(path: Path, profile_id: str, *, enabled: bool | None = None) -> Path:
    entry = _entry(profile_id)
    if profile_id == "codex":
        enabled_line = "" if enabled is None else f"enabled = {str(enabled).lower()}\n"
        return _write_toml(
            path,
            "[mcp_servers.cymatix-context]\n"
            'command = "python"\n'
            'args = ["-m", "cymatix_context.mcp_server"]\n'
            f"{enabled_line}\n"
            "[mcp_servers.cymatix-context.env]\n"
            'CYMATIX_MCP_URL = "http://127.0.0.1:11437"\n'
            'CYMATIX_MCP_HANDLE = "codex-agent"\n'
            'CYMATIX_MCP_HOST = "codex"\n'
            'UNRELATED_SECRET = "must-not-escape"\n',
        )
    if profile_id == "antigravity" and enabled is not None:
        entry["disabled"] = not enabled
    return _write_json(path, {"mcpServers": {"cymatix-context": entry}})


def _write_toml(path: Path, payload: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


def test_profiles_are_complete_ordered_and_source_pinned():
    assert [profile.id for profile in HOST_PROFILES] == list(EXPECTED_IDENTITIES)
    for profile in HOST_PROFILES:
        display_name, agent_kind, mcp_host = EXPECTED_IDENTITIES[profile.id]
        assert (profile.display_name, profile.agent_kind, profile.mcp_host) == (
            display_name,
            agent_kind,
            mcp_host,
        )
        assert profile.accessed_on == date(2026, 8, 27)
        assert profile.source_urls


@pytest.mark.parametrize("profile_id", EXPECTED_IDENTITIES)
def test_rendered_snippet_is_native_parseable_and_safe(profile_id: str):
    rendered = render_mcp_snippet(profile_id)
    body = rendered.split("\n", 1)[1].rsplit("\n```", 1)[0]
    parsed = tomllib.loads(body) if profile_id == "codex" else json.loads(body)
    assert parsed
    assert "cymatix-context" in rendered
    assert "cymatix_context.mcp_server" in rendered
    assert "CYMATIX_MCP_URL" in rendered
    assert "http://127.0.0.1:11437" in rendered
    assert "C:\\Users\\" not in rendered
    assert "/Users/" not in rendered
    assert "CYMATIX_CALLER_MODEL_CLASS" not in rendered


@pytest.mark.parametrize("profile_id", EXPECTED_IDENTITIES)
def test_native_canonical_configs_preserve_the_configured_health_url(
    tmp_path: Path, profile_id: str
):
    profile = get_profile(profile_id)
    config = _write_config(
        tmp_path / ("config.toml" if profile_id == "codex" else "config.json"),
        profile_id,
        enabled=False if profile_id in {"codex", "antigravity"} else None,
    )
    activation_path = tmp_path / "mcp-server-enablement.json"
    if profile_id == "gemini-cli":
        _write_json(activation_path, {})

    parsed = parse_host_config(config, profile, activation_path=activation_path)

    assert parsed.configuration == "canonical"
    assert parsed.configured_url == "http://127.0.0.1:11437"
    assert parsed.handle == f"{profile_id}-agent"
    assert parsed.mcp_host == EXPECTED_IDENTITIES[profile_id][2]
    assert "must-not-escape" not in repr(parsed)


def test_native_activation_states_are_host_specific(tmp_path: Path):
    claude = parse_host_config(
        _write_config(tmp_path / "claude.json", "claude-code"), get_profile("claude-code")
    )
    codex_disabled = parse_host_config(
        _write_config(tmp_path / "codex-disabled.toml", "codex", enabled=False),
        get_profile("codex"),
    )
    codex_default = parse_host_config(
        _write_config(tmp_path / "codex-default.toml", "codex"), get_profile("codex")
    )
    antigravity_disabled = parse_host_config(
        _write_config(tmp_path / "antigravity-disabled.json", "antigravity", enabled=False),
        get_profile("antigravity"),
    )
    antigravity_default = parse_host_config(
        _write_config(tmp_path / "antigravity-default.json", "antigravity"),
        get_profile("antigravity"),
    )

    assert claude.activation == "unknown"
    assert codex_disabled.activation == "disabled"
    assert codex_default.activation == "enabled"
    assert antigravity_disabled.activation == "disabled"
    assert antigravity_default.activation == "enabled"


def test_gemini_enablement_requires_a_valid_record(tmp_path: Path):
    config = _write_config(tmp_path / "settings.json", "gemini-cli")
    record = tmp_path / "mcp-server-enablement.json"
    profile = get_profile("gemini-cli")

    assert parse_host_config(config, profile, activation_path=record).activation == "unknown"
    _write_json(record, {"cymatix-context": {"enabled": False}})
    assert parse_host_config(config, profile, activation_path=record).activation == "disabled"
    _write_json(record, {})
    assert parse_host_config(config, profile, activation_path=record).activation == "enabled"
    _write_json(record, {"cymatix-context": {"enabled": "false"}})
    assert parse_host_config(config, profile, activation_path=record).activation == "unknown"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"mcpServers": {}}, "missing"),
        ({"mcpServers": {"cymatix-context": {"command": "python"}}}, "noncanonical"),
        ({"mcpServers": {"cymatix": _entry("claude-code")}}, "noncanonical"),
    ],
)
def test_config_states_distinguish_missing_from_noncanonical_entries(
    tmp_path: Path, payload: dict[str, object], expected: str
):
    path = _write_json(tmp_path / "config.json", payload)
    parsed = parse_host_config(path, get_profile("claude-code"))

    assert parsed.configuration == expected


def test_invalid_config_is_reported_without_environment_data(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text('{"mcpServers": ', encoding="utf-8")

    parsed = parse_host_config(path, get_profile("claude-code"))

    assert parsed.configuration == "invalid"
    assert parsed.activation == "unknown"
    assert parsed.detail
    assert "UNRELATED_SECRET" not in parsed.detail


def test_discovery_prefers_one_workspace_match_and_reports_ambiguity(tmp_path: Path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    _write_config(workspace / ".codex/config.toml", "codex")
    _write_config(home / ".gemini/settings.json", "gemini-cli")

    selected = discover_profile(workspace=workspace, home=home)

    assert selected.selection == "codex"
    assert selected.profile == get_profile("codex")
    assert [match.profile_id for match in selected.matches] == ["codex"]
    assert home / ".gemini/settings.json" not in selected.inspected_paths

    _write_config(workspace / ".mcp.json", "claude-code")
    ambiguous = discover_profile(workspace=workspace, home=home)

    assert ambiguous.selection == "ambiguous"
    assert ambiguous.profile is None
    assert {match.profile_id for match in ambiguous.matches} == {"claude-code", "codex"}


def test_discovery_uses_user_scope_only_after_workspace_has_no_matches(tmp_path: Path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    _write_config(home / ".gemini/settings.json", "gemini-cli")

    discovery = discover_profile(workspace=workspace, home=home)

    assert discovery.selection == "gemini-cli"
    assert discovery.matches[0].configured_url == "http://127.0.0.1:11437"


def test_discovery_returns_unknown_without_a_native_match(tmp_path: Path):
    discovery = discover_profile(workspace=tmp_path / "workspace", home=tmp_path / "home")

    assert discovery.selection == "unknown"
    assert discovery.profile is None
    assert discovery.matches == ()


def test_explicit_path_and_known_native_path_select_the_matching_profile(tmp_path: Path):
    config = _write_config(tmp_path / "anything.toml", "codex")
    profile = profile_for_config_path(config)

    assert profile == get_profile("codex")
    parsed, inspected = discover_host_config(
        profile, workspace=tmp_path / "workspace", home=tmp_path / "home", explicit_path=config
    )
    assert inspected == (config,)
    assert parsed.configuration == "canonical"
    assert profile_for_config_path(tmp_path / "unrelated.json") is None


def test_codex_skill_honors_absolute_path_disable_override(tmp_path: Path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    skill_path = workspace / ".agents/skills/cymatix-context/SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: cymatix-context\n---\n", encoding="utf-8")
    config_path = _write_toml(
        workspace / ".codex/config.toml",
        "[[skills.config]]\n"
        f'path = "{skill_path.resolve().as_posix()}"\n'
        "enabled = false\n",
    )

    report = discover_skill(
        get_profile("codex"),
        workspace=workspace,
        home=home,
        config_path=config_path,
    )

    assert report.installation == "present"
    assert report.path == skill_path
    assert report.activation == "disabled"


def test_codex_skill_uses_a_valid_config_without_an_mcp_entry(tmp_path: Path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    skill_path = workspace / ".agents/skills/cymatix-context/SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("shared skill", encoding="utf-8")
    _write_toml(workspace / ".codex/config.toml", "[features]\nexample = true\n")

    report = discover_skill(get_profile("codex"), workspace=workspace, home=home)

    assert report.installation == "present"
    assert report.activation == "enabled"


def test_non_codex_present_skill_is_enabled_and_missing_skill_is_unknown(tmp_path: Path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    skill = workspace / ".agents/skills/cymatix-context/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("shared skill", encoding="utf-8")

    found = discover_skill(get_profile("gemini-cli"), workspace=workspace, home=home)
    missing = discover_skill(
        get_profile("antigravity"),
        workspace=tmp_path / "missing-workspace",
        home=home,
    )

    assert (found.installation, found.activation, found.path) == ("present", "enabled", skill)
    assert missing.installation == "missing"
    assert missing.activation == "unknown"
