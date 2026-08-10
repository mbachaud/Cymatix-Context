"""Official SCBench result loading and paired regression analysis."""

from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import random
import statistics
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import Arm, CampaignConfig


log = logging.getLogger("cymatix.scbench.analysis")
GateStatus = Literal["pass", "fail", "not_evaluable"]


class AnalysisIntegrityError(RuntimeError):
    """Official results or paired metadata cannot support a valid analysis."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CheckpointResult(_FrozenModel):
    problem: str = Field(min_length=1)
    checkpoint: str = Field(min_length=1)
    checkpoint_index: int = Field(ge=1)
    replicate: int = Field(ge=1)
    arm: Arm
    scbench_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    metric_version: str = Field(min_length=1)
    strict_pass_rate: float = Field(ge=0, le=1)
    isolated_pass_rate: float = Field(ge=0, le=1)
    erosion: float | None = None
    verbosity: float | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    elapsed_seconds: float | None = Field(default=None, ge=0)

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.problem, self.checkpoint, self.replicate)


class ArmResults(_FrozenModel):
    arm: Arm
    replicate: int = Field(ge=1)
    scbench_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    metric_version: str = Field(min_length=1)
    checkpoints: tuple[CheckpointResult, ...]

    @model_validator(mode="after")
    def _validate_checkpoint_metadata(self) -> "ArmResults":
        keys: set[tuple[str, str, int]] = set()
        for checkpoint in self.checkpoints:
            if (
                checkpoint.arm != self.arm
                or checkpoint.replicate != self.replicate
                or checkpoint.scbench_commit != self.scbench_commit
                or checkpoint.metric_version != self.metric_version
            ):
                raise ValueError("checkpoint metadata differs from its arm")
            if checkpoint.key in keys:
                raise ValueError(f"duplicate checkpoint result: {checkpoint.key}")
            keys.add(checkpoint.key)
        return self


class PairedCheckpoint(_FrozenModel):
    control: CheckpointResult
    cymatix: CheckpointResult

    @model_validator(mode="after")
    def _validate_pair(self) -> "PairedCheckpoint":
        if self.control.arm != "control" or self.cymatix.arm != "cymatix":
            raise ValueError("paired results are stored under the wrong arm")
        if self.control.key != self.cymatix.key:
            raise ValueError("paired checkpoint identities differ")
        if self.control.checkpoint_index != self.cymatix.checkpoint_index:
            raise ValueError("paired checkpoint indices differ")
        return self

    @property
    def problem(self) -> str:
        return self.control.problem

    @property
    def checkpoint_index(self) -> int:
        return self.control.checkpoint_index


class PairedDataset(_FrozenModel):
    pairs: tuple[PairedCheckpoint, ...]
    operational_invalidations: tuple[str, ...] = ()

    @property
    def primary_pairs(self) -> tuple[PairedCheckpoint, ...]:
        return tuple(pair for pair in self.pairs if pair.checkpoint_index > 1)


class GateResult(_FrozenModel):
    gate_id: str
    description: str
    status: GateStatus
    observed: float | int | None
    threshold: str


class BootstrapInterval(_FrozenModel):
    seed: int
    iterations: int = Field(gt=0)
    lower: float | None
    upper: float | None
    accepted_samples: int = Field(ge=0)
    rejected_samples: int = Field(ge=0)


class PilotSummary(_FrozenModel):
    all_checkpoint_count: int = Field(ge=0)
    primary_checkpoint_count: int = Field(ge=0)
    control_isolated_passes: int = Field(ge=0)
    cymatix_isolated_passes: int = Field(ge=0)
    control_regression_events: int = Field(ge=0)
    cymatix_regression_events: int = Field(ge=0)
    control_regression_rate: float | None
    cymatix_regression_rate: float | None
    treatment_effect: float | None
    net_prevented_events: int
    relative_regression_reduction: float | None
    control_isolated_solve_rate: float | None
    cymatix_isolated_solve_rate: float | None
    isolated_solve_difference: float | None
    median_erosion_delta: float | None
    median_verbosity_delta: float | None
    median_input_token_ratio: float | None
    median_elapsed_ratio: float | None
    integrity_failure_count: int = Field(ge=0)
    operational_invalidations: tuple[str, ...]
    event_cells: dict[str, int]
    gates: tuple[GateResult, ...]
    graduated: bool
    bootstrap: BootstrapInterval | None = None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _required(record: Mapping[str, Any], field: str, line_number: int) -> Any:
    if field not in record:
        raise AnalysisIntegrityError(
            f"official field {field!r} is missing at line {line_number}"
        )
    return record[field]


def load_arm_results(
    results_file: Path,
    *,
    arm: Arm,
    replicate: int,
    scbench_commit: str,
    expected_problem: str | None = None,
) -> ArmResults:
    """Load and validate one arm's official ``checkpoint_results.jsonl``."""
    checkpoints: list[CheckpointResult] = []
    metric_versions: set[str] = set()
    try:
        lines = results_file.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        log.warning("failed to read SCBench results: %s", results_file, exc_info=True)
        raise AnalysisIntegrityError(f"cannot read results: {results_file}") from exc

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except Exception as exc:
            log.warning("malformed SCBench JSONL line", exc_info=True)
            raise AnalysisIntegrityError(
                f"malformed result JSON at line {line_number}"
            ) from exc
        if not isinstance(record, dict):
            raise AnalysisIntegrityError(
                f"result at line {line_number} must be an object"
            )
        problem = _required(record, "problem", line_number)
        checkpoint = _required(record, "checkpoint", line_number)
        index = _required(record, "idx", line_number)
        strict_rate = _required(record, "strict_pass_rate", line_number)
        isolated_rate = _required(record, "isolated_pass_rate", line_number)
        metric_version = _required(record, "scb_check_version", line_number)
        if expected_problem is not None and str(problem).replace("_", "-") != (
            expected_problem.replace("_", "-")
        ):
            raise AnalysisIntegrityError(
                f"problem identity differs at line {line_number}: {problem}"
            )
        for name, value in (
            ("idx", index),
            ("strict_pass_rate", strict_rate),
            ("isolated_pass_rate", isolated_rate),
        ):
            if not _is_number(value):
                raise AnalysisIntegrityError(
                    f"official field {name!r} is not numeric at line {line_number}"
                )
        if record.get("state", "ran") != "ran":
            raise AnalysisIntegrityError(
                f"checkpoint did not reach state 'ran' at line {line_number}"
            )

        def optional_number(name: str) -> float | None:
            value = record.get(name)
            if value is None:
                return None
            if not _is_number(value):
                raise AnalysisIntegrityError(
                    f"official field {name!r} is not numeric at line {line_number}"
                )
            return float(value)

        input_value = optional_number("input")
        duration_value = optional_number("duration")
        checkpoint_result = CheckpointResult(
            problem=str(problem),
            checkpoint=str(checkpoint),
            checkpoint_index=int(index),
            replicate=replicate,
            arm=arm,
            scbench_commit=scbench_commit,
            metric_version=str(metric_version),
            strict_pass_rate=float(strict_rate),
            isolated_pass_rate=float(isolated_rate),
            erosion=optional_number("erosion"),
            verbosity=optional_number("verbosity"),
            input_tokens=int(input_value) if input_value is not None else None,
            elapsed_seconds=duration_value,
        )
        checkpoints.append(checkpoint_result)
        metric_versions.add(checkpoint_result.metric_version)

    if not checkpoints:
        raise AnalysisIntegrityError(f"no checkpoint results found: {results_file}")
    if len(metric_versions) != 1:
        raise AnalysisIntegrityError("metric version differs within one arm")
    try:
        return ArmResults(
            arm=arm,
            replicate=replicate,
            scbench_commit=scbench_commit,
            metric_version=next(iter(metric_versions)),
            checkpoints=tuple(checkpoints),
        )
    except Exception as exc:
        log.warning("arm result validation failed", exc_info=True)
        raise AnalysisIntegrityError(f"invalid arm results: {exc}") from exc


