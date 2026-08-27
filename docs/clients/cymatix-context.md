# `cymatix-context` client integration

`cymatix-context` is one host-neutral Agent Skill plus one stdio MCP adapter.
It gives an agent a consistent way to retrieve repository context, while the
host keeps its native configuration and lifecycle. It is not a universal model
proxy: direct MCP calls work without configuring a model-proxy route.

## Five independent planes

| Plane | Responsibility | If absent or unavailable |
| --- | --- | --- |
| Agent Skill | Teaches when to retrieve, verify locally, and fall back | The model still works, but without the shared decision policy |
| MCP adapter | Exposes the `cymatix_*` tools over host stdio | Use native repository and file tools |
| Cymatix server | Performs retrieval, ingest, health, and registry operations | MCP calls fail or time out; continue with native tools |
| Optional model proxy | Injects context into OpenAI-compatible model traffic | Direct MCP behavior is unchanged |
| Optional tray/launcher | Supervises and visualizes the server | A separately started healthy server remains available |

The tray is not the backend. `GET /health` on the configured server is the
availability authority; a missing tray does not make a healthy server
unavailable, and a running tray does not make an unhealthy server healthy.

## Canonical names

| Surface | Name |
| --- | --- |
| Product, Agent Skill, MCP entry | `cymatix-context` |
| MCP protocol server/namespace | `cymatix` |
| Python package and modules | `cymatix_context` |
| MCP tool prefix | `cymatix_` |
| CLI prefix | `cymatix-` |
| Environment prefix | `CYMATIX_` |

The one authored skill is [`skills/cymatix-context/SKILL.md`](../../skills/cymatix-context/SKILL.md).
Host guides copy that exact file to their native workspace or user discovery
directory; they do not maintain host-specific skill bodies.

## Identity contract

The adapter carries these eight independent identity variables:

| Variable | Meaning |
| --- | --- |
| `CYMATIX_ORG` | Tenant or team |
| `CYMATIX_PARTY_ID` | Device/party identity used for presence |
| `CYMATIX_DEVICE` | Ingest-time device override, normally the party |
| `CYMATIX_USER` | Human participant |
| `CYMATIX_AGENT` | AI persona or author handle |
| `CYMATIX_AGENT_KIND` | Model/tool family |
| `CYMATIX_MCP_HANDLE` | Live session handle |
| `CYMATIX_MCP_HOST` | Host application |

The four supported profile identity rows are:

| Profile | `CYMATIX_AGENT_KIND` | `CYMATIX_MCP_HOST` |
| --- | --- | --- |
| Claude Code | `claude-code` | `claude-code` |
| Codex | `codex` | `codex` |
| Gemini CLI | `gemini` | `gemini-cli` |
| Antigravity | `gemini` | `antigravity` |

### Model fields

These fields are deliberately distinct:

| Field | Meaning | Effect |
| --- | --- | --- |
| `model` | Downstream model name or family hint | May affect budget and legacy small-model behavior |
| `caller_model_class` | Retrieval/render policy: `generic`, `small_moe`, or `frontier` | Selects the existing retrieval branch |
| `model_id` | Registry/dashboard label sent by `cymatix_announce` | Display and telemetry only; it does not select retrieval |

Pass `model` and `caller_model_class` explicitly to `cymatix_context` when the
caller knows them. An explicit tool argument wins over
`CYMATIX_CALLER_MODEL_CLASS`; an invalid configured default fails adapter
startup with the allowed values. Standard host snippets leave that optional
default unset, so an unidentified caller remains `generic`.

For one compatibility window, the HTTP `/context` route accepts
`downstream_model` only when canonical `model` is absent. This is an HTTP-only
alias; MCP schemas and new client guidance use `model`, and canonical `model`
wins when both are supplied.

## Lean MCP surface

The default profile exposes exactly these six tools:

`cymatix_context`, `cymatix_context_packet`, `cymatix_ingest`,
`cymatix_health`, `cymatix_sessions_list`, and `cymatix_announce`.

Administrative and debug tools are available only with `CYMATIX_MCP_FULL=1`.
If a host cached an older list, refresh or restart that host before diagnosing
a missing tool.

## Status and readiness

Run `cymatix-status --host <profile-id> --json` for a read-only report. It
keeps these dimensions separate:

| Dimension | Values |
| --- | --- |
| Host selection | profile id, `unknown`, or `ambiguous` |
| Server transport / health | `reachable`/`unreachable`; `healthy`/`unhealthy`/`unknown` |
| Launcher state | `running`, `stopped`, `unreachable`, `not_configured` (informational) |
| MCP configuration | `canonical`, `noncanonical`, `missing`, `invalid` |
| MCP activation | `enabled`, `disabled`, `unknown` |
| MCP live | `connected`, `disconnected`, `unknown` |
| Skill installation / activation | `present`/`missing`; `enabled`/`disabled`/`unknown` |
| Readiness | `configured_ready` and `guided_ready`, each `true`, `false`, or `null` |

`configured_ready=true` means canonical configuration, known enabled
activation, and a healthy backend. It does not mean that the proprietary host
has launched the adapter. `guided_ready` additionally requires the shared
skill. `mcp.live=connected` requires positive evidence from an exact current
session/handle match in the active registry; configuration plus `/health`
cannot prove a live host connection. Unknown approval or activation remains
unknown rather than being guessed as enabled.

If a Cymatix call fails, use health if it is callable, then continue with
native repository tools. Cymatix improves a task but never blocks it.

## Manual smoke test

1. Start a healthy backend without a tray or proxy:
   `python -m uvicorn cymatix_context._asgi:app --host 127.0.0.1 --port 11437`
2. Launch `python -m cymatix_context.mcp_server` through the host's native
   stdio configuration.
3. Confirm the six lean tools are listed and call `cymatix_health`.
4. Call `cymatix_announce` with a descriptive `model_id`.
5. Call `cymatix_context` with an explicit `caller_model_class` (and `model`
   when known), then inspect its response metadata.
6. Verify `/sessions?status=active` contains the exact configured
   `CYMATIX_MCP_HANDLE` and `CYMATIX_MCP_HOST`.

The compatibility import target `cymatix_context.server:app` remains available
where an existing runtime script requires it; new setup uses `_asgi:app`.

## Rollback

Disable the entry using the host-native mechanism in its guide, or restore the
timestamped same-directory backup. Re-enable only after health and the smoke
test succeed. Native fallback remains clean: leave MCP disabled and continue
with the repository's normal CLI, search, and file tools.

See the [Claude Code](claude-code.md), [Codex](codex.md),
[Gemini CLI](gemini-cli.md), and [Antigravity](antigravity.md) guides for
native destinations, generated snippets, activation semantics, and reversible
commands.
