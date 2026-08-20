# Fork 1 slice 1 — encoder daemon + RemoteCodec: binding design contract

Companion to `2026-08-05-fork1-encoder-daemon-kickoff.md`. This file fixes the
API and semantics that the daemon, the client, and the tests are written
against. Deviations require editing this doc first.

## Components

- `cymatix_context/encoder_daemon.py` — FastAPI app factory
  `create_encoder_app(...)` + `main()` (`python -m cymatix_context.encoder_daemon`).
  One process owns BGE-M3 + SPLADE + SEMA. Default port **11439** (11437 =
  backend, 11491-11493 = bench harnesses; the daemon gets its own constant).
- `cymatix_context/backends/encoder_client.py` — URL resolution, stdlib-urllib
  transport (explicit `timeout=` on every call), circuit breaker, and the
  remote codec classes. Never imports torch/transformers/sentence_transformers
  at module import; never constructs local models except on fallback.

## HTTP API (JSON over localhost)

| Endpoint | Body | Response |
|---|---|---|
| `POST /encode/dense` | `{"texts": [...], "task": "query"\|"passage"}` | `{"vectors": [[f32 ...]], "dim": N, "model": name}` |
| `POST /encode/splade` | `{"texts": [...], "top_k": 128}` | `{"sparse": [{token: weight}, ...]}` |
| `POST /encode/sema` | `{"texts": [...]}` | `{"vectors": [[20 x f32]]}` |
| `POST /encode/bundle` | `{"text": str, "task": "query", "top_k": 128}` | `{"dense": [...], "splade": {...}, "sema": [...]}` |
| `GET /ready` | — | 200 `{"ready": true, "spawn_token": tok, "probe_ms": ...}` only after all models loaded AND one probe bundle round-tripped; 503 `{"ready": false, "state": ...}` before |
| `GET /health` | — | 200 always once HTTP is up: `{"status", "ready", "pid", "spawn_token", "capacity": {...}}` |

- Spawn-token identity: daemon echoes `CYMATIX_ENCODER_SPAWN_TOKEN` (env) in
  `/ready` and `/health` — same rationale as `CYMATIX_BENCH_SPAWN_TOKEN` in
  `BenchServer._wait_healthy` (Windows venv shims re-exec python; pid is not
  authoritative).
- Text caps are applied by CALLERS (sema `[:1000]`, dense
  `[:dense_passage_char_cap]`, splade `[:splade_content_cap]`) — the daemon
  encodes exactly what it receives.

## Byte-parity rules (the load-bearing part)

1. **Dense**: daemon uses `BGEM3Codec.encode_batch` (documented byte-identical
   to sequential `encode()`; same backend-selection order FlagEmbedding →
   sentence-transformers as the client host). Query prefix is applied by the
   daemon via the real codec's `task=` handling — clients send raw text +
   task, never pre-prefixed.
2. **SPLADE**: daemon executes micro-batches as a **sequential per-item
   `splade_backend.encode()` loop** — `encode_batch`'s `padding=True` changes
   max-pool bytes vs today's query path (`padding=False`). Weights stay
   `round(w, 4)`, dict order stays descending-weight; JSON round-trips both.
3. **SEMA**: same per-item-loop rule (single-item `SemaCodec.encode`
   semantics). Daemon imports `PRIMES` / `SemaCodec` from
   `cymatix_context.backends.sema` — identical anchors and projection.
4. Bars still clear with per-item splade/sema: CPU min(18.6, 18.8, 139) ≈ 18.6
   bundles/s ≥ 3.5; GPU min(421.6, ~135, 155) ≈ 135 ≥ 30. BGE batching is
   where the win lives and is the only place batching is byte-sanctioned.

## Micro-batcher

- One queue + worker thread per model family. Requests enqueue
  `(payload, Future)`; worker drains up to `max_batch` within a window.
- Window default **2 ms** (`CYMATIX_ENCODER_BATCH_WINDOW_MS`). Lowered from
  the original 8 ms sane-range guess per 2026-08-05 dogfood A/B receipts: 8ms
  cost +4pp on the c=1 latency gate with no batching benefit at any measured
  concurrency, because coalescing under load comes from queue backpressure
  while the worker encodes, not from the idle window.
- `max_batch`: `[hardware] batch_sizes = { dense = N, ... }` override first,
  then `recommended_batch_size(model)`, then daemon defaults dense=16,
  splade=8, sema=64 (the `_BATCH_TABLE` has no dense/sema rows — floor 1 is
  not a real cap).
- `/encode/bundle` fans one text into all three queues concurrently and joins.

