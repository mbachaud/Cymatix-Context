"""Tests for vault.db state tracking."""
from __future__ import annotations

from pathlib import Path

import pytest

from helix_context.vault.state import VaultState


@pytest.fixture
def state(tmp_path: Path) -> VaultState:
    return VaultState(vault_root=tmp_path)


class TestSchemaCreation:
    def test_vault_db_created_on_init(self, tmp_path: Path):
        VaultState(vault_root=tmp_path)
        assert (tmp_path / "vault.db").exists()

    def test_top_level_state_initialized(self, tmp_path: Path):
        s = VaultState(vault_root=tmp_path)
        top = s.read_top_level_state()
        assert top["schema_version"] == 1
        assert top["last_full_export_ts"] == 0.0
        assert top["last_incremental_export_ts"] == 0.0
        assert top["exported_gene_count"] == 0


class TestVaultStateRecord:
    def test_set_and_get_path(self, state):
        state.upsert_record(gene_id="abc123", path="genes/auth/middleware-7f3a1c.md", ts=100.0, disk_hash="aaa")
        rec = state.get_record("abc123")
        assert rec is not None
        assert rec.vault_path == "genes/auth/middleware-7f3a1c.md"
        assert rec.last_exported_ts == 100.0
        assert rec.last_exported_disk_hash == "aaa"

    def test_get_missing_returns_none(self, state):
        assert state.get_record("nope") is None

    def test_upsert_replaces(self, state):
        state.upsert_record(gene_id="abc", path="genes/x.md", ts=1.0, disk_hash="h1")
        state.upsert_record(gene_id="abc", path="genes/y.md", ts=2.0, disk_hash="h2")
        rec = state.get_record("abc")
        assert rec.vault_path == "genes/y.md"
        assert rec.last_exported_ts == 2.0
        assert rec.last_exported_disk_hash == "h2"

    def test_delete_record(self, state):
        state.upsert_record(gene_id="abc", path="genes/x.md", ts=1.0, disk_hash="h")
        state.delete_record("abc")
        assert state.get_record("abc") is None

    def test_iter_all_records(self, state):
        state.upsert_record(gene_id="a", path="genes/a.md", ts=1.0, disk_hash="ha")
        state.upsert_record(gene_id="b", path="genes/b.md", ts=2.0, disk_hash="hb")
        records = list(state.iter_records())
        assert len(records) == 2
        ids = {r.gene_id for r in records}
        assert ids == {"a", "b"}


class TestTopLevelStatePersistence:
    def test_update_persists_across_reload(self, tmp_path: Path):
        s1 = VaultState(vault_root=tmp_path)
        s1.update_top_level_state(last_full_export_ts=999.0, exported_gene_count=42)
        s1.close()

        s2 = VaultState(vault_root=tmp_path)
        top = s2.read_top_level_state()
        assert top["last_full_export_ts"] == 999.0
        assert top["exported_gene_count"] == 42
        assert top["schema_version"] == 1


class TestSchemaVersion:
    def test_version_mismatch_raises(self, tmp_path: Path):
        # Initialize at v1, then write a v999 marker, then reopen
        s = VaultState(vault_root=tmp_path)
        s.close()
        import json
        state_file = tmp_path / ".helix-state.json"
        data = json.loads(state_file.read_text())
        data["schema_version"] = 999
        state_file.write_text(json.dumps(data))
        with pytest.raises(VaultState.SchemaVersionMismatch):
            VaultState(vault_root=tmp_path)
