r"""Run the 50-needle bench against the frozen fixture matrix via ``claude -p``.

A default run covers the trusted fixtures only; xl-sharded is excluded
because its `projects` shard fixture is corrupt (see issue #133). Pass
``--include-untrusted`` to run it anyway.

Per fixture:
  1. POST /admin/swap-db pointing at the fixture's primary .db
     (or restart uvicorn with HELIX_USE_SHARDS=1 when crossing
     blob<->sharded mode boundaries — handled by BenchServer)
  2. For each needle, invoke ``claude -p --output-format json`` so the
     model answers via helix-context MCP (which now hits the swapped db)
  3. Also call /context directly per needle for retrieval-only metrics
     (citation-grounded gold-source check, with legacy <GENE src> regex
     fallback for older response shapes / archived JSONL)
  4. Score answer ∈ {-1, 0, +1} via word-boundary accept-match
  5. Write per-fixture JSONL into a timestamped results dir

Pre-conditions:
  * Claude Code CLI available on PATH and the helix-context MCP wired up
    in the user's settings/.mcp.json. New ``claude -p`` subprocesses
    discover the MCP automatically.
  * By default the harness manages the helix server itself via
    ``bench_orchestrator.BenchServer``, including the
    HELIX_USE_SHARDS=1 env required for sharded fixtures. Pass
    ``--external-server`` to use a server you've started manually
    (legacy behavior).

Output (no overwrite — per-run subdir):
  benchmarks/results/claude_matrix_<UTC-timestamp>/
    summary.json
    small.jsonl
    medium.jsonl
    large.jsonl
    xl.jsonl
    medium-sharded.jsonl
    xl-sharded.jsonl     (only with --include-untrusted; see #133)
    run.log
    uvicorn.log         (when managing the server)

Usage:
  python benchmarks/bench_claude_matrix.py
  python benchmarks/bench_claude_matrix.py --only small,medium
  python benchmarks/bench_claude_matrix.py --skip large
  python benchmarks/bench_claude_matrix.py --include-untrusted
  python benchmarks/bench_claude_matrix.py --model sonnet --max-usd 0.20
  python benchmarks/bench_claude_matrix.py --external-server
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Sibling imports — bench_orchestrator + _citations live alongside this
# script in benchmarks/. Insert our own dir on sys.path so the imports
# work whether the script is invoked by absolute path, from the project
# root, or via python -m.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_orchestrator import BenchServer, Fixture  # noqa: E402
import _citations  # noqa: E402


# ── Configuration ────────────────────────────────────────────────────────

HELIX_URL = os.environ.get("HELIX_URL", "http://127.0.0.1:11437")
MANIFEST = Path(r"F:\Projects\helix-context\genomes\bench\matrix\frozen.json")
RESULTS_ROOT = Path(r"F:\Projects\helix-context\benchmarks\results")

CLAUDE_TIMEOUT_S = 300        # 5 min per question — generous for MCP + reasoning
SWAP_TIMEOUT_S = 60           # SPLADE rebuild on swap can take a moment
CONTEXT_TIMEOUT_S = 30        # /context retrieval-only call

# Profiles whose fixture is known-untrustworthy and excluded from a
# default run. xl-sharded's `projects` shard was built from a polluted
# source tree — F:\Projects\_worktrees\helix-context\* PR-branch
# worktrees plus a helix-retrieval-upgrade clone, with no canonical
# helix-context/ checkout — so the needles' gold_source labels resolve
# to nothing there and retr_hit / MRR on it are meaningless (correctness
# decoupled from retrieval). See issue #133. Re-include once the projects
# shard is rebuilt clean; --include-untrusted forces it in meanwhile.
UNTRUSTED_PROFILES = frozenset({"xl-sharded"})


# ── Needles (copied from benchmarks/bench_needle.py so this script is
#     standalone and the runner doesn't depend on the bench dir on PYTHONPATH).

#
# ``gold_source`` is a list of case-insensitive path substrings; a needle
# counts as ``gold_delivered`` when ANY entry appears (substring match,
# forward-slash + lowercased) in ``agent.citations[].source``. Multi-valid
# gold avoids penalizing retrieval for finding an equally valid answer in
# a non-canonical file (see docs/benchmarks/MULTI_VALID_GOLD.md for the
# per-needle curation rationale).
#
# Audit-trail rule: the historically labeled gold MUST stay first in the
# list so older JSONL files and analyses remain comparable. Additional
# valid sources are appended.
#
# IMPORTANT: do NOT add ``helix-context/docs/benchmarks/BENCHMARKS.md`` to
# any needle. That file is the bench answer key — adding it as gold would
# make retrieval succeed whenever the answer-key doc is retrieved, which
# is circular and inflates scores.

NEEDLES = [
    {"name": "helix_port",
     "query": "What port does the Helix proxy server listen on?",
     "expected": "11437", "accept": ["11437"],
     "gold_source": [
         "helix-context/helix.toml",
         "helix-context/README.md",
         "helix-context/CLAUDE.md",
         "helix-context/docs/SETUP.md",
         "helix-context/docs/TROUBLESHOOTING.md",
         "helix-context/docs/api/endpoints.md",
     ]},
    {"name": "scorerift_threshold",
     "query": "What is the divergence threshold that triggers alerts in ScoreRift?",
     "expected": "0.15", "accept": ["0.15", ".15"],
     "gold_source": [
         "two-brain-audit/README.md",
         "two-brain-audit/docs/QUICKSTART.md",
         "two-brain-audit/src/scorerift/reconciler.py",
         "two-brain-audit/src/scorerift/engine.py",
     ]},
    {"name": "biged_skills_count",
     "query": "How many skills does the BigEd fleet have?",
     "expected": "125", "accept": ["125", "129"],
     "gold_source": [
         "Education/CLAUDE.md",
         "Education/FRAMEWORK_BLUEPRINT.md",
         "Education/ROADMAP.md",
         "Education/fleet/CLAUDE.md",
         "Education/fleet/knowledge/wiki/overview.md",
         "Education/fleet/knowledge/wiki/architecture.md",
     ]},
    {"name": "bookkeeper_monetary",
     "query": "What type should be used for monetary values in BookKeeper instead of float?",
     "expected": "Decimal", "accept": ["decimal", "Decimal"],
     "gold_source": [
         "BookKeeper/CLAUDE.md",
         "BookKeeper/docs/planning/GAPS.md",
         "BookKeeper/bookkeeper/storage/db.py",
     ]},
    {"name": "helix_pipeline_steps",
     "query": "How many steps are in the Helix expression pipeline?",
     "expected": "6", "accept": ["6", "six"],
     "gold_source": [
         "helix-context/CLAUDE.md",
         "helix-context/README.md",
     ]},
    {"name": "biged_rust_binary_size",
     "query": "What is the binary size of the Rust BigEd build in MB?",
     "expected": "11", "accept": ["11", "11mb", "11 mb"],
     "gold_source": [
         "Education/biged-rs/README.md",
         "Education/biged-rs/DEPLOYMENT.md",
         "Education/fleet/knowledge/wiki/architecture.md",
     ]},
    {"name": "genome_compression_target",
     "query": "What is the target compression ratio for Helix Context?",
     "expected": "5x", "accept": ["5x", "5:1", "5 to 1"],
     "gold_source": [
         "helix-context/README.md",
         "helix-context/docs",
         "helix-context/BENCHMARK_NOTES.md",
     ]},
    {"name": "scorerift_preset_dimensions",
     "query": "How many dimensions does the Python preset in ScoreRift check?",
     "expected": "8", "accept": ["8", "eight"],
     "gold_source": [
         "two-brain-audit/README.md",
         "two-brain-audit/docs/ARCHITECTURE.md",
         "two-brain-audit/src/scorerift/presets/python_project.py",
     ]},
    {"name": "helix_ribosome_budget",
     "query": "How many tokens are allocated for the ribosome decoder prompt?",
     "expected": "3000", "accept": ["3000", "3k", "3,000"],
     "gold_source": [
         "helix-context/helix.toml",
         "helix-context/README.md",
         "helix-context/docs/config-reference.md",
     ]},
    {"name": "biged_default_model",
     "query": "What is the default local model used by BigEd for conductor tasks?",
     "expected": "qwen3", "accept": ["qwen3", "qwen3:4b", "qwen"],
     "gold_source": [
         "Education/CLAUDE.md",
         "Education/FRAMEWORK_BLUEPRINT.md",
         "Education/OPERATIONS.md",
         "Education/fleet/fleet.toml",
     ]},
    # ─── 2026-05-15 N=50 expansion (PR feat/bench-needles-50) ────────────
    # New needles target the noise floor problem: N=10 gives ~1.15-stddev
    # correctness noise, masking single-knob tuning effects. Each new
    # entry is hand-curated against the medium-sharded fixture: the fact
    # is grepped in the shard's genes table and gold_source paths are
    # verified to contain the expected token in answer-shape. See
    # docs/benchmarks/MULTI_VALID_GOLD.md for curation policy.

    # ─── BookKeeper (6) ───────────────────────────────────────────────
    {"name": "bookkeeper_dashboard_port",
     "query": "What port does the BookKeeper web dashboard listen on by default?",
     "expected": "8080", "accept": ["8080", "port 8080"],
     "gold_source": [
         "BookKeeper/bookkeeper.toml",
         "BookKeeper/README.md",
         "BookKeeper/docs/TENANCY_QUICKSTART.md",
     ]},
    {"name": "bookkeeper_1099_threshold",
     "query": "What dollar threshold triggers 1099 tracking and W-9 requirement in BookKeeper?",
     "expected": "600", "accept": ["600", "$600"],
     "gold_source": [
         "BookKeeper/bookkeeper.toml",
         "BookKeeper/README.md",
         "BookKeeper/bookkeeper/exports/tax_engine.py",
     ]},
    {"name": "bookkeeper_ocr_confidence",
     "query": "What is the OCR confidence threshold below which BookKeeper sends invoices to human review?",
     "expected": "0.85", "accept": ["0.85", ".85"],
     "gold_source": [
         "BookKeeper/bookkeeper.toml",
         "BookKeeper/README.md",
     ]},
    {"name": "bookkeeper_version",
     "query": "What is the current version of the BookKeeper package?",
     "expected": "0.13.0", "accept": ["0.13.0", "0.13.0b", "v0.13"],
     "gold_source": [
         "BookKeeper/pyproject.toml",
         "BookKeeper/bookkeeper/cli.py",
         "BookKeeper/CLAUDE.md",
         "BookKeeper/README.md",
     ]},
    {"name": "bookkeeper_test_count",
     "query": "How many tests does the BookKeeper suite have passing?",
     "expected": "507", "accept": ["507", "507 tests"],
     "gold_source": [
         "BookKeeper/README.md",
     ]},
    {"name": "bookkeeper_backup_interval",
     "query": "How often does BookKeeper run automatic backups in seconds?",
     "expected": "1200", "accept": ["1200", "20 minutes", "20 min", "20-minute"],
     "gold_source": [
         "BookKeeper/bookkeeper.toml",
     ]},

    # ─── CosmicTasha (5) ──────────────────────────────────────────────
    {"name": "cosmictasha_template_count",
     "query": "How many SOC 2 compliance document templates does CosmicTasha provide?",
     "expected": "14", "accept": ["14", "fourteen"],
     "gold_source": [
         "CosmicTasha/README.md",
         "CosmicTasha/docs/superpowers/plans/2026-04-07-auth-biged-templates.md",
         "CosmicTasha/web/src/lib/compliance-kb/soc2/templates/index.ts",
     ]},
    {"name": "cosmictasha_default_model",
     "query": "What is the default Ollama model tag used by CosmicTasha?",
     "expected": "qwen3:8b", "accept": ["qwen3:8b", "qwen3 8b", "qwen3"],
     "gold_source": [
         "CosmicTasha/README.md",
         "CosmicTasha/.github/workflows/ci.yml",
     ]},
    {"name": "cosmictasha_biged_port",
     "query": "What port does the biged-rs inference service that CosmicTasha talks to listen on?",
     "expected": "5555", "accept": ["5555", "port 5555", ":5555"],
     "gold_source": [
         "CosmicTasha/README.md",
         "CosmicTasha/docker-compose.yml",
         "CosmicTasha/.github/workflows/ci.yml",
     ]},
    {"name": "cosmictasha_auth_library",
     "query": "What authentication library does CosmicTasha use for sessions?",
     "expected": "Lucia", "accept": ["Lucia", "lucia"],
     "gold_source": [
         "CosmicTasha/README.md",
         "CosmicTasha/.planning/03-architecture-decisions.md",
     ]},
    {"name": "cosmictasha_postgres_version",
     "query": "What major version of PostgreSQL does CosmicTasha use in production?",
     "expected": "16", "accept": ["16", "PostgreSQL 16", "postgres 16"],
     "gold_source": [
         "CosmicTasha/README.md",
         "CosmicTasha/docker-compose.yml",
         "CosmicTasha/.planning/01-infrastructure-hosting.md",
     ]},

    # ─── two-brain-audit (3) ──────────────────────────────────────────
    {"name": "scorerift_defense_layers",
     "query": "How many defense layers does ScoreRift use to prevent the system from lying?",
     "expected": "6", "accept": ["6", "six", "Six"],
     "gold_source": [
         "two-brain-audit/README.md",
     ]},
    {"name": "scorerift_confidence_floor",
     "query": "What auto-confidence percentage must be exceeded for ScoreRift to trigger a divergence alert?",
     "expected": "50%", "accept": ["50%", "50 percent", "0.5", ".5", "50"],
     "gold_source": [
         "two-brain-audit/README.md",
     ]},
    {"name": "scorerift_pkg_version",
     "query": "What is the package version of scorerift in pyproject.toml?",
     "expected": "2.0.0", "accept": ["2.0.0", "v2.0.0", "2.0"],
     "gold_source": [
         "two-brain-audit/pyproject.toml",
     ]},

    # ─── MaxExpressKit (3) ────────────────────────────────────────────
    {"name": "mek_version",
     "query": "What is the current version of the MaxExpressKit (MEK) plugin?",
     "expected": "0.1.3", "accept": ["0.1.3", "v0.1.3"],
     "gold_source": [
         "MaxExpressKit/package.json",
         "MaxExpressKit/pyproject.toml",
         "MaxExpressKit/marketplace.json",
     ]},
    {"name": "mek_min_python",
     "query": "What is the minimum Python version required by MaxExpressKit?",
     "expected": "3.11", "accept": ["3.11", ">=3.11", "Python 3.11"],
     "gold_source": [
         "MaxExpressKit/pyproject.toml",
     ]},
    {"name": "mek_source_apps",
     "query": "MaxExpressKit was distilled from how many source applications?",
     "expected": "three", "accept": ["three", "3", "three full apps"],
     "gold_source": [
         "MaxExpressKit/README.md",
         "MaxExpressKit/docs/superpowers/specs/2026-05-10-mek-design.md",
     ]},

    # ─── Education / BigEd (13) ───────────────────────────────────────
    {"name": "biged_dashboard_port",
     "query": "What port does the BigEd fleet dashboard listen on?",
     "expected": "5555", "accept": ["5555", "port 5555", ":5555"],
     "gold_source": [
         "Education/README.md",
         "Education/DEVELOPMENT.md",
     ]},
    {"name": "biged_ram_ceiling",
     "query": "What RAM ceiling percentage does BigEd target before stopping agent scale-up?",
     "expected": "97", "accept": ["97", "97%", "97 percent"],
     "gold_source": [
         "Education/fleet/fleet.toml",
         "Education/CLAUDE.md",
         "Education/FRAMEWORK_BLUEPRINT.md",
     ]},
    {"name": "biged_max_workers",
     "query": "What is the maximum number of fleet workers BigEd boots by default in fleet.toml?",
     "expected": "14", "accept": ["14"],
     "gold_source": [
         "Education/fleet/fleet.toml",
         "Education/biged-rs/tests/contract_test.rs",
     ]},
    {"name": "biged_core_agents",
     "query": "How many core agents does BigEd boot before demand-based scaling kicks in?",
     "expected": "4", "accept": ["4", "four"],
     "gold_source": [
         "Education/README.md",
         "Education/ROADMAP.md",
         "Education/docs/flowcharts/boot_sequence.txt",
     ]},
    {"name": "biged_audit_dimensions",
     "query": "How many dimensions does the BigEd audit tracker grade against?",
     "expected": "12", "accept": ["12", "twelve"],
     "gold_source": [
         "Education/CLAUDE.md",
         "Education/ROADMAP.md",
         "Education/docs/superpowers/plans/ray_trace_plan.md",
     ]},
    {"name": "biged_db_tables",
     "query": "How many database tables does the BigEd fleet have?",
     "expected": "34", "accept": ["34", "34 tables"],
     "gold_source": [
         "Education/CLAUDE.md",
         "Education/AUDIT_TRACKER.md",
         "Education/AUDIT_TRACKER_COMPLIANCE_REPORT.md",
     ]},
    {"name": "biged_thermal_target",
     "query": "What is BigEd's CPU thermal cooldown target in Celsius?",
     "expected": "75", "accept": ["75", "75c", "75 c", "75°c", "75°"],
     "gold_source": [
         "Education/fleet/fleet.toml",
         "Education/FRAMEWORK_BLUEPRINT.md",
     ]},
    {"name": "biged_vision_model",
     "query": "What local vision model does BigEd use for image analysis?",
     "expected": "llava", "accept": ["llava", "LLaVA"],
     "gold_source": [
         "Education/fleet/fleet.toml",
         "Education/biged-rs/tests/contract_test.rs",
     ]},
    {"name": "biged_rust_msrv",
     "query": "What minimum Rust version does the biged-rs workspace require?",
     "expected": "1.76", "accept": ["1.76", "1.76+", "Rust 1.76"],
     "gold_source": [
         "Education/biged-rs/Cargo.toml",
         "Education/biged-rs/README.md",
     ]},
    {"name": "biged_rust_web_framework",
     "query": "What Rust web framework does biged-rs use for its REST API?",
     "expected": "axum", "accept": ["axum"],
     "gold_source": [
         "Education/biged-rs/Cargo.toml",
         "Education/biged-rs/README.md",
         "Education/biged-rs/DEPLOYMENT.md",
         "Education/FRAMEWORK_BLUEPRINT.md",
     ]},
    {"name": "biged_complex_model",
     "query": "What model does BigEd use for complex tasks like compliance docs and quality passes?",
     "expected": "gemma4:31b", "accept": ["gemma4:31b", "gemma4", "gemma 31b"],
     "gold_source": [
         "Education/fleet/fleet.toml",
         "Education/FRAMEWORK_BLUEPRINT.md",
         "Education/SESSION_HANDOFF.md",
     ]},
    {"name": "biged_smoke_tests",
     "query": "What is BigEd's smoke test pass count out of total?",
     "expected": "51/52", "accept": ["51/52", "51 of 52", "51 out of 52"],
     "gold_source": [
         "Education/CLAUDE.md",
         "Education/CONTRIBUTING.md",
         "Education/CROSS_PLATFORM.md",
     ]},
    {"name": "biged_idle_timeout",
     "query": "What is the idle timeout in seconds before BigEd workers go to sleep?",
     "expected": "10", "accept": ["10", "10 seconds", "10s"],
     "gold_source": [
         "Education/fleet/fleet.toml",
         "Education/biged-rs/fleet.toml",
         "Education/biged-rs/crates/biged-core/src/config.rs",
     ]},

    # ─── helix-context (10) ───────────────────────────────────────────
    {"name": "helix_expression_budget",
     "query": "What is the expression_tokens budget set in helix.toml?",
     "expected": "7000", "accept": ["7000", "7K", "7,000", "~7K"],
     "gold_source": [
         "helix-context/helix.toml",
         "helix-context/overnight_logs/broad_tighten_2026-05-12_1422_report.md",
     ]},
    {"name": "helix_max_genes_per_turn",
     "query": "What is the max_genes_per_turn cap in helix.toml?",
     "expected": "12", "accept": ["12"],
     "gold_source": [
         "helix-context/helix.toml",
         "helix-context/docs/config-reference.md",
     ]},
    {"name": "helix_rrf_k",
     "query": "What is the RRF k constant used by Helix Reciprocal Rank Fusion?",
     "expected": "60", "accept": ["60", "k=60", "k = 60"],
     "gold_source": [
         "helix-context/helix.toml",
         "helix-context/docs/config-reference.md",
         "helix-context/docs/specs/2026-05-08-stage-3-rrf-fusion.md",
     ]},
    {"name": "helix_cold_start_threshold",
     "query": "What is the cold_start_threshold gene count in the Helix knowledge store?",
     "expected": "10", "accept": ["10", "10 genes"],
     "gold_source": [
         "helix-context/helix.toml",
         "helix-context/docs/config-reference.md",
         "helix-context/CLAUDE.md",
     ]},
    {"name": "helix_session_window",
     "query": "What is the synthetic_session_window_s in seconds for grouping same-IP requests?",
     "expected": "300", "accept": ["300", "5 min", "5 minutes", "five minutes"],
     "gold_source": [
         "helix-context/helix.toml",
         "helix-context/docs/config-reference.md",
         "helix-context/CLAUDE.md",
     ]},
    {"name": "helix_headroom_port",
     "query": "What is the default port for the Headroom proxy that Helix can route through?",
     "expected": "8787", "accept": ["8787", "port 8787"],
     "gold_source": [
         "helix-context/helix.toml",
         "helix-context/docs/config-reference.md",
     ]},
    {"name": "helix_calibration_staleness",
     "query": "After how many days does Helix flag the know-confidence calibration as stale?",
     "expected": "30", "accept": ["30", "30 days"],
     "gold_source": [
         "helix-context/helix.toml",
         "helix-context/docs/specs/2026-05-08-stage-6-know-miss-blocks.md",
     ]},
    {"name": "helix_dense_encoder",
     "query": "Which dense embedding model does Helix use for Stage-2 recall?",
     "expected": "BGE-M3", "accept": ["BGE-M3", "bge-m3", "bge m3"],
     "gold_source": [
         "helix-context/CLAUDE.md",
         "helix-context/README.md",
         "helix-context/helix.toml",
     ]},
    {"name": "helix_filename_anchor",
     "query": "What is the filename_anchor_weight per-match boost in helix.toml?",
     "expected": "4.0", "accept": ["4.0", "4"],
     "gold_source": [
         "helix-context/helix.toml",
         "helix-context/docs/config-reference.md",
     ]},
    {"name": "helix_subpackages_count",
     "query": "Into how many sub-packages is helix_context organized after the PR #90 restructure?",
     "expected": "16", "accept": ["16", "sixteen"],
     "gold_source": [
         "helix-context/CLAUDE.md",
         "helix-context/docs/superpowers/plans/2026-05-13-readme-v3-plan.md",
     ]},
]


# Markers that indicate the model abstained rather than guessed.
ABSTAIN_MARKERS = [
    r"\bi (?:don't|cannot|can't|am unable to|do not) (?:find|know|determine)\b",
    r"\bno (?:relevant )?information\b",
    r"\bnot (?:available|found|present)\b",
    r"\binsufficient (?:context|information|data)\b",
    r"\bunable to (?:find|determine|answer)\b",
    r"\bcouldn't find\b",
]
ABSTAIN_RE = re.compile("|".join(ABSTAIN_MARKERS), re.IGNORECASE)

# Legacy renderer fallback for /context content. Modern path uses the
# structured ``agent.citations`` list (preferred); ``_citations`` (PR #109)
# is the canonical shared parser — we reuse its regex for the fallback
# instead of redefining one here.
LEGACY_GENE_SRC_RE = _citations.LEGACY_GENE_SRC_RE


# ── Setup logging ────────────────────────────────────────────────────────

def make_run_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_ROOT / f"claude_matrix_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def setup_logger(run_dir: Path) -> logging.Logger:
    log_path = run_dir / "run.log"
    logger = logging.getLogger("bench.claude_matrix")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            datefmt="%H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


# ── Swap helper (external-server path) ────────────────────────────────────

def swap_db_external(target_path: str, log: logging.Logger) -> dict:
    """POST /admin/swap-db when running against an externally-managed server.

    Used only with ``--external-server``. When the harness manages uvicorn
    itself (default), it uses BenchServer.switch which also handles
    cross-mode (blob<->sharded) restarts that this endpoint alone cannot.
    """
    log.info("swap-db (external) -> %s", target_path)
    try:
        resp = httpx.post(
            f"{HELIX_URL}/admin/swap-db",
            json={"path": target_path},
            timeout=SWAP_TIMEOUT_S,
        )
    except Exception as exc:
        return {"error": f"swap-db request failed: {exc}"}
    if resp.status_code != 200:
        return {"error": f"swap-db HTTP {resp.status_code}: {resp.text[:200]}"}
    body = resp.json()
    log.info("  swapped: %d genes in %s ms", body.get("genes", -1), body.get("elapsed_ms"))
    return body


# ── Retrieval-only probe via /context ────────────────────────────────────

def gold_match_rank(delivered_sources: list[str],
                    gold_sources: list[str]) -> int | None:
    """1-based rank of the first delivered source matching any gold source.

    ``delivered_sources`` is the ordered list ``/context`` returned
    (citation rank order). Returns the 1-based position of the earliest
    entry whose path contains any ``gold_sources`` substring (forward-slash
    normalized, case-insensitive), or ``None`` when nothing matches.

    Rank-aware generalization of the gold-delivered predicate:
    ``gold_match_rank(...) is not None`` is exactly the old boolean
    ``gold_delivered``. Empty/whitespace gold entries are skipped so a
    malformed needle cannot match every delivery.
    """
    gold_norm = [
        gs.replace("\\", "/").lower()
        for gs in gold_sources
        if gs and gs.strip()
    ]
    for i, src in enumerate(delivered_sources):
        norm = str(src or "").replace("\\", "/").lower()
        if any(g in norm for g in gold_norm):
            return i + 1
    return None


def retrieval_probe(query: str, gold_sources: list[str],
                    helix_url: str = HELIX_URL) -> dict:
    """Direct /context call to get retrieval signal independent of the model.

    Prefers structured ``agent.citations[].source`` (issue #101 fix),
    falls back to ``<GENE src="...">`` regex on the rendered content
    (still emitted by context_manager.py:1367 in the splice path).
    """
    t0 = time.perf_counter()
    try:
        resp = httpx.post(
            f"{helix_url}/context",
            json={"query": query, "decoder_mode": "none"},
            timeout=CONTEXT_TIMEOUT_S,
        )
    except Exception as exc:
        return {"status": "error", "error": str(exc),
                "latency_s": time.perf_counter() - t0}
    latency = time.perf_counter() - t0
    if resp.status_code != 200:
        return {"status": "error", "http": resp.status_code,
                "latency_s": latency}
    data = resp.json()
    entry = data[0] if isinstance(data, list) and data else {}
    content = entry.get("content", "") or ""
    citations = entry.get("agent", {}).get("citations", []) or []

    # Primary: structured citations.
    delivered_sources: list[str] = [
        str(c.get("source", "") or "") for c in citations
        if c.get("source")
    ]
    sources_via = "citations" if delivered_sources else None

    # Fallback: legacy regex on rendered content (still works today, kept
    # in case the renderer changes or for cases where citations are empty
    # but content has <GENE src=> wrappers).
    if not delivered_sources:
        delivered_sources = LEGACY_GENE_SRC_RE.findall(content)
        if delivered_sources:
            sources_via = "regex_fallback"

    gold_rank = gold_match_rank(delivered_sources, gold_sources)

    return {
        "status": "ok",
        "latency_s": latency,
        "delivered_count": len(delivered_sources),
        "delivered_sources": delivered_sources[:20],
        "sources_via": sources_via,
        "gold_delivered": gold_rank is not None,
        "gold_rank": gold_rank,
        "ellipticity": entry.get("context_health", {}).get("ellipticity"),
        "n_citations": len(citations),
    }


# ── Claude -p invocation ─────────────────────────────────────────────────

def run_claude(query: str, model: str, max_usd: float,
               cwd: str, log: logging.Logger) -> dict:
    """Run a single ``claude -p`` subprocess and capture structured output."""
    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--no-session-persistence",
        "--permission-mode", "bypassPermissions",
        "--model", model,
        "--max-budget-usd", str(max_usd),
        query,
    ]
    log.info("  claude -p (model=%s, budget=$%.2f): %s", model, max_usd, query[:60])
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_S,
            cwd=cwd,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "elapsed_s": CLAUDE_TIMEOUT_S}
    except Exception as exc:
        return {"status": "error", "error": str(exc),
                "elapsed_s": time.perf_counter() - t0}
    elapsed = time.perf_counter() - t0
    if proc.returncode != 0:
        return {
            "status": "exit_nonzero",
            "returncode": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-400:],
            "stdout_tail": (proc.stdout or "")[-400:],
            "elapsed_s": elapsed,
        }
    raw = proc.stdout.strip()
    try:
        result = json.loads(raw)
    except Exception:
        return {
            "status": "parse_error",
            "stdout_tail": raw[-400:],
            "elapsed_s": elapsed,
        }
    return {"status": "ok", "result": result, "elapsed_s": elapsed,
            "stderr_tail": (proc.stderr or "")[-200:] if proc.stderr else ""}


# ── Scoring ──────────────────────────────────────────────────────────────

def score_answer(answer_text: str, accept_substrings: list[str]) -> dict:
    """Return {-1, 0, +1} score plus diagnostics.

    +1: word-boundary match of any accept substring (correct)
     0: model abstained (says "I don't know" or similar)
    -1: confident answer but no accept-substring match (likely wrong)
    """
    text = (answer_text or "").strip()
    if not text:
        return {"score": 0, "reason": "empty"}

    for a in accept_substrings:
        if re.search(rf"\b{re.escape(a)}\b", text, re.IGNORECASE):
            return {"score": 1, "reason": f"accept-match:{a}",
                    "matched_token": a}

    if ABSTAIN_RE.search(text):
        return {"score": 0, "reason": "abstain"}

    return {"score": -1, "reason": "no-match-and-confident"}


# ── Per-needle execution (factored out so the inner loop is identical
#     whether the harness is managing uvicorn or running against an
#     external server) ─────────────────────────────────────────────────────

def run_one_needle(needle: dict, profile_key: str, model: str,
                   max_usd: float, claude_cwd: str, helix_url: str,
                   log: logging.Logger) -> dict:
    log.info("[%s] %s", profile_key, needle["name"])
    retr = retrieval_probe(needle["query"], needle["gold_source"], helix_url=helix_url)
    claude_r = run_claude(needle["query"], model, max_usd, claude_cwd, log)

    answer_text = ""
    tokens: dict = {}
    cost_usd = None
    if claude_r["status"] == "ok":
        res = claude_r["result"]
        answer_text = (
            res.get("result")
            or res.get("answer")
            or res.get("content")
            or ""
        )
        for k in ("total_tokens", "input_tokens", "output_tokens",
                  "cache_creation_input_tokens",
                  "cache_read_input_tokens"):
            if k in res:
                tokens[k] = res[k]
        if "usage" in res and isinstance(res["usage"], dict):
            for k, v in res["usage"].items():
                tokens.setdefault(k, v)
        cost_usd = (
            res.get("total_cost_usd")
            or res.get("total_cost")
            or res.get("cost_usd")
        )

    score = score_answer(answer_text, needle["accept"])

    record = {
        "profile": profile_key,
        "needle": needle["name"],
        "query": needle["query"],
        "expected": needle["expected"],
        "accept": needle["accept"],
        "gold_source": needle["gold_source"],
        "retrieval": retr,
        "claude_status": claude_r["status"],
        "claude_elapsed_s": claude_r.get("elapsed_s"),
        "answer_text": answer_text[:2000],
        "tokens": tokens,
        "cost_usd": cost_usd,
        "score": score["score"],
        "score_reason": score["reason"],
        "score_matched_token": score.get("matched_token"),
    }
    if claude_r["status"] != "ok":
        record["claude_debug"] = {
            k: v for k, v in claude_r.items() if k != "result"
        }

    in_tok = tokens.get("input_tokens", 0)
    out_tok = tokens.get("output_tokens", 0)
    cache_r = tokens.get("cache_read_input_tokens", 0)
    cache_w = tokens.get("cache_creation_input_tokens", 0)
    cost_s = f"${cost_usd:.4f}" if isinstance(cost_usd, (int, float)) else "?"
    rank = retr.get("gold_rank")
    retr_str = f"#{rank}" if rank else ("miss" if retr.get("status") == "ok" else "err")
    log.info(
        "  retr=%s score=%+d cost=%s in=%s out=%s cache_r=%s cache_w=%s",
        retr_str, score["score"],
        cost_s, in_tok, out_tok, cache_r, cache_w,
    )
    return record


# ── Main loop ────────────────────────────────────────────────────────────

def parse_filter(spec: str | None, all_keys: list[str]) -> set[str]:
    if not spec:
        return set()
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    bad = [p for p in parts if p not in all_keys]
    if bad:
        raise SystemExit(f"unknown profile(s): {bad}; available: {all_keys}")
    return set(parts)


def resolve_profiles(all_keys: list[str], only: set[str], skip: set[str],
                     include_untrusted: bool = False) -> list[str]:
    """Ordered list of profiles to run.

    Applies --only / --skip, then drops UNTRUSTED_PROFILES unless
    ``include_untrusted`` is set or the profile was explicitly named in
    ``only`` (an explicit by-name request wins). Order follows ``all_keys``.
    """
    keys = [k for k in all_keys
            if (not only or k in only) and k not in skip]
    if not include_untrusted:
        keys = [k for k in keys
                if k not in UNTRUSTED_PROFILES or k in only]
    return keys


def summarize_profile(per_needle: list[dict], n_needles: int) -> dict:
    n_correct = sum(1 for r in per_needle if r["score"] == 1)
    n_abstain = sum(1 for r in per_needle if r["score"] == 0)
    n_wrong = sum(1 for r in per_needle if r["score"] == -1)
    n_retr_hit = sum(1 for r in per_needle if r["retrieval"].get("gold_delivered"))
    total_score = sum(r["score"] for r in per_needle)
    total_cost = sum((r["cost_usd"] or 0.0) for r in per_needle)
    # mrr — rank-sensitive retrieval metric (issue #137). gold_delivered_rate
    # is blind to *where* the gold doc lands; MRR (mean of 1/gold_rank, misses
    # counted as 0) resolves a reshuffle the boolean cannot see.
    reciprocal_ranks = [
        1.0 / rank
        for r in per_needle
        if (rank := r["retrieval"].get("gold_rank"))
    ]
    mrr = (sum(reciprocal_ranks) / n_needles) if n_needles else 0
    return {
        "needles_run": len(per_needle),
        "answers": {
            "correct": n_correct,
            "abstain": n_abstain,
            "wrong": n_wrong,
            "score_sum": total_score,
            "score_normalized": (total_score / n_needles) if n_needles else 0,
        },
        "retrieval": {
            "gold_delivered_count": n_retr_hit,
            "gold_delivered_rate": n_retr_hit / n_needles if n_needles else 0,
            "mrr": round(mrr, 4),
        },
        "total_cost_usd": round(total_cost, 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="Comma-separated profiles to run")
    parser.add_argument("--skip", help="Comma-separated profiles to skip")
    parser.add_argument("--include-untrusted", action="store_true",
                        help="Include UNTRUSTED_PROFILES (xl-sharded; corrupt "
                             "fixture, see issue #133). Excluded by default.")
    parser.add_argument("--model", default="haiku",
                        help="claude -p --model arg (haiku/sonnet/opus/full id). "
                             "Default: haiku — cloud speed prioritized over model "
                             "quality for retrieval-quality bench. See "
                             "memory/bench_default_haiku.md.")
    parser.add_argument("--max-usd", type=float, default=0.30,
                        help="Per-question budget cap (sonnet smoke run averaged $0.11)")
    parser.add_argument("--cwd", default=r"F:\Projects\helix-context",
                        help="cwd for claude -p subprocess")
    parser.add_argument("--external-server", action="store_true",
                        help="Don't manage uvicorn; use a server you've started yourself. "
                             "Required for sharded fixtures to set HELIX_USE_SHARDS=1 "
                             "manually. Default: harness manages uvicorn via BenchServer.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Host for the managed server (ignored with --external-server)")
    parser.add_argument("--port", default=11437, type=int,
                        help="Port for the managed server (ignored with --external-server)")
    parser.add_argument("--health-timeout", default=180.0, type=float,
                        help="Seconds to wait for /health after uvicorn boot")
    args = parser.parse_args()

    if not MANIFEST.exists():
        print(f"!! manifest not found at {MANIFEST}; run freeze_matrix_manifest.py first",
              file=sys.stderr)
        return 2

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    all_keys = list(manifest["targets"].keys())
    only = parse_filter(args.only, all_keys)
    skip = parse_filter(args.skip, all_keys)
    keys = resolve_profiles(all_keys, only, skip, args.include_untrusted)

    run_dir = make_run_dir()
    log = setup_logger(run_dir)
    log.info("RUN START run_dir=%s", run_dir)
    log.info("model=%s max_usd_per_question=%.2f external_server=%s",
             args.model, args.max_usd, args.external_server)
    log.info("profiles in run: %s", keys)
    for k in all_keys:
        if k not in UNTRUSTED_PROFILES:
            continue
        if k in keys:
            log.warning("profile %r is UNTRUSTED: fixture corrupt, its "
                        "retr_hit / MRR are not meaningful (see issue #133)", k)
        else:
            log.info("untrusted profile %r excluded from this run "
                     "(see issue #133; --include-untrusted to force)", k)
    untrusted_excluded = [k for k in all_keys
                          if k in UNTRUSTED_PROFILES and k not in keys]

    overall: dict = {
        "run_dir": str(run_dir),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "max_usd_per_question": args.max_usd,
        "external_server": args.external_server,
        "untrusted_excluded": untrusted_excluded,
        "profiles": {},
    }

    helix_url = f"http://{args.host}:{args.port}"

    if args.external_server:
        # Legacy path: server is already running. We can only hot-swap (no
        # cross-mode handling). For sharded fixtures the operator must
        # have started the server with HELIX_USE_SHARDS=1.
        try:
            r = httpx.get(f"{helix_url}/stats", timeout=10)
            log.info("server /stats ok: %d genes", r.json().get("total_genes", -1))
        except Exception as exc:
            log.error("server not reachable at %s: %s", helix_url, exc)
            return 3
        rc = _run_loop_external(keys, manifest, run_dir, args, helix_url, log, overall)
    else:
        # Managed path: BenchServer handles boot + hot-swap + cross-mode
        # restarts automatically.
        rc = _run_loop_managed(keys, manifest, run_dir, args, helix_url, log, overall)

    overall["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(overall, indent=2), encoding="utf-8")
    log.info("=" * 60)
    log.info("RUN DONE. Summary at %s", summary_path)
    return rc


def _run_loop_external(keys, manifest, run_dir, args, helix_url, log, overall) -> int:
    for key in keys:
        target = manifest["targets"][key]
        log.info("=" * 60)
        log.info("PROFILE: %s (mode=%s)", key, target.get("mode"))

        if target.get("status") == "missing":
            log.warning("  target missing on disk, skipping")
            continue
        if target.get("mode") == "sharded":
            log.warning(
                "  sharded profile %s requires the external server to "
                "have been started with HELIX_USE_SHARDS=1; harness "
                "cannot enforce this in --external-server mode",
                key,
            )

        swap_result = swap_db_external(target["path"], log)
        if "error" in swap_result:
            log.error("  swap failed: %s", swap_result["error"])
            overall["profiles"][key] = {"swap_error": swap_result["error"]}
            continue

        per_needle = _run_profile(key, run_dir, args, helix_url, log)
        overall["profiles"][key] = summarize_profile(per_needle, len(NEEDLES))
    return 0


def _run_loop_managed(keys, manifest, run_dir, args, helix_url, log, overall) -> int:
    uvicorn_log = run_dir / "uvicorn.log"
    with BenchServer(
        host=args.host,
        port=args.port,
        health_timeout_s=args.health_timeout,
        log_to=uvicorn_log,
    ) as srv:
        for key in keys:
            target = manifest["targets"][key]
            log.info("=" * 60)
            log.info("PROFILE: %s (mode=%s)", key, target.get("mode"))

            if target.get("status") == "missing":
                log.warning("  target missing on disk, skipping")
                continue

            fixture = Fixture(
                name=key,
                db=str(target["path"]).replace("\\", "/"),
                sharded=(target.get("mode") == "sharded"),
                read_only=True,
            )
            try:
                swap = srv.switch(fixture)
            except Exception as exc:
                log.error("  switch failed: %s", exc)
                overall["profiles"][key] = {"swap_error": str(exc)}
                continue
            log.info("  %s in %.2fs (genes=%d, pid=%s)",
                     swap.mechanism, swap.elapsed_s, swap.genes, swap.server_pid)

            per_needle = _run_profile(key, run_dir, args, helix_url, log)
            overall["profiles"][key] = summarize_profile(per_needle, len(NEEDLES))
    return 0


def _run_profile(profile_key: str, run_dir: Path, args, helix_url: str,
                 log: logging.Logger) -> list[dict]:
    profile_path = run_dir / f"{profile_key}.jsonl"
    per_needle: list[dict] = []
    with open(profile_path, "w", encoding="utf-8") as f:
        for i, n in enumerate(NEEDLES):
            log.info("[%s %d/%d] %s", profile_key, i + 1, len(NEEDLES), n["name"])
            record = run_one_needle(
                n, profile_key, args.model, args.max_usd, args.cwd,
                helix_url, log,
            )
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            per_needle.append(record)
    total_correct = sum(1 for r in per_needle if r["score"] == 1)
    total_abstain = sum(1 for r in per_needle if r["score"] == 0)
    total_wrong = sum(1 for r in per_needle if r["score"] == -1)
    total_retr = sum(1 for r in per_needle if r["retrieval"].get("gold_delivered"))
    total_cost = sum((r["cost_usd"] or 0.0) for r in per_needle)
    reciprocal_ranks = [
        1.0 / rk for r in per_needle
        if (rk := r["retrieval"].get("gold_rank"))
    ]
    mrr = (sum(reciprocal_ranks) / len(NEEDLES)) if NEEDLES else 0.0
    log.info(
        "  PROFILE %s: correct=%d abstain=%d wrong=%d retr_hit=%d/%d "
        "mrr=%.3f cost=$%.4f",
        profile_key, total_correct, total_abstain, total_wrong,
        total_retr, len(NEEDLES), mrr, total_cost,
    )
    return per_needle


if __name__ == "__main__":
    sys.exit(main())
