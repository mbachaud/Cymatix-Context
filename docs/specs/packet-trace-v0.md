# Packet trace v0 (Phase 1)

Status: experimental, on by default at the `metadata` level since 2026-10-18 (set `enabled = false` or `CYMATIX_TRACE_ENABLED=0` to turn it off). Tracks issue #493. This page
describes the Phase 1 subset; later phases are listed at the end.

A packet trace records what Cymatix delivered for one query: ids, keyed
hashes, ranks, scores, the know/miss verdict, and a replay fingerprint.
Retrieval is deterministic, so the record is replayable evidence rather
than only a log. Traces are written locally and never leave the machine.

## Configuration

`[trace]` in `cymatix.toml`, with `CYMATIX_TRACE_ENABLED` (`1` = on),
`CYMATIX_TRACE_LEVEL` and `CYMATIX_TRACE_PATH` env overrides. Precedence is
env > toml > default, resolved at use time (same split as `[telemetry]`).

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch. Off means no file, no thread. |
| `level` | `"metadata"` | `off`, `metadata` or `full`. |
| `path` | `""` | Trace directory; empty means `<genome dir>/traces` (`genomes/main/traces` when the store is in memory). |
| `hash_chain` | `false` | Each record carries `prev_hash`, the sha256 of the previous serialized line. |
| `sampler_ratio` | `1.0` | Fraction of requests recorded. |
| `rotate_bytes` | 64 MiB | `packets.jsonl` is renamed to `packets-<UTC stamp>.jsonl` past this size. |

## Levels

- `metadata`: no raw query and no chunk text anywhere in the record.
  `query_hash` and `content_hash` are HMAC-SHA256 under a per-deployment
  secret (`<trace dir>/.trace_key`, created on first use, mode 0600 where
  supported). A plain SHA-256 of a short query is reversible by dictionary,
  so it is never used.
- `full`: additionally the raw `query` and per-chunk `text`. Local only.

## packet_id and propagation

Every `/context` and `/context/packet` response carries a `packet_id`
(ULID, 26 chars) in the JSON body and in the `X-Cymatix-Packet-Id` header,
whether or not tracing is on. The MCP `cymatix_context` and
`cymatix_context_packet` tools return the body, so they carry it too.

An inbound W3C `traceparent` header is validated; a valid one is stored
as `trace.{trace_id, parent_span_id, trace_flags}` and an invalid one is
ignored. When OTel is active, the span gets `cymatix.packet_id` (the only
attribute added).

## `packet` event (schema_version "0")

One JSON object per line in `packets.jsonl`.

```json
{
  "schema_version": "0", "event": "packet",
  "packet_id": "01M4...", "pipeline_request_id": "ab12cd34ef56",
  "session_id": "sess-1", "ts": 1791582325.294, "level": "metadata",
  "query_hash": "<hmac>", "query_len_terms": 3,
  "replay": {"index_fingerprint": "<32 hex>", "doc_count": 9,
             "config_hash": "<sha256>", "cymatix_version": "0.11.1",
             "git_sha": "<sha or null>"},
  "chunks": [{"chunk_id": "g..", "doc_id": "g..", "content_hash": "<hmac>",
              "rank": 1, "score": 2.5, "source_kind": "code",
              "lane_contribs": {"fts5": 1.0}}],
  "verdict": {"kind": "know", "confidence": 0.8},
  "timing_ms": {"total": 12.5,
                "stages": {"classify": 0.04, "extract": 0.31, "express": 8.12,
                           "rerank": 0.2, "splice": 0.9, "assemble": 2.4},
                "signals": {"tag_exact": 0.6, "fts5": 5.3, "harmonic": 1.1}},
  "pool": {"tag_exact": 12, "fts5": 190, "harmonic": 30, "pool_total": 205},
  "trace": {"trace_id": "..", "parent_span_id": "..", "trace_flags": "01"},
  "prev_hash": null
}
```

Fields that are unavailable are omitted, not invented. `verdict.kind` is
`know`, `miss` (with `reason`) or null; `abstain` is not yet distinguished
from `miss`. `index_fingerprint` is a hash of document count and max rowid,
cached for 60 s; it is a cheap change detector, not a content hash.
`config_hash` is the sha256 of the loaded runtime config with secrets
redacted.

### Latency profiling fields

`timing_ms.total` is unchanged (route wall time). Three additive fields say
where the time went; all reuse timers that already run, are not content, and
are kept at both levels and by `trace_export`. Values are ms rounded to 0.01.

- `timing_ms.stages`: per-request pipeline stage ms (the same `_stage_timer`
  stages as `cymatix_pipeline_stage_seconds`: `classify`, `extract`,
  `express`, `rerank`, `splice`, `assemble`, plus sub-stages such as
  `delivery_lookup`). Sub-stages nest inside their parent, so do not sum.
  `persist` runs in the background and is not included.
- `timing_ms.signals`: the store's per-signal ms for the retrieval inside
  `express` (`tag_exact`, `tag_prefix`, `fts5`, `harmonic`, ...; only signals
  that ran). Multi-sub-query requests sum across sub-queries.
- `pool`: candidates scored per tier (genes carrying a contribution from that
  tier) and `pool_total`, the candidate pool size reaching Tier 5.

