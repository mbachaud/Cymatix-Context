"""Run one prepared migration arm on fresh, source-preserving snapshots.

This command executes queries. prepare.py is the preparation-only entry point.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

from .gates import ARMS, digest, file_digest, normalized_context
from .prepare import ROOT, code_sha, validate_bank


def data_state(path):
    states = {}
    for suffix in ("", "-wal"):
        candidate = Path(str(path) + suffix)
        states[suffix or "db"] = ({"bytes": candidate.stat().st_size,
                                  "mtime_ns": candidate.stat().st_mtime_ns}
                                 if candidate.exists() else None)
    return states


def snapshot(source, destination):
    """Use SQLite backup, including WAL state, with an exclusive new target."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file() or source == destination or destination.exists():
        raise ValueError("Snapshot requires an existing source and a distinct new destination")
    before = data_state(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)) as src:
        src.execute("PRAGMA query_only=ON")
        with contextlib.closing(sqlite3.connect(destination, timeout=30)) as dst:
            src.backup(dst, pages=4096, sleep=0.05)
    after = data_state(source)
    if before != after:
        raise ValueError("Source DB/WAL changed while creating snapshot")
    return before


def _file_tree_hash(path):
    root = Path(path)
    return digest({str(p.relative_to(root)): file_digest(p)
                   for p in sorted(root.rglob("*")) if p.is_file()})


def observe_window(manager, window, mode):
    """Capture exact delivered/ranked identities alongside text hashes."""
    from cymatix_context.context_manager import DECODER_MODES, CANONICAL_DECODER_MODES

    scores = window.retrieval_scores
    if scores is None:
        raise ValueError("Missing request-scoped retrieval scores")
    delivered = list(window.expressed_gene_ids)
    decoder = window.ribosome_prompt or ""
    canonical_prompts = {value: DECODER_MODES[key] for key, value in CANONICAL_DECODER_MODES.items()}
    normalized_decoder = canonical_prompts.get(decoder, decoder)
    populated_matches = set()
    for key, template in CANONICAL_DECODER_MODES.items():
        if template.count("{answer_slate}") != 1:
            continue
        before, after = template.split("{answer_slate}")
        if decoder.startswith(before) and decoder.endswith(after) and len(decoder) >= len(before) + len(after):
            slate = decoder[len(before):len(decoder) - len(after) if after else None]
            populated_matches.add(DECODER_MODES[key].replace("{answer_slate}", slate))
    if len(populated_matches) > 1:
        raise ValueError("Ambiguous populated decoder template")
    if populated_matches:
        normalized_decoder = populated_matches.pop()
    tiers = window.tier_contributions or {}
    row = {
        "delivered_ids": delivered, "score_map_sha256": digest(scores),
        "ranked_ids_sha256": digest(list(manager.genome.last_ranked_ids or [])),
        "context_sha256": digest(window.expressed_context or ""),
        "decoder_sha256": digest(decoder),
        "normalized_decoder_sha256": digest(normalized_decoder),
        "harmonic_contributions": sum(1 for values in tiers.values() if values.get("harmonic", 0)),
    }
    try:
        row["normalized_context_sha256"] = digest(normalized_context(
            window.expressed_context or "", delivered, mode,
        ))
    except ValueError as exc:
        row["normalization_error"] = str(exc)
    return row


