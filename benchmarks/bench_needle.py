r"""
Benchmark: Needle in a Haystack

The standard test for context systems: can you find a specific fact
buried in a large knowledge base?

Plants 10 "needles" (specific facts) across ingested content,
then queries for each one. Measures:
  - Retrieval rate (did the genome express the right gene?)
  - Answer accuracy (did the model answer correctly?)
  - Latency per retrieval

This is the benchmark KV cache papers and TurboQuant use to show
information retention at various compression ratios.

Usage:
    python benchmarks/bench_needle.py
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _citations  # noqa: E402

HELIX_URL = os.environ.get("HELIX_URL", "http://127.0.0.1:11437")

# Needles: specific facts that exist in the ingested content
# Each has a query and the expected answer substring
# Each needle has `gold_source`: a list of case-insensitive path
# substrings identifying the source file(s) that contain the answer.
# `found_in_context` checks that one of these sources is in the
# delivered gene set AND its block body contains an accept substring.
# Payload-wide substring matches are retained as `false_positive_substring`
# diagnostics. Waude diagnostic 2026-04-17 showed the old pure-substring
# check inflated delivery by counting URL port numbers and compression
# metadata as hits.
#
# Multi-valid-gold curation (2026-05-14): `gold_source` is an ANY-match
# list -- if ANY entry matches a delivered citation, the needle counts
# as gold-delivered. The historically labeled gold (the file in `source`)
# is preserved as the first list entry so older JSONL captures remain
# comparable. Per-needle rationale in docs/benchmarks/MULTI_VALID_GOLD.md.
#
# DO NOT add `helix-context/docs/benchmarks/BENCHMARKS.md` (the bench
# answer-key) to any needle's gold_source -- it would inflate the metric
# circularly.
NEEDLES = [
    {
        "name": "helix_port",
        "query": "What port does the Helix proxy server listen on?",
        "expected": "11437",
        "accept": ["11437"],
        "source": "helix-context/helix.toml",
        "gold_source": [
            "helix-context/helix.toml",
            "helix-context/README.md",
            "helix-context/CLAUDE.md",
            "helix-context/docs/SETUP.md",
            "helix-context/docs/TROUBLESHOOTING.md",
            "helix-context/docs/api/endpoints.md",
        ],
    },
    {
        "name": "scorerift_threshold",
        "query": "What is the divergence threshold that triggers alerts in ScoreRift?",
        "expected": "0.15",
        "accept": ["0.15", ".15"],
        "source": "two-brain-audit/README.md",
        "gold_source": [
            "two-brain-audit/README.md",
            "two-brain-audit/docs/QUICKSTART.md",
            "two-brain-audit/src/scorerift/reconciler.py",
            "two-brain-audit/src/scorerift/engine.py",
        ],
    },
    {
        "name": "biged_skills_count",
        "query": "How many skills does the BigEd fleet have?",
        "expected": "125",
        "accept": ["125", "129"],  # count changes between versions
        "source": "Education/CLAUDE.md",
        "gold_source": [
            "Education/CLAUDE.md",
            "Education/FRAMEWORK_BLUEPRINT.md",
            "Education/ROADMAP.md",
            "Education/fleet/CLAUDE.md",
            "Education/fleet/knowledge/wiki/overview.md",
            "Education/fleet/knowledge/wiki/architecture.md",
        ],
    },
    {
        "name": "bookkeeper_monetary",
        "query": "What type should be used for monetary values in BookKeeper instead of float?",
        "expected": "Decimal",
        "accept": ["decimal", "Decimal"],
        "source": "BookKeeper/CLAUDE.md",
        "gold_source": [
            "BookKeeper/CLAUDE.md",
            "BookKeeper/docs/planning/GAPS.md",
            "BookKeeper/bookkeeper/storage/db.py",
        ],
    },
    {
        "name": "helix_pipeline_steps",
        "query": "How many steps are in the Helix expression pipeline?",
        "expected": "6",
        "accept": ["6", "six"],
        "source": "helix-context/README.md",
        "gold_source": [
            "helix-context/CLAUDE.md",
            "helix-context/README.md",
        ],
    },
    {
        "name": "biged_rust_binary_size",
        "query": "What is the binary size of the Rust BigEd build in MB?",
        "expected": "11",
        "accept": ["11", "11mb", "11 mb"],
        "source": "Education/biged-rs/README.md",
        "gold_source": [
            "Education/biged-rs/README.md",
            "Education/biged-rs/DEPLOYMENT.md",
            "Education/fleet/knowledge/wiki/architecture.md",
        ],
    },
    {
        "name": "genome_compression_target",
        "query": "What is the target compression ratio for Helix Context?",
        "expected": "5x",
        "accept": ["5x", "5:1", "5 to 1"],
        "source": "helix-context design spec",
        "gold_source": [
            "helix-context/README.md",
            "helix-context/docs",
            "helix-context/BENCHMARK_NOTES.md",
        ],
    },
    {
        "name": "scorerift_preset_dimensions",
        "query": "How many dimensions does the Python preset in ScoreRift check?",
        "expected": "8",
        "accept": ["8", "eight"],
        "source": "two-brain-audit/README.md",
        "gold_source": [
            "two-brain-audit/README.md",
            "two-brain-audit/docs/ARCHITECTURE.md",
            "two-brain-audit/src/scorerift/presets/python_project.py",
        ],
    },
    {
        "name": "helix_ribosome_budget",
        "query": "How many tokens are allocated for the ribosome decoder prompt?",
        "expected": "3000",
        "accept": ["3000", "3k", "3,000"],
        "source": "helix-context design spec",
        "gold_source": [
            "helix-context/helix.toml",
            "helix-context/README.md",
            "helix-context/docs/config-reference.md",
        ],
    },
    {
        "name": "biged_default_model",
        "query": "What is the default local model used by BigEd for conductor tasks?",
        "expected": "qwen3",
        "accept": ["qwen3", "qwen3:4b", "qwen"],
        "source": "Education/CLAUDE.md",
        "gold_source": [
            "Education/CLAUDE.md",
            "Education/FRAMEWORK_BLUEPRINT.md",
            "Education/OPERATIONS.md",
            "Education/fleet/fleet.toml",
        ],
    },
    # ─── 2026-05-15 N=50 expansion (PR feat/bench-needles-50) ────────────
    # See bench_claude_matrix.py and docs/benchmarks/MULTI_VALID_GOLD.md
    # for the curation policy. Each fact is verified to live in the
    # historically-labeled gold source first; other valid sources follow.

    # ─── BookKeeper (6) ───────────────────────────────────────────────
    {
        "name": "bookkeeper_dashboard_port",
        "query": "What port does the BookKeeper web dashboard listen on by default?",
        "expected": "8080",
        "accept": ["8080", "port 8080"],
        "source": "BookKeeper/bookkeeper.toml",
        "gold_source": [
            "BookKeeper/bookkeeper.toml",
            "BookKeeper/README.md",
            "BookKeeper/docs/TENANCY_QUICKSTART.md",
        ],
    },
    {
        "name": "bookkeeper_1099_threshold",
        "query": "What dollar threshold triggers 1099 tracking and W-9 requirement in BookKeeper?",
        "expected": "600",
        "accept": ["600", "$600"],
        "source": "BookKeeper/bookkeeper.toml",
        "gold_source": [
            "BookKeeper/bookkeeper.toml",
            "BookKeeper/README.md",
            "BookKeeper/bookkeeper/exports/tax_engine.py",
        ],
    },
    {
        "name": "bookkeeper_ocr_confidence",
        "query": "What is the OCR confidence threshold below which BookKeeper sends invoices to human review?",
        "expected": "0.85",
        "accept": ["0.85", ".85"],
        "source": "BookKeeper/bookkeeper.toml",
        "gold_source": [
            "BookKeeper/bookkeeper.toml",
            "BookKeeper/README.md",
        ],
    },
    {
        "name": "bookkeeper_version",
        "query": "What is the current version of the BookKeeper package?",
        "expected": "0.13.0",
        "accept": ["0.13.0", "0.13.0b", "v0.13"],
        "source": "BookKeeper/pyproject.toml",
        "gold_source": [
            "BookKeeper/pyproject.toml",
            "BookKeeper/bookkeeper/cli.py",
            "BookKeeper/CLAUDE.md",
            "BookKeeper/README.md",
        ],
    },
    {
        "name": "bookkeeper_test_count",
        "query": "How many tests does the BookKeeper suite have passing?",
        "expected": "507",
        "accept": ["507", "507 tests"],
        "source": "BookKeeper/README.md",
        "gold_source": [
            "BookKeeper/README.md",
        ],
    },
    {
        "name": "bookkeeper_backup_interval",
        "query": "How often does BookKeeper run automatic backups in seconds?",
        "expected": "1200",
        "accept": ["1200", "20 minutes", "20 min", "20-minute"],
        "source": "BookKeeper/bookkeeper.toml",
        "gold_source": [
            "BookKeeper/bookkeeper.toml",
        ],
    },

    # ─── CosmicTasha (5) ──────────────────────────────────────────────
    {
        "name": "cosmictasha_template_count",
        "query": "How many SOC 2 compliance document templates does CosmicTasha provide?",
        "expected": "14",
        "accept": ["14", "fourteen"],
        "source": "CosmicTasha/README.md",
        "gold_source": [
            "CosmicTasha/README.md",
            "CosmicTasha/docs/superpowers/plans/2026-04-07-auth-biged-templates.md",
            "CosmicTasha/web/src/lib/compliance-kb/soc2/templates/index.ts",
        ],
    },
    {
        "name": "cosmictasha_default_model",
        "query": "What is the default Ollama model tag used by CosmicTasha?",
        "expected": "qwen3:8b",
        "accept": ["qwen3:8b", "qwen3 8b", "qwen3"],
        "source": "CosmicTasha/README.md",
        "gold_source": [
            "CosmicTasha/README.md",
            "CosmicTasha/.github/workflows/ci.yml",
        ],
    },
    {
        "name": "cosmictasha_biged_port",
        "query": "What port does the biged-rs inference service that CosmicTasha talks to listen on?",
        "expected": "5555",
        "accept": ["5555", "port 5555", ":5555"],
        "source": "CosmicTasha/README.md",
        "gold_source": [
            "CosmicTasha/README.md",
            "CosmicTasha/docker-compose.yml",
            "CosmicTasha/.github/workflows/ci.yml",
        ],
    },
    {
        "name": "cosmictasha_auth_library",
        "query": "What authentication library does CosmicTasha use for sessions?",
        "expected": "Lucia",
        "accept": ["Lucia", "lucia"],
        "source": "CosmicTasha/README.md",
        "gold_source": [
            "CosmicTasha/README.md",
            "CosmicTasha/.planning/03-architecture-decisions.md",
        ],
    },
    {
        "name": "cosmictasha_postgres_version",
        "query": "What major version of PostgreSQL does CosmicTasha use in production?",
        "expected": "16",
        "accept": ["16", "PostgreSQL 16", "postgres 16"],
        "source": "CosmicTasha/README.md",
        "gold_source": [
            "CosmicTasha/README.md",
            "CosmicTasha/docker-compose.yml",
            "CosmicTasha/.planning/01-infrastructure-hosting.md",
        ],
    },

    # ─── two-brain-audit (3) ──────────────────────────────────────────
    {
        "name": "scorerift_defense_layers",
        "query": "How many defense layers does ScoreRift use to prevent the system from lying?",
        "expected": "6",
        "accept": ["6", "six", "Six"],
        "source": "two-brain-audit/README.md",
        "gold_source": [
            "two-brain-audit/README.md",
        ],
    },
    {
        "name": "scorerift_confidence_floor",
        "query": "What auto-confidence percentage must be exceeded for ScoreRift to trigger a divergence alert?",
        "expected": "50%",
        "accept": ["50%", "50 percent", "0.5", ".5", "50"],
        "source": "two-brain-audit/README.md",
        "gold_source": [
            "two-brain-audit/README.md",
        ],
    },
    {
        "name": "scorerift_pkg_version",
        "query": "What is the package version of scorerift in pyproject.toml?",
        "expected": "2.0.0",
        "accept": ["2.0.0", "v2.0.0", "2.0"],
        "source": "two-brain-audit/pyproject.toml",
        "gold_source": [
            "two-brain-audit/pyproject.toml",
        ],
    },

    # ─── MaxExpressKit (3) ────────────────────────────────────────────
    {
        "name": "mek_version",
        "query": "What is the current version of the MaxExpressKit (MEK) plugin?",
        "expected": "0.1.3",
        "accept": ["0.1.3", "v0.1.3"],
        "source": "MaxExpressKit/package.json",
        "gold_source": [
            "MaxExpressKit/package.json",
            "MaxExpressKit/pyproject.toml",
            "MaxExpressKit/marketplace.json",
        ],
    },
    {
        "name": "mek_min_python",
        "query": "What is the minimum Python version required by MaxExpressKit?",
        "expected": "3.11",
        "accept": ["3.11", ">=3.11", "Python 3.11"],
        "source": "MaxExpressKit/pyproject.toml",
        "gold_source": [
            "MaxExpressKit/pyproject.toml",
        ],
    },
    {
        "name": "mek_source_apps",
        "query": "MaxExpressKit was distilled from how many source applications?",
        "expected": "three",
        "accept": ["three", "3", "three full apps"],
        "source": "MaxExpressKit/README.md",
        "gold_source": [
            "MaxExpressKit/README.md",
            "MaxExpressKit/docs/superpowers/specs/2026-05-10-mek-design.md",
        ],
    },

    # ─── Education / BigEd (13) ───────────────────────────────────────
    {
        "name": "biged_dashboard_port",
        "query": "What port does the BigEd fleet dashboard listen on?",
        "expected": "5555",
        "accept": ["5555", "port 5555", ":5555"],
        "source": "Education/README.md",
        "gold_source": [
            "Education/README.md",
            "Education/DEVELOPMENT.md",
        ],
    },
    {
        "name": "biged_ram_ceiling",
        "query": "What RAM ceiling percentage does BigEd target before stopping agent scale-up?",
        "expected": "97",
        "accept": ["97", "97%", "97 percent"],
        "source": "Education/fleet/fleet.toml",
        "gold_source": [
            "Education/fleet/fleet.toml",
            "Education/CLAUDE.md",
            "Education/FRAMEWORK_BLUEPRINT.md",
        ],
    },
    {
        "name": "biged_max_workers",
        "query": "What is the maximum number of fleet workers BigEd boots by default in fleet.toml?",
        "expected": "14",
        "accept": ["14"],
        "source": "Education/fleet/fleet.toml",
        "gold_source": [
            "Education/fleet/fleet.toml",
            "Education/biged-rs/tests/contract_test.rs",
        ],
    },
    {
        "name": "biged_core_agents",
        "query": "How many core agents does BigEd boot before demand-based scaling kicks in?",
        "expected": "4",
        "accept": ["4", "four"],
        "source": "Education/README.md",
        "gold_source": [
            "Education/README.md",
            "Education/ROADMAP.md",
            "Education/docs/flowcharts/boot_sequence.txt",
        ],
    },
    {
        "name": "biged_audit_dimensions",
        "query": "How many dimensions does the BigEd audit tracker grade against?",
        "expected": "12",
        "accept": ["12", "twelve"],
        "source": "Education/CLAUDE.md",
        "gold_source": [
            "Education/CLAUDE.md",
            "Education/ROADMAP.md",
            "Education/docs/superpowers/plans/ray_trace_plan.md",
        ],
    },
    {
        "name": "biged_db_tables",
        "query": "How many database tables does the BigEd fleet have?",
        "expected": "34",
        "accept": ["34", "34 tables"],
        "source": "Education/CLAUDE.md",
        "gold_source": [
            "Education/CLAUDE.md",
            "Education/AUDIT_TRACKER.md",
            "Education/AUDIT_TRACKER_COMPLIANCE_REPORT.md",
        ],
    },
    {
        "name": "biged_thermal_target",
        "query": "What is BigEd's CPU thermal cooldown target in Celsius?",
        "expected": "75",
        "accept": ["75", "75c", "75 c", "75°c", "75°"],
        "source": "Education/fleet/fleet.toml",
        "gold_source": [
            "Education/fleet/fleet.toml",
            "Education/FRAMEWORK_BLUEPRINT.md",
        ],
    },
    {
        "name": "biged_vision_model",
        "query": "What local vision model does BigEd use for image analysis?",
        "expected": "llava",
        "accept": ["llava", "LLaVA"],
        "source": "Education/fleet/fleet.toml",
        "gold_source": [
            "Education/fleet/fleet.toml",
            "Education/biged-rs/tests/contract_test.rs",
        ],
    },
    {
        "name": "biged_rust_msrv",
        "query": "What minimum Rust version does the biged-rs workspace require?",
        "expected": "1.76",
        "accept": ["1.76", "1.76+", "Rust 1.76"],
        "source": "Education/biged-rs/Cargo.toml",
        "gold_source": [
            "Education/biged-rs/Cargo.toml",
            "Education/biged-rs/README.md",
        ],
    },
    {
        "name": "biged_rust_web_framework",
        "query": "What Rust web framework does biged-rs use for its REST API?",
        "expected": "axum",
        "accept": ["axum"],
        "source": "Education/biged-rs/Cargo.toml",
        "gold_source": [
            "Education/biged-rs/Cargo.toml",
            "Education/biged-rs/README.md",
            "Education/biged-rs/DEPLOYMENT.md",
            "Education/FRAMEWORK_BLUEPRINT.md",
        ],
    },
    {
        "name": "biged_complex_model",
        "query": "What model does BigEd use for complex tasks like compliance docs and quality passes?",
        "expected": "gemma4:31b",
        "accept": ["gemma4:31b", "gemma4", "gemma 31b"],
        "source": "Education/fleet/fleet.toml",
        "gold_source": [
            "Education/fleet/fleet.toml",
            "Education/FRAMEWORK_BLUEPRINT.md",
            "Education/SESSION_HANDOFF.md",
        ],
    },
    {
        "name": "biged_smoke_tests",
        "query": "What is BigEd's smoke test pass count out of total?",
        "expected": "51/52",
        "accept": ["51/52", "51 of 52", "51 out of 52"],
        "source": "Education/CLAUDE.md",
        "gold_source": [
            "Education/CLAUDE.md",
            "Education/CONTRIBUTING.md",
            "Education/CROSS_PLATFORM.md",
        ],
    },
    {
        "name": "biged_idle_timeout",
        "query": "What is the idle timeout in seconds before BigEd workers go to sleep?",
        "expected": "10",
        "accept": ["10", "10 seconds", "10s"],
        "source": "Education/fleet/fleet.toml",
        "gold_source": [
            "Education/fleet/fleet.toml",
            "Education/biged-rs/fleet.toml",
            "Education/biged-rs/crates/biged-core/src/config.rs",
        ],
    },

    # ─── helix-context (10) ───────────────────────────────────────────
    {
        "name": "helix_expression_budget",
        "query": "What is the expression_tokens budget set in helix.toml?",
        "expected": "7000",
        "accept": ["7000", "7K", "7,000", "~7K"],
        "source": "helix-context/helix.toml",
        "gold_source": [
            "helix-context/helix.toml",
            "helix-context/overnight_logs/broad_tighten_2026-05-12_1422_report.md",
        ],
    },
    {
        "name": "helix_max_genes_per_turn",
        "query": "What is the max_genes_per_turn cap in helix.toml?",
        "expected": "12",
        "accept": ["12"],
        "source": "helix-context/helix.toml",
        "gold_source": [
            "helix-context/helix.toml",
            "helix-context/docs/config-reference.md",
        ],
    },
    {
        "name": "helix_rrf_k",
        "query": "What is the RRF k constant used by Helix Reciprocal Rank Fusion?",
        "expected": "60",
        "accept": ["60", "k=60", "k = 60"],
        "source": "helix-context/helix.toml",
        "gold_source": [
            "helix-context/helix.toml",
            "helix-context/docs/config-reference.md",
            "helix-context/docs/specs/2026-05-08-stage-3-rrf-fusion.md",
        ],
    },
    {
        "name": "helix_cold_start_threshold",
        "query": "What is the cold_start_threshold gene count in the Helix knowledge store?",
        "expected": "10",
        "accept": ["10", "10 genes"],
        "source": "helix-context/helix.toml",
        "gold_source": [
            "helix-context/helix.toml",
            "helix-context/docs/config-reference.md",
            "helix-context/CLAUDE.md",
        ],
    },
    {
        "name": "helix_session_window",
        "query": "What is the synthetic_session_window_s in seconds for grouping same-IP requests?",
        "expected": "300",
        "accept": ["300", "5 min", "5 minutes", "five minutes"],
        "source": "helix-context/helix.toml",
        "gold_source": [
            "helix-context/helix.toml",
            "helix-context/docs/config-reference.md",
            "helix-context/CLAUDE.md",
        ],
    },
    {
        "name": "helix_headroom_port",
        "query": "What is the default port for the Headroom proxy that Helix can route through?",
        "expected": "8787",
        "accept": ["8787", "port 8787"],
        "source": "helix-context/helix.toml",
        "gold_source": [
            "helix-context/helix.toml",
            "helix-context/docs/config-reference.md",
        ],
    },
    {
        "name": "helix_calibration_staleness",
        "query": "After how many days does Helix flag the know-confidence calibration as stale?",
        "expected": "30",
        "accept": ["30", "30 days"],
        "source": "helix-context/helix.toml",
        "gold_source": [
            "helix-context/helix.toml",
            "helix-context/docs/specs/2026-05-08-stage-6-know-miss-blocks.md",
        ],
    },
    {
        "name": "helix_dense_encoder",
        "query": "Which dense embedding model does Helix use for Stage-2 recall?",
        "expected": "BGE-M3",
        "accept": ["BGE-M3", "bge-m3", "bge m3"],
        "source": "helix-context/CLAUDE.md",
        "gold_source": [
            "helix-context/CLAUDE.md",
            "helix-context/README.md",
            "helix-context/helix.toml",
        ],
    },
    {
        "name": "helix_filename_anchor",
        "query": "What is the filename_anchor_weight per-match boost in helix.toml?",
        "expected": "4.0",
        "accept": ["4.0", "4"],
        "source": "helix-context/helix.toml",
        "gold_source": [
            "helix-context/helix.toml",
            "helix-context/docs/config-reference.md",
        ],
    },
    {
        "name": "helix_subpackages_count",
        "query": "Into how many sub-packages is helix_context organized after the PR #90 restructure?",
        "expected": "16",
        "accept": ["16", "sixteen"],
        "source": "helix-context/CLAUDE.md",
        "gold_source": [
            "helix-context/CLAUDE.md",
            "helix-context/docs/superpowers/plans/2026-05-13-readme-v3-plan.md",
        ],
    },
]


# ── Gold-gene delivery check (Waude diagnostic 2026-04-17) ────────────

# Legacy assembly markup -- retained as a fallback for historical JSONL
# inputs. The live /context renderer emits ``[gene=...]`` legibility
# headers + structured ``agent.citations`` instead (see issue #101).
# `benchmarks/_citations.py` is the canonical parser.
GENE_BLOCK_RE = _citations.LEGACY_GENE_BLOCK_RE


def parse_delivered_genes(content: str):
    """Extract (src, body) tuples from a legacy content string.

    Kept for historical JSONL inspection. For live /context responses,
    use ``parse_delivered_genes_from_response`` -- modern payloads carry
    structured citations at ``response[0]["agent"]["citations"]`` and no
    longer embed ``<GENE src=...>`` markup in ``content``.
    """
    return _citations.parse_legacy_gene_blocks(content or "")


def parse_delivered_genes_from_response(payload):
    """Extract (src, body) tuples from a /context response.

    Prefers ``agent.citations`` + the corresponding per-block bodies
    parsed from the legibility-headered content blob. Falls back to the
    legacy ``<GENE>...</GENE>`` regex when no structured citations are
    present (historical JSONL replays).
    """
    return [(src, body) for src, _gid, body in _citations.extract_block_bodies(payload)]


def _body_contains_accept(body: str, accept) -> bool:
    """Word-boundary match to exclude substring-in-URL false positives.

    ``\\b11\\b`` matches "11 MB" but NOT "localhost:11434". Plain
    substring match was the 2026-04-16 bench bug — it counted
    ``11434`` as an "11" hit.
    """
    for a in accept:
        pattern = rf"\b{re.escape(a)}\b"
        if re.search(pattern, body, re.IGNORECASE):
            return True
    return False


def check_gold_delivery(content: str, gold_sources, accept, *, response=None):
    """Honest delivery check for a needle.

    Pass ``response`` for live /context payloads (modern shape with
    ``agent.citations``). ``content`` is retained as a fallback so the
    helper still works on historical JSONL records whose only artifact
    is the inline assembly string.

    Returns a dict with these dimensions:
      - ``gold_delivered``: gold source file is in delivered top-K
        (the retrieval-rank metric — addresses D-category failures
        directly, per Waude diagnostic 2026-04-17)
      - ``gold_has_answer``: gold-source block body contains accept
      - ``body_has_answer``: ANY delivered block body contains accept
        with word boundaries (what the consumer actually sees; more
        honest than raw substring match because it excludes URL
        ports and metadata headers)
      - ``false_positive_substring``: raw payload substring match
        fires BUT no gene body has a word-boundary match (i.e.,
        the match is pure metadata/header/URL noise)
    """
    if response is not None:
        blocks = parse_delivered_genes_from_response(response)
    else:
        blocks = parse_delivered_genes(content)

    def src_matches(src: str) -> bool:
        src_norm = src.replace("\\", "/").lower()
        return any(
            g.replace("\\", "/").lower() in src_norm for g in gold_sources
        )

    gold_blocks = [(src, body) for src, body in blocks if src_matches(src)]
    gold_delivered = len(gold_blocks) > 0

    gold_has_answer = any(
        _body_contains_accept(body, accept) for _, body in gold_blocks
    )
    body_has_answer = any(
        _body_contains_accept(body, accept) for _, body in blocks
    )

    # ``content_has_answer``: word-boundary accept match over the FULL
    # assembled context the model actually reads, NOT the per-block bodies.
    # This is the honest deliverability signal. ``body_has_answer`` relies on
    # citation->body pairing (``_citations.extract_block_bodies``), which fails
    # CLOSED to empty bodies whenever the citation:block counts don't line up
    # — systematically under the legibility-disabled bench probe profile, where
    # blocks carry no ``[gene=...]`` header to id-join on (root-caused
    # 2026-07-06: 17 of 23 xl "gold delivered but body missing" needles had the
    # answer present in ``content`` all along). Word-boundary (not substring)
    # keeps the 11434-vs-11437 guard. When legibility is ON in production the
    # content also carries fact headers, so body_has_answer remains the
    # stricter "in a readable body" metric; both are reported.
    content_has_answer = _body_contains_accept(content or "", accept)

    old_substring_hit = any(a.lower() in (content or "").lower() for a in accept)
    false_positive = old_substring_hit and not body_has_answer

    return {
        "gold_delivered": gold_delivered,
        "gold_has_answer": gold_has_answer,
        "body_has_answer": body_has_answer,
        "content_has_answer": content_has_answer,
        "old_substring_hit": old_substring_hit,
        "false_positive_substring": false_positive,
        "n_gold_blocks": len(gold_blocks),
        "n_delivered_blocks": len(blocks),
    }


def find_needle(client, needle):
    """Try to find a specific needle in the genome."""
    t0 = time.time()

    # Step 1: Context query. ignore_delivered opts out of the session
    # working-set register: production defaults keep session delivery +
    # synthetic sessions ON (300s same-IP window), so a 50-needle battery
    # would otherwise get elision stubs instead of gold bodies for any
    # document retrieved twice (CLAUDE.md bench guidance; review 2026-07-05).
    try:
        resp = client.post(f"{HELIX_URL}/context", json={
            "query": needle["query"],
            "decoder_mode": "none",
            "ignore_delivered": True,
        })
    except Exception:
        return {
            "name": needle["name"], "query": needle["query"],
            "expected": needle["expected"],
            "found_in_context": False, "answer_correct": False,
            "context_latency_s": time.time() - t0,
            "ellipticity": 0, "status": "error", "genes_expressed": 0,
            "answer_preview": "server unreachable",
        }
    context_latency = time.time() - t0

    if resp.status_code != 200:
        return {
            "name": needle["name"], "query": needle["query"],
            "expected": needle["expected"],
            "found_in_context": False, "answer_correct": False,
            "context_latency_s": context_latency,
            "ellipticity": 0, "status": "error", "genes_expressed": 0,
            "answer_preview": f"HTTP {resp.status_code}",
        }

    data = resp.json()
    entry = data[0] if data else {}
    content = entry.get("content", "")
    health = entry.get("context_health", {})

    # Gold-gene delivery check (Waude diagnostic 2026-04-17): require
    # the answer's source file to be in the delivered gene set AND
    # that block's body to contain an accept substring. Fall back to
    # payload substring if a needle has no gold_source defined.
    #
    # The full ``data`` (list-wrapped response) is passed through so the
    # citation parser can read ``agent.citations`` for modern responses
    # and fall back to legacy ``<GENE src=...>`` markup automatically
    # (issue #101).
    accept = needle.get("accept", [needle["expected"]])
    gold_sources = needle.get("gold_source", [])
    if gold_sources:
        gold = check_gold_delivery(
            content, gold_sources, accept, response=data,
        )
        # Primary metric: does any delivered gene BODY contain the
        # answer with word-boundary match. This is what the consumer
        # actually sees, minus metadata/URL false positives.
        found_in_context = gold["body_has_answer"]
        gold_delivered = gold["gold_delivered"]
        gold_has_answer = gold["gold_has_answer"]
        content_has_answer = gold["content_has_answer"]
        false_positive = gold["false_positive_substring"]
        n_gold_blocks = gold["n_gold_blocks"]
        n_delivered_blocks = gold["n_delivered_blocks"]
    else:
        found_in_context = any(a.lower() in content.lower() for a in accept)
        gold_delivered = found_in_context
        gold_has_answer = found_in_context
        content_has_answer = found_in_context
        false_positive = False
        n_gold_blocks = 0
        n_delivered_blocks = len(parse_delivered_genes_from_response(data))

    # Step 2: Full proxy query for answer accuracy. Guarded: an
    # answer-step failure (e.g. a slow local model outliving the
    # caller's client timeout) must NOT destroy the Step-1 retrieval
    # fields — callers aggregate gold_delivered over returned rows, so
    # a raised exception here silently shrinks that denominator
    # (review 2026-07-05).
    t1 = time.time()
    model = os.environ.get("HELIX_MODEL", "qwen3:8b")
    answer_correct = False
    answer_text = ""
    try:
        proxy_resp = client.post(f"{HELIX_URL}/v1/chat/completions", json={
            "model": model,
            "messages": [{"role": "user", "content": needle["query"]}],
            "stream": False,
            "options": {"temperature": 0, "num_predict": 256},
        })
        if proxy_resp.status_code == 200:
            choices = proxy_resp.json().get("choices", [])
            if choices:
                answer_text = choices[0].get("message", {}).get("content", "")
                answer_correct = any(
                    a.lower() in answer_text.lower() for a in accept
                )
    except Exception:
        answer_text = ""
    proxy_latency = time.time() - t1

    return {
        "name": needle["name"],
        "query": needle["query"],
        "expected": needle["expected"],
        "found_in_context": found_in_context,
        "answer_correct": answer_correct,
        "gold_delivered": gold_delivered,
        "gold_has_answer": gold_has_answer,
        "content_has_answer": content_has_answer,
        "false_positive_substring": false_positive,
        "n_gold_blocks": n_gold_blocks,
        "n_delivered_blocks": n_delivered_blocks,
        "context_latency_s": round(context_latency, 3),
        "proxy_latency_s": round(proxy_latency, 3),
        "ellipticity": health.get("ellipticity", 0),
        "status": health.get("status", "unknown"),
        "genes_expressed": health.get("genes_expressed", 0),
        # Step 1b weighing surface (2026-04-17): coordinate-resolution
        # confidence that tells the consumer whether to act on the pointer
        # or go fetch. Separate from ellipticity (retrospective).
        "coordinate_crispness": health.get("coordinate_crispness", 0),
        "neighborhood_density": health.get("neighborhood_density", 0),
        "resolution_confidence": health.get("resolution_confidence", 0),
        "top_score_raw": health.get("top_score_raw", 0),
        "top_dominance": health.get("top_dominance", 0),
        "path_token_coverage": health.get("path_token_coverage", 0),
        "file_token_coverage": health.get("file_token_coverage", 0),
        "answer_preview": answer_text[:200] if answer_text else "",
    }


def main():
    client = httpx.Client(timeout=300)

    # Check server
    try:
        stats = client.get(f"{HELIX_URL}/stats").json()
        print(f"Genome: {stats['total_genes']} genes, {stats['compression_ratio']:.1f}x")
    except Exception:
        print(f"Cannot reach Helix at {HELIX_URL}")
        sys.exit(1)

    print(f"\n=== Needle in a Haystack ({len(NEEDLES)} needles) ===\n")

    results = []
    found_context = 0
    found_answer = 0

    for needle in NEEDLES:
        r = find_needle(client, needle)
        results.append(r)

        icon_ctx = "+" if r["found_in_context"] else "-"
        icon_ans = "+" if r["answer_correct"] else "-"
        icon_gold = "+" if r.get("gold_delivered") else "-"
        conf = r.get("resolution_confidence", 0)
        print(f"  ctx[{icon_ctx}] ans[{icon_ans}] gold[{icon_gold}]  "
              f"{r['context_latency_s']:>5.1f}s  "
              f"e={r.get('ellipticity', 0):.2f}  "
              f"conf={conf:.2f}  "
              f"{r['name']}: \"{r['expected']}\"")

        if r["found_in_context"]:
            found_context += 1
        if r["answer_correct"]:
            found_answer += 1

    print(f"\n=== Results ===")
    print(f"Context retrieval (honest):  {found_context}/{len(NEEDLES)} ({found_context/len(NEEDLES)*100:.0f}%)")
    print(f"Answer accuracy:             {found_answer}/{len(NEEDLES)} ({found_answer/len(NEEDLES)*100:.0f}%)")

    gold_delivered = sum(1 for r in results if r.get("gold_delivered"))
    false_positives = sum(1 for r in results if r.get("false_positive_substring"))
    print(f"Gold source in top-K:        {gold_delivered}/{len(NEEDLES)} ({gold_delivered/len(NEEDLES)*100:.0f}%)")
    print(f"False-positive substring:    {false_positives}/{len(NEEDLES)} "
          f"(old-scoring would count these as hits)")

    # Weighing surface (Step 1b, 2026-04-17): know-vs-go quality
    # correctly_known_miss = gold missing AND helix's resolution_confidence
    # is below threshold. High rate means helix knows when it doesn't know.
    # silent_miss = gold missing AND confidence above threshold (dangerous).
    # overconfident_false_positive = substring false-positive AND confidence high.
    confidence_threshold = 0.30  # empirical; tune against distribution below
    misses = [r for r in results if not r.get("gold_delivered")]
    known_miss = sum(
        1 for r in misses
        if r.get("resolution_confidence", 0) < confidence_threshold
    )
    silent_miss = len(misses) - known_miss
    avg_conf_hit = (
        sum(r.get("resolution_confidence", 0) for r in results if r.get("gold_delivered"))
        / max(gold_delivered, 1)
    )
    avg_conf_miss = (
        sum(r.get("resolution_confidence", 0) for r in misses) / max(len(misses), 1)
    )
    print(
        f"Correctly-known miss:        {known_miss}/{len(misses)} "
        f"(confidence < {confidence_threshold} when gold absent)"
    )
    print(
        f"Silent miss (danger):        {silent_miss}/{len(misses)} "
        f"(confident but wrong)"
    )
    print(
        f"Avg resolution_confidence:   hit={avg_conf_hit:.3f}  miss={avg_conf_miss:.3f}  "
        f"(want hit >> miss)"
    )

    avg_latency = sum(r["context_latency_s"] for r in results) / len(results)
    print(f"Avg context latency: {avg_latency:.1f}s")

    # Save results
    output = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "genome_genes": stats["total_genes"],
        "compression_ratio": stats["compression_ratio"],
        "needles": results,
        "summary": {
            "context_retrieval_rate": found_context / len(NEEDLES),
            "answer_accuracy_rate": found_answer / len(NEEDLES),
            "gold_delivered_rate": gold_delivered / len(NEEDLES),
            "false_positive_substring_count": false_positives,
            "correctly_known_miss_count": known_miss,
            "silent_miss_count": silent_miss,
            "avg_resolution_confidence_hit": round(avg_conf_hit, 4),
            "avg_resolution_confidence_miss": round(avg_conf_miss, 4),
            "confidence_threshold": confidence_threshold,
            "avg_context_latency_s": round(avg_latency, 3),
            "scoring": "gold_source_in_top_K_and_body_substring",
        },
    }

    out_path = os.path.join(os.path.dirname(__file__), "results", "needle_results.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