Emitted by `POST /context` and the ERB ablation ladder; `/context/packet`
records carry `timing_ms.total` only. Concurrency: stages are contextvar
scoped (exact per request). Signals and `pool_total` are read from the store's
`last_*` attributes under the same lock and at the same point as
`last_query_scores` (issue #350 semantics), so under concurrent serving on one
store they can reflect a neighbouring request; profile with serial requests
for exact attribution.

## `action` event (schema_version "0")

One record per agent tool call, joined to the packet that informed it.
Adapters `POST /trace/action` with `{session_id, tool_name, args,
packet_id?}` (same admin-token guard as the read endpoints; 404 when
tracing is disabled). Hashing is server-side: the deployment key never
leaves the server and raw args are never written at `metadata`.

| field | notes |
|---|---|
| `event` | `"action"` |
| `schema_version` | `"0"` |
| `packet_id` | explicit if given, else the session's latest packet; `null` if none |
| `orphan` | `true` iff `packet_id` is null (not an error) |
| `session_id`, `ts`, `level`, `trace` | as for `packet` |
| `tool_name` | as supplied |
| `args_hash` | HMAC-SHA256 of canonical JSON (sorted keys, compact) of `args` |
| `args_keys` | sorted top-level keys of `args` (empty if not an object) |
| `args` | raw args, `full` level only |

The route returns `{packet_id, orphan}`. The session-to-latest-packet map
is in memory (LRU, 10,000 sessions) and starts empty after a restart.
Actions share the packet writer and hash chain.

## `label` event (schema_version "0", Phase 4)

Appended to `packets.jsonl` by `PacketTracer.emit_label(packet_id, label,
label_source="trace_check", confidence=None, evidence=None)`. It refers to
an existing packet by `packet_id` and shares the writer, hash chain and
rotation of `packet` events. Labels are recorded only; no detector in
Phase 4 emits them.

```json
{
  "schema_version": "0", "event": "label",
  "packet_id": "01M4...", "ts": 1791582325.294,
  "labels": ["indirect_prompt_injection"], "label_source": "trace_check",
  "confidence": 0.9, "evidence": {"reason": "instruction-like chunk"},
  "prev_hash": null
}
```

- `labels` holds one label from the vocabulary in
  `cymatix_context/telemetry/taxonomy.py`: `indirect_prompt_injection`,
  `control_tag_forgery`, `rbac_leak`, `corpus_poisoning`,
  `data_exfiltration`, `benign`. An unknown label raises `ValueError`.
- `label_source` is one of `trace_check`, `human`, `judge`. Any other value
  raises `ValueError`.
- Validation runs before the tracing-disabled check, so a bad argument
  fails even when tracing is off. A valid call with tracing off is a no-op
  that returns `None`.
- `confidence` and `evidence` are optional. When not supplied they are
  written as `null`.
- The taxonomy maps each non-benign label to candidate OWASP LLM Top 10
  (2025) and MITRE ATLAS ids. Every mapping is `verified: false` until a
  human review. Do not cite the mapping as settled.

## Read endpoints

- `GET /trace/{packet_id}`: the record, or 404 (also 404 when disabled).
- `GET /trace/recent?session_id=&limit=&event=`: `event` filters to `packet` or `action`; newest first, `[]` when disabled.

Both scan the local JSONL; they are for debugging and eval harnesses, not
for high-volume use.

## Writer behaviour

A background thread drains a bounded queue (10,000 records). A full queue
drops the new record with a warning. Any write failure is logged at warning
level and never reaches the request. Overhead receipt:
`scripts/bench_trace_overhead.py`.

### End-to-end overhead on `POST /context`

`scripts/bench_trace_e2e.py` drives four in-process apps (trace off, an
off-vs-off control, `metadata`, `full`; `hash_chain = true` on both ON arms)
over identical 200-doc stores through FastAPI `TestClient`, same queries,
rotating-order interleave, n = 1000 after 60 warmup. Receipt:
`benchmarks/receipts/trace_overhead_e2e_2026-10-10.json` (carries the commit
SHA, the observed runtime `enabled`/`level`/`hash_chain` per arm, and 1060
lines written per ON arm).

| Arm | request p50 | p95 | p99 | paired delta p50 | paired delta mean |
|---|---|---|---|---|---|
| off | 9.28 ms | 13.04 ms | 16.11 ms | - | - |
| off2 (control) | 9.24 ms | 13.14 ms | 15.33 ms | -0.02 ms | -0.05 ms |
| metadata | 9.54 ms | 13.28 ms | 15.02 ms | +0.28 ms | +0.24 ms |
| full | 9.63 ms | 13.35 ms | 15.51 ms | +0.34 ms | +0.30 ms |

Reading: the median and mean added cost is about 0.3 ms per request (< 1 ms
holds at p50 and mean). The paired-delta p95 (about 2.0 ms) is **not** a
trace cost: the off-vs-off control shows the same 1.8 ms p95 from per-request
jitter (the difference of two noisy ~9 ms measurements), so a paired p95 at
this request latency cannot resolve a sub-millisecond effect. The unpaired
p95/p99 of the ON arms sit inside the OFF arm's own spread. Caveats:
`TestClient` adds its own per-request overhead (a floor shared by all arms),
the store is small, and the writer thread competes for the GIL only on the
same process, which this setup does include.

## Deferred

Phase 2 eval checks and judge-agreement receipt, Phase 3 harness adapters
and `action` events, Phase 4 security detectors (the `label` event and
taxonomy schema are in place; nothing emits labels yet), Phase 5
export, re-keying, replay tool and JSON Schema. Also not in Phase 1:
`usurpers`, `ingested_at` (only emitted when supplied), `rbac_scope`,
`ingest_source_hash`, instruction-like-text flags.
