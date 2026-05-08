# Foveated-Splice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement rank-scaled per-gene compression + reverse-rank placement for the BROAD tier of `build_context`, off by default, gated by `foveated_enabled` config flag. Per-gene byte cap follows `c_i = max(c_min, c_max · i^(-α))` and feeds the existing Step 4 `compress_text(target_chars=...)` call so each gene gets a rank-proportional slice of the BROAD char budget.

**Architecture:** Three integration points inside `helix_context/context_manager.py`:
1. **After tier resolves (post line ~843):** compute `caps[]` and reverse `(candidates, caps)` together when foveated is active on BROAD. Stash on `self._last_foveated_caps`.
2. **Step 4 compression loop (around line 965):** swap the hardcoded `target_chars=1000` for `int(caps[i] * foveated_base_chars)` when caps are stashed; otherwise keep `1000`.
3. **`_assemble` (line 1716):** add a `respect_caller_order: bool = False` parameter. When True, skip the score-DESC and sequence-index re-sorts so the reversed BROAD ordering survives all the way to the prompt.

Compression schedule lives in a pure helper (`_compute_foveated_caps`) for ease of testing. Telemetry adds two `metadata["foveated_*"]` keys when the path fires.

**Tech Stack:** Python 3.13, pytest, dataclasses (config), OpenTelemetry counters (already present, no new metrics).

**Spec:** `docs/specs/2026-05-03-foveated-splice-design.md` (NOTE: §7 names `_build_item(max_item_chars=...)` as the seam, but on master that function lives only in `context_packet.py` and is not reachable from `build_context`. The actual seam is `compress_text(target_chars=...)` at `context_manager.py:965`. The plan codifies this; a follow-up commit can patch the spec.)

---

## File Structure

| File | Action | Responsibility |
| --- | --- | --- |
| `helix_context/config.py` | Modify | Add 4 fields to `BudgetConfig` (`foveated_enabled`, `foveated_alpha`, `foveated_c_min`, `foveated_base_chars`) + matching loader entries in `load_config`. |
| `helix.toml` | Modify | Add the four `[budget]` keys with comment block referencing the spec §6. |
| `helix_context/context_manager.py` | Modify | Add `_compute_foveated_caps` helper, foveated block after BROAD resolves, Step 4 cap lookup, `_assemble` `respect_caller_order` parameter, telemetry stash. |
| `tests/test_foveated_splice.py` | Create | All eight cases from spec §10 + helper unit tests + the `_assemble` ordering regression. |
| `tests/test_config.py` | Modify | Default + toml-override regression tests for the four new keys. |

**Files explicitly NOT touched:**
- `helix_context/context_packet.py` — `_build_item` / `_DEFAULT_MAX_ITEM_CHARS = 280` belong to the `/context` packet path, not `build_context`. Foveated does not run there.
- `helix_context/telemetry.py` — `budget_tier_counter` already labels `"broad"`; no new counter needed (per spec §8).
- `helix_context/headroom_bridge.py` — `compress_text` already accepts `target_chars`; no signature change.

---

## Task 1: Add the four `foveated_*` config fields

**Files:**
- Modify: `helix_context/config.py` — `BudgetConfig` dataclass (around the existing `abstain_enabled` field) and `load_config` (around the existing `abstain_enabled` loader line).
- Modify: `helix.toml` — `[budget]` section.
- Modify: `tests/test_config.py` — extend with new regression tests.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
def test_budget_foveated_defaults_off_with_alpha_one():
    """Regression: foveated ships off-by-default with alpha=1.0, c_min=0.15.

    The 2026-05-03 foveated-splice spec (docs/specs/2026-05-03-foveated-
    splice-design.md §6.3) ships off-by-default for a measurement period.
    A bench α-sweep is required before flipping on. Bumping any default
    here without bench evidence would silently change BROAD-tier
    compression on every install.
    """
    from helix_context.config import HelixConfig
    cfg = HelixConfig()
    assert cfg.budget.foveated_enabled is False
    assert cfg.budget.foveated_alpha == 1.0
    assert cfg.budget.foveated_c_min == 0.15
    assert cfg.budget.foveated_base_chars == 1000


