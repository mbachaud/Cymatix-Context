def test_upstream_timeout_default_is_180s():
    """Regression test for the 2026-05-02 default bump.

    Cymatix's default of 120s was observed to silently return Proxy 500s
    on slow gemma4:e4b GPQA queries at ~125s (full breakdown in the
    2026-05-01 overnight report). 180s is the shipping default; 120s
    is a regression.
    """
    from cymatix_context.config import CymatixConfig
    cfg = CymatixConfig()
    assert cfg.server.upstream_timeout == 180.0


def test_budget_abstain_enabled_default_is_true():
    """Regression: new ABSTAIN gate ships on by default.

    The 2026-05-02 ABSTAIN tier (docs/specs/2026-05-02-abstain-tier-design.md)
    is shipped on-by-default so the latency win lands without an opt-in step.
    Operators flip to false in cymatix.toml [budget] for the legacy always-
    inject behavior. Bumping this default to false would silently undo the
    GPQA Diamond p95 fix.
    """
    from cymatix_context.config import CymatixConfig
    cfg = CymatixConfig()
    assert cfg.budget.abstain_enabled is True


def test_budget_abstain_enabled_toml_override(tmp_path):
    """Regression: cymatix.toml [budget] abstain_enabled = false is honored."""
    from cymatix_context.config import load_config
    toml = tmp_path / "cymatix.toml"
    toml.write_text(
        "[budget]\nabstain_enabled = false\n",
        encoding="utf-8",
    )
    cfg = load_config(str(toml))
    assert cfg.budget.abstain_enabled is False


# ── Ingestion / ribosome coherence guard ──────────────────────────────
# Tests pin the auto-fallback added 2026-05-12: when [ribosome] is
# disabled (LLM-free pillar) but [ingestion] still points at an LLM
# backend, ``load_config`` flips ingestion to "cpu" so `cymatix ingest`
# doesn't crash with "Ribosome is disabled". See AI-user feedback on
# cli+mcp ingest path.


