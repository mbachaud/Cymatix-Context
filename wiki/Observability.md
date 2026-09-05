# Observability

- Cymatix emits OpenTelemetry **traces, metrics and logs** covering everything
  upstream of the LLM answer boundary — pipeline stage timings, which retrieval
  signals fired, know/miss decision mix, freshness demotions, knowledge-store and
  WAL health, and the proxy's token accounting.
- Telemetry is **off by default**. Turn it on with `CYMATIX_OTEL_ENABLED=1`,
  `[telemetry] enabled = true`, or by running under the tray launcher, which
  exports the env var for you once the collector answers.
- The stack is a **native sidecar** — five pinned binaries, no Docker required.
  A containerized alternative exists and is bit-for-bit compatible: same
  dashboard JSON, same datasource UIDs, only the receiver runtime differs.
- **Metric names are a Prometheus contract and do not get renamed.** Several
  carry the project's legacy biology vocabulary (`cymatix_genome_*`,
  `cymatix_chromatin_state_total`, `cymatix_ribosome_call_seconds`). Dashboard
  *panel titles* use the software vocabulary and reference the legacy term
  inline, so navigation stays one step. The bidirectional index is
  [Lexicon](Lexicon).
- Seven Grafana dashboards ship with the repo and are auto-provisioned.

## Bring the stack up

```powershell
scripts\setup-grafana-telem.ps1     # Windows
```

```bash
scripts/setup-grafana-telem.sh      # Linux / macOS
```

- The wrapper downloads the five pinned binaries into `tools/native-otel/`,
  renders runtime configs, and wires dashboard provisioning. It is **idempotent**
  — re-runs skip already-installed binaries and only refresh configs.

Then start the supervisor:

```bash
cymatix-launcher --tray              # daily driver: tray icon + the cymatix backend
cymatix-launcher --no-autostart      # observability only, no backend
```

| Service | Port | Purpose |
|---|---|---|
| OTel Collector | 4317 (gRPC), 4318 (HTTP) | Receives OTLP from cymatix |
| Prometheus | 9090 | Metrics storage |
| Tempo | 3200 | Trace storage |
| Loki | 3100 | Log storage |
| Grafana | 3000 | Dashboards (`admin` / `admin`) |

Install the client packages on the cymatix side:

```bash
pip install "cymatix-context[otel]"
```

Landing dashboard: <http://localhost:3000/d/cymatix-overview>.

## Turning it on

Either env vars:

```bash
export CYMATIX_OTEL_ENABLED=1
export CYMATIX_OTEL_ENDPOINT=localhost:4317   # default
python -m uvicorn cymatix_context._asgi:app --port 11437
```

or the TOML section:

```toml
[telemetry]
enabled  = true
endpoint = "localhost:4317"
```

- **Precedence is per knob: env var > `[telemetry]` TOML > code default**, and
  **env wins in both directions** — an explicit `CYMATIX_OTEL_ENABLED=0` silences
  a TOML `enabled = true`. An env var set to the empty string counts as unset.
  Resolution happens in `telemetry.otel.resolve_telemetry_settings()`, not in the
  TOML loader.
- **The tray launcher needs no configuration.** Once it starts (or adopts) the
  native stack *and* the collector's OTLP port is actually accepting connections,
  it exports `CYMATIX_OTEL_ENABLED=1` into the backend child's environment. That
  export is an env-layer value, so it beats the shipped `enabled = false` default
  but never overrides an explicit `CYMATIX_OTEL_ENABLED` you set yourself.
- **The port probe is the safety interlock.** If the stack fails to start, or a
  service spawns but never becomes ready, the probe fails and the export is
  skipped — a backend dialing a dead collector would wedge its gRPC channel.
- The endpoint is **not** exported by the launcher; it resolves through the normal
  env > TOML > default chain, so an explicit `[telemetry] endpoint` is respected.

| Env var | `[telemetry]` key | Default | Purpose |
|---|---|---|---|
| `CYMATIX_OTEL_ENABLED` | `enabled` | off | Master switch (env: `1` = on) |
| `CYMATIX_OTEL_ENDPOINT` | `endpoint` | `localhost:4317` | OTLP gRPC endpoint |
| `CYMATIX_OTEL_INSECURE` | `insecure` | on | Plain gRPC, for local dev |
| `CYMATIX_OTEL_SAMPLER_RATIO` | `sampler_ratio` | `1.0` | Trace sampler, 0.0–1.0. Drop to `0.1` at high QPS |
| `CYMATIX_OTEL_REDACT_QUERY` | `redact_query` | on | Hash query strings (env: `0` = raw) |
| `CYMATIX_OTEL_LOGS_ENABLED` | `logs_enabled` | on | Ship Python logs → collector → Loki |
| `CYMATIX_OTEL_LOGS_LEVEL` | `logs_level` | `INFO` | Minimum log level forwarded |

