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

| Key | Type | Default | Effect |
|---|---|---|---|
| `enabled` | bool | `false` | Master opt-in. When `false`, every other key in this section is ignored at runtime; `RibosomeConfig.effective_backend` returns `"disabled"`. The shipped value (`helix.toml:21`). |
| `backend` | str | `"ollama"` | Legacy/default placeholder. Only `"litellm"` and `"deberta"` are honored when `enabled = true` (see `RibosomeConfig.effective_backend` in `helix_context/config.py:69-80`). `"ollama"` and `"claude"` resolve to `"disabled"` at runtime even when `enabled = true`. The shipped value (`helix.toml:22`). |
| `model` | str | `"gemma4:e2b"` | Ollama model name for `pack()` and `replicate()` when the compressor runs against Ollama. ~2GB VRAM footprint. The shipped value (`helix.toml:23`). |
| `base_url` | str | `"http://localhost:11434"` | Ollama API endpoint. The shipped value (`helix.toml:24`). |
| `timeout` | float | `120.0` | Per-request timeout in seconds for compressor calls. Bumped from the 10s code default to 120s for bulk ingestion. The shipped value (`helix.toml:25`). |
| `keep_alive` | str | `"30m"` | Ollama `keep_alive` directive that pins the compressor model in VRAM between calls. Critical when the same Ollama instance also serves the chat upstream (model swap latency dominates). The shipped value (`helix.toml:26`). |
| `warmup` | bool | `false` | Pre-load the compressor model on server start. Disabled in the shipped file because long-running benches keep the larger generation model resident. The shipped value (`helix.toml:27`). |
| `query_expansion_enabled` | bool | `false` | Step 0 LLM query-intent expansion (one compressor call per novel query, LRU-cached). `false` = strictly LLM-free `/context`; the 12 retrieval tiers run on raw query text plus the `[synonyms]` map. Flipping on adds 2-3pp on ambiguous queries at the cost of one compressor call per request. The shipped value (`helix.toml:28`). |
| `query_decomposition_enabled` | bool | `false` | Sub-query decomposition (Step 2). Decomposes broad queries into 2-4 point-fact sub-queries via one LLM call. Only fires for `multi_hop` and `default` classifier classes. Requires `query_expansion_enabled = true`. The shipped value (`helix.toml:31`). |
| `claude_model` | str | `"claude-haiku-4-5-20251001"` | Claude model used when `backend = "claude"`. Code default in `helix_context/config.py:33`. Commented in `helix.toml:38`. |
| `claude_base_url` | str | `""` | Empty = direct Anthropic API. Set to a proxy URL (e.g., `http://127.0.0.1:8787` for Headroom) to route through a gateway. Code default in `helix_context/config.py:34`. Commented in `helix.toml:39`. |
| `litellm_model` | str | `"gemini/gemini-2.5-flash"` | LiteLLM model string used when `backend = "litellm"`. Code default in `helix_context/config.py:35`. The commented example `"ollama/gemma4:e2b"` in `helix.toml:40` shows local routing. |
| `rerank_model_path` | str | `"training/models/rerank"` | DeBERTa rerank head artifact path. Code default only (`helix_context/config.py:36`). |
| `splice_model_path` | str | `"training/models/splice"` | DeBERTa splice head artifact path. Code default only (`helix_context/config.py:37`). |
| `splice_threshold` | float | `0.5` | Probability cutoff for the splice head. Code default only (`helix_context/config.py:38`). |
| `nli_model_path` | str | `"training/models/nli"` | NLI head artifact path. Code default only (`helix_context/config.py:39`). |
| `nli_splice_bonus` | float | `0.15` | Probability bonus for entailment-linked fragments. Code default only (`helix_context/config.py:40`). |
| `nli_splice_penalty` | float | `0.15` | Probability penalty for alternation-linked fragments. Code default only (`helix_context/config.py:41`). |
| `device` | str | `"auto"` | **DEPRECATED.** Legacy device hint kept for one release. The loader emits a `WARNING` whenever this key is set, urging the operator to move to `[hardware] device`. When both keys are present, `[hardware] device` wins (`helix_context/config.py:817-834`). |

**Example.**