def test_ingestion_falls_back_to_cpu_when_ribosome_disabled(tmp_path, caplog):
    """Default-disabled ribosome + ollama ingest → auto-flip to cpu."""
    import logging
    from cymatix_context.config import load_config

    toml = tmp_path / "cymatix.toml"
    toml.write_text(
        "[ribosome]\nenabled = false\n[ingestion]\nbackend = \"ollama\"\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="cymatix_context.config"):
        cfg = load_config(str(toml))
    assert cfg.ingestion.backend == "cpu"
    assert any("auto-falling-back" in r.message for r in caplog.records), (
        "expected an auto-fallback WARNING; got: "
        f"{[r.message for r in caplog.records]}"
    )


def test_ingestion_left_alone_when_ribosome_enabled(tmp_path):
    """Operators who enable ribosome + ollama get exactly what they asked for."""
    from cymatix_context.config import load_config
    toml = tmp_path / "cymatix.toml"
    toml.write_text(
        "[ribosome]\nenabled = true\n[ingestion]\nbackend = \"ollama\"\n",
        encoding="utf-8",
    )
    cfg = load_config(str(toml))
    assert cfg.ingestion.backend == "ollama"


def test_ingestion_left_alone_when_explicitly_cpu(tmp_path):
    """Explicit cpu / hybrid backends stay put even with ribosome off."""
    from cymatix_context.config import load_config
    toml = tmp_path / "cymatix.toml"
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

from cymatix_context.config import load_config


def test_hardware_section_parses(tmp_path):
    """[hardware] section parses with all defaults."""
    cfg_text = """
[hardware]
device = "cuda"
batch_sizes = "auto"
low_vram_threshold_gb = 4.0
"""
    p = tmp_path / "cymatix.toml"
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
    p = tmp_path / "cymatix.toml"
    p.write_text(cfg_text)
    cfg = load_config(str(p))
    assert cfg.hardware.batch_sizes == {"rerank": 16, "splice": 32}


def test_ribosome_device_deprecation_warning(tmp_path, caplog):
    """[ribosome] device alone (no [hardware]) triggers deprecation warning."""
    cfg_text = """
[ribosome]
device = "cuda"
"""
    p = tmp_path / "cymatix.toml"
    p.write_text(cfg_text)
    with caplog.at_level("WARNING", logger="cymatix_context.config"):
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
    p = tmp_path / "cymatix.toml"
    p.write_text(cfg_text)
    with caplog.at_level("WARNING", logger="cymatix_context.config"):
        cfg = load_config(str(p))
    assert cfg.hardware.device == "cuda"
    assert any(
        "deprecated" in rec.message.lower() and "override" in rec.message.lower()
        for rec in caplog.records
    )


def test_no_device_config_defaults_to_auto(tmp_path):
    p = tmp_path / "cymatix.toml"
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
    from cymatix_context.config import CymatixConfig
    cfg = CymatixConfig()
    assert cfg.budget.foveated_enabled is False
    assert cfg.budget.foveated_alpha == 1.0
    assert cfg.budget.foveated_c_min == 0.15
    assert cfg.budget.foveated_base_chars == 1000


# ── Env overrides on failed-load paths (bugbash BUG-1) ───────────────
# Documented precedence is env > toml > default. The pre-fix loader
# returned bare defaults from the missing-file and malformed-TOML early
# exits, silently dropping CYMATIX_GENOME_PATH / CYMATIX_SERVER_UPSTREAM.


def test_env_overrides_apply_when_config_file_missing(tmp_path, monkeypatch):
    """CYMATIX_* env overrides must win even when cymatix.toml does not exist."""
    from cymatix_context.config import load_config
    monkeypatch.setenv("CYMATIX_GENOME_PATH", "genomes/env-override.db")
    monkeypatch.setenv("CYMATIX_SERVER_UPSTREAM", "http://127.0.0.1:9999")
    cfg = load_config(str(tmp_path / "does-not-exist.toml"))
    assert cfg.genome.path == "genomes/env-override.db"
    assert cfg.server.upstream == "http://127.0.0.1:9999"


def test_env_overrides_apply_when_toml_malformed(tmp_path, monkeypatch):
    """CYMATIX_* env overrides must win even when cymatix.toml fails to parse."""
    from cymatix_context.config import load_config
    p = tmp_path / "cymatix.toml"
    p.write_text("[genome\npath = broken", encoding="utf-8")
    monkeypatch.setenv("CYMATIX_GENOME_PATH", "genomes/env-override.db")
    monkeypatch.setenv("CYMATIX_SERVER_UPSTREAM", "http://127.0.0.1:9999")
    cfg = load_config(str(p))
    assert cfg.genome.path == "genomes/env-override.db"
    assert cfg.server.upstream == "http://127.0.0.1:9999"


def test_env_overrides_still_beat_toml_on_success_path(tmp_path, monkeypatch):
    """Sanity pin: env > toml on the normal (parsed) load path too."""
    from cymatix_context.config import load_config
    p = tmp_path / "cymatix.toml"
    p.write_text(
        "[genome]\npath = \"genomes/from-toml.db\"\n"
        "[server]\nupstream = \"http://toml-upstream:1\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CYMATIX_GENOME_PATH", "genomes/env-override.db")
    monkeypatch.setenv("CYMATIX_SERVER_UPSTREAM", "http://127.0.0.1:9999")
    cfg = load_config(str(p))
    assert cfg.genome.path == "genomes/env-override.db"
    assert cfg.server.upstream == "http://127.0.0.1:9999"


def test_budget_foveated_toml_override(tmp_path):
    """Regression: cymatix.toml [budget] foveated_* keys are honored."""
    from cymatix_context.config import load_config
    p = tmp_path / "cymatix.toml"
    p.write_text(
        "[budget]\nfoveated_enabled = true\nfoveated_alpha = 2.0\nfoveated_c_min = 0.20\nfoveated_base_chars = 1500\n",
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    assert cfg.budget.foveated_enabled is True
    assert cfg.budget.foveated_alpha == 2.0
    assert cfg.budget.foveated_c_min == 0.20
    assert cfg.budget.foveated_base_chars == 1500


# ── [encoder_daemon] section (Fork 1 slice 1) ─────────────────────────
# url="" (default) means "off" — every encoder seam stays in-process,
# byte-identical to pre-daemon behavior. Mirrors the [server].upstream /
# CYMATIX_SERVER_UPSTREAM three-part pattern above.


def test_encoder_daemon_url_default_is_empty():
    """Default is off: no cymatix.toml, no env → in-process encoders."""
    from cymatix_context.config import CymatixConfig
    cfg = CymatixConfig()
    assert cfg.encoder_daemon.url == ""


def test_encoder_daemon_url_toml_override(tmp_path):
    """cymatix.toml [encoder_daemon] url is honored."""
    from cymatix_context.config import load_config
    p = tmp_path / "cymatix.toml"
    p.write_text(
        "[encoder_daemon]\nurl = \"http://127.0.0.1:11439\"\n",
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    assert cfg.encoder_daemon.url == "http://127.0.0.1:11439"


def test_encoder_daemon_url_env_override_wins_over_toml(tmp_path, monkeypatch):
    """CYMATIX_ENCODER_URL beats [encoder_daemon] url from toml (env > toml > default)."""
    from cymatix_context.config import load_config
    p = tmp_path / "cymatix.toml"
    p.write_text(
        "[encoder_daemon]\nurl = \"http://toml-daemon:1\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CYMATIX_ENCODER_URL", "http://env-daemon:2")
    cfg = load_config(str(p))
    assert cfg.encoder_daemon.url == "http://env-daemon:2"


def test_encoder_daemon_url_env_override_applies_when_config_file_missing(tmp_path, monkeypatch):
    """CYMATIX_ENCODER_URL must win even when cymatix.toml does not exist."""
    from cymatix_context.config import load_config
    monkeypatch.setenv("CYMATIX_ENCODER_URL", "http://env-daemon:2")
    cfg = load_config(str(tmp_path / "does-not-exist.toml"))
    assert cfg.encoder_daemon.url == "http://env-daemon:2"


def test_encoder_daemon_url_empty_env_does_not_override_toml(tmp_path, monkeypatch):
    """Truthiness check: CYMATIX_ENCODER_URL="" counts as unset, not a disable-override."""
    from cymatix_context.config import load_config
    p = tmp_path / "cymatix.toml"
    p.write_text(
        "[encoder_daemon]\nurl = \"http://toml-daemon:1\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CYMATIX_ENCODER_URL", "")
    cfg = load_config(str(p))
    assert cfg.encoder_daemon.url == "http://toml-daemon:1"
