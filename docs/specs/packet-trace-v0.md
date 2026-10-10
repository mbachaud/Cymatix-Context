# Packet trace v0 (Phase 1)

Status: experimental, opt-in, off by default. Tracks issue #493. This page
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
  "timing_ms": {"total": 12.5},
  "trace": {"trace_id": "..", "parent_span_id": "..", "trace_flags": "01"},
  "prev_hash": null
}
```

Fields that are unavailable are omitted, not invented. `verdict.kind` is
`know`, `miss` (with `reason`) or null; `abstain` is not yet distinguished
from `miss`. `index_fingerprint` is a hash of document count and max rowid,
cached for 60 s; it is a cheap change detector, not a content hash.
`config_hash` is the sha256 of the loaded runtime config with secrets
redacted. `timing_ms` carries only the total in Phase 1.

## Read endpoints

- `GET /trace/{packet_id}`: the record, or 404 (also 404 when disabled).
- `GET /trace/recent?session_id=&limit=`: newest first, `[]` when disabled.

Both scan the local JSONL; they are for debugging and eval harnesses, not
for high-volume use.

## Writer behaviour

A background thread drains a bounded queue (10,000 records). A full queue
drops the new record with a warning. Any write failure is logged at warning
level and never reaches the request. Overhead receipt:
`scripts/bench_trace_overhead.py`.

## Deferred

Phase 2 eval checks and judge-agreement receipt, Phase 3 harness adapters
and `action` events, Phase 4 security detections and labels, Phase 5
export, re-keying, replay tool and JSON Schema. Also not in Phase 1:
`usurpers`, `ingested_at` (only emitted when supplied), `rbac_scope`,
`ingest_source_hash`, instruction-like-text flags, per-stage `timing_ms`.
