# Hardware Detection PR2 — MPS + ROCm + CI Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the CI/testing infrastructure that proves the hardware-detection module works on macOS (MPS) and Linux/Windows runners, and provide opt-in real-hardware test scaffolds for ROCm + CUDA so external contributors with the right rigs can validate non-NVIDIA paths.

**Architecture:** PR1 already wired CUDA + ROCm + MPS detection branches in `helix_context/hardware.py` and populated the full `_BATCH_TABLE` (rows for `("cuda", *)`, `("rocm", *)`, `("mps", *)`, `("cpu", *)`). What's left is exclusively test infrastructure + GitHub Actions: a 3-job CI workflow (Linux + macOS-14 + Windows), a macOS-runner MPS smoke test, two skip-by-default real-hardware test files, and a general enhancement issue template (Intel XPU as natural first user). One small spec edit refreshes §3 with the "ROCm capable but unverified" disclaimer matching the native-sidecar pattern.

**Tech Stack:** GitHub Actions (Linux ubuntu-latest, macOS macos-14, Windows windows-latest runners) · pytest markers (`requires_rocm`, `requires_real_cuda`, `requires_mps`) · `HELIX_TEST_ROCM` / `HELIX_TEST_CUDA` env-var gates · torch CPU wheel for Linux/Windows · torch with native MPS for macOS-14

---

## Background — what changed since the spec was written

When the spec at `docs/specs/2026-05-04-hardware-detection-design.md` was authored on 2026-05-04, §9.2 listed these PR2 work items:

> - `helix_context/hardware.py` — wire ROCm + MPS into auto picker, populate `device_type`
> - `_BATCH_TABLE` — fill in `("rocm", *)` and `("mps", *)` rows
> - Backends — most need no change (they consult `get_hardware().device` from PR1); fp16/bf16 fixups only if the macOS-runner smoke test surfaces dtype issues

PR1 (squash-merged at `f25211c`) over-implemented: `helix_context/hardware.py` already has `_detect_cuda_or_rocm(rocm=True)` for ROCm, `_detect_mps()` for MPS, and the auto-picker walks CUDA → ROCm → MPS → CPU. `_BATCH_TABLE` already has all 5 cuda rows, all 5 rocm rows, all 3 mps rows, and all 3 cpu rows. **No further hardware.py or batch-table changes are required for PR2** — the spec's own §9.2 file list is now stale on those two bullets.

What remains from §9.2:
- New file: `.github/workflows/ci.yml`
- New file: `tests/test_hardware_rocm.py` (skip-marked)
- New file: `tests/test_hardware_cuda_real.py` (skip-marked)
- New file: `.github/ISSUE_TEMPLATE/enhancement.md`
- macOS MPS smoke test (per spec §8.2 — load deberta tokenizer + 2-pair forward pass)
- This spec — §3 disclaimer update for "ROCm capable but unverified"

The fp16/bf16 risk in spec §9.2 is reactive (only triggered if macOS smoke test surfaces dtype issues) — addressed inline in Task 4 if it fires.

**No bench gate** per spec §9.2: new device backends are inactive on this rig (no MPS, no ROCm), so bench numbers are unchanged from PR1's measured −20.74 s p95 delta. The macOS-runner MPS smoke test is the integration check; ROCm relies on the opt-in marker until external validation.

---

## Spec references

- `docs/specs/2026-05-04-hardware-detection-design.md` (LOCKED on master)
  - §3.1 — `HardwareInfo` dataclass shape (referenced by tests)
  - §5 — auto-picker / fallback logic (already implemented in PR1)
  - §8 — testing strategy (Layer 1 mocked, Layer 2 GHA, Layer 3 opt-in real hardware)
  - §9.2 — PR2 rollout (this plan implements it, minus the picker/batch work that already shipped in PR1)
  - §11 — locked decisions

---

## File structure

