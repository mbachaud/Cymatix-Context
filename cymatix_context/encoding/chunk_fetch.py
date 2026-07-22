"""Chunk-level retrieval helpers.

Full-stack composition can often answer from relevant genes/chunks
without rereading the whole source file. This module fetches a small
set of document chunks using tags plus FTS as bounded lexical
rescue.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from ..accel import expand_query_terms, extract_query_signals


@dataclass(frozen=True)
class ChunkHit:
    gene_id: str
    source_id: str | None
    content: str
    score: float


def _match_expr(terms: list[str]) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        t = term.strip().lower()
        if len(t) <= 2 or t in seen:
            continue
        seen.add(t)
        out.append(t.replace('"', '""'))
    return " OR ".join(f'"{term}"' for term in out)


def fetch_relevant_chunks(
    query: str,
    *,
    genome_path: str,
    limit: int = 8,
) -> list[ChunkHit]:
    """Return relevant document chunks for a query, ordered by lexical signal."""
    terms = expand_query_terms(sum(extract_query_signals(query), []))
    if not terms or limit <= 0:
        return []

    hits: dict[str, ChunkHit] = {}
    conn = sqlite3.connect(genome_path)
    try:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in terms)
        promoter_rows = conn.execute(
            f"""SELECT g.gene_id, g.source_id, g.content,
                       COUNT(DISTINCT pi.tag_value) AS hits
                FROM promoter_index pi
                JOIN genes g ON g.gene_id = pi.gene_id
                WHERE pi.tag_value IN ({placeholders})
                GROUP BY g.gene_id
                ORDER BY hits DESC
                LIMIT ?""",
            (*terms, max(limit * 3, limit)),
        ).fetchall()
        for row in promoter_rows:
            hits[row["gene_id"]] = ChunkHit(
                gene_id=row["gene_id"],
                source_id=row["source_id"],
                content=row["content"] or "",
                score=float(row["hits"]),
            )

        match_expr = _match_expr(terms)
        if match_expr:
            fts_rows = conn.execute(
                """SELECT g.gene_id, g.source_id, g.content,
                          bm25(genes_fts) AS rank
                   FROM genes_fts f
                   JOIN genes g ON g.gene_id = f.gene_id
                   WHERE f.genes_fts MATCH ?
                   ORDER BY bm25(genes_fts)
                   LIMIT ?""",
                (match_expr, max(limit * 3, limit)),
            ).fetchall()
            for row in fts_rows:
                # bm25 is lower-is-better and commonly negative in SQLite FTS.
                score = 1.0 / (1.0 + abs(float(row["rank"])))
                existing = hits.get(row["gene_id"])
                if existing is None:
                    hits[row["gene_id"]] = ChunkHit(
                        gene_id=row["gene_id"],
                        source_id=row["source_id"],
                        content=row["content"] or "",
                        score=score,
                    )
                else:
                    hits[row["gene_id"]] = ChunkHit(
                        gene_id=existing.gene_id,
                        source_id=existing.source_id,
                        content=existing.content,
                        score=existing.score + score,
                    )
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

    return sorted(hits.values(), key=lambda hit: hit.score, reverse=True)[:limit]
