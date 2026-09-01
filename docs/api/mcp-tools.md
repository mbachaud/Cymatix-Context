# Cymatix Context — MCP Tools

Source of truth: [`cymatix_context/mcp/mcp_server.py`](../../cymatix_context/mcp/mcp_server.py)
(re-exported by the `cymatix_context.mcp_server` shim module for the documented
`python -m cymatix_context.mcp_server` entry point). This is a thin stdio
JSON-RPC adapter — every tool call proxies over HTTP to a running cymatix
server (default `http://127.0.0.1:11437`, override via `CYMATIX_MCP_URL`).
No pipeline logic lives in this file; it is pure transport + parameter
shaping.

The server self-identifies as **`cymatix`** (`MCPServer("cymatix")`). MCP
hosts that namespace tool names by server (Claude Code among them) expose
every tool below as `mcp__cymatix__<tool_name>` — e.g.
`mcp__cymatix__cymatix_document_query`.

**Two surfaces, one process.** By default the server prunes itself down to
a **lean 11-tool core set** at startup — the full 25-tool surface costs
~4-5K tokens of schema on every turn, most of it for admin/debug tools an
agent rarely calls. Set `CYMATIX_MCP_FULL=1` to expose everything. See
`_MCP_CORE_TOOLS` / `_apply_mcp_profile()` in the module for the pruning
mechanics (it calls `MCPServer.remove_tool()` for every non-core name;
failure to enumerate/prune falls back to leaving the full surface exposed).

This doc mirrors the ROSETTA Tier 2 canonical-name push (`docs/ROSETTA.md`,
issue `#87`): the `document_*` tool names are the software-vocabulary
canonical spellings; the older biology-named tools (`gene_get`,
`splice_preview`, `neighbors`, ...) remain callable, unremoved, and three of
them carry a docstring deprecation nudge (**R4**, see below).

## Core tool set (default surface, 11 tools)

Exposed with no configuration. Every one of the five `cymatix_document_*`
entries is a byte-identical pass-through to a legacy tool listed in the
back-compat table further down — same HTTP call, same response shape.

| Tool | Purpose | Parameters | Wire route |
|---|---|---|---|
| `cymatix_context` | Primary retrieval — compressed context window for a query. | `query: str`, `decoder_mode: str \| None` ("condensed" default / "broad" / "dense"), `model: str \| None`, `caller_model_class: str \| None` ("generic" default / "small_moe" / "frontier"), `session_id: str \| None` (defaults to this MCP subprocess's stable session id) | `POST /context` |
| `cymatix_context_packet` | Agent-safe evidence packet — `verified` / `stale_risk` buckets plus `refresh_targets`, instead of raw content. | `query: str`, `task_type: str = "explain"` ("plan"\|"explain"\|"review"\|"edit"\|"debug"\|"ops"\|"quote"), `max_genes: int = 8` | `POST /context/packet` |
| `cymatix_ingest` | Add content to the knowledge store, with attribution forwarded from this process's env. | `content: str`, `content_type: str = "text"`, `metadata: dict \| None` | `POST /ingest` |
| `cymatix_health` | Cheap readiness probe — compressor backend, doc count, upstream URL. | *(none)* | `GET /health` |
| `cymatix_sessions_list` | List active session-registry participants (sibling-agent awareness). | `party_id: str \| None`, `status: str = "active"` ("active"\|"stale"\|"all"), `workspace: str \| None` | `GET /sessions` |
| `cymatix_announce` | Self-report this session's model identity (+ optional IDE override) to the dashboard. Requires a prior successful registry handshake — returns `{"ok": False, ...}` otherwise. | `model_id: str`, `ide_override: str \| None` | *(no HTTP call — uses the process's registered `AgentBridge`)* |
| `cymatix_document_get` | Fetch one document by ID. Canonical alias for `cymatix_gene_get`. | `document_id: str` | `GET /genes/{id}` |
| `cymatix_document_query` | Same as `cymatix_context`. Canonical alias for `cymatix_context`. | identical to `cymatix_context` above | `POST /context` |
| `cymatix_document_preview` | Dry-run retrieval — candidates without the splice/compression step. Canonical alias for `cymatix_splice_preview`. | `query: str`, `max_docs: int = 12` (canonical param name; forwarded on the wire as `max_genes`) | `GET /debug/preview` |
| `cymatix_document_fingerprint` | Navigation-first fingerprints (scores + pointers, no assembled content). Canonical alias for `cymatix_fingerprint`. | `query: str`, `max_results: int \| None`, `profile: str \| None` | `POST /fingerprint` |
| `cymatix_document_neighbors` | Top-k co-activation neighbors for a query (light `cymatix_resonance`). Canonical alias for `cymatix_neighbors`. | `query: str`, `k: int = 10` | `GET /debug/neighbors` |

## Legacy-name back-compat table

