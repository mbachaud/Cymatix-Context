# Observability — OTel → Grafana stack

Helix emits OpenTelemetry traces + metrics when `HELIX_OTEL_ENABLED=1`. Everything upstream of the LLM answer boundary becomes visible on one dashboard.

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

**Enable on the helix server:**

```bash
export HELIX_OTEL_ENABLED=1
export HELIX_OTEL_ENDPOINT=localhost:4317   # default
python -m uvicorn helix_context.server:app --port 11437
```

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
2. **OTel `gen_ai.*` standard** — `helix_genai_*` metrics added in the
   May 2026 telemetry rebuild. Follows the upstream OpenTelemetry GenAI
   semantic conventions for token usage, TTFT, finish reasons, and per-
   call cost. Lives in `helix_context/genai_telemetry.py`.

| Metric | Type | Labels | Source |
|---|---|---|---|
| `helix_context_latency_seconds` | histogram | `health`, `budget_tier`, `cold_tier_used` | `/context` endpoint |
| `helix_pipeline_stage_seconds` | histogram | `stage`, `decoder_mode` | per-stage `_stage_timer` + `pipeline_stage_span` |
| `helix_context_cache_outcome_total` | counter | `outcome` ∈ {hit, miss, partial} | `/context` cache classification |
| `helix_context_health_status_total` | counter | `status` ∈ {aligned, sparse, stale, denatured} | `/context` health classifier |
| `helix_context_ellipticity` | histogram | `party` | per-query coverage × density × freshness |
| `helix_tier_contribution` | histogram | `tier` | `query_genes` accumulation |
| `helix_tier_fired_total` | counter | `tier` | `query_genes` accumulation |
| `helix_cwola_bucket_total` | counter | `bucket` ∈ {A, B, pending} | `cwola.log_query` + `sweep_buckets` |
| `helix_cwola_f_gap_sq` | gauge | — | `cwola.sweep_buckets` |
| `helix_harmonic_edges_total` | gauge | `source` ∈ {seeded, co_retrieved, cwola_validated} | `/stats` snapshot |
| `helix_chromatin_state_total` | gauge | `state` ∈ {open, euchromatin, heterochromatin} | `/stats` snapshot |
| `helix_genome_size_genes` | gauge | — | `/stats` snapshot |
| `helix_genome_wal_size_bytes` | gauge | — | `/stats` snapshot |
| `helix_genome_signal_seconds` | histogram | `signal` | per-signal SQLite query timing |
| `helix_genome_checkpoint_blocked_total` | counter | — | WAL checkpoint contention |
| `helix_hub_concentration_ratio` | gauge | — | `/stats` snapshot |
| `helix_hub_inbound_degree` | gauge | `stat` ∈ {max, p99, p95, p50, mean} | `/stats` snapshot |
| `helix_ribosome_call_seconds` | histogram | `backend`, `model`, `call_kind` | every compressor call |
| `helix_ribosome_info` | gauge | `backend`, `model`, `cost_class` | active compressor backend |
| `helix_genai_client_token_usage` | histogram | `gen_ai.token.type`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.operation.name` | every LLM call |
| `helix_genai_time_to_first_chunk_seconds` | histogram | `gen_ai.request.model`, `gen_ai.provider.name` | streaming responses |
| `helix_genai_cost_usd` | histogram | `gen_ai.request.model`, `gen_ai.provider.name` | per-call cost from `PRICE_TABLE` |
| `helix_genai_finish_reasons_total` | counter | `finish_reason` | every LLM call |

`/stats`-sourced gauges are refreshed each time `/stats` is hit.
Prometheus scrapes every 15s — if nothing polls `/stats`, the gauges go
stale. `benchmark_monitor.py` or a cron scraping `/stats` keeps them
fresh.

### Spans

The May 2026 instrumentation adds two span families on top of FastAPI
auto-instrumentation:

1. **Pipeline stage spans** — `helix.pipeline.<stage>` for each of
   the 6 retrieval stages (classify / extract / express / rerank /
   splice / assemble), plus `helix.pipeline.build_context` as the
   request-level root. Implemented via
   `helix_context.telemetry.pipeline_stage_span()`. Lets Tempo show the
   per-request waterfall instead of just the request boundary.
2. **GenAI client spans** — `<operation> <model>` (e.g. `chat
   qwen3:8b`) for every LLM-touching call site: the `/v1/chat/completions`
   proxy paths, the compressor backends (Ollama / Anthropic / LiteLLM),
   and the local embedding/scoring backends (BGE-M3 / SEMA / SPLADE /
   DeBERTa / NLI). Attributes follow the OTel GenAI spec:
   `gen_ai.provider.name`, `gen_ai.operation.name`, `gen_ai.request.model`,
   `gen_ai.usage.input_tokens` / `output_tokens` /
   `cached_input_tokens` / `reasoning.output_tokens`,
   `gen_ai.response.finish_reasons`, `gen_ai.response.time_to_first_chunk`.
   Implemented via `helix_context.genai_telemetry.llm_span()` +
   `record_response()`.

### Logs

Helix's `log.warning` / `log.debug` calls propagate to stdout; when
running under the OTel SDK with a log handler configured, they flow to
Loki tagged with trace context so you can pivot from a slow span to
its logs.

The `helix.proxy` logger emits one **structured-JSON line per
`/v1/chat/completions` request** via
`helix_context.genai_telemetry.emit_proxy_log_line()`. Fields:
`request_id`, `trace_id`, `model`, `provider`, `prompt_hash`,
`tokens.{in,out,cached,reasoning}`, `ttft_ms`, `total_ms`,
`finish_reason`, `cost_usd_estimate`, `helix.cache_outcome`,
`helix.context_block` (`know` | `miss` | `none`). Filter in Grafana
with `{logger="helix.proxy"}`. The `Helix — Operations Overview` and
`Helix — GenAI` dashboards both surface this stream.

## Privacy

Query text is hashed by default — spans carry `query=<first-50-chars>[hash:<12-hex>]`. Set `HELIX_OTEL_REDACT_QUERY=0` to store raw query strings (dev only; do not enable in shared deployments).

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `HELIX_OTEL_ENABLED` | `0` | master switch |
| `HELIX_OTEL_ENDPOINT` | `localhost:4317` | OTLP gRPC endpoint |
| `HELIX_OTEL_INSECURE` | `1` | plain gRPC (local dev) |
| `HELIX_OTEL_SAMPLER_RATIO` | `1.0` | trace sampler 0.0–1.0 |
| `HELIX_OTEL_REDACT_QUERY` | `1` | hash query strings |

## Dashboards

Four dashboards under `deploy/otel/grafana/dashboards/`. All use
engineering vocabulary in panel titles; bio-domain legacy terms are
referenced inline in panel descriptions. See `docs/ROSETTA.md` for the
full bidirectional vocabulary table.

- **Helix — Operations Overview** (`helix-overview.json`) — default
  landing dashboard. Top-line operational KPIs: `/context` request
  rate, latency p50/p95/p99 by health, cache hit / miss / partial
  outcome, per-stage pipeline latency, compressor backend cost class
  + active model, knowledge-store size, WAL health, structured proxy
  log stream. Cross-links to the other three dashboards.
- **Helix — GenAI** (`helix-genai.json`) — the OTel `gen_ai.*` standard
  surface added May 2026. LLM call rate by provider + operation, token
  usage by direction (input / output / cached / reasoning), USD cost
  per minute + per model, cache hit ratio, TTFT p50/p95/p99 + heatmap,
  finish-reasons distribution. Companion to the new
  `helix_context.genai_telemetry` instrumentation.
- **Helix — Internals & Research** (`helix-internals.json`) — preserved
  bio/research panels with engineering titles and inline legacy
  references: tier dynamics (legacy: tier_fired), A/B cluster
  convergence (legacy: CWoLa Label Clock), co-activation graph
  (legacy: harmonic_links + chromatin), hub concentration, compressor
  diagnostics, genome-signal latency. For deep-design work — not
  day-to-day operations.
- **Helix — Retrieval Quality + HITL** (`helix-retrieval-hitl.json`) —
  per-query ellipticity distribution, health-status pie, denatured-rate
  alert stat, budget-tier mix, ellipticity percentiles, HITL pause-event
  signals (alignment, override, escalation) with party-id breakdown.
  Uses technical-term vocabulary (ellipticity, denatured) that is on
  ROSETTA's "STAYS" list.

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
python -m uvicorn helix_context.server:app --port 11437

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

- **"OTel disabled" in logs** — set `HELIX_OTEL_ENABLED=1` in the helix server's env.
- **"OTel packages not installed"** — `pip install "helix-context[otel]"`.
- **Grafana shows no data** — check `http://localhost:9090/targets`; the `otel-collector` target should be `UP`. If it isn't, `docker compose logs otel-collector`.
- **Trace spans appear but metrics don't** — Prometheus remote-write endpoint needs `--web.enable-remote-write-receiver` (included in the provided `docker-compose.yml`). If running Prometheus outside the compose stack, add the flag.

## Shutting down

```bash
cd deploy/otel
docker compose down        # stop containers
docker compose down -v     # + discard volumes
```
