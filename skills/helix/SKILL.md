---
name: helix
description: Use Helix MCP tools for architectural, historical, semantic, or cross-file repo questions before broad raw-file exploration. Prefer local file reads for exact current code or line-accurate edits, and use Helix health, ingest, and session tools with the full Helix identity contract so context stays attributable and retrievable.
---

# Helix Usage

You have access to Helix through MCP tools backed by the local Helix server at `http://127.0.0.1:11437`.

## Identity Contract

Claude's Helix MCP config should carry the full local-first identity shape:

- `HELIX_ORG`: tenant or team, for example `swiftwing`
- `HELIX_PARTY_ID`: device or party id used for session presence, for example `swift_wing21`
- `HELIX_DEVICE`: ingest-time device override; normally keep this equal to `HELIX_PARTY_ID`
- `HELIX_USER`: human participant handle, for example `max`
- `HELIX_AGENT`: AI persona handle for authored ingests, for example `laude`
- `HELIX_AGENT_KIND`: optional agent family or tool kind, usually `claude-code`
- `HELIX_MCP_HANDLE`: live session handle shown in `helix_sessions_list`
- `HELIX_MCP_HOST`: host capability tag, for example `claude-code`

Helix uses these as separate layers:

- `org`: tenant or team
- `party`: device
- `participant`: human user
- `agent`: AI persona

Important distinction:

- `HELIX_MCP_HANDLE` is for session presence and heartbeat.
- `HELIX_AGENT` is for ingest attribution.
- Tools are capability tags on the live participant, not a separate identity layer.
- Sub-agents should be modeled as separate participants if the host creates them explicitly.

## Use Helix First When

- The question is architectural, historical, semantic, or spans multiple files.
- You want a compressed overview before deciding which files to open.
- You need sibling-agent awareness or recent activity, not just code text.

Start with `helix_context`.

## Prefer Local Files When

- You need exact current code, exact line references, or are about to edit a file.
- Helix reports sparse, stale, denatured, or unavailable context.
- The task is narrow enough that direct reads are cheaper than a semantic pass.

## Core MCP Tools

- `helix_context`: main compressed retrieval for a query.
- `helix_health`: check whether Helix is up before assuming config is broken.
- `helix_ingest`: add meaningful new code, docs, or notes to the genome with full org/party/participant/agent attribution when the MCP env is configured.
- `helix_sessions_list`: see active participants.
- `helix_session_recent`: inspect recent work from a handle.
- `helix_stats`: check genome size and health.

## Workflow

1. For repo understanding, call `helix_context` before broad searching.
2. If Helix returns strong context, use it to focus follow-up file reads.
3. If the result looks weak or stale, switch to local files without hesitation.
4. After substantial new work, call `helix_ingest` so future queries stay useful and attributable.
5. When coordinating with sibling agents, use `helix_sessions_list` and `helix_session_recent` instead of guessing from logs or git state.
6. Treat session presence and authored ingests as complementary:
   `helix_sessions_list` tells you who is alive now; ingest attribution tells you who authored a stored gene.
7. After your first `helix_health` call in a session, also call
   `helix_announce(model_id=...)` once with your model identifier
   (e.g., `"claude-opus-4-7"`, `"gpt-5"`, `"gemini-2-5-pro"`) so the
   dashboard can display your model in the agent badge tooltip. If
   the IDE auto-detection got it wrong, pass `ide_override=...` to
   correct it.

## Attribution Expectations

- A well-configured Claude session should register as a live participant with `HELIX_MCP_HANDLE`.
- A Helix ingest from that same Claude session should attribute authored genes to:
  `org = HELIX_ORG`
  `party = HELIX_DEVICE or HELIX_PARTY_ID`
  `participant = HELIX_USER`
  `agent = HELIX_AGENT`
- If `HELIX_AGENT_KIND` is unset, Helix should fall back to `HELIX_MCP_HOST`.
- If the MCP server is up but attribution looks wrong, check the MCP env first, not just the Helix server env.

## Failure Handling

- If a Helix MCP tool fails, call `helix_health` next.
- If the server is down, fall back to normal local exploration.
- Do not let Helix unavailability block exact code work.
