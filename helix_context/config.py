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
    # default aligned with shipped helix.toml (2026-06-12 default-honesty pass):
    # the shipped toml pins the light pack/replicate fallback model instead of
    # "auto" (compressor auto-detect). Inert while enabled=False.
    model: str = "gemma4:e2b"
    base_url: str = "http://localhost:11434"
    timeout: float = 120.0  # default aligned with shipped helix.toml (2026-06-12 default-honesty pass) — bulk ingestion headroom
    keep_alive: str = "30m"     # How long Ollama keeps the compressor model loaded
    warmup: bool = False        # Pre-load model on server start. Default aligned with shipped helix.toml (2026-06-12 default-honesty pass)
    backend: str = "none"       # disabled-state placeholder; only "deberta" or "litellm" are honored when enabled. Default aligned with shipped helix.toml (2026-06-12 default-honesty pass)
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
    # default aligned with shipped helix.toml (2026-06-12 default-honesty
    # pass): false keeps /context strictly LLM-free (the design default —
    # docs/MISSION.md); flip on for ~2-3pp on ambiguous queries at one
    # ribosome call per novel query.
    query_expansion_enabled: bool = False
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
    expression_tokens: int = 7000  # default aligned with shipped helix.toml (2026-06-12 default-honesty pass)
    max_genes_per_turn: int = 12  # default aligned with shipped helix.toml (2026-06-12 default-honesty pass)
    max_fingerprints_per_turn: int = 40
    splice_aggressiveness: float = 0.3  # default aligned with shipped helix.toml (2026-06-12 default-honesty pass)
    decoder_mode: str = "condensed"  # "full"|"condensed"|"minimal"|"none". Default aligned with shipped helix.toml (2026-06-12 default-honesty pass)
    # Issue #207 item 6: operator override for the compressor/ribosome-model
    # capability classification (context_manager.resolve_model_capability_class)
    # -- NOT the same table as decoder_mode above. Maps a model-name substring
    # (case-insensitive) to one of "moe" / "small" / "large"; checked before
    # the hand-calibrated MOE_MODEL_FAMILIES / SMALL_MODEL_PATTERNS tables and
    # the generic ":NNb" parameter-size fallback those tables now have. Empty
    # by default: byte-identical to pre-#207 behavior.
    decoder_mode_overrides: Dict[str, str] = field(default_factory=dict)
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
    # pay full token cost for content it already holds. Enabled 2026-04-19
    # (saves ~40% tokens on multi-turn conversations); only fires when the
    # caller supplies a session_id. See session_delivery.py.
    # default aligned with shipped helix.toml (2026-06-12 default-honesty pass)
    session_delivery_enabled: bool = True
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
    # Splice-floor fix (J-space council kill-switch #1, 2026-07-06).
    # Per-document char target for the Step-4 splice loop (non-foveated
    # path). 0 (default) = budget-proportional auto:
    # int(expression_tokens · 4 chars/token · 0.9) // n_candidates,
    # floored at the legacy 1000 — so no document ever gets less room
    # than the old uniform cap, and the expression budget is actually
    # used (12 × 1000 chars ≈ 3000 tokens vs the default 7000). Any
    # positive value pins a fixed target; 1000 restores the exact
    # legacy query-agnostic floor. See context_manager.
    # _compute_splice_target and encoding/headroom_bridge.
    # _query_aware_trim (truncation keeps query-term lines either way).
    splice_target_chars: int = 0
    # -- Issue #207 item 4: budget-tier constants -> knobs (default-inert). -
    # The four fields below expose the hard-coded tier constants in
    # pipeline/tier_logic.py. Defaults reproduce the prior literals
    # byte-for-byte. All were calibrated on owner-corpus probes at the
    # additive/BM25 score scale; exposing them does NOT recalibrate them
    # (recalibration rides the #287 abstain-calibration work).
    tier_tight_ratio: float = 3.0  # Issue #207 item 4: top/mean ratio at-or-above which retrieval enters TIGHT tier (top 3 docs). Prior literal 3.0 in pipeline/tier_logic.py.
    tier_focused_ratio: float = 1.8  # Issue #207 item 4: top/mean ratio at-or-above which retrieval enters FOCUSED tier (top 6 docs). Prior literal 1.8 in pipeline/tier_logic.py.
    tier_hard_floor_frac: float = 0.15  # Issue #207 item 4: score-gate hard floor — drop candidates scoring below this fraction of the top score (they move to the shadow pool at 0.5x weight). Prior literal 0.15 in pipeline/tier_logic.py.
    tier_lagrange_frac: float = 0.7  # Issue #207 item 4: Lagrange pull-back threshold — a shadow-pool doc needs standalone score >= this fraction of the winners' floor (plus <20% co-activation overlap) to be pulled back. Prior literal 0.7 in pipeline/tier_logic.py.


@dataclass
class GenomeConfig:
    path: str = "genomes/main/genome.db"  # default aligned with shipped helix.toml (2026-06-12 default-honesty pass) — genomes/ is the phase-2 sharding root; CLAUDE.md documents this as THE default
    compact_interval: float = 3600.0    # Seconds between source-change checks
    cold_start_threshold: int = 10      # Fix 3: documents needed before history stripping
    replicas: List[str] = field(default_factory=list)  # Read-only clone paths
    replica_sync_interval: int = 100    # Sync replicas every N inserts


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 11437
    upstream: str = "http://localhost:11434"
    # Dev/configuration mode (v0.7.0): run a SECOND helix instance on a
    # side port bound to a bench genome, so a primary chat stays attached
    # to the main genome while a subagent drives the bench-harness against
    # the bench port. Default OFF — a final deployment leaves this off and
    # gets exactly one server. The launcher reads these at boot; flip in
    # helix.toml or via --bench / HELIX_BENCH_ENABLED=1.
    bench_enabled: bool = False
    bench_port: int = 11439
    bench_genome_path: str = "genomes/bench/bench.genome.db"
    upstream_timeout: float = 180.0     # Timeout for proxied requests to Ollama. Bumped from 120s on 2026-05-02 — observed Proxy 500s on slow gemma4:e4b GPQA queries at ~125s; 180s gives long-tail generation room without letting truly stuck requests hang. Override per-deployment via [server] in helix.toml.


@dataclass
class TelemetryConfig:
    """[telemetry] — OpenTelemetry export defaults for the backend.

    Mirrors the HELIX_OTEL_* env vars read by ``telemetry/otel.py``.
    Precedence at setup time is env > this section > dataclass default —
    the resolution happens in ``otel.resolve_telemetry_settings()``, NOT
    here, so ``load_config`` stays env-free for telemetry and the
    default-honesty comparator (tests/test_config_default_honesty.py)
    never sees env-dependent values.

    ``enabled`` defaults false: a bare backend (no launcher, no stack)
    must not dial a dead collector. The launcher closes the out-of-the-
    box gap the other way — when it starts (or adopts) the observability
    stack it exports HELIX_OTEL_ENABLED=1 into the helix child's env
    (launcher/app.py ``_export_otel_env_for_backend``), which wins over
    this default by design.
    """
    enabled: bool = False               # Master switch (HELIX_OTEL_ENABLED)
    endpoint: str = "localhost:4317"    # OTLP gRPC (HELIX_OTEL_ENDPOINT)
    insecure: bool = True               # Plain gRPC, dev-local (HELIX_OTEL_INSECURE)
    sampler_ratio: float = 1.0          # Trace sampler 0.0-1.0 (HELIX_OTEL_SAMPLER_RATIO)
    redact_query: bool = True           # Hash query strings in spans (HELIX_OTEL_REDACT_QUERY)
    logs_enabled: bool = True           # Ship Python logs to OTel/Loki (HELIX_OTEL_LOGS_ENABLED)
    logs_level: str = "INFO"            # Min level forwarded (HELIX_OTEL_LOGS_LEVEL)


