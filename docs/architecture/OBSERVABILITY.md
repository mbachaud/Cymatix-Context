# Observability — OTel → Grafana stack

Helix emits OpenTelemetry traces + metrics when telemetry is enabled — via `HELIX_OTEL_ENABLED=1`, `[telemetry] enabled = true` in helix.toml, or automatically under the tray launcher once the local stack is up. Everything upstream of the LLM answer boundary becomes visible on one dashboard.

## Quick start

**One-time setup** — bring up the observability stack via the native
sidecar (recommended; no Docker required):

```powershell
# Windows
scripts\setup-grafana-telem.ps1
```

```bash
# Linux / macOS
scripts/setup-grafana-telem.sh
```

That wrapper downloads the five pinned binaries into `tools/native-otel/`,
renders runtime configs, and wires dashboard provisioning. It is
idempotent — re-runs skip already-installed binaries and only refresh
configs. Then start the supervisor:

```bash
helix-launcher --tray              # daily-driver flow (tray icon + helix)
helix-launcher --no-autostart      # observability only, no helix backend
```

That launches:

| Service | Port | Purpose |
|---|---|---|
| OTel Collector | 4317 (gRPC), 4318 (HTTP) | receives OTLP from helix |
| Prometheus | 9090 | metrics storage |
| Tempo | 3200 | trace storage |
| Loki | 3100 | log storage |
| Grafana | 3000 | dashboards (`admin` / `admin`) |

**Install OTel client packages:**

```bash
pip install "helix-context[otel]"
```

**Enable on the helix server** — either via env vars:

```bash
export HELIX_OTEL_ENABLED=1
export HELIX_OTEL_ENDPOINT=localhost:4317   # default
python -m uvicorn helix_context._asgi:app --port 11437
```

or via the `[telemetry]` section in `helix.toml`:

```toml
[telemetry]
enabled = true
endpoint = "localhost:4317"
```

Precedence per knob: `HELIX_OTEL_*` env var > `[telemetry]` toml >
code default. Env wins in both directions — an explicit
`HELIX_OTEL_ENABLED=0` silences a toml `enabled = true`.

**Tray launcher:** no configuration needed. When the launcher starts
(or adopts) the native observability stack — and the collector's OTLP
port `:4317` is actually accepting connections — it exports
`HELIX_OTEL_ENABLED=1` into the helix child's environment, so the
default tray boot ships data to Grafana out of the box. Set
`HELIX_OTEL_ENABLED=0` yourself to keep the stack up with a silent
backend. If the stack fails to start, or a service spawns but never
becomes ready (red status), the collector-port probe fails and the
export is skipped — a backend dialing a dead collector would wedge its
gRPC channel. The endpoint itself is not exported; it resolves via the
normal env > toml > default chain, so an explicit `[telemetry]
endpoint` in helix.toml is respected.

Open <http://localhost:3000/d/helix-overview>. Retrieval latency, tier contributions, CWoLa f_gap, chromatin distribution, harmonic-edges-by-source — all live.

**Docker-compose alternative.** If you prefer the containerized stack,
see [`deploy/otel/README.md`](../../deploy/otel/README.md). Both
runtimes are bit-for-bit compatible — same dashboard JSON, same
datasource UIDs, only the receiver runtime differs.

## What's instrumented

### Traces (`/context` span tree)

Auto-instrumentation via `opentelemetry-instrumentation-fastapi` wraps every route in a span. Attributes are the request path + status code. Sampling is controlled by `HELIX_OTEL_SAMPLER_RATIO` (default `1.0`; drop to `0.1` at high QPS).

### Metrics

Two surfaces:

1. **Helix-domain** — `helix_*` metrics that capture the engine's
   internal mechanics (pipeline stages, retrieval tiers, knowledge-store
   health, A/B cluster convergence, co-activation graph). Vocabulary
   has bio-domain origins (chromatin, harmonic_links, CWoLa) and the
   metric names are stable contracts; dashboard panels translate to
   engineering names with inline references — see `docs/ROSETTA.md` for
   the full bidirectional table.
