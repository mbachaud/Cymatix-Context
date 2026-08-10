"""Paired SCBench campaign runner tests."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from cymatix_context.integrations.scbench.config import (
    CampaignConfig,
    PairSpec,
    SourcePolicy,
)
from cymatix_context.integrations.scbench.runner import (
    CampaignRunner,
    PairInvalidError,
    PreflightError,
    build_scbench_command,
)


CYMATIX_HEAD = "1" * 40
SCBENCH_HEAD = "2" * 40
CYMATIX_BASE = "0" * 40
SCBENCH_BASE = "b" * 40


def _campaign_data() -> dict:
    return {
        "campaign_id": "campaign-1",
        "cymatix_commit": CYMATIX_HEAD,
        "scbench_commit": SCBENCH_HEAD,
        "codex_version": "codex-cli 0.74.0",
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


@dataclass(frozen=True)
class HarnessLayout:
    campaign: CampaignConfig
    manifest: Path
    cymatix_root: Path
    scbench_root: Path
    problem_root: Path
    output_root: Path
    auth_path: Path


@pytest.fixture
def layout(tmp_path) -> HarnessLayout:
    cymatix_root = tmp_path / "cymatix"
    scbench_root = tmp_path / "scbench"
    campaign_root = (
        cymatix_root
        / "benchmarks"
        / "scbench"
        / "campaigns"
        / "campaign-1"
    )
    problem_root = tmp_path / "problems"
    output_root = campaign_root / "pairs"
    auth_path = tmp_path / ".codex" / "auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text("not-read-by-preflight", encoding="utf-8")

    contracts = cymatix_root / "benchmarks" / "scbench" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "cymatix-contract.json").write_text(
        json.dumps(
            {
                "base_commit": CYMATIX_BASE,
                "packet_session_delivery": True,
            }
        ),
        encoding="utf-8",
    )
    (contracts / "scbench-06b5c068.json").write_text(
        json.dumps({"commit": SCBENCH_BASE}),
        encoding="utf-8",
    )

    agents = scbench_root / "configs" / "agents"
    models = scbench_root / "configs" / "models"
    agents.mkdir(parents=True)
    models.mkdir(parents=True)
    common_agent = {
        "binary": "codex",
        "version": "0.74.0",
        "timeout": 7200,
        "extra_args": [],
        "env": {},
        "cost_limits": {
            "cost_limit": 0,
            "step_limit": 0,
            "net_cost_limit": 0,
            "max_retries": 0,
        },
    }
    (agents / "codex.yaml").write_text(
        yaml.safe_dump({"type": "codex", **common_agent}),
        encoding="utf-8",
    )
    (agents / "codex_cymatix.yaml").write_text(
        yaml.safe_dump(
            {
                "type": "codex_cymatix",
                **common_agent,
                "campaign_manifest": "__SET_BY_HARNESS__",
                "pair_id": "__SET_BY_HARNESS__",
                "replicate": 1,
                "receipt_root": "__SET_BY_HARNESS__",
            }
        ),
        encoding="utf-8",
    )
    (models / "gpt-5.6-luna.yaml").write_text(
        yaml.safe_dump(
            {
                "internal_name": "gpt-5.6-luna",
                "provider": "codex_auth",
                "thinking": "medium",
                "pricing": {},
            }
        ),
        encoding="utf-8",
    )

    problem = problem_root / "file_backup"
    problem.mkdir(parents=True)
    (problem / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "file_backup",
                "checkpoints": {
                    "checkpoint_1": {"order": 1},
                    "checkpoint_2": {"order": 2},
                },
            }
        ),
        encoding="utf-8",
    )

    campaign = CampaignConfig.model_validate(_campaign_data())
    campaign_root.mkdir(parents=True)
    manifest = campaign_root / "manifest.json"
    manifest.write_text(
        json.dumps(campaign.model_dump(mode="json")),
        encoding="utf-8",
    )
    return HarnessLayout(
        campaign=campaign,
        manifest=manifest,
        cymatix_root=cymatix_root,
        scbench_root=scbench_root,
        problem_root=problem_root,
        output_root=output_root,
        auth_path=auth_path,
    )


class FakeCommandRunner:
    def __init__(self, layout: HarnessLayout):
        self.layout = layout
        self.calls: list[tuple[list[str], dict]] = []
        self.arm_order: list[str] = []
        self.fail_arms: set[str] = set()
        self.rate_limit_arms: set[str] = set()
        self.reported_error_arms: set[str] = set()
        self.cymatix_head = CYMATIX_HEAD
        self.scbench_head = SCBENCH_HEAD
        self.cymatix_status = ""
        self.scbench_status = ""
        self.committed_cymatix_paths: tuple[str, ...] = ()
        self.spacy_available = True

    def __call__(self, command, **kwargs):
        command = [str(part) for part in command]
        self.calls.append((command, kwargs))
        assert kwargs["check"] is False
        assert kwargs["timeout"] > 0
        assert kwargs["creationflags"] == getattr(
            subprocess,
            "CREATE_NO_WINDOW",
            0,
        )

        if Path(command[0]).name.casefold() in {"cmd", "cmd.exe"}:
            assert command[1:4] == ["/d", "/s", "/c"]
            command = command[4:]
        executable = Path(command[0]).name.casefold()
        cwd = Path(kwargs["cwd"])
        if executable == "git" and command[1:3] == ["rev-parse", "HEAD"]:
            head = (
                self.cymatix_head
                if cwd == self.layout.cymatix_root
                else self.scbench_head
            )
            return subprocess.CompletedProcess(command, 0, head + "\n", "")
        if executable == "git" and command[1:3] == ["status", "--porcelain"]:
            status = (
                self.cymatix_status
                if cwd == self.layout.cymatix_root
                else self.scbench_status
            )
            return subprocess.CompletedProcess(command, 0, status, "")
        if executable == "git" and command[1:3] == [
            "merge-base",
            "--is-ancestor",
        ]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if executable == "git" and command[1:3] == ["diff", "--name-only"]:
            paths = (
                self.committed_cymatix_paths
                if cwd == self.layout.cymatix_root
                else ()
            )
            output = "".join(f"{path}\n" for path in paths)
            return subprocess.CompletedProcess(command, 0, output, "")
        if executable == "codex" and command[1:] == ["--version"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "codex-cli 0.74.0\n",
                "",
            )
        if executable == "codex" and command[1:] == ["login", "status"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "Logged in using ChatGPT\n",
                "",
            )
        if executable in {"python", "python.exe"} and command[1:2] == ["-c"]:
            return subprocess.CompletedProcess(
                command,
                0 if self.spacy_available else 1,
                "3.8.7\n" if self.spacy_available else "",
                "" if self.spacy_available else "No module named 'spacy'\n",
            )
        if executable == "docker" and command[1] == "info":
            return subprocess.CompletedProcess(command, 0, '"27.0"\n', "")
        if executable == "docker" and command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "sha256:same-image\n",
                "",
            )
        if "run" in command:
            arm = (
                "cymatix"
                if "codex_cymatix.yaml" in " ".join(command)
                else "control"
            )
            self.arm_order.append(arm)
            if arm in self.rate_limit_arms:
                kwargs["stderr"].write("HTTP 429: usage limit reached\n")
                kwargs["stderr"].flush()
            if arm not in self.fail_arms:
                self._write_terminal_artifacts(command, arm)
            return subprocess.CompletedProcess(
                command,
                1 if arm in self.fail_arms or arm in self.rate_limit_arms else 0,
                None,
                None,
            )
        raise AssertionError(f"unexpected command: {command}")

    def _write_terminal_artifacts(self, command: list[str], arm: str) -> None:
        save_dir = next(
            value.split("=", 1)[1]
            for value in command
            if value.startswith("save_dir=")
        )
        save_template = next(
            value.split("=", 1)[1]
            for value in command
            if value.startswith("save_template=")
        )
        run_root = Path(save_dir) / save_template
        problem_root = run_root / "file_backup"
        problem_root.mkdir(parents=True)
        (run_root / "checkpoint_results.jsonl").write_text(
            '{"checkpoint":"checkpoint_1"}\n'
            '{"checkpoint":"checkpoint_2"}\n',
            encoding="utf-8",
        )
        (problem_root / "run_info.yaml").write_text(
            yaml.safe_dump(
                {
                    "summary": {
                        "checkpoints": {
                            "checkpoint_1": "ran",
                            "checkpoint_2": "ran",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        for index in (1, 2):
            checkpoint = problem_root / f"checkpoint_{index}"
            agent_dir = checkpoint / "agent"
            (checkpoint / "snapshot").mkdir(parents=True)
            agent_dir.mkdir()
            (checkpoint / "inference_result.json").write_text(
                json.dumps({"had_error": arm in self.reported_error_arms}),
                encoding="utf-8",
            )
            (checkpoint / "snapshot" / "state.txt").write_text(
                f"{arm} checkpoint {index}",
                encoding="utf-8",
            )
            prompt = f"checkpoint {index}"
            if arm == "cymatix" and index > 1:
                prompt += (
                    '\n\n<cymatix_context version="1">'
                    "prior constraint</cymatix_context>"
                )
            (agent_dir / "prompt.txt").write_text(
                prompt,
                encoding="utf-8",
            )
            (agent_dir / "stdout.jsonl").write_text("{}\n", encoding="utf-8")
            (agent_dir / "stderr.log").write_text("", encoding="utf-8")
            if arm == "cymatix":
                (agent_dir / "cymatix-receipt-refs.json").write_text(
                    json.dumps({"checkpoint": index}),
                    encoding="utf-8",
                )


def _runner(layout: HarnessLayout, fake: FakeCommandRunner) -> CampaignRunner:
    return CampaignRunner(
        layout.campaign,
        manifest_path=layout.manifest,
        cymatix_root=layout.cymatix_root,
        scbench_root=layout.scbench_root,
        problem_root=layout.problem_root,
        output_root=layout.output_root,
        auth_path=layout.auth_path,
        run_command=fake,
        min_free_disk_bytes=1,
    )


def test_preflight_records_exact_clean_subscription_environment(layout):
    """Skipping any exact environment check would permit unrecorded drift."""
    fake = FakeCommandRunner(layout)
    runner = _runner(layout, fake)

    receipt = runner.preflight()

    assert receipt.valid is True
    assert receipt.observed["cymatix_commit"] == CYMATIX_HEAD
    assert receipt.observed["scbench_commit"] == SCBENCH_HEAD
    assert receipt.observed["auth_type"] == "chatgpt_subscription"
    assert receipt.observed["reasoning_effort"] == "medium"
    assert receipt.observed["docker_image_digest"] == "sha256:same-image"
    assert receipt.observed["spacy_version"] == "3.8.7"
    assert receipt.observed["checkpoint_counts"] == {"file-backup": 2}
    assert (layout.manifest.parent / "preflight.json").exists()


def test_preflight_rejects_missing_sidecar_conversation_dependency(layout):
    """A code-only smoke test must not hide a broken conversation ingest path."""
    fake = FakeCommandRunner(layout)
    fake.spacy_available = False
    runner = _runner(layout, fake)

    with pytest.raises(PreflightError, match="sidecar NLP dependency"):
        runner.preflight()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows command-shim contract")
def test_preflight_invokes_codex_through_windows_command_processor(layout):
    """CreateProcess cannot execute npm's extensionless/command shim directly."""
    fake = FakeCommandRunner(layout)
    runner = _runner(layout, fake)

    runner.preflight()

    codex_calls = [
        command
        for command, _kwargs in fake.calls
        if "codex" in command and "--version" in command
    ]
    assert len(codex_calls) == 1
    assert Path(codex_calls[0][0]).name.casefold() in {"cmd", "cmd.exe"}
    assert codex_calls[0][1:4] == ["/d", "/s", "/c"]


