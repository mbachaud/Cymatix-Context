"""Read-only, host-aware readiness diagnostics for ``cymatix-status``."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
import ipaddress
import json
import logging
import os
from pathlib import Path
from typing import Any, Literal
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from cymatix_context.integrations.host_profiles import (
    ActivationState,
    ConfigState,
    HostProfile,
    ParsedHostConfig,
    SkillReport,
    discover_host_config,
    discover_profile,
    discover_skill,
    get_profile,
    profile_for_config_path,
)

from .dispatcher import invoked_prog


log = logging.getLogger(__name__)

DEFAULT_SERVER_URL = os.environ.get("CYMATIX_STATUS_URL", "http://127.0.0.1:11437").rstrip("/")
DEFAULT_LAUNCHER_URL = os.environ.get("CYMATIX_LAUNCHER_URL", "http://127.0.0.1:11438").rstrip("/")
MAX_STATUS_RESPONSE_BYTES = 64 * 1024
_MAX_STATUS_URL_LENGTH = 2048

_DEFAULT_STATUS_TIMEOUT_S = 10.0
try:
    DEFAULT_STATUS_TIMEOUT_S = float(
        os.environ.get("CYMATIX_STATUS_TIMEOUT_S", _DEFAULT_STATUS_TIMEOUT_S)
    )
except ValueError:
    import sys

    sys.stderr.write(
        f"CYMATIX_STATUS_TIMEOUT_S={os.environ.get('CYMATIX_STATUS_TIMEOUT_S')!r} "
        f"is not a float; falling back to {_DEFAULT_STATUS_TIMEOUT_S}s\n"
    )
    DEFAULT_STATUS_TIMEOUT_S = _DEFAULT_STATUS_TIMEOUT_S


@dataclass(frozen=True)
class ProbeResult:
    """Result of one bounded JSON HTTP probe without collapsing evidence."""

    transport: Literal["reachable", "unreachable"]
    payload: Mapping[str, Any] | None
    parse_error: str | None
    error: str | None


@dataclass(frozen=True)
class ServerReport:
    """Transport and health dimensions of a server status probe."""

    url: str
    transport: Literal["reachable", "unreachable"]
    health: Literal["healthy", "unhealthy", "unknown"]
    payload: Mapping[str, Any] | None
    parse_error: str | None
    error: str | None


@dataclass(frozen=True)
class McpLiveReport:
    """Evidence-based current MCP connection state."""

    state: Literal["connected", "disconnected", "unknown"]
    registry_url: str
    reason: str | None


@dataclass(frozen=True)
class ValidatedStatusUrl:
    """A structurally safe HTTP(S) base URL, never resolved for trust."""

    request_url: str
    reported_url: str
    hostname: str
    canonical: tuple[str, str, int, str]


@dataclass(frozen=True)
class ProbeTarget:
    """One safe status target or an explanation for a deliberate non-probe."""

    request_url: str | None
    reported_url: str
    reason: str | None
    action: str | None
    source: Literal["configured", "default", "explicit", "launcher"]


def _bounded_text(value: object, limit: int = 500) -> str:
    return str(value)[:limit]


def _reported_url(value: object) -> str:
    """Render a URL without userinfo, query, or fragment, even when invalid."""

    if not isinstance(value, str) or len(value) > _MAX_STATUS_URL_LENGTH:
        return "<invalid URL>"
    try:
        split = urlsplit(value)
        hostname = split.hostname
        port = split.port
    except (TypeError, ValueError):
        return "<invalid URL>"
    if not split.scheme or not hostname:
        return "<invalid URL>"
    host = hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((split.scheme.lower(), netloc, split.path, "", ""))


def _validate_status_base_url(value: object) -> tuple[ValidatedStatusUrl | None, str | None]:
    """Accept only absolute HTTP(S) base URLs with no secret-bearing parts."""

    if not isinstance(value, str) or not value:
        return None, "URL must be a nonempty absolute HTTP(S) URL."
    if len(value) > _MAX_STATUS_URL_LENGTH:
        return None, "URL exceeds the status probe length limit."
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in value):
        return None, "URL contains whitespace or a control character."
    try:
        split = urlsplit(value)
        hostname = split.hostname
        port = split.port
    except (TypeError, ValueError):
        return None, "URL is malformed."
    if split.scheme.lower() not in {"http", "https"} or not hostname:
        return None, "URL must use HTTP(S) and include a hostname."
    if split.username is not None or split.password is not None or "@" in split.netloc:
        return None, "URL must not include userinfo."
    if split.query or split.fragment or "?" in value or "#" in value:
        return None, "URL must not include a query or fragment."
    scheme = split.scheme.lower()
    canonical_host = hostname.lower()
    try:
        canonical_host = ipaddress.ip_address(canonical_host).compressed
    except ValueError:
        pass
    path = split.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    canonical_port = port if port is not None else (443 if scheme == "https" else 80)
    return (
        ValidatedStatusUrl(
            request_url=value.rstrip("/"),
            reported_url=_reported_url(value),
            hostname=hostname,
            canonical=(scheme, canonical_host, canonical_port, path),
        ),
        None,
    )


def _is_loopback_hostname(hostname: str) -> bool:
    """Recognize local names/IPs without DNS or platform-specific resolution."""

    normalized = hostname.lower().rstrip(".")
    if normalized == "localhost":
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or (mapped is not None and mapped.is_loopback)


def _refused_target(
    *,
    reported_url: str,
    reason: str,
    action: str,
    source: Literal["configured", "default", "explicit", "launcher"],
) -> ProbeTarget:
    return ProbeTarget(
        request_url=None,
        reported_url=reported_url,
        reason=_bounded_text(f"Status probe was not probed: {reason}"),
        action=action,
        source=source,
    )


def _select_server_target(
    *, explicit_url: str | None, configured_url: str | None
) -> ProbeTarget:
    """Prefer a validated explicit target; implicit targets must be loopback."""

    if explicit_url is not None:
        validated, error = _validate_status_base_url(explicit_url)
        if validated is None:
            return _refused_target(
                reported_url=_reported_url(explicit_url),
                reason=error or "URL is invalid.",
                action="Pass a valid absolute HTTP(S) --server-url without credentials, query, or fragment.",
                source="explicit",
            )
        return ProbeTarget(
            validated.request_url, validated.reported_url, None, None, "explicit"
        )

    automatic_url = configured_url if configured_url is not None else DEFAULT_SERVER_URL
    source = (
        "Configured MCP server URL"
        if configured_url is not None
        else "Automatic server URL"
    )
    target_source: Literal["configured", "default"] = (
        "configured" if configured_url is not None else "default"
    )
    validated, error = _validate_status_base_url(automatic_url)
    if validated is None:
        return _refused_target(
            reported_url=_reported_url(automatic_url),
            reason=error or "URL is invalid.",
            action="Pass a valid absolute HTTP(S) --server-url without credentials, query, or fragment.",
            source=target_source,
        )
    if not _is_loopback_hostname(validated.hostname):
        return _refused_target(
            reported_url=validated.reported_url,
            reason=f"{source} is not loopback.",
            action=(
                f"{source} is not loopback; pass --server-url explicitly "
                "to opt in to remote status probing."
            ),
            source=target_source,
        )
    return ProbeTarget(
        validated.request_url, validated.reported_url, None, None, target_source
    )


def _select_launcher_target(launcher_url: str | None) -> ProbeTarget | None:
    """Validate launcher input before requesting it; launcher has no readiness role."""

    if launcher_url is None:
        return None
    validated, error = _validate_status_base_url(launcher_url)
    if validated is None:
        return _refused_target(
            reported_url=_reported_url(launcher_url),
            reason=error or "URL is invalid.",
            action="Pass a valid absolute HTTP(S) --launcher-url without credentials, query, or fragment.",
            source="launcher",
        )
    return ProbeTarget(
        validated.request_url, validated.reported_url, None, None, "launcher"
    )


def _configured_url_match(
    *, server_target: ProbeTarget, configured_url: str | None
) -> bool | None:
    """Compare only validated URL forms; never resolve hostnames for equivalence."""

    if configured_url is None:
        return None
    configured, _error = _validate_status_base_url(configured_url)
    if configured is None:
        return None
    if server_target.source == "configured":
        return True
    if server_target.source != "explicit" or server_target.request_url is None:
        return None
    explicit, _error = _validate_status_base_url(server_target.request_url)
    if explicit is None:
        return None
    return explicit.canonical == configured.canonical


def _diagnostic_target_action(configured_url_match: bool | None) -> str | None:
    """Explain why a different diagnostic backend cannot prove host readiness."""

    if configured_url_match is False:
        return (
            "Update the selected host MCP URL to the diagnostic endpoint, or rerun "
            "--server-url with the matching configured URL; a different endpoint "
            "cannot prove host readiness."
        )
    return None


def _parse_json_mapping(body: bytes) -> tuple[Mapping[str, Any] | None, str | None]:
    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception as exc:
        log.warning("Could not parse status response JSON: %s", exc)
        return None, _bounded_text(exc)
    if not isinstance(parsed, Mapping):
        return None, "JSON response is not an object."
    return parsed, None


def _read_bounded_status_body(response: Any, *, url: str) -> tuple[bytes | None, str | None]:
    """Read at most one bounded response body and retain no unbounded content."""

    try:
        body = response.read(MAX_STATUS_RESPONSE_BYTES + 1)
    except Exception as exc:
        log.warning("Could not read status response body for %s: %s", url, exc, exc_info=True)
        return None, _bounded_text(exc)
    if not isinstance(body, bytes):
        return None, "Status response body is not bytes."
    if len(body) > MAX_STATUS_RESPONSE_BYTES:
        log.warning("Status response body exceeded %d bytes for %s", MAX_STATUS_RESPONSE_BYTES, url)
        return None, f"Status response exceeds {MAX_STATUS_RESPONSE_BYTES} byte limit."
    return body, None


def _probe_json(url: str, timeout_s: float = DEFAULT_STATUS_TIMEOUT_S) -> ProbeResult:
    """Probe a JSON endpoint without treating HTTP status as transport failure."""

    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            body, body_error = _read_bounded_status_body(response, url=url)
        if body_error is not None:
            return ProbeResult("reachable", None, body_error, None)
        assert body is not None
        payload, parse_error = _parse_json_mapping(body)
        return ProbeResult("reachable", payload, parse_error, None)
    except urllib.error.HTTPError as exc:
        body, body_error = _read_bounded_status_body(exc, url=url)
        if body_error is not None:
            return ProbeResult("reachable", None, body_error, f"HTTP {exc.code}")
        assert body is not None
        payload, parse_error = _parse_json_mapping(body)
        return ProbeResult("reachable", payload, parse_error, f"HTTP {exc.code}")
    except urllib.error.URLError as exc:
        return ProbeResult("unreachable", None, None, _bounded_text(exc.reason))
    except Exception as exc:
        log.warning("Status probe failed for %s: %s", url, exc, exc_info=True)
        return ProbeResult(
            "unreachable",
            None,
            None,
            _bounded_text(f"{type(exc).__name__}: {exc}"),
        )


def map_server_health(probe: ProbeResult, *, url: str) -> ServerReport:
    """Map probe evidence to the separate server transport and health fields."""

    if probe.transport == "unreachable":
        health: Literal["healthy", "unhealthy", "unknown"] = "unknown"
    elif probe.payload is None:
        health = "unknown"
    elif probe.payload.get("status") == "ok":
        health = "healthy"
    else:
        health = "unhealthy"
    return ServerReport(
        url=url,
        transport=probe.transport,
        health=health,
        payload=probe.payload,
        parse_error=probe.parse_error,
        error=probe.error,
    )


def map_launcher_state(
    probe: ProbeResult | None,
) -> Literal["running", "stopped", "unreachable", "not_configured"]:
    """Read launcher state without allowing it to affect direct MCP readiness."""

    if probe is None:
        return "not_configured"
    if probe.transport == "unreachable" or probe.payload is None:
        return "unreachable"
    cymatix = probe.payload.get("cymatix")
    if not isinstance(cymatix, Mapping):
        return "unreachable"
    running = cymatix.get("running")
    if running is True:
        return "running"
    if running is False:
        return "stopped"
    return "unreachable"


def configured_ready(
    *,
    configuration: ConfigState,
    activation: ActivationState,
    health: Literal["healthy", "unhealthy", "unknown"],
) -> bool | None:
    """Return only what canonical config, activation, and backend health prove."""

    if configuration != "canonical" or activation == "disabled":
        return False
    if activation == "enabled":
        return health == "healthy"
    if health == "healthy":
        return None
    return False


def _configured_readiness_with_target_evidence(
    *,
    configuration: ConfigState,
    activation: ActivationState,
    health: Literal["healthy", "unhealthy", "unknown"],
    configured_url_match: bool | None,
) -> bool | None:
    """Do not treat health for another endpoint as configured-host evidence."""

    if configuration != "canonical" or activation == "disabled":
        return False
    if configured_url_match is False:
        return None
    if configured_url_match is None:
        return configured_ready(
            configuration=configuration,
            activation=activation,
            health="unknown",
        )
    return configured_ready(
        configuration=configuration,
        activation=activation,
        health=health,
    )


def guided_ready(
    configured: bool | None,
    installation: Literal["present", "missing"],
    activation: ActivationState,
) -> bool | None:
    """Add portable-skill evidence without conflating it with MCP readiness."""

    if configured is False or installation == "missing" or activation == "disabled":
        return False
    if configured is None or activation == "unknown":
        return None
    return True


def determine_mcp_live(
    *,
    handle: str | None,
    configured_host: str | None,
    registry: ProbeResult,
    registry_url: str = "",
) -> McpLiveReport:
    """Require exact configured identity evidence from a parsed active registry."""

    if not handle or not handle.strip() or not configured_host or not configured_host.strip():
        return McpLiveReport("unknown", registry_url, "No unambiguous MCP handle and host are configured.")
    if registry.transport != "reachable":
        return McpLiveReport("unknown", registry_url, registry.error or "Session registry is unreachable.")
    if registry.payload is None:
        return McpLiveReport("unknown", registry_url, registry.parse_error or "Session registry is unreadable.")
    participants = registry.payload.get("participants")
    if not isinstance(participants, list):
        return McpLiveReport("unknown", registry_url, "Session registry has no participants list.")

    for participant in participants:
        if not isinstance(participant, Mapping):
            return McpLiveReport("unknown", registry_url, "Session registry contains an ambiguous participant.")
        participant_handle = participant.get("handle")
        participant_host = participant.get("mcp_host")
        participant_status = participant.get("status")
        if not all(isinstance(value, str) for value in (participant_handle, participant_host, participant_status)):
            return McpLiveReport("unknown", registry_url, "Session registry contains an ambiguous participant.")
        if (
            participant_handle == handle
            and participant_host == configured_host
            and participant_status == "active"
        ):
            return McpLiveReport("connected", registry_url, None)
    return McpLiveReport("disconnected", registry_url, None)


def _select_host(
    *,
    host: str,
    mcp_config: Path | None,
    workspace: Path,
    home: Path,
) -> tuple[str, HostProfile | None, ParsedHostConfig | None, tuple[Path, ...]]:
    """Select one profile without assigning semantics to an unknown config file."""

    explicit_path = Path(mcp_config) if mcp_config is not None else None
    if host != "auto":
        profile = get_profile(host)
        parsed, inspected_paths = discover_host_config(
            profile,
            workspace=workspace,
            home=home,
            explicit_path=explicit_path,
        )
        return profile.id, profile, parsed, inspected_paths

    if explicit_path is not None:
        profile = profile_for_config_path(explicit_path)
        if profile is None:
            return "unknown", None, None, (explicit_path,)
        parsed, inspected_paths = discover_host_config(
            profile,
            workspace=workspace,
            home=home,
            explicit_path=explicit_path,
        )
        return profile.id, profile, parsed, inspected_paths

    discovery = discover_profile(workspace=workspace, home=home)
    if discovery.profile is None or not discovery.matches:
        return discovery.selection, None, None, discovery.inspected_paths
    return (
        discovery.selection,
        discovery.profile,
        discovery.matches[0],
        discovery.inspected_paths,
    )


def _server_dict(
    report: ServerReport,
    *,
    source: Literal["configured", "default", "explicit"],
    configured_url_match: bool | None,
) -> dict[str, Any]:
    return {
        "url": report.url,
        "source": source,
        "configured_url_match": configured_url_match,
        "transport": report.transport,
        "health": report.health,
        "payload": report.payload,
        "parse_error": report.parse_error,
        "error": report.error,
    }


def _next_action(
    *,
    selection: str,
    configuration: ConfigState,
    mcp_activation: ActivationState,
    health: Literal["healthy", "unhealthy", "unknown"],
    skill: SkillReport,
    live: McpLiveReport,
    target_action: str | None = None,
    diagnostic_action: str | None = None,
) -> str:
    if selection == "unknown":
        return "Pass --host or add one canonical cymatix-context MCP entry."
    if selection == "ambiguous":
        return "Multiple host configs contain cymatix-context; pass --host explicitly."
    if diagnostic_action is not None:
        return diagnostic_action
    if configuration != "canonical":
        return "Add or repair the canonical cymatix-context MCP entry for this host."
    if target_action is not None:
        return target_action
    if mcp_activation == "disabled":
        return "Enable the cymatix-context MCP entry in the selected host config."
    if mcp_activation == "unknown":
        return "Confirm the selected host has enabled the cymatix-context MCP entry."
    if health != "healthy":
        return "Start or repair the Cymatix server configured for this MCP entry."
    if skill.installation == "missing":
        return "Install or restore the shared cymatix-context skill."
    if skill.activation == "disabled":
        return "Enable the installed cymatix-context skill."
    if skill.activation == "unknown":
        return "Confirm the selected host has enabled the cymatix-context skill."
    if live.state == "disconnected":
        return "Restart or refresh the host MCP session to establish a live connection."
    if live.state == "unknown":
        return "Configure an unambiguous MCP handle and host, then verify the active session registry."
    return "Use cymatix_context for repo questions."


def collect_status(
    *,
    host: str = "auto",
    server_url: str | None = None,
    launcher_url: str | None = DEFAULT_LAUNCHER_URL,
    mcp_config: Path | None = None,
    skill_dir: Path | None = None,
    start_dir: Path | None = None,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Collect independent host, server, launcher, MCP, and skill evidence."""

    workspace = Path(start_dir) if start_dir is not None else Path.cwd()
    home = Path(home_dir) if home_dir is not None else Path.home()
    selection, profile, parsed, inspected_paths = _select_host(
        host=host,
        mcp_config=mcp_config,
        workspace=workspace,
        home=home,
    )

    server_target = _select_server_target(
        explicit_url=server_url,
        configured_url=parsed.configured_url if parsed is not None else None,
    )
    configured_url_match = _configured_url_match(
        server_target=server_target,
        configured_url=parsed.configured_url if parsed is not None else None,
    )
    diagnostic_action = _diagnostic_target_action(configured_url_match)
    server_probe = (
        _probe_json(f"{server_target.request_url.rstrip('/')}/health")
        if server_target.request_url is not None
        else ProbeResult("unreachable", None, None, server_target.reason)
    )
    server_report = map_server_health(
        server_probe,
        url=server_target.reported_url,
    )
    launcher_target = _select_launcher_target(launcher_url)
    launcher_probe = (
        None
        if launcher_target is None
        else (
            _probe_json(f"{launcher_target.request_url.rstrip('/')}/api/state")
            if launcher_target.request_url is not None
            else ProbeResult("unreachable", None, None, launcher_target.reason)
        )
    )
    launcher_state = map_launcher_state(launcher_probe)

    if profile is None or parsed is None:
        configuration: ConfigState = "invalid" if selection == "ambiguous" else "missing"
        live = McpLiveReport("unknown", "", "No host profile was selected.")
        unknown_skill = SkillReport(
            installation="missing",
            activation="unknown",
            path=Path(),
            detail="Skill discovery requires a selected host.",
        )
        return {
            "host": {
                "selection": selection,
                "profile": None,
                "inspected_paths": [str(path) for path in inspected_paths],
            },
            "server": _server_dict(
                server_report,
                source=server_target.source,
                configured_url_match=configured_url_match,
            ),
            "launcher": {
                "url": launcher_target.reported_url if launcher_target is not None else None,
                "state": launcher_state,
            },
            "mcp": {
                "configuration": configuration,
                "activation": "unknown",
                "live": live.state,
                "path": None,
                "detail": live.reason,
            },
            "skill": {
                "installation": unknown_skill.installation,
                "activation": unknown_skill.activation,
                "path": None,
                "detail": unknown_skill.detail,
            },
            "configured_ready": False,
            "guided_ready": False,
            "next_action": _next_action(
                selection=selection,
                configuration=configuration,
                mcp_activation="unknown",
                health=server_report.health,
                skill=unknown_skill,
                live=live,
                target_action=server_target.action,
                diagnostic_action=diagnostic_action,
            ),
        }

    if configured_url_match is True:
        registry_url = (
            f"{server_target.request_url.rstrip('/')}/sessions?status=active"
            if server_target.request_url is not None
            else ""
        )
        if (
            server_target.request_url is not None
            and parsed.handle
            and parsed.handle.strip()
            and parsed.mcp_host
            and parsed.mcp_host.strip()
        ):
            registry = _probe_json(registry_url)
        else:
            registry = ProbeResult(
                "unreachable",
                None,
                None,
                server_target.reason or "No configured MCP identity.",
            )
        live = determine_mcp_live(
            handle=parsed.handle,
            configured_host=parsed.mcp_host,
            registry=registry,
            registry_url=registry_url,
        )
    else:
        registry_url = ""
        match_reason = (
            "The explicit diagnostic server URL does not match the selected host MCP URL."
            if configured_url_match is False
            else "No configured MCP endpoint can be matched to the status probe."
        )
        live = McpLiveReport("unknown", registry_url, match_reason)
    skill = discover_skill(
        profile,
        workspace=workspace,
        home=home,
        config_path=parsed.path,
        explicit_dir=skill_dir,
    )
    configured = _configured_readiness_with_target_evidence(
        configuration=parsed.configuration,
        activation=parsed.activation,
        health=server_report.health,
        configured_url_match=configured_url_match,
    )
    guided = guided_ready(configured, skill.installation, skill.activation)
    return {
        "host": {
            "selection": selection,
            "profile": profile.display_name,
            "inspected_paths": [str(path) for path in inspected_paths],
        },
        "server": _server_dict(
            server_report,
            source=server_target.source,
            configured_url_match=configured_url_match,
        ),
        "launcher": {
            "url": launcher_target.reported_url if launcher_target is not None else None,
            "state": launcher_state,
        },
        "mcp": {
            "configuration": parsed.configuration,
            "activation": parsed.activation,
            "live": live.state,
            "path": str(parsed.path) if parsed.path else None,
            "detail": parsed.detail,
        },
        "skill": {
            "installation": skill.installation,
            "activation": skill.activation,
            "path": str(skill.path),
            "detail": skill.detail,
        },
        "configured_ready": configured,
        "guided_ready": guided,
        "next_action": _next_action(
            selection=selection,
            configuration=parsed.configuration,
            mcp_activation=parsed.activation,
            health=server_report.health,
            skill=skill,
            live=live,
            target_action=server_target.action,
            diagnostic_action=diagnostic_action,
        ),
    }


