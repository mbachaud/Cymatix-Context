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

    def test_target_outside_vault_raises(self, tmp_path: Path):
        outside = tmp_path / ".." / "evil" / "x.md"
        with pytest.raises(ValueError, match="outside vault root"):
            write_atomic(vault_root=tmp_path, target=outside, content="bad")

    def test_write_atomic_then_compute_disk_hash_roundtrip(self, tmp_path: Path):
        """Confirms writer encoding matches hasher's binary read."""
        target = tmp_path / "x.md"
        content = "hellö 世界\nline2\n"
        write_atomic(vault_root=tmp_path, target=target, content=content)
        h = compute_disk_hash(target)
        assert h == hashlib.sha256(content.encode("utf-8")).hexdigest()


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
        # The exact format must remain stable for downstream parsers.
        assert "[redacted body — sha256=" in md
        # First 16 hex chars of content_sha256 should appear.
        assert gene.content_sha256[:16] in md

    def test_empty_domains_does_not_crash(self, gene):
        gene.domains = []
        md = render_gene_markdown(gene, redact_body=False)
        rest = md[len("---\n"):]
        end = rest.index("---\n")
        fm = yaml.safe_load(rest[:end])
        assert fm["domains"] == []


class TestRenderTraceMarkdown:
    def test_includes_request_id_and_timing(self):
        from helix_context.vault.writer import render_trace_markdown

        md = render_trace_markdown(
            request_id="abc12345",
            created_at="2026-05-06T22:14:06Z",
            expires_at="2026-05-08T22:14:06Z",
            pinned=False,
            trigger_reason="latency_outlier",
            total_latency_ms=18432,
            health_status="sparse",
            stage_timing_ms={
                "extract": 12, "express": 45, "rerank": 12_400,
                "splice": 5_800, "assemble": 175,
            },
            fingerprint_route="(no fingerprint payload)",
            foveated_ranks="(none)",
            final_genes=[("middleware-7f3a1c", 1, 0.92)],
        )
        assert "abc12345" in md
        assert "18432" in md
        assert "rerank" in md
        assert "12_400" in md or "12400" in md
        assert "[[middleware-7f3a1c]]" in md
        assert md.startswith("---\n")

    def test_frontmatter_contains_expires_at(self):
        from helix_context.vault.writer import render_trace_markdown

        md = render_trace_markdown(
            request_id="x", created_at="t1", expires_at="t2",
            pinned=False, trigger_reason="auto",
            total_latency_ms=0, health_status="aligned",
            stage_timing_ms={}, fingerprint_route="", foveated_ranks="",
            final_genes=[],
        )
        rest = md[len("---\n"):]
        end = rest.index("---\n")
        fm = yaml.safe_load(rest[:end])
        assert fm["request_id"] == "x"
        assert fm["expires_at"] == "t2"
        assert fm["pinned"] is False
        # Body sanity: empty stage_timing_ms must NOT emit a header-only table.
        assert "| stage | ms |" not in md
        assert "*(no per-stage data)*" in md
        assert "*(none)*" in md  # fingerprint_route + foveated_ranks both empty
        assert "*(no genes returned)*" in md

    def test_handles_none_and_nan_scores(self):
        from helix_context.vault.writer import render_trace_markdown

        md = render_trace_markdown(
            request_id="x", created_at="t1", expires_at="t2",
            pinned=False, trigger_reason="auto",
            total_latency_ms=0, health_status="aligned",
            stage_timing_ms={}, fingerprint_route="", foveated_ranks="",
            final_genes=[
                ("a-stem", 1, None),
                ("b-stem", 2, float("nan")),
                ("c-stem", 3, 0.75),
            ],
        )
        # None and NaN should both render as 0.00; valid score renders normally.
        assert "[[a-stem]] (rank 1, score 0.00)" in md
        assert "[[b-stem]] (rank 2, score 0.00)" in md
        assert "[[c-stem]] (rank 3, score 0.75)" in md


# ---------------------------------------------------------------------------
# Task 8 — full_export
# ---------------------------------------------------------------------------

def _make_test_gene(content: str, source_id: str, domains=None):
    """Build a test gene with explicit source_id and EUCHROMATIN to survive the density gate."""
    from tests.conftest import make_gene
    from helix_context.schemas import ChromatinState

    g = make_gene(content, domains=domains or [], chromatin=ChromatinState.EUCHROMATIN)
    g.source_id = source_id
    return g


