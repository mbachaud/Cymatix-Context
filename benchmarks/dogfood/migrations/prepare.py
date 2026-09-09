"""Prepare explicit migration arms and commands; never open a benchmark bed."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .gates import ARMS, PAIRS, arm_config, digest, file_digest

ROOT = Path(__file__).resolve().parents[3]


def validate_bank(needles, gold_map):
    if not isinstance(needles, list) or not needles or not isinstance(gold_map, dict):
        raise ValueError("needles and gold must be a nonempty resolved bank")
    names = []
    for row in needles:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip() for k in ("name", "query")):
            raise ValueError("every needle needs nonempty string name and query")
        values = gold_map.get(row["name"])
        if not isinstance(values, list) or not values or any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("gold must contain nonempty lists of document ID strings")
        names.append(row["name"])
    if len(set(names)) != len(names):
        raise ValueError("needle names must be unique")
    return names


def code_sha(ref):
    return subprocess.run(
        ["git", "rev-parse", f"{ref}^{{commit}}"], cwd=ROOT,
        capture_output=True, text=True, check=True, timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-ref", required=True, help="frozen tag or immutable commit")
    parser.add_argument("--source-bed", required=True)
    parser.add_argument("--bed-identity", required=True)
    parser.add_argument("--ingest-c", type=int, required=True)
    parser.add_argument("--tagger-version", type=int, required=True)
    parser.add_argument("--fixture-manifest", help="required evidence for modern beds without tagger version in build provenance")
    parser.add_argument("--fixture-target", help="target key in the fixture manifest")
    parser.add_argument("--resolved", required=True)
    parser.add_argument("--gold", required=True)
    parser.add_argument("--headroom-toin", required=True)
    parser.add_argument("--headroom-ccr", required=True)
    parser.add_argument("--headroom-config", required=True)
    parser.add_argument("--config", default=str(ROOT / "cymatix.toml"))
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)
    if args.ingest_c < 1 or args.tagger_version < 1:
        parser.error("ingest-c and tagger-version must be positive known values")
    if bool(args.fixture_manifest) != bool(args.fixture_target):
        parser.error("fixture-manifest and fixture-target must be provided together")
    output = Path(args.out_dir).resolve()
    if output.is_relative_to(Path(args.headroom_config).resolve()):
        parser.error("out-dir cannot be inside the source Headroom config tree")
    if output.exists():
        parser.error("out-dir must be new; preparation never overwrites a prior campaign")
    source = Path(args.config).read_text(encoding="utf-8")
    configs = {name: arm_config(source, flags) for name, flags in ARMS.items()}
    resolved = Path(args.resolved).resolve()
    gold = Path(args.gold).resolve()
    needles = json.loads(resolved.read_text(encoding="utf-8"))["needles"]
    gold_map = json.loads(gold.read_text(encoding="utf-8"))
    try:
        names = validate_bank(needles, gold_map)
    except ValueError as exc:
        parser.error(str(exc))
    manifest = {
        "status": "prepared_not_run", "code_ref": args.code_ref,
        "pins": {"code_sha": code_sha(args.code_ref),
                 "bed_identity": args.bed_identity, "ingest_c": args.ingest_c,
                 "tagger_version": args.tagger_version, "k": 12,
                 "base_config_sha256": file_digest(args.config),
                 "resolved_sha256": file_digest(resolved), "gold_sha256": file_digest(gold),
                 "needle_count": len(names), "needle_order_sha256": digest(names)},
        "source_bed": str(Path(args.source_bed).resolve()),
        "resolved": str(resolved), "gold": str(gold), "needle_count": len(names),
        "base_config": str(Path(args.config).resolve()),
        "headroom_inputs": {"toin": str(Path(args.headroom_toin).resolve()),
                            "ccr": str(Path(args.headroom_ccr).resolve()),
                            "config": str(Path(args.headroom_config).resolve())},
        "fixture_evidence": ({"path": str(Path(args.fixture_manifest).resolve()),
                              "sha256": file_digest(args.fixture_manifest), "target": args.fixture_target}
                             if args.fixture_manifest else None),
        "environment": {"PYTHONHASHSEED": "0", "PYTHONUTF8": "1",
                        "CYMATIX_DISABLE_LEARN": "1", "CYMATIX_SQLITE_CACHE_SIZE": "-12582912"},
        "unset_environment": ["CYMATIX_ENCODER_URL", "CYMATIX_CONFIG", "CYMATIX_GENOME_PATH", "CYMATIX_STORE_PATH",
                              "CYMATIX_DISABLE_HEADROOM", "CYMATIX_ABSTAIN_DISABLE"],
        "arms": {}, "commands": [],
        "notes": ["Use a clean checkout at the pinned commit for capture.",
                  "Each capture creates a fresh bed and Headroom snapshot under this directory.",
                  "No benchmarks or snapshot operations run during preparation.",
                  "Latency recorded by instrumented capture is diagnostic, not a performance gate."],
    }
    output.mkdir(parents=True)
    plan_path = output / "plan.json"
    for name, content in configs.items():
        config_path = output / f"{name}.toml"
        config_path.write_text(content, encoding="utf-8", newline="\n")
        manifest["arms"][name] = {"config": str(config_path),
                                  "config_sha256": file_digest(config_path),
                                  "flags": ARMS[name],
                                  "harmonic_bind_cap": 1 if name == "harmonic_staged" else None}
        manifest["commands"].append([sys.executable, "-m", "benchmarks.dogfood.migrations.capture",
                                     "--plan", str(plan_path), "--arm", name])
    for gate, (off, on) in PAIRS.items():
        manifest["commands"].append([sys.executable, "-m", "benchmarks.dogfood.migrations.gates",
                                     "--gate", gate, "--off", str(output / f"{off}.json"),
                                     "--on", str(output / f"{on}.json"),
                                     "--out", str(output / f"{gate}_verdict.json")])
    plan_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(plan_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
