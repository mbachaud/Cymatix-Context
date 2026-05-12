# MCP slimdown plan — keep the adapter, drop the bloat

**Date:** 2026-05-12
**Status:** Decision recorded, implementation phased
**Supersedes:** none — first formal decision on MCP surface shape
**Related:**
- [`docs/clients/claude-code.md`](../clients/claude-code.md) — current MCP routing doc + 8-var identity contract
- [`docs/architecture/SESSION_REGISTRY.md`](../architecture/SESSION_REGISTRY.md) — presence + attribution model
- [`docs/clients/cli.md`](../clients/cli.md) — `helix` CLI v1 (PR #71, merged 2026-05-12)
- [`docs/api/mcp-tools.md`](../api/mcp-tools.md) — current 23-tool inventory

## Background

After PR #71 shipped the cold-start `helix` CLI, pushback surfaced from
several agents to either remove or deprioritize the `helix-context` MCP
server. The argument: 23 `@mcp.tool` decorators, 1,038 LOC of adapter,
15 distinct `HELIX_*` env vars, 228 LOC of identity-contract tests, and
a 12-commit churn pattern that's almost entirely registry/identity
plumbing rather than retrieval value. The CLI now covers `query / ingest
/ status / diag / config` end-to-end, so "why the second public surface?"
is a fair question.

A three-seat advisory council convened (defender, skeptic, evidence).
Both opinion seats converged on the same answer: **keep the MCP, but
slim it aggressively**. The MCP earns its keep only where stdio latency
beats `subprocess.run("helix …")` per turn and where capabilities
genuinely depend on a long-lived process. Everything else is paying
identity-contract bugs for wire-format translation.

This document records the decision and stages the implementation.

## Decision

The MCP server stays. The 23-tool surface shrinks to a **6-tool core**
once CLI parity lands for the deprecation candidates. The 8-var (in
practice 15-var) identity contract collapses to one
`HELIX_IDENTITY_TOML=<path>` pointer.

### The 6-tool MCP core (post-slimdown)

| Tool | Reason it stays |
|---|---|
| `helix_context` | In-loop retrieval. Subprocess spawn per call would dominate latency budget — the whole point of the adapter. |
| `helix_context_packet` | Same latency argument; the packet surface is the primary path for agent-safe retrieval. |
| `helix_ingest` | Hot-loop ingest during a session keeps `participant_id` attribution coherent without re-registration per call. |
| `helix_sessions_list` | Sibling-agent awareness. Even after `helix sessions list` lands in CLI, agents discovering peers mid-turn benefit from the in-process call. |
| `helix_announce` | Self-report flow — "I am `claude-opus-4-7`, override my IDE to `cursor`" — structurally requires the registered participant the long-lived process provides. |
| `helix_health` | Readiness probe at MCP-spawn time before the agent commits to a tool plan. Sub-millisecond, can't reasonably shell out. |

### The 17 tools being deprecated (have CLI peer or are aliases)

**Software-vocabulary aliases — drop entirely, callers should migrate:**
- `helix_document_get` (alias for `helix_gene_get`)
- `helix_document_query` (alias for `helix_context`)
- `helix_document_preview` (alias for `helix_splice_preview`)
- `helix_document_fingerprint` (alias for `helix_fingerprint`)

**Read-only operational — promote to CLI:**
- `helix_stats` → `helix status --stats` (already partially covered by `helix status`)
- `helix_metrics_tokens` → `helix status --tokens`
- `helix_bridge_status` → `helix status --bridge`
- `helix_health` (stays in MCP per above, also kept in `helix status`)
- `helix_session_recent` → `helix sessions recent <handle>`

**Introspection — promote to CLI:**
- `helix_gene_get` → `helix diag gene <id>`
- `helix_neighbors` → `helix diag neighbors <query>`
- `helix_splice_preview` → `helix diag splice <query>`
- `helix_resonance` → `helix diag resonance <query>`
- `helix_fingerprint` → `helix diag fingerprint <query>`

**Batch operations — promote to CLI:**
- `helix_refresh_targets` → `helix query <q> --refresh-only` or `helix diag refresh-plan`
- `helix_consolidate` → `helix consolidate` (cold-start subcommand)

**HITL — split:**
- `helix_hitl_emit` → KEEP IN MCP. Emit-time is per-call from inside a session; CLI doesn't help.
- `helix_hitl_recent` → `helix diag hitl --since` (read path)

Counting: 6 stay, 4 alias removals, 13 CLI promotions, 1 stays despite
being borderline (`helix_hitl_emit`). Net MCP surface: 7 tools post-
slimdown (the 6 core + `helix_hitl_emit`). Adapter LOC projected to
drop from ~1038 to ~450.

## The 3-move plan

### Move 1 — `HELIX_IDENTITY_TOML` consolidation (½ day)

Today's `.mcp.json` env block carries 8 keys that are "not optional in
spirit" plus another 7 the adapter reads opportunistically. Operators
get them wrong constantly.

**Replace with one pointer:**

```json
{
  "mcpServers": {
    "helix-context": {
      "command": "python",
      "args": ["-m", "helix_context.mcp_server"],
      "env": {
        "HELIX_IDENTITY_TOML": "~/.helix/identities/laude.toml"
      }
    }
  }
}
```

```toml
# ~/.helix/identities/laude.toml
mcp_url    = "http://127.0.0.1:11437"
org        = "swiftwing"
party_id   = "swift_wing21"
device     = "swift_wing21"           # optional, defaults to party_id
user       = "max"
agent      = "laude"
agent_kind = "claude-code"
mcp_handle = "laude"
mcp_host   = "claude-code"            # optional, auto-detected from VSCODE_PID / CURSOR_TRACE_ID

[advanced]
timeout_s  = 30
log_level  = "INFO"
```

Loader precedence: env vars > TOML file > defaults. Existing `HELIX_*`
env vars keep working — TOML is the new recommended path, not a forced
migration. The 5 deprecated env vars (`HELIX_PARTY` fallback chain,
`HELIX_LOG_LEVEL`) get a deprecation warning at adapter startup.

### Move 2 — CLI parity for promotion candidates (~3 days)

Build the 14 new CLI subcommands listed above. Reuse `helix_context/bridge.py`
for everything that hits `/sessions/*`; the bridge already does what
the MCP adapter does for those endpoints, the CLI just becomes the
third caller. Most commands are <40 LOC each (argparse stanza + bridge
call + json/text formatter).

Order:
1. `helix sessions {list, recent, register}` — unblocks the
   sibling-awareness counter to the pushback.
2. `helix status --{stats, tokens, bridge}` — folds the operational
   trio into the existing `status` subcommand.
3. `helix diag {gene, neighbors, splice, resonance, fingerprint,
   refresh-plan, hitl}` — introspection cluster.
4. `helix consolidate` — batch operation.

Per-subcommand tests follow the existing `tests/test_cli_*.py` pattern.

### Move 3 — MCP tool deprecation (~1 day, gated on Move 2)

For each of the 17 deprecation candidates, emit a one-time warning on
first call after this version ships:

```
DeprecationWarning: helix_gene_get is deprecated; use `helix diag gene <id>`.
This MCP tool will be removed in helix-context v0.5.0.
```

Two release cycles later (v0.5.0), delete the tool decorators and the
`_http(...)` shims. `docs/api/mcp-tools.md` shrinks from 23 sections to 7.
The 4 software-vocabulary aliases can be removed in the same release
since they have a documented canonical name already.

## Non-goals

- **No replacement of the underlying HTTP API.** Endpoints stay; this
  is purely about which surface exposes them to agents.
- **No new CLI commands beyond the 14 promotion candidates.** If you
  want a new operation, add it as an HTTP endpoint first, then promote
  to CLI; MCP gets it only if it has a session-bound rationale.
- **No change to identity model.** The 4 layers (org/party/participant/
  agent) stay. Only the env-var surface that configures them is
  collapsing.

## Risk register

| Risk | Mitigation |
|---|---|
| Existing operators on `HELIX_*` env vars get warned but not broken | Keep env-var precedence; TOML is additive. Two-cycle deprecation. |
| Agents calling deprecated MCP tools mid-session | Emit warning once per process, not per call. Don't break the call. |
| Sibling agents that depend on `helix_session_recent` mid-turn | Tool stays callable until v0.5.0; CLI variant lands in v0.4.x as the recommended path. |
| `helix_announce` semantics drift if CLI gains a peer | Deliberately NOT promoting `announce` to CLI — it requires the registered participant. Single-source. |
| Adapter LOC reduction doesn't materialize because shared helpers | Acceptable; the goal is fewer tools and one env var, not a specific LOC target. |

## Success criteria

After v0.5.0:
- `docs/api/mcp-tools.md` lists ≤7 tools.
- `.mcp.json` env blocks in `docs/clients/claude-code.md` show one
  `HELIX_IDENTITY_TOML` line.
- `helix --help` lists `sessions`, `diag`, `consolidate` as
  first-class subcommands.
- `tests/test_mcp_server.py` no longer needs separate identity-
  contract regression tests for renamed env-var fallback chains
  (the chain is gone).
- A new operator can wire up Claude Code + Helix in one config file
  edit instead of nine.

## Council provenance

- **Evidence seat** quantified the cost: 1038 LOC, 23 tools, 14 commits
  in 30 days, 5 of which were pure registry plumbing.
- **Defender seat** identified the 5 capabilities that genuinely
  require the long-lived process (presence, gene-author attribution,
  `helix_announce`, tool-picker namespacing, multi-IDE auto-detect).
- **Skeptic seat** identified the 17 tools that have a CLI peer and
  recommended the 6-tool core. Skeptic's wording on the residual
  justification was the most precise: *"the in-loop retrieval call
  where stdio latency beats `subprocess.run('helix query')` per turn;
  everything else is dead weight that costs identity-contract bugs."*

## Implementation pointer

This plan is a phased decision, not a single PR. Move 1 (TOML loader)
should ship first as it's standalone and unblocks the deprecation
warnings. Moves 2 and 3 follow once the CLI peers exist. Track via
issues filed against this doc; close them when each move's PRs merge.
