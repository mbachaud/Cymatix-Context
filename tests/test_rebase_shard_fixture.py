"""Tests for scripts/rebase_shard_fixture.py — the shard-fixture relocation
tool ("needle adapter", cc-exchange spark-erb-receipts 0007/0008).

Scenario under test: a sharded fixture set (coordinator ``main.genome.db`` +
shard ``.db`` files) is built on one host, then consumed on a foreign host
where every ``shards.path`` recorded in the coordinator is an absolute path
from the origin box. ShardRouter._open_shard finds nothing, every query
returns empty with rc=0 — the exact ``CELL3_UNSUPPORTED`` signature from
spark-erb-receipts 0007.

Covered:
    (a) pre-adapter: the router serves ZERO genes against the broken fixture
        (reproduces the foreign-host signature — delivery 0, pool 0, no error)
    (b) post-adapter: paths rewritten, serve verification counts genes > 0,
        and the smoke gate passes (nonzero delivery AND nonzero pool)
    (c) --dry-run is side-effect-free: prints the plan, touches nothing
    (d) hard-fail diagnostics: empty coordinator (no shard rows) and a
        recorded shard whose file is absent from the new root both exit
        nonzero with distinct codes
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from rebase_shard_fixture import (  # noqa: E402
    EXIT_COORDINATOR_INVALID,
    EXIT_NO_SHARD_ROWS,
    EXIT_SERVE_FAILED,
    EXIT_SMOKE_FAILED,
    EXIT_UNMATCHED_SHARDS,
    main as rebase_main,
)

from cymatix_context.genome import Genome
from cymatix_context.schemas import (
    ChromatinState,
    EpigeneticMarkers,
    Gene,
    PromoterTags,
)
from cymatix_context.shard_schema import (
    init_main_db,
    open_main_db,
    register_shard,
    upsert_fingerprint,
)
from cymatix_context.sharding import ShardedGenomeAdapter


def _mk_gene(content: str, domains: list[str], entities: list[str], source: str) -> Gene:
    return Gene(
        gene_id="",
        content=content,
        complement=content[:50],
        codons=[],
        promoter=PromoterTags(domains=domains, entities=entities, sequence_index=0),
        epigenetics=EpigeneticMarkers(),
        chromatin=ChromatinState.OPEN,
        is_fragment=False,
        source_id=source,
    )


def _close_genome(g: Genome) -> None:
    g.conn.close()
    if g._reader:
        g._reader.close()


@pytest.fixture
def relocated_fixture(tmp_path: Path):
    """Build a 2-shard fixture whose coordinator records FOREIGN paths.

    Layout on disk (the "new host" root):
        <root>/main.genome.db
        <root>/F/Projects/alpha/alpha.genome.db
        <root>/F/Projects/beta/beta.genome.db

    Coordinator ``shards.path`` values point at an "origin-e92c" prefix —
    the origin host's absolute layout — so the router can serve nothing
    until the adapter runs. The prefix lives under tmp_path because a
    per-shard Genome AUTO-CREATES a missing db (mkdir + empty schema):
    probing a foreign path materialises an empty shard there, serving
    zero with rc=0 — the exact 0007 failure mode — and the test must
    keep that side effect inside the sandbox.
    """
    foreign_prefix = (tmp_path / "origin-e92c" / "beds" / "fixture").as_posix()
    root = tmp_path / "fixture"
    a_dir = root / "F" / "Projects" / "alpha"
    b_dir = root / "F" / "Projects" / "beta"
    a_dir.mkdir(parents=True)
    b_dir.mkdir(parents=True)
    a_path = a_dir / "alpha.genome.db"
    b_path = b_dir / "beta.genome.db"

    ga = Genome(str(a_path))
    gene_a = _mk_gene(
        "Helix retrieval pipeline design. Splice and assemble context.",
        domains=["retrieval"],
        entities=["helix"],
        source="/docs/pipeline.md",
    )
    gene_a_id = ga.upsert_gene(gene_a, apply_gate=False)
    _close_genome(ga)

    gb = Genome(str(b_path))
    gene_b = _mk_gene(
        "Auth module notes. JWT sessions and retrieval of tokens.",
        domains=["auth", "retrieval"],
        entities=["jwt"],
        source="/code/auth.py",
    )
    gene_b_id = gb.upsert_gene(gene_b, apply_gate=False)
    _close_genome(gb)

    main_path = root / "main.genome.db"
    main = open_main_db(str(main_path))
    init_main_db(main)
    # Record FOREIGN absolute paths — the origin host's layout.
    register_shard(
        main, "alpha", "reference",
        f"{foreign_prefix}/F/Projects/alpha/alpha.genome.db", gene_count=1,
    )
    register_shard(
        main, "beta", "reference",
        f"{foreign_prefix}/F/Projects/beta/beta.genome.db", gene_count=1,
    )
    upsert_fingerprint(
        main, gene_id=gene_a_id, shard_name="alpha",
        source_id="/docs/pipeline.md",
        domains_json=json.dumps(["retrieval"]),
        entities_json=json.dumps(["helix"]),
        key_values_json="[]",
    )
    upsert_fingerprint(
        main, gene_id=gene_b_id, shard_name="beta",
        source_id="/code/auth.py",
        domains_json=json.dumps(["auth", "retrieval"]),
        entities_json=json.dumps(["jwt"]),
        key_values_json="[]",
    )
    main.close()

    return {
        "root": root,
        "main_path": main_path,
        "a_path": a_path,
        "b_path": b_path,
    }


def _query_signature(main_path: Path) -> tuple[int, int]:
    """(delivery, pool) for a probe query through the real serving path."""
    adapter = ShardedGenomeAdapter(main_path=str(main_path))
    try:
        genes = adapter.query_docs(
            domains=["retrieval"], entities=[], max_genes=8, read_only=True,
        )
        return len(genes), len(adapter.last_query_scores)
    finally:
        adapter.close()


def test_pre_adapter_router_serves_zero(relocated_fixture):
    """(a) Reproduce Joe's signature: foreign paths -> 0 delivery / 0 pool,
    no exception raised anywhere (rc would be 0)."""
    delivery, pool = _query_signature(relocated_fixture["main_path"])
    assert delivery == 0
    assert pool == 0


def test_dry_run_is_side_effect_free(relocated_fixture, capsys):
    """(c) --dry-run prints the rewrite plan and changes nothing."""
    main_path = relocated_fixture["main_path"]
    before = sqlite3.connect(str(main_path))
    rows_before = sorted(
        before.execute("SELECT shard_name, path, updated_at FROM shards").fetchall()
    )
    before.close()

    rc = rebase_main([str(relocated_fixture["root"]), "--dry-run"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "alpha.genome.db" in out
    assert "beta.genome.db" in out
    assert "origin-e92c" in out  # the old (foreign) path shows in the plan

    after = sqlite3.connect(str(main_path))
    rows_after = sorted(
        after.execute("SELECT shard_name, path, updated_at FROM shards").fetchall()
    )
    after.close()
    assert rows_before == rows_after
    # Still serves zero — dry-run must not have fixed anything.
    delivery, pool = _query_signature(main_path)
    assert delivery == 0 and pool == 0


def test_rebase_restores_serving_and_smoke_gate_passes(relocated_fixture, capsys):
    """(b) Full run: paths rewritten to the new root, serve verification
    reports genes, smoke gate green, rc=0."""
    rc = rebase_main([str(relocated_fixture["root"])])
    out = capsys.readouterr().out
    assert rc == 0, out

    conn = sqlite3.connect(str(relocated_fixture["main_path"]))
    paths = {
        r[0]: r[1]
        for r in conn.execute("SELECT shard_name, path FROM shards").fetchall()
    }
    conn.close()
    assert Path(paths["alpha"]) == relocated_fixture["a_path"].resolve()
    assert Path(paths["beta"]) == relocated_fixture["b_path"].resolve()

    delivery, pool = _query_signature(relocated_fixture["main_path"])
    assert delivery > 0
    assert pool > 0
    # Receipts present in the output: serve count + per-arm gate lines.
    assert "gene rows served" in out
    assert "SMOKE GATE" in out


def test_rebase_is_idempotent(relocated_fixture):
    """Second run over an already-rebased fixture is a no-op green run."""
    assert rebase_main([str(relocated_fixture["root"])]) == 0
    assert rebase_main([str(relocated_fixture["root"])]) == 0


def test_verify_only_fails_on_broken_fixture(relocated_fixture):
    """--verify-only on the un-rebased fixture must exit nonzero (this is
    the pre-run gate that Joe's 'code path executes' check missed)."""
    rc = rebase_main([str(relocated_fixture["root"]), "--verify-only"])
    assert rc == EXIT_SERVE_FAILED


def test_verify_only_passes_after_rebase(relocated_fixture):
    assert rebase_main([str(relocated_fixture["root"])]) == 0
    assert rebase_main([str(relocated_fixture["root"]), "--verify-only"]) == 0


def test_empty_coordinator_hard_fails(tmp_path: Path):
    """(d) A coordinator with zero shard rows is Joe's empty-coordinator
    case — distinct exit code + diagnostic, never rc=0."""
    root = tmp_path / "empty"
    root.mkdir()
    main = open_main_db(str(root / "main.genome.db"))
    init_main_db(main)
    main.close()
    rc = rebase_main([str(root)])
    assert rc == EXIT_NO_SHARD_ROWS


def test_missing_coordinator_hard_fails(tmp_path: Path):
    root = tmp_path / "nothing"
    root.mkdir()
    rc = rebase_main([str(root)])
    assert rc == EXIT_COORDINATOR_INVALID


def test_unmatched_shard_hard_fails(relocated_fixture):
    """(d) A recorded shard whose file is absent under the new root fails
    the plan with a diagnostic naming the shard."""
    relocated_fixture["b_path"].unlink()
    rc = rebase_main([str(relocated_fixture["root"])])
    assert rc == EXIT_UNMATCHED_SHARDS


def test_smoke_gate_fails_on_empty_shards(tmp_path: Path):
    """A fixture whose shards open but hold zero genes must fail the
    serve verification, not pass with rc=0."""
    root = tmp_path / "hollow"
    shard_dir = root / "shards"
    shard_dir.mkdir(parents=True)
    shard_path = shard_dir / "hollow.genome.db"
    g = Genome(str(shard_path))  # schema only, no genes
    _close_genome(g)

    main = open_main_db(str(root / "main.genome.db"))
    init_main_db(main)
    register_shard(
        main, "hollow", "reference",
        (tmp_path / "origin-e92c" / "shards" / "hollow.genome.db").as_posix(),
        gene_count=0,
    )
    main.close()
    rc = rebase_main([str(root)])
    assert rc in (EXIT_SERVE_FAILED, EXIT_SMOKE_FAILED)
