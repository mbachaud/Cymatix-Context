"""
Tests for CpuTagger entity hygiene — the v2 tagger fix for issue #410.

Motivated by the 2026-08-30 EnronQA ingest-decay receipt
(``benchmarks/dogfood/receipts/ingest_decay_enronqa_2026-08-30.json``):
the tagger extracted MIME/email header plumbing as entities, so the
enronqa_padded bed's top entity_graph hubs were ``content-type``
(171,696 rows), ``content-transfer-encoding`` (171,352), ``javamail``,
``x-origin``, ``filename``, ``mime-version`` — making per-insert entity
auto-linking O(N²) and filling the COVER graph with noise. spaCy NER
spans also crossed line boundaries, emitting multi-line garbage entities
('john martin - baylor\\ncc: vince.kaminski@enron.com\\nmime-version').

These tests pin the fix:

  - entities containing newlines/tabs are rejected
  - standard email/MIME header field names are never entities
  - RFC 822 extension headers (x-*) are never entities
  - transport artifacts (JavaMail, quoted-printable, ...) are never entities
  - legitimate entities (orgs, people, CamelCase identifiers) survive
  - the change rides a tagger-version bump (tags are part of the
    bed-content digest — scripts/ingest_equivalence_gate.py — so beds
    built across versions are not comparable), recorded in the
    fixture-matrix build manifest
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cymatix_context import tagger as tagger_mod
from cymatix_context.tagger import CpuTagger

from tests.conftest import requires_spacy_model

# Make scripts/ importable (same shim as test_build_fixture_matrix.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


# Module-level singleton avoids re-loading spaCy per test (expensive)
_TAGGER = None


def _tagger() -> CpuTagger:
    global _TAGGER
    if _TAGGER is None:
        _TAGGER = CpuTagger()
    return _TAGGER


# ── Tagger version bump ─────────────────────────────────────────────

class TestTaggerVersion:
    """Entity extraction changed → bed content changed → version bump."""

    def test_tagger_version_is_2(self):
        assert tagger_mod.TAGGER_VERSION == 2

    def test_manifest_records_tagger_version(self, tmp_path):
        import build_fixture_matrix as bfm

        bfm.update_manifest(
            str(tmp_path),
            {"profile": "testprof", "files": 1, "genes": 1},
            mode="blob",
        )
        manifest = json.loads((tmp_path / "manifest.json").read_text())
        assert (
            manifest["targets"]["testprof"]["tagger_version"]
            == tagger_mod.TAGGER_VERSION
        )


# ── Noise predicate (pure, no spaCy needed) ─────────────────────────

class TestNoiseEntityPredicate:
    """_is_noise_entity: the hygiene gate applied to every candidate entity."""

    def test_multiline_garbage_entity_rejected(self):
        # Verbatim shape from the 2026-08-30 receipt: a spaCy span that
        # crossed two line boundaries inside an Enron header block.
        garbage = (
            "john martin - baylor\ncc: vince.kaminski@enron.com\nmime-version"
        )
        assert tagger_mod._is_noise_entity(garbage)

    def test_carriage_return_rejected(self):
        assert tagger_mod._is_noise_entity("john martin\r\nmime-version")

    def test_tab_rejected(self):
        assert tagger_mod._is_noise_entity("foo\tbar")

    @pytest.mark.parametrize(
        "header",
        [
            "Content-Type",
            "content-type",
            "Content-Transfer-Encoding",
            "CONTENT-TRANSFER-ENCODING",
            "Mime-Version",
            "MIME-Version",
            "Message-ID",
            "FileName",
            "filename",
            "boundary",
            "charset",
            "Return-Path",
            "Reply-To",
            "In-Reply-To",
        ],
    )
    def test_standard_header_names_rejected(self, header):
        assert tagger_mod._is_noise_entity(header)

    @pytest.mark.parametrize(
        "xheader",
        ["X-Origin", "x-origin", "X-FileName", "X-Folder", "X-From", "x-cc"],
    )
    def test_extension_header_shape_rejected(self, xheader):
        assert tagger_mod._is_noise_entity(xheader)

    @pytest.mark.parametrize(
        "colon_form",
        ["X-Origin:", "x-origin:", "Content-Type:", "Mime-Version:"],
    )
    def test_header_key_with_trailing_colon_rejected(self, colon_form):
        # spaCy NER spans sometimes include the header's colon; the 2000-file
        # receipt's v2 arm still carried 58 'x-origin:' entity_graph rows.
        assert tagger_mod._is_noise_entity(colon_form)

    @pytest.mark.parametrize(
        "artifact",
        ["JavaMail", "javamail", "quoted-printable", "us-ascii", "7bit", "8bit"],
    )
    def test_transport_artifacts_rejected(self, artifact):
        assert tagger_mod._is_noise_entity(artifact)

    @pytest.mark.parametrize(
        "legit",
        [
            "Enron",
            "Vince Kaminski",
            "CpuTagger",
            "Anthropic",
            "New York",
            "Content Management Team",  # contains 'content' but is not a header
        ],
    )
    def test_legitimate_entities_survive(self, legit):
        assert not tagger_mod._is_noise_entity(legit)


# ── Integration: pack() on an Enron-shaped email ────────────────────

# Header block modeled on the real corpus files (JavaMail Message-ID,
# X-* Outlook export headers, MIME plumbing) with a body that carries a
# deterministic CamelCase identifier the scan must still surface.
_ENRON_EMAIL = """Message-ID: <13350103.1075856966563.JavaMail.evans@thyme>
Date: Wed, 2 May 2001 09:26:00 -0700 (PDT)
From: vince.kaminski@enron.com
To: john.martin@bus.example.edu
Subject: Re: DealBench quarterly review
Mime-Version: 1.0
Content-Type: text/plain; charset=us-ascii
Content-Transfer-Encoding: 7bit
X-From: Vince J Kaminski
X-To: John Martin - Baylor
X-Origin: Kaminski-V
X-FileName: vkamins.nsf

John,

Thanks for the update on the DealBench platform. The risk team at Enron
reviewed the quarterly numbers and the forward curve model looks solid.

Vince
"""


@requires_spacy_model
class TestPackEntityHygiene:
    def test_no_header_plumbing_entities(self):
        gene = _tagger().pack(_ENRON_EMAIL, content_type="text")
        lowered = {e.lower() for e in gene.promoter.entities}
        plumbing = {
            "content-type",
            "content-transfer-encoding",
            "mime-version",
            "message-id",
            "x-origin",
            "x-filename",
            "x-from",
            "x-to",
            "filename",
            "javamail",
        }
        assert not (lowered & plumbing), (
            f"header plumbing leaked into entities: {sorted(lowered & plumbing)}"
        )

    def test_no_multiline_entities(self):
        gene = _tagger().pack(_ENRON_EMAIL, content_type="text")
        bad = [e for e in gene.promoter.entities if "\n" in e or "\r" in e]
        assert not bad, f"multi-line entities leaked: {bad!r}"

    def test_legitimate_camelcase_entity_survives(self):
        gene = _tagger().pack(_ENRON_EMAIL, content_type="text")
        lowered = {e.lower() for e in gene.promoter.entities}
        assert "dealbench" in lowered
