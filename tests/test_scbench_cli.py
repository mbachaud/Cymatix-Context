"""Exit-code and dispatch tests for the SCBench campaign CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from cymatix_context.integrations.scbench import cli
from cymatix_context.integrations.scbench.runner import (
    PairInvalidError,
    PreflightError,
)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object | None]] = []
        self.error: Exception | None = None

    def _record(self, name: str, value: object | None = None) -> object:
        self.calls.append((name, value))
        if self.error is not None:
            raise self.error
        return {"valid": True}

    def preflight(self) -> object:
        return self._record("preflight")

    def run_pair(self, pair: object) -> object:
        return self._record("run_pair", pair)

    def resume_pair(self, pair: object) -> object:
        return self._record("resume_pair", pair)

    def verify_receipts(self) -> object:
        return self._record("verify_receipts")


@pytest.fixture
def fake_runner(monkeypatch) -> FakeRunner:
    runner = FakeRunner()
    monkeypatch.setattr(cli, "_build_runner", lambda _args: runner)
    return runner


def test_preflight_returns_zero_only_after_valid_completion(fake_runner, capsys):
    code = cli.main(["preflight", "--manifest", "manifest.json"])

    assert code == 0
    assert fake_runner.calls == [("preflight", None)]
    assert '"valid": true' in capsys.readouterr().out.casefold()


def test_manifest_or_preflight_error_returns_two(fake_runner, capsys):
    fake_runner.error = PreflightError("SCBench commit")

    code = cli.main(["preflight", "--manifest", "manifest.json"])

    assert code == 2
    assert "SCBench commit" in capsys.readouterr().err


def test_run_pair_dispatches_resume_and_invalid_pair_returns_three(
    fake_runner,
    capsys,
):
    code = cli.main(
        [
            "run-pair",
            "--manifest",
            "manifest.json",
            "--problem",
            "file-backup",
            "--replicate",
            "2",
            "--resume",
        ]
    )
    assert code == 0
    name, pair = fake_runner.calls[-1]
    assert name == "resume_pair"
    assert pair.problem == "file-backup"
    assert pair.replicate == 2

    fake_runner.error = PairInvalidError("control invalid")
    code = cli.main(
        [
            "run-pair",
            "--manifest",
            "manifest.json",
            "--problem",
            "file-backup",
            "--replicate",
            "1",
        ]
    )
    assert code == 3
    assert "control invalid" in capsys.readouterr().err


def test_verify_receipts_uses_invalid_pair_exit_code(fake_runner):
    fake_runner.error = PairInvalidError("hash mismatch")

    code = cli.main(["verify-receipts", "--manifest", "manifest.json"])

    assert code == 3


def test_analysis_gate_failure_returns_four(monkeypatch, capsys):
    def fail(_manifest: Path) -> object:
        raise RuntimeError("graduation gate failed")

    monkeypatch.setattr(cli, "_run_analysis", fail)

    code = cli.main(["analyze", "--manifest", "manifest.json"])

    assert code == 4
    assert "graduation gate failed" in capsys.readouterr().err


def test_analysis_v2_dispatches_corrected_analyzer(monkeypatch, capsys):
    calls: list[Path] = []

    def succeed(manifest: Path) -> object:
        calls.append(manifest)
        return {"gate_passed": True, "verdict": "no_detectable_difference"}

    monkeypatch.setattr(cli, "_run_analysis_v2", succeed)

    code = cli.main(["analyze-v2", "--manifest", "manifest.json"])

    assert code == 0
    assert calls == [Path("manifest.json")]
    assert "no_detectable_difference" in capsys.readouterr().out


def test_malformed_manifest_returns_two(tmp_path, capsys):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    code = cli.main(["preflight", "--manifest", str(manifest)])

    assert code == 2
    assert "manifest" in capsys.readouterr().err.casefold()
