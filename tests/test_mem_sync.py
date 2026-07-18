"""
Tests for helix_context.mem_sync — bugbash regressions.

BUG-1a: all syncers share one global state file (~/.helix/mem_sync_state.json)
        but deletion detection considered only the *current invocation's*
        watch dirs, so agent A's sync pass tombstoned + dropped agent B's
        tracking entries. Deletion detection must be scoped to entries that
        live under a watch dir actually scanned this pass.
BUG-1b: the tombstone call targeted /admin/genes/tombstone which did not
        exist server-side — deleted memories stayed retrievable forever.
        The route now exists and demotes matching genes to heterochromatin.
BUG-2:  the syncer's 4-layer identity env (HELIX_USER / HELIX_AGENT /
        HELIX_DEVICE / HELIX_ORG) was never forwarded to /ingest; the
        server resolved attribution from its OWN process env, so provenance
        fell back to the server's identity. The syncer must forward its
        identity explicitly in the /ingest payload.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import helix_context.mem_sync as mem_sync


@pytest.fixture(autouse=True)
def _no_state_io(monkeypatch):
    """Never touch the real ~/.helix/mem_sync_state.json from tests."""
    monkeypatch.setattr(mem_sync, "_save_state", lambda state: None)
    monkeypatch.setattr(mem_sync, "_load_state", lambda: {})


# ── BUG-1a: deletion detection scoped to scanned watch dirs ──────────

class TestScopedDeletion:
    def test_foreign_entries_survive_other_agents_pass(self, tmp_path, monkeypatch):
        """A sync pass over agent A's dir must not delete agent B's
        tracking entries (shared global state file)."""
        mine = tmp_path / "agent_a"
        mine.mkdir()
        (mine / "keep.md").write_text("keep body", encoding="utf-8")

        other = tmp_path / "agent_b"
        other.mkdir()
        foreign_key = str((other / "foreign.md").resolve())
        state = {foreign_key: "deadbeef"}

        tombstoned: list = []
        monkeypatch.setattr(
            mem_sync, "_tombstone_file",
            lambda url, p: tombstoned.append(str(p)),
        )
        monkeypatch.setattr(
            mem_sync, "_ingest_file", lambda *a, **k: ["g1"],
        )

        counters = mem_sync.sync_once([str(mine)], "http://127.0.0.1:1", state=state)

        assert counters["deleted"] == 0
        assert foreign_key in state, (
            "BUG-1a: sync over agent A's dirs deleted agent B's state entry"
        )
        assert tombstoned == []

    def test_deletion_detected_in_own_watch_dir(self, tmp_path, monkeypatch):
        """A file gone from a dir we DID scan this pass is still detected."""
        mine = tmp_path / "agent_a"
        mine.mkdir()
        gone_key = str((mine / "gone.md").resolve())
        state = {gone_key: "cafebabe"}

        tombstoned: list = []
        monkeypatch.setattr(
            mem_sync, "_tombstone_file",
            lambda url, p: tombstoned.append(str(p)),
        )
        monkeypatch.setattr(
            mem_sync, "_ingest_file", lambda *a, **k: ["g1"],
        )

        counters = mem_sync.sync_once([str(mine)], "http://127.0.0.1:1", state=state)

        assert counters["deleted"] == 1
        assert gone_key not in state
        assert tombstoned == [gone_key]

    def test_missing_watch_dir_entries_survive(self, tmp_path, monkeypatch):
        """A watch dir that is temporarily missing (unmounted share, moved
        checkout) must not tombstone everything it used to contain."""
        missing = tmp_path / "not_there"
        stale_key = str((missing / "note.md").resolve())
        state = {stale_key: "feedface"}

        tombstoned: list = []
        monkeypatch.setattr(
            mem_sync, "_tombstone_file",
            lambda url, p: tombstoned.append(str(p)),
        )

        counters = mem_sync.sync_once([str(missing)], "http://127.0.0.1:1", state=state)

        assert counters["deleted"] == 0
        assert stale_key in state
        assert tombstoned == []

    def test_state_key_matches_ingest_path(self, tmp_path, monkeypatch):
        """The path we ingest under must equal the state key, so the
        tombstone source_id later matches what the gene stored."""
        mine = tmp_path / "agent_a"
        mine.mkdir()
        md = mine / "note.md"
        md.write_text("note body", encoding="utf-8")

        seen: list = []

        def fake_ingest(url, path, content, fields, agent_kind=None):
            seen.append(str(path))
            return ["g1"]

        monkeypatch.setattr(mem_sync, "_ingest_file", fake_ingest)
        monkeypatch.setattr(mem_sync, "_tombstone_file", lambda url, p: None)

        state: dict = {}
        mem_sync.sync_once([str(mine)], "http://127.0.0.1:1", state=state)

        assert list(state.keys()) == seen


# ── BUG-2: syncer identity forwarded to /ingest ──────────────────────

class _FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class TestIdentityForwarding:
    def _capture_urlopen(self, monkeypatch, body=None):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _FakeResponse(body or {"gene_ids": ["g1"]})

        monkeypatch.setattr(mem_sync.urllib.request, "urlopen", fake_urlopen)
        return captured

    def test_ingest_forwards_syncer_identity_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HELIX_USER", "max")
        monkeypatch.setenv("HELIX_AGENT", "raude")
        monkeypatch.setenv("HELIX_DEVICE", "gandalf")
        monkeypatch.setenv("HELIX_ORG", "helixorg")
        captured = self._capture_urlopen(monkeypatch)

        md = tmp_path / "note.md"
        md.write_text("body", encoding="utf-8")
        gene_ids = mem_sync._ingest_file(
            "http://127.0.0.1:1", md, "body", {}, agent_kind="claude-code",
        )

        assert gene_ids == ["g1"]
        payload = captured["payload"]
        assert payload["participant_handle"] == "max", (
            "BUG-2: HELIX_USER not forwarded — provenance falls back to "
            "the server's identity"
        )
        assert payload["agent_handle"] == "raude"
        assert payload["party_id"] == "gandalf"
        assert payload["org_id"] == "helixorg"
        assert payload["agent_kind"] == "claude-code"

    def test_ingest_omits_unset_identity_fields(self, tmp_path, monkeypatch):
        for var in ("HELIX_USER", "HELIX_AGENT", "HELIX_DEVICE",
                    "HELIX_PARTY", "HELIX_ORG"):
            monkeypatch.delenv(var, raising=False)
        captured = self._capture_urlopen(monkeypatch)

        md = tmp_path / "note.md"
        md.write_text("body", encoding="utf-8")
        mem_sync._ingest_file("http://127.0.0.1:1", md, "body", {})

        payload = captured["payload"]
        for field in ("participant_handle", "agent_handle", "party_id", "org_id"):
            assert field not in payload, (
                f"unset identity must not ship as null: {field}"
            )
        # Server-side defaulting still applies when nothing is forwarded.
        assert "content" in payload and "metadata" in payload


# ── BUG-1b: tombstone hits a real route ──────────────────────────────

class TestTombstoneRoute:
    @pytest.fixture
    def client(self):
        from tests.conftest import make_client

        return make_client()

    @staticmethod
    def _seed_gene(genome, gene_id: str, source_id: str):
        from helix_context.schemas import (
            ChromatinState,
            EpigeneticMarkers,
            Gene,
            PromoterTags,
        )

        gene = Gene(
            gene_id=gene_id,
            content=f"memory body for {gene_id}",
            complement="",
            codons=[],
            promoter=PromoterTags(domains=["memory"], entities=[]),
            epigenetics=EpigeneticMarkers(),
            chromatin=ChromatinState.OPEN,
            is_fragment=False,
            source_id=source_id,
        )
        genome.upsert_gene(gene, apply_gate=False)

    def test_tombstone_demotes_matching_genes(self, client):
        genome = client.app.state.helix.genome
        self._seed_gene(genome, "tomb-1", "C:/mem/feedback.md")
        self._seed_gene(genome, "tomb-2", "C:/mem/feedback.md")
        self._seed_gene(genome, "keep-1", "C:/mem/other.md")

        resp = client.post(
            "/admin/genes/tombstone",
            json={"source_id": "C:/mem/feedback.md"},
        )
        assert resp.status_code == 200, (
            "BUG-1b: /admin/genes/tombstone does not exist — deleted "
            "memories stay retrievable"
        )
        body = resp.json()
        assert body["tombstoned"] == 2
        assert sorted(body["gene_ids"]) == ["tomb-1", "tomb-2"]

        rows = genome.conn.execute(
            "SELECT gene_id, chromatin FROM genes ORDER BY gene_id",
        ).fetchall()
        tiers = {r["gene_id"]: r["chromatin"] for r in rows}
        assert tiers["tomb-1"] == 2
        assert tiers["tomb-2"] == 2
        assert tiers["keep-1"] != 2, "tombstone must be scoped to source_id"

    def test_tombstone_requires_source_id(self, client):
        resp = client.post("/admin/genes/tombstone", json={})
        assert resp.status_code == 400

    def test_tombstone_no_match_is_ok(self, client):
        resp = client.post(
            "/admin/genes/tombstone",
            json={"source_id": "C:/mem/never-ingested.md"},
        )
        assert resp.status_code == 200
        assert resp.json()["tombstoned"] == 0

    def test_mem_sync_tombstone_posts_state_path_then_alias(self, tmp_path, monkeypatch):
        """_tombstone_file must target the real route with the source_id
        the gene actually stored (the absolute path — metadata["path"]
        wins over metadata["source_id"] at ingest), falling back to the
        legacy mem:// alias."""
        calls: list = []

        def fake_urlopen(req, timeout=None):
            calls.append(json.loads(req.data.decode("utf-8"))["source_id"])
            assert req.full_url.endswith("/admin/genes/tombstone")
            assert timeout is not None
            # First candidate misses, second hits.
            hit = {"tombstoned": 1 if len(calls) == 2 else 0, "gene_ids": []}
            return _FakeResponse(hit)

        monkeypatch.setattr(mem_sync.urllib.request, "urlopen", fake_urlopen)

        path = tmp_path / "gone.md"
        assert mem_sync._tombstone_file("http://127.0.0.1:1", path) is True
        assert calls == [str(path), f"mem://{path.name}"]
