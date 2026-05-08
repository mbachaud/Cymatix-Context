# Query Classifier / Injection Router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an upstream rule-based query classifier that contributes a decoder-mode hint and an assembly-stage gene-count cap to `build_context()`, without altering retrieval depth or the existing score-ratio tier.

**Architecture:** Pure-function classifier in a new module `helix_context/query_classifier.py`. `HelixContextManager.build_context()` calls it once near the top, threads the result through the existing `effective_decoder_prompt` resolution, and intersects its `assembly_max_genes_cap` into the final assembled-gene count alongside the score-ratio budget. The classifier is infallible (try/except → `default`), config-gated (`[classifier] enabled = true` in `helix.toml`), and emits per-call observability via `metadata["classifier"]`. No new HTTP endpoint.

**Tech Stack:** Python 3.11+, dataclass, `re`. Tests in `pytest` (existing harness in `tests/`). No new runtime deps.

**Spec:** [docs/specs/2026-04-29-query-classifier-injection-router-design.md](../specs/2026-04-29-query-classifier-injection-router-design.md)

---

## File Structure

| File | Role | Status |
|---|---|---|
| `helix_context/query_classifier.py` | Pure classifier module: rule tables, signal scanner, `classify_query()`, `ClassifierResult` dataclass | **Create** |
| `helix_context/config.py` | Add `ClassifierConfig` dataclass + TOML loader for `[classifier]` section | **Modify** |
| `helix_context/context_manager.py` | Wire classifier into `build_context()`; thread `decoder_mode` and `assembly_max_genes_cap` through; emit `metadata["classifier"]` | **Modify** |
| `helix.toml` | Add `[classifier]` section with `enabled = true` | **Modify** |
| `tests/test_query_classifier.py` | Pure-function unit tests for classifier logic | **Create** |
| `tests/test_context_manager_classifier.py` | Integration tests for `build_context()` wiring (override audit, no-op equivalence, failure contract) | **Create** |

---

## Conventions for this plan

