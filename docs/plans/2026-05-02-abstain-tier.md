# ABSTAIN Tier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an ABSTAIN tier above BROAD in `_build_context_internal` so weak retrieval (`top_score < 2.5` AND `ratio < 1.8`) returns a marker-only ContextWindow instead of dumping ~12K tokens of irrelevant gene expressions to the small e4b model.

**Architecture:** A single new branch inside the existing dynamic-budget-tier block in `context_manager.py:746-787`. Reuses already-calibrated `FOCUSED_SCORE_FLOOR = 2.5` and `ratio < 1.8` thresholds verbatim — the trigger is simply the negative space of TIGHT/FOCUSED. Output reuses the empty-candidates path's marker string `"(no relevant context found in genome)"` (lifted to a shared module constant) but emits a new `ContextHealth.status = "abstain"` so observability can distinguish "genome had nothing" (`denatured`) from "genome had weak signals" (`abstain`).

**Tech Stack:** Python 3.13, pytest, dataclasses (config), OpenTelemetry counters (telemetry).

**Spec:** `docs/specs/2026-05-02-abstain-tier-design.md`

---

## File Structure

| File | Action | Responsibility |
| --- | --- | --- |
| `helix_context/config.py` | Modify | Add `abstain_enabled: bool = True` field to `BudgetConfig` + loader entry in `load_config`. |
| `helix.toml` | Modify | Add `abstain_enabled = true` to `[budget]` with comment block explaining the gate. |
| `helix_context/context_manager.py` | Modify | Add `_ABSTAIN_MARKER` constant, local `_abstain_env_disabled()` helper, `_resolve_abstain_enabled()` method, `_build_abstain_window()` helper, and the gate inside `_build_context_internal`. Refactor empty-candidates branch (line 706) to reference the constant. |
| `helix_context/telemetry.py` | Modify | One-line update to `budget_tier_counter` docstring listing `abstain` as a valid label value. |
| `tests/test_abstain_tier.py` | Create | All eight test cases from spec §8 plus `_env_truthy` and `_build_abstain_window` unit tests. |
| `tests/test_config.py` | Modify | Extend with `abstain_enabled` default + toml-override regression tests. |

**Files explicitly NOT touched:**
- `helix_context/schemas.py` — `ContextHealth.status` is a free-form string today; no enum to extend.
- `helix_context/server.py` — `_munge_messages` (line 2785) is agnostic to the marker contents.
- `helix_context/context_packet.py` — `_coordinate_signals` (deferred follow-up, see spec §11).

---

## Task 1: Add `abstain_enabled` config field

**Files:**
- Modify: `helix_context/config.py:117-134` (BudgetConfig) and `:382-394` (loader).
- Modify: `helix.toml` — `[budget]` section.
- Modify: `tests/test_config.py` — extend with new regression tests.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
def test_budget_abstain_enabled_default_is_true():
    """Regression: new ABSTAIN gate ships on by default.

    The 2026-05-02 ABSTAIN tier (docs/specs/2026-05-02-abstain-tier-design.md)
    is shipped on-by-default so the latency win lands without an opt-in step.
    Operators flip to false in helix.toml [budget] for the legacy always-
    inject behavior. Bumping this default to false would silently undo the
    GPQA Diamond p95 fix.
    """
    from helix_context.config import HelixConfig
    cfg = HelixConfig()
    assert cfg.budget.abstain_enabled is True


def test_budget_abstain_enabled_toml_override(tmp_path):
    """Regression: helix.toml [budget] abstain_enabled = false is honored."""
    from helix_context.config import load_config
    toml = tmp_path / "helix.toml"
    toml.write_text(
        "[budget]\nabstain_enabled = false\n",
        encoding="utf-8",
    )
    cfg = load_config(str(toml))
    assert cfg.budget.abstain_enabled is False
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `py -3 -m pytest tests/test_config.py -v`
Expected: 2 new tests FAIL with `AttributeError: 'BudgetConfig' object has no attribute 'abstain_enabled'`. Existing `test_upstream_timeout_default_is_180s` still passes.

- [ ] **Step 3: Add the field to `BudgetConfig`**

In `helix_context/config.py`, locate the `BudgetConfig` dataclass (around line 117). Add a new field at the end of the dataclass body (preserve dataclass-default-value ordering — all fields here are already keyword-with-default so this is safe):

```python
@dataclass
class BudgetConfig:
    # ... existing fields unchanged ...
    session_delivery_enabled: bool = True
    abstain_enabled: bool = True       # NEW — see docs/specs/2026-05-02-abstain-tier-design.md
```

- [ ] **Step 4: Add the loader entry**

In `load_config` (around line 382-394), inside the `if "budget" in raw:` block, add the new key alongside the others:

```python
cfg.budget = BudgetConfig(
    ribosome_tokens=b.get("ribosome_tokens", cfg.budget.ribosome_tokens),
    expression_tokens=b.get("expression_tokens", cfg.budget.expression_tokens),
    max_genes_per_turn=b.get("max_genes_per_turn", cfg.budget.max_genes_per_turn),
    max_fingerprints_per_turn=b.get("max_fingerprints_per_turn", cfg.budget.max_fingerprints_per_turn),
    splice_aggressiveness=float(b.get("splice_aggressiveness", cfg.budget.splice_aggressiveness)),
    decoder_mode=b.get("decoder_mode", cfg.budget.decoder_mode),
    legibility_enabled=bool(b.get("legibility_enabled", cfg.budget.legibility_enabled)),
    session_delivery_enabled=bool(b.get("session_delivery_enabled", cfg.budget.session_delivery_enabled)),
    abstain_enabled=bool(b.get("abstain_enabled", cfg.budget.abstain_enabled)),
)
```

- [ ] **Step 5: Add the helix.toml knob**

In `helix.toml`, inside the `[budget]` block (around line 39-58), add right after `session_delivery_enabled = true`:

```toml
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
```

- [ ] **Step 6: Run tests, verify they pass**

Run: `py -3 -m pytest tests/test_config.py -v`
Expected: all tests PASS, including the two new ones.

- [ ] **Step 7: Commit**

```bash
git add helix_context/config.py helix.toml tests/test_config.py
git commit -m "feat(config): add [budget] abstain_enabled knob (default true)

Adds BudgetConfig.abstain_enabled (default True) + loader entry +
helix.toml knob + regression tests pinning the default. No runtime
behavior change yet — wiring the gate that consumes the flag lands
in subsequent commits.

See docs/specs/2026-05-02-abstain-tier-design.md §6."
```

---

## Task 2: Lift the marker string to a shared constant

**Files:**
- Modify: `helix_context/context_manager.py:706` (empty-candidates branch).
- Create: `tests/test_abstain_tier.py` (first test file).

This is a pure refactor — no behavior change. Just lifts the literal `"(no relevant context found in genome)"` to a module-level constant so the abstain branch (Task 5) can reuse the same bytes.

- [ ] **Step 1: Write the failing test**

Create `tests/test_abstain_tier.py` with the file's first test:

```python
"""Tests for the ABSTAIN tier — confidence-gated context attachment.

See docs/specs/2026-05-02-abstain-tier-design.md and
docs/plans/2026-05-02-abstain-tier.md.
"""

from helix_context import context_manager as cm


def test_abstain_marker_constant_is_exported():
    """The shared marker string is exposed at module scope so the empty-
    candidates branch and the abstain branch can ship identical bytes."""
    assert cm._ABSTAIN_MARKER == "(no relevant context found in genome)"
```

- [ ] **Step 2: Run, verify it fails**

Run: `py -3 -m pytest tests/test_abstain_tier.py -v`
Expected: FAIL with `AttributeError: module 'helix_context.context_manager' has no attribute '_ABSTAIN_MARKER'`.

- [ ] **Step 3: Add the constant + refactor the empty-candidates branch**

In `helix_context/context_manager.py`, near the top of the file (after the imports / decoder-prompt block, before any class definitions — pick a stable location around line 60-100), add:

```python
# Shared marker injected when build_context has nothing useful to ship —
# either the genome had no candidates ("denatured") or post-refinement
# scores fell below the FOCUSED floor on both axes ("abstain"). Both
# branches ship the same bytes so the small model's prompt-conditioning
# is identical regardless of which short-circuit fired. The semantic
# difference is observable only via context_health.status.
_ABSTAIN_MARKER = "(no relevant context found in genome)"
```

Then locate the empty-candidates branch in `_build_context_internal` (around line 694-711) and replace the literal at line 706:

```python
return ContextWindow(
    ribosome_prompt=effective_decoder_prompt,
    expressed_context=_ABSTAIN_MARKER,            # was: "(no relevant context found in genome)"
    total_estimated_tokens=estimate_tokens(effective_decoder_prompt),
    compression_ratio=1.0,
    context_health=empty_health,
    metadata={"query": query, "genes_expressed": 0},
)
```

- [ ] **Step 4: Run, verify it passes**

Run: `py -3 -m pytest tests/test_abstain_tier.py tests/test_pipeline.py -v`
Expected: new test PASSES; existing pipeline tests still pass (this was a pure refactor).

- [ ] **Step 5: Commit**

