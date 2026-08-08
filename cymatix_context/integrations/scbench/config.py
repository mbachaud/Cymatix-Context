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
        return self

    @classmethod
    def load(cls, path: Path) -> "CampaignConfig":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def config_hash(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def arm_order(self, replicate: int) -> tuple[Arm, Arm]:
        for spec in self.replicates:
            if spec.id == replicate:
                return spec.order
        raise ValueError(f"unknown replicate: {replicate}")
