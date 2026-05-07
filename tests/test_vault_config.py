"""Tests for VaultConfig + VaultTracesConfig parsing."""
from __future__ import annotations

import textwrap
from pathlib import Path

from helix_context.config import HelixConfig, load_config


def test_vault_defaults_when_section_absent(tmp_path: Path):
    """If helix.toml has no [vault] section, defaults apply and vault is disabled."""
    cfg_path = tmp_path / "helix.toml"
    cfg_path.write_text("")
    cfg = load_config(cfg_path)
    assert cfg.vault.enabled is False
    assert cfg.vault.path == "~/.helix/vault"
    assert cfg.vault.traces.retention_hours == 48
    assert cfg.vault.traces.enabled is True


def test_vault_section_overrides_defaults(tmp_path: Path):
    cfg_path = tmp_path / "helix.toml"
    cfg_path.write_text(textwrap.dedent("""
        [vault]
        enabled = true
        path = "/tmp/myvault"
        party_id = "party_a"
        fan_out_threshold = 1000
        redact_body = true
        stale_threshold = 0.3

        [vault.traces]
        enabled = true
        retention_hours = 12
        max_retention_hours_hard = 168
        max_count = 500
        rollup_enabled = true
        rollup_shard = "hour"
        prune_interval_minutes = 30
        trigger_only = true
    """))
    cfg = load_config(cfg_path)
    assert cfg.vault.enabled is True
    assert cfg.vault.path == "/tmp/myvault"
    assert cfg.vault.party_id == "party_a"
    assert cfg.vault.fan_out_threshold == 1000
    assert cfg.vault.redact_body is True
    assert cfg.vault.stale_threshold == 0.3
    assert cfg.vault.traces.enabled is True
    assert cfg.vault.traces.retention_hours == 12
    assert cfg.vault.traces.max_retention_hours_hard == 168
    assert cfg.vault.traces.max_count == 500
    assert cfg.vault.traces.rollup_enabled is True
    assert cfg.vault.traces.rollup_shard == "hour"
    assert cfg.vault.traces.prune_interval_minutes == 30
    assert cfg.vault.traces.trigger_only is True


def test_vault_traces_max_retention_hours_hard_can_be_null(tmp_path: Path):
    cfg_path = tmp_path / "helix.toml"
    cfg_path.write_text(textwrap.dedent("""
        [vault.traces]
        max_retention_hours_hard = 0
    """))
    cfg = load_config(cfg_path)
    # 0 disables the hard cap (treated as null/None)
    assert cfg.vault.traces.max_retention_hours_hard == 0
