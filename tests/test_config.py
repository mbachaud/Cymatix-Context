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


# ── Ingestion / ribosome coherence guard ──────────────────────────────
# Tests pin the auto-fallback added 2026-05-12: when [ribosome] is
# disabled (LLM-free pillar) but [ingestion] still points at an LLM
# backend, ``load_config`` flips ingestion to "cpu" so `helix ingest`
# doesn't crash with "Ribosome is disabled". See AI-user feedback on
# cli+mcp ingest path.


def test_ingestion_falls_back_to_cpu_when_ribosome_disabled(tmp_path, caplog):
    """Default-disabled ribosome + ollama ingest → auto-flip to cpu."""
    import logging
    from helix_context.config import load_config

    toml = tmp_path / "helix.toml"
    toml.write_text(
        "[ribosome]\nenabled = false\n[ingestion]\nbackend = \"ollama\"\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="helix_context.config"):
        cfg = load_config(str(toml))
    assert cfg.ingestion.backend == "cpu"
    assert any("auto-falling-back" in r.message for r in caplog.records), (
        "expected an auto-fallback WARNING; got: "
        f"{[r.message for r in caplog.records]}"
    )


def test_ingestion_left_alone_when_ribosome_enabled(tmp_path):
    """Operators who enable ribosome + ollama get exactly what they asked for."""
    from helix_context.config import load_config
    toml = tmp_path / "helix.toml"
    toml.write_text(
        "[ribosome]\nenabled = true\n[ingestion]\nbackend = \"ollama\"\n",
        encoding="utf-8",
    )
    cfg = load_config(str(toml))
    assert cfg.ingestion.backend == "ollama"


def test_ingestion_left_alone_when_explicitly_cpu(tmp_path):
    """Explicit cpu / hybrid backends stay put even with ribosome off."""
    from helix_context.config import load_config
    toml = tmp_path / "helix.toml"
    toml.write_text(
        "[ribosome]\nenabled = false\n[ingestion]\nbackend = \"hybrid\"\n",
        encoding="utf-8",
    )
    cfg = load_config(str(toml))
    assert cfg.ingestion.backend == "hybrid"


# ── Hardware section + ribosome.device deprecation shim (Task 9) ─────
# Tests pin parsing of the [hardware] TOML section and the deprecation
# warning emitted for legacy [ribosome] device usage. See
# docs/specs/2026-05-04-hardware-detection-design.md for the contract.

from helix_context.config import load_config


def test_hardware_section_parses(tmp_path):
    """[hardware] section parses with all defaults."""
    cfg_text = """
[hardware]
device = "cuda"
batch_sizes = "auto"
low_vram_threshold_gb = 4.0
"""
    p = tmp_path / "helix.toml"
    p.write_text(cfg_text)
    cfg = load_config(str(p))
    assert cfg.hardware.device == "cuda"
    assert cfg.hardware.batch_sizes == {}
    assert cfg.hardware.low_vram_threshold_gb == 4.0


def test_hardware_section_batch_sizes_dict(tmp_path):
    cfg_text = """
[hardware]
device = "auto"
batch_sizes = { rerank = 16, splice = 32 }
"""
    p = tmp_path / "helix.toml"
    p.write_text(cfg_text)
    cfg = load_config(str(p))
    assert cfg.hardware.batch_sizes == {"rerank": 16, "splice": 32}


def test_ribosome_device_deprecation_warning(tmp_path, caplog):
    """[ribosome] device alone (no [hardware]) triggers deprecation warning."""
    cfg_text = """
[ribosome]
device = "cuda"
"""
    p = tmp_path / "helix.toml"
    p.write_text(cfg_text)
    with caplog.at_level("WARNING", logger="helix_context.config"):
        cfg = load_config(str(p))
    assert cfg.hardware.device == "cuda"
    assert any(
        "ribosome" in rec.message.lower() and "deprecated" in rec.message.lower()
        for rec in caplog.records
    )


def test_hardware_overrides_ribosome_device(tmp_path, caplog):
    """When both are set, [hardware] wins; warning still fires noting override."""
    cfg_text = """
[ribosome]
device = "cpu"

[hardware]
device = "cuda"
"""
    p = tmp_path / "helix.toml"
    p.write_text(cfg_text)
    with caplog.at_level("WARNING", logger="helix_context.config"):
        cfg = load_config(str(p))
    assert cfg.hardware.device == "cuda"
    assert any(
        "deprecated" in rec.message.lower() and "override" in rec.message.lower()
        for rec in caplog.records
    )


def test_no_device_config_defaults_to_auto(tmp_path):
    p = tmp_path / "helix.toml"
    p.write_text("# empty\n")
    cfg = load_config(str(p))
    assert cfg.hardware.device == "auto"
    assert cfg.hardware.batch_sizes == {}


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


# ── Env overrides on failed-load paths (bugbash BUG-1) ───────────────
# Documented precedence is env > toml > default. The pre-fix loader
# returned bare defaults from the missing-file and malformed-TOML early
# exits, silently dropping HELIX_GENOME_PATH / HELIX_SERVER_UPSTREAM.


def test_env_overrides_apply_when_config_file_missing(tmp_path, monkeypatch):
    """HELIX_* env overrides must win even when helix.toml does not exist."""
    from helix_context.config import load_config
    monkeypatch.setenv("HELIX_GENOME_PATH", "genomes/env-override.db")
    monkeypatch.setenv("HELIX_SERVER_UPSTREAM", "http://127.0.0.1:9999")
    cfg = load_config(str(tmp_path / "does-not-exist.toml"))
    assert cfg.genome.path == "genomes/env-override.db"
    assert cfg.server.upstream == "http://127.0.0.1:9999"


def test_env_overrides_apply_when_toml_malformed(tmp_path, monkeypatch):
    """HELIX_* env overrides must win even when helix.toml fails to parse."""
    from helix_context.config import load_config
    p = tmp_path / "helix.toml"
    p.write_text("[genome\npath = broken", encoding="utf-8")
    monkeypatch.setenv("HELIX_GENOME_PATH", "genomes/env-override.db")
    monkeypatch.setenv("HELIX_SERVER_UPSTREAM", "http://127.0.0.1:9999")
    cfg = load_config(str(p))
    assert cfg.genome.path == "genomes/env-override.db"
    assert cfg.server.upstream == "http://127.0.0.1:9999"


def test_env_overrides_still_beat_toml_on_success_path(tmp_path, monkeypatch):
    """Sanity pin: env > toml on the normal (parsed) load path too."""
    from helix_context.config import load_config
    p = tmp_path / "helix.toml"
    p.write_text(
        "[genome]\npath = \"genomes/from-toml.db\"\n"
        "[server]\nupstream = \"http://toml-upstream:1\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HELIX_GENOME_PATH", "genomes/env-override.db")
    monkeypatch.setenv("HELIX_SERVER_UPSTREAM", "http://127.0.0.1:9999")
    cfg = load_config(str(p))
    assert cfg.genome.path == "genomes/env-override.db"
    assert cfg.server.upstream == "http://127.0.0.1:9999"


def test_budget_foveated_toml_override(tmp_path):
    """Regression: helix.toml [budget] foveated_* keys are honored."""
    from helix_context.config import load_config
    p = tmp_path / "helix.toml"
    p.write_text(
        "[budget]\nfoveated_enabled = true\nfoveated_alpha = 2.0\nfoveated_c_min = 0.20\nfoveated_base_chars = 1500\n",
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    assert cfg.budget.foveated_enabled is True
    assert cfg.budget.foveated_alpha == 2.0
    assert cfg.budget.foveated_c_min == 0.20
    assert cfg.budget.foveated_base_chars == 1500