```bash
git add helix_context/context_manager.py tests/test_abstain_tier.py
git commit -m "refactor(context): lift empty-context marker to _ABSTAIN_MARKER

Pure refactor — no behavior change. The literal '(no relevant context
found in genome)' at line 706 is lifted to a module constant so the
ABSTAIN branch (next commit) can ship identical bytes. The two paths
will stay distinct on context_health.status (denatured vs abstain) but
present the same prompt-conditioning to the LLM.

See docs/specs/2026-05-02-abstain-tier-design.md §4.1."
```

---

## Task 3: Add `_env_truthy` local helper

**Files:**
- Modify: `helix_context/context_manager.py` (add helper near top).
- Modify: `tests/test_abstain_tier.py` (add helper unit tests).

A 2-state variant of the launcher's `launcher/app.py:261` helper — returns `True` for set-and-truthy, `False` otherwise (no `None` tristate, simpler for our use). Defined inline rather than imported to avoid `context_manager → launcher` coupling.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_abstain_tier.py`:

```python
import pytest


@pytest.mark.parametrize("value,expected", [
    ("1", True),
    ("true", True),
    ("TRUE", True),
    ("yes", True),
    ("on", True),
    ("0", False),
    ("false", False),
    ("no", False),
    ("", False),
    ("garbage", False),
])
def test_env_truthy_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("HELIX_TEST_ENV_TRUTHY", value)
    assert cm._env_truthy("HELIX_TEST_ENV_TRUTHY") is expected


def test_env_truthy_unset_is_false(monkeypatch):
    monkeypatch.delenv("HELIX_TEST_ENV_TRUTHY", raising=False)
    assert cm._env_truthy("HELIX_TEST_ENV_TRUTHY") is False
```

- [ ] **Step 2: Run, verify failure**

Run: `py -3 -m pytest tests/test_abstain_tier.py -v`
Expected: 11 new tests FAIL with `AttributeError: module 'helix_context.context_manager' has no attribute '_env_truthy'`.

- [ ] **Step 3: Add the helper**

In `helix_context/context_manager.py`, near the `_ABSTAIN_MARKER` constant, add:

```python
def _env_truthy(name: str) -> bool:
    """Return True iff env var is set to a truthy value.

    Truthy values (case-insensitive): '1', 'true', 'yes', 'on'. Anything
    else (including unset) returns False. This is the 2-state variant of
    helix_context.launcher.app._env_truthy — defined locally to avoid a
    context_manager → launcher import edge.
    """
    v = os.environ.get(name)
    if v is None:
        return False
    return v.strip().lower() in ("1", "true", "yes", "on")
```

(`os` is already imported at the top of the module.)

- [ ] **Step 4: Run, verify pass**

Run: `py -3 -m pytest tests/test_abstain_tier.py -v`
Expected: 11 tests PASS (parametrized + unset).

- [ ] **Step 5: Commit**

```bash
git add helix_context/context_manager.py tests/test_abstain_tier.py
git commit -m "feat(context): add _env_truthy 2-state helper

Local 2-state variant of launcher/app.py::_env_truthy used by the
ABSTAIN gate's HELIX_ABSTAIN_DISABLE override. Defined inline rather
than imported to avoid a context_manager → launcher coupling.

See docs/specs/2026-05-02-abstain-tier-design.md §6.3."
```

---

## Task 4: Add `_build_abstain_window` helper

**Files:**
- Modify: `helix_context/context_manager.py` — add `_build_abstain_window` method on `HelixContextManager`.
- Modify: `tests/test_abstain_tier.py` — add helper unit test.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_abstain_tier.py`:

```python
from helix_context.config import (
    BudgetConfig,
    ClassifierConfig,
    GenomeConfig,
    HelixConfig,
    RibosomeConfig,
)
from helix_context.context_manager import HelixContextManager
from tests.test_pipeline import PipelineMockBackend


@pytest.fixture
def abstain_manager():
    """Manager with mock backend + in-memory genome + abstain on."""
    cfg = HelixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(max_genes_per_turn=12, abstain_enabled=True),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        classifier=ClassifierConfig(enabled=False),
    )
    mgr = HelixContextManager(cfg)
    mgr.ribosome.backend = PipelineMockBackend()
    yield mgr
    mgr.close()


def test_build_abstain_window_shape(abstain_manager):
    """The helper returns a ContextWindow with the spec-§4 shape."""
    win = abstain_manager._build_abstain_window(
        query="anything",
        effective_decoder_prompt="DECODER",
        top_score=1.5,
        ratio=1.2,
        reason="score_below_floor",
    )
    assert win.expressed_context == cm._ABSTAIN_MARKER
    assert win.context_health.status == "abstain"
    assert win.context_health.genes_expressed == 0
    assert win.metadata["genes_expressed"] == 0
    assert win.metadata["budget_tier"] == "abstain"
    assert win.metadata["abstain_reason"] == "score_below_floor"
    assert win.metadata["top_score"] == 1.5
    assert win.metadata["ratio"] == 1.2
    assert win.compression_ratio == 1.0