def pair_checkpoints(control: ArmResults, cymatix: ArmResults) -> PairedDataset:
    """Pair exact checkpoint identities and record missing mates as invalidations."""
    if control.arm != "control" or cymatix.arm != "cymatix":
        raise AnalysisIntegrityError("control and Cymatix arms are reversed")
    if control.replicate != cymatix.replicate:
        raise AnalysisIntegrityError("replicate differs across arms")
    if control.scbench_commit != cymatix.scbench_commit:
        raise AnalysisIntegrityError("SCBench commit differs across arms")
    if control.metric_version != cymatix.metric_version:
        raise AnalysisIntegrityError("metric version differs across arms")

    control_by_key = {checkpoint.key: checkpoint for checkpoint in control.checkpoints}
    cymatix_by_key = {checkpoint.key: checkpoint for checkpoint in cymatix.checkpoints}
    shared = sorted(set(control_by_key) & set(cymatix_by_key))
    pairs: list[PairedCheckpoint] = []
    invalidations: list[str] = []
    for key in shared:
        try:
            pairs.append(
                PairedCheckpoint(
                    control=control_by_key[key],
                    cymatix=cymatix_by_key[key],
                )
            )
        except Exception as exc:
            raise AnalysisIntegrityError(f"paired checkpoint metadata differs: {key}") from exc
    for problem, checkpoint, replicate in sorted(set(control_by_key) - set(cymatix_by_key)):
        invalidations.append(
            f"missing cymatix checkpoint: {problem}/{checkpoint}/r{replicate}"
        )
    for problem, checkpoint, replicate in sorted(set(cymatix_by_key) - set(control_by_key)):
        invalidations.append(
            f"missing control checkpoint: {problem}/{checkpoint}/r{replicate}"
        )
    pairs.sort(
        key=lambda pair: (
            pair.problem,
            pair.control.replicate,
            pair.checkpoint_index,
            pair.control.checkpoint,
        )
    )
    return PairedDataset(
        pairs=tuple(pairs),
        operational_invalidations=tuple(invalidations),
    )


