# Dense BGE-M3 Ingest on ≤12 GB Rigs

**Issue:** #178  
**Triggered by:** 2026-06-07 handoff (Laude → Raude), PR #177 / issue #176  
**Applies to:** any operator running GPU dense ingest (`[ingestion] dense_embed_on_ingest = true` or `scripts/backfill_bgem3_v2.py`) on a card with 12 GB or less of VRAM, especially where Ollama or another model server may be resident.

---

## The problem in one paragraph

BGE-M3 dense encoding on CUDA consumes roughly 5–6 GB of VRAM per ingest worker, measured on a 12 GB RTX 3080 Ti. That leaves room for exactly one worker before the card is full. Two workers trigger a `BrokenProcessPool` OOM immediately; three workers cause a multi-hour VRAM-thrash livelock on Windows (WDDM shared-memory spill, issue #176). Even a single worker is not safe to leave unattended: before PR #177 landed, `BGEM3Codec.encode_batch` never called `torch.cuda.empty_cache()`, so VRAM climbs steadily across a long corpus, reaching the card ceiling (measured 11.7 GB / 95% on a 12 GB card on one worker) and then spills into system RAM. On a rig where Ollama is already resident and holding VRAM — the typical dev setup — the real headroom is lower still.

This runbook covers the safe operating envelope and the env-var knobs that control it.

---

## Choosing your ingest mode

Three paths exist. Pick based on corpus size and available VRAM:

**CPU path — use for: corpora of any size, safety-first, Ollama resident.**  
Set `BGEM3_DEVICE=cpu` (or leave it unset on a CPU-only host). The backfill script auto-detects CUDA when `BGEM3_DEVICE` is empty or `"auto"`, so an explicit `cpu` value is necessary when you want to force the CPU path on a machine that has a GPU but whose VRAM is contended. Throughput is ~30–90 minutes for an 18.9k-document store on CPU sentence-transformers BGE-M3; no OOM risk.

**1-worker GPU path — use for: speed, 12 GB card, Ollama NOT resident.**  
Set `BGEM3_DEVICE=cuda`, one worker. Expect ~5–15 minutes for an 18.9k-document store (FlagEmbedding) or ~12 minutes for a single ContextBench django task. Peak VRAM before PR #177 reached 11.7 GB / 95% on one worker without cache release. After PR #177 merges, set `CYMATIX_DENSE_VRAM_RELEASE_EVERY` to a value between 64 and 256 to bound the within-run peak. Do not raise the worker count above 1 on a 12 GB card.

**Deferred-backfill path — use for: latency-sensitive ingest where you want no GPU involvement at ingest time.**  
Set `[ingestion] dense_embed_on_ingest = false` in `cymatix.toml`. Documents are ingested without a dense vector; the `embedding_dense_v2` column stays NULL. Run `scripts/backfill_bgem3_v2.py` separately during a maintenance window, using the CPU or 1-worker-GPU guidance above. Retrieval continues to work on the lexical/tag path; dense recall is disabled until backfill completes.

---

## Env vars and config keys

All values below have been verified against the source files indicated.

### `BGEM3_DEVICE`

Controls which device the BGE-M3 codec runs on in `scripts/backfill_bgem3_v2.py` and the inline ingest path. Device selection follows this priority order (source: `scripts/backfill_bgem3_v2.py:127–146`):

1. An explicit `BGEM3_DEVICE` env var, if set and non-empty.
2. Auto-detected CUDA when `torch.cuda.is_available()` returns true and `BGEM3_DEVICE` is empty or `"auto"`.
3. CPU otherwise.

`BGEM3Codec.__init__` defaults to `device="cpu"` (`cymatix_context/backends/bgem3_codec.py:57`), so any path that bypasses the backfill script's device-selection block lands on CPU. Setting `BGEM3_DEVICE=cpu` explicitly is the safe way to force CPU when a GPU is visible.

```powershell
# Force CPU even on a CUDA-visible machine
$env:BGEM3_DEVICE = "cpu"
python scripts/backfill_bgem3_v2.py
```

```bash
# Linux / macOS
BGEM3_DEVICE=cpu python scripts/backfill_bgem3_v2.py
```

### `CYMATIX_DENSE_VRAM_RELEASE_EVERY`

**Status: added in PR #177, not yet merged to `master` as of 2026-06-07.**

When merged, this env var adds a periodic `torch.cuda.empty_cache()` call inside `BGEM3Codec.encode_batch`, bounded to CUDA-only, byte-neutral (does not change the stored vectors). Default is 256 batches between cache releases. Lower values reduce peak VRAM at the cost of slightly more overhead; values as low as 64 are reasonable on a 12 GB card.

```powershell
# After PR #177 merges: release cache every 64 batches (more aggressive)
$env:CYMATIX_DENSE_VRAM_RELEASE_EVERY = "64"
$env:BGEM3_DEVICE = "cuda"
python scripts/backfill_bgem3_v2.py
```

Until PR #177 is merged, there is no automatic cache release on the production code path. On a 12 GB card with a large corpus, the GPU path will climb toward the card ceiling regardless of batch size. The workaround is either `BGEM3_DEVICE=cpu` or very small corpora per session with a process restart between runs.

### `CYMATIX_BFM_SPLADE` and `CYMATIX_BFM_DENSE_BACKFILL`

**Status: announced in release notes (`.qa-rel065.py`) and CLAUDE.md; not yet present in Python source as of 2026-06-07.**

These are lean-ingest kill-switches intended to prevent the multi-CUDA-context WDDM-spill livelock (issue #176) during test runs and parallel worker scenarios. When implemented, setting either to `0` will force the lean path — omitting SPLADE encoding or inline BGE-M3 backfill respectively — so that only one CUDA context is active at a time.

Until the implementation lands, the equivalent is to set in `cymatix.toml`:

```toml
[ingestion]
splade_enabled = false        # disables SPLADE — no SPLADE CUDA context
dense_embed_on_ingest = false # disables inline dense encoding at ingest time
```

Combined with `BGEM3_DEVICE=cpu` on the backfill script, this eliminates the multi-context livelock entirely.

### `[ingestion] dense_embed_on_ingest`

Config key in `cymatix.toml`, default `true` (code default: `cymatix_context/config.py:198`; cymatix.toml also sets `true`). Controls whether the inline ingest path (`context_manager.ingest`) calls the BGE-M3 codec to populate `embedding_dense_v2` at write time. Set to `false` to defer all dense encoding to the offline backfill script.

### `[ingestion] splade_enabled`

Config key in `cymatix.toml`, default `false` in code (`cymatix_context/config.py:186`), but `true` in the shipped `cymatix.toml`. SPLADE encoding loads a separate transformer model and opens a second CUDA context. On a 12 GB card with dense encoding active, two concurrent CUDA contexts (SPLADE + BGE-M3) is the trigger for the #176 livelock. Set to `false` during dense backfill.

---

## The #176 livelock: what it looks like and how to recover

**Trigger:** two or more CUDA contexts open simultaneously on a 12 GB card under Windows WDDM — typically SPLADE + BGE-M3 dense, or any combination of (SPLADE, BGE-M3 dense, Ollama) that pushes total allocation past the VRAM physical limit. WDDM spills to system RAM but does not kill the process; instead the workload enters a multi-hour thrash state where every allocation requires a VRAM eviction/reload cycle.

**Symptoms:**
- GPU task manager shows near-100% utilisation but throughput (documents/second logged by the backfill script) drops to near zero.
- VRAM "Dedicated GPU Memory" is at or above the card's physical limit; "Shared GPU Memory" (system RAM mapped into GPU address space) is growing.
- The Python process is alive and not OOM-killed, but has been running for hours with minimal progress.
- On Windows: the display may stutter or go momentarily blank as WDDM services GPU page faults.

**Recovery:**
1. Kill the hung Python process (Ctrl-C or Task Manager).
2. Restart the machine or wait for WDDM to release the reserved shared-memory pages (which can take several minutes after process exit on Windows 11).
3. Confirm VRAM is fully released before restarting: open Task Manager → Performance → GPU, verify Dedicated GPU Memory returns to baseline (Ollama resident: ~2–4 GB; nothing resident: near zero).
4. Restart using the CPU path or the 1-worker GPU path with Ollama stopped, as described below.

---

## Recommended settings for a 12 GB card with Ollama resident

This is the scenario described in CLAUDE.md (Ryzen 7, 48 GB RAM, 12 GB VRAM, Ollama often resident). Ollama holds 2–4 GB of VRAM for a loaded model. Effective headroom for dense ingest is therefore 8–10 GB, which is below the 5–6 GB/worker floor for a second worker.

Use the CPU path:

```powershell
# cymatix.toml — set once:
# [ingestion]
# dense_embed_on_ingest = false
# splade_enabled = false

# Then run the backfill on CPU:
$env:BGEM3_DEVICE = "cpu"
python scripts/backfill_bgem3_v2.py
```

If Ollama can be stopped during the backfill window and you want GPU speed:

```powershell
# Stop Ollama first (releases its VRAM reservation)
Stop-Process -Name "ollama" -Force   # or use the Ollama tray icon

# One worker, GPU
$env:BGEM3_DEVICE = "cuda"
# If PR #177 is merged, also set:
# $env:CYMATIX_DENSE_VRAM_RELEASE_EVERY = "128"

python scripts/backfill_bgem3_v2.py

# Restart Ollama when done
Start-Process "ollama" serve
```

Do not attempt a parallel/multi-worker backfill on a 12 GB card under any circumstances.

---

## Copy-pasteable backfill command sequence

This sequence is safe for an 18.9k-document store on a 12 GB card. It takes the CPU path (slowest but safe), is fully resumable if interrupted, and leaves a diagnostic printout of coverage.

```powershell
# 1. Snapshot the DB before starting.
Copy-Item genomes\main\genome.db genomes\main\genome.db.bak

# 2. Stop the cymatix server if running (prevents concurrent writes).
#    If using the launcher, quit via the system tray.
#    If started manually:
# Stop-Process -Name "python" -ErrorAction SilentlyContinue

# 3. Force CPU device and run the backfill.
$env:BGEM3_DEVICE = "cpu"
python scripts/backfill_bgem3_v2.py

# The script prints progress lines:
#   [backfill] codec device=cpu
#   [backfill] genomes/main/genome.db: genes total=18900 v2_populated_before=0 dim=1024
#   [backfill] genomes/main/genome.db: rows to process: 18900
#   [backfill] ...18900/18900 processed=18900 skipped=0 rate=3.5 genes/s
#   [backfill] DONE ... coverage=100.00% elapsed=5400.0s

# 4. Verify coverage=100% in the final line before proceeding.

# 5. Restart the cymatix server.
python -m uvicorn cymatix_context._asgi:app --host 127.0.0.1 --port 11437
```

To use a non-default DB path or a smaller batch size (reduces peak RAM usage on very large stores):

```powershell
$env:BGEM3_DEVICE = "cpu"
python scripts/backfill_bgem3_v2.py path\to\genome.db --batch 32
```

The `--batch` flag controls how many documents are encoded and committed in one pass (default 64, source: `scripts/backfill_bgem3_v2.py:278`). Lowering it reduces peak memory at the cost of more SQLite commits. `--limit N` processes only the first N rows — useful for a smoke test before committing to a full run:

```powershell
python scripts/backfill_bgem3_v2.py --limit 200
```

The script is idempotent: rows that already carry a correctly-sized `embedding_dense_v2` BLOB are skipped, so a re-run after a crash picks up where it left off.

---

## After the backfill

Once `coverage=100%`, dense recall is active under the default `[retrieval] dense_embedding_enabled = true`. The next step in the Stage 2 → Stage 4 calibration sequence is to recalibrate `ann_similarity_threshold` at 1024-dim using `scripts/calibrate_thresholds.py` — the shipped default of `0.35` (`cymatix.toml`) is a legacy value calibrated at dim=256 and should be re-derived. See `docs/operator-runbooks.md` Runbook 2 for that procedure.

The `ann_threshold_max_genes` cap of `12` (`cymatix.toml`) is a known candidate pool collapse bug on single-shard stores (issue tracked in the 2026-06-06 handoff). Until a fix ships, consider raising it to 500 via `cymatix.toml` if dense retrieval returns fewer candidates than expected:

```toml
[retrieval]
ann_threshold_max_genes = 500
ann_similarity_threshold = 0.0
```

This does not affect ingest; it only widens the ANN candidate pool at query time.