def test_preflight_mismatch_writes_invalid_receipt_and_blocks_execution(layout):
    """A commit mismatch must remain an invalid receipt, not a warning."""
    fake = FakeCommandRunner(layout)
    fake.scbench_head = "9" * 40
    runner = _runner(layout, fake)

    with pytest.raises(PreflightError, match="SCBench commit"):
        runner.preflight()
    with pytest.raises(PreflightError):
        runner.run_pair(PairSpec(problem="file-backup", replicate=1))

    data = json.loads(
        (layout.manifest.parent / "preflight.json").read_text(encoding="utf-8")
    )
    assert data["valid"] is False
    assert "SCBench commit" in data["errors"]


def test_preflight_accepts_metadata_only_commit_after_cymatix_baseline(layout):
    """A committed manifest cannot self-reference its own exact Git commit."""
    baseline = "3" * 40
    campaign = layout.campaign.model_copy(update={"cymatix_commit": baseline})
    layout.manifest.write_text(
        json.dumps(campaign.model_dump(mode="json")),
        encoding="utf-8",
    )
    fake = FakeCommandRunner(layout)
    fake.committed_cymatix_paths = (
        "benchmarks/scbench/campaigns/campaign-1/manifest.json",
        "benchmarks/scbench/README.md",
    )
    runner = CampaignRunner(
        campaign,
        manifest_path=layout.manifest,
        cymatix_root=layout.cymatix_root,
        scbench_root=layout.scbench_root,
        problem_root=layout.problem_root,
        output_root=layout.output_root,
        auth_path=layout.auth_path,
        run_command=fake,
        min_free_disk_bytes=1,
    )

    receipt = runner.preflight()

    assert receipt.valid is True
    assert receipt.observed["cymatix_baseline_commit"] == baseline
    assert receipt.observed["cymatix_commit"] == CYMATIX_HEAD