def _readiness_label(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def _render_text(status: Mapping[str, Any]) -> str:
    """Render the stable report dimensions in a concise operator-facing form."""

    host = status["host"]
    server = status["server"]
    launcher = status["launcher"]
    mcp = status["mcp"]
    skill = status["skill"]
    lines = [
        f"Host: {host['selection']} ({host['profile'] or 'unselected'})",
        "Server: "
        f"{server['transport']}/{server['health']} ({server['url']}; "
        f"source={server.get('source', 'unknown')}, "
        f"configured_url_match={_readiness_label(server.get('configured_url_match'))})",
        f"Launcher: {launcher['state']} ({launcher['url']})",
        f"MCP: configuration={mcp['configuration']}, activation={mcp['activation']}, live={mcp['live']}",
        f"Skill: installation={skill['installation']}, activation={skill['activation']}",
        f"Configured ready: {_readiness_label(status['configured_ready'])}",
        f"Guided ready: {_readiness_label(status['guided_ready'])}",
        f"Next action: {status['next_action']}",
    ]
    if mcp["path"]:
        lines.append(f"MCP path: {mcp['path']}")
    if skill["path"]:
        lines.append(f"Skill path: {skill['path']}")
    if mcp["detail"]:
        lines.append(f"MCP detail: {mcp['detail']}")
    if skill["detail"]:
        lines.append(f"Skill detail: {skill['detail']}")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=invoked_prog(default="cymatix-status"),
        description="Check Cymatix server, launcher, host MCP configuration, and skill state.",
    )
    parser.add_argument("--server-url", default=None)
    parser.add_argument("--launcher-url", default=DEFAULT_LAUNCHER_URL)
    parser.add_argument(
        "--host",
        choices=("auto", "claude-code", "codex", "gemini-cli", "antigravity"),
        default="auto",
    )
    parser.add_argument("--mcp-config", type=Path, default=None)
    parser.add_argument("--skill-dir", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    status = collect_status(
        host=args.host,
        server_url=args.server_url.rstrip("/") if args.server_url is not None else None,
        launcher_url=args.launcher_url.rstrip("/") if args.launcher_url is not None else None,
        mcp_config=args.mcp_config,
        skill_dir=args.skill_dir,
    )
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print(_render_text(status))
    return 0 if status["configured_ready"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