def test_budget_foveated_toml_override(tmp_path):
    """Regression: helix.toml [budget] foveated_* keys are honored."""
    import tomli_w
    from helix_context.config import load_config
    p = tmp_path / "helix.toml"
    p.write_bytes(tomli_w.dumps({
        "budget": {
            "foveated_enabled": True,
            "foveated_alpha": 2.0,
            "foveated_c_min": 0.20,
            "foveated_base_chars": 1500,
        }
    }).encode())
    cfg = load_config(str(p))
    assert cfg.budget.foveated_enabled is True
    assert cfg.budget.foveated_alpha == 2.0
    assert cfg.budget.foveated_c_min == 0.20
    assert cfg.budget.foveated_base_chars == 1500
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m pytest tests/test_config.py::test_budget_foveated_defaults_off_with_alpha_one tests/test_config.py::test_budget_foveated_toml_override -v`

Expected: FAIL with `AttributeError: 'BudgetConfig' object has no attribute 'foveated_enabled'`

- [ ] **Step 3: Add the four fields to `BudgetConfig`**

In `helix_context/config.py`, locate the `abstain_enabled: bool = True` line in `BudgetConfig` and append below it:

```python
    # Foveated-splice (BROAD tier only). Off by default for the measurement
    # period — see docs/specs/2026-05-03-foveated-splice-design.md §6.3 and
    # docs/plans/2026-05-05-foveated-splice.md. Flip to True only after the
    # phased α-sweep bench (§9) identifies a winning configuration.
    foveated_enabled: bool = False
    # Power-law exponent for c_i = max(c_min, c_max · i^(-α)). α=0.5 = gentle
    # decay, α=1.0 = harmonic-ish, α=2.0 = aggressive top-bias.
    foveated_alpha: float = 1.0
    # Rank-N floor compression ratio. Pinned at 0.15 by spec §4.1.
    foveated_c_min: float = 0.15
    # Per-gene char-budget multiplier. Each gene's target_chars =
    # int(c_i · foveated_base_chars). Default 1000 matches the current
    # uniform behavior at c_i = 1.0. The Step 4 compression loop in
    # context_manager.py uses 1000 today; keeping this configurable lets
    # bench cells (and a future on-by-default ship) tune the top-1 ceiling
    # without touching code.
    foveated_base_chars: int = 1000
```

- [ ] **Step 4: Add the loader entries**

In `helix_context/config.py` `load_config`, locate the `abstain_enabled=bool(b.get(...))` line and append below it:

```python
            foveated_enabled=bool(b.get("foveated_enabled", cfg.budget.foveated_enabled)),
            foveated_alpha=float(b.get("foveated_alpha", cfg.budget.foveated_alpha)),
            foveated_c_min=float(b.get("foveated_c_min", cfg.budget.foveated_c_min)),
            foveated_base_chars=int(b.get("foveated_base_chars", cfg.budget.foveated_base_chars)),
```

- [ ] **Step 5: Add the `[budget]` keys to `helix.toml`**

Append to the `[budget]` section in `helix.toml`:

```toml
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
```

- [ ] **Step 6: Run config tests — expect PASS**

Run: `py -3 -m pytest tests/test_config.py -v`

Expected: all green, including the two new tests.

- [ ] **Step 7: Commit**

```bash
git add helix_context/config.py helix.toml tests/test_config.py
git commit -m "feat(config): add foveated-splice config fields

Adds foveated_enabled, foveated_alpha, foveated_c_min, foveated_base_chars
to BudgetConfig + helix.toml [budget]. All four ship off-by-default.

Spec: docs/specs/2026-05-03-foveated-splice-design.md §6
Plan: docs/plans/2026-05-05-foveated-splice.md Task 1

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Add `_compute_foveated_caps` pure helper

**Files:**
- Modify: `helix_context/context_manager.py` — add module-level helper near the top of the file (after imports, before the class definition). A pure function makes the schedule shape trivially testable.
- Create: `tests/test_foveated_splice.py` — start the file, add helper unit tests.

- [ ] **Step 1: Write failing helper tests**

Create `tests/test_foveated_splice.py`:

```python
"""Tests for foveated-splice — rank-scaled BROAD compression schedule.

Spec: docs/specs/2026-05-03-foveated-splice-design.md
Plan: docs/plans/2026-05-05-foveated-splice.md
"""
import pytest

from helix_context.context_manager import _compute_foveated_caps


class TestComputeFoveatedCaps:
    """Pure-function tests for the schedule-shape helper (spec §4.1)."""

    def test_alpha_one_n_twelve_matches_spec_table(self):
        """Spec §10 Test 6: caps[0]=1.0, caps[1]=0.5, caps[5]≈1/6, caps[11]=c_min."""
        caps = _compute_foveated_caps(n=12, alpha=1.0, c_min=0.15, c_max=1.0)
        assert len(caps) == 12
        assert caps[0] == 1.0
        assert caps[1] == 0.5
        assert caps[5] == pytest.approx(1 / 6, abs=1e-9)
        # rank-12 → 1/12 ≈ 0.083 < c_min=0.15, so floor wins
        assert caps[11] == 0.15

    def test_alpha_two_collapses_to_floor_by_rank_three(self):
        """Spec §10 Test 8: α=2 floors caps[5..11] at c_min."""
        caps = _compute_foveated_caps(n=12, alpha=2.0, c_min=0.15, c_max=1.0)
        # 1/3^2 = 0.111 < 0.15 → already floored at rank 3
        for i in range(2, 12):
            assert caps[i] == 0.15, f"caps[{i}]={caps[i]} expected 0.15"

    def test_alpha_half_gentle_decay(self):
        """Spec §4.2 table: α=0.5 caps[1] ≈ 0.71."""
        caps = _compute_foveated_caps(n=12, alpha=0.5, c_min=0.15, c_max=1.0)
        assert caps[0] == 1.0
        assert caps[1] == pytest.approx(1 / (2 ** 0.5), abs=1e-9)  # ≈ 0.7071
        assert caps[2] == pytest.approx(1 / (3 ** 0.5), abs=1e-9)  # ≈ 0.5774

    def test_c_min_floor_is_inclusive_lower_bound(self):
        """A custom c_min raises the floor for low-rank genes."""
        caps = _compute_foveated_caps(n=12, alpha=1.0, c_min=0.30, c_max=1.0)
        assert caps[0] == 1.0
        # rank-4 → 0.25 < c_min=0.30, floors at 0.30
        for i in range(3, 12):
            assert caps[i] == 0.30

    def test_n_one_returns_single_c_max(self):
        """Edge case: a single-candidate BROAD set returns [c_max]."""
        assert _compute_foveated_caps(n=1, alpha=1.0, c_min=0.15) == [1.0]

    def test_n_zero_returns_empty(self):
        """Edge case: empty input → empty output, never raises."""
        assert _compute_foveated_caps(n=0, alpha=1.0, c_min=0.15) == []
```

- [ ] **Step 2: Run tests — expect ImportError**

Run: `py -3 -m pytest tests/test_foveated_splice.py -v`

Expected: FAIL with `ImportError: cannot import name '_compute_foveated_caps' from 'helix_context.context_manager'`

- [ ] **Step 3: Add the helper**

In `helix_context/context_manager.py`, after the existing imports and before the `class HelixContextManager:` line (search for the first `class ` definition; the helper goes immediately above it):

```python
def _compute_foveated_caps(
    n: int,
    alpha: float,
    c_min: float,
    c_max: float = 1.0,
) -> list[float]:
    """Power-law per-gene compression caps for foveated-splice.

    c_i = max(c_min, c_max · i^(-α))    for i ∈ [1, N]

    Returns a list of N floats in forward-rank order (caps[0] = rank-1 cap,
    caps[N-1] = rank-N cap). Caller reverses to pair with reverse-rank
    candidate placement.

    Spec: docs/specs/2026-05-03-foveated-splice-design.md §4.1
    """
    if n <= 0:
        return []
    return [max(c_min, c_max * ((i + 1) ** -alpha)) for i in range(n)]
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `py -3 -m pytest tests/test_foveated_splice.py -v`

Expected: 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add helix_context/context_manager.py tests/test_foveated_splice.py
git commit -m "feat(context): add _compute_foveated_caps schedule helper

Pure function computing power-law per-gene compression caps for
foveated-splice. Tests cover α∈{0.5,1.0,2.0}, c_min floor, edge cases
(N=0, N=1).

Spec: docs/specs/2026-05-03-foveated-splice-design.md §4.1
Plan: docs/plans/2026-05-05-foveated-splice.md Task 2

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Add `respect_caller_order` parameter to `_assemble`

**Files:**
- Modify: `helix_context/context_manager.py` — `_assemble` signature (around line 1716) + the use_slate sort block (around lines 1740-1752).
- Modify: `tests/test_foveated_splice.py` — add ordering test (covers spec §10 Test 7).

**Why this task is separate:** `_assemble` re-sorts candidates internally (line 1745: `sorted(candidates, key=score, reverse=True)` on the slate path; line 1752: `sorted(by sequence_index)` on the dense path). A naïve `list.reverse()` before the call gets clobbered. We need to opt out of the re-sort when foveated is driving placement.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_foveated_splice.py`:

```python
class TestAssembleRespectsCallerOrder:
    """When respect_caller_order=True, _assemble preserves the candidates list
    order verbatim — no score-desc or sequence-index re-sort. This is what
    makes reverse-rank placement actually reach the prompt (spec §5)."""

    def test_assemble_default_resorts_by_score_for_slate(self, helix_manager_with_three_genes):
        """Sanity: default (use_slate=True) re-sorts by score DESC."""
        m, g_low, g_mid, g_high = helix_manager_with_three_genes
        # Pass in low→high; default behavior should re-emit high→low
        window = m._assemble(
            query="q",
            candidates=[g_low, g_mid, g_high],
            spliced_map={
                g_low.gene_id: "<G>L</G>",
                g_mid.gene_id: "<G>M</G>",
                g_high.gene_id: "<G>H</G>",
            },
            relation_graph={},
            answer_slate=["k=v"],  # forces use_slate=True
        )
        # Top-score gene (g_high) should appear first in the rendered text.
        assert window.text.find("H") < window.text.find("L")

    def test_assemble_respects_caller_order_when_flagged(self, helix_manager_with_three_genes):
        """With respect_caller_order=True, the input order is preserved."""
        m, g_low, g_mid, g_high = helix_manager_with_three_genes
        # Pass in reverse-rank order (low→high → bottom-rank first, top-rank last)
        window = m._assemble(
            query="q",
            candidates=[g_low, g_mid, g_high],
            spliced_map={
                g_low.gene_id: "<G>L</G>",
                g_mid.gene_id: "<G>M</G>",
                g_high.gene_id: "<G>H</G>",
            },
            relation_graph={},
            answer_slate=["k=v"],
            respect_caller_order=True,
        )
        # L came first in input → stays first in text. H last in input → last.
        assert window.text.find("L") < window.text.find("M") < window.text.find("H")
```

Add the fixture at the top of the test file (or in `conftest.py` if one exists in `tests/`):

```python
@pytest.fixture
def helix_manager_with_three_genes(tmp_path):
    """In-memory genome + a HelixContextManager with 3 genes of distinct scores.

    Mirrors the tests/test_abstain_tier.py fixture pattern. The three genes
    have score 1.0 (g_low), 5.0 (g_mid), 9.0 (g_high) so any sort by score
    is observable.
    """
    from helix_context.config import HelixConfig
    from helix_context.context_manager import HelixContextManager
    from helix_context.genome import Genome
    # Replicate the abstain-tier fixture's exact construction:
    # see tests/test_abstain_tier.py for the canonical pattern.
    raise NotImplementedError(
        "Copy the fixture from tests/test_abstain_tier.py and adapt to "
        "produce three genes with scores 1.0, 5.0, 9.0. See spec §10 "
        "preamble: 'Test fixtures mirror the tests/test_abstain_tier.py "
        "pattern (in-memory genome, mock backend, controllable scores via "
        "_stub_express).'"
    )
```

> **Note for the implementer:** Read `tests/test_abstain_tier.py` first to copy the existing fixture pattern. The placeholder above intentionally raises so you don't skip this step. The three genes need stable `gene_id`s and the `last_query_scores` map needs to contain {g_low: 1.0, g_mid: 5.0, g_high: 9.0} so the slate-path sort has something to sort by.

- [ ] **Step 2: Run tests — expect FAIL**

Run: `py -3 -m pytest tests/test_foveated_splice.py::TestAssembleRespectsCallerOrder -v`

Expected: FAIL — either `TypeError: _assemble() got an unexpected keyword argument 'respect_caller_order'` or, after adding the param without wiring, the order test fails because the slate sort still runs.

- [ ] **Step 3: Add the parameter and gate the re-sort**

In `helix_context/context_manager.py` `_assemble` (around line 1716), add `respect_caller_order: bool = False` to the signature. Then around lines 1740-1752, gate the existing sort:

```python
        if respect_caller_order:
            # Foveated-splice path (spec §5): the caller has already arranged
            # candidates in the desired emission order (e.g., reverse-rank
            # for BROAD). Skip the re-sort so reverse-rank actually reaches
            # the prompt instead of being clobbered back to score-DESC.
            sorted_genes = list(candidates)
        elif use_slate:
            # MoE/small-model: relevance-first ordering — best gene at position 0
            # so it's within every sliding-window attention layer
            scores = self.genome.last_query_scores or {}
            sorted_genes = sorted(
                candidates,
                key=lambda g: scores.get(g.gene_id, 0),
                reverse=True,
            )
        else:
            # Dense: sequence ordering for narrative coherence
            sorted_genes = sorted(candidates, key=lambda g: g.promoter.sequence_index or 0)
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `py -3 -m pytest tests/test_foveated_splice.py::TestAssembleRespectsCallerOrder -v`

Expected: 2 tests PASS.

- [ ] **Step 5: Run the full assembled-related regression suite**

Run: `py -3 -m pytest tests/test_abstain_tier.py tests/test_context_manager_classifier.py -q`

Expected: all green. The new parameter defaults to False, so existing call sites are unaffected.

- [ ] **Step 6: Commit**

```bash
git add helix_context/context_manager.py tests/test_foveated_splice.py
git commit -m "feat(context): add respect_caller_order to _assemble

Lets foveated-splice's reverse-rank placement survive _assemble's
internal re-sort. Default False = no behavior change for existing
callers. The slate-path sort (line 1745) and sequence-index sort
(line 1752) only run when the flag is False.

Spec: docs/specs/2026-05-03-foveated-splice-design.md §5
Plan: docs/plans/2026-05-05-foveated-splice.md Task 3

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Wire foveated into `build_context` BROAD branch

**Files:**
- Modify: `helix_context/context_manager.py` — three insert/edit sites:
  - After line 843 (BROAD-tier resolution): compute caps + reverse.
  - Around line 940-970 (Step 4 compress loop): use foveated caps when stashed.
  - Around line 974 (`_assemble` call): pass `respect_caller_order=foveated_active`.
  - After line 985 (window metadata): stash `foveated_caps` + `foveated_alpha` when active.
- Modify: `tests/test_foveated_splice.py` — add tier-gating + metadata + reverse-rank-end-to-end tests (spec §10 Tests 1, 2, 3, 4, 5, 7).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_foveated_splice.py`:

```python
class TestFoveatedTierGating:
    """Foveated only fires on BROAD (spec §3, §10 Tests 1-5)."""

    def test_disabled_broad_no_metadata(self, helix_manager_broad_with_twelve_genes):
        """Test 1: foveated_enabled=False on BROAD → no metadata key, uniform compression."""
        m = helix_manager_broad_with_twelve_genes
        m.config.budget.foveated_enabled = False
        window = m.build_context("a query that lands in BROAD")
        assert window.metadata.get("budget_tier") == "broad"
        assert "foveated_caps" not in window.metadata

    def test_enabled_broad_metadata_present(self, helix_manager_broad_with_twelve_genes):
        """Test 2: foveated_enabled=True on BROAD → metadata['foveated_caps'] is a list, len = N."""
        m = helix_manager_broad_with_twelve_genes
        m.config.budget.foveated_enabled = True
        window = m.build_context("a query that lands in BROAD")
        assert window.metadata.get("budget_tier") == "broad"
        caps = window.metadata.get("foveated_caps")
        assert isinstance(caps, list)
        assert len(caps) == 12
        assert window.metadata.get("foveated_alpha") == 1.0

    def test_enabled_tight_no_metadata(self, helix_manager_tight):
        """Test 3: foveated_enabled=True but tier=tight → no metadata key."""
        m = helix_manager_tight
        m.config.budget.foveated_enabled = True
        window = m.build_context("a query that lands in TIGHT")
        assert window.metadata.get("budget_tier") == "tight"
        assert "foveated_caps" not in window.metadata

    def test_enabled_focused_no_metadata(self, helix_manager_focused):
        """Test 4: foveated_enabled=True but tier=focused → no metadata key."""
        m = helix_manager_focused
        m.config.budget.foveated_enabled = True
        window = m.build_context("a query that lands in FOCUSED")
        assert window.metadata.get("budget_tier") == "focused"
        assert "foveated_caps" not in window.metadata

    def test_enabled_abstain_no_metadata(self, helix_manager_abstain):
        """Test 5: foveated_enabled=True but ABSTAIN gate fired first → no metadata key."""
        m = helix_manager_abstain
        m.config.budget.foveated_enabled = True
        window = m.build_context("a weak query that triggers ABSTAIN")
        assert window.metadata.get("budget_tier") == "abstain"
        assert "foveated_caps" not in window.metadata


