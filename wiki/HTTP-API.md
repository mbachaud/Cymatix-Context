# HTTP API

- One FastAPI app, bound to **`127.0.0.1:11437`** by default. Start it with
  `cymatix-server`, or directly with
  `python -m uvicorn cymatix_context._asgi:app --host 127.0.0.1 --port 11437`.
- Four groups of routes: **retrieval**, **ingestion and maintenance**,
  **identity and sessions**, and **diagnostics**. Everything speaks JSON.
- The retrieval routes make **no model call**. The only route that reaches an
  LLM is `POST /v1/chat/completions`, and the call it makes is to your
  configured upstream, on your behalf.
- **A loopback bind is not authentication.** Any process on the host can reach
  the port. `[server] admin_token` gates the mutating surface — see
  [Security knobs](#security-knobs).
- Schema-level reference with route-handler line numbers:
  [`docs/api/context-endpoint.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/api/context-endpoint.md).

## Retrieval

| Endpoint | What it does |
|---|---|
| `POST /context` | The primary read. Runs the full pipeline and returns assembled context, per-document citations, `context_health`, and a `know` / `miss` block |
| `POST /context/packet` | The **agent-safe** surface. Pointers and a verdict, no assembled content: `verified` / `stale_risk` / `contradictions` items plus a `refresh_targets` reread plan. Model-free end to end |
| `POST /context/refresh-plan` | `refresh_targets` only. Cheaper than a full packet when the caller already has content cached and just needs to know what to reread |
| `POST /fingerprint` | Navigation-first. Scored document pointers, tier contributions, and honest floor accounting (`evaluated_total` / `above_floor_total` / `returned` / `filtered_by_floor` / `truncated_by_cap`) — no content |
| `GET /context/expand` | 1-hop neighborhood from a document id. Query params: `gene_id` (required), `direction` (`forward` default), `k` (default 5, clamped to `[1, 100]`), `session_id` |
| `POST /v1/chat/completions` | OpenAI-compatible proxy. Intercepts the messages, builds the context window, injects it into the system message, forwards to the configured upstream |

- `/context` and `/context/packet` take the same body. `query` is the only
  required field; an empty or whitespace-only string returns **400**.
- `POST /context` with `response_mode: "packet"` is equivalent to calling
  `/context/packet`. Any other value returns 400.
- `max_genes` on the packet routes defaults to **8** and is clamped to
  `[1, 32]` — distinct from the `/context` window cap
  (`[budget] max_docs_per_turn`, default 12).
- Pass `ignore_delivered: true` to bypass the session working-set elision so
  already-delivered documents can re-fire. Benchmarks need this; production
  callers do not.

## Ingestion and maintenance

| Endpoint | What it does |
|---|---|
| `POST /ingest` | Add content. Body: `content` (required), `source_id`, `source_kind` (`code` / `doc` / `conversation` / `config`), `party_id`. Returns the created document ids |
| `POST /consolidate` | Distills the active session buffer into consolidated documents. **No request body** — any body is ignored by design; it is an aggregate-over-the-buffer operation, not a per-document rewrite |
| `POST /admin/refresh` | Reopen the store connection so external changes become visible, and invalidate the retrieval-layer caches. Returns `{"refreshed": true, "genes": <count>}` |
| `POST /admin/vacuum` | SQLite `VACUUM`. Blocks all writers — maintenance windows only |
| `POST /admin/compact` | Run the density gate over OPEN documents and demote low-signal ones |
| `POST /admin/checkpoint` | WAL checkpoint |
| `POST /admin/swap-db` | Hot-swap the `.db` file without a restart. Constrained by `[server] swap_db_roots` when that list is non-empty |
| `POST /admin/reload` / `POST /admin/shutdown` | Reload config / stop the process |
| `GET /admin/components` | Per-component load state — `idle (not loaded)` vs loaded, which is how you confirm `[hardware] lazy_encoders` is doing its job |

- Every route in this group is behind the admin dependency: with
  `[server] admin_token` set they demand `Authorization: Bearer <token>` and
  return **401** otherwise. `/ingest` and `/consolidate` are included.
- To re-consolidate a *single* document, the supported path is:
  `POST /context/refresh-plan` → reread those sources in your agent →
  `POST /ingest` the refreshed content. A per-document server-side endpoint is
  deliberately not implemented.

## Identity and sessions

| Endpoint | What it does |
|---|---|
| `POST /sessions/register` | Register an agent participant. Requires `party_id` and `handle` (both non-null strings, ≥3 chars). Optional: `workspace`, `pid`, `capabilities`, `display_name`, `agent_kind`, `mcp_host`, `model_id`, … Trust-on-first-use for `party_id` |
| `POST /sessions/{participant_id}/heartbeat` | Keeps the participant live; it TTLs out when the host process stops |
| `POST /sessions/{participant_id}/announce` | Self-reported model identity, used by the MCP shim |
| `GET /sessions` | List registered participants |
| `GET /sessions/{handle}/recent` | Recent documents authored by that handle |
| `GET /session/{session_id}/manifest` | The session delivery log — what has already been shipped to this session, and therefore what gets elided on the next turn |
| `POST /hitl/emit` · `GET /hitl/recent` | Record and read human-in-the-loop pause events |

## Diagnostics

| Endpoint | What it does |
|---|---|
| `GET /health` | Readiness and provenance: `status` (`ok` / `degraded`), compressor model and backend, cost class, document count, upstream URL and reachability, a `hardware` block, and a `calibration` block (`ann_threshold_mode`, `abstain_mode`, `abstain_classes`) |
| `GET /stats` | Corpus metrics — see the key list below. Cheap synchronous read; safe to poll |
| `GET /genes/{gene_id}` | Single document detail |
| `GET /documents/{gene_id}` | **Canonical alias of the route above.** Same handler, same response, its own OpenAPI operation id. New in 0.9.1 |
| `GET /debug/neighbors` | Top-k SEMA neighbors for a query |
| `GET /debug/preview` | Which documents *would* be selected — retrieval through candidate selection, no compression |
| `GET /debug/resonance` | Tier activation profile |
| `GET /debug/pipeline/recent` | Recent pipeline traces |
| `GET /metrics/tokens` | Session and lifetime token counters |
| `GET /vault/status` | Obsidian vault export state |
| `GET /bridge/status` · `GET /replicas` · `GET /health/history` | Bridge, replica, and health-history introspection |

`GET /stats` returns `total_genes`, `open`, `euchromatin`, `heterochromatin`,
`total_chars_raw`, `total_chars_compressed`, `compression_ratio`, a
`compression_tiers` sub-object, plus the manager's `health`, `config`,
`pending_replications`, `session_buffer_size`, and `session_learn_count`.

## A full `/context/packet` exchange

**Request:**

```bash
curl -s http://127.0.0.1:11437/context/packet \
  -H "content-type: application/json" \
  -d '{
    "query": "how does the freshness gate demote stale docs?",
    "task_type": "edit",
    "max_genes": 8,
    "include_raw": false,
    "read_only": true
  }'
```

- `task_type` ∈ `{plan, explain, review, edit, debug, ops, quote}`, default
  `"explain"`. Higher-risk types apply stricter freshness and
  coordinate-confidence gates, so the same query returns a different verdict
  under `edit` than under `explain`.
- `include_raw: true` swaps each item's compressed thumbnail for the full
  document body; `max_item_chars` caps it.
- `clean: true` implies `read_only: true` and resets per-session caches before
  the request runs.

**Response** (a refresh-class miss — the freshness gate found the top document
older than its last verification):

```json
{
  "miss": {
    "miss": true,
    "reason": "stale",
    "top_score": 0.83,
    "ratio": 1.42,
    "escalate_to": [],
    "refresh_targets": ["cymatix_context/retrieval/freshness.py"],
    "do_not_answer_from_genome": true
  },
  "task_type": "edit",
  "query": "how does the freshness gate demote stale docs?",
  "verified": [],
  "stale_risk": [
    {
      "kind": "gene",
      "gene_id": "c084a6dc1f2b",
      "claim_id": null,
      "title": "freshness.py — demote_stale_candidates",
      "content": "The gate scores each candidate against last_verified_at ...",
      "relevance_score": 0.83,
      "live_truth_score": 0.41,
      "source_id": "cymatix_context/retrieval/freshness.py",
      "source_kind": "code",
      "document_id": "cymatix_context/retrieval/freshness.py",
      "volatility_class": "medium",
      "authority_class": "primary",
      "last_verified_at": 1756500000.0,
      "status": "needs_refresh",
      "citations": []
    }
  ],
  "contradictions": [],
  "refresh_targets": [
    {
      "target_kind": "code",
      "source_id": "cymatix_context/retrieval/freshness.py",
      "reason": "fresh verification required before action",
      "priority": 0.87
    }
  ],
  "working_set_id": null,
  "notes": [],
  "coordinate_confidence": 0.78,
  "file_coverage": 0.5,
  "delivery": {
    "pool_size": 41,
    "delivered_count": 8,
    "delivered_gene_ids": [
      "c084a6dc1f2b", "9b1f30ade7c4", "5e2a71c0b83d", "a30f9d4e1c67",
      "77b1e50a9f2c", "1c8d43be0a95", "e04a6f21d738", "b592c7ae30f1"
    ],
    "cap_binding": "assembly_cap"
  },
  "response_mode": "packet"
}
```

How to read it:

- **Branch on `miss` vs `know` first**, before you read `stale_risk` or
  `verified`. Exactly one of the two keys is present on every well-formed
  response; both-set or both-absent is a server bug, not a fall-through case.
- **`do_not_answer_from_genome: true` is the load-bearing bit.** It is always
  `true` on a miss, and agents must honor it. Capable models paint over it
  unless the prompt fragment is in the system prompt — see
  [Agent Contract](Agent-Contract).
- **`reason` splits into two classes.** Escalate-class (`abstain`, `denatured`,
  `sparse`, `no_promoter_match`) populates `escalate_to` from
  `("grep", "rag", "web", "ask_human")` and leaves `refresh_targets` empty.
  Refresh-class (`stale`, `cold`, `superseded`) does the opposite. The
  invariant is enforced by a model validator — a violating construction raises.
- **Item `status`** is one of `verified` / `stale_risk` / `needs_refresh`.
  Only non-`verified` items produce a `refresh_targets` row.
- **`refresh_targets` appears twice with two different shapes.** Inside the
  `miss` block it is a flat list of source strings; at the top level of the
  packet it is a list of `RefreshTarget` objects (`target_kind`, `source_id`,
  `reason`, `priority`). Read the top-level one when you want the priority
  ordering.
- **`delivery.cap_binding`** answers "why did I only get N documents?" — the
  assembly cap or the token budget. This is what
  `[budget] min_delivered_docs` lifts; see [Configuration](Configuration).
- **`plr_confidence`** is attached when `[plr] enabled` is true (it is by
  default): `prob_B`, `logit`, `score_A`, `high_risk`, `artifact_label_set`.
  It is a **query-quality** signal, not a per-document ranking input — every
  candidate in a retrieval shares its feature vector.

The `know` branch replaces the `miss` object with:

```json
{
  "know": {
    "found": true,
    "confidence": 0.83,
    "top_score": 0.92,
    "score_gap": 0.41,
    "lexical_dense_agree": true,
    "gene_id_match": "config.py",
    "coordinate_confidence": 0.78,
    "soft_stale": false
  }
}
```

**Calibration honesty:** the contract *shape* is stable and load-bearing, but
`confidence` is not. At 0.9.x shipped defaults the KnowBlock effectively never
fires — measured maximum confidence ≈ 0.28 against an `emit_floor` of 0.45,
because the `lexical_dense_agree` feature is structurally dead on the
neural-free path. That fails safe (0% false-KNOW) but it means you should
branch on `found` / `reason`, not on the scalar
([#287](https://github.com/mbachaud/Cymatix-Context/issues/287),
[#239](https://github.com/mbachaud/Cymatix-Context/issues/239)).

*Wire field names are legacy. `gene_id`, `delivered_gene_ids`, and
`do_not_answer_from_genome` above are reproduced verbatim — the JSON contract
still speaks the biology lexicon even though the prose and the config surface
have moved to software terms. Renaming them is a tracked Tier-3 change,
deliberately not taken in 0.9.1 because it breaks every existing consumer. See
[Lexicon](Lexicon).*

## `caller_model_class`

`/context` accepts `caller_model_class: "generic" | "small_moe" | "frontier"`
(default `"generic"`; an unknown value returns 400). It selects a render
branch, not a different retrieval:

| Knob | `generic` | `small_moe` | `frontier` |
|---|---|---|---|
| Foveated splice | on if `budget_tier == "broad"` and `foveated_enabled` | on always | **off** — skip the reversal entirely |
| Answer slate | only when the small-model heuristic fires | always, as a JSON object | off |
| Slate bound | 20 entries | 1,500 chars (`[budget] slate_char_budget`) | n/a |
| Assembly cap | the classifier's cap | `min(classifier_cap, 4)` | `max(12, classifier_cap * 2)` |
| Legibility headers | on when `legibility_enabled` | suppressed | on |
| Candidate order | reversed if foveated is active, else forward | reversed | **forward, rank 1 first** |

- The `generic` branch is regression-locked byte-identical to pre-Stage-5
  output, so adding the field to an existing integration changes nothing until
  you set it to something else.
- `frontier` gets rank-1-first ordering because long-context attention expects
  narrative coherence; reversal degrades retrieval for those callers.
- Decoder mode resolves from the classifier class **and** the caller class. For
  the default `generic` caller the modes are `minimal` (arithmetic),
  `condensed` (factual), `full` (procedural, multi_hop), and none at all for
  `default`. The slate-shaped modes belong to `small_moe` only.
- `decoder_mode` on the request body overrides the resolved value; an
  unrecognized value is ignored and the classifier's choice wins.

## Security knobs

```toml
[server]
admin_token = "your-secret"              # 401s /admin/*, /ingest, /consolidate without the bearer
swap_db_roots = ["F:/cymatix/genomes"]   # 403s /admin/swap-db targets outside these roots

[budget]
neutralize_control_tags = true           # default on since 2026-08-19 (#351)
```

- **`admin_token`** (default `""` = no auth) gates every `/admin/*` route plus
  `/ingest` and `/consolidate`. Read-only routes stay open.
- **`swap_db_roots`** (default `[]` = unrestricted) rejects a `/admin/swap-db`
  target that resolves outside the listed roots with **403**, before a store is
  opened.
- **`neutralize_control_tags`** escapes a content-sourced `<cymatix:` at
  assembly time so an ingested document cannot forge the genuine control tags
  a downstream agent trusts. It closes tag forgery only — general indirect
  prompt injection through document text is inherent to context injection.
- A non-loopback `[server] host` with an empty `admin_token` logs a
  `NETWORK-EXPOSED ADMIN SURFACE` warning at startup. Warning only; the bind is
  honored.

Full rationale and receipts: [Configuration](Configuration).

## OpenAI-compatible proxy

```bash
OPENAI_BASE_URL=http://localhost:11437/v1 your-app
```

- No code change on the client side. Cymatix intercepts the messages, runs the
  retrieval pipeline, injects the assembled window into the system message, and
  forwards to `[server] upstream` (Ollama at `http://localhost:11434` by
  default).
- `[server] upstream_timeout` defaults to **180 s**. Raise it for very slow
  models or large prompts; lower it if you want fail-fast.
- In Continue IDE, use **Chat mode, not Agent mode** — the proxy does not route
  tool calls. See [MCP and IDE Integration](MCP-and-IDE-Integration).

## Go deeper

- [`docs/api/context-endpoint.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/api/context-endpoint.md) — the canonical schema reference: every request field, both response branches, the reason vocabulary, the rendering matrix, and worked client examples
- [`docs/api/endpoints.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/api/endpoints.md) — the shorter route round-up
- [`cymatix_context/schemas.py`](https://github.com/mbachaud/Cymatix-Context/blob/master/cymatix_context/schemas.py) — the Pydantic models the wire shapes are generated from
- [`docs/agent-sdk-fragment.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/agent-sdk-fragment.md) — the prompt fragment a consuming agent must carry
- Next: [Agent Contract](Agent-Contract) · [Configuration](Configuration) · [CLI](CLI) · [MCP and IDE Integration](MCP-and-IDE-Integration)
