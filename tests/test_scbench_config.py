"""Frozen SCBench campaign configuration tests."""

from __future__ import annotations

import json

import pytest

from cymatix_context.integrations.scbench.config import (
    CampaignConfig,
    PairSpec,
    SourcePolicy,
)


def valid_campaign_dict() -> dict:
    return {
        "schema_version": 1,
        "campaign_id": "2026-08-08-codex-pilot",
        "cymatix_commit": "a" * 40,
        "scbench_commit": "b" * 40,
        "codex_version": "codex-cli 1.2.3",
        "codex_model": "gpt-5-codex",
        "reasoning_effort": "high",
        "auth_type": "chatgpt_subscription",
        "num_workers": 1,
        "concurrent_evaluation": False,
        "problems": ["file-backup", "database-migration"],
        "replicates": [
            {"id": 1, "order": ["control", "cymatix"]},
            {"id": 2, "order": ["cymatix", "control"]},
        ],
        "treatment": {
            "max_genes": 8,
            "include_raw": True,
            "max_item_chars": 2000,
            "max_packet_tokens": 4000,
            "tokenizer": "o200k_base",
            "ignore_delivered": True,
            "read_only": False,
            "splade_enabled": False,
            "ribosome_enabled": False,
        },
        "source_policy": SourcePolicy.default().model_dump(mode="json"),
    }


def frozen_pilot_dict() -> dict:
    data = valid_campaign_dict()
    data.update(
        {
            "docker_image_digest": "sha256:" + "c" * 64,
            "problem_groups": {
                "easy": ["file-backup"],
                "medium": ["database-migration"],
                "hard": [],
            },
            "expected_checkpoints": 9,
            "primary_checkpoints": 7,
            "bootstrap_seed": 20260808,
            "retry_policy": {
                "agent_max_retries": 0,
                "invalid_checkpoint_pair_action": "clean_paired_rerun",
                "rate_limit_action": "pause_campaign",
                "resume_boundary": "clean_checkpoint",
                "record_failed_attempt": True,
                "prove_workspace_and_genome_state": True,
                "same_policy_both_arms": True,
                "favorable_attempt_selection": False,
            },
            "graduation_thresholds": {
                "net_prevented_events_min": 2,
                "relative_regression_reduction_min": 0.15,
                "isolated_solve_difference_min": -0.025,
                "median_erosion_delta_max": 0.03,
                "median_verbosity_delta_max": 0.03,
                "median_input_token_ratio_max": 1.25,
                "median_elapsed_ratio_max": 1.30,
                "unresolved_integrity_failures_max": 0,
            },
        }
    )
    return data


def test_campaign_accepts_frozen_balanced_configuration():
    campaign = CampaignConfig.model_validate(valid_campaign_dict())

    assert campaign.arm_order(1) == ("control", "cymatix")
    assert campaign.arm_order(2) == ("cymatix", "control")
    assert PairSpec(problem="file-backup", replicate=1).replicate == 1


def test_campaign_rejects_non_balanced_orders():
    data = valid_campaign_dict()
    data["replicates"] = [{"id": 1, "order": ["control", "cymatix"]}]

    with pytest.raises(ValueError, match="AB and BA"):
        CampaignConfig.model_validate(data)


def test_campaign_rejects_duplicate_problems():
    data = valid_campaign_dict()
    data["problems"] = ["file-backup", "file-backup"]

    with pytest.raises(ValueError, match="unique"):
        CampaignConfig.model_validate(data)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("num_workers",), 2, "num_workers"),
        (("concurrent_evaluation",), True, "concurrent_evaluation"),
        (("treatment", "max_genes"), 7, "max_genes"),
        (("treatment", "max_item_chars"), 1999, "max_item_chars"),
        (("treatment", "max_packet_tokens"), 3999, "max_packet_tokens"),
    ],
)
def test_campaign_rejects_non_frozen_execution_settings(path, value, message):
    data = valid_campaign_dict()
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(ValueError, match=message):
        CampaignConfig.model_validate(data)


def test_campaign_hash_is_canonical_and_ignores_manifest_location(tmp_path):
    data = valid_campaign_dict()
    left_path = tmp_path / "left" / "manifest.json"
    right_path = tmp_path / "right" / "renamed.json"
    left_path.parent.mkdir()
    right_path.parent.mkdir()
    left_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    right_path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")

    left = CampaignConfig.load(left_path)
    right = CampaignConfig.load(right_path)

    assert left.config_hash() == right.config_hash()
    assert len(left.config_hash()) == 64


def test_campaign_models_forbid_unknown_fields():
    data = valid_campaign_dict()
    data["unreviewed_knob"] = True

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        CampaignConfig.model_validate(data)


def test_campaign_accepts_complete_frozen_pilot_design():
    campaign = CampaignConfig.model_validate(frozen_pilot_dict())

    assert campaign.expected_checkpoints == 9
    assert campaign.primary_checkpoints == 7
    assert campaign.bootstrap_seed == 20260808
    assert campaign.graduation_thresholds.net_prevented_events_min == 2


def test_campaign_rejects_partial_frozen_pilot_design():
    data = frozen_pilot_dict()
    del data["bootstrap_seed"]

    with pytest.raises(ValueError, match="pilot fields must be set together"):
        CampaignConfig.model_validate(data)


def test_campaign_rejects_problem_group_drift():
    data = frozen_pilot_dict()
    data["problem_groups"]["hard"] = ["unfrozen-problem"]

    with pytest.raises(ValueError, match="problem_groups must exactly partition problems"):
        CampaignConfig.model_validate(data)


def test_campaign_rejects_primary_checkpoint_arithmetic_drift():
    data = frozen_pilot_dict()
    data["primary_checkpoints"] = 8

    with pytest.raises(ValueError, match="primary_checkpoints must equal"):
        CampaignConfig.model_validate(data)