class TestFoveatedReverseRankEndToEnd:
    """Top-rank gene lands LAST in the assembled prompt (spec §10 Test 7)."""

    def test_top_rank_gene_appears_last_in_window_text(
        self, helix_manager_broad_with_twelve_genes,
    ):
        m = helix_manager_broad_with_twelve_genes
        m.config.budget.foveated_enabled = True
        window = m.build_context("a query that lands in BROAD")
        # The fixture seeds gene_0..gene_11 with descending scores. With
        # foveated on, gene_0 (highest score) should be reversed to LAST.
        # Verify by index of the gene_id strings in window.text.
        idx_top = window.text.find("gene_0")
        idx_bottom = window.text.find("gene_11")
        assert idx_top != -1 and idx_bottom != -1
        assert idx_top > idx_bottom, (
            "Reverse-rank failed: top-score gene should appear AFTER "
            "bottom-rank gene in the assembled window."
        )
```

> **Implementer note on fixtures:** The four `helix_manager_*` fixtures (`_broad_with_twelve_genes`, `_tight`, `_focused`, `_abstain`) need to seed scores that land in the right tier per the existing logic at `context_manager.py:829-843`:
> - **BROAD:** 12 genes, top_score < 5.0 (below TIGHT_SCORE_FLOOR) — e.g. all scores in [1.0, 3.0] — so the tier resolves to `broad`.
> - **TIGHT:** ratio ≥ 3.0 AND top ≥ 5.0 AND ≥ 3 candidates — e.g. top=10, others=2.0.
> - **FOCUSED:** ratio ≥ 1.8 AND top ≥ 2.5 AND ≥ 6 candidates, but not TIGHT — e.g. top=4.5, mean=2.0.
> - **ABSTAIN:** trigger via `tests/test_abstain_tier.py`'s existing weak-retrieval pattern.
> Read `tests/test_abstain_tier.py` end-to-end before writing these — copy its fixture machinery rather than re-deriving it.

- [ ] **Step 2: Run tests — expect FAIL**

Run: `py -3 -m pytest tests/test_foveated_splice.py -v`

Expected: the tier-gating and reverse-rank tests FAIL because the foveated block doesn't exist yet in `build_context`.

- [ ] **Step 3: Add the foveated block after BROAD tier resolves**

In `helix_context/context_manager.py`, immediately after the `# else: broad — keep current up-to-max_genes set` comment (around line 843, BEFORE the `# Stash shadow pool for Lagrange check` line), insert:

```python
                # Foveated-splice (spec §4-5): for BROAD only, replace uniform
                # per-gene compression with a rank-scaled power-law schedule
                # AND reverse the assembly order so top-rank lands nearest
                # the user query (lost-in-the-middle exploit). Off by default;
                # see docs/specs/2026-05-03-foveated-splice-design.md §6.3.
                # No env override in v1 — when foveated flips on by default
                # later, add HELIX_FOVEATED_DISABLE via _env_truthy at that
                # point (spec §6.3).
                foveated_active = (
                    budget_tier == "broad"
                    and self.config.budget.foveated_enabled
                    and len(candidates) > 1
                )
                if foveated_active:
                    caps = _compute_foveated_caps(
                        n=len(candidates),
                        alpha=self.config.budget.foveated_alpha,
                        c_min=self.config.budget.foveated_c_min,
                        c_max=1.0,
                    )
                    # Reverse together so caps[i] still pairs with candidates[i]
                    candidates = list(reversed(candidates))
                    caps = list(reversed(caps))
                    self._last_foveated_caps = caps
                    self._last_foveated_active = True
                else:
                    self._last_foveated_caps = None
                    self._last_foveated_active = False
```

- [ ] **Step 4: Wire foveated caps into the Step 4 compression loop**

In `helix_context/context_manager.py` around line 940-970, replace the existing loop with one that consults the stashed caps:

```python
        # Step 4: Dense gene expression
        # Each gene expressed as: Facts (KV pairs) + Source + Raw content
        # Dense format minimizes prose for small model extraction.
        spliced_map = {}
        answer_slate_lines = []  # MoE answer slate — flat KV pairs
        foveated_caps = getattr(self, "_last_foveated_caps", None)
        foveated_base = self.config.budget.foveated_base_chars
        for idx, g in enumerate(candidates):
            src = g.source_id or ""
            short = ""
            if src and not src.startswith("_"):
                parts = src.replace("\\", "/").split("/")
                try:
                    j = parts.index("Projects")
                    short = "/".join(parts[j + 1:])
                except ValueError:
                    short = "/".join(parts[-3:]) if len(parts) > 3 else src
            kv_attrs = ""
            if g.key_values:
                kv_pairs = " ".join(g.key_values[:5])
                kv_attrs = f' facts="{kv_pairs}"'
                for kv in g.key_values[:5]:
                    answer_slate_lines.append(kv)
            src_attr = f' src="{short}"' if short else ""
            # Foveated path overrides the uniform 1000-char target with a
            # rank-proportional cap per gene. When foveated_caps is None
            # (default / non-BROAD / disabled), preserve current behavior.
            if foveated_caps is not None:
                target = max(1, int(foveated_caps[idx] * foveated_base))
            else:
                target = 1000
            content = compress_text(
                g.content,
                target_chars=target,
                content_type=g.promoter.domains,
            )
            spliced_map[g.gene_id] = f"<GENE{src_attr}{kv_attrs}>\n{content}\n</GENE>"
```

> **Implementer note:** The only behavior change when `foveated_caps is None` is replacing the implicit `for g in candidates:` with `for idx, g in enumerate(candidates):`. The hardcoded `target_chars=1000` becomes the explicit `target = 1000` else-branch. No change to the `<GENE>` rendering, KV attrs, or `spliced_map` shape.

- [ ] **Step 5: Pass `respect_caller_order` to `_assemble`**

Around line 974, change the `_assemble` call to forward the foveated flag:

```python
        window = self._assemble(
            query, candidates, spliced_map, relation_graph,
            query_signals=(domains, entities),
            answer_slate=answer_slate_lines if use_slate else None,
            session_id=session_id,
            ignore_delivered=ignore_delivered,
            decoder_prompt_override=effective_decoder_prompt,
            respect_caller_order=getattr(self, "_last_foveated_active", False),
        )
```

- [ ] **Step 6: Stash telemetry metadata when foveated fired**

After the existing `window.metadata["budget_tokens_est"] = budget_tokens_est` line (around line 986), add:

```python
            if getattr(self, "_last_foveated_active", False):
                # Spec §8: per-call provenance for post-hoc α-curve attribution.
                # Absent when foveated_enabled=false or tier != broad.
                window.metadata["foveated_caps"] = self._last_foveated_caps
                window.metadata["foveated_alpha"] = self.config.budget.foveated_alpha
```

- [ ] **Step 7: Run tests — expect PASS**

Run: `py -3 -m pytest tests/test_foveated_splice.py -v`

Expected: all foveated tests PASS (helper unit tests from Task 2, ordering test from Task 3, tier-gating + reverse-rank end-to-end from Task 4).

- [ ] **Step 8: Commit**

