---
name: cymatix
description: Use Cymatix MCP tools for architectural, historical, semantic, or cross-file repo questions before broad raw-file exploration. Prefer local file reads for exact current code or line-accurate edits, and use Cymatix health, ingest, and session tools with the full Cymatix identity contract so context stays attributable and retrievable.
---

# Cymatix Usage

You have access to Cymatix through MCP tools backed by the local Cymatix server at `http://127.0.0.1:11437`.

## Identity Contract

Claude's Cymatix MCP config should carry the full local-first identity shape:

- `CYMATIX_ORG`: tenant or team, for example `swiftwing`
- `CYMATIX_PARTY_ID`: device or party id used for session presence, for example `swift_wing21`
- `CYMATIX_DEVICE`: ingest-time device override; normally keep this equal to `CYMATIX_PARTY_ID`
- `CYMATIX_USER`: human participant handle, for example `max`
- `CYMATIX_AGENT`: AI persona handle for authored ingests, for example `laude`
- `CYMATIX_AGENT_KIND`: optional agent family or tool kind, usually `claude-code`
- `CYMATIX_MCP_HANDLE`: live session handle shown in `cymatix_sessions_list`
- `CYMATIX_MCP_HOST`: host capability tag, for example `claude-code`

Cymatix uses these as separate layers:

- `org`: tenant or team
- `party`: device
- `participant`: human user
- `agent`: AI persona

Important distinction:

- `CYMATIX_MCP_HANDLE` is for session presence and heartbeat.
- `CYMATIX_AGENT` is for ingest attribution.
- Tools are capability tags on the live participant, not a separate identity layer.
- Sub-agents should be modeled as separate participants if the host creates them explicitly.

## Use Cymatix First When

- The question is architectural, historical, semantic, or spans multiple files.
- You want a compressed overview before deciding which files to open.
- You need sibling-agent awareness or recent activity, not just code text.

Start with `cymatix_context`.

## Prefer Local Files When

- You need exact current code, exact line references, or are about to edit a file.
- Cymatix reports sparse, stale, denatured, or unavailable context.
- The task is narrow enough that direct reads are cheaper than a semantic pass.

## Core MCP Tools

- `cymatix_context`: main compressed retrieval for a query.
- `cymatix_health`: check whether Cymatix is up before assuming config is broken.
- `cymatix_ingest`: add meaningful new code, docs, or notes to the knowledge store with full org/party/participant/agent attribution when the MCP env is configured.
- `cymatix_sessions_list`: see active participants.
- `cymatix_session_recent`: inspect recent work from a handle.
- `cymatix_stats`: check knowledge store size and health.

## Workflow

1. For repo understanding, call `cymatix_context` before broad searching.
2. If Cymatix returns strong context, use it to focus follow-up file reads.
3. If the result looks weak or stale, switch to local files without hesitation.
4. After substantial new work, call `cymatix_ingest` so future queries stay useful and attributable.
5. When coordinating with sibling agents, use `cymatix_sessions_list` and `cymatix_session_recent` instead of guessing from logs or git state.
6. Treat session presence and authored ingests as complementary:
   `cymatix_sessions_list` tells you who is alive now; ingest attribution tells you who authored a stored document.
7. After your first `cymatix_health` call in a session, also call
   `cymatix_announce(model_id=...)` once with your model identifier
   (e.g., `"claude-opus-4-7"`, `"gpt-5"`, `"gemini-2-5-pro"`) so the
   dashboard can display your model in the agent badge tooltip. If
   the IDE auto-detection got it wrong, pass `ide_override=...` to
   correct it.

## Attribution Expectations

- A well-configured Claude session should register as a live participant with `CYMATIX_MCP_HANDLE`.
- A Cymatix ingest from that same Claude session should attribute authored documents to:
  `org = CYMATIX_ORG`
  `party = CYMATIX_DEVICE or CYMATIX_PARTY_ID`
  `participant = CYMATIX_USER`
  `agent = CYMATIX_AGENT`
- If `CYMATIX_AGENT_KIND` is unset, Cymatix should fall back to `CYMATIX_MCP_HOST`.
- If the MCP server is up but attribution looks wrong, check the MCP env first, not just the Cymatix server env.

## Failure Handling

- If a Cymatix MCP tool fails, call `cymatix_health` next.
- If the server is down, fall back to normal local exploration.
- Do not let Cymatix unavailability block exact code work.
