"""Closed-loop SCBench treatment coordinator tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import cymatix_context.integrations.scbench.coordinator as coordinator_mod
from cymatix_context.integrations.scbench.client import (
    IngestVerification,
    PacketResponse,
)
from cymatix_context.integrations.scbench.config import (
    CampaignConfig,
    SourcePolicy,
)
from cymatix_context.integrations.scbench.coordinator import (
    TreatmentCoordinator,
    TreatmentInvalidError,
)


def _campaign() -> CampaignConfig:
    return CampaignConfig.model_validate(
        {
            "campaign_id": "campaign-1",
            "cymatix_commit": "1" * 40,
            "scbench_commit": "2" * 40,
            "codex_version": "codex-cli 1.2.3",
            "codex_model": "gpt-5.6-luna",
            "reasoning_effort": "medium",
            "problems": ["file-backup"],
            "replicates": [
                {"id": 1, "order": ["control", "cymatix"]},
                {"id": 2, "order": ["cymatix", "control"]},
            ],
            "treatment": {},
            "source_policy": SourcePolicy.default().model_dump(mode="json"),
        }
    )


@dataclass
class FakeClient:
    fail_operation: str | None = None
    health_ready: bool = True

    def __post_init__(self):
        self.gene_count = 0
        self.source_calls = []
        self.conversation_calls = []
        self.tombstone_calls = []
        self.packet_calls = []
        self.closed = False

    def _fail(self, operation: str) -> None:
        if self.fail_operation == operation:
            raise RuntimeError(f"forced {operation} failure")

    def wait_ready(self, *, deadline_s: float):
        self._fail("ready")
        return {
            "status": "ok",
            "checks": {"genome_ready": True},
            "genes": self.gene_count,
        }

    def stats(self):
        return {"total_genes": self.gene_count}

    def health(self):
        return {
            "status": "ok" if self.health_ready else "degraded",
            "checks": {"genome_ready": self.health_ready},
            "genes": self.gene_count,
        }

    def ingest_source(self, source):
        self._fail("ingest")
        self.source_calls.append(source)
        self.gene_count += 1
        return IngestVerification(
            source_id=source.source_id,
            gene_ids=(f"gene-{self.gene_count}",),
            count=1,
        )

    def ingest_conversation(self, **kwargs):
        self._fail("ingest")
        self.conversation_calls.append(kwargs)
        self.gene_count += 1
        return IngestVerification(
            source_id="conversation://campaign-1/file-backup-r1/1/1",
            gene_ids=(f"gene-{self.gene_count}",),
            count=1,
        )

    def tombstone(self, source_id):
        self._fail("tombstone")
        self.tombstone_calls.append(source_id)
        return ("gene-deleted",)

    def context_packet(self, query, **kwargs):
        self._fail("packet")
        self.packet_calls.append({"query": query, **kwargs})
        return PacketResponse(
            query=query,
            task_type=kwargs["task_type"],
            payload={
                "task_type": kwargs["task_type"],
                "query": query,
                "verified": [
                    {
                        "gene_id": "gene-1",
                        "source_id": "repo://file-backup/src/app.py",
                        "title": "Prior VALUE constraint",
                        "status": "verified",
                        "content": "Keep VALUE = 1 compatible",
                        "relevance_score": 0.9,
                        "live_truth_score": 1.0,
                    }
                ],
                "stale_risk": [],
                "contradictions": [],
                "refresh_targets": [],
                "notes": [],
            },
        )

    def close(self):
        self.closed = True


def _workspace(tmp_path):
    root = tmp_path / "file-backup"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root


def _coordinator(tmp_path, client, *, warmup_enabled=False):
    return TreatmentCoordinator(
        campaign=_campaign(),
        pair_id="file-backup-r1",
        replicate=1,
        problem="file-backup",
        workspace_root=_workspace(tmp_path),
        receipt_root=tmp_path / "receipts",
        client=client,
        warmup_enabled=warmup_enabled,
    )


def _finish_first_checkpoint(coordinator):
    coordinator.before_checkpoint("first")
    coordinator.after_checkpoint("first", "implemented first")
    coordinator.finish_checkpoint()


def test_checkpoint_one_is_aa_sentinel(tmp_path):
    """Injecting any packet at checkpoint one would break the A/A sentinel."""
    client = FakeClient()
    coordinator = _coordinator(tmp_path, client)
    coordinator.initialize()

    prepared = coordinator.before_checkpoint("build feature one")

    assert prepared.prompt == "build feature one"
    assert prepared.packet is None
    assert client.packet_calls == []


def test_initialization_accepts_empty_create_from_scratch_workspace(tmp_path):
    """Checkpoint one may legitimately create every allowlisted source."""
    client = FakeClient()
    workspace = tmp_path / "file-backup"
    workspace.mkdir()
    coordinator = TreatmentCoordinator(
        campaign=_campaign(),
        pair_id="file-backup-r1",
        replicate=1,
        problem="file-backup",
        workspace_root=workspace,
        receipt_root=tmp_path / "receipts",
        client=client,
        warmup_enabled=False,
    )

    coordinator.initialize()
    prepared = coordinator.before_checkpoint("create the initial implementation")

    assert coordinator.initial_sync is not None
    assert coordinator.initial_sync.ingested == 0
    assert coordinator.initial_sync.verified is True
    assert client.source_calls == []
    assert prepared.prompt == "create the initial implementation"
    assert prepared.packet is None


def test_initialization_warms_packet_path_without_scored_prompt(tmp_path):
    """Skipping warm-up would put model/server startup only in treatment timing."""
    client = FakeClient()
    coordinator = _coordinator(tmp_path, client, warmup_enabled=True)

    coordinator.initialize()

    assert len(client.packet_calls) == 1
    assert client.packet_calls[0]["query"].startswith("SCBench unscored warm-up")
    assert coordinator.warmup_elapsed_ms >= 0


def test_checkpoint_two_injects_after_verified_learning(tmp_path):
    """Retrieving before the prior checkpoint sync completes would use stale state."""
    client = FakeClient()
    coordinator = _coordinator(tmp_path, client)
    coordinator.initialize()
    _finish_first_checkpoint(coordinator)

    prepared = coordinator.before_checkpoint("second")

    assert '<cymatix_context version="1">' in prepared.prompt
    assert prepared.prompt.startswith("second\n\n<cymatix_context")
    assert client.packet_calls[0]["session_id"] == coordinator.session_id
    assert client.packet_calls[0]["treatment"].ignore_delivered is True
    assert prepared.prompt_artifact is not None
    assert prepared.query_artifact is not None
    assert prepared.packet_artifact is not None


def test_after_checkpoint_learns_only_final_message_and_syncs_delta(tmp_path):
    """Learning anything except the final agent message could ingest private traces."""
    client = FakeClient()
    coordinator = _coordinator(tmp_path, client)
    coordinator.initialize()
    coordinator.before_checkpoint("Keep VALUE compatible")
    root = coordinator.workspace_root
    (root / "src" / "app.py").unlink()
    (root / "src" / "accessor.py").write_text(
        "def get_value(): return 1\n",
        encoding="utf-8",
    )

    sync = coordinator.after_checkpoint(
        "Keep VALUE compatible",
        "I preserved the public VALUE contract",
    )

    assert client.conversation_calls[0]["content"] == (
        "User:\nKeep VALUE compatible\n\n"
        "Assistant:\nI preserved the public VALUE contract"
    )
    assert [item.source_id for item in client.source_calls][-1] == (
        "repo://file-backup/src/accessor.py"
    )
    assert client.tombstone_calls == ["repo://file-backup/src/app.py"]
    assert sync.verified is True
    assert coordinator.deleted_source_ids == {"repo://file-backup/src/app.py"}


def test_unready_health_invalidates_post_checkpoint_sync(tmp_path):
    """Accepting a degraded genome would turn a failed sync into scored evidence."""
    client = FakeClient()
    coordinator = _coordinator(tmp_path, client)
    coordinator.initialize()
    coordinator.before_checkpoint("first")
    client.health_ready = False

    with pytest.raises(TreatmentInvalidError, match="genome_ready"):
        coordinator.after_checkpoint("first", "implemented first")


@pytest.mark.parametrize("failure", ["ingest", "packet", "receipt"])
def test_treatment_failure_never_returns_bare_prompt(
    failure,
    monkeypatch,
    tmp_path,
):
    """Any treatment failure must invalidate the run instead of degrading to control."""
    client = FakeClient()
    coordinator = _coordinator(tmp_path, client)
    coordinator.initialize()
    if failure == "ingest":
        coordinator.before_checkpoint("first")
        client.fail_operation = "ingest"
        with pytest.raises(TreatmentInvalidError, match="ingest"):
            coordinator.after_checkpoint("first", "done")
    else:
        _finish_first_checkpoint(coordinator)
        if failure == "packet":
            client.fail_operation = "packet"
        else:
            def fail_write(*_args, **_kwargs):
                raise OSError("forced receipt failure")

            monkeypatch.setattr(coordinator_mod, "write_artifact", fail_write)
        with pytest.raises(TreatmentInvalidError, match=failure):
            coordinator.before_checkpoint("checkpoint two")

    with pytest.raises(TreatmentInvalidError):
        coordinator.before_checkpoint("must not become a bare prompt")


def test_close_releases_injected_client(tmp_path):
    """Leaking the sidecar client would contaminate later sequential pairs."""
    client = FakeClient()
    coordinator = _coordinator(tmp_path, client)
    coordinator.initialize()

    coordinator.close()

    assert client.closed is True
