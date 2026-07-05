"""Unit tests for the two genome tools.

  * benchmarks/build_gold_from_genome.py -- needle generation from a blob genome
  * scripts/shard_to_blob.py             -- sharded -> blob merge, dense preserved

Pure-Python, no GPU / server / network. Uses tiny temp SQLite fixtures.

Run:
    python -m pytest tests/test_genome_tools.py -q --noconftest
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(mod_name: str, rel_path: str):
    """Load a module by file path (the tools aren't importable packages)."""
    spec = importlib.util.spec_from_file_location(
        mod_name, str(REPO_ROOT / rel_path)
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod

build_gold = _load("build_gold_from_genome", "benchmarks/build_gold_from_genome.py")
shard_to_blob = _load("shard_to_blob", "scripts/shard_to_blob.py")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

# Minimal genes-table DDL matching the on-disk contract (storage/ddl.py).
_GENES_DDL = """
CREATE TABLE genes (
    gene_id            TEXT PRIMARY KEY,
    content            TEXT,
    complement         TEXT,
    source_id          TEXT,
    embedding_dense_v2 BLOB
)
"""

_PROMOTER_DDL = """
CREATE TABLE promoter_index (
    gene_id   TEXT,
    tag_type  TEXT,
    tag_value TEXT
)
"""


def _fake_vec(seed: int, dim: int = 8) -> bytes:
    """Little-endian fp32 vector as a BLOB, like embedding_dense_v2."""
    return struct.pack("<%df" % dim, *[float((seed + i) % 7) for i in range(dim)])


def _make_genome(path: Path, rows: list[dict], with_promoter: bool = True) -> None:
    """Create a blob genome with the given gene rows.

    Each row: {gene_id, content, source_id, dense(bool), tags(list)}.
    """
    conn = sqlite3.connect(str(path))
    conn.execute(_GENES_DDL)
    if with_promoter:
        conn.execute(_PROMOTER_DDL)
    for i, r in enumerate(rows):
        vec = _fake_vec(i) if r.get("dense", True) else None
        conn.execute(
            "INSERT INTO genes (gene_id, content, complement, source_id, "
            "embedding_dense_v2) VALUES (?, ?, ?, ?, ?)",
            (r["gene_id"], r["content"], r.get("complement", ""),
             r["source_id"], vec),
        )
        if with_promoter:
            for tag in r.get("tags", []):
                conn.execute(
                    "INSERT INTO promoter_index VALUES (?, 'domain', ?)",
                    (r["gene_id"], tag),
                )
    conn.commit()
    conn.close()


# ===========================================================================
# build_gold_from_genome
# ===========================================================================

def test_source_prefix_peels_drive_and_sources_container():
    p = build_gold.source_prefix
    assert p(r"F:\tmp\enterprise_rag_500k\sources\github\pr-1.json") == "github"
    assert p("F:/Projects/helix-context/helix_context/config.py") == "Projects"
    assert p("C:/tmp/erb/sources/gmail/alex/m.txt") == "gmail"
    assert p("") == "_unknown"


def test_build_gold_emits_needles_with_required_schema(tmp_path):
    genome = tmp_path / "blob.genome.db"
    rows = [
        # Source-prefix "alpha" (top path segment after the drive)
        {"gene_id": "a1", "source_id": r"F:\alpha\authentication_handler.py",
         "content": "The retry policy uses exponential backoff to recover from "
                    "transient upstream failures before surfacing an error to the caller."},
        {"gene_id": "a2", "source_id": r"F:\alpha\billing_engine.py",
         "content": "Invoices are reconciled nightly against the ledger and any "
                    "discrepancy greater than one cent triggers a manual review queue."},
        {"gene_id": "a3", "source_id": r"F:\alpha\notes.md",
         "content": "# Onboarding\n\nNew team members should request access to the "
                    "staging cluster and read the deployment runbook before shipping."},
        # Source-prefix "beta"
        {"gene_id": "b1", "source_id": r"F:\beta\report_builder.py",
         "content": "Quarterly summaries aggregate revenue by region and present "
                    "the top performing segments in a sortable dashboard table."},
        {"gene_id": "b2", "source_id": r"F:\beta\cache_layer.py",
         "content": "Entries expire after the configured window and a background "
                    "sweep evicts cold keys to keep memory pressure bounded."},
    ]
    _make_genome(genome, rows)

    out = tmp_path / "gold.jsonl"
    rc = build_gold.main([
        "--genome", str(genome),
        "--per-source", "5",
        "--out", str(out),
        "--seed", "7",
        "--min-content-chars", "50",
    ])
    assert rc == 0
    assert out.exists()

    needles = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert needles, "expected at least one needle"

    required = {"id", "project", "type", "file_type", "question", "gold_paths"}
    prefixes = set()
    for nd in needles:
        assert required <= set(nd), "missing schema fields: {}".format(required - set(nd))
        assert nd["type"] in ("within", "cross")
        assert isinstance(nd["gold_paths"], list) and nd["gold_paths"]
        assert isinstance(nd["question"], str) and len(nd["question"]) >= 40
        prefixes.add(nd["project"])

    # Both source-prefixes represented across the within-needles.
    within_prefixes = {nd["project"] for nd in needles if nd["type"] == "within"}
    assert {"alpha", "beta"} <= within_prefixes

    # ~20% cross tagging present.
    assert any(nd["type"] == "cross" for nd in needles)


def test_build_gold_no_leak_guard_strips_path_and_filename_tokens(tmp_path):
    genome = tmp_path / "blob.genome.db"
    # The content deliberately echoes the filename stem + dir tokens + a
    # snake_case symbol -- all must be scrubbed out of the question.
    rows = [{
        "gene_id": "x1",
        "source_id": r"F:\proj\widgetkit\frobnicator_service.py",
        "content": (
            "The frobnicator_service in widgetkit exposes a frobnicator_service "
            "endpoint. The frobnicator handler validates the payload and then "
            "schedules a background reconciliation job for downstream consumers."
        ),
    }]
    _make_genome(genome, rows)

    out = tmp_path / "gold.jsonl"
    rc = build_gold.main([
        "--genome", str(genome), "--per-source", "1",
        "--out", str(out), "--seed", "1", "--min-content-chars", "50",
    ])
    assert rc == 0
    needles = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
    within = [n for n in needles if n["type"] == "within"]
    assert within
    q = within[0]["question"].lower()

    # Leak tokens that MUST be absent from the question.
    for leaked in ("widgetkit", "frobnicator_service", "frobnicator", ".py"):
        assert leaked not in q, "no-leak guard failed: '{}' leaked into {!r}".format(leaked, q)

    # gold_paths still carries the real source path (that's the recall target).
    assert within[0]["gold_paths"] == [rows[0]["source_id"]]


def test_build_gold_opens_genome_read_only(tmp_path):
    genome = tmp_path / "blob.genome.db"
    _make_genome(genome, [{
        "gene_id": "r1", "source_id": r"F:\proj\zeta\service.py",
        "content": "The scheduler dispatches jobs to workers and retries failed "
                   "tasks with a capped jitter to avoid thundering-herd spikes.",
    }])
    conn = build_gold._open_ro(str(genome))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO genes (gene_id) VALUES ('nope')")
    finally:
        conn.close()


# ===========================================================================
# shard_to_blob
# ===========================================================================

def _make_sharded_tree(root: Path):
    """Build main.genome.db + two content shards; return (counts, dense_counts)."""
    root.mkdir(parents=True, exist_ok=True)

    # main.genome.db -- routing DB, no content tables (must be ignored).
    main = sqlite3.connect(str(root / "main.genome.db"))
    main.execute(
        "CREATE TABLE shards (shard_name TEXT PRIMARY KEY, gene_count INTEGER)"
    )
    main.execute("INSERT INTO shards VALUES ('alpha', 2), ('beta', 3)")
    main.commit()
    main.close()

    # shard alpha: 2 genes, both dense.
    alpha_dir = root / "F" / "proj" / "alpha"
    alpha_dir.mkdir(parents=True, exist_ok=True)
    _make_genome(alpha_dir / "alpha.genome.db", [
        {"gene_id": "g_a1", "source_id": r"F:\proj\alpha\a.py",
         "content": "alpha one content", "tags": ["auth"]},
        {"gene_id": "g_a2", "source_id": r"F:\proj\alpha\b.py",
         "content": "alpha two content", "tags": ["cache"]},
    ])

    # shard beta: 3 genes; one of them has NO dense vector.
    beta_dir = root / "F" / "proj" / "beta"
    beta_dir.mkdir(parents=True, exist_ok=True)
    _make_genome(beta_dir / "beta.genome.db", [
        {"gene_id": "g_b1", "source_id": r"F:\proj\beta\x.py",
         "content": "beta one content", "tags": ["report"]},
        {"gene_id": "g_b2", "source_id": r"F:\proj\beta\y.py",
         "content": "beta two content", "dense": False, "tags": []},
        {"gene_id": "g_b3", "source_id": r"F:\proj\beta\z.py",
         "content": "beta three content", "tags": ["queue"]},
    ])

    return {"genes": 5, "dense": 4}


def test_discover_shards_excludes_main(tmp_path):
    expected = _make_sharded_tree(tmp_path)
    shards = shard_to_blob.discover_shards(str(tmp_path))
    names = {os.path.basename(s) for s in shards}
    assert names == {"alpha.genome.db", "beta.genome.db"}
    assert all("main.genome.db" not in s for s in shards)


def test_shard_to_blob_merge_counts_and_dense_preserved(tmp_path):
    expected = _make_sharded_tree(tmp_path)
    out = tmp_path / "merged_blob.genome.db"

    summary = shard_to_blob.shard_to_blob(str(tmp_path), str(out))

    # Gene count == sum of shard gene counts (no collisions here).
    assert summary["expected_genes_sum"] == expected["genes"]
    assert summary["blob_genes"] == expected["genes"]
    assert summary["genes_ok"] is True

    # Dense vectors preserved verbatim -- non-null count matches.
    assert summary["expected_dense_sum"] == expected["dense"]
    assert summary["blob_dense"] == expected["dense"]
    assert summary["dense_ok"] is True

    # Verify directly against the produced blob.
    conn = sqlite3.connect(str(out))
    try:
        assert conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0] == 5
        assert conn.execute(
            "SELECT COUNT(*) FROM genes WHERE embedding_dense_v2 IS NOT NULL"
        ).fetchone()[0] == 4
        # FTS rebuilt and queryable.
        assert conn.execute("SELECT COUNT(*) FROM genes_fts").fetchone()[0] == 5
        hit = conn.execute(
            "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH 'beta'"
        ).fetchall()
        assert len(hit) == 3
        # The actual vector BLOB is byte-identical to the source shard's.
        src = sqlite3.connect(_uri_ro(tmp_path / "F" / "proj" / "alpha" / "alpha.genome.db"), uri=True)
        try:
            src_vec = src.execute(
                "SELECT embedding_dense_v2 FROM genes WHERE gene_id='g_a1'"
            ).fetchone()[0]
        finally:
            src.close()
        blob_vec = conn.execute(
            "SELECT embedding_dense_v2 FROM genes WHERE gene_id='g_a1'"
        ).fetchone()[0]
        assert blob_vec == src_vec and blob_vec is not None
    finally:
        conn.close()


def _uri_ro(path: Path) -> str:
    return "file:{}?mode=ro".format(str(path).replace("\\", "/"))


def test_shard_to_blob_dedupes_gene_id_collisions(tmp_path):
    """Two shards sharing an identical gene_id should de-dupe defensively."""
    root = tmp_path
    (root).mkdir(parents=True, exist_ok=True)
    main = sqlite3.connect(str(root / "main.genome.db"))
    main.execute("CREATE TABLE shards (shard_name TEXT PRIMARY KEY)")
    main.close()

    s1 = root / "one"
    s1.mkdir()
    _make_genome(s1 / "one.genome.db", [
        {"gene_id": "dup", "source_id": "F:/a/dup.py", "content": "shared content"},
        {"gene_id": "u1", "source_id": "F:/a/u1.py", "content": "unique one"},
    ])
    s2 = root / "two"
    s2.mkdir()
    _make_genome(s2 / "two.genome.db", [
        {"gene_id": "dup", "source_id": "F:/a/dup.py", "content": "shared content"},
        {"gene_id": "u2", "source_id": "F:/b/u2.py", "content": "unique two"},
    ])

    out = root / "blob.genome.db"
    summary = shard_to_blob.shard_to_blob(str(root), str(out))

    # 4 rows across shards, 1 collision -> 3 unique genes.
    assert summary["expected_genes_sum"] == 4
    assert summary["blob_genes"] == 3
    assert summary["deduped_collisions"] == 1
    assert summary["genes_ok"] is True
    assert summary["dense_ok"] is True


def test_shard_to_blob_reads_shards_with_live_wal_sidecars(tmp_path):
    """Shards left in WAL mode (live -wal/-shm sidecars) must still merge.

    Real on-disk shards are frequently in WAL journal mode with uncheckpointed
    sidecars; a naive mode=ro open fails with "disk I/O error". The tool opens
    sources with immutable=1, which reads the main DB file directly. This test
    leaves a shard in WAL mode (sidecars present) and asserts the merge works.
    """
    root = tmp_path
    main = sqlite3.connect(str(root / "main.genome.db"))
    main.execute("CREATE TABLE shards (shard_name TEXT PRIMARY KEY)")
    main.close()

    sdir = root / "s"
    sdir.mkdir()
    shard_path = sdir / "wal.genome.db"
    _make_genome(shard_path, [
        {"gene_id": "w1", "source_id": "F:/a/w1.py", "content": "wal gene one"},
        {"gene_id": "w2", "source_id": "F:/a/w2.py", "content": "wal gene two"},
    ])
    # Force the shard into WAL mode and leave an uncheckpointed sidecar open.
    keep = sqlite3.connect(str(shard_path))
    keep.execute("PRAGMA journal_mode=WAL")
    keep.execute(
        "UPDATE genes SET content = content || ' edited' WHERE gene_id = 'w1'"
    )
    keep.commit()
    # Do NOT checkpoint or close yet -- sidecars are live on disk.
    assert (sdir / "wal.genome.db-wal").exists()

    out = root / "blob.genome.db"
    summary = shard_to_blob.shard_to_blob(str(root), str(out))
    keep.close()

    assert summary["blob_genes"] == 2
    assert summary["dense_ok"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "--noconftest"]))