- **Working directory:** `f:/Projects/helix-context`. All paths below are relative to it.
- **Run tests with:** `python -m pytest <path> -v` (Windows-native Python — *not* `uv run`, per global preferences).
- **Commit cadence:** one commit per task (after the task's tests pass). Use the project's existing message style (lowercase prefix like `feat:` / `test:` / `refactor:` — verify via `git log --oneline -5` if uncertain).
- **No emoji in code or commits.**

---

## Task 1: Scaffold the classifier module with `ClassifierResult` + `default`-only behavior

**Goal:** Land a no-op `classify_query()` that always returns `default`. This is the safety baseline — even if every later task is reverted, the system behaves exactly as today.

**Files:**
- Create: `helix_context/query_classifier.py`
- Create: `tests/test_query_classifier.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_query_classifier.py`:

```python
"""Unit tests for helix_context.query_classifier."""

from helix_context.query_classifier import (
    ClassifierResult,
    classify_query,
)


def test_default_class_for_arbitrary_query():
    result = classify_query("Hello world.")
    assert isinstance(result, ClassifierResult)
    assert result.cls == "default"
    assert result.signals_matched == []
    assert result.signal_count == 0
    assert result.assembly_max_genes_cap is None
    assert result.decoder_mode is None
    assert result.reason is None


def test_empty_query_returns_default():
    assert classify_query("").cls == "default"
    assert classify_query(None).cls == "default"  # type: ignore[arg-type]
```

- [ ] **Step 2: Run test to verify it fails**

```
python -m pytest tests/test_query_classifier.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'helix_context.query_classifier'`.

- [ ] **Step 3: Write minimal implementation**

Create `helix_context/query_classifier.py`:

```python
"""Upstream query classifier for the injection router.

Pure-function regex/keyword scan. No I/O, no model calls, infallible by
construction. See docs/specs/2026-04-29-query-classifier-injection-router-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


# Hard cap to bound work on pasted code/logs.
_QUERY_TRUNCATE_CHARS = 2000


@dataclass(frozen=True)
class ClassifierResult:
    cls: str
    signals_matched: List[str] = field(default_factory=list)
    signal_count: int = 0
    threshold_required: int = 0
    assembly_max_genes_cap: Optional[int] = None
    decoder_mode: Optional[str] = None
    reason: Optional[str] = None


_DEFAULT = ClassifierResult(cls="default")


def classify_query(query: Optional[str]) -> ClassifierResult:
    """Classify a query into one of the router classes.

    Always returns a ClassifierResult; never raises. On empty/None input
    or any internal exception, returns the `default` result (no-op).
    """
    if not query:
        return _DEFAULT
    try:
        text = query[:_QUERY_TRUNCATE_CHARS]
        # Rule scan lives in later tasks. For now, default-only.
        del text
        return _DEFAULT
    except Exception:
        return ClassifierResult(cls="default", reason="classifier_error")
```

- [ ] **Step 4: Run test to verify it passes**

```
python -m pytest tests/test_query_classifier.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/query_classifier.py tests/test_query_classifier.py
git commit -m "feat(classifier): scaffold query classifier with default-only behavior"
```

---

## Task 2: Implement the `arithmetic` class with min-signal threshold

**Goal:** First real rule. Min-signal threshold logic is exercised here; later classes inherit the same pattern.

**Files:**
- Modify: `helix_context/query_classifier.py`
- Modify: `tests/test_query_classifier.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_query_classifier.py`:

```python
# --- arithmetic ---


def test_arithmetic_two_keywords_fires():
    r = classify_query("Calculate the total cost of the migration.")
    assert r.cls == "arithmetic"
    assert r.assembly_max_genes_cap == 2
    assert r.decoder_mode == "minimal"
    assert r.signal_count >= 2
    assert r.threshold_required == 2


def test_arithmetic_operator_plus_numeric_keyword_fires():
    # 1 strong: operator (`+`) + numeric keyword ("total") → fires on
    # the strong-pair shortcut even though signal_count would otherwise be 2.
    r = classify_query("What is 5 + the total?")
    assert r.cls == "arithmetic"


def test_arithmetic_single_weak_signal_falls_through():
    # Single stray `%` in an otherwise factual query must NOT fire.
    r = classify_query("What is the cache hit rate at 95%?")
    assert r.cls != "arithmetic"


def test_arithmetic_critical_path_phrase_counts_as_one_keyword():
    # "critical path" is a single multi-word keyword. Alone it should NOT fire.
    r = classify_query("Tell me about the critical path.")
    assert r.cls != "arithmetic"


def test_arithmetic_critical_path_plus_calculate_fires():
    r = classify_query("Calculate the critical path.")
    assert r.cls == "arithmetic"
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_query_classifier.py -v
```

Expected: 5 new tests fail (currently classifier returns `default` for everything); the 2 existing tests still pass.

- [ ] **Step 3: Implement the `arithmetic` rule**

Edit `helix_context/query_classifier.py`. Replace the body of `classify_query()` and add helpers:

```python
import re

# Operator characters that count as one signal per *distinct* operator class
# present (de-duplicated so a math expression with three `+` is still 1 signal).
_OPERATOR_CHARS = ("+", "-", "*", "/", "%")

# Single-word numeric/quantity keywords (lowercase, word-boundary matched).
_NUMERIC_KEYWORDS = ("calculate", "total", "sum")

# Multi-word keywords (lowercase substring match).
_MULTIWORD_KEYWORDS = ("critical path",)


def _scan_arithmetic(lower: str) -> List[str]:
    """Return the list of arithmetic signal tags matched in `lower`."""
    signals: List[str] = []
    for op in _OPERATOR_CHARS:
        if op in lower:
            signals.append(f"operator:{op}")
    for kw in _NUMERIC_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", lower):
            signals.append(f"keyword:{kw}")
    for kw in _MULTIWORD_KEYWORDS:
        if kw in lower:
            signals.append(f"keyword:{kw}")
    return signals


def _arithmetic_meets_threshold(signals: List[str]) -> bool:
    """Arithmetic fires if:
    - signal_count >= 2, OR
    - exactly 1 operator signal AND >= 1 numeric/quantity keyword signal
      (the strong-pair shortcut).
    """
    if len(signals) >= 2:
        return True
    has_op = any(s.startswith("operator:") for s in signals)
    has_kw = any(s.startswith("keyword:") for s in signals)
    return has_op and has_kw
```

Then update `classify_query()`:

```python
def classify_query(query: Optional[str]) -> ClassifierResult:
    if not query:
        return _DEFAULT
    try:
        text = query[:_QUERY_TRUNCATE_CHARS]
        lower = text.lower()

        # Priority 1: arithmetic
        arith_signals = _scan_arithmetic(lower)
        if _arithmetic_meets_threshold(arith_signals):
            return ClassifierResult(
                cls="arithmetic",
                signals_matched=arith_signals,
                signal_count=len(arith_signals),
                threshold_required=2,
                assembly_max_genes_cap=2,
                decoder_mode="minimal",
            )

        return _DEFAULT
    except Exception:
        return ClassifierResult(cls="default", reason="classifier_error")
```

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest tests/test_query_classifier.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/query_classifier.py tests/test_query_classifier.py
git commit -m "feat(classifier): add arithmetic class with min-signal threshold"
```

---

## Task 3: Implement the `factual` class with strict length AND-guard

**Goal:** Add factual classification. The length guard must be **AND**, not a short-circuit on wh-word match — this is the failure mode called out in the spec.

**Files:**
- Modify: `helix_context/query_classifier.py`
- Modify: `tests/test_query_classifier.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
# --- factual ---


def test_factual_short_wh_query_fires():
    r = classify_query("What port does helix use?")
    assert r.cls == "factual"
    assert r.assembly_max_genes_cap == 5
    assert r.decoder_mode == "condensed"


def test_factual_long_wh_query_does_not_fire():
    # 16 words — over the < 15 word threshold; must fall through.
    long_q = (
        "What is the precise mechanism by which the helix promoter index "
        "interacts with the synonym map and the co-activation graph during retrieval?"
    )
    assert len(long_q.split()) >= 16
    r = classify_query(long_q)
    assert r.cls != "factual"


def test_factual_at_14_words_fires():
    q = "What does the helix promoter index do during retrieval for very small queries?"
    assert len(q.split()) == 14
    r = classify_query(q)
    assert r.cls == "factual"


def test_factual_no_wh_word_does_not_fire():
    # Short, but no leading wh-word.
    r = classify_query("Helix port number.")
    assert r.cls != "factual"
```

- [ ] **Step 2: Run tests — expect failures**

```
python -m pytest tests/test_query_classifier.py -v
```

Expected: 4 new tests fail.

- [ ] **Step 3: Implement the `factual` rule**

Add to `helix_context/query_classifier.py`:

```python
_WH_WORDS = ("who", "what", "where", "when", "which")
_FACTUAL_MAX_WORDS = 15  # strict less-than


def _is_factual(lower: str) -> Optional[List[str]]:
    """Return signal list if factual; None otherwise.

    Length check is **AND** — both leading wh-word AND `< 15` words required.
    """
    stripped = lower.lstrip()
    leading = stripped.split(maxsplit=1)
    if not leading:
        return None
    first = leading[0].rstrip("?.,!:;")
    if first not in _WH_WORDS:
        return None
    word_count = len(stripped.split())
    if word_count >= _FACTUAL_MAX_WORDS:
        return None
    return [f"wh:{first}", f"length:{word_count}"]
```

Insert into `classify_query()` after the arithmetic block, before the final `return _DEFAULT`:

```python
        # Priority 2: factual
        fact_signals = _is_factual(lower)
        if fact_signals is not None:
            return ClassifierResult(
                cls="factual",
                signals_matched=fact_signals,
                signal_count=len(fact_signals),
                threshold_required=1,
                assembly_max_genes_cap=5,
                decoder_mode="condensed",
            )
```

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest tests/test_query_classifier.py -v
```

Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/query_classifier.py tests/test_query_classifier.py
git commit -m "feat(classifier): add factual class with strict length AND-guard"
```

---

## Task 4: Implement `procedural` and `multi_hop` classes

**Goal:** Add the two remaining real classes. Their relative ordering is **provisional** per the spec — code comment must flag this.

**Files:**
- Modify: `helix_context/query_classifier.py`
- Modify: `tests/test_query_classifier.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
# --- procedural ---


def test_procedural_how_to_fires():
    r = classify_query("How do I configure the ribosome timeout?")
    assert r.cls == "procedural"
    assert r.assembly_max_genes_cap == 6
    assert r.decoder_mode == "full"


def test_procedural_steps_keyword_fires():
    r = classify_query("Walk me through the ingest steps.")
    assert r.cls == "procedural"


# --- multi_hop ---


def test_multi_hop_connective_fires():
    r = classify_query("Compare the cold tier and the hot tier.")
    assert r.cls == "multi_hop"
    assert r.assembly_max_genes_cap == 8
    assert r.decoder_mode == "full"


def test_multi_hop_long_query_fires():
    # > 25 words, no other markers — length alone qualifies.
    q = " ".join(["token"] * 26)
    r = classify_query(q)
    assert r.cls == "multi_hop"


def test_multi_hop_and_then_connective():
    r = classify_query("Run ingest and then verify the gene count.")
    assert r.cls == "multi_hop"
```

- [ ] **Step 2: Run tests — expect failures**

```
python -m pytest tests/test_query_classifier.py -v
```

- [ ] **Step 3: Implement the rules**

Add to `helix_context/query_classifier.py`:

```python
_PROCEDURAL_PATTERNS = ("how do i", "how to", "steps", "walk me through")

_MULTIHOP_CONNECTIVES = (
    "and then", "because", "after that", "compare", " vs ",
)
_MULTIHOP_BETWEEN_RE = re.compile(r"\bbetween\s+\S+\s+and\s+\S+", re.IGNORECASE)
_MULTIHOP_LONG_WORDS = 25  # strict greater-than


def _scan_procedural(lower: str) -> List[str]:
    return [f"keyword:{p}" for p in _PROCEDURAL_PATTERNS if p in lower]


def _scan_multi_hop(lower: str, raw: str) -> List[str]:
    signals: List[str] = []
    for c in _MULTIHOP_CONNECTIVES:
        if c in lower:
            signals.append(f"connective:{c.strip()}")
    if _MULTIHOP_BETWEEN_RE.search(raw):
        signals.append("connective:between-x-and-y")
    if len(raw.split()) > _MULTIHOP_LONG_WORDS:
        signals.append(f"length:{len(raw.split())}")
    return signals
```

Insert into `classify_query()` between factual and the default fallback. **Add the provisional-ordering comment**:

```python
        # Priority 3 vs 4: procedural before multi_hop is PROVISIONAL.
        # The ordering between these two is asserted, not benchmarked.
        # See spec §3 — revisit after first procedural benchmark run.
        proc_signals = _scan_procedural(lower)
        if proc_signals:
            return ClassifierResult(
                cls="procedural",
                signals_matched=proc_signals,
                signal_count=len(proc_signals),
                threshold_required=1,
                assembly_max_genes_cap=6,
                decoder_mode="full",
            )

        mh_signals = _scan_multi_hop(lower, text)
        if mh_signals:
            return ClassifierResult(
                cls="multi_hop",
                signals_matched=mh_signals,
                signal_count=len(mh_signals),
                threshold_required=1,
                assembly_max_genes_cap=8,
                decoder_mode="full",
            )
```

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest tests/test_query_classifier.py -v
```

Expected: 16 passed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/query_classifier.py tests/test_query_classifier.py
git commit -m "feat(classifier): add procedural and multi_hop classes"
```

---

## Task 5: Priority + negative-priority + code-paste robustness tests

**Goal:** Lock down the cross-class invariants the spec explicitly calls out. No production-code change should be needed if Tasks 2-4 were correct.

**Files:**
- Modify: `tests/test_query_classifier.py`

- [ ] **Step 1: Write the tests**

Append:

```python
# --- priority and robustness ---


def test_priority_arithmetic_beats_multi_hop():
    r = classify_query("Calculate the critical path and then explain why.")
    assert r.cls == "arithmetic"


def test_negative_priority_long_factual_with_stray_percent_stays_default():
    # Long factual question with a single `%` — must NOT become arithmetic.
    # No wh-word at front, > 15 words, single weak signal → default OR multi_hop
    # (length-based) but never arithmetic.
    q = (
        "The dashboard reports the cache hit rate at 95% and the team is "
        "discussing whether the synonym map is contributing to that figure."
    )
    r = classify_query(q)
    assert r.cls != "arithmetic"


def test_code_paste_does_not_trigger_arithmetic_from_paste_alone():
    # A pasted code block carries operators but only one numeric keyword would
    # qualify it. Without the keyword, the operator soup must not fire arithmetic.
    paste = """
    def f(x):
        return x + 1 - 2 * 3 / 4 % 5
    """
    r = classify_query(f"Explain this:\n{paste}")
    assert r.cls != "arithmetic"


def test_truncation_bounds_classifier_work():
    # A 10k-char query shouldn't blow up; classification must complete.
    huge = "what " + ("x " * 5000)
    r = classify_query(huge)
    assert r.cls in {"factual", "default", "multi_hop"}


def test_classifier_never_raises_on_pathological_input():
    # Surrogate pairs, control chars, etc. Must not raise.
    weird = "\x00\x01\x02 what \udcff is this?"
    classify_query(weird)  # no assertion needed — just no exception
```

- [ ] **Step 2: Run tests**

```
python -m pytest tests/test_query_classifier.py -v
```

Expected: 21 passed.

- [ ] **Step 3 (only if any test fails): adjust classifier**

If `test_priority_arithmetic_beats_multi_hop` fails, the priority order in `classify_query()` is wrong. If `test_negative_priority_...` fails, revisit `_arithmetic_meets_threshold`. Do NOT relax tests — fix the code.

- [ ] **Step 4: Commit**

```bash
git add tests/test_query_classifier.py
git commit -m "test(classifier): priority, negative-priority, code-paste robustness"
```

---

## Task 6: Add `[classifier]` config section + `ClassifierConfig` dataclass

**Goal:** Add the on/off flag (`enabled = true` default) and the loader so the integration in Task 7 can read it.

**Files:**
- Modify: `helix_context/config.py`
- Modify: `helix.toml`
- Create test inline in: `tests/test_query_classifier.py` (one test, kept with the classifier test module)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_query_classifier.py`:

```python
def test_classifier_config_defaults_to_enabled(tmp_path):
    from helix_context.config import load_config
    cfg_path = tmp_path / "helix.toml"
    cfg_path.write_text("[classifier]\nenabled = true\n", encoding="utf-8")
    cfg = load_config(str(cfg_path))
    assert cfg.classifier.enabled is True


def test_classifier_config_can_be_disabled(tmp_path):
    from helix_context.config import load_config
    cfg_path = tmp_path / "helix.toml"
    cfg_path.write_text("[classifier]\nenabled = false\n", encoding="utf-8")
    cfg = load_config(str(cfg_path))
    assert cfg.classifier.enabled is False


def test_classifier_config_default_when_section_absent(tmp_path):
    from helix_context.config import load_config
    cfg_path = tmp_path / "helix.toml"
    cfg_path.write_text("", encoding="utf-8")
    cfg = load_config(str(cfg_path))
    assert cfg.classifier.enabled is True
```

- [ ] **Step 2: Run — expect failures**

```
python -m pytest tests/test_query_classifier.py -v -k classifier_config
```

Expected: 3 fail with `AttributeError: 'HelixConfig' object has no attribute 'classifier'`.

- [ ] **Step 3: Add the dataclass and loader branch**

In `helix_context/config.py`, define the dataclass near the other config dataclasses (e.g. just below `RetrievalConfig` for proximity to similar feature toggles):

```python
@dataclass
class ClassifierConfig:
    enabled: bool = True
```

Add `classifier` to `HelixConfig`:

```python
@dataclass
class HelixConfig:
    ribosome: RibosomeConfig = field(default_factory=RibosomeConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    # ... existing fields unchanged ...
    headroom: HeadroomConfig = field(default_factory=HeadroomConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    synonym_map: Dict[str, List[str]] = field(default_factory=dict)
```

Add a loader branch in `load_config()` near the other `if "<section>" in raw:` blocks (e.g. just before the `synonym_map` block):

```python
    if "classifier" in raw:
        cls_section = raw["classifier"]
        _warn_unknown("classifier", cls_section, ClassifierConfig)
        cfg.classifier = ClassifierConfig(
            enabled=bool(cls_section.get("enabled", cfg.classifier.enabled)),
        )
```

In `helix.toml`, add a new section near `[retrieval]`:

```toml
[classifier]
# Upstream rule-based query classifier / injection router.
# When enabled, contributes a decoder-mode hint and an assembly-stage
# gene-count cap to build_context(). See
# docs/specs/2026-04-29-query-classifier-injection-router-design.md.
enabled = true
```

- [ ] **Step 4: Run all classifier tests**

```
python -m pytest tests/test_query_classifier.py -v
```

Expected: all passed (24 total).

- [ ] **Step 5: Commit**

```bash
git add helix_context/config.py helix.toml tests/test_query_classifier.py
git commit -m "feat(config): add [classifier] section with enabled flag"
```

---

## Task 7: Wire the classifier into `build_context()`

**Goal:** The integration step. Order of operations is fixed by the spec — classifier always runs (cheap, no I/O), `decoder_override` wins for decoder selection, and `assembly_max_genes_cap` is intersected with the score-ratio budget after retrieval.

**Files:**
- Modify: `helix_context/context_manager.py`
- Create: `tests/test_context_manager_classifier.py`

### Background — context inside `build_context()`

The spec's order of operations corresponds to these regions in [helix_context/context_manager.py](../../helix_context/context_manager.py):

- **Decoder resolution** — around line 628 (`if decoder_override and decoder_override in DECODER_MODES:`).
- **`max_genes` initial value** — around line 637 (`max_genes = self.config.budget.max_genes_per_turn`).
- **Score-ratio tier (TIGHT/FOCUSED/BROAD)** — lines ~711-761; this assigns the final `candidates` list. The classifier cap intersects **here**, after the score-ratio tier has already chosen its preferred top-N.
- **Metadata annotation** — `window.metadata["budget_tier"] = ...` around line 884 is the right neighborhood for the new `metadata["classifier"]` payload.

### Steps

- [ ] **Step 1: Write integration tests (skeleton — flesh out fixtures based on existing patterns)**

Inspect existing tests for a reusable manager fixture:

```
python -m pytest --collect-only tests/test_cwola_window.py 2>/dev/null | head -20
```

Look at `tests/conftest.py` and `tests/test_context_packet.py` (or any test that already builds a `HelixContextManager`) for the fixture pattern. Reuse it.

Create `tests/test_context_manager_classifier.py`:

```python
"""Integration tests: classifier wiring inside HelixContextManager.build_context()."""

import pytest

from helix_context.config import load_config
from helix_context.context_manager import HelixContextManager


@pytest.fixture
def manager(tmp_path):
    """Manager with an empty in-memory genome — no live ribosome required."""
    cfg = load_config()  # picks up project helix.toml defaults
    cfg.genome.path = str(tmp_path / "genome.db")
    # Force the classifier ON for these tests (overrides repo helix.toml if needed).
    cfg.classifier.enabled = True
    mgr = HelixContextManager(cfg)
    yield mgr
    mgr.close()


def test_arithmetic_query_emits_classifier_metadata(manager):
    win = manager.build_context("Calculate the total cost of migration.")
    meta = win.metadata.get("classifier")
    assert meta is not None
    assert meta["class"] == "arithmetic"
    assert meta["assembly_max_genes_cap"] == 2
    assert meta["decoder_selected"] == "minimal"
    assert meta["override_applied"] is False
    assert "candidate_pool_size" in meta
    assert "max_genes_effective" in meta


def test_decoder_override_wins_but_classifier_still_logged(manager):
    win = manager.build_context(
        "Calculate the total cost of migration.",
        decoder_override="full",
    )
    meta = win.metadata["classifier"]
    assert meta["class"] == "arithmetic"        # classifier still ran
    assert meta["override_applied"] is True
    # The decoder we *actually* used was the override, not the classifier's pick.
    # The classifier reports what *it* would have picked:
    assert meta["decoder_selected"] == "minimal"


def test_default_class_is_no_op_for_max_genes(manager):
    """A `default`-classified query must produce identical max_genes_effective
    to a baseline run with the classifier disabled."""
    q = "Hello there."  # falls to default

    # Baseline: classifier disabled
    manager.config.classifier.enabled = False
    win_off = manager.build_context(q)

    # Classifier on
    manager.config.classifier.enabled = True
    win_on = manager.build_context(q)

    # Same final assembled-gene count.
    assert win_off.metadata.get("genes_expressed") == win_on.metadata.get("genes_expressed")


def test_classifier_disabled_skips_metadata(manager):
    manager.config.classifier.enabled = False
    win = manager.build_context("Calculate the total cost.")
    assert "classifier" not in (win.metadata or {})


def test_classifier_failure_falls_back_to_default(manager, monkeypatch):
    """If the classifier raises, build_context() must still succeed and
    metadata must reflect the failure."""
    from helix_context import query_classifier

    def boom(_q):
        raise RuntimeError("synthetic")

    # Patch the symbol used inside context_manager (import-site bind):
    monkeypatch.setattr(
        "helix_context.context_manager.classify_query",
        lambda q: query_classifier.ClassifierResult(
            cls="default", reason="classifier_error",
        ),
    )
    win = manager.build_context("Calculate the total.")
    meta = win.metadata.get("classifier")
    assert meta is not None
    assert meta["class"] == "default"
```

- [ ] **Step 2: Run tests — expect failures**

```
python -m pytest tests/test_context_manager_classifier.py -v
```

Expected: 5 fail (`metadata["classifier"]` doesn't exist yet).

- [ ] **Step 3: Wire the classifier into `build_context()`**

Edit `helix_context/context_manager.py`:

**3a. Add the import near the other `from .` imports at the top of the file:**

```python
from .query_classifier import ClassifierResult, classify_query
```

**3b. Inside `build_context()`, immediately after the `_maybe_compact()` call (~line 624) and before the existing `decoder_override` resolution:**

```python
        # Step 0a: Upstream query classifier / injection router.
        # Always runs (cheap, no I/O) — even when decoder_override is set,
        # so the audit trail records what the classifier *would* have picked.
        # See docs/specs/2026-04-29-query-classifier-injection-router-design.md.
        classifier_enabled = getattr(
            getattr(self.config, "classifier", None), "enabled", True,
        )
        classifier_result: Optional[ClassifierResult] = None
        if classifier_enabled:
            classifier_result = classify_query(query)
```

**3c. Replace the existing decoder-override block (~lines 628-631):**

```python
        # Decoder selection: explicit caller override > classifier hint > default.
        if decoder_override and decoder_override in DECODER_MODES:
            effective_decoder_prompt = DECODER_MODES[decoder_override]
            override_applied = True
        elif (
            classifier_result is not None
            and classifier_result.decoder_mode
            and classifier_result.decoder_mode in DECODER_MODES
        ):
            effective_decoder_prompt = DECODER_MODES[classifier_result.decoder_mode]
            override_applied = False
        else:
            effective_decoder_prompt = self._decoder_prompt
            override_applied = False
```

**3d. After the score-ratio tier finishes (i.e. after the `if scores and any(scores.values()):` block ending around line 824, but before `# Step 3.5: NLI classification`), intersect the classifier cap into the final `candidates` list:**

```python
        # Step 3.6: Apply classifier assembly cap.
        # Invariant: classifier can only LOWER the assembled gene count.
        # It cannot raise it, and it cannot reduce retrieval depth — the
        # score-ratio tier above already saw the full candidate set.
        candidate_pool_size = len(candidates)
        if (
            classifier_result is not None
            and classifier_result.assembly_max_genes_cap is not None
            and len(candidates) > classifier_result.assembly_max_genes_cap
        ):
            log.debug(
                "Classifier cap: assembled %d -> %d (class=%s)",
                len(candidates),
                classifier_result.assembly_max_genes_cap,
                classifier_result.cls,
            )
            candidates = candidates[: classifier_result.assembly_max_genes_cap]
```

> **Note on `candidate_pool_size`:** capture the count *before* applying the cap. This is what the spec means by "distinguishes retrieved-N from assembled-M" in metadata.

**3e. After the existing `window.metadata["budget_tier"]` lines (~line 884), emit the classifier payload:**

```python
        # Classifier observability payload (spec §5.2).
        if classifier_result is not None and window.metadata is not None:
            window.metadata["classifier"] = {
                "class": classifier_result.cls,
                "signals_matched": list(classifier_result.signals_matched),
                "signal_count": classifier_result.signal_count,
                "threshold_required": classifier_result.threshold_required,
                "assembly_max_genes_cap": classifier_result.assembly_max_genes_cap,
                "max_genes_effective": len(candidates),
                "decoder_selected": classifier_result.decoder_mode,
                "override_applied": override_applied,
                "candidate_pool_size": candidate_pool_size,
            }
            if classifier_result.reason:
                window.metadata["classifier"]["reason"] = classifier_result.reason
```

- [ ] **Step 4: Run integration tests + full classifier suite**

```
python -m pytest tests/test_query_classifier.py tests/test_context_manager_classifier.py -v
```

Expected: all passed.

If any test in `tests/test_context_manager_classifier.py` fails because the manager fixture can't construct without a live ribosome, refer to `tests/conftest.py` for the existing pattern (likely uses `DisabledBackend` or a config that sets the ribosome to disabled). Adjust fixture accordingly — do **not** relax the assertions.

- [ ] **Step 5: Run the broader test suite to confirm no regressions**

```
python -m pytest tests/ -m "not live" -v
```

Expected: same pass count as before this plan, plus the new tests.

- [ ] **Step 6: Commit**

```bash
git add helix_context/context_manager.py tests/test_context_manager_classifier.py
git commit -m "feat(context-manager): wire query classifier into build_context"
```

---

## Task 8: README / CLAUDE.md mention (optional but recommended)

**Goal:** A two-line mention so the next reader knows the classifier exists.

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add a row under the "Structure" table or a one-line note in the pipeline description**

In `CLAUDE.md`, the "How It Works" pipeline section currently lists 6 steps. Add a brief 0a step:

```markdown
**6-step pipeline per turn:**
0a. **Classify** — rule-based query classifier picks decoder mode + assembly cap (no model call)
1. **Extract** — heuristic keyword extraction from query (no model call)
...
```

And add a row to the Structure table:

```markdown
| `query_classifier.py` | Upstream rule-based router: classify_query() → decoder mode + assembly cap |
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: mention query classifier in pipeline overview"
```

---

## Verification checklist (before marking complete)

- [ ] `python -m pytest tests/test_query_classifier.py -v` — all pass
- [ ] `python -m pytest tests/test_context_manager_classifier.py -v` — all pass
- [ ] `python -m pytest tests/ -m "not live" -v` — full mock suite passes (no regressions)
- [ ] Manual smoke: start the server, hit `/context` with `{"query": "Calculate the total cost"}`, confirm response includes `metadata.classifier.class == "arithmetic"`
- [ ] Spec invariants verified in code:
  - Classifier runs even when `decoder_override` is set (audit trail)
  - Classifier cap is applied **after** the score-ratio tier
  - `candidate_pool_size` captures retrieval depth, `max_genes_effective` captures assembled depth
  - `default` class is a true no-op
  - Classifier failure falls back to `default` and the request still succeeds

---

## Out of scope (deferred to future plans)

- Aggregate Prometheus counters (`helix_classifier_class_total`, `helix_classifier_override_total`) — spec §8 marks these as v1.1.
- "How many" / "how long" + numeric conjunction rule — backlog item per spec §3.1.
- Procedural vs multi_hop ordering revisit — pending procedural benchmark data.
- Embedding-NN fallback for high `default`-rate buckets — separate spec.
