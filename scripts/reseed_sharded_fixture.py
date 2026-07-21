"""reseed_sharded_fixture.py -- backfill issue #223's harmonic_links fix
onto a sharded fixture that was already built BEFORE the fix landed.

``scripts/build_fixture_matrix.py --mode sharded`` now seeds intra-shard
edges per-shard (``seed_edges``) and cross-shard edges once across every
shard (``seed_cross_shard_edges``) automatically on every FRESH build
(see ``_build_one_shard`` and ``build_profile_sharded``). Fixtures built
before that fix landed shipped ZERO ``harmonic_links`` rows and would
otherwise need a full multi-hour re-ingest + dense-backfill just to pick
up harmonic_links -- this script applies the exact same two passes
directly to an existing fixture's shard ``.db`` files, reading the shard
list from ``main.genome.db`` (no re-ingest, no re-embed; runtime is
minutes).

Usage
-----
python scripts/reseed_sharded_fixture.py --profile-dir genomes/bench/matrix-sharded/medium

Idempotent: both underlying passes use ``ON CONFLICT ... DO NOTHING``, so
re-running this script on an already-seeded fixture is a harmless no-op
(reports 0 new edges written).
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cymatix_context.genome import Genome
from cymatix_context.shard_schema import list_shards, open_main_db
from cymatix_context.sharding import main_db_path
from cymatix_context.retrieval.seeded_edges import (
    seed_edges,
    seed_cross_shard_edges,
    SEEDING_CAP,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("reseed.sharded_fixture")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--profile-dir", required=True,
        help="Sharded fixture dir, e.g. genomes/bench/matrix-sharded/medium "
             "(must contain main.genome.db).",
    )
    ap.add_argument(
        "--cross-shard-per-shard-sample", type=int, default=20_000,
        help="Genes sampled per shard for the cross-shard bucket pass "
             "(default 20000 -- covers every shard in a medium-scale "
             "fixture in one pass; lower this for huge corpora).",
    )
    ap.add_argument(
        "--cross-shard-cap", type=int, default=400,
        help="Max cross-shard edges written in total (default 400).",
    )
    args = ap.parse_args(argv)

    main_path = main_db_path(args.profile_dir)
    if not main_path.exists():
        print(f"ERROR: {main_path} not found", file=sys.stderr)
        return 1
    main_conn = open_main_db(str(main_path))
    shards = list_shards(main_conn)
    if not shards:
        print(f"ERROR: no shards registered in {main_path}", file=sys.stderr)
        main_conn.close()
        return 1

    print(f"[reseed] {len(shards)} shard(s) registered in {main_path}")

    # -- Pass 1: intra-shard (seed_edges, per shard, first SEEDING_CAP genes) --
    intra_total = 0
    for row in shards:
        shard_name = row["shard_name"]
        shard_path = row["path"]
        if not os.path.exists(shard_path):
            log.warning("shard %s: db missing at %s, skipping", shard_name, shard_path)
            continue
        genome = Genome(
            path=shard_path, synonym_map={}, splade_enabled=False, entity_graph=True,
        )
        try:
            gene_ids = [
                r[0] for r in genome.conn.execute(
                    "SELECT gene_id FROM genes"
                ).fetchall()
            ]
            n = seed_edges(genome, gene_ids)
            intra_total += n
            log.info(
                "shard %s: seeded %d intra-shard edges (%d genes, cap=%d)",
                shard_name, n, len(gene_ids), SEEDING_CAP,
            )
        finally:
            genome.close()
    print(f"[reseed] intra-shard total: {intra_total}")

    # -- Pass 2: cross-shard (seed_cross_shard_edges, all shards at once) --
    shard_conns: dict[str, sqlite3.Connection] = {}
    cross_total = 0
    try:
        for row in shards:
            if os.path.exists(row["path"]):
                shard_conns[row["shard_name"]] = sqlite3.connect(row["path"])
        cross_total = seed_cross_shard_edges(
            shard_conns,
            cap=args.cross_shard_cap,
            per_shard_sample=args.cross_shard_per_shard_sample,
        )
        print(f"[reseed] cross-shard total: {cross_total}")
    finally:
        for conn in shard_conns.values():
            conn.close()

    main_conn.close()

    print()
    print("=" * 60)
    print(f"DONE  intra_shard={intra_total}  cross_shard={cross_total}  "
          f"total={intra_total + cross_total}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
