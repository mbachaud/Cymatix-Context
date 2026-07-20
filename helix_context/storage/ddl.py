"""DDL — schema creation, migrations, indexes, and auto-repair.

Extracted from knowledge_store.py (approach C: standalone functions).
Every function takes a ``sqlite3.Connection`` and returns results via
return values, never via ``self``.  The KnowledgeStore.__init__ method
calls ``init_db()`` and reads back the ``fts_available`` flag.

SQL table and column names (``genes``, ``gene_id``, ``promoter``,
``epigenetics``, ``chromatin``, ``codons``, ``harmonic_links``, etc.)
are the on-disk contract and remain untouched.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

from ..schemas import ChromatinState

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> bool:
    """Create / migrate all schema objects.  Returns ``fts_available``."""
    cur = conn.cursor()
    _create_genes_table(cur)
    _migrate_genes_columns(cur, conn)
    _create_genes_indexes(cur)
    _create_promoter_index(cur)
    _create_health_log(cur)
    _create_gene_relations(cur)
    _create_entity_graph(cur)
    _create_symbol_defs(cur)
    _create_path_key_index(cur)
    _create_filename_index(cur)
    _create_okf_links(cur)
    _auto_repair(cur, conn)
    fts_available = _create_fts5(cur, conn)

    try:
        ensure_registry_schema(cur, conn)
    except Exception:
        log.warning("Session registry schema init failed", exc_info=True)

    conn.commit()
    return fts_available


# ---------------------------------------------------------------------------
# genes table
# ---------------------------------------------------------------------------

def _create_genes_table(cur: sqlite3.Cursor) -> None:
    cur.execute("""
    CREATE TABLE IF NOT EXISTS genes (
        gene_id      TEXT PRIMARY KEY,
        content      TEXT,
        complement   TEXT,
        codons       TEXT,     -- JSON list[str]
        promoter     TEXT,     -- JSON PromoterTags
        epigenetics  TEXT,     -- JSON EpigeneticMarkers
        chromatin    INTEGER,
        is_fragment  INTEGER,
        embedding    TEXT,     -- JSON list[float] | NULL
        source_id    TEXT,
        repo_root    TEXT,
        source_kind  TEXT,
        observed_at  REAL,
        mtime        REAL,
        content_hash TEXT,
        volatility_class TEXT,
        authority_class  TEXT,
        support_span     TEXT,
        last_verified_at REAL,
        version      INTEGER,
        supersedes   TEXT,
        key_values   TEXT,    -- JSON list[str] | NULL
        embedding_dense TEXT  -- JSON list[float] | NULL (BGE-M3, Step 4)
    )
    """)


def _migrate_genes_columns(cur: sqlite3.Cursor, conn: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"]
        for row in cur.execute("PRAGMA table_info(genes)").fetchall()
    }

    # Auto-add key_values column if upgrading from older schema
    if "key_values" not in existing_columns:
        cur.execute("ALTER TABLE genes ADD COLUMN key_values TEXT")
        log.info("Added key_values column to genes table")
        existing_columns.add("key_values")

    for column_name, column_def in (
        ("repo_root", "TEXT"),
        ("source_kind", "TEXT"),
        ("observed_at", "REAL"),
        ("mtime", "REAL"),
        ("content_hash", "TEXT"),
        ("volatility_class", "TEXT"),
        ("authority_class", "TEXT"),
        ("support_span", "TEXT"),
        ("last_verified_at", "REAL"),
        ("embedding_dense", "TEXT"),  # BGE-M3 dense vector (Step 4, 2026-05-08)
        # Stage 2 (2026-05-08): BLOB column for raw little-endian fp32
        # BGE-M3 vectors at full 1024-dim. 18.9k * 1024 * 4 = 77.6 MiB raw,
        # vs ~600 MiB for JSON-encoded text. np.frombuffer is zero-copy.
        ("embedding_dense_v2", "BLOB"),
    ):
        if column_name not in existing_columns:
            cur.execute(f"ALTER TABLE genes ADD COLUMN {column_name} {column_def}")
            log.info("Added %s column to genes table", column_name)
            existing_columns.add(column_name)

    # Auto-add compression_tier column (0=OPEN, 1=EUCHROMATIN, 2=HETEROCHROMATIN)
    if "compression_tier" not in existing_columns:
        cur.execute("ALTER TABLE genes ADD COLUMN compression_tier INTEGER DEFAULT 0")
        log.info("Added compression_tier column to genes table")
        existing_columns.add("compression_tier")

    # Auto-add last_seen column -- Unix epoch of the most recent retrieval;
    # used by the vault incremental export (Task 9) for efficient range scan.
    if "last_seen" not in existing_columns:
        cur.execute("ALTER TABLE genes ADD COLUMN last_seen REAL")
        log.info("Added last_seen column to genes table")
        existing_columns.add("last_seen")

    # Auto-add live_truth_score column -- freshness score [0.0, 1.0];
    # 1.0 = fully fresh (default). Used by _stale/ view in the vault pruner.
    if "live_truth_score" not in existing_columns:
        cur.execute("ALTER TABLE genes ADD COLUMN live_truth_score REAL DEFAULT 1.0")
        log.info("Added live_truth_score column to genes table")
        existing_columns.add("live_truth_score")


def _create_genes_indexes(cur: sqlite3.Cursor) -> None:
    # Stage 2: partial index over hot-tier rows with v2 vectors.
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_genes_dense_v2_hot "
        "ON genes(gene_id) "
        "WHERE embedding_dense_v2 IS NOT NULL AND chromatin < 2"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_genes_last_seen ON genes(last_seen)"
    )
    # Stage 7 -- partial index over the reverse-supersedes column.
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_genes_supersedes "
        "ON genes(supersedes) WHERE supersedes IS NOT NULL"
    )


# ---------------------------------------------------------------------------
# promoter_index
# ---------------------------------------------------------------------------

def _create_promoter_index(cur: sqlite3.Cursor) -> None:
    cur.execute("""
    CREATE TABLE IF NOT EXISTS promoter_index (
        gene_id   TEXT,
        tag_type  TEXT,   -- 'domain' | 'entity'
        tag_value TEXT
    )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_promoter_value "
        "ON promoter_index(tag_value)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_promoter_gene "
        "ON promoter_index(gene_id)"
    )