def test_preflight_rejects_committed_source_drift_after_cymatix_baseline(layout):
    """Baseline ancestry is insufficient when later commits alter implementation."""
    baseline = "3" * 40
    campaign = layout.campaign.model_copy(update={"cymatix_commit": baseline})
    layout.manifest.write_text(
        json.dumps(campaign.model_dump(mode="json")),
        encoding="utf-8",
    )
    fake = FakeCommandRunner(layout)
    fake.committed_cymatix_paths = ("cymatix_context/context_packet.py",)
    runner = CampaignRunner(
        campaign,
        manifest_path=layout.manifest,
        cymatix_root=layout.cymatix_root,
        scbench_root=layout.scbench_root,
        problem_root=layout.problem_root,
        output_root=layout.output_root,
        auth_path=layout.auth_path,
        run_command=fake,
        min_free_disk_bytes=1,
    )

    with pytest.raises(PreflightError, match="Cymatix commit"):
        runner.preflight()


def test_execution_rechecks_preflight_environment_for_drift(layout):
    """A valid receipt must not authorize execution after its checkout moves."""
    fake = FakeCommandRunner(layout)
    runner = _runner(layout, fake)
    runner.preflight()
    fake.scbench_head = "9" * 40

    with pytest.raises(PreflightError, match="SCBench commit drift"):
        runner.run_pair(PairSpec(problem="file-backup", replicate=1))

    assert fake.arm_order == []


