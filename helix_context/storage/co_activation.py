"""Co-activation graph updates and queries.

Extracted from knowledge_store.py (approach C: standalone functions).
Functions take explicit ``conn`` / ``cur`` parameters rather than
``self`` so KnowledgeStore can delegate without a mixin.

SQL table and column names (``harmonic_links``, ``gene_relations``,
``entity_graph``) are the on-disk contract and remain untouched.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

from ..accel import parse_epigenetics
from ..schemas import ChromatinState, Gene
from ..backends.sema_codec import decode_embedding

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Entity graph: auto-link documents sharing entities
# ---------------------------------------------------------------------------

def auto_link_by_entity(
    gene_id: str,
    entities: List[str],
    cur: sqlite3.Cursor,
) -> None:
    """Find documents that share 2+ entities with *gene_id* and create
    co-activation links (relation = COVER = 5).
    """
    if len(entities) < 2:
        return

    ent_lower = [e.lower() for e in entities[:15]]
    placeholders = ",".join("?" * len(ent_lower))

    rows = cur.execute(
        f"SELECT gene_id, COUNT(*) as shared "
        f"FROM entity_graph "
        f"WHERE entity IN ({placeholders}) AND gene_id != ? "
        f"GROUP BY gene_id "
        f"HAVING shared >= 2 "
        f"ORDER BY shared DESC "
        f"LIMIT 10",
        ent_lower + [gene_id],
    ).fetchall()

    for r in rows:
        peer_id = r["gene_id"]
        shared_count = r["shared"]
        confidence = min(shared_count / len(ent_lower), 1.0)
        cur.execute(
            "INSERT OR REPLACE INTO gene_relations "
            "(gene_id_a, gene_id_b, relation, confidence, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (gene_id, peer_id, 5, confidence, time.time()),
        )


# ---------------------------------------------------------------------------
# Entity graph: expand retrieval by entity overlap (1-hop)
# ---------------------------------------------------------------------------

def expand_by_entity_graph(
    gene_ids: List[str],
    limit: int,
    cur: sqlite3.Cursor,
) -> List[str]:
    """Given retrieved document IDs, find additional documents that share
    entities with them via 1-hop graph traversal.
    """
    if not gene_ids:
        return []

    id_ph = ",".join("?" * len(gene_ids))

    rows = cur.execute(
        f"SELECT DISTINCT entity FROM entity_graph WHERE gene_id IN ({id_ph})",
        gene_ids,
    ).fetchall()
    entities = [r["entity"] for r in rows]

    if not entities:
        return []

    ent_ph = ",".join("?" * len(entities))

    neighbor_rows = cur.execute(
        f"SELECT gene_id, COUNT(*) as shared "
        f"FROM entity_graph "
        f"WHERE entity IN ({ent_ph}) AND gene_id NOT IN ({id_ph}) "
        f"GROUP BY gene_id "
        f"HAVING shared >= 2 "
        f"ORDER BY shared DESC "
        f"LIMIT ?",
        entities + gene_ids + [limit],
    ).fetchall()

    return [r["gene_id"] for r in neighbor_rows]


# ---------------------------------------------------------------------------
# Co-activation expansion (pull-forward)
# ---------------------------------------------------------------------------

def expand_coactivated(
    genes: List[Gene],
    limit: int,
    conn: sqlite3.Connection,
    entity_graph_enabled: bool,
) -> List[Gene]:
    """Expand retrieved gene list with co-activated neighbours.

    The caller (KnowledgeStore.query_docs) passes ``conn`` (always master)
    and a ``row_to_gene`` callable so this module stays decoupled from the
    KnowledgeStore instance.
    """
    from ..accel import parse_promoter, parse_epigenetics, json_loads
    from ..schemas import PromoterTags, EpigeneticMarkers

    cur = conn.cursor()

    existing_ids = {g.gene_id for g in genes}
    additional_ids: set[str] = set()

    for g in genes:
        if g.epigenetics.typed_co_activated:
            for link in g.epigenetics.typed_co_activated[:5]:
                if link.gene_id in existing_ids:
                    continue
                if link.relation in (0, 1, 2):
                    additional_ids.add(link.gene_id)
                elif link.relation == 3:
                    pass
                elif link.confidence > 0.7:
                    additional_ids.add(link.gene_id)
        else:
            for gid in g.epigenetics.co_activated_with[:3]:
                if gid not in existing_ids:
                    additional_ids.add(gid)

    if entity_graph_enabled:
        try:
            graph_ids = expand_by_entity_graph(
                [g.gene_id for g in genes],
                limit=5,
                cur=cur,
            )
            additional_ids.update(gid for gid in graph_ids if gid not in existing_ids)
        except Exception:
            log.debug("Entity graph expansion failed", exc_info=True)

    # WS2: pull in the definitions that retrieved code chunks reference. A
    # SYMBOL_REF edge points from a referencing chunk to the chunk that defines
    # the called symbol, so a hit on the caller surfaces the definition it needs
    # (the cross-file/cross-chunk case lexical term-overlap misses). Data-gated:
    # no SYMBOL_REF edges (prose genomes) -> no-op.
    try:
        from ..schemas import StructuralRelation
        cand_ids = [g.gene_id for g in genes]
        if cand_ids:
            ph = ",".join("?" * len(cand_ids))
            sref = cur.execute(
                f"SELECT DISTINCT gene_id_b FROM gene_relations "
                f"WHERE relation = ? AND gene_id_a IN ({ph})",
                (int(StructuralRelation.SYMBOL_REF), *cand_ids),
            ).fetchall()
            additional_ids.update(
                row[0] for row in sref if row[0] not in existing_ids
            )
    except Exception:
        log.debug("Symbol-graph (SYMBOL_REF) expansion failed", exc_info=True)

    if not additional_ids:
        return genes

    placeholders = ",".join("?" * len(additional_ids))
    rows = cur.execute(
        f"""
        SELECT * FROM genes
        WHERE gene_id IN ({placeholders})
          AND chromatin < ?
        """,
        (*additional_ids, int(ChromatinState.HETEROCHROMATIN)),
    ).fetchall()

    # Import row_to_gene lazily to avoid circular imports at module level.
    # The function lives in the KnowledgeStore class but the caller passes
    # it indirectly -- here we do a lightweight inline conversion that
    # mirrors _row_to_gene exactly.
    extra: List[Gene] = []
    for r in rows:
        try:
            extra.append(_row_to_gene_inline(r))
        except Exception:
            log.debug("row_to_gene failed in coactivation expansion", exc_info=True)
    return genes + extra


def _row_to_gene_inline(row: sqlite3.Row) -> Gene:
    """Lightweight row-to-Gene converter used by expand_coactivated.

    Mirrors KnowledgeStore._row_to_gene exactly.  Kept here to avoid
    a circular import back into the class.
    """
    from ..accel import parse_promoter, parse_epigenetics, json_loads
    from ..schemas import PromoterTags, EpigeneticMarkers

    def _opt(key: str, default=None):
        try:
            return row[key]
        except (IndexError, KeyError):
            return default

    try:
        promoter = parse_promoter(row["promoter"]) if row["promoter"] else PromoterTags()
    except Exception:
        promoter = PromoterTags()
    try:
        epigenetics = parse_epigenetics(row["epigenetics"]) if row["epigenetics"] else EpigeneticMarkers()
    except Exception:
        epigenetics = EpigeneticMarkers()
    try:
        chromatin = ChromatinState(row["chromatin"]) if row["chromatin"] is not None else ChromatinState.OPEN
    except (ValueError, TypeError):
        chromatin = ChromatinState.OPEN

    try:
        kv_raw = row["key_values"]
        key_values = json_loads(kv_raw) if kv_raw else []
    except (IndexError, KeyError):
        key_values = []

    return Gene(
        gene_id=row["gene_id"],
        content=row["content"] or "",
        complement=row["complement"] or "",
        codons=json_loads(row["codons"]) if row["codons"] else [],
        promoter=promoter,
        epigenetics=epigenetics,
        chromatin=chromatin,
        is_fragment=bool(row["is_fragment"]) if row["is_fragment"] is not None else False,
        embedding=decode_embedding(row["embedding"]),
        source_id=row["source_id"],
        repo_root=_opt("repo_root"),
        source_kind=_opt("source_kind"),
        observed_at=_opt("observed_at"),
        mtime=_opt("mtime"),
        content_hash=_opt("content_hash"),
        volatility_class=_opt("volatility_class"),
        authority_class=_opt("authority_class"),
        support_span=_opt("support_span"),
        last_verified_at=_opt("last_verified_at"),
        version=row["version"] if row["version"] is not None else 1,
        supersedes=row["supersedes"],
        key_values=key_values,
    )


# ---------------------------------------------------------------------------
# Harmonic weights (cymatics)
# ---------------------------------------------------------------------------

def store_harmonic_weights(
    conn: sqlite3.Connection,
    weights: List[Tuple[str, str, float]],
) -> None:
    """Store weighted co-activation edges from cymatics spectral overlap.

    As of Sprint 4, edges carry provenance and Hebbian evidence counters:
      source            - 'seeded' | 'co_retrieved' | 'cwola_validated'
      co_count          - # times both endpoints co-retrieved in a query
      miss_count        - fractional (dense-rank weighted) miss events
      created_at        - epoch seconds
    """
    if not weights:
        return
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS harmonic_links (
            gene_id_a  TEXT NOT NULL,
            gene_id_b  TEXT NOT NULL,
            weight     REAL NOT NULL,
            updated_at REAL NOT NULL,
            source     TEXT NOT NULL DEFAULT 'co_retrieved',
            co_count   INTEGER NOT NULL DEFAULT 0,
            miss_count REAL NOT NULL DEFAULT 0.0,
            created_at REAL,
            PRIMARY KEY (gene_id_a, gene_id_b)
        )
    """)
    for col, defn in (
        ("source", "TEXT NOT NULL DEFAULT 'co_retrieved'"),
        ("co_count", "INTEGER NOT NULL DEFAULT 0"),
        ("miss_count", "REAL NOT NULL DEFAULT 0.0"),
        ("created_at", "REAL"),
    ):
        try:
            cur.execute(f"ALTER TABLE harmonic_links ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass
    now = time.time()
    for a, b, w in weights:
        cur.execute(
            """INSERT INTO harmonic_links
               (gene_id_a, gene_id_b, weight, updated_at, source, created_at)
               VALUES (?, ?, ?, ?, 'co_retrieved', ?)
               ON CONFLICT(gene_id_a, gene_id_b) DO UPDATE SET
                 weight = excluded.weight,
                 updated_at = excluded.updated_at""",
            (a, b, w, now, now),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Typed document relations (NLI)
# ---------------------------------------------------------------------------

def store_relation(
    conn: sqlite3.Connection,
    gene_id_a: str,
    gene_id_b: str,
    relation: int,
    confidence: float,
) -> None:
    """Store a typed logical relation between two documents."""
    conn.execute(
        "INSERT OR REPLACE INTO gene_relations "
        "(gene_id_a, gene_id_b, relation, confidence, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (gene_id_a, gene_id_b, relation, confidence, time.time()),
    )
    conn.commit()


def store_relations_batch(
    conn: sqlite3.Connection,
    relations: list,
) -> None:
    """Store multiple typed relations. Each item: (id_a, id_b, relation, confidence)."""
    now = time.time()
    conn.executemany(
        "INSERT OR REPLACE INTO gene_relations "
        "(gene_id_a, gene_id_b, relation, confidence, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [(a, b, r, c, now) for a, b, r, c in relations],
    )
    conn.commit()


def get_relations(conn: sqlite3.Connection, gene_id: str) -> list:
    """Get all typed relations for a document.
    Returns [(other_id, relation, confidence)].
    """
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT gene_id_b AS other, relation, confidence "
        "FROM gene_relations WHERE gene_id_a = ? "
        "UNION "
        "SELECT gene_id_a AS other, relation, confidence "
        "FROM gene_relations WHERE gene_id_b = ?",
        (gene_id, gene_id),
    ).fetchall()
    return [(r["other"], r["relation"], r["confidence"]) for r in rows]
