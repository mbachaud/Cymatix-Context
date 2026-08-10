"""Optional SCBench Cymatix Codex adapter tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("slop_code")

from slop_code.agent_runner.agents.cli_utils import AgentCommandResult
from slop_code.agent_runner.agents.codex import CodexAgent
from slop_code.agent_runner.models import AgentCostLimits
from slop_code.common.llms import APIPricing

from cymatix_context.integrations.scbench.agent import (
    CymatixCodexAgent,
    CymatixCodexConfig,
)
import cymatix_context.integrations.scbench.agent as agent_mod
from cymatix_context.integrations.scbench.coordinator import PreparedPrompt
from cymatix_context.integrations.scbench.receipts import (
    ArtifactRef,
    RetrievalReceipt,
    SyncReceipt,
)


def _artifact(path: str, digest: str) -> ArtifactRef:
    return ArtifactRef(
        path=path,
        sha256=digest,
        size_bytes=4,
        media_type="text/plain",
    )


def _prepared() -> PreparedPrompt:
    query = _artifact("artifacts/query/b.txt", "b" * 64)
    packet = _artifact("artifacts/packet/c.json", "c" * 64)
    rendered = _artifact("artifacts/rendered-packet/d.txt", "d" * 64)
    return PreparedPrompt(
        original_prompt="cp2",
        prompt=(
            "cp2\n\n<cymatix_context version=\"1\">"
            "prior constraint</cymatix_context>"
        ),
        packet=None,
        checkpoint_index=2,
        prompt_artifact=_artifact("artifacts/prompt/a.txt", "a" * 64),
        query_artifact=query,
        packet_artifact=packet,
        rendered_packet_artifact=rendered,
        retrieval=RetrievalReceipt(
            query_artifact=query,
            query_sha256=query.sha256,
            packet_artifact=packet,
            packet_sha256=packet.sha256,
            rendered_artifact=rendered,
            rendered_token_count=321,
            tokenizer="o200k_base",
            latency_ms=17,
            deleted_source_filtered=("repo://p/deleted.py",),
        ),
    )


class FakeCoordinator:
    def __init__(self):
        self.prepared = _prepared()
        self.before_prompts = []
        self.learned = []
        self.finished = 0
        self.closed = False
        self.session_id = "campaign:file-backup-r1:1:file-backup"
        self.warmup_elapsed_ms = 9
        self.initial_sync = _sync("0", "1")
        self.last_sync = _sync("1", "2")

    def before_checkpoint(self, prompt):
        self.before_prompts.append(prompt)
        return self.prepared

    def after_checkpoint(self, original_prompt, final_agent_message):
        self.learned.append((original_prompt, final_agent_message))

    def finish_checkpoint(self):
        self.finished += 1

    def close(self):
        self.closed = True


def _sync(before: str, after: str) -> SyncReceipt:
    return SyncReceipt(
        pre_manifest_hash=before * 64,
        post_manifest_hash=after * 64,
        genome_id="campaign:file-backup-r1:1:file-backup",
        pre_genome_checksum="3" * 64,
        post_genome_checksum="4" * 64,
        ingested=1,
        skipped=0,
        latency_ms=12,
        verified=True,
    )


def _adapter(tmp_path: Path) -> CymatixCodexAgent:
    agent = CymatixCodexAgent(
        problem_name="file_backup",
        verbose=False,
        image="test-image",
        cost_limits=AgentCostLimits(cost_limit=0, net_cost_limit=0),
        pricing=APIPricing(),
        credential=None,
        binary="codex",
        model="gpt-5.6-luna",
        timeout=7200,
        thinking="medium",
        max_thinking_tokens=None,
        extra_args=[],
        env={},
        campaign_manifest=tmp_path / "manifest.json",
        pair_id="file-backup-r1",
        replicate=1,
        receipt_root=tmp_path / "receipts",
    )
    agent.coordinator = FakeCoordinator()
    return agent


def _command(stdout: str) -> AgentCommandResult:
    return AgentCommandResult(
        result=None,
        steps=[],
        usage_totals={},
        stdout=stdout,
        stderr="",
    )


def test_config_registers_treatment_only_fields():
    """Dropping a harness field would make run identity impossible to replay."""
    config = CymatixCodexConfig(
        version="0.74.0",
        cost_limits=AgentCostLimits(cost_limit=0, net_cost_limit=0),
        campaign_manifest=Path("manifest.json"),
        pair_id="file-backup-r1",
        replicate=1,
        receipt_root=Path("receipts"),
    )

    assert config.type == "codex_cymatix"
    assert config.binary == "codex"
    assert config.pair_id == "file-backup-r1"


def test_adapter_passes_only_prepared_prompt_and_learns_final_message(
    monkeypatch,
    tmp_path,
):
    """Calling Codex with the bare task or learning traces breaks treatment isolation."""
    agent = _adapter(tmp_path)

    def run_base(instance, task):
        instance.observed_base_task = task
        instance._last_command = _command(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "reasoning", "text": "private"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "draft"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "final answer",
                            },
                        }
                    ),
                ]
            )
        )

    monkeypatch.setattr(CodexAgent, "run", run_base)

    agent.run("cp2")

    assert agent.observed_base_task == agent.coordinator.prepared.prompt
    assert agent.coordinator.before_prompts == ["cp2"]
    assert agent.coordinator.learned == [("cp2", "final answer")]


def test_extract_final_agent_message_ignores_malformed_and_non_agent_events(
    tmp_path,
):
    """Using the last JSONL item would ingest tool output or private reasoning."""
    agent = _adapter(tmp_path)
    agent._last_command = _command(
        "\n".join(
            [
                "not-json",
                '{"type":"item.completed","item":{"type":"reasoning","text":"secret"}}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"answer"}}',
                '{"type":"item.completed","item":{"type":"tool_output","text":"trace"}}',
            ]
        )
    )

    assert agent.extract_final_agent_message() == "answer"


def test_medium_reasoning_is_present_in_rendered_codex_command(tmp_path):
    """Recording medium without rendering it would invalidate the paired run."""
    command = _adapter(tmp_path)._build_command("checkpoint")

    assert 'model_reasoning_effort="medium"' in command


def test_finish_checkpoint_advances_coordinator_before_reset(tmp_path):
    """Resetting Codex without closing the sync boundary would desynchronize state."""
    agent = _adapter(tmp_path)
    agent._last_prompt = "prepared"

    agent.finish_checkpoint(reset_context=True)

    assert agent.coordinator.finished == 1
    assert agent._last_prompt == ""


def test_save_artifacts_keeps_native_files_and_adds_only_receipt_refs(tmp_path):
    """Copying raw Cymatix content into SCBench artifacts would expand ingest scope."""
    agent = _adapter(tmp_path)
    agent._last_prompt = agent.coordinator.prepared.prompt
    agent._last_prepared = agent.coordinator.prepared
    agent._last_command = _command('{"type":"turn.completed"}\n')
    output_dir = tmp_path / "checkpoint-artifacts"

    agent.save_artifacts(output_dir)

    assert (output_dir / "prompt.txt").read_text() == agent._last_prompt
    assert (output_dir / "stdout.jsonl").exists()
    references = json.loads(
        (output_dir / "cymatix-receipt-refs.json").read_text(encoding="utf-8")
    )
    serialized = json.dumps(references)
    assert references["session_id"] == agent.coordinator.session_id
    assert references["checkpoint"] == 2
    assert references["prompt"]["sha256"] == "a" * 64
    assert references["warmup_elapsed_ms"] == 9
    assert references["initial_sync"]["verified"] is True
    assert references["sync"]["post_manifest_hash"] == "2" * 64
    assert references["retrieval"]["latency_ms"] == 17
    assert references["retrieval"]["rendered_token_count"] == 321
    assert references["retrieval"]["deleted_source_filtered"] == [
        "repo://p/deleted.py"
    ]
    assert "prior constraint" not in serialized


def test_cleanup_closes_coordinator_and_codex_resources(tmp_path):
    """Leaving the treatment coordinator open would leak state into later pairs."""
    agent = _adapter(tmp_path)

    agent.cleanup()

    assert agent.coordinator.closed is True


def test_setup_failure_closes_partially_initialized_coordinator(monkeypatch, tmp_path):
    """A failed initial sync must not leave the spawned sidecar running."""
    agent = _adapter(tmp_path)

    class FailingCoordinator:
        instance = None

        def __init__(self, **_kwargs):
            self.closed = False
            FailingCoordinator.instance = self

        def initialize(self):
            raise RuntimeError("forced initialization failure")

        def close(self):
            self.closed = True

    monkeypatch.setattr(CodexAgent, "setup", lambda _self, _session: None)
    monkeypatch.setattr(CodexAgent, "cleanup", lambda _self: None)
    monkeypatch.setattr(
        agent_mod.CampaignConfig,
        "load",
        lambda _path: SimpleNamespace(reasoning_effort="medium"),
    )
    monkeypatch.setattr(agent, "_verify_reasoning_effort", lambda _campaign: None)
    monkeypatch.setattr(agent_mod, "TreatmentCoordinator", FailingCoordinator)

    with pytest.raises(RuntimeError, match="forced initialization failure"):
        agent.setup(SimpleNamespace(working_dir=tmp_path))

    assert FailingCoordinator.instance is not None
    assert FailingCoordinator.instance.closed is True
