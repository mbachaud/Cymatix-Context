"""
Fragments — Semantic chunking and fragment encoding.

Two distinct roles:
    1. CodonChunker  — restriction enzyme: cuts raw text into RawStrands
       (pre-document chunks sized for the compressor to process)
    2. CodonEncoder  — serialization: converts fragment meaning labels into
       prompt-ready strings for the big model

Bio analogue (legacy term: codon):
    DNA codons are nucleotide triplets mapping to amino acids.
    Our fragments are semantic groups mapping to meaning labels.
    The compressor (small model) does the actual encoding;
    this module provides the chunking and serialization primitives.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

log = logging.getLogger(__name__)

from ..accel import (
    RE_PARAGRAPH_SPLIT,
    RE_SENTENCE_SPLIT,
    RE_CODE_BOUNDARY,
    RE_CODE_BOUNDARY_MATCH,
    RE_CODE_BLOCK_SPLIT,
)


# ── Pre-document chunk (output of chunking, input to compressor) ──────────

@dataclass
class RawStrand:
    """A pre-document chunk of raw text waiting for Compressor translation."""
    content: str
    sequence_index: int
    is_fragment: bool
    content_type: str           # "text", "code", "conversation"
    metadata: Dict[str, Any] = field(default_factory=dict)


# ── Encoded fragment (output of compressor pack) ─────────────────────────

@dataclass
class Codon:
    """A semantic unit — the fundamental piece of compressed context."""
    tokens: List[str]           # Raw tokens that make up this fragment
    meaning: str                # Semantic label / compressed representation
    weight: float = 1.0         # 1.0=critical, 0.5=useful, 0.1=filler
    is_exon: bool = True        # True=load-bearing, False=can be spliced out


# ── Chunker ─────────────────────────────────────────────────────────

class CodonChunker:
    """
    Restriction enzyme — cuts raw content into RawStrands.

    Domain-aware: text (paragraphs), code (functions/classes),
    conversation (turn pairs). Each strategy preserves reading order
    via sequence_index and flags forced cuts via is_fragment.
    """

    def __init__(self, max_chars_per_strand: int = 4000,
                 symbol_graph: bool = False):
        # ~4000 chars ≈ ~1000 tokens, safe for a small compressor model
        self.max_chars = max_chars_per_strand
        # WS2 review FIX-3: symbol def/ref extraction is opt-in. The flag
        # gates EXTRACTION here (second parse + symbol walk in
        # chunk_code_with_symbols), not just emission downstream — flag-off
        # code chunking routes through the plain cAST chunker at zero extra
        # cost and attaches no defs/refs metadata. Threaded from
        # [ingestion] symbol_graph by the context manager; default mirrors
        # the config default (dark-shipped, False).
        self.symbol_graph = symbol_graph

    def chunk(
        self,
        content: str,
        content_type: str = "text",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[RawStrand]:
        """Route to the appropriate domain-aware chunking strategy."""
        metadata = metadata or {}
        if content_type == "code":
            return self._chunk_code(content, metadata)
        elif content_type == "conversation":
            return self._chunk_conversation(content, metadata)
        return self._chunk_text(content, metadata)

    # ── Text chunking (paragraph-first, sentence fallback) ──────────

    def _chunk_text(self, text: str, metadata: Dict) -> List[RawStrand]:
        paragraphs = RE_PARAGRAPH_SPLIT.split(text)
        strands: List[RawStrand] = []
        current = ""
        seq = 0

        for p in paragraphs:
            if len(current) + len(p) < self.max_chars:
                current += p + "\n\n"
            else:
                if current:
                    strands.append(RawStrand(
                        content=current.strip(),
                        sequence_index=seq,
                        is_fragment=False,
                        content_type="text",
                        metadata=metadata,
                    ))
                    seq += 1

                if len(p) >= self.max_chars:
                    # Hard cut — polyadenylation trigger. Loop so the
                    # remainder is re-cut until every piece fits the budget.
                    while len(p) >= self.max_chars:
                        strands.append(RawStrand(
                            content=p[: self.max_chars],
                            sequence_index=seq,
                            is_fragment=True,
                            content_type="text",
                            metadata=metadata,
                        ))
                        seq += 1
                        p = p[self.max_chars :]
                    current = p + "\n\n"
                else:
                    current = p + "\n\n"

        if current.strip():
            strands.append(RawStrand(
                content=current.strip(),
                sequence_index=seq,
                is_fragment=False,
                content_type="text",
                metadata=metadata,
            ))

        return strands

    # ── Code chunking (function/class boundary splitting) ───────────

    def _chunk_code(self, code: str, metadata: Dict) -> List[RawStrand]:
        # Tree-sitter AST chunking (opt-in, requires tree-sitter-languages).
        # Falls through to regex path if unavailable or language unknown.
        source_id = metadata.get("path") or metadata.get("source_id")
        try:
            from . import tree_chunker
            if tree_chunker.is_available():
                strands: List[RawStrand] = []
                if self.symbol_graph:
                    # WS2: symbol-aware chunking — each chunk carries the
                    # symbols it defines/references so the ingest path can
                    # build the symbol graph.
                    sym_chunks = tree_chunker.chunk_code_with_symbols(
                        code,
                        max_chars=self.max_chars,
                        source_id=source_id,
                    )
                    for seq, ch in enumerate(sym_chunks):
                        smeta = dict(metadata)
                        if ch["defs"]:
                            smeta["defs"] = ch["defs"]
                        if ch["refs"]:
                            smeta["refs"] = ch["refs"]
                        strands.append(RawStrand(
                            content=ch["text"].strip(),
                            sequence_index=seq,
                            is_fragment=ch["is_fragment"],
                            content_type="code",
                            metadata=smeta,
                        ))
                else:
                    # WS2 review FIX-3: flag off — plain cAST chunking, no
                    # symbol extraction pass, no defs/refs metadata. Chunk
                    # texts are identical either way (chunk_code_with_symbols
                    # wraps this same chunker).
                    ast_blocks = tree_chunker.chunk_code_ast(
                        code,
                        max_chars=self.max_chars,
                        source_id=source_id,
                    )
                    for seq, (block_text, is_fragment) in enumerate(ast_blocks):
                        strands.append(RawStrand(
                            content=block_text.strip(),
                            sequence_index=seq,
                            is_fragment=is_fragment,
                            content_type="code",
                            metadata=metadata,
                        ))
                if strands:
                    # Phase-0 observability (2026-07-01): prove the AST path
                    # fired. Bench assertion on code corpora:
                    # chunk_ast > 0 and chunk_regex == 0.
                    try:
                        from ..telemetry import tier_fired_counter
                        tier_fired_counter().add(1, {"tier": "chunk_ast"})
                    except Exception:
                        pass
                    log.debug("chunking path: ast for %s", source_id)
                    return strands
        except ImportError:
            # tree-sitter not installed
            _fallback_reason = "tree_sitter_unavailable"
        except ValueError:
            # unsupported/undetected language
            _fallback_reason = "unsupported_language"
        except RecursionError:
            # pathologically/adversarially deeply-nested code blowing the
            # recursive split() — degrade to regex, don't abort the
            # document's ingest (council fix).
            _fallback_reason = "recursion_limit"
        else:
            # is_available() False, or AST produced zero strands
            _fallback_reason = "ast_unavailable_or_empty"

        # Phase-0 observability (2026-07-01): the regex fallback is no
        # longer silent — PRD leak-guard 5 / council finding #10 minimum
        # bar. Counter rides the existing helix_tier_fired_total series;
        # the log line is greppable with OTel off.
        try:
            from ..telemetry import tier_fired_counter
            tier_fired_counter().add(1, {"tier": "chunk_regex"})
        except Exception:
            pass
        if _fallback_reason == "tree_sitter_unavailable":
            log.warning(
                "code chunking fell back to regex for %s: tree-sitter not "
                "installed — `pip install helix-context[ast]` for "
                "structure-aware chunks", source_id,
            )
        else:
            log.info(
                "chunking path: regex (%s) for %s", _fallback_reason, source_id,
            )

        # Regex fallback — splits on top-level def/class keywords
        blocks = RE_CODE_BOUNDARY.split(code)

        # Re-stitch split delimiters with their content
        stitched: List[str] = []
        if blocks and not RE_CODE_BOUNDARY_MATCH.match(blocks[0]):
            stitched.append(blocks[0])
            blocks = blocks[1:]

        for i in range(0, len(blocks), 2):
            if i + 1 < len(blocks):
                stitched.append(blocks[i] + blocks[i + 1])
            elif blocks[i].strip():
                stitched.append(blocks[i])

        strands: List[RawStrand] = []
        current = ""
        seq = 0

        for block in stitched:
            if len(current) + len(block) < self.max_chars:
                current += block
            else:
                if current:
                    strands.append(RawStrand(
                        content=current.strip(),
                        sequence_index=seq,
                        is_fragment=False,
                        content_type="code",
                        metadata=metadata,
                    ))
                    seq += 1

                if len(block) >= self.max_chars:
                    # Loop so the remainder is re-cut until every piece
                    # fits the budget (mirrors the text hard-cut path).
                    while len(block) >= self.max_chars:
                        strands.append(RawStrand(
                            content=block[: self.max_chars],
                            sequence_index=seq,
                            is_fragment=True,
                            content_type="code",
                            metadata=metadata,
                        ))
                        seq += 1
                        block = block[self.max_chars :]
                    current = block
                else:
                    current = block

        if current.strip():
            strands.append(RawStrand(
                content=current.strip(),
                sequence_index=seq,
                is_fragment=False,
                content_type="code",
                metadata=metadata,
            ))

        return strands

    # ── Conversation chunking (turn pairs) ──────────────────────────

    def _chunk_conversation(self, conversation: str, metadata: Dict) -> List[RawStrand]:
        # MVP: fall back to text chunking — the proxy layer handles
        # conversations as structured JSON, not raw strings.
        return self._chunk_text(conversation, metadata)


# ── Encoder (serialization for prompt injection) ────────────────────

class CodonEncoder:
    """
    Serializes fragment meaning labels for injection into the big model's prompt.
    Also provides sentence-level chunking used by the compressor's pack operation.
    """

    def __init__(self, chunk_target: int = 3, overlap: int = 0):
        if chunk_target <= 0:
            raise ValueError(
                f"chunk_target must be positive, got {chunk_target}"
            )
        if overlap < 0:
            raise ValueError(f"overlap must be non-negative, got {overlap}")
        # If overlap >= chunk_target the windowed loop in chunk_text()
        # would never advance (i = end - overlap <= i), producing an
        # infinite loop. Clamp instead of raising so existing callers
        # that default to overlap=0 are unaffected.
        if overlap >= chunk_target:
            overlap = chunk_target - 1
        self.chunk_target = chunk_target
        self.overlap = overlap

    def chunk_text(self, text: str) -> List[List[str]]:
        """Split text into sentence groups (proto-fragments for compressor pack)."""
        sentences = self._split_sentences(text)
        if not sentences:
            return []

        groups: List[List[str]] = []
        i = 0
        while i < len(sentences):
            end = min(i + self.chunk_target, len(sentences))
            groups.append(sentences[i:end])
            i = end - self.overlap if self.overlap else end
        return groups

    def chunk_code(self, code: str) -> List[List[str]]:
        """Split code into logical blocks (one block = one fragment group)."""
        blocks = self._split_code_blocks(code)
        return [[b] for b in blocks] if blocks else [[code]]

    def chunk_conversation(self, messages: List[Dict]) -> List[List[str]]:
        """Split conversation into turn-pair groups."""
        groups: List[List[str]] = []
        for i in range(0, len(messages), 2):
            pair = messages[i : i + 2]
            group = [f"{m.get('role', '?')}: {m.get('content', '')}" for m in pair]
            groups.append(group)
        return groups

    def codons_to_sequence(self, codons: List[Codon], exon_only: bool = False) -> str:
        """Serialize fragments into a compact string representation."""
        filtered = [c for c in codons if c.is_exon] if exon_only else codons
        return " ".join(f"[{c.meaning}|w={c.weight:.1f}]" for c in filtered)

    def sequence_to_prompt(self, expressed: str) -> str:
        """Wrap retrieved context for injection into the big model's prompt."""
        return (
            "<expressed_context>\n"
            f"{expressed}\n"
            "</expressed_context>"
        )

    @staticmethod
    def fragment_id(tokens: List[str]) -> str:
        raw = "||".join(tokens)
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    # R3 Stage C legacy alias (same staticmethod descriptor).
    codon_id = fragment_id

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        raw = RE_SENTENCE_SPLIT.split(text.strip())
        return [s.strip() for s in raw if s.strip()]

    @staticmethod
    def _split_code_blocks(code: str) -> List[str]:
        blocks = RE_CODE_BLOCK_SPLIT.split(code)
        return [b.strip() for b in blocks if b.strip()]


# ── Compression metrics ─────────────────────────────────────────────

def compression_ratio(raw_text: str, codons: List[Codon], exon_only: bool = True) -> float:
    """How much we compressed. Higher = more compression."""
    encoder = CodonEncoder()
    compressed = encoder.codons_to_sequence(codons, exon_only=exon_only)
    return len(raw_text) / max(len(compressed), 1)