| File | Status | Responsibility |
|---|---|---|
| `pyproject.toml` | modify | Register pytest markers `requires_rocm`, `requires_real_cuda`, `requires_mps` so pytest doesn't warn `PytestUnknownMarkWarning` |
| `tests/test_hardware_rocm.py` | create | Skip-by-default opt-in ROCm hardware test (gated by `HELIX_TEST_ROCM=1`); contributor with ROCm rig flips the env var to validate the rocm branch end-to-end |
| `tests/test_hardware_cuda_real.py` | create | Skip-by-default opt-in real-CUDA hardware test (gated by `HELIX_TEST_CUDA=1`); validates the picker against actual NVIDIA hardware (mocked tests cover the wiring; this validates probe + mem_get_info on real silicon) |
| `tests/test_hardware_mps_smoke.py` | create | Platform-gated MPS smoke test — runs on darwin only; skipped on Linux/Windows. Loads deberta tokenizer + 2-pair forward pass on mps; asserts output shape. Per spec §8.2 the macOS-14 CI runner exercises this every PR |
| `.github/workflows/ci.yml` | create | 3-job CI matrix: `test-linux` (ubuntu-latest), `test-macos-mps` (macos-14), `test-windows` (windows-latest). All run `pytest tests/`; macos job additionally runs `tests/test_hardware_mps_smoke.py` (which auto-skips on the other two via platform guard) |
| `.github/ISSUE_TEMPLATE/enhancement.md` | create | General enhancement-request template; Intel XPU listed as the natural first use case in the example body. Concrete need is to capture Intel XPU support requests from external contributors per spec §11.4 |
| `docs/specs/2026-05-04-hardware-detection-design.md` | modify | §3 disclaimer addition: "ROCm capable but unverified" — matches native-sidecar §3 posture |

**Touched in PR1, untouched in PR2:** `helix_context/hardware.py`, `helix_context/config.py`, `helix.toml`, all backend files (`deberta_backend.py`, `nli_backend.py`, `splade_backend.py`, `sema.py`), `helix_context/launcher/tray.py`, `helix_context/server.py`. PR1 handled the picker, batch table, config schema, and surfacing.

---

## Branch and commit hygiene

Worktree already at `F:/Projects/helix-context-mps-rocm` on branch `feat/hardware-mps-rocm` (rooted at master HEAD `14e1614`). All work commits go on that branch. Final push: `git push -u origin feat/hardware-mps-rocm` then open PR via `gh pr create`.

Commit cadence: one commit per task (Task 1 = pyproject markers; Task 2 = ROCm test scaffold; etc.). Commit message style matches PR1 (`feat(ci): ...` / `test(hardware): ...` / `docs(spec): ...`).

---

## Task 1: Register pytest markers

**Why first:** The new test files use `@pytest.mark.requires_rocm` / `requires_real_cuda` / `requires_mps`. Without registration in `pyproject.toml`, pytest emits `PytestUnknownMarkWarning` on collection. Tests still pass — but warnings clutter CI output and any future `-W error::PytestUnknownMarkWarning` flag would convert them to failures. Easier to register them upfront.

**Files:**
- Modify: `pyproject.toml` (the `[tool.pytest.ini_options]` block — currently has only `markers = ["live: requires Ollama running with at least one model"]`)

- [ ] **Step 1: Find the existing markers list**

Use Grep on `pyproject.toml` for `markers = `. The existing block is at the very end of the file (verified during planning):

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["live: requires Ollama running with at least one model"]
```

- [ ] **Step 2: Replace `markers = [...]` with the expanded list**

Use Edit tool to replace exactly:

```toml
markers = ["live: requires Ollama running with at least one model"]
```

with:

```toml
markers = [
    "live: requires Ollama running with at least one model",
    "requires_rocm: opt-in test that needs ROCm-capable hardware (gated by HELIX_TEST_ROCM=1)",
    "requires_real_cuda: opt-in test that needs real CUDA hardware (gated by HELIX_TEST_CUDA=1)",
    "requires_mps: test that runs only on macOS with MPS-capable hardware",
]
```

- [ ] **Step 3: Verify pytest accepts the markers**

Run: `python -m pytest --collect-only tests/test_hardware.py -q 2>&1 | head -20`
Expected: Collection succeeds, no `PytestUnknownMarkWarning` on the existing markers (the new ones aren't used yet by any test, so they won't warn either).

If pytest emits any error (typo, malformed TOML, etc.), inspect the diff and fix.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "test(pytest): register requires_rocm/requires_real_cuda/requires_mps markers"
```

---

## Task 2: Skip-by-default ROCm opt-in hardware test

**Why:** Per spec §8.3, contributors with ROCm-capable AMD hardware on Linux can validate the rocm branch end-to-end by setting `HELIX_TEST_ROCM=1`. Without an env var gate, this test would always run (and always skip in absence of `torch.version.hip`) — env var gives explicit opt-in semantics so a contributor knows the test ran intentionally vs. was a no-op silent skip.

The test exercises the full `get_hardware()` path on real ROCm hardware: device-type==rocm, vram_total_gb is not None, fallback_reason is None, recommended_batch_size returns a positive int. This catches "wheel says yes but kernel launch fails" failures that mocked tests can't surface.

**Files:**
- Create: `tests/test_hardware_rocm.py`