```toml
[ribosome]
enabled = false                       # design pillar — leave false for LLM-free /context
backend = "ollama"                    # placeholder; only "litellm"/"deberta" honored when enabled
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

| Key | Type | Default | Effect |
|---|---|---|---|
| `device` | str | `"auto"` | Device picker. Accepts `"auto"`, `"cuda"`, `"rocm"`, `"mps"`, `"cpu"`. `"auto"` walks the priority list `cuda -> rocm -> mps -> cpu` and falls back loudly to CPU on probe failure (state surfaced via `/health`). One-shot override via `HELIX_DEVICE=cpu` env var. The shipped value (`helix.toml:47`). |
| `batch_sizes` | inline-table or str `"auto"` | `{}` (auto) | Per-model batch-size overrides applied on top of the auto-detected VRAM/RAM-aware table in `helix_context/hardware.py`. Empty dict (or the literal string `"auto"`) means "use the table". When provided as a TOML inline table, every key is cast to `int`. The shipped value uses the `"auto"` string sentinel (`helix.toml:52`). Loader normalisation lives in `helix_context/config.py:809-814`. |
| `low_vram_threshold_gb` | float | `4.0` | Soft warning threshold (GB). VRAM under this tier surfaces a `low_vram` hint via `/health` and a one-time tray balloon. Set `0.0` to disable. The shipped value (`helix.toml:56`). Surfaced for downstream consumers — not consulted by the picker itself. |

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

| Key | Type | Default | Effect |
|---|---|---|---|
| `ribosome_tokens` | int | `3000` | Fixed decoder prompt budget for the compressor path. Reserves headroom for the `<helix:slate>` decoder block. The shipped value (`helix.toml:59`). |
| `expression_tokens` | int | `12000` | Total context budget delivered to the chat upstream. Sized for a 1:10 ratio at 128K context = ~12.8K-token window. The shipped value (`helix.toml:60`). |
| `max_genes_per_turn` | int | `12` | Hard cap on assembled document count after refinement. Distinct from the dense recall pool (`[retrieval] dense_pool_size`). The shipped value (`helix.toml:61`). |
| `max_fingerprints_per_turn` | int | `40` | Cap on fingerprints returned by `POST /fingerprint`. Navigation-first payload, not a frontier width — wider candidate pools still feed the ranker upstream of this cap. The shipped value (`helix.toml:62`). |
| `splice_aggressiveness` | float | `0.3` | `0.0` = keep all, `1.0` = ruthless trim. Lower preserves more literal detail. Maps onto the cymatics splice resonance threshold via `[cymatics] splice_threshold_scale`. The shipped value (`helix.toml:63`). |
| `decoder_mode` | str | `"condensed"` | One of `"full"` \| `"condensed"` \| `"minimal"` \| `"none"`. `"none"` saves ~750 tokens for API models that already follow the document-tag contract. The shipped value (`helix.toml:64`). |
| `legibility_enabled` | bool | `true` | Sprint-1 legibility pack — emits a one-line metadata header per document in `expressed_context` (fired tiers, confidence marker, short `gene_id`, raw → compressed char count). Flip to `false` to restore the pre-Sprint-1 plain-dividers format for bench A/B. The shipped value (`helix.toml:70`). |
| `session_delivery_enabled` | bool | `true` | Sprint-2 working-set register. Tracks delivered documents per session in `session_delivery_log`; re-retrievals replace document bodies with a pointer stub, eliding token cost on repeats. Synthetic `session_id` fallback (see `[session]`) covers callers that don't supply one. Flip to `false` to restore pre-MVP dark behavior. The shipped value (`helix.toml:77`). |
| `abstain_enabled` | bool | `true` | Confidence-gated context attachment (ABSTAIN tier). When `true`, `build_context` returns a marker-only `ContextWindow` when post-refinement retrieval is weak on **both** absolute score (`top_score < 2.5`) **and** ratio (`top_score / mean < 1.8`). Skips the 12K-token BROAD fallback so the chat model answers from weights instead of digesting irrelevant noise. `HELIX_ABSTAIN_DISABLE=1` env var forces off without redeploy. Stage-4 (`[abstain].mode = "per_classifier"`) replaces the hard-coded floors with per-class values. The shipped value (`helix.toml:87`). |
| `foveated_enabled` | bool | `false` | Foveated-splice schedule for the BROAD branch. When `true`, the BROAD branch replaces uniform per-document compression with a rank-scaled power-law schedule and reverses assembly order so the top-ranked document lands immediately before the user query. Off by default through the Phase-2 measurement window. The shipped value (`helix.toml:94`). |
| `foveated_alpha` | float | `1.0` | Power-law exponent for `c_i = max(c_min, c_max · i^(-α))`. `α = 0.5` = gentle decay, `α = 1.0` = harmonic-ish (default), `α = 2.0` = aggressive top-bias. **Stage 4 override:** when `[abstain].mode = "per_classifier"`, this knob is bypassed in favour of `[abstain.<cls>].foveated_alpha` via `HelixContextManager._alpha_for_cls(cls)` (Stage-4 spec §7). Window metadata records `foveated_alpha_source: "per_classifier:<cls>"` for telemetry. The shipped value (`helix.toml:99`). |
| `foveated_c_min` | float | `0.15` | Rank-N (bottom of BROAD) compression floor. Each document's effective char cap is `max(foveated_c_min · base, c_i · base)` where `c_i = c_max · i^(-α)`. The shipped value (`helix.toml:103`). |
| `foveated_base_chars` | int | `1000` | Per-document char-budget multiplier. `target_chars per gene = int(c_i · foveated_base_chars)`. Default 1000 matches current uniform Step-4 behavior at `c_i = 1.0`. The shipped value (`helix.toml:107`). |
| `slate_char_budget` | int | `1500` | Stage-5 (2026-05-08) char budget for the `small_moe` JSON answer slate. Counts the rendered string the model actually sees, including the `<helix:slate>...</helix:slate>` wrapper, JSON braces, quotes, commas, and per-KV separators. Generic and frontier branches do not consult this knob. Code default in `helix_context/config.py:139`. |
| `mode` | str | `"token"` (Headroom only) | **NOTE:** despite appearing in the task spec, `mode` is **not** a `[budget]` key. The token-vs-cache mode lives under `[headroom] mode` (`helix.toml:168`). Documented under `[headroom]` below. |

**Example.**

```toml
[budget]
ribosome_tokens = 3000
expression_tokens = 12000
max_genes_per_turn = 12
max_fingerprints_per_turn = 40
splice_aggressiveness = 0.3
decoder_mode = "condensed"
legibility_enabled = true
session_delivery_enabled = true
abstain_enabled = true
foveated_enabled = false
foveated_alpha = 1.0
foveated_c_min = 0.15
foveated_base_chars = 1000
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

