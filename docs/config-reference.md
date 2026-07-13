# Configuration Reference

This document is the canonical reference for every section and key in
`helix.toml`. It tracks the runtime configuration consumed by
`helix_context/config.py` (the TOML loader) and the post-merge state of
the 7-stage retrieval-fix initiative landed 2026-05-08 / 2026-05-10.

Every key documented below is verified against `helix.toml` in the
repository root (the canonical reference) and against the dataclass
definitions in `helix_context/config.py`. File-line citations use the
`helix.toml:NN` shorthand.

The configuration file is loaded at process start by
`helix_context.config.load_config()`. The default lookup order is:

1. Path argument supplied to `load_config(path=...)` (programmatic).
2. `HELIX_CONFIG` environment variable (process-level override).
3. `helix.toml` in the current working directory (default).

If no file is found, the loader returns the dataclass defaults baked
into `helix_context/config.py`. Malformed TOML is logged and falls back
to the same defaults so the server never blocks on a config typo. Keys
in a section that the loader does not recognise are surfaced as a
`WARNING` in the server log via `_warn_unknown` (`helix_context/config.py:500`)
but do not abort startup.

The order of sections below mirrors the file's top-to-bottom order so
operators reading along can follow each block in place.

Each section's **Keys** table lives between a
"BEGIN GENERATED: config-tables:&lt;name&gt;" /
"END GENERATED" HTML-comment marker pair and is produced by
[`scripts/gen_config_reference.py`](../scripts/gen_config_reference.py)
directly from the `helix_context/config.py` dataclasses (field name,
type annotation, default value, and — where a comment sits next to the
field in the source — a harvested description). **Do not hand-edit the
rows inside a marked region**: edit the field / comment in
`helix_context/config.py` and re-run
`python scripts/gen_config_reference.py` instead. Everything outside
the markers (this intro, each section's **Purpose**, **Example**,
**Migration notes**, and **Cross-refs** prose) stays hand-authored.
`tests/test_config_reference_sync.py` fails if a generated region ever
drifts from a fresh run of the script (issue #219 slice 4).

---

## `[ribosome]`

**Purpose.** The compressor is the optional enrichment layer that runs an
LLM (Ollama / DeBERTa / LiteLLM / Claude) for ingest-time document packing,
background persistence, and the default-off Step 0 query intent
expansion. The 12-tier `/context` retrieval pipeline is **LLM-free** —
this section configures a separate subsystem against the same knowledge store.
Leaving `enabled = false` (the design pillar: deterministic,
auditable, LLM-free retrieval) means `/context` never touches an LLM
even when other knobs in this section are populated. See
`helix.toml:1-19` for the design preamble.

**Keys.**

<!-- BEGIN GENERATED: config-tables:ribosome -->
| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `false` | Master switch; false = ignore compressor config/runtime |
| `model` | `str` | `"gemma4:e2b"` | default aligned with shipped helix.toml (2026-06-12 default-honesty pass): the shipped toml pins the light pack/replicate fallback model instead of "auto" (compressor auto-detect). Inert while enabled=False. |
| `base_url` | `str` | `"http://localhost:11434"` |  |
| `timeout` | `float` | `120.0` | default aligned with shipped helix.toml (2026-06-12 default-honesty pass) — bulk ingestion headroom |
| `keep_alive` | `str` | `"30m"` | How long Ollama keeps the compressor model loaded |
| `warmup` | `bool` | `false` | Pre-load model on server start. Default aligned with shipped helix.toml (2026-06-12 default-honesty pass) |
| `backend` | `str` | `"none"` | disabled-state placeholder; only "deberta" or "litellm" are honored when enabled. Default aligned with shipped helix.toml (2026-06-12 default-honesty pass) |
| `claude_model` | `str` | `"claude-haiku-4-5-20251001"` | Claude model when backend="claude" |
| `claude_base_url` | `str` | `""` | Proxy URL (e.g. Headroom at http://127.0.0.1:8787); "" = direct |
| `litellm_model` | `str` | `"gemini/gemini-2.5-flash"` | LiteLLM model string when backend="litellm" |
| `rerank_model_path` | `str` | `"training/models/rerank"` |  |
| `splice_model_path` | `str` | `"training/models/splice"` |  |
| `splice_threshold` | `float` | `0.5` |  |
| `nli_model_path` | `str` | `"training/models/nli"` |  |
| `nli_splice_bonus` | `float` | `0.15` | Prob bonus for entailment-linked fragments |
| `nli_splice_penalty` | `float` | `0.15` | Prob penalty for alternation-linked fragments |
| `device` | `str` | `"auto"` | "auto", "cpu", "cuda" |
| `query_expansion_enabled` | `bool` | `false` | Step 0 query-intent expansion fires ONE LLM call per novel query (LRU-cached) upstream of the 12-tone retrieval stack. Flip to false for a strictly LLM-free /context pipeline — the 12 tiers below still run on raw query text + synonym map. See context_manager _expand_query_intent. default aligned with shipped helix.toml (2026-06-12 default-honesty pass): false keeps /context strictly LLM-free (the design default — docs/MISSION.md); flip on for ~2-3pp on ambiguous queries at one ribosome call per novel query. |
| `query_decomposition_enabled` | `bool` | `false` | Step 2 sub-query decomposition: decomposes broad queries into 2-4 point-fact sub-queries via one LLM call. Only fires for multi_hop/default classifier classes. Dark-shipped (default off). |
<!-- END GENERATED -->

`device` is **DEPRECATED**: a legacy device hint kept for one release.
The loader emits a `WARNING` whenever this key is set, urging the
operator to move to `[hardware] device`. When both keys are present,
`[hardware] device` wins (`helix_context/config.py:1267-1279`).

**Example.**

```toml
[ribosome]
enabled = false                       # design pillar — leave false for LLM-free /context
backend = "none"                      # disabled-state placeholder; only "litellm"/"deberta" honored when enabled
model = "gemma4:e2b"
base_url = "http://localhost:11434"
timeout = 120
keep_alive = "30m"
warmup = false
query_expansion_enabled = false
query_decomposition_enabled = false
```

**Migration notes.** `[ribosome] device` is deprecated by the
2026-05-04 hardware-detection PR (#17, merged at `f25211c`). Move to
`[hardware] device`. The loader emits a `WARNING` with both
`"deprecated"` and `"override"` substrings whenever the legacy key is
set, and the warning text is part of the test contract — see
`tests/test_hardware_overrides_ribosome_device`.

**Cross-refs.** `[hardware]` (device picker), `[budget]` (token caps
for the compressor), `helix_context/config.py:24-117` (`RibosomeConfig`
dataclass), `RibosomeConfig.cost_class` for the surfaced cost-class
(`local` / `api+paid` / `disabled`) consumed by `/health`.

---

## `[hardware]`

**Purpose.** Device picker and per-model batch-size policy for the
DeBERTa rerank/splice heads, the SPLADE expander, and the BGE-M3 dense
recall path. The Stage-2 hardware-detection initiative
(`docs/archive/specs/2026-05-04-hardware-detection-design.md`) replaced
ad-hoc `cuda_if_available()` probes with a single auto-picker invoked
from `hardware.init_from_config()` at server startup.

**Keys.**

<!-- BEGIN GENERATED: config-tables:hardware -->
| Key | Type | Default | Description |
|---|---|---|---|
| `device` | `str` | `"auto"` |  |
| `batch_sizes` | `Dict[str, int]` | `{}` |  |
| `low_vram_threshold_gb` | `float` | `4.0` |  |
| `lazy_encoders` | `bool` | `true` | #219 slice 2: when true (default), heavy encoders (ΣĒMA MiniLM, DeBERTa rerank/splice) are armed lazily and load on FIRST USE; when false, restore the pre-slice eager warmup at manager init for operators who want first-query latency paid at boot. SPLADE / BGE-M3 / spaCy were already first-use-lazy and ignore this knob. |
<!-- END GENERATED -->

`batch_sizes`'s generated default of `{}` is the runtime-equivalent of
the shipped TOML string sentinel `"auto"` — both mean "use the
auto-detected VRAM/RAM-aware table in `helix_context/hardware.py`".
When provided as a TOML inline table, every key is cast to `int`
(loader normalisation in `helix_context/config.py`).

**Example.**

```toml
[hardware]
device = "auto"
batch_sizes = "auto"             # equivalent to {}
low_vram_threshold_gb = 4.0

# Per-model override:
# batch_sizes = { rerank = 16, splice = 32, splade = 8, nli = 8 }
```

**Migration notes.** Introduced 2026-05-04 by PR #17
(`f25211c`, "Hardware detection — Stage 1"). Stage 2 of that
initiative (MPS+ROCm+CI hardening) is the next merge in flight per the
session-memory note.

**Cross-refs.** `[ribosome] device` (deprecated; this section
overrides it), `helix_context/config.py:432-449` (`Hardware`
dataclass), `helix_context/hardware.py` (auto-detection table),
`docs/archive/specs/2026-05-04-hardware-detection-design.md` (design
spec).

---

## `[budget]`

**Purpose.** Token budgets, document caps, and decoder-mode knobs for the
`/context` build pipeline. Drives both the dynamic-budget tier
selector (TIGHT / FOCUSED / BROAD / ABSTAIN) and the per-document
character cap inside the foveated splice schedule. Stage-4
calibration adds a per-classifier override path for `foveated_alpha`
(see `[abstain]`).

**Keys.**

<!-- BEGIN GENERATED: config-tables:budget -->
| Key | Type | Default | Description |
|---|---|---|---|
| `ribosome_tokens` | `int` | `3000` |  |
| `expression_tokens` | `int` | `7000` | default aligned with shipped helix.toml (2026-06-12 default-honesty pass) |
| `max_genes_per_turn` | `int` | `12` | default aligned with shipped helix.toml (2026-06-12 default-honesty pass) |
| `max_fingerprints_per_turn` | `int` | `40` |  |
| `splice_aggressiveness` | `float` | `0.3` | default aligned with shipped helix.toml (2026-06-12 default-honesty pass) |
| `decoder_mode` | `str` | `"condensed"` | "full"\|"condensed"\|"minimal"\|"none". Default aligned with shipped helix.toml (2026-06-12 default-honesty pass) |
| `decoder_mode_overrides` | `Dict[str, str]` | `{}` | Issue #207 item 6: operator override for the compressor/ribosome-model capability classification (context_manager.resolve_model_capability_class) -- NOT the same table as decoder_mode above. Maps a model-name substring (case-insensitive) to one of "moe" / "small" / "large"; checked before the hand-calibrated MOE_MODEL_FAMILIES / SMALL_MODEL_PATTERNS tables and the generic ":NNb" parameter-size fallback those tables now have. Empty by default: byte-identical to pre-#207 behavior. |
| `legibility_enabled` | `bool` | `true` | Sprint 1 legibility pack (AI-consumer roadmap): emit a one-line metadata header per document in expressed_context — fired tiers, confidence marker, short gene_id, compression ratio. See helix_context/legibility.py. Default on; flip off to restore the pre-Sprint-1 plain-dividers format (useful for bench A/B). |
| `slate_char_budget` | `int` | `1500` | Stage 5 (2026-05-08): char-budget for the small_moe JSON answer slate. Counts the rendered string the model actually sees, INCLUDING the <helix:slate>...</helix:slate> wrapper, JSON braces, quotes, commas, and per-KV separators. Spec §5 default is 1500. Generic and frontier branches do not consult this knob. |
| `session_delivery_enabled` | `bool` | `true` | Sprint 2 session working-set register: track delivered documents per session, elide repeats with a pointer stub so the consumer doesn't pay full token cost for content it already holds. Enabled 2026-04-19 (saves ~40% tokens on multi-turn conversations); only fires when the caller supplies a session_id. See session_delivery.py. default aligned with shipped helix.toml (2026-06-12 default-honesty pass) |
| `abstain_enabled` | `bool` | `true` | NEW — see docs/specs/2026-05-02-abstain-tier-design.md |
| `foveated_enabled` | `bool` | `false` | Foveated-splice (BROAD tier only). Off by default for the measurement period — see docs/specs/2026-05-03-foveated-splice-design.md §6.3 and docs/plans/2026-05-05-foveated-splice.md. Flip to True only after the phased α-sweep bench (§9) identifies a winning configuration. |
| `foveated_alpha` | `float` | `1.0` | Power-law exponent for c_i = max(c_min, c_max · i^(-α)). α=0.5 = gentle decay, α=1.0 = harmonic-ish, α=2.0 = aggressive top-bias. |
| `foveated_c_min` | `float` | `0.15` | Rank-N floor compression ratio. Pinned at 0.15 by spec §4.1. |
| `foveated_base_chars` | `int` | `1000` | Per-document char-budget multiplier. Each document's target_chars = int(c_i · foveated_base_chars). Default 1000 matches the current uniform behavior at c_i = 1.0. The Step 4 compression loop in context_manager.py uses 1000 today; keeping this configurable lets bench cells (and a future on-by-default ship) tune the top-1 ceiling without touching code. |
| `splice_target_chars` | `int` | `0` | Splice-floor fix (J-space council kill-switch #1, 2026-07-06). Per-document char target for the Step-4 splice loop (non-foveated path). 0 (default) = budget-proportional auto: int(expression_tokens · 4 chars/token · 0.9) // n_candidates, floored at the legacy 1000 — so no document ever gets less room than the old uniform cap, and the expression budget is actually used (12 × 1000 chars ≈ 3000 tokens vs the default 7000). Any positive value pins a fixed target; 1000 restores the exact legacy query-agnostic floor. See context_manager. _compute_splice_target and encoding/headroom_bridge. _query_aware_trim (truncation keeps query-term lines either way). |
<!-- END GENERATED -->

**Note:** despite sometimes appearing alongside these keys in prose,
`mode` is **not** a `[budget]` key. The token-vs-cache mode lives under
`[headroom] mode`; see that section below.

**Example.**

```toml
[budget]
ribosome_tokens = 3000
expression_tokens = 7000
max_genes_per_turn = 12
max_fingerprints_per_turn = 40
splice_aggressiveness = 0.3
decoder_mode = "condensed"
legibility_enabled = true
slate_char_budget = 1500
session_delivery_enabled = true
abstain_enabled = true
foveated_enabled = false
foveated_alpha = 1.0
foveated_c_min = 0.15
foveated_base_chars = 1000
splice_target_chars = 0
```

**Migration notes.** `foveated_*` keys are dark on first ship; flip on
only after the phased α-sweep bench
(`docs/archive/specs/2026-05-03-foveated-splice-design.md` §6.3,
§9). Stage-4 calibration script
(`scripts/calibrate_thresholds.py`) emits per-class `foveated_alpha`
values that override `budget.foveated_alpha` when
`[abstain].mode = "per_classifier"`.

**Cross-refs.** `[abstain]` (Stage-4 per-classifier overrides),
`[cymatics] splice_threshold_scale` (consumes `splice_aggressiveness`),
`docs/archive/specs/2026-05-02-abstain-tier-design.md` (ABSTAIN tier
design), `docs/archive/specs/2026-05-03-foveated-splice-design.md`
(foveated schedule), `helix_context/config.py:120-162`
(`BudgetConfig`), `helix_context/legibility.py`
(`legibility_enabled`), `helix_context/session_delivery.py`
(`session_delivery_enabled`).

---

## `[session]`

**Purpose.** CWoLa label-logger session and party fallback. Without
these defaults, clients that omit `session_id` / `party_id` log NULL
into `cwola_log`, which causes `sweep_buckets` to treat every row as
Bucket A (no re-query detectable without a session) and breaks CWoLa
training. The synthetic fallback generates a deterministic
`session_id` from `(client_ip, time_window)` so bursts of traffic from
the same operator group into coherent sessions for bucket assignment.
Fixed 2026-04-13. See `cwola.py` and
`docs/archive/FUTURE/STATISTICAL_FUSION.md` §C2.

**Keys.**

<!-- BEGIN GENERATED: config-tables:session -->
| Key | Type | Default | Description |
|---|---|---|---|
| `default_party_id` | `str` | `"default"` | Used when the request omits party_id |
| `synthetic_session_window_s` | `int` | `300` | 5 min — close-in-time same-IP requests become one session |
| `synthetic_session_enabled` | `bool` | `true` | Flip to false to preserve prior "NULL = all A" behavior |
<!-- END GENERATED -->

**Example.**

```toml
[session]
default_party_id = "default"
synthetic_session_window_s = 300
synthetic_session_enabled = true
```

**Cross-refs.** `helix_context/config.py:221-236` (`SessionConfig`),
`docs/archive/FUTURE/STATISTICAL_FUSION.md` §C2 (CWoLa framework),
`cwola.py` (`sweep_buckets` consumer).

---

## `[genome]`

**Purpose.** SQLite knowledge store path, compaction cadence, persistence
shape. The 2026-04-16 fresh-rebuild swapped to `genomes/main/` as the
phase-2 sharding root; future shards land at `genomes/reference/`,
`genomes/agent/`, etc. Phase-1 cutover disables replicas pending the
shard router.

**Keys.**

<!-- BEGIN GENERATED: config-tables:genome -->
| Key | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | `"genomes/main/genome.db"` | default aligned with shipped helix.toml (2026-06-12 default-honesty pass) — genomes/ is the phase-2 sharding root; CLAUDE.md documents this as THE default |
| `compact_interval` | `float` | `3600.0` | Seconds between source-change checks |
| `cold_start_threshold` | `int` | `10` | Fix 3: documents needed before history stripping |
| `replicas` | `List[str]` | `[]` | Read-only clone paths |
| `replica_sync_interval` | `int` | `100` | Sync replicas every N inserts |
<!-- END GENERATED -->

`path`'s generated default (`genome.db`, the bare code-default) differs
from the **shipped** `helix.toml` value (`genomes/main/genome.db`,
CLAUDE.md's documented default) — see the Migration notes below.
Override via the `HELIX_GENOME_PATH` env var, honored even when the
`[genome]` section is absent from `helix.toml`.

**Example.**

```toml
[genome]
path = "genomes/main/genome.db"
compact_interval = 3600
cold_start_threshold = 10
replicas = []
replica_sync_interval = 100
```

**Migration notes.** No time-based decay. Documents never expire.
2026-04-16 rebuild migrated from `F:/Projects/helix-context/genome.db`
(old master) and `C:/helix-cache/genome.db` (old replica with
backfill) to the new `genomes/` folder. `HELIX_GENOME_PATH` env var
overrides this path so sharded vs monolithic servers can coexist on
different ports without duplicating `helix.toml`.

**Cross-refs.** `helix_context/config.py:165-171` (`GenomeConfig`),
`docs/archive/FUTURE/GENOME_SHARDING.md` (phase-2 sharding plan).

---

## `[server]`

**Purpose.** HTTP server and chat-upstream wiring for the
OpenAI-compatible proxy. Helix front-ends Ollama (or any
OpenAI-compatible upstream) at `127.0.0.1:11437` by default.

**Keys.**

<!-- BEGIN GENERATED: config-tables:server -->
| Key | Type | Default | Description |
|---|---|---|---|
| `host` | `str` | `"127.0.0.1"` |  |
| `port` | `int` | `11437` |  |
| `upstream` | `str` | `"http://localhost:11434"` |  |
| `bench_enabled` | `bool` | `false` | Dev/configuration mode (v0.7.0): run a SECOND helix instance on a side port bound to a bench genome, so a primary chat stays attached to the main genome while a subagent drives the bench-harness against the bench port. Default OFF — a final deployment leaves this off and gets exactly one server. The launcher reads these at boot; flip in helix.toml or via --bench / HELIX_BENCH_ENABLED=1. |
| `bench_port` | `int` | `11439` |  |
| `bench_genome_path` | `str` | `"genomes/bench/bench.genome.db"` |  |
| `upstream_timeout` | `float` | `180.0` | Timeout for proxied requests to Ollama. Bumped from 120s on 2026-05-02 — observed Proxy 500s on slow gemma4:e4b GPQA queries at ~125s; 180s gives long-tail generation room without letting truly stuck requests hang. Override per-deployment via [server] in helix.toml. |
<!-- END GENERATED -->

`bench_enabled` / `bench_port` / `bench_genome_path` are the v0.7.0
dev/configuration mode: a second helix instance on a side port bound to
a bench genome, so a primary chat session stays attached to the main
genome while a subagent drives the bench harness against the bench
port. Default off; flip via `helix.toml`, `--bench`, or
`HELIX_BENCH_ENABLED=1`.

**Example.**

```toml
[server]
host = "127.0.0.1"
port = 11437
upstream = "http://localhost:11434"
# upstream_timeout = 180
```

**Cross-refs.** `helix_context/config.py` (`ServerConfig`), env
overrides in `load_config()`.

---

## `[telemetry]`

**Purpose.** OpenTelemetry export defaults for the backend — traces,
metrics, and (optionally) forwarded Python logs to the native OTel
sidecar (collector + Prometheus + Tempo + Loki + Grafana; see
`docs/architecture/OBSERVABILITY.md`). Mirrors the `HELIX_OTEL_*` env
vars read by `telemetry/otel.py`. Precedence at setup time is
**env > toml > default** — resolved in `otel.resolve_telemetry_settings()`,
not in `load_config()`, so the default-honesty comparator
(`tests/test_config_default_honesty.py`) never sees env-dependent
values.

`enabled` defaults `false`: a bare backend (no launcher, no stack) must
not dial a dead collector. The tray launcher closes the out-of-the-box
gap the other way — once it starts (or adopts) the observability stack
it exports `HELIX_OTEL_ENABLED=1` into the helix child's env
(`launcher/app.py` `_export_otel_env_for_backend`), which wins over
this default by design.

**Keys.**

<!-- BEGIN GENERATED: config-tables:telemetry -->
| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `false` | Master switch (HELIX_OTEL_ENABLED) |
| `endpoint` | `str` | `"localhost:4317"` | OTLP gRPC (HELIX_OTEL_ENDPOINT) |
| `insecure` | `bool` | `true` | Plain gRPC, dev-local (HELIX_OTEL_INSECURE) |
| `sampler_ratio` | `float` | `1.0` | Trace sampler 0.0-1.0 (HELIX_OTEL_SAMPLER_RATIO) |
| `redact_query` | `bool` | `true` | Hash query strings in spans (HELIX_OTEL_REDACT_QUERY) |
| `logs_enabled` | `bool` | `true` | Ship Python logs to OTel/Loki (HELIX_OTEL_LOGS_ENABLED) |
| `logs_level` | `str` | `"INFO"` | Min level forwarded (HELIX_OTEL_LOGS_LEVEL) |
<!-- END GENERATED -->

**Example.**

```toml
[telemetry]
enabled = false
endpoint = "localhost:4317"
insecure = true
sampler_ratio = 1.0
redact_query = true
logs_enabled = true
logs_level = "INFO"
```

**Cross-refs.** `helix_context/config.py` (`TelemetryConfig`),
`helix_context/telemetry/otel.py` (`resolve_telemetry_settings`, env >
toml > default resolution), `helix_context/launcher/app.py`
(`_export_otel_env_for_backend`), `docs/architecture/OBSERVABILITY.md`.

---

## `[headroom]`

**Purpose.** Optional Headroom proxy lifecycle controls — launcher
only. Headroom (`https://github.com/chopratejas/headroom`) is a
separate process serving a compression proxy + dashboard at
`http://{host}:{port}/dashboard`. When `enabled = true`, the launcher
adopts an existing process if one is already listening on `port`
(adopted process survives launcher Quit) and otherwise spawns a fresh
child. The tray menu surfaces "Open Headroom Dashboard" and
Start/Restart/Stop entries.

Requires `pip install "helix-context[codec]"` (pulls
`headroom-ai[proxy]`).

**Keys.**

<!-- BEGIN GENERATED: config-tables:headroom -->
| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `true` | Master switch; false = do nothing. Default aligned with shipped helix.toml (2026-06-12 default-honesty pass) |
| `autostart` | `bool` | `true` | When enabled: adopt if running, spawn if not |
| `host` | `str` | `"127.0.0.1"` |  |
| `port` | `int` | `8787` |  |
| `mode` | `str` | `"token"` | "token" \| "cache" (passed to --mode) |
| `dashboard_path` | `str` | `"/dashboard"` | Appended to http://{host}:{port} |
| `route_upstream` | `bool` | `false` | When true: launcher points helix's chat upstream at this proxy |
<!-- END GENERATED -->

`route_upstream` controls whether helix's chat upstream is rewritten to
dial this proxy — separate from `enabled` (proxy lifecycle: start /
adopt the process). You may want the proxy + dashboard running without
the chat redirect, or vice versa. Off by default so a fresh install
never silently rewrites the upstream to a proxy that isn't actually
running. The `HELIX_HEADROOM_ROUTE_UPSTREAM_AUTO` env var (truthy →
force on, falsy → force off, unset → defer to config) is a per-launch
override.

**Example.**

```toml
[headroom]
enabled = true
autostart = true
host = "127.0.0.1"
port = 8787
mode = "token"
dashboard_path = "/dashboard"
route_upstream = false
```

**Cross-refs.** `helix_context/config.py` (`HeadroomConfig`).

---

## `[ingestion]`

**Purpose.** Selects the encoder backend that turns raw content into
documents during `POST /ingest`. The phased rollout (Phase 2 SPLADE,
Phase 3 cross-encoder rerank, Phase 4 ColBERT, Phase 5 entity-graph)
is governed by per-feature flags in this section.

**Keys.**

<!-- BEGIN GENERATED: config-tables:ingestion -->
| Key | Type | Default | Description |
|---|---|---|---|
| `backend` | `str` | `"cpu"` | "ollama" \| "cpu" \| "hybrid" |
| `splade_enabled` | `bool` | `true` | Phase 2: SPLADE sparse expansion at index time |
| `rerank_model` | `str` | `"cross-encoder/ms-marco-MiniLM-L-6-v2"` | Phase 3: pretrained cross-encoder HF model ID — inert while rerank_enabled=False. Default aligned with shipped helix.toml (2026-06-12 default-honesty pass) |
| `rerank_enabled` | `bool` | `false` | Phase 3: enable cross-encoder reranking |
| `colbert_enabled` | `bool` | `false` | Phase 4: ColBERT late interaction (optional) |
| `entity_graph` | `bool` | `true` | Phase 5: entity-based co-activation links (ingest-time edges). Default aligned with shipped helix.toml (2026-06-12 default-honesty pass) |
| `dense_embed_on_ingest` | `bool` | `true` | Tier-0 PR-1 (2026-05-16): compute BGE-M3 dense vectors (genes.embedding_dense_v2) inline at ingest. Default true so a genome built by `helix ingest` / `/ingest` / context_manager.ingest is dense-populated without a separate backfill pass. Latency-sensitive callers can set false to defer encoding to scripts/backfill_bgem3_v2.py. This is purely the WRITE path — retrieval still gates on [retrieval] dense_embedding_enabled (default true). |
| `sema_embed_on_ingest` | `bool` | `true` | Issue #227: compute the 20D ΣĒMA embedding at ingest (feeds TCM / cymatics via gene.embedding). Default True preserves current behaviour. Set False to skip the ingest-time SEMA encode entirely — the MiniLM model is then never materialized (TCM falls back to its text-derived path, cymatics off), which is what a lexical-only config or a multi-worker bench wants. Without this, ingest always materialized the lazy SEMA codec (#220), loading MiniLM per worker and OOMing parallel bench runs even with dense/cymatics disabled. |
| `splade_auto_enable_below_genes` | `int` | `0` | Issue #164 (size-aware SPLADE auto-toggle): SPLADE expansion's value follows a corpus-regime curve -- the v2 EnterpriseRAG-Onyx storage breakdown showed SPLADE at 21.1% of disk on the 850K-gene fixture while contributing 0 pp recall@10 vs SPLADE-off at the same scale (n=5 + 100q in-flight; see issue body). Below ~50K it's likely useful; above ~200K it's likely net-negative (disk + p95 + SQL fan-out). When BOTH thresholds are 0 (default) the toggle is disabled and the static ``splade_enabled`` value governs every upsert -- byte-identical to pre-#164 behaviour. Setting either threshold to a positive value opts the genome in: - splade_auto_enable_below_genes > 0: force SPLADE ON when the current gene_count is strictly below the threshold, even if ``splade_enabled = false``. The "sparse-corpus rescue" arm. - splade_auto_disable_above_genes > 0: force SPLADE OFF when the current gene_count is strictly above the threshold, even if ``splade_enabled = true``. The "enterprise-scale storage cliff" arm. Both default 0 (opt-in) because the scale curve in #164 is not yet empirically resolved across the 10K-100K transition band; conservative defaults will land in a follow-up once the per-fixture sweep is wired to a head-to-head SPLADE-on/off ablation across that range. |
| `splade_auto_disable_above_genes` | `int` | `0` |  |
| `splade_model` | `str` | `"naver/splade-cocondenser-ensembledistil"` | Issue #207 (de-hardcoding wave 2, items 1-3). Defaults reproduce the prior hardwired literals byte-for-byte; air-gap / mirror deployments repoint the model IDs at a local mirror, and recall-ceiling tuning raises the caps. item 1 — model IDs (were hardwired in splade_backend/sema). Dense (BAAI/bge-m3) is deferred to a fast-follow: its codec is a process-wide shared singleton (get_shared_codec) + the passage cap must stay byte-identical between inline ingest and scripts/backfill_bgem3_v2.py. |
| `sema_model` | `str` | `"all-MiniLM-L6-v2"` |  |
| `splade_content_cap` | `int` | `1000` | chars SPLADE-encoded at ingest (storage/indexes.sync_splade_index) |
| `dense_passage_char_cap` | `int` | `2000` | chars BGE-M3-encoded per passage |
| `citation_path_anchors` | `List[str]` | `["sources", "Projects"]` | item 2 — citation shortener anchors (were literal 'sources'/'Projects' in context_manager): last occurrence of each, in list order, is the strip point. Add your ingest roots here for correct <GENE src=...> shortening. |
| `deny_list_extra` | `List[str]` | `[]` | Issue #207 item 5: deny-list extensibility. The built-in structural deny list is the documented constant knowledge_store.DENY_PATTERNS (+ LOCALE_DENY_PATTERN for non-English locale/ demotion, gated by locale_demotion_enabled below). deny_list_extra entries are regex fragments ORed onto the built-in list at KnowledgeStore construction (same re.IGNORECASE, directory-boundary-anchored semantics — e.g. r"[\\/]internal_only[\\/]"). Empty by default: byte-identical to the prior hardwired behavior. |
| `locale_demotion_enabled` | `bool` | `true` | Non-English software locale/ directories (locale/de/, locale/ja/, ...) are demoted to HETEROCHROMATIN at ingest by default (high-volume, low-signal for typical English-primary retrieval workloads). Flip to False for deployments that DO want non-English locale content ingested at full tier. Default True reproduces the prior always-on behavior. |
<!-- END GENERATED -->

`splade_auto_enable_below_genes` / `splade_auto_disable_above_genes`
are the issue #164 size-aware SPLADE auto-toggle: both default `0`
(disabled — the static `splade_enabled` value governs every upsert,
byte-identical to pre-#164 behavior). `splade_model` / `sema_model` /
`splade_content_cap` / `dense_passage_char_cap` are the issue #207
de-hardcoding wave — defaults reproduce the prior hardwired literals
byte-for-byte; air-gap / mirror deployments repoint the model IDs at a
local mirror. `dense_passage_char_cap` in particular MUST stay
identical across the three BGE-M3 encode paths that read it — inline
ingest (`context_manager.ingest`), query-side store encode
(`KnowledgeStore._encode_dense_v2_blob`), and offline backfill
(`scripts/backfill_bgem3_v2.py`) — or the `embedding_dense_v2` vectors
written by each path silently diverge for long passages.

**Example.**

```toml
[ingestion]
backend = "cpu"
splade_enabled = true
rerank_model = "cross-encoder/ms-marco-MiniLM-L-6-v2"
rerank_enabled = false
colbert_enabled = false
entity_graph = true
sema_embed_on_ingest = true
dense_embed_on_ingest = true
splade_model = "naver/splade-cocondenser-ensembledistil"
sema_model = "all-MiniLM-L6-v2"
splade_content_cap = 1000
dense_passage_char_cap = 2000
```

**Cross-refs.** `helix_context/config.py`
(`IngestionConfig`).

---

## `[context]`

**Purpose.** Retrieval-time behavior for `context_manager`. The
cold-tier knobs were added 2026-04-10 (C.2 of the B->C migration).
Cold-tier is the opt-in retrieval path that consults heterochromatin
documents via SEMA cosine similarity, returning their preserved content
(only possible after C.1 made `compress_to_heterochromatin`
non-destructive).

**Keys.**

<!-- BEGIN GENERATED: config-tables:context -->
| Key | Type | Default | Description |
|---|---|---|---|
| `cold_tier_enabled` | `bool` | `false` | Master opt-in for cold-tier fallthrough |
| `cold_tier_min_hot_genes` | `int` | `0` | Fall through when hot returns <= this many documents (0 = only on empty) |
| `cold_tier_k` | `int` | `3` | Max cold-tier documents to retrieve per query |
| `cold_tier_min_cosine` | `float` | `0.15` | SEMA cosine floor (sparse 20-dim — see Genome.query_cold_tier) |
| `fingerprint_mode_profile` | `str` | `"balanced"` | "fast" \| "balanced" \| "quality" |
<!-- END GENERATED -->

**Example.**

```toml
[context]
cold_tier_enabled = false
cold_tier_min_hot_genes = 0
cold_tier_k = 3
cold_tier_min_cosine = 0.15
fingerprint_mode_profile = "balanced"
```

**Cross-refs.** `helix_context/config.py`
(`ContextConfig`), `Genome.query_cold_tier` (consumer).

---

## `[cymatics]`

**Purpose.** Frequency-domain re-rank + splice. CPU math that
replaces LLM splice calls with spectral analysis over per-document
fingerprint vectors. Blends as a bonus (max 0.5), not a primary
ranker.

**Keys.**

<!-- BEGIN GENERATED: config-tables:cymatics -->
| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `true` | Master switch |
| `n_bins` | `int` | `256` | Spectrum resolution (<2KB per spectrum) |
| `peak_width` | `float` | `3.0` | Gaussian peak width (overridden by Q-factor) |
| `splice_threshold_scale` | `float` | `0.7` | Maps splice_aggressiveness to resonance threshold |
| `use_embeddings` | `bool` | `false` | Use Gene.embedding when available |
| `harmonic_links` | `bool` | `true` | Compute weighted co-activation edges |
| `distance_metric` | `str` | `"cosine"` | "cosine" (weighted dot) \| "w1" (Werman 1986 circular Wasserstein-1) |
<!-- END GENERATED -->

**Example.**

```toml
[cymatics]
enabled = true
n_bins = 256
peak_width = 3.0
use_embeddings = false
harmonic_links = true
distance_metric = "cosine"
```

**Cross-refs.** `helix_context/config.py`
(`CymaticsConfig`).

---

## `[classifier]`

**Purpose.** Upstream rule-based query classifier and injection
router. Contributes a decoder-mode hint and an assembly-stage
document-count cap to `build_context()`. Drives the per-classifier
abstain-floor lookup when `[abstain].mode = "per_classifier"`.

**Keys.**

<!-- BEGIN GENERATED: config-tables:classifier -->
| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `true` |  |
<!-- END GENERATED -->

**Example.**

```toml
[classifier]
enabled = true
```

**Cross-refs.** `helix_context/config.py`
(`ClassifierConfig`),
`docs/archive/specs/2026-04-29-query-classifier-injection-router-design.md`,
`[abstain]` (per-classifier floors keyed by classifier output).

---

## `[retrieval]`

**Purpose.** The big one. Configures every recall and rerank tier in
`Genome.query_genes()` and the new Stage-2 / Stage-3 / Stage-4 paths
(dense recall, RRF fusion, margin-over-random ANN threshold). Since
issue #202 the per-tier weights bind in **both** fusion modes: under
`"additive"` (the legacy pre-Stage-3 path) each weight is the tier's
coefficient/cap itself (defaults equal the old inline literals, so
untouched configs keep byte-identical rankings); under `"rrf"` (the
default since 2026-07-06) each weight is a post-multiplier applied to
`1 / (k + rank)`. The defaults preserve the implicit weights baked into
the legacy additive accumulator so `fusion_mode = "rrf"` is a clean
rank-fusion swap, not a re-tuning.

The full Stage-3 tier inventory (which tiers participate in RRF and
which stay additive) lives in
`docs/specs/2026-05-08-stage-3-rrf-fusion.md` §3.

**Keys.**

<!-- BEGIN GENERATED: config-tables:retrieval -->
| Key | Type | Default | Description |
|---|---|---|---|
| `sr_enabled` | `bool` | `false` | Successor Representation (Stachenfeld 2017) - lazy on-demand SR rows via truncated power series over co-activation graph. 2026-06-12 default-honesty pass: stays FALSE on both sides. helix.toml had flipped this true (2026-04-22 Stage-1 bench), but the evidence roadmap measured SR at zero effect on retrieval outcomes, so the shipped toml was aligned back to the code default (the inverse of the usual toml-wins rule: measured-zero features default off). |
| `sr_gamma` | `float` | `0.85` | Discount factor (5-10 hop horizon at 0.9) |
| `sr_k_steps` | `int` | `4` | Power-series truncation depth |
| `sr_weight` | `float` | `1.5` | Per-document contribution multiplier |
| `sr_cap` | `float` | `3.0` | Max per-document SR boost (matches harmonic cap) |
| `ray_trace_theta` | `bool` | `false` | Dark ship |
| `theta_weight` | `float` | `1.0` | Softmax temperature on v·document dot product |
| `seeded_edges_enabled` | `bool` | `false` | Dark ship — flip to start evidence accumulation |
| `seeded_edge_weight` | `float` | `1.0` | Base weight written on seed insertion |
| `filename_anchor_enabled` | `bool` | `true` | Stage-1 bench flip 2026-04-22: +12pp Dewey axis-2. Default aligned with shipped helix.toml (2026-06-12 default-honesty pass) |
| `filename_anchor_weight` | `float` | `4.0` | Per-match boost (higher than Tier 1's 3.0) |
| `bm25_shortlist_enabled` | `bool` | `true` | Keep on (2026-04-22 sprint): +1/8 ans_full, clean attribution. Default aligned with shipped helix.toml (2026-06-12 default-honesty pass) |
| `bm25_shortlist_size` | `int` | `50` | BM25 top-N kept in the final ranking |
| `bm25_prefilter_enabled` | `bool` | `false` |  |
| `bm25_prefilter_size` | `int` | `200` | BM25 top-N fed into tier scoring |
| `fts5_candidate_depth` | `int` | `0` | A4 / #205 candidate-pool-depth knob (2026-07-06). Overrides the Tier-3 FTS5 content-search fetch depth (legacy default: max_genes*4 = 48 at the shipped max_genes=12). 0 = auto (legacy behavior). Widens ONLY the raw candidate pool fed into tier scoring — the returned pool (max_genes*2) and the delivery cap (max_genes) are unchanged, so a deeper pool cannot trivially inflate gold_delivered. Lets the SIKE bedsweep isolate FTS pool starvation (A4) from rank squeeze (B2) on the xl bed. |
| `entity_graph_retrieval_enabled` | `bool` | `false` | Tier 5b: entity graph co-occurrence boost (Step 3C, 2026-05-08). Documents sharing entity nodes with query terms get a score boost proportional to entity overlap. Dark ship — flip to true for A/B. |
| `dense_embedding_enabled` | `bool` | `true` | Step 4 — BGE-M3 dense vectors + ANN threshold-based dynamic document counts (2026-05-08). Tier-0 PR-3 (2026-05-16) flipped this default to true: PR-1 computes embedding_dense_v2 at ingest and PR-3 decoupled dense recall from fusion_mode, so dense recall is now a shipped retrieval signal in both additive and RRF mode. |
| `dense_embedding_dim` | `int` | `1024` | Stage 2 (2026-05-08): default dim raised from 256 -> 1024. Full BGE-M3 Matryoshka. dim=256 collapsed random-pair cosine to ~0.6, sabotaging threshold semantics. |
| `dense_model` | `str` | `"BAAI/bge-m3"` | #207 dense fast-follow (item 1, deferred from wave 2): the BGE-M3 model ID was hardwired across three encode paths — inline ingest (context_manager._get_dense_codec), query-side store encode (KnowledgeStore._encode_dense_v2_blob via get_shared_codec), and offline backfill (scripts/backfill_bgem3_v2.py). Default reproduces the prior literal byte-for-byte; air-gap / mirror deployments repoint at a local mirror. get_shared_codec's cache key is (model_name, dim, device), so a repointed model_name still gets its own cached singleton. |
| `ann_similarity_threshold` | `float` | `0.58` | Stage 4 / Issue #139 (2026-05-18): recalibrated 0.35 -> 0.58 for dim=1024. 0.35 was a dim-256 value. Measured over the dim-1024 BGE-M3 v2 vectors in the bench fixtures (17.5k docs, 200k random unrelated doc pairs): unrelated-pair cosine mean ~0.50, std ~0.066, p90 ~0.58. So 0.35 sat below the p1 noise floor (~0.36) and never cut; 0.58 sits just above the p90 of unrelated pairs. |
| `ann_threshold_min_genes` | `int` | `1` |  |
| `ann_threshold_max_genes` | `int` | `12` |  |
| `ann_threshold_mode` | `str` | `"absolute"` | Stage 4 (2026-05-08): margin-over-random ANN calibration. Spec docs/specs/2026-05-08-stage-4-threshold-calibration.md §3-§6. ``"absolute"`` (default) keeps Stage-3 behavior byte-for-byte; ``"margin_over_random"`` reads the persisted threshold from the ``genome_calibration`` table (populated by ``scripts/calibrate_thresholds.py``). |
| `ann_threshold_sigma_multiplier` | `float` | `3.0` |  |
| `dense_pool_floor_genes` | `int` | `8` | Issue #214 (2026-06-12): dense pool floor. The margin-over-random calibration measures mu + sigma_mult*sigma over RANDOM gene pairs; embedding anisotropy can push that bound ABOVE every real query-doc cosine. Measured twice, independently: (a) cc-exchange embedding-upgrade L1b — calibrated threshold 0.779 vs corpus max query-doc cosine ~0.713 (golds 0.46-0.68), so 0/5000 pool docs cleared the gate by dense; (b) a 480-question ERB run with 70.0% never-surfaced golds (gold absent from top-10), matching an independent 67.2% measurement. A threshold that admits ZERO dense candidates is mis-calibration by definition, and pool membership is strictly upstream of fusion ranking — no re-weighting can recover a candidate the gate already dropped. When fewer than this many dense-scored candidates survive the ANN threshold cut (but the dense leg HAD scored candidates), the top-N dense hits by cosine are admitted into the pool anyway; they then compete normally in fusion scoring. 0 disables (legacy gate-only). See knowledge_store.apply_ann_gate. |
| `dense_pool_size` | `int` | `500` | Stage 2 (2026-05-08): dense recall pool size. Decoupled from ann_threshold_max_genes (the final cut). 500 hits ~3% of an 18.9k corpus per spec §4. |
| `fusion_mode` | `str` | `"rrf"` | "rrf" \| "additive" (legacy) |
| `rrf_k` | `int` | `60` | Cormack 2009 default |
| `rerank_combinator` | `str` | `"additive"` | additive \| fused_tier \| eps_band \| off |
| `rerank_band_delta` | `float` | `0.05` | eps_band relative tie-band width δ (ratio of the leader's fused score). |
| `rerank_tier_weight` | `float` | `1.0` | fused_tier uniform per-class rank post-multiplier (single weight — a per-class weight would re-introduce hand-picked exchange rates). |
| `rerank_combinator_by_class` | `Dict[str, str]` | `{}` | Issue #255 (classifier-gated combinator, 2026-07-12): per-query-class rerank combinator override map {classifier_class: combinator_name}. The stage-0 rule-based query classifier assigns each query a class (arithmetic / factual / procedural / multi_hop / default); a populated entry makes THAT class use its mapped combinator instead of the global rerank_combinator above. Empty (default) => every query uses the global combinator, so this ships BYTE-IDENTICAL. The design is default-inert because the winning combinator is CORPUS-DEPENDENT: the desk test found rerank additives are load-bearing on literal beds while eps_band/off win the semantic 10k ERB bed (docs/research/2026-07-10-rerank-combinator- desktest.md + the 2026-07-11 semantic-arm re-run) — so no global flip, per-class selection instead. Keys are validated against the classifier class set and values against VALID_COMBINATORS at load (RetrievalConfig.__post_init__); an unknown key or value is a hard config error (fail loud at load, not silently at query time). Classifier disabled => map ignored, global combinator used. |
| `blend_mode` | `str` | `"scale_relative"` | legacy \| scale_relative (default) \| off |
| `fts5_weight` | `float` | `3.0` | cap-only in additive: cap = 2.0 × this (6.0) |
| `splade_weight` | `float` | `3.5` | leading coeff == tier cap |
| `tag_exact_weight` | `float` | `3.0` | current weight × match_count |
| `tag_prefix_weight` | `float` | `1.5` | current weight × match_count |
| `sema_boost_weight` | `float` | `2.0` | Issue #202: warm ΣĒMA boost (Tier 4 Mode A) weight — NEW knob; the additive literal was sim·2.0·scale and the tier previously had no weight knob at all (post-fusion additive under RRF, never fused). Default == old literal, so untouched configs are bit-identical. |
| `sema_cold_weight` | `float` | `3.0` | current sim·3.0 multiplier |
| `lex_anchor_weight` | `float` | `1.5` | idf coeff; cap = 2.0 × this (3.0) |
| `harmonic_weight` | `float` | `1.0` | per-link weight; cap = 3.0 × this (3.0) |
| `entity_graph_weight` | `float` | `0.5` | per-row bonus; cap = 4.0 × this (2.0) |
| `dense_weight` | `float` | `1.0` | Stage 2 dense recall, RRF participant |
| `dense_additive_weight` | `float` | `4.0` | Tier-0 PR-3 (2026-05-16): additive-mode dense merge weight. Under fusion_mode == "additive" a dense hit's cosine is scaled by this before entering the gene_scores accumulator. BM25-comparable (tag_exact_weight is 3.0). Unused under RRF. Issue #203 (closed 2026-07-03): the real-query sweep (``benchmarks/sweep_dense_additive_weight.py``, n=100 ERB queries per bed) found recall@10 monotone INCREASING in this weight — erb10k 0.58 (w=0) → 0.64 (w=6), erb50k 0.47 → 0.56, medium 0.23 → 0.40 — with zero gold evictions at any weight. The #138 H10q gold-eviction fear did not reproduce on enterprise-class queries. 4.0 stands; the raise-to-6.0 decision is deferred to the #205 per-class retrieval profiles (w=6 may become the semantic-class value rather than a global default). ``0.0`` flips dense additively-off without disabling the dense write path or RRF participation. |
| `dense_additive_min_cosine` | `float` | `0.15` | Tier-0 review fix (2026-05-16): noise floor for the additive-mode dense merge. A dense hit whose cosine is below this does not contribute to gene_scores (it is still kept as a candidate with negligible weight). Consistent with the cold tier's 0.15 min_cosine; deliberately gentle so it removes only noise-grade hits. Unused under RRF. |
| `semantic_dense_additive_weight` | `float` | `16.0` | Semantic-wiring arm (2026-06-02; PRD docs/prds/2026-06-02-semantic-wiring-arm.md). When query_type=="semantic" AND env HELIX_SEMANTIC_ARM=1, the per-shard dense term is scaled by semantic_dense_additive_weight (instead of dense_additive_weight) AND routing broadens to all healthy shards. The two fire together or not at all. Default-off (env unset) => byte-identical baseline; lexical/tag/SPLADE tiers are never touched (additive KEEP-BOTH). |
| `semantic_broaden_routing` | `bool` | `true` |  |
| `pki_weight` | `float` | `1.0` | PKI tier, RRF participant |
| `shard_fetch_multiplier` | `float` | `2.0` | ── Sharded-retrieval fetch depth + co-activation budget (#222/#223) ── These bind ONLY on the sharded read path (ShardRouter); blob mode never constructs a router, so they are inert there. Threaded to the router via open_read_source -> ShardedGenomeAdapter -> ShardRouter (mirrors semantic_broaden_routing). Defaults reproduce the dark-shipped env-knob behaviour byte-for-byte and keep the sharded merge identical to today. #222 per-shard fetch depth: the router fetches max_genes * multiplier candidates per shard before the cross-shard merge. multiplier=2.0 is the legacy flat 2× cut. scale_with_shards amplifies the multiplier by sqrt(n_shards) (clamped to 10×max_genes) so populous many-shard corpora oversample each shard deeply enough that a mid-shard gold survives to the merge. HELIX_SHARD_FETCH_FACTOR (int) overrides. |
| `shard_fetch_scale_with_shards` | `bool` | `false` |  |
| `coact_reserved_slots` | `int` | `0` | #223 co-activation reserved budget: reserve up to N of the final 2×max_genes output slots for newly graph-promoted (co-activated) docs so a link-discounted gold isn't truncated by lexical incumbents. 0 = legacy (no reservation). HELIX_SHARD_COACT_RESERVE (int) overrides. coact_link_boost is the discount a linked doc enters at (× its source doc's corrected score); 0.5 == the shipped constant. |
| `coact_link_boost` | `float` | `0.5` |  |
<!-- END GENERATED -->

`rerank_combinator` / `rerank_band_delta` / `rerank_tier_weight` are
the issue #255 post-fusion rerank combinator (PR-2, 2026-07-10): under
`fusion_mode == "rrf"` the four rerank classes (authority / sema_boost
/ party_attr / access_rate) combine with the fused RRF score via this
operator. Default `"additive"` is byte-identical to the shipped
fused+rerank_additive block so this knob ships inert; the alternatives
(`"fused_tier"`, `"eps_band"`, `"off"`) are bench-gated on the
50-needle beds — see
`docs/research/2026-07-09-scoring-combinator-exploration.md`.
`semantic_dense_additive_weight` / `semantic_broaden_routing` are the
2026-06-02 semantic-wiring arm (env-gated, `HELIX_SEMANTIC_ARM=1`) —
see `docs/prds/2026-06-02-semantic-wiring-arm.md`.
`dense_pool_floor_genes` (issue #214) is a graceful-degradation floor:
when the ANN threshold gate admits zero dense candidates due to
embedding-anisotropy mis-calibration, the top-N dense hits by cosine
are admitted into the pool anyway rather than starving the dense leg
entirely.

**Stage-3 RRF tier inventory (additive vs RRF participants).** Per
`docs/specs/2026-05-08-stage-3-rrf-fusion.md` §3, recall/discovery
tiers participate in RRF; re-rank/tiebreaker/policy boosts stay
additive on top of the fused score so the operator's "is this document
authoritative?" semantics survive unchanged. The split:

| Tier | RRF participant? | Weight knob |
|---|---|---|
| `pki` (path-key compound) | yes | `pki_weight` |
| `filename_anchor` | yes | `filename_anchor_weight` (reused) |
| `tag_exact` | yes | `tag_exact_weight` |
| `tag_prefix` | yes | `tag_prefix_weight` |
| `fts5` | yes | `fts5_weight` |
| `splade` | yes | `splade_weight` |
| `sema_cold` | yes (only when fires) | `sema_cold_weight` |
| `lex_anchor` (IDF) | yes | `lex_anchor_weight` |
| `harmonic` | yes | `harmonic_weight` (additive cap = 3.0 × weight) |
| `sr` (Successor Repr.) | yes | `sr_weight` (reused) |
| `entity_graph` | yes | `entity_graph_weight` |
| `dense` (Stage 2) | yes | `dense_weight` |
| `sema_boost` (gate-only re-rank) | **no** — tiebreaker, applied AFTER RRF | `sema_boost_weight` (#202; additive/re-rank coefficient) |
| `authority_*` (source/domain/recency) | **no** — flat boost on existing pool | n/a |
| `party_attr` | **no** — flat additive AFTER RRF | n/a |
| `access_rate` | **no** — explicit tiebreaker AFTER RRF | n/a |

**Example.**

```toml
[retrieval]
# Tier 5.5 Successor Representation — measured zero effect; stays off
sr_enabled = false
sr_gamma = 0.85
sr_k_steps = 4
sr_weight = 1.5
sr_cap = 3.0
# Theta alternation (dark)
ray_trace_theta = false
theta_weight = 1.0
# Seeded edges (dark)
seeded_edges_enabled = false
seeded_edge_weight = 1.0
# Tier 0.5 filename anchor
filename_anchor_enabled = true
filename_anchor_weight = 4.0
# BM25 shortlist
bm25_shortlist_enabled = true
bm25_shortlist_size = 50
bm25_prefilter_enabled = false
bm25_prefilter_size = 200
# #205 candidate-pool-depth (0 = auto = max_genes*4)
fts5_candidate_depth = 0
# Tier 5b entity graph (dark)
entity_graph_retrieval_enabled = false
# Stage 2 dense recall — shipped on since Tier-0 PR-3 (2026-05-16)
dense_embedding_enabled = true
dense_embedding_dim = 1024
dense_model = "BAAI/bge-m3"           # #207 dense fast-follow (2026-07-10)
ann_similarity_threshold = 0.58       # recalibrated for dim=1024 (issue #139)
ann_threshold_min_genes = 1
ann_threshold_max_genes = 12
# Stage 4 threshold mode
ann_threshold_mode = "absolute"
ann_threshold_sigma_multiplier = 3.0
dense_pool_floor_genes = 8             # issue #214 graceful-degradation floor
dense_pool_size = 500
# Stage 3 RRF fusion (default "rrf" since 2026-07-06; "additive" = legacy)
fusion_mode = "rrf"
rrf_k = 60
# issue #255 post-fusion rerank combinator — default inert (byte-identical)
rerank_combinator = "additive"
rerank_band_delta = 0.05
rerank_tier_weight = 1.0
fts5_weight = 3.0
splade_weight = 3.5
tag_exact_weight = 3.0
tag_prefix_weight = 1.5
sema_boost_weight = 2.0
sema_cold_weight = 3.0
lex_anchor_weight = 1.5
harmonic_weight = 1.0
entity_graph_weight = 0.5
dense_weight = 1.0
dense_additive_weight = 4.0            # additive-mode only; unused under rrf
dense_additive_min_cosine = 0.15       # additive-mode only; unused under rrf
# Semantic-wiring arm (env-gated HELIX_SEMANTIC_ARM=1)
semantic_dense_additive_weight = 16.0
semantic_broaden_routing = true
pki_weight = 1.0
```

**Migration notes.**

- `fusion_mode` default flipped `"additive"` → `"rrf"` on 2026-07-06
  (this is `v(N+1)` of the `docs/specs/2026-05-08-stage-3-rrf-fusion.md`
  §7 deprecation timeline; `v(N+2)` removes the additive code path).
  Set `fusion_mode = "additive"` explicitly to restore the legacy
  accumulator until then.
- Under `"rrf"` the abstain/TIGHT/FOCUSED gates run **ratio-only**
  (`pipeline/tier_logic.py` `skip_absolute_floors`) — the Stage-3 spec
  §9 transitional bypass — because the global hard-coded floors
  (`5.0` / `2.5`) were calibrated against additive scores and become
  unreachable post-RRF. For floor-driven gates under RRF score scales,
  use `[abstain].mode = "per_classifier"` with RRF-calibrated floors.
- `dense_embedding_dim` raised from 256 → 1024 in Stage 2. Existing
  256-d embeddings on disk continue to load via the codec guard
  (`bgem3_codec.py:53-54`), but new ingest writes 1024-d vectors. Stage 4
  recalibrates `ann_similarity_threshold` against the new dim.
- `ann_threshold_mode = "margin_over_random"` only takes effect after
  `scripts/calibrate_thresholds.py` has populated the
  `genome_calibration` table. Missing row → one-time WARN, fallback to
  `ann_similarity_threshold`.

**Cross-refs.** `helix_context/config.py:239-320`
(`RetrievalConfig`), `helix_context/fusion.py` (RRF `Fuser`),
`docs/specs/2026-05-08-stage-2-dense-recall.md`,
`docs/specs/2026-05-08-stage-3-rrf-fusion.md`,
`docs/specs/2026-05-08-stage-4-threshold-calibration.md`,
`scripts/calibrate_thresholds.py` (operator runbook for
margin-over-random calibration), `[abstain]` (Stage-4 floors that
replace the global hard-coded `5.0` / `2.5` constants).

---

## `[plr]`

**Purpose.** Stacked PLR (Pairwise Logistic Ranker) query-confidence
head. Attaches a `plr_confidence` log-odds signal to
`/context/packet` responses — predicted log-odds that the user will
re-query within 60s under the training discipline (cos(q_t, q_{t+1})
filter + 60s window). Higher = more likely to re-query = lower
confidence in the retrieval.

The current artifact is a **query-quality head**, not the per-(q, g)
ranker originally described in the spec. Document ranking stays on the
fuser (additive or RRF, see `[retrieval] fusion_mode`); this signal
only feeds the packet / router. See `helix_context/fusion_plr.py`
docstring and `docs/archive/FUTURE/STATISTICAL_FUSION.md` §C3 addendum
for the scope trade-off.

**Keys.**

<!-- BEGIN GENERATED: config-tables:plr -->
| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `true` | default aligned with shipped helix.toml (2026-06-12 default-honesty pass) — bench-gated #74; soft-no-op without artifact |
| `model_path` | `str` | `"training/models/stacked_plr.joblib"` |  |
| `expected_sha256` | `str` | `""` | SHA256 of the artifact — when set, load refuses to proceed unless the file's digest matches. Empty string = trust the sidecar .sha256 next to the artifact (written by the trainer). Set a pinned hex digest in helix.toml if you want explicit operator-level pinning. |
| `high_risk_threshold` | `float` | `0.5` | Threshold the fuser's `prob_B` is compared against to emit a coarse "likely-to-re-query" boolean alongside the log-odds. 0.5 is the symmetric default; tune only with bench evidence. |
<!-- END GENERATED -->

**Example.**

```toml
[plr]
enabled = false
model_path = "training/models/stacked_plr.joblib"
expected_sha256 = ""
high_risk_threshold = 0.5
```

**Migration notes.** Train a fresh artifact with:

```bash
python scripts/pwpc/sprint3.py <windowed_export.json> \
    --save-model training/models/stacked_plr.joblib \
    --save-label-set best
```

**Cross-refs.** `helix_context/config.py:387-410` (`PLRConfig`),
`helix_context/fusion_plr.py` (consumer),
`docs/archive/FUTURE/STATISTICAL_FUSION.md` §C3.

---

## `[know]`

**Purpose.** Stage-6 KnowBlock confidence logistic. Maps four (Stage
6) or five (Stage 7) retrieval signals to a calibrated probability
that the `/context` retrieval is ground-truth correct:

```
z = b0
  + b1 * tanh(top_score / s_ref)
  + b2 * tanh(score_gap / g_ref)
  + b3 * (1.0 if lexical_dense_agree else 0.0)
  + b4 * coordinate_confidence
  + b5 * freshness_min                  # Stage 7 (NEW)
confidence = 1 / (1 + exp(-z))
```

When `confidence >= emit_floor`, a `KnowBlock` is emitted; otherwise
the decision falls through to `MissBlock(reason="sparse")` (or
`reason="stale"` under Stage 7's freshness gate).

These are SHIP-TIME defaults. Operator action post-merge:

```bash
python scripts/calibrate_know_confidence.py \
    --input results/located_n1000.jsonl \
    --out helix.toml
```

Calibration requires Stage 1 (bench axis split) to land first — it
produces the bench JSONL.

**Keys.**

<!-- BEGIN GENERATED: config-tables:know -->
| Key | Type | Default | Description |
|---|---|---|---|
| `emit_floor` | `float` | `0.55` | Probability floor below which no KnowBlock is emitted (falls through to MissBlock(reason="sparse")). |
| `s_ref` | `float` | `1.0` | tanh feature-scale references for top_score / score_gap. |
| `g_ref` | `float` | `0.5` |  |
| `betas` | `List[float]` | `[-2.0, 2.0, 1.5, 0.7, 1.8, 1.5]` | (b0, b1..b5) — intercept + 5 feature coefficients (Stage 7 added b5 for freshness_min). Malformed/odd-length lists soft-fail to defaults at load time so a bad calibration write can never break retrieval. |
| `calibrated_at` | `Optional[str]` | `None` | Written by scripts/calibrate_know_confidence.py; None = uncalibrated. |
| `calibrated_on_n` | `Optional[int]` | `None` |  |
| `stale_after_days` | `int` | `30` | Stage 4 (spec §9, issue #63): age in days after which the /context response flags ``calibration_stale``. |
<!-- END GENERATED -->

`stale_after_days` (Stage 7, spec §9 / issue #63) is the age in days
after which the `/context` response flags `calibration_stale`.
`calibrated_at` / `calibrated_on_n` are written by
`scripts/calibrate_know_confidence.py` after a fresh calibration run —
`None` means the betas are still the SHIP-TIME defaults. The shipped
`helix.toml` today carries the 2026-07-06 real calibration fit (ECE
0.74 → 0.04 vs the code defaults above); see
`docs/benchmarks/2026-07-06-know-logistic-calibration.md`.

**Example (post-Stage-7 calibration).**

```toml
[know]
emit_floor = 0.55
s_ref = 1.0
g_ref = 0.5
betas = [-2.0, 2.0, 1.5, 0.7, 1.8, 1.5]   # [intercept, top, gap, agree, coord, freshness]
calibrated_at = "2026-05-08T18:30:00Z"
calibrated_on_n = 800
```

**Migration notes.** Pre-Stage-7 `[know]` blocks carry a 5-element
`betas` array (missing the freshness coefficient). The loader expects
6 entries (`1 + N_FEATURES = 1 + 5`) post-Stage-7; a mismatched length
logs a `WARNING` and falls back to the 6-element code default.
Operators upgrading from a pre-Stage-7 `helix.toml` should either
re-run `scripts/calibrate_know_confidence.py` to refresh `[know]`, or
manually append the Stage-7 default coefficient (`+1.5`) to the array.
The shipped `helix.toml` has carried a real 6-element calibration fit
since the 2026-07-06 rrf-default sweep.

**Cross-refs.** `helix_context/know_calibration.py` (pure-function
loader; soft-fails to defaults), `helix_context/context_packet.py`
(`KnowBlock` / `MissBlock` emit path),
`docs/specs/2026-05-08-stage-6-know-miss-blocks.md` §3, §11,
`docs/specs/2026-05-08-stage-7-freshness-gate.md` §10,
`scripts/calibrate_know_confidence.py` (operator runbook).

---

## `[mem_sync]`

**Purpose.** Auto-memory → helix sync. Every `.md` file in a watched
directory becomes a document; persona / agent attribution comes from
`HELIX_AGENT` / `HELIX_USER` env vars on the syncer process. See
`scripts/run_mem_sync.py`.

**Keys.**

| Key | Type | Default | Effect |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. Set `true` and run `scripts/run_mem_sync.py`. The shipped value (`helix.toml:349`). |
| `helix_url` | str | `"http://127.0.0.1:11437"` | Helix server URL — must match `[server] host:port` for the local case. The shipped value (`helix.toml:350`). |
| `sync_interval_s` | int | `60` | Poll cadence in seconds. Human-speed writes; cheap stat+hash check per file. The shipped value (`helix.toml:351`). |
| `agent_kind` | str | `"claude-code"` | Stamp written on every ingest; identifies the writer tool for downstream attribution. The shipped value (`helix.toml:352`). |
| `watch_dirs` | array<str> | `[]` | Directories to watch. `~` is expanded automatically. Empty by default for the public release — point this at your own memory/notes directories. |

**Example.**

```toml
[mem_sync]
enabled = false
helix_url = "http://127.0.0.1:11437"
sync_interval_s = 60
agent_kind = "claude-code"
# watch_dirs = ["~/.claude/projects/<your-project>/memory"]
watch_dirs = []
```

**Note.** This section is **not** parsed by
`helix_context/config.py` directly — it is consumed by the standalone
`scripts/run_mem_sync.py` syncer. The block lives in `helix.toml` for
single-source-of-truth co-location; the syncer reads its own copy.

**Cross-refs.** `scripts/run_mem_sync.py` (consumer).

---

## `[synonyms]`

**Purpose.** Lightweight synonym map for tags expansion. Query
keywords map to a list of synonym tags that the retrieval layer uses
to broaden tag-exact and tag-prefix matches. Edit-and-restart only —
read once at process start.

**Schema.**

Each entry is `<query keyword> = [<synonym>, <synonym>, ...]`. There
are no nested tables. The loader builds `cfg.synonym_map` as a
`Dict[str, List[str]]` (`helix_context/config.py:867-870`).

**Example (excerpt from the shipped file at `helix.toml:357-398`).**

```toml
[synonyms]
slow = ["performance", "latency", "bottleneck", "timeout", "lag"]
fast = ["performance", "speed", "optimization", "cache"]
cache = ["redis", "ttl", "invalidation", "cdn", "caching", "eviction"]
auth = ["jwt", "login", "security", "token", "session", "oauth"]
helix = ["genome", "ribosome", "codons", "splice", "chromatin", "promoter", "context compression"]
db = ["database", "sqlite", "postgres", "sql", "query", "schema"]
api = ["endpoint", "route", "rest", "http", "request", "response"]
test = ["pytest", "unittest", "mock", "assert", "coverage"]
deploy = ["docker", "kubernetes", "ci", "cd", "pipeline", "helm"]
config = ["settings", "toml", "env", "environment", "dotenv"]
# ... (full set in helix.toml)
```

**Operator note.** The synonym map is critical. If queries return "no
relevant context", the most common cause is that query keywords don't
map to the tags the ingestion layer assigned. Add a synonym
entry and restart.

**Cross-refs.** `helix_context/config.py:866-870` (loader),
`helix.toml:357-398` (full shipped map).

---

## `[abstain]`

**Purpose.** **NEW (Stage 4, 2026-05-08)** — per-classifier
confidence floors. When `mode = "global"` (default), the
`context_manager` retains the legacy hard-coded
`TIGHT_SCORE_FLOOR = 5.0` / `FOCUSED_SCORE_FLOOR = 2.5` /
`abstain = 2.5` constants — pre-Stage-4 behavior byte-for-byte. Flip
to `"per_classifier"` after running
`scripts/calibrate_thresholds.py` to consume the emitted
`[abstain.<cls>]` blocks.

`"per_classifier"` **REQUIRES** an `[abstain.default]` block — the
loader raises `ConfigError` otherwise (`helix_context/config.py:751-756`).
Other classes may be omitted; runtime falls back to
`[abstain.default]` via `AbstainConfig.floors_for(cls)`.

**Top-level keys.**

<!-- BEGIN GENERATED: config-tables:abstain -->
| Key | Type | Default | Description |
|---|---|---|---|
| `mode` | `str` | `"global"` |  |
<!-- END GENERATED -->

**Sub-tables.** `[abstain.factual]`, `[abstain.multi_hop]`,
`[abstain.arithmetic]`, `[abstain.procedural]`, `[abstain.default]`.
Each takes the same four keys (calibrated from `located_n1000.json`
score distributions per Stage-4 spec §4):

<!-- BEGIN GENERATED: config-tables:abstain.subtable -->
| Key | Type | Default | Description |
|---|---|---|---|
| `abstain_top` | `float` | `2.5` | p85 of MISS scores — anything strictly below this is abstain. |
| `focused_top` | `float` | `2.5` | p25 of HIT scores — at-or-above this enters FOCUSED tier (with ratio gate). |
| `tight_top` | `float` | `5.0` | p60 of HIT scores — at-or-above this enters TIGHT tier (with ratio gate). |
| `foveated_alpha` | `float` | `1.0` | Per-class foveated splice power-law exponent. Replaces ``budget.foveated_alpha`` when ``[abstain].mode = "per_classifier"``. |
<!-- END GENERATED -->

**Example (calibrated, post-`scripts/calibrate_thresholds.py`).**

```toml
[abstain]
mode = "per_classifier"

[abstain.factual]
abstain_top = 0.42
focused_top = 0.71
tight_top = 1.18
foveated_alpha = 1.4

[abstain.multi_hop]
abstain_top = 0.38
focused_top = 0.55
tight_top = 0.92
foveated_alpha = 0.6

[abstain.arithmetic]
abstain_top = 0.30
focused_top = 0.60
tight_top = 1.00
foveated_alpha = 1.6

[abstain.procedural]
abstain_top = 0.40
focused_top = 0.65
tight_top = 1.10
foveated_alpha = 0.9

[abstain.default]
abstain_top = 0.40
focused_top = 0.65
tight_top = 1.10
foveated_alpha = 1.0
```

**Migration notes.**

- `mode = "global"` preserves Stage-3 behavior byte-for-byte.
- Flipping to `"per_classifier"` requires `[abstain.default]`; other
  classes are optional (fall back to default).
- Calibration percentiles are asymmetric by design: `abstain_top` is
  the upper tail of misses (recall-biased) while `tight_top` is the
  lower-middle of hits (precision-biased). See Stage-4 spec §4 for
  the rationale.
- Acceptance criterion (Stage-4 spec §11): per-classifier abstain
  rate on factual queries ≤ 5% (current implicit ~95% — `5.0` floor
  unreachable post-RRF).

**Cross-refs.** `helix_context/config.py:323-373` (`AbstainConfig`,
`AbstainClassFloors`, `floors_for`),
`docs/specs/2026-05-08-stage-4-threshold-calibration.md` §4 + §6,
`scripts/calibrate_thresholds.py` (operator runbook —
emits `[abstain.<cls>]` blocks from a `located_n1000.jsonl` bench
run), `[budget] foveated_alpha` (overridden when
`mode = "per_classifier"`),
`[retrieval] fusion_mode` (RRF requires per-class floors per
Stage-3 spec §9 transitional bypass).

---

## `[vault]` (commented-out template)

**Purpose.** Obsidian-style vault export for operators. v1 ships as a
read-only export plus diagnostic `/context` traces; curation / inbox
arrive in v1.1. Off by default; the entire block is commented out in
the shipped `helix.toml`.

**Keys.**

<!-- BEGIN GENERATED: config-tables:vault -->
| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `false` |  |
| `path` | `str` | `"~/.helix/vault"` |  |
| `party_id` | `str` | `""` | empty = use server's primary party |
| `fan_out_threshold` | `int` | `5000` |  |
| `redact_body` | `bool` | `false` |  |
| `stale_threshold` | `float` | `0.5` |  |
<!-- END GENERATED -->

**`[vault.traces]` sub-table.**

<!-- BEGIN GENERATED: config-tables:vault.traces -->
| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `true` |  |
| `retention_hours` | `int` | `48` |  |
| `max_retention_hours_hard` | `int` | `720` | 30 days; 0 disables |
| `max_count` | `int` | `10000` | v1.1: not yet enforced |
| `rollup_enabled` | `bool` | `true` |  |
| `rollup_shard` | `str` | `"hour"` | "hour" \| "daily" |
| `prune_interval_minutes` | `int` | `60` |  |
| `trigger_only` | `bool` | `false` | v1.1: not yet enforced |
<!-- END GENERATED -->

**Example (uncomment to enable).**

```toml
[vault]
enabled = false
path = "~/.helix/vault"
fan_out_threshold = 5000
redact_body = false
stale_threshold = 0.5

[vault.traces]
enabled = true
retention_hours = 48
max_retention_hours_hard = 720
max_count = 10000
rollup_enabled = true
rollup_shard = "hour"
prune_interval_minutes = 60
trigger_only = false
```

**Cross-refs.** `helix_context/config.py:452-476` (`VaultConfig`,
`VaultTracesConfig`), `helix.toml:399-419` (commented template).

---

# Configuration loading order

`helix_context.config.load_config()` resolves configuration in this
priority order (highest priority wins):

1. **Per-call request fields.** Endpoint-specific overrides on the
   request body (e.g., `caller_model_class` on `/context`,
   `include_cold` on `/context`, `pool_size` on the dense recall
   path). Per-call overrides do not mutate the loaded `HelixConfig`.
2. **Process-level environment variables.** A defined set of `HELIX_*`
   env vars override loaded values:
   - `HELIX_CONFIG` — path to the TOML file (defaults to
     `helix.toml`). Read in
     `helix_context/config.py:520-521`.
   - `HELIX_GENOME_PATH` — overrides `[genome] path` after the TOML
     load. Read in `helix_context/config.py:611-612`.
   - `HELIX_SERVER_UPSTREAM` — overrides `[server] upstream`. Read in
     `helix_context/config.py:616-617`.
   - `HELIX_SERVER_UPSTREAM_TIMEOUT` — overrides `[server]
     upstream_timeout` (float, ignored on parse error with a
     `WARNING`). Read in `helix_context/config.py:618-625`.
   - `HELIX_DEVICE` — one-shot device pin. Consumed by
     `hardware.init_from_config()`, not the TOML loader directly.
   - `HELIX_PARTY_ID` — preferred override for `[session]
     default_party_id` (consumed by request-handling code, not the
     loader).
   - `HELIX_USE_SHARDS` — sharded vs monolithic knowledge store routing.
   - `HELIX_FILENAME_ANCHOR_ENABLED` — one-shot override for
     `[retrieval] filename_anchor_enabled`.
   - `HELIX_ABSTAIN_DISABLE` — one-shot override for `[budget]
     abstain_enabled`.
3. **TOML file values.** `helix.toml` (or the path in `HELIX_CONFIG`).
   Loaded once at process start.
4. **Dataclass defaults.** Every field in `helix_context/config.py`
   has a typed default. Returned as-is when no file is present
   (`helix_context/config.py:524-526`).

The loader is **soft-fail** on every section: malformed TOML, unknown
keys (`_warn_unknown` in `helix_context/config.py:500-512`), or
malformed values fall back to the dataclass default with a
`log.warning`. The single hard-fail is `[abstain].mode =
"per_classifier"` without an `[abstain.default]` block, which raises
`ConfigError` (`helix_context/config.py:751-756`).

---

# Hot-reload semantics

**Restart-required.** Most sections are read **once** at process
start. Changes take effect only after restarting the helix-context
process:

- `[ribosome]`, `[hardware]`, `[server]`, `[genome]`, `[ingestion]`,
  `[cymatics]`, `[classifier]`, `[plr]`, `[abstain]`, `[mem_sync]`,
  `[synonyms]`, `[vault]`, `[headroom]`.
- `[budget]` token caps and `[retrieval]` weights / mode (loaded once
  into `RetrievalConfig`; not mutated at runtime).
- `[session]` synthetic-session window.

**Hot-reloaded (per-call).** Some knobs are read on each request:

- `[budget] foveated_alpha`, `[budget] foveated_c_min`,
  `[budget] foveated_base_chars` — consulted per `/context` call by
  the foveated splice scheduler (subject to per-classifier overrides
  from `[abstain]` at query time).
- `[retrieval] ann_threshold_mode` — checked per
  `query_genes_ann` invocation; the runtime then either reads the
  legacy `ann_similarity_threshold` (absolute mode) or queries the
  `genome_calibration` table (margin-over-random mode).
- `[retrieval] fusion_mode` — checked per `query_genes` invocation
  (`fusion_mode == "rrf"` selects the `Fuser` branch; otherwise the
  legacy additive accumulator).

**Hot-reloaded via `/admin/refresh`.** The retrieval-layer cache
invalidation surface:

- The `[retrieval]` knobs that are cached at startup (e.g., the
  `_dense_pool_size` instance attribute) refresh when the
  retrieval layer reloads.
- The `genome_calibration` row consumed by `ann_threshold_mode =
  "margin_over_random"` is read on first `/context` after process
  start, then cached. **Persistence-manager rotation invalidates the
  cache** (`helix_context/genome.py` `_effective_ann_threshold` —
  Stage-4 spec §8).

**`[know]` hot-reload.** The `[know]` block is **hot-reloaded** via
the pure-function loader in `helix_context/know_calibration.py`. The
calibration table is read on first `/context` after process start and
cached thereafter; subsequent edits to `[know]` require a
`/admin/refresh` (or process restart) to invalidate the cache. The
loader is fail-soft: a missing file, missing `[know]` table, or
malformed entries log a `WARNING` and return defaults — KnowBlock
emission continues at the SHIP-TIME operating point until calibration
runs.

**Operator note.** When in doubt, restart. Hot-reload paths exist for
the highest-churn knobs (`fusion_mode`, `ann_threshold_mode`,
`[know]`) but the rest of the surface is start-once.

---

# Default `helix.toml`

The full current `helix.toml` as of 2026-05-10 (post-7-stage merge),
provided as a copy-paste baseline for new operators. Cross-reference
the per-section tables above for key-by-key behavior.

```toml
# ──────────────────────────────────────────────────────────────────────
# [ribosome] — OPTIONAL ENRICHMENT LAYER. Off the hot path.
#
# The 12-tier retrieval pipeline and /context path are LLM-free today.
# MiniLM (SEMA) and DeBERTa (rerank/splice, when enabled) are encoders/
# classifiers, not generators. Context assembly is pure Python.
#
# The ribosome is only consulted when a ribosome op is EXPLICITLY
# invoked — ingest-time `pack()`, background `replicate()`, or the
# (default-off) Step 0 query intent expansion. Leave this section
# alone and /context never touches an LLM.
#
# Think of it as a "subconscious" layer: reflective re-processing
# during idle, tighter complements, cross-document pattern noticing
# with a larger model — separate subsystem against the same knowledge
# store, not a
# dependency of the retrieval loop. Partner/vendor eval hook too —
# Anthropic / Google / etc. can plug a hosted compression model into
# this seam without forking.
# ──────────────────────────────────────────────────────────────────────
[ribosome]
enabled = false                        # Explicit opt-in. Disabled/legacy ribosome config is ignored unless you turn this on.
backend = "ollama"                      # Legacy/default placeholder. Only "litellm" and "deberta" are honored when enabled=true.
model = "gemma4:e2b"                    # Ollama model for pack + replicate (light, ~2GB VRAM)
base_url = "http://localhost:11434"
timeout = 120                            # Increased for bulk ingestion
keep_alive = "30m"                      # keep Ollama model loaded
warmup = false                          # pre-load Ollama model on server start (disabled for N=1000 bench — qwen3:4b holds VRAM)
query_expansion_enabled = false         # Step 0 LLM query-intent expansion. false = strictly LLM-free /context (the 12 retrieval tiers never need this). Flip on for an extra 2-3pp on ambiguous queries at the cost of one ribosome call per request.
# Sub-query decomposition — decomposes broad queries into 3 point-fact sub-queries.
# Only fires for multi_hop/default classifier class. Requires query_expansion_enabled = true.
query_decomposition_enabled = false

# Cloud-backend opt-in — keep commented so local stays default.
# Useful for partner/vendor eval (Anthropic, Google, etc.) plugging a
# hosted compression/extraction model into the ribosome seam without
# forking, or for freeing the local GPU during heavy bench runs.
# backend = "claude"
# claude_model = "claude-haiku-4-5-20251001"   # haiku = cost-effective bulk; swap to claude-sonnet-4-6 for higher resolution
# claude_base_url = ""                          # "" = direct Anthropic API; set to a proxy URL (e.g. http://127.0.0.1:8787) to route through a gateway
# litellm_model = "ollama/gemma4:e2b"           # LiteLLM model string — only used when backend = "litellm"

[hardware]
# Device picker. "auto" picks best-available (cuda -> rocm -> mps -> cpu).
# Explicit values fall back loudly to CPU on probe failure; helix never
# blocks on hardware mismatch -- see /health for fallback state.
# Override one-shot via HELIX_DEVICE=cpu env var.
device = "auto"        # auto | cuda | rocm | mps | cpu

# Batch-size policy. "auto" consults the VRAM/RAM-aware table in
# helix_context/hardware.py. Override per model when tuning:
#   batch_sizes = { rerank = 16, splice = 32, splade = 8, nli = 8 }
batch_sizes = "auto"

# Soft-warn threshold. Below this, /health returns a "low_vram" hint and
# the tray surfaces a one-time balloon. Set to 0 to disable.
low_vram_threshold_gb = 4.0

[budget]
ribosome_tokens = 3000                  # fixed decoder prompt
expression_tokens = 12000               # 1:10 ratio at 128K = ~12.8K total context budget
max_genes_per_turn = 12
max_fingerprints_per_turn = 40          # navigation-first fingerprint payload cap (final returned count, not frontier width)
splice_aggressiveness = 0.3             # 0=keep all, 1=ruthless trim (lower preserves more literal detail)
decoder_mode = "condensed"              # "full"|"condensed"|"minimal"|"none" (none saves ~750 tokens for API models)
# Sprint 1 legibility pack (AI-consumer roadmap): emit one metadata line
# per gene in expressed_context — fired tiers, confidence marker, short
# gene_id, raw→compressed chars. See helix_context/legibility.py + docs/
# FUTURE/AI_CONSUMER_ROADMAP_2026-04-14.md. Flip to false to restore
# plain-dividers format for bench A/B.
legibility_enabled = true
# Sprint 2 session working-set register (AI-consumer roadmap): track
# genes delivered per session so re-retrievals elide repeats with a
# pointer stub. Enabled 2026-04-19 — MVP writes to session_delivery_log
# on every /context call (synthetic session_id fallback covers callers
# that don't supply one). Set to false to restore pre-MVP dark behavior.
# See helix_context/session_delivery.py.
session_delivery_enabled = true
# Confidence-gated context attachment (ABSTAIN tier). When true (default),
# build_context returns a marker-only ContextWindow when post-refinement
# retrieval is weak on BOTH absolute score (top_score < 2.5) AND ratio
# (top_score/mean < 1.8) — the negative space of the TIGHT and FOCUSED
# tiers. Goal: skip the 12K-token BROAD fallback on queries where helix
# can't help, so the small model answers from weights instead of digesting
# irrelevant noise. Set to false to restore the legacy always-inject
# behavior (BROAD takes the negative space). HELIX_ABSTAIN_DISABLE=1 env
# var forces off without redeploy. See docs/specs/2026-05-02-abstain-tier-design.md.
abstain_enabled = true
# Foveated-splice (BROAD tier only). When True, the BROAD branch of the
# dynamic-budget tier replaces uniform per-gene compression with a rank-
# scaled power-law schedule and reverses the assembly order so the top-
# ranked gene lands immediately before the user query. Off by default for
# the measurement period — see docs/specs/2026-05-03-foveated-splice-
# design.md §6.3.
foveated_enabled = false

# Power-law exponent: c_i = max(c_min, c_max · i^(-α)). α=0.5 = gentle
# decay, α=1.0 (default) = harmonic-ish, α=2.0 = aggressive top-bias.
# Bench Phase 2 sweeps {0.5, 1.0, 2.0}; ship the winner.
foveated_alpha = 1.0

# Rank-N (bottom of BROAD) compression floor. Each gene's effective char
# cap = max(foveated_c_min · base, c_i · base) where c_i = c_max · i^(-α).
foveated_c_min = 0.15

# Per-gene char-budget multiplier. target_chars per gene = int(c_i · base).
# Default 1000 matches current uniform Step 4 behavior at c_i = 1.0.
foveated_base_chars = 1000

[session]
# CWoLa session / party fallback. Prior to 2026-04-13 the /context endpoint
# passed NULL to cwola.log_query when clients didn't supply session_id or
# party_id — which caused sweep_buckets to treat every row as Bucket A
# (no re-query detectable without a session), making CWoLa training
# impossible. See cwola.py + STATISTICAL_FUSION.md §C2.
# Default party_id for CWoLa / session attribution when no env var or header is set.
# Operators: set HELIX_PARTY_ID in your environment (preferred) or change this value.
default_party_id = "default"            # Attribution fallback when the request omits party_id
synthetic_session_window_s = 300        # 5 min — same-IP requests within this window group into one session
synthetic_session_enabled = true        # Flip to false to restore prior NULL-session behavior (not recommended)

[genome]
# 2026-04-16: swapped to fresh-rebuild clean genome. Old paths archived:
# F:/Projects/helix-context/genome.db   — old master (pre-rebuild)
# C:/helix-cache/genome.db              — old replica (had backfill)
# New genomes/ folder is the phase-2 sharding root. main/ is v1's only
# shard; future shards land as genomes/reference/, genomes/agent/, etc.
path = "genomes/main/genome.db"
compact_interval = 3600                 # seconds between source-change checks (hourly)
cold_start_threshold = 10               # genes needed before history stripping kicks in (Fix 3)
# Replicas disabled during phase-1 cutover. Will re-enable alongside
# the shard router in phase 2 (sharding changes the replication shape).
replicas = []
replica_sync_interval = 100             # sync replicas every N inserts
# No time-based decay. Genes never expire.
# Compaction only detects source file changes (mtime vs last_accessed).
# Irrelevant data is handled by splice (intron/exon) at expression time.

[server]
host = "127.0.0.1"
port = 11437
upstream = "http://localhost:11434"     # Ollama
# upstream_timeout = 180                # Default in config.py is 180s. Raise to 240+ for very slow models or large prompts; lower if you want fail-fast.

# ── [headroom] — OPTIONAL PROXY LIFECYCLE (launcher only). ─────────────
# Headroom (https://github.com/chopratejas/headroom) is a separate
# process serving a compression proxy + dashboard at
# http://{host}:{port}/dashboard. The launcher can spawn it as a child
# at boot (autostart=true) and surface it in the tray menu.
#
# When ``enabled=true`` the launcher:
#   1. Adopts an existing headroom proxy if one is already listening on
#      ``port`` — no duplicate spawn, and adopted process survives
#      launcher Quit.
#   2. Otherwise spawns a fresh headroom child (``autostart=true`` default).
# the tray gains "Open Headroom Dashboard" + Start/Restart/Stop
# Headroom menu items.
# Requires: pip install "helix-context[codec]" (pulls headroom-ai[proxy])
[headroom]
enabled = true                          # Master switch; true = tray wires Headroom and adopts/spawns it on launch
autostart = true                        # When enabled: adopt if running, spawn if not. Set false for menu-only mode.
host = "127.0.0.1"
port = 8787                             # Default headroom proxy port
mode = "token"                          # "token" (compression-first) | "cache" (prefix-cache-stable)
dashboard_path = "/dashboard"           # Appended to http://{host}:{port} for the tray menu link

[ingestion]
backend = "cpu"                         # "ollama" (LLM, slow) | "cpu" (spaCy+regex, fast) | "hybrid"
splade_enabled = true                   # Phase 2: SPLADE sparse expansion at index time
rerank_model = "cross-encoder/ms-marco-MiniLM-L-6-v2"
rerank_enabled = false                  # Phase 3: enable pretrained cross-encoder reranking
colbert_enabled = false                 # Phase 4: ColBERT late interaction (optional)
entity_graph = true                     # Phase 5: entity-based co-activation links
dense_passage_char_cap = 2000           # #207 dense fast-follow (2026-07-10): BGE-M3 passage char cap; must match [retrieval] dense_model's codec on all 3 encode paths

[context]
# Cold-tier retrieval (C.2 of B->C, 2026-04-10)
# Heterochromatin-tier genes are demoted by the density gate but their
# content is preserved (C.1). When this fallthrough fires, /context
# consults cold-tier genes via SEMA cosine similarity in 20-dim space.
# Per-request override: `include_cold: true` on the /context body.
# min_cosine too high (e.g. 0.25) means cold-tier returns nothing on
# real NIAH queries. 0.15 is calibrated to surface matches in the noise floor.
cold_tier_enabled = false               # master opt-in
cold_tier_min_hot_genes = 0             # fall through when hot returns <= this many genes (0 = only on empty)
cold_tier_k = 3                         # max cold-tier genes per query
cold_tier_min_cosine = 0.15             # SEMA cosine floor (sparse 20-dim — see comment above)
fingerprint_mode_profile = "balanced"   # "fast" | "balanced" | "quality" for POST /fingerprint and /debug/preview

[cymatics]
enabled = true                          # Blended as bonus (0.5 max), not used to re-sort
n_bins = 256                            # Spectrum resolution (256 bins = <2KB per spectrum)
peak_width = 3.0                        # Gaussian peak width — overridden by Q-factor from splice_aggressiveness
use_embeddings = false                  # Use Gene.embedding when populated (requires sentence-transformers)
harmonic_links = true                   # Compute weighted co-activation edges between expressed genes
distance_metric = "cosine"              # "cosine" (weighted dot) | "w1" (Werman 1986 circular Wasserstein-1; Singh 2020 CMD)

[classifier]
# Upstream rule-based query classifier / injection router.
# When enabled, contributes a decoder-mode hint and an assembly-stage
# gene-count cap to build_context(). See
# docs/specs/2026-04-29-query-classifier-injection-router-design.md.
enabled = true

[retrieval]
# Tier 5.5 — Successor Representation (Stachenfeld 2017). γ-discounted
# future-occupancy boost over co-activation graph. Lazy.
sr_enabled = true                       # Stage-1 bench flip (2026-04-22); research review flagged this as dark-shipped signal-positive
sr_gamma = 0.85                         # 5-10 hop horizon (Stachenfeld grid-cell sweet spot)
sr_k_steps = 4                          # Power-series truncation (cap runaway propagation)
sr_weight = 1.5                         # Per-gene SR contribution multiplier
sr_cap = 3.0                            # Max per-gene SR boost (matches harmonic cap)
# Theta alternation (Wang/Foster/Pfeiffer 2020) — bias ray_trace neighbor
# direction. Fore on even bounces, aft on odd.
ray_trace_theta = false                 # Dark ship — requires TCM velocity input (Sprint 1 item 3)
theta_weight = 1.0                      # Softmax temperature on v·gene_input_vector
# Sprint 4 — seeded co-activation edges (Hebbian dense-rank weighting).
seeded_edges_enabled = false            # Dark ship — flip to start accumulating co_count / miss_count
seeded_edge_weight = 1.0                # Base weight stamped at seed insertion
# Tier 0.5 filename-anchor (2026-04-15 Dewey-pivot spike).
# Dewey bench 2026-04-14: filename-as-primary-anchor gave +24pp
# retrieval over full path-token bag. Boosts genes whose filename
# stem matches a query term. One-shot env var override:
# HELIX_FILENAME_ANCHOR_ENABLED=1.
filename_anchor_enabled = true          # Stage-1 bench flip (2026-04-22); +12pp on Dewey axis-2 spike
filename_anchor_weight = 4.0            # Per-match boost (Tier 1 exact-tag = 3.0 for reference)
# BM25 shortlist post-filter (2026-04-22, research-review Pareto move 1).
# When enabled, query_genes restricts final ranking to genes in BM25 top-N.
# Dark by design — flip on for Stage-2 A/B measurement.
bm25_shortlist_enabled = true           # Keep on (2026-04-22 sprint): +1/8 ans_full on helix_rag + helix_full_stack, clean attribution
bm25_shortlist_size = 50
# BM25 pre-filter — fires BEFORE tier scoring (vs the post-filter shortlist).
# Enable for A/B against bm25_shortlist. Disable shortlist when using this.
bm25_prefilter_enabled = false
bm25_prefilter_size = 200
# Tier 5b: entity graph co-occurrence boost (Step 3C, 2026-05-08).
# Genes sharing entity nodes with query terms get a score boost
# proportional to entity overlap. Dark ship — flip to true for A/B.
entity_graph_retrieval_enabled = false
# Step 4 — BGE-M3 dense vectors + ANN threshold-based dynamic gene counts
# (2026-05-08). Stage 2 (2026-05-08): dim raised to 1024 (full BGE-M3
# Matryoshka). The dim=256 truncation collapsed random-pair cosine to
# ~0.6, sabotaging the threshold. Stage 4 will recalibrate
# ann_similarity_threshold at 1024-dim. Dense pool size decouples recall
# breadth from the final cut (max_genes).
dense_embedding_enabled = false
dense_embedding_dim = 1024
dense_model = "BAAI/bge-m3"               # #207 dense fast-follow (2026-07-10): BGE-M3 model ID / local mirror path
ann_similarity_threshold = 0.35           # legacy / fallback when calibration row missing
ann_threshold_min_genes = 1
ann_threshold_max_genes = 12
# Stage 4 (2026-05-08): margin-over-random ANN threshold. Spec
# docs/specs/2026-05-08-stage-4-threshold-calibration.md §3+§6.
# "absolute" (default) keeps Stage-3 behavior byte-for-byte: query_genes_ann
# uses ann_similarity_threshold above. Flip to "margin_over_random" after
# running scripts/calibrate_thresholds.py — the runtime then reads the
# persisted threshold from genome_calibration. Falls back to
# ann_similarity_threshold (with one-time WARN) if the row is missing.
ann_threshold_mode = "absolute"           # "absolute" | "margin_over_random"
ann_threshold_sigma_multiplier = 3.0      # mu + N*sigma over random pairs
dense_pool_size = 500
# Stage 3 (2026-05-08): Reciprocal Rank Fusion. Spec
# docs/specs/2026-05-08-stage-3-rrf-fusion.md replaces the additive
# gene_scores += tier_score accumulator with rank-level RRF (Cormack 2009).
# Default "rrf" since 2026-07-06 (v(N+1)). "additive" restores the legacy
# pre-Stage-3 byte-identical path. Per-tier weights below are RRF
# post-multipliers (preserve current implicit weights).
# Deprecation timeline: v(N) shipped additive default; v(N+1) flips to rrf
# default (this release); v(N+2) removes the additive code path.
fusion_mode = "rrf"                     # "rrf" | "additive" (legacy)
rrf_k = 60                              # Cormack 2009 default
fts5_weight = 3.0                       # current FTS5 cap
splade_weight = 3.5                     # current SPLADE cap
tag_exact_weight = 3.0                  # Tier 1 weight × match_count
tag_prefix_weight = 1.5                 # Tier 2 weight × match_count
sema_cold_weight = 3.0                  # ΣĒMA cold-start sim multiplier
lex_anchor_weight = 1.5                 # IDF anchor multiplier
harmonic_weight = 1.0                   # Tier 5 per-link weight
entity_graph_weight = 0.5               # Tier 5b entity boost
dense_weight = 1.0                      # Stage 2 dense recall, RRF participant
pki_weight = 1.0                        # path-key-index tier, RRF participant
# Note: filename_anchor_weight (above) and sr_weight (above) are reused.

[plr]
# Stacked PLR query-confidence head (STATISTICAL_FUSION.md §C3).
#
# Attaches a `plr_confidence` log-odds signal to /context/packet responses —
# predicted log-odds that the user will re-query within 60s under the
# training discipline (cos(q_t, q_{t+1}) filter + 60s window). Higher =
# more likely to re-query = lower confidence in the retrieval.
#
# The current artifact is a **query-quality head**, not the per-(q, g)
# ranker originally described in the spec. Gene ranking stays on the
# additive fuser; this signal only feeds the packet / router.
# See helix_context/fusion_plr.py docstring for the scope trade-off.
#
# Train a fresh artifact with:
#   python scripts/pwpc/sprint3.py <windowed_export.json> \
#       --save-model training/models/stacked_plr.joblib --save-label-set best
enabled = false                         # Dark by default; bench before flipping
model_path = "training/models/stacked_plr.joblib"
expected_sha256 = ""                    # Empty = trust the .sha256 sidecar (written by the trainer)
high_risk_threshold = 0.5               # `prob_B > this` surfaces a coarse "likely-to-re-query" boolean

[know]
# Stage 6 (2026-05-08) — KnowBlock confidence logistic.
#
# Spec: docs/specs/2026-05-08-stage-6-know-miss-blocks.md §3, §11.
#
# Maps four retrieval signals to a calibrated probability that the
# /context retrieval is ground-truth correct:
#
#   z = b0 + b1*tanh(top_score / s_ref)
#          + b2*tanh(score_gap / g_ref)
#          + b3*lexical_dense_agree
#          + b4*coordinate_confidence
#   confidence = 1 / (1 + exp(-z))
#
# When confidence >= emit_floor a KnowBlock is emitted; otherwise the
# decision falls through to MissBlock(reason="sparse").
#
# These are SHIP-TIME defaults. Operator action post-merge:
#   python scripts/calibrate_know_confidence.py \
#       --input results/located_n1000.jsonl \
#       --out helix.toml
# Calibration requires Stage 1 to land first (it produces the bench
# JSONL).
#
# # STAGE-7-EXT: Stage 7 will append b5 (default +1.5) for
# # freshness_min as the fifth feature; the betas list grows to 6
# # entries. The calibration script auto-detects the feature count.
emit_floor      = 0.55
s_ref           = 1.0
g_ref           = 0.5
betas           = [-2.0, 2.0, 1.5, 0.7, 1.8]

[mem_sync]
# Auto-memory → helix sync. Every .md file in a watched dir becomes a
# gene; persona/agent attribution comes from HELIX_AGENT / HELIX_USER
# env vars on the syncer process. See scripts/run_mem_sync.py.
enabled = false                         # Set true + run scripts/run_mem_sync.py
helix_url = "http://127.0.0.1:11437"
sync_interval_s = 60                    # Poll cadence; human-speed writes, cheap stat+hash
agent_kind = "claude-code"              # Stamp on every ingest; identifies writer tool
watch_dirs = [                          # Expand ~ automatically; add more as needed
  "~/.claude/projects/f--Projects-Education/memory",
]

[synonyms]
# Fix 1: lightweight synonym expansion for promoter queries
slow = ["performance", "latency", "bottleneck", "timeout", "lag"]
fast = ["performance", "speed", "optimization", "cache"]
cache = ["redis", "ttl", "invalidation", "cdn", "caching", "eviction"]
auth = ["jwt", "login", "security", "token", "session", "oauth"]
protein = ["folding", "amino", "alpha_helix", "beta_sheet", "alphafold", "biochemistry"]
tree = ["btree", "b-tree", "data_structures", "index", "binary_tree"]
helix = ["genome", "ribosome", "codons", "splice", "chromatin", "promoter", "context compression"]
pipeline = ["steps", "expression", "workflow", "process", "stages"]
scoring = ["scorerift", "divergence", "audit", "grades", "ratchet", "dimensions"]
compress = ["compression", "ratio", "target", "context_compression"]
ratio = ["compression", "ratio", "target", "factor"]
biged = ["biged-rs", "rust", "axum", "egui", "pyo3", "supervisor", "fleet"]
bookkeeper = ["bookkeeping", "financial", "transactions", "xero", "invoices", "tenant"]
cosmictasha = ["cosmic", "tasha", "novabridge", "biged-rs"]
error = ["exception", "crash", "failure", "bug", "traceback"]
port = ["proxy", "server", "configuration", "system_settings", "api"]
threshold = ["divergence", "scoring", "reconciliation", "configuration"]
model = ["ollama", "llm", "configuration", "deployment", "agent_frameworks"]
skills = ["fleet", "agent", "worker", "llm_systems", "systems_architecture"]
monetary = ["decimal", "financial", "data_processing", "bookkeeping"]
budget = ["tokens", "configuration", "ribosome", "expression"]
dimensions = ["audit", "scoring", "preset", "ci_cd"]
binary = ["rust", "deployment", "packaging", "compatibility"]
default = ["configuration", "system_settings", "deployment"]
db = ["database", "sqlite", "postgres", "sql", "query", "schema"]
api = ["endpoint", "route", "rest", "http", "request", "response"]
ui = ["frontend", "component", "render", "layout", "css", "style"]
test = ["pytest", "unittest", "mock", "assert", "coverage"]
deploy = ["docker", "kubernetes", "ci", "cd", "pipeline", "helm"]
config = ["settings", "toml", "env", "environment", "dotenv"]
conductor = ["llm", "agent", "orchestration", "model", "fleet"]

# Session terms (2026-04-09 SIKE / MoE decoder work)
vacuum = ["reclaim", "sqlite", "pages", "compact", "shrink", "admin"]
sike = ["scale_invariant", "benchmark", "retrieval", "sike_score"]
moe = ["mixture_of_experts", "sliding_window", "swa", "gemma4", "tissue", "answer_slate"]
slate = ["answer_slate", "kv_facts", "front_loaded", "extraction", "moe"]
decoder = ["decoder_mode", "condensed", "moe", "ribosome_prompt", "instructions"]
maintenance = ["admin", "vacuum", "refresh", "checkpoint", "compact"]

# ── Vault export (Obsidian) — opt-in, off by default ────────────────────
# Renders the genome as a browsable markdown vault for operators.
# v1: read-only export + diagnostic /context traces. Curation/inbox in v1.1.
# [vault]
# enabled = false
# path = "~/.helix/vault"
# party_id = ""                     # empty = server's primary party
# fan_out_threshold = 5000          # split domain folders above this count
# redact_body = false               # true → replace body with sha+excerpt
#                                   # recommended for cloud-synced setups
# stale_threshold = 0.5             # genes with live_truth_score < this go to _stale/
#
# [vault.traces]
# enabled = true                    # auto-export every /context call
# retention_hours = 48              # default; ≥720 for 30-day audit
# max_retention_hours_hard = 720    # force-deletes pinned past this; 0 disables
# max_count = 10000                 # safety cap on burst floods (v1.1: not yet enforced)
# rollup_enabled = true
# rollup_shard = "hour"             # hour | daily
# prune_interval_minutes = 60
# trigger_only = false              # emit only on threshold (v1.1: not yet enforced)

# ── Stage 4 (2026-05-08): per-classifier confidence floors ──────────────
# Spec: docs/specs/2026-05-08-stage-4-threshold-calibration.md §6.
# When mode='global' (default), context_manager uses the legacy hard-coded
# TIGHT_SCORE_FLOOR=5.0 / FOCUSED_SCORE_FLOOR=2.5 / abstain=2.5 — pre-Stage-4
# behavior byte-for-byte. Flip to 'per_classifier' after running
# scripts/calibrate_thresholds.py to consume the emitted [abstain.<cls>] blocks.
#
# 'per_classifier' REQUIRES an [abstain.default] block (loader raises
# ConfigError otherwise). Other classes may be omitted — runtime falls back
# to [abstain.default].
[abstain]
mode = "global"  # "global" | "per_classifier"

# Example calibrated blocks (commented out — populated by calibrate_thresholds.py):
# [abstain.factual]
# abstain_top = 0.42
# focused_top = 0.71
# tight_top = 1.18
# foveated_alpha = 1.4
#
# [abstain.multi_hop]
# abstain_top = 0.38
# focused_top = 0.55
# tight_top = 0.92
# foveated_alpha = 0.6
#
# [abstain.arithmetic]
# abstain_top = 0.30
# focused_top = 0.60
# tight_top = 1.00
# foveated_alpha = 1.6
#
# [abstain.procedural]
# abstain_top = 0.40
# focused_top = 0.65
# tight_top = 1.10
# foveated_alpha = 0.9
#
# [abstain.default]
# abstain_top = 0.40
# focused_top = 0.65
# tight_top = 1.10
# foveated_alpha = 1.0
```