def regression_event(result: Mapping[str, Any] | CheckpointResult) -> bool:
    """Return true only for an isolated solve that fails strict regression tests."""
    isolated: Any
    strict: Any
    if isinstance(result, CheckpointResult):
        isolated = result.isolated_pass_rate
        strict = result.strict_pass_rate
    else:
        isolated = result.get("isolated_pass_rate")
        strict = result.get("strict_pass_rate")
    if not _is_number(isolated) or not _is_number(strict):
        return False
    return math.isclose(float(isolated), 1.0) and not math.isclose(
        float(strict), 1.0
    )


def _pass(rate: float) -> bool:
    return math.isclose(rate, 1.0)


def _median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _gate(
    gate_id: str,
    description: str,
    observed: float | int | None,
    threshold: str,
    passes: bool | None,
) -> GateResult:
    status: GateStatus
    if passes is None:
        status = "not_evaluable"
    else:
        status = "pass" if passes else "fail"
    return GateResult(
        gate_id=gate_id,
        description=description,
        status=status,
        observed=observed,
        threshold=threshold,
    )


def compute_pilot_summary(
    dataset: PairedDataset,
    *,
    integrity_failures: int = 0,
) -> PilotSummary:
    """Compute the frozen directional pilot estimands and eight gates."""
    primary = dataset.primary_pairs
    control_isolated = sum(_pass(pair.control.isolated_pass_rate) for pair in primary)
    cymatix_isolated = sum(_pass(pair.cymatix.isolated_pass_rate) for pair in primary)
    control_events = sum(regression_event(pair.control) for pair in primary)
    cymatix_events = sum(regression_event(pair.cymatix) for pair in primary)
    control_regression_rate = _rate(control_events, control_isolated)
    cymatix_regression_rate = _rate(cymatix_events, cymatix_isolated)
    treatment_effect = (
        control_regression_rate - cymatix_regression_rate
        if control_regression_rate is not None
        and cymatix_regression_rate is not None
        else None
    )
    relative_reduction = (
        treatment_effect / control_regression_rate
        if treatment_effect is not None
        and control_regression_rate is not None
        and control_regression_rate > 0
        else None
    )
    pair_count = len(primary)
    control_isolated_rate = _rate(control_isolated, pair_count)
    cymatix_isolated_rate = _rate(cymatix_isolated, pair_count)
    isolated_difference = (
        cymatix_isolated_rate - control_isolated_rate
        if control_isolated_rate is not None and cymatix_isolated_rate is not None
        else None
    )

    erosion_deltas = [
        pair.cymatix.erosion - pair.control.erosion
        for pair in primary
        if pair.control.erosion is not None and pair.cymatix.erosion is not None
    ]
    verbosity_deltas = [
        pair.cymatix.verbosity - pair.control.verbosity
        for pair in primary
        if pair.control.verbosity is not None
        and pair.cymatix.verbosity is not None
    ]
    input_ratios = [
        pair.cymatix.input_tokens / pair.control.input_tokens
        for pair in primary
        if pair.control.input_tokens is not None
        and pair.cymatix.input_tokens is not None
        and pair.control.input_tokens > 0
    ]
    elapsed_ratios = [
        pair.cymatix.elapsed_seconds / pair.control.elapsed_seconds
        for pair in primary
        if pair.control.elapsed_seconds is not None
        and pair.cymatix.elapsed_seconds is not None
        and pair.control.elapsed_seconds > 0
    ]
    median_erosion = _median(erosion_deltas)
    median_verbosity = _median(verbosity_deltas)
    median_input = _median(input_ratios)
    median_elapsed = _median(elapsed_ratios)
    total_integrity_failures = integrity_failures + len(
        dataset.operational_invalidations
    )

    cells = {"neither": 0, "control_only": 0, "cymatix_only": 0, "both": 0}
    for pair in primary:
        control_event = regression_event(pair.control)
        cymatix_event = regression_event(pair.cymatix)
        if control_event and cymatix_event:
            cells["both"] += 1
        elif control_event:
            cells["control_only"] += 1
        elif cymatix_event:
            cells["cymatix_only"] += 1
        else:
            cells["neither"] += 1

    net_prevented = control_events - cymatix_events
    gates = (
        _gate(
            "net_prevented_events",
            "At least two net regression events are prevented by Cymatix.",
            net_prevented if primary else None,
            ">= 2",
            net_prevented >= 2 if primary else None,
        ),
        _gate(
            "relative_regression_reduction",
            "The relative reduction in regression rate is at least 15%.",
            relative_reduction,
            ">= 0.15",
            relative_reduction >= 0.15 if relative_reduction is not None else None,
        ),
        _gate(
            "isolated_solve_noninferiority",
            "Isolated solve rate is no worse than control by more than 2.5 percentage points.",
            isolated_difference,
            ">= -0.025",
            isolated_difference >= -0.025 if isolated_difference is not None else None,
        ),
        _gate(
            "median_erosion_delta",
            "Median paired erosion is no worse than control by more than 0.03.",
            median_erosion,
            "<= 0.03",
            median_erosion <= 0.03 if median_erosion is not None else None,
        ),
        _gate(
            "median_verbosity_delta",
            "Median paired verbosity is no worse than control by more than 0.03.",
            median_verbosity,
            "<= 0.03",
            median_verbosity <= 0.03 if median_verbosity is not None else None,
        ),
        _gate(
            "median_input_token_ratio",
            "Median input tokens increase by no more than 25%.",
            median_input,
            "<= 1.25",
            median_input <= 1.25 if median_input is not None else None,
        ),
        _gate(
            "median_elapsed_ratio",
            "Median checkpoint elapsed time increases by no more than 30%.",
            median_elapsed,
            "<= 1.30",
            median_elapsed <= 1.30 if median_elapsed is not None else None,
        ),
        _gate(
            "integrity_failures",
            "There are zero unresolved ingest, packet, version, receipt-integrity, or evaluation-contamination failures.",
            total_integrity_failures,
            "== 0",
            total_integrity_failures == 0,
        ),
    )
    return PilotSummary(
        all_checkpoint_count=len(dataset.pairs),
        primary_checkpoint_count=pair_count,
        control_isolated_passes=control_isolated,
        cymatix_isolated_passes=cymatix_isolated,
        control_regression_events=control_events,
        cymatix_regression_events=cymatix_events,
        control_regression_rate=control_regression_rate,
        cymatix_regression_rate=cymatix_regression_rate,
        treatment_effect=treatment_effect,
        net_prevented_events=net_prevented,
        relative_regression_reduction=relative_reduction,
        control_isolated_solve_rate=control_isolated_rate,
        cymatix_isolated_solve_rate=cymatix_isolated_rate,
        isolated_solve_difference=isolated_difference,
        median_erosion_delta=median_erosion,
        median_verbosity_delta=median_verbosity,
        median_input_token_ratio=median_input,
        median_elapsed_ratio=median_elapsed,
        integrity_failure_count=total_integrity_failures,
        operational_invalidations=dataset.operational_invalidations,
        event_cells=cells,
        gates=gates,
        graduated=all(gate.status == "pass" for gate in gates),
    )