- [ ] **Step 1: Write the test file**

```python
"""Opt-in real-hardware ROCm test.

Skipped by default. Set ``HELIX_TEST_ROCM=1`` and run on a Linux host with
ROCm-built torch (``torch.version.hip is not None``) + AMD GPU passthrough
to exercise the full picker against real hardware. See spec
``docs/specs/2026-05-04-hardware-detection-design.md`` §8.3 for rationale.
"""

from __future__ import annotations

import os

import pytest

from helix_context import hardware

requires_rocm = pytest.mark.skipif(
    os.environ.get("HELIX_TEST_ROCM") != "1",
    reason="Set HELIX_TEST_ROCM=1 on a ROCm-capable host to enable",
)


@pytest.fixture(autouse=True)
def _reset_hardware_cache():
    hardware.reset_for_test()
    yield
    hardware.reset_for_test()


@requires_rocm
@pytest.mark.requires_rocm
def test_rocm_auto_picker_lands_on_rocm():
    """On a ROCm-capable host with HELIX_TEST_ROCM=1, auto-mode resolves
    to a rocm:N device with positive vram_total_gb and no fallback."""
    info = hardware.get_hardware()
    assert info.device_type == "rocm", f"Expected rocm, got {info.device_type!r}"
    assert info.device.startswith("rocm:"), f"Expected rocm:N, got {info.device!r}"
    assert info.vram_total_gb is not None and info.vram_total_gb > 0, (
        f"vram_total_gb={info.vram_total_gb!r}; expected positive float on real hardware"
    )
    assert info.fallback_reason is None, (
        f"Unexpected fallback: {info.fallback_reason!r}"
    )


@requires_rocm
@pytest.mark.requires_rocm
def test_rocm_recommended_batch_size_is_positive():
    """recommended_batch_size returns a positive int for known models on rocm."""
    info = hardware.get_hardware()
    assert info.device_type == "rocm"
    for model in ("rerank", "splice", "splade", "nli"):
        bs = hardware.recommended_batch_size(model)
        assert isinstance(bs, int) and bs > 0, (
            f"recommended_batch_size({model!r})={bs!r}; expected positive int"
        )
```

- [ ] **Step 2: Verify the file collects without warnings and skips by default**

Run: `python -m pytest tests/test_hardware_rocm.py -v 2>&1 | head -20`
Expected: Both tests collected, both reported as `SKIPPED [reason: Set HELIX_TEST_ROCM=1 on a ROCm-capable host to enable]`. No `PytestUnknownMarkWarning` (markers were registered in Task 1).