```

- [ ] **Step 2: Run, verify fail**

Run: `py -3 -m pytest tests/test_abstain_tier.py::test_build_abstain_window_shape -v`
Expected: FAIL with `AttributeError: 'HelixContextManager' object has no attribute '_build_abstain_window'`.

- [ ] **Step 3: Add the helper**

In `helix_context/context_manager.py`, inside the `HelixContextManager` class — place near the empty-candidates branch's surrounding methods (somewhere after `_express` and before the public `build_context` method, or co-located with other private helpers). Add:

```python
def _build_abstain_window(
    self,
    *,
    query: str,
    effective_decoder_prompt: str,
    top_score: float,
    ratio: float,
    reason: str,
) -> ContextWindow:
    """Return the marker-only ContextWindow shipped when the ABSTAIN tier fires.

    See docs/specs/2026-05-02-abstain-tier-design.md §4. Distinct from the
    empty-candidates branch (line ~694) only on context_health.status —
    the LLM-visible bytes are identical (both ship _ABSTAIN_MARKER).
    """
    health = ContextHealth(
        ellipticity=0.0,
        coverage=0.0,
        density=0.0,
        freshness=0.0,
        genes_available=self.genome.stats().get("total_genes", 0),
        genes_expressed=0,
        status="abstain",
    )
    return ContextWindow(
        ribosome_prompt=effective_decoder_prompt,
        expressed_context=_ABSTAIN_MARKER,
        total_estimated_tokens=estimate_tokens(effective_decoder_prompt),
        compression_ratio=1.0,
        context_health=health,
        metadata={
            "query": query,
            "genes_expressed": 0,
            "budget_tier": "abstain",
            "abstain_reason": reason,
            "top_score": float(top_score),
            "ratio": float(ratio),
        },
    )
```

- [ ] **Step 4: Run, verify pass**

Run: `py -3 -m pytest tests/test_abstain_tier.py::test_build_abstain_window_shape -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add helix_context/context_manager.py tests/test_abstain_tier.py
git commit -m "feat(context): add _build_abstain_window helper

Builds the ContextWindow returned when the ABSTAIN tier fires (next
commit wires it). Identical LLM-visible bytes to the empty-candidates
branch but distinct context_health.status='abstain' for observability.

See docs/specs/2026-05-02-abstain-tier-design.md §4."
```

---

## Task 5: Wire the gate — weak retrieval triggers ABSTAIN

**Files:**
- Modify: `helix_context/context_manager.py:746-787` (insert gate inside the existing scoring block).
- Modify: `tests/test_abstain_tier.py` (add the weak-retrieval gate test + the fixture-stub helper).

This is the structural commit. The gate sits between the score-floor materialization (line 759) and the existing TIGHT/FOCUSED check (line 774).

- [ ] **Step 1: Add the test stub helper + first gate test**

Append to `tests/test_abstain_tier.py`:

```python
def _stub_express(manager, *, candidates, scores):
    """Replace _express with a canned-result version for deterministic tests.

    Real _express runs the genome lookup + co-activation expansion + tier
    accumulation. For ABSTAIN tier tests we need precise top_score/ratio
    control, so we bypass the retrieval pipeline and stuff
    last_query_scores directly. We also stub _apply_candidate_refiners
    to a no-op pass-through so cymatics / harmonic-bin / TCM don't
    perturb the injected scores.
    """
    def fake_express(domains, entities, max_genes, **_kwargs):
        # Real _express has positional-or-keyword args (query_text,
        # include_cold, party_id, use_harmonic, use_sr, read_only). The
        # caller in _build_context_internal passes 4 of those by keyword;
        # **_kwargs absorbs whichever the production code happens to pass
        # so this stub stays robust if the real signature evolves.
        manager.genome.last_query_scores = dict(scores)
        return list(candidates)
    manager._express = fake_express

    def fake_refiners(query, candidates, max_genes, **_kwargs):
        return list(candidates), {}
    manager._apply_candidate_refiners = fake_refiners


def _weak_setup(abstain_manager, *, top_score=1.5, ratio=1.2, n=8):
    """Seed n candidates whose scores yield (top_score, ratio).

    Solves: top = top_score, mean = top_score / ratio. We distribute
    n-1 candidates at score = (n*mean - top) / (n - 1) so the mean
    lands exactly. Returns (candidates, scores).
    """
    from tests.conftest import make_gene
    candidates = [
        make_gene(f"weak_{i}", gene_id=f"weak_gene_{i:010d}")
        for i in range(n)
    ]
    mean = top_score / ratio
    rest = (n * mean - top_score) / (n - 1)
    scores = {candidates[0].gene_id: top_score}
    for c in candidates[1:]:
        scores[c.gene_id] = rest
    return candidates, scores


