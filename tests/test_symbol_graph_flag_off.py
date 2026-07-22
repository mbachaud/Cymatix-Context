"""WS2 review FIX-3: flag-off means zero extraction cost, not just no emission.

With ``[ingestion] symbol_graph = false`` the code chunker must route through
the plain cAST chunker (``chunk_code_ast``): no second parse, no symbol walk,
no defs/refs strand metadata, no ``symbol_defs`` / SYMBOL_REF rows. Previously
the flag only gated emission — ``chunk_code_with_symbols`` (and its extra
parse) ran unconditionally.
"""
from __future__ import annotations

import pytest

from cymatix_context.encoding import tree_chunker as tc
from cymatix_context.encoding.fragments import CodonChunker

_needs_tree_sitter = pytest.mark.skipif(
    not tc.is_available(), reason="tree-sitter (+ tree-sitter-python) not installed"
)

SAMPLE = '''def helper(x):
    return x + 1


class Widget:
    def run(self):
        return helper(self.value)
'''


def test_symbol_graph_default_is_dark_shipped():
    """WS2 review FIX-5: [ingestion] symbol_graph ships default-False (dark).

    The 2026-07-20 ContextBench held-out result cleared the gate (packet
    +2.8pp line / +3.8pp sym), but dark-ship is the intentional decision
    pending the cap sweep + code-gating validation — see
    docs/benchmarks/2026-07-20-armc-contextbench-heldout.md and
    docs/ROADMAP.md. Flipping this default belongs to #231's follow-up,
    not a drive-by.
    """
    from cymatix_context.config import IngestionConfig

    assert IngestionConfig().symbol_graph is False


class TestChunkerFlagOff:
    def test_flag_off_never_invokes_symbol_extractor(self, monkeypatch):
        """With the flag off, chunk_code_with_symbols must not be called at
        all — extraction cost is zero, not merely discarded."""

        def boom(*_a, **_kw):
            raise AssertionError(
                "chunk_code_with_symbols was invoked with symbol_graph=False"
            )

        monkeypatch.setattr(tc, "chunk_code_with_symbols", boom)
        chunker = CodonChunker(max_chars_per_strand=4000, symbol_graph=False)
        strands = chunker.chunk(SAMPLE, content_type="code",
                                metadata={"path": "widget.py"})
        assert strands, "flag-off code chunking produced no strands"
        for s in strands:
            assert "defs" not in s.metadata
            assert "refs" not in s.metadata

    @_needs_tree_sitter
    def test_flag_on_still_extracts_symbols(self):
        chunker = CodonChunker(max_chars_per_strand=4000, symbol_graph=True)
        strands = chunker.chunk(SAMPLE, content_type="code",
                                metadata={"path": "widget.py"})
        defs = set()
        for s in strands:
            defs |= set(s.metadata.get("defs", []))
        assert {"helper", "Widget"} <= defs

    @_needs_tree_sitter
    def test_chunk_texts_identical_on_and_off(self):
        """The flag changes metadata and cost, never chunk content."""
        on = CodonChunker(max_chars_per_strand=200, symbol_graph=True)
        off = CodonChunker(max_chars_per_strand=200, symbol_graph=False)
        texts_on = [s.content for s in on.chunk(SAMPLE, content_type="code",
                                                metadata={"path": "widget.py"})]
        texts_off = [s.content for s in off.chunk(SAMPLE, content_type="code",
                                                  metadata={"path": "widget.py"})]
        assert texts_on == texts_off


# ---------------------------------------------------------------------------
# End-to-end: flag-off ingest writes no symbol rows and never touches the
# symbol extractor.
# ---------------------------------------------------------------------------

_BIG = "\n".join(f"    step_{i} = {i} * 2" for i in range(400))
_CODE = (
    "def compute_tax(amount):\n" + _BIG + "\n    return amount * 0.2\n\n\n"
    "def invoice_total(items):\n    sub = sum(items)\n    return sub + compute_tax(sub)\n"
)


def _manager(tmp_path, monkeypatch, symbol_graph: bool):
    monkeypatch.delenv("HELIX_USE_SHARDS", raising=False)
    monkeypatch.setenv("HELIX_GENOME_PATH", str(tmp_path / "genome.db"))
    from cymatix_context.config import load_config
    from cymatix_context.context_manager import HelixContextManager

    cfg = load_config()
    cfg.ingestion.symbol_graph = symbol_graph
    cfg.ingestion.sema_embed_on_ingest = False
    return HelixContextManager(cfg)


def test_flag_off_ingest_writes_zero_symbol_rows(tmp_path, monkeypatch):
    """Flag-off ingest succeeds even if the symbol extractor would explode —
    proof it is never invoked — and leaves symbol tables empty."""
    from cymatix_context.schemas import StructuralRelation

    def boom(*_a, **_kw):
        raise AssertionError(
            "chunk_code_with_symbols was invoked during flag-off ingest"
        )

    monkeypatch.setattr(tc, "chunk_code_with_symbols", boom)
    mgr = _manager(tmp_path, monkeypatch, symbol_graph=False)
    gene_ids = mgr.ingest(_CODE, content_type="code",
                          metadata={"path": "billing.py", "source_id": "billing.py"})
    assert gene_ids, "flag-off ingest produced no genes"

    conn = mgr.genome.conn
    assert conn.execute("SELECT COUNT(*) FROM symbol_defs").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM gene_relations WHERE relation = ?",
        (int(StructuralRelation.SYMBOL_REF),),
    ).fetchone()[0] == 0
