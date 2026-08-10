"""Tamper-evident SCBench receipt tests."""

from __future__ import annotations

import json
import os
from unittest.mock import Mock

import pytest

from cymatix_context.integrations.scbench.receipts import (
    ArmReceipt,
    ArtifactRef,
    CheckpointReceipt,
    CodexReceipt,
    HostObservation,
    PairReceipt,
    ReceiptIntegrityError,
    ScoreReceipt,
    SyncReceipt,
    atomic_write_receipt,
    redact_secrets,
    verify_receipt_tree,
    write_artifact,
)


def _artifact(path: str, digest: str = "a" * 64) -> ArtifactRef:
    return ArtifactRef(
        path=path,
        sha256=digest,
        size_bytes=10,
        media_type="text/plain",
    )


def valid_checkpoint_receipt(
    *,
    arm: str = "control",
    checkpoint: int = 1,
    prompt: ArtifactRef | None = None,
    output: ArtifactRef | None = None,
    score: ArtifactRef | None = None,
) -> CheckpointReceipt:
    return CheckpointReceipt(
        campaign_id="campaign-1",
        pair_id="file-backup-r1",
        replicate=1,
        arm=arm,
        order_index=1 if arm == "control" else 2,
        problem="file-backup",
        checkpoint=checkpoint,
        config_hash="c" * 64,
        cymatix_commit="1" * 40,
        scbench_commit="2" * 40,
        codex_version="codex-cli 1.2.3",
        codex_model="gpt-5.6-luna",
        reasoning_effort="medium",
        image_digest="sha256:" + "d" * 64,
        auth_type="chatgpt_subscription",
        host=HostObservation(fingerprint="windows-x86_64", load="normal"),
        started_at="2026-08-10T12:00:00Z",
        ended_at="2026-08-10T12:00:02Z",
        elapsed_ms=2000,
        workspace_manifest_hash="e" * 64,
        prompt_artifact=prompt or _artifact("artifacts/prompt/control.txt"),
        sync=SyncReceipt(
            pre_manifest_hash="e" * 64,
            post_manifest_hash="f" * 64,
            genome_id="genome-file-backup-r1",
            pre_genome_checksum="0" * 64,
            post_genome_checksum="1" * 64,
            ingested=1,
            skipped=0,
            deleted_source_ids=(),
            latency_ms=12,
            verified=True,
        ),
        codex=CodexReceipt(
            exit_status=0,
            input_tokens=100,
            output_tokens=20,
            cached_input_tokens=0,
            rate_limit_events=(),
            result_artifact=output or _artifact("artifacts/output/control.jsonl"),
        ),
        score=ScoreReceipt(
            strict_pass_rate=1.0,
            isolated_pass_rate=1.0,
            erosion=0.0,
            verbosity=0.0,
            raw_artifact=score or _artifact("artifacts/score/control.json"),
        ),
        terminal_state="completed",
        valid=True,
        invalidation_reason=None,
        retry_of=None,
    )


def test_receipt_rejects_credentials_and_auth_contents(tmp_path):
    """Removing recursive credential scanning would persist secret material."""
    receipt = valid_checkpoint_receipt().model_dump(mode="json")
    receipt["codex"]["OPENAI_API_KEY"] = "sk-secret"

    with pytest.raises(ReceiptIntegrityError, match="credential"):
        atomic_write_receipt(tmp_path / "checkpoint.json", receipt)


def test_atomic_write_leaves_no_partial_file(monkeypatch, tmp_path):
    """Replacing the target before fsync succeeds would leave a false receipt."""
    target = tmp_path / "receipt.json"
    monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        atomic_write_receipt(target, valid_checkpoint_receipt())

    assert not target.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_write_artifact_is_content_addressed_and_replayable(tmp_path):
    """Writing artifacts under caller-chosen names would allow silent replacement."""
    reference = write_artifact(
        tmp_path,
        "prompt",
        "hello",
        media_type="text/plain",
    )

    assert reference.sha256 == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    assert reference.size_bytes == 5
    assert (tmp_path / reference.path).read_text(encoding="utf-8") == "hello"


