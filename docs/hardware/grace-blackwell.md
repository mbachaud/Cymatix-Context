# NVIDIA Grace + Blackwell (GB10 / DGX Spark / aarch64) — running cymatix-context

> Proposed landing path in the repo: `docs/hardware/grace-blackwell.md`

This page records what we hit running cymatix-context's dense embed/re-embed path on an
**NVIDIA DGX Spark (GB10 superchip, aarch64/Grace CPU + Blackwell GPU, compute capability
sm_121)** with a CUDA-13 PyTorch nightly, and the one-line, opt-in fix that makes it land —
so the next person doesn't burn 10 hours rediscovering a livelock.

## TL;DR

On GB10 the dense embedding pass **livelocks on the first CUDA batch** with the GPU pinned at
~96% utilization but only ~55 W. Set **`CUDA_LAUNCH_BLOCKING=1`** before importing torch and it
completes. This is **opt-in / default-off** so x86_64 / RTX boxes don't pay the synchronization
tax. Everything else is stock.

---

## Environment under test (what "GB10" meant here)

| | |
|---|---|
| Board | NVIDIA DGX Spark — GB10 Grace+Blackwell superchip |
| CPU arch | `aarch64` (`platform.machine() == "aarch64"`) |
| GPU | NVIDIA GB10, compute capability **sm_121** |
| Driver | 580 |
| PyTorch | `2.13.0.dev20260526+cu130` (CUDA 13.0 nightly) |
| transformers / sentence-transformers | 5.9 / 5.5.1 |
| triton | 3.7 |

(Verified live on the box: `torch 2.13.0.dev+cu130 | arch aarch64 | cuda 13.0 | device NVIDIA GB10`.)

---

## Symptom — the 10-hour livelock

The dense PASSAGE embedding/backfill pass hangs on its **very first `encode_batch`**:

- GPU shows **96% utilization** but draws only **53–56 W** (i.e. a *busy-wait*, not real compute —
  the GPU is spinning, not working).
- `py-spy` / faulthandler stacks show the process parked in a `sched_yield` busy-wait inside
  `libcuda`, reached through the fp16 GEMM → `scaled_dot_product_attention` → device→host copy.
- Throughput creeps **>8× slower** than the same encode run in isolation.
- An isolated CUDA encode (256 texts in ~15 s) and an encode+SQL-write loop both **complete fine** —
  only the full-shard driver path trips the livelock. A `--full` run would livelock on shard 1, i.e.
  it does NOT eventually finish; the "it's just slow, give it 3h" reading is wrong.

### Root cause (what it is, and what it is NOT)