```bash
git add helix_context/context_manager.py tests/test_foveated_splice.py
git commit -m "feat(context): wire foveated-splice into BROAD tier

After BROAD tier resolves, compute power-law per-gene compression caps,
reverse (candidates, caps) together, and stash on the manager. Step 4's
compress_text() consumes the stashed cap when present (else falls back
to the uniform 1000-char target). _assemble receives respect_caller_order
so reverse-rank placement reaches the prompt. Telemetry stashes
metadata['foveated_caps'] + ['foveated_alpha'] for post-hoc attribution.

Spec: docs/specs/2026-05-03-foveated-splice-design.md §4-8
Plan: docs/plans/2026-05-05-foveated-splice.md Task 4

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Regression sweep

**Files:** none modified — this is a verification task.

- [ ] **Step 1: Run the foveated test file**

Run: `py -3 -m pytest tests/test_foveated_splice.py -v`

Expected: 8+ tests pass, exactly the spec §10 cases plus helper unit tests + the `_assemble` ordering tests.

- [ ] **Step 2: Run the surrounding suites flagged by the spec**

Run: `py -3 -m pytest tests/test_abstain_tier.py tests/test_pipeline.py tests/test_context_manager_classifier.py tests/test_config.py -q`

Expected: all green. ABSTAIN tier and classifier integration unaffected because `foveated_enabled = False` is the default.

- [ ] **Step 3: Run the full project suite as a final sanity check**

Run: `py -3 -m pytest -q`

Expected: no new failures vs the pre-foveated master baseline. (Some pre-existing test files may already be skipped/marked — accept whatever was green on master.)

- [ ] **Step 4: Smoke-test foveated_enabled at runtime**

```bash
HELIX_CONFIG=/tmp/helix_foveated_smoke.toml py -3 -c "
from helix_context.config import HelixConfig
cfg = HelixConfig()
cfg.budget.foveated_enabled = True
cfg.budget.foveated_alpha = 1.0
print('foveated config OK:', cfg.budget.foveated_enabled, cfg.budget.foveated_alpha)
"
```

Expected: prints `foveated config OK: True 1.0`. (No real query — that's the bench's job; this just confirms the config wires through.)

- [ ] **Step 5: Commit (if any test fixture cleanup landed)**

If the regression sweep surfaced any fixture or test cleanups, commit them now with a message like `chore(tests): foveated regression cleanup`. Otherwise skip.

---

## Task 6: Bench-prep + PR scaffolding

**Files:**
- Create (or update): `overnight_logs/.gitkeep` if it doesn't exist (most overnight logs are gitignored — see spec §9.4 `git add -f` instruction).
- Add a PR-body template at `docs/plans/2026-05-05-foveated-splice.md` bottom? **No — the PR body is composed at PR-creation time, not in the plan.** This task is just a checklist for the human reviewer.

- [ ] **Step 1: Verify `overnight_logs/` is in `.gitignore`**

Run: `grep -n "overnight_logs" .gitignore`

Expected: matches a line. The spec instructs phase reports to be committed with `git add -f` because the directory is gitignored.

- [ ] **Step 2: Confirm Phase 0 baseline exists**

The spec §9.1 references `overnight_logs/diamond_2026-05-03_report.md` as the post-ABSTAIN baseline. Verify it's accessible from the bench machine — if not, surface to the human before attempting Phase 1.

- [ ] **Step 3: Stop here — bench is out of plan scope**

The bench plan (spec §9) is a 4-cell, ~20-28h overnight effort run on a bench machine with the appropriate fixtures. It is NOT executed as part of this implementation plan. This plan ships:

- Code: `foveated_enabled = false` default, no behavior change in production
- Tests: 8+ passing tests
- Telemetry: `foveated_caps` / `foveated_alpha` keys stashed when active

Phase 1 + 2 + the on-by-default flip are gated on §9.4 pass criteria and happen in a follow-up commit, NOT this plan.

---

## Out of Scope (Deferred — see spec §11)

- **Lagrangian-optimal log-utility schedule** — v2 if power-law underperforms.
- **Score-driven (vs rank-driven) schedule** — v2 if rank fails on flat-score corpora.
- **Foveated extending to TIGHT/FOCUSED** — v3 once BROAD validates.
- **Sandwich placement** — v3, orthogonal.
- **BROAD budget tighten** — separate spec.
- **`HELIX_FOVEATED_DISABLE` env override** — added when foveated flips on by default.

---

## Risk Register Cross-Check (spec §12)

| Spec risk | Plan mitigation |
| --- | --- |
| Wrong attribution if interventions bundled | Phase 1 (placement isolation at α=1) is bench-driven, NOT in this plan. This plan ships both code paths but `foveated_enabled = false` so production sees neither until bench picks the winner. |
| α=1 is wrong default | Default is harmonic-ish per spec; bench Phase 2 sweeps and ships winner. |
| Reverse-rank breaks classifier integration | Task 5 Step 2 runs `tests/test_context_manager_classifier.py` as a regression gate. |
| Per-gene byte-cap mid-token truncation | Task 4 uses real `compress_text()` (not a mock) so any truncation pathology surfaces in Test 8 (helper unit test) and the end-to-end tests. |
| 15-25% missed-paper risk | Out of scope for the plan; the spec §13 prior-art section is the artifact for the eventual paper draft. |
| Bench cells take longer than expected | Out of scope for the plan — Task 6 explicitly punts bench to a follow-up commit. |
| Foveated flips on by default before validation | Default is `false`; flip requires a separate config commit gated on §9.4 pass criteria. |