def run_observed_arm(config_path, genome_path, needles, gold, mode, harmonic_cap=None):
    """Keep the established ladder's execution; observe its real return values."""
    from benchmarks.dogfood.erb import ablation_ladder as ladder
    from cymatix_context.context_manager import CymatixContextManager
    from cymatix_context import knowledge_store as store

    observed = []
    counts = {"batched": 0, "overflow": 0}
    original_build = CymatixContextManager.build_context
    original_rows = store.KnowledgeStore._harmonic_candidate_rows
    original_limit = store._sqlite_variable_limit

    def rows(instance, cursor, candidates, *, batched):
        counts["batched"] += int(batched)
        counts["overflow"] += int(2 * len(candidates) > original_limit(cursor.connection))
        return original_rows(instance, cursor, candidates, batched=batched)

    def build(instance, *args, **kwargs):
        before = dict(counts)
        started = time.perf_counter()
        try:
            result = original_build(instance, *args, **kwargs)
            build_ms = (time.perf_counter() - started) * 1000
            row = observe_window(instance, result, mode)
        except Exception as exc:  # intentional benchmark recovery; ladder records failure too
            observed.append({"error": f"{type(exc).__name__}: {exc}"})
            raise
        row.update(harmonic_batched_calls=counts["batched"] - before["batched"],
                   harmonic_overflow_calls=counts["overflow"] - before["overflow"], build_only_ms=build_ms)
        observed.append(row)
        return result

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(CymatixContextManager, "build_context", build))
        stack.enter_context(patch.object(store.KnowledgeStore, "_harmonic_candidate_rows", rows))
        if harmonic_cap is not None:
            stack.enter_context(patch.object(store, "_sqlite_variable_limit", lambda conn: harmonic_cap))
        result = ladder.run_arm(
            ladder.Arm("baseline", validation=ladder.VALIDATION_CONTROL),
            genome_path=str(genome_path), config_path=str(config_path), needles=needles,
            gold_by_needle=gold, k=12, per_query=True,
        )
    # Existing ladder runs exactly one untimed warmup, even when it fails.
    if len(observed) != len(needles) + 1:
        raise ValueError("Ladder call count changed; refusing misaligned capture")
    for record, extra in zip(result["per_query"], observed[1:], strict=True):
        record.update(extra)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--arm", choices=ARMS, required=True)
    args = parser.parse_args(argv)
    plan_path = Path(args.plan).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan_path.parent.is_relative_to(Path(plan["headroom_inputs"]["config"]).resolve()):
        parser.error("campaign output cannot be nested inside source Headroom config")
    arm = plan["arms"][args.arm]
    if code_sha("HEAD") != plan["pins"]["code_sha"]:
        parser.error("checkout does not match pinned code commit")
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT,
        capture_output=True, text=True, check=True, timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()
    if dirty:
        parser.error("capture requires a clean frozen checkout; put campaign outputs outside it")
    for label, path, expected in (
        ("base config", plan["base_config"], plan["pins"]["base_config_sha256"]),
        ("arm config", arm["config"], arm["config_sha256"]),
        ("needles", plan["resolved"], plan["pins"]["resolved_sha256"]),
        ("gold", plan["gold"], plan["pins"]["gold_sha256"]),
    ):
        if file_digest(path) != expected:
            parser.error(f"{label} hash no longer matches preparation")
    if arm["flags"] != ARMS[args.arm]:
        parser.error("arm flags differ from the declared treatment")
    if os.environ.get("PYTHONHASHSEED") != "0":
        parser.error("launch this process with PYTHONHASHSEED=0")
    for key in plan["unset_environment"]:
        os.environ.pop(key, None)
    os.environ.update(plan["environment"])
    scratch = plan_path.parent / args.arm
    if scratch.exists() or (plan_path.parent / f"{args.arm}.json").exists():
        parser.error("arm scratch/receipt already exists; use a new preparation directory")
    scratch.mkdir()
    source = Path(plan["source_bed"])
    snapshot_path = scratch / "genome.db"
    source_state = snapshot(source, snapshot_path)
    with contextlib.closing(sqlite3.connect(snapshot_path, timeout=30)) as conn:
        provenance = conn.execute(
            "SELECT identity_sha256, ingest_c, config_json, at_utc FROM bed_provenance WHERE event='build' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not provenance or tuple(provenance[:2]) != (plan["pins"]["bed_identity"], plan["pins"]["ingest_c"]):
            raise ValueError("Bed build identity/ingest concurrency does not match plan")
        # Same streaming identity algorithm as storage.provenance: ordered
        # UTF-8 document IDs separated by NUL, without loading the full pool.
        identity = hashlib.sha256()
        for (document_id,) in conn.execute("SELECT gene_id FROM genes ORDER BY gene_id"):
            identity.update(str(document_id).encode("utf-8") + b"\x00")
        if identity.hexdigest() != plan["pins"]["bed_identity"]:
            raise ValueError("Current document identity differs from the bed build stamp")
        ingest_config = json.loads(provenance[2] or "{}")
        tagger_version = ingest_config.get("tagger_version", ingest_config.get("ingestion.tagger_version"))
        if tagger_version is None and provenance[3] < "2026-08-30":
            tagger_version = 1  # BASELINES.md's explicit pre-v2 provenance rule.
        if tagger_version is None and plan.get("fixture_evidence"):
            evidence = plan["fixture_evidence"]
            if file_digest(evidence["path"]) != evidence["sha256"]:
                raise ValueError("Fixture manifest changed since preparation")
            entry = json.loads(Path(evidence["path"]).read_text(encoding="utf-8"))["targets"][evidence["target"]]
            tagger_version = entry.get("tagger_version")
        if tagger_version != plan["pins"]["tagger_version"]:
            raise ValueError("Tagger provenance missing or does not match plan")
        last_access = conn.execute("SELECT MAX(json_extract(epigenetics, '$.last_accessed')) FROM genes").fetchone()[0] or 0
        if time.time() - float(last_access) < 3600:
            raise ValueError("Source logical access state is newer than the one-hour cold gate")
        harmonic_rows = conn.execute("SELECT COUNT(*) FROM harmonic_links").fetchone()[0]
        if args.arm.startswith("harmonic") and not harmonic_rows:
            raise ValueError("Harmonic equivalence gate requires populated harmonic_links")
    inputs = plan["headroom_inputs"]
    headroom = scratch / "headroom"
    headroom.mkdir()
    toin_hash = file_digest(inputs["toin"])
    shutil.copy2(inputs["toin"], headroom / "toin.json")
    ccr_state = snapshot(inputs["ccr"], headroom / "ccr_store.db")
    config_hash = _file_tree_hash(inputs["config"])
    shutil.copytree(inputs["config"], headroom / "config")
    os.environ.update(HEADROOM_WORKSPACE_DIR=str(headroom), HEADROOM_TOIN_PATH=str(headroom / "toin.json"),
                      HEADROOM_CCR_SQLITE_PATH=str(headroom / "ccr_store.db"),
                      HEADROOM_CONFIG_DIR=str(headroom / "config"))
    from cymatix_context.config import load_config
    from dataclasses import asdict
    effective = load_config(arm["config"])
    for path, expected in arm["flags"].items():
        section, field = path.split(".")
        if getattr(getattr(effective, section), field) != expected:
            raise ValueError(f"Effective configuration pin failed: {path}")
    common_config = asdict(effective)
    for path in arm["flags"]:
        section, field = path.split(".")
        common_config[section].pop(field)
    runtime_environment = {}
    for key, value in os.environ.items():
        if key.startswith(("CYMATIX_", "HEADROOM_", "PYTHON", "OMP_", "MKL_")):
            normalized = value.replace(str(headroom), "<arm-headroom>")
            runtime_environment[key] = digest(normalized) if re.search("KEY|TOKEN|PASSWORD|SECRET", key) else normalized
    needles = json.loads(Path(plan["resolved"]).read_text(encoding="utf-8"))["needles"]
    raw_gold = json.loads(Path(plan["gold"]).read_text(encoding="utf-8"))
    validate_bank(needles, raw_gold)
    gold = {key: set(value) for key, value in raw_gold.items()}
    result = run_observed_arm(arm["config"], snapshot_path, needles, gold,
                              arm["flags"]["budget.wire_format"], arm["harmonic_bind_cap"])
    errors = list(result.get("errors", []))
    if data_state(source) != source_state or data_state(inputs["ccr"]) != ccr_state:
        errors.append("source DB/WAL changed during arm")
    if file_digest(inputs["toin"]) != toin_hash or _file_tree_hash(inputs["config"]) != config_hash:
        errors.append("source Headroom inputs changed during arm")
    result.update(arm=args.arm, errors=errors, harmonic_rows=harmonic_rows,
                  pins={**plan["pins"], "source_state": source_state, "ccr_state": ccr_state,
                        "toin_sha256": toin_hash, "headroom_config_sha256": config_hash,
                        "common_config_sha256": digest(common_config), "runtime_environment": runtime_environment,
                        "python": os.sys.version, "sqlite": sqlite3.sqlite_version},
                  config_sha256=arm["config_sha256"], effective_config_sha256=digest(asdict(effective)),
                  flags=arm["flags"], harmonic_bind_cap=arm["harmonic_bind_cap"],
                  source_last_access=last_access, python=os.sys.version, sqlite=sqlite3.sqlite_version,
                  environment={**plan["environment"], **{k: os.environ[k] for k in (
                      "HEADROOM_WORKSPACE_DIR", "HEADROOM_TOIN_PATH", "HEADROOM_CCR_SQLITE_PATH", "HEADROOM_CONFIG_DIR")}},
                  latency_claim="none; instrumentation and snapshot copying affect timing")
    (plan_path.parent / f"{args.arm}.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
