"""Default-honesty regression — code defaults vs the shipped helix.toml.

2026-06-12 council audit (Option B slice 1): ``HelixConfig()`` dataclass
defaults had silently drifted from the shipped helix.toml on 20 fields —
the audit's headline five (expression_tokens 6000 vs 7000,
max_genes_per_turn 8 vs 12, splice_aggressiveness 0.5 vs 0.3,
decoder_mode "full" vs "condensed", sr_enabled false vs true) plus 15
more found by this module's comparator (ribosome model/timeout/warmup/
backend/query_expansion, session_delivery, genome.path, ingestion
backend/splade/rerank_model/entity_graph, filename_anchor,
bm25_shortlist, plr.enabled, headroom.enabled).

Resolution policy: the shipped helix.toml is the operationally-tested
product (every bench this month ran those values), so CODE defaults were
aligned to the TOML — except measured-zero features, which align the
other way: sr_enabled was flipped to false in the TOML because the
evidence roadmap measured SR at zero retrieval effect.

This module is the ratchet: any NEW divergence between the shipped toml
and the dataclass defaults fails CI unless it is added to
``INTENTIONAL_DIVERGENCES`` with a documented reason.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from helix_context.config import HelixConfig, KnowConfig, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_TOML = REPO_ROOT / "helix.toml"

# Fields where the shipped helix.toml and the code defaults are ALLOWED to
# differ. Keep this empty or tiny — every entry needs a reason.
INTENTIONAL_DIVERGENCES: dict[str, str] = {
    # The shipped toml carries a starter synonym vocabulary for the repo's
    # own docs corpus. It is data payload, not a behavioral default — the
    # dataclass default must stay {} so embedded/library callers start with
    # a neutral expansion map instead of another project's vocabulary.
    "synonym_map": "starter vocabulary data, not a behavioral default",
}

# Env vars load_config consults; the comparator must neutralize them so a
# developer shell (HELIX_GENOME_PATH etc.) can't fake or mask a drift.
_CONFIG_ENV_OVERRIDES = (
    "HELIX_CONFIG",
    "HELIX_GENOME_PATH",
    "HELIX_BENCH_ENABLED",
    "HELIX_SERVER_UPSTREAM",
    "HELIX_SERVER_UPSTREAM_TIMEOUT",
)


def _walk_drift(toml_obj, code_obj, prefix: str = "") -> dict[str, tuple]:
    """Recursively diff two config dataclasses; returns {field: (code, toml)}."""
    drifts: dict[str, tuple] = {}
    if dataclasses.is_dataclass(toml_obj):
        for f in dataclasses.fields(toml_obj):
            path = f"{prefix}.{f.name}" if prefix else f.name
            drifts.update(
                _walk_drift(getattr(toml_obj, f.name), getattr(code_obj, f.name), path)
            )
    elif toml_obj != code_obj:
        drifts[prefix] = (code_obj, toml_obj)
    return drifts


def _shipped_vs_code_drift(monkeypatch) -> dict[str, tuple]:
    for var in _CONFIG_ENV_OVERRIDES:
        monkeypatch.delenv(var, raising=False)
    toml_cfg = load_config(str(SHIPPED_TOML))
    return _walk_drift(toml_cfg, HelixConfig())


def test_shipped_toml_matches_code_defaults(monkeypatch):
    """The honesty ratchet: shipped helix.toml == HelixConfig() defaults.

    Allowed exceptions live in INTENTIONAL_DIVERGENCES, each with a
    documented reason. A failure here means someone changed a default on
    ONE side only — either align the other side or (rarely) document the
    divergence in the allowlist.
    """
    assert SHIPPED_TOML.exists(), "shipped helix.toml missing from repo root"
    drift = _shipped_vs_code_drift(monkeypatch)

    undocumented = {k: v for k, v in drift.items() if k not in INTENTIONAL_DIVERGENCES}
    assert not undocumented, (
        "helix.toml and HelixConfig() defaults drifted on undocumented "
        "fields (code, toml): "
        + "; ".join(f"{k}={v}" for k, v in sorted(undocumented.items()))
        + " — align the lagging side (toml wins unless the feature measured "
        "zero) or document the divergence in INTENTIONAL_DIVERGENCES."
    )

    # Keep the allowlist honest in the other direction too: an entry whose
    # field no longer drifts is stale and must be removed.
    stale = set(INTENTIONAL_DIVERGENCES) - set(drift)
    assert not stale, f"stale INTENTIONAL_DIVERGENCES entries (no longer drift): {sorted(stale)}"


def test_sr_enabled_defaults_false_on_both_sides(monkeypatch):
    """sr_enabled is the measured-zero inverse case: BOTH sides false.

    The evidence roadmap measured SR (Tier 5.5 successor representation)
    at zero retrieval effect, so the 2026-04-22 toml flip to true was
    reverted in the default-honesty pass rather than propagated into code.
    """
    for var in _CONFIG_ENV_OVERRIDES:
        monkeypatch.delenv(var, raising=False)
    assert HelixConfig().retrieval.sr_enabled is False
    assert load_config(str(SHIPPED_TOML)).retrieval.sr_enabled is False


# ── [know] — folded from the know_calibration shadow loader ─────────────


def test_know_config_defaults_match_know_calibration_constants():
    """config.KnowConfig literals must equal the spec constants in
    scoring/know_calibration.py — the two modules carry the same ship-time
    defaults on purpose (config.py stays import-light, so the values are
    duplicated and this test is the lock between them)."""
    from helix_context.scoring import know_calibration as kc

    k = KnowConfig()
    assert tuple(k.betas) == kc.DEFAULT_BETAS
    assert k.s_ref == kc.DEFAULT_S_REF
    assert k.g_ref == kc.DEFAULT_G_REF
    assert k.emit_floor == kc.DEFAULT_EMIT_FLOOR
    assert k.stale_after_days == kc.DEFAULT_STALE_AFTER_DAYS
    assert len(k.betas) == 1 + kc.N_FEATURES


def test_know_section_loads_through_config(tmp_path):
    """[know] is a first-class config section now (not a shadow loader)."""
    toml = tmp_path / "helix.toml"
    toml.write_text(
        "[know]\n"
        "emit_floor = 0.7\n"
        "s_ref = 2.0\n"
        "g_ref = 0.25\n"
        "betas = [-1.0, 1.0, 1.0, 0.5, 1.0, 0.5]\n"
        "calibrated_at = \"2026-06-01T00:00:00Z\"\n"
        "calibrated_on_n = 800\n"
        "stale_after_days = 7\n",
        encoding="utf-8",
    )
    cfg = load_config(str(toml))
    assert cfg.know.emit_floor == 0.7
    assert cfg.know.s_ref == 2.0
    assert cfg.know.g_ref == 0.25
    assert cfg.know.betas == [-1.0, 1.0, 1.0, 0.5, 1.0, 0.5]
    assert cfg.know.calibrated_at == "2026-06-01T00:00:00Z"
    assert cfg.know.calibrated_on_n == 800
    assert cfg.know.stale_after_days == 7


def test_know_section_malformed_betas_soft_fail(tmp_path, caplog):
    """Bad calibration writes must never break startup (shadow-loader
    contract, kept verbatim in the config loader)."""
    import logging

    toml = tmp_path / "helix.toml"
    toml.write_text('[know]\nbetas = ["nope"]\nstale_after_days = -3\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="helix_context.config"):
        cfg = load_config(str(toml))
    assert cfg.know.betas == list(KnowConfig().betas)
    assert cfg.know.stale_after_days == KnowConfig().stale_after_days
    assert any("betas" in r.message for r in caplog.records)


def test_know_section_wrong_length_betas_soft_fail(tmp_path):
    toml = tmp_path / "helix.toml"
    toml.write_text("[know]\nbetas = [1.0, 2.0]\n", encoding="utf-8")
    cfg = load_config(str(toml))
    assert cfg.know.betas == list(KnowConfig().betas)


def test_load_calibration_from_toml_back_compat_delegates_to_config(tmp_path):
    """The legacy entry point must keep working for direct callers, now
    routed through load_config + calibration_from_config."""
    from helix_context.scoring.know_calibration import (
        KnowCalibration,
        calibration_from_config,
        load_calibration_from_toml,
    )

    toml = tmp_path / "helix.toml"
    toml.write_text(
        "[know]\nemit_floor = 0.61\nbetas = [-2.0, 2.0, 1.5, 0.7, 1.8, 1.5]\n"
        "stale_after_days = 14\n",
        encoding="utf-8",
    )
    via_shim = load_calibration_from_toml(toml)
    via_config = calibration_from_config(load_config(str(toml)).know)
    assert isinstance(via_shim, KnowCalibration)
    assert via_shim == via_config
    assert via_shim.emit_floor == 0.61
    assert via_shim.stale_after_days == 14
    assert via_shim.betas == (-2.0, 2.0, 1.5, 0.7, 1.8, 1.5)  # tuple, frozen


def test_load_calibration_from_toml_no_args_self_loads(tmp_path, monkeypatch):
    """Callers that pass nothing still self-load via the config system
    (HELIX_CONFIG / ./helix.toml discovery) and default cleanly when no
    file exists."""
    from helix_context.scoring.know_calibration import (
        KnowCalibration,
        load_calibration_from_toml,
    )

    monkeypatch.delenv("HELIX_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # no helix.toml here
    assert load_calibration_from_toml() == KnowCalibration()

    # And the env-var discovery path the old shadow loader never had:
    custom = tmp_path / "custom.toml"
    custom.write_text("[know]\nemit_floor = 0.9\n", encoding="utf-8")
    monkeypatch.setenv("HELIX_CONFIG", str(custom))
    assert load_calibration_from_toml().emit_floor == 0.9
