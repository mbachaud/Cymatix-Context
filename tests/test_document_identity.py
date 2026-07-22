"""Unit tests for the ``document_identity`` extractor (v1 = raw source_id).

Pins the core identity contract:
  * returns ``gene.source_id`` verbatim when present,
  * returns ``None`` when absent/empty,
  * and — critically — anchors on the GENE's ``source_id``, never the
    ``meta``/source_row copy that diverges between the blob and sharded
    routes (context_packet.py:101). If a passed ``meta`` carries a
    *different* source_id, the gene's value must still win.
"""

from __future__ import annotations

from cymatix_context.context_packet import document_identity

from tests.conftest import make_gene


def test_returns_source_id_when_present():
    gene = make_gene("some content")
    gene.source_id = "/repo/docs/design.md"
    assert document_identity(gene) == "/repo/docs/design.md"


def test_returns_none_when_source_id_absent():
    gene = make_gene("some content")
    gene.source_id = None
    assert document_identity(gene) is None


def test_returns_none_when_source_id_empty_string():
    gene = make_gene("some content")
    gene.source_id = ""
    assert document_identity(gene) is None


def test_gene_source_id_wins_over_diverging_meta():
    """Parity invariant: identity anchors on the gene, not meta/source_row.

    On the blob route ``_effective_meta`` can override ``source_id`` from a
    ``source_index`` row (context_packet.py:101). ``document_identity`` must
    ignore that and return the gene's own ``source_id`` so the value is
    identical across the blob and sharded routes.
    """
    gene = make_gene("some content")
    gene.source_id = "/repo/docs/design.md"
    meta = {"source_id": "/repo/config/OVERRIDDEN.toml"}
    assert document_identity(gene, meta) == "/repo/docs/design.md"


def test_meta_none_default_is_accepted():
    gene = make_gene("some content")
    gene.source_id = "/repo/a.md"
    # meta param is optional; identity value ignores it in v1.
    assert document_identity(gene, None) == "/repo/a.md"