| Key | Type | Default | Effect |
|---|---|---|---|
| `default_party_id` | str | `"default"` | Attribution fallback when the request omits `party_id` and no `HELIX_PARTY_ID` env var or header is set. The shipped value (`helix.toml:117`). |
| `synthetic_session_window_s` | int | `300` | Time window (seconds) over which same-IP requests are grouped into one synthetic session. 5 minutes by default. The shipped value (`helix.toml:118`). |
| `synthetic_session_enabled` | bool | `true` | Master switch for synthetic-session attribution. Flip to `false` to restore the prior NULL-session behavior (not recommended — re-introduces the always-Bucket-A bug). The shipped value (`helix.toml:119`). |

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

| Key | Type | Default | Effect |
|---|---|---|---|
| `path` | str | `"genome.db"` (code default) / `"genomes/main/genome.db"` (shipped) | SQLite database path for the primary (master) knowledge store. Override via `HELIX_GENOME_PATH` env var (loader honors this even when the section is absent — `helix_context/config.py:611-612`). The shipped value (`helix.toml:127`). |
| `compact_interval` | float | `3600.0` | Seconds between source-change checks. Hourly default. Compaction only detects source-file changes (mtime vs `last_accessed`); irrelevant data is handled by splice (intron / exon) at retrieval time. The shipped value (`helix.toml:128`). |
| `cold_start_threshold` | int | `10` | Documents needed before history stripping kicks in (Fix 3). Below this count, the system retains all retrieval history to avoid pathological cold-start behavior. The shipped value (`helix.toml:129`). |
| `replicas` | array<str> | `[]` | Read-only clone paths for the replica fan-out. Empty during phase-1 cutover; will re-enable alongside the shard router in phase 2 (sharding changes the persistence shape). The shipped value (`helix.toml:132`). |
| `replica_sync_interval` | int | `100` | Sync replicas every N inserts. The shipped value (`helix.toml:133`). |

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

| Key | Type | Default | Effect |
|---|---|---|---|
| `host` | str | `"127.0.0.1"` | HTTP bind address. The shipped value (`helix.toml:139`). |
| `port` | int | `11437` | HTTP listen port. The shipped value (`helix.toml:140`). |
| `upstream` | str | `"http://localhost:11434"` | Chat upstream URL (OpenAI-compatible). Default points at Ollama. Override via `HELIX_SERVER_UPSTREAM` env var (`helix_context/config.py:616-617`). The shipped value (`helix.toml:141`). |
| `upstream_timeout` | float | `180.0` | Per-request timeout (seconds) for proxied requests to the chat upstream. Bumped from 120s on 2026-05-02 — observed Proxy-500s on slow `gemma4:e4b` GPQA queries at ~125s; 180s gives long-tail generation room without letting truly stuck requests hang. Override via `HELIX_SERVER_UPSTREAM_TIMEOUT` env var. Code default in `helix_context/config.py:179`. Commented in `helix.toml:142`. |

**Example.**

```toml
[server]
host = "127.0.0.1"
port = 11437
upstream = "http://localhost:11434"
# upstream_timeout = 180
```

**Cross-refs.** `helix_context/config.py:174-179` (`ServerConfig`),
env overrides at `helix_context/config.py:614-625`.

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

| Key | Type | Default | Effect |
|---|---|---|---|
| `enabled` | bool | `false` (code) / `true` (shipped) | Master switch. `false` = launcher does nothing. The shipped value (`helix.toml:164`). |
| `autostart` | bool | `true` | When `enabled`: adopt if running, spawn if not. Set `false` for menu-only mode. The shipped value (`helix.toml:165`). |
| `host` | str | `"127.0.0.1"` | Headroom bind address. The shipped value (`helix.toml:166`). |
| `port` | int | `8787` | Headroom proxy port. The shipped value (`helix.toml:167`). |
| `mode` | str | `"token"` | One of `"token"` (compression-first) or `"cache"` (prefix-cache-stable). Passed through as `--mode` to the Headroom child. The shipped value (`helix.toml:168`). |
| `dashboard_path` | str | `"/dashboard"` | URL path appended to `http://{host}:{port}` for the tray-menu link. The shipped value (`helix.toml:169`). |

**Example.**

