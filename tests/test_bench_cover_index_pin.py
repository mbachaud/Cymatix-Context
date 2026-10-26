"""Bench drivers pin CYMATIX_COVER_INDEX_AUTOBUILD=0 before any store opens.

A background covering-index build during a measured run perturbs latency, so
every benchmark/measurement driver that opens an existing bed directly sets the
env var at import time, before its first ``cymatix_context`` import. The check
is static (AST), so no benchmark runs. ``ablation_ladder.py`` is excluded: it
takes an explicit ``--cover-index {off,prebuild}`` flag (tests/test_ladder_cover_index.py)
and the sweep runners pin the env in their child env and pass the flag.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENV = "CYMATIX_COVER_INDEX_AUTOBUILD"
MARKER = "# bench-pin: cover-index autobuild"
EXCLUDED = {"benchmarks/dogfood/erb/ablation_ladder.py"}

PINNED = [
    "benchmarks/ab_rerank_combinator.py",
    "benchmarks/ab_semantic_probe.py",
    "benchmarks/bench_oauth_scope.py",
    "benchmarks/cb_cymatix_pred.py",
    "benchmarks/cb_dpacket_clamp_rescore.py",
    "benchmarks/coderag_diag.py",
    "benchmarks/diag_rrf_tier_breadth.py",
    "benchmarks/dogfood/ablate_cymatics.py",
    "benchmarks/dogfood/erb/know_abstain_replay.py",
    "benchmarks/dogfood/erb/neutralize_ab.py",
    "benchmarks/dogfood/erb/probe_harmonic_off_dilution.py",
    "benchmarks/dogfood/erb/probe_head_lane_live.py",
    "benchmarks/dogfood/erb/probe_pool_depth.py",
    "benchmarks/dogfood/erb/sema_readgate_cell.py",
    "benchmarks/dogfood/erb/verify_a1_code_trace.py",
    "benchmarks/dogfood/hit_slate_sample.py",
    "benchmarks/dogfood/jev/sonnet_crater_search.py",
    "benchmarks/dogfood/mine_interlopers.py",
    "benchmarks/dogfood/run_baseline.py",
    "benchmarks/dogfood/run_expression_arms.py",
    "benchmarks/dogfood/run_ingest_throughput.py",
    "benchmarks/dogfood/run_ladder.py",
    "benchmarks/dogfood/run_latency_scaling.py",
    "benchmarks/dogfood/run_packet_replay.py",
    "benchmarks/dogfood/run_write_contention.py",
    "benchmarks/faithfulness/external/build_bed_239.py",
    "benchmarks/faithfulness/external/build_bed_239b.py",
    "benchmarks/faithfulness/external/dump_expressed.py",
    "benchmarks/faithfulness/real_cymatix_faithfulness.py",
    "benchmarks/located_n1000.py",
    "benchmarks/onyx_recall.py",
    "benchmarks/precision_probe.py",
    "benchmarks/precision_tie_break_ab.py",
    "benchmarks/repobench_r_cymatix.py",
    "benchmarks/repobench_r_cymatix_global.py",
    "scripts/bench_chain/s4_symbol_backfill.py",
    "scripts/bench_chain/sike_bed_ingest.py",
    "scripts/cross_compare_genomes.py",
    "scripts/fresh_genome_build.py",
    "scripts/bench_trace_e2e.py",
]


def _pin_and_import_lines(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    pin_line = None
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Subscript)
                and isinstance(node.targets[0].slice, ast.Constant)
                and node.targets[0].slice.value == ENV
                and isinstance(node.value, ast.Constant) and node.value.value == "0"):
            pin_line = node.lineno
            break
    import_lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and \
                node.module.split(".")[0] == "cymatix_context":
            import_lines.append(node.lineno)
        elif isinstance(node, ast.Import) and any(
                a.name.split(".")[0] == "cymatix_context" for a in node.names):
            import_lines.append(node.lineno)
    return pin_line, import_lines


@pytest.mark.parametrize("rel", PINNED)
def test_driver_pins_cover_index_before_any_store_open(rel):
    path = ROOT / rel
    assert path.exists(), rel
    src = path.read_text(encoding="utf-8")
    assert MARKER in src, f"{rel} lacks the bench-pin block"
    pin_line, import_lines = _pin_and_import_lines(path)
    assert pin_line is not None, f"{rel}: no top-level {ENV} = '0' assignment"
    # Drivers that only reach the store indirectly have no direct import to
    # order against; the pin is still required above.
    if import_lines:
        assert pin_line < min(import_lines), (
            f"{rel}: pin at line {pin_line} must precede cymatix_context imports "
            f"(first at {min(import_lines)})")


def test_every_store_opening_bench_driver_is_pinned():
    opens = []
    for base in ("benchmarks", "scripts"):
        for path in (ROOT / base).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel in EXCLUDED or "/tests/" in rel:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "CymatixContextManager(" in text or "KnowledgeStore(" in text:
                opens.append(rel)
    missing = sorted(set(opens) - set(PINNED) - EXCLUDED)
    assert not missing, f"store-opening drivers not pinned: {missing}"