def test_weak_retrieval_triggers_abstain(abstain_manager):
    """top_score < 2.5 AND ratio < 1.8 → ABSTAIN."""
    candidates, scores = _weak_setup(abstain_manager, top_score=1.5, ratio=1.2)
    _stub_express(abstain_manager, candidates=candidates, scores=scores)

    win = abstain_manager.build_context("anything")

    assert win.metadata["budget_tier"] == "abstain"
    assert win.context_health.status == "abstain"
    assert win.expressed_context == cm._ABSTAIN_MARKER
    assert win.metadata["abstain_reason"] == "score_below_floor"
    # top_score and ratio in metadata reflect what the gate observed
    assert win.metadata["top_score"] == pytest.approx(1.5, abs=1e-6)
    assert win.metadata["ratio"] == pytest.approx(1.2, abs=1e-3)
    assert win.context_health.genes_expressed == 0
```

- [ ] **Step 2: Run, verify fail**

Run: `py -3 -m pytest tests/test_abstain_tier.py::test_weak_retrieval_triggers_abstain -v`
Expected: FAIL — current code falls into BROAD, so `metadata["budget_tier"]` will be `"broad"`, not `"abstain"`.

- [ ] **Step 3: Resolve `_abstain_enabled` per-call**

In `helix_context/context_manager.py`, inside `_build_context_internal` near where `max_genes` is initialized (around line 663), add the per-call resolution:

```python
max_genes = self.config.budget.max_genes_per_turn

# ABSTAIN gate enable-state: config flag AND no env override.
# Resolved per-call so HELIX_ABSTAIN_DISABLE flips without restart.
abstain_enabled = (
    self.config.budget.abstain_enabled
    and not _env_truthy("HELIX_ABSTAIN_DISABLE")
)
```

- [ ] **Step 4: Insert the gate**

In the existing scoring block at `context_manager.py:746-787`, after `gated`/`shadow_pool` are materialized (right after line 759, before the `# Confidence tiering (with shadow pool tracking)` comment block at line 761):

```python
gated = [g for g in candidates if scores.get(g.gene_id, 0) >= floor]
shadow_pool: List[Gene] = [g for g in candidates if scores.get(g.gene_id, 0) < floor]
if len(gated) >= 3:
    candidates = gated

# ── ABSTAIN gate ──────────────────────────────────────────────────
# When retrieval is weak on BOTH the absolute floor AND the ratio,
# inject a marker-only ContextWindow so the small model answers from
# weights instead of digesting 12K of irrelevant noise. Reuses the
# existing FOCUSED_SCORE_FLOOR (defined just below) verbatim — strict
# < on both axes. Telemetry counter is recorded inside the helper's
# call site below, alongside the existing tier counts.
FOCUSED_SCORE_FLOOR_FOR_ABSTAIN = 2.5    # mirrors the local FOCUSED_SCORE_FLOOR below
if (
    abstain_enabled
    and top_score < FOCUSED_SCORE_FLOOR_FOR_ABSTAIN
    and ratio < 1.8
):
    try:
        from .telemetry import budget_tier_counter
        budget_tier_counter().add(1, attributes={"tier": "abstain"})
    except Exception:  # pragma: no cover
        pass
    return self._build_abstain_window(
        query=query,
        effective_decoder_prompt=effective_decoder_prompt,
        top_score=top_score,
        ratio=ratio,
        reason="score_below_floor",
    )

# Confidence tiering (with shadow pool tracking)
# ...
```

> **Why the local `FOCUSED_SCORE_FLOOR_FOR_ABSTAIN` constant?** The existing `FOCUSED_SCORE_FLOOR = 2.5` is defined a few lines below the gate (line 773). Hoisting the existing constant above the gate would change unrelated code; mirroring the value with a separately-named local keeps the diff surgical. Both must stay in sync — Task 5 step 7 below adds an assertion.

- [ ] **Step 5: Run, verify pass**

Run: `py -3 -m pytest tests/test_abstain_tier.py::test_weak_retrieval_triggers_abstain -v`
Expected: PASS.

- [ ] **Step 6: Run the full new-file suite + integration tests**

Run: `py -3 -m pytest tests/test_abstain_tier.py tests/test_pipeline.py tests/test_context_manager_classifier.py -v`
Expected: all PASS — pre-existing pipeline + classifier integration tests still pass since strong-signal queries don't hit the gate.

- [ ] **Step 7: Add a sync-check test for the two FOCUSED-floor constants**

