"""Route-parity test for ``ContextItem.document_id``.

The same content-addressed gene must yield an IDENTICAL ``document_id`` whether
it is served via the blob route (a ``KnowledgeStore``/``Genome`` + a ``main_conn``
``source_index``) or the sharded route (a ``ShardedGenomeAdapter``). The
adversarial twist: the blob route's ``source_index`` row *overrides* the gene's
``source_id`` (this is live — it drives ``item.source_id``), while the sharded
route has no such override. ``document_id`` must ignore the override and anchor
on ``gene.source_id`` on BOTH routes, so the two values match.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from cymatix_context.context_packet import build_context_packet
from cymatix_context.genome import Genome
from cymatix_context.sharding import ShardedGenomeAdapter
from cymatix_context.shard_schema import (
    init_main_db,
    open_main_db,
    register_shard,
    upsert_fingerprint,
    upsert_source_index,
)

from tests.conftest import make_gene

# The one content string used on both routes. gene_id is content-addressed,
# so identical content => identical gene_id => a true same-gene comparison.
_CONTENT = "Helix design notes for the agent index and retrieval"
_DOMAINS = ["helix", "design"]
_QUERY = "helix design"
_TRUE_SOURCE_ID = "/repo/docs/design.md"
_OVERRIDE_SOURCE_ID = "/repo/config/OVERRIDDEN.toml"


def _blob_item():
    """Blob route: Genome + a main_conn whose source_index OVERRIDES source_id."""
    now_ts = 10_000.0
    genome = Genome(":memory:")
    main_conn = open_main_db(":memory:")
    init_main_db(main_conn)
    register_shard(main_conn, "main_ref", "reference", ":memory:")
    try:
        gene = make_gene(_CONTENT, domains=_DOMAINS)
        gene.source_id = _TRUE_SOURCE_ID
        gene.source_kind = "doc"
        gene.volatility_class = "stable"
        gene.authority_class = "primary"
        gene.last_verified_at = now_ts - 60.0
        gene_id = genome.upsert_gene(gene, apply_gate=False)

        # Adversarial: source_index overrides source_id for the blob route.
        upsert_source_index(
            main_conn,
            gene_id=gene_id,
            shard_name="main_ref",
            source_id=_OVERRIDE_SOURCE_ID,
            source_kind="doc",
            volatility_class="stable",
            authority_class="primary",
            last_verified_at=now_ts - 60.0,
        )

        packet = build_context_packet(
            _QUERY,
            task_type="explain",
            genome=genome,
            main_conn=main_conn,
            now_ts=now_ts,
        )
        items = packet.verified + packet.stale_risk
        assert items, "blob route returned no items"
        return gene_id, items[0]
    finally:
        main_conn.close()
        genome.close()


def _sharded_item(tmpdir: Path):
    """Sharded route: one-shard ShardedGenomeAdapter, no source_index override."""
    now_ts = 10_000.0
    main_path = str(tmpdir / "main.db")
    shard_path = str(tmpdir / "shard_a.db")

    ga = Genome(shard_path)
    gene = make_gene(_CONTENT, domains=_DOMAINS)
    gene.source_id = _TRUE_SOURCE_ID
    gene.source_kind = "doc"
    gene.volatility_class = "stable"
    gene.authority_class = "primary"
    gene.last_verified_at = now_ts - 60.0
    gene_id = ga.upsert_gene(gene, apply_gate=False)
    ga.conn.close()
    if ga._reader:
        ga._reader.close()

    main = open_main_db(main_path)
    init_main_db(main)
    register_shard(main, "shard_a", "reference", shard_path, gene_count=1)
    upsert_fingerprint(
        main,
        gene_id=gene_id,
        shard_name="shard_a",
        source_id=_TRUE_SOURCE_ID,
        domains_json=json.dumps(_DOMAINS),
        entities_json="[]",
        key_values_json="[]",
    )
    main.close()

    adapter = ShardedGenomeAdapter(main_path=main_path)
    try:
        packet = build_context_packet(
            _QUERY,
            task_type="explain",
            router=adapter,
            now_ts=now_ts,
        )
        items = packet.verified + packet.stale_risk
        assert items, "sharded route returned no items"
        return gene_id, items[0]
    finally:
        adapter.close()


def test_document_id_identical_across_blob_and_sharded_routes():
    with tempfile.TemporaryDirectory() as td:
        blob_gid, blob_item = _blob_item()
        shard_gid, shard_item = _sharded_item(Path(td))

    # Content-addressed: same content => same gene_id on both routes.
    assert blob_gid == shard_gid

    # The core invariant: document_id is identical across routes...
    assert blob_item.document_id == shard_item.document_id
    # ...and equals the gene's own source_id.
    assert blob_item.document_id == _TRUE_SOURCE_ID
    assert shard_item.document_id == _TRUE_SOURCE_ID


def test_document_id_ignores_source_index_override_on_blob_route():
    """Proves anchoring: the blob route's source_index override drives
    ``item.source_id`` but must NOT leak into ``document_id``."""
    _, blob_item = _blob_item()
    # The override is live — it wins for source_id...
    assert blob_item.source_id == _OVERRIDE_SOURCE_ID
    # ...but document_id anchors on gene.source_id, ignoring the override.
    assert blob_item.document_id == _TRUE_SOURCE_ID
