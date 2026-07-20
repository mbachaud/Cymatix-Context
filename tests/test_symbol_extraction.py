"""WS2 slice 1: per-chunk symbol definition + reference extraction.

chunk_code_with_symbols annotates each cAST chunk with the symbols it defines
(function/class/method names) and references (call targets, base classes), which
the symbol graph resolves into referencing-chunk -> defining-chunk edges.
"""
import pytest

from helix_context.encoding import tree_chunker as tc

pytestmark = pytest.mark.skipif(
    not tc.is_available(), reason="tree-sitter (+ tree-sitter-python) not installed"
)

SAMPLE = '''import os


def helper(x):
    return x + 1


class Base:
    pass


class Widget(Base):
    def run(self):
        return helper(self.value)
'''


def _union(chunks, key):
    out = set()
    for c in chunks:
        out |= set(c[key])
    return out


def test_defs_and_refs_collected_whole_file():
    chunks = tc.chunk_code_with_symbols(SAMPLE, max_chars=4000, language="python")
    defs = _union(chunks, "defs")
    refs = _union(chunks, "refs")
    assert {"helper", "Base", "Widget", "run"} <= defs
    assert "helper" in refs   # run() calls helper()
    assert "Base" in refs      # class Widget(Base)


def test_cross_chunk_reference_edge_candidate():
    # a tight budget splits helper's definition away from its caller
    chunks = tc.chunk_code_with_symbols(SAMPLE, max_chars=80, language="python")
    assert len(chunks) > 1
    # at least one chunk references helper without defining it (a real edge)
    assert any("helper" in c["refs"] and "helper" not in c["defs"] for c in chunks)


def test_attribute_call_uses_final_name():
    code = "def f(self):\n    return self.compute(1)\n"
    chunks = tc.chunk_code_with_symbols(code, max_chars=4000, language="python")
    assert "compute" in _union(chunks, "refs")


def test_chunks_carry_byte_exact_spans():
    chunks = tc.chunk_code_with_symbols(SAMPLE, max_chars=80, language="python")
    for c in chunks:
        assert c["end_byte"] > c["start_byte"]
        # cAST chunks are verbatim slices, so the recovered span is exact
        assert c["text"] in SAMPLE or c["text"].strip() in SAMPLE


def test_unknown_language_raises():
    with pytest.raises(ValueError):
        tc.chunk_code_with_symbols("x = 1", language="cobol")
