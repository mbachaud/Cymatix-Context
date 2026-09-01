"""Native, read-only configuration profiles for supported MCP hosts.

The registry deliberately captures only stable host facts.  It does not retain
environment mappings, credentials, or user-specific paths: callers receive
only the configured endpoint and safe identity labels needed for diagnostics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import logging
from pathlib import Path
import tomllib
from typing import Literal, Mapping


log = logging.getLogger(__name__)

HostId = Literal["claude-code", "codex", "gemini-cli", "antigravity"]
HostSelection = HostId | Literal["unknown", "ambiguous"]
Scope = Literal["workspace", "user"]
ConfigFormat = Literal["json", "toml"]
ConfigState = Literal["canonical", "noncanonical", "missing", "invalid"]
ActivationState = Literal["enabled", "disabled", "unknown"]

_PROFILE_IDS: tuple[HostId, ...] = (
    "claude-code",
    "codex",
    "gemini-cli",
    "antigravity",
)
_SERVER_NAME = "cymatix-context"
_COMMAND = "python"
_ARGS = ["-m", "cymatix_context.mcp_server"]
_LOCAL_URL = "http://127.0.0.1:11437"
_SKILL_FILE = "SKILL.md"


@dataclass(frozen=True)
class ConfigCandidate:
    """A host-native configuration or skill location relative to its scope."""

    scope: Scope
    relative_path: str


@dataclass(frozen=True)
class HostProfile:
    """Stable, non-secret documentation and discovery facts for one host."""

    id: HostId
    display_name: str
    config_format: ConfigFormat
    config_candidates: tuple[ConfigCandidate, ...]
    skill_candidates: tuple[ConfigCandidate, ...]
    agent_kind: str
    mcp_host: str
    source_urls: tuple[str, ...]
    accessed_on: date
    restart_guidance: str
    verify_guidance: str
    activation_record: str | None = None


@dataclass(frozen=True)
class ParsedHostConfig:
    """Safe parse result for a single native MCP configuration file."""

    path: Path | None
    profile_id: HostId
    configuration: ConfigState
    activation: ActivationState
    configured_url: str | None
    handle: str | None
    mcp_host: str | None
    detail: str | None = None


@dataclass(frozen=True)
class HostDiscovery:
    """The deterministic result of selecting a host from config evidence."""

    selection: HostSelection
    profile: HostProfile | None
    matches: tuple[ParsedHostConfig, ...]
    inspected_paths: tuple[Path, ...]


@dataclass(frozen=True)
class SkillReport:
    """Safe discovery result for a portable host skill installation."""

    installation: Literal["present", "missing"]
    activation: ActivationState
    path: Path
    detail: str | None = None


HOST_PROFILES: tuple[HostProfile, ...] = (
    HostProfile(
        id="claude-code",
        display_name="Claude Code",
        config_format="json",
        config_candidates=(
            ConfigCandidate("workspace", ".mcp.json"),
            ConfigCandidate("user", ".claude.json"),
        ),
        skill_candidates=(
            ConfigCandidate("workspace", ".claude/skills/cymatix-context/SKILL.md"),
            ConfigCandidate("user", ".claude/skills/cymatix-context/SKILL.md"),
        ),
        agent_kind="claude-code",
        mcp_host="claude-code",
        source_urls=(
            "https://code.claude.com/docs/en/slash-commands",
            "https://code.claude.com/docs/en/mcp",
        ),
        accessed_on=date(2026, 8, 27),
        restart_guidance="Restart Claude Code or refresh its MCP session after changes.",
        verify_guidance="Run `claude mcp get cymatix-context`, `claude mcp list`, or `/mcp`.",
    ),
    HostProfile(
        id="codex",
        display_name="Codex",
        config_format="toml",
        config_candidates=(
            ConfigCandidate("workspace", ".codex/config.toml"),
            ConfigCandidate("user", ".codex/config.toml"),
        ),
        skill_candidates=(
            ConfigCandidate("workspace", ".agents/skills/cymatix-context/SKILL.md"),
            ConfigCandidate("user", ".agents/skills/cymatix-context/SKILL.md"),
        ),
        agent_kind="codex",
        mcp_host="codex",
        source_urls=(
            "https://learn.chatgpt.com/docs/build-skills",
            "https://learn.chatgpt.com/docs/extend/mcp",
        ),
        accessed_on=date(2026, 8, 27),
        restart_guidance="Restart Codex after MCP or skill activation changes.",
        verify_guidance="Restart Codex, then inspect the configured MCP server and skill.",
    ),
    HostProfile(
        id="gemini-cli",
        display_name="Gemini CLI",
        config_format="json",
        config_candidates=(
            ConfigCandidate("workspace", ".gemini/settings.json"),
            ConfigCandidate("user", ".gemini/settings.json"),
        ),
        skill_candidates=(
            ConfigCandidate("workspace", ".agents/skills/cymatix-context/SKILL.md"),
            ConfigCandidate("workspace", ".gemini/skills/cymatix-context/SKILL.md"),
            ConfigCandidate("user", ".agents/skills/cymatix-context/SKILL.md"),
            ConfigCandidate("user", ".gemini/skills/cymatix-context/SKILL.md"),
        ),
        agent_kind="gemini",
        mcp_host="gemini-cli",
        source_urls=(
            "https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/using-agent-skills.md",
            "https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md",
        ),
        accessed_on=date(2026, 8, 27),
        restart_guidance="Restart Gemini CLI after MCP changes; use `/skills reload` after skill changes.",
        verify_guidance="Run `gemini mcp list` or `/mcp`; use `/skills list` for skills.",
        activation_record=".gemini/mcp-server-enablement.json",
    ),
    HostProfile(
        id="antigravity",
        display_name="Google Antigravity",
        config_format="json",
        config_candidates=(
            ConfigCandidate("workspace", ".agents/mcp_config.json"),
            ConfigCandidate("user", ".gemini/config/mcp_config.json"),
        ),
        skill_candidates=(
            ConfigCandidate("workspace", ".agents/skills/cymatix-context/SKILL.md"),
            ConfigCandidate("user", ".gemini/config/skills/cymatix-context/SKILL.md"),
        ),
        agent_kind="gemini",
        mcp_host="antigravity",
        source_urls=(
            "https://antigravity.google/docs/skills",
            "https://antigravity.google/docs/mcp",
            "https://www.antigravity.google/docs/cli/gcli-migration/",
        ),
        accessed_on=date(2026, 8, 27),
        restart_guidance="Refresh or restart Antigravity after MCP changes.",
        verify_guidance="Inspect the Antigravity MCP manager and refresh the entry.",
    ),
)


def get_profile(host: str) -> HostProfile:
    """Return an approved host profile, rejecting unrecognized host identifiers."""

    for profile in HOST_PROFILES:
        if profile.id == host:
            return profile
    raise ValueError(f"Unknown host profile: {host}")


def _detail(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _read_native_config(path: Path, config_format: ConfigFormat) -> tuple[object | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
        return (tomllib.loads(text) if config_format == "toml" else json.loads(text)), None
    except Exception as exc:
        log.warning("Could not read host config %s: %s", path, exc, exc_info=True)
        return None, _detail(exc)


def _mapping_value(value: object, key: str) -> str | None:
    if not isinstance(value, Mapping):
        return None
    candidate = value.get(key)
    return candidate if isinstance(candidate, str) else None


def _entry_container(data: object, profile: HostProfile) -> object | None:
    if not isinstance(data, Mapping):
        return None
    key = "mcp_servers" if profile.config_format == "toml" else "mcpServers"
    return data.get(key)


def _looks_like_cymatix_key(key: object) -> bool:
    return isinstance(key, str) and key.lower().replace("_", "-").startswith("cymatix")


def _entry_state(
    data: object, profile: HostProfile
) -> tuple[ConfigState, Mapping[str, object] | None, str | None]:
    container = _entry_container(data, profile)
    if container is None:
        return "missing", None, "No MCP server definitions were found."
    if not isinstance(container, Mapping):
        return "noncanonical", None, "The native MCP server section is not a mapping."

    entry = container.get(_SERVER_NAME)
    if entry is None:
        if any(_looks_like_cymatix_key(key) for key in container):
            return "noncanonical", None, "A noncanonical Cymatix MCP entry was found."
        return "missing", None, "No cymatix-context MCP entry was found."
    if not isinstance(entry, Mapping):
        return "noncanonical", None, "The cymatix-context MCP entry is not a mapping."

    env = entry.get("env")
    configured_url = _mapping_value(env, "CYMATIX_MCP_URL")
    is_canonical = (
        entry.get("command") == _COMMAND
        and entry.get("args") == _ARGS
        and isinstance(env, Mapping)
        and isinstance(configured_url, str)
        and bool(configured_url)
    )
    if is_canonical:
        return "canonical", entry, None
    return "noncanonical", entry, "The cymatix-context MCP entry does not use the canonical command, module, or URL."


def _activation_error_detail(exc: Exception) -> str:
    """Return a useful parse diagnostic without exposing record contents."""

    return f"{type(exc).__name__} while reading Gemini MCP enablement record."[:500]


def _gemini_activation(activation_path: Path | None) -> tuple[ActivationState, str | None]:
    if activation_path is None or not activation_path.is_file():
        return "unknown", None
    try:
        data = json.loads(activation_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning(
            "Could not read Gemini activation record %s: %s",
            activation_path,
            exc,
            exc_info=True,
        )
        return "unknown", _activation_error_detail(exc)
    if not isinstance(data, Mapping):
        return "unknown", "Gemini MCP enablement record is not a mapping."
    for server_id, state in data.items():
        if not isinstance(server_id, str) or not isinstance(state, Mapping):
            return "unknown", "Gemini MCP enablement record has an invalid server state."
        if not isinstance(state.get("enabled"), bool):
            return "unknown", "Gemini MCP enablement record has an invalid server state."
    state = data.get(_SERVER_NAME)
    if state is None:
        return "enabled", None
    # The validation loop above establishes the type without serializing the
    # untrusted record or retaining any of its values.
    return ("enabled" if state["enabled"] else "disabled"), None


def _activation_state(
    profile: HostProfile, entry: Mapping[str, object] | None, activation_path: Path | None
) -> tuple[ActivationState, str | None]:
    if entry is None:
        return "unknown", None
    if profile.id == "claude-code":
        # A file cannot prove approval or connection in Claude Code.
        return "unknown", None
    if profile.id == "codex":
        value = entry.get("enabled", True)
        if value is False:
            return "disabled", None
        return ("enabled" if value is True else "unknown"), None
    if profile.id == "gemini-cli":
        return _gemini_activation(activation_path)
    value = entry.get("disabled", False)
    if value is True:
        return "disabled", None
    return ("enabled" if value is False else "unknown"), None


def parse_host_config(
    path: Path, profile: HostProfile, *, activation_path: Path | None = None
) -> ParsedHostConfig:
    """Read one native config without retaining environment mappings or secrets."""

    if not path.is_file():
        return ParsedHostConfig(
            path=path,
            profile_id=profile.id,
            configuration="missing",
            activation="unknown",
            configured_url=None,
            handle=None,
            mcp_host=None,
            detail="Configuration file is missing.",
        )

    data, error = _read_native_config(path, profile.config_format)
    if error is not None:
        return ParsedHostConfig(
            path=path,
            profile_id=profile.id,
            configuration="invalid",
            activation="unknown",
            configured_url=None,
            handle=None,
            mcp_host=None,
            detail=error,
        )

    configuration, entry, detail = _entry_state(data, profile)
    env = entry.get("env") if entry is not None else None
    configured_url = _mapping_value(env, "CYMATIX_MCP_URL")
    handle = _mapping_value(env, "CYMATIX_MCP_HANDLE")
    mcp_host = _mapping_value(env, "CYMATIX_MCP_HOST")
    activation, activation_detail = _activation_state(profile, entry, activation_path)
    return ParsedHostConfig(
        path=path,
        profile_id=profile.id,
        configuration=configuration,
        activation=activation,
        configured_url=configured_url,
        handle=handle,
        mcp_host=mcp_host,
        detail=activation_detail or detail,
    )


def _path_for_candidate(candidate: ConfigCandidate, workspace: Path, home: Path) -> Path:
    return (workspace if candidate.scope == "workspace" else home) / candidate.relative_path


def _activation_path(profile: HostProfile, home: Path) -> Path | None:
    return home / profile.activation_record if profile.activation_record else None


def discover_host_config(
    profile: HostProfile,
    *,
    workspace: Path,
    home: Path,
    explicit_path: Path | None = None,
) -> tuple[ParsedHostConfig, tuple[Path, ...]]:
    """Discover the preferred config for one explicit host profile.

    Workspace candidates take precedence over user candidates.  The inspected
    path list remains complete so status callers can explain a missing result.
    """

    activation_path = _activation_path(profile, home)
    if explicit_path is not None:
        return (
            parse_host_config(explicit_path, profile, activation_path=activation_path),
            (explicit_path,),
        )

    parsed_configs: list[ParsedHostConfig] = []
    inspected_paths: list[Path] = []
    for candidate in profile.config_candidates:
        candidate_path = _path_for_candidate(candidate, workspace, home)
        inspected_paths.append(candidate_path)
        parsed_configs.append(
            parse_host_config(candidate_path, profile, activation_path=activation_path)
        )

    for parsed in parsed_configs:
        if parsed.configuration != "missing":
            return parsed, tuple(inspected_paths)
    return parsed_configs[0], tuple(inspected_paths)


def discover_profile(*, workspace: Path, home: Path) -> HostDiscovery:
    """Select one profile from native config evidence without process guessing."""

    inspected_paths: list[Path] = []
    for scope in ("workspace", "user"):
        matches: list[ParsedHostConfig] = []
        for profile in HOST_PROFILES:
            candidates = [
                candidate for candidate in profile.config_candidates if candidate.scope == scope
            ]
            for candidate in candidates:
                candidate_path = _path_for_candidate(candidate, workspace, home)
                inspected_paths.append(candidate_path)
                parsed = parse_host_config(
                    candidate_path,
                    profile,
                    activation_path=_activation_path(profile, home),
                )
                if parsed.configuration in {"canonical", "noncanonical"}:
                    matches.append(parsed)
        if len(matches) == 1:
            profile = get_profile(matches[0].profile_id)
            return HostDiscovery(
                selection=profile.id,
                profile=profile,
                matches=tuple(matches),
                inspected_paths=tuple(inspected_paths),
            )
        if len(matches) > 1:
            return HostDiscovery(
                selection="ambiguous",
                profile=None,
                matches=tuple(matches),
                inspected_paths=tuple(inspected_paths),
            )
    return HostDiscovery(
        selection="unknown",
        profile=None,
        matches=(),
        inspected_paths=tuple(inspected_paths),
    )


def profile_for_config_path(path: Path) -> HostProfile | None:
    """Infer a profile from an explicit documented native config pathname."""

    name = path.name.lower()
    parent = path.parent.name.lower()
    if name in {".mcp.json", ".claude.json"}:
        return get_profile("claude-code")
    if name.endswith(".toml"):
        return get_profile("codex")
    if name == "settings.json" and parent == ".gemini":
        return get_profile("gemini-cli")
    if name == "mcp_config.json":
        return get_profile("antigravity")
    return None


def _find_skill_path(
    profile: HostProfile,
    workspace: Path,
    home: Path,
    explicit_dir: Path | None,
) -> Path:
    if explicit_dir is not None:
        return explicit_dir if explicit_dir.name == _SKILL_FILE else explicit_dir / _SKILL_FILE
    candidates = [
        _path_for_candidate(candidate, workspace, home) for candidate in profile.skill_candidates
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _read_codex_skill_overrides(config_path: Path) -> tuple[object | None, str | None]:
    try:
        return tomllib.loads(config_path.read_text(encoding="utf-8")), None
    except Exception as exc:
        log.warning(
            "Could not read Codex skill config %s: %s", config_path, exc, exc_info=True
        )
        return None, _detail(exc)


def _codex_skill_activation(skill_path: Path, config_path: Path | None) -> tuple[ActivationState, str | None]:
    if config_path is None or not config_path.is_file():
        return "unknown", "Codex skill activation config is missing."
    data, error = _read_codex_skill_overrides(config_path)
    if error is not None:
        return "unknown", error
    if not isinstance(data, Mapping):
        return "unknown", "Codex skill activation config is not a mapping."
    skills = data.get("skills")
    if skills is None:
        return "enabled", None
    if not isinstance(skills, Mapping):
        return "unknown", "Codex skill configuration is not a mapping."
    overrides = skills.get("config", [])
    if not isinstance(overrides, list):
        return "unknown", "Codex skill overrides are not a list."

    target = skill_path.resolve()
    matching_values: list[bool] = []
    for override in overrides:
        if not isinstance(override, Mapping):
            return "unknown", "Codex skill override is not a mapping."
        override_path = override.get("path")
        if not isinstance(override_path, str):
            continue
        try:
            matches = Path(override_path).resolve() == target
        except Exception as exc:
            log.warning(
                "Could not resolve Codex skill override path %s: %s",
                config_path,
                exc,
                exc_info=True,
            )
            return "unknown", _detail(exc)
        if not matches:
            continue
        enabled = override.get("enabled")
        if not isinstance(enabled, bool):
            return "unknown", "Codex matching skill override has no boolean enabled state."
        matching_values.append(enabled)
    if not matching_values:
        return "enabled", None
    if any(value is False for value in matching_values):
        return "disabled", None
    return "enabled", None


def discover_skill(
    profile: HostProfile,
    *,
    workspace: Path,
    home: Path,
    config_path: Path | None = None,
    explicit_dir: Path | None = None,
) -> SkillReport:
    """Discover the portable skill and its host-native activation evidence."""

    skill_path = _find_skill_path(profile, workspace, home, explicit_dir)
    if not skill_path.is_file():
        return SkillReport(
            installation="missing",
            activation="unknown",
            path=skill_path,
            detail="Skill file is missing.",
        )
    if profile.id != "codex":
        return SkillReport(installation="present", activation="enabled", path=skill_path)

    if config_path is None:
        parsed_config, _inspected = discover_host_config(
            profile, workspace=workspace, home=home
        )
        # A valid Codex file may carry only ``[[skills.config]]`` and no MCP
        # table, so a missing Cymatix entry must not discard that parse target.
        config_path = (
            parsed_config.path
            if parsed_config.path is not None and parsed_config.path.is_file()
            else None
        )
    activation, detail = _codex_skill_activation(skill_path, config_path)
    return SkillReport(
        installation="present",
        activation=activation,
        path=skill_path,
        detail=detail,
    )


def _snippet_environment(profile: HostProfile) -> dict[str, str]:
    return {
        "CYMATIX_MCP_URL": _LOCAL_URL,
        "CYMATIX_ORG": "<org>",
        "CYMATIX_PARTY_ID": "<party-id>",
        "CYMATIX_DEVICE": "<party-id>",
        "CYMATIX_USER": "<user>",
        "CYMATIX_AGENT": "<agent-handle>",
        "CYMATIX_AGENT_KIND": profile.agent_kind,
        "CYMATIX_MCP_HANDLE": "<agent-handle>",
        "CYMATIX_MCP_HOST": profile.mcp_host,
    }


def render_mcp_snippet(profile_id: str) -> str:
    """Render one deterministic, native, placeholder-only MCP configuration block."""

    profile = get_profile(profile_id)
    environment = _snippet_environment(profile)
    if profile.id == "codex":
        lines = [
            "[mcp_servers.cymatix-context]",
            'command = "python"',
            'args = ["-m", "cymatix_context.mcp_server"]',
            "enabled = true",
            "",
            "[mcp_servers.cymatix-context.env]",
        ]
        lines.extend(f'{key} = "{value}"' for key, value in environment.items())
        return "```toml\n" + "\n".join(lines) + "\n```"

    entry: dict[str, object] = {
        "command": _COMMAND,
        "args": _ARGS,
    }
    if profile.id == "antigravity":
        entry["disabled"] = False
    entry["env"] = environment
    rendered = json.dumps({"mcpServers": {_SERVER_NAME: entry}}, indent=2)
    return f"```json\n{rendered}\n```"


__all__ = [
    "ActivationState",
    "ConfigCandidate",
    "ConfigFormat",
    "ConfigState",
    "HOST_PROFILES",
    "HostDiscovery",
    "HostId",
    "HostProfile",
    "HostSelection",
    "ParsedHostConfig",
    "Scope",
    "SkillReport",
    "discover_host_config",
    "discover_profile",
    "discover_skill",
    "get_profile",
    "parse_host_config",
    "profile_for_config_path",
    "render_mcp_snippet",
]
