"""Optional host-side SCBench adapter for the Cymatix Codex treatment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from slop_code.agent_runner.agent import Agent
from slop_code.agent_runner.agent import AgentConfigBase
from slop_code.agent_runner.agents.codex import CodexAgent
from slop_code.agent_runner.agents.codex import CodexConfig
from slop_code.agent_runner.credentials import ProviderCredential
from slop_code.agent_runner.models import AgentCostLimits
from slop_code.agent_runner.registry import register_agent
from slop_code.common.llms import APIPricing
from slop_code.common.llms import ModelDefinition
from slop_code.common.llms import ThinkingPreset

from .config import CampaignConfig
from .coordinator import PreparedPrompt
from .coordinator import TreatmentCoordinator
from .coordinator import TreatmentInvalidError


class CymatixCodexConfig(CodexConfig, agent_type="codex_cymatix"):
    """SCBench Codex configuration with benchmark-run identity fields."""

    type: Literal["codex_cymatix"] = "codex_cymatix"
    campaign_manifest: Path
    pair_id: str = Field(min_length=1)
    replicate: int = Field(ge=1)
    receipt_root: Path


class CymatixCodexAgent(CodexAgent):
    """Wrap stock Codex with deterministic pre/post checkpoint coordination."""

    RECEIPT_REFS_FILENAME = "cymatix-receipt-refs.json"

    def __init__(
        self,
        problem_name: str,
        verbose: bool,  # noqa: FBT001
        image: str,
        cost_limits: AgentCostLimits,
        pricing: APIPricing | None,
        credential: ProviderCredential | None,
        binary: str,
        model: str,
        timeout: int | None,
        thinking: ThinkingPreset | None,
        max_thinking_tokens: int | None,
        extra_args: list[str],
        env: dict[str, str],
        campaign_manifest: Path,
        pair_id: str,
        replicate: int,
        receipt_root: Path,
    ) -> None:
        super().__init__(
            problem_name=problem_name,
            verbose=verbose,
            image=image,
            cost_limits=cost_limits,
            pricing=pricing,
            credential=credential,
            binary=binary,
            model=model,
            timeout=timeout,
            thinking=thinking,
            max_thinking_tokens=max_thinking_tokens,
            extra_args=extra_args,
            env=env,
        )
        self.campaign_manifest = campaign_manifest
        self.pair_id = pair_id
        self.replicate = replicate
        self.receipt_root = receipt_root
        self.coordinator: TreatmentCoordinator | None = None
        self._last_prepared: PreparedPrompt | None = None

    @classmethod
    def _from_config(
        cls,
        config: AgentConfigBase,
        model: ModelDefinition,
        credential: ProviderCredential,
        problem_name: str,
        verbose: bool,  # noqa: FBT001
        image: str | None,
        thinking_preset: ThinkingPreset | None = None,
        thinking_max_tokens: int | None = None,
    ) -> Agent:
        if not isinstance(config, CymatixCodexConfig):
            raise TypeError(
                f"Expected CymatixCodexConfig, got {type(config).__name__}"
            )
        if image is None:
            raise ValueError("CymatixCodexAgent requires an image")

        thinking = thinking_preset
        max_thinking_tokens = thinking_max_tokens
        if thinking is None and max_thinking_tokens is None:
            thinking, max_thinking_tokens = model.get_thinking_config("codex")

        return cls(
            problem_name=problem_name,
            verbose=verbose,
            image=image,
            cost_limits=config.cost_limits,
            pricing=model.pricing,
            credential=credential,
            binary=config.binary,
            model=model.get_model_slug(credential.provider),
            timeout=config.timeout,
            thinking=thinking,
            max_thinking_tokens=max_thinking_tokens,
            extra_args=config.extra_args,
            env=config.env,
            campaign_manifest=config.campaign_manifest,
            pair_id=config.pair_id,
            replicate=config.replicate,
            receipt_root=config.receipt_root,
        )

    def _coordinator_or_raise(self) -> TreatmentCoordinator:
        if self.coordinator is None:
            raise TreatmentInvalidError("treatment coordinator is not initialized")
        return self.coordinator

    def _verify_reasoning_effort(self, campaign: CampaignConfig) -> None:
        expected = campaign.reasoning_effort
        rendered = self._build_command("SCBench reasoning-effort preflight")
        expected_argument = f'model_reasoning_effort="{expected}"'
        if self.thinking != expected or expected_argument not in rendered:
            raise TreatmentInvalidError(
                "campaign reasoning effort is not enforced in the Codex command: "
                f"expected {expected!r}, resolved {self.thinking!r}"
            )

    def setup(self, session) -> None:
        super().setup(session)
        coordinator: TreatmentCoordinator | None = None
        try:
            campaign = CampaignConfig.load(self.campaign_manifest)
            self._verify_reasoning_effort(campaign)
            problem = self.problem_name.replace("_", "-")
            coordinator = TreatmentCoordinator(
                campaign=campaign,
                pair_id=self.pair_id,
                replicate=self.replicate,
                problem=problem,
                workspace_root=session.working_dir,
                receipt_root=self.receipt_root,
            )
            self.coordinator = coordinator
            coordinator.initialize()
        except Exception:
            self.log.error(
                "agent.codex_cymatix.setup_failed",
                exc_info=True,
            )
            if coordinator is not None:
                coordinator.close()
                self.coordinator = None
            super().cleanup()
            raise

    def extract_final_agent_message(self) -> str:
        command = self._last_command
        if command is None or not isinstance(command.stdout, str):
            raise TreatmentInvalidError("Codex emitted no JSONL output")

        final_message: str | None = None
        for line in command.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "item.completed":
                continue
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "agent_message":
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                final_message = text
        if final_message is None:
            raise TreatmentInvalidError(
                "Codex emitted no terminal item.completed agent_message"
            )
        return final_message

    def run(self, task: str) -> None:
        coordinator = self._coordinator_or_raise()
        prepared = coordinator.before_checkpoint(task)
        self._last_prepared = prepared
        super().run(prepared.prompt)
        final_message = self.extract_final_agent_message()
        coordinator.after_checkpoint(task, final_message)

    def finish_checkpoint(self, reset_context: bool = True) -> None:
        self._coordinator_or_raise().finish_checkpoint()
        super().finish_checkpoint(reset_context=reset_context)

    def save_artifacts(self, path: Path) -> None:
        super().save_artifacts(path)
        prepared = self._last_prepared
        if prepared is None:
            return
        coordinator = self._coordinator_or_raise()
        references = {
            "schema_version": 1,
            "session_id": coordinator.session_id,
            "checkpoint": prepared.checkpoint_index,
            "warmup_elapsed_ms": coordinator.warmup_elapsed_ms,
            "initial_sync": (
                coordinator.initial_sync.model_dump(mode="json")
                if coordinator.initial_sync is not None
                else None
            ),
            "sync": (
                coordinator.last_sync.model_dump(mode="json")
                if coordinator.last_sync is not None
                else None
            ),
            "retrieval": (
                prepared.retrieval.model_dump(mode="json")
                if prepared.retrieval is not None
                else None
            ),
            "prompt": prepared.prompt_artifact.model_dump(mode="json"),
            "query": (
                prepared.query_artifact.model_dump(mode="json")
                if prepared.query_artifact is not None
                else None
            ),
            "packet": (
                prepared.packet_artifact.model_dump(mode="json")
                if prepared.packet_artifact is not None
                else None
            ),
            "rendered_packet": (
                prepared.rendered_packet_artifact.model_dump(mode="json")
                if prepared.rendered_packet_artifact is not None
                else None
            ),
        }
        (path / self.RECEIPT_REFS_FILENAME).write_text(
            json.dumps(references, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def cleanup(self) -> None:
        try:
            if self.coordinator is not None:
                self.coordinator.close()
        finally:
            super().cleanup()


register_agent("codex_cymatix", CymatixCodexAgent)
