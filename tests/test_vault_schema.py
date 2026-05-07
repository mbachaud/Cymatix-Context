"""Tests for frontmatter rendering, filename derivation, and path safety."""
from __future__ import annotations

from pathlib import Path

import pytest

from helix_context.vault.schema import (
    AUTHORED_FIELDS,
    COMPUTED_FIELDS,
    authored_placeholders,
    derive_gene_filename,
    derive_gene_relpath,
    safe_resolve_under,
)


class TestFieldClassification:
    def test_computed_fields_disjoint_from_authored(self):
        assert COMPUTED_FIELDS.isdisjoint(AUTHORED_FIELDS)

    def test_computed_fields_present(self):
        for k in [
            "gene_id", "chromatin", "domains", "content_type",
            "source_id", "source_lines", "content_sha256",
            "last_seen", "last_seen_ts", "live_truth_score",
            "co_activation_partners", "party_id", "participant_handle",
        ]:
            assert k in COMPUTED_FIELDS

    def test_authored_fields_present(self):
        for k in [
            "operator_notes", "operator_tags", "pinned", "quarantine_reason",
            "supersedes", "contradicts", "implements", "documented_by", "tests",
        ]:
            assert k in AUTHORED_FIELDS


class TestAuthoredPlaceholders:
    def test_placeholder_values(self):
        p = authored_placeholders()
        assert p["operator_notes"] == ""
        assert p["operator_tags"] == []
        assert p["pinned"] is False
        assert p["quarantine_reason"] is None
        for k in ("supersedes", "contradicts", "implements", "documented_by", "tests"):
            assert p[k] == []


class TestDeriveFilename:
    def test_simple_python_path(self):
        # gene_id "abc123def456" → short_id "abc123"
        assert derive_gene_filename("helix_context/auth/middleware.py", "abc123def456") \
            == "middleware-abc123.md"

    def test_strips_extension(self):
        assert derive_gene_filename("foo/bar.md", "1234567890ab") == "bar-123456.md"

    def test_no_extension(self):
        assert derive_gene_filename("foo/Makefile", "ab12cd34ef56") == "Makefile-ab12cd.md"

    def test_short_id_exactly_six_chars(self):
        result = derive_gene_filename("x.py", "0123456789ab")
        assert result == "x-012345.md"


class TestDeriveRelpath:
    def test_with_domain(self):
        assert derive_gene_relpath(
            domain="auth",
            source_id="helix_context/auth/middleware.py",
            gene_id="abc123def456",
        ) == "genes/auth/middleware-abc123.md"

    def test_no_domain_goes_to_orphan(self):
        assert derive_gene_relpath(
            domain=None,
            source_id="x/y.py",
            gene_id="abc123def456",
        ) == "genes/_orphan/y-abc123.md"

    def test_empty_domain_goes_to_orphan(self):
        assert derive_gene_relpath(
            domain="",
            source_id="x/y.py",
            gene_id="abc123def456",
        ) == "genes/_orphan/y-abc123.md"


class TestSafeResolveUnder:
    def test_normal_path_resolves(self, tmp_path: Path):
        target = safe_resolve_under(tmp_path, tmp_path / "genes" / "x.md")
        assert target == (tmp_path / "genes" / "x.md").resolve()

    def test_path_outside_root_raises(self, tmp_path: Path):
        outside = tmp_path / ".." / "etc" / "passwd"
        with pytest.raises(ValueError, match="outside vault root"):
            safe_resolve_under(tmp_path, outside)

    def test_traversal_via_symlink_raises(self, tmp_path: Path):
        # Construct a path that resolves outside via "..", regardless of FS
        candidate = tmp_path / "a" / ".." / ".." / "outside"
        with pytest.raises(ValueError, match="outside vault root"):
            safe_resolve_under(tmp_path, candidate)