```toml
[headroom]
enabled = true
autostart = true
host = "127.0.0.1"
port = 8787
mode = "token"
dashboard_path = "/dashboard"
```

**Cross-refs.** `helix_context/config.py:413-429` (`HeadroomConfig`).

---

## `[ingestion]`

**Purpose.** Selects the encoder backend that turns raw content into
documents during `POST /ingest`. The phased rollout (Phase 2 SPLADE,
Phase 3 cross-encoder rerank, Phase 4 ColBERT, Phase 5 entity-graph)
is governed by per-feature flags in this section.

**Keys.**

| Key | Type | Default | Effect |
|---|---|---|---|
| `backend` | str | `"ollama"` (code) / `"cpu"` (shipped) | Encoder backend. One of `"ollama"` (LLM, slow), `"cpu"` (spaCy + regex, fast), `"hybrid"`. The shipped value (`helix.toml:172`). |
| `splade_enabled` | bool | `false` (code) / `true` (shipped) | Phase-2 SPLADE sparse expansion at index time. Adds expanded sparse vectors that feed Tier-FTS5 / SPLADE retrieval at query time. The shipped value (`helix.toml:173`). |
| `rerank_model` | str | `""` (code) / `"cross-encoder/ms-marco-MiniLM-L-6-v2"` (shipped) | Phase-3 pretrained cross-encoder HF model ID. The shipped value (`helix.toml:174`). |
| `rerank_enabled` | bool | `false` | Phase-3 cross-encoder reranking on the post-shortlist candidate set. The shipped value (`helix.toml:175`). |
| `colbert_enabled` | bool | `false` | Phase-4 ColBERT late-interaction tier (optional). The shipped value (`helix.toml:176`). |
| `entity_graph` | bool | `false` (code) / `true` (shipped) | Phase-5 entity-based co-activation links. Index-time flag — gates the construction of the `entity_co_activation` table consumed by `[retrieval] entity_graph_retrieval_enabled` at query time. The shipped value (`helix.toml:177`). |

**Example.**

```toml
[ingestion]
backend = "cpu"
splade_enabled = true
rerank_model = "cross-encoder/ms-marco-MiniLM-L-6-v2"
rerank_enabled = false
colbert_enabled = false
entity_graph = true
```

**Cross-refs.** `helix_context/config.py:182-190`
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

| Key | Type | Default | Effect |
|---|---|---|---|
| `cold_tier_enabled` | bool | `false` | Master opt-in for cold-tier fallthrough. The shipped value (`helix.toml:195`). |
| `cold_tier_min_hot_genes` | int | `0` | Fall through to cold-tier when hot returns at most this many documents. `0` = only on empty hot results. The shipped value (`helix.toml:196`). |
| `cold_tier_k` | int | `3` | Maximum cold-tier documents to retrieve per query. The shipped value (`helix.toml:197`). |
| `cold_tier_min_cosine` | float | `0.15` | SEMA cosine floor (sparse 20-dim — see `Genome.query_cold_tier`). Tuning matters: too high (e.g., `0.25`) means cold-tier returns nothing on real NIAH queries. `0.15` is calibrated to surface matches in the noise floor. The shipped value (`helix.toml:198`). |
| `fingerprint_mode_profile` | str | `"balanced"` | One of `"fast"` \| `"balanced"` \| `"quality"` for `POST /fingerprint` and `POST /debug/preview`. The shipped value (`helix.toml:199`). Lower-cased by the loader (`helix_context/config.py:649`). |

**Example.**

```toml
[context]
cold_tier_enabled = false
cold_tier_min_hot_genes = 0
cold_tier_k = 3
cold_tier_min_cosine = 0.15
fingerprint_mode_profile = "balanced"
```

**Cross-refs.** `helix_context/config.py:193-206`
(`ContextConfig`), `Genome.query_cold_tier` (consumer).

---

## `[cymatics]`

**Purpose.** Frequency-domain re-rank + splice. CPU math that
replaces LLM splice calls with spectral analysis over per-document
fingerprint vectors. Blends as a bonus (max 0.5), not a primary
ranker.

**Keys.**

| Key | Type | Default | Effect |
|---|---|---|---|
| `enabled` | bool | `true` | Master switch. The shipped value (`helix.toml:202`). |
| `n_bins` | int | `256` | Spectrum resolution (256 bins = <2KB per spectrum). The shipped value (`helix.toml:203`). |
| `peak_width` | float | `3.0` | Gaussian peak width. Overridden at runtime by Q-factor derived from `[budget] splice_aggressiveness`. The shipped value (`helix.toml:204`). |
| `splice_threshold_scale` | float | `0.7` | Maps `[budget] splice_aggressiveness` (0-1) to the spectral resonance threshold. Code default only (`helix_context/config.py:215`). |
| `use_embeddings` | bool | `false` | Use `Gene.embedding` when populated (requires `sentence-transformers`). The shipped value (`helix.toml:205`). |
| `harmonic_links` | bool | `true` | Compute weighted co-activation edges between retrieved documents (feeds Tier-5 harmonic boost). The shipped value (`helix.toml:206`). |
| `distance_metric` | str | `"cosine"` | One of `"cosine"` (weighted dot) or `"w1"` (Werman 1986 circular Wasserstein-1; Singh 2020 CMD). Lower-cased by the loader (`helix_context/config.py:663`). The shipped value (`helix.toml:207`). |

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

