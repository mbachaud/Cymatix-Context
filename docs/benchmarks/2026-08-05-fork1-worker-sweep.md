# Fork 1 worker sweep — GPU encoder daemon × executor size × bed scale

Branch `fork1/encoder-daemon` @ 5f95e9c, 2026-08-05/06. n≈100 samples/level
(dogfood 18×6, ERB carves 30×3), trim (`CYMATIX_DISABLE_HEADROOM=1`),
c=1,2,4,8 clients, daemon-ON only (no OFF arms this sweep — see the
slice-1 receipts doc for the ON/OFF A/B). Daemon on the RTX 3080 Ti via
`f:/tmp/venv-cuda` (torch 2.11+cu128), CUDA-verified in every pass
manifest before any bed ran; backend on main `.venv` with
`CYMATIX_EXECUTOR_WORKERS` ∈ {2,4,8}. Receipts:
`benchmarks/dogfood/sweep/n100_gdaemon_<bed>_x<N>.json` (+ two
CPU-daemon x2 reference receipts `n100_daemon_{dogfood,erb100k}_x2.json`).
Every arm daemon-log traffic-verified (6,845-8,195 `/encode` POSTs/pass).

## Headline: the daemon moved the W3.2 wall

W3.2 (2026-08-04, in-process encoders): executor 2/4/8 changed nothing —
qps pinned ≈1.7 by encoder/GIL contention. With encode off-process on the
GPU daemon, executor sizing works again:

| bed | peak qps x2 | x4 | x8 | x8/x2 |
|---|---|---|---|---|
| dogfood | 3.32 | 3.95 | 4.09 | +23% |
| erb10k | 2.24 | 2.74 | 2.87 | +28% |
| erb50k | 0.93 | 1.21 | 1.33 | +43% |
| erb100k | 0.56 | 0.75 | 0.81 | +45% |

The x2 curves flatline at c=4 (two server threads saturate); x4/x8 keep
climbing through c=8. Relative executor gains GROW with bed size — the
SQL/splice work that dominates large beds parallelizes across executor
threads once encode is out of the process.

## Full matrix (median ms / qps)

| bed · exec | c=1 | c=2 | c=4 | c=8 |
|---|---|---|---|---|
| dogfood x2 | 559 / 1.77 | 711 / 2.80 | 1232 / 3.18 | 2398 / 3.32 |
| dogfood x4 | 512 / 1.96 | 670 / 2.96 | 1039 / 3.82 | 2013 / 3.95 |
| dogfood x8 | 481 / 2.06 | 636 / 3.12 | 975 / 4.09 | 2110 / 3.77 |
| erb10k x2 | 676 / 1.47 | 936 / 2.13 | 1777 / 2.24 | 3589 / 2.21 |
| erb10k x4 | 713 / 1.39 | 956 / 2.07 | 1492 / 2.66 | 2917 / 2.74 |
| erb10k x8 | 678 / 1.48 | 931 / 2.14 | 1387 / 2.86 | 2760 / 2.87 |
| erb50k x2 | 1419 / 0.68 | 2067 / 0.92 | 4151 / 0.93 | 8290 / 0.93 |
| erb50k x4 | 1484 / 0.64 | 2070 / 0.92 | 3067 / 1.21 | 6199 / 1.21 |
| erb50k x8 | 1380 / 0.69 | 2018 / 0.95 | 2882 / 1.29 | 5592 / 1.33 |
| erb100k x2 | 2408 / 0.39 | 3403 / 0.55 | 6681 / 0.56 | 13582 / 0.55 |
| erb100k x4 | 2245 / 0.41 | 3316 / 0.56 | 4953 / 0.75 | 9733 / 0.75 |
| erb100k x8 | 2189 / 0.43 | 3277 / 0.57 | 4604 / 0.79 | 8984 / 0.81 |

Zero HTTP errors anywhere. **Sweet spot on this 8C/16T box: executor 4.**
x8 buys little over x4 and regresses dogfood c=8 (3.95→3.77 — 8 clients +
8 executors + daemon + uvicorn oversubscribe 16 threads; Task Manager
showed 91% all-core even at x4).

## GPU vs CPU daemon (x2 reference points)

dogfood c=1 782.6→559ms (−29%), peak qps 1.95 (CPU x2 best) → 3.95 (GPU
x4) ≈ 2×. erb100k c=1 2413→2408ms (nil — SQL/splice-bound, as the GPU
ladder found); its gains are concurrency-side only. The CPU sweep beyond
x2 was abandoned: with encode in-process on CPU the box sits at 91%
all-core and executor scaling has no headroom to expose (the W3.2 result,
reproduced).

## Rank stability under parallelism (the valid=False flags)

x4/x8 receipts carry per-level `valid=false` flags: recall deviates up to
±0.05 from the same run's c=1 (both directions; overlap_fraction ≥0.975
everywhere; errors 0). Mechanism: on CUDA, dense micro-batch SHAPE depends
on concurrent arrival order, and batched-GEMM low-mantissa drift across
batch shapes flips near-cutoff ranks — exactly the contract's documented
device caveat (CPU-path byte-identity held in the slice-1 receipts; the
final review predicted this failure signature). Quality-neutral in
expectation at this tolerance, but GPU-daemon serving is NOT rank-bit-stable
across concurrency levels. Latency/qps unaffected. Follow-up if tighter
stability is wanted: fixed-size dense batching (pad to max_batch) or
fp32-accumulation flags — slice-2 material.

## Bench-fidelity notes (cost this sweep real time)

- erb10k/erb50k stage beds predate the 2026-08-04 index fixes and can't
  get them under read-only serving — both re-carved from the blob
  (`f:/tmp/erb_carve/erb_{10k,50k}.db`, indexes + ANALYZE baked). One
  consistent needle set (30 carve needles) across all ERB beds; erb10k
  recall is floor-biased (10/30 needles have gold in a 10k carve) —
  latency unaffected, don't read its recall column as quality.
- erb250k/erb500k dropped mid-sweep (user call): c≥4 levels at those
  scales exceed sane wall-clock on one box (c=2 at 500k degraded
  super-linearly — 31GB bed vs 48GB RAM page-cache pressure; worth its own
  investigation before any fleet story at that scale).
- `TaskStop` on a detached Git-Bash job does NOT kill its tree on Windows:
  the orphaned script kept re-binding the bench port and one cleanup regex
  (`uvicorn`) matched the LIVE backend on 11437 (tray supervisor
  restarted it). Kill by port ownership + `run_server_scaling|encoder_daemon`
  only; the sweep script's own cleanup does this.
- CPU x2 receipts for erb10k/erb50k were casualties of the port collision
  (bench ran, receipt never written); dogfood/erb100k CPU x2 survive as
  the CPU-daemon reference points.

## Operating recommendation (this box)

GPU daemon + `CYMATIX_EXECUTOR_WORKERS=4`: dogfood-class 4.0 qps at c=8
(vs 1.7 pre-fork = 2.3×), erb100k 0.75 qps (vs 0.55 at x2, +36%), CPU
headroom left for ingest/tagging. Fleet math: at 51 bundles/s GPU daemon
capacity vs ~16 bundles/s consumed at dogfood 4 qps (×4 encodes/query),
the encoder supports ~3× this load before it's the wall.