def _percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = position - lower_index
    return ordered[lower_index] + fraction * (
        ordered[upper_index] - ordered[lower_index]
    )


def clustered_bootstrap(
    dataset: PairedDataset,
    *,
    iterations: int = 10_000,
    seed: int = 20260808,
) -> BootstrapInterval:
    """Bootstrap the paired effect by resampling whole problem trajectories."""
    if iterations <= 0:
        raise ValueError("iterations must be positive")
    grouped: dict[str, list[PairedCheckpoint]] = defaultdict(list)
    for pair in dataset.primary_pairs:
        grouped[pair.problem].append(pair)
    problems = sorted(grouped)
    if not problems:
        return BootstrapInterval(
            seed=seed,
            iterations=iterations,
            lower=None,
            upper=None,
            accepted_samples=0,
            rejected_samples=iterations,
        )
    generator = random.Random(seed)
    effects: list[float] = []
    rejected = 0
    for _iteration in range(iterations):
        sampled_pairs: list[PairedCheckpoint] = []
        for problem in generator.choices(problems, k=len(problems)):
            sampled_pairs.extend(grouped[problem])
        control_isolated = sum(
            _pass(pair.control.isolated_pass_rate) for pair in sampled_pairs
        )
        cymatix_isolated = sum(
            _pass(pair.cymatix.isolated_pass_rate) for pair in sampled_pairs
        )
        if control_isolated == 0 or cymatix_isolated == 0:
            rejected += 1
            continue
        control_events = sum(regression_event(pair.control) for pair in sampled_pairs)
        cymatix_events = sum(regression_event(pair.cymatix) for pair in sampled_pairs)
        effects.append(
            control_events / control_isolated
            - cymatix_events / cymatix_isolated
        )
    return BootstrapInterval(
        seed=seed,
        iterations=iterations,
        lower=_percentile(effects, 0.025) if effects else None,
        upper=_percentile(effects, 0.975) if effects else None,
        accepted_samples=len(effects),
        rejected_samples=rejected,
    )


