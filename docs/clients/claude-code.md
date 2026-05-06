# Claude Code → Helix Routing

How a Claude Code session reaches the Helix server, what env identity
travels with each call, and where the agent-side skill lives.

**See also:**
- [`skills/helix/SKILL.md`](../../skills/helix/SKILL.md) — the agent-side skill (identity contract + tool-use rules)
- [`docs/architecture/SESSION_REGISTRY.md`](../architecture/SESSION_REGISTRY.md) — server-side presence + attribution model
- [`docs/ops/SKILLS_BUNDLE.md`](../ops/SKILLS_BUNDLE.md) — how a skills.md file becomes retrievable genes (a different lifecycle — content ingest, not connection routing)

## The shape

```
┌─────────────────────────┐    stdio JSON-RPC    ┌─────────────────────────┐
│   Claude Code (host)    │ ───────────────────► │  helix_context.mcp      │
│   spawns subprocess     │                       │  (MCP adapter)          │
│   per .mcp.json entry   │ ◄─── tool results ── │                          │
└─────────────────────────┘                       └────────────┬────────────┘
                                                                │ HTTP
                                                                ▼ (HELIX_MCP_URL)
                                                  ┌─────────────────────────┐
                                                  │  helix-context server   │
                                                  │  http://127.0.0.1:11437 │
                                                  └─────────────────────────┘
```

Two hops. Claude Code spawns the MCP adapter as a stdio subprocess and
talks JSON-RPC. The adapter proxies every tool call over HTTP to the
helix server. The adapter is a thin shim — it does no retrieval logic
of its own. See [`helix_context/mcp_server.py`](../../helix_context/mcp_server.py)
for the full tool list.

## .mcp.json wiring

Drop this into your Claude Code MCP config (project-level
`.mcp.json` or user-level `~/.claude/mcp.json`):

```json
{
  "mcpServers": {
    "helix-context": {
      "command": "python",
      "args": ["-m", "helix_context.mcp_server"],
      "env": {
        "HELIX_MCP_URL": "http://127.0.0.1:11437",
        "HELIX_ORG":         "swiftwing",
        "HELIX_PARTY_ID":    "swift_wing21",
        "HELIX_DEVICE":      "swift_wing21",
        "HELIX_USER":        "max",
        "HELIX_AGENT":       "laude",
        "HELIX_AGENT_KIND":  "claude-code",
        "HELIX_MCP_HANDLE":  "laude",
        "HELIX_MCP_HOST":    "claude-code"
      }
    }
  }
}
```

On Windows, wrap stdio launchers per global guidance — use `cmd /c python ...`
if `python` isn't directly resolvable in the host's spawn environment.

The eight identity vars are not optional in spirit. Anything you omit
falls back to a default that erodes attribution. Defaults are documented
in [`mcp_server.py`](../../helix_context/mcp_server.py) — the registry
will still accept the registration, but the badges in the dashboard and
the `authored_by_*` columns in the genome will read as `unknown` or
`mcp-<pid>` instead of `laude` / `claude-code`.

## Per-host variants

Same env contract, different `HELIX_AGENT` and `HELIX_MCP_HOST`
combinations. The `HELIX_MCP_HOST` value is what the dashboard's
session pill uses to tag which IDE spawned a given participant.

| Host                  | `HELIX_MCP_HOST`  | Typical `HELIX_AGENT` |
|-----------------------|-------------------|------------------------|
| Claude Code (CLI)     | `claude-code`     | `laude`                |
| Claude Desktop        | `claude-desktop`  | `laude`                |
| Antigravity (Gemini)  | `antigravity`     | `raude`                |
| Cursor                | `cursor`          | `taude`                |
| VS Code Continue      | `vscode-continue` | `laude` or per-user    |