Five biology-named tools each have a canonical `document_*` counterpart
registered in the core set above. Both names stay callable indefinitely —
ROSETTA's rename policy is additive-only, no removals
(`docs/ROSETTA.md` "What we are explicitly NOT doing").

| Legacy name | Canonical replacement | Signature difference | R4 docstring nudge (#87) |
|---|---|---|---|
| `cymatix_gene_get(gene_id)` | `cymatix_document_get(document_id)` | param renamed `gene_id` → `document_id`; behavior identical | Yes — first docstring line: *"(legacy name — prefer cymatix_document_get)"* |
| `cymatix_context(...)` | `cymatix_document_query(...)` | none — identical signature | No — `cymatix_context` keeps its plain docstring; it is not soft-deprecated (it is also the tool named directly in the module's own usage docs) |
| `cymatix_splice_preview(query, max_genes)` | `cymatix_document_preview(query, max_docs)` | param renamed `max_genes` → `max_docs` on the canonical tool; the legacy tool's own `max_genes` parameter is untouched | Yes — *"...context window (legacy name — prefer cymatix_document_preview)"* |
| `cymatix_fingerprint(...)` | `cymatix_document_fingerprint(...)` | none — identical signature | No — `cymatix_fingerprint`'s docstring carries no nudge |
| `cymatix_neighbors(query, k)` | `cymatix_document_neighbors(query, k)` | none — identical signature | Yes — *"...(light version of cymatix_resonance) (legacy name — prefer cymatix_document_neighbors)"* |

**R4 nudge policy (issue #87, ROSETTA phase R4):** soft-deprecation only —
a docstring hint on the legacy tool's first line naming its specific
canonical replacement, nothing more. No tool is removed, no default
behavior changes, and callers already using a legacy name keep working
unchanged. Only 3 of the 5 legacy tools above actually carry the nudge
text today (`cymatix_gene_get`, `cymatix_splice_preview`,
`cymatix_neighbors`); `cymatix_context` and `cymatix_fingerprint` do not —
verify against the module source before assuming otherwise, since this is
prose describing current code, not a policy guarantee that every pair gets
nudged.

## Full-surface-only tools (`CYMATIX_MCP_FULL=1`)

14 additional tools, hidden by default, exposed once the operator opts in.
`cymatix_gene_get`, `cymatix_splice_preview`, `cymatix_neighbors`, and
`cymatix_fingerprint` from the back-compat table above are also in this
group (their `document_*` aliases are what ships in the core set instead).

**Retrieval / introspection / debugging:**

| Tool | Purpose | Parameters | Wire route |
|---|---|---|---|
| `cymatix_refresh_targets` | Just the reread plan for a high-risk action — no evidence buckets. | `query: str`, `task_type: str = "edit"`, `max_genes: int = 8` | `POST /context/refresh-plan` |
| `cymatix_stats` | Full knowledge-store health/size stats (heavier than `cymatix_health`). | *(none)* | `GET /stats` |
| `cymatix_resonance` | Four-primitive introspection: SEMA prime vector, cymatic spectrum, top-k neighbors, co-activation edges (legacy: `harmonic_links`). | `query: str`, `k: int = 10`, `downsample: int = 64` | `GET /debug/resonance` |
| `cymatix_gene_get` | Legacy — see back-compat table. | `gene_id: str` | `GET /genes/{id}` |
| `cymatix_neighbors` | Legacy — see back-compat table. | `query: str`, `k: int = 10` | `GET /debug/neighbors` |
| `cymatix_splice_preview` | Legacy — see back-compat table. | `query: str`, `max_genes: int = 12` | `GET /debug/preview` |
| `cymatix_fingerprint` | Legacy — see back-compat table. | `query: str`, `max_results: int \| None`, `profile: str \| None` | `POST /fingerprint` |

**Session registry / HITL:**

| Tool | Purpose | Parameters | Wire route |
|---|---|---|---|
| `cymatix_session_recent` | Recent documents authored by a session handle, chronological. | `handle: str`, `limit: int = 10`, `party_id: str \| None`, `since_ts: float \| None` | `GET /sessions/{handle}/recent` |
| `cymatix_hitl_emit` | Record a Human-In-The-Loop pause event. | `pause_type: str`, `task_context: str \| None`, `resolved_without_operator: bool = False`, `tone_uncertainty: float \| None`, `risk_keywords: list[str] \| None`, `recoverability: str \| None`, `participant_id: str \| None`, `party_id: str \| None` | `POST /hitl/emit` |
| `cymatix_hitl_recent` | List recent HITL events, newest first. | `party_id: str \| None`, `pause_type: str \| None`, `since_ts: float \| None`, `limit: int = 20` | `GET /hitl/recent` |
| `cymatix_consolidate` | Distill the session buffer into consolidated knowledge documents. Cheap but non-idempotent. | *(none)* | `POST /consolidate` |

**Admin / ops:**

| Tool | Purpose | Parameters | Wire route |
|---|---|---|---|
| `cymatix_swap_db` | Hot-swap the active knowledge-store `.db` file without restarting the server. | `path: str`, `read_only: bool = False` | `POST /admin/swap-db` |
| `cymatix_metrics_tokens` | Session + lifetime token counters (exact where upstream reports usage, char-estimate otherwise). | *(none)* | `GET /metrics/tokens` |
| `cymatix_bridge_status` | Federation/bridge state — shared-dir inbox count + in-flight signals across instances. | *(none)* | `GET /bridge/status` |

## Environment variables

| Var | Effect |
|---|---|
| `CYMATIX_MCP_URL` | Cymatix HTTP base URL every tool call proxies to. Default `http://127.0.0.1:11437`. |
| `CYMATIX_MCP_TIMEOUT` | Per-request timeout in seconds. Default `30`. |
| `CYMATIX_MCP_FULL` | `1`/`true`/`yes`/`on` exposes the full 25-tool surface at startup instead of the lean 11-tool core set. Unset/anything else = lean default. |
| `CYMATIX_MCP_HANDLE` | This MCP subprocess's session handle (e.g. `"laude"`, `"raude"`). Used for session-registry presence, `cymatix_context`'s default `session_id`, and as an `CYMATIX_AGENT` fallback for telemetry. Falls back to `mcp-<pid>`. |
| `CYMATIX_PARTY_ID`, `CYMATIX_DEVICE`, `CYMATIX_PARTY` | Party/device id — **two different priority orders**, by call site: session-registry presence and `cymatix_hitl_emit`/`cymatix_hitl_recent`'s default `party_id` resolve `CYMATIX_PARTY_ID` > `CYMATIX_DEVICE` > `CYMATIX_PARTY`, falling back to `socket.gethostname()`; `cymatix_ingest`'s attribution `party_id` resolves `CYMATIX_DEVICE` > `CYMATIX_PARTY_ID` > `CYMATIX_PARTY` instead, with no hostname fallback (omitted from the payload if none are set). |
| `CYMATIX_MCP_HOST` | MCP host/tool-family tag for session-registry presence (e.g. `"claude-code"`, `"cursor"`). Default `"unknown"` (normalized to `None` on the wire). Also the `agent_kind` fallback for ingest attribution when `CYMATIX_AGENT_KIND` is unset. |
| `CYMATIX_ORG` | Ingest attribution: `org_id`. |
| `CYMATIX_USER` | Ingest attribution: `participant_handle`. |
| `CYMATIX_AGENT` | Ingest attribution + telemetry `agent_handle`; preferred over `CYMATIX_MCP_HANDLE` for both. |
| `CYMATIX_AGENT_KIND` | Ingest attribution + session-registry `agent_kind` (e.g. `"claude-code"`, `"gemini-cli"`). No fallback for session-registry; ingest attribution falls back to `CYMATIX_MCP_HOST`. |
| `CYMATIX_MCP_LOG_LEVEL` | Log level for the MCP subprocess's own logging (`main()`). Default `"INFO"`. |

**`CYMATIX_MCP_COMPAT` — historical, not implemented.** The module's
top-of-file docstring mentions `CYMATIX_MCP_COMPAT` only to record that it
is unread by current versions. Grepping the module finds zero code reading
this variable — it is a stale leftover from the pre-0.8.5 rename and does
nothing today. Do not depend on it; treat only the vars in the table above
as live.

## Wire fields stay legacy-named (Tier 3, out of scope here)

Renaming *tool and parameter names* (this doc) is a different move from
renaming *wire/JSON field names* inside a tool's response. Every tool above
still returns legacy-named fields — `gene_id`, not `document_id`, inside
result payloads (e.g. `cymatix_document_neighbors` still returns
`neighbors: [{gene_id, ...}]`). Wire-field renaming (Tier 3 of this rename
initiative, per its planning docs) is explicitly out of scope here — no
wire byte for an existing call changes; only tool and parameter *names*
got canonical aliases. See `docs/ROSETTA.md` for the full biology↔software
lexicon these wire fields still use.

## MCP host configuration

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

Add the same `mcpServers` block for Cursor, Continue, or any other MCP
host — the config shape is standard across clients. Set
`"CYMATIX_MCP_FULL": "1"` in `env` to get the full 25-tool surface instead
of the lean 11-tool default.

## Legacy standalone server (rollback only)

[`cymatix_context/mcp/server.py`](../../cymatix_context/mcp/server.py) is a
separate, hand-rolled 3-tool stdio server (`cymatix_context`,
`cymatix_ingest`, `cymatix_stats`) kept only for rollback compatibility. Its
own module docstring says to prefer `python -m cymatix_context.mcp_server`
(the module this document covers) instead. Not part of the tool surface
described above.