def render_markdown(summary: PilotSummary) -> str:
    """Render a stable human-readable summary without hiding opposing cells."""
    cells = summary.event_cells
    lines = [
        "# SCBench paired regression analysis",
        "",
        f"Graduated: **{'yes' if summary.graduated else 'no'}**",
        "",
        "## Paired regression cells",
        "",
        "| Cell | Count |",
        "|---|---:|",
        f"| Neither | {cells['neither']} |",
        f"| Control only | {cells['control_only']} |",
        f"| Cymatix only | {cells['cymatix_only']} |",
        f"| Both | {cells['both']} |",
        "",
        "## Graduation gates",
        "",
        "| Gate | Status | Observed | Threshold |",
        "|---|---|---:|---|",
    ]
    for gate in summary.gates:
        observed = "n/a" if gate.observed is None else str(gate.observed)
        lines.append(
            f"| {gate.description} | {gate.status} | {observed} | {gate.threshold} |"
        )
    if summary.operational_invalidations:
        lines.extend(["", "## Operational invalidations", ""])
        lines.extend(f"- {item}" for item in summary.operational_invalidations)
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        log.warning("failed to write analysis artifact: %s", path, exc_info=True)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except Exception:
                log.warning("failed to remove analysis temporary", exc_info=True)
        raise


