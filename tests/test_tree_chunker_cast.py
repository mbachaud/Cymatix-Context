"""cAST recursive split-then-merge chunker (WS1 / Phase 1).

Guards the behaviour change from top-level greedy-merge + char-cut to cAST
recursive split-then-merge: oversized nodes recurse into their children (a big
class -> whole methods) instead of being hard-cut mid-symbol; only an atomic
oversized leaf is char-cut; small adjacent pieces re-merge to the budget; and
definition chunks carry symbol/type/span metadata for the symbol graph (WS2).
"""
import pytest

from helix_context.encoding import tree_chunker as tc

pytestmark = pytest.mark.skipif(
    not tc.is_available(), reason="tree-sitter (+ tree-sitter-python) not installed"
)


def _big_class(n_methods=8, body_lines=12):
    methods = []
    for i in range(n_methods):
        body = "\n".join(f"        x{j} = {j} + {i}" for j in range(body_lines))
        methods.append(f"    def method_{i}(self):\n{body}\n        return x0")
    return "class Big:\n" + "\n\n".join(methods) + "\n"


def test_small_file_is_one_chunk():
    code = "def f():\n    return 1\n"
    chunks = tc.chunk_code_ast(code, max_chars=4000, language="python")
    assert len(chunks) == 1
    assert "def f" in chunks[0][0]
    assert chunks[0][1] is False  # not a fragment


def test_oversized_class_recurses_into_whole_methods():
    # max_chars chosen so the class exceeds it but a single method fits.
    code = _big_class(n_methods=8, body_lines=12)
    chunks = tc.chunk_code_ast_with_meta(code, max_chars=300, language="python")

    # cAST recursion means NO method is hard-cut — no fragment pieces at all.
    assert all(frag is False for _t, frag, _m in chunks), "a method was hard-cut"

    joined = "\n".join(t for t, _f, _m in chunks)
    for i in range(8):
        # each method appears exactly once and is therefore not split across chunks
        assert joined.count(f"def method_{i}") == 1
    # each method keeps its body: the 'def' and its 'return' land in the same chunk
    for t, _f, _m in chunks:
        for i in range(8):
            if f"def method_{i}" in t:
                assert "return x0" in t, f"method_{i} split from its body"


def test_atomic_oversized_leaf_is_char_cut():
    # a single huge string literal has no splittable children -> last-resort cut
    code = "BIG = '" + ("A" * 6000) + "'\n"
    chunks = tc.chunk_code_ast_with_meta(code, max_chars=1000, language="python")
    assert any(frag for _t, frag, _m in chunks), "atomic oversized leaf should fragment"
    # and every emitted piece respects the budget
    assert all(len(t) <= 1000 for t, _f, _m in chunks)


def test_definition_metadata_is_captured():
    code = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"
    # max_chars large enough that each function is emitted whole (so meta fires),
    # but small enough that the file splits into separate chunks.
    chunks = tc.chunk_code_ast_with_meta(code, max_chars=40, language="python")
    metas = [m for _t, _f, m in chunks if m]
    syms = {m["symbol"] for m in metas}
    assert {"alpha", "beta"} <= syms
    for m in metas:
        assert m["type"] == "function_definition"
        assert m["language"] == "python"
        assert m["end_byte"] > m["start_byte"]


def test_small_adjacent_pieces_merge_to_budget():
    code = "\n\n".join(f"def f{i}():\n    return {i}" for i in range(20)) + "\n"
    # all tiny -> a generous budget merges everything into one chunk
    one = tc.chunk_code_ast(code, max_chars=4000, language="python")
    assert len(one) == 1
    # a tight budget forces multiple chunks, none exceeding the budget
    many = tc.chunk_code_ast(code, max_chars=120, language="python")
    assert len(many) > 1
    assert all(len(t) <= 120 for t, _f in many)


def test_two_tuple_wrapper_matches_meta_variant():
    code = _big_class(5, 6)
    a = tc.chunk_code_ast(code, max_chars=200, language="python")
    b = tc.chunk_code_ast_with_meta(code, max_chars=200, language="python")
    assert a == [(t, f) for t, f, _m in b]


def test_char_cut_never_splits_multibyte_cjk():
    # A giant atomic string of 3-byte CJK chars: a byte-offset hard cut lands
    # mid-codepoint and each piece decodes with U+FFFD unless the cut point is
    # snapped to a UTF-8 character boundary. max_chars=1000 (≢ 0 mod 3)
    # guarantees misaligned cuts regardless of the node's start offset.
    code = 'BIG = "' + ("日" * 3000) + '"\n'
    chunks = tc.chunk_code_ast(code, max_chars=1000, language="python")
    joined = "".join(t for t, _f in chunks)
    assert "�" not in joined, "hard cut split a multibyte character"
    assert joined.count("日") == 3000  # no codepoint lost at a cut
    # every piece still respects the (byte) budget
    assert all(len(t.encode("utf-8")) <= 1000 for t, _f in chunks)


def test_char_cut_never_splits_emoji():
    # 4-byte emoji straddling the cut; max_chars=1001 (≢ 0 mod 4) forces at
    # least one misaligned byte cut.
    code = 'E = "' + ("\U0001f680" * 2000) + '"\n'
    chunks = tc.chunk_code_ast(code, max_chars=1001, language="python")
    joined = "".join(t for t, _f in chunks)
    assert "�" not in joined, "hard cut split an emoji codepoint"
    assert joined.count("\U0001f680") == 2000
    assert all(len(t.encode("utf-8")) <= 1001 for t, _f in chunks)


def test_unknown_language_raises():
    with pytest.raises(ValueError):
        tc.chunk_code_ast("x = 1", max_chars=100, language="cobol")
