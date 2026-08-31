"""Read-only integration discovery primitives."""

from .host_profiles import (
    HOST_PROFILES,
    HostDiscovery,
    HostProfile,
    ParsedHostConfig,
    SkillReport,
    discover_host_config,
    discover_profile,
    discover_skill,
    get_profile,
    parse_host_config,
    profile_for_config_path,
    render_mcp_snippet,
)

__all__ = [
    "HOST_PROFILES",
    "HostDiscovery",
    "HostProfile",
    "ParsedHostConfig",
    "SkillReport",
    "discover_host_config",
    "discover_profile",
    "discover_skill",
    "get_profile",
    "parse_host_config",
    "profile_for_config_path",
    "render_mcp_snippet",
]
