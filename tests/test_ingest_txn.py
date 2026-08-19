"""Issue #372 — one transaction per document + ingest stage timers.

Covers:

- ``KnowledgeStore.deferred_commits()``: N strand upserts + a relations
  batch fold into exactly ONE durable commit (counted by the W0.3
  ``commit_count`` trace hook — the same instrument the perf receipts use).
- Crash mid-document rolls the WHOLE document back — no partial strands.
- A swallowed inner failure (the parent-doc / symbol-graph pattern) rolls
  back only its own SAVEPOINT write unit; prior strands still commit.
- Standalone callers keep the historical commit-per-call behavior.
- Nested scopes join the outermost transaction (still one commit).
- ``CymatixContextManager.ingest()`` drives a multi-chunk document
  (strands + parent + CHUNK_OF edges) through one commit, a crash
  mid-document leaves nothing behind, and the ingest_* stage timers land
  in the pipeline ring with a per-document commit delta.

In-memory genomes; no model calls, no network. The end-to-end ingest
tests reach CpuTagger.pack() and need the spaCy pipeline (#313), same as
the other ingest tests.
"""

from __future__ import annotations

import pytest

from cymatix_context.context_manager import (
    CymatixContextManager,
    get_recent_pipeline_events,
)
from cymatix_context.genome import Genome
from cymatix_context.schemas import StructuralRelation

from tests.conftest import make_cymatix_config, make_gene, requires_spacy_model


# ── Store-level: deferred_commits seam ───────────────────────────────


@pytest.fixture
def store():
    g = Genome(path=":memory:")
    yield g
    g.close()


def _gene_count(store) -> int:
    return store.conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0]


class TestDeferredCommits:
    def test_multi_strand_document_is_one_commit(self, store):
        """5 strand upserts + a CHUNK_OF batch = exactly 1 durable commit."""
        c0 = store.commit_count
        with store.deferred_commits():
            ids = [
                store.upsert_doc(make_gene(f"strand {i} of the document"))
                for i in range(5)
            ]
            store.store_relations_batch([
                (gid, ids[0], int(StructuralRelation.CHUNK_OF), 1.0)
                for gid in ids[1:]
            ])
        assert store.commit_count - c0 == 1
        assert _gene_count(store) == 5
        # The edges landed in the same commit as the strands.
        assert store.conn.execute(
            "SELECT COUNT(*) FROM gene_relations"
        ).fetchone()[0] == 4

    def test_crash_mid_document_leaves_no_partial_strands(self, store):
        """An exception inside the scope rolls the whole document back."""
        c0 = store.commit_count
        with pytest.raises(RuntimeError, match="mid-document"):
            with store.deferred_commits():
                store.upsert_doc(make_gene("strand before the crash"))
                store.upsert_doc(make_gene("second strand before the crash"))
                raise RuntimeError("simulated crash mid-document")
        assert store.commit_count - c0 == 0
        assert _gene_count(store) == 0

    def test_swallowed_inner_failure_keeps_prior_strands(self, store):
        """The parent-doc pattern: an inner unit that fails and is caught
        by the caller must roll back ONLY its own SAVEPOINT — the strands
        already staged still commit at scope exit ("chunks still
        ingested" stays true)."""
        c0 = store.commit_count
        with store.deferred_commits():
            gid = store.upsert_doc(make_gene("strand that must survive"))
            with pytest.raises(Exception):
                # Wrong arity — the executemany inside the unit raises.
                store.store_relations_batch([("bad", "arity")])
        assert store.commit_count - c0 == 1
        assert store.conn.execute(
            "SELECT COUNT(*) FROM genes WHERE gene_id = ?", (gid,)
        ).fetchone()[0] == 1

    def test_standalone_upsert_still_commits_per_call(self, store):
        """No scope, no behavior change: one commit per upsert_doc call."""
        c0 = store.commit_count
        store.upsert_doc(make_gene("standalone strand one"))
        store.upsert_doc(make_gene("standalone strand two"))
        assert store.commit_count - c0 == 2

    def test_nested_scopes_join_outermost(self, store):
        c0 = store.commit_count
        with store.deferred_commits():
            store.upsert_doc(make_gene("outer strand"))
            with store.deferred_commits():
                store.upsert_doc(make_gene("inner strand"))
        assert store.commit_count - c0 == 1
        assert _gene_count(store) == 2

    def test_deferred_data_is_readable_after_exit(self, store):
        with store.deferred_commits():
            gid = store.upsert_doc(make_gene("durable strand"))
        row = store.conn.execute(
            "SELECT content FROM genes WHERE gene_id = ?", (gid,)
        ).fetchone()
        assert row is not None and "durable strand" in row["content"]