class TestFullExport:
    @pytest.fixture
    def vault_root(self, tmp_path: Path) -> Path:
        return tmp_path / "vault"

    @pytest.fixture
    def genome(self, tmp_path: Path):
        """File-based genome (not in-memory) so VaultLock can live alongside it."""
        from helix_context.genome import Genome

        db_path = tmp_path / "genome.db"
        g = Genome(path=str(db_path))
        yield g
        g.close()

    @pytest.fixture
    def state(self, vault_root: Path):
        from helix_context.vault.state import VaultState

        vault_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        vs = VaultState(vault_root)
        yield vs
        vs.close()

    @pytest.fixture
    def lock(self, vault_root: Path):
        from helix_context.vault.locking import VaultLock

        vault_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return VaultLock(vault_root, timeout=10.0)

    def test_exports_all_genes(self, genome, state, lock, vault_root):
        import time
        from helix_context.vault.writer import full_export

        g1 = _make_test_gene("def foo(): pass", "src/foo.py", domains=["auth"])
        g2 = _make_test_gene("def bar(): pass", "src/bar.py", domains=["db"])
        genome.upsert_gene(g1)
        genome.upsert_gene(g2)
        genome.conn.commit()

        result = full_export(
            genome=genome,
            state=state,
            lock=lock,
            vault_root=vault_root,
            party_id=None,
            redact_body=False,
            fan_out_threshold=50,
            batch_size=500,
        )

        assert result["genes_exported"] == 2
        assert result["errors"] == 0
        # Files must exist somewhere under vault_root/genes/
        gene_files = list(vault_root.glob("genes/**/*.md"))
        assert len(gene_files) == 2

    def test_export_filters_by_party(self, genome, state, lock, vault_root):
        import time
        from helix_context.vault.writer import full_export

        g1 = _make_test_gene("def alpha(): pass", "src/alpha.py", domains=["auth"])
        g2 = _make_test_gene("def beta(): pass", "src/beta.py", domains=["db"])
        gid_a = genome.upsert_gene(g1)
        genome.conn.execute(
            "INSERT INTO gene_attribution (gene_id, party_id, participant_id, authored_at)"
            " VALUES (?, ?, ?, ?)",
            (gid_a, "party_a", "h_a", time.time()),
        )
        gid_b = genome.upsert_gene(g2)
        genome.conn.execute(
            "INSERT INTO gene_attribution (gene_id, party_id, participant_id, authored_at)"
            " VALUES (?, ?, ?, ?)",
            (gid_b, "party_b", "h_b", time.time()),
        )
        genome.conn.commit()

        result = full_export(
            genome=genome,
            state=state,
            lock=lock,
            vault_root=vault_root,
            party_id="party_a",
            redact_body=False,
            fan_out_threshold=50,
            batch_size=500,
        )

        assert result["genes_exported"] == 1
        gene_files = list(vault_root.glob("genes/**/*.md"))
        assert len(gene_files) == 1

    def test_state_records_each_gene(self, genome, state, lock, vault_root):
        from helix_context.vault.writer import full_export

        g1 = _make_test_gene("def record_me(): pass", "src/record.py", domains=["core"])
        gid = genome.upsert_gene(g1)
        genome.conn.commit()

        full_export(
            genome=genome,
            state=state,
            lock=lock,
            vault_root=vault_root,
            party_id=None,
            redact_body=False,
            fan_out_threshold=50,
            batch_size=500,
        )

        record = state.get_record(gid)
        assert record is not None
        assert record.vault_path.startswith("genes/")
        assert record.last_exported_disk_hash is not None

    def test_attribute_error_in_adapter_propagates(self, tmp_path: Path, monkeypatch):
        """Programmer bugs (AttributeError) should NOT be silently swallowed."""
        from helix_context.genome import Genome
        from helix_context.vault import writer as vault_writer
        from helix_context.vault.locking import VaultLock
        from helix_context.vault.state import VaultState

        def broken_adapter(row):
            raise AttributeError("simulated bug in row adapter")

        monkeypatch.setattr(vault_writer, "_row_to_gene", broken_adapter)

        genome = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
        try:
            genome.upsert_gene(_make_test_gene("x", "x.py", ["d"]))
            vault_root = tmp_path / "vault"
            state = VaultState(vault_root=vault_root)
            lock = VaultLock(vault_root=vault_root)
            try:
                with pytest.raises(AttributeError, match="simulated bug"):
                    vault_writer.full_export(
                        genome=genome, state=state, lock=lock,
                        vault_root=vault_root, party_id="",
                        redact_body=False, fan_out_threshold=5000,
                    )
            finally:
                state.close()
        finally:
            genome.close()
