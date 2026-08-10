"""Fail-closed orchestration for paired SCBench campaign arms."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from .config import Arm, CampaignConfig, PairSpec


log = logging.getLogger("cymatix.scbench.runner")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_ARM_TIMEOUT_S = 14_400
_PROBE_TIMEOUT_S = 30


class PreflightError(RuntimeError):
    """The frozen campaign environment does not match its manifest."""


class PairInvalidError(RuntimeError):
    """A pair or one of its evidence chains is invalid."""


class _Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PreflightReceipt(_Receipt):
    receipt_type: Literal["preflight"] = "preflight"
    schema_version: Literal[1] = 1
    campaign_id: str
    config_hash: str
    generated_at: str
    valid: bool
    observed: dict[str, Any]
    checks: dict[str, bool]
    contract_hashes: dict[str, str]
    errors: tuple[str, ...]


class ArmAttemptReceipt(_Receipt):
    receipt_type: Literal["arm_attempt"] = "arm_attempt"
    schema_version: Literal[1] = 1
    campaign_id: str
    config_hash: str
    pair_id: str
    problem: str
    replicate: int
    arm: Arm
    attempt: int
    attempt_id: str
    output_path: str
    command: tuple[str, ...] | None = None
    command_sha256: str | None = None
    exit_code: int | None = None
    evidence_hashes: dict[str, str]
    valid: bool
    invalidation_reason: str | None = None
    retry_of: str | None = None
    generated_at: str


class PairRunReceipt(_Receipt):
    receipt_type: Literal["pair_run"] = "pair_run"
    schema_version: Literal[1] = 1
    campaign_id: str
    config_hash: str
    pair_id: str
    problem: str
    replicate: int
    order: tuple[Arm, Arm]
    arms: dict[str, str]
    valid: bool
    invalidation_reason: str | None = None
    generated_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: BaseModel | dict[str, Any]) -> None:
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    payload = json.dumps(data, sort_keys=True, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        log.warning("failed to write SCBench receipt %s", path, exc_info=True)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except Exception:
                log.warning("failed to remove temporary receipt", exc_info=True)
        raise


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON object required: {path}")
    return data


def _problem_slug(problem: str) -> str:
    return problem.replace("-", "_")


def _pair_id(pair: PairSpec) -> str:
    return f"{pair.problem}-r{pair.replicate}"


def _normalized_version(value: str) -> str:
    return value.strip().removeprefix("codex-cli ").strip()


def build_scbench_command(
    campaign: CampaignConfig,
    pair: PairSpec,
    arm: Arm,
    *,
    scbench_root: Path,
    manifest_path: Path,
    arm_root: Path,
) -> list[str]:
    """Build the pinned, sequential SCBench command for one complete arm."""
    executable = scbench_root / ".venv" / "Scripts" / "slop-code.exe"
    binary = str(executable) if executable.is_file() else "slop-code"
    agent_name = "codex_cymatix.yaml" if arm == "cymatix" else "codex.yaml"
    command = [
        binary,
        "run",
        "--agent",
        (scbench_root / "configs" / "agents" / agent_name).resolve().as_posix(),
        "--model",
        f"codex_auth/{campaign.codex_model}",
        "--problem",
        _problem_slug(pair.problem),
        "--num-workers",
        "1",
        "--no-concurrent-evaluation",
        "--no-live-progress",
        "--evaluate",
        f"thinking={campaign.reasoning_effort}",
        f"save_dir={arm_root.resolve().as_posix()}",
        "save_template=scbench",
    ]
    if arm == "cymatix":
        command.extend(
            [
                f"agent.campaign_manifest={manifest_path.resolve().as_posix()}",
                f"agent.pair_id={_pair_id(pair)}",
                f"agent.replicate={pair.replicate}",
                f"agent.receipt_root={(arm_root / 'receipts').resolve().as_posix()}",
            ]
        )
    return command


class CampaignRunner:
    """Run, resume, and verify fail-closed paired SCBench experiments."""

    def __init__(
        self,
        campaign: CampaignConfig,
        *,
        manifest_path: Path,
        cymatix_root: Path,
        scbench_root: Path,
        problem_root: Path,
        output_root: Path,
        auth_path: Path | None = None,
        run_command: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
        min_free_disk_bytes: int = 20 * 1024**3,
        probe_timeout_s: int = _PROBE_TIMEOUT_S,
        arm_timeout_s: int = _ARM_TIMEOUT_S,
    ) -> None:
        self.campaign = campaign
        self.manifest_path = manifest_path.resolve()
        self.cymatix_root = cymatix_root.resolve()
        self.scbench_root = scbench_root.resolve()
        self.problem_root = problem_root.resolve()
        self.output_root = output_root.resolve()
        self.auth_path = (auth_path or Path.home() / ".codex" / "auth.json").resolve()
        self.run_command = run_command
        self.min_free_disk_bytes = min_free_disk_bytes
        self.probe_timeout_s = probe_timeout_s
        self.arm_timeout_s = arm_timeout_s
        self.campaign_root = self.manifest_path.parent
        try:
            self.output_root.relative_to(self.campaign_root)
        except ValueError as exc:
            raise ValueError("output_root must be inside the campaign directory") from exc
        self.preflight_path = self.campaign_root / "preflight.json"
        self._preflight: PreflightReceipt | None = None

    def _probe(self, command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[Any]:
        probe_command = command
        if sys.platform == "win32" and Path(command[0]).name.casefold() == "codex":
            probe_command = [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/s",
                "/c",
                *command,
            ]
        return self.run_command(
            probe_command,
            cwd=cwd,
            check=False,
            timeout=self.probe_timeout_s,
            creationflags=_NO_WINDOW,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _yaml(path: Path) -> dict[str, Any]:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"YAML object required: {path}")
        return value

    def _cymatix_baseline_valid(self, head: str) -> bool:
        """Allow only committed campaign metadata after the implementation pin."""
        baseline = self.campaign.cymatix_commit
        if head == baseline:
            return True
        try:
            ancestor = self._probe(
                ["git", "merge-base", "--is-ancestor", baseline, head],
                cwd=self.cymatix_root,
            )
            if ancestor.returncode != 0:
                return False
            changed = self._probe(
                ["git", "diff", "--name-only", baseline, head, "--"],
                cwd=self.cymatix_root,
            )
            if changed.returncode != 0:
                return False
            campaign_prefix = (
                self.campaign_root.relative_to(self.cymatix_root).as_posix() + "/"
            )
            allowed_files = {"benchmarks/scbench/README.md"}
            paths = tuple(
                line.strip().replace("\\", "/")
                for line in (changed.stdout or "").splitlines()
                if line.strip()
            )
            return bool(paths) and all(
                path.startswith(campaign_prefix) or path in allowed_files
                for path in paths
            )
        except Exception:
            log.warning("Cymatix implementation baseline validation failed", exc_info=True)
            return False

    def preflight(self) -> PreflightReceipt:
        """Observe and strictly validate every frozen campaign dependency."""
        checks: dict[str, bool] = {}
        errors: list[str] = []
        observed: dict[str, Any] = {"auth_type": self.campaign.auth_type}
        contract_hashes: dict[str, str] = {}

        def record(name: str, valid: bool) -> None:
            checks[name] = bool(valid)
            if not valid:
                errors.append(name)

        def probe_text(command: list[str], cwd: Path) -> tuple[bool, str]:
            try:
                result = self._probe(command, cwd=cwd)
                output = result.stdout or result.stderr or ""
                return result.returncode == 0, output.strip()
            except Exception:
                log.warning("preflight probe failed: %s", command, exc_info=True)
                return False, ""

        manifest_valid = False
        try:
            manifest_valid = (
                CampaignConfig.load(self.manifest_path).config_hash()
                == self.campaign.config_hash()
            )
        except Exception:
            log.warning("campaign manifest validation failed", exc_info=True)
        record("campaign manifest", manifest_valid)

        cymatix_ok, cymatix_head = probe_text(
            ["git", "rev-parse", "HEAD"], self.cymatix_root
        )
        observed["cymatix_commit"] = cymatix_head
        observed["cymatix_baseline_commit"] = self.campaign.cymatix_commit
        record(
            "Cymatix commit",
            cymatix_ok and self._cymatix_baseline_valid(cymatix_head),
        )
        scbench_ok, scbench_head = probe_text(
            ["git", "rev-parse", "HEAD"], self.scbench_root
        )
        observed["scbench_commit"] = scbench_head
        record("SCBench commit", scbench_ok and scbench_head == self.campaign.scbench_commit)

        for label, root in (
            ("Cymatix worktree clean", self.cymatix_root),
            ("SCBench worktree clean", self.scbench_root),
        ):
            ok, status = probe_text(["git", "status", "--porcelain"], root)
            record(label, ok and not status)

        contract_specs = (
            ("cymatix-contract.json", self.cymatix_root, "base_commit"),
            ("scbench-06b5c068.json", self.scbench_root, "commit"),
        )
        contract_root = self.cymatix_root / "benchmarks" / "scbench" / "contracts"
        for name, repository, base_key in contract_specs:
            path = contract_root / name
            contract_ok = False
            try:
                payload = path.read_bytes()
                contract_hashes[name] = _sha256_bytes(payload)
                contract = json.loads(payload)
                base_commit = contract[base_key]
                ancestor = self._probe(
                    ["git", "merge-base", "--is-ancestor", base_commit, "HEAD"],
                    cwd=repository,
                )
                contract_ok = ancestor.returncode == 0
                if name == "cymatix-contract.json":
                    contract_ok = contract_ok and contract.get(
                        "packet_session_delivery"
                    ) is True
            except Exception:
                log.warning("contract validation failed: %s", path, exc_info=True)
            record(f"contract {name}", contract_ok)

        try:
            control = self._yaml(self.scbench_root / "configs" / "agents" / "codex.yaml")
            treatment = self._yaml(
                self.scbench_root / "configs" / "agents" / "codex_cymatix.yaml"
            )
            ignored = {
                "type",
                "campaign_manifest",
                "pair_id",
                "replicate",
                "receipt_root",
            }
            symmetric = {
                key: value for key, value in control.items() if key not in ignored
            } == {key: value for key, value in treatment.items() if key not in ignored}
            retry_values = (
                control.get("cost_limits", {}).get("max_retries"),
                treatment.get("cost_limits", {}).get("max_retries"),
            )
            record("agent configuration symmetry", symmetric)
            record("agent retries disabled", retry_values == (0, 0))
            record(
                "agent Codex version",
                _normalized_version(str(control.get("version", "")))
                == _normalized_version(self.campaign.codex_version),
            )
        except Exception:
            log.warning("agent configuration validation failed", exc_info=True)
            record("agent configuration symmetry", False)
            record("agent retries disabled", False)
            record("agent Codex version", False)

        try:
            model = self._yaml(
                self.scbench_root / "configs" / "models" / f"{self.campaign.codex_model}.yaml"
            )
            observed["reasoning_effort"] = model.get("thinking")
            record("model identity", model.get("internal_name") == self.campaign.codex_model)
            record("model provider", model.get("provider") == "codex_auth")
            record("reasoning effort", model.get("thinking") == self.campaign.reasoning_effort)
        except Exception:
            log.warning("model configuration validation failed", exc_info=True)
            record("model identity", False)
            record("model provider", False)
            record("reasoning effort", False)

        codex_ok, codex_version = probe_text(["codex", "--version"], self.cymatix_root)
        observed["codex_version"] = codex_version
        record(
            "host Codex version",
            codex_ok
            and _normalized_version(codex_version)
            == _normalized_version(self.campaign.codex_version),
        )
        login_ok, login_status = probe_text(
            ["codex", "login", "status"], self.cymatix_root
        )
        observed["codex_login_status"] = login_status
        record("Codex login", login_ok and "logged in" in login_status.casefold())
        record("Codex auth file", self.auth_path.is_file())

        sidecar_python_candidates = (
            self.scbench_root / ".venv" / "Scripts" / "python.exe",
            self.scbench_root / ".venv" / "bin" / "python",
        )
        sidecar_python = next(
            (path for path in sidecar_python_candidates if path.is_file()),
            Path(sys.executable),
        )
        spacy_ok, spacy_version = probe_text(
            [
                str(sidecar_python),
                "-c",
                (
                    "import spacy; "
                    "spacy.load('en_core_web_sm', disable=['lemmatizer']); "
                    "print(spacy.__version__)"
                ),
            ],
            self.scbench_root,
        )
        observed["spacy_version"] = spacy_version
        record("sidecar NLP dependency", spacy_ok and bool(spacy_version))

        docker_ok, docker_version = probe_text(
            ["docker", "info", "--format", "{{json .ServerVersion}}"],
            self.scbench_root,
        )
        observed["docker_version"] = docker_version
        record("Docker daemon", docker_ok)
        digests: list[str] = []
        agent_version = _normalized_version(self.campaign.codex_version)
        for agent in ("codex", "codex_cymatix"):
            tag = f"slop-code:{agent}-{agent_version}-python3.12"
            ok, digest = probe_text(
                ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                self.scbench_root,
            )
            record(f"Docker image {agent}", ok and bool(digest))
            if ok:
                digests.append(digest)
        observed["docker_image_digest"] = digests[0] if digests else ""
        record("Docker image parity", len(digests) == 2 and len(set(digests)) == 1)

        checkpoint_counts: dict[str, int] = {}
        for problem in self.campaign.problems:
            valid_problem = False
            try:
                problem_config = self._yaml(
                    self.problem_root / _problem_slug(problem) / "config.yaml"
                )
                checkpoints = problem_config.get("checkpoints")
                valid_problem = isinstance(checkpoints, dict) and bool(checkpoints)
                if isinstance(checkpoints, dict) and checkpoints:
                    checkpoint_counts[problem] = len(checkpoints)
            except Exception:
                log.warning("problem validation failed: %s", problem, exc_info=True)
            record(f"problem {problem}", valid_problem)
        observed["checkpoint_counts"] = checkpoint_counts

        output_empty = not self.output_root.exists() or not any(self.output_root.iterdir())
        record("campaign output empty", output_empty)
        disk_parent = self.output_root if self.output_root.exists() else self.output_root.parent
        try:
            free_bytes = shutil.disk_usage(disk_parent).free
        except Exception:
            log.warning("disk-space validation failed", exc_info=True)
            free_bytes = 0
        observed["free_disk_bytes"] = free_bytes
        record("free disk space", free_bytes >= self.min_free_disk_bytes)

        receipt = PreflightReceipt(
            campaign_id=self.campaign.campaign_id,
            config_hash=self.campaign.config_hash(),
            generated_at=_now(),
            valid=not errors,
            observed=observed,
            checks=checks,
            contract_hashes=contract_hashes,
            errors=tuple(errors),
        )
        _write_json(self.preflight_path, receipt)
        self._preflight = receipt
        if errors:
            raise PreflightError("preflight failed: " + ", ".join(errors))
        return receipt

    def _require_preflight(self) -> PreflightReceipt:
        receipt = self._preflight
        if receipt is None and self.preflight_path.is_file():
            try:
                receipt = PreflightReceipt.model_validate(_load_json(self.preflight_path))
            except Exception as exc:
                raise PreflightError(f"invalid preflight receipt: {exc}") from exc
        if receipt is None:
            raise PreflightError("preflight has not been completed")
        if not receipt.valid or receipt.config_hash != self.campaign.config_hash():
            raise PreflightError("valid preflight for this campaign is required")
        drift = self._preflight_drift_errors(receipt)
        if drift:
            raise PreflightError("preflight environment drift: " + ", ".join(drift))
        return receipt

    def _preflight_drift_errors(self, receipt: PreflightReceipt) -> list[str]:
        """Recheck mutable dependencies before authorizing a scored process."""
        errors: list[str] = []

        def probe(command: list[str], cwd: Path) -> subprocess.CompletedProcess[Any] | None:
            try:
                return self._probe(command, cwd=cwd)
            except Exception:
                log.warning("preflight freshness probe failed: %s", command, exc_info=True)
                return None

        try:
            manifest_hash = CampaignConfig.load(self.manifest_path).config_hash()
        except Exception:
            log.warning("campaign manifest freshness check failed", exc_info=True)
            manifest_hash = ""
        if manifest_hash != receipt.config_hash:
            errors.append("campaign manifest drift")

        for label, root, expected in (
            (
                "Cymatix",
                self.cymatix_root,
                str(receipt.observed.get("cymatix_commit", "")),
            ),
            ("SCBench", self.scbench_root, self.campaign.scbench_commit),
        ):
            head = probe(["git", "rev-parse", "HEAD"], root)
            observed_head = (head.stdout or "").strip() if head is not None else ""
            if head is None or head.returncode != 0 or observed_head != expected:
                errors.append(f"{label} commit drift")
            status = probe(["git", "status", "--porcelain"], root)
            status_text = (status.stdout or "") if status is not None else ""
            dirty = bool(status_text.strip())
            if label == "Cymatix" and dirty:
                dirty = self._has_non_campaign_worktree_changes(
                    root,
                    status_text,
                )
            if status is None or status.returncode != 0 or dirty:
                errors.append(f"{label} worktree drift")

        contract_root = self.cymatix_root / "benchmarks" / "scbench" / "contracts"
        for name, expected_hash in receipt.contract_hashes.items():
            path = contract_root / name
            try:
                actual_hash = _sha256_file(path)
            except Exception:
                log.warning("contract freshness check failed: %s", path, exc_info=True)
                actual_hash = ""
            if actual_hash != expected_hash:
                errors.append(f"contract drift: {name}")

        if not self.auth_path.is_file():
            errors.append("Codex auth file drift")
        codex = probe(["codex", "--version"], self.cymatix_root)
        codex_version = (
            (codex.stdout or codex.stderr or "").strip() if codex is not None else ""
        )
        if (
            codex is None
            or codex.returncode != 0
            or _normalized_version(codex_version)
            != _normalized_version(self.campaign.codex_version)
        ):
            errors.append("host Codex version drift")
        login = probe(["codex", "login", "status"], self.cymatix_root)
        login_text = (
            (login.stdout or login.stderr or "").strip() if login is not None else ""
        )
        if (
            login is None
            or login.returncode != 0
            or "logged in" not in login_text.casefold()
        ):
            errors.append("Codex login drift")

        docker = probe(
            ["docker", "info", "--format", "{{json .ServerVersion}}"],
            self.scbench_root,
        )
        if docker is None or docker.returncode != 0:
            errors.append("Docker daemon drift")
        expected_digest = receipt.observed.get("docker_image_digest")
        agent_version = _normalized_version(self.campaign.codex_version)
        for agent in ("codex", "codex_cymatix"):
            image = probe(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    f"slop-code:{agent}-{agent_version}-python3.12",
                ],
                self.scbench_root,
            )
            digest = (image.stdout or "").strip() if image is not None else ""
            if image is None or image.returncode != 0 or digest != expected_digest:
                errors.append(f"Docker image drift: {agent}")

        expected_counts = receipt.observed.get("checkpoint_counts", {})
        for problem in self.campaign.problems:
            try:
                count = self._checkpoint_count(problem)
            except Exception:
                log.warning("problem freshness check failed: %s", problem, exc_info=True)
                count = -1
            if count != expected_counts.get(problem):
                errors.append(f"problem checkpoint drift: {problem}")
        return errors

    def _has_non_campaign_worktree_changes(self, root: Path, status: str) -> bool:
        """Permit only harness-generated files below an in-repo campaign root."""
        try:
            campaign_relative = self.campaign_root.relative_to(root).as_posix()
        except ValueError:
            return bool(status.strip())
        if campaign_relative in {"", "."}:
            return bool(status.strip())
        prefix = campaign_relative.rstrip("/") + "/"
        for line in status.splitlines():
            if not line.strip():
                continue
            candidate = line[3:] if len(line) > 3 else line
            if " -> " in candidate:
                candidate = candidate.rsplit(" -> ", 1)[1]
            candidate = candidate.strip().strip('"').replace("\\", "/")
            if candidate != campaign_relative and not candidate.startswith(prefix):
                return True
        return False

    def _attempt_paths(
        self, pair: PairSpec, arm: Arm, attempt: int
    ) -> tuple[Path, Path, Path, Path]:
        arm_root = self.output_root / _pair_id(pair) / arm
        container = arm_root if attempt == 1 else arm_root / f"attempt-{attempt:03d}"
        return (
            arm_root,
            container,
            container / "scbench",
            arm_root / f"attempt-{attempt:03d}.json",
        )

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.campaign_root.resolve()).as_posix()

    def _terminal_evidence(self, pair: PairSpec, arm: Arm, run_root: Path) -> dict[str, str]:
        checkpoint_names = self._checkpoint_names(pair.problem)
        expected_count = len(checkpoint_names)
        results = run_root / "checkpoint_results.jsonl"
        if not results.is_file():
            raise PairInvalidError(f"{arm}: checkpoint_results.jsonl is missing")
        lines = [line for line in results.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(lines) != expected_count:
            raise PairInvalidError(
                f"{arm}: expected {expected_count} checkpoint results, got {len(lines)}"
            )
        for line in lines:
            json.loads(line)

        problem_root = run_root / _problem_slug(pair.problem)
        run_info = problem_root / "run_info.yaml"
        info = self._yaml(run_info)
        statuses = info.get("summary", {}).get("checkpoints")
        expected_names = set(checkpoint_names)
        if not isinstance(statuses, dict) or set(statuses) != expected_names:
            raise PairInvalidError(f"{arm}: run_info checkpoint set mismatch")
        if any(value != "ran" for value in statuses.values()):
            raise PairInvalidError(f"{arm}: one or more checkpoints did not run")

        evidence = {
            self._relative(path): _sha256_file(path) for path in (results, run_info)
        }
        for index, checkpoint_name in enumerate(checkpoint_names, start=1):
            checkpoint = problem_root / checkpoint_name
            agent = checkpoint / "agent"
            required = (
                checkpoint / "inference_result.json",
                agent / "prompt.txt",
                agent / "stdout.jsonl",
                agent / "stderr.log",
            )
            if not (checkpoint / "snapshot").is_dir() or any(
                not path.is_file() for path in required
            ):
                raise PairInvalidError(f"{arm}: {checkpoint_name} is incomplete")
            inference = _load_json(checkpoint / "inference_result.json")
            if inference.get("had_error") is not False:
                raise PairInvalidError(
                    f"{arm}: {checkpoint_name} reported an inference error"
                )
            prompt = (agent / "prompt.txt").read_text(encoding="utf-8")
            refs = agent / "cymatix-receipt-refs.json"
            marker = '<cymatix_context version="1">'
            if arm == "control" and (refs.exists() or marker in prompt):
                raise PairInvalidError("control: treatment evidence leaked into control")
            if arm == "cymatix":
                if not refs.is_file():
                    raise PairInvalidError(
                        f"cymatix: {checkpoint_name} receipt refs missing"
                    )
                if index == 1 and marker in prompt:
                    raise PairInvalidError("cymatix: checkpoint_1 must not receive a packet")
                if index > 1 and marker not in prompt:
                    raise PairInvalidError(
                        f"cymatix: {checkpoint_name} packet marker missing"
                    )
                evidence[self._relative(refs)] = _sha256_file(refs)
            for path in required:
                evidence[self._relative(path)] = _sha256_file(path)
            snapshot_files = sorted(
                path for path in (checkpoint / "snapshot").rglob("*") if path.is_file()
            )
            snapshot_manifest = b"".join(
                path.relative_to(checkpoint / "snapshot").as_posix().encode("utf-8")
                + b"\0"
                + _sha256_file(path).encode("ascii")
                + b"\n"
                for path in snapshot_files
            )
            files_hash_key = self._relative(checkpoint / "snapshot") + "/.tree"
            evidence[files_hash_key] = _sha256_bytes(snapshot_manifest)
        return evidence

    def _checkpoint_count(self, problem: str) -> int:
        return len(self._checkpoint_names(problem))

    def _checkpoint_names(self, problem: str) -> tuple[str, ...]:
        data = self._yaml(self.problem_root / _problem_slug(problem) / "config.yaml")
        checkpoints = data.get("checkpoints")
        if not isinstance(checkpoints, dict) or not checkpoints:
            raise PairInvalidError(f"problem checkpoint configuration invalid: {problem}")
        try:
            return tuple(
                name
                for name, _config in sorted(
                    checkpoints.items(),
                    key=lambda item: int(item[1]["order"]),
                )
            )
        except Exception as exc:
            raise PairInvalidError(
                f"problem checkpoint ordering invalid: {problem}"
            ) from exc

    def _write_partial_receipt(self, pair: PairSpec, arm: Arm) -> ArmAttemptReceipt:
        arm_root, _container, run_root, receipt_path = self._attempt_paths(pair, arm, 1)
        receipt = ArmAttemptReceipt(
            campaign_id=self.campaign.campaign_id,
            config_hash=self.campaign.config_hash(),
            pair_id=_pair_id(pair),
            problem=pair.problem,
            replicate=pair.replicate,
            arm=arm,
            attempt=1,
            attempt_id="attempt-001",
            output_path=self._relative(run_root),
            evidence_hashes={},
            valid=False,
            invalidation_reason="partial arm found without a terminal receipt",
            generated_at=_now(),
        )
        arm_root.mkdir(parents=True, exist_ok=True)
        _write_json(receipt_path, receipt)
        return receipt

    def _run_arm(
        self,
        pair: PairSpec,
        arm: Arm,
        attempt: int,
        *,
        retry_of: str | None,
    ) -> ArmAttemptReceipt:
        _arm_root, container, run_root, receipt_path = self._attempt_paths(
            pair, arm, attempt
        )
        container.mkdir(parents=True, exist_ok=True)
        stdout_path = container / "stdout.log"
        stderr_path = container / "stderr.log"
        command = build_scbench_command(
            self.campaign,
            pair,
            arm,
            scbench_root=self.scbench_root,
            manifest_path=self.manifest_path,
            arm_root=container,
        )
        env = os.environ.copy()
        prior_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(self.cymatix_root) + (
            os.pathsep + prior_pythonpath if prior_pythonpath else ""
        )
        env["PYTHONUTF8"] = "1"
        exit_code: int | None = None
        invalidation: str | None = None
        evidence: dict[str, str] = {}
        try:
            with stdout_path.open("w", encoding="utf-8", newline="\n") as stdout, stderr_path.open(
                "w", encoding="utf-8", newline="\n"
            ) as stderr:
                result = self.run_command(
                    command,
                    cwd=self.scbench_root,
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                    check=False,
                    timeout=self.arm_timeout_s,
                    creationflags=_NO_WINDOW,
                )
            exit_code = result.returncode
            evidence[self._relative(stdout_path)] = _sha256_file(stdout_path)
            evidence[self._relative(stderr_path)] = _sha256_file(stderr_path)
            if exit_code != 0:
                stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
                failure_text = stderr_text.casefold()
                rate_limit_markers = (
                    "429",
                    "rate limit",
                    "rate_limit",
                    "usage limit",
                    "quota exceeded",
                )
                if any(marker in failure_text for marker in rate_limit_markers):
                    invalidation = f"{arm} rate limit invalidation (status {exit_code})"
                else:
                    invalidation = f"{arm} process exited with status {exit_code}"
            else:
                try:
                    evidence.update(self._terminal_evidence(pair, arm, run_root))
                except Exception as exc:
                    invalidation = f"{arm} terminal verification failed: {exc}"
        except Exception as exc:
            log.warning("SCBench arm execution failed", exc_info=True)
            invalidation = f"{arm} execution failed: {exc}"
            for path in (stdout_path, stderr_path):
                if path.is_file():
                    evidence[self._relative(path)] = _sha256_file(path)

        receipt = ArmAttemptReceipt(
            campaign_id=self.campaign.campaign_id,
            config_hash=self.campaign.config_hash(),
            pair_id=_pair_id(pair),
            problem=pair.problem,
            replicate=pair.replicate,
            arm=arm,
            attempt=attempt,
            attempt_id=f"attempt-{attempt:03d}",
            output_path=self._relative(run_root),
            command=tuple(command),
            command_sha256=_sha256_bytes(_canonical_json(command)),
            exit_code=exit_code,
            evidence_hashes=evidence,
            valid=invalidation is None,
            invalidation_reason=invalidation,
            retry_of=retry_of,
            generated_at=_now(),
        )
        _write_json(receipt_path, receipt)
        return receipt

    def _validate_pair(self, pair: PairSpec) -> None:
        if pair.problem not in self.campaign.problems:
            raise ValueError(f"problem is not in campaign: {pair.problem}")
        self.campaign.arm_order(pair.replicate)

    def run_pair(self, pair: PairSpec) -> PairRunReceipt:
        """Run a new pair, stopping before the mate if its first arm is invalid."""
        self._require_preflight()
        self._validate_pair(pair)
        pair_root = self.output_root / _pair_id(pair)
        if pair_root.exists():
            raise PairInvalidError(
                f"pair output already exists; use resume_pair: {_pair_id(pair)}"
            )
        return self._execute_pair(pair, resume=False)

    def resume_pair(self, pair: PairSpec) -> PairRunReceipt:
        """Resume only at arm boundaries, preserving all partial attempts."""
        self._require_preflight()
        self._validate_pair(pair)
        return self._execute_pair(pair, resume=True)

    def _latest_attempt(self, pair: PairSpec, arm: Arm) -> ArmAttemptReceipt | None:
        arm_root = self.output_root / _pair_id(pair) / arm
        paths = sorted(arm_root.glob("attempt-*.json")) if arm_root.exists() else []
        if not paths:
            return None
        return ArmAttemptReceipt.model_validate(_load_json(paths[-1]))

    def _attempt_still_valid(self, receipt: ArmAttemptReceipt) -> bool:
        if not receipt.valid:
            return False
        try:
            self._verify_evidence_hashes(receipt.evidence_hashes)
        except PairInvalidError:
            return False
        return True

    def _execute_pair(self, pair: PairSpec, *, resume: bool) -> PairRunReceipt:
        order = self.campaign.arm_order(pair.replicate)
        arms: dict[str, str] = {}
        pair_root = self.output_root / _pair_id(pair)
        pair_root.mkdir(parents=True, exist_ok=True)
        for arm in order:
            latest = self._latest_attempt(pair, arm)
            if latest is None and (pair_root / arm).exists() and any((pair_root / arm).iterdir()):
                latest = self._write_partial_receipt(pair, arm)
            if resume and latest is not None and self._attempt_still_valid(latest):
                arms[arm] = self._relative(
                    pair_root / arm / f"attempt-{latest.attempt:03d}.json"
                )
                continue
            attempt = 1 if latest is None else latest.attempt + 1
            retry_of = latest.attempt_id if latest is not None else None
            receipt = self._run_arm(pair, arm, attempt, retry_of=retry_of)
            arms[arm] = self._relative(
                pair_root / arm / f"attempt-{attempt:03d}.json"
            )
            if not receipt.valid:
                pair_receipt = PairRunReceipt(
                    campaign_id=self.campaign.campaign_id,
                    config_hash=self.campaign.config_hash(),
                    pair_id=_pair_id(pair),
                    problem=pair.problem,
                    replicate=pair.replicate,
                    order=order,
                    arms=arms,
                    valid=False,
                    invalidation_reason=receipt.invalidation_reason,
                    generated_at=_now(),
                )
                _write_json(pair_root / "pair.json", pair_receipt)
                raise PairInvalidError(receipt.invalidation_reason or f"{arm} invalid")

        pair_receipt = PairRunReceipt(
            campaign_id=self.campaign.campaign_id,
            config_hash=self.campaign.config_hash(),
            pair_id=_pair_id(pair),
            problem=pair.problem,
            replicate=pair.replicate,
            order=order,
            arms=arms,
            valid=True,
            generated_at=_now(),
        )
        _write_json(pair_root / "pair.json", pair_receipt)
        return pair_receipt

    def _verify_evidence_hashes(self, evidence: dict[str, str]) -> None:
        for relative, expected in evidence.items():
            path = self.campaign_root / relative
            if relative.endswith("/.tree"):
                snapshot = Path(str(path)[: -len("/.tree")])
                files = sorted(item for item in snapshot.rglob("*") if item.is_file())
                manifest = b"".join(
                    item.relative_to(snapshot).as_posix().encode("utf-8")
                    + b"\0"
                    + _sha256_file(item).encode("ascii")
                    + b"\n"
                    for item in files
                )
                actual = _sha256_bytes(manifest)
            elif path.is_file():
                actual = _sha256_file(path)
            else:
                raise PairInvalidError(f"evidence missing: {relative}")
            if actual != expected:
                raise PairInvalidError(f"evidence hash mismatch: {relative}")

    def verify_receipts(self) -> tuple[PairRunReceipt, ...]:
        """Verify all final pair receipts and their selected evidence chains."""
        self._require_preflight()
        pair_paths = sorted(self.output_root.glob("*-r[12]/pair.json"))
        if not pair_paths:
            raise PairInvalidError("no pair receipts found")
        verified: list[PairRunReceipt] = []
        for path in pair_paths:
            pair = PairRunReceipt.model_validate(_load_json(path))
            if not pair.valid or pair.config_hash != self.campaign.config_hash():
                raise PairInvalidError(f"invalid pair receipt: {path}")
            if set(pair.arms) != {"control", "cymatix"}:
                raise PairInvalidError(f"pair receipt lacks both arms: {path}")
            for arm, receipt_relative in pair.arms.items():
                attempt_path = self.campaign_root / receipt_relative
                attempt = ArmAttemptReceipt.model_validate(_load_json(attempt_path))
                if not attempt.valid or attempt.arm != arm:
                    raise PairInvalidError(f"invalid selected arm receipt: {attempt_path}")
                self._verify_evidence_hashes(attempt.evidence_hashes)
            verified.append(pair)
        return tuple(verified)
