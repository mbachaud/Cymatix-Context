"""Command-line entry point for the paired SCBench campaign harness."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .config import CampaignConfig, PairSpec
from .runner import CampaignRunner, PairInvalidError, PreflightError


log = logging.getLogger("cymatix.scbench.cli")
EXIT_OK = 0
EXIT_PREFLIGHT = 2
EXIT_INVALID_PAIR = 3
EXIT_ANALYSIS_GATE = 4


def _add_manifest_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", type=Path, required=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cymatix_context.integrations.scbench.cli",
        description="Run the frozen paired SCBench Cymatix experiment.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    _add_manifest_argument(preflight)

    run_pair = subparsers.add_parser("run-pair")
    _add_manifest_argument(run_pair)
    run_pair.add_argument("--problem", required=True)
    run_pair.add_argument("--replicate", type=int, choices=(1, 2), required=True)
    run_pair.add_argument(
        "--resume",
        action="store_true",
        help="Restart only incomplete arms in new attempt directories.",
    )

    verify = subparsers.add_parser("verify-receipts")
    _add_manifest_argument(verify)

    analyze = subparsers.add_parser("analyze")
    _add_manifest_argument(analyze)

    analyze_v2 = subparsers.add_parser("analyze-v2")
    _add_manifest_argument(analyze_v2)

    for command in (preflight, run_pair, verify):
        command.add_argument("--cymatix-root", type=Path)
        command.add_argument("--scbench-root", type=Path)
        command.add_argument("--problem-root", type=Path)
        command.add_argument("--output-root", type=Path)
        command.add_argument("--auth-path", type=Path)
        command.add_argument("--min-free-disk-gib", type=float, default=20.0)
    return parser


def _environment_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def _build_runner(args: argparse.Namespace) -> CampaignRunner:
    manifest = args.manifest.resolve()
    campaign = CampaignConfig.load(manifest)
    cymatix_root = (
        args.cymatix_root
        or _environment_path("CYMATIX_ROOT")
        or Path(__file__).resolve().parents[3]
    )
    scbench_root = (
        args.scbench_root
        or _environment_path("SCBENCH_ROOT")
        or cymatix_root.parent / "slop-code-bench-cymatix"
    )
    problem_root = (
        args.problem_root
        or _environment_path("SCBENCH_PROBLEM_ROOT")
        or Path.home() / ".cache" / "scbench" / "problems"
    )
    output_root = args.output_root or manifest.parent / "pairs"
    auth_path = args.auth_path or Path.home() / ".codex" / "auth.json"
    return CampaignRunner(
        campaign,
        manifest_path=manifest,
        cymatix_root=cymatix_root,
        scbench_root=scbench_root,
        problem_root=problem_root,
        output_root=output_root,
        auth_path=auth_path,
        min_free_disk_bytes=round(args.min_free_disk_gib * 1024**3),
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def _print_result(value: Any) -> None:
    print(json.dumps(_jsonable(value), sort_keys=True, indent=2))


def _run_analysis(manifest: Path) -> object:
    """Defer to Task 11's analyzer while keeping this command stable."""
    from .analysis import analyze_campaign

    result = analyze_campaign(manifest.resolve())
    data = _jsonable(result)
    if isinstance(data, dict):
        gate = data.get("graduated", data.get("gate_passed", True))
        if gate is not True:
            raise RuntimeError("analysis graduation gate did not pass")
    return result


def _run_analysis_v2(manifest: Path) -> object:
    """Run the corrected paired-regression analysis and enforce its safety gates."""
    from .analysis import analyze_campaign_v2

    result = analyze_campaign_v2(manifest.resolve())
    if result.gate_passed is not True:
        raise RuntimeError("analysis v2 safety gate did not pass")
    return result


def main(argv: list[str] | None = None) -> int:
    """Dispatch one harness operation and return its documented exit code."""
    args = _build_parser().parse_args(argv)
    if args.command in ("analyze", "analyze-v2"):
        try:
            operation = _run_analysis if args.command == "analyze" else _run_analysis_v2
            _print_result(operation(args.manifest))
            return EXIT_OK
        except Exception as exc:
            log.warning("SCBench analysis failed", exc_info=True)
            print(f"analysis gate failure: {exc}", file=sys.stderr)
            return EXIT_ANALYSIS_GATE

    try:
        runner = _build_runner(args)
        result: object
        if args.command == "preflight":
            result = runner.preflight()
        elif args.command == "run-pair":
            pair = PairSpec(problem=args.problem, replicate=args.replicate)
            result = runner.resume_pair(pair) if args.resume else runner.run_pair(pair)
        elif args.command == "verify-receipts":
            result = runner.verify_receipts()
        else:  # pragma: no cover - argparse enforces the command set
            raise ValueError(f"unknown command: {args.command}")
        _print_result(result)
        return EXIT_OK
    except PairInvalidError as exc:
        print(f"invalid pair: {exc}", file=sys.stderr)
        return EXIT_INVALID_PAIR
    except (PreflightError, ValidationError, OSError, ValueError, json.JSONDecodeError) as exc:
        log.warning("SCBench manifest/preflight operation failed", exc_info=True)
        print(f"manifest/preflight error: {exc}", file=sys.stderr)
        return EXIT_PREFLIGHT
    except Exception as exc:
        log.warning("unexpected SCBench preflight failure", exc_info=True)
        print(f"manifest/preflight error: {exc}", file=sys.stderr)
        return EXIT_PREFLIGHT


if __name__ == "__main__":
    raise SystemExit(main())
