# Hardware Detection & Device Backend — Design Spec

**Date:** 2026-05-04
**Status:** Draft → review
**Owner:** SwiftWing21
**Related:** [native-observability-sidecar (#16)](2026-05-04-native-observability-sidecar-design.md), [foveated-splice (next)](#9-relationship-to-other-work)

---

## 1. Overview

Centralize device detection, VRAM-aware batch sizing, and graceful fallback for all torch-using backends in `helix-context`. Today, three backends each call `torch.cuda.is_available()` independently, no VRAM is inspected, batch sizes are static, and only NVIDIA + CUDA-built torch is supported in practice. The goal is to make helix work top-to-bottom — phone-class to server-class — by detecting available hardware once at startup, picking sensible batch sizes, and degrading gracefully (with surfacing) when explicit configs don't match the host.

Two PRs ship the work:

- **PR1 — Centralization + VRAM-aware batching.** New `helix_context/hardware.py` module, `[hardware]` config section, chunked batches in deberta/splade/nli/sema, fallback surfacing. NVIDIA + CPU only; non-NVIDIA paths parse but resolve to CPU fallback.
- **PR2 — MPS + ROCm + CI workflow.** Wires up the alt-device branches in the picker, adds GHA workflow with a macOS-runner MPS smoke test, opt-in env-var gates for ROCm hardware tests. Intel XPU deferred to enhancement-request.

---

## 2. Goals & non-goals

### Goals

- **One source of truth** — `helix_context/hardware.py` is the only place that calls `torch.cuda.is_available()` (and equivalents). All four backends consult it.
- **Auto-mode that just works** — `device = "auto"` (the default) picks the best available device on any host without configuration.
- **VRAM-aware batch sizing** — the same code path produces sensible batch sizes on a 4 GB GPU and a 24 GB GPU; OOM on small cards stops being the default failure mode.
- **Graceful degradation** — explicit-device mismatches log loudly, expose state via `/health` and a tray balloon, but never block startup or `/context` requests.
- **CPU support across architectures** — Intel x86, AMD x86, ARM (Linux/macOS) all work via torch's transparent CPU dispatch. Hardware report describes the platform honestly.
- **Alt-GPU readiness** — MPS (Apple Silicon) and ROCm (AMD GPU on Linux) ship as named device backends in PR2, with appropriate "capable but unverified" disclaimers per non-NVIDIA path until validated on real hardware.

### Non-goals

- **Multi-GPU sharding.** Auto-mode picks the single GPU with the most free VRAM and reports the others; helix does not split work across multiple devices.
- **Intel XPU support.** Hardware is rare today; deferred to enhancement-request via the new `.github/ISSUE_TEMPLATE/enhancement.md` template (PR2). The picker recognizes the device-type string but resolves to CPU fallback if requested.
- **OOM-recovery retry loops.** Catching `CudaOutOfMemoryError` and halving batch sizes on the fly is rejected as over-engineered; bug-hiding; non-deterministic latency.
- **fp16/bf16 dtype optimization.** Kept fp32 in PR1 to avoid entangling dtype perf with the centralization. PR2 may add minimal dtype handling only as needed for MPS smoke-test stability.
- **Multi-process device negotiation.** Helix runs as a single process per genome; if a user spawns multiple, each detects independently.

---

## 3. Architecture

### Verification posture

- **NVIDIA + CUDA-built torch:** validated on this rig (RTX-class consumer card, Windows 11, CUDA 12.1 wheel). PR1 bench gate measured −20.74 s p95 vs master baseline.
- **ROCm + AMD GPU on Linux:** wired in PR1, *capable but unverified* — no maintainer with ROCm hardware has run the opt-in `tests/test_hardware_rocm.py` against real silicon. Same posture as the native-observability sidecar's macOS/Linux scripts (#16 §3 non-goals). Contributors with ROCm rigs are invited to validate via `HELIX_TEST_ROCM=1 pytest tests/test_hardware_rocm.py`.
- **MPS / Apple Silicon:** wired in PR1, *capable but unverified* — same posture as ROCm. The smoke-test scaffold lives at `tests/test_hardware_mps_smoke.py` for any future maintainer with a Mac to run locally, but it is **not** exercised in CI: the GHA macos-14 runner's MPS shared pool OOMs at ~1 GiB peak on every cross-encoder forward pass we tried (deberta-v2-xsmall, MiniLM-L-6), even with `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0`. PR2 attempted the smoke gate per spec §8.2 but the runner's resource ceiling defeats it; the test self-skips when `GITHUB_ACTIONS=true`. Re-enabling on CI is tracked as a follow-up that needs a real Apple-Silicon dev rig to validate against first. Contributors with Macs are invited to run `pytest tests/test_hardware_mps_smoke.py -v` locally and report findings.
- **CPU on x86 (Intel/AMD) and ARM (Linux/macOS):** validated indirectly — the CPU branch is the fallback of last resort and is exercised on every CI run (Linux + Windows runners) via the unit suite.
- **Intel XPU:** not wired; deferred to enhancement-request via `.github/ISSUE_TEMPLATE/enhancement.md`. The picker recognizes the device-type string but resolves to CPU fallback per §2 non-goals.

### 3.1 Module shape

New module: **`helix_context/hardware.py`**.

```python
@dataclass(frozen=True)
class HardwareInfo:
    device: str                       # "cuda:0" | "cuda:1" | "rocm:0" | "mps" | "cpu"
                                       # — string form so HardwareInfo is constructible
                                       # even if torch failed to import (§5.5).
                                       # Backends call torch.device(info.device).
    device_type: str                  # "cuda" | "rocm" | "mps" | "cpu" — picker key
    device_name: str                  # e.g. "NVIDIA GeForce RTX 4090"
    vram_total_gb: float | None       # None for cpu — used by batch-size table
    vram_free_gb: float | None        # None for cpu / mps — INFORMATIONAL ONLY
                                       # (cached at startup; see "free vs total" below)
    cpu_arch: str                     # "x86_64" | "arm64" | "aarch64"
    cpu_brand: str                    # see "cpu brand source" below
    system_ram_gb: float
    requested_device: str             # "auto" | "cuda" | ... — what user asked for
    fallback_reason: str | None       # set when requested_device != device_type
    batch_size_overrides: Mapping[str, int]   # parsed from [hardware] batch_sizes
                                       # config; consulted by recommended_batch_size().

def get_hardware() -> HardwareInfo: ...     # cached singleton
def reset_for_test() -> None: ...           # test-only cache reset
def recommended_batch_size(model: str) -> int: ...   # consults singleton; see §7.3
```

**Singleton with lazy first-call computation.** Importing the module does not trigger any torch CUDA-driver loading; the first call to `get_hardware()` performs detection and caches the result for the process lifetime. `reset_for_test()` clears the cache for unit tests that mock torch internals.

**No public mutation.** The dataclass is `frozen=True`. Overrides flow through config or env var (Section 4), not via setters.

**Free VRAM vs total VRAM.** `vram_free_gb` is captured *once* at startup and reused; the actual free VRAM at request time will differ. **Batch-size sizing therefore keys on `vram_total_gb`** (Section 7.1), which is invariant for the GPU. `vram_free_gb` is reported in the startup banner and `/health` for visibility only — useful to spot "the GPU is already half-consumed by another process at startup", not to drive runtime decisions. If we ever need free-VRAM-aware behavior, it would re-poll `mem_get_info()` per-call rather than reading the cached field.

**`device` as string, not `torch.device`.** Storing the device as a string makes `HardwareInfo` constructible in the torch-not-installed posture (§5.5) where `torch.device` is unavailable. Backends construct `torch.device(info.device)` when they need the actual object — one extra line at the call site, zero coupling at the dataclass.

**`cpu_brand` source.** Resolved in this order:

1. `cpuinfo.get_cpu_info()["brand_raw"]` (from `py-cpuinfo`, added to the `launcher` extra)
2. `platform.processor()` if non-empty
3. `"unknown CPU"` as terminal fallback

`py-cpuinfo` is MIT-licensed, single-file, ~50 KB, no native deps. Adding it as a dep is part of PR1's `pyproject.toml` change.

**`batch_size_overrides` field.** Parsed from `[hardware] batch_sizes` at config-load time; empty mapping when `batch_sizes = "auto"` (default). Consulted by `recommended_batch_size()` to enforce the override hierarchy (Section 7.3).

### 3.2 Affected backends

Today's call sites for `torch.cuda.is_available()`:

| File | Line | Pattern |
|---|---|---|
| `helix_context/deberta_backend.py` | 61 | `device="auto"` constructor arg |
| `helix_context/nli_backend.py` | 52 | `device="auto"` constructor arg |
| `helix_context/splade_backend.py` | 46 | module-level `_ensure_loaded` |

`helix_context/sema.py:128` accepts a `device` parameter and passes it to `SentenceTransformer(...)`. No caller plumbs config to it today (caller `context_manager.py:312` passes `config.ribosome.device` to `DeBERTaRibosome`, which passes it to `NLIClassifier` but not to `SemanticEncoder`).

After PR1: every backend consults `get_hardware()` for its device; constructor `device` arguments still accept overrides for tests but default to `None`-meaning-"use hardware module".

---

## 4. Config schema

### 4.1 New `[hardware]` section

```toml
[hardware]
# device picker. "auto" picks best-available (cuda → rocm → mps → cpu).
# Explicit values fall back loudly if the backend isn't usable; helix
# never blocks on hardware mismatch — see /health for fallback state.
device = "auto"        # auto | cuda | rocm | mps | cpu

# Batch-size policy. "auto" consults the VRAM/RAM-aware table in
# helix_context/hardware.py. Override with explicit ints when tuning:
#   batch_sizes = { rerank = 16, splice = 32, splade = 8, nli = 8 }
batch_sizes = "auto"

# Soft-warn threshold. Below this, /health returns a "low_vram" hint and
# the tray surfaces a one-time balloon. Set to 0 to disable.
low_vram_threshold_gb = 4.0
```

### 4.2 Env-var override

`HELIX_DEVICE=cpu` (case-insensitive: `auto` / `cuda` / `rocm` / `mps` / `cpu`). One-shot override, matches existing `HELIX_OBSERVABILITY=0` and `HELIX_ABSTAIN_DISABLE=1` patterns.

**Resolution order:**
1. `HELIX_DEVICE` env var (highest)
2. `[hardware] device` config
3. `"auto"` default

### 4.3 Deprecation of `[ribosome] device`

`[ribosome] device` is read for one release with a backwards-compat shim:

- If `[ribosome] device` is present and `[hardware] device` is **not**, helix uses the ribosome value and logs a single `WARNING` from `helix.config` at startup pointing the user to migrate (see §9.1 for the wording).
- If both `[ribosome] device` and `[hardware] device` are present, `[hardware] device` wins and the deprecation warning still fires noting the override.
- After one release, the shim is removed (one-line change in `config.py`).

Splade and sema have no current device config, so nothing to deprecate there — they newly consult `[hardware]` (clean addition).

---

## 5. Detection & fallback logic

### 5.1 Auto-mode picking order

Each candidate is checked for both **wheel-level availability** (does the installed torch build advertise this backend?) and **per-device probed usability** (is at least one device of that backend healthy enough to round-trip a tensor?). The picker walks the list in order and stops at the first candidate that passes both:

1. **CUDA** — `torch.cuda.is_available()` AND `torch.cuda.device_count() > 0` AND at least one device probes successfully (§5.2 / §5.3)
2. **ROCm** — `torch.version.hip is not None` AND `torch.cuda.device_count() > 0` AND at least one device probes successfully
3. **MPS** — `torch.backends.mps.is_available()` AND `torch.backends.mps.is_built()` AND probe succeeds
4. **CPU** — terminal fallback; always succeeds

**Mutual-exclusion note.** A given torch wheel is built for exactly one of CUDA / ROCm; it cannot serve both. On real hardware, `torch.version.hip is not None` is True only on a ROCm build (where `torch.cuda.is_available()` reports `True` and surfaces HIP devices through the same `torch.cuda` API). So in practice steps 1 and 2 are mutually exclusive at the wheel level — only one of them ever passes its first check on any single host. The fall-through-on-probe-failure path within auto-mode therefore *normally* drops straight to MPS or CPU, since the unsupported sibling backend short-circuits on its availability check before being probed. Mocked tests (§8.1) exercise the fall-through logic by simulating multiple candidates simultaneously available — that is a logic test, not a real-world configuration.

### 5.2 Probe protocol

For each candidate device, run a 1-element zero-tensor round-trip in a try/except, **targeting the specific device index** (not just the device-type string):

```python
def _probe(device_str: str) -> tuple[bool, str | None]:
    """device_str = 'cuda:0' / 'cuda:1' / 'mps' / 'cpu'."""
    try:
        t = torch.zeros(1).to(device_str)
        _ = t.cpu()
        return True, None
    except Exception as exc:
        return False, f"probe failed: {type(exc).__name__}: {exc}"
```

Probe is ~1 ms on healthy hardware. Catches "looks present, isn't usable" failure modes (stale CUDA driver, detached eGPU, container-without-device-passthrough, dead device in a multi-GPU box).

### 5.3 Multi-GPU device selection

For CUDA / ROCm (which share the `torch.cuda.device_count()` API):

1. Enumerate all device indices `0..device_count() - 1`
2. For each index, attempt `torch.cuda.mem_get_info(i)` — devices that raise are dead and skipped
3. Among the live devices, pick the one with the most **free** VRAM at startup (free here is fine; it's a one-shot pick, not a runtime budget)
4. Probe `cuda:N` (or the equivalent on ROCm) for the chosen N — if probe fails, repeat from step 2 with the next-best device, and so on. If no devices probe, the candidate is rejected and auto-mode falls through.
5. Set `info.device = "cuda:N"` for the winner. Log all enumerated devices (live and dead) in the startup banner.

This avoids the "device 0 is broken, device 1 is healthy, but probing 'cuda' (default 0) rejects CUDA entirely" failure. Users who want to pin a specific card can still use `CUDA_VISIBLE_DEVICES` — that masks devices before the picker even sees them.

### 5.4 Explicit-device fallback policy

If the user set `device = "cuda"` (or env var) and probe fails:

- Fall back **directly to CPU** (skip rocm/mps — user said cuda, downgrade to cpu is the smallest surprise)
- Set `fallback_reason = "requested 'cuda' but probe failed: <reason>"`
- All three surfacing channels fire (Section 6)

Same posture for explicit `mps` / `rocm`: probe failure → CPU. **Never block; always degrade.**

### 5.5 Torch-not-installed posture

If `import torch` raises (bare `helix-context` install without the ML extras):

- `get_hardware()` returns a synthetic CPU-only `HardwareInfo` with `device_name = "torch unavailable"`
- A clear log line surfaces the missing extras
- Backends that require torch (deberta/nli/splade/sema) raise their existing `ImportError` when called — no change

This makes the helix server survivable on a bare install; only the ML-using endpoints fail.

---

## 6. Surfacing fallback state

When `requested_device != device_type` (loud-fallback active), three independent channels surface it.

### 6.1 Startup logs

A `WARNING` from the `helix.hardware` logger:

```
WARNING helix.hardware - device fallback: requested 'cuda' → using 'cpu'.
  Reason: torch.cuda.is_available() returned False (no CUDA driver).
  To suppress, set [hardware] device = "auto" or "cpu" in helix.toml.
```

An `INFO`-level startup banner always fires regardless of fallback:

```
INFO helix.hardware - device=cuda (NVIDIA GeForce RTX 4090, 24.0 GB total / 22.4 GB free);
  cpu=AMD Ryzen 9 7900X (x86_64, 64.0 GB system);
  recommended batches: rerank=64 splice=128 splade=32 nli=32
```

### 6.2 `/health` endpoint

`helix_context/server.py:health()` adds a `hardware` block to its JSON response:

```json
{
  "ok": true,
  "hardware": {
    "device": "cpu",
    "device_name": "AMD Ryzen 9 7900X",
    "requested_device": "cuda",
    "fallback_active": true,
    "fallback_reason": "torch.cuda.is_available() returned False (no CUDA driver)",
    "vram_total_gb": null,
    "system_ram_gb": 64.0,
    "low_vram_warning": false
  }
}
```

`helix_health_check` (and any other consumer of `/health`) gets fallback state for free.

### 6.3 Tray balloon (launcher-only)

Same pattern as the native-observability "install pending" balloon in `helix_context/launcher/tray.py`. A balloon fires once per state-change combination:

- Fires on launcher start where `fallback_active == true`
- Buttons: "Don't show again" / "Open helix.toml"
- Sentinel file at `<state_dir>/.hardware-fallback-acknowledged-{requested}-{active}` dedupes
- A new combination (e.g., user fixed CUDA → no balloon; later they unplug GPU and now MPS falls to CPU → new balloon) re-fires

**Scope:** the balloon channel covers `fallback_active == true` only — the case where the user explicitly asked for a device they didn't get, and we want to make sure they know. **Low-VRAM warnings are NOT balloon-surfaced**; they're a hint, not a fault, and would be annoying to bubble. Low-VRAM stays on logs + `/health` (`hardware.low_vram_warning: true`).

**Headless / server posture:** the balloon channel is launcher-only. Headless server deployments rely on logs (§6.1) and `/health` (§6.2) — both fire identically regardless of whether a tray exists.

### 6.4 Why three channels

- Logs miss eyes that don't grep
- `/health` misses users who never poll it
- Tray balloons miss users running helix as a server

Together they catch every audience without spam (one balloon per state-change, not per launch). The channels degrade independently — a tray-less server still gets logs + `/health`; a user who never polls `/health` still sees the log line + the balloon.

---

## 7. VRAM-aware batch sizing

### 7.1 Lookup table

The table is keyed on **`vram_total_gb`** for CUDA/ROCm (invariant per GPU; safe across runs) and on **`system_ram_gb`** for MPS (shared with system) and CPU. Free VRAM at startup (§3.1) is informational only and never drives the table.

```python
# (device_type, ram_tier_gb_min) → batch sizes per model
_BATCH_TABLE: dict[tuple[str, float], dict[str, int]] = {
    # CUDA / ROCm tiers — keyed on TOTAL VRAM
    ("cuda", 24.0): {"rerank": 64, "splice": 128, "splade": 32, "nli": 32},
    ("cuda", 12.0): {"rerank": 32, "splice": 64,  "splade": 16, "nli": 16},
    ("cuda",  8.0): {"rerank": 16, "splice": 32,  "splade":  8, "nli":  8},
    ("cuda",  4.0): {"rerank":  8, "splice": 16,  "splade":  4, "nli":  4},
    ("cuda",  0.0): {"rerank":  4, "splice":  8,  "splade":  2, "nli":  2},  # < 4GB GPU
    ("rocm", 24.0): {"rerank": 64, "splice": 128, "splade": 32, "nli": 32},
    ("rocm", 12.0): {"rerank": 32, "splice": 64,  "splade": 16, "nli": 16},
    ("rocm",  8.0): {"rerank": 16, "splice": 32,  "splade":  8, "nli":  8},
    ("rocm",  4.0): {"rerank":  8, "splice": 16,  "splade":  4, "nli":  4},
    ("rocm",  0.0): {"rerank":  4, "splice":  8,  "splade":  2, "nli":  2},
    # MPS — keyed on system_ram_gb (MPS shares system RAM)
    ("mps",  16.0): {"rerank": 16, "splice": 32,  "splade":  8, "nli":  8},
    ("mps",   8.0): {"rerank":  8, "splice": 16,  "splade":  4, "nli":  4},
    ("mps",   0.0): {"rerank":  4, "splice":  8,  "splade":  2, "nli":  2},  # defensive floor
    # CPU — keyed on system_ram_gb, conservative defaults
    ("cpu",  16.0): {"rerank":  8, "splice": 16,  "splade":  4, "nli":  4},
    ("cpu",   8.0): {"rerank":  4, "splice":  8,  "splade":  2, "nli":  2},
    ("cpu",   0.0): {"rerank":  2, "splice":  4,  "splade":  1, "nli":  1},
}
```

`recommended_batch_size("rerank")` finds the highest tier row whose threshold ≤ `vram_total_gb` (CUDA/ROCm) or `system_ram_gb` (MPS/CPU), reads the model column.

**Calibration.** Numbers are starting points keyed on rough rules of thumb (deberta-v3-small at 256 max-len ≈ 60 MB activation per batch item at fp32; halve again for safety). PR1's bench gate (Section 9) verifies they don't regress on our 24 GB rig. Lower tiers ship as conservative heuristics; users on those cards can report back via the enhancement template.

### 7.2 Where the table is consumed

| Backend | Today | After PR1 |
|---|---|---|
| `deberta_backend.py:re_rank` | tokenizes ALL pairs in one call | chunked: `for i in range(0, len(pairs_a), bs):` |
| `deberta_backend.py:splice` | same | same |
| `splade_backend.py:encode_batch` | static `batch_size: int = 16` default | default = `recommended_batch_size("splade")`; explicit caller still wins |
| `nli_backend.py:classify_batch` | tokenizes all pairs | chunked |
| `sema.py` (sentence-transformers) | pass-through `batch_size` | optional pass-through; not gated by bench |

The chunked pattern already exists in `splade_backend.encode_batch` (line 116) and is the template for the others.

### 7.3 Override hierarchy for batch sizes

The hierarchy lives partly in `recommended_batch_size()` and partly at the call site. Resolution per model name:

1. **Explicit caller-passed `batch_size=` argument** at the backend's call site (e.g., a test passing `batch_size=2`). Bypasses `recommended_batch_size()` entirely. Preserved verbatim from existing test patterns.
2. **`[hardware] batch_sizes = { rerank = 16 }` config dict** for the named model. Read at config-load and stored in `HardwareInfo.batch_size_overrides`. `recommended_batch_size("rerank")` checks this first and short-circuits the table lookup if the key is present.
3. **Auto from the table** (default). The table is consulted only when steps 1 and 2 don't resolve.

Function shape:

```python
def recommended_batch_size(model: str) -> int:
    info = get_hardware()
    if model in info.batch_size_overrides:
        return info.batch_size_overrides[model]
    tier_key = info.vram_total_gb if info.device_type in {"cuda", "rocm"} else info.system_ram_gb
    return _lookup_table(info.device_type, tier_key, model)
```

Backends call `recommended_batch_size(model)` with no arguments other than the model name, then optionally override via the call-site `batch_size=` parameter when present.

---

## 8. Testing strategy

### 8.1 Layer 1 — Mocked-torch unit tests

`tests/test_hardware.py`. Runs everywhere (Linux/macOS/Windows, NVIDIA-or-not). Mocks `torch.cuda.is_available`, `torch.cuda.device_count`, `torch.cuda.get_device_name`, `torch.cuda.mem_get_info`, `torch.backends.mps.is_available`, `torch.backends.mps.is_built`, `torch.version.hip`, plus the `torch.zeros(1).cuda()` probe.

**Coverage:**

- Auto-mode picks correct device for each `(cuda?, rocm?, mps?)` matrix combination — including the realistic-build cases (only one of CUDA/ROCm advertised at a time) and the mocked-multi case (both advertised, exercises fall-through logic per §5.1's mutual-exclusion note)
- Multi-GPU CUDA: dead device-0 + healthy device-1 → picker selects `cuda:1`, never rejects CUDA outright (regression pin for B2)
- Probe-failure on CUDA in auto mode (with all CUDA devices dead) falls through to the next advertised candidate, not to CPU directly
- Probe-failure on explicit `device = "cuda"` falls back to CPU directly (not ROCm/MPS) — the asymmetry from §5.4
- `recommended_batch_size` returns expected value for each tier boundary (boundary tests at 4.0 / 8.0 / 12.0 / 24.0)
- `recommended_batch_size` consults `info.batch_size_overrides` first and returns the override value when set; falls through to the table when the model name isn't in the override dict
- `vram_total_gb` drives the table lookup; mutating `vram_free_gb` between calls does NOT change the result (regression pin for B3)
- HardwareInfo cache is reset by `reset_for_test()`
- `HELIX_DEVICE` env var beats `[hardware] device` config beats default
- `[ribosome] device` deprecation warning fires once per process; `[hardware] device` overrides ribosome-device when both are set; warning still fires noting the override
- `cpu_brand` source order: py-cpuinfo present → uses it; py-cpuinfo absent + `platform.processor()` non-empty → uses that; both absent → `"unknown CPU"` (no crash)

**Mocking pattern:** `monkeypatch.setattr("torch.cuda.is_available", lambda: True)` — same idiom used in `tests/test_observability_paths.py` for platformdirs.

### 8.2 Layer 2 — GHA workflow

`.github/workflows/ci.yml` (new). Three jobs:

```yaml
jobs:
  test-linux:
    runs-on: ubuntu-latest
    # Full unit suite, mocked-torch coverage.

  test-macos-mps:
    runs-on: macos-14   # M1 Apple Silicon, MPS-capable
    # Full unit suite + 1-iteration MPS smoke test:
    # load deberta tokenizer, do a 2-pair forward pass on mps,
    # assert output shape. Catches MPS regressions on every PR.

  test-windows:
    runs-on: windows-latest
    # Full unit suite — catches Windows-specific path/encoding bugs
    # (we already had 5 of those on the sidecar PR).
```

CPU-only runners on all three (no GHA NVIDIA hosts on the free tier). Total CI ≈ 4–5 min per PR.

### 8.3 Layer 3 — Opt-in real-hardware tests

Pytest markers `requires_rocm` and `requires_real_cuda` (custom). Skip-by-default; check env vars `HELIX_TEST_ROCM=1` / `HELIX_TEST_CUDA=1` and only run when set.

Test files: `tests/test_hardware_rocm.py`, `tests/test_hardware_cuda_real.py`. Anyone with the appropriate hardware can run the full path via the env var without modifying test plumbing.

### 8.4 What is NOT tested

- We do not test that an actual deberta forward pass on a 4 GB GPU avoids OOM. The batch-size table is a heuristic; validation is the bench gate (Section 9), not a unit test.
- We do not test multi-GPU sharding (out of scope).
- We do not test fp16/bf16 dtype paths in PR1; that is a separate optimization track.

---

## 9. Rollout

### 9.1 PR1 — Centralization + VRAM-aware batching

**Branch:** `feat/hardware-detection`

**Files added:**
- `helix_context/hardware.py`
- `tests/test_hardware.py`

**Files modified:**
- `helix_context/config.py` — parse `[hardware]` section, deprecation read for `[ribosome] device`
- `helix.toml` — add documented `[hardware]` block
- `helix_context/deberta_backend.py`, `nli_backend.py`, `splade_backend.py`, `sema.py` — consult `get_hardware()`, chunk batches
- `helix_context/server.py` `health()` — add `hardware` block
- `helix_context/launcher/tray.py` — fallback balloon + sentinel-file dedup
- `pyproject.toml` — add `py-cpuinfo>=9.0` to the `launcher` extra (see §3.1 for the cpu_brand source rationale)

**No new device backends.** `device = "rocm"` or `"mps"` parses cleanly but resolves to CPU fallback in PR1. The picker's auto-mode order documents the future hooks but only the CUDA branch is wired up.

**Bench gate (mandatory before merge):** GPQA Diamond n=20 procedure (same shape as the sidecar PR's Task 14). The bench artifact `benchmarks/results/gpqa_native_n20_2026-05-04.json` from the sidecar PR was kept local-only; it is NOT on master. The bench-gate procedure for PR1:

1. Re-run the n=20 native-stack bench against `master` HEAD before applying PR1's changes — capture as the local baseline JSON
2. Run the same n=20 bench against PR1's HEAD
3. Compare same-IDs p95 delta — must be ≤ 5 s

This avoids relying on a stale local artifact, and gives a clean baseline at the moment PR1 opens. Both runs use the same native sidecar instance to keep observability constant. The risk being measured: chunked batch processing in deberta might add per-chunk overhead that wasn't there before.

**Deprecation behavior:** If `[ribosome] device` is present and `[hardware]` is absent:

```
WARNING helix.config - [ribosome] device is deprecated; move to [hardware] device.
  Using ribosome.device='cuda' for now.
```

Logged once per process at config load.

### 9.2 PR2 — MPS + ROCm + CI workflow

**Branch:** `feat/hardware-mps-rocm`

**Files added:**
- `.github/workflows/ci.yml`
- `tests/test_hardware_rocm.py` (skip-marked)
- `tests/test_hardware_cuda_real.py` (skip-marked)
- `.github/ISSUE_TEMPLATE/enhancement.md` (general enhancement template, with Intel XPU as the natural first user)

**Files modified:**
- `helix_context/hardware.py` — wire ROCm + MPS into auto picker, populate `device_type`
- `_BATCH_TABLE` — fill in `("rocm", *)` and `("mps", *)` rows
- Backends — most need no change (they consult `get_hardware().device` from PR1); fp16/bf16 fixups only if the macOS-runner smoke test surfaces dtype issues
- This spec — update §3 disclaimers ("ROCm capable but unverified" matching native-sidecar §3 posture)

**No bench gate on PR2.** New device backends are inactive on our rig (no MPS, no ROCm), so bench numbers are unchanged from PR1's gate. The macOS-runner MPS smoke test serves as the integration check; ROCm relies on the opt-in marker until verified on hardware.

### 9.3 Rollout posture

- PR1 ships first, lands on master after bench gate
- PR2 lands after PR1, opens the door for non-NVIDIA users without changing NVIDIA behavior
- The `[ribosome] device` deprecation read survives both PRs; removal is a follow-up one-liner in a later release

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Chunked batches in deberta fire more torch kernel launches → throughput penalty on the 24 GB tier where "all-at-once" happened to fit | Bench gate on PR1; if regressed, fix is `min(recommended, len(input))` — chunk only if needed |
| MPS dtype quirks (fp16 hardware, float32 ops fall back to CPU silently) | macOS smoke test in CI catches the cases that matter for our backends |
| ROCm "capable but unverified" carries reputational risk | Same disclaimer pattern as native-sidecar's macOS/Linux scripts; PR2 body explicit; opt-in marker means contributors with hardware can validate |
| Probe in `_probe()` slows startup measurably on slow GPUs | 1 ms typical on healthy hardware; if measured slow, downgrade to `is_available()` only with a configurable `HELIX_HARDWARE_PROBE=0` |
| `[ribosome] device` deprecation surprises existing users | Read shim covers one release with a clear warning; removal scheduled, not silent |

---

## 11. Relationship to other work

- **Native observability sidecar (#16, merged):** unchanged. The hardware module logs into the same OTel/Prometheus stack via the existing logger; no observability config changes.
- **Foveated splice (next, branch `spec/foveated-splice`):** foveated may want VRAM-aware batch sizing for its rank-scaled compression step. PR1 makes that available via `recommended_batch_size("foveated")` — the table just gains a new column when foveated lands. No coordination needed.
- **Update-check / version-headroom (just merged on master):** unrelated.

---

## 12. Open questions

- *(none at spec close — all resolved during brainstorm)*

---

## 13. References

- [Native observability sidecar design](2026-05-04-native-observability-sidecar-design.md) — pattern source for tray balloon + capable-but-unverified disclaimer
- [PyTorch device docs](https://pytorch.org/docs/stable/notes/cuda.html) — multi-GPU + `mem_get_info`
- [PyTorch MPS backend](https://pytorch.org/docs/stable/notes/mps.html) — Apple Silicon support
- [PyTorch ROCm](https://pytorch.org/get-started/locally/) — installation and `torch.version.hip`