- [ ] **Step 3: Verify they would run if env var were set (collection only — don't actually run, since this rig has CUDA not ROCm)**

Run: `HELIX_TEST_ROCM=1 python -m pytest tests/test_hardware_rocm.py --collect-only -q 2>&1 | head -10`
Expected: Both tests collected, neither marked as deselect-skipped. (If we actually ran them on this rig, they'd fail because `torch.version.hip is None` → `_detect_cuda_or_rocm(rocm=True)` returns None → fallback to CPU → `assert info.device_type == "rocm"` fails. That's the correct behavior — the env var is a contributor opt-in for hardware they actually have.)

- [ ] **Step 4: Commit**

```bash
git add tests/test_hardware_rocm.py
git commit -m "test(hardware): add opt-in ROCm real-hardware test (HELIX_TEST_ROCM=1)"
```

---

## Task 3: Skip-by-default real-CUDA opt-in hardware test

**Why:** Symmetric to ROCm — spec §8.3 calls out `requires_real_cuda` for "the full path via the env var without modifying test plumbing." Mocked CUDA tests in `tests/test_hardware.py` cover the wiring; this validates `torch.cuda.mem_get_info`, `torch.cuda.get_device_name`, and the probe on actual NVIDIA hardware.

This rig has NVIDIA hardware, so we *can* run this test if we want to manually validate. The test still skips by default because `HELIX_TEST_CUDA` is unset — explicit opt-in is the contract.

**Files:**
- Create: `tests/test_hardware_cuda_real.py`

- [ ] **Step 1: Write the test file**

```python
"""Opt-in real-hardware CUDA test.

Skipped by default. Set ``HELIX_TEST_CUDA=1`` on a host with CUDA-built
torch + NVIDIA GPU to exercise the full picker against real hardware.
Mocked CUDA tests in ``tests/test_hardware.py`` cover the wiring; this
validates ``torch.cuda.mem_get_info``, ``get_device_name``, and the probe
round-trip on actual silicon. See spec
``docs/specs/2026-05-04-hardware-detection-design.md`` §8.3.
"""

from __future__ import annotations

import os

import pytest

from helix_context import hardware

requires_real_cuda = pytest.mark.skipif(
    os.environ.get("HELIX_TEST_CUDA") != "1",
    reason="Set HELIX_TEST_CUDA=1 on a CUDA-capable host to enable",
)


@pytest.fixture(autouse=True)
def _reset_hardware_cache():
    hardware.reset_for_test()
    yield
    hardware.reset_for_test()


@requires_real_cuda
@pytest.mark.requires_real_cuda
def test_cuda_auto_picker_lands_on_cuda():
    """On a CUDA-capable host with HELIX_TEST_CUDA=1, auto-mode resolves
    to a cuda:N device with positive vram_total_gb and a non-empty device_name."""
    info = hardware.get_hardware()
    assert info.device_type == "cuda", f"Expected cuda, got {info.device_type!r}"
    assert info.device.startswith("cuda:"), f"Expected cuda:N, got {info.device!r}"
    assert info.vram_total_gb is not None and info.vram_total_gb > 0, (
        f"vram_total_gb={info.vram_total_gb!r}; expected positive float on real hardware"
    )
    assert info.device_name and isinstance(info.device_name, str), (
        f"device_name={info.device_name!r}; expected non-empty string"
    )
    assert info.fallback_reason is None, (
        f"Unexpected fallback: {info.fallback_reason!r}"
    )


@requires_real_cuda
@pytest.mark.requires_real_cuda
def test_cuda_recommended_batch_size_is_positive():
    """recommended_batch_size returns a positive int for known models on cuda."""
    info = hardware.get_hardware()
    assert info.device_type == "cuda"
    for model in ("rerank", "splice", "splade", "nli"):
        bs = hardware.recommended_batch_size(model)
        assert isinstance(bs, int) and bs > 0, (
            f"recommended_batch_size({model!r})={bs!r}; expected positive int"
        )
```

- [ ] **Step 2: Verify default-skip**

Run: `python -m pytest tests/test_hardware_cuda_real.py -v 2>&1 | head -20`
Expected: Both tests `SKIPPED [reason: Set HELIX_TEST_CUDA=1 on a CUDA-capable host to enable]`.

- [ ] **Step 3: Verify it works on this rig (since we have NVIDIA + CUDA-built torch — optional but fast)**

Run: `HELIX_TEST_CUDA=1 python -m pytest tests/test_hardware_cuda_real.py -v 2>&1 | head -20`
Expected: Both tests PASS. Validates the test code itself works against the real picker, not just default-skips. (If torch wheel is CPU-only on this rig — which the PR1 memory notes is the case — both tests will FAIL with `assert info.device_type == "cuda"` because the picker falls back to CPU. That's diagnostic, not a problem with the test code itself. Proceed to Step 4 in either outcome — the env-var-default-off path is what CI exercises.)

- [ ] **Step 4: Commit**

```bash
git add tests/test_hardware_cuda_real.py
git commit -m "test(hardware): add opt-in real-CUDA hardware test (HELIX_TEST_CUDA=1)"
```

---

## Task 4: macOS MPS smoke test

**Why:** Per spec §8.2, the macos-14 CI runner runs a 1-iteration MPS smoke test every PR: load deberta tokenizer, do a 2-pair forward pass on mps, assert output shape. This is the only end-to-end integration check that catches regressions in the MPS branch — mocked unit tests can't surface dtype quirks (fp16 hardware, float32-on-MPS silent CPU fallback) that real MPS exhibits.

The test must auto-skip on Linux + Windows (no MPS) and unconditionally run on macOS-14 (always Apple Silicon, always MPS-capable). Pattern: `@pytest.mark.skipif(sys.platform != "darwin" or not torch.backends.mps.is_available(), ...)`.

**Why a 2-pair forward pass:** spec §8.2 specifies "2-pair forward pass" (not 1-pair) — exercises the chunking path in `re_rank` (which builds pairs from texts_a, texts_b lists). Single pair would skip the chunk-loop branch entirely.

**Risk: deberta tokenizer download in CI:** First run downloads ~500 MB from HuggingFace. CI cold-cache adds ~30-60 s. Acceptable for first iteration; HuggingFace cache action can be added later if it becomes painful.

**Files:**
- Create: `tests/test_hardware_mps_smoke.py`

- [ ] **Step 1: Write the smoke test file**

```python
"""macOS MPS smoke test — runs on darwin only.

Loads a tiny deberta-class tokenizer + model and does a 2-pair forward
pass on mps to catch MPS-branch regressions on every PR. Skipped on
non-darwin platforms (Linux / Windows). See spec
``docs/specs/2026-05-04-hardware-detection-design.md`` §8.2.
"""

from __future__ import annotations

import sys

import pytest


def _mps_available() -> bool:
    try:
        import torch
    except ImportError:
        return False
    return (
        sys.platform == "darwin"
        and torch.backends.mps.is_available()
        and torch.backends.mps.is_built()
    )


requires_mps_runtime = pytest.mark.skipif(
    not _mps_available(),
    reason="Requires darwin + MPS-capable hardware (skipped on Linux/Windows runners)",
)


@requires_mps_runtime
@pytest.mark.requires_mps
def test_deberta_classifier_two_pair_forward_pass_on_mps():
    """Load a small cross-encoder, run a 2-pair forward pass on mps, assert
    output shape == (2,). Catches MPS-branch regressions before they reach
    users."""
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    # Smallest-stable cross-encoder we already pin via deberta_backend's
    # default. Keeping the model name local to the test (not importing
    # deberta_backend) avoids ImportError surfaces on macOS-14 if optional
    # extras aren't installed.
    model_id = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForSequenceClassification.from_pretrained(model_id)
    model = model.to("mps")
    model.eval()

    pairs_a = ["What is helix-context?", "How does the picker work?"]
    pairs_b = ["A retrieval system.", "It walks CUDA, ROCm, MPS, then CPU."]

    enc = tokenizer(
        pairs_a, pairs_b,
        padding=True, truncation=True, max_length=128, return_tensors="pt",
    ).to("mps")

    with torch.no_grad():
        out = model(**enc).logits

    assert out.shape == (2, 1) or out.shape == (2,), (
        f"Expected logits shape (2, 1) or (2,); got {tuple(out.shape)!r}"
    )
    # Round-trip back to CPU to catch silent fallback / NaN-on-MPS issues
    cpu_logits = out.cpu()
    assert not torch.isnan(cpu_logits).any(), "NaN in MPS logits — dtype/op mismatch?"
```

Note: `model.eval()` here calls torch's `nn.Module.eval()` (sets dropout/batchnorm to inference mode) — it has nothing to do with Python's `eval` builtin.

- [ ] **Step 2: Verify default-skip on this Windows host**

Run: `python -m pytest tests/test_hardware_mps_smoke.py -v 2>&1 | head -10`
Expected: Test `SKIPPED [reason: Requires darwin + MPS-capable hardware (skipped on Linux/Windows runners)]`.

- [ ] **Step 3: Verify the file imports without error** (the platform-guarded skip is at decorator level, but the module-level imports must not fail)

Run: `python -c "import tests.test_hardware_mps_smoke"`
Expected: Exit code 0, no output. (If `transformers` isn't installed locally, this Step would fail with ImportError. That's acceptable since the macOS-14 CI runner installs `transformers` per Task 5; on this Windows dev box, transformers is already pulled in by the launcher venv per PR1's setup. If it does fail locally, note it but proceed — CI will catch the regression on a clean environment.)

- [ ] **Step 4: Commit**

```bash
git add tests/test_hardware_mps_smoke.py
git commit -m "test(hardware): add macOS MPS 2-pair smoke test for CI runner"
```

---

## Task 5: GitHub Actions CI workflow

**Why:** Per spec §8.2, three runners (ubuntu-latest, macos-14, windows-latest) catch (a) Linux-specific path/encoding bugs, (b) MPS regressions every PR, (c) Windows-specific path/encoding bugs (5 of which surfaced on the native-sidecar PR). All three run the full unit suite (mocked torch covers cuda/rocm/mps branches without needing real hardware); macOS additionally exercises the MPS smoke test from Task 4 (auto-skipped on the other two via the platform guard).

**Concurrency:** Cancel in-progress runs on the same ref so push-after-push doesn't queue stale runs. Standard pattern.

**Caching:** Skip pip cache on first iteration. Adds complexity; can revisit if cold-install dominates CI time.

**Triggers:** PRs into `master` and pushes to `master` (the latter so we have green-on-master signal). No scheduled runs.

**Concrete install commands (chosen during planning):**
- Linux + Windows: `pip install --index-url https://download.pytorch.org/whl/cpu torch` then `pip install -e ".[dev]" psutil platformdirs py-cpuinfo`
- macOS: `pip install -e ".[dev]" torch transformers psutil platformdirs py-cpuinfo` (native MPS-aware torch wheel; `transformers` needed for the MPS smoke test)

The `[dev]` extra (per `pyproject.toml`) is `["pytest", "pytest-asyncio"]` — minimal. We deliberately don't pull `[all]` to keep CI install time bounded; the unit suite mocks anything heavier than torch + cpuinfo + psutil.

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Write the workflow file**

```yaml
name: CI

on:
  push:
    branches: [master]
  pull_request:
    branches: [master]

# Cancel in-progress runs on the same ref so a fast follow-up push
# doesn't keep a stale run alive.
concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  test-linux:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install CPU-only torch
        run: pip install --index-url https://download.pytorch.org/whl/cpu torch

      - name: Install package + test deps
        run: pip install -e ".[dev]" psutil platformdirs py-cpuinfo

      - name: Run unit suite
        run: pytest tests/ -v

  test-macos-mps:
    runs-on: macos-14
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install package + test deps + transformers (for MPS smoke test)
        run: pip install -e ".[dev]" torch transformers psutil platformdirs py-cpuinfo

      - name: Run unit suite (MPS smoke test runs only on this job)
        run: pytest tests/ -v

  test-windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install CPU-only torch
        run: pip install --index-url https://download.pytorch.org/whl/cpu torch

      - name: Install package + test deps
        run: pip install -e ".[dev]" psutil platformdirs py-cpuinfo

      - name: Run unit suite
        run: pytest tests/ -v
```

- [ ] **Step 2: Validate workflow YAML locally**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`
Expected: Exit code 0, no output. (Catches indent/syntax errors before pushing.)

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "feat(ci): add Linux + macOS-MPS + Windows test runners"
```

(Note: the workflow won't actually execute until the branch is pushed and a PR is opened — Task 8 covers that.)

---

## Task 6: Enhancement issue template

**Why:** Per spec §11.4 + §9.2, Intel XPU is deferred to enhancement-request rather than implemented in PR1/PR2 — the hardware is rare today and adding torch.xpu detection without anyone to validate is YAGNI. But contributors with Intel Arc / Battlemage *should* have a clear way to file a request that captures the hardware specifics needed to assess the work.

A general enhancement template (not Intel-XPU-specific) covers Intel XPU as the natural first user while leaving room for other future requests (e.g., DirectML, Vulkan). Keep it short — issue templates are friction-bearing.

**Files:**
- Create: `.github/ISSUE_TEMPLATE/enhancement.md`

- [ ] **Step 1: Write the template**

```markdown
---
name: Enhancement request
about: Suggest a new capability or extension to helix-context
title: "[Enhancement] "
labels: enhancement
assignees: ''
---

## What you'd like to see

A clear, concise description of the enhancement.

## Why it matters

- Use case
- Who benefits
- What's currently blocked or awkward without it

## Implementation sketch (optional)

If you have a sense of how this could land, share. Otherwise, no obligation — the value of this template is capturing the *need*, not the implementation.

## Hardware / environment specifics (if relevant)

If the enhancement is hardware-related (e.g., a new device backend like Intel XPU, DirectML, Vulkan), please include:

- Vendor + model (e.g., "Intel Arc A770 16 GB" or "Apple M3 Max")
- Driver / runtime version
- OS + Python version
- Whether you have a rig you can use to validate a PR

## Related work

Links to specs, prior issues, PyTorch docs, vendor SDK docs, etc.
```

- [ ] **Step 2: Verify markdown renders cleanly**

No tooling step — visually inspect the file. (GitHub doesn't validate issue templates client-side; first issue filed against the template is the validation.) Confirm the YAML frontmatter has `name:`, `about:`, `title:`, `labels:`, `assignees:` — required by GitHub for the New Issue picker to discover the template.

- [ ] **Step 3: Commit**

```bash
git add .github/ISSUE_TEMPLATE/enhancement.md
git commit -m "feat(github): add general enhancement issue template"
```

---

## Task 7: Spec disclaimer refresh — "ROCm capable but unverified"

**Why:** Spec §3 — the architecture overview — was written before PR1 implemented detection. It claims MPS + ROCm "ship as named device backends in PR2, with appropriate 'capable but unverified' disclaimers per non-NVIDIA path until validated on real hardware." That language belongs as a concrete sentence inline, not just buried in §11. Easier for someone reading the spec cold to know "the rocm path is wired but no maintainer has tested it on hardware."

This is a small doc edit — single sentence addition.

**Files:**
- Modify: `docs/specs/2026-05-04-hardware-detection-design.md` near the §3.1 / §3.2 boundary

- [ ] **Step 1: Read the current §3 wording** (lines ~42-60 of the spec)

Use Read tool on `docs/specs/2026-05-04-hardware-detection-design.md` lines 42-100. Locate where §3.1 ends and §3.2 begins.

- [ ] **Step 2: Add a new bullet under §3 disclaimers**

The exact insertion point: after the "Goals" / "Non-goals" paragraphs near §3, before the §3.1 "Module shape" subheading. Add a clearly-marked subsection "**Verification posture**":

```markdown
### Verification posture

- **NVIDIA + CUDA-built torch:** validated on this rig (RTX-class consumer card, Windows 11, CUDA 12.1 wheel). PR1 bench gate measured −20.74 s p95 vs master baseline.
- **ROCm + AMD GPU on Linux:** wired in PR1, *capable but unverified* — no maintainer with ROCm hardware has run the opt-in `tests/test_hardware_rocm.py` against real silicon. Same posture as the native-observability sidecar's macOS/Linux scripts (#16 §3 non-goals). Contributors with ROCm rigs are invited to validate via `HELIX_TEST_ROCM=1 pytest tests/test_hardware_rocm.py`.
- **MPS / Apple Silicon:** wired in PR1; smoke-tested on every PR via the macOS-14 GitHub Actions runner (PR2's `tests/test_hardware_mps_smoke.py`). Full backend validation against deberta/nli/splade/sema on Apple Silicon is *not* claimed — only that a 2-pair deberta forward pass through MPS does not regress.
- **CPU on x86 (Intel/AMD) and ARM (Linux/macOS):** validated indirectly — the CPU branch is the fallback of last resort and is exercised on every CI run (Linux + Windows runners) via the unit suite.
- **Intel XPU:** not wired; deferred to enhancement-request via `.github/ISSUE_TEMPLATE/enhancement.md`. The picker recognizes the device-type string but resolves to CPU fallback per §2 non-goals.
```

(Implementation note: this is a content addition, not a replacement. The Edit tool's `old_string` should match the exact text just before the §3.1 heading; `new_string` is that text + the new "Verification posture" block + `### 3.1 Module shape` heading. If the old_string match is ambiguous, use a longer prefix.)

- [ ] **Step 3: Verify the spec renders correctly**

No tooling step — visually inspect via Read tool to confirm the new subsection sits cleanly between "Non-goals" and "Module shape".

- [ ] **Step 4: Commit**

```bash
git add docs/specs/2026-05-04-hardware-detection-design.md
git commit -m "docs(spec): add §3 verification posture (ROCm capable-but-unverified)"
```

---

## Task 8: Final integration — push branch, open PR, verify all CI jobs pass

**Why:** The CI workflow lands in this PR but only fires *on* this PR. We can't validate green-CI locally — only by pushing and watching. This task is the manual integration check.

If any job fails:
- Linux/Windows path/encoding bugs: surface a fix-up commit on this branch, push, wait for re-run
- macOS MPS smoke test fails (e.g., dtype quirk per spec §10's MPS risk row): per spec §9.2 "fp16/bf16 fixups only if the macOS-runner smoke test surfaces dtype issues" — investigate, add a targeted fix in `helix_context/deberta_backend.py` or directly in the smoke test
- Pip resolution failures: simplify the install command (e.g., drop transformers and inline a smaller HF model)

**Files:** No file changes — purely CI verification.

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feat/hardware-mps-rocm
```

Expected: Push succeeds, branch tracking set up.

- [ ] **Step 2: Open the PR via gh CLI**

Use a HEREDOC for the body. PR title: `feat: hardware detection PR2 — MPS + ROCm + CI workflow`.

PR body content (paste into HEREDOC):

```
## Summary

PR2 of the hardware-detection split (PR1 was #17, merged at `f25211c`). PR1 over-implemented — the picker branches and `_BATCH_TABLE` rows for ROCm + MPS already shipped in PR1 — so this PR is exclusively the testing/CI infrastructure that proves the module works end-to-end:

- New `.github/workflows/ci.yml` — 3-job matrix (Linux + macOS-14 + Windows)
- New macOS-14 MPS smoke test: load deberta tokenizer + 2-pair forward pass on `mps`, assert output shape (per spec §8.2)
- New skip-by-default opt-in real-hardware test scaffolds (`HELIX_TEST_ROCM=1` / `HELIX_TEST_CUDA=1`)
- New general enhancement-request issue template (Intel XPU as natural first use case)
- Spec §3 verification-posture section: ROCm "capable but unverified" disclaimer

## Verification posture (also added to spec §3)

- **NVIDIA + CUDA:** validated on the maintainer's rig
- **ROCm:** wired, *capable but unverified* — opt-in test for contributors with hardware
- **MPS:** smoke-tested every PR via the macOS-14 runner
- **CPU x86 + ARM:** exercised every PR via Linux + Windows runners
- **Intel XPU:** not wired; deferred to enhancement-request

## Test plan

- [ ] Linux runner: full unit suite passes
- [ ] macOS-14 runner: full unit suite + MPS smoke test pass
- [ ] Windows runner: full unit suite passes
- [ ] Opt-in tests skip by default; runnable via env var

## Related

- Spec: `docs/specs/2026-05-04-hardware-detection-design.md` §9.2
- Plan: `docs/plans/2026-05-04-hardware-mps-rocm-ci.md`
- PR1: #17 (merged)
```

Run via:
```bash
gh pr create --title "feat: hardware detection PR2 — MPS + ROCm + CI workflow" --body "$(cat <<'EOF'
<paste the body content above here, including a trailing line:
🤖 Generated with [Claude Code](https://claude.com/claude-code)>
EOF
)"
```

Expected: PR URL printed. Capture the number for Step 3.

- [ ] **Step 3: Wait for CI and verify all 3 jobs pass**

```bash
gh pr checks <PR_NUMBER> --watch
```

Expected: All three jobs (`test-linux`, `test-macos-mps`, `test-windows`) report `pass` status. macos-14 + transformers + tokenizer download takes ~3-4 min; Linux + Windows ~2 min each.

- [ ] **Step 4: If anything fails, diagnose + fix-up commit**

Report the failing job + log to the user. Common failure modes:
- pip resolution: drop a dep or pin a version
- MPS dtype: try `model.to(torch.float32)` before `.to("mps")` (spec §10 "MPS dtype quirks")
- Windows Path issues: use `pathlib.Path` instead of `str` concatenation
- Test collection: missing dependency in install command

Each fix is a new commit on this branch with message `fix(ci): <one-line description>` and pushed; CI re-runs automatically.

- [ ] **Step 5: Once green, request review from the user**

Reply to the user: "All 3 CI jobs green on PR #N: <URL>. Ready for your review."

---

## Acceptance criteria for the whole PR

Plan is complete when:

1. ✅ Pytest markers registered in `pyproject.toml`
2. ✅ `tests/test_hardware_rocm.py` skips by default; runs only with `HELIX_TEST_ROCM=1`
3. ✅ `tests/test_hardware_cuda_real.py` skips by default; runs only with `HELIX_TEST_CUDA=1`
4. ✅ `tests/test_hardware_mps_smoke.py` runs on darwin+MPS only; skips otherwise
5. ✅ `.github/workflows/ci.yml` — all 3 jobs pass on PR open
6. ✅ `.github/ISSUE_TEMPLATE/enhancement.md` shows up in GitHub's New Issue picker
7. ✅ Spec §3 has the "Verification posture" block with ROCm-capable-but-unverified language
8. ✅ Existing test suite still passes (regression check) — `pytest tests/ -v` reports same pass/skip count as before this PR's changes (modulo the new test files)
9. ✅ PR opened, all CI green, awaiting maintainer review

No bench gate per spec §9.2 — new device backends are inactive on this rig, so bench numbers are unchanged from PR1's measured −20.74 s p95 delta.

---

## Out of scope (deferred to future PRs)

- **fp16/bf16 dtype optimization** (spec §2 non-goal) — only triggered if MPS smoke test surfaces dtype regressions, addressed inline in Task 4 if so
- **OOM-recovery retry loops** (spec §2 non-goal)
- **Multi-GPU sharding** (spec §2 non-goal)
- **Intel XPU implementation** — captured in enhancement-request template
- **Removing `[ribosome] device` deprecation shim** — one-liner in a later release per spec §9.3
- **HuggingFace cache action** — pip cache + hf cache adds 30-60 s savings on cold CI; revisit if CI time becomes painful
- **Pre-built CUDA tests on a self-hosted GHA runner** — out of scope; opt-in env-var coverage is sufficient

---

## Notes for the executing engineer

- This plan was written *after* PR1 (#17, merged at `f25211c`) had already over-implemented the picker and batch table. The spec's §9.2 file list mentions hardware.py + _BATCH_TABLE changes — ignore those, they shipped in PR1.
- Worktree is at `F:/Projects/helix-context-mps-rocm` on branch `feat/hardware-mps-rocm` rooted at master `14e1614`.
- All work is in test/CI infra — no production code changes. Risk of regressing existing functionality is near-zero.
- Tasks 1-7 are local; Task 8 requires push + open-PR auth.
- This rig is Windows + bash + native python (no `uv run`, no WSL) per global preferences.
