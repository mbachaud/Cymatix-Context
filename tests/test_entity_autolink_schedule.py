"""Entity auto-link scheduling knob (2026-08-30 enronqa_padded ingest-decay fix).

py-spy on the live 289k-gene enronqa_padded build measured 89.5% of writer
wall time inside ``auto_link_by_entity`` — the per-insert GROUP BY sweeps
the posting lists of hub entities ('content-type' alone: 171k rows), so
bulk builds decay O(N^2). The knob lets bulk builders skip COVER-edge
formation (``gene_relations`` relation=5) while still writing
``entity_graph`` rows, keeping the documents+content identity bar of
``scripts/ingest_equivalence_gate.py`` intact — COVER edges are
default-inert at query time (W2.1 cover-walk kill receipt) and already
order-nondeterministic under ``--parallel``.
"""
import sqlite3

from cymatix_context.schemas import Gene, PromoterTags, EpigeneticMarkers
from cymatix_context.genome import Genome


def _make(content, entities):
    return Gene(
        gene_id=Genome.make_gene_id(content),
        content=content,
        complement="",
        codons=[],
        promoter=PromoterTags(domains=[], entities=entities),
        epigenetics=EpigeneticMarkers(),
    )


def _upsert_pair(g):
    g.upsert_gene(_make("first doc about kaminski and houston desk",
                        ["kaminski", "houston", "desk"]))
    g.upsert_gene(_make("second doc about kaminski and houston floor",
                        ["kaminski", "houston", "floor"]))


def test_autolink_on_by_default():
    """entity_graph=True alone must keep today's behavior: COVER edges form."""
    g = Genome(path=":memory:", synonym_map={}, entity_graph=True)
    _upsert_pair(g)
    edges = g.conn.execute(
        "SELECT COUNT(*) FROM gene_relations WHERE relation = 5"
    ).fetchone()[0]
    assert edges >= 1


def test_autolink_off_skips_edges_keeps_entity_rows():
    """entity_autolink=False: entity_graph rows still written, no COVER edges."""
    g = Genome(
        path=":memory:", synonym_map={},
        entity_graph=True, entity_autolink=False,
    )
    _upsert_pair(g)
    ents = g.conn.execute("SELECT COUNT(*) FROM entity_graph").fetchone()[0]
    edges = g.conn.execute(
        "SELECT COUNT(*) FROM gene_relations WHERE relation = 5"
    ).fetchone()[0]
    assert ents == 6  # 3 entities x 2 genes, content identical to autolink=on
    assert edges == 0


def test_filename_index_delete_uses_gene_id_index():
    """The per-upsert DELETE must not full-scan filename_index.

    Measured on the live enronqa_padded bed (279k rows): the unindexed
    DELETE was 6.6% of writer wall time and grows O(N).
    """
    from cymatix_context import filename_anchor

    conn = sqlite3.connect(":memory:")
    filename_anchor.ensure_schema(conn)
    names = {r[1] for r in conn.execute("PRAGMA index_list(filename_index)")}
    assert "idx_filename_gene" in names
    plan = " ".join(
        r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN DELETE FROM filename_index WHERE gene_id = 'x'"
        )
    )
    assert "SCAN filename_index" not in plan, plan
