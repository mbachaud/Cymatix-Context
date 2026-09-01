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

The file is instructional only. When this canonical file is present at a
native destination, `cymatix-status` reports `skill.installation=present` and
`skill.activation=enabled`. That confirms installation, not that Claude Code
has loaded or discovered it; restart or refresh the host to establish runtime
discovery.

## Configure native MCP

Put this generated block in the project `.mcp.json` or the user
`~/.claude.json` MCP configuration. Do not add a second, forked skill body.
Before editing an existing JSON file, create a same-directory backup, for
example `cp <config-path> <config-path>.bak-<timestamp>`. The full generated
object below is suitable only for an otherwise empty/new target. For an
existing JSON object, merge only the canonical `mcpServers.cymatix-context`
entry into its existing `mcpServers` object and preserve every unrelated root
setting and server; do not replace the root object.

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

A valid loopback configured URL is probed automatically. To deliberately probe
a remote endpoint, pass `--server-url <validated-url>`; it is a health
diagnostic override, not a change to the selected host's MCP endpoint. JSON
reports `server.source` and `server.configured_url_match`. Only a canonical
match with the configured URL (case-normalized scheme/host, effective port, and
normalized trailing slash/path; never DNS equivalence) may supply session,
live, or readiness evidence. A mismatch can report diagnostic health, but
returns nonzero and tells you to update the config or probe the matching URL.
Malformed URLs, URLs containing credentials, query strings, or fragments, and
implicit non-loopback URLs are not probed. Never put credentials in a URL.

The report may show `mcp.activation` or skill activation as `unknown`, and
`mcp.live` remains unknown until an exact active session/handle is observed.
`configured_ready` means canonical config + known enabled activation + healthy
server; it is not live-session evidence. Unknown activation remains
`unknown`, and readiness is `null` whenever that missing evidence prevents a
conclusion. Unknown live evidence remains `mcp.live=unknown`; status never fabricates
true/false from missing evidence.

## Disable or roll back

Use Claude Code's native `/mcp` disable/rejection control, or restore a
timestamped backup of the config. File presence alone remains activation
unknown. If MCP is disabled or unavailable, continue with native repository
and file tools; the backend can still be started independently with:

```bash
python -m uvicorn cymatix_context._asgi:app --host 127.0.0.1 --port 11437
```