Append to `tests/test_abstain_tier.py`:

```python
def test_focused_score_floor_constants_in_sync():
    """The ABSTAIN gate mirrors the FOCUSED_SCORE_FLOOR = 2.5 constant
    defined just below it in context_manager.py. If one is bumped
    without the other, the gate's strict-less-than semantic and the
    FOCUSED tier's threshold will drift. This test pins them together.
    """
    import inspect
    src = inspect.getsource(cm.HelixContextManager._build_context_internal) \
        if hasattr(cm.HelixContextManager, "_build_context_internal") \
        else inspect.getsource(cm)
    assert "FOCUSED_SCORE_FLOOR_FOR_ABSTAIN = 2.5" in src
    assert "FOCUSED_SCORE_FLOOR = 2.5" in src
```

- [ ] **Step 8: Commit**

```bash
git add helix_context/context_manager.py tests/test_abstain_tier.py
git commit -m "feat(context): wire ABSTAIN gate above BROAD tier

When abstain_enabled=true and HELIX_ABSTAIN_DISABLE is unset, return
a marker-only ContextWindow whenever post-refinement top_score < 2.5
AND ratio < 1.8. Closes the open finding from the 2026-05-01 GPQA
Diamond overnight where helix paid +34s mean latency on the n=147
fic=False subset by injecting 12K of irrelevant noise.

See docs/specs/2026-05-02-abstain-tier-design.md §5."
```

---

## Task 6: Strong-signal & boundary cases (don't false-fire)

**Files:**
- Modify: `tests/test_abstain_tier.py`.

These tests verify the gate's strict-< semantics and that strong/moderate signals continue to land in TIGHT/FOCUSED unchanged.

- [ ] **Step 1: Write the four cases**

Append to `tests/test_abstain_tier.py`:

```python
def test_strong_signal_lands_in_tight(abstain_manager):
    """top_score=8.0, ratio=4.0 → TIGHT, ABSTAIN does not fire."""
    candidates, scores = _weak_setup(abstain_manager, top_score=8.0, ratio=4.0)
    _stub_express(abstain_manager, candidates=candidates, scores=scores)
    win = abstain_manager.build_context("anything")
    assert win.metadata["budget_tier"] == "tight"
    assert win.context_health.status != "abstain"


def test_focused_eligible_lands_in_focused(abstain_manager):
    """top_score=3.5, ratio=2.0 → FOCUSED, ABSTAIN does not fire."""
    candidates, scores = _weak_setup(abstain_manager, top_score=3.5, ratio=2.0)
    _stub_express(abstain_manager, candidates=candidates, scores=scores)
    win = abstain_manager.build_context("anything")
    assert win.metadata["budget_tier"] == "focused"
    assert win.context_health.status != "abstain"


def test_boundary_at_score_floor_does_not_abstain(abstain_manager):
    """top_score=2.5 (== FOCUSED_SCORE_FLOOR) does NOT trigger ABSTAIN.

    Strict-< on the score axis means the FOCUSED-eligible boundary
    case still earns FOCUSED. ratio is held below the FOCUSED ratio
    floor (2.0) but above the abstain ratio floor (1.8) intentionally
    isn't possible at this score; instead we verify that with
    top_score=2.5 and ratio=1.2, the ABSTAIN axis fails (score axis
    is NOT-less-than 2.5) and we fall through to BROAD.
    """
    candidates, scores = _weak_setup(abstain_manager, top_score=2.5, ratio=1.2)
    _stub_express(abstain_manager, candidates=candidates, scores=scores)
    win = abstain_manager.build_context("anything")
    assert win.metadata["budget_tier"] != "abstain"


def test_boundary_at_ratio_floor_does_not_abstain(abstain_manager):
    """ratio=1.8 (== abstain ratio floor) does NOT trigger ABSTAIN.

    Strict-< on the ratio axis means the boundary stays out of ABSTAIN.
    With top_score=1.5 < FOCUSED_SCORE_FLOOR but ratio==1.8, the gate
    fails on the ratio axis and we fall through to BROAD.
    """
    candidates, scores = _weak_setup(abstain_manager, top_score=1.5, ratio=1.8)
    _stub_express(abstain_manager, candidates=candidates, scores=scores)
    win = abstain_manager.build_context("anything")
    assert win.metadata["budget_tier"] != "abstain"
```

- [ ] **Step 2: Run, verify all four pass without code changes**

Run: `py -3 -m pytest tests/test_abstain_tier.py -v -k "strong or focused_eligible or boundary"`
Expected: 4 PASS — gate has strict-< on both axes; existing TIGHT/FOCUSED logic still owns its tiers.

