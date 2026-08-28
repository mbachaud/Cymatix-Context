# Gemini CLI + `cymatix-context`

Official references: [Gemini CLI Agent Skills](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/using-agent-skills.md)
and [Gemini CLI MCP](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md)
(accessed 2026-08-27). See the [shared overview](cymatix-context.md) for
identity and readiness semantics.

## Install the shared skill

Copy the canonical artifact unchanged to one of these workspace or user
destinations:

```bash
# Workspace scope
mkdir -p .agents/skills/cymatix-context
cp <absolute-repo-path>/skills/cymatix-context/SKILL.md .agents/skills/cymatix-context/SKILL.md
mkdir -p .gemini/skills/cymatix-context
cp <absolute-repo-path>/skills/cymatix-context/SKILL.md .gemini/skills/cymatix-context/SKILL.md

# User scope (choose the native directory you use)
mkdir -p ~/.agents/skills/cymatix-context
cp <absolute-repo-path>/skills/cymatix-context/SKILL.md ~/.agents/skills/cymatix-context/SKILL.md
mkdir -p ~/.gemini/skills/cymatix-context
cp <absolute-repo-path>/skills/cymatix-context/SKILL.md ~/.gemini/skills/cymatix-context/SKILL.md
```

## Configure native MCP

Add this block to workspace `.gemini/settings.json` or user
`~/.gemini/settings.json`:
Before editing an existing JSON file, create a same-directory backup, for
example `cp <config-path> <config-path>.bak-<timestamp>`. The full generated
object below is suitable only for an otherwise empty/new target. For an
existing JSON object, merge only the canonical `mcpServers.cymatix-context`
entry into its existing `mcpServers` object and preserve every unrelated root
setting and server; do not replace the root object.

<!-- BEGIN GENERATED MCP: gemini-cli -->
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
        "CYMATIX_AGENT_KIND": "gemini",
        "CYMATIX_MCP_HANDLE": "<agent-handle>",
        "CYMATIX_MCP_HOST": "gemini-cli"
      }
    }
  }
}
```
<!-- END GENERATED MCP: gemini-cli -->

Direct MCP needs neither the model proxy nor the tray. Restart Gemini CLI after
MCP edits; use `/skills reload` after skill edits.

## Activate, refresh, and verify

Run `gemini mcp list` or `/mcp`, `/skills reload`, and `/skills list`. Confirm
the six lean tools and call `cymatix_health`. Then inspect read-only status:

```bash
cymatix-status --host gemini-cli --json
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

The separate enablement record may be missing or malformed; in that case
activation is `unknown`, not enabled. `configured_ready` combines canonical
configuration, known enabled activation, and healthy server. It cannot prove a
live stdio connection; `mcp.live` requires exact active-session evidence.
Unknown activation or live evidence stays `unknown`; readiness is `null` when
that missing evidence prevents a conclusion; status never fabricates
true/false.

## Disable or roll back

Disable natively with `gemini mcp disable cymatix-context`; roll back with
`gemini mcp enable cymatix-context` after health and smoke checks. A timestamped
settings backup is an alternative rollback. If the adapter is unavailable,
continue with native repository tools.