Full env-precedence context, including the non-telemetry `CYMATIX_*` variables:
[Configuration](Configuration).

## The seven dashboards

All live under `deploy/otel/grafana/dashboards/`. The native install auto-syncs
them; after editing a JSON, re-run the launcher render step or restart the
launcher to propagate.

| Dashboard | File | What it shows |
|---|---|---|
| **Operations Overview** | `cymatix-overview.json` | The default landing view: `/context` request rate, latency p50/p95/p99 by health, cache hit/miss/partial, per-stage pipeline latency, compressor backend cost class and active model, knowledge-store size, WAL health, and the structured proxy log stream. Cross-links to the rest |
| **Agent Usage** | `cymatix-agent-usage.json` | `/context` call mix and latency bucketed by `caller_model_class` |
| **Pipeline Observatory** | `cymatix-pipeline-observatory.json` | Research panels on real instrument names: retrieval-signal activation share, per-signal store latency, A/B bucket accumulation, p99 `/context` latency, co-activation edges by provenance, lifecycle-tier distribution, hub concentration and inbound-degree stats |
| **Internals & Research** | `cymatix-internals.json` | The tuning view: dense-cosine calibration distribution, shard-router fan-out and discrimination, know/miss/abstain decision mix, session-elision token savings, splice compression ratio by caller class. For deep-design work, not day-to-day operations |
| **GenAI** | `cymatix-genai.json` | The OTel `gen_ai.*` standard surface: token throughput by type, TTFT quantiles per model, finish-reason mix, cost per hour and top spend by model, cache-outcome breakdown, and the `cymatix.proxy` log stream. A contract test keeps every panel query backed by an emitted metric |
| **Retrieval Quality + HITL** | `cymatix-retrieval-hitl.json` | Per-query ellipticity distribution, health-status mix, the denatured-rate alert stat, budget-tier mix, ellipticity percentiles, and human-in-the-loop pause events (alignment, override, escalation) broken down by party |
| **Know/Miss (Hallucination Visibility)** | `cymatix-know-miss.json` | The goal-gates view: non-know share against its 10% budget, miss rate by reason, know-confidence percentiles against `emit_floor`, abstain fires by gate, freshness demotions, the splice-ratio/abstain balancing pair, dense cosine by arm, shard fan-out, session-elision savings, and a latency row with load-annotation discipline |

- **Internals and Know/Miss overlap on purpose.** Internals is the tuning view;
  Know/Miss is the goal-gates view. Same series, different question.