If any FAIL, the gate condition was implemented as `<=` somewhere — go back to Task 5 step 4 and fix.

- [ ] **Step 3: Commit**

```bash
git add tests/test_abstain_tier.py
git commit -m "test(context): pin ABSTAIN strict-< boundaries + non-firing on strong signals

Verifies the gate doesn't false-fire on TIGHT-eligible (top=8.0,
ratio=4.0) or FOCUSED-eligible (top=3.5, ratio=2.0) queries, and
that the boundary cases (top==2.5 or ratio==1.8) stay outside
ABSTAIN per spec §3 strict-< semantics.

See docs/specs/2026-05-02-abstain-tier-design.md §3, §8."
```

---

## Task 7: Flag-off + env-override cases

**Files:**
- Modify: `tests/test_abstain_tier.py`.

- [ ] **Step 1: Add the two tests**

Append to `tests/test_abstain_tier.py`:

```python
def test_abstain_disabled_via_config_falls_through_to_broad(abstain_manager):
    """abstain_enabled=False on weak retrieval → BROAD (legacy behavior)."""
    abstain_manager.config.budget.abstain_enabled = False
    candidates, scores = _weak_setup(abstain_manager, top_score=1.5, ratio=1.2)
    _stub_express(abstain_manager, candidates=candidates, scores=scores)
    win = abstain_manager.build_context("anything")
    assert win.metadata["budget_tier"] == "broad"
    assert win.context_health.status != "abstain"


def test_abstain_env_override_beats_config_flag(abstain_manager, monkeypatch):
    """HELIX_ABSTAIN_DISABLE=1 forces off even when config flag is on."""
    monkeypatch.setenv("HELIX_ABSTAIN_DISABLE", "1")
    assert abstain_manager.config.budget.abstain_enabled is True   # config still on
    candidates, scores = _weak_setup(abstain_manager, top_score=1.5, ratio=1.2)
    _stub_express(abstain_manager, candidates=candidates, scores=scores)
    win = abstain_manager.build_context("anything")
    assert win.metadata["budget_tier"] == "broad"
```

- [ ] **Step 2: Run, verify pass**

Run: `py -3 -m pytest tests/test_abstain_tier.py -v -k "disabled or env_override"`
Expected: 2 PASS — Task 5 step 3 already wires both checks.

- [ ] **Step 3: Commit**

```bash
git add tests/test_abstain_tier.py
git commit -m "test(context): pin ABSTAIN kill-switch behavior (flag + env)

Two tests covering the rollout safety net: setting [budget]
abstain_enabled=False or exporting HELIX_ABSTAIN_DISABLE=1 must
restore legacy BROAD-on-weak-retrieval behavior. The env override
is honored even when the config flag is on, so an operator can
flip without redeploy.

See docs/specs/2026-05-02-abstain-tier-design.md §6.3."
```

---

## Task 8: Telemetry counter increments with `tier="abstain"`

**Files:**
- Modify: `tests/test_abstain_tier.py`.
- Modify: `helix_context/telemetry.py:374-378` (docstring only).

- [ ] **Step 1: Update the counter docstring**

In `helix_context/telemetry.py`, edit the `budget_tier_counter` description (lines ~374-378):

```python
def budget_tier_counter():
    if "budget_tier" not in _instruments:
        _instruments["budget_tier"] = meter.create_counter(
            "helix_budget_tier_total",
            description="Dynamic budget tier selected per /context call, labelled "
                        "by tier (tight | focused | broad | abstain). Tier reflects "
                        "retrieval confidence: tight = single-gene dominance, "
                        "focused = moderate, broad = weak signal / widen the net, "
                        "abstain = below FOCUSED floor on both axes (no injection).",
        )
    return _instruments["budget_tier"]
```

- [ ] **Step 2: Add the telemetry test**

Append to `tests/test_abstain_tier.py`:

```python
def test_telemetry_counter_increments_with_abstain_label(
    abstain_manager, monkeypatch
):
    """Verify budget_tier_counter is called with attributes={'tier': 'abstain'}."""
    calls: list[dict] = []

    class _Recorder:
        def add(self, value, attributes=None):
            calls.append({"value": value, "attributes": dict(attributes or {})})

    monkeypatch.setattr(
        "helix_context.telemetry.budget_tier_counter",
        lambda: _Recorder(),
    )
    candidates, scores = _weak_setup(abstain_manager, top_score=1.5, ratio=1.2)
    _stub_express(abstain_manager, candidates=candidates, scores=scores)
    abstain_manager.build_context("anything")

    abstain_calls = [c for c in calls if c["attributes"].get("tier") == "abstain"]
    assert len(abstain_calls) == 1
    assert abstain_calls[0]["value"] == 1
```

- [ ] **Step 3: Run, verify pass**

