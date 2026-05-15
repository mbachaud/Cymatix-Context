"""
KnowledgeStore — SQLite cold storage for the document pool.

Bio analogue (legacy term: genome):
    The genome is the full DNA library. Only ~1% is expressed per cell cycle.
    Our knowledge store stores all context documents in SQLite with a tags index
    for fast retrieval. Chromatin state controls accessibility.

Includes:
    - DDL (documents table + promoter_index join table)
    - Content-addressed document IDs (SHA256[:16])
    - Fix 1: synonym expansion for tags queries
    - Fix 1: co-activation pull-forward (associative memory)
    - Compaction (decay stale documents → COLD lifecycle tier)
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .accel import (
    json_loads,
    json_dumps,
    parse_promoter,
    parse_epigenetics,
    clear_parse_caches,
    batch_update_epigenetics,
)
from .exceptions import PromoterMismatch
from .schemas import ChromatinState, EpigeneticMarkers, Gene, PromoterTags

log = logging.getLogger(__name__)


# ── Struggle 1 fix: source-path deny list ───────────────────────────────
#
# Paths that are structurally noise regardless of content. Any document whose
# source_id matches one of these patterns goes directly to HETEROCHROMATIN
# without computing a density score — it's cheaper and more reliable than
# relying on the scorer for content types we already know are noise.
#
# Categories covered:
#   - Build artifacts (.next, node_modules, __pycache__, dist, build, target)
#   - Lockfiles and minified bundles
#   - Web manifest files (app-paths-manifest.json, reference-manifest.js)
#   - Non-English locale directories (software i18n is high-volume low-signal)
#
# NOT in this list (deliberate):
#   - *.csv — business CSVs (customer data, financial records, invoice exports)
#     are legitimate ingest targets. Generic low-density CSVs will be caught
#     by the score gate instead.
#   - *.json — JSON is everywhere, most of it is config/data with signal
#   - *.md — markdown is primary signal content
#   - Cargo.toml / pyproject.toml — project metadata is signal
#   - Steam / game content (SteamLibrary, steamapps, BeamNG, Hades,
#     Factorio, Dyson Sphere, etc.) — reframed as high-SNR signal on
#     2026-04-10. Game files are content-dense with unambiguous literal
#     values (configs, enums, item IDs, code) and empirically produced
#     86% of correct answers on the N=50 v2 NIAH benchmark before the
#     original gate. Individual low-density game documents still get caught
#     by the score gate; the structural path is no longer a categorical
#     reject. See docs/BENCHMARKS.md and ~/.helix/shared/handoffs/ for
#     the full empirical basis.
#
# Patterns are anchored to directory boundaries to avoid false positives
# on legitimate files that happen to contain the substring.
_DENY_PATTERNS = [
    # Build artifacts
    r"[\\/]\.next[\\/]",
    r"[\\/]node_modules[\\/]",
    r"[\\/]__pycache__[\\/]",
    r"[\\/]dist[\\/]",
    r"[\\/]build[\\/](?!\.(bat|ps1|sh)$)",  # keep build.bat/build.sh etc.
    r"[\\/]target[\\/]debug[\\/]",
    r"[\\/]target[\\/]release[\\/]",
    # Lockfiles / manifests
    r"[\\/]package-lock\.json$",
    r"[\\/]yarn\.lock$",
    r"[\\/]Cargo\.lock$",
    r"[\\/]uv\.lock$",
    r"[\\/]poetry\.lock$",
    r"[\\/]Pipfile\.lock$",
    r"[\\/]composer\.lock$",
    # Minified bundles / source maps
    r"\.min\.(js|css|mjs)$",
    r"\.map$",
    # Next.js / web-framework manifests
    r"app-paths-manifest\.json$",
    r"app-build-manifest\.json$",
    r"_buildManifest\.js$",
    r"_ssgManifest\.js$",
    r"client-reference-manifest\.(js|json)$",
    r"server-reference-manifest\.(js|json)$",
    # Binary / compiled artifacts
    r"\.(pyc|pyo|so|dll|dylib|exe|wasm|bin|pack|idx)$",
    # Non-English software locale directories (English is kept as the
    # primary user base; other locales are high-volume low-signal for
    # typical retrieval workloads). Game subtitles are NOT in this list —
    # they're reframed as signal along with the rest of the game content.
    r"[\\/]locale[\\/](?!en[\\/])[a-z]{2,3}[\\/]",
]

_DENY_RE = re.compile("|".join(_DENY_PATTERNS), re.IGNORECASE)


def is_denied_source(source_id: Optional[str]) -> bool:
    """Return True iff source_id matches the structural noise deny list.

    Exposed as a module-level function so tests and scripts can reuse it
    without constructing a full KnowledgeStore instance.
    """
    if not source_id:
        return False
    return bool(_DENY_RE.search(source_id))


# ── Path tokenization for the path_key_index retrieval layer ────────────
# Splits source_id on common path separators + common filename punctuation.
# Each token becomes a retrieval signal paired with the document's key_values
# keys. A query like "what is the value of helix_port?" hits the index on
# path_token='helix' AND kv_key='port' → direct boost to the document.
#
# No LLM, no manual project list, no re-ingest required — purely derived
# from source_id + CpuTagger-extracted key_values. When a new project
# ingests at /SomeDir/NewProject/..., the token "newproject" becomes a
# retrieval signal automatically.
_PATH_SPLIT_RE = re.compile(r"[\\/\-_.\s:]+")

# Tokens that appear on nearly every path and carry no discriminating
# signal. Keeping this list tiny on purpose — it's the only maintenance
# burden, and overflowing it would be throwing signal away. Subset
# chosen from the actual source_id distribution on the 2026-04-12 knowledge store.
_PATH_NOISE_TOKENS = frozenset({
    "",
    # Drive / filesystem roots
    "f", "c", "d", "e", "g", "h",
    # Ubiquitous container dirs
    "projects", "project", "src", "lib", "app", "apps", "test", "tests",
    "docs", "doc", "main", "master", "common", "shared", "core", "util",
    "utils", "include", "includes", "public", "private", "tmp", "temp",
    "cache", "dist", "build", "bin", "obj", "x64", "x86", "arm64", "debug",
    "release", "data", "file", "files", "assets", "resources", "scripts",
    "config", "configs", "node_modules", "pycache", "__pycache__",
    # Filename-level noise
    "init", "index", "new", "old", "copy", "readme", "license",
    # Extensions that appear as tokens after splitting
    "py", "js", "ts", "tsx", "jsx", "md", "txt", "json", "toml", "yaml",
    "yml", "ini", "cfg", "xml", "html", "css", "scss", "rs", "go",
    "java", "c", "cpp", "h", "hpp", "sh", "bat", "ps1", "log",
})


def _kv_keys_from_list(kv_list) -> list:
    """Extract lowercased key names from a key_values List[str] of 'key=value'.

    Gene.key_values is stored as ["port=8080", "model=llava", ...] — each
    entry is a 'key=value' string. This helper pulls out just the key side,
    lowercased, deduped, skipping empties.

    Works transparently on dict inputs too for forward-compat — if a future
    document schema changes key_values to Dict[str, str], this still returns
    the right keys.
    """
    if not kv_list:
        return []
    # dict path (future-proof)
    if isinstance(kv_list, dict):
        return [k.lower() for k in kv_list.keys() if k]
    # list-of-strings path (current schema)
    out = []
    seen = set()
    for entry in kv_list:
        if not isinstance(entry, str):
            continue
        # Split on the FIRST '=' only — values can contain '='
        if "=" not in entry:
            continue
        key = entry.split("=", 1)[0].strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def path_tokens(source_id: Optional[str]) -> set:
    """Extract retrieval-signal tokens from a file path.

    Splits on path separators and common filename punctuation, lowercases
    everything, drops single-char tokens and generic noise (see
    _PATH_NOISE_TOKENS). The result is the set of tokens that meaningfully
    identify this document's provenance for compound-lookup retrieval.

    Examples:
        "F:/Projects/helix-context/helix_context/config.py"
          → {"helix-context", "helix_context", "helix", "context"}

        "F:/SteamLibrary/steamapps/common/Hades II/content/maps.lua"
          → {"steamlibrary", "steamapps", "hades", "ii", "content", "maps"}

        "F:/Projects/CosmicTasha/src/components/Hero.tsx"
          → {"cosmictasha", "components", "hero"}

    Exposed as a module-level function so tests, backfill scripts, and
    retrieval code can reuse it without constructing a KnowledgeStore.
    """
    if not source_id:
        return set()
    out = set()
    # First pass: raw split on separators + punctuation
    for tok in _PATH_SPLIT_RE.split(source_id):
        t = tok.lower()
        if len(t) <= 1 or t in _PATH_NOISE_TOKENS:
            continue
        out.add(t)
        # Sub-split on interior hyphens/underscores to surface "helix"
        # from "helix-context" and "helix_context". Keeps the compound
        # form too so direct matches still work.
        if "-" in t or "_" in t:
            for sub in re.split(r"[-_]+", t):
                s = sub.lower()
                if len(s) > 1 and s not in _PATH_NOISE_TOKENS:
                    out.add(s)
    return out


def file_tokens(source_id: Optional[str]) -> set:
    """Extract tokens from just the filename (basename), excluding folders.

    Companion to path_tokens() — where that returns folder + file tokens
    mixed together, this returns only the basename's tokens. Motivated by
    the "same-folder-wrong-file" failure mode on the 10-needle bench:
    a query for "helix pipeline" matches path_tokens on any document under
    helix-context/, but only the documents whose filename itself mentions
    "pipeline" deserve a coordinate-confidence boost.

    Uses the same split + noise rules as path_tokens() but restricts
    input to the basename after the last separator.

    Examples:
        "F:/Projects/helix-context/docs/architecture/PIPELINE_LANES.md"
          → {"pipeline", "lanes"}

        "F:/Projects/helix-context/helix_context/retrieval.py"
          → {"retrieval"}

        "F:/Projects/helix-context/helix_context/genome.py"
          → {"knowledge store"}
    """
    if not source_id:
        return set()
    # Find the last separator (either / or \) to isolate the basename.
    # PurePath would also work but adds an import; cheap local split is fine.
    for sep in ("/", "\\"):
        if sep in source_id:
            basename = source_id.rsplit(sep, 1)[-1]
            break
    else:
        basename = source_id
    if not basename:
        return set()
    out = set()
    for tok in _PATH_SPLIT_RE.split(basename):
        t = tok.lower()
        if len(t) <= 1 or t in _PATH_NOISE_TOKENS:
            continue
        out.add(t)
        if "-" in t or "_" in t:
            for sub in re.split(r"[-_]+", t):
                s = sub.lower()
                if len(s) > 1 and s not in _PATH_NOISE_TOKENS:
                    out.add(s)
    return out


# Thresholds for the score-based gate. Calibrated against the 2026-04-10
# noise-diluted knowledge store (8,063 documents, ~42% structural noise). See
# scripts/simulate_density_gate_v2.py for the empirical basis.
_DENSITY_HETEROCHROMATIN_THRESHOLD = 0.50
_DENSITY_EUCHROMATIN_THRESHOLD = 1.00
_DENSITY_CONTENT_LENGTH_FLOOR = 100  # chars — prevents tiny-content score explosion
_DENSITY_ACCESS_OVERRIDE = 5         # access_count >= this keeps document OPEN regardless

# Working-set inference (Phase 1 slice 2 of the 8D dimensional roadmap).
# A document with at least _DENSITY_RATE_MIN_HITS accesses in the last
# _DENSITY_RATE_WINDOW seconds is considered "actively used right now"
# and gets the OPEN override regardless of static density score. The
# rate signal is sharper than the monotonic _DENSITY_ACCESS_OVERRIDE
# because it distinguishes "hot last hour" from "hot once a year ago" —
# the monotonic counter conflates them. Documents with empty recent_accesses
# buffers (legacy documents that pre-date Phase 1, or freshly ingested
# documents that haven't been touched yet) fall through to the monotonic
# fallback path, preserving backward compatibility.
#
# Reference: ~/.helix/shared/handoffs/2026-04-11_8d_dimensional_roadmap.md
_DENSITY_RATE_WINDOW = 3600.0   # 1-hour window
_DENSITY_RATE_MIN_HITS = 3      # ≥3 accesses in the window → override

# TTL for the memoized corpus-size count used by the IDF-weighted lexical
# anchor tier. Re-queried at most once per window to avoid a COUNT(*) on
# every retrieval call.
_CORPUS_SIZE_TTL = 60.0


class KnowledgeStore:
    """SQLite-backed document storage with tags-tag retrieval."""

    def __init__(
        self,
        path: str,
        synonym_map: Optional[Dict[str, List[str]]] = None,
        sema_codec=None,
        splade_enabled: bool = False,
        entity_graph: bool = False,
        sr_enabled: bool = False,
        sr_gamma: float = 0.85,
        sr_k_steps: int = 4,
        sr_weight: float = 1.5,
        sr_cap: float = 3.0,
        seeded_edges_enabled: bool = False,
        filename_anchor_enabled: bool = False,
        filename_anchor_weight: float = 4.0,
        bm25_shortlist_enabled: bool = False,
        bm25_shortlist_size: int = 50,
        bm25_prefilter_enabled: bool = False,
        bm25_prefilter_size: int = 200,
        entity_graph_retrieval_enabled: bool = False,
        dense_embedding_enabled: bool = False,
        # Stage 2 (2026-05-08): default dim raised from 256 -> 1024 (full
        # BGE-M3 Matryoshka). dim=256 collapsed random-pair cosine.
        dense_embedding_dim: int = 1024,
        ann_similarity_threshold: float = 0.35,
        ann_threshold_min_genes: int = 1,
        ann_threshold_max_genes: int = 12,
        # Stage 4 (2026-05-08): margin-over-random ANN calibration. When
        # ``ann_threshold_mode == "margin_over_random"``, query_genes_ann
        # reads the persisted threshold from ``genome_calibration``, falling
        # back to ``ann_similarity_threshold`` if the row is missing (with
        # one-time WARN). ``"absolute"`` keeps the legacy hand-picked value.
        # See docs/specs/2026-05-08-stage-4-threshold-calibration.md §3.
        ann_threshold_mode: str = "absolute",
        ann_threshold_sigma_multiplier: float = 3.0,
        # Stage 2: dense recall pool size, decoupled from ann_threshold_max_genes.
        dense_pool_size: int = 500,
        # Stage 3 (2026-05-08): Reciprocal Rank Fusion (spec
        # docs/specs/2026-05-08-stage-3-rrf-fusion.md). Default
        # "additive" preserves pre-Stage-3 ranking byte-for-byte; flip
        # to "rrf" to enable rank fusion. Per-tier weights are RRF
        # post-multipliers; defaults map to the existing implicit
        # additive weights.
        fusion_mode: str = "additive",
        rrf_k: int = 60,
        fts5_weight: float = 3.0,
        splade_weight: float = 3.5,
        tag_exact_weight: float = 3.0,
        tag_prefix_weight: float = 1.5,
        sema_cold_weight: float = 3.0,
        lex_anchor_weight: float = 1.5,
        harmonic_weight: float = 1.0,
        entity_graph_weight: float = 0.5,
        dense_weight: float = 1.0,
        pki_weight: float = 1.0,
        main_conn: Optional[sqlite3.Connection] = None,
        shard_name: str = "main",
        read_only: bool = False,
    ):
        self.path = path
        self.read_only = read_only
        self.synonym_map = synonym_map or {}
        self._sema_codec = sema_codec  # Optional SemaCodec for Tier 4 retrieval
        self._replication_mgr = None  # Set by set_replication_manager()
        self._splade_enabled = splade_enabled
        self._entity_graph_enabled = entity_graph
        # Tier 5b: entity graph retrieval boost (Step 3C, 2026-05-08).
        # Separate from _entity_graph_enabled (write-side) — this controls
        # whether entity_graph rows are consulted during query_genes().
        self._entity_graph_retrieval_enabled: bool = bool(entity_graph_retrieval_enabled)
        # Tier 5.5 Successor Representation (Sprint 2, Stachenfeld 2017).
        self._sr_enabled = sr_enabled
        self._sr_gamma = sr_gamma
        self._sr_k_steps = sr_k_steps
        self._sr_weight = sr_weight
        self._sr_cap = sr_cap
        # Sprint 4 — Hebbian seeded-edge evidence accumulation.
        self._seeded_edges_enabled = seeded_edges_enabled
        # Tier 0.5 filename-anchor (2026-04-15 Dewey-pivot spike).
        self._filename_anchor_enabled = filename_anchor_enabled
        self._filename_anchor_weight = filename_anchor_weight
        self._bm25_shortlist_enabled = bool(bm25_shortlist_enabled)
        self._bm25_shortlist_size = int(bm25_shortlist_size) if bm25_shortlist_size else 50
        self._bm25_prefilter_enabled = bool(bm25_prefilter_enabled)
        self._bm25_prefilter_size = int(bm25_prefilter_size)
        # Step 4 — BGE-M3 dense vectors + ANN threshold (2026-05-08).
        self._dense_embedding_enabled: bool = bool(dense_embedding_enabled)
        self._dense_embedding_dim: int = int(dense_embedding_dim)
        self._ann_threshold: float = float(ann_similarity_threshold)
        self._ann_min_genes: int = int(ann_threshold_min_genes)
        self._ann_max_genes: int = int(ann_threshold_max_genes)
        # Stage 4: persisted calibration mode + cache.
        # ``_ann_threshold_calibrated`` is the lazily-loaded value from
        # ``genome_calibration``; ``None`` means "not loaded yet" and the
        # next ``_get_effective_ann_threshold`` call will populate it.
        self._ann_threshold_mode: str = str(ann_threshold_mode)
        self._ann_threshold_sigma_multiplier: float = float(ann_threshold_sigma_multiplier)
        self._ann_threshold_calibrated: Optional[float] = None
        self._ann_threshold_calibration_meta: Optional[Dict[str, Any]] = None
        self._ann_threshold_fallback_warned: bool = False
        # Stage 2 (2026-05-08): pool size for dense + lex recall, decoupled
        # from max_genes (the post-ranking final cut).
        self._dense_pool_size: int = int(dense_pool_size)
        self._dense_codec: "BGEM3Codec | None" = None  # lazy-loaded
        # Stage 2: in-memory hot-tier dense matrix cache. Populated lazily by
        # _ensure_dense_matrix() on first dense recall query; invalidated by
        # _invalidate_dense_matrix() after upsert/delete batches.
        self._dense_matrix: "np.ndarray | None" = None
        self._dense_matrix_ids: "list[str] | None" = None
        self._dense_matrix_lock = threading.Lock()
        # One-time-only flags so we don't spam logs.
        self._dense_v2_partial_warned: bool = False
        # ── Stage 3 (2026-05-08): RRF fusion mode + per-tier weights ──
        # Spec: docs/specs/2026-05-08-stage-3-rrf-fusion.md.
        # When "additive", query_genes() uses the legacy gene_scores +=
        # tier_score accumulator unchanged. When "rrf", each tier ALSO
        # ranks its output through the Fuser and the final sort uses
        # fused scores. Per-tier weights below are RRF post-multipliers.
        # Validate now so a typo in helix.toml fails fast at construction
        # instead of producing surprising rankings at query time.
        if fusion_mode not in ("additive", "rrf"):
            raise ValueError(
                f"fusion_mode must be 'additive' or 'rrf', got {fusion_mode!r}"
            )
        self._fusion_mode: str = fusion_mode
        self._rrf_k: int = int(rrf_k)
        self._fts5_weight: float = float(fts5_weight)
        self._splade_weight: float = float(splade_weight)
        self._tag_exact_weight: float = float(tag_exact_weight)
        self._tag_prefix_weight: float = float(tag_prefix_weight)
        self._sema_cold_weight: float = float(sema_cold_weight)
        self._lex_anchor_weight: float = float(lex_anchor_weight)
        self._harmonic_weight: float = float(harmonic_weight)
        self._entity_graph_weight: float = float(entity_graph_weight)
        self._dense_weight: float = float(dense_weight)
        self._pki_weight: float = float(pki_weight)
        self._threshold_dim_warned: bool = False
        # Phase 2 claims layer (2026-04-19). Optional hook — when a main.db
        # connection is supplied, upsert_gene emits literal claims into it
        # after each ingest. None = no auto-hook, preserving legacy behavior.
        self._main_conn = main_conn
        self._shard_name = shard_name

        # Checkpoint WAL BEFORE opening our long-lived connection
        # so we see the latest state from any external writers.
        # Speculative pre-open optimisation — the subsequent connect() will
        # still succeed if this fails, so we log at debug level but keep the
        # exc_info so diagnostic scrapes can see why it failed.
        try:
            _tmp = sqlite3.connect(self.path)
            _tmp.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _tmp.close()
        except Exception:
            log.debug("WAL pre-open checkpoint failed", exc_info=True)

        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")  # 30s retry on lock
        # Cap WAL file size — without this, SQLite resets (zero-fills) the
        # WAL on truncate but keeps the high-water-mark size on disk.
        self.conn.execute("PRAGMA journal_size_limit=67108864")  # 64 MB
        self._upsert_count = 0  # WAL checkpoint cadence counter
        self.last_query_scores: Dict[str, float] = {}  # Retrieval scores from last query
        self._last_query_scores_lock = threading.Lock()
        # Per-tier score breakdown for the last query: {gene_id: {tier_name: score}}.
        # Populated alongside last_query_scores in query_genes(). Lets the bench /
        # profiler see which retrieval signals fired (and how strongly) for each
        # candidate document — turns the lane graph into a measurable activation matrix.
        # See benchmarks/bench_skill_activation.py and docs/PIPELINE_LANES.md.
        self.last_tier_contributions: Dict[str, Dict[str, float]] = {}
        self._sema_cache: Optional[Dict] = None  # Pre-materialized ΣĒMA vectors (hot tier)
        self._cold_sema_cache: Optional[Dict] = None  # Pre-materialized ΣĒMA vectors (cold tier, C.2)
        # Memoized corpus size for IDF weighting (refreshed every
        # _CORPUS_SIZE_TTL seconds). Prevents the IDF denominator from
        # collapsing to the scored-candidate count on every query.
        self._corpus_size: int = 0
        self._corpus_size_ts: float = 0.0
        self._init_db()

        # Dedicated read-only connection — WAL allows concurrent readers
        # without blocking the writer. Separate connection = no lock contention.
        # isolation_level=None makes Python's sqlite3 module skip the implicit
        # BEGIN around SELECTs. Without it, the first read pins a WAL snapshot
        # for the lifetime of the process and prevents wal_checkpoint(TRUNCATE)
        # from advancing — which is the dominant cause of WAL bloat.
        if self.path != ":memory:":
            self._reader = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True,
                check_same_thread=False, timeout=10,
                isolation_level=None,
            )
            self._reader.row_factory = sqlite3.Row
            self._reader.execute("PRAGMA busy_timeout=10000")
        else:
            self._reader = None

        # Create SPLADE inverted index if enabled
        if self._splade_enabled:
            try:
                from .backends import splade_backend
                splade_backend.create_splade_table(self.conn)
                log.info("SPLADE inverted index ready")
            except ImportError:
                log.warning("SPLADE backend not available (transformers not installed)")
                self._splade_enabled = False
            except Exception:
                log.warning("SPLADE table creation failed", exc_info=True)
                self._splade_enabled = False

    def _init_db(self) -> None:
        from .storage.ddl import init_db
        self._fts_available = init_db(self.conn)

    def _ensure_registry_schema(self, cur: sqlite3.Cursor) -> None:
        """Delegate to storage.ddl.ensure_registry_schema."""
        from .storage.ddl import ensure_registry_schema
        ensure_registry_schema(cur, self.conn)

    # ── WAL snapshot management ──────────────────────────────────────

    def _refresh_snapshot(self) -> None:
        """Release stale WAL read transaction so next SELECT sees current state.

        In SQLite WAL mode, Python's sqlite3 module starts an implicit
        transaction on SELECT. That transaction pins a snapshot — external
        writers (ingest, thinning scripts) commit to the WAL but this
        connection won't see those changes until the implicit transaction ends.

        Calling commit() ends the implicit transaction. The next SELECT
        will start a new one with the latest WAL state.
        """
        try:
            self.conn.commit()
        except Exception:
            pass  # No active transaction — safe to ignore

    # ── ΣĒMA vector cache (pre-materialized for fast Mode B scans) ──

    def _build_sema_cache(self) -> None:
        """
        Load all ΣĒMA vectors into RAM as a numpy matrix for fast
        cosine similarity. Eliminates 7K json_loads() per Mode B query.

        Cache structure:
            gene_ids: list[str] — ordered document IDs
            matrix: np.ndarray (N, 20) — float32 ΣĒMA vectors
        """
        try:
            import numpy as np
        except ImportError:
            log.debug("numpy not available, ΣĒMA cache disabled")
            return

        cur = self.read_conn.cursor()
        # Try the tier-aware query first; fall back to legacy schema
        # when the read path is a replica that hasn't been migrated yet.
        try:
            rows = cur.execute(
                "SELECT gene_id, embedding FROM genes "
                "WHERE embedding IS NOT NULL AND chromatin < ? "
                "AND COALESCE(compression_tier, 0) < 2",
                (int(ChromatinState.HETEROCHROMATIN),),
            ).fetchall()
        except sqlite3.OperationalError as e:
            if "compression_tier" in str(e):
                log.warning(
                    "read_conn lacks compression_tier column — "
                    "falling back to legacy schema (likely a stale replica)"
                )
                rows = cur.execute(
                    "SELECT gene_id, embedding FROM genes "
                    "WHERE embedding IS NOT NULL AND chromatin < ?",
                    (int(ChromatinState.HETEROCHROMATIN),),
                ).fetchall()
            else:
                raise

        gene_ids = []
        vectors = []
        for r in rows:
            try:
                vec = json_loads(r["embedding"])
                if isinstance(vec, list) and len(vec) == 20:
                    gene_ids.append(r["gene_id"])
                    vectors.append(vec)
            except Exception:
                continue

        if vectors:
            matrix = np.array(vectors, dtype=np.float32)
            # Normalize rows for cosine similarity via dot product
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms < 1e-8] = 1.0
            matrix = matrix / norms
            self._sema_cache = {"gene_ids": gene_ids, "matrix": matrix}
            log.info("ΣĒMA cache built: %d vectors (%d KB)",
                     len(gene_ids), matrix.nbytes // 1024)
        else:
            self._sema_cache = None

    def invalidate_sema_cache(self) -> None:
        """Mark hot-tier cache stale — rebuilt on next Mode B query."""
        self._sema_cache = None

    # ── Cold-tier ΣĒMA retrieval (C.2, 2026-04-10) ─────────────────────
    #
    # The hot-tier retrieval paths all filter `WHERE lifecycle tier <
    # HETEROCHROMATIN`, so heterochromatin documents are invisible to normal
    # /context queries. C.1 made compress_to_heterochromatin non-destructive
    # so the underlying content/complement/codons are preserved. This
    # block adds the opt-in retrieval path that consults cold-tier documents
    # via ΣĒMA cosine similarity and returns them with content restored.
    #
    # Design notes:
    #   - Separate cache from the hot-tier _sema_cache so hot queries have
    #     zero overhead from cold-tier capability.
    #   - Lazy build on first use. Invalidated on any upsert.
    #   - Requires numpy for batched cosine similarity (falls back to
    #     empty result if unavailable, matching hot-tier Mode B behavior).
    #   - Requires a SemaCodec attached to the KnowledgeStore instance (for
    #     encoding the query text). If no codec, returns empty.
    #   - Callers must explicitly request cold-tier retrieval — it is
    #     never invoked implicitly from query_genes(). The wiring into
    #     context_manager is a follow-up (C.2-wire) and is gated behind
    #     a helix.toml config flag.

    def _build_cold_sema_cache(self) -> None:
        """Build the heterochromatin ΣĒMA vector cache for fast cosine scans.

        Scans all documents at ``chromatin = HETEROCHROMATIN`` that still have
        an embedding (ΣĒMA vector). Normalizes and stacks them into a
        dense numpy matrix for batched cosine similarity at query time.
        """
        try:
            import numpy as np
        except ImportError:
            log.debug("numpy not available, cold ΣĒMA cache disabled")
            return

        cur = self.read_conn.cursor()
        rows = cur.execute(
            "SELECT gene_id, embedding FROM genes "
            "WHERE embedding IS NOT NULL AND chromatin = ?",
            (int(ChromatinState.HETEROCHROMATIN),),
        ).fetchall()

        gene_ids = []
        vectors = []
        for r in rows:
            try:
                vec = json_loads(r["embedding"])
                if isinstance(vec, list) and len(vec) == 20:
                    gene_ids.append(r["gene_id"])
                    vectors.append(vec)
            except Exception:
                continue

        if vectors:
            matrix = np.array(vectors, dtype=np.float32)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms < 1e-8] = 1.0
            matrix = matrix / norms
            self._cold_sema_cache = {"gene_ids": gene_ids, "matrix": matrix}
            log.info("Cold ΣĒMA cache built: %d vectors (%d KB)",
                     len(gene_ids), matrix.nbytes // 1024)
        else:
            self._cold_sema_cache = None

    def invalidate_cold_sema_cache(self) -> None:
        """Mark cold-tier cache stale — rebuilt on next query_cold_tier call."""
        self._cold_sema_cache = None

    def mark_verified(
        self,
        gene_ids: List[str],
        ts: float,
        *,
        read_only: bool = False,
    ) -> int:
        """Stage 7: bump ``last_verified_at`` for the listed gene_ids.

        Spec: docs/specs/2026-05-08-stage-7-freshness-gate.md §4 + §5.

        Called from ``freshness.revalidate_and_mark`` after a successful
        mtime-fresh check. ``read_only`` gates the write — under
        ``read_only=True`` this is a no-op so the Stage-1 read_only
        contract holds. Cache updates in ``revalidate_source`` happen
        in-memory and are NOT a DB write, so they remain allowed.

        Returns the number of rows actually updated. Zero is a valid
        return (e.g. caller passed unknown gene_ids); the freshness
        gate uses this method as a hint, not as a correctness check.
        """
        if read_only:
            # No-op under the read_only contract. Stage 7 spec §5
            # requires this to be silent — no warning, no exception.
            return 0
        if not gene_ids:
            return 0
        try:
            cur = self.conn.cursor()
            placeholders = ",".join("?" * len(gene_ids))
            cur.execute(
                f"UPDATE genes SET last_verified_at = ? "
                f"WHERE gene_id IN ({placeholders})",
                (float(ts), *gene_ids),
            )
            updated = cur.rowcount or 0
            self.conn.commit()
            return int(updated)
        except Exception:
            log.warning(
                "mark_verified: UPDATE failed for %d gene_ids",
                len(gene_ids),
                exc_info=True,
            )
            return 0

    def query_cold_tier(
        self,
        query_text: str,
        k: int = 3,
        min_cosine: float = 0.15,
    ) -> List[Gene]:
        """Search heterochromatin-tier documents by ΣĒMA cosine similarity.

        Cold-tier retrieval is opt-in — normal ``query_genes()`` does not
        consult this path. Use when a query is known to target archived
        knowledge, or as a fallthrough when hot-tier results are empty or
        too sparse to answer confidently.

        Parameters
        ----------
        query_text : str
            Natural-language query to encode via the attached SemaCodec.
        k : int
            Maximum number of cold-tier documents to return. Defaults to 3 —
            cold retrieval should be a precision tool, not a dump.
        min_cosine : float
            Similarity floor in 20-dim ΣĒMA space. Matches below this
            are discarded. Defaults to 0.25 — ΣĒMA's 20-dim projection
            is sparse by design, so typical close-paraphrase pairs score
            around 0.15–0.30 in cosine. The existing hot-tier Mode A/B
            paths use 0.3/0.4 as meaningful thresholds (see
            ``query_genes`` Tier 4). 0.25 here is slightly more
            permissive because the cold path is only reached when hot
            results are already thin — better to surface a weak match
            than nothing.

        Returns
        -------
        list[Document]
            Up to ``k`` heterochromatin documents with full content restored,
            sorted by cosine similarity descending. Each document's
            ``chromatin`` field will still show HETEROCHROMATIN — the
            caller is responsible for deciding whether to promote the
            document back to OPEN based on the retrieval event (e.g., by
            updating access_count and letting a future sweep reconsider it).

        Returns empty list when any precondition fails:
            - No SemaCodec attached (self._sema_codec is None)
            - numpy unavailable
            - No heterochromatin documents with embeddings in the knowledge store
            - No matches clear the min_cosine threshold
        """
        if self._sema_codec is None:
            return []

        try:
            import numpy as np
        except ImportError:
            return []

        if self._cold_sema_cache is None:
            self._build_cold_sema_cache()
        if self._cold_sema_cache is None:
            return []  # Nothing in the cold tier

        try:
            query_vec = self._sema_codec.encode(query_text)
            q = np.array(query_vec, dtype=np.float32)
            q_norm = np.linalg.norm(q)
            if q_norm < 1e-8:
                return []
            q = q / q_norm

            cache = self._cold_sema_cache
            sims = cache["matrix"] @ q  # (N,20) @ (20,) → (N,)

            # Sort descending, filter by threshold, take top-k
            top_idx = np.argsort(sims)[::-1]
            selected_ids: List[str] = []
            selected_sims: Dict[str, float] = {}
            for idx in top_idx:
                sim = float(sims[idx])
                if sim < min_cosine:
                    break  # Sorted descending — once below threshold, stop
                gid = cache["gene_ids"][idx]
                selected_ids.append(gid)
                selected_sims[gid] = sim
                if len(selected_ids) >= k:
                    break

            if not selected_ids:
                return []

            # Fetch full Document objects (content is preserved thanks to C.1)
            genes: List[Gene] = []
            for gid in selected_ids:
                gene = self.get_doc(gid)
                if gene is not None:
                    genes.append(gene)

            # Expose similarity scores via last_query_scores for the
            # caller (context_manager uses this for tier-budget decisions)
            for gid, sim in selected_sims.items():
                self.last_query_scores[gid] = sim

            return genes
        except Exception:
            log.debug("cold-tier ΣĒMA retrieval failed", exc_info=True)
            return []

    # ── Stage 4: calibrated ANN threshold reader ─────────────────────

    def _get_effective_ann_threshold(self) -> float:
        """Return the active ANN cosine cutoff for ``query_genes_ann``.

        Stage 4 (spec §3): when ``ann_threshold_mode == "margin_over_random"``,
        read the persisted ``ann_threshold`` row from ``genome_calibration``
        on first call and cache it. If the row is missing (e.g. operator has
        not yet run ``scripts/calibrate_thresholds.py``), log a one-time WARN
        and fall back to the legacy absolute ``self._ann_threshold``.

        ``"absolute"`` mode returns the legacy value with no DB read —
        ``mode="global"`` callers (the default) get byte-identical pre-Stage-4
        behavior.

        Cache invalidation: ``set_replication_manager`` clears the cache so
        readers connected through a swapped replica re-read on next call.
        """
        if self._ann_threshold_mode != "margin_over_random":
            return self._ann_threshold
        if self._ann_threshold_calibrated is not None:
            return self._ann_threshold_calibrated

        try:
            row = self.read_conn.execute(
                "SELECT value_json FROM genome_calibration WHERE key = 'ann_threshold'"
            ).fetchone()
        except Exception:
            # Table may not exist on a pre-Stage-4 database — treat as missing.
            log.debug("genome_calibration read failed", exc_info=True)
            row = None

        if row is None:
            if not self._ann_threshold_fallback_warned:
                log.warning(
                    "ann_threshold_mode='margin_over_random' but no calibration row in "
                    "genome_calibration — falling back to ann_similarity_threshold=%.3f. "
                    "Run scripts/calibrate_thresholds.py to populate.",
                    self._ann_threshold,
                )
                self._ann_threshold_fallback_warned = True
            self._ann_threshold_calibrated = self._ann_threshold
            return self._ann_threshold

        try:
            meta = json_loads(row["value_json"]) if hasattr(row, "__getitem__") else json_loads(row[0])
            value = float(meta["value"])
        except Exception:
            log.warning(
                "genome_calibration ann_threshold row is malformed; falling back to "
                "ann_similarity_threshold=%.3f",
                self._ann_threshold,
                exc_info=True,
            )
            self._ann_threshold_calibrated = self._ann_threshold
            return self._ann_threshold

        self._ann_threshold_calibrated = value
        self._ann_threshold_calibration_meta = meta
        return value

    def get_calibration_provenance(self) -> Optional[Dict[str, Any]]:
        """Return the cached calibration metadata (for /health and /context).

        Triggers a lazy load via ``_get_effective_ann_threshold`` if the cache
        is empty AND mode is ``margin_over_random``. Returns ``None`` if the
        mode is ``absolute`` OR no calibration row is present.
        """
        if self._ann_threshold_mode != "margin_over_random":
            return None
        # Trigger lazy load.
        self._get_effective_ann_threshold()
        if self._ann_threshold_calibration_meta is None:
            return None
        # Defensive copy — callers may mutate.
        return dict(self._ann_threshold_calibration_meta)

    def upsert_calibration(self, key: str, value: Dict[str, Any]) -> None:
        """UPSERT a row into ``genome_calibration``. Used by the calibration
        script and tests. Idempotent — last write wins.

        SQLite's ``busy_timeout=30000`` and the journal_mode=WAL configuration
        on ``self.conn`` (set in ``__init__``) handle writer serialization;
        no in-process lock is needed.
        """
        payload = json_dumps(value)
        now = time.time()
        self.conn.execute(
            "INSERT INTO genome_calibration (key, value_json, computed_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  value_json = excluded.value_json, "
            "  computed_at = excluded.computed_at",
            (key, payload, now),
        )
        self.conn.commit()
        # Invalidate cache so the next read sees the new value.
        if key == "ann_threshold":
            self._ann_threshold_calibrated = None
            self._ann_threshold_calibration_meta = None
            self._ann_threshold_fallback_warned = False

    # ── Persistence ──────────────────────────────────────────────────

    def set_replication_manager(self, mgr) -> None:
        """Attach a ReplicationManager for distributed knowledge store clones."""
        self._replication_mgr = mgr
        # Stage 4: invalidate calibration cache so a swapped replica re-reads.
        self._ann_threshold_calibrated = None
        self._ann_threshold_calibration_meta = None
        self._ann_threshold_fallback_warned = False

    def corpus_size(self) -> int:
        """Return the memoized total document count for IDF weighting.

        Refreshed from ``SELECT COUNT(*) FROM genes`` at most once per
        ``_CORPUS_SIZE_TTL`` seconds. Used by the IDF-weighted lexical
        anchor tier so rare-term boosts reflect the *knowledge store* size, not
        the size of the scored-candidate pool.
        """
        now = time.time()
        # Check the timestamp, not the value: a legitimately empty knowledge store
        # (count = 0) would otherwise never get cached and every call
        # would re-hit SQLite. _corpus_size_ts is initialized to 0.0 in
        # __init__, so a stale/never-refreshed cache still falls through.
        if self._corpus_size_ts > 0 and (now - self._corpus_size_ts) < _CORPUS_SIZE_TTL:
            return self._corpus_size
        try:
            total = self.read_conn.execute(
                "SELECT COUNT(*) FROM genes"
            ).fetchone()[0]
            self._corpus_size = int(total) if total else 0
            self._corpus_size_ts = now
        except Exception:
            log.warning("corpus_size refresh failed", exc_info=True)
            # Fall back to whatever we have (may be 0 on first failure).
        return self._corpus_size

    @property
    def read_conn(self) -> sqlite3.Connection:
        """
        Dedicated read-only connection. In WAL mode, readers and writers
        don't block each other — but only if they use separate connections.

        Priority: persistence replica > dedicated reader > write connection.
        """
        if self._replication_mgr is not None:
            try:
                return self._replication_mgr.get_reader()
            except Exception:
                pass
        if self._reader is not None:
            return self._reader
        return self.conn

    # ── Document ID (content-addressable) ───────────────────────────────

    @staticmethod
    def make_gene_id(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    # ── Upsert ──────────────────────────────────────────────────────

    def upsert_doc(
        self,
        gene: Gene,
        apply_gate: bool = True,
        splade_sparse: Optional[Dict[str, float]] = None,
    ) -> str:
        """
        Insert or replace a document in the knowledge store.

        If ``apply_gate`` is True (the default), the density gate runs
        before storage and may override the document's lifecycle tier. Callers
        that have a reason to bypass the gate — cross-store import imports, benchmark
        setup scripts, explicit backfill tools, manual `compact_genome`
        re-runs — can pass ``apply_gate=False`` to preserve the incoming
        lifecycle tier as-is.

        Returns the gene_id (content-addressed if not pre-populated).
        """
        gene_id = gene.gene_id or self.make_gene_id(gene.content)
        if self.read_only:
            log.debug("read_only: skipping upsert_doc")
            return gene_id

        # Struggle 1 fix: apply density gate at the storage boundary so
        # that bulk ingest scripts (ingest_steam.py, ingest_fdrive.py,
        # ingest_all.py) calling upsert_gene directly also respect the
        # gate. Previously the gate lived in context_manager.ingest() and
        # was bypassed by every bulk ingest path. See:
        #   scripts/simulate_density_gate_v2.py for the empirical basis
        #   (51.6% of the noise-diluted knowledge store demoted, >97% signal retained).
        #
        # Crucially, the gate only acts on documents arriving as OPEN — if the
        # caller has explicitly set EUCHROMATIN or HETEROCHROMATIN, we
        # trust that decision. This means cross-store import imports, test fixtures, and
        # any code that deliberately creates demoted documents retain their
        # intended state. The gate is admission-control, not state-reset.
        if apply_gate and gene.chromatin == ChromatinState.OPEN:
            new_state, reason = self.apply_density_gate(gene)
            if new_state != gene.chromatin:
                log.debug(
                    "Density gate demoted %s: OPEN -> %s (reason=%s)",
                    gene_id, new_state.name, reason,
                )
                gene.chromatin = new_state
        else:
            reason = "gate_bypassed" if not apply_gate else "explicit_chromatin_preserved"

        # Compute compression tier from final lifecycle tier
        tier = 0  # OPEN
        if gene.chromatin == ChromatinState.EUCHROMATIN:
            tier = 1
        elif gene.chromatin == ChromatinState.HETEROCHROMATIN:
            tier = 2

        cur = self.conn.cursor()
        observed_at = (
            gene.observed_at
            if gene.observed_at is not None
            else getattr(gene.epigenetics, "created_at", None)
        )
        content_hash = gene.content_hash
        if content_hash is None and gene.content:
            content_hash = hashlib.sha256(gene.content.encode("utf-8")).hexdigest()
        # Stage 7 (2026-05-08, spec §4) — wire ``last_verified_at`` on
        # ingest. The column has shipped since Stage 1 (DDL row :471,
        # ALTER row :499); Stage 7 only ensures the writer populates it.
        # Order: document-supplied > observed_at fallback > now(). Setting
        # to now() on legacy bare-inserts means the freshness gate has
        # something to compare mtime against on the very next turn,
        # rather than treating every newly-ingested document as
        # "freshness unknown" forever.
        last_verified_at = (
            gene.last_verified_at
            if gene.last_verified_at is not None
            else (observed_at if observed_at is not None else time.time())
        )

        cur.execute(
            "INSERT OR REPLACE INTO genes "
            "(gene_id, content, complement, codons, promoter, epigenetics, "
            "chromatin, is_fragment, embedding, source_id, repo_root, source_kind, "
            "observed_at, mtime, content_hash, volatility_class, authority_class, "
            "support_span, last_verified_at, version, supersedes, key_values, "
            "compression_tier, last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                gene_id,
                gene.content,
                gene.complement,
                json_dumps(gene.codons),
                gene.promoter.model_dump_json(),
                gene.epigenetics.model_dump_json(),
                int(gene.chromatin),
                int(gene.is_fragment),
                json_dumps(gene.embedding) if gene.embedding else None,
                gene.source_id,
                gene.repo_root,
                gene.source_kind,
                observed_at,
                gene.mtime,
                content_hash,
                gene.volatility_class,
                gene.authority_class,
                gene.support_span,
                last_verified_at,
                gene.version,
                gene.supersedes,
                json_dumps(gene.key_values) if gene.key_values else None,
                tier,
                time.time(),  # last_seen: always stamp current epoch on every upsert
            ),
        )
        # Invalidate parse cache for this document's promoter/epigenetics
        clear_parse_caches()

        # ── Index population (delegated to storage.indexes) ──────────
        from .storage.indexes import (
            rebuild_promoter_index,
            sync_fts5,
            sync_entity_graph,
            sync_path_key_index,
            sync_filename_index,
            sync_splade_index,
        )
        rebuild_promoter_index(cur, gene_id, gene)
        sync_fts5(cur, gene_id, gene, self._fts_available)
        sync_entity_graph(cur, gene_id, gene, self._entity_graph_enabled)
        sync_path_key_index(cur, gene_id, gene)
        sync_filename_index(cur, gene_id, gene.source_id)
        sync_splade_index(
            cur, gene_id, gene.content, self._splade_enabled,
            splade_sparse=splade_sparse,
        )

        # Single atomic commit — document + tags + FTS5 + entity graph + SPLADE
        self.conn.commit()

        # Periodic WAL checkpoint to prevent data loss on crash
        # PASSIVE every 50 documents (~non-blocking), TRUNCATE every 500 (resets WAL)
        self._upsert_count += 1
        if self._upsert_count % 500 == 0:
            self.checkpoint("TRUNCATE")
        elif self._upsert_count % 50 == 0:
            self.checkpoint("PASSIVE")

        # Invalidate ΣĒMA caches (new document may have embedding, and
        # lifecycle tier changes can reshuffle hot/cold tier membership)
        if self._sema_cache is not None:
            self._sema_cache = None
        if self._cold_sema_cache is not None:
            self._cold_sema_cache = None
        # Stage 2: invalidate the in-memory dense matrix. Rebuild is full
        # (~78 MiB / sub-200 ms at 18.9k rows) on first dense recall after
        # this batch. See spec §4 "Invalidation".
        self._invalidate_dense_matrix()

        # Notify persistence manager (if attached)
        if self._replication_mgr is not None:
            self._replication_mgr.notify_write()

        # Phase 2 claims hook: emit literal claims into main.db if wired.
        # Soft-fail — ingest should never break because of claim extraction.
        if self._main_conn is not None:
            try:
                from .identity.claims import extract_literal_claims, persist_claims
                claims = extract_literal_claims(gene, shard_name=self._shard_name)
                if claims:
                    persist_claims(self._main_conn, claims)
                    # Edge detection scoped to the new claims' entity_keys.
                    # Narrow scan keeps per-ingest cost bounded; groups
                    # larger than max_group_size are skipped anyway.
                    from .identity.claims_analyze import detect_and_persist_edges
                    touched_keys = {
                        c.entity_key for c in claims if c.entity_key
                    }
                    if touched_keys:
                        detect_and_persist_edges(
                            self._main_conn,
                            entity_keys=touched_keys,
                        )
            except Exception:
                log.warning("claim extraction failed at ingest", exc_info=True)

        return gene_id

    # ── Fix 1: synonym expansion ────────────────────────────────────

    def _expand_terms(self, terms: List[str]) -> List[str]:
        expanded = set(t.lower() for t in terms)
        for t in terms:
            key = t.lower()
            if key in self.synonym_map:
                expanded.update(self.synonym_map[key])
        # ``sorted`` rather than ``list(set(...))`` so iteration order
        # is independent of PYTHONHASHSEED. Downstream SQL parameter
        # ordering and rank tiebreaks become deterministic across
        # processes (matters when bench replays the same query in a
        # freshly-spawned uvicorn — see #131).
        return sorted(expanded)

    # ── Authority boosts: distinguish "about X" from "mentions X" ──

    def _apply_authority_boosts(
        self,
        cur,
        gene_scores: Dict[str, float],
        query_terms: List[str],
        rerank_additive: Optional[Dict[str, float]] = None,
        tier_contrib: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> None:
        """
        Post-rank boosts that distinguish authoritative documents from tangential ones.

        Three signals:
          1. Source authority (+2.0): query term in source_id path
             — a file named BENCHMARK_NOTES.md answering "benchmark" is authoritative
          2. Domain primacy (+1.5): query term in top-3 tags domains
             — primary domains = what the document is ABOUT, not mentions
          3. Creation recency (+0.5): document created in last 48 hours
             — bootstraps new concepts before they build co-activation history

        All boosts are additive to existing scores. Low risk — only raises
        the ceiling on already-scored documents, never adds new candidates.
        """
        if not gene_scores:
            return

        import time as _time
        now = _time.time()
        recency_window = 48 * 3600  # 48 hours in seconds

        gene_ids = list(gene_scores.keys())
        id_ph = ",".join("?" * len(gene_ids))
        lower_terms = [t.lower() for t in query_terms]

        # Fetch source_id, tags, signals for all candidates in one query
        rows = cur.execute(
            f"SELECT gene_id, source_id, promoter, epigenetics "
            f"FROM genes WHERE gene_id IN ({id_ph})",
            gene_ids,
        ).fetchall()

        for r in rows:
            gid = r["gene_id"]
            boost = 0.0

            # 1. Source authority: query term in path
            source = (r["source_id"] or "").lower()
            if source and any(t in source for t in lower_terms):
                boost += 2.0

            # 2. Domain primacy: query term in top-3 tags domains
            try:
                prom = parse_promoter(r["promoter"]) if r["promoter"] else None
                if prom and prom.domains:
                    primary_domains = {d.lower() for d in prom.domains[:3]}
                    if any(t in primary_domains for t in lower_terms):
                        boost += 1.5
            except Exception:
                pass

            # 3. Creation recency: document created in last 48h
            try:
                epi = parse_epigenetics(r["epigenetics"]) if r["epigenetics"] else None
                if epi and epi.created_at > 0:
                    age = now - epi.created_at
                    if 0 < age < recency_window:
                        boost += 0.5
            except Exception:
                pass

            if boost > 0:
                gene_scores[gid] += boost
                # Stage 3: dual-write to the post-fusion additives map
                # so RRF mode can apply authority on top of the fused
                # score (per spec §3 / §99: re-rank class).
                if rerank_additive is not None:
                    rerank_additive[gid] = rerank_additive.get(gid, 0.0) + boost
                if tier_contrib is not None:
                    tier_contrib.setdefault(gid, {})["authority"] = (
                        tier_contrib.get(gid, {}).get("authority", 0.0) + boost
                    )

    # ── Core retrieval (Step 2) — hybrid tags + FTS5 ────────────

    def fts_doc_count(self) -> int:
        """Total document count in this knowledge store's FTS5 index.

        Used by ShardRouter for cross-shard BM25 IDF normalization (#118).
        Returns 0 if FTS is unavailable.
        """
        if not self._fts_available:
            return 0
        try:
            row = self.read_conn.execute(
                "SELECT COUNT(*) FROM genes_fts"
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def term_doc_frequencies(self, terms: list[str]) -> Dict[str, int]:
        """Document frequency in this shard's FTS5 index, per term.

        Returns a dict ``{term: df}``. Missing/unknown terms map to 0.

        Used by ShardRouter to compute global IDFs for cross-shard
        BM25 normalization (#118). Implements one ``MATCH "term"`` COUNT
        per requested term — fine at query time since the number of
        query terms is bounded (typically <20) and SQLite's FTS5
        ``MATCH`` is fast on a single-token exact phrase.
        """
        result: Dict[str, int] = {t: 0 for t in terms}
        if not self._fts_available or not terms:
            return result
        cur = self.read_conn.cursor()
        try:
            for term in terms:
                # Skip terms that are too short to be FTS-indexed (mirrors
                # the >2 filter used elsewhere in query_docs).
                if not term or len(term) <= 2:
                    continue
                # Quote the term to disable FTS5 syntax interpretation —
                # treats hyphens, colons, etc. as literal characters.
                # Double any embedded quotes per FTS5 escape rules.
                escaped = term.replace('"', '""')
                try:
                    row = cur.execute(
                        'SELECT COUNT(*) FROM genes_fts WHERE genes_fts MATCH ?',
                        (f'"{escaped}"',),
                    ).fetchone()
                    if row:
                        result[term] = int(row[0])
                except Exception:
                    # Single-term FTS errors (e.g., malformed token) shouldn't
                    # poison the whole batch. df=0 is a safe default.
                    continue
        finally:
            cur.close()
        return result

    def _bm25_candidate_set(self, query_terms: list[str], size: int) -> set[str] | None:
        """Return FTS5 BM25 top-N gene_ids, or None if FTS unavailable/empty. Soft-fails to None."""
        if not self._fts_available:
            return None
        bm25_terms = [t for t in query_terms if len(t) > 2]
        if not bm25_terms:
            return None
        cur = self.read_conn.cursor()
        try:
            bm25_match = " OR ".join(f'"{t.replace(chr(34), chr(34)*2)}"' for t in bm25_terms)
            rows = cur.execute(
                "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH ? ORDER BY rank LIMIT ?",
                (bm25_match, size),
            ).fetchall()
            if not rows:
                return None
            return {r["gene_id"] for r in rows}
        except Exception:
            log.warning("BM25 pre-filter failed — falling back to full corpus", exc_info=True)
            return None
        finally:
            cur.close()

    def query_docs(
        self,
        domains: List[str],
        entities: List[str],
        max_genes: int = 8,
        party_id: Optional[str] = None,
        use_harmonic: bool = True,
        use_sr: Optional[bool] = None,
        use_entity_graph: Optional[bool] = None,
        read_only: bool = False,
    ) -> List[Gene]:
        """
        Find documents matching the given tags signals.

        Multi-tier retrieval:
            1. Exact tag match (highest confidence)
            2. Prefix tag match — "server" matches "serverconfig" (medium)
            3. FTS5 content search — searches document text directly (fallback)
            3.5 SPLADE sparse retrieval
            4. SEMA semantic retrieval + re-ranking
            5. Harmonic co-activation boost (mutual reinforcement)
            Tiebreaker: access-rate bonus for equal-scored documents

        When party_id is provided, Tiers 1-3 exclude documents attributed
        to OTHER parties (cross-party leakage prevention). Documents with
        NO attribution row (legacy ingests, bridge inbox drops without
        a participant_id) remain retrievable — without this fallback,
        retrieval on an unattributed legacy knowledge store would collapse to
        ~0 hits. Attributed-to-this-party documents do NOT get a retrieval
        bonus here — that is a separate concern handled at a higher
        layer (see roadmap Phase 2c).

        Results are merged with weighted scoring, then expanded via
        co-activation pull-forward. Returns up to max_genes * 2 candidates.
        """
        domains = self._expand_terms(domains)
        entities = self._expand_terms(entities)

        query_terms = domains + entities
        if not query_terms:
            raise PromoterMismatch("No query terms after expansion")

        self._refresh_snapshot()  # See latest WAL state (external thinning, deletes)
        cur = self.read_conn.cursor()  # Read path — avoids WAL lock contention
        limit = max_genes * 2

        # ── BM25 pre-filter (tier-0, 2026-05-08 upgrade) ──────────────
        _prefilter_set: set[str] | None = None
        if self._bm25_prefilter_enabled:
            _prefilter_set = self._bm25_candidate_set(query_terms, self._bm25_prefilter_size)
            log.debug("bm25 prefilter: size=%d result=%s", self._bm25_prefilter_size,
                      len(_prefilter_set) if _prefilter_set is not None else "fallback")

        _prefilter_aliased_clause = ""   # for Tier 1/2 using g.gene_id alias
        _prefilter_bare_clause = ""      # for Tier 3/3.5 without alias
        _prefilter_params: list = []
        if _prefilter_set is not None:
            _prefilter_ids = list(_prefilter_set)[:998]  # SQLite variable limit is 999
            ph = ",".join("?" * len(_prefilter_ids))
            _prefilter_aliased_clause = f" AND g.gene_id IN ({ph})"
            _prefilter_bare_clause = f" AND gene_id IN ({ph})"
            _prefilter_params = _prefilter_ids

        # Document scores: gene_id → float (accumulated across tiers)
        gene_scores: Dict[str, float] = {}

        # Per-tier contribution tracking (parallel to gene_scores).
        # Each accumulation point also writes the contribution to
        # tier_contrib[gid][tier_name]. Surfaced via last_tier_contributions
        # for the activation profiler bench (bench_skill_activation.py).
        tier_contrib: Dict[str, Dict[str, float]] = {}

        # ── Stage 3: Reciprocal Rank Fusion accumulator ────────────
        # Spec: docs/specs/2026-05-08-stage-3-rrf-fusion.md.
        # In "rrf" mode, every recall/discovery tier ALSO calls
        # fuser.add_tier(...) with its (gid, raw_score) ranked list and
        # the operator-tunable post-multiplier weight. The final sort
        # branches on _fusion_mode at line ~2489.
        #
        # In "additive" mode, the Fuser is built but never queried —
        # the existing gene_scores accumulator runs unchanged so the
        # output is byte-identical to pre-Stage-3 ranking.
        #
        # Re-rank/tiebreaker tiers (sema_boost, authority, party_attr,
        # access_rate) stay ADDITIVE on top of the fused score. Per
        # spec §3 rule of thumb: agreement across recall tiers is the
        # signal RRF retrieves; "is this document authoritative?" is a
        # different question that survives unchanged.
        from .retrieval.fusion import Fuser as _Fuser
        fuser = _Fuser(k=self._rrf_k)

        # ── Stage 3: re-rank-class additive collector ──────────────
        # The post-fusion additives — sema_boost (gate-only re-rank
        # confidence boost), authority_*, party_attr, access_rate —
        # capture the "is this document authoritative?" signal. Per spec
        # §3 these stay ADDITIVE on top of the fused score, NOT
        # rank-fused. We dual-write them to gene_scores (additive mode
        # path) and to rerank_additive (RRF mode path) so the sort can
        # trivially compose fused + rerank without re-deriving anything.
        rerank_additive: Dict[str, float] = {}

        # ── party_id filter clause (reused across Tiers 1-3) ──────
        # Semantics: when party_id is provided, return documents that are
        # EITHER attributed to this party OR have no attribution at all
        # (legacy documents ingested before the registry shipped). This keeps
        # retrieval useful on the predominantly-unattributed current
        # knowledge store — a strict IN(...) clause would collapse to ~0 hits.
        # Cross-party leakage is still prevented: documents attributed to a
        # DIFFERENT party are excluded via the NOT IN sub-select.
        _party_filter = ""
        _party_params: list = []
        if party_id is not None:
            try:
                _has_attr_table = cur.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type='table' AND name='gene_attribution'"
                ).fetchone()[0]
            except Exception:
                _has_attr_table = False
            if _has_attr_table:
                _party_filter = (
                    " AND ("
                    "g.gene_id IN (SELECT gene_id FROM gene_attribution WHERE party_id = ?)"
                    " OR g.gene_id NOT IN (SELECT gene_id FROM gene_attribution)"
                    ")"
                )
                _party_params = [party_id]

        # ── Tier 0: path-key compound index (IDF-weighted) ─────────
        # Highest-confidence retrieval signal — a hit means the query
        # mentions both a path-token (project/module) AND a kv_key that
        # an indexed document was tagged with at ingest. This catches
        # template queries like "what is the value of helix_port?" where
        # FTS5/SPLADE both miss because the query lacks domain context.
        #
        # CRITICAL: bonus is INVERSELY proportional to the (path_token,
        # kv_key) pair cardinality. Rare pairs like (helix, port) — only
        # 2-3 documents share — each get a strong boost (~+5). Common pairs
        # like (steamapps, url) — 3000+ documents share — get ~zero boost,
        # so they don't drown the signal. This is the standard IDF
        # idea applied to compound retrieval keys.
        #
        # Without IDF weighting, a query containing common terms like
        # "url" or "value" would dump +8 on thousands of false-positive
        # documents, regressing retrieval (empirically observed 12% -> 6%
        # on the 2026-04-12 KV-harvest bench before this fix).
        q_lower_tokens = [t.lower() for t in query_terms if t]
        if q_lower_tokens:
            _pki_t0 = time.monotonic()
            try:
                # Group hits by (path_token, kv_key) to compute per-pair
                # cardinality. SQLite GROUP BY + COUNT does this in one
                # pass over the index.
                pt_ph = ",".join("?" * len(q_lower_tokens))
                kk_ph = ",".join("?" * len(q_lower_tokens))
                pki_sql = (
                    f"SELECT path_token, kv_key, gene_id FROM path_key_index "
                    f"WHERE path_token IN ({pt_ph}) AND kv_key IN ({kk_ph})"
                )
                pki_params = list(q_lower_tokens) + list(q_lower_tokens)
                if party_id is not None and _party_filter:
                    pki_sql = (
                        f"SELECT path_token, kv_key, gene_id FROM path_key_index "
                        f"WHERE path_token IN ({pt_ph}) "
                        f"AND kv_key IN ({kk_ph}) "
                        f"AND gene_id IN (SELECT gene_id FROM "
                        f"gene_attribution WHERE party_id = ?)"
                    )
                    pki_params = pki_params + [party_id]
                pki_hits = cur.execute(pki_sql, pki_params).fetchall()

                # Bucket: pair_count[(pt, kk)] = number of distinct documents
                # gene_pairs[gene_id] = list of (pt, kk) pairs that hit
                pair_count: Dict[tuple, int] = {}
                gene_pairs: Dict[str, list] = {}
                for r in pki_hits:
                    pt = r["path_token"]
                    kk = r["kv_key"]
                    gid = r["gene_id"]
                    pair_count[(pt, kk)] = pair_count.get((pt, kk), 0) + 1
                    gene_pairs.setdefault(gid, []).append((pt, kk))

                # Score each document by sum of inverse-cardinality boosts
                # over all (pt, kk) pairs it matched on.
                #
                # Boost formula:
                #   per-pair bonus = PKI_BASE / max(pair_card, PKI_FLOOR)
                # where:
                #   PKI_BASE  = 10.0  (so a unique pair lands at +10)
                #   PKI_FLOOR =  2.0  (caps top-end at +5 for 2-document pairs)
                # A pair with 100 documents contributes only +0.1 per document —
                # essentially noise. A pair with 5 documents contributes +2.
                PKI_BASE = 10.0
                PKI_FLOOR = 2.0
                # Hard-skip pairs with cardinality > this — they're noise
                PKI_NOISE_CUTOFF = 200
                _pki_ranked: List[Tuple[str, float]] = []  # Stage 3 RRF
                for gid, pairs in gene_pairs.items():
                    bonus = 0.0
                    for pair in pairs:
                        card = pair_count[pair]
                        if card > PKI_NOISE_CUTOFF:
                            continue  # too common, skip
                        bonus += PKI_BASE / max(card, PKI_FLOOR)
                    if bonus > 0:
                        # Cap total bonus to keep one runaway document from
                        # saturating; 12.0 is roughly 3x the strongest
                        # single signal.
                        capped = min(bonus, 12.0)
                        gene_scores[gid] = gene_scores.get(gid, 0) + capped
                        tier_contrib.setdefault(gid, {})["pki"] = capped
                        _pki_ranked.append((gid, capped))
                # Stage 3: feed PKI ranks into the Fuser regardless of
                # fusion_mode — the Fuser is queried only when "rrf",
                # so additive mode pays a tiny add_tier cost (~1µs per
                # 100 documents) but stays byte-identical at the output.
                fuser.add_tier("pki", _pki_ranked, weight=self._pki_weight)
            except Exception as exc:
                log.debug("path_key_index tier skipped: %s", exc)
            finally:
                try:
                    from .telemetry import genome_signal_histogram
                    genome_signal_histogram().record(
                        time.monotonic() - _pki_t0, {"signal": "pki"}
                    )
                except Exception:
                    pass

        # ── Tier 0.5: filename-anchor boost (flag-gated spike) ─────
        # Dewey bench 2026-04-14: filename alone drives retrieval lift;
        # project/module over-constrain once filename pins location.
        # Boosts documents whose filename_stem matches a query term.
        # Flag-off is a no-op. See helix_context/filename_anchor.py.
        if getattr(self, "_filename_anchor_enabled", False):
            try:
                from . import filename_anchor as _fa
                # Stage 3: capture which gids the filename_anchor tier
                # contributed to so we can feed the Fuser. The boost
                # helper writes tier_contrib[gid]["filename_anchor"] —
                # we diff before/after to extract the (gid, score) pairs.
                _fa_gids_before = {
                    gid for gid, contribs in tier_contrib.items()
                    if "filename_anchor" in contribs
                }
                _fa.boost_scores(
                    cur.connection,
                    query_terms,
                    gene_scores,
                    tier_contrib,
                    weight=getattr(self, "_filename_anchor_weight", 4.0),
                    party_filter_sql=_party_filter,
                    party_params=tuple(_party_params),
                )
                _fa_ranked: List[Tuple[str, float]] = [
                    (gid, contribs["filename_anchor"])
                    for gid, contribs in tier_contrib.items()
                    if "filename_anchor" in contribs and gid not in _fa_gids_before
                ]
                fuser.add_tier(
                    "filename_anchor",
                    _fa_ranked,
                    weight=getattr(self, "_filename_anchor_weight", 4.0),
                )
            except Exception as exc:
                log.debug("filename_anchor tier skipped: %s", exc)

        # ── Tier 1: exact tag match (weight 3.0) ──────────
        _tag_exact_t0 = time.monotonic()
        placeholders = ",".join("?" * len(query_terms))
        rows = cur.execute(
            f"""
            SELECT g.gene_id, COUNT(pi.tag_value) AS match_count
            FROM genes g
            JOIN promoter_index pi ON g.gene_id = pi.gene_id
            WHERE pi.tag_value IN ({placeholders})
              AND g.chromatin < ?
              {_party_filter}
              {_prefilter_aliased_clause}
            GROUP BY g.gene_id
            """,
            (*query_terms, int(ChromatinState.HETEROCHROMATIN), *_party_params, *_prefilter_params),
        ).fetchall()

        _tag_exact_ranked: List[Tuple[str, float]] = []  # Stage 3 RRF
        for r in rows:
            tag_score = r["match_count"] * 3.0
            gene_scores[r["gene_id"]] = tag_score
            tier_contrib.setdefault(r["gene_id"], {})["tag_exact"] = tag_score
            _tag_exact_ranked.append((r["gene_id"], tag_score))
        # Stage 3: count tier — rank by raw score (= match_count × 3.0)
        # which is monotone in match_count, so the rank order matches
        # the spec's "rank by match_count descending" rule (§4).
        fuser.add_tier("tag_exact", _tag_exact_ranked, weight=self._tag_exact_weight)
        try:
            from .telemetry import genome_signal_histogram
            genome_signal_histogram().record(
                time.monotonic() - _tag_exact_t0, {"signal": "tag_exact"}
            )
        except Exception:
            pass

        # ── Tier 2: prefix tag match (weight 1.5) ──────────────────
        # "server" matches "serverconfig", "server_api", etc.
        _tag_prefix_t0 = time.monotonic()
        prefix_conditions = " OR ".join(
            "pi.tag_value LIKE ?" for _ in query_terms
        )
        prefix_params = [f"{t}%" for t in query_terms]
        rows = cur.execute(
            f"""
            SELECT g.gene_id, COUNT(pi.tag_value) AS match_count
            FROM genes g
            JOIN promoter_index pi ON g.gene_id = pi.gene_id
            WHERE ({prefix_conditions})
              AND g.chromatin < ?
              {_party_filter}
              {_prefilter_aliased_clause}
            GROUP BY g.gene_id
            """,
            (*prefix_params, int(ChromatinState.HETEROCHROMATIN), *_party_params, *_prefilter_params),
        ).fetchall()

        _tag_prefix_ranked: List[Tuple[str, float]] = []  # Stage 3 RRF
        for r in rows:
            gid = r["gene_id"]
            prefix_score = r["match_count"] * 1.5
            gene_scores[gid] = gene_scores.get(gid, 0) + prefix_score
            tier_contrib.setdefault(gid, {})["tag_prefix"] = prefix_score
            _tag_prefix_ranked.append((gid, prefix_score))
        fuser.add_tier("tag_prefix", _tag_prefix_ranked, weight=self._tag_prefix_weight)
        try:
            from .telemetry import genome_signal_histogram
            genome_signal_histogram().record(
                time.monotonic() - _tag_prefix_t0, {"signal": "tag_prefix"}
            )
        except Exception:
            pass

        # ── Tier 3: FTS5 content search (weight 3.0) ───────────────
        if self._fts_available:
            # Build FTS5 query: OR-join all terms
            _fts5_t0 = time.monotonic()
            fts_query = " OR ".join(
                f'"{t}"' for t in query_terms if len(t) > 2
            )
            if fts_query:
                try:
                    fts_rows = cur.execute(
                        f"""
                        SELECT gene_id, rank
                        FROM genes_fts
                        WHERE genes_fts MATCH ?
                        {_prefilter_bare_clause}
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (fts_query, *_prefilter_params, limit * 2),
                    ).fetchall()

                    # Filter by lifecycle tier (batch lookup)
                    if fts_rows:
                        fts_ids = [r["gene_id"] for r in fts_rows]
                        fts_ranks = {r["gene_id"]: r["rank"] for r in fts_rows}
                        id_ph = ",".join("?" * len(fts_ids))
                        valid = cur.execute(
                            f"SELECT gene_id FROM genes g "
                            f"WHERE g.gene_id IN ({id_ph}) AND g.chromatin < ?"
                            f" {_party_filter}",
                            (*fts_ids, int(ChromatinState.HETEROCHROMATIN), *_party_params),
                        ).fetchall()
                        valid_ids = {r["gene_id"] for r in valid}

                        _fts5_ranked: List[Tuple[str, float]] = []  # Stage 3 RRF
                        for gid in fts_ids:
                            if gid not in valid_ids:
                                continue
                            # FTS5 rank is negative (lower = better match)
                            # Normalize: -rank gives positive, cap at 6.0
                            # (was 15*3=45 — drowned out tag matches at 3-9)
                            fts_score = min(-fts_ranks[gid], 6.0)
                            gene_scores[gid] = gene_scores.get(gid, 0) + fts_score
                            tier_contrib.setdefault(gid, {})["fts5"] = fts_score
                            # Stage 3: feed Fuser with the RAW
                            # negative-bm25 magnitude (-fts_ranks[gid])
                            # rather than the capped value, so the rank
                            # order matches FTS5's true ordering even at
                            # the score-cap saturation point.
                            _fts5_ranked.append((gid, -fts_ranks[gid]))
                        fuser.add_tier("fts5", _fts5_ranked, weight=self._fts5_weight)
                except Exception:
                    log.warning("FTS5 query failed", exc_info=True)
                finally:
                    try:
                        from .telemetry import genome_signal_histogram
                        genome_signal_histogram().record(
                            time.monotonic() - _fts5_t0, {"signal": "fts5"}
                        )
                    except Exception:
                        pass

        # ── Tier 3.5: SPLADE sparse retrieval (weight 3.5) ─────────
        if self._splade_enabled:
            _splade_t0 = time.monotonic()
            try:
                from .backends import splade_backend
                # Check if splade_terms table exists
                has_table = cur.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='splade_terms'"
                ).fetchone()[0]
                if has_table:
                    query_text = " ".join(query_terms)
                    query_sparse = splade_backend.encode(query_text)
                    splade_hits = splade_backend.query_splade(self.read_conn, query_sparse, limit=limit * 2)
                    if _prefilter_set is not None:
                        splade_hits = [(gid, s) for gid, s in splade_hits if gid in _prefilter_set]
                    _splade_ranked: List[Tuple[str, float]] = []  # Stage 3 RRF
                    for gid, score in splade_hits:
                        # Normalize SPLADE score to be comparable with other tiers
                        splade_score = min(score, 20.0) * 3.5 / 20.0  # Cap at 3.5
                        gene_scores[gid] = gene_scores.get(gid, 0) + splade_score
                        tier_contrib.setdefault(gid, {})["splade"] = splade_score
                        # Stage 3: feed Fuser with the RAW SPLADE score
                        # (uncapped) so saturated documents still have a
                        # well-defined rank.
                        _splade_ranked.append((gid, float(score)))
                    fuser.add_tier("splade", _splade_ranked, weight=self._splade_weight)
            except Exception:
                log.warning("SPLADE retrieval failed", exc_info=True)
            finally:
                try:
                    from .telemetry import genome_signal_histogram
                    genome_signal_histogram().record(
                        time.monotonic() - _splade_t0, {"signal": "splade"}
                    )
                except Exception:
                    pass

        # ── Tier 4: ΣĒMA semantic retrieval + re-ranking ───────────────
        # Two modes:
        #   A) Boost existing candidates (when Tiers 1-3.5 have candidates)
        #   B) Add new candidates via vector scan (when pool is too small)
        if self._sema_codec is not None:
            try:
                query_text = " ".join(query_terms)
                query_vec = self._sema_codec.encode(query_text)
                top_score = max(gene_scores.values()) if gene_scores else 0

                # Mode A: Boost existing candidates when confidence is weak
                _sema_boost_t0 = time.monotonic()
                if gene_scores and top_score < 20.0:
                    existing_ids = list(gene_scores.keys())
                    id_ph = ",".join("?" * len(existing_ids))
                    sema_rows = cur.execute(
                        f"SELECT gene_id, embedding FROM genes "
                        f"WHERE gene_id IN ({id_ph}) AND embedding IS NOT NULL",
                        existing_ids,
                    ).fetchall()

                    if sema_rows:
                        candidates_sema = []
                        for r in sema_rows:
                            try:
                                vec = json_loads(r["embedding"])
                                if isinstance(vec, list) and len(vec) == 20:
                                    candidates_sema.append((r["gene_id"], vec))
                            except Exception:
                                continue

                        if candidates_sema:
                            nearest = self._sema_codec.nearest(
                                query_vec, candidates_sema, k=len(candidates_sema),
                            )
                            for gid, sim in nearest:
                                if sim > 0.3:
                                    boost_scale = max(0.5, 1.0 - top_score / 40.0)
                                    sema_boost = sim * 2.0 * boost_scale
                                    gene_scores[gid] += sema_boost
                                    tier_contrib.setdefault(gid, {})["sema_boost"] = sema_boost
                                    # Stage 3: post-fusion additive (re-rank class).
                                    rerank_additive[gid] = rerank_additive.get(gid, 0.0) + sema_boost
                try:
                    from .telemetry import genome_signal_histogram
                    genome_signal_histogram().record(
                        time.monotonic() - _sema_boost_t0, {"signal": "sema_boost"}
                    )
                except Exception:
                    pass

                # Mode B: Add new candidates when pool is undersized
                # Uses pre-materialized numpy cache for fast cosine scan
                # instead of deserializing 7K JSON blobs per query.
                if len(gene_scores) < limit // 2:
                    # Build cache on first use (lazy init)
                    if self._sema_cache is None:
                        self._build_sema_cache()

                    if self._sema_cache is not None:
                        try:
                            import numpy as np
                            cache = self._sema_cache
                            q = np.array(query_vec, dtype=np.float32)
                            q_norm = np.linalg.norm(q)
                            if q_norm > 1e-8:
                                q = q / q_norm
                                # Batch cosine similarity: (N,20) @ (20,) → (N,)
                                sims = cache["matrix"] @ q
                                # Mask already-scored documents
                                existing = set(gene_scores.keys())
                                fill_count = limit - len(gene_scores)
                                # Get top-k indices
                                top_idx = np.argsort(sims)[::-1]
                                added = 0
                                _sema_cold_ranked: List[Tuple[str, float]] = []
                                for idx in top_idx:
                                    if added >= fill_count:
                                        break
                                    gid = cache["gene_ids"][idx]
                                    sim = float(sims[idx])
                                    if gid in existing:
                                        continue
                                    if sim > 0.4:
                                        sema_new = sim * 3.0
                                        gene_scores[gid] = sema_new
                                        tier_contrib.setdefault(gid, {})["sema_cold"] = sema_new
                                        # Stage 3: rank by RAW cosine
                                        # similarity, not the *3.0
                                        # multiplied score — the
                                        # multiplier is the additive
                                        # weight, but rank comes from
                                        # the cosine ordering itself.
                                        _sema_cold_ranked.append((gid, sim))
                                        added += 1
                                fuser.add_tier(
                                    "sema_cold", _sema_cold_ranked,
                                    weight=self._sema_cold_weight,
                                )
                        except ImportError:
                            pass  # numpy not available
            except Exception:
                log.debug("ΣĒMA retrieval failed, continuing without")

        # ── Stage 2/3: dense recall as RRF participant ──────────────
        # Stage 2's query_genes_dense_recall returns [(gid, cosine), ...]
        # already sorted descending. We feed it directly as a tier so
        # the Fuser can rank-fuse it with the lex tiers under RRF mode.
        # This also seeds gene_scores in additive mode so the dense
        # contribution survives the back-compat path — but at the
        # cosine·dense_weight scale it's roughly noise next to the lex
        # weights (3.0+), which is why Stage 2 didn't merge it into
        # query_genes() in the first place. Under RRF, cosine ordering
        # is what matters and the rank-1 dense hit gets the same
        # 1/(k+1) weight as the rank-1 FTS hit.
        # Dense participation runs only under RRF mode. In additive mode
        # we skip it to keep byte-identical compatibility with pre-Stage-3
        # ranking — pre-Stage-3 query_genes() never called dense recall
        # (dense was query_genes_ann's job). Stage 4 may revisit this.
        if self._dense_embedding_enabled and self._fusion_mode == "rrf":
            try:
                _dense_t0 = time.monotonic()
                dense_hits = self.query_docs_dense_recall(
                    " ".join(query_terms),
                    k=min(self._dense_pool_size, limit * 4),
                    party_id=party_id,
                    read_only=read_only,
                )
                # Stage 3: feed Fuser. raw_score = cosine.
                fuser.add_tier(
                    "dense", dense_hits, weight=self._dense_weight,
                )
                # Also record in tier_contrib for telemetry (raw cosine,
                # not multiplied — matches the rule "telemetry observes
                # raw pre-RRF scores", spec §6).
                for gid, cosine in dense_hits:
                    tier_contrib.setdefault(gid, {})["dense"] = float(cosine)
                    # Ensure dense-only documents appear in gene_scores so
                    # they survive the eligible_ids gate at the sort.
                    # Use a tiny epsilon so we don't perturb additive
                    # ranking (which is unreachable in this branch
                    # anyway). The actual ordering comes from the Fuser.
                    if gid not in gene_scores:
                        gene_scores[gid] = 1e-9
                try:
                    from .telemetry import genome_signal_histogram
                    genome_signal_histogram().record(
                        time.monotonic() - _dense_t0, {"signal": "dense"}
                    )
                except Exception:
                    pass
            except Exception:
                log.debug("dense recall tier skipped", exc_info=True)

        if not gene_scores:
            raise PromoterMismatch("Zero genes matched across all tiers")

        # ── Lexical anchoring: IDF-weighted rare-term boost ────────
        # Weight query terms by inverse document frequency — rare terms
        # are stronger discriminators. A document matching "conductor" (3 documents)
        # is much more likely the answer than one matching "biged" (200+ documents).
        # Use the real (memoized) knowledge store size, NOT len(gene_scores) — the
        # latter is the scored-candidate pool and collapses IDF to ~0 on
        # large knowledge stores, nullifying the boost.
        total_genes_est = max(self.corpus_size(), len(gene_scores), 100)
        import math as _math
        # Stage 3: lex_anchor accumulates over multiple query terms.
        # We capture per-document total contribution after the loop and
        # rank-feed it as one tier (a single tier name covering all
        # IDF-anchored documents — same semantics as the existing
        # tier_contrib["lex_anchor"] aggregation).
        for term in query_terms:
            term_freq = cur.execute(
                "SELECT COUNT(DISTINCT gene_id) FROM promoter_index WHERE tag_value = ?",
                (term,),
            ).fetchone()[0]
            if term_freq == 0:
                continue
            # IDF boost: rare terms get up to 3.0, common terms ~0.5
            # Capped at 3.0 (was 5.0) to reduce tangential rare-term over-boost.
            idf = _math.log(total_genes_est / term_freq) if term_freq > 0 else 0
            boost = min(idf * 1.5, 3.0)
            if boost > 1.0:
                anchor_genes = cur.execute(
                    f"SELECT pi.gene_id FROM promoter_index pi "
                    f"JOIN genes g ON pi.gene_id = g.gene_id "
                    f"WHERE pi.tag_value = ? AND g.chromatin < ?"
                    f" {_party_filter}"
                    f" {_prefilter_aliased_clause}",
                    (term, int(ChromatinState.HETEROCHROMATIN), *_party_params, *_prefilter_params),
                ).fetchall()
                for r in anchor_genes:
                    gid = r["gene_id"]
                    gene_scores[gid] = gene_scores.get(gid, 0) + boost
                    # Anchor IDF can fire for multiple terms in same query; sum them.
                    tc = tier_contrib.setdefault(gid, {})
                    tc["lex_anchor"] = tc.get("lex_anchor", 0.0) + boost
        # Stage 3: feed accumulated lex_anchor totals into the Fuser.
        # Aggregated total IS the per-document strength of the IDF signal,
        # so ranking by it is the right thing.
        _lex_anchor_ranked: List[Tuple[str, float]] = [
            (gid, contribs["lex_anchor"])
            for gid, contribs in tier_contrib.items()
            if "lex_anchor" in contribs
        ]
        fuser.add_tier(
            "lex_anchor", _lex_anchor_ranked, weight=self._lex_anchor_weight,
        )

        # ── Authority boosts: distinguish "about X" from "mentions X" ──
        # Stage 3: thread rerank_additive + tier_contrib so RRF mode
        # can apply the authority bonus on top of the fused score.
        self._apply_authority_boosts(
            cur, gene_scores, query_terms,
            rerank_additive=rerank_additive,
            tier_contrib=tier_contrib,
        )

        # ── Tier 5: harmonic co-activation boost ──────────────────
        # For each candidate, add a score bonus from documents that are
        # harmonically linked to OTHER candidates (mutual reinforcement).
        # Weight: 1.0 per link, capped at 3.0 total bonus.
        if use_harmonic and gene_scores:
            try:
                _has_harmonic = cur.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type='table' AND name='harmonic_links'"
                ).fetchone()[0]
            except Exception:
                _has_harmonic = False
            if _has_harmonic:
                try:
                    candidate_ids = list(gene_scores.keys())
                    cid_ph = ",".join("?" * len(candidate_ids))
                    harmonic_rows = cur.execute(
                        f"SELECT gene_id_a, gene_id_b, weight "
                        f"FROM harmonic_links "
                        f"WHERE gene_id_a IN ({cid_ph}) "
                        f"  AND gene_id_b IN ({cid_ph})",
                        (*candidate_ids, *candidate_ids),
                    ).fetchall()
                    harmonic_bonus: Dict[str, float] = {}
                    for hr in harmonic_rows:
                        for gid in (hr["gene_id_a"], hr["gene_id_b"]):
                            harmonic_bonus[gid] = min(
                                harmonic_bonus.get(gid, 0) + 1.0, 3.0,
                            )
                    _harmonic_ranked: List[Tuple[str, float]] = []  # Stage 3 RRF
                    for gid, bonus in harmonic_bonus.items():
                        gene_scores[gid] = gene_scores.get(gid, 0) + bonus
                        tier_contrib.setdefault(gid, {})["harmonic"] = bonus
                        _harmonic_ranked.append((gid, bonus))
                    fuser.add_tier(
                        "harmonic", _harmonic_ranked, weight=self._harmonic_weight,
                    )
                except Exception:
                    log.debug("Harmonic boost failed", exc_info=True)

        # ── Tier 5.5: Successor Representation boost ──────────────
        # Discounted future-occupancy over the co-activation graph.
        # Generalises Tier 5's 1-hop harmonic pull-forward to a
        # gamma-discounted k-step horizon. See helix_context/sr.py and
        # docs/FUTURE/SUCCESSOR_REPRESENTATION.md. Feature-flagged
        # (retrieval.sr_enabled) so it can A/B before promotion.
        sr_enabled = self._sr_enabled if use_sr is None else bool(use_sr)
        if sr_enabled and gene_scores:
            try:
                from .retrieval.sr import sr_boost
                sr_bonus = sr_boost(
                    self,
                    list(gene_scores.keys()),
                    gamma=self._sr_gamma,
                    k_steps=self._sr_k_steps,
                    weight=self._sr_weight,
                    cap=self._sr_cap,
                )
                _sr_ranked: List[Tuple[str, float]] = []  # Stage 3 RRF
                for gid, bonus in sr_bonus.items():
                    gene_scores[gid] = gene_scores.get(gid, 0) + bonus
                    tier_contrib.setdefault(gid, {})["sr"] = bonus
                    _sr_ranked.append((gid, bonus))
                # Reuse existing _sr_weight knob (spec §3).
                fuser.add_tier("sr", _sr_ranked, weight=self._sr_weight)
            except Exception:
                log.debug("SR Tier 5.5 failed", exc_info=True)

        # ── Tier 5b: entity graph co-occurrence boost ─────────────────────
        # Documents sharing entity nodes with query entities get a score boost.
        # Additive on top of Tier 5 harmonic; capped at +2.0 per document.
        # entity_graph schema: entity (TEXT), gene_id (TEXT) — no weight col.
        _eg_enabled = self._entity_graph_retrieval_enabled if use_entity_graph is None else bool(use_entity_graph)
        if _eg_enabled and entities and gene_scores:
            try:
                _eg_t0 = time.monotonic()
                eq_ph = ",".join("?" * len(entities))
                eg_rows = cur.execute(
                    f"SELECT gene_id FROM entity_graph "
                    f"WHERE entity IN ({eq_ph})",
                    entities,
                ).fetchall()
                _eg_ranked: List[Tuple[str, float]] = []  # Stage 3 RRF
                for row in eg_rows:
                    gid = row["gene_id"]
                    if gid in gene_scores:
                        bonus = min(1.0 * 0.5, 2.0)  # weight=1.0, cap 2.0
                        gene_scores[gid] += bonus
                        tier_contrib.setdefault(gid, {})["entity_graph"] = (
                            tier_contrib.get(gid, {}).get("entity_graph", 0.0) + bonus
                        )
                        _eg_ranked.append((gid, bonus))
                fuser.add_tier(
                    "entity_graph", _eg_ranked, weight=self._entity_graph_weight,
                )
                log.debug("tier 5b entity_graph: %d hits, %.1fms",
                          len(eg_rows), (time.monotonic() - _eg_t0) * 1000)
            except Exception:
                log.warning("entity_graph tier 5b failed", exc_info=True)

        # ── Party attribution bonus (+0.5) ────────────────────────
        if party_id is not None and _party_filter and gene_scores:
            try:
                attr_ids_ph = ",".join("?" * len(gene_scores))
                attr_rows = cur.execute(
                    f"SELECT gene_id FROM gene_attribution "
                    f"WHERE party_id = ? AND gene_id IN ({attr_ids_ph})",
                    (party_id, *list(gene_scores.keys())),
                ).fetchall()
                for ar in attr_rows:
                    gid = ar["gene_id"]
                    gene_scores[gid] += 0.5
                    tier_contrib.setdefault(gid, {})["party_attr"] = 0.5
                    # Stage 3: post-fusion additive (re-rank class).
                    rerank_additive[gid] = rerank_additive.get(gid, 0.0) + 0.5
            except Exception:
                log.debug("Party attribution bonus failed", exc_info=True)

        # ── Access-rate tiebreaker ────────────────────────────────
        # Small bonus: score += 0.05 * min(rate * 3600, 5). Max 0.25.
        # Only a tiebreaker — breaks ties for documents with equal scores.
        if gene_scores:
            try:
                rate_ids = list(gene_scores.keys())
                rate_ph = ",".join("?" * len(rate_ids))
                epi_rows = cur.execute(
                    f"SELECT gene_id, epigenetics FROM genes "
                    f"WHERE gene_id IN ({rate_ph}) AND epigenetics IS NOT NULL",
                    rate_ids,
                ).fetchall()
                for er in epi_rows:
                    try:
                        epi = parse_epigenetics(er["epigenetics"])
                        rate = epi.access_rate(3600.0)
                        if rate > 0:
                            bonus = 0.05 * min(rate * 3600.0, 5.0)
                            gid = er["gene_id"]
                            gene_scores[gid] += bonus
                            tier_contrib.setdefault(gid, {})["access_rate"] = bonus
                            # Stage 3: post-fusion additive (tiebreaker class).
                            rerank_additive[gid] = rerank_additive.get(gid, 0.0) + bonus
                    except Exception:
                        continue
            except Exception:
                log.debug("Access-rate tiebreaker failed", exc_info=True)

        # Layered fingerprints: inject parent-document aggregate scores when
        # ≥ 2 chunks of the same file surface in candidates. Opt-in via
        # HELIX_LAYERED_FINGERPRINTS=1. See docs/FUTURE/LAYERED_FINGERPRINTS.md.
        if os.environ.get("HELIX_LAYERED_FINGERPRINTS", "0") == "1":
            try:
                self._aggregate_parent_fingerprints(gene_scores, tier_contrib)
            except Exception:
                log.warning("parent fingerprint aggregation failed", exc_info=True)

        # Expose scores + per-tier breakdown for score-gated retrieval in
        # context_manager + the activation profiler bench. Both writes
        # need to be under the same lock — concurrent /context calls
        # would otherwise read a (scores_from_call_A, tiers_from_call_B)
        # torn pair.
        with self._last_query_scores_lock:
            self.last_query_scores = dict(gene_scores)
            self.last_tier_contributions = tier_contrib

        # Emit per-tier contribution telemetry (OTel — no-op when off).
        # One histogram observation per (tier, document) pair; a single
        # counter tick per tier that fired at all, labelled by tier
        # name. Makes the bench_skill_activation heatmap live-observable
        # instead of a one-shot static file.
        try:
            from .telemetry import tier_contribution_histogram, tier_fired_counter
            hist = tier_contribution_histogram()
            cnt = tier_fired_counter()
            tiers_seen: set = set()
            for contribs in tier_contrib.values():
                for tier, score in contribs.items():
                    hist.record(float(score), {"tier": tier})
                    tiers_seen.add(tier)
            for tier in tiers_seen:
                cnt.add(1, {"tier": tier})
        except Exception:
            # Promoted to warning so silent histogram failures surface.
            # The tier-activation + per-tier-contribution panels go dark
            # when this swallows; matching the cwola/latency/gauges pattern.
            log.warning("tier telemetry emit failed", exc_info=True)

        # ── BM25 shortlist post-filter (research review 2026-04-22) ──
        # When enabled, restrict the final ranking to documents that cleared a
        # BM25 top-N pass. All tiers still accumulated scores above; this
        # drops candidates BM25 would never surface before the sort. Tests
        # the hypothesis that tier-based scoring on BM25-invisible documents
        # is pulling wrong answers into the top-k. Post-filter by design —
        # isolates the ranking-set question from candidate-generation
        # latency work. Soft-fails to the unfiltered ranking on any error.
        if (
            getattr(self, "_bm25_shortlist_enabled", False)
            and not self._bm25_prefilter_enabled  # don't double-filter
            and self._fts_available
            and gene_scores
        ):
            try:
                bm25_terms = [t for t in query_terms if len(t) > 2]
                if bm25_terms:
                    bm25_match = " OR ".join(f'"{t}"' for t in bm25_terms)
                    shortlist_rows = cur.execute(
                        "SELECT gene_id FROM genes_fts "
                        "WHERE genes_fts MATCH ? ORDER BY rank LIMIT ?",
                        (bm25_match, self._bm25_shortlist_size),
                    ).fetchall()
                    shortlist = {r["gene_id"] for r in shortlist_rows}
                    # Empty shortlist (BM25 found nothing) → don't filter;
                    # the tier-only ranking is better than nothing.
                    if shortlist:
                        before = len(gene_scores)
                        gene_scores = {
                            g: s for g, s in gene_scores.items() if g in shortlist
                        }
                        tier_contrib = {
                            g: c for g, c in tier_contrib.items() if g in shortlist
                        }
                        log.debug(
                            "bm25 shortlist: scored=%d shortlist=%d kept=%d",
                            before, len(shortlist), len(gene_scores),
                        )
            except Exception:
                log.warning(
                    "bm25 shortlist filter failed — falling back to unfiltered ranking",
                    exc_info=True,
                )

        # ── Stage 3: branch on fusion_mode for the final ranking ──
        # Spec: docs/specs/2026-05-08-stage-3-rrf-fusion.md §5.
        # additive (default for one release): unchanged sort on the
        #   accumulated gene_scores.
        # rrf: take fused scores from the Fuser, add re-rank-class
        #   additives (sema_boost, authority, party_attr, access_rate)
        #   on top, restrict to documents that survived the bm25 shortlist
        #   filter (= present in gene_scores), then sort.
        if self._fusion_mode == "rrf":
            fused_scores = fuser.all_scores()
            # The bm25_shortlist filter mutated gene_scores above; honor
            # that filter under RRF too — only documents still in
            # gene_scores are eligible. Documents dense-recall surfaced but
            # bm25_shortlist dropped should not resurface in the fused
            # output.
            eligible_ids = set(gene_scores.keys())
            final_scores: Dict[str, float] = {}
            for gid in eligible_ids:
                final_scores[gid] = (
                    fused_scores.get(gid, 0.0)
                    + rerank_additive.get(gid, 0.0)
                )
            # Sort: primary key = final fused+additive score desc,
            # secondary = gene_id asc (matches Fuser's tie-break).
            ranked_ids = sorted(
                final_scores,
                key=lambda gid: (-final_scores[gid], gid),
            )[:limit]
            # last_query_scores semantics under RRF: the fused+additive
            # final score, NOT the raw additive accumulator. This is
            # what context_manager reads for ratio gates.
            with self._last_query_scores_lock:
                self.last_query_scores = dict(final_scores)
            # Optional new telemetry: rrf_fused_score_histogram (spec §6).
            try:
                from .telemetry import rrf_fused_score_histogram
                hist = rrf_fused_score_histogram()
                for gid, score in final_scores.items():
                    hist.record(float(score), {"gene_id": gid})
            except Exception:
                pass  # New histogram is optional — telemetry module may not declare it yet.
        else:
            # Additive mode — pre-Stage-3 behavior, byte-identical.
            ranked_ids = sorted(gene_scores, key=gene_scores.get, reverse=True)[:limit]

        # Walking tie-break (opt-in via HELIX_WALKING_TIEBREAK=1).
        # When adjacent top-k documents have bitwise-identical fused scores,
        # re-order them using associative-graph signals (neighborhood
        # size, direct edge weight, NLI entailment, freshness) instead
        # of dict insertion order. Overall score ordering is preserved —
        # only within-tie ordering changes. Soft-fails: any exception in
        # the tie-break path falls through to the original ranking.
        # See docs/FUTURE/tie_break_walking.md for the empirical basis.
        try:
            from .retrieval import tie_break
            if tie_break.is_enabled():
                ranked_ids = tie_break.walking_reorder(
                    self.conn, ranked_ids, gene_scores,
                )
        except Exception:
            log.warning("walking tie-break failed, using insertion-order default", exc_info=True)

        # ── Sprint 4: Hebbian evidence accumulation on seeded edges ───
        # Fire-and-forget update to harmonic_links so seeded / co_retrieved
        # rows accrue co_count (both endpoints in top-k) or miss_count
        # (one endpoint retrieved, other in candidate pool but below the
        # cut — weighted by dense-rank distance to cutoff). Candidacy
        # gate: documents outside gene_scores are ignored (topical-orthogonal
        # queries should not punish the edge). Soft-fails — logger
        # hiccups never perturb the retrieval result.
        if self._seeded_edges_enabled and ranked_ids and not read_only:
            try:
                from .retrieval.seeded_edges import update_edge_evidence
                update_edge_evidence(
                    self, gene_scores, ranked_ids, max_genes=max_genes,
                )
            except Exception:
                log.debug("Hebbian edge update failed", exc_info=True)

        # Batch fetch document rows
        id_placeholders = ",".join("?" * len(ranked_ids))
        rows = cur.execute(
            f"SELECT * FROM genes WHERE gene_id IN ({id_placeholders})",
            ranked_ids,
        ).fetchall()

        # Preserve ranked order
        row_map = {r["gene_id"]: r for r in rows}
        genes = [self._row_to_gene(row_map[gid]) for gid in ranked_ids if gid in row_map]

        # Co-activation pull-forward
        expanded = self._expand_coactivated(genes, limit=limit)

        # Dedupe while preserving order
        seen: set[str] = set()
        result: List[Gene] = []
        for g in expanded:
            if g.gene_id not in seen:
                seen.add(g.gene_id)
                result.append(g)

        return result[:limit]

    # ── BGE-M3 dense retrieval (Step 4, 2026-05-08; Stage 2 first-class) ─────

    def _get_dense_codec(self):
        """Lazy-load the BGE-M3 codec on first use.

        Stage 2 (2026-05-08): KnowledgeStore wires _dense_embedding_dim from config,
        which now defaults to 1024 (full BGE-M3 Matryoshka). The codec itself
        warns if the dim is not a sanctioned breakpoint.
        """
        if self._dense_codec is None:
            from .backends.bgem3_codec import BGEM3Codec
            self._dense_codec = BGEM3Codec(dim=self._dense_embedding_dim)
            # One-time threshold-staleness warn: if v2 coverage is non-empty
            # AND the threshold was calibrated at a different dim, surface it.
            # Threshold recalibration is Stage 4; we just want it loud.
            if self._dense_embedding_enabled and not self._threshold_dim_warned:
                try:
                    coverage = self._reader.execute(
                        "SELECT COUNT(*) AS c FROM genes WHERE embedding_dense_v2 IS NOT NULL"
                    ).fetchone() if self._reader is not None else None
                    has_v2 = bool(coverage and coverage["c"] > 0)
                except Exception:
                    has_v2 = False
                if has_v2:
                    log.warning(
                        "ann_similarity_threshold=%.3f is calibrated for dim=256; "
                        "v2 vectors are dim=%d. Threshold recalibration is Stage 4. "
                        "Recall pool is independent of threshold.",
                        self._ann_threshold, self._dense_embedding_dim,
                    )
                    self._threshold_dim_warned = True
        return self._dense_codec

    def _invalidate_dense_matrix(self, force: bool = False) -> None:
        """Drop the in-memory dense matrix so it rebuilds on next query.

        Called from upsert/delete paths after an ingest batch. Rebuild is full
        (not incremental) — at ~78 MiB and 18.9k rows this is sub-200 ms and
        triggers only on the first dense recall after an ingest batch.

        ``force=True`` is a no-op flag retained for API symmetry (spec §4) —
        the rebuild semantics are always full.
        """
        with self._dense_matrix_lock:
            self._dense_matrix = None
            self._dense_matrix_ids = None

    def _ensure_dense_matrix(self):
        """Build/load the hot-tier (lifecycle tier < 2) dense matrix.

        Returns ``(matrix, gene_ids)`` or ``(None, None)`` if the v2 column is
        empty. Hot-tier only — heterochromatin (lifecycle tier=2) is reachable via
        ``query_cold_tier()``. Stage 7 surfaces cold matches as MissBlocks.

        Raw BLOB layout: little-endian fp32, ``dim * 4`` bytes per row,
        zero-copy via ``np.frombuffer``.
        """
        with self._dense_matrix_lock:
            if self._dense_matrix is not None and self._dense_matrix_ids is not None:
                return self._dense_matrix, self._dense_matrix_ids

            try:
                import numpy as np
            except ImportError:
                log.debug("numpy unavailable; dense recall disabled")
                return None, None

            # Hot-tier scan. Partial index idx_genes_dense_v2_hot makes this
            # an index range scan rather than a full table scan during
            # partial-rollout / backfill.
            cur = self._reader.cursor() if self._reader is not None else self.conn.cursor()
            rows = cur.execute(
                "SELECT gene_id, embedding_dense_v2 FROM genes "
                "WHERE embedding_dense_v2 IS NOT NULL AND chromatin < ?",
                (int(ChromatinState.HETEROCHROMATIN),),
            ).fetchall()
            if not rows:
                return None, None

            dim = self._dense_embedding_dim
            expected_bytes = dim * 4  # fp32
            ids: list[str] = []
            vecs: list[np.ndarray] = []
            for r in rows:
                blob = r["embedding_dense_v2"]
                if blob is None or len(blob) != expected_bytes:
                    continue
                # Little-endian fp32, zero-copy view -> copy into accum array.
                vec = np.frombuffer(blob, dtype="<f4")
                if vec.shape[0] != dim:
                    continue
                ids.append(r["gene_id"])
                vecs.append(vec)
            if not vecs:
                return None, None
            matrix = np.stack(vecs).astype(np.float32, copy=False)
            self._dense_matrix = matrix
            self._dense_matrix_ids = ids
            log.debug(
                "dense matrix loaded: shape=%s dtype=%s ids=%d",
                matrix.shape, matrix.dtype, len(ids),
            )
            return matrix, ids

    def query_docs_dense_recall(
        self,
        query: str,
        *,
        k: int = 500,
        party_id: Optional[str] = None,
        read_only: bool = False,
    ) -> List[tuple[str, float]]:
        """Stage 2 first-class dense recall.

        Returns ``[(gene_id, cosine), ...]`` sorted descending. Hot-tier only.
        Does NOT load document bodies — that's deferred to ``query_genes_ann`` so
        we body-fetch once after the lex+dense union.

        Falls back to ``[]`` (with one-time warn) if v2 coverage is empty —
        callers degrade to lexical-only.

        ``party_id`` and ``read_only`` are accepted for API symmetry with
        ``query_genes_ann``; party-aware sharding is out of scope for Stage 2
        (spec §13 punts it to a later release).
        """
        del party_id, read_only  # reserved; spec §13 explicitly defers sharding
        if not self._dense_embedding_enabled:
            return []
        try:
            import numpy as np
        except ImportError:
            return []

        matrix, ids = self._ensure_dense_matrix()
        if matrix is None or ids is None:
            if not self._dense_v2_partial_warned:
                log.warning(
                    "embedding_dense_v2 coverage is empty; dense recall disabled. "
                    "Run scripts/backfill_bgem3_v2.py against this genome to populate."
                )
                self._dense_v2_partial_warned = True
            return []

        codec = self._get_dense_codec()
        query_vec = np.asarray(codec.encode(query, task="query"), dtype=np.float32)
        if query_vec.shape[0] != matrix.shape[1]:
            log.warning(
                "dense recall dim mismatch: query=%d matrix=%d; returning []",
                query_vec.shape[0], matrix.shape[1],
            )
            return []

        # All vectors are L2-normalized at encode/backfill time (codec
        # contract). matmul == cosine.
        sims = matrix @ query_vec
        n = sims.shape[0]
        k_eff = min(int(k), n)
        if k_eff <= 0:
            return []
        # argpartition is O(n); the ~k tail is then sorted.
        idx_part = np.argpartition(-sims, k_eff - 1)[:k_eff]
        # Sort the partition descending by similarity.
        idx_sorted = idx_part[np.argsort(-sims[idx_part])]
        return [(ids[int(i)], float(sims[int(i)])) for i in idx_sorted]

    def query_docs_ann(
        self,
        query: str,
        threshold: float | None = None,
        max_genes: int | None = None,
        min_genes: int | None = None,
        domains: list[str] | None = None,
        entities: list[str] | None = None,
        party_id: Optional[str] = None,
        use_harmonic: bool = True,
        use_sr: Optional[bool] = None,
        use_entity_graph: Optional[bool] = None,
        read_only: bool = False,
        *,
        pool_size: int | None = None,
    ) -> List[Gene]:
        """Stage 2 retrieval: parallel lex + dense recall, union, threshold cut.

        Refactor (spec §6):

        1. Resolve ``pool_size = pool_size or self._dense_pool_size``.
        2. Fan out:
           - lex pool via ``query_genes(..., max_genes=pool_size)``
           - dense pool via ``query_genes_dense_recall(query, k=pool_size)``
        3. Union by gene_id, preserving best score per source.
        4. Body-fetch once via ``_load_genes_by_ids``.
        5. Apply threshold + ``min_genes`` floor; cap at ``max_genes``.

        Stage 2 is **threshold-stale**: ``ann_similarity_threshold`` was
        calibrated at dim=256; v2 vectors are dim=1024. Stage 4 recalibrates.
        ``max_genes`` is the hard cap, so blast radius is bounded even if the
        threshold over-includes.

        Back-compat: callers that pass only positional/keyword args still
        work; ``pool_size`` is keyword-only and optional.
        """
        # Stage 4: when mode='margin_over_random', read the calibrated value
        # from genome_calibration; falls back to self._ann_threshold (legacy
        # absolute) on missing row. Caller-supplied ``threshold`` still wins.
        threshold = threshold if threshold is not None else self._get_effective_ann_threshold()
        max_genes = max_genes if max_genes is not None else self._ann_max_genes
        min_genes = min_genes if min_genes is not None else self._ann_min_genes
        pool_size = pool_size if pool_size is not None else self._dense_pool_size
        domains = domains or []
        entities = entities or []

        # ── 1. Lex recall pool (size = pool_size, NOT max_genes). ─────
        lex_pool = self.query_docs(
            domains,
            entities,
            max_genes=pool_size,
            party_id=party_id,
            use_harmonic=use_harmonic,
            use_sr=use_sr,
            use_entity_graph=use_entity_graph,
            read_only=read_only,
        )

        # Dense disabled -> degrade to legacy lex-only flow, capped at max_genes.
        if not self._dense_embedding_enabled:
            return lex_pool[:max_genes]

        # ── 2. Dense recall pool (id+score, no bodies). ──────────────
        dense_hits = self.query_docs_dense_recall(
            query, k=pool_size, party_id=party_id, read_only=read_only,
        )

        # ── 3. Union by gene_id. Lex documents get sim=threshold-0.01 unless
        # they also appeared in the dense pool. Dense-only ids get loaded
        # via _load_genes_by_ids so we body-fetch once.
        codec = self._get_dense_codec() if dense_hits else None
        query_vec = codec.encode(query, task="query") if codec is not None else None

        sim_by_id: dict[str, float] = {gid: float(s) for gid, s in dense_hits}
        ordered_ids: list[str] = [gid for gid, _ in dense_hits]
        seen: set[str] = set(ordered_ids)
        for doc in lex_pool:
            if doc.gene_id not in seen:
                seen.add(doc.gene_id)
                ordered_ids.append(doc.gene_id)
                # Lex-only id w/o dense score: pin slightly below threshold so
                # the min_genes floor can still rescue them. Spec §8 retains
                # this behavior; Stage 3 (RRF) replaces the placeholder.
                sim_by_id[doc.gene_id] = threshold - 0.01

        # Body-fetch missing ids in bulk. Lex pool already has bodies; only
        # dense-only ids need loading.
        lex_by_id = {d.gene_id: d for d in lex_pool}
        missing_ids = [gid for gid in ordered_ids if gid not in lex_by_id]
        loaded = self._load_genes_by_ids(missing_ids) if missing_ids else {}

        # ── 4. Resolve final Document objects in score order. ────────────
        scored: list[tuple[Gene, float]] = []
        for gid in ordered_ids:
            doc = lex_by_id.get(gid) or loaded.get(gid)
            if doc is None:
                continue
            scored.append((doc, sim_by_id[gid]))
        scored.sort(key=lambda x: x[1], reverse=True)

        # ── 5. Threshold cut + min_genes floor + max_genes cap. ──────
        result: list[Gene] = []
        for doc, sim in scored:
            if sim >= threshold or len(result) < min_genes:
                result.append(doc)
            else:
                break
        return result[:max_genes]

    def _load_genes_by_ids(self, gene_ids: list[str]) -> dict[str, Gene]:
        """Bulk-load Document objects by id. Used by query_genes_ann to fetch
        bodies for dense-only hits exactly once.
        """
        if not gene_ids:
            return {}
        ph = ",".join("?" * len(gene_ids))
        rows = self.read_conn.cursor().execute(
            f"SELECT * FROM genes WHERE gene_id IN ({ph})",
            gene_ids,
        ).fetchall()
        out: dict[str, Gene] = {}
        for r in rows:
            try:
                out[r["gene_id"]] = self._row_to_gene(r)
            except Exception:
                log.debug("row_to_gene failed for %s", r["gene_id"], exc_info=True)
        return out

    # ── Entity graph: auto-link documents sharing entities ───────────────

    def _auto_link_by_entity(self, gene_id: str, entities: List[str], cur) -> None:
        """Delegate to storage.co_activation.auto_link_by_entity."""
        from .storage.co_activation import auto_link_by_entity
        auto_link_by_entity(gene_id, entities, cur)

    def _expand_by_entity_graph(
        self, gene_ids: List[str], limit: int, cur
    ) -> List[str]:
        """Delegate to storage.co_activation.expand_by_entity_graph."""
        from .storage.co_activation import expand_by_entity_graph
        return expand_by_entity_graph(gene_ids, limit, cur)

    # ── Co-activation expansion ─────────────────────────────────────

    def _expand_coactivated(self, genes: List[Gene], limit: int) -> List[Gene]:
        from .storage.co_activation import expand_coactivated
        return expand_coactivated(
            genes, limit, self.conn, self._entity_graph_enabled,
        )

    # ── Row → Document ──────────────────────────────────────────────────

    def _row_to_gene(self, row: sqlite3.Row) -> Gene:
        def _opt(key: str, default=None):
            try:
                return row[key]
            except (IndexError, KeyError):
                return default

        # Guard against NULL/corrupt metadata fields
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

        # key_values may not exist in older databases before ALTER TABLE runs
        try:
            kv_raw = row["key_values"]
            key_values = json_loads(kv_raw) if kv_raw else []
        except (IndexError, KeyError):
            key_values = []

        return Gene(
            gene_id=row["gene_id"],
            content=row["content"] or "",
            # Heterochromatin-compressed documents have complement=NULL after
            # compress_to_heterochromatin(). Fall back to "" for Pydantic.
            complement=row["complement"] or "",
            codons=json_loads(row["codons"]) if row["codons"] else [],
            promoter=promoter,
            epigenetics=epigenetics,
            chromatin=chromatin,
            is_fragment=bool(row["is_fragment"]) if row["is_fragment"] is not None else False,
            embedding=json_loads(row["embedding"]) if row["embedding"] else None,
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

    # ── Touch (update signals on access) ────────────────────────

    def touch_genes(self, gene_ids: List[str]) -> None:
        if self.read_only:
            log.debug("read_only: skipping touch_genes")
            return
        if not gene_ids:
            return

        cur = self.conn.cursor()
        now = time.time()

        # Batch fetch all signals in one query
        placeholders = ",".join("?" * len(gene_ids))
        rows = cur.execute(
            f"SELECT gene_id, epigenetics FROM genes WHERE gene_id IN ({placeholders})",
            gene_ids,
        ).fetchall()

        # Individual UPDATEs — safe against column-swap corruption
        # (CASE WHEN batch was causing signals JSON to land in lifecycle tier)
        for row in rows:
            if not row["epigenetics"]:
                continue
            epi = parse_epigenetics(row["epigenetics"], use_cache=False)
            epi.last_accessed = now
            epi.access_count += 1
            epi.decay_score = min(1.0, epi.decay_score + 0.1)
            # Phase 1 slice 2 — populate the windowed access-rate buffer.
            # Bounded ring buffer of last 100 timestamps (~800 bytes).
            # The slice 1 schema added the field; this is where it gets
            # written. apply_density_gate consumes it via the rate signal.
            epi.recent_accesses.append(now)
            if len(epi.recent_accesses) > 100:
                epi.recent_accesses = epi.recent_accesses[-100:]
            cur.execute(
                "UPDATE genes SET epigenetics = ?, chromatin = ? WHERE gene_id = ?",
                (epi.model_dump_json(), int(ChromatinState.OPEN), row["gene_id"]),
            )

        self.conn.commit()
        clear_parse_caches()

    # ── Update co-activation links (mutual) ─────────────────────────

    def link_coactivated(self, gene_ids: List[str]) -> None:
        """Create mutual co-activation links between all retrieved documents."""
        if self.read_only:
            log.debug("read_only: skipping link_coactivated")
            return
        if len(gene_ids) < 2:
            return

        cur = self.conn.cursor()

        # Batch fetch all signals in one query
        placeholders = ",".join("?" * len(gene_ids))
        rows = cur.execute(
            f"SELECT gene_id, epigenetics FROM genes WHERE gene_id IN ({placeholders})",
            gene_ids,
        ).fetchall()

        # Build individual updates (signals only, preserve lifecycle tier)
        for row in rows:
            if not row["epigenetics"]:
                continue
            epi = parse_epigenetics(row["epigenetics"], use_cache=False)
            gid = row["gene_id"]
            peers = [other for other in gene_ids if other != gid]

            existing = set(epi.co_activated_with)
            existing.update(peers)
            epi.co_activated_with = list(existing)[:10]

            cur.execute(
                "UPDATE genes SET epigenetics = ? WHERE gene_id = ?",
                (epi.model_dump_json(), gid),
            )

        self.conn.commit()
        clear_parse_caches()

    # ── Harmonic weights (cymatics) ──────────────────────────────────

    def store_harmonic_weights(self, weights: List[Tuple[str, str, float]]) -> None:
        """Delegate to storage.co_activation.store_harmonic_weights."""
        if self.read_only:
            log.debug("read_only: skipping store_harmonic_weights")
            return
        from .storage.co_activation import store_harmonic_weights
        store_harmonic_weights(self.conn, weights)

    # ── Typed document relations (NLI) ───────────────────────────────────

    def store_relation(
        self, gene_id_a: str, gene_id_b: str,
        relation: int, confidence: float,
    ) -> None:
        """Delegate to storage.co_activation.store_relation."""
        from .storage.co_activation import store_relation
        store_relation(self.conn, gene_id_a, gene_id_b, relation, confidence)

    def store_relations_batch(
        self, relations: list,
    ) -> None:
        """Delegate to storage.co_activation.store_relations_batch."""
        from .storage.co_activation import store_relations_batch
        store_relations_batch(self.conn, relations)

    def get_relations(self, gene_id: str) -> list:
        """Delegate to storage.co_activation.get_relations."""
        from .storage.co_activation import get_relations
        return get_relations(self.conn, gene_id)

    # ── Layered fingerprints: query-time parent aggregation ──────────

    def _aggregate_parent_fingerprints(
        self,
        gene_scores: Dict[str, float],
        tier_contrib: Dict[str, Dict[str, float]],
    ) -> None:
        """Inject parent-document aggregate scores into gene_scores + tier_contrib
        when ≥ 2 chunks of the same file hit current candidates.

        Mutates the two dicts in place. Called only when
        HELIX_LAYERED_FINGERPRINTS=1. Errors are swallowed by the caller.

        Rules:
          - Candidates with a CHUNK_OF edge to parent P that has N≥2
            distinct children in gene_scores: parent P gets an
            aggregated score = sum(child_scores) * (1 + 0.1 * log1p(N)).
          - Parent tier_contributions = per-tier sum of child contributions.
          - If parent already in gene_scores (because it fired via its
            own content matching), add the aggregated bonus on top
            instead of replacing.
        """
        from math import log1p
        from .schemas import StructuralRelation as _SR

        if not gene_scores:
            return

        cand_ids = list(gene_scores.keys())
        # Batched edge lookup. SQLite IN clause capped at ~999 params;
        # chunk candidates if larger. Normal top-k is < 100 so a single
        # query is fine in practice.
        placeholders = ",".join("?" * len(cand_ids))
        rows = self.conn.execute(
            f"SELECT gene_id_a AS child, gene_id_b AS parent "
            f"FROM gene_relations "
            f"WHERE gene_id_a IN ({placeholders}) AND relation = ?",
            (*cand_ids, int(_SR.CHUNK_OF)),
        ).fetchall()
        if not rows:
            return

        # parent -> set of children hit
        from collections import defaultdict
        parent_children: Dict[str, set] = defaultdict(set)
        for r in rows:
            parent_children[r["parent"]].add(r["child"])

        for parent_gid, children in parent_children.items():
            n_hits = len(children)
            if n_hits < 2:
                continue  # co-activation requires ≥ 2 children

            child_score_sum = sum(gene_scores.get(c, 0.0) for c in children)
            co_activation_bonus = 1.0 + 0.1 * log1p(n_hits)
            aggregated = child_score_sum * co_activation_bonus

            if parent_gid in gene_scores:
                # Parent fired on its own; add aggregated bonus instead of replacing.
                gene_scores[parent_gid] += aggregated
            else:
                gene_scores[parent_gid] = aggregated

            # Aggregate per-tier contributions from children.
            agg_tiers: Dict[str, float] = {}
            for child_gid in children:
                for tier, val in tier_contrib.get(child_gid, {}).items():
                    agg_tiers[tier] = agg_tiers.get(tier, 0.0) + val
            agg_tiers["parent_coactivation"] = round(aggregated, 3)
            agg_tiers["chunks_hit"] = n_hits

            existing = tier_contrib.get(parent_gid, {})
            for tier, val in agg_tiers.items():
                existing[tier] = existing.get(tier, 0.0) + val if tier not in ("chunks_hit",) else val
            tier_contrib[parent_gid] = existing

    # ── Layered fingerprints: parent-pull reassembly ──────────────────

    def reassemble(self, parent_gene_id: str, separator: str = "\n\n") -> dict:
        """Reassemble a file-level parent document into its full content.

        Reads the parent's ``codons`` field (ordered list of child gene_ids),
        fetches each child's content from the documents table, sorts by
        ``promoter.sequence_index`` for deterministic ordering, and joins
        with the given separator.

        See docs/FUTURE/LAYERED_FINGERPRINTS.md for the design.

        Returns:
            {
                "content":          <stitched text>,
                "source_id":        <original file path>,
                "chunk_count":      <N>,
                "reassembled_from": [<child gene_ids in sequence order>],
                "missing_children": [<child gene_ids that no longer exist>],
            }

        Raises:
            ValueError: gene_id does not exist, or is not a parent document.
        """
        from .schemas import StructuralRelation as _SR  # local import, cycle-safe

        cur = self.conn.cursor()
        row = cur.execute(
            "SELECT gene_id, content, codons, source_id, key_values "
            "FROM genes WHERE gene_id = ?",
            (parent_gene_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"gene_id {parent_gene_id!r} not found")

        # Parent detection: key_values contains "is_parent=true"
        kv_raw = row["key_values"] or "[]"
        try:
            kv = json_loads(kv_raw) if isinstance(kv_raw, str) else kv_raw
        except Exception:
            kv = []
        is_parent = any(
            isinstance(k, str) and k.lower() == "is_parent=true" for k in kv
        )
        if not is_parent:
            raise ValueError(
                f"gene_id {parent_gene_id!r} is not a parent gene "
                f"(missing is_parent=true marker in key_values)"
            )

        codons_raw = row["codons"] or "[]"
        try:
            child_ids = json_loads(codons_raw) if isinstance(codons_raw, str) else codons_raw
        except Exception:
            child_ids = []
        if not child_ids:
            raise ValueError(f"parent {parent_gene_id!r} has empty codons list")

        # Batched fetch of children.
        placeholders = ",".join("?" * len(child_ids))
        child_rows = cur.execute(
            f"SELECT gene_id, content, promoter FROM genes "
            f"WHERE gene_id IN ({placeholders})",
            tuple(child_ids),
        ).fetchall()

        found = {r["gene_id"]: r for r in child_rows}
        missing = [cid for cid in child_ids if cid not in found]
        if missing:
            log.warning(
                "reassemble(%s): %d missing children — stitched output will skip them",
                parent_gene_id, len(missing),
            )

        def _seq_index(r) -> int:
            try:
                p = json_loads(r["promoter"]) if r["promoter"] else {}
            except Exception:
                p = {}
            idx = p.get("sequence_index") if isinstance(p, dict) else None
            return idx if isinstance(idx, int) else 0

        ordered = sorted(
            (found[cid] for cid in child_ids if cid in found),
            key=_seq_index,
        )

        stitched = separator.join(r["content"] or "" for r in ordered)
        return {
            "content": stitched,
            "source_id": row["source_id"],
            "chunk_count": len(child_ids),
            "reassembled_from": [r["gene_id"] for r in ordered],
            "missing_children": missing,
        }

    # ── Compaction (decay stale documents) ──────────────────────────────

    def compact(self) -> int:
        """
        Check documents for source file changes. No time-based decay.

        Documents are NEVER removed by time alone. Knowledge doesn't expire.
        Only two things change a document's state:

        1. SOURCE CHANGED: if gene.source_id points to a file whose mtime
           is newer than last_accessed, decay_score drops to 0.5 (AGING)
           and lifecycle tier moves to EUCHROMATIN. The document is still queryable
           but the system knows it's outdated. Re-ingesting resets it.

        2. EXPLICIT SPLICE: the compressor's splice operation cuts introns
           per-query (irrelevant fragments). This is the RNA splicing analog —
           relevance filtering happens at retrieval time, not storage time.

        Time since last access is used ONLY for retrieval priority
        (recently accessed documents rank higher in query results), never
        for deletion or decay.

        Returns the number of documents marked as source-changed.
        """
        cur = self.conn.cursor()
        change_detected = 0

        rows = cur.execute(
            "SELECT gene_id, epigenetics, chromatin, source_id FROM genes"
        ).fetchall()

        for row in rows:
            source_id = row["source_id"]
            if not source_id or not os.path.exists(source_id):
                continue

            try:
                file_mtime = os.path.getmtime(source_id)
            except OSError:
                continue

            epi = parse_epigenetics(row["epigenetics"], use_cache=False)

            if file_mtime > epi.last_accessed:
                # Source changed — document is outdated but NOT removed
                epi.decay_score = min(epi.decay_score, 0.5)
                new_chromatin = int(ChromatinState.EUCHROMATIN)
                change_detected += 1

                cur.execute(
                    "UPDATE genes SET epigenetics = ?, chromatin = ? WHERE gene_id = ?",
                    (epi.model_dump_json(), new_chromatin, row["gene_id"]),
                )

        self.conn.commit()
        if change_detected:
            log.info("Compaction: %d source changes detected (genes marked EUCHROMATIN)",
                     change_detected)
        return change_detected

    # ── Stats ───────────────────────────────────────────────────────

    def stats(self) -> Dict:
        self._refresh_snapshot()  # See latest WAL state
        cur = self.conn.cursor()  # Always master — stats must be authoritative

        total = cur.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
        by_chromatin = cur.execute(
            "SELECT chromatin, COUNT(*) FROM genes GROUP BY chromatin"
        ).fetchall()

        chromatin_counts = {}
        for r in by_chromatin:
            try:
                key = ChromatinState(int(r[0])).name if r[0] is not None else "UNKNOWN"
            except (ValueError, TypeError):
                key = "UNKNOWN"
            chromatin_counts[key] = chromatin_counts.get(key, 0) + r[1]

        total_raw = cur.execute(
            "SELECT COALESCE(SUM(LENGTH(content)), 0) FROM genes"
        ).fetchone()[0]
        total_compressed = cur.execute(
            "SELECT COALESCE(SUM(LENGTH(complement)), 0) FROM genes"
        ).fetchone()[0]

        # Compression tier distribution
        tier_counts = {0: 0, 1: 0, 2: 0}
        try:
            by_tier = cur.execute(
                "SELECT COALESCE(compression_tier, 0), COUNT(*) FROM genes "
                "GROUP BY COALESCE(compression_tier, 0)"
            ).fetchall()
            for r in by_tier:
                tier_counts[int(r[0])] = r[1]
        except Exception:
            pass  # Column may not exist yet on old schemas

        return {
            "total_genes": total,
            "open": chromatin_counts.get("OPEN", 0),
            "euchromatin": chromatin_counts.get("EUCHROMATIN", 0),
            "heterochromatin": chromatin_counts.get("HETEROCHROMATIN", 0),
            "total_chars_raw": total_raw,
            "total_chars_compressed": total_compressed,
            "compression_ratio": total_raw / max(total_compressed, 1),
            "compression_tiers": {
                "open_full": tier_counts[0],
                "euchromatin_summary": tier_counts[1],
                "heterochromatin_cold": tier_counts[2],
            },
        }

    # ── Get single document ─────────────────────────────────────────────

    def get_doc(self, gene_id: str) -> Optional[Gene]:
        row = self.conn.execute(
            "SELECT * FROM genes WHERE gene_id = ?", (gene_id,)
        ).fetchone()
        return self._row_to_gene(row) if row else None

    # ── Citation lookup (polymorphic with ShardedGenomeAdapter) ─────────

    def get_citation_rows(self, gene_ids: List[str]) -> Dict[str, Dict]:
        """Return source_id + parsed promoter tags for a batch of gene_ids.

        Used by /context and packet-builder citation construction. Mirrors
        ``ShardedGenomeAdapter.get_citation_rows`` so callers don't need to
        branch on adapter type. Missing ids are simply absent from the
        return map.

        Return shape (per gene_id):
            ``{"source_id": str, "domains": list[str], "entities": list[str]}``
        """
        if not gene_ids:
            return {}
        ph = ",".join("?" * len(gene_ids))
        rows = self.read_conn.cursor().execute(
            f"SELECT gene_id, source_id, promoter FROM genes "
            f"WHERE gene_id IN ({ph})",
            gene_ids,
        ).fetchall()
        out: Dict[str, Dict] = {}
        for r in rows:
            domains: List[str] = []
            entities: List[str] = []
            if r["promoter"]:
                try:
                    prom = parse_promoter(r["promoter"])
                    if prom is not None:
                        domains = list(prom.domains)
                        entities = list(prom.entities)
                except Exception:
                    pass
            out[r["gene_id"]] = {
                "source_id": r["source_id"] or "",
                "domains": domains,
                "entities": entities,
            }
        return out

    # ── Health logging ─────────────────────────────────────────────

    def log_health(
        self,
        query: str,
        ellipticity: float,
        coverage: float,
        density: float,
        freshness: float,
        genes_expressed: int,
        genes_available: int,
        status: str,
    ) -> None:
        """Record a health signal for historical tracking."""
        if self.read_only:
            log.debug("read_only: skipping log_health")
            return
        self.conn.execute(
            "INSERT INTO health_log (timestamp, query, ellipticity, coverage, "
            "density, freshness, genes_expressed, genes_available, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), query, ellipticity, coverage, density, freshness,
             genes_expressed, genes_available, status),
        )
        self.conn.commit()

    def health_history(self, limit: int = 50) -> List[Dict]:
        """Return recent health signals, newest first."""
        rows = self.conn.execute(
            "SELECT timestamp, query, ellipticity, coverage, density, freshness, "
            "genes_expressed, genes_available, status "
            "FROM health_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "timestamp": r[0],
                "query": r[1],
                "ellipticity": r[2],
                "coverage": r[3],
                "density": r[4],
                "freshness": r[5],
                "genes_expressed": r[6],
                "genes_available": r[7],
                "status": r[8],
            }
            for r in rows
        ]

    def health_summary(self) -> Dict:
        """Aggregate health stats across all logged queries."""
        cur = self.conn.cursor()
        total = cur.execute("SELECT COUNT(*) FROM health_log").fetchone()[0]
        if total == 0:
            return {"total_queries": 0, "avg_ellipticity": 0, "status_counts": {}}

        avg = cur.execute(
            "SELECT AVG(ellipticity), AVG(coverage), AVG(density), AVG(freshness) "
            "FROM health_log"
        ).fetchone()

        status_counts = {}
        for row in cur.execute(
            "SELECT status, COUNT(*) FROM health_log GROUP BY status"
        ).fetchall():
            status_counts[row[0]] = row[1]

        return {
            "total_queries": total,
            "avg_ellipticity": round(avg[0], 4),
            "avg_coverage": round(avg[1], 4),
            "avg_density": round(avg[2], 4),
            "avg_freshness": round(avg[3], 4),
            "status_counts": status_counts,
        }

    # ── FTS5 index rebuild ────────────────────────────────────────────

    def rebuild_fts(self) -> int:
        """Rebuild the FTS5 index from all documents. Returns count indexed.

        Includes source_id + tags in the searchable content so
        tag-based knowledge survives rebuilds. At 100K+ documents this takes
        several seconds — prefer incremental sync for normal operation.
        """
        if not self._fts_available:
            log.warning("FTS5 not available — cannot rebuild")
            return 0

        import time as _time
        t0 = _time.time()
        cur = self.conn.cursor()

        # Clear and repopulate with enriched content
        cur.execute("DELETE FROM genes_fts")
        cur.execute(
            "INSERT INTO genes_fts(gene_id, content, complement) "
            "SELECT g.gene_id, "
            "  COALESCE(g.source_id,'') || ' ' || "
            "  COALESCE((SELECT GROUP_CONCAT(pi.tag_value, ' ') "
            "    FROM promoter_index pi WHERE pi.gene_id = g.gene_id), '') "
            "  || ' ' || g.content, "
            "  COALESCE(g.complement, '') "
            "FROM genes g"
        )
        self.conn.commit()
        count = cur.execute("SELECT COUNT(*) FROM genes_fts").fetchone()[0]
        elapsed = _time.time() - t0
        log.info("FTS5 index rebuilt: %d genes indexed in %.1fs", count, elapsed)
        return count

    # ── WAL refresh ────────────────────────────────────────────────

    def refresh(self) -> None:
        """Refresh WAL snapshot to see changes from external writers.

        Primary mechanism: commit() releases the implicit read transaction,
        forcing the next SELECT to start a new snapshot. This is the
        lightweight Tier 1 fix — no connection churn.

        Fallback: if the connection is in a bad state, close and reopen.
        """
        try:
            self._refresh_snapshot()
            # Verify the connection is healthy
            self.conn.execute("SELECT 1").fetchone()
        except Exception:
            log.warning("Snapshot refresh failed, reopening connection", exc_info=True)
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=30000")
            self.conn.execute("PRAGMA journal_size_limit=67108864")  # 64 MB

    # ── Close ───────────────────────────────────────────────────────

    def checkpoint(self, mode: str = "PASSIVE") -> None:
        """
        Force a WAL checkpoint to flush data from WAL to main database.

        Modes:
            PASSIVE  — non-blocking, skips frames held by readers (~5ms)
            FULL     — blocks until all frames are checkpointed
            TRUNCATE — like FULL, then truncates WAL file to zero bytes

        Call periodically during bulk ingest to prevent data loss on crash.
        Recommended cadence: PASSIVE every 50 documents, TRUNCATE every 500.
        """
        mode = mode.upper()
        if mode not in ("PASSIVE", "FULL", "RESTART", "TRUNCATE"):
            mode = "PASSIVE"
        try:
            # Best-effort: release the reader's WAL snapshot before checkpointing
            # so a TRUNCATE can actually advance. Safe no-op when the reader is
            # already in autocommit mode (isolation_level=None).
            if self._reader is not None:
                try:
                    self._reader.commit()
                except Exception:
                    pass
            row = self.conn.execute(
                f"PRAGMA wal_checkpoint({mode})"
            ).fetchone()
            # Returns (busy, log_pages, checkpointed_pages). busy=1 means a
            # reader was holding a snapshot and the checkpoint was incomplete.
            if row and row[0]:
                log.warning(
                    "WAL checkpoint (%s) blocked by reader: %d/%d pages flushed",
                    mode, row[2] or 0, row[1] or 0,
                )
                try:
                    from .telemetry import genome_checkpoint_blocked_counter
                    genome_checkpoint_blocked_counter().add(
                        1, {"mode": mode}
                    )
                except Exception:
                    pass
            else:
                log.debug(
                    "WAL checkpoint (%s) completed: %d pages flushed",
                    mode, (row[2] if row else 0) or 0,
                )
        except Exception:
            log.warning("WAL checkpoint (%s) failed", mode, exc_info=True)

    def emit_wal_health_gauges(self) -> None:
        """Emit helix_genome_wal_size_bytes gauge for the current knowledge store WAL.

        Intended to be called from a background task every ~30 s. No-op for
        in-memory knowledge stores and when OTel is disabled (noop instruments drop the
        call silently). Never raises — failures are logged at DEBUG level.
        """
        if self.path == ":memory:":
            return
        try:
            wal_path = self.path + "-wal"
            wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
            from .telemetry import genome_wal_size_gauge
            genome_wal_size_gauge().set(wal_size)
        except Exception:
            log.debug("emit_wal_health_gauges failed", exc_info=True)

    def vacuum(self) -> Dict[str, int]:
        """
        Reclaim free pages from the knowledge store database.

        After large-scale operations (thinning, compaction, source-change
        repair) SQLite holds deleted pages until a VACUUM releases them.
        For a heavily-thinned knowledge store this can be 30-50% of the file size.

        This method:
          1. Checkpoints the WAL so all data is in the main DB file
          2. Closes the long-lived connection (VACUUM needs exclusive access)
          3. Runs VACUUM via a dedicated connection
          4. Reopens the long-lived connection

        Returns:
            dict with before/after sizes in bytes, and bytes reclaimed.

        Warning: VACUUM is a full-file rewrite — it temporarily doubles disk
        usage and blocks all writers. Run during maintenance windows.
        """
        import os
        path = self.path
        before = os.path.getsize(path) if os.path.exists(path) else 0

        # Flush WAL and close the main connection
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.conn.commit()
        except Exception:
            log.warning("Pre-VACUUM WAL checkpoint failed", exc_info=True)
        try:
            self.conn.close()
        except Exception:
            pass

        # Run VACUUM on a fresh, autocommit connection
        try:
            vac_conn = sqlite3.connect(path)
            vac_conn.isolation_level = None  # autocommit — VACUUM requires it
            vac_conn.execute("VACUUM")
            vac_conn.close()
            log.info("VACUUM completed on %s", path)
        except Exception:
            log.warning("VACUUM failed", exc_info=True)

        # Reopen the long-lived connection
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA journal_size_limit=67108864")  # 64 MB

        after = os.path.getsize(path) if os.path.exists(path) else 0
        reclaimed = before - after

        return {
            "before_bytes": before,
            "after_bytes": after,
            "reclaimed_bytes": reclaimed,
            "reclaimed_pct": round(reclaimed / max(before, 1) * 100, 1),
        }

    # ── Cold-storage compression (ΣĒMA-based lifecycle tier tiers) ──────

    TIER_OPEN = 0           # Full fidelity — hot retrieval pool
    TIER_EUCHROMATIN = 1    # Summary + ΣĒMA — warm, reduced storage
    TIER_HETEROCHROMATIN = 2  # ΣĒMA + metadata only — cold, ~90% smaller

    def compute_density_score(self, gene: Gene) -> float:
        """
        Information density score for a document. Higher = more valuable.

        Combines:
          - Entity/domain tag count (tags richness)
          - Key-value extraction count (factual density)
          - Content length efficiency (short + rich > long + sparse)
          - Access count (usage signal from signals)

        Uses a content-length floor (100 chars) in the denominator to
        prevent tiny-content documents from producing nonsensical tag-density
        scores of 20+. See _DENSITY_CONTENT_LENGTH_FLOOR above.
        """
        tag_count = len(gene.promoter.domains) + len(gene.promoter.entities)
        kv_count = len(gene.key_values) if gene.key_values else 0
        # Floor the content length so a 30-char document with 5 tags doesn't
        # produce tag_density = 166 and break all downstream thresholds
        effective_len = max(len(gene.content), _DENSITY_CONTENT_LENGTH_FLOOR)

        # Normalize: tags per KB of (effective) content
        tag_density = tag_count / (effective_len / 1000.0)
        kv_density = kv_count / (effective_len / 1000.0)
        access = gene.epigenetics.access_count

        # Weighted combination (tag density dominates)
        score = (
            tag_density * 0.4
            + kv_density * 0.3
            + min(access / 10.0, 1.0) * 0.2
            + (1.0 if gene.complement and len(gene.complement) > 50 else 0.0) * 0.1
        )
        return score

    def apply_density_gate(self, gene: Gene) -> tuple[ChromatinState, str]:
        """
        Decide the lifecycle tier for a document at ingest time.

        Returns (chromatin_state, reason) — reason is one of:
            "deny_list"           : source path matches structural deny list
            "low_score_hetero"    : score below heterochromatin threshold
            "low_score_euchro"    : score below euchromatin threshold
            "access_rate_override": active in the windowed-rate sense
                                    (Phase 1 slice 2 — preferred when the
                                    document's recent_accesses buffer has data)
            "access_override"     : accessed >= _DENSITY_ACCESS_OVERRIDE
                                    times monotonically (legacy fallback for
                                    documents whose rate buffer is empty)
            "open"                : high score or unknown source, keep OPEN

        Never raises. Never touches the database. Pure decision function.

        The gate has three stages:
          1. Path deny list (fast-reject for structural noise)
          2. Access override (never demote frequently-used documents)
             2a. Windowed access-rate (preferred — sharper signal)
             2b. Monotonic access-count (legacy fallback for empty buffers)
          3. Score-based demotion (tag + KV density with recalibrated thresholds)

        Stages 2a and 2b run BEFORE the score check so that a document
        that's been touched multiple times can't be killed by a batch
        compact_genome sweep just because its static content is sparse.

        Stage 2a is a strict improvement on Stage 2b: it distinguishes
        "actively used right now" from "popular at some point in the
        past." Stage 2b remains as the fallback because all documents that
        pre-date the slice 1 schema change have empty recent_accesses
        buffers and need the monotonic counter to remain a valid signal
        until they're touched again under the slice 2 wiring.
        """
        # Stage 1: structural deny list
        if is_denied_source(gene.source_id):
            return ChromatinState.HETEROCHROMATIN, "deny_list"

        # Stage 2a: windowed access-rate override (preferred)
        # Empty buffer → access_rate returns 0.0 → falls through to 2b cleanly.
        if gene.epigenetics:
            rate_threshold = _DENSITY_RATE_MIN_HITS / _DENSITY_RATE_WINDOW
            if gene.epigenetics.access_rate(_DENSITY_RATE_WINDOW) >= rate_threshold:
                return ChromatinState.OPEN, "access_rate_override"

        # Stage 2b: monotonic access-count override (legacy fallback)
        access = gene.epigenetics.access_count if gene.epigenetics else 0
        if access >= _DENSITY_ACCESS_OVERRIDE:
            return ChromatinState.OPEN, "access_override"

        # Stage 3: score-based demotion
        score = self.compute_density_score(gene)
        if score < _DENSITY_HETEROCHROMATIN_THRESHOLD:
            return ChromatinState.HETEROCHROMATIN, "low_score_hetero"
        if score < _DENSITY_EUCHROMATIN_THRESHOLD:
            return ChromatinState.EUCHROMATIN, "low_score_euchro"
        return ChromatinState.OPEN, "open"

    def compress_to_euchromatin(self, gene_id: str) -> bool:
        """
        Compress a document to EUCHROMATIN tier: drop raw content, keep summary.

        Keeps: complement, fragments, tags, signals, embedding, key_values
        Drops: content (replaced with pointer to source_id for unwinding)
        """
        cur = self.conn.cursor()
        row = cur.execute(
            "SELECT source_id, complement FROM genes WHERE gene_id = ?",
            (gene_id,),
        ).fetchone()
        if not row or not row["complement"]:
            return False

        cur.execute(
            "UPDATE genes SET content = ?, compression_tier = 1 WHERE gene_id = ?",
            (f"[COMPRESSED:euchromatin] source={row['source_id'] or 'unknown'}", gene_id),
        )
        self.conn.commit()
        log.debug("Compressed gene %s to EUCHROMATIN", gene_id)
        return True

    def compress_to_heterochromatin(self, gene_id: str) -> bool:
        """
        Move a document to HETEROCHROMATIN tier (cold storage).

        As of 2026-04-10 (C.1 of B→C), this is **non-destructive**. The
        function only flips the ``chromatin`` and ``compression_tier``
        flags — it does NOT drop ``content``, ``complement``, ``codons``,
        SPLADE terms, or FTS5 index entries.

        Rationale: the lifecycle tier flag + ``WHERE chromatin < HETEROCHROMATIN``
        filter on all hot-tier retrieval paths already excludes demoted
        documents from normal ``/context`` queries. Destroying the underlying
        content eliminated any possibility of cold-tier retrieval (via
        ΣĒMA cosine similarity on the retained embedding) actually
        returning useful data — you'd match the embedding but have
        nothing to show.

        With content preserved, the cold-tier retrieval path added in C.2
        can reactivate demoted documents on-demand for queries that specifically
        need them. The storage cost is modest — SPLADE terms and FTS5
        index entries are small per-document — and the optional nature of
        cold-tier retrieval means hot queries are unaffected.

        Callers who explicitly want to reclaim disk space on a known-dead
        document can call ``delete_gene()`` instead. Heterochromatin is now
        strictly a **tier flag**, not a destructive compression.

        Keeps: everything (content, complement, fragments, SPLADE, FTS5)
        Flips: ``chromatin = 2``, ``compression_tier = 2``
        """
        cur = self.conn.cursor()
        row = cur.execute(
            "SELECT source_id FROM genes WHERE gene_id = ?",
            (gene_id,),
        ).fetchone()
        if not row:
            return False

        cur.execute(
            "UPDATE genes SET chromatin = 2, compression_tier = 2 "
            "WHERE gene_id = ?",
            (gene_id,),
        )
        self.conn.commit()
        # Document moved hot → cold — both tier caches are now stale
        if self._sema_cache is not None:
            self._sema_cache = None
        if self._cold_sema_cache is not None:
            self._cold_sema_cache = None
        # Stage 2: hot/cold transitions change the dense matrix membership too.
        self._invalidate_dense_matrix()
        log.debug("Moved gene %s to HETEROCHROMATIN (non-destructive)", gene_id)
        return True

    def compact_genome(self, dry_run: bool = False) -> Dict:
        """
        Run a compaction sweep: apply the density gate to every currently-OPEN
        document and demote those that fail it.

        Shares gate logic with ingest-time `apply_density_gate()`, so a document
        that would be demoted by a fresh ingest will also be demoted by a
        retroactive sweep. The three stages are the same:
          1. Structural deny list (Steam, build artifacts, lockfiles, etc.)
          2. Access-count override (access_count >= 5 keeps document OPEN)
          3. Score-based thresholds (< 0.5 hetero, < 1.0 euchro, else open)

        Only operates on documents currently at compression_tier = 0 (OPEN).
        Already-demoted documents are left alone.

        When ``dry_run=True``, returns the same stats without writing to
        the DB. Useful for previewing the impact before running the sweep
        against a live knowledge store.

        Returns a dict with:
            scanned               : number of OPEN documents examined
            to_heterochromatin    : count that would go to HETEROCHROMATIN
            to_euchromatin        : count that would go to EUCHROMATIN
            kept_open             : count that would stay OPEN
            skipped_no_embedding  : count skipped because no ΣĒMA vector exists
            by_reason             : dict of reason counts (deny_list, low_score_*, etc.)
        """
        cur = self.conn.cursor()
        rows = cur.execute(
            "SELECT gene_id, content, complement, codons, promoter, "
            "epigenetics, chromatin, embedding, source_id, key_values, "
            "compression_tier "
            "FROM genes WHERE compression_tier = 0"
        ).fetchall()

        stats = {
            "scanned": len(rows),
            "to_euchromatin": 0,
            "to_heterochromatin": 0,
            "kept_open": 0,
            "skipped_no_embedding": 0,
            "by_reason": {},
        }

        for r in rows:
            gene = self._compact_row_to_gene(r)
            if gene is None:
                continue

            new_state, reason = self.apply_density_gate(gene)
            stats["by_reason"][reason] = stats["by_reason"].get(reason, 0) + 1

            if new_state == ChromatinState.OPEN:
                stats["kept_open"] += 1
                continue

            # Deny-listed documents ALWAYS demote — with or without embedding.
            # The whole point of the deny list is "this is structural
            # noise we never want to retrieve again." compress_to_heterochromatin
            # only needs source_id (it strips content, complement, fragments,
            # SPLADE, and FTS). The pre-existing no-embedding guard below
            # was a safety for score-based demotions, where a document might
            # later turn out to be useful and we want the ΣĒMA vector
            # available for reactivation. That reasoning doesn't apply
            # to structural deny-list hits. Previously this guard cost
            # us ~40% of expected demotions on the 2026-04-10 knowledge store
            # (3358 documents with no embeddings, mostly from pre-ΣĒMA
            # bulk ingests like ingest_steam.py).
            if reason == "deny_list":
                if not dry_run:
                    self.compress_to_heterochromatin(gene.gene_id)
                stats["to_heterochromatin"] += 1
                continue

            # Score-based demotions keep the embedding guard — these
            # documents might turn out to be useful later, so we want the
            # ΣĒMA vector available for reactivation via cosine similarity.
            has_embedding = r["embedding"] is not None
            if not has_embedding:
                stats["skipped_no_embedding"] += 1
                stats["kept_open"] += 1
                continue

            if new_state == ChromatinState.EUCHROMATIN:
                if gene.complement and len(gene.complement) > 30:
                    if not dry_run:
                        self.compress_to_euchromatin(gene.gene_id)
                    stats["to_euchromatin"] += 1
                else:
                    # No summary available to preserve; fall through to
                    # heterochromatin which doesn't need one
                    if not dry_run:
                        self.compress_to_heterochromatin(gene.gene_id)
                    stats["to_heterochromatin"] += 1
            elif new_state == ChromatinState.HETEROCHROMATIN:
                if not dry_run:
                    self.compress_to_heterochromatin(gene.gene_id)
                stats["to_heterochromatin"] += 1

        if not dry_run:
            self.checkpoint("PASSIVE")

        log.info(
            "Compaction sweep (%s): scanned=%d open=%d euchromatin=%d heterochromatin=%d skipped_no_emb=%d",
            "dry-run" if dry_run else "applied",
            stats["scanned"], stats["kept_open"],
            stats["to_euchromatin"], stats["to_heterochromatin"],
            stats["skipped_no_embedding"],
        )
        return stats

    def _compact_row_to_gene(self, row) -> Optional[Gene]:
        """Convert a compact database row to a Document object. Returns None on error.

        Pre-existing bug fix (2026-04-10): the original version passed
        key_values=None when the DB column was NULL, but Gene.key_values
        is declared as list[str] and Pydantic rejects None. Any document
        without extracted KVs (~35% of the 2026-04-10 knowledge store sample)
        silently failed parsing, causing compact_genome to skip them.
        Now we pass [] as the empty-list fallback, matching how other
        list fields (fragments) are handled.
        """
        try:
            def _opt(key: str, default=None):
                try:
                    return row[key]
                except (IndexError, KeyError):
                    return default

            return Gene(
                gene_id=row["gene_id"],
                content=row["content"] or "",
                complement=row["complement"],
                codons=json_loads(row["codons"]) if row["codons"] else [],
                promoter=parse_promoter(row["promoter"]),
                epigenetics=parse_epigenetics(row["epigenetics"]),
                chromatin=ChromatinState(row["chromatin"]),
                embedding=json_loads(row["embedding"]) if row["embedding"] else None,
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
                key_values=json_loads(row["key_values"]) if row["key_values"] else [],
            )
        except Exception:
            log.debug("Failed to parse gene row %s", row["gene_id"], exc_info=True)
            return None

    def close(self) -> None:
        self.checkpoint("TRUNCATE")  # Flush all WAL data before closing
        if self._reader is not None:
            try:
                self._reader.close()
            except Exception:
                pass
        self.conn.close()

    # ── Legacy method aliases (R3 Stage C; see docs/ROSETTA.md) ─────
    # Each alias points to the *same function object* as the canonical
    # method ("upsert_gene is upsert_doc" is True). External callers
    # that still call .upsert_doc() / .query_docs() / .get_doc()
    # keep working unchanged (notably api.py::gene_get and the
    # cross_store_import / sharding paths).
    #
    # SQL column names (gene_id, etc.) are unchanged — only the Python
    # method-name surface moved.
    upsert_gene              = upsert_doc
    query_genes              = query_docs
    query_genes_ann          = query_docs_ann
    query_genes_dense_recall = query_docs_dense_recall
    get_gene                 = get_doc


# R3 legacy alias — pre-R3 callers still import Genome. Identity preserved:
# Genome is KnowledgeStore. SQL table/column names (genes, gene_id, etc) are
# the on-disk contract and remain untouched. See docs/ROSETTA.md.
Genome = KnowledgeStore