def test_freshness_allows_receipts_but_rejects_source_drift(layout):
    """Harness outputs may grow in-tree, but source edits revoke authorization."""
    fake = FakeCommandRunner(layout)
    runner = _runner(layout, fake)
    runner.preflight()
    fake.cymatix_status = (
        "?? benchmarks/scbench/campaigns/campaign-1/preflight.json\n"
        "?? benchmarks/scbench/campaigns/campaign-1/pairs/evidence.json\n"
    )

    result = runner.run_pair(PairSpec(problem="file-backup", replicate=1))
    assert result.valid is True

    fake.cymatix_status += " M cymatix_context/context_packet.py\n"
    with pytest.raises(PreflightError, match="Cymatix worktree drift"):
        runner.run_pair(PairSpec(problem="file-backup", replicate=2))


def test_scbench_command_is_sequential_and_disables_concurrent_eval(layout):
    """Parallel or concurrent evaluation would contaminate paired timing."""
    pair = PairSpec(problem="file-backup", replicate=1)
    command = build_scbench_command(
        layout.campaign,
        pair,
        "control",
        scbench_root=layout.scbench_root,
        manifest_path=layout.manifest,
        arm_root=layout.output_root / "pair" / "control",
    )

    assert command[command.index("--num-workers") + 1] == "1"
    assert "--no-concurrent-evaluation" in command
    assert "--no-live-progress" in command
    assert "thinking=medium" in command
    assert "--resume" not in command
    assert "codex.yaml" in command[command.index("--agent") + 1]


def test_treatment_command_targets_loaded_agent_config_fields(layout):
    """SCBench only applies custom YAML fields through explicit agent.* overrides."""
    pair = PairSpec(problem="file-backup", replicate=1)
    command = build_scbench_command(
        layout.campaign,
        pair,
        "cymatix",
        scbench_root=layout.scbench_root,
        manifest_path=layout.manifest,
        arm_root=layout.output_root / "pair" / "cymatix",
    )

    overrides = {
        value.split("=", 1)[0]
        for value in command
        if value.startswith("agent.")
    }
    assert overrides == {
        "agent.campaign_manifest",
        "agent.pair_id",
        "agent.replicate",
        "agent.receipt_root",
    }


def test_pair_runner_inverts_arm_order_by_replicate(layout):
    """Running both replicates in AB order would retain an order confound."""
    fake = FakeCommandRunner(layout)
    runner = _runner(layout, fake)
    runner.preflight()

    runner.run_pair(PairSpec(problem="file-backup", replicate=1))
    runner.run_pair(PairSpec(problem="file-backup", replicate=2))

    assert fake.arm_order == ["control", "cymatix", "cymatix", "control"]