**Cross-refs.** `helix_context/config.py:209-218`
(`CymaticsConfig`).

---

## `[classifier]`

**Purpose.** Upstream rule-based query classifier and injection
router. Contributes a decoder-mode hint and an assembly-stage
document-count cap to `build_context()`. Drives the per-classifier
abstain-floor lookup when `[abstain].mode = "per_classifier"`.

**Keys.**

| Key | Type | Default | Effect |
|---|---|---|---|
| `enabled` | bool | `true` | Master switch. When `false`, the classifier path is bypassed and `build_context` falls back to the global `[budget]` knobs. The shipped value (`helix.toml:214`). |

**Example.**

```toml
[classifier]
enabled = true
```

**Cross-refs.** `helix_context/config.py:376-384`
(`ClassifierConfig`),
`docs/archive/specs/2026-04-29-query-classifier-injection-router-design.md`,
`[abstain]` (per-classifier floors keyed by classifier output).

---

## `[retrieval]`

**Purpose.** The big one. Configures every recall and rerank tier in
`Genome.query_genes()` and the new Stage-2 / Stage-3 / Stage-4 paths
(dense recall, RRF fusion, margin-over-random ANN threshold). Every
RRF tier weight is a post-multiplier applied to `1 / (k + rank)`; the
weights below preserve the implicit weights baked into the legacy
additive accumulator so `fusion_mode = "rrf"` is a clean rank-fusion
swap, not a re-tuning.

The full Stage-3 tier inventory (which tiers participate in RRF and
which stay additive) lives in
`docs/specs/2026-05-08-stage-3-rrf-fusion.md` §3.

**Keys.**