2. **OTel `gen_ai.*` standard** — `helix_context/telemetry/genai_telemetry.py`
   (#209). `helix_genai_*` token usage / TTFT / finish reasons /
   per-call cost following the upstream GenAI semantic conventions,
   plus `helix_context_cache_outcome_total`. Metric names carry the
   `helix_` namespace prefix; the spec name (`gen_ai.client.token.usage`,
   …) lives in each instrument's description. Emitting call sites today:
   the three `/v1/chat/completions` proxy forward paths (streaming,
   non-streaming, raw passthrough) and `CachedDAL.fetch` for the cache
   counter. Compressor/embedding backends are not yet instrumented —
   `llm_span` / `record_response` are the ready-made helpers when they
   are.

   | Metric | Type | Labels | Source |
   |---|---|---|---|
   | `helix_genai_client_token_usage` | histogram | `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.operation.name`, `gen_ai.token.type` ∈ {input, output, cached, reasoning} | proxy forward paths |
   | `helix_genai_time_to_first_chunk_seconds` | histogram | same minus token.type | streaming proxy path |
   | `helix_genai_cost_usd` | histogram | same minus token.type | `estimate_cost_usd` over the module's `PRICE_TABLE`; 0.0 for local/unpriced models |
   | `helix_genai_finish_reasons_total` | counter | `finish_reason` | proxy forward paths |
   | `helix_context_cache_outcome_total` | counter | `outcome` ∈ {hit, miss, partial} | `CachedDAL.fetch` (partial = stale-then-refetched; not on the retrieval hot path yet) |

| Metric | Type | Labels | Source |
|---|---|---|---|
| `helix_context_latency_seconds` | histogram | `health`, `budget_tier`, `cold_tier_used` | `/context` endpoint |
| `helix_pipeline_stage_seconds` | histogram | `stage` | `_stage_timer` in `context_manager.py` records the histogram; `pipeline_stage_span` emits spans only — both cover all 7 stages |
| `helix_context_health_status_total` | counter | `status` ∈ {aligned, sparse, stale, denatured} | `/context` health classifier |
| `helix_context_ellipticity` | histogram | `party` | per-query coverage × density × freshness |
| `helix_tier_contribution` | histogram | `tier` | `query_genes` accumulation |
| `helix_tier_fired_total` | counter | `tier` | `query_genes` accumulation |
| `helix_rrf_fused_score` | histogram | — | RRF fused-score distribution, recorded per query per fused document (`query_genes`, default `fusion_mode = "rrf"` path); attribute-less by design — a per-document label would explode series cardinality |
| `helix_cwola_bucket_total` | counter | `bucket` ∈ {A, B, pending} | `cwola.log_query` + `sweep_buckets` |
| `helix_cwola_f_gap_sq` | gauge | — | `cwola.sweep_buckets` |
| `helix_harmonic_edges_total` | gauge | `source` ∈ {seeded, co_retrieved, cwola_validated} | `/stats` snapshot |
| `helix_chromatin_state_total` | gauge | `state` ∈ {open, euchromatin, heterochromatin} | `/stats` snapshot |
| `helix_genome_size_bytes` | gauge | `kind` ∈ {raw, compressed} | `/stats` snapshot |
| `helix_genome_wal_size_bytes` | gauge | — | `/stats` snapshot |
| `helix_genome_signal_seconds` | histogram | `signal` | per-signal SQLite query timing |
| `helix_genome_checkpoint_blocked_total` | counter | — | WAL checkpoint contention |
| `helix_hub_concentration_ratio` | gauge | — | `/stats` snapshot |
| `helix_hub_inbound_degree` | gauge | `stat` ∈ {max, p99, p95, p50, mean} | `/stats` snapshot |
| `helix_ribosome_call_seconds` | histogram | `backend`, `model`, `call_kind` | every compressor call |
| `helix_ribosome_info` | gauge | `backend`, `model`, `cost_class` | active compressor backend |
| `helix_dense_cosine` | histogram | `arm` ∈ {hot, cold} | dense-recall merge + cold-tier scan (#209) |
| `helix_shard_fanout` | histogram | — | shards consulted per `ShardRouter.query_genes` (#209) |
| `helix_shard_discrimination` | histogram | — | fraction of healthy shards hit per routed query (#209) |
| `helix_know_decision_total` | counter | `outcome` ∈ {know, miss, abstain}, `reason` | `decide_know_or_miss` (#209) |
| `helix_session_tokens_saved_total` | counter | — | session working-set elision savings (#209) |
| `helix_splice_ratio` | histogram | `caller_model_class` | assembled-window compression ratio (#209) |
| `helix_know_confidence` | histogram | — | KnowBlock confidence, know outcomes only (#209 ph2) |
| `helix_abstain_total` | counter | `gate` ∈ {floor_and_ratio, ratio_only}, `fusion_mode` | ABSTAIN gate in `pipeline/tier_logic.py` (#209 ph2) |
| `helix_freshness_demotion_total` | counter | `status` ∈ {stale, missing, unknown, superseded} | freshness revalidation + supersession (#209 ph2) |
| `helix_session_elided_total` | counter | — | elision-stub event count (#209 ph2) |
| `helix_pki_candidates` | histogram | — | docs hit by ≥1 path_key_index pair per query (#209 ph2) |
| `helix_pki_pairs_skipped_total` | counter | — | PKI pairs over the noise cutoff (#209 ph2) |
| `helix_fingerprint_filtered_total` | counter | `cause` ∈ {floor, cap} | /fingerprint floor/cap outcomes (#209 ph2) |
| `helix_ingest_vram_bytes` | gauge | — | CUDA memory per dense ingest batch (#209 ph2) |

The #209 ph2 rows are the hallucination-visibility completion set — gates
and alert queries in `docs/specs/2026-07-01-goal-gates-hallucination-visibility.md`.

`/stats`-sourced gauges are refreshed each time `/stats` is hit.
Prometheus scrapes every 15s — if nothing polls `/stats`, the gauges go
stale. `benchmark_monitor.py` or a cron scraping `/stats` keeps them
fresh.

### Spans

Two span families sit on top of FastAPI auto-instrumentation:

1. **Pipeline stage spans** — `helix.pipeline.<stage>` for each of
   the 7 pipeline stages (classify / extract / express / rerank /
   splice / assemble / persist), plus `helix.pipeline.build_context`
   as the request-level root wrapping `build_context`. Implemented via
   `helix_context.telemetry.pipeline_stage_span()`, which emits the
   span only; the matching `helix_pipeline_stage_seconds` histogram
   point comes from the `_stage_timer` context manager in
   `context_manager.py`. Both mechanisms cover all seven stages. The
   persist span is emitted from the background `learn()` task after
   the response ships, so it is **not** a child of the
   `helix.pipeline.build_context` root (which closed with the
   request) — on the server path it attaches to the enclosing HTTP
   request span, outside the build_context waterfall. Lets Tempo
   show the per-request waterfall instead of just the request
   boundary.
2. **GenAI client spans** — OTel GenAI semantic-convention spans
   (`<operation> <model>`, e.g. `chat qwen3:8b`) via
   `genai_telemetry.llm_span()` (#209). Attributes:
   `gen_ai.provider.name`, `gen_ai.operation.name`,
   `gen_ai.request.*`, and `gen_ai.response.*` /
   `gen_ai.usage.*` populated by `record_response()`. Emitted today
   on the `/v1/chat/completions` proxy forward paths; note the
   streaming path opens its span *after* the stream completes (the
   response was already forwarded chunk-by-chunk), so that span's
   duration is not the upstream latency — use the `total_ms` field
   on the `helix.proxy` log line or the TTFT histogram for timing.
   Compressor/embedding call sites are not yet wrapped.

### Logs

Helix's `log.warning` / `log.debug` calls propagate to stdout; when
running under the OTel SDK with a log handler configured, they flow to
Loki tagged with trace context so you can pivot from a slow span to
its logs.

Every `/v1/chat/completions` request additionally emits one
structured-JSON `helix.proxy` log line (`genai_telemetry.
emit_proxy_log_line()`, #209): request id, trace id, model + provider,
token counts split four ways (in/out/cached/reasoning), TTFT and total
latency, finish reason, cost estimate, and the prompt's SHA256-prefix
hash (never the prompt text). Filter in Loki with
`{logger="helix.proxy"} |= "proxy.call"`.

## Privacy

Query text is hashed by default — spans carry `query=<first-50-chars>[hash:<12-hex>]`. Set `HELIX_OTEL_REDACT_QUERY=0` to store raw query strings (dev only; do not enable in shared deployments).

## Configuration

Every knob is settable two ways — an env var or its `[telemetry]` key
in `helix.toml`. Resolution per knob: **env var > toml > default**
(`helix_context.telemetry.otel.resolve_telemetry_settings`). An env var
set to the empty string counts as unset.

| Env var | `[telemetry]` key | Default | Purpose |
|---|---|---|---|
| `HELIX_OTEL_ENABLED` | `enabled` | off | master switch (env: `1` = on) |
| `HELIX_OTEL_ENDPOINT` | `endpoint` | `localhost:4317` | OTLP gRPC endpoint |
| `HELIX_OTEL_INSECURE` | `insecure` | on | plain gRPC (local dev) |
| `HELIX_OTEL_SAMPLER_RATIO` | `sampler_ratio` | `1.0` | trace sampler 0.0–1.0 |
| `HELIX_OTEL_REDACT_QUERY` | `redact_query` | on | hash query strings (env: `0` = raw) |
| `HELIX_OTEL_LOGS_ENABLED` | `logs_enabled` | on | ship Python logs → collector → Loki |
| `HELIX_OTEL_LOGS_LEVEL` | `logs_level` | `INFO` | min log level forwarded |

The tray launcher exports `HELIX_OTEL_ENABLED=1` into the helix child's
environment after the observability stack is up and the collector's
OTLP port answers — that export is an env-layer value, so it beats the
shipped `[telemetry] enabled = false` default but never overrides an
explicit user `HELIX_OTEL_ENABLED`, and it does not touch
`HELIX_OTEL_ENDPOINT`.

## Dashboards

Six dashboards under `deploy/otel/grafana/dashboards/`. All use
engineering vocabulary in panel titles; bio-domain legacy terms are
referenced inline in panel descriptions. See `docs/ROSETTA.md` for the
full bidirectional vocabulary table.

- **Helix — Operations Overview** (`helix-overview.json`) — default
  landing dashboard. Top-line operational KPIs: `/context` request
  rate, latency p50/p95/p99 by health, cache hit / miss / partial
  outcome, per-stage pipeline latency, compressor backend cost class
  + active model, knowledge-store size, WAL health, structured proxy
  log stream. Cross-links to the other dashboards.
- **Helix — Agent Usage** (`helix-agent-usage.json`) — `/context` call
  mix and latency bucketed by `caller_model_class`
  (`helix_context_calls_by_class_total`).
- **Helix — Pipeline Observatory** (`helix-pipeline-observatory.json`)
  — research panels reconciled to real instrument names in #209
  phase 1: tier activation share, per-signal retrieval latency, CWoLa
  bucket accumulation, p99 `/context` latency, co-activation edges by
  provenance, lifecycle tier distribution, hub concentration +
  inbound-degree stats, knowledge-store size.
- **Helix — Internals & Research** (`helix-internals.json`) — the #209
  phase-1 tuning signals: dense-cosine calibration distribution,
  shard-router fan-out + discrimination, know / miss / abstain
  decision mix, session-elision token savings, splice compression
  ratio by caller class. For deep-design work — not day-to-day
  operations.
- **Helix — GenAI** (`helix-genai.json`) — OTel `gen_ai.*` standard
  surface (#209): token throughput by type, TTFT quantiles per model,
  finish-reason mix, cost/hour + top spend by model, cache-outcome
  pie, and the `helix.proxy` structured log stream. Populated by
  `helix_context/telemetry/genai_telemetry.py`; a contract test
  (`tests/test_genai_telemetry.py::test_genai_dashboard_queries_are_covered`)
  keeps every panel query backed by an emitted metric.
- **Helix — Retrieval Quality + HITL** (`helix-retrieval-hitl.json`) —
  per-query ellipticity distribution, health-status pie, denatured-rate
  alert stat, budget-tier mix, ellipticity percentiles, HITL pause-event
  signals (alignment, override, escalation) with party-id breakdown.
  Uses technical-term vocabulary (ellipticity, denatured) that is on
  ROSETTA's "STAYS" list.
- **Helix — Know/Miss (Hallucination Visibility)** (`helix-know-miss.json`,
  #209 phase 2) — the G1 visibility-gate view: non-know share vs the 10%
  budget, miss rate by reason, know-confidence percentiles vs emit_floor,
  abstain fires by gate, freshness demotions, splice-ratio/abstain
  balancing pair, dense-cosine by arm, shard fan-out + discrimination,
  session-elision savings, latency row with load-annotation discipline.
  Overlaps helix-internals on the phase-1 series by design: internals is
  the tuning view, this is the goal-gates view. Companion spec:
  `docs/specs/2026-07-01-goal-gates-hallucination-visibility.md`.

The native install (`tools/native-otel/`) auto-syncs dashboards from
`deploy/otel/grafana/dashboards/` via
`helix_context.launcher.observability_render._wire_grafana_provisioning`.
After editing a dashboard JSON, re-run the launcher render step to
propagate, or restart the launcher.

## Verifying the stack

```bash
# Bring up the stack
cd deploy/otel && docker compose up -d

# In another shell, start helix with OTel on
export HELIX_OTEL_ENABLED=1
python -m uvicorn helix_context._asgi:app --port 11437

# Hit /context to emit spans + tier metrics
curl -s -X POST http://localhost:11437/context \
  -H "Content-Type: application/json" \
  -d '{"query":"what port does helix use","verbose":true}'

# Hit /stats to refresh chromatin + edge gauges
curl -s http://localhost:11437/stats > /dev/null

# Confirm Prometheus received metrics
curl -s 'http://localhost:9090/api/v1/query?query=helix_context_latency_seconds_count'
```

If the final `curl` returns `data.result[0].value`, metrics are flowing end-to-end.

## Troubleshooting

- **"OTel disabled" in logs** — set `HELIX_OTEL_ENABLED=1` in the helix server's env or `[telemetry] enabled = true` in helix.toml. If the server runs under the tray launcher this is exported automatically once the stack starts — check the launcher log for "Observability stack up" and make sure no stray `HELIX_OTEL_ENABLED=0` is set in the launcher's shell.
- **"OTel packages not installed"** — `pip install "helix-context[otel]"`.
- **Grafana shows no data** — check `http://localhost:9090/targets`; the `otel-collector` target should be `UP`. If it isn't, `docker compose logs otel-collector`.
- **Trace spans appear but metrics don't** — Prometheus remote-write endpoint needs `--web.enable-remote-write-receiver` (included in the provided `docker-compose.yml`). If running Prometheus outside the compose stack, add the flag.

## Shutting down

```bash
cd deploy/otel
docker compose down        # stop containers
docker compose down -v     # + discard volumes
```