# ── End-to-end: CymatixContextManager.ingest ─────────────────────────

# Large enough to chunk into >= 2 strands so the parent-doc + CHUNK_OF
# path runs (the exact strand count is the chunker's business).
MULTI_CHUNK_CONTENT = "\n\n".join(
    f"Section {i}. "
    + ("The ingest write path folds every strand into one transaction. " * 10)
    for i in range(30)
)


@pytest.fixture
def manager():
    mgr = CymatixContextManager(make_cymatix_config())
    yield mgr
    mgr.close()


@requires_spacy_model
class TestIngestOneCommitPerDocument:
    def test_multi_chunk_ingest_is_one_commit(self, manager):
        genome = manager.genome
        c0 = genome.commit_count
        gene_ids = manager.ingest(
            MULTI_CHUNK_CONTENT, content_type="text",
            metadata={"path": "docs/one_txn.md"},
        )
        assert len(gene_ids) >= 2, "content must chunk into multiple strands"
        # Strands + parent doc + CHUNK_OF edges: ONE commit for the lot.
        assert genome.commit_count - c0 == 1
        # And the parent + edges really are there (same commit, not skipped).
        parent_gid = CymatixContextManager._make_parent_doc_id("docs/one_txn.md")
        assert genome.conn.execute(
            "SELECT COUNT(*) FROM genes WHERE gene_id = ?", (parent_gid,)
        ).fetchone()[0] == 1
        assert genome.conn.execute(
            "SELECT COUNT(*) FROM gene_relations WHERE gene_id_b = ? "
            "AND relation = ?",
            (parent_gid, int(StructuralRelation.CHUNK_OF)),
        ).fetchone()[0] == len(gene_ids)

    def test_crash_mid_document_rolls_back_all_strands(self, manager,
                                                       monkeypatch):
        genome = manager.genome
        rows_before = genome.conn.execute(
            "SELECT COUNT(*) FROM genes").fetchone()[0]
        c0 = genome.commit_count

        original = genome.upsert_doc
        calls = {"n": 0}

        def flaky_upsert(gene, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated crash mid-document")
            return original(gene, **kwargs)

        monkeypatch.setattr(genome, "upsert_doc", flaky_upsert)
        with pytest.raises(RuntimeError, match="mid-document"):
            manager.ingest(
                MULTI_CHUNK_CONTENT, content_type="text",
                metadata={"path": "docs/crash.md"},
            )
        assert calls["n"] == 2, "the crash must have hit strand #2"
        # Strand #1 was staged but never committed — nothing landed.
        assert genome.conn.execute(
            "SELECT COUNT(*) FROM genes").fetchone()[0] == rows_before
        assert genome.commit_count - c0 == 0


@requires_spacy_model
class TestIngestStageTimers:
    def test_ring_carries_ingest_stages_and_commit_delta(self, manager):
        manager.ingest(
            MULTI_CHUNK_CONTENT, content_type="text",
            metadata={"path": "docs/timed.md"},
        )
        events = get_recent_pipeline_events(limit=64)
        commits = [e for e in events if e.get("stage") == "ingest_commit"]
        assert commits, "ingest_commit stage entry missing from the ring"
        last = commits[-1]
        rid = last["request_id"]
        stages = {e["stage"] for e in events if e.get("request_id") == rid}
        # encode is if-enabled; the four structural stages are always on.
        assert {"ingest_chunk", "ingest_tag", "ingest_upsert",
                "ingest_commit"} <= stages
        # The commit entry carries the per-document commit delta: exactly
        # one durable commit for the whole document.
        assert last.get("commits") == 1
        # Entries look like every other ring entry (ms/ts present, sane).
        assert last["ms"] >= 0.0 and last["ts"] > 0
