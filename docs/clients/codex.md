# Codex + `cymatix-context`

Official references: [Codex skills](https://learn.chatgpt.com/docs/build-skills)
and [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp) (accessed 2026-08-27).
The [shared overview](cymatix-context.md) defines the cross-host tool and
status contract.

## Install the shared skill

Copy the canonical `SKILL.md` unchanged to either supported destination:

```bash
# Workspace scope
mkdir -p .agents/skills/cymatix-context
cp <absolute-repo-path>/skills/cymatix-context/SKILL.md .agents/skills/cymatix-context/SKILL.md

# User scope
mkdir -p ~/.agents/skills/cymatix-context
cp <absolute-repo-path>/skills/cymatix-context/SKILL.md ~/.agents/skills/cymatix-context/SKILL.md
```

## Configure native MCP

Add this block to the workspace `.codex/config.toml` or user
`~/.codex/config.toml` Codex TOML configuration:
Before editing an existing TOML file, create a same-directory backup, for
example `cp <config-path> <config-path>.bak-<timestamp>`. The full generated
block below is suitable only for an otherwise empty/new target. For an
existing TOML file, merge or append only the canonical
`[mcp_servers.cymatix-context]` and its `.env` table, preserving every
unrelated setting and server table; preserve those entries and do not replace
the whole file.

<!-- BEGIN GENERATED MCP: codex -->
```toml
[mcp_servers.cymatix-context]
command = "python"
args = ["-m", "cymatix_context.mcp_server"]
enabled = true

[mcp_servers.cymatix-context.env]
CYMATIX_MCP_URL = "http://127.0.0.1:11437"
CYMATIX_ORG = "<org>"
CYMATIX_PARTY_ID = "<party-id>"
CYMATIX_DEVICE = "<party-id>"
CYMATIX_USER = "<user>"
CYMATIX_AGENT = "<agent-handle>"
CYMATIX_AGENT_KIND = "codex"
CYMATIX_MCP_HANDLE = "<agent-handle>"
CYMATIX_MCP_HOST = "codex"
```
<!-- END GENERATED MCP: codex -->

Restart Codex after MCP or skill changes. The proxy and tray are optional for
direct MCP; a healthy headless server is sufficient.

## Activate, refresh, and verify

MCP activation follows the native `enabled` field. Restart Codex, confirm the
entry and six lean tools, and call `cymatix_health`. A status report is:

```bash
cymatix-status --host codex --json
```

A valid loopback configured URL is probed automatically. To deliberately probe
a remote endpoint, pass `--server-url <validated-url>`; it overrides the
configured URL. Malformed URLs, URLs containing credentials, query strings, or
fragments, and implicit non-loopback URLs are not probed. Never put credentials
in a URL.

Skill activation is separate. `configured_ready=true` means canonical config,
known enabled MCP activation, and healthy server—not a live host connection.
`mcp.live=connected` requires exact active-session evidence; unknown values are
reported as `null` readiness where activation cannot be established.

## Disable or roll back

Set `enabled = false` for MCP. To disable the skill, add a native
`[[skills.config]]` override with its absolute `SKILL.md` path and
`enabled = false`; restore or set `enabled = true` to roll back. A timestamped
config backup is also a reversible rollback. With MCP disabled, use Codex's
native repository and file tools.
