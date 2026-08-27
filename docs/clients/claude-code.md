# Claude Code + `cymatix-context`

Official references: [Claude Code skills](https://code.claude.com/docs/en/slash-commands)
and [Claude Code MCP](https://code.claude.com/docs/en/mcp) (accessed 2026-08-27).
For the shared contract, identity fields, status semantics, and smoke test, see
the [`cymatix-context` overview](cymatix-context.md).

Related operator references: [session registry](../architecture/SESSION_REGISTRY.md),
[skills bundle](../ops/SKILLS_BUNDLE.md), and [vault status](../ops/OBSIDIAN_VAULT_STATUS.md).

## Install the shared skill

Copy the one canonical artifact to either supported destination:

```bash
# Workspace scope
mkdir -p .claude/skills/cymatix-context
cp <absolute-repo-path>/skills/cymatix-context/SKILL.md .claude/skills/cymatix-context/SKILL.md

# User scope
mkdir -p ~/.claude/skills/cymatix-context
cp <absolute-repo-path>/skills/cymatix-context/SKILL.md ~/.claude/skills/cymatix-context/SKILL.md
```

The file is instructional only. Claude Code may discover it after a restart or
skill refresh; file presence alone does not prove activation.

## Configure native MCP

Put this generated block in the project `.mcp.json` or the user Claude Code
MCP configuration. Do not add a second, forked skill body.

<!-- BEGIN GENERATED MCP: claude-code -->
```json
{
  "mcpServers": {
    "cymatix-context": {
      "command": "python",
      "args": [
        "-m",
        "cymatix_context.mcp_server"
      ],
      "env": {
        "CYMATIX_MCP_URL": "http://127.0.0.1:11437",
        "CYMATIX_ORG": "<org>",
        "CYMATIX_PARTY_ID": "<party-id>",
        "CYMATIX_DEVICE": "<party-id>",
        "CYMATIX_USER": "<user>",
        "CYMATIX_AGENT": "<agent-handle>",
        "CYMATIX_AGENT_KIND": "claude-code",
        "CYMATIX_MCP_HANDLE": "<agent-handle>",
        "CYMATIX_MCP_HOST": "claude-code"
      }
    }
  }
}
```
<!-- END GENERATED MCP: claude-code -->

The adapter identifies its stable MCP protocol namespace as `cymatix`, so the
host-visible tool namespace remains `cymatix_*`. Direct MCP does not require
the optional model proxy or tray.

## Activate, refresh, and verify

Project approval and the host session determine activation; a configured file
cannot prove that approval. Restart Claude Code after project approval or MCP
configuration changes, then run `claude mcp get cymatix-context`,
`claude mcp list`, or `/mcp`. Confirm the six lean tools are listed and call
`cymatix_health`. For a read-only machine summary, run:

```bash
cymatix-status --host claude-code --json
```

The report may show `mcp.activation` or skill activation as `unknown`, and
`mcp.live` remains unknown until an exact active session/handle is observed.
`configured_ready` means canonical config + known enabled activation + healthy
server; it is not live-session evidence.

## Disable or roll back

Use Claude Code's native `/mcp` disable/rejection control, or restore a
timestamped backup of the config. File presence alone remains activation
unknown. If MCP is disabled or unavailable, continue with native repository
and file tools; the backend can still be started independently with:

```bash
python -m uvicorn cymatix_context._asgi:app --host 127.0.0.1 --port 11437
```
