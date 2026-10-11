# Packet trace phases 2–5: implementation plan (issue #493)

Status: DRAFT, 2026-10-10. Phase 1 shipped (`cymatix_context/telemetry/trace.py`,
`server/routes_trace.py`, `server/routes_context.py` `_begin_packet`/`_emit_trace`,
`[trace]` in `config.py`, `tests/test_packet_trace.py`). Spec:
`docs/specs/packet-trace-v0.md`. Every phase is test-first.

## Dependencies and waves

- **Wave A (parallel):** P2 core checks, P3 action-event plumbing + adapters,
  P4 chunk flags (ingest side), P5 re-key / secret scan / JSON Schema.
- **Wave B:** P4 detections (needs P3 `action` events), P5 Parquet + dataset card,
  `action`/`label` events added to export.
- **Last:** P2 judge-vs-trace agreement run (full LLM judge run; `/preflight` first).

### Gated on the 947k p95 overhead measurement

Not blocked: P2, P5, and the pure-function parts of P3/P4 (offline, off the request path).

Gated: any new hot-path field in `build_packet_event` (per-stage `timing_ms`,
`usurpers`, `lane_contribs`, `rbac_scope`, `ingested_at`,
`contains_instruction_like_text`). Each lands behind a flag and re-runs
`scripts/bench_trace_e2e.py`. Default-on `[trace]` anywhere public is also gated.
P4's ingest-time scoring is off the request path but needs an ingest-throughput check.

## Phase 2: eval checks (M; agreement run L wall-clock)

Goal: delivery, know/miss compliance and over-delivery computed from traces
without an LLM; judge-residue mode in the harness; judge-vs-trace agreement receipt.

Acceptance (measurable form):
- On a pinned v0.11.x baseline (receipt SHA, corpus, fingerprint), trace
  delivery@k and know/miss counts equal the harness numbers exactly.
- Agreement receipt: n, Cohen's kappa, raw agreement, 95% CI, enumerated
  disagreements; pass threshold set by the owner (proposed kappa ≥ 0.8).

Files: `cymatix_context/telemetry/trace_checks.py` (`check_delivery`,
`check_know_miss`, `over_delivery`, `classify_residue`, `sample_for_judge`),
`scripts/trace_eval.py`, `scripts/trace_judge_agreement.py`, ERB harness
`--judge-residue` flag (harness location to confirm), gold→`content_hash`
helper using the deployment `KeyedHasher`.

Tests (`tests/test_trace_checks.py`): delivery hit/miss + rank; know/miss
compliance both directions; over-delivery ratio and None without actions;
residue routing + deterministic seeded sample; trace == harness on fixture;
kappa known values.

Workers: Haiku — pure checks + kappa. Sonnet — harness integration, gold-hash
mapping, baseline-equality test.

## Phase 3: harness adapters (L, three PRs)

Goal: capture `action` events and run the grounding check; adapter code stays
outside the core engine (`adapters/trace/`).

Acceptance (proposed): on a fixture session 100% of tool calls carry the latest
`packet_id` for that session; hook adds < 50 ms p95 per tool call and never
blocks; metadata-level actions carry only keyed `args_hash`.

Files: core — `PacketTracer.emit_action()`, `latest_packet_id(session_id)`,
`POST /trace/action`, event filter on `GET /trace/recent`, spec text for the
`action` event. Adapters — `claude_code_hook.py` (PostToolUse),
`otel_genai_exporter.py`, `mcp_proxy.py`, `grounding.py`
(`referenced_chunk_ids`).

Tests (`tests/test_trace_adapters.py`): action schema + join key; orphan action
is not an error; latest packet per session; hook tags last packet; args keyed
not plain; grounding true/false positives; MCP proxy tagging; OTel attr mapping;
admin token on the action endpoint.

Workers: Haiku — endpoint/schema plumbing + pattern-following tests. Sonnet —
hashing-location design, grounding heuristics, hook, MCP proxy.

## Phase 4: security detections + taxonomy labels (M–L)

Goal: `label` events for indirect prompt injection, control-tag forgery, RBAC
leak, corpus poisoning, data exfiltration (+ `benign`), mapped to OWASP LLM
Top 10 / MITRE ATLAS.

Acceptance (proposed, owner sets targets): per-detector labeled fixtures with a
precision/recall receipt (recall ≥ 0.9 synthetic attacks, FPR ≤ 1% benign);
detectors are pure functions, no LLM; `label_source = "trace_check"`.

Files: `telemetry/trace_detect.py` (`detect_injection`, `detect_rbac_leak`,
`detect_poisoning`, `detect_exfil`, `emit_labels`), `telemetry/taxonomy.py`,
ingest-time lexical `instruction_like_score` reusing the
`neutralize_control_tags` detection, persisted `ingested_at` /
`ingest_source_hash`, per-chunk fields in `build_packet_event` (hot path —
gated), `PacketTracer.emit_label()`.

Tests (`tests/test_trace_detect.py`): each detector positive and benign case;
labels restricted to taxonomy ids; lexical score cases; ingest stores flag +
provenance; benign-corpus FPR.

Workers: Haiku — taxonomy constants, label plumbing, fixture tests. Sonnet —
heuristics, ingest change, poisoning statistics, bench re-run.

## Phase 5: export + replay (L)

Goal: `cymatix trace export` / `cymatix trace replay`, JSON Schema, HF dataset card.

Acceptance (measurable form): canary-seeded fixture exports with zero canaries;
every hash is keyed under a fresh export key and none verifies under the
deployment key; secret scan exits 0; `datasets.load_dataset` returns expected
rows/columns. Replay compares `chunk_id` order, ranks, and scores (tolerance
1e-9), excluding timings and `packet_id`.

Files: `cli/cmd_trace.py` (registered in `cli/dispatcher.py`),
`telemetry/trace_export.py` (`rekey_record` = `HMAC(export_key, old_hmac)`,
`refuse_full_level`, `secret_scan`, `write_manifest`, `to_parquet`,
`write_dataset_card`), `docs/specs/packet-trace-v0.schema.json`,
`docs/specs/packet-trace-card.md`, optional `[trace-export]` extra
(`datasets`, `pyarrow`).

Tests (`tests/test_trace_export.py`): refuses `full`; joins preserved within an
export; uncorrelatable across exports; no hash verifies under deployment key;
raw text stripped; canary scan fails; manifest; consent default not shareable;
parquet loads; schema validation; replay pass / fingerprint mismatch refuse /
diff detected.

Workers: Haiku — schema, card, manifest, parquet, CLI wiring + tests. Sonnet —
re-key design, secret scan, replay, fingerprint upgrade.

## Owner decisions (2026-10-10)

- **Adapter key:** server-side hashing. Adapters POST raw args to the local
  `/trace/action` endpoint; the server hashes with the deployment key, which never leaves it.
- **Replay:** takes a separate query file, matched to trace records by `query_hash`.
- **Abstain:** deferred; Phase 2 treats abstain as miss, revisit after Phase 2 lands.
- **Wave A started:** P2 eval checks, P3 action plumbing, P5 export core, P4 taxonomy + label plumbing.

## Owner decisions still open

1. Definition of "out-of-pattern tool call"; source of the requesting principal's scope for RBAC (P4).
2. Whether `index_fingerprint` (doc count + max rowid) becomes a real content hash, so a
   replay match proves identical content (P5).
3. Kappa threshold; detector precision/recall targets.
4. Chain hashes in export: recompute or drop.
5. Issue open questions: `packet_id` vs `pipeline_request_id`, `/vault/trace` migration,
   red-team corpus for the first HF release.
