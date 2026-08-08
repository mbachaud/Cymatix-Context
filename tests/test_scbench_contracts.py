"""Pinned API and lifecycle contracts for the SCBench A/B harness."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CYMATIX_CONTRACT = (
    ROOT / "benchmarks" / "scbench" / "contracts" / "cymatix-contract.json"
)
SCBENCH_CONTRACT = (
    ROOT
    / "benchmarks"
    / "scbench"
    / "contracts"
    / "scbench-06b5c068.json"
)


def test_scbench_contracts_pin_required_surfaces() -> None:
    cymatix = json.loads(CYMATIX_CONTRACT.read_text(encoding="utf-8"))
    scbench = json.loads(SCBENCH_CONTRACT.read_text(encoding="utf-8"))

    assert cymatix["schema_version"] == 1
    assert len(cymatix["base_commit"]) == 40
    assert cymatix["routes"]["ingest"] == "/ingest"
    assert cymatix["routes"]["packet"] == "/context/packet"
    assert cymatix["routes"]["tombstone"] == "/admin/genes/tombstone"
    assert cymatix["health_ready_path"] == ["checks", "genome_ready"]
    assert cymatix["packet_session_delivery"] is False
    assert cymatix["config"]["splade_enabled"] == [
        "ingestion",
        "splade_enabled",
    ]
    assert cymatix["config"]["ribosome_enabled"] == [
        "ribosome",
        "enabled",
    ]

    assert scbench["schema_version"] == 1
    assert scbench["commit"] == "06b5c0687d4c05ee502e9696a4d0c22fc1eec5e0"
    assert scbench["codex_agent_class"] == (
        "slop_code.agent_runner.agents.codex.agent.CodexAgent"
    )
    assert scbench["checkpoint_reset"] == (
        "finish_checkpoint(reset_context=True)"
    )
    assert scbench["strict_field"] == "strict_pass_rate"
    assert scbench["isolated_field"] == "isolated_pass_rate"
    assert scbench["artifacts"]["prompt"] == "prompt.txt"
    assert scbench["artifacts"]["stdout"] == "stdout.jsonl"
    assert scbench["artifacts"]["stderr"] == "stderr.log"
