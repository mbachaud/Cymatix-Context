# Helix-Context Hardware Optimization Investigation

**Date:** 2026-06-02
**Author:** Laude (+ 27-agent read-only investigation team)
**Status:** Investigation / recommendation only — **nothing actioned.** Raude owns internals.
**Scope:** gandalf dev rig + DGX Spark profiling · bench/normal-op tuning · dual-genome dynamic-port tray · switchboard visualizer.

> **Method note.** 7 read-only mapping agents → 16 adversarial verify agents → 4 synthesis agents.
> Everything below is grounded in code at `file:line`. The investigation did **not** touch the live
> daemon (Raude was mid-benchmark on the 850K Onyx corpus at `:11437`). All "set X" lines are
> recommendations, not changes. Anything touching public API / feature flags / retrieval behaviour
> is **PRD-gated** per the project workflow.

---

## 0. Version provenance — a THREE-WAY split (read this first)

> **2026-06-02 recheck (per Max): re-baselined against v0.6.2.** The first pass read the **stale local
> `master`** and over-generalised from it. The corrected picture below supersedes that. Net: the live daemon
> is *behind* shipped helix, and two "absent/refuted" findings were master-only artifacts.

The single most important finding is not a hardware fact — it's a **tree fact**. Three trees diverge:

| Tree | Ref | What it is | Has the perf machinery? |
|---|---|---|---|
| **Local `master`** | `826d053` v0.5.0 | **STALE / un-pulled**, behind every release. *What the first pass mistakenly read.* Not running, not shipped. | No (none of it) |
| **LIVE daemon** (`:11437`) | worktree `vibrant-easley-73d68a` (`perf/dense-prefilter-via-splade-candidates`, `e54a250` + dirty), via `bgem3_gpu_venv` editable install (torch **cu121** → can see the GPU) | A perf *experiment* branched off **old** master 826d053. Has its own dense-prefilter/shard-worker/fp16 edits. | **Partial — LACKS #173 singleton & `HELIX_MEM_PROFILE`** |
| **Shipped `v0.6.2`** | `3173c47` (on `origin/master` + 2 feat branches; not tagged v0.6.2 locally) | The PyPI release line. **The baseline to reason against.** | **Yes** (see recheck table) |

**Consequence:** before trusting *any* env/config lever, confirm **which tree + which venv** the daemon
launched from. The live daemon is **not** master and **not** v0.6.2 — it's a stale experiment branch *missing
the very RAM fixes that target gandalf's 48 GB ceiling*. Lever tags below: `[v0.6.2]` = in shipped release ·
`[worktree]` = only in the running experiment branch · `[OS/env]` = tree-independent. **A `[v0.6.2]` lever does
NOT work on the currently-running worktree daemon until v0.6.2 is merged into it (or the daemon is relaunched on
v0.6.2).**

### Recheck table — keystone findings against v0.6.2 (verified at `3173c47`)

| Finding | master 0.5.0 *(first read)* | **v0.6.2 (shipped)** | LIVE worktree *(running now)* |
|---|---|---|---|
| **BGE-M3 dense codec device** | CPU-pinned | **STILL CPU-pinned** — `knowledge_store.py:2524` `get_shared_codec(dim=…, share=…)` passes **no `device=`** → factory default `"cpu"` (`bgem3_codec.py:192`); ingest `context_manager.py:706` same; no `get_hardware()` for dense | CPU-pinned (`knowledge_store.py:2475`) |
| `HELIX_MEM_PROFILE` (auto) | absent | **PRESENT** (`hardware.py:462,472`; RAM-aware SQLite budget + configurable `mmap_size`) | **absent** |
| #173 singleton (120→7 GB) | absent | **PRESENT** (`get_shared_codec`, `HELIX_SHARE_DENSE_CODEC=0` reverts) | **absent** |
| `HELIX_SHARD_WORKERS` | absent | **PRESENT** (`shard_router.py:66-74,597`) | present (own edit) |
| `HELIX_DENSE_MATRIX_DTYPE=float16` | absent → *first pass "REFUTED"* | **PRESENT — real lever** (`knowledge_store.py:348`) | present (own edit) |
| LRU shard cache | absent | **still absent** (`shard_router.py:319` dict + lock at `:343`, **no eviction**) | absent |
| rerank `re_rank`/`rerank` split | present | **still present** (`deberta_backend.py:100 def re_rank` vs `blend.py:84 .rerank()`) | present |
| dense_prefilter | absent | **absent** (worktree-only experiment) | present |

### Corrections to the first-pass verify verdicts
- ✅ **Keystone CONFIRMED on v0.6.2 too:** the BGE-M3 dense codec is CPU-pinned in *all three* trees — the #173
  singleton dedup'd the model for RAM but never threaded the device. So `HELIX_DEVICE=cuda` does not move dense
  encode on shipped helix. **An unmerged branch `chore/backfill-bgem3-device-env` (`6c149cf`, adds `BGEM3_DEVICE`
  env) already addresses this — flag to Raude.** This *strengthens* recommendation #5 below.
- ↩ **"`HELIX_DENSE_MATRIX_DTYPE=float16` doesn't exist" was a master-only artifact** — it IS a real shipped lever
  in v0.6.2 (`knowledge_store.py:348`). Same for `HELIX_MEM_PROFILE` (real, `auto` default in v0.6.2). The
  refutation held only against the stale master tree.
- **PARTIAL (still stands):** the commit balloon (~240–247 GB → OOM on a small pagefile) + 240 GB-pagefile
  requirement are real and measured, driven by **SQLite page heaps + per-shard model/cache + unbounded shard
  cache**, not a 42.6 GB mmap. BUT this was measured on master/worktree which **lack `HELIX_MEM_PROFILE` + the
  #173 singleton** — **v0.6.2 materially improves the RAM ceiling**, so re-measure the pagefile story on v0.6.2.

> **★ Top operational takeaway (post-recheck):** the highest-value single action is to **run bench *and* normal-op
> on v0.6.2** (or merge v0.6.2 → the worktree). The live daemon is a stale experiment missing `HELIX_MEM_PROFILE`
> and the 120→7 GB singleton — the exact fixes for gandalf's Wall-1. **Benching on the worktree under-measures
> what shipped helix already does.**

## 0.5 LIVE-RUN UPDATE — supersedes the Wall-1 analysis (2026-06-02)

Raude advanced the worktree to an unreleased **".3"** experiment (the *widened-rerank* PRD) and kicked off a run on
`enterprise_rag` full_2 (100 shards). Live telemetry + a partial result (66/125) **corroborate the keystone and
retire Wall-1**:

- **Wall-1 (pagefile storm / 104 GB ceiling) is RETIRED for full_2 on this branch.** Live: **committed 38/288 GB**,
  RAM 21.9/47.9 (46%), 24.1 GB shards in page cache, **Disk F at 3%** — no spill. The `get_shared_codec` singleton
  (the 120→7 GB fix) is now present as an **uncommitted edit in the running worktree** (grep-confirmed in
  `knowledge_store.py` + `bgem3_codec.py`; HEAD ancestry lacks `d5ade41` because it's *dirty*, not committed). 100
  shards + the deberta cross-encoder fit with ~26 GB free. ⇒ **The only remaining wall is Wall-2** (brute-force/no-ANN
  CPU scan, ~100 s/query). My "Wall-1 dominates" held only for the pre-singleton trees — strike it for this branch.
- **GPU idle CONFIRMED empirically:** GPU0 1% / 30 °C — retrieval + the *now-working* CE rerank are 100% CPU
  (keystone proof). **CPU 52% @ 4.68 GHz with `HELIX_SHARD_WORKERS=8` + `OMP_NUM_THREADS=1`** — the BLAS-pin +
  parallel-fanout config (rec §2c) is already in use; ~100 s/query is CPU-bound brute-force, not contention.
- **The `re_rank`/`rerank` mismatch was FIXED this session** (alias at `deberta_backend.py:189`) → the cross-encoder
  now actively reorders. My "structurally dead" finding is now historical.
- **But the working CE rerank is NET-ZERO on recall@10** (PRE 3.0% → POST 3.0%): of 12 in-pool-but-below-top-10
  golds it rescued 2, displaced 2, whiffed 10. Not broken — a properly-functioning MS-MARCO cross-encoder simply
  **can't separate gold from topical near-neighbors on paraphrase queries.** This is the clean verdict the clamp was
  hiding, and it matches the encoder near-neighbor-dilution root cause.
- **recall@200 = 21% (14/66)** → **~79% of semantic gold never enters the 200-pool.** Reach, not ranking, is the
  dominant problem; rerank can't touch what retrieval never surfaces. **Lever-ladder rung #1 (rerank) is now
  exhausted for a *real* reason** → weight shifts to (a) reach (recall@200 / offline rank-distribution probe +
  expansion) and (b) encoder discrimination (fine-tune / re-embed) — i.e. the embedding track.

**Two recommendations updated by this evidence:**
1. **Dual-genome resource verdict SOFTENED** (§4): with the singleton active, one full_2 daemon ≈ 22 GB working +
   ~24 GB *reclaimable* page cache. Two big genomes concurrently is no longer "OOM-infeasible" — but they'd contend
   for page cache (the very thing giving Disk-3% / fast latency), so expect a **latency regression**, not an OOM.
   Small-corpus pairing remains the clean recommendation.
2. **GPU offload (encode / CE → CUDA) is a LATENCY lever, not a recall lever** (§2b/§2d #3,#7,#8): rerank is net-zero
   on recall and reach is the bottleneck, so GPU work buys throughput/latency (and enables the eventual re-embed),
   **not** recall. Keep it, but don't sell it as a recall fix.

*(Partial: 66/125, ~1.7 h remaining. Full 125 + the offline rank-distribution probe pending from Raude.)*

## 0.6 cc-exchange reconciliation — completed Spark + encoder data (2026-06-02)

Two remote sources — **`github.com/addiplus/cc-exchange`** (the live Max↔Joe channel) and **`github.com/CosmicTasha/helix-bench`** (a mirror of the local bench) — supplied the completed numbers the local tree lacked, including the Spark figures this report earlier marked "not in Joe's files." Corrections, by section:

**Spark worker sweep (Joe, v0.6.2, 470q, 100-shard onyx, 37 h, GPU 0% throughout) — sources the "w8 knee / 37h / matmul" terms:**

| workers | p50 latency | recall@10 |
|---:|---:|---:|
| 1 | ~95.0 s | 27.45% |
| **8** | **~53.1 s (knee, 1.8×)** | 27.66% |
| 16 | ~62.7 s (slower!) | 27.66% |
| 24 | ~62.3 s | 27.66% |

- **Latency REGRESSES past w8 → bandwidth/merge-bound** (the one shape a GPU can't rescue). `HELIX_SHARD_WORKERS=8` is the measured knee. **Supersedes §2c's "leave serial (1)"** (that was under the now-retired Wall-1 assumption).

**Wall-2 reframe (supersedes §1 ladder item 2 + §2):** the dense GEMV is **~13 ms ≈ 0.01%** of a 53–95 s query. The other ~99.9% is the **serial cross-shard merge + per-shard lexical (FTS5/BM25/SPLADE) + IO** path. So GPU / matmul / fp-precision is a **non-lever for query latency** (confirmed by GPU 0% + the regresses-past-w8 signature). The memory-bandwidth framing holds, but the expensive bytes are lexical+merge+IO, **not** the matmul.

**fp16 correction (supersedes §2c float16 row):** `HELIX_DENSE_MATRIX_DTYPE=float16` is **storage-only on the CPU NumPy path** (NumPy promotes to fp32 in the matmul → *no* compute-bandwidth win today). It's a RAM-footprint lever; the bandwidth win materialises only after the `torch.cuda` kernel port.

**Canonical recall (supersedes §3's 27.66/27.2 headline):** 27.66% is the **mg1** number. The apples-to-apples canonical is **recall@10 = 29.8%** (splade-on + `ann_threshold_min_genes=10`, binary, full-470): @1 17.7 / @3 21.5 / @5 24.7 / @10 29.8 (SPLADE +6 pp, mg gate +~8 pp, both order-preserving). **Semantic-type** recall@10 = 2.4% (end-to-end 850k) is the separate failure-mode number. Multi-gold-aware exact lands a touch under 29.8% (20% of Qs are multi-gold).

**Latency ≠ bench score (sharpens §6):** the bench runs at 53–95 s/q against a **600 s/q budget** = 6–11× headroom. So every latency lever in this report is a **product-latency win, not a leaderboard mover.** The score is gated by **recall = the embedding.** Two orthogonal tracks: merge/IO = latency (Joe's box), embedding = score — don't pour GPU-time into shaving a 53 s query while the score is stuck at the encoder.

**Full-500 correctness (Opus judge, splade-on/mg10):** correctness **13.8%** / completeness 8.6% / doc-recall(answer-pass) 17.6% / 71% abstain / **15% hallucination — NOT 0%** (68 genuine confident-wrong; **0 fabrication on the 20 unanswerable**) / gen $24.10. Hallucination is **retrieval-driven** (tracks low-recall types; semantic=21) → the embedding lift cuts hallucination **and** lifts recall.
- **Conversion gap = +8 pp config knob:** of 79 golds ranked@10-but-answered-wrong, **39 were never delivered into the answer context** (per-gene delivery clamp) → ~+8 pp correctness from a config knob, no embedding work. Confirms the delivery-clamp finding.

**Encoder benchoff — the re-embed candidate is now CHOSEN (feeds Appendix B's >20k track).** Joe ran a semantic-125 proxy (736-doc pool, 1024-dim, CPU; *relative-to-control only — NOT a 2.4% prediction*):

| encoder | R@10 | R@200 | MRR@200 | ΔR@10 vs control |
|---|---:|---:|---:|---:|
| **Qwen3-Embedding-0.6B** | **86.8** | **100.0** | **0.699** | **+17.4 pp** |
| bge-large-en-v1.5 | 74.4 | 98.4 | 0.549 | +5.0 pp |
| BGE-M3 (control) | 69.4 | 98.4 | 0.473 | — |

- **Qwen3-Embedding-0.6B wins** — MRR 0.47→0.70, R@200 100%, **1024-dim native** (zero schema change), **Apache-2.0** (commercial-OK). bge-large (MIT) is the fallback; NV-Embed-v2 disqualified (CC-BY-NC).
- **Live blocker on the re-embed:** GPU embedding is **broken on the GB10** (Triton can't JIT CUDA kernels on ARM64/Blackwell; `gcc cuda_utils.c` link fails). Qwen3-0.6B at ~2.5 s/doc on CPU → full 850k re-embed ≈ **590 h CPU = infeasible.** Needs GPU embedding unblocked first (`attn_implementation="eager"` or vLLM). **This is the gate on the score-moving track.**

**GPU kernel port — now owned by Joe (supersedes §2b/§2d "speculative"):** the `_ensure_dense_matrix`/codec → `torch.cuda` port is being taken, gated **for re-embed + bench throughput (GEMV→GEMM query-batching), not single-query latency.** Order: fp16 + fp32-accumulate → batch → optional fp8-coarse, each recall-identity-gated on semantic-125.

## 1. Executive summary — the bottleneck ladder

---

## 1. Executive summary — the bottleneck ladder

For both gandalf (live `device=cpu`, 850K Onyx) and the DGX Spark, current Helix retrieval is **CPU- and
memory-bandwidth-bound, with both GPUs idle**. The cost ladder, in order:

1. **Wall-1 — pagefile I/O storm (gandalf only, at ~100-shard scale).** The 850K Onyx genome drives daemon
   private commit to **~240–247 GB** against 48 GB RAM → ~200 GB spills to the 980 Pro. Because **C: (pagefile)
   and F: (corpus) are the same NVMe**, fault traffic serialises on one PCIe queue. This *dominates* wall-clock.
   The Spark dissolves this entirely (128 GB unified, mmap-in-RAM) — that's its measured **2.1× p95 win**.
2. **Wall-2 — bandwidth-bound dense GEMV.** `sims = matrix @ query_vec` is a single-threaded fp32 NumPy
   matrix-vector product over every hot embedding (`knowledge_store.py:2635`). ~2 FLOP/byte → bytes/s is the wall.
   gandalf's **mismatched DDR4 (2×16+2×8)** throttles exactly this kernel; AVX2-vs-AVX512 is irrelevant.
   *(Note: "ANN" is a misnomer — there is no HNSW/IVF/faiss index anywhere.)*
3. **CPU transformer encode.** BGE-M3 dense + SPLADE query encoders run on the 5800X. This is the *only* slice
   the 3080 Ti would accelerate — and it's a fraction of per-query wall-clock at 850K scale.

**Headline:** the GPU is **idle by explicit request** (`HELIX_DEVICE=cpu`, reserving the 3080 Ti for the Sonnet
generation arm — `helix-bench/exchange/2026-05-31_results-and-embedding-track.md:30`). Even flipping the device
**does not** reach the heaviest neural op, because the **BGE-M3 dense codec is hard-pinned to CPU** (constructed
with no device arg at `knowledge_store.py:2443` → defaults `device="cpu"` at `bgem3_codec.py:57`; it never
consults `get_hardware()`). SPLADE/SEMA/NLI *do* follow `get_hardware().device`. **This codec wiring gap is the
single highest-leverage internals fix.**

Also: the system `python` has `torch 2.11.0+cpu` (no CUDA) — only the bench venv (`/f/tmp/bgem3_gpu_venv`,
`torch 2.5.1+cu121`) can physically see the GPU. **A torch-less interpreter silently degrades retrieval to
lex-only** (`bgem3_codec.py:74-81`). Always launch from a cu121 venv.

---

## 2. Gandalf hardware profile + tuning

**Box:** Ryzen 7 5800X (Zen 3, 8c/16t, single CCD/CCX, 32 MB L3, AVX2, **no AVX-512**) · 48 GB DDR4-3200
**mismatched** (2×16 + 2×8) · Samsung 980 Pro 1 TB carrying **both C: and F:** · EVGA RTX 3080 Ti (GA102,
12 GB GDDR6X, CC 8.6) · Win 11 Pro.

### 2a. Config / env Max can set today (no code change)

| Lever | Tree | Current | Recommended (this HW) | Effect | Helps |
|---|---|---|---|---|---|
| **Launch from cu121 venv** | OS | system `torch+cpu` | always cu121 venv | precondition for any GPU; prevents silent lex-only degrade (`bgem3_codec.py:74-81`) | both |
| **`splade_enabled=false`** (`helix.toml:193`) | master | `true` | `false` **on Onyx 850K only** | PRD: SPLADE = **0 pp recall, 3-for-3** on Onyx, costs 94–153 s p95; splade-off is the **only** config with zero 10-min timeouts | both (Onyx) |
| **`HELIX_DEVICE=cuda`** | master | `cpu` (pinned) | `cuda` **only for latency-only runs (GPU not generating)** | moves SPLADE/SEMA/NLI to GPU (they read `get_hardware().device`). **Does NOT move BGE-M3 dense** (codec gap §2b). Keep `cpu` while GPU generates. | bench |
| **`ann_threshold_min_genes=10`** | master | `1` prod / `10` bench | `10` **recall-bench only** | adaptive cut can early-exit; min_genes=10 forces a true top-k floor (~**+8 pp** measured). **Never in prod `/context`** (forces ≥10 docs/answer) | bench |
| **`HELIX_OTEL_ENABLED=1` + collector** (`otel.py:231`) | master | off → `_NoopMeter` | `1` via `backend-with-otel.bat` | makes any CPU→GPU win **measurable** (otherwise invisible) | measurement |
| **Fewer-larger shards (≤~15)** | OS/build | ~100–105 (Onyx) | re-shard `--auto-subshard-threshold-files=100K` → ~12 shards | cuts the Wall-1 commit storm (~50 GB vs 240 GB) — the dominant cost | both |
| **Keep ≥240 GB fixed pagefile** | OS | up to 240 GB | keep while serving 850K | 247 GB commit OOMs on a 56 GB pagefile (143 GB OOM measured). **Survival only — no speedup** | survival |
| **Pagefile on a 2nd NVMe** (if available) | OS | C:+F: on 980 Pro | move pagefile off the corpus drive | de-contends pagefile faults from corpus reads on one queue | both (mag. speculative) |

### 2b. Internals Raude must change (code)

| Change | Current (file:line) | Recommendation | Why it matters |
|---|---|---|---|
| **Thread device into BGE-M3 codec** ★ | `BGEM3Codec(dim=...)` no device → CPU (`knowledge_store.py:2443`; default `bgem3_codec.py:57`) | `BGEM3Codec(dim=..., device=get_hardware().device)` at both call sites (+ ingest `context_manager.py:706`) | **Highest-leverage GPU enablement.** Without it `HELIX_DEVICE=cuda` leaves the heaviest per-query op on CPU. ~1 line each |
| **fp16 + device on FlagEmbedding path** | `BGEM3FlagModel(use_fp16=False)`, no device (`bgem3_codec.py:76`) | `use_fp16=(device!='cpu')` + device | only the ST fallback honours `self._device` (`:80`); device-threading alone insufficient on a FlagEmbedding install |
| **Move dense GEMV to GPU (torch/cupy)** | NumPy CPU `matrix @ query_vec` (`knowledge_store.py:2635`) | torch-CUDA matmul (fp16 matrix ≈ 1.65 GB, fits 12 GB easily) | GDDR6X ~900 GB/s vs DDR4-3200 ~40–50 GB/s — the only thing that beats the mismatched-kit wall. Bigger payoff = batched GEMM at index-time |
| **LRU-bound the shard cache** | unbounded `self._shards`, never evicts (`shard_router.py:294-306`) | LRU cap (~32 open Genomes), reopen on miss | primary Wall-1 driver; lowest-risk durable fix. **Not yet written anywhere** |
| **Fix rerank (3 independent blockers)** | `rerank_enabled=false`; DeBERTa exposes `re_rank` but blend calls `.rerank()` (`blend.py:81` vs `deberta_backend.py:100`); gated to `profile=='quality'` (`routes_context.py:754`, default `balanced`) | add `rerank` alias + relax profile gate, then config can enable | flipping the flag alone is a **silent no-op**. Reranker model already `.to(get_hardware().device)` — GPU-ready once reached. **But it only re-orders the existing pool — can't rescue gold dense never surfaced** |
| **Process-shared codec singleton** | per-`KnowledgeStore` codec (`:2441-2443`), not injected into shards | inject one shared dense codec per shard `Genome` (mirror `sema_codec` at `context_manager.py:443-468`) | the "120 GB→7 GB singleton" fix is **not in master 0.5.0** — N shards can materialise N codecs |

### 2c. Worktree-gated levers (need Raude's branch on master first)
- **`HELIX_SHARD_WORKERS`** `[v0.6.2]` — parallel shard fan-out (`shard_router.py:65-79`). Order-preserving =
  recall-identical, latency-only. **Set to 8 — the measured knee** (Joe's sweep: w1 95 s → **w8 53 s, 1.8×**; w16/w24
  *regress* to ~62 s, the bandwidth/merge-bound signature). *(Supersedes the earlier "leave serial" — that assumed
  the now-retired Wall-1; with the singleton active, w8 is the rec, and the live run already uses 8.)*
- **BLAS pin** `[OS/env, but only with workers≥2]` — `OMP_NUM_THREADS=MKL_NUM_THREADS=OPENBLAS_NUM_THREADS=1`
  removes the 8×16=128-thread oversubscription during the parallel GEMV burst (`throughput-and-concurrency.md:24`).
  **Only pair with `HELIX_SHARD_WORKERS≥2`** — on serial master, BLAS should keep the cores for the single GEMV.
- **`dense_prefilter_enabled=true`** `[worktree]` — ~170× FLOP cut, recall-neutral across 3 fixtures, but the win is
  **un-measurable on gandalf while pagefile-bound** (it attacks Wall-2, not Wall-1).
- **`HELIX_DENSE_MATRIX_DTYPE=float16`** `[v0.6.2 / worktree]` — ✅ real shipped lever in v0.6.2
  (`knowledge_store.py:348`), but **storage-only on the CPU NumPy path** — NumPy promotes fp16→fp32 for the matmul,
  so it halves *resident RAM* but gives **no compute-bandwidth win today**. *(Corrects both the stale-master
  "doesn't exist" AND my earlier "attacks the bandwidth wall" framing — Joe confirmed it's storage-only; the
  bandwidth win needs the `torch.cuda` kernel port. And the matmul is only ~0.01% of the query anyway — see §0.6.)*

### 2d. Priority order (impact ÷ effort), with honesty flags
1. `splade_enabled=false` on Onyx — *config, trivial, evidenced.* Biggest measurable latency win today; zero recall cost. **Onyx-only — SPLADE is +6 pp on Max's own code corpus.**
2. Fewer-larger shards (≤~15) + keep 240 GB pagefile — *ops, moderate.* Attacks the dominant Wall-1.
3. Thread device into BGE-M3 codec (both sites) — *internals, ~1 line, high confidence.* The keystone GPU enablement.
4. `HELIX_OTEL_ENABLED=1` — *config, trivial.* Nothing above is verifiable without it.
5. `HELIX_DEVICE=cuda` from cu121 venv, latency-only runs — *config, trivial, bounded upside* (encode is a fraction of wall-clock; size on ~20 q first; only while GPU isn't generating).
6. LRU shard cache — *internals, moderate, high confidence in design* (not yet written).
7. Move GEMV to GPU — *internals, high effort, the real Wall-2 fix* (magnitude speculative until measured).
8. Fix rerank then enable — *mixed, medium.* Cheap GPU precision, but only re-orders the pool.
9. `dense_prefilter` + parallel fan-out — *after merge, Spark-first.* Recall-neutral latency levers that pay off only once Wall-1 is gone — **last on gandalf, first on Spark.**

### 2e. Standing hardware constraints (cannot be tuned away)
- **Mismatched DDR4 (2×16+2×8):** likely below matched-dual-rank bandwidth (possible 2T/Gear-Down) — permanently caps the GEMV. A matched dual-rank kit is the only cure.
- **Single 980 Pro for C:+F::** pagefile and corpus reads contend on one queue — no flag isolates them; a 2nd NVMe for the pagefile is the only de-contention.
- **12 GB VRAM:** ample for these encoders in fp32 (BGE-M3 ~2.3 GB, SPLADE ~0.5 GB, MiniLM ~90 MB). **Not** the binding constraint for serving; it *is* for parallel SPLADE *ingest* (auto-caps at 3 workers, ~4 GB each, `parallel.py:51-77`).

---

## 3. DGX Spark profile + gandalf-vs-Spark comparison

> **Sourcing discipline.** `results/spark/` holds only a README (no result-row JSONs yet — `results/spark/README.md:7`).
> Every Spark **number** below is from Joe's markdown exchange files; every **spec** (ARM core count, LPDDR5x, FP4,
> ConnectX) is **inferred from general GB10 knowledge** and marked as such. Of the "w8 knee / matmul 0.01% /
> GPU-0%-over-37h / FP4 / ConnectX" terms, **only "GPU at 0%" appears in Joe's files** — the rest trace to session
> memory, not measured bench, and are not asserted here as Spark facts.

### 3a. Measured Spark behaviour (canonical 470-q bench)
- **recall@10 = 27.2%** (`helix-bench/README.md:16`) — within noise of gandalf's 27.66%; 6/8 query types bit-identical.
- **p95 latency = 146.7 s** vs gandalf 310.8 s → **2.1× speedup** (`README.md:19`). Same pre-#172 serial daemon on both → "pure memory architecture (mmap-in-RAM vs pagefile), no threading confound."
- **Full-run = 13.5 h Spark vs 20.6 h gandalf** (`exchange/2026-05-31_splade-500q-test-plan.md:41`).
- **GPU at 0% the whole run** (`README.md:21`) — dense matmul is CPU NumPy, BGE-M3 encode is CPU. **Idle exactly like the 3080 Ti.**
- Joe advised **`HELIX_SHARD_WORKERS` 16+** and uvicorn **`--workers 2-3`** — viable *only* because each worker materialises its own genome in 128 GB; the same move **OOMs gandalf's 48 GB** (`docs/throughput-and-concurrency.md:25`).

### 3b. Comparison

| Axis | gandalf | DGX Spark | Helix consequence |
|---|---|---|---|
| **CPU** | x86 Zen3 8c/16t, 32 MB L3, AVX2 | ARM ~20-core Grace *(inferred)* | hot path CPU-bound on **both**; kernel is bandwidth-bound not FLOP-bound → ISA is secondary |
| **Memory** | 48 GB DDR4-3200 **mismatched** + pagefile on 980 Pro | **128 GB unified LPDDR5x, mmap-in-RAM** | **the entire latency story.** gandalf spills ~200 GB → page-fault storm; Spark fits everything resident → 2.1× p95 |
| **GPU** | 12 GB GDDR6X CC8.6 — **idle (`device=cpu`)** | Blackwell unified *(inferred)* — **idle (0%)** | both GPUs unused; Spark's biggest asset contributes **nothing** today |
| **Storage** | one 980 Pro: C:+F:+corpus+pagefile contend | spill not exercised | gandalf's single-drive layout compounds the spill |

### 3c. Reconciliation
- **recall 27.66 vs 27.2 is NOT a hardware story.** Retrieval is deterministic/order-preserving; hardware moves
  *wall-clock*, never *recall*. The recall ceiling is the **embedding (BGE-M3 reach)** — semantic recall is
  1.6–2.4% on **both** hosts. That's why the embedding-upgrade/re-embed track is assigned to Spark: not because
  Spark lifts recall, but because the **GPU-batch re-embed of 850K genes fits its 128 GB unified memory**.
- **GPU-0% on both confirms CPU/bandwidth-bound retrieval.** Spark wins latency today *only* by dissolving
  gandalf's pagefile cliff. **What would make Spark decisively pull ahead:** (1) GPU encode (needs the codec
  device fix §2b), (2) GEMV→batched-GEMM on `torch.cuda` (`throughput-and-concurrency.md:26`), (3) unified-memory
  process parallelism (`--workers 2-3`, `HELIX_SHARD_WORKERS 16+`), (4) the GPU-batch re-embed. Until then both
  boxes run the same CPU-bound, GPU-idle retrieval.

---

## 4. Dual-genome / dynamic-port tray feasibility

**Scenario:** keep the tray helix-mcp running (genome #1 → VSCode dev, port 11437) while concurrently benching a
2nd genome on a 2nd port — so benching never tears down the live dev MCP.

### Verdict — three-layered (don't collapse to one word)
1. **Two daemons / two ports / two genomes — possible TODAY, config-only.** Helix is strictly
   one-genome-per-process (`context_manager.py:464`), but **port and genome are both externally injected**:
   port via uvicorn `--port` / `--helix-port` (never read inside `create_app`); genome via
   `HELIX_GENOME_PATH` (`config.py:644-645`) + `HELIX_USE_SHARDS=1`. The live Onyx daemon *is* the proof
   (its genome ≠ the helix.toml default). One command stands up a 2nd instance:
   `HELIX_GENOME_PATH=<g2> HELIX_USE_SHARDS=1 python -m uvicorn helix_context._asgi:app --port 11439`.
2. **Tray *supervising* the 2nd daemon — needs internals.** The supervisor is hard-wired to ONE helix child
   (`supervisor.py:71-126`, single `helix_pid/helix_port` in `state.py:28-48`). **But the launcher already fully
   supervises a second distinct process — Headroom** (own host/port, adopt-if-running, owned/adopted lifecycle,
   tray items: `app.py:392-461`, `tray.py:250-283`). That is a ready blueprint. `find_orphan_helix()` can even
   *adopt* a hand-launched daemon by LISTEN-socket + cmdline marker (`supervisor.py:222-291`).
3. **Dynamic port allocation/discovery — doesn't exist.** Ports are static defaults; "dynamic" today = "a
   different fixed value you pass." `_port_is_free` exists (`supervisor.py:306`) — fixed-second-port-with-check
   (e.g. 11439) is simpler and sufficient; true ephemeral allocation only matters for many bench lanes.

### Pointing the bench at port B
- **enterprise_rag scripts: config-only, works today** — `bench_enterprise_rag(_recall).py` expose `--helix-url`
  (`:433`, `:32`) + `--external-helix` ("a daemon is already running"). `--helix-url http://127.0.0.1:11439
  --external-helix`. Zero code change.
- **helix-bench/harnesses probes: ~5 LOC each** — `probe_latency.py`, `xl_ab.py`, `recall_lex*_compare.py`
  hardcode `URL='http://127.0.0.1:11437/fingerprint'`. Need a one-line `os.environ.get('HELIX_BENCH_URL', …)`.
- **Genome is chosen by the daemon, not the harness** — point lane-BENCH's `HELIX_GENOME_PATH` at the desired corpus.
- **Bench floor:** recall@k on port B still needs `ann_threshold_min_genes>=10` in *that* daemon's config
  (lane-DEV keeps prod default 1) — exactly the kind of divergence two daemons make clean.

### Session attribution (the lane-tagging mechanism)
Registry supports many participants/daemon via `HELIX_MCP_HANDLE→participant` (`mcp_server.py:119`). DEV `.mcp.json`
→ `HANDLE=laude` @ :11437; bench env → `HANDLE=raude` @ :11439. **Caveat:** registry tables live *inside each
genome's SQLite* — two genomes = two participant tables; `raude`/`laude` won't appear in each other's `/sessions`.
Good for isolation, but a dual-genome switchboard must **query both daemons and merge**.

### Resource reality check on gandalf
- **Concurrent dev + 850K Onyx — NO. Resource-infeasible.** 850K alone drives commit to 240–247 GB / requires the
  240 GB pagefile / 143 GB OOM on default. A 2nd daemon adds its own genome + its own ~7 GB encoder bundle
  (per-process; not shareable; the singleton fix isn't even on master). Two daemons both leaning on one 980 Pro
  for mmap + pagefile faults, on a box already at its ceiling. This is a **commit-budget wall**, not a tuning problem.
- **Small-corpus pairing — YES (recommended shape).** gandalf's own **medium/xl** corpora are orders of magnitude
  lighter than 850K Onyx. Lane-DEV (medium dogfood) + Lane-BENCH (medium/xl) is comfortably concurrent on 48 GB;
  the 3080 Ti is idle so no VRAM contention. **Measurement gap:** medium/xl have **no wired gold `questions.jsonl`** —
  only unlabeled ordering-diff probes (`xl_ab.py`). So you get *latency* dogfood free, but *recall@k* on Max's own
  corpus needs a gold set first; enterprise_rag is currently the only labeled-gold recall path.
- **Sequential fallback (if heavy Onyx bench is required):** the existing **`/admin/swap-db`** hot-swap
  (`routes_admin.py:845-965`, MCP `helix_swap_db`) flips the single daemon dev↔bench — config-only, zero extra
  RAM, but *serial* (not concurrent) and mutates shared state for all clients.

### Risks + minimal first step
**Risks:** R1 RAM wall (concurrent + 850K = OOM) · R2 encoder bundles not process-shareable · R3 single-NVMe
contention · R4 split registry presence · R5 provenance (which tree/venv each lane launches) · R6 probe URL hardcoding.

**Minimal step (PRD-first — touches launcher + ports + feature behaviour):** scope `docs/prds/2026-06-xx-dual-genome-tray.md` around the **small-corpus pairing**, with:
- **Phase 0 (zero-code, validate):** by hand, launch a 2nd daemon on :11439 → *medium* corpus, leave the tray dev
  daemon untouched, run `bench_enterprise_rag_recall.py --helix-url http://127.0.0.1:11439 --external-helix`.
  Proves two-lane concurrency *and* measures real 2-genome RAM cost. (Do **after** the current bench finishes.)
- **Phase 1 (internals, only if Phase-0 RAM is acceptable):** generalise `HelixSupervisor` to `(port, genome_path)`
  threaded into child env; clone the Headroom state/tray pattern for a 2nd helix slot; teach the dashboard collector
  to merge both ports' `/sessions` + `/health`.
- **Explicitly scope OUT** the concurrent-850K case (resource-blocked → use `/admin/swap-db` sequential fallback).

---

### 4.1 Does `start-helix-tray.bat` block bench control of the server? (port-11437 conflict + per-lane telemetry)

**Yes — on port 11437 the tray and the bench launcher actively fight (mutual takeover/kill, not a soft block).**
- The tray runs `python -m helix_context.launcher.app --tray`, whose supervisor manages exactly ONE helix on a
  single port (default 11437): on start it spawns, or **adopts** an existing helix on 11437
  (`supervisor.py:222,306-323`); on Quit/restart it **`taskkill /F /T`s the tracked PID** (`:519-528`). Unlike
  Headroom, **helix has no "adopted-survives-Quit" flag** (`state.py` has `headroom_owned`, no `helix_owned`) — so an
  *adopted* daemon is killed on Quit too.
- The bench launcher `start_helix_for_enterprise_rag.py` is **hard-wired to 11437 and explicitly kills any uvicorn on
  11437** before starting its own (`:32, :109, :137-138`).
- ⇒ **Tray-first:** the bench launcher kills the tray's daemon and seizes 11437. **Bench-first (today's live run):**
  launching the tray **adopts the in-flight bench daemon and puts it under kill-on-Quit** — so **do NOT launch the
  tray on 11437 during Raude's run.** (HTTP query harnesses just POST — never "blocked" — but pointed at the default
  URL they hit whichever daemon holds 11437, possibly the wrong genome.)

**Coexistence (tray on 11437 + bench on its own port) — config-only today + two small internals:**
1. **Separate ports (config today).** Keep the tray's dev daemon on 11437; run the bench daemon **by hand** on e.g.
   11439: `HELIX_GENOME_PATH=<bench> HELIX_USE_SHARDS=1 <cu121-venv>\python -m uvicorn helix_context._asgi:app
   --host 127.0.0.1 --port 11439`. The tray supervisor only watches 11437 → it will **not adopt or kill** 11439.
   Point the harness with `bench_enterprise_rag_recall.py --helix-url http://127.0.0.1:11439` (already supported; the
   5 `harnesses/*` probes need the ~5-LOC env-URL read). *Small internals nicety:* give
   `start_helix_for_enterprise_rag.py` a `--port` and drop its blanket "kill anything on 11437."
2. **Per-lane telemetry "which is which" (the key small internals change).** `service.name` is hardcoded
   `"helix-context"` (`server/app.py:190`) and `otel.py`'s `Resource.create({SERVICE_NAME: service_name})` reads **no
   env override** — so two daemons emit identical `service.name` and **merge indistinguishably** in
   Prometheus/Tempo/Grafana (standard `OTEL_SERVICE_NAME` won't help — the explicit Resource overrides it). Fix (~3-5
   lines): read `HELIX_OTEL_SERVICE_NAME`/`HELIX_LANE` into `service_name` and add `service.instance.id = host:port`
   to the Resource. Then launch dev with `HELIX_OTEL_SERVICE_NAME=helix-dev` and bench with `=helix-bench` → every
   metric/trace/log is lane-tagged and splittable. The tray's OTel Collector (`:4317`) + Prometheus already ingest
   both pushes; only the label is missing. *(Session-level "which is which" already works: registry handles
   `laude`=dev / `raude`=bench live in each genome's own participant table.)*
3. **Both lanes inside the tray dashboard (internals).** The launcher collector polls only 11437; surfacing the bench
   lane in-tray needs the dual-genome collector generalization (§4) — not required for Grafana visibility, which #2
   delivers on its own.

**Caveat:** the tray bat launches via bare `python` — if that resolves to the system `torch+cpu` interpreter, the
tray's dev daemon silently runs **lex-only** (no dense/SPLADE; `bgem3_codec.py:74-81`). Point the tray at a cu121
venv so the dev lane isn't quietly degraded while you bench.

## 5. Switchboard visualizer feasibility + design

**Goal:** a tray/dashboard panel showing **(1) tuned values, (2) gate thresholds, (3) latency-per-step, live.**
The headline: it splits into a **cheap two-thirds and an expensive one-third**, and the expensive third is
double-gated by a flag the live daemon probably doesn't have on.

### 5a. Data availability

| Panel | Source today | Verdict |
|---|---|---|
| **Tuned values** | `GET /stats` → `.config` (`context_manager.py:1844-1862`), already polled by the launcher collector every 2 s | **CHEAP — data exists** |
| **Gate thresholds** | `GET /health` → `.calibration` (`routes_admin.py:444-455`), already polled | **CHEAP — data exists** |
| **Latency-per-step** | OTel histograms `helix_pipeline_stage_seconds` + `helix_genome_signal_seconds` (`otel.py:595-608, 690-703`) | **EXPENSIVE — flag-gated, push-only, no HTTP snapshot** |

- **Panel 1 gap:** `/stats.config` carries `ribosome_budget`, `expression_budget`, `max_genes_per_turn`,
  `splice_aggressiveness`, `decoder_mode`, `budget_zone_enabled` — but **omits** the retrieval-tier knobs
  (`rerank_enabled`, `splade_enabled`, `dense_embedding_enabled`, `fusion_mode`, device, shard_workers). There is
  **no full-config dump endpoint**. ⚠ Two requested knobs aren't real master knobs: **`per_gene_budget`** (master
  uses a hard `target=1000` splice clamp; dynamic allocator is worktree/PyPI) and **`dense_prefilter_enabled`**
  (worktree-only; master's prefilter knob is `bm25_prefilter_enabled`). Don't render them as live master knobs.
- **Panel 2 gap:** `/health` shows the *configured mode* (`ann_threshold_mode=margin_over_random`,
  `abstain_mode=global`), not the live numeric cut per query.
- **Panel 3:** timers are good, but `setup_telemetry()` early-returns when `HELIX_OTEL_ENABLED!="1"` (`otel.py:231`)
  → every `.record()` is a no-op; **there is no Prometheus `/metrics` scrape endpoint** (OTLP-push only). A live
  readout must query **Prometheus :9090** (only up if the grafana/docker scripts ran).

### 5b. Design
- **Lives in the existing launcher dashboard `:11438`** (`launcher/app.py`) — not a new service, not inside the
  serving daemon (coupling a UI concern to the bench-critical process is wrong). It already polls every 2 s, fans
  out over httpx with timeouts, and degrades to null panels. A switchboard is a **new tab + panel template**.
- **Reads:** reuse `/stats.config` + `/health.calibration` (have it now) → add one read-only **`GET /admin/config`**
  on the daemon to dump the *resolved* `HelixConfig` (closes the tuned-values gap honestly, with override
  provenance) → **Prometheus PromQL** for latency (`histogram_quantile(0.95, sum by (le,stage)(rate(helix_pipeline_stage_seconds_bucket[5m])))`).
  A dedicated `/switchboard` aggregator is **not** recommended (duplicates the above + adds a daemon route).
- **Two correctness guards:** (a) label latency rows with the **real timer stages** `extract/express/rerank/splice/assemble`
  (not the CLAUDE.md "0–6" marketing pipeline; `express` bundles all retrieval). (b) **Flag runtime overrides** —
  show *resolved* values with provenance tags; reading only `helix.toml` would show `device="auto"` while the daemon
  is actually CPU via `HELIX_DEVICE` env. That trap is the whole point of the panel.

### 5c. Effort tiers
- **Tier 0 — MVP (CHEAP, recommended scope):** read-only tuned-values + gate-thresholds tab from already-polled
  `/stats.config` + `/health.calibration` (+ optional on-disk toml for the omitted knobs). Pure launcher-side work
  (`panels/switchboard.html`, a `_switchboard_panel()` in `collector.py`). Never touches the serving daemon.
- **Tier 1 — live latency-per-step (MODERATE, gated):** needs the daemon launched with `HELIX_OTEL_ENABLED=1`
  **and** Prometheus up; then PromQL the existing histograms. No new helix instrumentation — the *operational
  dependency* is the cost. **Cannot be enabled on the currently-serving bench daemon without a relaunch (forbidden
  this session).**
- **Tier 2 — editable / hot-reload (HARDEST — DANGER, do not MVP):** writing knobs touches the config write path
  and mutates state shared by **all** MCP clients. Many knobs are read once at construction (genome, device, encoder
  placement) and are **not hot-reloadable** → a UI toggle silently no-ops or needs a restart that kills in-flight
  clients. The only runtime mutation today is `/admin/swap-db` (all-clients blast radius). **Explicitly out of scope —
  this is the feature-creep this assessment was asked to guard against.** Needs its own PRD + a hot-reloadable-knob
  taxonomy + per-client vs global scope semantics.

### 5d. Recommendation
Ship **Tier 0** (read-only tuned values + gate thresholds) — two-thirds of the ask is nearly free. Add a
read-only **`GET /admin/config`** so it shows *resolved* values with override provenance, not toml defaults. Make
latency-per-step a **second card that degrades gracefully to "no data"** when telemetry is off / Prometheus is
down. **Stop short of editable/hot-reload.** PRD-gated the moment it adds the new endpoint or the Prometheus
dependency.

---

## 6. Consolidated next steps (all PRD-gated where noted)

**Cheapest measurable wins (config/ops, gandalf):**
1. Launch daemons from the cu121 venv (prevents silent lex-only degrade).
2. `splade_enabled=false` on the Onyx 850K bench (frees 94–153 s p95, kills timeouts, 0 pp recall) — **Onyx only.**
3. Re-shard Onyx to ≤~15 shards + keep the 240 GB pagefile (attacks Wall-1).
4. `HELIX_OTEL_ENABLED=1` so any subsequent change is measurable.

**Highest-leverage internals (Raude, PRD if touching retrieval behaviour):**
5. Thread `get_hardware().device` into `BGEM3Codec` (both call sites) + fp16/device on the FlagEmbedding path — the keystone GPU enablement.
6. LRU-bound the shard cache (durable Wall-1 fix).
7. Port the dense GEMV to `torch.cuda` (the real Wall-2 fix; the only lever that beats the mismatched-DDR4 wall).
8. Fix the rerank name-mismatch + profile gate, then enable (cheap GPU precision — but only re-orders the pool).

**Spark-specific (Joe):** `HELIX_SHARD_WORKERS 16+`, `uvicorn --workers 2-3`, and the GPU-batch re-embed track — all enabled by 128 GB unified memory and foreclosed on gandalf.

**Features (investigation → PRD before code):**
9. Dual-genome tray: PRD around the **small-corpus pairing**, Phase-0 by-hand validation, Headroom supervisor as template; scope OUT concurrent-850K.
10. Switchboard visualizer: PRD for **Tier 0–1 read-only**; explicitly exclude Tier 2 editable/hot-reload.

**Cross-cutting precondition:** establish **which tree + venv** each daemon launches from before trusting any worktree-gated lever. Most latency machinery (`HELIX_SHARD_WORKERS`, fp16 dtype, dense-prefilter, the 7 GB singleton) is **not on master 0.5.0**.

---

## Appendix — verification & sources

- **Verify pass:** 16 high-impact claims → **12 confirmed, 3 partial, 1 refuted.** Corrections folded into §0.
- **Knob inventory:** 87 knobs catalogued, **46 hardware-sensitive** (full dump in the workflow result JSON).
- **Key code refs:** `hardware.py:40-48,254-326` · `bgem3_codec.py:57,76,80` · `knowledge_store.py:2443,2576,2635` ·
  `shard_router.py:294-306,443` · `sharding.py:212,488-500` · `context_manager.py:464,1844-1862` ·
  `routes_admin.py:444-455,845-965` · `launcher/supervisor.py:71-126,222-291,306` · `launcher/collector.py:81-171` ·
  `telemetry/otel.py:231,595-608,690-703`.
- **Bench refs (`helix-bench/`):** `README.md:16,18,19,21` · `exchange/2026-05-31_results-and-embedding-track.md:30,46` ·
  `exchange/2026-05-31_splade-500q-test-plan.md:25,41` · `docs/throughput-and-concurrency.md:14-26` · `results/spark/README.md`.
- **Investigation cost:** 27 agents, 2.69M tokens, 638 tool calls, ~52 min wall-clock.

---

## Appendix B — Probe-interpretation rubric (rank-band → lever)

*Ready to receive the offline rank-distribution probe + recall@k curve from the widened-rerank run. Slot the band
counts into the table; the pre-checks pick the track before you read the bands.*

### Pre-check 0 · Is dense actually surfacing on the sharded path? — **answerable by the probe itself**
The probe computes **pure-dense rank over all 850K vectors, bypassing the daemon's retrieval entirely.** Compare it
to the daemon's live pool:

> **probe pure-dense recall@200  vs  daemon pool recall@200 (= 21%)**

- **pure-dense@200 ≫ 21%** → dense isn't surfacing effectively on the sharded path → **wiring/routing lever
  (near-free); do NOT spend GPU-weeks on re-embed.**
- **pure-dense@200 ≈ 21%** → dense *is* contributing → 21% is a real **geometry** verdict → go to the band split.

*Already established (so we don't re-litigate):* dense recall is **not** globally off — it fires per-shard via
`query_docs` under **additive fusion** (`dense_embedding_enabled=true`, `dense_additive_weight=4.0`). The only dead
dense path is `query_docs_ann`'s ANN-threshold gate (refuted-hyp #4). So PC-0 is specifically about **per-shard
effectiveness**, not a global on/off.

### Pre-check 1 · Routing — does the router even visit the gold's shard?
The probe-vs-pool delta also exposes this: a gold that ranks high in pure-dense (global) but never enters the pool
**and lives on a shard the router didn't select** is a **routing** miss, not a geometry miss → fix routing/fan-out,
not the encoder. Separate "gold on an unvisited shard" from "gold visited but out-ranked."

### Bands (for golds that are embedded AND on a visited shard)

| Gold rank band | Diagnosis | Lever | Effort | Recoverable ceiling |
|---|---|---|---|---|
| **1–10** | delivered (today's ~3%) | — | — | baseline |
| **11–200** | in widened pool, mis-ranked vs near-neighbors | rerank **proven net-zero here** → **encoder discrimination** (hard-neg fine-tune); fusion-weight retune low-ceiling | rerank=done; FT=high | low without encoder work — *the dilution band* |
| **201–1,000** | right neighborhood, just outside delivered k | **k↑ / pool-widen** (config) + light rerank | low | **HIGH if populated — cheapest win** |
| **1,001–~20k** | facet miss: one paraphrase vector misses a lexical/semantic facet | **query expansion** (multi-query / HyDE / sub-query decomp) | med | medium |
| **>20k, embedded** | gold genuinely far in geometry | **re-embed / fine-tune** encoder | high (GPU re-embed 850k) | the residual — only geometry fixes it |
| **∞ (no vector / wrong dim / unrouted)** | data/routing bug | **pipeline/routing fix** | low–med | possibly large & cheap — check via PC-0/PC-1 FIRST |

### The fork (where the 79% lives)
mass in **201–1k** → widen k · mass in **1k–20k** → expansion track · mass in **>20k-embedded** → re-embed/fine-tune ·
mass in **∞ / unvisited shard / pure-dense≫pool** → it's wiring/routing, **don't burn GPU-weeks on a model fix for a
config bug.**

### Expansion vs re-embed (decide once band counts land)
- **Expansion** — inference-time, no re-index; cost ≈ N× query latency (brutal at gandalf's ~100 s/q CPU brute-force,
  fine on Spark/GPU); lift **bounded by what's retrievable at large k** → only helps the 1k–20k band.
- **Re-embed / fine-tune** — one-time GPU batch over 850k genes (fits Spark's 128 GB, painful on gandalf); permanently
  changes geometry; the **only** fix for the >20k band — but **validate a candidate encoder on a held-out gold set
  before re-embedding everything.**

*Inputs to drop in when the run finishes:* recall@k curve (10/50/200/1k/5k) · per-band counts split semantic-vs-other ·
the pure-dense@200-vs-pool@200 delta (PC-0).

### Re-embed track — candidate already CHOSEN (cc-exchange encoder benchoff, 2026-06-02)
The ">20k / re-embed" lever's "validate a candidate before re-embedding" step is **done**. Joe's semantic-125 proxy
(736-doc pool, 1024-dim, CPU; relative-to-control only) ranked **Qwen3-Embedding-0.6B** the clear winner — **R@10
86.8 (+17.4 pp over BGE-M3 control), R@200 100%, MRR 0.47→0.70**, and it's **1024-dim native (zero schema change) +
Apache-2.0**. bge-large-en-v1.5 (+5 pp, MIT) is the fallback. So if the probe's mass lands in the >20k band, the
encoder swap is pre-selected.
**Gate:** GPU embedding is currently **broken on the GB10** (Triton/ARM64 — `gcc cuda_utils.c` link fails); CPU
re-embed of 850k ≈ 590 h = infeasible. The re-embed track is blocked on unblocking GPU embedding
(`attn_implementation="eager"` / vLLM) — Joe owns that. *(Caveat: the proxy is a relative encoder-selection signal
over a 736-doc pool, NOT a prediction of the 2.4% end-to-end semantic number — the real lift is confirmed only by a
new-genome semantic-125 re-run + threshold recalibration.)*

### RESULTS — probe landed (2026-06-02): the fork resolved → WIRING @10, geometry @200
The widened-rerank run + offline cross-join came back. **The top-tier win is FREE — it's wiring, not the encoder.**

**Cross-join (semantic-125): of the 9 golds that pure global-dense ranks top-10, the daemon delivers 0:**

| outcome | count | what happened |
|---|---:|---|
| routing/cut dropped | 5 | gold absent from the fused 200-pool — dense ranks it top-10 globally, the daemon never had it |
| fusion demoted | 4 | gold in the pool but pushed to ranks 11/11/40/45 by lexical+SPLADE |
| kept | 0 | — |

The daemon's own 3 top-10 hits are *different* gold (lexical/tag/filename). **Dense and lexical catch DISJOINT gold
(9 vs 3); additive fusion + routing loses dense's 9 instead of keeping both.**

**PC-0 — k-dependent (refines the rubric's binary pre-check):**
- **@10 = pure wiring:** pure-dense 7.2% vs fused 2.4% — dense already ranks them; fusion/routing throws them away.
- **@200 = geometry, not wiring:** pure-dense 28.8% vs pool 24% (Δ+5 pp only) → the bulk is genuinely out of dense's
  top-200 → do NOT spend GPU-weeks on routing for it. Chromatin clean ({0:120, 1:1}) → no lifecycle demotion.

**The full fork, by band:**

| band | count | lever | cost |
|---|---:|---|---|
| **wiring-lost** (dense-top-10, routing/fusion dropped) | **9** | **semantic-aware routing + dense-dominant fusion** | **cheap, no GPU ← TEST NEXT** |
| 201–1k | 17 | widen-k (helps recall@k; limited at 12-gene delivery) | cheap, modest |
| 1k–20k | **41 (33%, biggest)** | query / **doc-side** expansion (facet miss) | med; doc-side GPU-light |
| >20k | 27 (22%) | re-embed / fine-tune (**Qwen3-0.6B** chosen) | expensive (residual geometry) |
| no-vector | 4 | data gap — gold absent/unembedded in corpus | investigate ingest |

**Lever ladder, rewritten by this run (supersedes "re-embed is the lever"):**
1. ~~rerank~~ — **exhausted** (net-zero, valid).
2. **NEW #1 — semantic-aware routing + dense-dominant fusion:** cheap config, **~4× ceiling on @10** (3 → ~12, ≈9.6%),
   no GPU. *Caveat (reconciles refuted-hyp #5): route-all **alone** didn't lift recall@200 — broadening the pool
   without changing the ranking lets fusion re-bury gold. It's the **combination** that's the lever. Cheap, testable.*
   **The thing to test next.**
3. **doc-side expansion** — the 33% facet-miss band (1k–20k); GPU-light at ingest.
4. **re-embed / fine-tune (Qwen3-0.6B)** — the >20k residual (22%) + the 11–200 dilution; expensive, **last** (gated on
   GB10 GPU-embed).
5. **fix the 4 no-vector data gaps** (ingest).

**Bottom line: re-embed is the RESIDUAL (22%), not the front-runner.** The free win is recovering dense's already-good
@10 ranks via semantic-aware routing + dense-dominant fusion — ~4× on semantic@10, no GPU, no re-index.