This is a **platform/driver async-dispatch instability on sm_121**, not a bug in cymatix-context's
encode path. We proved it by diffing the faulthandler frame with and without our unrelated codec
changes — identical — so the application diff is exonerated. It lines up with reports of
async-launch instability on Blackwell-class sm_121 under CUDA-13 nightlies (cf. vLLM issue #37431).

It is specifically **NOT**:
- a flash-attention build problem (we already use SDPA, see prerequisites), nor
- a memory-allocator issue (no `PYTORCH_CUDA_ALLOC_CONF` tuning was needed), nor
- a CUDA-stream configuration issue (no explicit stream/`set_device` shim was needed).

We searched for and did **not** need any allocator or stream shims; the launch-blocking env var was
the entire platform fix.

---

## The fix — `CUDA_LAUNCH_BLOCKING=1`, set before torch import, opt-in

Forcing **synchronous kernel launches** serializes CUDA dispatch and sidesteps the async-dispatch
busy-wait. The embed pass then runs to completion (in our run: 100% dense coverage, verify cosine
0.999997, full 850k-vector re-embed completed cleanly).

```python
import os
# Must be set BEFORE any torch / CUDA import — CUDA_LAUNCH_BLOCKING is read at CUDA-runtime init;
# setting it after the first CUDA context is a no-op.
os.environ.setdefault("CUDA_LAUNCH_BLOCKING", "1")
# ... only now import torch / sentence_transformers / the codec ...
```

Notes that matter:
- **`setdefault`, not `=`** — respects an operator who already exported the var, and keeps the
  in-code value a *fallback*.
- **Ordering is load-bearing** — it must precede the first torch/CUDA import or it does nothing.

### Two important properties

1. **Embeddings are byte-identical with the flag on vs off** (`max_abs_diff = 0.0`). Synchronous
   dispatch changes *timing only*, never the kernels or their numerics — so a genome embedded with
   the flag still reproduces a genome embedded without it. (This is why we picked it over
   `attn_implementation=eager` / force-SDPA-MATH / a torch pin: those change the attention kernel and
   would only hold cosine ≈0.9999 to the reference, which is unacceptable for a reproducible re-embed.)

2. **It is NOT free** — serialization removes async overlap, so it does **not** restore full
   throughput; on GB10 the pass ran at the ~55 W serialized floor (the full re-embed took ~10h, not
   the hoped ~3h). On a healthy x86_64 / RTX box that does not livelock, turning this on would just
   pay the synchronization tax for no benefit.

**Therefore it must be opt-in / default-off**, gated behind an explicit signal so only
Grace+Blackwell / GB10 operators enable it.

### Proposed env handshake (so it's not tribal knowledge)

Default-off; cymatix only sets `CUDA_LAUNCH_BLOCKING` when the operator opts in, and never overrides a
value the operator already exported:

```python
# cymatix-context platform handshake (default-OFF; byte-identical for everyone who leaves it unset)
import os
if os.environ.get("CYMATIX_CUDA_LAUNCH_BLOCKING", "0") == "1":
    os.environ.setdefault("CUDA_LAUNCH_BLOCKING", "1")
```

Operators on GB10 then run with `CYMATIX_CUDA_LAUNCH_BLOCKING=1`. Optionally, ship a
`cymatix-context[gb10]` extra whose docs point here and whose import path sets the same default — see
OPEN_QUESTIONS for the exact env-var name / extra-name decision (placeholder `CYMATIX_CUDA_LAUNCH_BLOCKING`).

x86_64 behavior is unchanged: with the var unset, cymatix imports torch exactly as before.

---

## Environment / system prerequisites (separate from the code fix above)

These are **host setup steps**, not code changes — but they bit us and are easy to miss on a fresh
aarch64 / Grace box:

- **`python3.12-dev` headers must be installed.** Without them, Triton's JIT fails to compile
  `cuda_utils.c` at `#include <Python.h>` and GPU embedding dies with
  `fatal error: Python.h: No such file or directory`. Fix:

  ```bash
  sudo apt-get install -y python3.12-dev   # match your Python minor version exactly (3.12.3 here)
  ```

  Historical note: this was at one point mis-diagnosed as a Blackwell/sm_121/`libcuda` link
  incompatibility. It is **not** — pure-torch cuBLAS GPU always worked; the `-l:libcuda.so.1` link
  error was a red herring, since the build died at the preprocessor step before ever linking. The
  real missing piece was the Python dev headers.

- **flash-attention-2 is unavailable on sm_121** (won't build). cymatix-context already defaults the
  dense codec to `attn_implementation="sdpa"` on all platforms, so no action is required — just don't
  try to force `flash_attention_2` on GB10.

---

## Quick checklist for a new GB10 / DGX Spark box

1. `sudo apt-get install -y python3.12-dev` (version-matched).
2. Leave the dense codec on SDPA (the default) — do not force flash-attention-2.
3. For dense embed / re-embed runs, opt into launch-blocking: `CYMATIX_CUDA_LAUNCH_BLOCKING=1`
   (or `CUDA_LAUNCH_BLOCKING=1`), set **before** torch import.
4. Expect serialized throughput (lower W, longer wall-clock) — embeddings are unaffected and
   byte-identical to a non-blocking run.
