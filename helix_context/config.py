"""
Config — Runtime signal environment.

Loads helix.toml and exposes typed configuration for all modules.
Falls back to sensible defaults if no config file exists.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass
class RibosomeConfig:
    enabled: bool = False              # Master switch; false = ignore compressor config/runtime
    model: str = "auto"
    base_url: str = "http://localhost:11434"
    timeout: float = 10.0
    keep_alive: str = "30m"     # How long Ollama keeps the compressor model loaded
    warmup: bool = True         # Pre-load model on server start
    backend: str = "ollama"     # legacy placeholder; only "deberta" or "litellm" are honored when enabled
    claude_model: str = "claude-haiku-4-5-20251001"   # Claude model when backend="claude"
    claude_base_url: str = ""   # Proxy URL (e.g. Headroom at http://127.0.0.1:8787); "" = direct
    litellm_model: str = "gemini/gemini-2.5-flash"    # LiteLLM model string when backend="litellm"
    rerank_model_path: str = "training/models/rerank"
    splice_model_path: str = "training/models/splice"
    splice_threshold: float = 0.5
    nli_model_path: str = "training/models/nli"
    nli_splice_bonus: float = 0.15       # Prob bonus for entailment-linked fragments
    nli_splice_penalty: float = 0.15     # Prob penalty for alternation-linked fragments
    device: str = "auto"        # "auto", "cpu", "cuda"
    # Step 0 query-intent expansion fires ONE LLM call per novel query
    # (LRU-cached) upstream of the 12-tone retrieval stack. Flip to false
    # for a strictly LLM-free /context pipeline — the 12 tiers below still
    # run on raw query text + synonym map. See context_manager
    # _expand_query_intent.
    query_expansion_enabled: bool = True
    # Step 2 sub-query decomposition: decomposes broad queries into 2-4
    # point-fact sub-queries via one LLM call. Only fires for multi_hop/default
    # classifier classes. Dark-shipped (default off).
    query_decomposition_enabled: bool = False

    # ── Cost classification (W2-B) ─────────────────────────────────
    # Derived classification of the chosen backend's cost profile. Used
    # by /health and by a server-startup WARNING so operators are never
    # surprised by paid-API compressor calls. Add new backends to the
    # appropriate tuple.
    _LOCAL_BACKENDS = ("ollama", "deberta")
    _PAID_API_BACKENDS = ("claude",)
    # litellm is paid OR local depending on the underlying model string;
    # classified separately by ``cost_class`` (see below).

    @property
    def normalized_backend(self) -> str:
        return str(self.backend or "").strip().lower()

    @property
    def effective_backend(self) -> str:
        """Return the backend Helix should actually run.

        Compressor is opt-in. Legacy/default backends like ``ollama`` are kept
        in config for future reference but intentionally ignored unless a
        supported backend is selected explicitly.
        """
        if not self.enabled:
            return "disabled"
        if self.normalized_backend in ("litellm", "deberta"):
            return self.normalized_backend
        return "disabled"

    @property
    def cost_class(self) -> str:
        """Return one of ``disabled`` | ``local`` | ``api+free`` | ``api+paid``.

        - ``disabled``= compressor is intentionally inactive.
        - ``local``   = runs on the operator's machine (Ollama / DeBERTa).
        - ``api+free``= remote API with no metered cost (none today).
        - ``api+paid``= remote API that bills per call (Claude direct, or
                        LiteLLM routed to a paid model string).

        litellm with a model that starts with ``ollama/`` is treated as
        local (the call goes to the local Ollama). Any other litellm
        model string is treated as paid.
        """
        b = self.effective_backend
        if b == "disabled":
            return "disabled"
        if b in self._LOCAL_BACKENDS:
            return "local"
        if b in self._PAID_API_BACKENDS:
            return "api+paid"
        if b == "litellm":
            return "local" if self.litellm_model.startswith("ollama/") else "api+paid"
        return "api+paid"  # unknown backend defaults to paid for safety

    @property
    def active_model(self) -> str:
        """Return the model string actually in use, given the backend."""
        b = self.effective_backend
        if b == "disabled":
            return "disabled"
        if b == "claude":
            return self.claude_model
        if b == "litellm":
            return self.litellm_model
        return self.model


@dataclass
class BudgetConfig:
    ribosome_tokens: int = 3000
    expression_tokens: int = 6000
    max_genes_per_turn: int = 8
    max_fingerprints_per_turn: int = 40
    splice_aggressiveness: float = 0.5
    decoder_mode: str = "full"  # "full"|"condensed"|"minimal"|"none"
    # Sprint 1 legibility pack (AI-consumer roadmap): emit a one-line
    # metadata header per document in expressed_context — fired tiers,
    # confidence marker, short gene_id, compression ratio. See
    # helix_context/legibility.py. Default on; flip off to restore the
    # pre-Sprint-1 plain-dividers format (useful for bench A/B).
    legibility_enabled: bool = True
    # Stage 5 (2026-05-08): char-budget for the small_moe JSON answer slate.
    # Counts the rendered string the model actually sees, INCLUDING the
    # <helix:slate>...</helix:slate> wrapper, JSON braces, quotes, commas,
    # and per-KV separators. Spec §5 default is 1500. Generic and frontier
    # branches do not consult this knob.
    slate_char_budget: int = 1500
    # Sprint 2 session working-set register: track delivered documents per
    # session, elide repeats with a pointer stub so the consumer doesn't
    # pay full token cost for content it already holds. Dark on first
    # ship — flip to true in helix.toml to A/B. See session_delivery.py.
    session_delivery_enabled: bool = False
    abstain_enabled: bool = True       # NEW — see docs/specs/2026-05-02-abstain-tier-design.md
    # Foveated-splice (BROAD tier only). Off by default for the measurement
    # period — see docs/specs/2026-05-03-foveated-splice-design.md §6.3 and
    # docs/plans/2026-05-05-foveated-splice.md. Flip to True only after the
    # phased α-sweep bench (§9) identifies a winning configuration.
    foveated_enabled: bool = False
    # Power-law exponent for c_i = max(c_min, c_max · i^(-α)). α=0.5 = gentle
    # decay, α=1.0 = harmonic-ish, α=2.0 = aggressive top-bias.
    foveated_alpha: float = 1.0
    # Rank-N floor compression ratio. Pinned at 0.15 by spec §4.1.
    foveated_c_min: float = 0.15
    # Per-document char-budget multiplier. Each document's target_chars =
    # int(c_i · foveated_base_chars). Default 1000 matches the current
    # uniform behavior at c_i = 1.0. The Step 4 compression loop in
    # context_manager.py uses 1000 today; keeping this configurable lets
    # bench cells (and a future on-by-default ship) tune the top-1 ceiling
    # without touching code.
    foveated_base_chars: int = 1000


@dataclass
class GenomeConfig:
    path: str = "genome.db"
    compact_interval: float = 3600.0    # Seconds between source-change checks
    cold_start_threshold: int = 10      # Fix 3: documents needed before history stripping
    replicas: List[str] = field(default_factory=list)  # Read-only clone paths
    replica_sync_interval: int = 100    # Sync replicas every N inserts


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 11437
    upstream: str = "http://localhost:11434"
    upstream_timeout: float = 180.0     # Timeout for proxied requests to Ollama. Bumped from 120s on 2026-05-02 — observed Proxy 500s on slow gemma4:e4b GPQA queries at ~125s; 180s gives long-tail generation room without letting truly stuck requests hang. Override per-deployment via [server] in helix.toml.


@dataclass
class IngestionConfig:
    """Controls which backend encodes raw content into documents."""
    backend: str = "ollama"         # "ollama" | "cpu" | "hybrid"
    splade_enabled: bool = False    # Phase 2: SPLADE sparse expansion at index time
    rerank_model: str = ""          # Phase 3: pretrained cross-encoder HF model ID
    rerank_enabled: bool = False    # Phase 3: enable cross-encoder reranking
    colbert_enabled: bool = False   # Phase 4: ColBERT late interaction (optional)
    entity_graph: bool = False      # Phase 5: entity-based co-activation links


@dataclass
class ContextConfig:
    """Retrieval-time behavior for context_manager.

    Cold-tier knobs were added 2026-04-10 (C.2 of B->C). Cold-tier is the
    opt-in retrieval path that consults heterochromatin documents via SEMA
    cosine similarity, returning their preserved content (only possible
    after C.1 made compress_to_heterochromatin non-destructive).
    """
    cold_tier_enabled: bool = False         # Master opt-in for cold-tier fallthrough
    cold_tier_min_hot_genes: int = 0        # Fall through when hot returns <= this many documents (0 = only on empty)
    cold_tier_k: int = 3                    # Max cold-tier documents to retrieve per query
    cold_tier_min_cosine: float = 0.15      # SEMA cosine floor (sparse 20-dim — see Genome.query_cold_tier)
    fingerprint_mode_profile: str = "balanced"  # "fast" | "balanced" | "quality"


@dataclass
class CymaticsConfig:
    """Frequency-domain re_rank + splice (CPU math replaces LLM calls)."""
    enabled: bool = True                # Master switch
    n_bins: int = 256                   # Spectrum resolution (<2KB per spectrum)
    peak_width: float = 3.0             # Gaussian peak width (overridden by Q-factor)
    splice_threshold_scale: float = 0.7 # Maps splice_aggressiveness to resonance threshold
    use_embeddings: bool = False        # Use Gene.embedding when available
    harmonic_links: bool = True         # Compute weighted co-activation edges
    distance_metric: str = "cosine"     # "cosine" (weighted dot) | "w1" (Werman 1986 circular Wasserstein-1)


@dataclass
class SessionConfig:
    """CWoLa label-logger session/party defaults.

    Without these, clients that don't pass ``session_id`` / ``party_id`` in
    the /context POST body get logged with NULL in cwola_log, which causes
    sweep_buckets to treat every row as Bucket A (no re-query detectable
    without a session). The synthetic fallback generates a deterministic
    session_id from (client_ip, time_window), so bursts of traffic from the
    same operator get grouped into coherent sessions for bucket assignment.

    See docs/future/STATISTICAL_FUSION.md §C2 for the CWoLa framework.
    """
    default_party_id: str = "default"         # Used when the request omits party_id
    synthetic_session_window_s: int = 300     # 5 min — close-in-time same-IP requests become one session
    synthetic_session_enabled: bool = True    # Flip to false to preserve prior "NULL = all A" behavior


@dataclass
class RetrievalConfig:
    """Tier 5.5 SR + theta ray_trace bias (Sprint 2)."""
    # Successor Representation (Stachenfeld 2017) - lazy on-demand SR rows
    # via truncated power series over co-activation graph.
    sr_enabled: bool = False            # Dark ship — flip on for A/B
    sr_gamma: float = 0.85              # Discount factor (5-10 hop horizon at 0.9)
    sr_k_steps: int = 4                 # Power-series truncation depth
    sr_weight: float = 1.5              # Per-document contribution multiplier
    sr_cap: float = 3.0                 # Max per-document SR boost (matches harmonic cap)
    # Theta alternation (Wang/Foster/Pfeiffer 2020) — fore/aft ray_trace
    # sampling biased by the current TCM velocity vector.
    ray_trace_theta: bool = False       # Dark ship
    theta_weight: float = 1.0           # Softmax temperature on v·document dot product
    # Sprint 4 — seeded co-activation edges with Hebbian evidence decay.
    # Three-class edge provenance (seeded / co_retrieved / cwola_validated)
    # with Laplace-smoothed co_count vs miss_count per edge.
    seeded_edges_enabled: bool = False  # Dark ship — flip to start evidence accumulation
    seeded_edge_weight: float = 1.0     # Base weight written on seed insertion
    # Tier 0.5 filename-anchor (2026-04-15 Dewey-pivot spike).
    # Dewey bench showed filename alone outperforms the full
    # project+module+filename bag by 24pp. Boosts documents whose
    # filename_stem matches a query term.
    filename_anchor_enabled: bool = False   # Dark ship — flip for A/B
    filename_anchor_weight: float = 4.0     # Per-match boost (higher than Tier 1's 3.0)
    # BM25 shortlist post-filter (2026-04-22, research-review Pareto move 1).
    # When enabled, query_genes restricts its final ranking to documents that
    # cleared a BM25/FTS5 top-N pass — other tiers still accumulate scores,
    # but candidates BM25 would never surface are dropped before the sort.
    # Post-filter by design (isolates the ranking-set hypothesis from the
    # candidate-generation optimisation). Dark ship.
    bm25_shortlist_enabled: bool = False
    bm25_shortlist_size: int = 50           # BM25 top-N kept in the final ranking
    bm25_prefilter_enabled: bool = False
    bm25_prefilter_size: int = 200          # BM25 top-N fed into tier scoring
    # Tier 5b: entity graph co-occurrence boost (Step 3C, 2026-05-08).
    # Documents sharing entity nodes with query terms get a score boost proportional
    # to entity overlap. Dark ship — flip to true for A/B.
    entity_graph_retrieval_enabled: bool = False
    # Step 4 — BGE-M3 dense vectors + ANN threshold-based dynamic document counts
    # (2026-05-08). Dark ship — all flags off by default.
    dense_embedding_enabled: bool = False
    # Stage 2 (2026-05-08): default dim raised from 256 -> 1024. Full BGE-M3
    # Matryoshka. dim=256 collapsed random-pair cosine to ~0.6, sabotaging
    # threshold semantics. Stage 4 will recalibrate ann_similarity_threshold
    # at the new dim.
    dense_embedding_dim: int = 1024
    ann_similarity_threshold: float = 0.35
    ann_threshold_min_genes: int = 1
    ann_threshold_max_genes: int = 12
    # Stage 4 (2026-05-08): margin-over-random ANN calibration. Spec
    # docs/specs/2026-05-08-stage-4-threshold-calibration.md §3-§6.
    # ``"absolute"`` (default) keeps Stage-3 behavior byte-for-byte;
    # ``"margin_over_random"`` reads the persisted threshold from the
    # ``genome_calibration`` table (populated by
    # ``scripts/calibrate_thresholds.py``).
    ann_threshold_mode: str = "absolute"
    ann_threshold_sigma_multiplier: float = 3.0
    # Stage 2 (2026-05-08): dense recall pool size. Decoupled from
    # ann_threshold_max_genes (the final cut). 500 hits ~3% of an 18.9k
    # corpus per spec §4.
    dense_pool_size: int = 500
    # Stage 3 (2026-05-08): Reciprocal Rank Fusion accumulator.
    # Spec: docs/specs/2026-05-08-stage-3-rrf-fusion.md.
    # When ``fusion_mode == "additive"`` (default for one release), the
    # legacy ``gene_scores += tier_score`` accumulator path is unchanged.
    # When ``"rrf"``, each tier writes both raw scores AND ranks the
    # tier output through the Fuser; the final sort uses fused scores.
    # Per-tier weights below are RRF post-multipliers.
    fusion_mode: str = "additive"           # "additive" | "rrf"
    rrf_k: int = 60                         # Cormack 2009 default
    fts5_weight: float = 3.0                # current implicit cap (see genome.py FTS tier)
    splade_weight: float = 3.5              # current implicit cap
    tag_exact_weight: float = 3.0           # current weight × match_count
    tag_prefix_weight: float = 1.5          # current weight × match_count
    sema_cold_weight: float = 3.0           # current sim·3.0 multiplier
    lex_anchor_weight: float = 1.5          # current idf·1.5 (capped at 3.0)
    harmonic_weight: float = 1.0            # current per-link weight
    entity_graph_weight: float = 0.5        # current 1.0·0.5 implicit
    dense_weight: float = 1.0               # Stage 2 dense recall, RRF participant
    pki_weight: float = 1.0                 # PKI tier, RRF participant
    # Note: filename_anchor_weight, sr_weight reuse their existing knobs above.


@dataclass
class AbstainClassFloors:
    """Stage 4 per-classifier confidence floors.

    Replaces the global ``TIGHT_SCORE_FLOOR=5.0`` / ``FOCUSED_SCORE_FLOOR=2.5``
    / ``FOCUSED_SCORE_FLOOR_FOR_ABSTAIN=2.5`` constants in
    ``context_manager.py:946-989`` with per-class values calibrated from
    ``located_n1000.json`` score distributions.

    Spec: docs/specs/2026-05-08-stage-4-threshold-calibration.md §4 + §6.
    """
    # p85 of MISS scores — anything strictly below this is abstain.
    abstain_top: float = 2.5
    # p25 of HIT scores — at-or-above this enters FOCUSED tier (with ratio gate).
    focused_top: float = 2.5
    # p60 of HIT scores — at-or-above this enters TIGHT tier (with ratio gate).
    tight_top: float = 5.0
    # Per-class foveated splice power-law exponent. Replaces
    # ``budget.foveated_alpha`` when ``[abstain].mode = "per_classifier"``.
    foveated_alpha: float = 1.0


@dataclass
class AbstainConfig:
    """Stage 4 abstain/floor configuration block.

    ``mode``:
      - ``"global"`` (default) preserves Stage-3 behavior byte-for-byte —
        the hard-coded floors in ``context_manager.py`` apply unchanged.
      - ``"per_classifier"`` consults ``per_class[cls]`` (with ``default``
        fallback). Loader RAISES ``ConfigError`` if any required block is
        missing.
    """
    mode: str = "global"
    per_class: Dict[str, AbstainClassFloors] = field(default_factory=dict)

    def floors_for(self, cls: Optional[str]) -> AbstainClassFloors:
        """Per-spec lookup with ``default`` fallback.

        Returns the global-equivalent floors when ``mode == "global"``.
        """
        if self.mode == "global":
            # Identity floors — context_manager uses its hard-coded constants
            # in this branch and never consults this object except for telemetry.
            return AbstainClassFloors()
        if cls and cls in self.per_class:
            return self.per_class[cls]
        if "default" in self.per_class:
            return self.per_class["default"]
        # Last-resort identity — should not happen if loader validation passed.
        return AbstainClassFloors()


@dataclass
class ClassifierConfig:
    """Upstream rule-based query classifier / injection router.

    When enabled, contributes a decoder-mode hint and an assembly-stage
    document-count cap to build_context(). See
    docs/specs/2026-04-29-query-classifier-injection-router-design.md.
    """
    enabled: bool = True


@dataclass
class PLRConfig:
    """Stacked PLR query-confidence head (STATISTICAL_FUSION.md §C3).

    Attaches a `plr_confidence` log-odds signal to /context/packet responses
    when a trained artifact is on disk. Dark by default — callers that need
    the router / HITL signal flip `enabled=true`.

    The current artifact is a **query-quality head** (same score for all documents
    in a retrieval) rather than the per-(q,g) ranker the spec originally
    described. See `helix_context/fusion_plr.py` docstring and
    STATISTICAL_FUSION.md §C3 addendum for the trade-off.
    """
    enabled: bool = False
    model_path: str = "training/models/stacked_plr.joblib"
    # SHA256 of the artifact — when set, load refuses to proceed unless the
    # file's digest matches. Empty string = trust the sidecar .sha256 next
    # to the artifact (written by the trainer). Set a pinned hex digest in
    # helix.toml if you want explicit operator-level pinning.
    expected_sha256: str = ""
    # Threshold the fuser's `prob_B` is compared against to emit a coarse
    # "likely-to-re-query" boolean alongside the log-odds. 0.5 is the
    # symmetric default; tune only with bench evidence.
    high_risk_threshold: float = 0.5


@dataclass
class HeadroomConfig:
    """Headroom proxy lifecycle controls — launcher only.

    Headroom is a separate process (headroom-ai[proxy]) that serves a
    compression proxy + dashboard at `http://{host}:{port}/dashboard`.
    When ``autostart`` is true, the launcher spawns it as a child and
    surfaces it in the tray menu. When it's already running on
    ``port``, the launcher **adopts** the existing process rather than
    spawning a duplicate — the adopted process survives launcher Quit.

    ``enabled`` controls the proxy lifecycle (start / adopt the process).
    ``route_upstream`` controls whether helix's chat upstream is
    rewritten to dial this proxy. Separate concerns: you may want the
    proxy + dashboard running without the chat redirect, or vice versa.
    Both default off so a fresh install never silently rewrites the
    upstream to a proxy that isn't actually running. The
    ``HELIX_HEADROOM_ROUTE_UPSTREAM_AUTO`` env var (truthy → force on,
    falsy → force off, unset → defer to config) continues to work as a
    per-launch override.
    """
    enabled: bool = False               # Master switch; false = do nothing
    autostart: bool = True              # When enabled: adopt if running, spawn if not
    host: str = "127.0.0.1"
    port: int = 8787
    mode: str = "token"                 # "token" | "cache" (passed to --mode)
    dashboard_path: str = "/dashboard"  # Appended to http://{host}:{port}
    route_upstream: bool = False        # When true: launcher points helix's chat upstream at this proxy


@dataclass
class Hardware:
    """[hardware] config — see docs/specs/2026-05-04-hardware-detection-design.md.

    device: ``auto`` | ``cuda`` | ``rocm`` | ``mps`` | ``cpu``. Wired into
    ``hardware.init_from_config()`` at server startup.

    batch_sizes: per-model overrides applied on top of the auto-detected
    table. Empty dict means "use the table". The TOML literal string
    ``"auto"`` is also accepted and equivalent to ``{}``.

    low_vram_threshold_gb: under this VRAM tier the operator may want
    fp8 / quantized variants. Surfaced for downstream consumers; not
    used by the picker itself.
    """
    device: str = "auto"
    batch_sizes: Dict[str, int] = field(default_factory=dict)
    low_vram_threshold_gb: float = 4.0


@dataclass
class VaultTracesConfig:
    """Per-trace TTL/rollup knobs — see [vault.traces] in helix.toml."""

    enabled: bool = True
    retention_hours: int = 48
    max_retention_hours_hard: int = 720  # 30 days; 0 disables
    max_count: int = 10_000  # v1.1: not yet enforced
    rollup_enabled: bool = True
    rollup_shard: str = "hour"  # "hour" | "daily"
    prune_interval_minutes: int = 60
    trigger_only: bool = False  # v1.1: not yet enforced


@dataclass
class VaultConfig:
    """Obsidian vault export settings — off by default (enabled = False)."""

    enabled: bool = False
    path: str = "~/.helix/vault"
    party_id: str = ""  # empty = use server's primary party
    fan_out_threshold: int = 5000
    redact_body: bool = False
    stale_threshold: float = 0.5
    traces: VaultTracesConfig = field(default_factory=VaultTracesConfig)


@dataclass
class HelixConfig:
    ribosome: RibosomeConfig = field(default_factory=RibosomeConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    genome: GenomeConfig = field(default_factory=GenomeConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    ingestion: IngestionConfig = field(default_factory=IngestionConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    cymatics: CymaticsConfig = field(default_factory=CymaticsConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    # Stage 4 (2026-05-08) per-classifier confidence floors.
    abstain: AbstainConfig = field(default_factory=AbstainConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    plr: PLRConfig = field(default_factory=PLRConfig)
    headroom: HeadroomConfig = field(default_factory=HeadroomConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    hardware: Hardware = field(default_factory=Hardware)
    vault: VaultConfig = field(default_factory=VaultConfig)
    synonym_map: Dict[str, List[str]] = field(default_factory=dict)


def _warn_unknown(section: str, raw_section: Dict[str, Any], dataclass_type: type) -> None:
    """Log a warning when a TOML section contains keys not in the dataclass.

    Lightweight — does not fail, just surfaces typos / stale config early.
    """
    try:
        known = {f.name for f in fields(dataclass_type)}
        extras = set(raw_section.keys()) - known
        if extras:
            log.warning("Unknown keys in [%s]: %s", section, sorted(extras))
    except Exception:
        # Defensive: never let config validation break startup.
        log.warning("Unknown-key check failed for [%s]", section, exc_info=True)


def load_config(path: Optional[str] = None) -> HelixConfig:
    """
    Load helix.toml from the given path, or auto-discover from cwd / env.
    Returns defaults if no config file is found.
    """
    if path is None:
        path = os.environ.get("HELIX_CONFIG", "helix.toml")

    config_path = Path(path)
    if not config_path.exists():
        log.info("No config file at %s, using defaults", path)
        return HelixConfig()

    with open(config_path, "rb") as f:
        try:
            raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            log.error("helix.toml is malformed (%s) — using defaults", exc)
            return HelixConfig()

    cfg = HelixConfig()

    # Compressor
    if "ribosome" in raw:
        r = raw["ribosome"]
        _warn_unknown("ribosome", r, RibosomeConfig)
        cfg.ribosome = RibosomeConfig(
            enabled=bool(r.get("enabled", cfg.ribosome.enabled)),
            model=r.get("model", cfg.ribosome.model),
            base_url=r.get("base_url", cfg.ribosome.base_url),
            timeout=float(r.get("timeout", cfg.ribosome.timeout)),
            keep_alive=r.get("keep_alive", cfg.ribosome.keep_alive),
            warmup=r.get("warmup", cfg.ribosome.warmup),
            backend=r.get("backend", cfg.ribosome.backend),
            claude_model=r.get("claude_model", cfg.ribosome.claude_model),
            claude_base_url=r.get("claude_base_url", cfg.ribosome.claude_base_url),
            litellm_model=r.get("litellm_model", cfg.ribosome.litellm_model),
            rerank_model_path=r.get("rerank_model_path", cfg.ribosome.rerank_model_path),
            splice_model_path=r.get("splice_model_path", cfg.ribosome.splice_model_path),
            splice_threshold=float(r.get("splice_threshold", cfg.ribosome.splice_threshold)),
            nli_model_path=r.get("nli_model_path", cfg.ribosome.nli_model_path),
            nli_splice_bonus=float(r.get("nli_splice_bonus", cfg.ribosome.nli_splice_bonus)),
            nli_splice_penalty=float(r.get("nli_splice_penalty", cfg.ribosome.nli_splice_penalty)),
            device=r.get("device", cfg.ribosome.device),
            query_expansion_enabled=bool(r.get("query_expansion_enabled", cfg.ribosome.query_expansion_enabled)),
            query_decomposition_enabled=bool(r.get("query_decomposition_enabled", cfg.ribosome.query_decomposition_enabled)),
        )

    # Budget
    if "budget" in raw:
        b = raw["budget"]
        _warn_unknown("budget", b, BudgetConfig)
        cfg.budget = BudgetConfig(
            ribosome_tokens=b.get("ribosome_tokens", cfg.budget.ribosome_tokens),
            expression_tokens=b.get("expression_tokens", cfg.budget.expression_tokens),
            max_genes_per_turn=b.get("max_genes_per_turn", cfg.budget.max_genes_per_turn),
            max_fingerprints_per_turn=b.get("max_fingerprints_per_turn", cfg.budget.max_fingerprints_per_turn),
            splice_aggressiveness=float(b.get("splice_aggressiveness", cfg.budget.splice_aggressiveness)),
            decoder_mode=b.get("decoder_mode", cfg.budget.decoder_mode),
            legibility_enabled=bool(b.get("legibility_enabled", cfg.budget.legibility_enabled)),
            session_delivery_enabled=bool(b.get("session_delivery_enabled", cfg.budget.session_delivery_enabled)),
            abstain_enabled=bool(b.get("abstain_enabled", cfg.budget.abstain_enabled)),
            foveated_enabled=bool(b.get("foveated_enabled", cfg.budget.foveated_enabled)),
            foveated_alpha=float(b.get("foveated_alpha", cfg.budget.foveated_alpha)),
            foveated_c_min=float(b.get("foveated_c_min", cfg.budget.foveated_c_min)),
            foveated_base_chars=int(b.get("foveated_base_chars", cfg.budget.foveated_base_chars)),
            slate_char_budget=int(b.get("slate_char_budget", cfg.budget.slate_char_budget)),
        )

    # KnowledgeStore
    if "genome" in raw:
        g = raw["genome"]
        _warn_unknown("genome", g, GenomeConfig)
        cfg.genome = GenomeConfig(
            path=g.get("path", cfg.genome.path),
            compact_interval=float(g.get("compact_interval", cfg.genome.compact_interval)),
            cold_start_threshold=int(g.get("cold_start_threshold", cfg.genome.cold_start_threshold)),
            replicas=g.get("replicas", cfg.genome.replicas),
            replica_sync_interval=int(g.get("replica_sync_interval", cfg.genome.replica_sync_interval)),
        )

    # Server
    if "server" in raw:
        s = raw["server"]
        _warn_unknown("server", s, ServerConfig)
        cfg.server = ServerConfig(
            host=s.get("host", cfg.server.host),
            port=int(s.get("port", cfg.server.port)),
            upstream=s.get("upstream", cfg.server.upstream),
            upstream_timeout=float(s.get("upstream_timeout", cfg.server.upstream_timeout)),
        )

    # KnowledgeStore path override — lets sharded vs monolithic servers coexist
    # on different ports without duplicating helix.toml. Typical use:
    # ``HELIX_GENOME_PATH=genomes/main.genome.db HELIX_USE_SHARDS=1`` for a
    # sharded bench server on a side port; defaults still serve monolithic.
    if os.environ.get("HELIX_GENOME_PATH"):
        cfg.genome.path = os.environ["HELIX_GENOME_PATH"]

    # Server env overrides — lets launchers/profiles redirect Helix to a
    # different chat upstream without rewriting helix.toml on disk.
    if os.environ.get("HELIX_SERVER_UPSTREAM"):
        cfg.server.upstream = os.environ["HELIX_SERVER_UPSTREAM"]
    if os.environ.get("HELIX_SERVER_UPSTREAM_TIMEOUT"):
        try:
            cfg.server.upstream_timeout = float(os.environ["HELIX_SERVER_UPSTREAM_TIMEOUT"])
        except ValueError:
            log.warning(
                "HELIX_SERVER_UPSTREAM_TIMEOUT=%r is not a float — ignoring override",
                os.environ["HELIX_SERVER_UPSTREAM_TIMEOUT"],
            )

    # Ingestion
    if "ingestion" in raw:
        i = raw["ingestion"]
        _warn_unknown("ingestion", i, IngestionConfig)
        cfg.ingestion = IngestionConfig(
            backend=i.get("backend", cfg.ingestion.backend),
            splade_enabled=i.get("splade_enabled", cfg.ingestion.splade_enabled),
            rerank_model=i.get("rerank_model", cfg.ingestion.rerank_model),
            rerank_enabled=i.get("rerank_enabled", cfg.ingestion.rerank_enabled),
            colbert_enabled=i.get("colbert_enabled", cfg.ingestion.colbert_enabled),
            entity_graph=i.get("entity_graph", cfg.ingestion.entity_graph),
        )

    # Context (cold-tier retrieval knobs — C.2 of B->C, 2026-04-10)
    if "context" in raw:
        c = raw["context"]
        _warn_unknown("context", c, ContextConfig)
        cfg.context = ContextConfig(
            cold_tier_enabled=bool(c.get("cold_tier_enabled", cfg.context.cold_tier_enabled)),
            cold_tier_min_hot_genes=int(c.get("cold_tier_min_hot_genes", cfg.context.cold_tier_min_hot_genes)),
            cold_tier_k=int(c.get("cold_tier_k", cfg.context.cold_tier_k)),
            cold_tier_min_cosine=float(c.get("cold_tier_min_cosine", cfg.context.cold_tier_min_cosine)),
            fingerprint_mode_profile=str(c.get("fingerprint_mode_profile", cfg.context.fingerprint_mode_profile)).lower(),
        )

    # Cymatics
    if "cymatics" in raw:
        cy = raw["cymatics"]
        _warn_unknown("cymatics", cy, CymaticsConfig)
        cfg.cymatics = CymaticsConfig(
            enabled=cy.get("enabled", cfg.cymatics.enabled),
            n_bins=int(cy.get("n_bins", cfg.cymatics.n_bins)),
            peak_width=float(cy.get("peak_width", cfg.cymatics.peak_width)),
            splice_threshold_scale=float(cy.get("splice_threshold_scale", cfg.cymatics.splice_threshold_scale)),
            use_embeddings=cy.get("use_embeddings", cfg.cymatics.use_embeddings),
            harmonic_links=cy.get("harmonic_links", cfg.cymatics.harmonic_links),
            distance_metric=str(cy.get("distance_metric", cfg.cymatics.distance_metric)).lower(),
        )

    # Retrieval (Sprint 2 — SR Tier 5.5 + theta alternation)
    if "retrieval" in raw:
        r = raw["retrieval"]
        _warn_unknown("retrieval", r, RetrievalConfig)
        cfg.retrieval = RetrievalConfig(
            sr_enabled=bool(r.get("sr_enabled", cfg.retrieval.sr_enabled)),
            sr_gamma=float(r.get("sr_gamma", cfg.retrieval.sr_gamma)),
            sr_k_steps=int(r.get("sr_k_steps", cfg.retrieval.sr_k_steps)),
            sr_weight=float(r.get("sr_weight", cfg.retrieval.sr_weight)),
            sr_cap=float(r.get("sr_cap", cfg.retrieval.sr_cap)),
            ray_trace_theta=bool(r.get("ray_trace_theta", cfg.retrieval.ray_trace_theta)),
            theta_weight=float(r.get("theta_weight", cfg.retrieval.theta_weight)),
            seeded_edges_enabled=bool(r.get("seeded_edges_enabled", cfg.retrieval.seeded_edges_enabled)),
            seeded_edge_weight=float(r.get("seeded_edge_weight", cfg.retrieval.seeded_edge_weight)),
            filename_anchor_enabled=bool(r.get("filename_anchor_enabled", cfg.retrieval.filename_anchor_enabled)),
            filename_anchor_weight=float(r.get("filename_anchor_weight", cfg.retrieval.filename_anchor_weight)),
            bm25_shortlist_enabled=bool(r.get("bm25_shortlist_enabled", cfg.retrieval.bm25_shortlist_enabled)),
            bm25_shortlist_size=int(r.get("bm25_shortlist_size", cfg.retrieval.bm25_shortlist_size)),
            bm25_prefilter_enabled=bool(r.get("bm25_prefilter_enabled", cfg.retrieval.bm25_prefilter_enabled)),
            bm25_prefilter_size=int(r.get("bm25_prefilter_size", cfg.retrieval.bm25_prefilter_size)),
            entity_graph_retrieval_enabled=bool(r.get("entity_graph_retrieval_enabled", cfg.retrieval.entity_graph_retrieval_enabled)),
            dense_embedding_enabled=bool(r.get("dense_embedding_enabled", cfg.retrieval.dense_embedding_enabled)),
            dense_embedding_dim=int(r.get("dense_embedding_dim", cfg.retrieval.dense_embedding_dim)),
            ann_similarity_threshold=float(r.get("ann_similarity_threshold", cfg.retrieval.ann_similarity_threshold)),
            ann_threshold_min_genes=int(r.get("ann_threshold_min_genes", cfg.retrieval.ann_threshold_min_genes)),
            ann_threshold_max_genes=int(r.get("ann_threshold_max_genes", cfg.retrieval.ann_threshold_max_genes)),
            # Stage 4 (2026-05-08) margin-over-random calibration mode.
            ann_threshold_mode=str(r.get("ann_threshold_mode", cfg.retrieval.ann_threshold_mode)),
            ann_threshold_sigma_multiplier=float(r.get(
                "ann_threshold_sigma_multiplier",
                cfg.retrieval.ann_threshold_sigma_multiplier,
            )),
            # Stage 2 (2026-05-08): dense recall pool size, decoupled from final cut.
            dense_pool_size=int(r.get("dense_pool_size", cfg.retrieval.dense_pool_size)),
            # Stage 3 (2026-05-08): RRF fusion. Default "additive" preserves
            # pre-Stage-3 behavior byte-for-byte. Flip to "rrf" for the new
            # rank-fusion path. Spec §7 deprecation timeline keeps both
            # implementations alive for one release.
            fusion_mode=str(r.get("fusion_mode", cfg.retrieval.fusion_mode)),
            rrf_k=int(r.get("rrf_k", cfg.retrieval.rrf_k)),
            fts5_weight=float(r.get("fts5_weight", cfg.retrieval.fts5_weight)),
            splade_weight=float(r.get("splade_weight", cfg.retrieval.splade_weight)),
            tag_exact_weight=float(r.get("tag_exact_weight", cfg.retrieval.tag_exact_weight)),
            tag_prefix_weight=float(r.get("tag_prefix_weight", cfg.retrieval.tag_prefix_weight)),
            sema_cold_weight=float(r.get("sema_cold_weight", cfg.retrieval.sema_cold_weight)),
            lex_anchor_weight=float(r.get("lex_anchor_weight", cfg.retrieval.lex_anchor_weight)),
            harmonic_weight=float(r.get("harmonic_weight", cfg.retrieval.harmonic_weight)),
            entity_graph_weight=float(r.get("entity_graph_weight", cfg.retrieval.entity_graph_weight)),
            dense_weight=float(r.get("dense_weight", cfg.retrieval.dense_weight)),
            pki_weight=float(r.get("pki_weight", cfg.retrieval.pki_weight)),
        )

    # Stage 4 (2026-05-08) abstain config — global vs per_classifier mode.
    # Spec docs/specs/2026-05-08-stage-4-threshold-calibration.md §6.
    if "abstain" in raw:
        a = raw["abstain"]
        if not isinstance(a, dict):
            log.warning("[abstain] is not a table; ignoring")
        else:
            mode = str(a.get("mode", "global"))
            if mode not in ("global", "per_classifier"):
                log.warning(
                    "[abstain].mode=%r is invalid; falling back to 'global'", mode
                )
                mode = "global"
            per_class: Dict[str, AbstainClassFloors] = {}
            # Discover sub-tables — any [abstain.<cls>] block.
            for cls, block in a.items():
                if cls == "mode" or not isinstance(block, dict):
                    continue
                _warn_unknown(f"abstain.{cls}", block, AbstainClassFloors)
                per_class[cls] = AbstainClassFloors(
                    abstain_top=float(block.get("abstain_top",
                                                AbstainClassFloors.abstain_top)),
                    focused_top=float(block.get("focused_top",
                                                AbstainClassFloors.focused_top)),
                    tight_top=float(block.get("tight_top",
                                              AbstainClassFloors.tight_top)),
                    foveated_alpha=float(block.get("foveated_alpha",
                                                   AbstainClassFloors.foveated_alpha)),
                )
            if mode == "per_classifier":
                # Spec §6: per_classifier requires a `default` block (the runtime
                # fallback for missing classes). Other classes may be omitted
                # without raising — `floors_for(cls)` will fall back to default.
                if "default" not in per_class:
                    from .exceptions import ConfigError
                    raise ConfigError(
                        "[abstain].mode='per_classifier' requires an "
                        "[abstain.default] block (loader §6); none found."
                    )
            cfg.abstain = AbstainConfig(mode=mode, per_class=per_class)

    # Session (CWoLa session/party fallback — 2026-04-13 fix for always-A bucket bug)
    if "session" in raw:
        s = raw["session"]
        _warn_unknown("session", s, SessionConfig)
        cfg.session = SessionConfig(
            default_party_id=str(s.get("default_party_id", cfg.session.default_party_id)),
            synthetic_session_window_s=int(s.get("synthetic_session_window_s", cfg.session.synthetic_session_window_s)),
            synthetic_session_enabled=bool(s.get("synthetic_session_enabled", cfg.session.synthetic_session_enabled)),
        )

    # PLR — stacked-classifier query-confidence head (dark by default)
    if "plr" in raw:
        p = raw["plr"]
        _warn_unknown("plr", p, PLRConfig)
        cfg.plr = PLRConfig(
            enabled=bool(p.get("enabled", cfg.plr.enabled)),
            model_path=str(p.get("model_path", cfg.plr.model_path)),
            expected_sha256=str(p.get("expected_sha256", cfg.plr.expected_sha256)),
            high_risk_threshold=float(
                p.get("high_risk_threshold", cfg.plr.high_risk_threshold)
            ),
        )

    # Headroom — optional proxy lifecycle controls
    if "headroom" in raw:
        h = raw["headroom"]
        _warn_unknown("headroom", h, HeadroomConfig)
        cfg.headroom = HeadroomConfig(
            enabled=bool(h.get("enabled", cfg.headroom.enabled)),
            autostart=bool(h.get("autostart", cfg.headroom.autostart)),
            host=str(h.get("host", cfg.headroom.host)),
            port=int(h.get("port", cfg.headroom.port)),
            mode=str(h.get("mode", cfg.headroom.mode)),
            dashboard_path=str(h.get("dashboard_path", cfg.headroom.dashboard_path)),
            route_upstream=bool(h.get("route_upstream", cfg.headroom.route_upstream)),
        )

    # Classifier — upstream rule-based query classifier / injection router
    if "classifier" in raw:
        cls_section = raw["classifier"]
        _warn_unknown("classifier", cls_section, ClassifierConfig)
        cfg.classifier = ClassifierConfig(
            enabled=bool(cls_section.get("enabled", cfg.classifier.enabled)),
        )

    # Hardware section — see docs/specs/2026-05-04-hardware-detection-design.md.
    # Always run, even if [hardware] is absent, because the [compressor]
    # device deprecation shim must fire whenever the legacy key is set.
    hw = raw.get("hardware", {})
    if isinstance(hw, dict):
        _warn_unknown("hardware", hw, Hardware)
    if isinstance(hw.get("batch_sizes"), str) and hw["batch_sizes"] == "auto":
        bs: Dict[str, int] = {}
    elif isinstance(hw.get("batch_sizes"), dict):
        bs = {k: int(v) for k, v in hw["batch_sizes"].items()}
    else:
        bs = {}
    hardware_device = hw.get("device", "auto")

    # Deprecation shim for [compressor] device. Fires whenever the legacy
    # key is present, even if [hardware] is also set — the warning text
    # MUST contain both "deprecated" and "override" in the both-set case
    # so test_hardware_overrides_ribosome_device's substring asserts
    # match. Don't reword without updating the tests.
    ribosome_device = raw.get("ribosome", {}).get("device")
    if ribosome_device is not None:
        if "device" in hw:
            log.warning(
                "[ribosome] device is deprecated; [hardware] device=%r takes "
                "precedence (override). Remove [ribosome] device.", hardware_device,
            )
        else:
            log.warning(
                "[ribosome] device is deprecated; move to [hardware] device. "
                "Using ribosome.device=%r for now.", ribosome_device,
            )
            hardware_device = ribosome_device

    cfg.hardware = Hardware(
        device=str(hardware_device),
        batch_sizes=bs,
        low_vram_threshold_gb=float(hw.get("low_vram_threshold_gb", 4.0)),
    )

    # Vault — Obsidian export (opt-in, off by default)
    v_section = raw.get("vault", {})
    _warn_unknown("vault", v_section, VaultConfig)
    v_traces_section = v_section.get("traces", {})
    _warn_unknown("vault.traces", v_traces_section, VaultTracesConfig)
    cfg.vault = VaultConfig(
        enabled=v_section.get("enabled", cfg.vault.enabled),
        path=v_section.get("path", cfg.vault.path),
        party_id=v_section.get("party_id", cfg.vault.party_id),
        fan_out_threshold=v_section.get("fan_out_threshold", cfg.vault.fan_out_threshold),
        redact_body=v_section.get("redact_body", cfg.vault.redact_body),
        stale_threshold=v_section.get("stale_threshold", cfg.vault.stale_threshold),
        traces=VaultTracesConfig(
            enabled=v_traces_section.get("enabled", cfg.vault.traces.enabled),
            retention_hours=v_traces_section.get("retention_hours", cfg.vault.traces.retention_hours),
            max_retention_hours_hard=v_traces_section.get("max_retention_hours_hard", cfg.vault.traces.max_retention_hours_hard),
            max_count=v_traces_section.get("max_count", cfg.vault.traces.max_count),
            rollup_enabled=v_traces_section.get("rollup_enabled", cfg.vault.traces.rollup_enabled),
            rollup_shard=v_traces_section.get("rollup_shard", cfg.vault.traces.rollup_shard),
            prune_interval_minutes=v_traces_section.get("prune_interval_minutes", cfg.vault.traces.prune_interval_minutes),
            trigger_only=v_traces_section.get("trigger_only", cfg.vault.traces.trigger_only),
        ),
    )

    # Fix 1: synonym map
    if "synonyms" in raw:
        cfg.synonym_map = {
            k: list(v) for k, v in raw["synonyms"].items()
        }

    # Coherence guard: if the ribosome is disabled (the LLM-free design
    # default — see docs/MISSION.md) but ingestion.backend still asks for
    # an LLM-backed path (``ollama`` / ``deberta`` / ``litellm``), the
    # CLI ``helix ingest`` call will raise ``TranscriptionError: Pack
    # failed: Ribosome is disabled`` on the first chunk. The two settings
    # contradict each other; flip ``ingestion.backend`` to ``"cpu"`` so
    # the spaCy/heuristic CpuTagger handles the ingest. Emit at WARNING
    # so the operator can see the auto-fallback in logs and disable it
    # by either (a) enabling ``[ribosome]`` explicitly or (b) setting
    # ``[ingestion] backend = "cpu"`` themselves. Requires the ``[cpu]``
    # extra (spaCy) to be installed — CpuTagger logs its own warning if
    # spaCy is missing.
    if (
        not cfg.ribosome.enabled
        and cfg.ingestion.backend in ("ollama", "deberta", "litellm")
    ):
        log.warning(
            "[ribosome] enabled=false but [ingestion] backend=%r requires the "
            "ribosome — auto-falling-back to backend='cpu' (spaCy CpuTagger). "
            "Install the [cpu] extra if you have not. Override by either "
            "enabling [ribosome] or setting [ingestion] backend='cpu' / 'hybrid' "
            "explicitly in helix.toml.",
            cfg.ingestion.backend,
        )
        cfg.ingestion.backend = "cpu"

    log.info("Config loaded from %s", config_path)
    return cfg
