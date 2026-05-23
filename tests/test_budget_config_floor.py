"""Tests for the per_gene_floor_chars config knob (H10l next lever).

H10l found correctness | gold-delivered is 76.8% at depth 8 vs 97.5% at d1
because the floor-then-greedy allocator floors *every* gene at 1000 chars and
hands surplus to top-rank genes only -- so a gold gene delivered at rank 6-8
stays at the 1000-char floor and the answer fact in its tail is truncated.

The lever is making the floor configurable so depth runs can raise it. The
allocator (helix_context/pipeline/per_gene_budget.py) already takes
``floor_chars`` as a parameter and tests pin its behavior at non-default
floors; this file pins the *config* surface so helix.toml can drive it.
"""
from __future__ import annotations

import textwrap
import tempfile
from pathlib import Path

from helix_context.config import BudgetConfig, load_config


def test_budget_config_default_floor_is_1000():
    # The default MUST stay 1000 — byte-identical to shipped behavior.
    assert BudgetConfig().per_gene_floor_chars == 1000


def test_load_config_reads_per_gene_floor_chars_from_toml():
    # The knob must round-trip through helix.toml so depth A/B runs can set it
    # without code changes.
    toml = textwrap.dedent("""
        [budget]
        per_gene_floor_chars = 3000
    """)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".toml", delete=False, encoding="utf-8",
    ) as f:
        f.write(toml)
        path = Path(f.name)
    try:
        cfg = load_config(path)
        assert cfg.budget.per_gene_floor_chars == 3000
    finally:
        path.unlink()


def test_load_config_per_gene_floor_chars_default_when_omitted():
    # Toml without the key inherits the dataclass default (1000).
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".toml", delete=False, encoding="utf-8",
    ) as f:
        f.write("[budget]\n")
        path = Path(f.name)
    try:
        cfg = load_config(path)
        assert cfg.budget.per_gene_floor_chars == 1000
    finally:
        path.unlink()
