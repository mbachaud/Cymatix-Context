"""Frozen, machine-validated configuration for SCBench campaigns."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Arm = Literal["control", "cymatix"]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourcePolicy(_FrozenModel):
    """Allowlist boundary for agent-visible problem workspace sources."""

    extensions: tuple[str, ...]
    excluded_directories: tuple[str, ...]
    excluded_names: tuple[str, ...]
    max_file_bytes: int = Field(gt=0)

    @classmethod
    def default(cls) -> "SourcePolicy":
        return cls(
            extensions=(
                ".c",
                ".cc",
                ".cpp",
                ".cs",
                ".css",
                ".go",
                ".h",
                ".hpp",
                ".html",
                ".java",
                ".js",
                ".json",
                ".jsx",
                ".md",
                ".php",
                ".py",
                ".rb",
                ".rs",
                ".sh",
                ".sql",
                ".toml",
                ".ts",
                ".tsx",
                ".yaml",
                ".yml",
            ),
            excluded_directories=(
                ".git",
                ".mypy_cache",
                ".pytest_cache",
                ".tox",
                ".venv",
                "__pycache__",
                "benchmarks",
                "build",
                "dist",
                "evaluation",
                "node_modules",
                "receipts",
                "venv",
            ),
            excluded_names=(
                ".env",
                ".env.local",
                "auth.json",
                "evaluation.json",
            ),
            max_file_bytes=1_000_000,
        )


class TreatmentConfig(_FrozenModel):
    """Qualified Cymatix treatment settings; changes require a new campaign."""

    max_genes: Literal[8] = 8
    include_raw: Literal[True] = True
    max_item_chars: Literal[2000] = 2000
    max_packet_tokens: Literal[4000] = 4000
    tokenizer: Literal["o200k_base"] = "o200k_base"
    ignore_delivered: Literal[True] = True
    read_only: Literal[False] = False
    splade_enabled: Literal[False] = False
    ribosome_enabled: Literal[False] = False


class ReplicateSpec(_FrozenModel):
    id: Literal[1, 2]
    order: tuple[Arm, Arm]


class PairSpec(_FrozenModel):
    problem: str = Field(min_length=1)
    replicate: Literal[1, 2]


class ProblemGroups(_FrozenModel):
    """Predeclared pilot strata; each problem appears exactly once."""

    easy: tuple[str, ...]
    medium: tuple[str, ...]
    hard: tuple[str, ...]

    def flattened(self) -> tuple[str, ...]:
        return self.easy + self.medium + self.hard


class RetryPolicy(_FrozenModel):
    """Symmetric operational invalidation and retry rules."""

    agent_max_retries: Literal[0] = 0
    invalid_checkpoint_pair_action: Literal["clean_paired_rerun"] = (
        "clean_paired_rerun"
    )
    rate_limit_action: Literal["pause_campaign"] = "pause_campaign"
    resume_boundary: Literal["clean_checkpoint"] = "clean_checkpoint"
    record_failed_attempt: Literal[True] = True
    prove_workspace_and_genome_state: Literal[True] = True
    same_policy_both_arms: Literal[True] = True
    favorable_attempt_selection: Literal[False] = False
    invalidation_reasons: tuple[str, ...] = (
        "initial_or_incremental_ingest_failure",
        "genome_not_ready",
        "packet_http_schema_renderer_or_ceiling_failure",
        "source_or_conversation_verification_failure",
        "workspace_or_prompt_hash_mismatch",
        "missing_codex_result_or_corrupt_scbench_score",
        "excluded_evaluation_data_contamination",
        "unrecorded_version_or_configuration_drift",
    )


class GraduationThresholds(_FrozenModel):
    """The eight immutable pilot graduation gates from the approved design."""

    net_prevented_events_min: Literal[2] = 2
    relative_regression_reduction_min: Literal[0.15] = 0.15
    isolated_solve_difference_min: Literal[-0.025] = -0.025
    median_erosion_delta_max: Literal[0.03] = 0.03
    median_verbosity_delta_max: Literal[0.03] = 0.03
    median_input_token_ratio_max: Literal[1.25] = 1.25
    median_elapsed_ratio_max: Literal[1.30] = 1.30
    unresolved_integrity_failures_max: Literal[0] = 0


class CampaignMetadata(_FrozenModel):
    """Durable links and evidence identifiers that do not alter execution."""

    design_path: str = Field(min_length=1)
    plan_path: str = Field(min_length=1)
    qualification_pair_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qualification_operational_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fork_repository: str = Field(min_length=1)
    fork_branch: str = Field(min_length=1)
    tracking_issue_url: str | None = None


class CampaignConfig(_FrozenModel):
    """Canonical configuration shared by the runner, receipts, and analysis."""

    schema_version: Literal[1] = 1
    campaign_id: str = Field(min_length=1)
    cymatix_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    scbench_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    codex_version: str = Field(min_length=1)
    codex_model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)
    auth_type: Literal["chatgpt_subscription"] = "chatgpt_subscription"
    num_workers: Literal[1] = 1
    concurrent_evaluation: Literal[False] = False
    problems: tuple[str, ...] = Field(min_length=1)
    replicates: tuple[ReplicateSpec, ...]
    treatment: TreatmentConfig
    source_policy: SourcePolicy
    docker_image_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:.+$",
    )
    problem_groups: ProblemGroups | None = None
    expected_checkpoints: int | None = Field(default=None, gt=0)
    primary_checkpoints: int | None = Field(default=None, gt=0)
    bootstrap_seed: Literal[20260808] | None = None
    retry_policy: RetryPolicy | None = None
    graduation_thresholds: GraduationThresholds | None = None
    metadata: CampaignMetadata | None = None

    @model_validator(mode="after")
    def _validate_experimental_design(self) -> "CampaignConfig":
        normalized = tuple(problem.strip() for problem in self.problems)
        if any(not problem for problem in normalized):
            raise ValueError("problem names must be non-empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("problem names must be unique")

        actual = {(replicate.id, replicate.order) for replicate in self.replicates}
        expected = {
            (1, ("control", "cymatix")),
            (2, ("cymatix", "control")),
        }
        if actual != expected or len(self.replicates) != 2:
            raise ValueError(
                "replicates must contain exactly the AB and BA orders"
            )

        pilot_fields = (
            self.docker_image_digest,
            self.problem_groups,
            self.expected_checkpoints,
            self.primary_checkpoints,
            self.bootstrap_seed,
            self.retry_policy,
            self.graduation_thresholds,
        )
        if any(value is not None for value in pilot_fields) and not all(
            value is not None for value in pilot_fields
        ):
            raise ValueError("pilot fields must be set together")
        if self.problem_groups is not None:
            if self.problem_groups.flattened() != normalized:
                raise ValueError(
                    "problem_groups must exactly partition problems in manifest order"
                )
            assert self.expected_checkpoints is not None
            assert self.primary_checkpoints is not None
            expected_primary = self.expected_checkpoints - len(normalized)
            if self.primary_checkpoints != expected_primary:
                raise ValueError(
                    "primary_checkpoints must equal expected_checkpoints minus "
                    "one sentinel checkpoint per problem"
                )
        return self

    @classmethod
    def load(cls, path: Path) -> "CampaignConfig":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def config_hash(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def arm_order(self, replicate: int) -> tuple[Arm, Arm]:
        for spec in self.replicates:
            if spec.id == replicate:
                return spec.order
        raise ValueError(f"unknown replicate: {replicate}")
