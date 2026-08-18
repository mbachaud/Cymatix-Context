"""run_ingest_throughput.py — ingest throughput + bed size per encoder-knob arm.

Fills the #377 "ingest throughput + bed size" gap and gives #371/#372 their
before/after instrument. At shipped defaults every ingested document pays the
BGE-M3 dense encode ([ingestion] dense_embed_on_ingest = true, ~2 GB model)
and the MiniLM SEMA encode ([ingestion] sema_embed_on_ingest = true) on top of
the spaCy CPU tagger, with one commit per strand. This harness makes those
costs measurable per-arm:

    defaults         shipped config as-is (dense + sema on)
    no_dense_ingest  ingestion.dense_embed_on_ingest = false
    no_embed_ingest  dense AND sema both false (no encoder ever materializes)

Worker model — one FRESH SUBPROCESS per arm, ablation_ladder-style config
overrides (fresh ``load_config()`` mutated per arm, fresh manager), fresh temp
genome per arm:

* Model loads are lazy and cached at module scope, so arms sharing a process
  would let arm #1 pre-pay arm #2's model bill and make ``first_file_s``
  meaningless. A fresh process per arm means the first ingest call carries
  exactly that arm's load bill (same reasoning as run_cold_start.py's
  fresh-server-per-iteration rule).
* Peak RSS is process-lifetime monotonic (Windows ``peak_wset``), so it is
  only attributable per-arm when the process IS the arm.
* The child pins ``CYMATIX_GENOME_PATH`` + ``CYMATIX_CONFIG`` to its temp bed
  (the documented bench convention — cymatix.toml [genome] notes env wins),
  so no stray ``load_config()`` inside the pipeline can touch a real genome.

Corpus: deterministic and in-repo — ``docs/**/*.md`` + ``cymatix_context/**/*.py``,
sorted by repo-relative path, seeded sample of ``--limit`` (default 60). The
receipt records the exact file list with a per-file content sha256 and a
corpus identity hash, so two runs are comparable byte-for-byte.

Honesty guardrails:

* **Model-load bill isolated.** ``first_file_s`` is timed separately from the
  steady-state per-file distribution (median/p95 over files 2..N) because the
  lazy encoder load lands entirely on file #1.
* **Arm self-validation from the produced bed**, not from the knob: the child
  counts non-null ``genes.embedding_dense_v2`` / ``genes.embedding`` rows and
  checks them against the arm's contract (defaults: both > 0; no_dense: dense
  0, sema > 0; no_embed: both 0). A knob that silently failed to apply — or an
  encoder that soft-failed — reads ``arm_valid: false`` with the reason, never
  as a clean fast arm.
* **#338 regression guard**: the receipt records whether the freshly-built
  bed's FTS5 table came out EXTERNAL-CONTENT
  (``storage.ddl.fts5_is_external_content``) — a fresh bed built contentful
  again would double-store every document.
* **Bed size is post-checkpoint.** The child runs
  ``PRAGMA wal_checkpoint(TRUNCATE)`` after closing the manager so
  ``final_db_bytes`` is the durable page count, not a WAL accident. Per-layer
  breakdown reuses ``benchmarks/dogfood/erb/storage_audit.py`` (dbstat when
  compiled in, page-math-anchored estimator otherwise — this box's CPython
  lacks SQLITE_ENABLE_DBSTAT_VTAB, verified 2026-08-05, so expect
  ``exact_available: false``).
* Temp beds are cleaned up on success and KEPT on failure with the path
  printed, so a failed arm can be autopsied.

Usage::

    python benchmarks/dogfood/run_ingest_throughput.py                # 60 files, 3 arms
    python benchmarks/dogfood/run_ingest_throughput.py --limit 12     # smoke
    python benchmarks/dogfood/run_ingest_throughput.py --arms defaults,no_embed_ingest
    python benchmarks/dogfood/run_ingest_throughput.py --keep-temp --out /tmp/it.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import random
import shutil
import sqlite3
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
ERB_DIR = Path(__file__).resolve().parent / "erb"
for _p in (str(REPO_ROOT), str(ERB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 — non-reconfigurable stream is fine
    pass

log = logging.getLogger("bench.ingest_throughput")

TOOL = "benchmarks/dogfood/run_ingest_throughput.py"

# ── Corpus selection ─────────────────────────────────────────────────────

CORPUS_GLOBS: Tuple[str, ...] = ("docs/**/*.md", "cymatix_context/**/*.py")
DEFAULT_LIMIT = 60
DEFAULT_SEED = 20260817
MAX_FILE_BYTES = 1 * 1024 * 1024  # 1 MB cap, fresh_genome_build.py convention

# ── Arm table ────────────────────────────────────────────────────────────
#
# Overrides are dotted config paths applied to a fresh load_config() object —
# the ablation_ladder.py pattern (config is per-manager; several gates are
# read once at construction, so a mid-run mutation would not take).
# Validation contracts are checked against the PRODUCED BED (non-null
# embedding column counts), not against the knob.

ARMS: Tuple[Tuple[str, Dict[str, Any]], ...] = (
    ("defaults", {}),
    ("no_dense_ingest", {"ingestion.dense_embed_on_ingest": False}),
    ("no_embed_ingest", {
        "ingestion.dense_embed_on_ingest": False,
        "ingestion.sema_embed_on_ingest": False,
    }),
)

# arm -> (dense_expected_nonzero, sema_expected_nonzero)
ARM_EMBED_CONTRACT: Dict[str, Tuple[bool, bool]] = {
    "defaults": (True, True),
    "no_dense_ingest": (False, True),
    "no_embed_ingest": (False, False),
}

# Modules whose presence in sys.modules at end-of-arm is a load observable.
OBSERVED_MODULES: Tuple[str, ...] = (
    "torch", "transformers", "sentence_transformers", "FlagEmbedding", "spacy",
)


# ── Small pure helpers ───────────────────────────────────────────────────


def set_dotted(cfg: Any, path: str, value: Any) -> bool:
    """Set ``cfg.<section>.<key> = value``. False when the path is unknown,
    so a renamed knob is a loud skip, not a silently-null arm
    (ablation_ladder.py's lesson)."""
    section, sep, key = path.partition(".")
    if not sep:
        if not hasattr(cfg, section):
            return False
        setattr(cfg, section, value)
        return True
    obj = getattr(cfg, section, None)
    if obj is None or not hasattr(obj, key):
        return False
    setattr(obj, key, value)
    return True


def get_dotted(cfg: Any, path: str, default: Any = None) -> Any:
    section, sep, key = path.partition(".")
    if not sep:
        return getattr(cfg, section, default)
    obj = getattr(cfg, section, None)
    if obj is None:
        return default
    return getattr(obj, key, default)


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile (no interpolation) — ablation_ladder.py
    convention, so p95 of a small arm is an observed value, not a fiction."""
    if not values:
        return None
    ordered = sorted(values)
    idx = int(round((pct / 100.0) * len(ordered) + 0.5)) - 1
    idx = max(0, min(idx, len(ordered) - 1))
    return round(float(ordered[idx]), 3)


def median_or_none(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return round(float(statistics.median(values)), 3)


def content_type_for(path: Path) -> str:
    return "code" if path.suffix.lower() == ".py" else "text"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_sha(repo_root: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:  # noqa: BLE001 — sha is metadata, not a gate
        log.warning("git rev-parse failed", exc_info=True)
    return None


def _rmtree_best_effort(path: Path) -> bool:
    """Windows-safe recursive delete (read-only flags cleared). Returns
    success; failure is logged, never raised — a stuck handle should not
    fail the run, just leave the path for the operator."""

    def _onerror(func: Any, p: str, exc_info: Any) -> None:
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:  # noqa: BLE001
            raise exc_info[1]

    try:
        shutil.rmtree(path, onerror=_onerror)
        return True
    except Exception:  # noqa: BLE001
        log.warning("could not remove %s", path, exc_info=True)
        return False


# ── Corpus ───────────────────────────────────────────────────────────────


def select_corpus(
    repo_root: Path, limit: int, seed: int,
) -> Tuple[List[Path], Dict[str, Any]]:
    """Deterministic corpus: glob, size-filter, sort, seeded sample, re-sort.

    The size filter runs BEFORE sampling so the candidate pool (and thus the
    sample) is a function of (tree content, seed, limit) only.
    """
    candidates: List[Path] = []
    for pattern in CORPUS_GLOBS:
        candidates.extend(repo_root.glob(pattern))
    pool: List[Path] = []
    for p in candidates:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if 0 < size <= MAX_FILE_BYTES:
            pool.append(p)
    pool.sort(key=lambda p: p.relative_to(repo_root).as_posix())
    if limit and limit < len(pool):
        selected = random.Random(seed).sample(pool, limit)
        selected.sort(key=lambda p: p.relative_to(repo_root).as_posix())
    else:
        selected = pool

    manifest: List[Dict[str, Any]] = []
    identity = hashlib.sha256()
    for p in selected:
        digest = sha256_file(p)
        rel = p.relative_to(repo_root).as_posix()
        manifest.append({"path": rel, "bytes": p.stat().st_size, "sha256": digest})
        identity.update(f"{rel}:{digest}\n".encode("utf-8"))

    corpus_block = {
        "globs": list(CORPUS_GLOBS),
        "seed": seed,
        "limit": limit,
        "max_file_bytes": MAX_FILE_BYTES,
        "candidates_total": len(pool),
        "selected": len(selected),
        "identity_sha256": identity.hexdigest(),
        "manifest": manifest,
    }
    return selected, corpus_block


# ── Child: one arm in a fresh process ───────────────────────────────────


def _peak_rss_bytes() -> Tuple[Optional[int], Optional[int]]:
    """``(peak_rss, current_rss)`` in bytes, cheapest available source.

    psutil's ``peak_wset`` (Windows) / ``ru_maxrss`` (POSIX, via resource)
    are both free reads. None when neither is available — never estimated.
    """
    try:
        import psutil  # noqa: PLC0415 — optional, child-only

        mi = psutil.Process().memory_info()
        peak = getattr(mi, "peak_wset", None)
        rss = getattr(mi, "rss", None)
        if peak is not None or rss is not None:
            return (int(peak) if peak is not None else None,
                    int(rss) if rss is not None else None)
    except Exception:  # noqa: BLE001
        log.warning("psutil RSS read failed", exc_info=True)
    if sys.platform != "win32":
        try:
            import resource  # noqa: PLC0415 — POSIX only

            ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # Linux reports KiB, macOS bytes; normalize the common case.
            return int(ru) * (1 if sys.platform == "darwin" else 1024), None
        except Exception:  # noqa: BLE001
            log.warning("resource ru_maxrss read failed", exc_info=True)
    return None, None


def _db_counts(conn: sqlite3.Connection) -> Dict[str, Any]:
    counts: Dict[str, Any] = {}

    def _one(key: str, sql: str) -> None:
        try:
            counts[key] = int(conn.execute(sql).fetchone()[0])
        except sqlite3.Error as exc:
            counts[key] = None
            counts.setdefault("count_errors", []).append(f"{key}: {exc}")

    _one("genes_rows", "SELECT COUNT(*) FROM genes")
    _one("dense_nonnull",
         "SELECT COUNT(*) FROM genes WHERE embedding_dense_v2 IS NOT NULL")
    _one("sema_nonnull",
         "SELECT COUNT(*) FROM genes WHERE embedding IS NOT NULL")
    _one("splade_rows", "SELECT COUNT(*) FROM splade_terms")
    if counts.get("splade_rows") is None:
        # Absent table = the layer wrote nothing; not an error worth keeping.
        counts.pop("count_errors", None)
    return counts


def _trimmed_storage_audit(db_path: Path) -> Dict[str, Any]:
    """Per-layer size breakdown via erb/storage_audit.py, trimmed for the
    receipt (the full per-object list stays in that tool's own output)."""
    try:
        import storage_audit  # noqa: PLC0415 — erb dir on sys.path
    except Exception as exc:  # noqa: BLE001
        log.warning("storage_audit import failed", exc_info=True)
        return {"error": f"storage_audit import failed: {exc}"}
    try:
        full = storage_audit.audit(db_path, want_exact=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("storage_audit.audit failed", exc_info=True)
        return {"error": f"storage_audit.audit failed: {exc}"}
    objects = full.get("objects") or []
    return {
        "mode": full.get("mode"),
        "exact_available": full.get("exact_available"),
        "exact_error": full.get("exact_error"),
        "file": full.get("file"),
        "accounted_bytes": full.get("accounted_bytes"),
        "unaccounted_bytes": full.get("unaccounted_bytes"),
        "layers": {
            layer: {"bytes": vals.get("bytes"), "pct_of_file": vals.get("pct_of_file")}
            for layer, vals in (full.get("layers") or {}).items()
        },
        "top_objects": [
            {"name": o.get("name"), "layer": o.get("layer"),
             "bytes": o.get("bytes"), "rows": o.get("rows")}
            for o in objects[:10]
        ],
        "unknown_objects": full.get("unknown_objects"),
    }


def _validate_arm_from_bed(
    arm: str, dense_nonnull: Optional[int], sema_nonnull: Optional[int],
) -> Tuple[bool, str]:
    """Check the produced bed against the arm's embedding contract."""
    contract = ARM_EMBED_CONTRACT.get(arm)
    if contract is None:
        return True, f"no embedding contract registered for arm {arm!r}"
    if dense_nonnull is None or sema_nonnull is None:
        return False, "embedding column counts unreadable — cannot validate"
    want_dense, want_sema = contract
    problems: List[str] = []
    if want_dense and dense_nonnull == 0:
        problems.append(
            "dense_embed_on_ingest is ON but 0 embedding_dense_v2 rows were "
            "written (encoder missing or soft-failed — the arm's cost does "
            "NOT include the dense encode)")
    if not want_dense and dense_nonnull > 0:
        problems.append(
            f"knob did not take: {dense_nonnull} embedding_dense_v2 rows "
            "written with dense_embed_on_ingest=false")
    if want_sema and sema_nonnull == 0:
        problems.append(
            "sema_embed_on_ingest is ON but 0 embedding rows were written "
            "(MiniLM missing or soft-failed)")
    if not want_sema and sema_nonnull > 0:
        problems.append(
            f"knob did not take: {sema_nonnull} embedding rows written with "
            "sema_embed_on_ingest=false")
    if problems:
        return False, "; ".join(problems)
    return True, (
        f"bed matches contract: dense_nonnull={dense_nonnull} "
        f"(expect {'>0' if want_dense else '0'}), sema_nonnull={sema_nonnull} "
        f"(expect {'>0' if want_sema else '0'})")


def run_arm_worker(spec_path: Path) -> int:
    """Child entry: one arm, fresh manager, fresh temp genome, result JSON."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    arm: str = spec["arm"]
    overrides: Dict[str, Any] = spec["overrides"]
    genome_path = Path(spec["genome_path"])
    result_path = Path(spec["result_path"])
    config_path: Optional[str] = spec.get("config")
    files = [Path(f) for f in spec["files"]]

    # Pin the bench bed BEFORE any cymatix import: CYMATIX_GENOME_PATH is the
    # documented highest-precedence override (cymatix.toml [genome] notes), so
    # even a stray load_config() deep in the pipeline resolves here, never to
    # a real genome.
    genome_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["CYMATIX_GENOME_PATH"] = str(genome_path)
    if config_path:
        os.environ["CYMATIX_CONFIG"] = config_path

    from cymatix_context.config import load_config  # noqa: PLC0415
    from cymatix_context.context_manager import CymatixContextManager  # noqa: PLC0415

    cfg = load_config(config_path)
    cfg.genome.path = str(genome_path)

    unapplied = [p for p, v in overrides.items() if not set_dotted(cfg, p, v)]
    result: Dict[str, Any] = {
        "arm": arm,
        "overrides": overrides,
        "genome_path": str(genome_path),
    }
    if unapplied:
        result.update(arm_valid=False,
                      validation_detail=f"unknown config path(s): {unapplied}",
                      errors=[f"config paths not found: {unapplied}"])
        result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 2

    # Identity guard (ablation_ladder pattern): an override that matches the
    # loaded base value measures the defaults arm twice. That A/A is still
    # run — it is a noise floor — but the receipt says so out loud.
    base_cfg = load_config(config_path)
    unchanged = [p for p, v in overrides.items() if get_dotted(base_cfg, p) == v]
    if overrides and len(unchanged) == len(overrides):
        result["identity_vs_defaults"] = (
            "all overrides already at base-config values: "
            + ", ".join(f"{p}={get_dotted(base_cfg, p)!r}" for p in unchanged))
    else:
        result["identity_vs_defaults"] = None

    result["effective"] = {
        "ingestion.dense_embed_on_ingest": get_dotted(cfg, "ingestion.dense_embed_on_ingest"),
        "ingestion.sema_embed_on_ingest": get_dotted(cfg, "ingestion.sema_embed_on_ingest"),
        "ingestion.splade_enabled": get_dotted(cfg, "ingestion.splade_enabled"),
        "ingestion.backend": get_dotted(cfg, "ingestion.backend"),
        "ingestion.entity_graph": get_dotted(cfg, "ingestion.entity_graph"),
        "ingestion.symbol_graph": get_dotted(cfg, "ingestion.symbol_graph"),
        "encoder_daemon.url": get_dotted(cfg, "encoder_daemon.url"),
    }

    errors: List[str] = []
    per_file: List[Dict[str, Any]] = []
    genes_created = 0

    manager = CymatixContextManager(cfg)
    t_total0 = time.perf_counter()
    try:
        for i, path in enumerate(files):
            rel = path.relative_to(REPO_ROOT).as_posix() if path.is_relative_to(REPO_ROOT) else str(path)
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{rel}: read failed: {type(exc).__name__}: {exc}")
                continue
            if not content.strip():
                continue
            t0 = time.perf_counter()
            try:
                gene_ids = manager.ingest(
                    content,
                    content_type=content_type_for(path),
                    metadata={"path": rel},
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{rel}: ingest failed: {type(exc).__name__}: {exc}")
                per_file.append({"path": rel,
                                 "ms": round((time.perf_counter() - t0) * 1000, 3),
                                 "genes": 0, "error": True})
                continue
            ms = (time.perf_counter() - t0) * 1000.0
            genes_created += len(gene_ids)
            per_file.append({"path": rel, "ms": round(ms, 3), "genes": len(gene_ids)})
            print(f"  [{arm}] [{i + 1}/{len(files)}] {ms:8.1f} ms  "
                  f"{len(gene_ids):3d} genes  {rel}", flush=True)
    finally:
        try:
            manager.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"manager.close failed: {type(exc).__name__}: {exc}")
    wall_total_s = time.perf_counter() - t_total0

    # ── Timing rollup ────────────────────────────────────────────────
    ok_ms = [r["ms"] for r in per_file if not r.get("error")]
    steady_ms = ok_ms[1:]  # file #1 carries the lazy model-load bill
    n_ok = len(ok_ms)
    result.update({
        "files_attempted": len(files),
        "files_ingested": n_ok,
        "genes_created": genes_created,
        "wall_total_s": round(wall_total_s, 3),
        "first_file_s": round(ok_ms[0] / 1000.0, 3) if ok_ms else None,
        "per_file_ms": {
            "median": median_or_none(ok_ms),
            "p95": percentile(ok_ms, 95),
            "min": round(min(ok_ms), 3) if ok_ms else None,
            "max": round(max(ok_ms), 3) if ok_ms else None,
        },
        "steady_state_ms": {
            "n": len(steady_ms),
            "median": median_or_none(steady_ms),
            "p95": percentile(steady_ms, 95),
        },
        "files_per_s": round(n_ok / wall_total_s, 3) if wall_total_s > 0 else None,
        "genes_per_s": round(genes_created / wall_total_s, 3) if wall_total_s > 0 else None,
        "steady_files_per_s": (
            round(len(steady_ms) / (sum(steady_ms) / 1000.0), 3)
            if steady_ms and sum(steady_ms) > 0 else None),
        "errors": errors,
        "per_file": per_file,
    })

    peak_rss, rss = _peak_rss_bytes()
    result["peak_rss_bytes"] = peak_rss
    result["rss_bytes"] = rss
    result["loaded_modules"] = {m: (m in sys.modules) for m in OBSERVED_MODULES}
    if "torch" in sys.modules:
        try:
            result["torch_cuda_available"] = bool(sys.modules["torch"].cuda.is_available())
        except Exception:  # noqa: BLE001
            log.warning("torch.cuda probe failed", exc_info=True)
            result["torch_cuda_available"] = None

    # ── Bed inspection (post-close, post-checkpoint) ─────────────────
    db_block: Dict[str, Any] = {}
    try:
        conn = sqlite3.connect(str(genome_path))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db_block["checkpoint"] = "TRUNCATE"
        except sqlite3.Error as exc:
            db_block["checkpoint"] = f"failed: {exc}"
        try:
            from cymatix_context.storage.ddl import fts5_is_external_content  # noqa: PLC0415

            db_block["fts5_is_external_content"] = fts5_is_external_content(conn.cursor())
        except Exception as exc:  # noqa: BLE001
            log.warning("fts5 form probe failed", exc_info=True)
            db_block["fts5_is_external_content"] = None
            errors.append(f"fts5 form probe failed: {exc}")
        db_block.update(_db_counts(conn))
        conn.close()
    except sqlite3.Error as exc:
        errors.append(f"bed inspection failed: {exc}")

    try:
        db_block["final_db_bytes"] = genome_path.stat().st_size
    except OSError as exc:
        errors.append(f"final db stat failed: {exc}")
        db_block["final_db_bytes"] = None
    for suffix in ("-wal", "-shm"):
        side = Path(str(genome_path) + suffix)
        db_block[f"sidecar{suffix}_bytes"] = side.stat().st_size if side.exists() else 0

    db_block["storage_audit"] = _trimmed_storage_audit(genome_path)
    result["db"] = db_block

    arm_valid, detail = _validate_arm_from_bed(
        arm, db_block.get("dense_nonnull"), db_block.get("sema_nonnull"))
    if not per_file or not n_ok:
        arm_valid, detail = False, f"no files ingested; {detail}"
    result["arm_valid"] = arm_valid
    result["validation_detail"] = detail
    result["errors"] = errors

    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0 if arm_valid else 1


# ── Parent: orchestrate arms, aggregate, receipt ─────────────────────────


def _spawn_arm(
    arm: str, overrides: Dict[str, Any], *, run_root: Path,
    files: List[Path], config_path: Optional[str], timeout_s: float,
) -> Dict[str, Any]:
    arm_dir = run_root / arm
    genome_dir = arm_dir / "genome"
    genome_dir.mkdir(parents=True, exist_ok=True)
    spec_path = arm_dir / "spec.json"
    result_path = arm_dir / "result.json"
    spec = {
        "arm": arm,
        "overrides": overrides,
        "genome_path": str(genome_dir / "genome.db"),
        "result_path": str(result_path),
        "config": config_path,
        "files": [str(f) for f in files],
    }
    spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")

    child_env = dict(os.environ)
    child_env["PYTHONUTF8"] = "1"
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--arm-worker", str(spec_path)],
        cwd=str(REPO_ROOT), env=child_env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    timed_out = False
    try:
        rc = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            log.warning("arm %s child did not die after kill", arm)
        rc = -1
    elapsed = time.perf_counter() - t0

    if result_path.exists():
        try:
            row = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            row = {"arm": arm, "arm_valid": False,
                   "errors": [f"result.json unreadable: {exc}"]}
    else:
        row = {"arm": arm, "arm_valid": False,
               "errors": [("child timed out after "
                           f"{timeout_s:.0f}s") if timed_out else
                          f"child exited rc={rc} with no result.json"]}
    row["child_rc"] = rc
    row["child_wall_s"] = round(elapsed, 3)
    row["_arm_dir"] = str(arm_dir)
    return row


def _vs_defaults(defaults_row: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    def _ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
        if a is None or b is None or b == 0:
            return None
        return round(a / b, 3)

    d_wall = defaults_row.get("wall_total_s")
    a_wall = row.get("wall_total_s")
    out["wall_speedup_x"] = _ratio(d_wall, a_wall)
    out["files_per_s_ratio"] = _ratio(row.get("files_per_s"),
                                      defaults_row.get("files_per_s"))
    out["steady_files_per_s_ratio"] = _ratio(row.get("steady_files_per_s"),
                                             defaults_row.get("steady_files_per_s"))
    d_first, a_first = defaults_row.get("first_file_s"), row.get("first_file_s")
    out["first_file_delta_s"] = (round(a_first - d_first, 3)
                                 if d_first is not None and a_first is not None else None)
    d_db = (defaults_row.get("db") or {}).get("final_db_bytes")
    a_db = (row.get("db") or {}).get("final_db_bytes")
    if d_db and a_db is not None:
        out["db_bytes_delta"] = a_db - d_db
        out["db_bytes_pct_of_defaults"] = round(100.0 * a_db / d_db, 2)
    else:
        out["db_bytes_delta"] = None
        out["db_bytes_pct_of_defaults"] = None
    d_rss, a_rss = defaults_row.get("peak_rss_bytes"), row.get("peak_rss_bytes")
    out["peak_rss_delta_bytes"] = (a_rss - d_rss
                                   if d_rss is not None and a_rss is not None else None)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"files to ingest per arm (default {DEFAULT_LIMIT})")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED,
                    help="seed for the corpus sample (default "
                         f"{DEFAULT_SEED}; corpus is sorted+seeded, so "
                         "(tree, seed, limit) fixes the file list)")
    ap.add_argument("--config", default=str(REPO_ROOT / "cymatix.toml"),
                    help="base TOML every arm starts from (default: shipped "
                         "repo cymatix.toml)")
    ap.add_argument("--arms", default=",".join(name for name, _ in ARMS),
                    help="comma-separated subset of: "
                         + ",".join(name for name, _ in ARMS))
    ap.add_argument("--arm-timeout", type=float, default=3600.0,
                    help="per-arm child timeout in seconds (default 3600; "
                         "the defaults arm pays the ~2 GB BGE-M3 load on "
                         "file #1)")
    ap.add_argument("--out", default="benchmarks/dogfood/ingest_throughput.json")
    ap.add_argument("--keep-temp", action="store_true",
                    help="keep temp genomes even on success")
    ap.add_argument("--arm-worker", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.arm_worker:
        return run_arm_worker(Path(args.arm_worker))

    arm_names = [a.strip() for a in args.arms.split(",") if a.strip()]
    arm_table = dict(ARMS)
    unknown = [a for a in arm_names if a not in arm_table]
    if unknown:
        print(f"unknown arm(s): {unknown}; known: {list(arm_table)}",
              file=sys.stderr)
        return 2

    config_path = str(Path(args.config).resolve())
    if not Path(config_path).exists():
        print(f"config not found: {config_path}", file=sys.stderr)
        return 2

    files, corpus_block = select_corpus(REPO_ROOT, args.limit, args.seed)
    if not files:
        print("corpus selection produced 0 files", file=sys.stderr)
        return 2

    run_root = Path(tempfile.mkdtemp(prefix="cymatix-ingest-bench-"))
    print(f"ingest throughput: {len(files)} files x {len(arm_names)} arms  "
          f"(seed {args.seed}, corpus {corpus_block['identity_sha256'][:16]})\n"
          f"temp root: {run_root}\n", flush=True)

    rows: List[Dict[str, Any]] = []
    failures: List[str] = []
    for arm in arm_names:
        print(f"── arm {arm} "
              f"(overrides: {arm_table[arm] or 'none — shipped config'}) ──",
              flush=True)
        row = _spawn_arm(arm, arm_table[arm], run_root=run_root, files=files,
                         config_path=config_path, timeout_s=args.arm_timeout)
        ok = bool(row.get("arm_valid")) and row.get("child_rc") == 0
        arm_dir = Path(row.pop("_arm_dir"))
        if ok and not args.keep_temp:
            # Free the bed immediately; result.json already holds everything.
            _rmtree_best_effort(arm_dir / "genome")
        else:
            if not ok:
                failures.append(arm)
            print(f"  arm {arm}: temp bed KEPT at {arm_dir / 'genome'}"
                  + ("" if ok else "  (arm failed — autopsy material)"),
                  flush=True)
        rows.append(row)
        db = row.get("db") or {}
        print(f"  arm {arm}: wall {row.get('wall_total_s')}s  "
              f"first-file {row.get('first_file_s')}s  "
              f"files/s {row.get('files_per_s')}  "
              f"genes {row.get('genes_created')}  "
              f"db {db.get('final_db_bytes')} B  "
              f"fts_external {db.get('fts5_is_external_content')}  "
              f"valid {row.get('arm_valid')}\n", flush=True)

    by_arm = {r["arm"]: r for r in rows}
    defaults_row = by_arm.get("defaults")
    summary: Dict[str, Any] = {
        "arms": {
            r["arm"]: {
                "arm_valid": r.get("arm_valid"),
                "wall_total_s": r.get("wall_total_s"),
                "first_file_s": r.get("first_file_s"),
                "steady_ms_median": (r.get("steady_state_ms") or {}).get("median"),
                "files_per_s": r.get("files_per_s"),
                "genes_per_s": r.get("genes_per_s"),
                "genes_created": r.get("genes_created"),
                "db_bytes": (r.get("db") or {}).get("final_db_bytes"),
                "peak_rss_bytes": r.get("peak_rss_bytes"),
                "fts5_is_external_content": (r.get("db") or {}).get(
                    "fts5_is_external_content"),
            } for r in rows
        },
    }
    if defaults_row is not None:
        summary["vs_defaults"] = {
            r["arm"]: _vs_defaults(defaults_row, r)
            for r in rows if r["arm"] != "defaults"
        }

    env_block = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "CYMATIX_ENCODER_URL": os.environ.get("CYMATIX_ENCODER_URL"),
        "CYMATIX_ENCODER_URL_set": "CYMATIX_ENCODER_URL" in os.environ,
        "CYMATIX_CONFIG": os.environ.get("CYMATIX_CONFIG"),
    }

    payload = {
        "tool": TOOL,
        "stamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git_sha": _git_sha(REPO_ROOT),
        "config": config_path,
        "config_sha256": sha256_file(Path(config_path)),
        "env": env_block,
        "note": (
            "one fresh subprocess per arm (model loads are lazy and cached "
            "at module scope — a shared process would let arm #1 pre-pay arm "
            "#2's load bill, and peak RSS is process-monotonic); fresh temp "
            "genome per arm; first_file_s carries the lazy encoder load, "
            "steady_state_ms excludes it; final_db_bytes is post-"
            "wal_checkpoint(TRUNCATE); arm validity is checked against the "
            "PRODUCED bed (non-null embedding column counts), not the knob; "
            "fts5_is_external_content doubles as the #338 fresh-bed "
            "regression guard"
        ),
        "corpus": corpus_block,
        "arms": rows,
        "summary": summary,
        "failures": failures,
    }
    out = Path(args.out)
    if not out.is_absolute():
        out = REPO_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if not failures and not args.keep_temp:
        if not _rmtree_best_effort(run_root):
            print(f"note: temp root not fully removed: {run_root}")
    else:
        print(f"temp root kept: {run_root}")

    print("\narm comparison:")
    for arm, s in summary["arms"].items():
        print(f"  {arm:18s} wall {s['wall_total_s']}s  "
              f"first-file {s['first_file_s']}s  "
              f"files/s {s['files_per_s']}  db {s['db_bytes']} B  "
              f"valid {s['arm_valid']}")
    print(f"\nwrote {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
