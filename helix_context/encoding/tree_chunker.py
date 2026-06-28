"""
Tree-sitter AST chunking — boundary-aware code splitting.

Optional backend for CodonChunker._chunk_code. Uses tree-sitter to
split code along real AST boundaries (functions, classes, methods)
instead of regex-matched keywords.

Bio analogue (legacy terms: codon / gene):
    The regex chunker cuts wherever it sees 'def' or 'class' — like
    a restriction enzyme that can't tell a real cut site from a
    sequence that happens to look like one. Tree-sitter understands
    the grammar: it cuts at function/class definitions, not at 'def'
    inside a docstring or a variable named 'my_class_name'.

Usage (from CodonChunker):
    try:
        from .tree_chunker import chunk_code_ast
        strands = chunk_code_ast(code, max_chars, language="python")
    except ImportError:
        strands = _chunk_code_regex(code, max_chars)  # Fallback

Supported languages (tier 1):
    - python
    - rust
    - javascript
    - typescript

Install:
    pip install tree-sitter tree-sitter-python tree-sitter-rust \
                tree-sitter-javascript tree-sitter-typescript
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("helix.tree_chunker")


# ── Language detection from file path ────────────────────────────

_EXT_TO_LANG = {
    ".py": "python",
    ".pyw": "python",
    ".rs": "rust",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".lua": "lua",
}


def detect_language(source_id: Optional[str]) -> Optional[str]:
    """Infer tree-sitter language from source file extension."""
    if not source_id:
        return None
    import os
    _, ext = os.path.splitext(source_id.lower())
    return _EXT_TO_LANG.get(ext)


# ── Parser cache ─────────────────────────────────────────────────

_parser_cache: Dict[str, "object"] = {}


def _get_parser(language: str):
    """
    Load and cache a tree-sitter parser for the given language.

    Uses individual tree-sitter-<lang> packages (tree-sitter-python, etc.)
    instead of the deprecated tree-sitter-languages bundle, which isn't
    maintained for Python 3.12+.

    Raises ImportError if tree-sitter stack isn't installed.
    """
    if language in _parser_cache:
        return _parser_cache[language]

    try:
        from tree_sitter import Language, Parser
    except ImportError as e:
        raise ImportError(
            "tree-sitter chunking requires: pip install tree-sitter "
            "tree-sitter-python tree-sitter-rust tree-sitter-javascript "
            "tree-sitter-typescript"
        ) from e

    # Map language name to module import
    lang_module_map = {
        "python": ("tree_sitter_python", "language"),
        "rust": ("tree_sitter_rust", "language"),
        "javascript": ("tree_sitter_javascript", "language"),
        "typescript": ("tree_sitter_typescript", "language_typescript"),
        "tsx": ("tree_sitter_typescript", "language_tsx"),
    }

    if language not in lang_module_map:
        raise ImportError(f"No tree-sitter package configured for {language}")

    module_name, attr_name = lang_module_map[language]
    try:
        import importlib
        mod = importlib.import_module(module_name)
        lang_capsule = getattr(mod, attr_name)()
    except ImportError as e:
        raise ImportError(
            f"Missing tree-sitter-{language}: pip install {module_name.replace('_', '-')}"
        ) from e

    lang = Language(lang_capsule)
    parser = Parser(lang)
    _parser_cache[language] = parser
    return parser


# ── Boundary node types per language ─────────────────────────────
#
# These are the AST node types that mark "good cut points" — places
# where splitting preserves semantic coherence. Functions and classes
# are universal; the rest are language-specific.

_BOUNDARY_NODES: Dict[str, Tuple[str, ...]] = {
    "python": (
        "function_definition",
        "class_definition",
        "decorated_definition",
    ),
    "rust": (
        "function_item",
        "impl_item",
        "struct_item",
        "enum_item",
        "trait_item",
        "mod_item",
    ),
    "javascript": (
        "function_declaration",
        "class_declaration",
        "method_definition",
        "arrow_function",
        "export_statement",
    ),
    "typescript": (
        "function_declaration",
        "class_declaration",
        "method_definition",
        "interface_declaration",
        "type_alias_declaration",
        "export_statement",
    ),
    "tsx": (
        "function_declaration",
        "class_declaration",
        "method_definition",
        "interface_declaration",
        "export_statement",
    ),
    "go": (
        "function_declaration",
        "method_declaration",
        "type_declaration",
    ),
    "java": (
        "method_declaration",
        "class_declaration",
        "interface_declaration",
    ),
    "c": (
        "function_definition",
        "struct_specifier",
    ),
    "cpp": (
        "function_definition",
        "class_specifier",
        "struct_specifier",
        "namespace_definition",
    ),
    "ruby": (
        "method",
        "class",
        "module",
    ),
    "lua": (
        "function_declaration",
        "local_function",
    ),
}


# ── AST-aware chunking ───────────────────────────────────────────

def _symbol_name(node) -> Optional[str]:
    """Best-effort symbol name for a definition node (its `name` child)."""
    try:
        named = node.child_by_field_name("name")
        if named is not None:
            return named.text.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — field-name API varies by grammar
        pass
    for c in getattr(node, "children", []):
        if c.type in ("identifier", "name", "type_identifier",
                      "field_identifier", "property_identifier", "constant"):
            try:
                return c.text.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                return None
    return None


def chunk_code_ast_with_meta(
    code: str,
    max_chars: int = 4000,
    language: Optional[str] = None,
    source_id: Optional[str] = None,
) -> List[Tuple[str, bool, Optional[dict]]]:
    """
    cAST recursive split-then-merge chunking, with per-chunk metadata.

    Algorithm (cAST, arXiv 2506.15655 — split-then-merge over the AST):
      - A node whose text fits within ``max_chars`` is emitted whole.
      - An oversized node is split by **recursing into its children** — a huge
        class becomes its methods, a huge function becomes its statements —
        instead of being hard-cut mid-symbol. Interstitial text between
        children (decorators, blank lines, comments) is preserved in order.
      - Only a *childless* node that still exceeds ``max_chars`` (a giant string
        or dict literal, a minified line) is hard-cut at a raw character
        boundary, as a last resort.
      - A final greedy pass re-merges adjacent small pieces up to ``max_chars``
        so we never emit needless tiny fragments.

    This replaces the previous top-level-only greedy merge, which hard-cut any
    top-level node ≥ max_chars at a character boundary (slicing a class through
    the middle of a method). Recursing first is the change that earns cAST's
    +4.3 Recall@5 / +2.67 Pass@1.

    Returns:
        List of ``(chunk_content, is_fragment, meta)``. ``is_fragment`` is True
        for a hard-cut last-resort piece. ``meta`` (when not None) carries
        ``{"symbol", "type", "start_byte", "end_byte", "language"}`` for the
        primary definition the chunk represents — consumed by the symbol graph
        (WS2). Chunks of interstitial / non-definition text have ``meta=None``.

    Raises:
        ImportError: if tree-sitter isn't installed
        ValueError: if language is unknown/unsupported
    """
    if language is None:
        language = detect_language(source_id)
    if language is None or language not in _BOUNDARY_NODES:
        raise ValueError(
            f"Unsupported or undetected language for tree-sitter chunking "
            f"(source={source_id!r}, language={language!r})"
        )

    parser = _get_parser(language)
    code_bytes = code.encode("utf-8")
    tree = parser.parse(code_bytes)
    root = tree.root_node
    boundary_types = set(_BOUNDARY_NODES[language])

    # A Span is a half-open byte range [start, end) plus is_fragment + meta.
    # Spans tile the file contiguously (no bytes dropped), so a merged chunk is
    # always an exact code_bytes[start:end] slice — i.e. a verbatim substring of
    # the source. (The earlier draft reassembled decoded piece text and dropped
    # whitespace-only gaps, which broke any consumer that line-maps a chunk by
    # verbatim match — e.g. ContextBench's recover_lines — collapsing recall.)
    Span = Tuple[int, int, bool, Optional[dict]]

    def text_of(start: int, end: int) -> str:
        return code_bytes[start:end].decode("utf-8", errors="replace")

    def meta_for(node) -> Optional[dict]:
        """Metadata for a definition node, else None. Unwraps decorators."""
        if node.type not in boundary_types:
            return None
        target = node
        if node.type == "decorated_definition":
            for c in node.children:
                if c.type in boundary_types and c.type != "decorated_definition":
                    target = c
                    break
        return {
            "symbol": _symbol_name(target),
            "type": target.type,
            "start_byte": node.start_byte,
            "end_byte": node.end_byte,
            "language": language,
        }

    def snap_to_char(pos: int) -> int:
        """Snap a byte offset back onto a UTF-8 character boundary.

        A UTF-8 continuation byte is 0b10xxxxxx; walking backwards past them
        lands on the codepoint's lead byte, so no cut ever splits a
        multibyte character (which would decode to U+FFFD on both sides).
        """
        while pos > 0 and (code_bytes[pos] & 0xC0) == 0x80:
            pos -= 1
        return pos

    def char_cut(start: int, end: int) -> List[Span]:
        """Last-resort hard cut of an atomic oversized span at char boundaries."""
        out: List[Span] = []
        s = start
        while end - s > max_chars:
            cut = snap_to_char(s + max_chars)
            if cut <= s:  # degenerate budget smaller than one codepoint
                cut = s + max_chars
            out.append((s, cut, True, None))
            s = cut
        if end > s:
            out.append((s, end, False, None))
        return out

    def split(node) -> List[Span]:
        if node.end_byte - node.start_byte <= max_chars:
            return [(node.start_byte, node.end_byte, False, meta_for(node))]
        kids = list(node.children)
        if not kids:
            return char_cut(node.start_byte, node.end_byte)
        spans: List[Span] = []
        cursor = node.start_byte
        for c in kids:
            # Interstitial gap before this child (decorators, blanks, comments) —
            # kept as a span so the tiling stays contiguous / byte-exact.
            if c.start_byte > cursor:
                if c.start_byte - cursor > max_chars:
                    spans.extend(char_cut(cursor, c.start_byte))
                else:
                    spans.append((cursor, c.start_byte, False, None))
            spans.extend(split(c))
            cursor = c.end_byte
        if node.end_byte > cursor:
            spans.append((cursor, node.end_byte, False, None))
        return spans

    spans = split(root)
    if not spans:
        return [(code, False, None)]

    # Greedy merge of adjacent spans up to max_chars. A merged chunk spans
    # [group_start, group_end); its text is the exact byte slice (so any
    # interstitial gap between merged units is preserved verbatim). It inherits
    # the metadata of its first definition-bearing span and is a fragment if any
    # constituent was hard-cut.
    merged: List[Tuple[str, bool, Optional[dict]]] = []
    gs: Optional[int] = None
    ge = 0
    gfrag = False
    gmeta: Optional[dict] = None

    def flush() -> None:
        if gs is None:
            return
        body = text_of(gs, ge)            # exact byte slice => verbatim substring
        if body.strip():                  # skip whitespace-only groups
            merged.append((body, gfrag, gmeta))

    for s, e, frag, meta in spans:
        if gs is not None and (e - gs) > max_chars:
            flush()
            gs = None
        if gs is None:
            gs, ge, gfrag, gmeta = s, e, frag, meta
        else:
            ge = e
            gfrag = gfrag or frag
            if gmeta is None and meta is not None:
                gmeta = meta
    flush()
    return merged


def chunk_code_ast(
    code: str,
    max_chars: int = 4000,
    language: Optional[str] = None,
    source_id: Optional[str] = None,
) -> List[Tuple[str, bool]]:
    """
    Backward-compatible 2-tuple wrapper over the cAST recursive chunker.

    Returns ``(chunk_content, is_fragment)`` pairs (is_fragment=True for a
    hard-cut piece). Callers that need symbol/span metadata (the symbol graph,
    WS2) should call :func:`chunk_code_ast_with_meta` instead.

    Raises:
        ImportError: if tree-sitter isn't installed
        ValueError: if language is unknown/unsupported
    """
    return [
        (text, frag)
        for (text, frag, _meta)
        in chunk_code_ast_with_meta(code, max_chars, language, source_id)
    ]


def is_available() -> bool:
    """Check whether tree-sitter chunking is usable in this environment."""
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_python  # noqa: F401
        return True
    except ImportError:
        return False


# ── Symbol def/ref extraction (WS2 — symbol graph) ───────────────────
#
# For each chunk, collect the symbols it DEFINES (function/class/method names)
# and the symbols it REFERENCES (call targets, base classes). The symbol graph
# (WS2) resolves a chunk's references to the chunks that define them and emits
# SYMBOL_REF edges, so retrieval can pull in the definition of what a hit calls.
#
# Reference extraction is the one genuinely-new parse pass; it is intentionally
# scoped to high-signal references (calls + base classes) rather than every
# identifier, to keep the resulting edges precise (naive name-matching links
# every `process()` to every other). Tier-1 language: python. Other languages
# return definitions only (empty refs) until their ref grammar is added — a
# safe degrade to WS1-only behaviour.

_PY_REF_LANGS = frozenset({"python"})


def _def_target(node):
    """Unwrap a decorated_definition to the inner def/class node for naming."""
    if node.type == "decorated_definition":
        for c in node.children:
            if c.type in ("function_definition", "class_definition"):
                return c
    return node


def _py_callee_name(call_node) -> Optional[str]:
    """Name of a python call target: `foo(...)` -> 'foo', `a.b.foo(...)` -> 'foo'."""
    try:
        fn = call_node.child_by_field_name("function")
    except Exception:  # noqa: BLE001
        fn = None
    if fn is None:
        return None
    if fn.type == "identifier":
        return fn.text.decode("utf-8", errors="replace")
    if fn.type == "attribute":
        attr = fn.child_by_field_name("attribute")
        if attr is not None:
            return attr.text.decode("utf-8", errors="replace")
    return None


def _collect_symbols(root, language: str):
    """Return (defs, refs): lists of (name, start_byte) over the whole tree.

    defs = definition sites (function/class/method names).
    refs = high-signal reference sites (call targets, base-class names).
    """
    boundary = set(_BOUNDARY_NODES.get(language, ()))
    defs = []
    refs = []
    stack = list(root.children)
    while stack:
        n = stack.pop()
        if n.type in boundary:
            name = _symbol_name(_def_target(n))
            if name:
                defs.append((name, n.start_byte))
        if language in _PY_REF_LANGS:
            if n.type == "call":
                name = _py_callee_name(n)
                if name:
                    refs.append((name, n.start_byte))
            elif n.type == "class_definition":
                supers = None
                try:
                    supers = n.child_by_field_name("superclasses")
                except Exception:  # noqa: BLE001
                    supers = None
                if supers is not None:
                    for c in supers.children:
                        if c.type == "identifier":
                            refs.append((c.text.decode("utf-8", errors="replace"),
                                         c.start_byte))
        stack.extend(n.children)
    return defs, refs


def chunk_code_with_symbols(
    code: str,
    max_chars: int = 4000,
    language: Optional[str] = None,
    source_id: Optional[str] = None,
) -> List[dict]:
    """
    cAST chunks annotated with the symbols each chunk defines / references.

    Returns a list of dicts, one per chunk, in source order::

        {"text", "is_fragment", "start_byte", "end_byte",
         "defs": [symbol, ...], "refs": [symbol, ...]}

    ``defs`` are the definitions inside the chunk; ``refs`` are the call targets
    and base classes it references (python tier-1; other languages -> []). The
    symbol graph (WS2) resolves each chunk's ``refs`` against every chunk's
    ``defs`` to emit referencing-chunk -> defining-chunk edges. Reference and
    definition sites are bucketed into chunks by byte offset; because cAST chunks
    are exact byte slices, chunk spans are recovered by verbatim search.
    """
    if language is None:
        language = detect_language(source_id)
    if language is None or language not in _BOUNDARY_NODES:
        raise ValueError(
            f"Unsupported or undetected language for tree-sitter chunking "
            f"(source={source_id!r}, language={language!r})"
        )

    parser = _get_parser(language)
    code_bytes = code.encode("utf-8")
    root = parser.parse(code_bytes).root_node

    chunks = chunk_code_ast_with_meta(code, max_chars, language, source_id)
    defs, refs = _collect_symbols(root, language)

    out: List[dict] = []
    spans: List[Tuple[int, int]] = []
    cursor = 0
    for text, frag, _meta in chunks:
        tb = text.encode("utf-8")
        idx = code_bytes.find(tb, cursor)
        if idx < 0:
            idx = code_bytes.find(tb)
        start = idx if idx >= 0 else cursor
        end = start + len(tb)
        spans.append((start, end))
        cursor = end if end > cursor else cursor
        out.append({
            "text": text, "is_fragment": frag,
            "start_byte": start, "end_byte": end,
            "defs": set(), "refs": set(),
        })

    def bucket(offset: int) -> Optional[int]:
        for i, (s, e) in enumerate(spans):
            if s <= offset < e:
                return i
        return None

    for name, off in defs:
        i = bucket(off)
        if i is not None:
            out[i]["defs"].add(name)
    for name, off in refs:
        i = bucket(off)
        if i is not None:
            out[i]["refs"].add(name)

    for c in out:
        c["defs"] = sorted(c["defs"])
        c["refs"] = sorted(c["refs"])
    return out