- The Know/Miss dashboard is where the
  [#287](https://github.com/mbachaud/Cymatix-Context/issues/287) calibration
  problem is visible: at shipped defaults `cymatix_know_confidence` sits far below
  `[know] emit_floor`, so the know share is ~0 and every decision lands on the
  miss side. That is the fail-safe direction, not a broken panel — see
  [Agent Contract](Agent-Contract).

## Metric families

Names below are **verbatim** — they are the query contract.

### Request and pipeline

| Metric | Type | Labels |
|---|---|---|
| `cymatix_context_latency_seconds` | histogram | `health`, `budget_tier`, `cold_tier_used` |
| `cymatix_pipeline_stage_seconds` | histogram | `stage` — all seven stages |
| `cymatix_context_health_status_total` | counter | `status` ∈ {aligned, sparse, stale, denatured} |
| `cymatix_context_ellipticity` | histogram | `party` — coverage × density × freshness per query |
| `cymatix_context_cache_outcome_total` | counter | `outcome` ∈ {hit, miss, partial} |
| `cymatix_splice_ratio` | histogram | `caller_model_class` — assembled-window compression ratio |

### Retrieval signals

| Metric | Type | Labels |
|---|---|---|
| `cymatix_tier_fired_total` | counter | `tier` — which retrieval signal fired |
| `cymatix_tier_contribution` | histogram | `tier` — score magnitude that signal added |
| `cymatix_rrf_fused_score` | histogram | none — attribute-less by design; a per-document label would explode cardinality |
| `cymatix_dense_cosine` | histogram | `arm` ∈ {hot, cold} |
| `cymatix_pki_candidates` | histogram | none — documents hit by at least one path-key pair |
| `cymatix_pki_pairs_skipped_total` | counter | none — pairs over the noise cutoff |
| `cymatix_fingerprint_filtered_total` | counter | `cause` ∈ {floor, cap} |
| `cymatix_shard_fanout` | histogram | none — shards consulted per routed query |
| `cymatix_shard_discrimination` | histogram | none — fraction of healthy shards hit |

### Agent contract and delivery

| Metric | Type | Labels |
|---|---|---|
| `cymatix_know_decision_total` | counter | `outcome` ∈ {know, miss, abstain}, `reason` |
| `cymatix_know_confidence` | histogram | none — know outcomes only |
| `cymatix_abstain_total` | counter | `gate` ∈ {floor_and_ratio, ratio_only}, `fusion_mode` |
| `cymatix_freshness_demotion_total` | counter | `status` ∈ {stale, missing, unknown, superseded} |
| `cymatix_session_elided_total` | counter | none — elision-stub events |
| `cymatix_session_tokens_saved_total` | counter | none — session working-set elision savings |

- **`cymatix_session_tokens_saved_total` is the counter that would turn the ~40%
  multi-turn savings figure from an unverified design estimate into a receipt.**
  It is instrumented; there is no published run. See
  [Benchmarks and Receipts](Benchmarks-and-Receipts).

### Knowledge store and graph

| Metric | Type | Labels |
|---|---|---|
| `cymatix_genome_size_bytes` | gauge | `kind` ∈ {raw, compressed} |
| `cymatix_genome_wal_size_bytes` | gauge | none — SQLite WAL file size |
| `cymatix_genome_signal_seconds` | histogram | `signal` — per-signal SQLite query latency |
| `cymatix_genome_checkpoint_blocked_total` | counter | none — WAL checkpoint contention |
| `cymatix_chromatin_state_total` | gauge | `state` ∈ {open, euchromatin, heterochromatin} — the document lifecycle tiers |
| `cymatix_harmonic_edges_total` | gauge | `source` ∈ {seeded, co_retrieved, cwola_validated} — co-activation edges by provenance |
| `cymatix_hub_concentration_ratio` | gauge | none — top-1% inbound over mean inbound |
| `cymatix_hub_inbound_degree` | gauge | `stat` ∈ {max, p99, p95, p50, mean} |
| `cymatix_cwola_bucket_total` | counter | `bucket` ∈ {A, B, pending} — A/B partition fill |
| `cymatix_cwola_f_gap_sq` | gauge | none — partition divergence gate (≥ 0.16 passes) |
| `cymatix_ingest_vram_bytes` | gauge | none — CUDA memory per dense ingest batch |

- **The `/stats`-sourced gauges go stale unless something polls `/stats`.**
  Prometheus scrapes every 15 s, but these gauges only refresh when the endpoint
  is hit. A cron or `benchmark_monitor.py` scraping `/stats` keeps them live.

### Compressor and GenAI

| Metric | Type | Labels |
|---|---|---|
| `cymatix_ribosome_call_seconds` | histogram | `backend`, `model`, `call_kind` — compressor call latency |
| `cymatix_ribosome_info` | gauge | `backend`, `model`, `cost_class` — active compressor backend |
| `cymatix_genai_client_token_usage` | histogram | `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.operation.name`, `gen_ai.token.type` ∈ {input, output, cached, reasoning} |
| `cymatix_genai_time_to_first_chunk_seconds` | histogram | as above, minus `token.type` — streaming path only |
| `cymatix_genai_cost_usd` | histogram | as above, minus `token.type` — 0.0 for local or unpriced models |
| `cymatix_genai_finish_reasons_total` | counter | `finish_reason` |

- The `cymatix_genai_*` family follows the OTel GenAI semantic conventions; the
  spec name (`gen_ai.client.token.usage`, …) lives in each instrument's
  description while the metric itself carries the `cymatix_` namespace. In
  Prometheus the dotted attribute names arrive with `.` replaced by `_`, so
  `gen_ai.provider.name` becomes the label `gen_ai_provider_name`.
- **Coverage limit:** the GenAI surface is emitted today from the three
  `/v1/chat/completions` proxy forward paths (streaming, non-streaming, raw
  passthrough) and from the cache counter. **Compressor and embedding backends are
  not yet instrumented** — `llm_span` and `record_response` are the ready-made
  helpers for when they are.

## Traces

- FastAPI auto-instrumentation wraps every route in a span with the request path
  and status code. Sampling is `CYMATIX_OTEL_SAMPLER_RATIO`, default `1.0`.
- **Pipeline stage spans** — `cymatix.pipeline.<stage>` for each of the seven
  stages, plus `cymatix.pipeline.build_context` as the request-level root. The
  span and the matching `cymatix_pipeline_stage_seconds` histogram point come from
  two different mechanisms, both covering all seven stages. This is what lets
  Tempo show a per-request waterfall instead of just the request boundary.
- **The persist span is not a child of the root**, and that is correct: it is
  emitted from the background learn task *after* the response ships, by which time
  the `build_context` root has closed. On the server path it attaches to the
  enclosing HTTP request span, outside the waterfall.
- **GenAI client spans** — OTel semantic-convention spans named `<operation>
  <model>` (e.g. `chat qwen3:8b`). **Caveat: the streaming path opens its span
  after the stream completes**, since the response was already forwarded
  chunk-by-chunk — that span's duration is *not* upstream latency. Use the
  `total_ms` field on the `cymatix.proxy` log line, or the TTFT histogram, for
  timing.

## Logs

- Ordinary `log.warning` / `log.debug` calls go to stdout, and under the OTel SDK
  with a log handler configured they flow to Loki **tagged with trace context**, so
  you can pivot from a slow span straight to its logs.
- Every `/v1/chat/completions` request additionally emits one structured-JSON
  `cymatix.proxy` line: request id, trace id, model and provider, token counts
  split four ways (input / output / cached / reasoning), TTFT and total latency,
  finish reason, cost estimate, and a **SHA256-prefix hash of the prompt — never
  the prompt text**.

```
{logger="cymatix.proxy"} |= "proxy.call"
```

## Privacy and redaction

- **Query text is hashed by default.** Spans carry
  `query=<first-50-chars>[hash:<12-hex>]`.
- `CYMATIX_OTEL_REDACT_QUERY=0` stores raw query strings. **Development only —
  do not enable it in a shared deployment.**
- The proxy log line never contains prompt text, only the hash prefix.
- Telemetry is a **local** stack by default: the collector, Prometheus, Tempo,
  Loki and Grafana all bind on your own machine, and nothing is exported off-box
  unless you point `CYMATIX_OTEL_ENDPOINT` somewhere else.

## Verify it end to end

```bash
export CYMATIX_OTEL_ENABLED=1
python -m uvicorn cymatix_context._asgi:app --port 11437

# Emit spans + retrieval-signal metrics
curl -s -X POST http://localhost:11437/context \
  -H "Content-Type: application/json" \
  -d '{"query":"what port does cymatix use","verbose":true}'

# Refresh the /stats-sourced gauges
curl -s http://localhost:11437/stats > /dev/null

# Confirm Prometheus received them
curl -s 'http://localhost:9090/api/v1/query?query=cymatix_context_latency_seconds_count'
```

If the final call returns `data.result[0].value`, metrics are flowing end to end.

## When it does not work

- **"OTel disabled" in the logs** — set `CYMATIX_OTEL_ENABLED=1` or `[telemetry]
  enabled = true`. Under the tray launcher, check the launcher log for
  "Observability stack up" and make sure no stray `CYMATIX_OTEL_ENABLED=0` is set
  in its shell.
- **"OTel packages not installed"** — `pip install "cymatix-context[otel]"`.
- **Grafana shows no data** — check `http://localhost:9090/targets`; the
  `otel-collector` target should be `UP`.
- **Spans appear but metrics do not** — Prometheus needs
  `--web.enable-remote-write-receiver`. It is included in the provided stack; add
  it if you run Prometheus yourself.
- **A gauge is flatlined** — it is probably `/stats`-sourced. Poll `/stats`.

Symptom-first coverage of the rest of the system: [Troubleshooting](Troubleshooting).

## Go deeper

- [`docs/architecture/OBSERVABILITY.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/architecture/OBSERVABILITY.md) — the full instrumentation surface: every metric row, every span family, the docker-compose alternative, and shutdown
- [`docs/ROSETTA.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/ROSETTA.md) — the metric and panel-title vocabulary table, and why metric names are never renamed
- [`deploy/otel/grafana/dashboards/`](https://github.com/mbachaud/Cymatix-Context/tree/master/deploy/otel/grafana/dashboards) — the seven dashboard JSON files
- [`deploy/otel/README.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/deploy/otel/README.md) — the containerized stack, bit-for-bit compatible with the native one
- [`cymatix_context/telemetry/genai_telemetry.py`](https://github.com/mbachaud/Cymatix-Context/blob/master/cymatix_context/telemetry/genai_telemetry.py) — the `gen_ai.*` instruments, the price table, and the proxy log line
- [`docs/specs/2026-07-01-goal-gates-hallucination-visibility.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/specs/2026-07-01-goal-gates-hallucination-visibility.md) — the gates and alert queries the Know/Miss dashboard was built against
- Next: [Configuration](Configuration) · [Agent Contract](Agent-Contract) · [Benchmarks and Receipts](Benchmarks-and-Receipts) · [Troubleshooting](Troubleshooting)
