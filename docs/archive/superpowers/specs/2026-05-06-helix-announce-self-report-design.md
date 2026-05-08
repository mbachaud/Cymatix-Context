# Helix Announce: Self-Report + Auto-Detect for Agent Identity

**Date:** 2026-05-06
**Status:** Design — pending implementation plan
**Predecessor:** [PR #26 — Vendor+host badges](https://github.com/SwiftWing21/helix-context/pull/26) ([plan](../plans/2026-05-05-vendor-host-badges.md))

## Goal

Surface **IDE/CLI + agent model** in the launcher dashboard for any
client, without depending on each vendor's MCP wrapper to set the
correct env vars in `.mcp.json`. Display only what we actually know —
no inferred values, explicit placeholders for what's missing.

## Why

PR #26 made `agent_kind` / `mcp_host` first-class columns on the
`participants` table, plumbed end-to-end from `HELIX_AGENT_KIND` /
`HELIX_MCP_HOST` env vars to the dashboard chip. **It works when the
client wraps correctly** — Claude Code's `.mcp.json` sets the env, the
chip renders "Claude Code + VS Code", everyone's happy.

It breaks when a vendor mis-configures its wrapper. Codex's MCP
adapter sends `mcp_host=codex` (the vendor's own name) instead of
`vscode` (the IDE Codex actually runs inside). Result: the chip
displays "Codex + Codex" — technically correct from the env, but not
informative.

Three brittleness points:
1. Per-vendor `.mcp.json` correctness — every new vendor is another
   place to depend on someone else getting it right.
2. No way to know the **model** (Opus 4.7 vs Sonnet 4.6 vs GPT-5 vs
   Gemini 2.5 Pro) — env vars don't carry it; only the agent knows.
3. No mechanism for the agent to self-correct when env is wrong.

## Goals (testable)

1. **Codex sessions, no client config change**, render with the correct
   IDE — adapter detects VS Code from `VSCODE_PID` env. Chip becomes
   "VS Code + Codex" instead of "Codex + Codex".
2. **Claude Code sessions** render the model on hover when the agent
   self-reports — tooltip shows "Model: Claude Opus 4.7 (1M context)".
3. **No fabricated data.** When the model isn't known, tooltip shows
   "Not announced", not a guess inferred from `agent_kind`.
4. **Backward-compatible.** Sessions registered via PR #26's path
   continue to render as before. New columns are nullable; their
   absence falls back to PR #26 behavior.

## Non-goals

- Validating that the agent is honest about its model — trust-on-first-use,
  same as the rest of the registry.
- Real-time model switching mid-session.
- Backfill of pre-existing participants from `agent_kind`/`mcp_host`.
- Heuristic detection beyond known env-var fingerprints (no PPID
  walking, no terminal-program guessing).

## Architecture

Two complementary mechanisms on the same write surface (the
`participants` row):

```
                                                 ┌─────────────────────────────┐
                                                 │  participants table         │
┌────────────────────────────────────┐           │                             │
│ Adapter at MCP startup             │  PATCH    │  ide_detected      TEXT     │
│ (_register_with_registry +         │ ────────► │  ide_detection_via TEXT     │
│  ide_fingerprint.detect())         │           │  model_id          TEXT     │
└────────────────────────────────────┘           │  (agent_kind, mcp_host      │
                                                 │   from PR #26 still here)   │
┌────────────────────────────────────┐           │                             │
│ Agent at session start             │  PATCH    │                             │
│ (helix_announce MCP tool, called   │ ────────► │                             │
│  per skill instruction)            │           │                             │
└────────────────────────────────────┘           └─────────────────────────────┘
```

### Adapter side: env-var fingerprint chain

A new module `helix_context/launcher/ide_fingerprint.py` exposes one
function:

```python
def detect_ide() -> tuple[str | None, str]:
    """Return (ide_value, detection_via).

    ide_value is one of: "vscode", "cursor", "claude-desktop", or None.
    detection_via documents the evidence: "env:VSCODE_PID",
    "env:CURSOR_TRACE_ID", "explicit:HELIX_MCP_HOST", or "no_match".
    """
```

Fingerprint chain (first match wins):

| Priority | Signal | Result |
|---|---|---|
| 1 | `HELIX_MCP_HOST` set and not `"unknown"` | trust the operator; via=`"explicit:HELIX_MCP_HOST"` |
| 2 | `VSCODE_PID` set | `"vscode"`; via=`"env:VSCODE_PID"` |
| 3 | `CURSOR_TRACE_ID` set | `"cursor"`; via=`"env:CURSOR_TRACE_ID"` |
| 4 | (placeholders for Antigravity, Claude Desktop as we collect their telltale env vars) | tracked in follow-ups |
| 99 | no signal matched | `(None, "no_match")` |

**No PPID walking.** No terminal-program guessing. Only env vars set
intentionally by the host process. The fingerprint module is small
(~50 lines), pure-function, easy to unit-test and easy to extend per
vendor as we learn what they set.

`_register_with_registry` calls `detect_ide()` at startup and passes
both fields through `AgentBridge.register_participant` →
`/sessions/register` → `registry.register_participant`. New nullable
columns; old code paths unaffected.

### Agent side: `helix_announce` MCP tool

New tool exposed by the MCP adapter (`helix_context/mcp_server.py`):

```python
@mcp.tool()
def helix_announce(
    model_id: str,
    ide_override: str | None = None,
) -> dict:
    """Announce agent identity to the registry.

    Updates this MCP session's participant row:
    - model_id: agent's self-reported model identifier (free-form;
      examples: "claude-opus-4-7", "gpt-5", "gemini-2-5-pro").
      No allowlist validation. No vendor inference.
    - ide_override: when set, replaces the adapter's auto-detected
      ide_detected and sets ide_detection_via="agent_override".

    Idempotent: multiple calls overwrite (last write wins). Designed to
    be called once per session; subsequent calls let the agent correct
    itself.
    """
```

Server-side: a new endpoint `PATCH /sessions/{participant_id}/announce`
(or extend `POST /sessions/register` — TBD in implementation plan).
Maps to `registry.update_announcement(participant_id, model_id,
ide_override)`.

### Skill update

`skills/helix/SKILL.md` adds one sentence in the **Workflow** section:

> 7. After your first `helix_health` call in a session, also call
>    `helix_announce(model_id=...)` once with your model identifier
>    so the dashboard can display it on hover.

The agent is the only authority for model. If they skip the skill,
`model_id` stays NULL and the tooltip says "Model: Not announced" —
diagnostic, not broken.

### Display

**Surface chip** — same `chip--agent-host` class as PR #26, but
sources data from the new fields with graceful single-chip degradation:

| `ide_detected` | `agent_kind` | Surface chip |
|---|---|---|
| `"vscode"` | `"claude-code"` | `VS Code + Claude Code` |
| `"vscode"` | NULL | `VS Code` *(single chip)* |
| NULL | `"claude-code"` | `Claude Code` *(single chip)* |
| NULL | NULL | *(no chip)* |

When both `ide_detected` and `agent_kind` are NULL, the `{% if %}` skips
rendering — same fall-through as today.

**Tooltip** — CSS-only `:hover` reveal on the same chip. Always-visible
fields, explicit placeholders for what's missing:

```
swift_wing21 / F:\Projects
laude  ·  active 26.7s ago
─────────────────────────────────
Model:    Claude Opus 4.7 (1M context)
Wrapper:  Claude Code
IDE:      VS Code  (detected via env:VSCODE_PID)
```

When a field is missing:

| Field | Source | Placeholder when missing | Hint |
|---|---|---|---|
| Model | `model_id` | "Not announced" | agent hasn't called `helix_announce` |
| Wrapper | `agent_kind` | "Not set" | `HELIX_AGENT_KIND` env not configured |
| IDE | `ide_detected` | "Not detected" | adapter heuristics didn't fire |

`ide_detection_via` always renders next to the IDE value (or as a
diagnostic when "Not detected"). This makes the gap visible to anyone
reading the tooltip and gives them a clear path to fix it.

**Pretty-mapping for `model_id`** — extend `host_labels.py` (or a new
sibling `model_labels.py`) with a small lookup map:

```python
_MODEL_MAP = {
    "claude-opus-4-7": "Claude Opus 4.7",
    "claude-opus-4-7-1m": "Claude Opus 4.7 (1M context)",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-haiku-4-5": "Claude Haiku 4.5",
    "gpt-5": "GPT-5",
    "gemini-2-5-pro": "Gemini 2.5 Pro",
    # ... grows as agents announce new IDs
}
```

**Unknown `model_id` echoes verbatim** — same pattern as `host_labels`.
A new model that's not in the map renders as whatever string the agent
sent. No fabrication; no silent drop.

## Data model — schema changes

Add three columns to `participants` (idempotent `ALTER TABLE`, same
pattern as PR #26):

```sql
ALTER TABLE participants ADD COLUMN ide_detected TEXT;        -- "vscode", "cursor", "claude-desktop", NULL
ALTER TABLE participants ADD COLUMN ide_detection_via TEXT;   -- "env:VSCODE_PID", "explicit:HELIX_MCP_HOST", "agent_override", "no_match"
ALTER TABLE participants ADD COLUMN model_id TEXT;            -- agent self-reported, NULL until announced
```

Existing columns kept:
- `agent_kind` — wrapper vendor, still sourced from `HELIX_AGENT_KIND`
- `mcp_host` — kept for backward-compat reads only; new writes go to
  `ide_detected`. Migration path noted below.

## Migration / backward-compat

- Sessions registered before this change: NULL on all three new
  columns. Display falls back to PR #26's `agent_kind` + `mcp_host`
  rendering. No regression.
- Sessions registered after this change with old client configs: the
  fingerprint chain populates `ide_detected` from `VSCODE_PID` etc.
  Chip renders correctly without the client changing anything.
- `mcp_host` is read-only deprecated. New code paths read
  `ide_detected` first, fall back to `mcp_host` if NULL. After a
  reasonable adoption period, `mcp_host` can be removed (separate plan).

## Failure modes

| Failure | Behavior |
|---|---|
| `VSCODE_PID` set but Code crashed before MCP started | adapter still records `ide_detected="vscode"` — fine; reflects what we know at register time |
| Agent skips `helix_announce` | `model_id` stays NULL; tooltip shows "Model: Not announced"; everything else works |
| Agent calls `helix_announce(model_id="bogus-name-xyz")` | stored as-is; pretty-map echoes verbatim; tooltip shows "Model: bogus-name-xyz" — honest about what was reported |
| Agent calls `helix_announce(ide_override="cursor")` from a VSCode session | `ide_detected` becomes "cursor", `ide_detection_via="agent_override"`; tooltip shows "IDE: Cursor (agent_override)" — gives the operator a way to debug if this is wrong |
| Two MCP sessions on same `HELIX_MCP_HANDLE` race on announce | last write wins (idempotent PATCH); not a real concern in practice |

## File-level breakdown (for the implementation plan to follow)

| File | Action | Responsibility |
|---|---|---|
| `helix_context/launcher/ide_fingerprint.py` | **create** | env-var fingerprint chain, `detect_ide() -> (str\|None, str)` |
| `helix_context/genome.py` | modify | add 3 idempotent ALTER TABLE statements |
| `helix_context/schemas.py` | modify | add 3 fields to `Participant` and `ParticipantInfo` |
| `helix_context/registry.py` | modify | persist + project new fields; add `update_announcement()` method |
| `helix_context/server.py` | modify | new endpoint for the announce PATCH; thread fields through `/sessions/register` |
| `helix_context/bridge.py` | modify | `AgentBridge.announce()` HTTP wrapper |
| `helix_context/mcp_server.py` | modify | call `detect_ide()` at startup; expose `helix_announce` tool |
| `helix_context/launcher/host_labels.py` | modify (or split) | add `model_pretty()` and `compose_tooltip()` |
| `helix_context/launcher/collector.py` | modify | populate tooltip fields on entries |
| `helix_context/launcher/templates/components/agents_panel.html` | modify | add `data-tooltip` attribute or `<div class="tooltip">` block |
| `helix_context/launcher/templates/components/participants_panel.html` | modify | same tooltip pattern |
| `helix_context/launcher/static/launcher.css` | modify | `:hover` tooltip styling (no JS) |
| `skills/helix/SKILL.md` | modify | add the workflow sentence about `helix_announce` |
| `docs/clients/claude-code.md` | modify | document the new tool + auto-detect; deprecate `HELIX_MCP_HOST` guidance |
| `docs/architecture/SESSION_REGISTRY.md` | modify | document new columns + the PATCH endpoint |

## Testing strategy

Each module gets unit tests for its own concern:

- `ide_fingerprint`: each branch of the chain — env var present
  triggers correct return; no env vars triggers `(None, "no_match")`;
  `HELIX_MCP_HOST` explicit overrides everything.
- Schema migration: same pattern as PR #26 — column existence + idempotency.
- Pydantic models: round-trip with new fields.
- Registry: persist + project + `update_announcement` (overwrite, idempotent).
- Endpoint: PATCH accepts payload; rejects invalid participant_id.
- Bridge: `AgentBridge.announce()` sends correct body.
- MCP tool: `helix_announce` invokes bridge with right args.
- Collector: tooltip fields populate; missing fields render as "Not …".
- Pretty-map: known IDs map; unknown echo; "unknown" sentinel returns None.
- E2E plumbing test: env → adapter detect → register → announce → list →
  collector entry has all three new fields.
- Template smoke: render with synthetic data, grep for tooltip text +
  surface chip text.

## Out of scope (deferred)

- Per-vendor `.mcp.json` template installer (so users don't hand-curate
  env blocks)
- Backfill of pre-existing participants from `agent_kind`/`mcp_host` →
  `ide_detected`
- `mcp_host` column removal (separate cleanup plan after adoption)
- Validating `model_id` against an allowlist
- Real-time model switching telemetry
- Tracking vendor explicitly (no `model_vendor` column — agents report
  IDs, no inference; future agents can carry vendor in `model_id`
  itself)
