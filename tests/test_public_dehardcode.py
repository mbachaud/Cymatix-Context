"""Public-product de-hardcoding guards (wave 1).

Pins the removal of owner-specific vocabulary from the shipped defaults:

* ``cymatix_context.tagger`` ships no private project names in its
  EntityRuler patterns or tech-term dictionary, and supports
  per-deployment extension via the HELIX_TAGGER_EXTRA_ENTITIES /
  HELIX_TAGGER_EXTRA_TERMS env vars.
* ``lexical_rescue`` path scoring is corpus-neutral — a generic
  query-term / path-segment match earns the boost, and this
  repository's own paths get no special treatment.
* ``helix.toml`` ships no owner-project synonym rows and no personal
  ``watch_dirs``.

None of these tests require the spaCy model — they assert on the
module-level vocabulary and the regex/scoring helpers only (mirrors
tests/test_tagger_kv.py, which exercises CpuTagger without _get_nlp()).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

import cymatix_context.tagger as tagger_mod
from cymatix_context.retrieval.lexical_rescue import _source_path_bonus

REPO_ROOT = Path(__file__).resolve().parent.parent

# Owner-project vocabulary that pre-public builds shipped as defaults.
OWNER_ENTITY_MARKERS = (
    "BigEd", "BookKeeper", "CosmicTasha", "two-brain", "ModuleHub",
    "Dr. Ders", "FleetDB", "SwiftWing21",
)
OWNER_TECH_TERMS = (
    "biged", "bookkeeper", "cosmictasha", "modulehub", "dr ders", "fleetdb",
)


# ── (a) tagger ships neutral vocabulary ─────────────────────────────

class TestTaggerShipsNeutralVocabulary:
    def test_no_owner_entities_in_vocabulary(self):
        rendered = repr(tagger_mod._PROJECT_ENTITIES)
        for marker in OWNER_ENTITY_MARKERS:
            assert marker not in rendered, marker

    def test_no_owner_entities_in_built_patterns(self):
        rendered = repr(tagger_mod._build_project_patterns())
        for marker in OWNER_ENTITY_MARKERS:
            assert marker not in rendered, marker

    def test_no_owner_tech_terms(self):
        for term in OWNER_TECH_TERMS:
            assert term not in tagger_mod._TECH_TERMS, term

    def test_own_stack_vocabulary_survives(self):
        # The product's own stack stays taggable out of the box.
        assert "Helix Context" in tagger_mod._PROJECT_ENTITIES["PRODUCT"]
        assert "helix" in tagger_mod._TECH_TERMS
        assert "splade" in tagger_mod._TECH_TERMS


# ── (b) env-var vocabulary extension ────────────────────────────────

class TestTaggerEnvExtension:
    def test_extra_entities_and_terms_appended(self, monkeypatch):
        monkeypatch.setenv(
            "HELIX_TAGGER_EXTRA_ENTITIES", "PRODUCT:AcmeApp, ORG:AcmeCorp"
        )
        monkeypatch.setenv("HELIX_TAGGER_EXTRA_TERMS", "acmeapp, AcmeCorp")
        mod = importlib.reload(tagger_mod)
        try:
            assert "AcmeApp" in mod._PROJECT_ENTITIES["PRODUCT"]
            assert "AcmeCorp" in mod._PROJECT_ENTITIES["ORG"]
            assert "acmeapp" in mod._TECH_TERMS
            assert "acmecorp" in mod._TECH_TERMS  # lowercased on ingest
            patterns = mod._build_project_patterns()
            assert {"label": "PRODUCT", "pattern": "AcmeApp"} in patterns
        finally:
            monkeypatch.delenv("HELIX_TAGGER_EXTRA_ENTITIES", raising=False)
            monkeypatch.delenv("HELIX_TAGGER_EXTRA_TERMS", raising=False)
            importlib.reload(tagger_mod)

    def test_new_label_creates_bucket(self, monkeypatch):
        monkeypatch.setenv("HELIX_TAGGER_EXTRA_ENTITIES", "gpe:Acmeville")
        mod = importlib.reload(tagger_mod)
        try:
            assert "Acmeville" in mod._PROJECT_ENTITIES["GPE"]
        finally:
            monkeypatch.delenv("HELIX_TAGGER_EXTRA_ENTITIES", raising=False)
            importlib.reload(tagger_mod)

    def test_malformed_entity_pairs_skipped(self, monkeypatch):
        monkeypatch.setenv(
            "HELIX_TAGGER_EXTRA_ENTITIES", "NoColonHere,:NoLabel,PRODUCT:Good"
        )
        mod = importlib.reload(tagger_mod)
        try:
            assert "Good" in mod._PROJECT_ENTITIES["PRODUCT"]
            rendered = repr(mod._PROJECT_ENTITIES)
            assert "NoColonHere" not in rendered
            assert "NoLabel" not in rendered
        finally:
            monkeypatch.delenv("HELIX_TAGGER_EXTRA_ENTITIES", raising=False)
            importlib.reload(tagger_mod)

    def test_clean_env_reload_restores_defaults(self, monkeypatch):
        monkeypatch.delenv("HELIX_TAGGER_EXTRA_ENTITIES", raising=False)
        monkeypatch.delenv("HELIX_TAGGER_EXTRA_TERMS", raising=False)
        mod = importlib.reload(tagger_mod)
        assert "AcmeApp" not in repr(mod._PROJECT_ENTITIES)
        assert "acmeapp" not in mod._TECH_TERMS


# ── (c) lexical rescue is corpus-neutral ────────────────────────────

class TestLexicalRescuePathAffinity:
    def test_query_term_path_segment_earns_boost(self):
        # Whole-segment match (+2.0) on top of substring agreement (+0.4)
        seg = _source_path_bonus("F:/work/acme/policies.md", {"acme"})
        # Substring-only match: "acme" is inside "acmeology" but names no
        # whole segment / stem.
        sub = _source_path_bonus("F:/work/acmeology/policies.md", {"acme"})
        assert seg - sub == pytest.approx(2.0)

    def test_filename_stem_counts_as_segment(self):
        with_stem = _source_path_bonus("F:/repo/conf/acme.toml", {"acme"})
        without = _source_path_bonus("F:/repo/conf/other.toml", {"acme"})
        # +2.0 segment-stem boost +0.4 substring agreement
        assert with_stem - without == pytest.approx(2.4)

    def test_short_terms_do_not_trigger_segment_boost(self):
        # len < 4 terms are too unspecific for the +2.0 path-affinity boost
        assert _source_path_bonus("F:/work/api/handlers.py", {"api"}) == 0.0

    def test_helix_context_paths_are_not_special_cased(self):
        # Structurally identical paths must score identically whether they
        # mention this product or any other project.
        helix = _source_path_bonus(
            "F:/projects/helix-context/server.py", {"helix"}
        )
        acme = _source_path_bonus(
            "F:/projects/acme-context/server.py", {"acme"}
        )
        assert helix == pytest.approx(acme)

        helix_toml = _source_path_bonus(
            "F:/projects/helix-context/helix.toml", {"helix"}
        )
        acme_toml = _source_path_bonus(
            "F:/projects/acme-context/acme.toml", {"acme"}
        )
        assert helix_toml == pytest.approx(acme_toml)

    def test_worktrees_penalty_removed_tests_penalty_kept(self):
        assert _source_path_bonus("F:/x/_worktrees/y.py", set()) == 0.0
        assert _source_path_bonus("F:/x/tests/y.py", set()) == -0.75

    def test_rescue_prefers_query_term_path_segment(self, tmp_path):
        from cymatix_context.genome import Genome
        from cymatix_context.retrieval.lexical_rescue import lexical_rescue_sources

        from tests.conftest import make_gene

        db = tmp_path / "genome.db"
        genome = Genome(str(db))
        try:
            generic = make_gene(
                "retention policy schedule for backups", domains=["retention"]
            )
            generic.source_id = "F:/work/other/policies.md"
            genome.upsert_gene(generic, apply_gate=False)

            acme = make_gene(
                "retention policy schedule for acme backups",
                domains=["retention"],
            )
            acme.source_id = "F:/work/acme/policies.md"
            genome.upsert_gene(acme, apply_gate=False)
        finally:
            genome.close()

        sources = lexical_rescue_sources(
            "acme retention policy", genome_path=str(db), limit=2
        )

        assert sources[0] == "F:/work/acme/policies.md"


# ── (d) helix.toml ships neutral config ─────────────────────────────

class TestHelixTomlShipsNeutral:
    @pytest.fixture()
    def cfg(self):
        _shipped = REPO_ROOT / "cymatix.toml"
        if not _shipped.exists():
            _shipped = REPO_ROOT / "helix.toml"
        with open(_shipped, "rb") as fh:
            return tomllib.load(fh)

    def test_synonyms_have_no_owner_rows(self, cfg):
        synonyms = cfg["synonyms"]
        for owner_key in ("biged", "bookkeeper", "cosmictasha", "fleet"):
            assert owner_key not in synonyms, owner_key
        flat_values = {v for values in synonyms.values() for v in values}
        assert not flat_values & {
            "biged", "biged-rs", "bookkeeper", "cosmictasha", "scorerift"
        }

    def test_mem_sync_watch_dirs_empty(self, cfg):
        assert cfg["mem_sync"]["watch_dirs"] == []