@dataclass
class IngestionConfig:
    """Controls which backend encodes raw content into documents."""
    # default aligned with shipped helix.toml (2026-06-12 default-honesty
    # pass): "cpu" (spaCy/heuristic CpuTagger). The load_config coherence
    # guard already auto-flipped "ollama" to "cpu" whenever the ribosome was
    # disabled (the default), so "cpu" is what installs actually ran.
    backend: str = "cpu"            # "ollama" | "cpu" | "hybrid"
    # default aligned with shipped helix.toml (2026-06-12 default-honesty
    # pass). NOTE #164 measured SPLADE at 0 pp recall@10 / 21.1% of disk on
    # the 850K fixture, but the curve below ~50K genes is unresolved and the
    # shipped toml (every bench this month) ran true — so toml wins here;
    # the size-aware auto-disable knob below covers the enterprise cliff.
    # Soft-fails to a no-op when torch/transformers are absent.
    splade_enabled: bool = True     # Phase 2: SPLADE sparse expansion at index time
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"  # Phase 3: pretrained cross-encoder HF model ID — inert while rerank_enabled=False. Default aligned with shipped helix.toml (2026-06-12 default-honesty pass)
    rerank_enabled: bool = False    # Phase 3: enable cross-encoder reranking
    colbert_enabled: bool = False   # Phase 4: ColBERT late interaction (optional)
    entity_graph: bool = True       # Phase 5: entity-based co-activation links (ingest-time edges). Default aligned with shipped helix.toml (2026-06-12 default-honesty pass)
    # Tier-0 PR-1 (2026-05-16): compute BGE-M3 dense vectors
    # (genes.embedding_dense_v2) inline at ingest. Default true so a genome
    # built by `helix ingest` / `/ingest` / context_manager.ingest is
    # dense-populated without a separate backfill pass. Latency-sensitive
    # callers can set false to defer encoding to scripts/backfill_bgem3_v2.py.
    # This is purely the WRITE path — retrieval still gates on
    # [retrieval] dense_embedding_enabled (default true).
    dense_embed_on_ingest: bool = True
    # Issue #227: compute the 20D ΣĒMA embedding at ingest (feeds TCM / cymatics
    # via gene.embedding). Default True preserves current behaviour. Set False to
    # skip the ingest-time SEMA encode entirely — the MiniLM model is then never
    # materialized (TCM falls back to its text-derived path, cymatics off), which
    # is what a lexical-only config or a multi-worker bench wants. Without this,
    # ingest always materialized the lazy SEMA codec (#220), loading MiniLM per
    # worker and OOMing parallel bench runs even with dense/cymatics disabled.
    sema_embed_on_ingest: bool = True
    # WS2 (symbol graph): at ingest, index symbol definitions and emit
    # referencing-chunk -> defining-chunk SYMBOL_REF edges (code only).
    # Resolution is intra-file (high precision). Off = WS1-only chunking, at
    # zero extraction cost (the flag gates the symbol parse itself, not just
    # emission — WS2 review FIX-3).
    #
    # Default False — INTENTIONAL DARK-SHIP (2026-07-20). The ContextBench
    # held-out re-run cleared the merge gate (packet +2.8pp line / +3.8pp
    # sym; docs/benchmarks/2026-07-20-armc-contextbench-heldout.md), so this
    # deviates deliberately from decision rule 2's "merge default-on":
    # SIKE 2026-07-19 showed a prose-bed regression with the current
    # code-query gating, so default-on waits on the symbol_expansion_cap
    # sweep {4,16} + code-gating validation. Flipping the default is #231's
    # follow-up, not this PR's.
    symbol_graph: bool = False
    # Issue #164 (size-aware SPLADE auto-toggle): SPLADE expansion's value
    # follows a corpus-regime curve -- the v2 EnterpriseRAG-Onyx storage
    # breakdown showed SPLADE at 21.1% of disk on the 850K-gene fixture
    # while contributing 0 pp recall@10 vs SPLADE-off at the same scale
    # (n=5 + 100q in-flight; see issue body). Below ~50K it's likely useful;
    # above ~200K it's likely net-negative (disk + p95 + SQL fan-out).
    # When BOTH thresholds are 0 (default) the toggle is disabled and the
    # static ``splade_enabled`` value governs every upsert -- byte-identical
    # to pre-#164 behaviour. Setting either threshold to a positive value
    # opts the genome in:
    #   - splade_auto_enable_below_genes > 0: force SPLADE ON when the
    #     current gene_count is strictly below the threshold, even if
    #     ``splade_enabled = false``. The "sparse-corpus rescue" arm.
    #   - splade_auto_disable_above_genes > 0: force SPLADE OFF when the
    #     current gene_count is strictly above the threshold, even if
    #     ``splade_enabled = true``. The "enterprise-scale storage cliff"
    #     arm.
    # Both default 0 (opt-in) because the scale curve in #164 is not yet
    # empirically resolved across the 10K-100K transition band; conservative
    # defaults will land in a follow-up once the per-fixture sweep is wired
    # to a head-to-head SPLADE-on/off ablation across that range.
    splade_auto_enable_below_genes: int = 0
    splade_auto_disable_above_genes: int = 0
    # Issue #207 (de-hardcoding wave 2, items 1-3). Defaults reproduce the prior
    # hardwired literals byte-for-byte; air-gap / mirror deployments repoint the
    # model IDs at a local mirror, and recall-ceiling tuning raises the caps.
    #   item 1 — model IDs (were hardwired in splade_backend/sema). Dense
    #   (BAAI/bge-m3) is deferred to a fast-follow: its codec is a process-wide
    #   shared singleton (get_shared_codec) + the passage cap must stay
    #   byte-identical between inline ingest and scripts/backfill_bgem3_v2.py.
    splade_model: str = "naver/splade-cocondenser-ensembledistil"
    sema_model: str = "all-MiniLM-L6-v2"
    #   item 3 — silent recall ceiling (SPLADE was hardwired content[:1000]):
    splade_content_cap: int = 1000   # chars SPLADE-encoded at ingest (storage/indexes.sync_splade_index)
    # #207 dense fast-follow: the passage cap deferred above. Was hardwired
    # PASSAGE_CHAR_CAP=2000 in backends/bgem3_codec.py; must stay byte-identical
    # across all three encode paths (inline ingest, query-side store encode,
    # scripts/backfill_bgem3_v2.py) — see BGEM3Codec module docstring.
    dense_passage_char_cap: int = 2000   # chars BGE-M3-encoded per passage
    #   item 2 — citation shortener anchors (were literal 'sources'/'Projects' in
    #   context_manager): last occurrence of each, in list order, is the strip
    #   point. Add your ingest roots here for correct <GENE src=...> shortening.
    citation_path_anchors: List[str] = field(
        default_factory=lambda: ["sources", "Projects"]
    )
    # Issue #207 item 5: deny-list extensibility. The built-in structural
    # deny list is the documented constant knowledge_store.DENY_PATTERNS
    # (+ LOCALE_DENY_PATTERN for non-English locale/ demotion, gated by
    # locale_demotion_enabled below). deny_list_extra entries are regex
    # fragments ORed onto the built-in list at KnowledgeStore construction
    # (same re.IGNORECASE, directory-boundary-anchored semantics — e.g.
    # r"[\\/]internal_only[\\/]"). Empty by default: byte-identical to the
    # prior hardwired behavior.
    deny_list_extra: List[str] = field(default_factory=list)
    # Non-English software locale/ directories (locale/de/, locale/ja/, ...)
    # are demoted to HETEROCHROMATIN at ingest by default (high-volume,
    # low-signal for typical English-primary retrieval workloads). Flip to
    # False for deployments that DO want non-English locale content ingested
    # at full tier. Default True reproduces the prior always-on behavior.
    locale_demotion_enabled: bool = True


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
    # 2026-06-12 default-honesty pass: stays FALSE on both sides. helix.toml
    # had flipped this true (2026-04-22 Stage-1 bench), but the evidence
    # roadmap measured SR at zero effect on retrieval outcomes, so the
    # shipped toml was aligned back to the code default (the inverse of the
    # usual toml-wins rule: measured-zero features default off).
    sr_enabled: bool = False
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
    filename_anchor_enabled: bool = True    # Stage-1 bench flip 2026-04-22: +12pp Dewey axis-2. Default aligned with shipped helix.toml (2026-06-12 default-honesty pass)
    filename_anchor_weight: float = 4.0     # Per-match boost (higher than Tier 1's 3.0)
    # BM25 shortlist post-filter (2026-04-22, research-review Pareto move 1).
    # When enabled, query_genes restricts its final ranking to documents that
    # cleared a BM25/FTS5 top-N pass — other tiers still accumulate scores,
    # but candidates BM25 would never surface are dropped before the sort.
    # Post-filter by design (isolates the ranking-set hypothesis from the
    # candidate-generation optimisation). Dark ship.
    bm25_shortlist_enabled: bool = True     # Keep on (2026-04-22 sprint): +1/8 ans_full, clean attribution. Default aligned with shipped helix.toml (2026-06-12 default-honesty pass)
    bm25_shortlist_size: int = 50           # BM25 top-N kept in the final ranking
    bm25_prefilter_enabled: bool = False
    bm25_prefilter_size: int = 200          # BM25 top-N fed into tier scoring
    # A4 / #205 candidate-pool-depth knob (2026-07-06). Overrides the Tier-3
    # FTS5 content-search fetch depth (legacy default: max_genes*4 = 48 at the
    # shipped max_genes=12). 0 = auto (legacy behavior). Widens ONLY the raw
    # candidate pool fed into tier scoring — the returned pool (max_genes*2)
    # and the delivery cap (max_genes) are unchanged, so a deeper pool cannot
    # trivially inflate gold_delivered. Lets the SIKE bedsweep isolate FTS
    # pool starvation (A4) from rank squeeze (B2) on the xl bed.
    fts5_candidate_depth: int = 0
    # Tier 5b: entity graph co-occurrence boost (Step 3C, 2026-05-08).
    # Documents sharing entity nodes with query terms get a score boost proportional
    # to entity overlap. Dark ship — flip to true for A/B.
    entity_graph_retrieval_enabled: bool = False
    # Step 4 — BGE-M3 dense vectors + ANN threshold-based dynamic document counts
    # (2026-05-08). Tier-0 PR-3 (2026-05-16) flipped this default to true:
    # PR-1 computes embedding_dense_v2 at ingest and PR-3 decoupled dense
    # recall from fusion_mode, so dense recall is now a shipped retrieval
    # signal in both additive and RRF mode.
    dense_embedding_enabled: bool = True
    # Stage 2 (2026-05-08): default dim raised from 256 -> 1024. Full BGE-M3
    # Matryoshka. dim=256 collapsed random-pair cosine to ~0.6, sabotaging
    # threshold semantics.
    dense_embedding_dim: int = 1024
    # #207 dense fast-follow (item 1, deferred from wave 2): the BGE-M3 model
    # ID was hardwired across three encode paths — inline ingest
    # (context_manager._get_dense_codec), query-side store encode
    # (KnowledgeStore._encode_dense_v2_blob via get_shared_codec), and offline
    # backfill (scripts/backfill_bgem3_v2.py). Default reproduces the prior
    # literal byte-for-byte; air-gap / mirror deployments repoint at a local
    # mirror. get_shared_codec's cache key is (model_name, dim, device), so a
    # repointed model_name still gets its own cached singleton.
    dense_model: str = "BAAI/bge-m3"
    # Stage 4 / Issue #139 (2026-05-18): recalibrated 0.35 -> 0.58 for dim=1024.
    # 0.35 was a dim-256 value. Measured over the dim-1024 BGE-M3 v2 vectors in
    # the bench fixtures (17.5k docs, 200k random unrelated doc pairs):
    # unrelated-pair cosine mean ~0.50, std ~0.066, p90 ~0.58. So 0.35 sat
    # below the p1 noise floor (~0.36) and never cut; 0.58 sits just above the
    # p90 of unrelated pairs.
    ann_similarity_threshold: float = 0.58
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
    # Issue #214 (2026-06-12): dense pool floor. The margin-over-random
    # calibration measures mu + sigma_mult*sigma over RANDOM gene pairs;
    # embedding anisotropy can push that bound ABOVE every real query-doc
    # cosine. Measured twice, independently: (a) cc-exchange
    # embedding-upgrade L1b — calibrated threshold 0.779 vs corpus max
    # query-doc cosine ~0.713 (golds 0.46-0.68), so 0/5000 pool docs cleared
    # the gate by dense; (b) a 480-question ERB run with 70.0% never-surfaced
    # golds (gold absent from top-10), matching an independent 67.2%
    # measurement. A threshold that admits ZERO dense candidates is
    # mis-calibration by definition, and pool membership is strictly upstream
    # of fusion ranking — no re-weighting can recover a candidate the gate
    # already dropped. When fewer than this many dense-scored candidates
    # survive the ANN threshold cut (but the dense leg HAD scored
    # candidates), the top-N dense hits by cosine are admitted into the pool
    # anyway; they then compete normally in fusion scoring. 0 disables
    # (legacy gate-only). See knowledge_store.apply_ann_gate.
    dense_pool_floor_genes: int = 8
    # Stage 2 (2026-05-08): dense recall pool size. Decoupled from
    # ann_threshold_max_genes (the final cut). 500 hits ~3% of an 18.9k
    # corpus per spec §4.
    dense_pool_size: int = 500
    # Stage 3 (2026-05-08): Reciprocal Rank Fusion accumulator.
    # Spec: docs/specs/2026-05-08-stage-3-rrf-fusion.md.
    # v(N+1) flip (2026-07-06, J-space roadmap council): default is now
    # "rrf" — each tier writes both raw scores AND ranks the tier output
    # through the Fuser; the final sort uses fused scores. SIKE Run-2
    # measured rrf > additive +12pp gold_delivered on xl (0.74 vs 0.62;
    # docs/benchmarks/2026-07-06-sike-run2-fts-depth-fusion.md) — the
    # additive path mis-scales dense (×16 semantic arm) against the FTS
    # bm25 cap (6.0). Set "additive" to restore the legacy
    # ``gene_scores += tier_score`` accumulator until v(N+2) removes it.
    # Issue #202: the per-tier weights below bind in BOTH fusion modes.
    # Under "additive" they are the tier coefficients/caps themselves
    # (defaults == the old inline literals); under "rrf" they are rank
    # post-multipliers. Under "rrf" the abstain gates run ratio-only
    # (pipeline/tier_logic.py skip_absolute_floors) because the absolute
    # floors were calibrated on additive scores.
    fusion_mode: str = "rrf"                # "rrf" | "additive" (legacy)
    rrf_k: int = 60                         # Cormack 2009 default
    # Issue #260 (2026-07-12): rank/confidence-gated RRF. At true corpus scale
    # (829K ERB blob) unconditional RRF let the dense arm's near-random deep-rank
    # signal (median gold rank 50,357 in a ~178K pool) demote a gold that lexical
    # ranked well — fused gd_id-given-pooled 0.156 (10/64) INVERTED below lexical
    # 0.333 (21/63) (docs/research/2026-07-11-overnight-bench-results.md P7').
    # The gate makes an arm's RRF contribution count only where its own evidence
    # is trustworthy: gate_top_m keeps only a tier's top-M ranks (scale-free,
    # honest across every arm); gate_min_score keeps only entries with raw arm
    # score >= floor. Ships INERT — with rrf_gate_enabled=False (or both sub-
    # levers at their 0 sentinels) fused scores are byte-identical to today. A
    # default flip is gated on a bench receipt (next window). Only bites arms
    # with a deep candidate pool (dense ~178K); lexical/tag pools (~50) are
    # naturally shallower than any sane M, so a uniform top_m leaves them intact.
    rrf_gate_enabled: bool = False          # master switch; False == byte-identical legacy RRF
    rrf_gate_top_m: int = 0                 # 0 = ungated; else a tier contributes RRF mass only for its top-M ranks
    # 0.0 = ungated; else a tier's entry contributes only when its raw arm score
    # >= this. NOTE: raw scores are non-commensurate across arms (FTS negative-bm25
    # vs BGE cosine vs tag {0,3}), so a single global float is only honest when the
    # operator knows the arm mix — prefer rrf_gate_top_m on a mixed-arm store
    # (issue #260 v1; a per-arm dict floor is deferred).
    rrf_gate_min_score: float = 0.0
    # Issue #255 (PR-2, 2026-07-10): post-fusion rerank combinator. Under
    # fusion_mode=="rrf" the four rerank classes (authority / sema_boost /
    # party_attr / access_rate) combine with the fused RRF score via this
    # operator. Default "additive" is byte-identical to the shipped
    # fused+rerank_additive block (DEFECT-1 carrier, audit §3) so this knob
    # ships inert; the alternatives are bench-gated on the 50-needle beds.
    # Design: docs/research/2026-07-09-scoring-combinator-exploration.md.
    #   "additive"   — final = fused + rerank (current behavior).
    #   "fused_tier" — each rerank class becomes a rank contribution
    #                  (tier_weight/(k+rank)); no exchange rate to hand-pick.
    #   "eps_band"   — rerank breaks ties only inside a relative fused-score
    #                  band of width rerank_band_delta (a ratio, scale-free).
    #   "off"        — pure fused ranking (rerank ignored; floor arm).
    rerank_combinator: str = "additive"     # additive | fused_tier | eps_band | off
    # eps_band relative tie-band width δ (ratio of the leader's fused score).
    rerank_band_delta: float = 0.05
    # fused_tier uniform per-class rank post-multiplier (single weight — a
    # per-class weight would re-introduce hand-picked exchange rates).
    rerank_tier_weight: float = 1.0
    # Issue #255 (classifier-gated combinator, 2026-07-12): per-query-class
    # rerank combinator override map {classifier_class: combinator_name}. The
    # stage-0 rule-based query classifier assigns each query a class
    # (arithmetic / factual / procedural / multi_hop / default); a populated
    # entry makes THAT class use its mapped combinator instead of the global
    # rerank_combinator above. An empty map => every query uses the global
    # combinator (byte-identical fallback). The design is per-class (not a
    # global flip) because the winning combinator is CORPUS-DEPENDENT: the
    # desk test found rerank additives are load-bearing on literal beds while
    # eps_band/off win the semantic 10k ERB bed (docs/research/2026-07-10-
    # rerank-combinator-desktest.md + the 2026-07-11 semantic-arm re-run).
    # GRADUATED 2026-07-16 on the knob-graduation receipt (PR #293,
    # docs/research/2026-07-16-knob-graduation-receipts.md /
    # issues/255#issuecomment-5005983077): {multi_hop: eps_band,
    # default: eps_band} vs the empty-map control replicated flat delivery
    # (gold_delivered byte-identical) with the median gold rank halved on
    # both semantic beds (10k 10→5, 50k 12→6); lift was confined to the
    # mapped classes and unmapped rows (xl literal, 37/50 needles) came back
    # byte-identical, so this is now the shipped default. Keys are validated
    # against the classifier class set and values against VALID_COMBINATORS
    # at load (RetrievalConfig.__post_init__); an unknown key or value is a
    # hard config error (fail loud at load, not silently at query time).
    # Classifier disabled => map ignored, global combinator used. Pass an
    # explicit empty dict ({}) in TOML to restore the pre-graduation
    # byte-identical global-combinator behavior.
    rerank_combinator_by_class: Dict[str, str] = field(
        default_factory=lambda: {"multi_hop": "eps_band", "default": "eps_band"}
    )
    # Issue #255 / audit §4 item 5 (2026-07-10): post-fusion BLEND layer mode.
    # The blend layer (cymatics 0.5 / harmonic_bin 1.5 / TCM 0.3) mutates
    # ``genome.last_query_scores`` AFTER fusion, on whatever scale the map
    # carries (DEFECT class 2d, audit §2d) — a mild nudge on additive scores,
    # an overwrite on RRF scores — and it also contaminates the ``[know]``
    # logistic inputs, which read the mutated map. GRADUATED 2026-07-13 on the
    # serving-profile receipt (docs/research/2026-07-12-blend-serving-receipt.md):
    # scale_relative replicated flat-to-positive on 5/6 serving cells and best-
    # on-10k-semantic, so it is now the default; off was REJECTED for serving
    # (delivery inverts on all 6 serving cells despite zeroing inversions).
    # Design + exact scale_relative mapping: helix_context/scoring/blend.py.
    #   "legacy"         — absolute additive blend (pre-graduation default;
    #                      BYTE-IDENTICAL to the original inline block. Set
    #                      explicitly to restore the old behavior).
    #                      DEPRECATED-FOR-REMOVAL (2026-07-13 council 3/3
    #                      CONCUR): condition-gated, not calendar-gated —
    #                      see helix_context/scoring/blend.py module
    #                      docstring for the four removal conditions.
    #                      Explicit selection now logs a one-time warning
    #                      (__post_init__ below).
    #   "scale_relative" — each absolute blend bonus b becomes a bounded
    #                      multiplier (1 + b/S_REF, S_REF a documented module
    #                      constant in blend.py) of the candidate's own score
    #                      (order-preserving under uniform rescale). DEFAULT
    #                      since 2026-07-13 — see the receipt doc above.
    #   "off"            — skip the blend mutations of last_query_scores entirely
    #                      (pure fused ranking; the rerank/truncation side effect
    #                      still runs). Clears the desk-test off-cell inversion
    #                      floor (docs/research/2026-07-10-rerank-combinator-desktest.md §5)
    #                      but REJECTED for serving: delivery inverts on all 6
    #                      serving cells (see the receipt doc above).
    blend_mode: str = "scale_relative"      # legacy (DEPRECATED-FOR-REMOVAL) | scale_relative (default) | off
    fts5_weight: float = 3.0                # cap-only in additive: cap = 2.0 × this (6.0)
    splade_weight: float = 3.5              # leading coeff == tier cap
    tag_exact_weight: float = 3.0           # current weight × match_count
    tag_prefix_weight: float = 1.5          # current weight × match_count
    # Issue #202: warm ΣĒMA boost (Tier 4 Mode A) weight — NEW knob; the
    # additive literal was sim·2.0·scale and the tier previously had no
    # weight knob at all (post-fusion additive under RRF, never fused).
    # Default == old literal, so untouched configs are bit-identical.
    sema_boost_weight: float = 2.0
    sema_cold_weight: float = 3.0           # current sim·3.0 multiplier
    lex_anchor_weight: float = 1.5          # idf coeff; cap = 2.0 × this (3.0)
    harmonic_weight: float = 1.0            # per-link weight; cap = 3.0 × this (3.0)
    entity_graph_weight: float = 0.5        # per-row bonus; cap = 4.0 × this (2.0)
    dense_weight: float = 1.0               # Stage 2 dense recall, RRF participant
    # Tier-0 PR-3 (2026-05-16): additive-mode dense merge weight. Under
    # fusion_mode == "additive" a dense hit's cosine is scaled by this
    # before entering the gene_scores accumulator. BM25-comparable
    # (tag_exact_weight is 3.0). Unused under RRF.
    #
    # Issue #203 (closed 2026-07-03): the real-query sweep
    # (``benchmarks/sweep_dense_additive_weight.py``, n=100 ERB queries
    # per bed) found recall@10 monotone INCREASING in this weight —
    # erb10k 0.58 (w=0) → 0.64 (w=6), erb50k 0.47 → 0.56, medium 0.23 →
    # 0.40 — with zero gold evictions at any weight. The #138 H10q
    # gold-eviction fear did not reproduce on enterprise-class queries.
    # 4.0 stands; the raise-to-6.0 decision is deferred to the #205
    # per-class retrieval profiles (w=6 may become the semantic-class
    # value rather than a global default). ``0.0`` flips dense
    # additively-off without disabling the dense write path or RRF
    # participation.
    dense_additive_weight: float = 4.0
    # Tier-0 review fix (2026-05-16): noise floor for the additive-mode
    # dense merge. A dense hit whose cosine is below this does not
    # contribute to gene_scores (it is still kept as a candidate with
    # negligible weight). Consistent with the cold tier's 0.15 min_cosine;
    # deliberately gentle so it removes only noise-grade hits. Unused under RRF.
    dense_additive_min_cosine: float = 0.15
    # Semantic-wiring arm (2026-06-02; PRD docs/prds/2026-06-02-semantic-wiring-arm.md).
    # When query_type=="semantic" AND env HELIX_SEMANTIC_ARM=1, the per-shard
    # dense term is scaled by semantic_dense_additive_weight (instead of
    # dense_additive_weight) AND routing broadens to all healthy shards. The two
    # fire together or not at all. Default-off (env unset) => byte-identical
    # baseline; lexical/tag/SPLADE tiers are never touched (additive KEEP-BOTH).
    semantic_dense_additive_weight: float = 16.0
    semantic_broaden_routing: bool = True
    pki_weight: float = 1.0                 # PKI tier, RRF participant
    # Note: filename_anchor_weight, sr_weight reuse their existing knobs above.

    # ── Sharded-retrieval fetch depth + co-activation budget (#222/#223) ──
    # These bind ONLY on the sharded read path (ShardRouter); blob mode never
    # constructs a router, so they are inert there. Threaded to the router via
    # open_read_source -> ShardedGenomeAdapter -> ShardRouter (mirrors
    # semantic_broaden_routing). Defaults reproduce the dark-shipped env-knob
    # behaviour byte-for-byte and keep the sharded merge identical to today.
    #
    # #222 per-shard fetch depth: the router fetches max_genes * multiplier
    #   candidates per shard before the cross-shard merge. multiplier=2.0 is
    #   the legacy flat 2× cut. scale_with_shards amplifies the multiplier by
    #   sqrt(n_shards) (clamped to 10×max_genes) so populous many-shard
    #   corpora oversample each shard deeply enough that a mid-shard gold
    #   survives to the merge. HELIX_SHARD_FETCH_FACTOR (int) overrides.
    shard_fetch_multiplier: float = 2.0
    shard_fetch_scale_with_shards: bool = False
    # #223 co-activation reserved budget: reserve up to N of the final
    #   2×max_genes output slots for newly graph-promoted (co-activated) docs
    #   so a link-discounted gold isn't truncated by lexical incumbents.
    #   0 = legacy (no reservation). HELIX_SHARD_COACT_RESERVE (int) overrides.
    #   coact_link_boost is the discount a linked doc enters at (× its source
    #   doc's corrected score); 0.5 == the shipped constant.
    coact_reserved_slots: int = 0
    coact_link_boost: float = 0.5
    # ── #121 doc-type boost mode (#264) ───────────────────────────────
    # Router-only. Controls how the README/CLAUDE/INDEX summary-doc lift
    # (#121) is applied on the cross-shard merge. DEFAULT-INERT: "additive"
    # reproduces the shipped fixed ×DOC_TYPE_BOOST (1.15) post-multiply on
    # the IDF-corrected score, byte-for-byte. The 1.15× multiplier was
    # calibrated on additive/BM25-scale per-shard margins; production
    # per-shard Genomes now score in RRF (fusion_mode="rrf"), which
    # compresses intra-shard margins to ~1.6% so the fixed multiplier
    # becomes decisive on nearly every candidate pair (#264). Two honest
    # RRF-native alternatives, both bench-gated behind this knob:
    #   "off"  — skip the boost entirely (the flip case resolves because
    #            the unboosted impl file already out-ranks the README).
    #   "rank" — apply the boost as a rank-domain tier input to the
    #            cross-shard Fuser (#264 candidate b) instead of a
    #            magnitude multiply; final merge sorts primarily by the
    #            rank-fused score so a summary doc can only reorder genuine
    #            rank near-ties, never leapfrog a doc that dominates the
    #            shard ranks. Scale-free under both fusion modes.
    # Inert on blob/single-shard paths (no ShardRouter constructed).
    doc_type_boost_mode: str = "additive"

    def __post_init__(self) -> None:
        # #264: validate the doc-type boost mode now so a typo in
        # [retrieval] doc_type_boost_mode fails fast at config load rather
        # than silently at merge time. The ShardRouter re-validates the
        # fanned kwarg for the direct-construction path.
        if self.doc_type_boost_mode not in ("additive", "rank", "off"):
            raise ValueError(
                "[retrieval] doc_type_boost_mode must be 'additive', 'rank' "
                f"or 'off', got {self.doc_type_boost_mode!r}"
            )

        # Issue #255 (classifier-gated combinator): fail loud at config load on
        # a typo'd class key or combinator name in rerank_combinator_by_class,
        # rather than silently at query time. Empty map (the default) is a
        # no-op, so untouched configs never enter this branch. Lazy imports
        # avoid an import cycle (config is imported very early).
        if self.rerank_combinator_by_class:
            from .retrieval.query_classifier import VALID_QUERY_CLASSES
            from .retrieval.rerank_combinators import VALID_COMBINATORS
            for _cls, _comb in self.rerank_combinator_by_class.items():
                if _cls not in VALID_QUERY_CLASSES:
                    raise ValueError(
                        "[retrieval] rerank_combinator_by_class: unknown query "
                        f"class {_cls!r} (expected one of {VALID_QUERY_CLASSES})"
                    )
                if _comb not in VALID_COMBINATORS:
                    raise ValueError(
                        "[retrieval] rerank_combinator_by_class["
                        f"{_cls!r}] = {_comb!r}: unknown combinator "
                        f"(expected one of {VALID_COMBINATORS})"
                    )

        # blend_mode="legacy" deprecation (2026-07-13 council 3/3 CONCUR,
        # follow-up to PR #282's default flip legacy -> scale_relative on
        # the serving-profile receipt,
        # docs/research/2026-07-12-blend-serving-receipt.md). scale_relative
        # is the shipped default, so the only way to land on "legacy" here
        # is an explicit [retrieval] blend_mode = "legacy" in TOML or a
        # direct kwarg. Warn ONCE at this config-load seam — this runs once
        # per RetrievalConfig construction, not per query. Removal
        # conditions: helix_context/scoring/blend.py module docstring.
        if self.blend_mode == "legacy":
            log.warning(
                "blend_mode='legacy' is deprecated (strictly dominated in "
                "desk/overnight/serving receipts, 2026-07-12 serving-profile "
                "A/B) and scheduled for removal; use 'scale_relative' "
                "(default) or 'off' (lexical-only profiles)."
            )


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

    Issue #207 item 4 (default-inert): ``ratio_threshold`` /
    ``ratio_threshold_rrf_norm`` expose the previously hard-coded ABSTAIN
    ratio gates in ``pipeline/tier_logic.py``. NOTE: the abstain *absolute*
    floors (``AbstainClassFloors``) were additive-calibrated and are bypassed
    under RRF fusion — the ratio gate runs alone there (issue #115). Exposing
    these thresholds as knobs does NOT recalibrate them; recalibration is
    #287's scope.
    """
    mode: str = "global"
    ratio_threshold: float = 1.8  # Issue #207 item 4: ABSTAIN ratio gate under additive fusion (legacy top/mean ratio). Prior literal ABSTAIN_RATIO_THRESHOLD=1.8 in pipeline/tier_logic.py. Additive-calibrated; exposing it does not recalibrate it (#287).
    ratio_threshold_rrf_norm: float = 1.5  # Issue #207 item 4: ABSTAIN ratio gate under RRF fusion (baseline-subtracted norm ratio, issue #115). Prior literal ABSTAIN_RATIO_THRESHOLD_RRF_NORM=1.5 in pipeline/tier_logic.py. Under RRF the absolute floors are bypassed and this gate runs alone.
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


# Ship-time [know] defaults (Stage 6 spec §3 + Stage 7 b5). These literals
# MUST stay equal to scoring/know_calibration.py's DEFAULT_* constants —
# tests/test_config_default_honesty.py cross-checks the two modules so they
# cannot drift apart again.
_KNOW_DEFAULT_BETAS: tuple = (-2.0, 2.0, 1.5, 0.7, 1.8, 1.5)
_KNOW_DEFAULT_S_REF: float = 1.0
_KNOW_DEFAULT_G_REF: float = 0.5
_KNOW_DEFAULT_EMIT_FLOOR: float = 0.55
_KNOW_DEFAULT_STALE_AFTER_DAYS: int = 30


@dataclass
class KnowConfig:
    """[know] — Stage 6/7 KnowBlock confidence logistic + staleness window.

    Folded into the config system on 2026-06-12 (default-honesty pass):
    these keys were previously parsed OUT-OF-BAND by
    ``scoring/know_calibration.py``'s shadow loader, so /health, docs and
    the loader disagreed about what the real knobs were (CLAUDE.md
    advertised non-existent ``confidence_floor`` / ``margin_threshold``).
    Field names/defaults == the shadow loader's ship-time values
    (spec docs/specs/2026-05-08-stage-6-know-miss-blocks.md §3, §11).

    ``scoring.know_calibration.load_calibration_from_toml`` now delegates
    here; ``calibration_from_config`` converts this block into the frozen
    ``KnowCalibration`` bundle the logistic consumes.
    """
    # Probability floor below which no KnowBlock is emitted (falls through
    # to MissBlock(reason="sparse")).
    emit_floor: float = _KNOW_DEFAULT_EMIT_FLOOR
    # tanh feature-scale references for top_score / score_gap.
    s_ref: float = _KNOW_DEFAULT_S_REF
    g_ref: float = _KNOW_DEFAULT_G_REF
    # (b0, b1..b5) — intercept + 5 feature coefficients (Stage 7 added b5
    # for freshness_min). Malformed/odd-length lists soft-fail to defaults
    # at load time so a bad calibration write can never break retrieval.
    betas: List[float] = field(default_factory=lambda: list(_KNOW_DEFAULT_BETAS))
    # Written by scripts/calibrate_know_confidence.py; None = uncalibrated.
    calibrated_at: Optional[str] = None
    calibrated_on_n: Optional[int] = None
    # Stage 4 (spec §9, issue #63): age in days after which the /context
    # response flags ``calibration_stale``.
    stale_after_days: int = _KNOW_DEFAULT_STALE_AFTER_DAYS


@dataclass
class PLRConfig:
    """Stacked PLR query-confidence head (STATISTICAL_FUSION.md §C3).

    Attaches a `plr_confidence` log-odds signal to /context/packet responses
    when a trained artifact is on disk. Bench-gated on 2026-05-12 (#74) and
    enabled by default since the 2026-06-12 default-honesty pass (aligned
    with shipped helix.toml); soft-fails to "PLR unavailable" when no
    artifact exists, so a fresh install pays nothing.

    The current artifact is a **query-quality head** (same score for all documents
    in a retrieval) rather than the per-(q,g) ranker the spec originally
    described. See `helix_context/fusion_plr.py` docstring and
    STATISTICAL_FUSION.md §C3 addendum for the trade-off.
    """
    enabled: bool = True  # default aligned with shipped helix.toml (2026-06-12 default-honesty pass) — bench-gated #74; soft-no-op without artifact
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
    ``enabled`` defaults on (2026-06-12 default-honesty pass, aligned with
    shipped helix.toml) — the launcher adopts/spawns the proxy and no-ops
    when the [codec] extra isn't installed. ``route_upstream`` stays off
    so a fresh install never silently rewrites the upstream to a proxy
    that isn't actually running. The
    ``HELIX_HEADROOM_ROUTE_UPSTREAM_AUTO`` env var (truthy → force on,
    falsy → force off, unset → defer to config) continues to work as a
    per-launch override.
    """
    enabled: bool = True                # Master switch; false = do nothing. Default aligned with shipped helix.toml (2026-06-12 default-honesty pass)
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
    # #219 slice 2: when true (default), heavy encoders (ΣĒMA MiniLM,
    # DeBERTa rerank/splice) are armed lazily and load on FIRST USE; when
    # false, restore the pre-slice eager warmup at manager init for
    # operators who want first-query latency paid at boot. SPLADE /
    # BGE-M3 / spaCy were already first-use-lazy and ignore this knob.
    lazy_encoders: bool = True


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
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
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
    # Stage 6 KnowBlock calibration — folded in from the know_calibration
    # shadow loader (2026-06-12 default-honesty pass).
    know: KnowConfig = field(default_factory=KnowConfig)
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


def _positive_float(
    section: str, key: str, raw_section: Dict[str, Any], default: float
) -> float:
    """Read a float knob that must be strictly positive; warn + fall back.

    Issue #207 item 4: the tier/abstain ratio-and-fraction knobs would
    silently break tiering at 0 or below (every query TIGHT, or the whole
    candidate set floor-gated), so a bad value falls back to the shipped
    default instead of propagating — mirroring the ``[abstain].mode``
    warn-and-fallback style.
    """
    if key not in raw_section:
        return default
    value = raw_section[key]
    try:
        v = float(value)
    except (TypeError, ValueError):
        log.warning(
            "[%s].%s=%r is not a number; using default %s",
            section, key, value, default,
        )
        return default
    if v <= 0:
        log.warning(
            "[%s].%s=%s must be > 0; using default %s", section, key, v, default,
        )
        return default
    return v


def _apply_env_overrides(cfg: HelixConfig) -> HelixConfig:
    """Apply HELIX_* env overrides to *cfg* in place and return it.

    Documented precedence is env > toml > default, so this must run on
    EVERY load path — success, missing file, and malformed TOML alike
    (the fallback paths used to return bare defaults, silently dropping
    HELIX_GENOME_PATH / HELIX_SERVER_UPSTREAM).
    """
    # KnowledgeStore path override — lets sharded vs monolithic servers coexist
    # on different ports without duplicating helix.toml. Typical use:
    # ``HELIX_GENOME_PATH=genomes/main.genome.db HELIX_USE_SHARDS=1`` for a
    # sharded bench server on a side port; defaults still serve monolithic.
    if os.environ.get("HELIX_GENOME_PATH"):
        cfg.genome.path = os.environ["HELIX_GENOME_PATH"]

    # Server env overrides — lets launchers/profiles redirect Helix to a
    # different chat upstream without rewriting helix.toml on disk.
    if os.environ.get("HELIX_BENCH_ENABLED"):
        cfg.server.bench_enabled = os.environ["HELIX_BENCH_ENABLED"].strip().lower() in (
            "1", "true", "yes", "on",
        )
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
    return cfg


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
        return _apply_env_overrides(HelixConfig())

    with open(config_path, "rb") as f:
        try:
            raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            log.error("helix.toml is malformed (%s) — using defaults", exc)
            return _apply_env_overrides(HelixConfig())

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
            decoder_mode_overrides=dict(b.get(
                "decoder_mode_overrides", cfg.budget.decoder_mode_overrides)),
            legibility_enabled=bool(b.get("legibility_enabled", cfg.budget.legibility_enabled)),
            session_delivery_enabled=bool(b.get("session_delivery_enabled", cfg.budget.session_delivery_enabled)),
            abstain_enabled=bool(b.get("abstain_enabled", cfg.budget.abstain_enabled)),
            foveated_enabled=bool(b.get("foveated_enabled", cfg.budget.foveated_enabled)),
            foveated_alpha=float(b.get("foveated_alpha", cfg.budget.foveated_alpha)),
            foveated_c_min=float(b.get("foveated_c_min", cfg.budget.foveated_c_min)),
            foveated_base_chars=int(b.get("foveated_base_chars", cfg.budget.foveated_base_chars)),
            slate_char_budget=int(b.get("slate_char_budget", cfg.budget.slate_char_budget)),
            splice_target_chars=int(b.get("splice_target_chars", cfg.budget.splice_target_chars)),
            # Issue #207 item 4: tier knobs (default-inert; must be > 0).
            tier_tight_ratio=_positive_float(
                "budget", "tier_tight_ratio", b, cfg.budget.tier_tight_ratio),
            tier_focused_ratio=_positive_float(
                "budget", "tier_focused_ratio", b, cfg.budget.tier_focused_ratio),
            tier_hard_floor_frac=_positive_float(
                "budget", "tier_hard_floor_frac", b, cfg.budget.tier_hard_floor_frac),
            tier_lagrange_frac=_positive_float(
                "budget", "tier_lagrange_frac", b, cfg.budget.tier_lagrange_frac),
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
            bench_enabled=bool(s.get("bench_enabled", cfg.server.bench_enabled)),
            bench_port=int(s.get("bench_port", cfg.server.bench_port)),
            bench_genome_path=s.get(
                "bench_genome_path", cfg.server.bench_genome_path,
            ),
        )

    # HELIX_GENOME_PATH / HELIX_SERVER_* overrides (env > toml > default).
    _apply_env_overrides(cfg)

    # Telemetry — toml defaults for the HELIX_OTEL_* knobs. Deliberately NO
    # env handling here (see TelemetryConfig docstring): otel.py resolves
    # env > toml > default at setup_telemetry time.
    if "telemetry" in raw:
        t = raw["telemetry"]
        _warn_unknown("telemetry", t, TelemetryConfig)
        cfg.telemetry = TelemetryConfig(
            enabled=bool(t.get("enabled", cfg.telemetry.enabled)),
            endpoint=str(t.get("endpoint", cfg.telemetry.endpoint)),
            insecure=bool(t.get("insecure", cfg.telemetry.insecure)),
            sampler_ratio=float(t.get("sampler_ratio", cfg.telemetry.sampler_ratio)),
            redact_query=bool(t.get("redact_query", cfg.telemetry.redact_query)),
            logs_enabled=bool(t.get("logs_enabled", cfg.telemetry.logs_enabled)),
            logs_level=str(t.get("logs_level", cfg.telemetry.logs_level)),
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
            dense_embed_on_ingest=i.get(
                "dense_embed_on_ingest", cfg.ingestion.dense_embed_on_ingest
            ),
            sema_embed_on_ingest=i.get(
                "sema_embed_on_ingest", cfg.ingestion.sema_embed_on_ingest
            ),
            symbol_graph=i.get("symbol_graph", cfg.ingestion.symbol_graph),
            # Issue #164: size-aware SPLADE auto-toggle thresholds.
            splade_auto_enable_below_genes=int(i.get(
                "splade_auto_enable_below_genes",
                cfg.ingestion.splade_auto_enable_below_genes,
            )),
            splade_auto_disable_above_genes=int(i.get(
                "splade_auto_disable_above_genes",
                cfg.ingestion.splade_auto_disable_above_genes,
            )),
            # Issue #207: de-hardcoded model IDs + SPLADE cap + citation anchors.
            splade_model=i.get("splade_model", cfg.ingestion.splade_model),
            sema_model=i.get("sema_model", cfg.ingestion.sema_model),
            splade_content_cap=int(i.get(
                "splade_content_cap", cfg.ingestion.splade_content_cap)),
            citation_path_anchors=list(i.get(
                "citation_path_anchors", cfg.ingestion.citation_path_anchors)),
            # #207 dense fast-follow: passage cap shared across all three
            # BGE-M3 encode paths.
            dense_passage_char_cap=int(i.get(
                "dense_passage_char_cap", cfg.ingestion.dense_passage_char_cap)),
            # Issue #207 item 5: deny-list extensibility.
            deny_list_extra=list(i.get(
                "deny_list_extra", cfg.ingestion.deny_list_extra)),
            locale_demotion_enabled=bool(i.get(
                "locale_demotion_enabled", cfg.ingestion.locale_demotion_enabled)),
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
            fts5_candidate_depth=int(r.get("fts5_candidate_depth", cfg.retrieval.fts5_candidate_depth)),
            entity_graph_retrieval_enabled=bool(r.get("entity_graph_retrieval_enabled", cfg.retrieval.entity_graph_retrieval_enabled)),
            dense_embedding_enabled=bool(r.get("dense_embedding_enabled", cfg.retrieval.dense_embedding_enabled)),
            dense_embedding_dim=int(r.get("dense_embedding_dim", cfg.retrieval.dense_embedding_dim)),
            # #207 dense fast-follow: BGE-M3 model ID.
            dense_model=str(r.get("dense_model", cfg.retrieval.dense_model)),
            ann_similarity_threshold=float(r.get("ann_similarity_threshold", cfg.retrieval.ann_similarity_threshold)),
            ann_threshold_min_genes=int(r.get("ann_threshold_min_genes", cfg.retrieval.ann_threshold_min_genes)),
            ann_threshold_max_genes=int(r.get("ann_threshold_max_genes", cfg.retrieval.ann_threshold_max_genes)),
            # Stage 4 (2026-05-08) margin-over-random calibration mode.
            ann_threshold_mode=str(r.get("ann_threshold_mode", cfg.retrieval.ann_threshold_mode)),
            ann_threshold_sigma_multiplier=float(r.get(
                "ann_threshold_sigma_multiplier",
                cfg.retrieval.ann_threshold_sigma_multiplier,
            )),
            # Issue #214: dense pool floor — graceful degradation when a
            # mis-calibrated ANN threshold gates the dense leg to zero.
            dense_pool_floor_genes=int(r.get(
                "dense_pool_floor_genes",
                cfg.retrieval.dense_pool_floor_genes,
            )),
            # Stage 2 (2026-05-08): dense recall pool size, decoupled from final cut.
            dense_pool_size=int(r.get("dense_pool_size", cfg.retrieval.dense_pool_size)),
            # Stage 3 (2026-05-08): RRF fusion. Default "additive" preserves
            # pre-Stage-3 behavior byte-for-byte. Flip to "rrf" for the new
            # rank-fusion path. Spec §7 deprecation timeline keeps both
            # implementations alive for one release.
            fusion_mode=str(r.get("fusion_mode", cfg.retrieval.fusion_mode)),
            rrf_k=int(r.get("rrf_k", cfg.retrieval.rrf_k)),
            # Issue #260: rank/confidence-gated RRF. Default-inert (enabled=False,
            # 0 / 0.0 sentinels) => byte-identical fused scores.
            rrf_gate_enabled=bool(r.get("rrf_gate_enabled", cfg.retrieval.rrf_gate_enabled)),
            rrf_gate_top_m=int(r.get("rrf_gate_top_m", cfg.retrieval.rrf_gate_top_m)),
            rrf_gate_min_score=float(r.get("rrf_gate_min_score", cfg.retrieval.rrf_gate_min_score)),
            # Issue #255 (PR-2): post-fusion rerank combinator + its two
            # scale-free knobs. Default "additive" is byte-identical to the
            # shipped fused+rerank_additive finalization.
            rerank_combinator=str(r.get("rerank_combinator", cfg.retrieval.rerank_combinator)),
            rerank_band_delta=float(r.get("rerank_band_delta", cfg.retrieval.rerank_band_delta)),
            rerank_tier_weight=float(r.get("rerank_tier_weight", cfg.retrieval.rerank_tier_weight)),
            # Issue #255 (classifier-gated combinator): per-query-class combinator
            # override map. Default GRADUATED 2026-07-16 (PR #293 receipt) to
            # {multi_hop: eps_band, default: eps_band}; an explicit empty map
            # in TOML still falls back to the global combinator for every
            # class (byte-identical pre-graduation behavior). Validated in
            # RetrievalConfig.__post_init__.
            rerank_combinator_by_class=dict(
                r.get(
                    "rerank_combinator_by_class",
                    cfg.retrieval.rerank_combinator_by_class,
                )
                or {}
            ),
            # Issue #255 / audit §4 item 5: post-fusion blend layer mode.
            # Graduated 2026-07-13 to "scale_relative" (serving-profile
            # receipt); see the field docstring above for the full record.
            blend_mode=str(r.get("blend_mode", cfg.retrieval.blend_mode)),
            fts5_weight=float(r.get("fts5_weight", cfg.retrieval.fts5_weight)),
            splade_weight=float(r.get("splade_weight", cfg.retrieval.splade_weight)),
            tag_exact_weight=float(r.get("tag_exact_weight", cfg.retrieval.tag_exact_weight)),
            tag_prefix_weight=float(r.get("tag_prefix_weight", cfg.retrieval.tag_prefix_weight)),
            # Issue #202: warm ΣĒMA boost knob (new).
            sema_boost_weight=float(r.get("sema_boost_weight", cfg.retrieval.sema_boost_weight)),
            sema_cold_weight=float(r.get("sema_cold_weight", cfg.retrieval.sema_cold_weight)),
            lex_anchor_weight=float(r.get("lex_anchor_weight", cfg.retrieval.lex_anchor_weight)),
            harmonic_weight=float(r.get("harmonic_weight", cfg.retrieval.harmonic_weight)),
            entity_graph_weight=float(r.get("entity_graph_weight", cfg.retrieval.entity_graph_weight)),
            dense_weight=float(r.get("dense_weight", cfg.retrieval.dense_weight)),
            # Tier-0 PR-3 (2026-05-16): additive-mode dense merge weight.
            dense_additive_weight=float(r.get("dense_additive_weight", cfg.retrieval.dense_additive_weight)),
            # Tier-0 review fix (2026-05-16): additive-mode dense merge noise floor.
            dense_additive_min_cosine=float(r.get("dense_additive_min_cosine", cfg.retrieval.dense_additive_min_cosine)),
            # Semantic-wiring arm (2026-06-02).
            semantic_dense_additive_weight=float(r.get("semantic_dense_additive_weight", cfg.retrieval.semantic_dense_additive_weight)),
            semantic_broaden_routing=bool(r.get("semantic_broaden_routing", cfg.retrieval.semantic_broaden_routing)),
            pki_weight=float(r.get("pki_weight", cfg.retrieval.pki_weight)),
            # Issues #222/#223: sharded per-shard fetch depth + co-activation
            # reserved budget. Router-only knobs (blob mode ignores them);
            # defaults reproduce the dark-shipped env-knob behaviour.
            shard_fetch_multiplier=float(r.get("shard_fetch_multiplier", cfg.retrieval.shard_fetch_multiplier)),
            shard_fetch_scale_with_shards=bool(r.get("shard_fetch_scale_with_shards", cfg.retrieval.shard_fetch_scale_with_shards)),
            coact_reserved_slots=int(r.get("coact_reserved_slots", cfg.retrieval.coact_reserved_slots)),
            coact_link_boost=float(r.get("coact_link_boost", cfg.retrieval.coact_link_boost)),
            # #264: doc-type boost mode (default-inert "additive").
            doc_type_boost_mode=str(r.get("doc_type_boost_mode", cfg.retrieval.doc_type_boost_mode)),
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
            # Issue #207 item 4: ABSTAIN ratio-gate knobs (default-inert;
            # must be > 0). Scalar keys — never mistaken for class
            # sub-tables by the discovery loop below (dict check).
            ratio_threshold = _positive_float(
                "abstain", "ratio_threshold", a, AbstainConfig.ratio_threshold)
            ratio_threshold_rrf_norm = _positive_float(
                "abstain", "ratio_threshold_rrf_norm", a,
                AbstainConfig.ratio_threshold_rrf_norm)
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
            cfg.abstain = AbstainConfig(
                mode=mode,
                ratio_threshold=ratio_threshold,
                ratio_threshold_rrf_norm=ratio_threshold_rrf_norm,
                per_class=per_class,
            )

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

    # Know — Stage 6 KnowBlock confidence logistic (2026-06-12: folded in
    # from the know_calibration shadow loader). Per-field soft-fail mirrors
    # the old loader exactly: a malformed calibration write must never be
    # able to break startup or retrieval, so bad betas / stale_after_days
    # fall back to ship-time defaults with a WARNING instead of raising.
    if "know" in raw:
        k = raw["know"]
        _warn_unknown("know", k, KnowConfig)
        betas_raw = k.get("betas", list(_KNOW_DEFAULT_BETAS))
        try:
            betas = [float(b) for b in betas_raw]
        except (TypeError, ValueError):
            log.warning("[know] betas is malformed; using defaults")
            betas = list(_KNOW_DEFAULT_BETAS)
        if len(betas) != len(_KNOW_DEFAULT_BETAS):
            log.warning(
                "[know] betas length %d != expected %d; using defaults",
                len(betas), len(_KNOW_DEFAULT_BETAS),
            )
            betas = list(_KNOW_DEFAULT_BETAS)
        try:
            stale_after_days = int(
                k.get("stale_after_days", _KNOW_DEFAULT_STALE_AFTER_DAYS)
            )
            if stale_after_days < 0:
                raise ValueError("negative")
        except (TypeError, ValueError):
            log.warning(
                "[know] stale_after_days is malformed; using default %d",
                _KNOW_DEFAULT_STALE_AFTER_DAYS,
            )
            stale_after_days = _KNOW_DEFAULT_STALE_AFTER_DAYS

        def _know_float(key: str, default: float) -> float:
            try:
                return float(k.get(key, default))
            except (TypeError, ValueError):
                log.warning("[know] %s is malformed; using default %s", key, default)
                return default

        cfg.know = KnowConfig(
            emit_floor=_know_float("emit_floor", _KNOW_DEFAULT_EMIT_FLOOR),
            s_ref=_know_float("s_ref", _KNOW_DEFAULT_S_REF),
            g_ref=_know_float("g_ref", _KNOW_DEFAULT_G_REF),
            betas=betas,
            calibrated_at=(
                str(k["calibrated_at"])
                if k.get("calibrated_at") is not None
                else None
            ),
            calibrated_on_n=(
                int(k["calibrated_on_n"])
                if k.get("calibrated_on_n") is not None
                else None
            ),
            stale_after_days=stale_after_days,
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
        lazy_encoders=bool(hw.get("lazy_encoders", True)),
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
