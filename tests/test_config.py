def test_upstream_timeout_default_is_180s():
    """Regression test for the 2026-05-02 default bump.

    Helix's default of 120s was observed to silently return Proxy 500s
    on slow gemma4:e4b GPQA queries at ~125s (full breakdown in the
    2026-05-01 overnight report). 180s is the shipping default; 120s
    is a regression.
    """
    from helix_context.config import HelixConfig
    cfg = HelixConfig()
    assert cfg.server.upstream_timeout == 180.0


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
