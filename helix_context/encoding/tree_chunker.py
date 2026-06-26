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

    Piece = Tuple[str, bool, Optional[dict]]

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

    def char_cut(start: int, end: int) -> List[Piece]:
        """Last-resort hard cut of an atomic oversized span at char boundaries."""
        out: List[Piece] = []
        s = start
        while end - s > max_chars:
            out.append((text_of(s, s + max_chars), True, None))
            s += max_chars
        if end > s:
            out.append((text_of(s, end), False, None))
        return out

    def emit_gap(start: int, end: int, pieces: List[Piece]) -> None:
        """Interstitial text between AST children (imports, comments, blanks)."""
        if start >= end:
            return
        if not text_of(start, end).strip():
            return
        if end - start > max_chars:
            pieces.extend(char_cut(start, end))
        else:
            pieces.append((text_of(start, end), False, None))

    def split(node) -> List[Piece]:
        if node.end_byte - node.start_byte <= max_chars:
            return [(text_of(node.start_byte, node.end_byte), False, meta_for(node))]
        kids = list(node.children)
        if not kids:
            return char_cut(node.start_byte, node.end_byte)
        pieces: List[Piece] = []
        cursor = node.start_byte
        for c in kids:
            emit_gap(cursor, c.start_byte, pieces)
            pieces.extend(split(c))
            cursor = c.end_byte
        emit_gap(cursor, node.end_byte, pieces)
        return pieces

    pieces = split(root)
    if not pieces:
        return [(code, False, None)]

    # Greedy merge: combine adjacent pieces up to max_chars. A merged chunk
    # inherits the metadata of its first definition-bearing piece, and is a
    # fragment if any constituent was hard-cut.
    merged: List[Piece] = []
    cur_text = ""
    cur_frag = False
    cur_meta: Optional[dict] = None
    for text, frag, meta in pieces:
        if cur_text and len(cur_text) + len(text) > max_chars:
            if cur_text.strip():
                merged.append((cur_text.strip(), cur_frag, cur_meta))
            cur_text, cur_frag, cur_meta = "", False, None
        cur_text += text
        cur_frag = cur_frag or frag
        if cur_meta is None and meta is not None:
            cur_meta = meta
    if cur_text.strip():
        merged.append((cur_text.strip(), cur_frag, cur_meta))
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