`HELIX_AGENT_KIND` is the vendor/family axis (`claude-code`, `gemini`,
`codex`) — orthogonal to host. The skill's
[Identity Contract](../../skills/helix/SKILL.md#identity-contract)
defines both.

As of 2026-05-05, `HELIX_AGENT_KIND` and `HELIX_MCP_HOST` are
persisted as first-class columns on the `participants` row (not
only smuggled in via `capabilities`). The dashboard's Agents and
Identities panels render a "Claude Code + VS Code" pretty-label
chip when both are present.

As of 2026-05-06, the MCP adapter additionally **auto-detects** the
host IDE from intentional env vars (`VSCODE_PID`, `CURSOR_TRACE_ID`)
and the agent **self-reports** its model via the new `helix_announce`
MCP tool. Together they populate three new columns on the
`participants` row — `ide_detected`, `ide_detection_via`, `model_id`
— and the dashboard renders the full identity in a tooltip on the
agent host chip. See
[`SESSION_REGISTRY.md`](../architecture/SESSION_REGISTRY.md#announce-endpoint)
for the API and [`skills/helix/SKILL.md`](../../skills/helix/SKILL.md#workflow)
for the agent contract.

The legacy `HELIX_MCP_HOST` env var is now optional — the adapter
will detect the IDE without it. Set it only when you want to override
the auto-detection (e.g., running Claude Code from a non-VS-Code
terminal but want the chip to say `"vscode"` anyway).

## Request lifecycle

**On MCP subprocess start** — `mcp_server.py:_register_with_registry()`:

1. Reads `HELIX_MCP_HANDLE`, `HELIX_PARTY_ID` (or `HELIX_DEVICE` / `HELIX_PARTY` /
   hostname), `HELIX_MCP_HOST`, and the workspace cwd.
2. Calls `AgentBridge.register_participant(...)` over HTTP — that posts
   to `/sessions/register` and starts a heartbeat.
3. Capability tags `["mcp_tools", "host:<HELIX_MCP_HOST>"]` are attached
   to the participant row so dashboards can filter by host/IDE.
4. Registration failure is **non-fatal**. Tool calls still proxy. A
   warning is logged.

**On every tool call** (e.g., `helix_context`, `helix_ingest`):

1. Claude Code emits a JSON-RPC `tools/call` over stdio.
2. The MCP adapter wraps the args into an HTTP request and posts to
   the helix server (`HELIX_MCP_URL`).
3. For ingest paths, the adapter attaches the four identity layers
   (`org`, `party`, `participant`, `agent`) so authored genes carry
   attribution. See the contract in
   [SKILL.md "Attribution Expectations"](../../skills/helix/SKILL.md#attribution-expectations).
4. The HTTP server returns the result; the adapter forwards it back
   over stdio.

**On heartbeat** — the bridge pings `/sessions/<participant>/heartbeat`
on a timer so the dashboard's "active 26.7s ago" stays current. Stop
the host process and the participant TTLs out naturally.

## Installing the agent-side skill

The skill at [`skills/helix/SKILL.md`](../../skills/helix/SKILL.md) is
the contract Claude follows when it calls Helix tools. Two install
choices:

**Project-scoped** — copy or symlink into the project's
`.claude/skills/`:

```bash
mkdir -p .claude/skills/helix
cp <repo>/skills/helix/SKILL.md .claude/skills/helix/SKILL.md
```

**User-global** — install once, applies to every project:

```bash
mkdir -p ~/.claude/skills/helix
cp <repo>/skills/helix/SKILL.md ~/.claude/skills/helix/SKILL.md
```

Either way, Claude Code's skill loader picks up `SKILL.md` and exposes
the skill via the `using-superpowers` priority rules. The skill itself
is purely instructional — it does not run code.

## Verifying the route

After wiring `.mcp.json` and starting Claude Code, ask Claude to call
`helix_health`. Then check the dashboard at
`http://127.0.0.1:11437/launcher` (or the launcher tray):

1. **Parties** panel should show your `HELIX_PARTY_ID`.
2. **Identities** panel should show a row for your `HELIX_USER`
   workspace.
3. **Agents** panel should show a participant whose handle matches
   `HELIX_MCP_HANDLE`, with capability tag `host:<HELIX_MCP_HOST>`.

If the host tag is `host:unknown`, your MCP env didn't carry
`HELIX_MCP_HOST`. If the agent appears as `mcp-<pid>` instead of your
chosen handle, `HELIX_MCP_HANDLE` didn't carry. In both cases, fix the
`.mcp.json` env block — the server is doing what the env said.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| MCP tool calls return network errors | `HELIX_MCP_URL` unreachable; server not running | Start `python -m uvicorn helix_context.server:app --host 127.0.0.1 --port 11437` |
| Tool calls work but participant never appears in dashboard | Registration silently failed | Check the MCP adapter's stderr for the warning from `_register_with_registry` |
| Authored genes show `agent=unknown` | `HELIX_AGENT` (or fallback chain) unset in MCP env | Set `HELIX_AGENT` in `.mcp.json` |
| Two Claude panels collide on one handle | Both panels share `HELIX_MCP_HANDLE` | Give each panel a distinct handle, or omit it (`mcp-<pid>` is unique per process) |
| `helix_sessions_list` empty | Helix bridge import failed at adapter startup | Check the adapter's startup log; likely a missing dep in the spawn env |

## What's not covered here

- The genome lifecycle of an ingested skill — see
  [`docs/ops/SKILLS_BUNDLE.md`](../ops/SKILLS_BUNDLE.md).
- Federation / cross-tenant routing — see
  [`docs/architecture/FEDERATION_LOCAL.md`](../architecture/FEDERATION_LOCAL.md).
- Antigravity / Gemini-as-Raude persona — see
  [`docs/architecture/raude_antigravity_persona.md`](../architecture/raude_antigravity_persona.md).
  This doc covers Claude Code specifically; per-host variants follow the
  same env contract with different `HELIX_MCP_HOST` / `HELIX_AGENT` values.
