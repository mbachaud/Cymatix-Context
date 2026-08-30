# MCP and IDE Integration

- Three ways an editor or agent host reaches Cymatix: the **MCP shim**
  (Claude Code, Cursor, Claude Desktop), the **OpenAI-compatible proxy**
  (Continue, or any client that honors `OPENAI_BASE_URL`), and raw
  [HTTP API](HTTP-API).
- **The MCP adapter is a thin shim, not a second engine.** It speaks stdio
  JSON-RPC to the host and proxies every tool call over HTTP to a running
  `cymatix-server`. It does no retrieval of its own — so the backend must be
  up, and everything on [Configuration](Configuration) applies unchanged.
- The default tool surface is deliberately **lean**. Every registered tool's
  name, description, and JSON schema is injected into the host's context on
  *every* turn, so 0.9.1 ships ten core tools and hides the admin, diagnostic,
  and legacy surface behind `CYMATIX_MCP_FULL=1`.
- **Canonical `cymatix_document_*` names are the ones to call.** The legacy
  biology-named tools still work under the full surface, with a deprecation
  nudge in their docstrings. Nothing was removed.
- **The consuming agent must carry the prompt fragment.** Without it, capable
  models paint over `do_not_answer_from_genome: true` and answer from their
  training prior anyway. See [Agent Contract](Agent-Contract).

## Setup

Install the extra, run the backend, then register the shim:

```bash
pip install "cymatix-context[mcp]"
cymatix-server                      # binds 127.0.0.1:11437
```

```json
{
  "mcpServers": {
    "cymatix-context": {
      "command": "python",
      "args": ["-m", "cymatix_context.mcp_server"],
      "cwd": "/absolute/path/to/your/project",
      "env": {
        "CYMATIX_MCP_URL": "http://127.0.0.1:11437"
      }
    }
  }
}
```

- Claude Code reads this from a project-level `.mcp.json` or a user-level
  `~/.claude/mcp.json`. Cursor and Continue take the same `mcpServers` block
  under their own MCP config file.
- The server self-identifies as `cymatix`, so tools appear to the host as
  `mcp__cymatix__*`.
- On Windows, wrap the stdio launcher as `cmd /c python ...` when `python` is
  not directly resolvable in the host's spawn environment.

## The tool surface

**Core set — what ships by default (ten tools).**

| Tool | Does | Backed by |
|---|---|---|
| `cymatix_document_query` | Build a compressed context window for a query | `POST /context` |
| `cymatix_context` | The same operation under its original name | `POST /context` |
| `cymatix_context_packet` | Agent-safe bundle: `verified` / `stale_risk` items plus the reread plan and a `know` / `miss` block | `POST /context/packet` |
| `cymatix_document_get` | Fetch one document by id | `GET /genes/{id}` |
| `cymatix_document_preview` | Which documents *would* be selected — retrieval through candidate selection, no compression. Takes `max_docs` | `GET /debug/preview` |
| `cymatix_document_fingerprint` | Scores and pointers, no content. Takes `max_results`, `profile` | `POST /fingerprint` |
| `cymatix_document_neighbors` | Top-k neighbors for a query. Takes `k` (default 10) | `GET /debug/neighbors` |
| `cymatix_ingest` | Contribute content to the knowledge store | `POST /ingest` |
| `cymatix_health` | Readiness probe — compressor, document count, upstream | `GET /health` |
| `cymatix_sessions_list` | Sibling-agent awareness: who else is registered | `GET /sessions` |

`cymatix_document_query` defaults its `session_id` to the MCP subprocess's own
session, so repeated calls within one host session elide already-delivered
documents automatically.

**Canonical and legacy names.** The five `document_*` tools are thin
pass-throughs to existing handlers — identical behavior, better names.

| Canonical (in the core set) | Legacy (full surface only) |
|---|---|
| `cymatix_document_query` | `cymatix_context` *(also in the core set)* |
| `cymatix_document_get` | `cymatix_gene_get` |
| `cymatix_document_preview` | `cymatix_splice_preview` |
| `cymatix_document_fingerprint` | `cymatix_fingerprint` |
| `cymatix_document_neighbors` | `cymatix_neighbors` |

**`CYMATIX_MCP_FULL=1` unhides everything else** — 24+ tools in total. Set it
in the same `env` block. What appears:

