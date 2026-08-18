"""Issue #357: [cymatics] peak_width is an explicit override, derived otherwise.

Before the fix the key was parsed and never read on the scoring path — the
manager always derived its width from [budget] splice_aggressiveness, so the
shipped ``peak_width = 3.0`` was dead config and #354's arm C (which set it
to 0.01) ran byte-identical to the shipped arm. Now: unset (None, the new
default) keeps the derived behaviour byte-identical; an explicit value is
genuinely honoured.
"""

import pytest

from cymatix_context.config import (
    BudgetConfig,
    ClassifierConfig,
    CymaticsConfig,
    CymatixConfig,
    GenomeConfig,
    RibosomeConfig,
    load_config,
)
from cymatix_context.context_manager import CymatixContextManager
from cymatix_context.scoring.cymatics import aggressiveness_to_peak_width


def _manager(**cymatics_kwargs):
    cfg = CymatixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(max_genes_per_turn=12),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        classifier=ClassifierConfig(enabled=False),
        cymatics=CymaticsConfig(**cymatics_kwargs),
    )
    return CymatixContextManager(cfg)


def test_default_none_derives_from_splice_aggressiveness():
    mgr = _manager()
    try:
        expected = aggressiveness_to_peak_width(
            mgr.config.budget.splice_aggressiveness
        )
        assert mgr._cymatics_peak_width == pytest.approx(expected)
        # Shipped splice_aggressiveness = 0.3 -> 1.55, the width the #357
        # instrumentation measured at the real read sites.
        assert mgr._cymatics_peak_width == pytest.approx(1.55)
    finally:
        mgr.close()


def test_explicit_peak_width_overrides_derivation():
    mgr = _manager(peak_width=0.9)
    try:
        assert mgr._cymatics_peak_width == pytest.approx(0.9)
    finally:
        mgr.close()


def test_toml_key_absent_parses_to_none(tmp_path):
    p = tmp_path / "cymatix.toml"
    p.write_text("[cymatics]\nenabled = true\n", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.cymatics.peak_width is None


def test_toml_explicit_value_is_honoured(tmp_path):
    p = tmp_path / "cymatix.toml"
    p.write_text("[cymatics]\npeak_width = 0.9\n", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.cymatics.peak_width == pytest.approx(0.9)
