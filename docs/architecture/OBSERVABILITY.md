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
python -m uvicorn helix_context._asgi:app --port 11437
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
2. **OTel `gen_ai.*` standard** — *planned (#209 phase 2)*. The
   `helix_context/genai_telemetry.py` module (`helix_genai_*` token
   usage / TTFT / finish reasons / per-call cost following the upstream
   GenAI semantic conventions, plus `helix_context_cache_outcome_total`)
   is **not on master**; this section returns when it lands.

| Metric | Type | Labels | Source |
|---|---|---|---|
| `helix_context_latency_seconds` | histogram | `health`, `budget_tier`, `cold_tier_used` | `/context` endpoint |
| `helix_pipeline_stage_seconds` | histogram | `stage`, `decoder_mode` | per-stage `_stage_timer` + `pipeline_stage_span` |
| `helix_context_health_status_total` | counter | `status` ∈ {aligned, sparse, stale, denatured} | `/context` health classifier |
| `helix_context_ellipticity` | histogram | `party` | per-query coverage × density × freshness |
| `helix_tier_contribution` | histogram | `tier` | `query_genes` accumulation |
| `helix_tier_fired_total` | counter | `tier` | `query_genes` accumulation |
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
2. **GenAI client spans** — *planned (#209 phase 2)*. OTel GenAI
   semantic-convention spans (`<operation> <model>`) for every
   LLM-touching call site will ship with
   `helix_context/genai_telemetry.py`, which is not on master yet.

### Logs

Helix's `log.warning` / `log.debug` calls propagate to stdout; when
running under the OTel SDK with a log handler configured, they flow to
Loki tagged with trace context so you can pivot from a slow span to
its logs.

A structured-JSON `helix.proxy` log line per `/v1/chat/completions`
request (`emit_proxy_log_line()` — request id, token counts, TTFT,
cost estimate, cache outcome) is *planned (#209 phase 2)* alongside
`helix_context/genai_telemetry.py`. Today the `{logger="helix.proxy"}`
stream carries the proxy's standard log records only.

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

Five dashboards under `deploy/otel/grafana/dashboards/`. All use
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
- **Helix — GenAI** (`helix-genai.json`) — *planned (#209 phase 2)*,
  ships together with `helix_context/genai_telemetry.py`.
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