- **Retrieval / store:** `cymatix_refresh_targets`, `cymatix_stats`,
  `cymatix_resonance`, `cymatix_consolidate`, plus the legacy names above.
- **Session registry:** `cymatix_session_recent`, `cymatix_announce`.
- **HITL events:** `cymatix_hitl_emit`, `cymatix_hitl_recent`.
- **Operational:** `cymatix_metrics_tokens`, `cymatix_bridge_status`,
  `cymatix_swap_db`.

Accepted truthy values: `1`, `true`, `yes`, `on`. Pruning is non-fatal — if the
adapter cannot enumerate or remove a tool it logs a warning and leaves the full
surface exposed. Correctness over token savings.

## Identity

The eight identity variables are not optional in spirit. Anything you omit
falls back to a default that erodes attribution: badges read `unknown` and the
`authored_by_*` columns fill with `mcp-<pid>` instead of your handle.

```json
"env": {
  "CYMATIX_MCP_URL":    "http://127.0.0.1:11437",
  "CYMATIX_ORG":        "your-org",
  "CYMATIX_PARTY_ID":   "your-device",
  "CYMATIX_DEVICE":     "your-device",
  "CYMATIX_USER":       "you",
  "CYMATIX_AGENT":      "laude",
  "CYMATIX_AGENT_KIND": "claude-code",
  "CYMATIX_MCP_HANDLE": "laude",
  "CYMATIX_MCP_HOST":   "claude-code"
}
```

| Variable | What it sets |
|---|---|
| `CYMATIX_MCP_URL` | Backend base URL. Default `http://127.0.0.1:11437` |
| `CYMATIX_MCP_TIMEOUT` | Per-request timeout in seconds. Default 30 |
| `CYMATIX_MCP_HANDLE` | This session's handle in the registry. Default `mcp-<pid>` |
| `CYMATIX_PARTY_ID` | Party this participant belongs to. Falls back to `CYMATIX_DEVICE`, then `CYMATIX_PARTY`, then the hostname |
| `CYMATIX_MCP_HOST` | Which IDE spawned the process — becomes a capability tag. **Optional since 2026-05-06:** the adapter auto-detects the host from `VSCODE_PID` / `CURSOR_TRACE_ID`. Set it only to override the detection |
| `CYMATIX_AGENT_KIND` | The vendor/family axis (`claude-code`, `gemini-cli`, `codex`) — orthogonal to host |
| `CYMATIX_ORG` / `CYMATIX_USER` / `CYMATIX_AGENT` / `CYMATIX_DEVICE` | The four attribution layers stamped on ingested content |
| `CYMATIX_MCP_FULL` | Expose the full tool surface |

Per-host conventions:

| Host | `CYMATIX_MCP_HOST` | Typical `CYMATIX_AGENT` |
|---|---|---|
| Claude Code (CLI) | `claude-code` | `laude` |
| Claude Desktop | `claude-desktop` | `laude` |
| Antigravity (Gemini) | `antigravity` | `raude` |
| Cursor | `cursor` | `taude` |
| VS Code Continue | `vscode-continue` | `laude` or per-user |

## How a call actually travels

Two hops, always:

```
Claude Code  --stdio JSON-RPC-->  cymatix_context.mcp  --HTTP-->  cymatix-server
   (host)                          (thin adapter)                 127.0.0.1:11437
```

- **On subprocess start** the adapter registers itself as a participant:
  it reads the identity env, posts to `/sessions/register`, starts a heartbeat,
  and attaches capability tags `["mcp_tools", "host:<CYMATIX_MCP_HOST>"]`.
  **Registration failure is non-fatal** — tool calls still proxy, and a warning
  is logged to the adapter's stderr.
- **On every tool call** the adapter wraps the arguments into an HTTP request,
  attaches the four identity layers on ingest paths, and forwards the response
  back over stdio.
- **On heartbeat** it pings `/sessions/{participant}/heartbeat` on a timer.
  Stop the host process and the participant TTLs out naturally.

**Verifying the route.** Ask the agent to call `cymatix_health`, then check the
dashboard at `http://127.0.0.1:11437/launcher`: the **Parties** panel should
show your `CYMATIX_PARTY_ID`, **Identities** your `CYMATIX_USER` workspace, and
**Agents** a participant matching `CYMATIX_MCP_HANDLE` with the
`host:<...>` capability tag.

