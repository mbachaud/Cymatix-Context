"""Rebuild the EnterpriseRAG-Bench 500k blob at v0.9.x shipped defaults.

Why a wrapper instead of calling ``build_fixture_matrix.py`` directly
--------------------------------------------------------------------
The builder's own defaults contradict the shipped v0.9.0 config, so the
obvious command produces a bed that is *not* the shipped world:

* ``CYMATIX_BFM_SPLADE`` and ``CYMATIX_BFM_DENSE_BACKFILL`` are read through
  ``_env_flag(name, default="1")`` — i.e. **ON unless explicitly set to 0**,
  while ``cymatix.toml`` ships ``splade_enabled = false`` and
  ``dense_embed_on_ingest = false`` (#371, 2026-08-19).
* ``entity_graph=True`` is **hardcoded** in the ``Genome`` constructor rather
  than read from config.

This wrapper pins those explicitly, then writes a ``bed_provenance`` build
row recording what actually ran — not what ``cymatix.toml`` said — so the bed
can be attributed months from now and so ingest-knob drift between rebuilds
is mechanically detectable.

It also builds in ``--mode blob`` (direct), skipping the
sharded → ``shard_to_blob.py`` merge that silently dropped 199 rows on the
2026-07-17 build.

Usage
-----
    python scripts/build_erb_blob_v09x.py --out F:/tmp/erb_blob_v09x.db
    python scripts/build_erb_blob_v09x.py --out ... --workers 6   # parallel

Parallel ingest is gated: see ``scripts/ingest_equivalence_gate.py``.  The
BASELINES house rule blocks ``ingest_c > 1`` until equivalence is proven, so
``--workers`` refuses to run without ``--equivalence-receipt`` naming a
passing gate result.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

log = logging.getLogger("build_erb_blob_v09x")

PROFILE = "enterprise_rag_500k"

# The effective ingest-affecting settings this wrapper pins.  Recorded verbatim
# into the provenance row so the bed describes itself.
EFFECTIVE = {
    "ingestion.splade_enabled": False,      # CYMATIX_BFM_SPLADE=0
    "ingestion.dense_embed_on_ingest": False,  # CYMATIX_BFM_DENSE_BACKFILL=0
    "ingestion.sema_embed_on_ingest": False,   # not written by the builder
    "ingestion.entity_graph": True,         # hardcoded in build_fixture_matrix
    "ingestion.symbol_graph": False,
    "ingestion.backend": "cpu",
}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", required=True, help="Destination blob path (.db).")
    p.add_argument(
        "--out-dir",
        default=None,
        help="Builder scratch dir (default: alongside --out).",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Parallel file workers (0 = sequential, ingest_c=1). Gated.",
    )
    p.add_argument(
        "--equivalence-receipt",
        default=None,
        help="Path to a PASSING ingest_equivalence_gate receipt; required for --workers > 1.",
    )
    p.add_argument(
        "--notes", default=None, help="Free-text note recorded on the build event."
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Keep an existing partial bed and skip files already ingested "
            "(file-level resume). Without this the bed is rebuilt from scratch."
        ),
    )
    p.add_argument(
        "--mem-profile",
        default="bulk_load",
        help=(
            "CYMATIX_MEM_PROFILE for the writer (default 'bulk_load': "
            "cache-heavy — 64 MiB vs 4 GiB cache measured 6.47 vs 16.16 "
            "genes/s at 22.7 GiB bed depth, read-side lookups; 2026-08-26 "
            "receipts ingest_commit_batch_ab[_cache64].json)."
        ),
    )
    p.add_argument(
        "--sqlite-cache-mb",
        type=int,
        default=0,
        help=(
            "HARD writer page-cache override in MiB (0 = let --mem-profile "
            "size it from available RAM; non-zero wins over the profile via "
            "CYMATIX_SQLITE_CACHE_SIZE)."
        ),
    )
    p.add_argument(
        "--sqlite-mmap-gb",
        type=int,
        default=0,
        help="HARD writer mmap_size override in GiB (0 = profile).",
    )
    p.add_argument(
        "--commit-batch",
        type=int,
        default=5000,
        help=(
            "Genes per durable commit in the drain (0 = legacy per-gene). "
            "Content byte-identical either way; minor wins at any cache size "
            "(2026-08-26 receipt)."
        ),
    )
    p.add_argument(
        "--max-files",
        type=int,
        default=0,
        help=(
            "Build only the first N files of the deterministic discovery "
            "order (scale tests; 0 = full corpus). The capped bed is a "
            "prefix of the full build."
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Print the plan and exit."
    )
    return p


def _check_equivalence_gate(path: str | None, workers: int) -> None:
    """Refuse parallel ingest unless a passing gate receipt is supplied.

    The BASELINES rule (adopted from cc-exchange EXP-A) blocks ``ingest_c > 1``
    until an equivalence gate exists.  EXP-A's drift was *LLM-batching* drift
    and v0.9.x ingests with the CPU tagger, so equivalence is plausible — but
    plausible is not measured, and this build is meant to be citable.
    """
    if workers <= 1:
        return
    if not path:
        raise SystemExit(
            "--workers > 1 requires --equivalence-receipt (BASELINES ingest_c rule). "
            "Run scripts/ingest_equivalence_gate.py first."
        )
    receipt = json.loads(Path(path).read_text(encoding="utf-8"))
    if not receipt.get("equivalent"):
        raise SystemExit(
            f"equivalence gate receipt {path} does not report equivalent=true; "
            "refusing parallel ingest."
        )
    log.info(
        "equivalence gate OK (%s, n=%s files, workers=%s)",
        path, receipt.get("sample_files"), receipt.get("workers"),
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _build_parser().parse_args(argv)

    out = Path(args.out).resolve()
    out_dir = Path(args.out_dir).resolve() if args.out_dir else out.parent
    _check_equivalence_gate(args.equivalence_receipt, args.workers)

    env = dict(os.environ)
    # The two kill-switches that default ON in the builder.
    env["CYMATIX_BFM_SPLADE"] = "0"
    env["CYMATIX_BFM_DENSE_BACKFILL"] = "0"
    # Do not seed co-activation edges during a bench build.
    env.setdefault("CYMATIX_BFM_SEED_EDGES", "0")
    # No learn writes while building.
    env["CYMATIX_DISABLE_LEARN"] = "1"

    # SQLite memory. The 'auto' profile caps the writer page cache at 64 MiB
    # on any host — measured 2.5x slower than a 4 GiB cache at 22.7 GiB bed
    # depth with IDENTICAL write volume (2026-08-26 receipts
    # ingest_commit_batch_ab.json / _cache64.json): the cache serves the
    # upsert path's indexed READ lookups, whose working set grows with the
    # bed. 'bulk_load' sizes the cache from available RAM (cap 4 GiB).
    # The flags below are the documented hard overrides and win over the
    # profile inside sqlite_memory_budget().
    env["CYMATIX_MEM_PROFILE"] = args.mem_profile
    if args.sqlite_cache_mb:
        env["CYMATIX_SQLITE_CACHE_SIZE"] = str(-args.sqlite_cache_mb * 1024)
    if args.sqlite_mmap_gb:
        env["CYMATIX_SQLITE_MMAP_SIZE"] = str(args.sqlite_mmap_gb * 1024 ** 3)
    # Commit batching in the drain (default 5000; 0 = legacy per-gene).
    env["CYMATIX_BFM_COMMIT_BATCH"] = str(max(0, int(args.commit_batch)))
    if args.max_files > 0:
        env["CYMATIX_BFM_MAX_FILES"] = str(args.max_files)

    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "build_fixture_matrix.py"),
        "--profile", PROFILE,
        "--mode", "blob",
        "--out-dir", str(out_dir),
    ]
    if not args.resume:
        cmd.append("--rebuild")
    if args.workers and args.workers > 1:
        cmd += ["--parallel", "--workers", str(args.workers)]

    ingest_c = max(1, int(args.workers or 1))
    built_path = out_dir / f"{PROFILE}.db"

    log.info("profile      : %s", PROFILE)
    log.info("mode         : blob (direct; no shard_to_blob merge)")
    log.info("ingest_c     : %d", ingest_c)
    log.info("builder out  : %s", built_path)
    log.info("final out    : %s", out)
    log.info("resume       : %s", args.resume)
    if args.max_files:
        log.info("max files    : %d (prefix of discovery order)", args.max_files)
    log.info("env pins     : CYMATIX_BFM_SPLADE=0 CYMATIX_BFM_DENSE_BACKFILL=0")
    log.info(
        "sqlite       : profile=%s cache=%s mmap=%s commit_batch=%s",
        args.mem_profile,
        f"{args.sqlite_cache_mb} MiB" if args.sqlite_cache_mb else "(profile)",
        f"{args.sqlite_mmap_gb} GiB" if args.sqlite_mmap_gb else "(profile)",
        args.commit_batch,
    )
    log.info("command      : %s", " ".join(cmd))
    if args.dry_run:
        return 0

    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd, cwd=str(REPO_ROOT), env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    wall = time.perf_counter() - t0
    if proc.returncode != 0:
        log.error("build failed (exit %s) after %.1f s", proc.returncode, wall)
        return proc.returncode
    log.info("build finished in %.1f s (%.2f h)", wall, wall / 3600.0)

    if built_path.resolve() != out:
        log.info("moving %s -> %s", built_path, out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()
        built_path.replace(out)
        for suffix in ("-wal", "-shm"):
            side = Path(str(built_path) + suffix)
            if side.exists():
                side.replace(Path(str(out) + suffix))

    # Stamp provenance from what actually ran.
    from cymatix_context.storage import provenance

    conn = sqlite3.connect(str(out))
    try:
        try:
            from cymatix_context.config import load_config

            config = load_config()
        except Exception:
            log.warning("config load failed; stamping overrides only", exc_info=True)
            config = None
        row_id = provenance.record_event(
            conn,
            provenance.EVENT_BUILD,
            config=config,
            overrides=EFFECTIVE,
            ingest_c=ingest_c,
            corpus_id=PROFILE,
            notes=args.notes or (
                f"ERB 500k blob rebuilt at v0.9.x defaults; direct blob mode; "
                f"build wall {wall / 3600.0:.2f} h"
            ),
            with_identity=True,
        )
        log.info("provenance build event id=%s", row_id)
        events = provenance.read_events(conn)
        if events:
            e = events[-1]
            log.info(
                "  version=%s sha=%s genes=%s fts=%s identity=%s",
                e.get("cymatix_version"),
                (e.get("git_sha") or "?")[:12],
                e.get("gene_count"),
                e.get("fts_count"),
                (e.get("identity_sha256") or "")[:16],
            )
    finally:
        conn.close()

    log.info("bed bytes: %d", out.stat().st_size)
    log.info("inspect with: python -m cymatix_context.cli.dispatcher diag bed --db %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