# ---------------------------------------------------------------------------
# health_log
# ---------------------------------------------------------------------------

def _create_health_log(cur: sqlite3.Cursor) -> None:
    cur.execute("""
    CREATE TABLE IF NOT EXISTS health_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp  REAL,
        query      TEXT,
        ellipticity REAL,
        coverage   REAL,
        density    REAL,
        freshness  REAL,
        genes_expressed INTEGER,
        genes_available INTEGER,
        status     TEXT
    )
    """)


# ---------------------------------------------------------------------------
# gene_relations
# ---------------------------------------------------------------------------

def _create_gene_relations(cur: sqlite3.Cursor) -> None:
    cur.execute("""
    CREATE TABLE IF NOT EXISTS gene_relations (
        gene_id_a  TEXT,
        gene_id_b  TEXT,
        relation   INTEGER,
        confidence REAL,
        updated_at REAL,
        PRIMARY KEY (gene_id_a, gene_id_b)
    )
    """)


# ---------------------------------------------------------------------------
# entity_graph
# ---------------------------------------------------------------------------

def _create_entity_graph(cur: sqlite3.Cursor) -> None:
    cur.execute("""
    CREATE TABLE IF NOT EXISTS entity_graph (
        entity   TEXT NOT NULL,
        gene_id  TEXT NOT NULL,
        PRIMARY KEY (entity, gene_id)
    )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_graph_entity "
        "ON entity_graph(entity)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_graph_gene "
        "ON entity_graph(gene_id)"
    )


# ---------------------------------------------------------------------------
# symbol_defs (WS2 — symbol graph)
# ---------------------------------------------------------------------------

def _create_symbol_defs(cur: sqlite3.Cursor) -> None:
    """Symbol-definition index: which gene (code chunk) defines a symbol.

    Mirrors entity_graph's shape. Resolves "who defines `foo`" at ingest (to
    emit SYMBOL_REF edges) and at query time (symbol-aware expansion). ``kind``
    distinguishes definition flavours (function/class/method) for future use.
    """
    cur.execute("""
    CREATE TABLE IF NOT EXISTS symbol_defs (
        symbol   TEXT NOT NULL,
        gene_id  TEXT NOT NULL,
        kind     TEXT,
        PRIMARY KEY (symbol, gene_id)
    )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_symbol_defs_symbol "
        "ON symbol_defs(symbol)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_symbol_defs_gene "
        "ON symbol_defs(gene_id)"
    )