def test_checkpoint_terminal_state_and_validity_must_agree():
    """A completed checkpoint marked invalid would make retry accounting ambiguous."""
    with pytest.raises(ValueError, match="completed checkpoint must be valid"):
        valid_checkpoint_receipt().model_copy(
            update={
                "valid": False,
                "invalidation_reason": "packet failed",
            },
        ).model_validate(
            {
                **valid_checkpoint_receipt().model_dump(mode="json"),
                "valid": False,
                "invalidation_reason": "packet failed",
            }
        )


def test_verify_receipt_tree_detects_tampered_artifact(tmp_path):
    """Skipping hash replay would accept modified prompts after a run."""
    prompt = write_artifact(tmp_path, "prompt", "original", media_type="text/plain")
    output = write_artifact(tmp_path, "output", "{}\n", media_type="application/jsonl")
    score = write_artifact(tmp_path, "score", "{}", media_type="application/json")
    receipt = valid_checkpoint_receipt(prompt=prompt, output=output, score=score)
    atomic_write_receipt(tmp_path / "checkpoint.json", receipt)
    (tmp_path / prompt.path).write_text("tampered", encoding="utf-8")

    with pytest.raises(ReceiptIntegrityError, match="artifact hash mismatch"):
        verify_receipt_tree(tmp_path)


def test_verify_receipt_tree_rejects_cymatix_block_in_control_prompt(tmp_path):
    """Allowing treatment text into control prompts would destroy arm symmetry."""
    prompt = write_artifact(
        tmp_path,
        "prompt",
        "checkpoint\n\n<cymatix_context version=\"1\">x</cymatix_context>",
        media_type="text/plain",
    )
    output = write_artifact(tmp_path, "output", "{}\n", media_type="application/jsonl")
    score = write_artifact(tmp_path, "score", "{}", media_type="application/json")
    atomic_write_receipt(
        tmp_path / "checkpoint.json",
        valid_checkpoint_receipt(prompt=prompt, output=output, score=score),
    )

    with pytest.raises(ReceiptIntegrityError, match="control prompt"):
        verify_receipt_tree(tmp_path)


def test_verify_receipt_tree_requires_paired_checkpoint_symmetry(tmp_path):
    """Accepting a one-arm checkpoint would bias operational invalidations."""
    prompt = write_artifact(tmp_path, "prompt", "checkpoint", media_type="text/plain")
    checkpoint = valid_checkpoint_receipt(prompt=prompt)
    checkpoint_ref = _artifact("receipts/control-cp1.json")
    pair = PairReceipt(
        campaign_id="campaign-1",
        pair_id="file-backup-r1",
        problem="file-backup",
        replicate=1,
        order=("control", "cymatix"),
        control=ArmReceipt(
            campaign_id="campaign-1",
            pair_id="file-backup-r1",
            problem="file-backup",
            replicate=1,
            arm="control",
            order_index=1,
            checkpoints=(checkpoint_ref,),
            valid=True,
            invalidation_reason=None,
            retry_of=None,
        ),
        cymatix=ArmReceipt(
            campaign_id="campaign-1",
            pair_id="file-backup-r1",
            problem="file-backup",
            replicate=1,
            arm="cymatix",
            order_index=2,
            checkpoints=(),
            valid=True,
            invalidation_reason=None,
            retry_of=None,
        ),
        valid=True,
        invalidation_reason=None,
        retry_of=None,
    )
    atomic_write_receipt(tmp_path / "control-cp1.json", checkpoint)
    atomic_write_receipt(tmp_path / "pair.json", pair)

    with pytest.raises(ReceiptIntegrityError, match="paired checkpoint"):
        verify_receipt_tree(tmp_path)


def test_redact_secrets_preserves_shape_without_secret_values():
    """Logging a nested auth value would leak the subscription credential."""
    value = {
        "auth": {"access_token": "top-secret", "type": "chatgpt_subscription"},
        "count": 3,
    }

    redacted = redact_secrets(value)

    assert redacted == {
        "auth": {"access_token": "[REDACTED]", "type": "chatgpt_subscription"},
        "count": 3,
    }
    assert "top-secret" not in json.dumps(redacted)
