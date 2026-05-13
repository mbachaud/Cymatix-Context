"""Bounded lexical rescue for full-stack source fetching.

Helix should not become plain BM25, but BM25 is an excellent safety net
for tiny literal needles. This module returns a small ordered list of
source_ids from ``genes_fts`` so callers can merge them after packet
sources and before DAL fetch.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable

from ..accel import expand_query_terms, extract_query_signals

_CONFIG_EXTENSIONS = (".toml", ".yaml", ".yml", ".json", ".ini", ".env", ".bat")


def normalize_source_id(source_id: str | None) -> str:
    """Normalize source ids for dedupe across slash/case variants."""
    return (source_id or "").replace("\\", "/").lower()


def merge_source_ids(*groups: Iterable[str | None], max_sources: int = 12) -> list[str]:
    """Merge source id groups in order, deduping by normalized path."""
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for source_id in group:
            if not source_id:
                continue
            key = normalize_source_id(source_id)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(source_id)
            if len(out) >= max_sources:
                return out
    return out


def _fts_match_expr(query: str) -> str:
    domains, entities = extract_query_signals(query)
    terms = expand_query_terms(list(domains) + list(entities))
    keep = []
    seen: set[str] = set()
    for term in terms:
        t = term.strip().lower()
        if len(t) <= 2 or t in seen:
            continue
        seen.add(t)
        keep.append(t.replace('"', '""'))
    return " OR ".join(f'"{term}"' for term in keep)


def _source_path_bonus(source_id: str, query_terms: set[str]) -> float:
    path = normalize_source_id(source_id)
    score = 0.0
    if path.endswith(_CONFIG_EXTENSIONS):
        score += 1.5
    if any(t in query_terms for t in {"port", "ports", "config", "configuration"}):
        if path.endswith(_CONFIG_EXTENSIONS):
            score += 1.0
    # Prefer same-path lexical agreement for source-level rescue. This
    # helps project config files beat generic docs/tests with similar tags.
    for term in query_terms:
        if len(term) > 3 and term in path:
            score += 0.4
    if "helix" in query_terms and "helix-context" in path:
        score += 2.0
    if "helix" in query_terms and path.endswith("/helix.toml"):
        score += 2.0
    if "/_worktrees/" in path:
        score -= 1.0
    if "/tests/" in path or "\\tests\\" in source_id.lower():
        score -= 0.75
    return score


def lexical_rescue_sources(
    query: str,
    *,
    genome_path: str,
    limit: int = 4,
    exclude_source_ids: Iterable[str | None] = (),
) -> list[str]:
    """Return a tiny BM25-ranked source-id rescue list.

    ``exclude_source_ids`` lets callers keep Helix packet sources first
    and only use BM25 to fill gaps.
    """
    match_expr = _fts_match_expr(query)
    if not match_expr:
        return []

    exclude = {normalize_source_id(s) for s in exclude_source_ids if s}
    out: list[str] = []
    seen: set[str] = set(exclude)
    conn = sqlite3.connect(genome_path)
    try:
        terms = expand_query_terms(sum(extract_query_signals(query), []))
        term_set = set(terms)
        promoter_candidates: list[str] = []
        if terms:
            placeholders = ",".join("?" for _ in terms)
            promoter_rows = conn.execute(
                f"""SELECT g.source_id, COUNT(DISTINCT pi.tag_value) AS hits
                    FROM promoter_index pi
                    JOIN genes g ON g.gene_id = pi.gene_id
                    WHERE pi.tag_value IN ({placeholders})
                      AND g.source_id IS NOT NULL
                    GROUP BY g.source_id
                    ORDER BY hits DESC
                    LIMIT ?""",
                (*terms, max(limit * 64, limit)),
            ).fetchall()
            term_set = set(terms)
            promoter_rows = sorted(
                promoter_rows,
                key=lambda row: (
                    float(row[1]) + _source_path_bonus(row[0], term_set)
                ),
                reverse=True,
            )
            for source_id, _hits in promoter_rows:
                promoter_candidates.append(source_id)

        rows = conn.execute(
            """SELECT g.source_id
               FROM genes_fts f JOIN genes g ON g.gene_id = f.gene_id
               WHERE f.genes_fts MATCH ?
                 AND g.source_id IS NOT NULL
               ORDER BY bm25(genes_fts)
               LIMIT ?""",
            (match_expr, max(limit * 4, limit)),
        ).fetchall()
        fts_candidates = [source_id for (source_id,) in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

    configish_query = bool(
        term_set & {"port", "ports", "config", "configuration", "listen", "listens"}
    )
    ordered_groups = (
        (promoter_candidates[:2], fts_candidates, promoter_candidates[2:])
        if configish_query
        else (fts_candidates, promoter_candidates)
    )
    for group in ordered_groups:
        for source_id in group:
            key = normalize_source_id(source_id)
            if key and key not in seen:
                seen.add(key)
                out.append(source_id)
                if len(out) >= limit:
                    return out
    return out