Run: `py -3 -m pytest tests/test_abstain_tier.py::test_telemetry_counter_increments_with_abstain_label -v`
Expected: PASS — Task 5 step 4 already adds the counter call.

- [ ] **Step 4: Run the full suite to catch any regression**

Run: `py -3 -m pytest tests/ -q`
Expected: all PASS. Full suite check is cheap (~30s) and ensures none of the prior tasks regressed unrelated tests.

- [ ] **Step 5: Commit**

```bash
git add helix_context/telemetry.py tests/test_abstain_tier.py
git commit -m "feat(telemetry): document abstain label + add regression test

budget_tier_counter description now lists abstain as a valid label
value alongside tight | focused | broad. Test pins that the gate's
firing path increments the counter with attributes={tier: abstain}.

See docs/specs/2026-05-02-abstain-tier-design.md §7."
```

---

## Task 9: Open the PR

**Files:** none — git operation only.

- [ ] **Step 1: Push the branch**

```bash
git push -u origin <feature-branch>
```

(Pick a branch name like `feat/abstain-tier`. If you started from `spec/abstain-tier`, branch off it: `git switch -c feat/abstain-tier`.)

- [ ] **Step 2: Open the PR**

```bash
gh pr create --title "feat: ABSTAIN tier — confidence-gated context attachment" --body "$(cat <<'EOF'
## Summary
- Adds an ABSTAIN tier above BROAD that returns a marker-only ContextWindow when post-refinement scores are weak on both axes (`top_score < 2.5` AND `ratio < 1.8`).
- Reuses calibrated FOCUSED thresholds verbatim — no new hyperparameter.
- Ships on by default; `[budget] abstain_enabled = false` or `HELIX_ABSTAIN_DISABLE=1` restores legacy behavior.
- New `tests/test_abstain_tier.py` covers all eight cases from spec §8.

## Why
Closes the open finding from the 2026-05-01 GPQA Diamond overnight: helix paid +34s mean latency on the n=147 `fic=False` subset by injecting 12K of irrelevant gene expressions into the small e4b model. Reframe: helix wasn't slow; helix was missing a confidence gate.

## Spec
docs/specs/2026-05-02-abstain-tier-design.md

## Test plan
- [x] `py -3 -m pytest tests/test_abstain_tier.py -v` — 16 tests pass
- [x] `py -3 -m pytest tests/ -q` — full suite green, no regressions
- [ ] Bench gate before merge: re-run GPQA Diamond on the n=147 fic=False subset. p95 must drop ≥ 15s without fic=True accuracy regression. Report committed to `overnight_logs/diamond_abstain_<date>_report.md`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Out of Scope (do NOT do in this PR)

These belong to follow-up PRs and are explicitly fenced off (spec §11):

- Coordinate-confidence as second OR-trigger. Lift `_coordinate_signals` to a shared module first.
- PLR `prob_B` as second-stage gate. Gated on PLR head reaching AUC ≥ 0.7.
- BROAD budget tighten (`expression_tokens` 12K → 6-8K, `max_genes_per_turn` 12 → 8).
- Mid-confidence "single-anchor" tier between FOCUSED and ABSTAIN.

---

## Bench gate (after Task 9 merges)

The implementing PR cannot merge until the bench gate passes. Run on your dev machine, NOT in CI (CI has no GPU/Ollama):

```bash
# Baseline list of fic=False ids — derived from 2026-05-01 overnight
# Find them in benchmarks/results/gpqa_on_diamond_2026-05-01.json
python -c "
import json, sys
data = json.load(open('benchmarks/results/gpqa_on_diamond_2026-05-01.json'))
fic_false = [r['id'] for r in data['results'] if not r.get('found_in_context')]
open('baseline_fic_false_ids.txt', 'w').write('\n'.join(fic_false))
print(f'wrote {len(fic_false)} ids')
"

# Run abstain bench
python benchmarks/bench_aa_suite.py \
    --bench gpqa_diamond \
    --ids baseline_fic_false_ids.txt \
    --timeout 240 \
    --out benchmarks/results/gpqa_on_diamond_abstain_<DATE>.json

# Compare
python benchmarks/compare_ab.py \
    benchmarks/results/gpqa_on_diamond_2026-05-01.json \
    benchmarks/results/gpqa_on_diamond_abstain_<DATE>.json \
    --subset fic_false
```

**Pass criteria** (both required):
1. p95 latency on the fic=False subset drops by ≥ 15s.
2. Accuracy on the fic=False subset is ≥ baseline (no regression).

Commit the report to `overnight_logs/diamond_abstain_<DATE>_report.md` (use `git add -f` — overnight_logs/ is gitignored). Cross-link from the PR body.