def test_invalid_first_arm_stops_before_its_mate(layout):
    """Starting the mate after terminal verification fails would score an invalid pair."""
    fake = FakeCommandRunner(layout)
    fake.fail_arms.add("control")
    runner = _runner(layout, fake)
    runner.preflight()

    with pytest.raises(PairInvalidError, match="control"):
        runner.run_pair(PairSpec(problem="file-backup", replicate=1))

    assert fake.arm_order == ["control"]
    attempt = json.loads(
        (
            layout.output_root
            / "file-backup-r1"
            / "control"
            / "attempt-001.json"
        ).read_text(encoding="utf-8")
    )
    assert attempt["valid"] is False


def test_reported_inference_error_stops_before_mate(layout):
    """A zero process status cannot turn an errored inference into a valid arm."""
    fake = FakeCommandRunner(layout)
    fake.reported_error_arms.add("control")
    runner = _runner(layout, fake)
    runner.preflight()

    with pytest.raises(PairInvalidError, match="reported an inference error"):
        runner.run_pair(PairSpec(problem="file-backup", replicate=1))

    assert fake.arm_order == ["control"]


def test_rate_limit_invalidates_pair_and_is_recorded(layout):
    """A subscription limit must pause the campaign, never become a model miss."""
    fake = FakeCommandRunner(layout)
    fake.rate_limit_arms.add("control")
    runner = _runner(layout, fake)
    runner.preflight()

    with pytest.raises(PairInvalidError, match="rate limit"):
        runner.run_pair(PairSpec(problem="file-backup", replicate=1))

    attempt = json.loads(
        (
            layout.output_root
            / "file-backup-r1"
            / "control"
            / "attempt-001.json"
        ).read_text(encoding="utf-8")
    )
    assert "rate limit" in attempt["invalidation_reason"]
    assert fake.arm_order == ["control"]


def test_resume_preserves_partial_arm_and_restarts_at_arm_boundary(layout):
    """Using SCBench checkpoint resume would reuse an unprovable treatment genome."""
    fake = FakeCommandRunner(layout)
    runner = _runner(layout, fake)
    runner.preflight()
    partial = (
        layout.output_root
        / "file-backup-r1"
        / "control"
        / "scbench"
        / "partial.txt"
    )
    partial.parent.mkdir(parents=True)
    partial.write_text("preserve me", encoding="utf-8")

    result = runner.resume_pair(PairSpec(problem="file-backup", replicate=1))

    assert result.valid is True
    assert partial.read_text(encoding="utf-8") == "preserve me"
    retry = json.loads(
        (
            layout.output_root
            / "file-backup-r1"
            / "control"
            / "attempt-002.json"
        ).read_text(encoding="utf-8")
    )
    assert retry["retry_of"] == "attempt-001"
    assert (
        layout.output_root
        / "file-backup-r1"
        / "control"
        / "attempt-002"
        / "scbench"
        / "checkpoint_results.jsonl"
    ).exists()
    run_commands = [command for command, _kwargs in fake.calls if "run" in command]
    assert all("--resume" not in command for command in run_commands)


def test_verify_receipts_detects_log_tampering(layout):
    """Ignoring captured-log hashes would allow post-run evidence changes."""
    fake = FakeCommandRunner(layout)
    runner = _runner(layout, fake)
    runner.preflight()
    runner.run_pair(PairSpec(problem="file-backup", replicate=1))
    stdout_log = (
        layout.output_root
        / "file-backup-r1"
        / "control"
        / "stdout.log"
    )
    stdout_log.write_text("tampered", encoding="utf-8")

    with pytest.raises(PairInvalidError, match="hash mismatch"):
        runner.verify_receipts()


def test_verify_receipts_detects_early_snapshot_tampering(layout):
    """Every checkpoint snapshot, not merely the final one, is scored evidence."""
    fake = FakeCommandRunner(layout)
    runner = _runner(layout, fake)
    runner.preflight()
    runner.run_pair(PairSpec(problem="file-backup", replicate=1))
    snapshot_file = (
        layout.output_root
        / "file-backup-r1"
        / "control"
        / "scbench"
        / "file_backup"
        / "checkpoint_1"
        / "snapshot"
        / "state.txt"
    )
    snapshot_file.write_text("tampered", encoding="utf-8")

    with pytest.raises(PairInvalidError, match="hash mismatch"):
        runner.verify_receipts()