| Symptom | Cause | Fix |
|---|---|---|
| Tool calls return network errors | `CYMATIX_MCP_URL` unreachable; backend not running | Start `cymatix-server` |
| Calls work but no participant in the dashboard | Registration silently failed | Read the adapter's stderr for the `_register_with_registry` warning |
| Ingested content shows `agent=unknown` | `CYMATIX_AGENT` unset in the MCP env | Set it in the `env` block |
| Agent appears as `mcp-<pid>` | `CYMATIX_MCP_HANDLE` did not carry | Set it, or accept the per-process unique default |
| Two panels collide on one handle | Both share `CYMATIX_MCP_HANDLE` | Give each a distinct handle, or omit it |
| `cymatix_sessions_list` empty | The bridge import failed at adapter startup | Check the adapter's startup log — usually a missing dependency in the spawn environment |
| Only ten tools visible | The lean default profile | Set `CYMATIX_MCP_FULL=1` |

## The agent-side skill

[`skills/cymatix/SKILL.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/skills/cymatix/SKILL.md)
is the contract Claude follows when calling these tools — the identity contract
and the tool-use rules. It is purely instructional; it runs no code. Install it
project-scoped or user-global:

```bash
mkdir -p .claude/skills/cymatix
cp <repo>/skills/cymatix/SKILL.md .claude/skills/cymatix/SKILL.md

# or once, for every project
mkdir -p ~/.claude/skills/cymatix
cp <repo>/skills/cymatix/SKILL.md ~/.claude/skills/cymatix/SKILL.md
```

## Continue IDE

```yaml
models:
  - name: Cymatix (Local)
    provider: openai
    model: gemma3:e4b
    apiBase: http://127.0.0.1:11437/v1
    apiKey: EMPTY
    roles: [chat]
    defaultCompletionOptions:
      contextLength: 128000
      maxTokens: 4096
```

**Use Chat mode, not Agent mode.** The proxy intercepts messages and injects
context; it does not route tool calls. Agent mode will appear to work and then
fail on the first tool invocation. If you want tool-shaped access from
Continue, add the `mcpServers` block above instead.

## OpenAI-compatible proxy

```bash
OPENAI_BASE_URL=http://localhost:11437/v1 your-app
```

Zero code changes. Cymatix intercepts the messages, runs the retrieval
pipeline, injects the assembled window into the system message, and forwards to
`[server] upstream`. Details: [HTTP API](HTTP-API).

## Which surface to pick

| Surface | Best for | Trade-off |
|---|---|---|
| **MCP** | Claude Code, Cursor, Claude Desktop — agents that should *decide* when to retrieve | Costs schema tokens every turn; needs the backend running |
| **HTTP proxy** | Continue, or any OpenAI-compatible app you cannot modify | Retrieval is automatic and unconditional; no tool routing |
| **CLI** | Scripts, CI, subprocess-driving agents, cold starts | No daemon at all, but pays process start-up per call. See [CLI](CLI) |

All three run the same pipeline against the same store and return the same
JSON shapes, so an integration can move between them without changing its
parser.

*Tool responses keep legacy field names on the wire — `gene_id` in a
`cymatix_document_get` or `cymatix_document_neighbors` result is verbatim, not
a leak from an unconverted page. The tool names and the prose moved to software
terms; renaming the JSON contract is a tracked Tier-3 change, deliberately not
taken in 0.9.1 because it breaks every existing consumer. See
[Lexicon](Lexicon).*

## Go deeper

- [`docs/clients/claude-code.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/clients/claude-code.md) — the routing reference: the two hops, the full identity contract, per-host variants, request lifecycle
- [`docs/api/mcp-tools.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/api/mcp-tools.md) — per-tool input schemas
- [`skills/cymatix/SKILL.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/skills/cymatix/SKILL.md) — the agent-side identity contract and tool-use rules
- [`docs/architecture/SESSION_REGISTRY.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/architecture/SESSION_REGISTRY.md) — the server-side presence and attribution model
- [`docs/agent-sdk-fragment.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/agent-sdk-fragment.md) — the prompt fragment the consuming agent must carry
- Next: [Agent Contract](Agent-Contract) · [HTTP API](HTTP-API) · [CLI](CLI) · [Getting Started](Getting-Started)
