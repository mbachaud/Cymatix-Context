# Google Antigravity + `cymatix-context`

Official references: [Antigravity Agent Skills](https://antigravity.google/docs/skills),
[Antigravity MCP](https://antigravity.google/docs/mcp), and [Gemini CLI migration](https://www.antigravity.google/docs/cli/gcli-migration/)
(accessed 2026-08-27). The [shared overview](cymatix-context.md) covers the
common contract.

## Install the shared skill

Copy the canonical file unchanged to either supported destination:

```bash
# Workspace scope
mkdir -p .agents/skills/cymatix-context
cp <absolute-repo-path>/skills/cymatix-context/SKILL.md .agents/skills/cymatix-context/SKILL.md

# User scope
mkdir -p ~/.gemini/config/skills/cymatix-context
cp <absolute-repo-path>/skills/cymatix-context/SKILL.md ~/.gemini/config/skills/cymatix-context/SKILL.md
```

## Configure native MCP

Add this block to workspace `.agents/mcp_config.json` or global
`~/.gemini/config/mcp_config.json`:

<!-- BEGIN GENERATED MCP: antigravity -->
```json
{
  "mcpServers": {
    "cymatix-context": {
      "command": "python",
      "args": [
        "-m",
        "cymatix_context.mcp_server"
      ],
      "disabled": false,
      "env": {
        "CYMATIX_MCP_URL": "http://127.0.0.1:11437",
        "CYMATIX_ORG": "<org>",
        "CYMATIX_PARTY_ID": "<party-id>",
        "CYMATIX_DEVICE": "<party-id>",
        "CYMATIX_USER": "<user>",
        "CYMATIX_AGENT": "<agent-handle>",
        "CYMATIX_AGENT_KIND": "gemini",
        "CYMATIX_MCP_HANDLE": "<agent-handle>",
        "CYMATIX_MCP_HOST": "antigravity"
      }
    }
  }
}
```
<!-- END GENERATED MCP: antigravity -->

Refresh or restart Antigravity after configuration or skill changes. Direct MCP
does not require the model proxy or tray; a healthy backend can run headless.

## Activate, refresh, and verify

In the MCP manager, use Refresh/Manage Servers and restart the application.
Confirm the six lean tools and call `cymatix_health`, then run:

```bash
cymatix-status --host antigravity --json
```

An entry's `disabled: true` is authoritative. Status separates configuration,
activation, health, launcher state, skill state, and live evidence. A canonical
enabled entry plus healthy server yields `configured_ready=true`, but only an
exact active session/handle match yields `mcp.live=connected`. Unknown
activation or live evidence stays `unknown`; readiness is `null` when that
missing evidence prevents a conclusion; status never fabricates true/false.

## Disable or roll back

Set `disabled: true` or use the UI disable action. Roll back with UI enable or
`disabled: false`, or restore a timestamped config backup after the smoke test.
If MCP is disabled, Antigravity continues with native repository and file tools.
