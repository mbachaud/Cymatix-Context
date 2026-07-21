"""
Headroom bridge — CPU-resident semantic compression for document content.

Replaces the legacy character-level truncation (``g.content[:1000]``) at the
retrieval seam with query-agnostic semantic compression via the Headroom
toolkit (Apache-2.0) by Tejas Chopra.

Upstream: https://github.com/chopratejas/headroom
PyPI:     https://pypi.org/project/headroom-ai/

Headroom is an optional dependency (``helix-context[codec]``). When it is not
installed, ``compress_text`` falls back to the legacy truncation so the rest
of the pipeline keeps working.

Dispatch by ``content_type`` hint (drawn from ``gene.promoter.domains``):

    code / python / rust / js / ts / go  → CodeAwareCompressor
    log / pytest / build / npm / cargo   → LogCompressor
    diff / patch                         → DiffCompressor
    anything else                        → KompressCompressor (ModernBERT ONNX)

All specialists are loaded lazily on first use and cached as module-level
singletons. Headroom itself guards the Kompress model load with an internal
lock, so repeated calls from the async request path are safe.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Iterable, Optional

log = logging.getLogger(__name__)

# ── Availability probe (cheap, no model download) ──────────────────────

_HEADROOM_IMPORT_LOCK = threading.Lock()
_HEADROOM_AVAILABLE: Optional[bool] = None  # tri-state: None = not probed yet

_TRUTHY = {"1", "true", "yes", "on"}


def _headroom_disabled_by_env() -> bool:
    """Honor HELIX_DISABLE_HEADROOM=1 for A/B benchmarking.

    When set to a truthy value (1/true/yes/on), compress_text bypasses all
    Headroom specialists and falls through to legacy character-level
    truncation. Useful for comparing v0.3.0b4-equivalent behavior against
    v0.3.0b5 on the same knowledge store without reverting code.
    """
    return os.environ.get("HELIX_DISABLE_HEADROOM", "").lower() in _TRUTHY


def is_headroom_available() -> bool:
    """Return True iff headroom-ai is importable AND not explicitly disabled.

    Probed once and cached (module-level), except for the env override check
    which is re-evaluated on every call so tests and benchmarks can toggle
    behavior per-process without a module reload.

    Does NOT load the Kompress model — that only happens on first compress call.
    """
    # Env override re-checked every call (cheap: os.environ lookup + str.lower)
    if _headroom_disabled_by_env():
        return False

    global _HEADROOM_AVAILABLE
    if _HEADROOM_AVAILABLE is not None:
        return _HEADROOM_AVAILABLE
    with _HEADROOM_IMPORT_LOCK:
        if _HEADROOM_AVAILABLE is not None:
            return _HEADROOM_AVAILABLE
        try:
            import headroom  # noqa: F401
            _HEADROOM_AVAILABLE = True
        except ImportError:
            log.info(
                "headroom-ai not installed — falling back to truncation. "
                "Install with: pip install helix-context[codec]"
            )
            _HEADROOM_AVAILABLE = False
    return _HEADROOM_AVAILABLE


# ── Specialist singletons (lazy) ────────────────────────────────────────

_SPECIALIST_LOCK = threading.Lock()
_KOMPRESS = None
_LOG_COMPRESSOR = None
_DIFF_COMPRESSOR = None
_CODE_COMPRESSOR = None


def _get_kompress():
    global _KOMPRESS
    if _KOMPRESS is not None:
        return _KOMPRESS
    with _SPECIALIST_LOCK:
        if _KOMPRESS is None:
            from headroom.transforms.kompress_compressor import KompressCompressor
            _KOMPRESS = KompressCompressor()
    return _KOMPRESS


def _get_log_compressor():
    global _LOG_COMPRESSOR
    if _LOG_COMPRESSOR is not None:
        return _LOG_COMPRESSOR
    with _SPECIALIST_LOCK:
        if _LOG_COMPRESSOR is None:
            from headroom.transforms.log_compressor import LogCompressor
            _LOG_COMPRESSOR = LogCompressor()
    return _LOG_COMPRESSOR


def _get_diff_compressor():
    global _DIFF_COMPRESSOR
    if _DIFF_COMPRESSOR is not None:
        return _DIFF_COMPRESSOR
    with _SPECIALIST_LOCK:
        if _DIFF_COMPRESSOR is None:
            from headroom.transforms.diff_compressor import DiffCompressor
            _DIFF_COMPRESSOR = DiffCompressor()
    return _DIFF_COMPRESSOR


def _get_code_compressor():
    global _CODE_COMPRESSOR
    if _CODE_COMPRESSOR is not None:
        return _CODE_COMPRESSOR
    with _SPECIALIST_LOCK:
        if _CODE_COMPRESSOR is None:
            from headroom.transforms.code_compressor import CodeAwareCompressor
            _CODE_COMPRESSOR = CodeAwareCompressor()
    return _CODE_COMPRESSOR


# ── Dispatch ────────────────────────────────────────────────────────────

_CODE_DOMAINS = frozenset({
    "code", "python", "rust", "javascript", "js", "typescript", "ts",
    "go", "java", "cpp", "c", "sql", "shell", "bash",
})
_LOG_DOMAINS = frozenset({
    # Strictly log-output-signaling tokens. Tool names like "cargo", "npm",
    # "build" are excluded because they also appear in code documents (e.g. a
    # Rust source file naturally has tags domain "cargo" from Cargo.toml).
    "log", "logs", "stderr", "stdout", "pytest", "jest", "traceback",
})
_DIFF_DOMAINS = frozenset({"diff", "patch", "git_diff"})

_LANGUAGE_HINTS = {
    "python": "python", "py": "python",
    "rust": "rust", "rs": "rust",
    "javascript": "javascript", "js": "javascript",
    "typescript": "typescript", "ts": "typescript",
    "go": "go", "java": "java", "cpp": "cpp", "c": "c",
}


def _pick_specialist(domains: Iterable[str]) -> str:
    """Return one of: 'log' | 'diff' | 'kompress'.

    NOTE (2026-04-12): CodeAwareCompressor disabled. Headroom 0.5.23
    changelog confirmed AST-based code compression produces invalid
    syntax on ~40% of real files. Code documents now fall through to
    Kompress (ModernBERT), which handles code adequately without
    corrupting syntax. See headroom-ai changelog for 0.5.23.
    """
    domains_set = {d.lower() for d in domains if d}
    if domains_set & _DIFF_DOMAINS:
        return "diff"
    if domains_set & _LOG_DOMAINS:
        return "log"
    # CodeAwareCompressor disabled — 40% invalid syntax rate on real files.
    # Code domains now route to Kompress (same as prose).
    return "kompress"


def _detect_language(domains: Iterable[str]) -> Optional[str]:
    for d in domains:
        hint = _LANGUAGE_HINTS.get(d.lower())
        if hint:
            return hint
    return None


# ── Public API ──────────────────────────────────────────────────────────

# Fraction of ``target_chars`` reserved for query-term lines rescued from
# beyond the prefix cut (J-space council kill-switch #1). The head keeps
# the remaining ≥60% so document framing survives.
_TRIM_TAIL_FRACTION = 0.4
_TRIM_SEPARATOR = "\n[...]\n"


def _query_aware_trim(
    content: str,
    target_chars: int,
    query_terms: Iterable[str],
) -> str:
    """Prefix cut that refuses to silently lose query-term lines.

    The legacy fallback ``content[:target_chars].strip()`` was
    query-agnostic: any answer past the cut was dropped — 6/50 SIKE xl
    needles had gold delivered with the answer truncated away. When
    ``query_terms`` are supplied and at least one is absent from the
    kept prefix but present on a line past the cut, that line is
    rescued into a tail block (document order, up to
    ``_TRIM_TAIL_FRACTION`` of the budget) and the head shrinks to keep
    the total within ``target_chars``. With no terms, or all terms
    already in the prefix, the output is byte-identical to the legacy
    cut.
    """
    prefix = content[:target_chars]
    terms = [t.lower() for t in (query_terms or []) if t and len(t) >= 3]
    if not terms:
        return prefix.strip()
    prefix_lower = prefix.lower()
    missing = [t for t in terms if t not in prefix_lower]
    if not missing:
        return prefix.strip()

    tail_budget = max(0, int(target_chars * _TRIM_TAIL_FRACTION) - len(_TRIM_SEPARATOR))
    kept: list = []
    used = 0
    pos = 0
    for line in content.splitlines(keepends=True):
        end = pos + len(line)
        # Only lines that would be clipped or lost by the prefix cut.
        if end > target_chars:
            line_lower = line.lower()
            if any(t in line_lower for t in missing):
                stripped = line.strip()
                if stripped:
                    cost = len(stripped) + 1  # newline joiner
                    if used + cost <= tail_budget:
                        kept.append(stripped)
                        used += cost
        pos = end
    if not kept:
        return prefix.strip()
    tail_block = "\n".join(kept)
    head_budget = target_chars - len(_TRIM_SEPARATOR) - len(tail_block)
    head = content[: max(0, head_budget)]
    return (head.rstrip() + _TRIM_SEPARATOR + tail_block).strip()


def compress_text(
    content: str,
    target_chars: int = 1000,
    content_type: Optional[Iterable[str]] = None,
    query_terms: Optional[Iterable[str]] = None,
) -> str:
    """Compress ``content`` to approximately ``target_chars`` characters.

    Parameters
    ----------
    content : str
        Raw text to compress.
    target_chars : int
        Soft cap on output length. Matches the historical ``content[:1000]``
        truncation so callers can swap 1:1 without budget changes.
    content_type : iterable of str, optional
        Hints about the content's nature, typically ``gene.promoter.domains``
        (e.g. ``["python", "code"]`` or ``["log", "pytest"]``). Determines
        which specialist is invoked. If None, falls through to Kompress.
    query_terms : iterable of str, optional
        Lowercase query keywords. When a truncation is required, lines
        containing these terms are retained (see ``_query_aware_trim``)
        instead of being lost to a blind prefix cut. ``None`` preserves
        the legacy query-agnostic behavior byte-for-byte.

    Returns
    -------
    str
        Compressed content. Guaranteed to be non-empty if ``content`` was
        non-empty; guaranteed to be shorter than or equal to ``content`` in
        the happy path, and falls back to a (query-aware) truncation on any
        error or if headroom is unavailable.
    """
    if not content:
        return ""

    # Fast path: content already under budget
    if len(content) <= target_chars:
        return content

    # Fallback path: headroom not installed
    if not is_headroom_available():
        return _query_aware_trim(content, target_chars, query_terms or [])

    domains = list(content_type) if content_type else []
    specialist = _pick_specialist(domains)

    try:
        if specialist == "code":
            language = _detect_language(domains)
            result = _get_code_compressor().compress(content, language=language)
            compressed = _extract_result_text(result)
        elif specialist == "log":
            result = _get_log_compressor().compress(content)
            compressed = _extract_result_text(result)
        elif specialist == "diff":
            result = _get_diff_compressor().compress(content)
            compressed = _extract_result_text(result)
        else:
            # Kompress aims for target_ratio = target_chars / len(content),
            # clamped so we never ask for 0% or >100%
            ratio = max(0.1, min(1.0, target_chars / max(len(content), 1)))
            result = _get_kompress().compress(content, target_ratio=ratio)
            compressed = _extract_result_text(result)
    except Exception:
        log.warning(
            "Headroom compression failed (specialist=%s), falling back to truncation",
            specialist,
            exc_info=True,
        )
        return _query_aware_trim(content, target_chars, query_terms or [])

    if not compressed:
        return _query_aware_trim(content, target_chars, query_terms or [])

    # Soft-cap: if the specialist returned more than target_chars, truncate
    # the compressed output. Still much better than raw truncation because
    # the first ``target_chars`` of a semantically-compressed string carry
    # more signal than the first ``target_chars`` of raw content.
    if len(compressed) > target_chars * 1.5:
        compressed = compressed[:target_chars].strip()

    return compressed


def _extract_result_text(result) -> str:
    """Pull the compressed string out of any Headroom *Result dataclass.

    Headroom's four specialists all return result objects with a
    ``compressed`` attribute, but we use getattr-with-fallback to stay
    robust against upstream schema drift.
    """
    for attr in ("compressed", "compressed_text", "output", "text"):
        value = getattr(result, attr, None)
        if isinstance(value, str) and value:
            return value
    # Last resort — str() the whole result
    return str(result) if result else ""
