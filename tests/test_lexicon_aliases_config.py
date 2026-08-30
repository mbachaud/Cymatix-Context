"""ROSETTA Tier 2: config-layer canonical-term aliases (Task A1).

Pins the additive alias contract in ``cymatix_context/config.py``:
``[compressor]`` <-> legacy ``[ribosome]``, ``[knowledge_store]`` <->
legacy ``[genome]``, ``[budget] retrieval_tokens``/``max_docs_per_turn``
<-> legacy ``expression_tokens``/``max_genes_per_turn``, and
``CYMATIX_STORE_PATH`` <-> legacy ``CYMATIX_GENOME_PATH``. Collision rule
throughout: the legacy name wins and a WARNING names both. An unknown
top-level TOML section also warns (typo guard), never errors.

See docs/ROSETTA.md for the full biology-to-software lexicon and
.claude/worktrees/.../task-A1-brief.md for the task contract.
"""
import logging

from cymatix_context.config import load_config


def test_compressor_section_aliases_ribosome(tmp_path):
    """[compressor] is the canonical name for the legacy [ribosome] section."""
    p = tmp_path / "cymatix.toml"
    p.write_text('[compressor]\ntimeout = 123\n', encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.ribosome.timeout == 123


def test_knowledge_store_section_aliases_genome(tmp_path, monkeypatch):
    """[knowledge_store] is the canonical name for the legacy [genome] section.

    tests/conftest.py sets CYMATIX_GENOME_PATH=":memory:" session-wide (to
    keep sqlite3 happy during collection); env wins over toml by documented
    precedence, so both env aliases must be cleared here to observe the
    toml-level [knowledge_store] -> [genome] section alias in isolation.
    """
    monkeypatch.delenv("CYMATIX_GENOME_PATH", raising=False)
    monkeypatch.delenv("CYMATIX_STORE_PATH", raising=False)
    p = tmp_path / "cymatix.toml"
    p.write_text('[knowledge_store]\npath = "x/y.db"\n', encoding="utf-8")
    assert load_config(str(p)).genome.path == "x/y.db"


def test_budget_key_aliases(tmp_path):
    """[budget] retrieval_tokens/max_docs_per_turn alias the legacy keys."""
    p = tmp_path / "cymatix.toml"
    p.write_text(
        '[budget]\nretrieval_tokens = 4321\nmax_docs_per_turn = 9\n',
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    assert cfg.budget.expression_tokens == 4321
    assert cfg.budget.max_genes_per_turn == 9


def test_legacy_section_wins_on_collision_with_warning(tmp_path, caplog):
    """Both [ribosome] and [compressor] present: legacy wins, WARNING names both."""
    p = tmp_path / "cymatix.toml"
    p.write_text('[ribosome]\ntimeout = 1\n[compressor]\ntimeout = 2\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="cymatix_context.config"):
        cfg = load_config(str(p))
    assert cfg.ribosome.timeout == 1
    assert any(
        "compressor" in r.message and "ribosome" in r.message
        for r in caplog.records
    ), f"expected a collision WARNING; got: {[r.message for r in caplog.records]}"


def test_legacy_budget_key_wins_on_collision_with_warning(tmp_path, caplog):
    """Both expression_tokens and retrieval_tokens present: legacy key wins + warns."""
    p = tmp_path / "cymatix.toml"
    p.write_text(
        '[budget]\nexpression_tokens = 111\nretrieval_tokens = 222\n',
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="cymatix_context.config"):
        cfg = load_config(str(p))
    assert cfg.budget.expression_tokens == 111
    assert any(
        "retrieval_tokens" in r.message and "expression_tokens" in r.message
        for r in caplog.records
    ), f"expected a collision WARNING; got: {[r.message for r in caplog.records]}"


def test_unknown_section_warns(tmp_path, caplog):
    """A typo'd section name (not a known section or alias) warns, not errors."""
    p = tmp_path / "cymatix.toml"
    p.write_text('[compresser]\ntimeout = 1\n', encoding="utf-8")  # typo section
    with caplog.at_level(logging.WARNING, logger="cymatix_context.config"):
        load_config(str(p))  # must not raise
    assert any("compresser" in r.message for r in caplog.records)


def test_known_sections_and_aliases_never_warn_as_unknown(tmp_path, caplog):
    """Real sections and the two Tier 2 aliases must never trip the unknown-section warning."""
    p = tmp_path / "cymatix.toml"
    p.write_text(
        '[ribosome]\nenabled = true\n'
        '[compressor]\ntimeout = 5\n'  # collides with [ribosome] above (legacy wins) but is a KNOWN alias
        '[knowledge_store]\npath = "a/b.db"\n'
        '[budget]\nabstain_enabled = true\n',
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="cymatix_context.config"):
        load_config(str(p))
    assert not any(
        "Unknown top-level section" in r.message for r in caplog.records
    ), f"unexpected unknown-section warning(s): {[r.message for r in caplog.records]}"


def test_store_path_env_alias(tmp_path, monkeypatch):
    """CYMATIX_STORE_PATH is honored when the legacy CYMATIX_GENOME_PATH is unset."""
    monkeypatch.delenv("CYMATIX_GENOME_PATH", raising=False)
    monkeypatch.setenv("CYMATIX_STORE_PATH", "env/store.db")
    p = tmp_path / "cymatix.toml"
    p.write_text("", encoding="utf-8")
    assert load_config(str(p)).genome.path == "env/store.db"


def test_genome_env_wins_over_store_env(tmp_path, monkeypatch, caplog):
    """Both env vars set: legacy CYMATIX_GENOME_PATH wins over CYMATIX_STORE_PATH."""
    monkeypatch.setenv("CYMATIX_GENOME_PATH", "legacy.db")
    monkeypatch.setenv("CYMATIX_STORE_PATH", "new.db")
    p = tmp_path / "cymatix.toml"
    p.write_text("", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="cymatix_context.config"):
        cfg = load_config(str(p))
    assert cfg.genome.path == "legacy.db"
    assert any(
        "CYMATIX_STORE_PATH" in r.message and "CYMATIX_GENOME_PATH" in r.message
        for r in caplog.records
    ), f"expected a collision WARNING; got: {[r.message for r in caplog.records]}"
