"""Grain-size ladder: TIGHT / FOCUSED tier cuts measured in characters.

The legacy cuts keep 3 / 6 documents whatever their size, which starves
small-grain beds (a 580-char TIGHT packet is ~1.7k chars). With
``tight_chars`` / ``focused_chars`` > 0 the cut keeps ranked candidates
while their cumulative content fits the character cap (always at least
one). 0 keeps the legacy seat counts byte-for-byte.
"""

import pytest

from cymatix_context.config import AbstainClassFloors, BudgetConfig, load_config
from cymatix_context.pipeline.tier_logic import apply_budget_tiers
from tests.conftest import make_gene


def _pool(values, chars):
    genes = [make_gene("x" * chars, gene_id=f"char_gene_{i:010d}") for i in range(len(values))]
    return genes, dict(zip((g.gene_id for g in genes), values))


TIGHT = [24.0] + [4.0] * 29
FOCUSED = [8.0] + [3.0] * 29


@pytest.mark.parametrize("values,tier,cap,chars,expected", [
    (TIGHT, "tight", 4500, 580, 7),      # 7 * 580 = 4060 <= 4500 < 8 * 580
    (TIGHT, "tight", 4500, 1500, 3),     # the c4000-control shape: still 3
    (FOCUSED, "focused", 9000, 580, 15),
    (FOCUSED, "focused", 9000, 1500, 6),
])
def test_char_caps_scale_with_grain(values, tier, cap, chars, expected):
    genes, scores = _pool(values, chars)
    kw = {"tight_chars": cap} if tier == "tight" else {"focused_chars": cap}
    result = apply_budget_tiers(genes, scores, AbstainClassFloors(), **kw)
    assert result.budget_tier == tier
    assert result.candidates == genes[:expected]
    assert result.shadow_pool == genes[expected:]


def test_char_cap_keeps_at_least_one_oversized_candidate():
    genes, scores = _pool(TIGHT, 9000)
    result = apply_budget_tiers(genes, scores, AbstainClassFloors(), tight_chars=4500)
    assert result.candidates == genes[:1]


@pytest.mark.parametrize("values,expected", [(TIGHT, 3), (FOCUSED, 6)])
def test_zero_keeps_legacy_seat_cuts(values, expected):
    genes, scores = _pool(values, 580)
    result = apply_budget_tiers(genes, scores, AbstainClassFloors())
    assert len(result.candidates) == expected


def test_budget_config_defaults_are_legacy():
    b = BudgetConfig()
    assert (b.tier_tight_chars, b.tier_focused_chars) == (0, 0)


def test_loader_reads_char_caps(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[budget]\ntier_tight_chars = 4500\ntier_focused_chars = 9000\n", encoding="utf-8")
    b = load_config(str(p)).budget
    assert (b.tier_tight_chars, b.tier_focused_chars) == (4500, 9000)


def test_negative_char_cap_rejected():
    with pytest.raises(ValueError):
        BudgetConfig(tier_tight_chars=-1)
