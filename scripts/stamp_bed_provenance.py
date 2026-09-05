"""Write a provenance event into a bed.

Two uses:

1. **Stamping a build.** Called by the bench build chain so a new bed records
   the code, version and ingest-affecting config that produced it.
2. **Retro-stamping.** Beds built before ``bed_provenance`` existed carry no
   history.  A retro-stamp cannot invent the build config, so it records what
   is knowable now and says plainly in ``notes`` that the origin is
   unreconstructed.  That is strictly better than an unlabelled file, and it
   must never be written as though it were an original build record.

Examples
--------
Stamp a fresh build::

    python scripts/stamp_bed_provenance.py --db F:/tmp/erb_blob_v09x.db \\
        --event build --ingest-c 1 --corpus-id enterprise_rag_500k \\
        --identity --notes "ERB 500k rebuild at v0.9.x defaults"

Retro-stamp an existing bed, without claiming to know its origin::

    python scripts/stamp_bed_provenance.py --db F:/tmp/erb_blob.db \\
        --event stamp --no-config --identity \\
        --notes "retro-stamp; build config unreconstructed"
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cymatix_context.storage import provenance  # noqa: E402

log = logging.getLogger("stamp_bed_provenance")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Write a provenance event into a bed.")
    p.add_argument("--db", required=True, help="Path to the bed (.db).")
    p.add_argument(
        "--event",
        default=provenance.EVENT_STAMP,
        help=(
            "Event kind: build | ingest | migrate | compact | backfill | stamp "
            "(default: stamp)."
        ),
    )
    p.add_argument(
        "--ingest-c",
        type=int,
        default=None,
        help="Ingest concurrency the bed was built at (BASELINES house rule).",
    )
    p.add_argument("--corpus-id", default=None, help="Corpus/profile identifier.")
    p.add_argument("--notes", default=None, help="Free-text note for this event.")
    p.add_argument(
        "--config",
        default=None,
        metavar="TOML",
        help="cymatix.toml to snapshot (default: normal config resolution).",
    )
    p.add_argument(
        "--no-config",
        action="store_true",
        help=(
            "Do not snapshot any config. Use when retro-stamping a bed whose "
            "build config is unknown — recording today's config would be a lie."
        ),
    )
    p.add_argument(
        "--identity",
        action="store_true",
        help="Compute the gene_id digest (full scan; minutes on an 800k bed).",
    )
    p.add_argument("--json", action="store_true", help="Machine-readable output.")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _build_parser().parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        log.error("no such bed: %s", db)
        return 2

    config = None
    if not args.no_config:
        try:
            from cymatix_context.config import load_config

            config = load_config(args.config)
        except Exception:
            log.warning("config load failed — stamping without a config snapshot",
                        exc_info=True)
            config = None

    conn = sqlite3.connect(str(db))
    try:
        row_id = provenance.record_event(
            conn,
            args.event,
            config=config,
            ingest_c=args.ingest_c,
            corpus_id=args.corpus_id,
            notes=args.notes,
            with_identity=args.identity,
        )
        if row_id is None:
            log.error("provenance write failed for %s", db)
            return 1
        written = provenance.read_events(conn)
        record = next((e for e in written if e.get("id") == row_id), None)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        log.info("stamped %s as event id %s (%s)", db, row_id, args.event)
        if record:
            log.info(
                "  version=%s sha=%s dirty=%s genes=%s fts=%s identity=%s",
                record.get("cymatix_version"),
                (record.get("git_sha") or "?")[:12],
                record.get("git_dirty"),
                record.get("gene_count"),
                record.get("fts_count"),
                (record.get("identity_sha256") or "not computed")[:16],
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
