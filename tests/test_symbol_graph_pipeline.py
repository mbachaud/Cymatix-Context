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


def _symdefs(conn):
    out = {}
    for sym, gid in conn.execute("SELECT symbol, gene_id FROM symbol_defs"):
        out.setdefault(sym, []).append(gid)
    return out


def test_expansion_is_append_only_lexical_first(tmp_path, monkeypatch):
    """Lexical-first guard: expansion only APPENDS referenced defs — it never
    reorders or displaces the lexical candidates (PRD §4)."""
    import sqlite3
    from helix_context.storage.co_activation import expand_coactivated, _row_to_gene_inline

    mgr = _manager(tmp_path, monkeypatch)
    mgr.ingest(_CODE, content_type="code",
               metadata={"path": "billing.py", "source_id": "billing.py"})
    conn = mgr.genome.conn
    sdefs = _symdefs(conn)
    it_gid = sdefs["invoice_total"][0]
    ct_gids = set(sdefs["compute_tax"])

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM genes WHERE gene_id = ?", (it_gid,)).fetchone()
    caller = _row_to_gene_inline(row)
    expanded = expand_coactivated([caller], limit=20, conn=conn,
                                  entity_graph_enabled=False, symbol_expansion_cap=8)
    # the lexical candidate stays at rank 0; referenced defs only appear after it
    assert expanded[0].gene_id == it_gid, "expansion displaced the lexical hit from rank 0"
    assert ct_gids & {g.gene_id for g in expanded[1:]}, "referenced def not appended"


def test_symbol_expansion_cap_bounds_top_k(tmp_path, monkeypatch):
    """The cap actually bounds the expansion to top-K (WS3 Phase 2a core)."""
    import sqlite3
    from helix_context.schemas import StructuralRelation
    from helix_context.storage.co_activation import expand_coactivated, _row_to_gene_inline

    mgr = _manager(tmp_path, monkeypatch)
    # 15 large helpers (each its own chunk) + one driver that calls all of them.
    body = "\n".join(f"    a{j} = {j} * 2" for j in range(150))  # ~2.5k chars -> own chunk
    helpers = "\n\n\n".join(f"def helper_{i}(x):\n{body}\n    return x" for i in range(15))
    driver = ("def driver(items):\n    return ["
              + " + ".join(f"helper_{i}(items)" for i in range(15)) + "]\n")
    mgr.ingest(helpers + "\n\n\n" + driver, content_type="code",
               metadata={"path": "m.py", "source_id": "m.py"})
    conn = mgr.genome.conn
    sdefs = _symdefs(conn)
    assert "driver" in sdefs
    driver_gid = sdefs["driver"][0]
    n_edges = conn.execute(
        "SELECT COUNT(*) FROM gene_relations WHERE relation = ? AND gene_id_a = ?",
        (int(StructuralRelation.SYMBOL_REF), driver_gid),
    ).fetchone()[0]
    assert n_edges > 8, f"need >8 referenced defs to exercise the cap (got {n_edges})"

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM genes WHERE gene_id = ?", (driver_gid,)).fetchone()
    caller = _row_to_gene_inline(row)

    capped = expand_coactivated([caller], limit=50, conn=conn,
                                entity_graph_enabled=False, symbol_expansion_cap=8)
    added = [g for g in capped if g.gene_id != driver_gid]
    assert len(added) <= 8, f"cap=8 not respected: {len(added)} defs added"

    disabled = expand_coactivated([caller], limit=50, conn=conn,
                                  entity_graph_enabled=False, symbol_expansion_cap=0)
    assert all(g.gene_id == driver_gid for g in disabled), "cap=0 should disable expansion"