## Daemon startup sequence (order is load-bearing)

1. `cfg = load_config()`; 2. `hardware.init_from_config(...)` with the four
`[hardware]` layer knobs — BEFORE anything touches `get_hardware()`;
3. construct codecs with explicit resolved devices
(`get_shared_codec(..., device=resolve_layer_device("dense"))`; SPLADE and
SEMA self-resolve); 4. warm-load all models eagerly (no lazy in the daemon);
5. run one probe bundle; 6. flip ready; 7. log the capacity self-report.

Capacity self-report (logged + `/health.capacity`): per-layer resolved device,
warm enc/s per model (5-encode probe), VRAM total/free + RAM (psutil guarded
optional), implied bundles/s (`1/sum(1/rate)` interleaved and `min(rate)`
pipelined), effective batch sizes, torch version.

## Client / RemoteCodec semantics

- URL resolution precedence: env `CYMATIX_ENCODER_URL` (truthy; empty = unset)
  > value passed by the manager from `cfg.encoder_daemon.url` via
  `encoder_client.configure(url)` > "" (off). `reset_for_test()` clears module
  state.
- **Off means untouched**: URL unset ⇒ `get_shared_codec` returns a real
  `BGEM3Codec` through the same `_GLOBAL_CODECS` path, splade functions run
  their current bodies, the manager constructs `LazySemaCodec` — byte-identical
  to today, asserted by tests.
- Ready gating: before first remote use, poll `GET /ready` (budget
  `CYMATIX_ENCODER_READY_TIMEOUT_S`, default 15 s, poll 0.5 s). Not ready in
  budget ⇒ fallback (below).
- Circuit breaker: any transport failure ⇒ log **one** warning per process,
  open circuit, serve in-process; re-probe after 30 s cooldown
  (`CYMATIX_ENCODER_RETRY_COOLDOWN_S`). Device-mismatch byte drift between
  remote and fallback vectors is an accepted, documented caveat of a
  mid-run daemon outage.
- **RemoteBGEM3Codec.encode never raises** (`query_genes_dense_recall` has no
  guard). Surface: `encode(text, task="passage") -> list[float]`,
  `encode_batch(texts, task="passage")` (`[]` ⇒ `[]`, no network),
  `similarity` (local np.dot), attrs `dim`, `model_name`, `_model` (None until
  fallback loads — `/admin/components` probe reads it).
- SPLADE interception: branch at the top of `splade_backend.encode` /
  `encode_batch` before any torch import; on remote failure fall through to
  the unchanged local body (in-process re-encode, never skip).
- `RemoteSemaCodec` duck-type: `encode`, `encode_batch`,
  `similarity`/`nearest` (local numpy, pass-through statics), `signature`,
  `fingerprint` (computed locally from `encode()` + `PRIMES` metadata),
  `projection_matrix` (None — matches CountingSemaCodec fake precedent),
  `embed_dim` (384), truthful `loaded` property, `peek()`, `warm()`,
  `is_lazy_component = True`. Factory branch at context_manager.py:894-898;
  `sema_embed_on_ingest=False` still means NO codec at all (global-off
  preserved). With a URL set, `sema_available()` is not required
  (daemon owns the model).
- Manager dense-ctor leak fix rides along: `context_manager._get_dense_codec`
  goes through `get_shared_codec(..., device=resolve_layer_device("dense"))`
  instead of a private CPU-pinned `BGEM3Codec` (flagged in the write-up).
- `cymatix ingest --deterministic` (all encoder flags off) must never consult
  the client: flags gate upstream of the seams, and tests assert no network.

## Config

- `[encoder_daemon]` section: `url: str = ""` only (empty = disabled). Dataclass
  `EncoderDaemonConfig` near `HeadroomConfig`; parsed BEFORE
  `_apply_env_overrides` (config.py:1272); env override mirrors
  `CYMATIX_SERVER_UPSTREAM` truthiness style; append `CYMATIX_ENCODER_URL` to
  `_CONFIG_ENV_OVERRIDES` in tests/test_config_default_honesty.py. Timeouts /
  windows / cooldowns are env-tunable constants, not config surface (slice 1).

## Determinism profile (slice-2 addition, 2026-08-06)

Added after slice 1 shipped; not part of the slice-1 contract above. Default
OFF — with the env unset, every path in this section is byte-for-byte what
slice 1 shipped, asserted by tests.

