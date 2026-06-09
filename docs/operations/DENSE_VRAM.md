# Dense (BGE-M3 / SPLADE) Ingest on Constrained-VRAM Rigs

Operator runbook for running the dense BGE-M3 (and SPLADE) ingest path on
GPUs with **≤12 GB VRAM** without the slow-path failure mode rediscovered
during the ContextBench code-retrieval track (2026-06-07).

Companion to PR #177 / issue #176, which added the bounding mechanism
(`HELIX_DENSE_VRAM_RELEASE_EVERY`, see the v0.6.4 CHANGELOG entry); this
doc is the operator-facing matrix for *using* it.

## Failure mode in one paragraph

torch's CUDA caching allocator keeps a separate cached block per distinct
input shape, so a long-lived process that batch-encodes many
differently-sized passages — the daemon `/ingest` route,
`scripts/backfill_bgem3_v2.py`, a 100k+-file genome build — climbs to the
card's dedicated VRAM ceiling and then, on **Windows / WDDM in
particular**, the driver silently transfers allocations to *shared* GPU
memory (system RAM) rather than hard-OOMing. The run keeps going at
fraction-of-a-percent throughput and looks like a hang. Measured on a
12 GB 3080 Ti: a single-worker dense ingest of one mid-size repo climbed
to ~11.7 GB / 95% utilization, then sawtoothed there for hours. The
periodic `empty_cache()` added in #177 (`encode_batch` releases torch's
caching allocator every `HELIX_DENSE_VRAM_RELEASE_EVERY` batches) holds
the same workload at a ~6 GB plateau. Vectors are byte-identical;
`empty_cache` only frees *unused* cached blocks.

## Config matrix

Pick the row matching your rig. *N* = number of ingest workers (process
or threadpool).

### ≤12 GB VRAM (e.g. RTX 3080 Ti, 3060, 4070)

**Prefer CPU for offline batch / benchmark dense ingest** unless you have
a specific latency need. Dense vectors from CPU are byte-identical to
GPU; there is no VRAM ceiling; ingest is RAM-bound and parallelizable at
~3–4 GB RAM per worker (measured: 4 workers ≈ 28 GB resident).

| Path | Device | Workers | Required env | Notes |
|---|---|---|---|---|
| Offline batch / benchmark | **CPU** | N ≈ cores | `OMP_NUM_THREADS=4`, `MKL_NUM_THREADS=4` | The 4-thread cap per worker keeps BLAS from oversubscribing across N processes. |
| Interactive / daemon GPU | CUDA | **1 only** | `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`, `HELIX_DENSE_VRAM_RELEASE_EVERY=64` | BGE-M3 ≈ 5–6 GB per worker → only one fits in 12 GB. Lower the release-every for big-file repos (sympy / sklearn-scale) where occasional very large single-file `encode_batch` calls spike VRAM mid-task. |
| Mixed (SPLADE on GPU, dense on CPU) | dense = CPU, SPLADE = CUDA | N | `OMP_NUM_THREADS=4`, `MKL_NUM_THREADS=4` | SPLADE is small (~0.5 GB per worker); safe to keep on GPU while the heavy dense path runs on CPU. |

**Confirmed failure modes — do not repeat:**

- 2 GPU dense workers → `BrokenProcessPool` (CUDA OOM at ~11.8 GB).
- 3 GPU dense workers → multi-hour VRAM thrash / WDDM spill to shared
  system memory (the slow-path "hang"; ~2 tasks completed in 6 h).
- 1 GPU dense worker on a mid-size repo (~1.1k files) → VRAM still
  sawtooths to ~11.5 GB inside a single task even with
  `expandable_segments` + periodic `empty_cache` every 100 files,
  because occasional very large single-file `encode_batch` calls spike.
  Small repos (e.g. `requests`, ~140 files) plateau cleanly ~6 GB.

### 16–24 GB VRAM (e.g. RTX 4080 / 4090 / A4000-class)

Two GPU dense workers fit; three is still tight. Default release cadence
is appropriate.

| Path | Device | Workers | Required env | Notes |
|---|---|---|---|---|
| Daemon / batch dense ingest | CUDA | **1–2** | `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`, `HELIX_DENSE_VRAM_RELEASE_EVERY=256` (default) | Two workers ≈ 10–12 GB steady state; leave headroom for the daemon's other CUDA consumers. |
| SPLADE alongside dense | CUDA | shared | as above | SPLADE adds ~0.5 GB per worker; well within budget. |

### ≥48 GB VRAM (e.g. A6000 / L40 / H100 / dual-card rigs)

VRAM is not the binding constraint; throughput is. Default config is
correct.

| Path | Device | Workers | Required env | Notes |
|---|---|---|---|---|
| Daemon / batch dense ingest | CUDA | N ≈ cards × 2 | `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` (optional), `HELIX_DENSE_VRAM_RELEASE_EVERY=256` (default) | The bounding mechanism is still cheap (one `cudaFree` per N batched encodes) and worth leaving on. Disable with `=0` only if you can prove a measurable regression. |

## Env knobs (single place)

- `HELIX_DENSE_VRAM_RELEASE_EVERY` — release torch's CUDA caching
  allocator every N batched `encode_batch` calls inside
  `BGEM3Codec.encode_batch`. Default `256`; set `0` to disable. CPU
  path is a no-op (no CUDA cache). See PR #177 and
  `helix_context/backends/bgem3_codec.py:_vram_release_interval`.
- `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` — kills allocator
  fragmentation. On the reference 12 GB 3080 Ti this dropped the
  *starting* peak from 11.7 GB → 7.7 GB even before the periodic
  release kicked in. Cheap; recommend leaving on across all VRAM tiers.
- `HELIX_SHARE_DENSE_CODEC=1` (default) — one shared BGE-M3 codec per
  process (the A1 singleton in `bgem3_codec.py`). Keeps a multi-shard
  in-process fan-out from loading ~100 copies of the ~2 GB model. Does
  **not** help across separate worker *processes* — each process loads
  its own model.
- `OMP_NUM_THREADS=4`, `MKL_NUM_THREADS=4` — CPU dense ingest only.
  Caps each worker's BLAS thread pool so N workers don't oversubscribe.
- `transformers==4.49.0` — pin. The 5.x line breaks the `PreTrainedModel`
  import path that helix's dense and SPLADE encoders use.

## Choosing the dense-backfill path

For first-time backfill of `embedding_dense_v2` (Stage 2; see
[Operator runbooks → Runbook 1](../operator-runbooks.md#runbook-1-bge-m3-1024-dim-backfill-stage-2)):

- **≤12 GB rig:** run `scripts/backfill_bgem3_v2.py` against a snapshot
  on CPU. Wall-clock estimate ~30–90 minutes for an 18.9k-document
  store; the script is idempotent and resumable.
- **16–24 GB rig:** GPU is fine with default release cadence.
- **≥48 GB rig:** GPU; default cadence.

The script header at `scripts/backfill_bgem3_v2.py:1-34` documents the
shared backfill loop and the byte-identical guarantee against the
inline-ingest path.

## References

- **Bug / fix:** issue #176 (the failure mode) → PR #177 (the
  `empty_cache` mechanism). v0.6.4 CHANGELOG entry for the shipping
  details.
- **This runbook:** issue #178.
- **Code:** `helix_context/backends/bgem3_codec.py`
  (`_vram_release_interval`, `_maybe_release_vram`, `encode_batch`).
- **Backfill:** `scripts/backfill_bgem3_v2.py`.
- **Source of measurements:** ContextBench code-retrieval track, frozen
  v0.6.3 wheel, 2026-06-07.
