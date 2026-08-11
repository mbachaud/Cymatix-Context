"""Paired estimand, integrity, gate, and bootstrap tests."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from cymatix_context.integrations.scbench.analysis import (
    AnalysisIntegrityError,
    ArmResults,
    CheckpointResult,
    PairedDataset,
    analyze_campaign,
    analyze_campaign_v2,
    clustered_bootstrap,
    compute_paired_regression_v2,
    compute_pilot_summary,
    load_arm_results,
    pair_checkpoints,
    regression_event,
    render_markdown,
    write_analysis_v2_summary,
)
from cymatix_context.integrations.scbench.config import CampaignConfig, SourcePolicy


FIXTURES = Path(__file__).parents[1] / "benchmarks" / "scbench" / "fixtures"
SCBENCH_COMMIT = "2" * 40


def _checkpoint(
    *,
    problem: str,
    checkpoint: int,
    arm: str,
    strict: float,
    isolated: float = 1.0,
    erosion: float | None = 0.20,
    verbosity: float | None = 0.20,
    input_tokens: int | None = 100,
    elapsed: float | None = 10.0,
    metric_version: str = "0.1.3",
    regression_passed: int | None = None,
    regression_total: int | None = None,
) -> CheckpointResult:
    return CheckpointResult(
        problem=problem,
        checkpoint=f"checkpoint_{checkpoint}",
        checkpoint_index=checkpoint,
        replicate=1,
        arm=arm,
        scbench_commit=SCBENCH_COMMIT,
        metric_version=metric_version,
        strict_pass_rate=strict,
        isolated_pass_rate=isolated,
        regression_passed=regression_passed,
        regression_total=regression_total,
        erosion=erosion,
        verbosity=verbosity,
        input_tokens=input_tokens,
        elapsed_seconds=elapsed,
    )


def _arm(arm: str, checkpoints: list[CheckpointResult]) -> ArmResults:
    return ArmResults(
        arm=arm,
        replicate=1,
        scbench_commit=SCBENCH_COMMIT,
        metric_version="0.1.3",
        checkpoints=tuple(checkpoints),
    )


def _synthetic_dataset(
    *,
    control_events: int,
    cymatix_events: int,
    isolated_each: int,
) -> PairedDataset:
    control: list[CheckpointResult] = []
    cymatix: list[CheckpointResult] = []
    for index in range(1, isolated_each + 1):
        problem = "alpha" if index <= isolated_each // 2 else "beta"
        checkpoint = index + 1
        control.append(
            _checkpoint(
                problem=problem,
                checkpoint=checkpoint,
                arm="control",
                strict=0.5 if index <= control_events else 1.0,
            )
        )
        cymatix.append(
            _checkpoint(
                problem=problem,
                checkpoint=checkpoint,
                arm="cymatix",
                strict=0.5 if index <= cymatix_events else 1.0,
                input_tokens=110,
                elapsed=11.0,
                erosion=0.21,
                verbosity=0.21,
            )
        )
    return pair_checkpoints(_arm("control", control), _arm("cymatix", cymatix))


def test_regression_event_requires_isolated_pass_and_strict_failure():
    """Changing either pass predicate must alter the primary event."""
    assert regression_event(
        {"isolated_pass_rate": 1.0, "strict_pass_rate": 0.75}
    )
    assert not regression_event(
        {"isolated_pass_rate": 0.5, "strict_pass_rate": 0.0}
    )
    assert not regression_event(
        {"isolated_pass_rate": 1.0, "strict_pass_rate": 1.0}
    )


def test_loader_preserves_direct_regression_test_counters(tmp_path):
    """Dropping prior-test counters would force v2 back onto strict-score proxies."""
    result = {
        "problem": "execution_server",
        "checkpoint": "checkpoint_2",
        "idx": 2,
        "state": "ran",
        "strict_pass_rate": 0.98,
        "isolated_pass_rate": 1.0,
        "regression_passed": 44,
        "regression_total": 45,
        "scb_check_version": "0.1.3",
    }
    path = tmp_path / "checkpoint_results.jsonl"
    path.write_text(json.dumps(result) + "\n", encoding="utf-8")

    loaded = load_arm_results(
        path,
        arm="control",
        replicate=1,
        scbench_commit=SCBENCH_COMMIT,
        expected_problem="execution-server",
    )

    assert loaded.checkpoints[0].regression_passed == 44
    assert loaded.checkpoints[0].regression_total == 45


def test_v2_uses_direct_regression_counters_not_isolated_eligibility():
    """A stronger current-checkpoint solve must not create a regression penalty."""
    control = _checkpoint(
        problem="execution-server",
        checkpoint=2,
        arm="control",
        strict=0.9703,
        isolated=0.9535,
        regression_passed=57,
        regression_total=58,
    )
    cymatix = _checkpoint(
        problem="execution-server",
        checkpoint=2,
        arm="cymatix",
        strict=0.9901,
        isolated=1.0,
        regression_passed=57,
        regression_total=58,
    )
    dataset = pair_checkpoints(_arm("control", [control]), _arm("cymatix", [cymatix]))

    summary = compute_paired_regression_v2(dataset, bootstrap_iterations=100)

    assert regression_event(control) is False
    assert regression_event(cymatix) is True
    assert summary.median_regression_delta == 0.0
    assert summary.regression_head_to_head == {
        "cymatix_better": 0,
        "control_better": 0,
        "tied": 1,
    }
    assert summary.verdict == "no_detectable_difference"


def test_v2_rejects_cross_arm_regression_denominator_drift():
    """Different prior-test sets cannot support a paired preservation estimate."""
    control = _checkpoint(
        problem="p",
        checkpoint=2,
        arm="control",
        strict=1.0,
        regression_passed=9,
        regression_total=10,
    )
    cymatix = _checkpoint(
        problem="p",
        checkpoint=2,
        arm="cymatix",
        strict=1.0,
        regression_passed=10,
        regression_total=11,
    )
    dataset = pair_checkpoints(_arm("control", [control]), _arm("cymatix", [cymatix]))

    with pytest.raises(AnalysisIntegrityError, match="regression denominator"):
        compute_paired_regression_v2(dataset, bootstrap_iterations=100)


def test_v2_detects_problem_clustered_regression_benefit(tmp_path):
    """Removing the paired delta or cluster interval would erase a real benefit."""
    control = _checkpoint(
        problem="p",
        checkpoint=2,
        arm="control",
        strict=0.9,
        regression_passed=9,
        regression_total=10,
    )
    cymatix = _checkpoint(
        problem="p",
        checkpoint=2,
        arm="cymatix",
        strict=1.0,
        regression_passed=10,
        regression_total=10,
    )
    dataset = pair_checkpoints(_arm("control", [control]), _arm("cymatix", [cymatix]))

    summary = compute_paired_regression_v2(dataset, bootstrap_iterations=100)
    json_path, markdown_path = write_analysis_v2_summary(summary, tmp_path)

    assert summary.median_regression_delta == pytest.approx(0.1)
    assert summary.regression_interval.lower == pytest.approx(0.1)
    assert summary.regression_interval.upper == pytest.approx(0.1)
    assert summary.verdict == "benefit_detected"
    assert summary.gate_passed is True
    assert json_path.name == "analysis-v2.json"
    assert markdown_path.name == "analysis-v2.md"


def test_official_fixture_loader_keeps_checkpoint_one_as_secondary():
    """Dropping checkpoint 1 at load time would corrupt secondary reporting."""
    control = load_arm_results(
        FIXTURES / "checkpoint-results-control.jsonl",
        arm="control",
        replicate=1,
        scbench_commit=SCBENCH_COMMIT,
    )
    cymatix = load_arm_results(
        FIXTURES / "checkpoint-results-cymatix.jsonl",
        arm="cymatix",
        replicate=1,
        scbench_commit=SCBENCH_COMMIT,
    )
    dataset = pair_checkpoints(control, cymatix)

    assert len(dataset.pairs) == 4
    assert [pair.checkpoint_index for pair in dataset.primary_pairs] == [2, 3, 4]
    assert dataset.pairs[0].control.input_tokens == 100
    assert dataset.pairs[1].cymatix.elapsed_seconds == 13.0


def test_loader_rejects_missing_official_fields(tmp_path):
    """Defaulting a missing official score would invent an observation."""
    path = tmp_path / "results.jsonl"
    path.write_text(
        json.dumps(
            {
                "problem": "p",
                "checkpoint": "checkpoint_1",
                "idx": 1,
                "strict_pass_rate": 1.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(AnalysisIntegrityError, match="isolated_pass_rate"):
        load_arm_results(
            path,
            arm="control",
            replicate=1,
            scbench_commit=SCBENCH_COMMIT,
        )


def test_loader_rejects_results_from_a_different_problem():
    """A valid result file from another problem cannot satisfy this pair."""
    with pytest.raises(AnalysisIntegrityError, match="problem identity"):
        load_arm_results(
            FIXTURES / "checkpoint-results-control.jsonl",
            arm="control",
            replicate=1,
            scbench_commit=SCBENCH_COMMIT,
            expected_problem="different-problem",
        )


def test_pairing_records_missing_checkpoint_as_operational_invalidation():
    """A missing mate must not become a regression failure or a pass."""
    control = _arm(
        "control",
        [
            _checkpoint(
                problem="p", checkpoint=2, arm="control", strict=0.5
            ),
            _checkpoint(
                problem="p", checkpoint=3, arm="control", strict=1.0
            ),
        ],
    )
    cymatix = _arm(
        "cymatix",
        [_checkpoint(problem="p", checkpoint=2, arm="cymatix", strict=1.0)],
    )

    dataset = pair_checkpoints(control, cymatix)
    summary = compute_pilot_summary(dataset)

    assert len(dataset.pairs) == 1
    assert dataset.operational_invalidations == (
        "missing cymatix checkpoint: p/checkpoint_3/r1",
    )
    assert summary.primary_checkpoint_count == 1
    assert summary.integrity_failure_count == 1


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("scbench_commit", "3" * 40, "SCBench commit"),
        ("metric_version", "0.1.4", "metric version"),
    ],
)
def test_pairing_rejects_cross_arm_version_drift(field, replacement, message):
    """Cross-arm implementation drift invalidates every paired estimand."""
    control = _arm(
        "control",
        [_checkpoint(problem="p", checkpoint=2, arm="control", strict=1.0)],
    )
    cymatix = _arm(
        "cymatix",
        [_checkpoint(problem="p", checkpoint=2, arm="cymatix", strict=1.0)],
    )
    cymatix = cymatix.model_copy(update={field: replacement})

    with pytest.raises(AnalysisIntegrityError, match=message):
        pair_checkpoints(control, cymatix)


def test_primary_effect_is_control_minus_cymatix():
    """Reversing the subtraction would invert the treatment claim."""
    summary = compute_pilot_summary(
        _synthetic_dataset(
            control_events=4,
            cymatix_events=2,
            isolated_each=8,
        )
    )

    assert summary.control_regression_rate == 0.5
    assert summary.cymatix_regression_rate == 0.25
    assert summary.treatment_effect == 0.25
    assert summary.net_prevented_events == 2
    assert summary.event_cells == {
        "neither": 4,
        "control_only": 2,
        "cymatix_only": 0,
        "both": 2,
    }


def test_all_eight_pilot_gates_are_machine_evaluated():
    """Omitting an overhead or integrity gate could falsely graduate a pilot."""
    summary = compute_pilot_summary(
        _synthetic_dataset(
            control_events=4,
            cymatix_events=2,
            isolated_each=8,
        )
    )

    assert len(summary.gates) == 8
    assert {gate.status for gate in summary.gates} == {"pass"}
    assert summary.graduated is True
    assert summary.relative_regression_reduction == 0.5
    assert summary.isolated_solve_difference == 0.0
    assert summary.median_input_token_ratio == 1.1
    assert summary.median_elapsed_ratio == 1.1


def test_not_evaluable_gate_prevents_graduation():
    """Missing denominators or metrics cannot silently pass a gate."""
    control = _checkpoint(
        problem="p",
        checkpoint=2,
        arm="control",
        strict=1.0,
        erosion=None,
    )
    treatment = _checkpoint(
        problem="p",
        checkpoint=2,
        arm="cymatix",
        strict=1.0,
        erosion=None,
    )
    dataset = pair_checkpoints(_arm("control", [control]), _arm("cymatix", [treatment]))

    summary = compute_pilot_summary(dataset)

    statuses = {gate.gate_id: gate.status for gate in summary.gates}
    assert statuses["relative_regression_reduction"] == "not_evaluable"
    assert statuses["median_erosion_delta"] == "not_evaluable"
    assert summary.graduated is False


def test_clustered_bootstrap_is_seeded_and_resamples_whole_problems():
    """Checkpoint-level resampling would understate within-problem dependence."""
    control = [
        _checkpoint(problem="alpha", checkpoint=2, arm="control", strict=0.5),
        _checkpoint(problem="alpha", checkpoint=3, arm="control", strict=0.5),
        _checkpoint(problem="beta", checkpoint=2, arm="control", strict=1.0),
        _checkpoint(problem="beta", checkpoint=3, arm="control", strict=1.0),
    ]
    cymatix = [
        _checkpoint(problem="alpha", checkpoint=2, arm="cymatix", strict=1.0),
        _checkpoint(problem="alpha", checkpoint=3, arm="cymatix", strict=1.0),
        _checkpoint(problem="beta", checkpoint=2, arm="cymatix", strict=0.5),
        _checkpoint(problem="beta", checkpoint=3, arm="cymatix", strict=0.5),
    ]
    dataset = pair_checkpoints(_arm("control", control), _arm("cymatix", cymatix))

    first = clustered_bootstrap(dataset, iterations=500, seed=20260808)
    second = clustered_bootstrap(dataset, iterations=500, seed=20260808)

    assert first == second
    assert first.lower == -1.0
    assert first.upper == 1.0
    assert first.accepted_samples == 500
    assert first.rejected_samples == 0


def test_markdown_report_includes_opposing_cells_and_gate_statuses():
    """A directional summary must still expose outcomes opposing the hypothesis."""
    summary = compute_pilot_summary(
        _synthetic_dataset(
            control_events=3,
            cymatix_events=1,
            isolated_each=8,
        )
    )

    report = render_markdown(summary)

    assert "Control only" in report
    assert "Cymatix only" in report
    assert "not_evaluable" in report or "pass" in report


def _campaign_tree(tmp_path: Path) -> tuple[Path, Path]:
    campaign_data = {
        "campaign_id": "analysis-campaign",
        "cymatix_commit": "1" * 40,
        "scbench_commit": SCBENCH_COMMIT,
        "codex_version": "codex-cli 0.74.0",
        "codex_model": "gpt-5.6-luna",
        "reasoning_effort": "medium",
        "problems": ["fixture-problem"],
        "replicates": [
            {"id": 1, "order": ["control", "cymatix"]},
            {"id": 2, "order": ["cymatix", "control"]},
        ],
        "treatment": {},
        "source_policy": SourcePolicy.default().model_dump(mode="json"),
    }
    campaign = CampaignConfig.model_validate(campaign_data)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(campaign.model_dump(mode="json")), encoding="utf-8"
    )
    (tmp_path / "preflight.json").write_text(
        json.dumps(
            {
                "valid": True,
                "config_hash": campaign.config_hash(),
                "observed": {"scbench_commit": SCBENCH_COMMIT},
            }
        ),
        encoding="utf-8",
    )
    first_results: Path | None = None
    for replicate in (1, 2):
        pair_id = f"fixture-problem-r{replicate}"
        pair_root = tmp_path / "pairs" / pair_id
        arm_receipts: dict[str, str] = {}
        for arm in ("control", "cymatix"):
            arm_root = pair_root / arm
            run_root = arm_root / "scbench"
            run_root.mkdir(parents=True)
            rows = [
                {
                    "problem": "fixture_problem",
                    "checkpoint": "checkpoint_1",
                    "idx": 1,
                    "state": "ran",
                    "strict_pass_rate": 1.0,
                    "isolated_pass_rate": 1.0,
                    "regression_passed": 1,
                    "regression_total": 1,
                    "erosion": 0.1,
                    "verbosity": 0.1,
                    "input": 100,
                    "duration": 10.0,
                    "scb_check_version": "0.1.3",
                },
                {
                    "problem": "fixture_problem",
                    "checkpoint": "checkpoint_2",
                    "idx": 2,
                    "state": "ran",
                    "strict_pass_rate": 0.5 if arm == "control" else 1.0,
                    "isolated_pass_rate": 1.0,
                    "regression_passed": 9 if arm == "control" else 10,
                    "regression_total": 10,
                    "erosion": 0.2 if arm == "control" else 0.21,
                    "verbosity": 0.2 if arm == "control" else 0.21,
                    "input": 100 if arm == "control" else 110,
                    "duration": 10.0 if arm == "control" else 11.0,
                    "scb_check_version": "0.1.3",
                },
            ]
            results = run_root / "checkpoint_results.jsonl"
            results.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            if first_results is None:
                first_results = results
            relative_results = results.relative_to(tmp_path).as_posix()
            digest = hashlib.sha256(results.read_bytes()).hexdigest()
            attempt = arm_root / "attempt-001.json"
            attempt.write_text(
                json.dumps(
                    {
                        "valid": True,
                        "campaign_id": campaign.campaign_id,
                        "config_hash": campaign.config_hash(),
                        "pair_id": pair_id,
                        "problem": "fixture-problem",
                        "replicate": replicate,
                        "arm": arm,
                        "output_path": run_root.relative_to(tmp_path).as_posix(),
                        "evidence_hashes": {relative_results: digest},
                    }
                ),
                encoding="utf-8",
            )
            arm_receipts[arm] = attempt.relative_to(tmp_path).as_posix()
        (pair_root / "pair.json").write_text(
            json.dumps(
                {
                    "valid": True,
                    "campaign_id": campaign.campaign_id,
                    "config_hash": campaign.config_hash(),
                    "pair_id": pair_id,
                    "problem": "fixture-problem",
                    "replicate": replicate,
                    "arms": arm_receipts,
                }
            ),
            encoding="utf-8",
        )
    assert first_results is not None
    return manifest, first_results


def test_campaign_analysis_writes_reports_from_selected_valid_pairs(tmp_path):
    """Bypassing selected pair receipts could mix attempts or invalid arms."""
    manifest, _results = _campaign_tree(tmp_path)

    summary = analyze_campaign(manifest)

    assert summary.primary_checkpoint_count == 2
    assert summary.net_prevented_events == 2
    assert summary.graduated is True
    assert (tmp_path / "analysis.json").is_file()
    assert (tmp_path / "analysis.md").is_file()


def test_campaign_v2_writes_distinct_reports_without_touching_v1(tmp_path):
    """Re-analysis must preserve the preregistered v1 record byte-for-byte."""
    manifest, _results = _campaign_tree(tmp_path)
    (tmp_path / "analysis.json").write_bytes(b"frozen-v1-json\n")
    (tmp_path / "analysis.md").write_bytes(b"frozen-v1-markdown\n")

    summary = analyze_campaign_v2(manifest, bootstrap_iterations=100)

    assert summary.verdict == "benefit_detected"
    assert (tmp_path / "analysis.json").read_bytes() == b"frozen-v1-json\n"
    assert (tmp_path / "analysis.md").read_bytes() == b"frozen-v1-markdown\n"
    assert (tmp_path / "analysis-v2.json").is_file()
    assert (tmp_path / "analysis-v2.md").is_file()


def test_campaign_analysis_rejects_tampered_official_results(tmp_path):
    """Post-receipt metric edits must not enter the paired estimate."""
    manifest, results = _campaign_tree(tmp_path)
    results.write_text(results.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(AnalysisIntegrityError, match="hash mismatch"):
        analyze_campaign(manifest)


def test_campaign_analysis_rejects_attempt_identity_substitution(tmp_path):
    """A selected receipt from another pair must not authorize its evidence."""
    manifest, _results = _campaign_tree(tmp_path)
    attempt = (
        tmp_path
        / "pairs"
        / "fixture-problem-r1"
        / "control"
        / "attempt-001.json"
    )
    data = json.loads(attempt.read_text(encoding="utf-8"))
    data["pair_id"] = "fixture-problem-r2"
    attempt.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(AnalysisIntegrityError, match="attempt identity"):
        analyze_campaign(manifest)