def write_analysis_summary(summary: PilotSummary, output_root: Path) -> tuple[Path, Path]:
    """Atomically persist canonical JSON and Markdown summaries."""
    json_path = output_root / "analysis.json"
    markdown_path = output_root / "analysis.md"
    payload = (
        json.dumps(summary.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    _atomic_write(json_path, payload)
    _atomic_write(markdown_path, render_markdown(summary).encode("utf-8"))
    return json_path, markdown_path


def _safe_campaign_path(campaign_root: Path, relative: str) -> Path:
    path = campaign_root.joinpath(*Path(relative).parts).resolve()
    if not path.is_relative_to(campaign_root.resolve()):
        raise AnalysisIntegrityError(f"receipt path escapes campaign: {relative}")
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_attempt_evidence(
    campaign_root: Path,
    evidence_hashes: Mapping[str, str],
) -> None:
    for relative, expected in evidence_hashes.items():
        path = _safe_campaign_path(campaign_root, relative)
        if relative.endswith("/.tree"):
            snapshot = path.parent
            files = sorted(item for item in snapshot.rglob("*") if item.is_file())
            manifest = b"".join(
                item.relative_to(snapshot).as_posix().encode("utf-8")
                + b"\0"
                + _sha256_file(item).encode("ascii")
                + b"\n"
                for item in files
            )
            actual = hashlib.sha256(manifest).hexdigest()
        elif path.is_file():
            actual = _sha256_file(path)
        else:
            raise AnalysisIntegrityError(f"selected evidence is missing: {relative}")
        if actual != expected:
            raise AnalysisIntegrityError(f"evidence hash mismatch: {relative}")


def analyze_campaign(manifest_path: Path) -> PilotSummary:
    """Load verified pair outputs for a frozen campaign and write its summaries."""
    campaign = CampaignConfig.load(manifest_path)
    campaign_root = manifest_path.resolve().parent
    preflight_path = campaign_root / "preflight.json"
    try:
        preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("failed to read analysis preflight receipt", exc_info=True)
        raise AnalysisIntegrityError("valid preflight receipt is required") from exc
    if (
        not isinstance(preflight, dict)
        or preflight.get("valid") is not True
        or preflight.get("config_hash") != campaign.config_hash()
        or preflight.get("observed", {}).get("scbench_commit")
        != campaign.scbench_commit
    ):
        raise AnalysisIntegrityError("preflight SCBench commit/config mismatch")

    combined_pairs: list[PairedCheckpoint] = []
    invalidations: list[str] = []
    pair_root = campaign_root / "pairs"
    for problem in campaign.problems:
        for replicate in (1, 2):
            pair_id = f"{problem}-r{replicate}"
            pair_path = pair_root / pair_id / "pair.json"
            if not pair_path.is_file():
                invalidations.append(f"missing pair receipt: {pair_id}")
                continue
            try:
                pair_receipt = json.loads(pair_path.read_text(encoding="utf-8"))
                if (
                    pair_receipt.get("valid") is not True
                    or pair_receipt.get("campaign_id") != campaign.campaign_id
                    or pair_receipt.get("config_hash") != campaign.config_hash()
                    or pair_receipt.get("pair_id") != pair_id
                    or pair_receipt.get("problem") != problem
                    or pair_receipt.get("replicate") != replicate
                ):
                    raise AnalysisIntegrityError("pair receipt is invalid")
                arms: dict[Arm, ArmResults] = {}
                for arm in ("control", "cymatix"):
                    attempt_path = _safe_campaign_path(
                        campaign_root, pair_receipt["arms"][arm]
                    )
                    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
                    if (
                        attempt.get("valid") is not True
                        or attempt.get("campaign_id") != campaign.campaign_id
                        or attempt.get("config_hash") != campaign.config_hash()
                        or attempt.get("pair_id") != pair_id
                        or attempt.get("problem") != problem
                        or attempt.get("replicate") != replicate
                        or attempt.get("arm") != arm
                    ):
                        raise AnalysisIntegrityError(
                            f"selected {arm} attempt identity is invalid"
                        )
                    evidence_hashes = attempt.get("evidence_hashes")
                    if not isinstance(evidence_hashes, dict):
                        raise AnalysisIntegrityError(
                            f"selected {arm} attempt lacks evidence hashes"
                        )
                    _verify_attempt_evidence(campaign_root, evidence_hashes)
                    run_root = _safe_campaign_path(
                        campaign_root, attempt["output_path"]
                    )
                    results_path = run_root / "checkpoint_results.jsonl"
                    results_relative = results_path.relative_to(
                        campaign_root
                    ).as_posix()
                    if results_relative not in evidence_hashes:
                        raise AnalysisIntegrityError(
                            f"selected {arm} attempt does not hash official results"
                        )
                    arms[arm] = load_arm_results(
                        results_path,
                        arm=arm,
                        replicate=replicate,
                        scbench_commit=campaign.scbench_commit,
                        expected_problem=problem,
                    )
                paired = pair_checkpoints(arms["control"], arms["cymatix"])
                combined_pairs.extend(paired.pairs)
                invalidations.extend(paired.operational_invalidations)
            except AnalysisIntegrityError:
                raise
            except Exception as exc:
                log.warning("campaign pair analysis failed: %s", pair_id, exc_info=True)
                invalidations.append(f"invalid pair {pair_id}: {exc}")

    dataset = PairedDataset(
        pairs=tuple(combined_pairs),
        operational_invalidations=tuple(invalidations),
    )
    summary = compute_pilot_summary(dataset)
    summary = summary.model_copy(
        update={"bootstrap": clustered_bootstrap(dataset, seed=20260808)}
    )
    write_analysis_summary(summary, campaign_root)
    return summary
