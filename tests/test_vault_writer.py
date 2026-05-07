# tests/test_vault_writer.py
"""Tests for vault writer — atomic writes + gene rendering."""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from helix_context.vault.writer import (
    compute_disk_hash,
    render_gene_markdown,
    write_atomic,
)


class TestWriteAtomic:
    def test_writes_file(self, tmp_path: Path):
        target = tmp_path / "out.md"
        write_atomic(vault_root=tmp_path, target=target, content="hello")
        assert target.read_text() == "hello"

    def test_no_tmp_left_behind(self, tmp_path: Path):
        target = tmp_path / "out.md"
        write_atomic(vault_root=tmp_path, target=target, content="hello")
        assert not target.with_suffix(".md.tmp").exists()

    def test_no_sentinel_left_behind(self, tmp_path: Path):
        target = tmp_path / "out.md"
        write_atomic(vault_root=tmp_path, target=target, content="hello")
        assert not (tmp_path / ".helix-syncing").exists()

    def test_creates_parent_dirs(self, tmp_path: Path):
        target = tmp_path / "genes" / "auth" / "x.md"
        write_atomic(vault_root=tmp_path, target=target, content="hi")
        assert target.exists()


class TestComputeDiskHash:
    def test_returns_sha256_hex(self, tmp_path: Path):
        f = tmp_path / "x.md"
        f.write_text("hello")
        h = compute_disk_hash(f)
        assert h == hashlib.sha256(b"hello").hexdigest()

    def test_handles_unicode(self, tmp_path: Path):
        f = tmp_path / "x.md"
        f.write_text("hellö 世界", encoding="utf-8")
        h = compute_disk_hash(f)
        assert h == hashlib.sha256("hellö 世界".encode("utf-8")).hexdigest()


class TestRenderGeneMarkdown:
    @pytest.fixture
    def gene(self):
        return SimpleNamespace(
            gene_id="abc123def456",
            content="def hello():\n    return 'world'\n",
            content_type="code",
            source_id="helix_context/auth/middleware.py",
            source_lines="42-89",
            domains=["auth", "jwt"],
            chromatin="euchromatin",
            content_sha256="7f3a1c000000000000000000000000000000000000000000000000000000000",
            last_seen="2026-05-06T20:45:00Z",
            last_seen_ts=1736198700.0,
            live_truth_score=0.92,
            co_activation_partners=7,
            party_id="swift_wing21",
            participant_handle="laude",
        )

    def test_produces_yaml_frontmatter(self, gene):
        md = render_gene_markdown(gene, redact_body=False)
        assert md.startswith("---\n")
        rest = md[len("---\n"):]
        end = rest.index("---\n")
        fm_text = rest[:end]
        fm = yaml.safe_load(fm_text)
        assert fm["gene_id"] == "abc123def456"
        assert fm["chromatin"] == "euchromatin"
        assert fm["domains"] == ["auth", "jwt"]
        assert fm["live_truth_score"] == 0.92

    def test_includes_authored_placeholders(self, gene):
        md = render_gene_markdown(gene, redact_body=False)
        rest = md[len("---\n"):]
        end = rest.index("---\n")
        fm = yaml.safe_load(rest[:end])
        assert fm["operator_notes"] == ""
        assert fm["operator_tags"] == []
        assert fm["pinned"] is False
        assert fm["supersedes"] == []

    def test_body_includes_content(self, gene):
        md = render_gene_markdown(gene, redact_body=False)
        assert "def hello():" in md
        assert "return 'world'" in md

    def test_body_includes_typed_edges_section(self, gene):
        md = render_gene_markdown(gene, redact_body=False)
        assert "## Typed edges" in md
        assert "v1.1" in md or "(none yet" in md

    def test_redact_body_replaces_with_summary(self, gene):
        md = render_gene_markdown(gene, redact_body=True)
        assert "def hello():" not in md
        body_sha = "[redacted body]"
        assert body_sha in md or "redacted" in md.lower()

    def test_empty_domains_does_not_crash(self, gene):
        gene.domains = []
        md = render_gene_markdown(gene, redact_body=False)
        rest = md[len("---\n"):]
        end = rest.index("---\n")
        fm = yaml.safe_load(rest[:end])
        assert fm["domains"] == []
