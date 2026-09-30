"""The three measurement arms must be explicit and default-inert."""
import pytest

from cymatix_context.config import BudgetConfig, CymatixConfig, load_config


def test_migration_defaults_preserve_released_behavior():
    cfg = CymatixConfig()
    assert cfg.budget.wire_format == "legacy"
    assert cfg.budget.tier_seat_floor_enabled is False
    assert cfg.retrieval.harmonic_batching_enabled is False


def test_migration_arms_load_from_toml(tmp_path):
    path = tmp_path / "arms.toml"
    path.write_text('[budget]\nwire_format="canonical"\ntier_seat_floor_enabled=true\n'
                    '[retrieval]\nharmonic_batching_enabled=true\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg.budget.wire_format == "canonical"
    assert cfg.budget.tier_seat_floor_enabled is True
    assert cfg.retrieval.harmonic_batching_enabled is True


@pytest.mark.parametrize("value", ["v2", "", None, 2])
def test_unknown_wire_format_rejected(value):
    with pytest.raises(ValueError, match="wire_format"):
        BudgetConfig(wire_format=value)