> **Naming note (#339 rider):** `CYMATIX_ENCODER_DETERMINISTIC` (this
> profile — how a running daemon encodes) is unrelated to
> `cymatix ingest --deterministic` (the OKF deterministic-ingest profile —
> whether ingest encodes at all; see `docs/INTEGRATING_WITH_EXISTING_RAG.md`,
> "Determinism — the canonical digest"). The shared adjective is a
> coincidence.

Motivation: `docs/benchmarks/2026-08-05-fork1-worker-sweep.md`, "Rank
stability under parallelism" — on CUDA, recall deviated ±0.05 from the same
run's c=1 (overlap ≥0.975, errors 0) because the dense micro-batch **shape**
(item count × padded token length) depends on concurrent arrival order, and
batched-GEMM low-mantissa drift across shapes flips near-cutoff ranks.

| Knob | Default | Effect |
|---|---|---|
| `CYMATIX_ENCODER_DETERMINISTIC` | off | Master gate for everything in this section |
| `CYMATIX_ENCODER_TF32` | off | Re-enables TF32 matmuls inside the profile (TF32 off is part of the profile) |
| `CYMATIX_ENCODER_FILLER_CHARS` | 4096 | Filler length; ~2x the 2000-char `dense_passage_char_cap` |
| `CUBLAS_WORKSPACE_CONFIG` | `:4096:8` | Set by the daemon **only if unset** by the operator |
| `CYMATIX_ENCODER_VECTOR_CACHE` | 4096 | Daemon-side vector LRU entries; 0 = off. Independent of the gate |

1. **Fixed-shape dense batching.** The dense executor pads each drained
   task-group up to exactly `max_batch` with copies of a constant filler
   text and discards the filler rows. Both backends return rows in input
   order, so the caller rows are the first `len(items)`. Public
   `encode_batch` only — no model library is forked or monkey-patched.
   SPLADE and SEMA are untouched: per-item execution is already fixed shape
   (n=1).
2. **Torch flags**, applied in `EncoderRegistry.load` (daemon process only,
   first statement — the last point before torch builds a CUDA context):
   `use_deterministic_algorithms(True, warn_only=True)`,
   `torch.backends.cuda.matmul.allow_tf32 = False`, `CUBLAS_WORKSPACE_CONFIG`
   if unset. Every step guarded for missing/CPU-only torch.
   `torch.backends.cudnn.allow_tf32` is deliberately untouched (BGE-M3
   inference is cuBLAS matmul, not cuDNN).
3. **Vector LRU**, keyed `(family, model, task, text)` — checked before
   enqueue, populated after a successful encode, never on failure,
   thread-safe, bounded. The bundle route fans into the same queues so it
   shares the cache. The readiness probe deliberately bypasses it.
4. `/health.capacity` gains `deterministic: bool` and
   `vector_cache: {enabled, size, max_size, hits, misses}`, both re-read
   live on every request rather than frozen at the ready flip.

**What is and is not shape-pinned (honest limits).** Item count is pinned.
Padded token length is pinned only *by domination*: neither
`BGEM3FlagModel.encode` nor `SentenceTransformer.encode` exposes a fixed
pad length (both sort by length, chunk by their own `batch_size`, and
tokenize with `padding=True` = pad-to-longest; there is no
`padding="max_length"` knob), so the filler pins the length only while it is
the longest member of the chunk. Consequences: (a) a caller that sends a
passage longer than the filler silently returns that batch to a
data-dependent shape — the daemon encodes exactly what it receives and
cannot enforce caller caps; (b) if `max_batch` exceeds the backend's own
encode `batch_size` (sentence-transformers: 32), our fixed batch is split
and only the chunk holding the filler is length-pinned; (c) under
flash-attention varlen packing sentence-transformers drops padding entirely
and packs the chunk, so count stays pinned and padded length becomes moot
and unpinned. The profile therefore reduces shape variance rather than
proving bit-identity, and it costs throughput (a 2-item drain becomes a
`max_batch`-item encode of near-max-length rows) — which is why it is off by
default and why any rank-stability claim needs its own GPU receipt.

## Testing rules

- Non-live suite must never load a real model or spawn a real process:
  daemon tested via `TestClient` on `create_encoder_app` with injected fakes
  (`FakeBGEM3Codec`, monkeypatched `splade_backend.encode`, CountingSemaCodec
  pattern); client tested against threaded stdlib `HTTPServer` fixtures and
  transport monkeypatches asserting `timeout is not None`.
- Every new test file: clear `_GLOBAL_CODECS`, null splade globals, and
  `delenv CYMATIX_ENCODER_URL` (autouse) — full-suite-only failures otherwise.
- Concurrency tests: `@pytest.mark.concurrency`, liveness before correctness,
  no absolute-ms asserts anywhere.