| Key | Type | Default | Effect |
|---|---|---|---|
| `sr_enabled` | bool | `false` (code) / `true` (shipped) | Tier-5.5 Successor Representation (Stachenfeld 2017). γ-discounted future-occupancy boost over the co-activation graph. Lazy on-demand SR rows via truncated power series. The shipped value (`helix.toml:219`). |
| `sr_gamma` | float | `0.85` | Discount factor (5-10 hop horizon at 0.9). The shipped value (`helix.toml:220`). |
| `sr_k_steps` | int | `4` | Power-series truncation depth (caps runaway propagation). The shipped value (`helix.toml:221`). |
| `sr_weight` | float | `1.5` | Per-document SR contribution multiplier. Reused as the Stage-3 RRF post-multiplier for the `sr` tier. The shipped value (`helix.toml:222`). |
| `sr_cap` | float | `3.0` | Maximum per-document SR boost (matches harmonic cap). The shipped value (`helix.toml:223`). |
| `ray_trace_theta` | bool | `false` | Theta alternation (Wang/Foster/Pfeiffer 2020). Fore/aft ray-trace sampling biased by the current TCM velocity vector. Dark ship — requires TCM velocity input (Sprint-3 item). The shipped value (`helix.toml:226`). |
| `theta_weight` | float | `1.0` | Softmax temperature on `v · gene_input_vector`. The shipped value (`helix.toml:227`). |
| `seeded_edges_enabled` | bool | `false` | Sprint-4 seeded co-activation edges with Hebbian evidence decay. Three-class edge provenance (`seeded` / `co_retrieved` / `cwola_validated`) with Laplace-smoothed `co_count` vs `miss_count` per edge. Dark ship — flip to start evidence accumulation. The shipped value (`helix.toml:229`). |
| `seeded_edge_weight` | float | `1.0` | Base weight stamped at seed insertion. The shipped value (`helix.toml:230`). |
| `filename_anchor_enabled` | bool | `false` (code) / `true` (shipped) | Tier-0.5 filename-anchor (2026-04-15 Dewey-pivot spike). Boosts documents whose `filename_stem` matches a query term. Dewey bench showed filename alone outperforms the full project + module + filename bag by 24pp. Override: `HELIX_FILENAME_ANCHOR_ENABLED=1`. The shipped value (`helix.toml:235`). |
| `filename_anchor_weight` | float | `4.0` | Per-match boost. Tier-1 exact-tag is `3.0` for reference; this tier intentionally outranks it. Reused as the Stage-3 RRF post-multiplier. The shipped value (`helix.toml:236`). |
| `bm25_shortlist_enabled` | bool | `false` (code) / `true` (shipped) | BM25 shortlist post-filter (2026-04-22, research-review Pareto move 1). When enabled, `query_genes` restricts its final ranking to documents that cleared a BM25 / FTS5 top-N pass — other tiers still accumulate scores, but candidates BM25 would never surface are dropped before the sort. Post-filter by design (isolates the ranking-set hypothesis from the candidate-generation optimisation). The shipped value (`helix.toml:240`). |
| `bm25_shortlist_size` | int | `50` | BM25 top-N kept in the final ranking. The shipped value (`helix.toml:241`). |
| `bm25_prefilter_enabled` | bool | `false` | BM25 pre-filter — fires **before** tier scoring (vs the post-filter shortlist). Enable for A/B against `bm25_shortlist`; disable shortlist when using this. The shipped value (`helix.toml:244`). |
| `bm25_prefilter_size` | int | `200` | BM25 top-N fed into tier scoring. The shipped value (`helix.toml:245`). |
| `entity_graph_retrieval_enabled` | bool | `false` | Tier-5b entity graph co-occurrence boost (Step 3C, 2026-05-08). Documents sharing entity nodes with query terms get a score boost proportional to entity overlap. Requires `[ingestion] entity_graph = true` at index time. The shipped value (`helix.toml:249`). |
| `dense_embedding_enabled` | bool | `false` | Step-4 BGE-M3 dense vectors (2026-05-08). Master switch for the Stage-2 dense recall path. The shipped value (`helix.toml:255`). |
| `dense_embedding_dim` | int | `1024` | BGE-M3 Matryoshka dim. **Stage 2 (2026-05-08): default raised from 256 → 1024.** `dim = 256` collapsed random-pair cosine to ~0.6, sabotaging absolute-threshold semantics. Stage 4 will recalibrate `ann_similarity_threshold` at 1024-d. Codec emits a one-time WARN when `dim not in (1024, 768, 512)`. The shipped value (`helix.toml:256`). |
| `ann_similarity_threshold` | float | `0.35` | Legacy / fallback ANN cosine threshold. Used when `ann_threshold_mode = "absolute"` (default), and as the fallback when `mode = "margin_over_random"` and the calibration row is missing (one-time WARN). The shipped value (`helix.toml:257`). |
| `ann_threshold_min_genes` | int | `1` | Floor on the ANN dynamic-cut count (always return at least this many). The shipped value (`helix.toml:258`). |
| `ann_threshold_max_genes` | int | `12` | Ceiling on the ANN dynamic-cut count (the final cut, not the recall pool — see `dense_pool_size`). The shipped value (`helix.toml:259`). |
| `ann_threshold_mode` | str | `"absolute"` | **NEW (Stage 4, 2026-05-08).** One of `"absolute"` (default — keeps Stage-3 behavior byte-for-byte: `query_genes_ann` uses `ann_similarity_threshold`) or `"margin_over_random"` (reads the persisted threshold from the `genome_calibration` table populated by `scripts/calibrate_thresholds.py`; falls back to `ann_similarity_threshold` with a one-time WARN when the row is missing). The shipped value (`helix.toml:267`). Spec: `docs/specs/2026-05-08-stage-4-threshold-calibration.md` §3 + §6. |
| `ann_threshold_sigma_multiplier` | float | `3.0` | **NEW (Stage 4).** `μ + N·σ` over random pairs. Only consulted when `ann_threshold_mode = "margin_over_random"`. The shipped value (`helix.toml:268`). |
| `dense_pool_size` | int | `500` | **NEW (Stage 2, 2026-05-08).** Dense recall pool width — decoupled from `ann_threshold_max_genes` (the final cut). 500 hits ~3% of an 18.9k-corpus per spec §4. Resolves at the top of `query_genes_ann` via `pool_size = pool_size or self._dense_pool_size` (Stage-2 spec §6). When dense is disabled, falls back to `max_genes` for back-compat. The shipped value (`helix.toml:269`). |
| `fusion_mode` | str | `"additive"` | **NEW (Stage 3, 2026-05-08).** One of `"additive"` (default for one release — legacy `gene_scores += tier_score` accumulator path) or `"rrf"` (Reciprocal Rank Fusion via `helix_context/fusion.py:Fuser`; final sort uses fused scores). Per-tier weights below are RRF post-multipliers. Spec: `docs/specs/2026-05-08-stage-3-rrf-fusion.md`. The shipped value (`helix.toml:278`). |
| `rrf_k` | int | `60` | Cormack 2009 default. Used in `score(d) = Σ weight_t · 1/(k + rank_t(d))`. The shipped value (`helix.toml:279`). |
| `fts5_weight` | float | `3.0` | RRF post-multiplier for the `fts5` tier. Preserves the current FTS5 implicit cap. The shipped value (`helix.toml:280`). |
| `splade_weight` | float | `3.5` | RRF post-multiplier for the `splade` tier. Preserves the current SPLADE cap. The shipped value (`helix.toml:281`). |
| `tag_exact_weight` | float | `3.0` | RRF post-multiplier for `tag_exact` (Tier-1 weight × match_count). The shipped value (`helix.toml:282`). |
| `tag_prefix_weight` | float | `1.5` | RRF post-multiplier for `tag_prefix` (Tier-2 weight × match_count). The shipped value (`helix.toml:283`). |
| `sema_cold_weight` | float | `3.0` | RRF post-multiplier for `sema_cold` (cold-start `sim · 3.0` multiplier). The shipped value (`helix.toml:284`). |
| `lex_anchor_weight` | float | `1.5` | RRF post-multiplier for `lex_anchor` (current `idf · 1.5`, capped at 3.0). The shipped value (`helix.toml:285`). |
| `harmonic_weight` | float | `1.0` | RRF post-multiplier for `harmonic` (per-link weight, cap stays at 3.0). The shipped value (`helix.toml:286`). |
| `entity_graph_weight` | float | `0.5` | RRF post-multiplier for `entity_graph` (Tier-5b implicit `1.0 · 0.5`). The shipped value (`helix.toml:287`). |
| `dense_weight` | float | `1.0` | RRF post-multiplier for the Stage-2 `dense` tier. The shipped value (`helix.toml:288`). |
| `pki_weight` | float | `1.0` | RRF post-multiplier for the path-key-index (`pki`) tier. The shipped value (`helix.toml:289`). |

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
| `harmonic` | yes | `harmonic_weight` (cap stays at 3.0) |
| `sr` (Successor Repr.) | yes | `sr_weight` (reused) |
| `entity_graph` | yes | `entity_graph_weight` |
| `dense` (Stage 2) | yes | `dense_weight` |
| `sema_boost` (gate-only re-rank) | **no** — tiebreaker, applied AFTER RRF | n/a |
| `authority_*` (source/domain/recency) | **no** — flat boost on existing pool | n/a |
| `party_attr` | **no** — flat additive AFTER RRF | n/a |
| `access_rate` | **no** — explicit tiebreaker AFTER RRF | n/a |

