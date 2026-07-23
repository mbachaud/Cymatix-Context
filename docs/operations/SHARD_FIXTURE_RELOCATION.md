# Relocating a sharded fixture set to a foreign host

`scripts/rebase_shard_fixture.py` — the shard-fixture relocation tool
("needle adapter"). Use it whenever a sharded knowledge-store fixture
(coordinator `main.genome.db` + shard `.db` files) is consumed on a box it
was not built on.

## Why you need it

The coordinator's `shards.path` column records each shard's location as an
**absolute path on the build host**. On any other box, `ShardRouter` either
finds nothing at those paths or — worse — a per-shard `Genome` silently
auto-creates an **empty** database there (mkdir + fresh schema). Every
retrieval arm then returns `0.000 delivery / 0.000 pool` with `rc=0` and no
error anywhere. A "does the code path execute" pre-check passes; serving is
dead. (Receipted in cc-exchange `spark-erb-receipts` 0007: `CELL3_UNSUPPORTED`.)

## Procedure

1. Copy the fixture tree to the new box — the whole directory containing
   `main.genome.db` and the shard files, **including any `-wal` / `-shm`
   siblings** (or run `PRAGMA wal_checkpoint(TRUNCATE)` on each db before
   copying). A coordinator copied without its WAL can be missing recent rows.
2. Preview the rewrite plan (touches nothing):

   ```bash
   python scripts/rebase_shard_fixture.py /path/to/fixture-root --dry-run
   ```

3. Rebase, verify, and gate in one shot:

   ```bash
   python scripts/rebase_shard_fixture.py /path/to/fixture-root
   ```

   This (a) rewrites every recorded shard path to its discovered location
   under the fixture root (matched by longest path-component suffix,
   case-insensitive, separator-agnostic — POSIX-recorded paths match
   Windows trees and vice versa; written as absolute paths, since the
   router hands `shards.path` verbatim to `sqlite3.connect` and relative
   paths would resolve against the process CWD), (b) reopens the
   coordinator through the real `ShardRouter` and prints per-shard
   **gene-rows-served** counts — zero rows or any unopenable shard is a
   hard fail, and (c) runs the smoke gate: N probe queries (default 3,
   auto-derived from the fixture's own fingerprint terms) through
   `ShardedGenomeAdapter.query_docs`, requiring **nonzero delivery AND
   nonzero candidate pool on every probe in every attempted arm**, else a
   nonzero exit.

4. Before every bench run, re-run the gate without rewriting:

   ```bash
   python scripts/rebase_shard_fixture.py /path/to/fixture-root --verify-only
   ```

   This is the pre-run discipline that a bare "code path executes" check
   cannot provide: it asserts *serving*, not execution.

## Arms and degradation

- `lexical` always runs.
- `fused` (per-shard FTS + dense fusion) runs only when `torch` imports and
  at least one shard has `embedding_dense_v2` vectors; otherwise the gate
  degrades to lexical-only with an explicit warning — never silently. The
  fused arm needs the BGE-M3 encoder loadable (locally cached or
  fetchable); pass `--no-dense` to force lexical-only.
- `--probe TERM` (repeatable) overrides the auto-derived probes;
  `--max-genes N` sets the per-probe query depth (default 8).

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | rewrite applied (if needed), serving verified, smoke gate green |
| 2 | coordinator missing or unreadable |
| 3 | coordinator has zero shard rows (empty-coordinator signature — the real `main.genome.db` likely did not travel; re-copy it with its WAL) |
| 4 | recorded shard(s) with no / ambiguous file under the root — per-shard diagnostics printed, nothing rewritten |
| 5 | serve verification failed (missing/unopenable shard, or zero gene rows served) |
| 6 | smoke gate failed (a probe returned zero delivery or zero pool) |

## Known limits

- `source_index.repo_root` / `source_id` provenance strings are **not**
  rewritten (metadata only — nothing on the serving path opens files
  through them). Citations keep origin-host paths.
- The tool relocates; it does not repair. An empty or schema-incompatible
  shard fails verification rather than being rebuilt.
- Idempotent: re-running on an already-rebased fixture is a no-op green run.

## Verified against

A copy of the real `matrix-sharded/medium` fixture (6 shards, 17,483 genes)
with all six coordinator paths rewritten to simulated origin-host absolute
paths: pre-adapter `--verify-only` exits 5 with 0 rows served (the exact
foreign-host signature); post-adapter all 6 paths rewrite, 17,483 gene rows
serve, and the gate passes on both lexical and fused arms. Unit/integration
coverage: `tests/test_rebase_shard_fixture.py`.