# ---------------------------------------------------------------------------
# path_key_index
# ---------------------------------------------------------------------------

def _create_path_key_index(cur: sqlite3.Cursor) -> None:
    # Issue #165: WITHOUT ROWID — a rowid table with a 3-col PK stores
    # every row twice (table + sqlite_autoindex); the WITHOUT ROWID form
    # stores it once in PK order and serves the same covering Tier-0
    # lookup plan. Existing rowid-table DBs keep working unchanged
    # (CREATE IF NOT EXISTS no-ops); convert them with
    # ``storage.indexes.compact_path_key_index`` (/admin/compact-pki).
    #
    # idx_pki_lookup is gone: EXPLAIN QUERY PLAN proved the live lookup
    # never chose it (strict prefix of the PK). idx_pki_gene stays — it
    # serves the upsert DELETE path.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS path_key_index (
        path_token TEXT NOT NULL,
        kv_key     TEXT NOT NULL,
        gene_id    TEXT NOT NULL,
        PRIMARY KEY (path_token, kv_key, gene_id)
    ) WITHOUT ROWID
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_pki_gene "
        "ON path_key_index(gene_id)"
    )


# ---------------------------------------------------------------------------
# filename_index
# ---------------------------------------------------------------------------

def _create_filename_index(cur: sqlite3.Cursor) -> None:
    try:
        from .. import filename_anchor as _fa
        _fa.ensure_schema(cur.connection)
    except Exception:
        log.warning(
            "filename_index schema init failed -- filename-anchor tier disabled",
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# okf_links (OKF Phase 1 — inert by design)
# ---------------------------------------------------------------------------

def _create_okf_links(cur: sqlite3.Cursor) -> None:
    # OKF bundle cross-link capture (docs/research/2026-07-08-okf-council.md,
    # Amendment 1). This table is INERT: it has zero readers in any retrieval
    # tier, on purpose. harmonic_links carries a flat Tier-5 per-edge boost
    # and gene_relations feeds tie-breaking, so writing OKF links to either
    # would ship an ungated scoring change. Graduation into live edges only
    # happens via the Phase-2 reviewed design ('asserted' provenance class).
    # resolved_target_gene_id is NULL for dangling links (spec §5.3: broken
    # links are not malformed — they may be not-yet-written knowledge).
    cur.execute("""
    CREATE TABLE IF NOT EXISTS okf_links (
        bundle_id               TEXT NOT NULL,
        source_concept_id       TEXT NOT NULL,
        target_concept_id       TEXT NOT NULL,
        resolved_source_gene_id TEXT NOT NULL,
        resolved_target_gene_id TEXT,
        link_text               TEXT
    )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_okf_links_bundle "
        "ON okf_links(bundle_id)"
    )


# ---------------------------------------------------------------------------
# Auto-repair corrupt data on startup
# ---------------------------------------------------------------------------

def _auto_repair(cur: sqlite3.Cursor, conn: sqlite3.Connection) -> None:
    repaired = 0
    bad = cur.execute(
        "SELECT COUNT(*) FROM genes WHERE typeof(chromatin) != 'integer' "
        "OR chromatin IS NULL OR chromatin NOT IN (0, 1, 2)"
    ).fetchone()[0]
    if bad:
        cur.execute(
            "UPDATE genes SET chromatin = 0 "
            "WHERE typeof(chromatin) != 'integer' "
            "OR chromatin IS NULL OR chromatin NOT IN (0, 1, 2)"
        )
        repaired += bad
        log.warning("Auto-repaired %d genes with corrupt chromatin", bad)

    null_epi = cur.execute(
        "SELECT COUNT(*) FROM genes WHERE epigenetics IS NULL"
    ).fetchone()[0]
    if null_epi:
        default_epi = '{"created_at":0,"last_accessed":0,"access_count":0,"co_activated_with":[],"typed_co_activated":[],"decay_score":1.0}'
        cur.execute(
            "UPDATE genes SET epigenetics = ? WHERE epigenetics IS NULL",
            (default_epi,),
        )
        repaired += null_epi
        log.warning("Auto-repaired %d genes with NULL epigenetics", null_epi)

    if repaired:
        conn.commit()


# ---------------------------------------------------------------------------
# FTS5
# ---------------------------------------------------------------------------

def _create_fts5(cur: sqlite3.Cursor, conn: sqlite3.Connection) -> bool:
    """Create and incrementally sync the FTS5 index.  Returns fts_available."""
    try:
        cur.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS genes_fts USING fts5(
            gene_id,
            content,
            complement
        )
        """)

        # fts5vocab "instance" shadow over genes_fts — one row per
        # (term, doc, col, offset) occurrence. Used by
        # KnowledgeStore.rescore_lexical_global_idf (#182) to read per-doc
        # term frequency + doc length for the cross-shard global-IDF
        # BM25 re-score. Zero storage (a view over the FTS index); created
        # here so the read-time rescore never has to write. Idempotent.
        try:
            cur.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS genes_fts_vocab "
                "USING fts5vocab(genes_fts, instance)"
            )
        except Exception:
            # fts5vocab unavailable on this SQLite build — the rescore
            # soft-fails to the scalar path, so this is non-fatal.
            log.warning(
                "fts5vocab unavailable — global-IDF rescore will fall back",
                exc_info=True,
            )

        # Incremental FTS5 sync -- only add missing documents, don't rebuild
        gene_count = cur.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
        fts_count = cur.execute("SELECT COUNT(*) FROM genes_fts").fetchone()[0]
        delta = gene_count - fts_count
        if delta > 0:
            cur.execute(
                "INSERT INTO genes_fts(gene_id, content, complement) "
                "SELECT g.gene_id, "
                "  COALESCE(g.source_id,'') || ' ' || "
                "  COALESCE((SELECT GROUP_CONCAT(pi.tag_value, ' ') "
                "    FROM promoter_index pi WHERE pi.gene_id = g.gene_id), '') "
                "  || ' ' || g.content, "
                "  COALESCE(g.complement, '') "
                "FROM genes g "
                "WHERE g.gene_id NOT IN (SELECT gene_id FROM genes_fts)"
            )
            conn.commit()
            log.info("FTS5 incremental sync: +%d genes (total: %d)", delta, gene_count)
        elif delta < 0:
            # Orphan FTS5 entries are HARMLESS at query time: downstream
            # ``gene_id`` joins return NULL for missing rows and the orphan
            # is filtered out before delivery. The previous cleanup used
            # ``WHERE gene_id NOT IN (SELECT gene_id FROM genes)`` which is
            # an O(N*M) correlated subquery — on a 850K-gene sharded fixture
            # (105 shards averaged ~8K genes each, ~40 orphans per shard) it
            # pegged a single core for 5-10 minutes PER SHARD on cold-cache,
            # blocking the daemon's first /fingerprint response for hours.
            # Skip cleanup entirely when orphans are <5% of gene_count
            # (statistical noise); use an indexed NOT EXISTS for the rare
            # significant-drift case.
            orphan_ratio = -delta / max(gene_count, 1)
            if orphan_ratio < 0.05:
                log.info(
                    "FTS5 has %d orphan entries (%.2f%% of %d genes); "
                    "leaving as-is (harmless at query time)",
                    -delta, orphan_ratio * 100, gene_count,
                )
            else:
                cur.execute(
                    "DELETE FROM genes_fts "
                    "WHERE NOT EXISTS ("
                    "  SELECT 1 FROM genes "
                    "  WHERE genes.gene_id = genes_fts.gene_id"
                    ")"
                )
                conn.commit()
                log.info("FTS5 cleanup: removed %d orphan entries", -delta)
        return True
    except Exception:
        log.warning(
            "FTS5 not available -- content search disabled",
            exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# Session registry schema (federation layers)
# ---------------------------------------------------------------------------

def ensure_registry_schema(
    cur: sqlite3.Cursor,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Create session registry tables + indexes. Idempotent.

    Implements the 4-layer federated identity model (see
    docs/FEDERATION_LOCAL.md):
        - orgs:              top-level tenant (org/team)
        - parties:           devices (PCs) belonging to an org
        - participants:      humans (users) on a device
        - agents:            AI personas working on a user's behalf
        - gene_attribution:  4-axis attribution row per document

    Each layer is independently queryable so we can answer
    "what did Laude on gandalf, on max's behalf, in SwiftWing21, do?"
    with a single composite filter.

    Schema is additive: pre-2026-04-12 databases auto-upgrade via
    IF NOT EXISTS table creates and ALTER ADD COLUMN for new fields
    on existing tables.
    """
    # -- Layer 1: orgs (top-level tenant) --------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orgs (
        org_id         TEXT PRIMARY KEY,
        display_name   TEXT NOT NULL,
        trust_domain   TEXT NOT NULL DEFAULT 'local',
        created_at     REAL NOT NULL,
        metadata       TEXT
    )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_orgs_trust_domain "
        "ON orgs(trust_domain)"
    )
    cur.execute(
        "INSERT OR IGNORE INTO orgs "
        "(org_id, display_name, trust_domain, created_at) "
        "VALUES ('local', 'Local Org (default)', 'local', ?)",
        (time.time(),),
    )

    cur.execute("""
    CREATE TABLE IF NOT EXISTS parties (
        party_id      TEXT PRIMARY KEY,
        display_name  TEXT NOT NULL,
        trust_domain  TEXT NOT NULL DEFAULT 'local',
        created_at    REAL NOT NULL,
        metadata      TEXT
    )
    """)
    for col in ("org_id", "timezone"):
        try:
            if col == "org_id":
                cur.execute(
                    "ALTER TABLE parties ADD COLUMN org_id TEXT "
                    "REFERENCES orgs(org_id)"
                )
            else:
                cur.execute(f"ALTER TABLE parties ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_parties_trust_domain "
        "ON parties(trust_domain)"
    )

    cur.execute("""
    CREATE TABLE IF NOT EXISTS participants (
        participant_id   TEXT PRIMARY KEY,
        party_id         TEXT NOT NULL REFERENCES parties(party_id),
        handle           TEXT NOT NULL,
        workspace        TEXT,
        pid              INTEGER,
        started_at       REAL NOT NULL,
        last_heartbeat   REAL NOT NULL,
        status           TEXT NOT NULL DEFAULT 'active',
        capabilities     TEXT,
        metadata         TEXT
    )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_participants_party "
        "ON participants(party_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_participants_heartbeat "
        "ON participants(last_heartbeat)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_participants_handle "
        "ON participants(handle)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_participants_status "
        "ON participants(status)"
    )

    # Vendor + host axes for dashboard badges (added 2026-05-05).
    for col in ("agent_kind", "mcp_host"):
        try:
            cur.execute(f"ALTER TABLE participants ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass

    # Announce columns (added 2026-05-06).
    for col in ("ide_detected", "ide_detection_via", "model_id"):
        try:
            cur.execute(f"ALTER TABLE participants ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass

    # -- Layer 4: agents (AI personas under a participant) ---------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS agents (
        agent_id        TEXT PRIMARY KEY,
        participant_id  TEXT NOT NULL
                        REFERENCES participants(participant_id) ON DELETE CASCADE,
        handle          TEXT NOT NULL,
        kind            TEXT,
        created_at      REAL NOT NULL,
        last_seen_at    REAL,
        metadata        TEXT,
        UNIQUE (participant_id, handle)
    )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_agents_participant "
        "ON agents(participant_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_agents_handle "
        "ON agents(handle)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_agents_kind "
        "ON agents(kind)"
    )

    cur.execute("""
    CREATE TABLE IF NOT EXISTS gene_attribution (
        gene_id         TEXT PRIMARY KEY
                        REFERENCES genes(gene_id) ON DELETE CASCADE,
        party_id        TEXT NOT NULL
                        REFERENCES parties(party_id),
        participant_id  TEXT
                        REFERENCES participants(participant_id) ON DELETE SET NULL,
        authored_at     REAL NOT NULL
    )
    """)
    for alter_sql in (
        "ALTER TABLE gene_attribution ADD COLUMN org_id TEXT "
        "REFERENCES orgs(org_id)",
        "ALTER TABLE gene_attribution ADD COLUMN agent_id TEXT "
        "REFERENCES agents(agent_id) ON DELETE SET NULL",
        "ALTER TABLE gene_attribution ADD COLUMN authored_tz TEXT",
    ):
        try:
            cur.execute(alter_sql)
        except sqlite3.OperationalError:
            pass

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_attribution_party_time "
        "ON gene_attribution(party_id, authored_at DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_attribution_participant_time "
        "ON gene_attribution(participant_id, authored_at DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_attribution_org_time "
        "ON gene_attribution(org_id, authored_at DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_attribution_agent_time "
        "ON gene_attribution(agent_id, authored_at DESC)"
    )

    # hitl_events
    cur.execute("""
    CREATE TABLE IF NOT EXISTS hitl_events (
        event_id                   TEXT PRIMARY KEY,
        party_id                   TEXT NOT NULL
                                   REFERENCES parties(party_id),
        participant_id             TEXT
                                   REFERENCES participants(participant_id) ON DELETE SET NULL,
        ts                         REAL NOT NULL,

        pause_type                 TEXT NOT NULL,
        task_context               TEXT,
        resolved_without_operator  INTEGER NOT NULL DEFAULT 0,

        operator_tone_uncertainty  REAL,
        operator_risk_keywords     TEXT,
        time_since_last_risk_event REAL,
        recoverability_signal      TEXT,

        genome_total_genes         INTEGER,
        genome_hetero_count        INTEGER,
        cold_cache_size            INTEGER,

        metadata                   TEXT
    )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_hitl_party_time "
        "ON hitl_events(party_id, ts DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_hitl_participant_time "
        "ON hitl_events(participant_id, ts DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_hitl_pause_type "
        "ON hitl_events(pause_type)"
    )

    # CWoLa label log
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cwola_log (
        retrieval_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ts                 REAL    NOT NULL,
        session_id         TEXT,
        party_id           TEXT,
        query              TEXT,
        tier_features      TEXT,
        top_gene_id        TEXT,
        bucket             TEXT,
        bucket_assigned_at REAL,
        requery_delta_s    REAL,
        query_sema         TEXT,
        top_candidate_sema TEXT
    )
    """)
    for _alter in (
        "ALTER TABLE cwola_log ADD COLUMN query_sema TEXT",
        "ALTER TABLE cwola_log ADD COLUMN top_candidate_sema TEXT",
    ):
        try:
            cur.execute(_alter)
        except sqlite3.OperationalError:
            pass
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_cwola_session_time "
        "ON cwola_log(session_id, ts)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_cwola_bucket "
        "ON cwola_log(bucket)"
    )

    # session_delivery_log
    cur.execute("""
    CREATE TABLE IF NOT EXISTS session_delivery_log (
        delivery_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id      TEXT NOT NULL,
        gene_id         TEXT NOT NULL,
        retrieval_id    INTEGER,
        delivered_at    REAL NOT NULL,
        content_hash    TEXT,
        mode            TEXT
    )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sdl_session_gene "
        "ON session_delivery_log(session_id, gene_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sdl_session_time "
        "ON session_delivery_log(session_id, delivered_at)"
    )

    # harmonic_links (Sprint 4)
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
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_harmonic_source "
        "ON harmonic_links(source)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_harmonic_b "
        "ON harmonic_links(gene_id_b)"
    )

    # Stage 4: persisted threshold calibration
    cur.execute("""
    CREATE TABLE IF NOT EXISTS genome_calibration (
        key          TEXT PRIMARY KEY,
        value_json   TEXT NOT NULL,
        computed_at  REAL NOT NULL
    )
    """)