**Example.**

```toml
[retrieval]
# Tier 5.5 Successor Representation
sr_enabled = true
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
# Tier 5b entity graph (dark)
entity_graph_retrieval_enabled = false
# Stage 2 dense recall (dark master)
dense_embedding_enabled = false
dense_embedding_dim = 1024
ann_similarity_threshold = 0.35
ann_threshold_min_genes = 1
ann_threshold_max_genes = 12
# Stage 4 threshold mode
ann_threshold_mode = "absolute"
ann_threshold_sigma_multiplier = 3.0
dense_pool_size = 500
# Stage 3 RRF fusion
fusion_mode = "additive"
rrf_k = 60
fts5_weight = 3.0
splade_weight = 3.5
tag_exact_weight = 3.0
tag_prefix_weight = 1.5
sema_cold_weight = 3.0
lex_anchor_weight = 1.5
harmonic_weight = 1.0
entity_graph_weight = 0.5
dense_weight = 1.0
pki_weight = 1.0
```

**Migration notes.**

- `fusion_mode` stays `"additive"` for one release. Per
  `docs/specs/2026-05-08-stage-3-rrf-fusion.md` §7 deprecation
  timeline: `v(N)` ships additive default; `v(N+1)` flips to `"rrf"`
  default; `v(N+2)` removes the additive code path.
- Flipping `fusion_mode` to `"rrf"` requires `[abstain].mode =
  "per_classifier"` for the floor-driven gates to take effect under
  RRF score scales. See Stage-3 spec §9 (transitional bypass) — the
  global hard-coded floors (`5.0` / `2.5`) were calibrated against
  additive scores and become unreachable post-RRF.
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

| Key | Type | Default | Effect |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. Dark by default; bench before flipping. The shipped value (`helix.toml:308`). |
| `model_path` | str | `"training/models/stacked_plr.joblib"` | Path to the trained artifact. The shipped value (`helix.toml:309`). |
| `expected_sha256` | str | `""` | Empty = trust the `.sha256` sidecar next to the artifact (written by the trainer). Set a pinned hex digest in `helix.toml` for explicit operator-level pinning; load refuses to proceed unless the artifact's digest matches. The shipped value (`helix.toml:310`). |
| `high_risk_threshold` | float | `0.5` | Symmetric default. The fuser's `prob_B` is compared against this to emit a coarse "likely-to-re-query" boolean alongside the log-odds. Tune only with bench evidence. The shipped value (`helix.toml:311`). |

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

| Key | Type | Default | Effect |
|---|---|---|---|
| `emit_floor` | float | `0.55` | Probability floor below which a KnowBlock is not emitted; falls through to `MissBlock(reason="sparse")`. The shipped value (`helix.toml:340`). |
| `s_ref` | float | `1.0` | Feature-scale reference for `tanh(top_score / s_ref)`. Pick `s_ref = median(top_score)` from the calibration set so `tanh(...)` saturates around the typical scale (Stage-6 spec §11). The shipped value (`helix.toml:341`). |
| `g_ref` | float | `0.5` | Feature-scale reference for `tanh(score_gap / g_ref)`. Pick `g_ref = median(score_gap)` from the calibration set. The shipped value (`helix.toml:342`). |
| `betas` | array<float> | `[-2.0, 2.0, 1.5, 0.7, 1.8, 1.5]` (code default; 6 coefficients with Stage-7 β5) | Logistic coefficients. Order: `[b0_intercept, b1_top, b2_gap, b3_agree, b4_coord, b5_freshness]`. The shipped `helix.toml:343` value `[-2.0, 2.0, 1.5, 0.7, 1.8]` has only 5 entries (pre-Stage-7 length); the loader emits a `WARNING` and falls back to the 6-element default when length mismatches `1 + N_FEATURES = 6` (`helix_context/know_calibration.py:152-160`). **Operators running Stage 7 must add the 6th coefficient (`+1.5`) or flip to defaults**. Stage 7 added β5 (freshness_min coefficient); calibration script auto-detects the feature count. |
| `calibrated_at` | str (ISO-8601) | `None` (optional) | Timestamp written by `scripts/calibrate_know_confidence.py` after a fresh calibration run. Optional; `None` means the betas are SHIP-TIME defaults. Not in the shipped `helix.toml`. |
| `calibrated_on_n` | int | `None` (optional) | Sample count from the calibration set. Written by `scripts/calibrate_know_confidence.py`. Not in the shipped `helix.toml`. |

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

