"""WS2 end-to-end: ingest -> symbol defs indexed -> cross-chunk SYMBOL_REF edge
-> retrieval expansion surfaces the referenced definition.

A hit on a chunk that *calls* a function should surface the chunk that *defines*
it — the cross-chunk case lexical term-overlap misses. Requires the full ingest
stack; skipped when tree-sitter isn't available.
"""
import os
import sqlite3

import pytest

from helix_context.encoding import tree_chunker as tc

pytestmark = pytest.mark.skipif(
    not tc.is_available(), reason="tree-sitter (+ tree-sitter-python) not installed"
)

# compute_tax is large (forces its own chunk); invoice_total is a separate chunk
# that calls it -> a cross-chunk SYMBOL_REF edge must form.
_BIG = "\n".join(f"    step_{i} = {i} * 2" for i in range(400))
_CODE = (
    "def compute_tax(amount):\n" + _BIG + "\n    return amount * 0.2\n\n\n"
    "def invoice_total(items):\n    sub = sum(items)\n    return sub + compute_tax(sub)\n"
)


def _manager(tmp_path, monkeypatch):
    monkeypatch.delenv("HELIX_USE_SHARDS", raising=False)
    monkeypatch.setenv("HELIX_GENOME_PATH", str(tmp_path / "genome.db"))
    from helix_context.config import load_config
    from helix_context.context_manager import HelixContextManager

    cfg = load_config()
    cfg.ingestion.symbol_graph = True
    cfg.ingestion.sema_embed_on_ingest = False
    return HelixContextManager(cfg)


def test_symbol_graph_end_to_end(tmp_path, monkeypatch):
    from helix_context.schemas import StructuralRelation
    from helix_context.storage.co_activation import (
        expand_coactivated, _row_to_gene_inline,
    )

    mgr = _manager(tmp_path, monkeypatch)
    mgr.ingest(_CODE, content_type="code",
               metadata={"path": "billing.py", "source_id": "billing.py"})

    conn = mgr.genome.conn
    sdefs = {}
    for sym, gid in conn.execute("SELECT symbol, gene_id FROM symbol_defs"):
        sdefs.setdefault(sym, []).append(gid)
    assert {"compute_tax", "invoice_total"} <= set(sdefs)

    ct = set(sdefs["compute_tax"])
    it = set(sdefs["invoice_total"])
    edges = conn.execute(
        "SELECT gene_id_a, gene_id_b FROM gene_relations WHERE relation = ?",
        (int(StructuralRelation.SYMBOL_REF),),
    ).fetchall()
    assert any(a in it and b in ct for a, b in edges), "no cross-chunk SYMBOL_REF edge"

    # a hit on the caller chunk surfaces the definition chunk via expansion
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM genes WHERE gene_id = ?", (next(iter(it)),)).fetchone()
    caller = _row_to_gene_inline(row)
    expanded = {g.gene_id for g in expand_coactivated(
        [caller], limit=10, conn=conn, entity_graph_enabled=False)}
    assert ct & expanded, "expansion did not surface the referenced definition"