**Migration notes.** The shipped `helix.toml:343` value
`[-2.0, 2.0, 1.5, 0.7, 1.8]` is 5-element (pre-Stage-7 length).
After Stage 7, the loader expects 6 entries
(`1 + N_FEATURES = 1 + 5`). Operators should either re-run the
calibration script to refresh `[know]` or manually append the Stage-7
default `+1.5` to the array.

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
| `watch_dirs` | array<str> | `["~/.claude/projects/f--Projects-Education/memory"]` | Directories to watch. `~` is expanded automatically. Add more as needed. The shipped value (`helix.toml:353-355`). |

**Example.**

```toml
[mem_sync]
enabled = false
helix_url = "http://127.0.0.1:11437"
sync_interval_s = 60
agent_kind = "claude-code"
watch_dirs = [
    "~/.claude/projects/f--Projects-Education/memory",
]
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

| Key | Type | Default | Effect |
|---|---|---|---|
| `mode` | str | `"global"` | One of `"global"` (legacy hard-coded floors) or `"per_classifier"` (consult sub-tables). Invalid values fall back to `"global"` with a `WARNING` (`helix_context/config.py:726-730`). The shipped value (`helix.toml:432`). |

**Sub-tables.** `[abstain.factual]`, `[abstain.multi_hop]`,
`[abstain.arithmetic]`, `[abstain.procedural]`, `[abstain.default]`.
Each takes the same four keys (calibrated from `located_n1000.json`
score distributions per Stage-4 spec §4):

| Sub-table key | Type | Default | Effect |
|---|---|---|---|
| `abstain_top` | float | `2.5` | p85 of MISS scores per class — `top_score < abstain_top` triggers the ABSTAIN tier. The cheap-to-be-wrong floor (re-engages BROAD on false-abstain). |
| `focused_top` | float | `2.5` | p25 of HIT scores per class — `top_score >= focused_top` enters the FOCUSED tier (with ratio gate). |
| `tight_top` | float | `5.0` | p60 of HIT scores per class — `top_score >= tight_top` enters the TIGHT tier (with ratio gate). The expensive-to-be-wrong floor (drops 9 candidates on false-tighten). |
| `foveated_alpha` | float | `1.0` | Per-class power-law exponent. Replaces `[budget] foveated_alpha` when `[abstain].mode = "per_classifier"` via `HelixContextManager._alpha_for_cls(cls)` (Stage-4 spec §7). Window metadata records `foveated_alpha_source: "per_classifier:<cls>"` for telemetry. |

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

| Key | Type | Default | Effect |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. |
| `path` | str | `"~/.helix/vault"` | Vault root. `~` is expanded automatically. |
| `party_id` | str | `""` | Empty = use the server's primary party. |
| `fan_out_threshold` | int | `5000` | Split domain folders above this document count. |
| `redact_body` | bool | `false` | Replace document body with `sha + excerpt`. Recommended for cloud-synced setups. |
| `stale_threshold` | float | `0.5` | Documents with `live_truth_score < this` go to `_stale/`. |

**`[vault.traces]` sub-table.**

| Key | Type | Default | Effect |
|---|---|---|---|
| `enabled` | bool | `true` | Auto-export every `/context` call. |
| `retention_hours` | int | `48` | Default retention. Set ≥720 for 30-day audit. |
| `max_retention_hours_hard` | int | `720` | Force-deletes pinned past this. `0` disables. |
| `max_count` | int | `10000` | Safety cap on burst floods (v1.1: not yet enforced). |
| `rollup_enabled` | bool | `true` | Roll up traces by shard. |
| `rollup_shard` | str | `"hour"` | One of `"hour"` \| `"daily"`. |
| `prune_interval_minutes` | int | `60` | Prune cadence. |
| `trigger_only` | bool | `false` | Emit only on threshold (v1.1: not yet enforced). |

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
# during idle, tighter complements, cross-gene pattern noticing with
# a larger model — separate subsystem against the same genome, not a
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
# Default "additive" is byte-identical to pre-Stage-3 behavior. Flip to
# "rrf" to enable the new path. Per-tier weights below are RRF
# post-multipliers (preserve current implicit weights).
# Deprecation timeline: v(N) ships additive default; v(N+1) flips to rrf
# default; v(N+2) removes the additive code path.
fusion_mode = "additive"                # "additive" | "rrf"
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
