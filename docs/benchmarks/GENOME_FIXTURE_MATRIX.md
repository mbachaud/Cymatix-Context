# Genome Fixture Matrix

**Status:** working operator matrix, 2026-05-13.

This document records the four current monolithic Helix test-genome size
profiles. It is intentionally limited to source-root selection: it does not
build databases, choose shard layout, or rename the current scripts. Use it as
the source of truth when the post-reorganization `.db` build path is wired.

## Current four

| Profile | Shape | Active roots | Scope notes |
|---|---|---:|---|
| `small` | Focused project smoke corpus | 4 | Core private project set only. Fastest non-toy profile for retrieval behavior checks. |
| `medium` | Broader project corpus | 6 | Adds `Education` and `helix-context`; ignore existing `.db` artifacts under `helix-context`. |
| `large` | Full projects corpus | 1 | Whole `F:/Projects` tree. Treat generated artifacts, dependency folders, logs, and existing genome DBs as exclusions. |
| `xl` | Projects plus external Steam/game code corpus | 13 | Full `F:/Projects` plus selected game installs across `F:`, `E:`, `D:`, and `C:`. External game roots are noise/stress material, not project ownership. |

## Root sets

### `small`

```text
F:/Projects/BookKeeper
F:/Projects/CosmicTasha
F:/Projects/two-brain-audit
F:/Projects/MaxExpressKit
```

### `medium`

```text
F:/Projects/BookKeeper
F:/Projects/CosmicTasha
F:/Projects/two-brain-audit
F:/Projects/MaxExpressKit
F:/Projects/Education
F:/Projects/helix-context
```

Medium-specific note: ignore `.db`, `.sqlite`, `.sqlite3`, and related SQLite
sidecar files inside `F:/Projects/helix-context` (and any other root that
holds knowledge-store artifacts).

### `large`

```text
F:/Projects
```

Large-specific note: this is the whole projects tree. It should still apply the
standard ingest denylist for dependency/build/cache directories and generated
benchmark artifacts.

### `xl`

```text
F:/Projects
F:/Factorio
F:/SteamLibrary/steamapps/common/Universe Sandbox 2
F:/SteamLibrary/steamapps/common/Satisfactory Modeler
F:/SteamLibrary/steamapps/common/Dyson Sphere Program
F:/SteamLibrary/steamapps/common/Cities Skylines II
E:/SteamLibrary/steamapps/common/SpaceEngineers2
E:/SteamLibrary/steamapps/common/BeamNG.drive
D:/SteamLibrary/steamapps/common/Kerbal Space Program
D:/SteamLibrary/steamapps/common/Turing Complete
C:/Program Files (x86)/Steam/steamapps/common/The Farmer Was Replaced
C:/Program Files (x86)/Steam/steamapps/common/Stationeers
```

XL-specific note: Steam/game roots should prefer code and script-like material
such as Lua, Python, C#/JS/TS/JSON/TOML/YAML, config files, manifests, and
Markdown/text. Large binaries, textures, models, audio, video, shader caches,
and save/build directories should stay out of the monolithic fixture.

## Common ingest rules

Apply these rules to all four profiles unless a profile overrides them:

| Rule | Applies to |
|---|---|
| Use forward-slash normalized paths in docs, config, manifests, and test output. | All profiles |
| Skip existing knowledge-store artifacts: `.db`, `.sqlite`, `.sqlite3`, `*-wal`, `*-shm`, benchmark output DBs. | All profiles |
| Skip dependency/build/cache folders such as `.git`, `.venv`, `venv`, `node_modules`, `.next`, `dist`, `build`, `target`, `__pycache__`, `.pytest_cache`, `.ruff_cache`. | All profiles |
| Skip obvious secret/key files and content-bearing credential blobs. | All profiles |
| Record missing roots as warnings, not hard failures, so the matrix can run on machines without every Steam library mounted. | Especially `xl` |

## Reserved follow-on: sharded fixtures

The current matrix above covers the four monolithic blob profiles. Sharded
genome testing is a separate follow-on after the repo reorganization settles.

The expected testing shape is:

| Reserved profile | Backing corpus | Purpose |
|---|---|---|
| `medium-sharded` | Same roots as `medium` | Validate sharded routing against a practical project corpus. |
| `xl-sharded` | Same roots as `xl` | Validate shard routing and noise isolation under the largest external stress corpus. |

That yields six total test genomes when sharded coverage is active: four
variable-size monolithic blobs plus two sharded variants.

## Sharded main.db indexes

When `scripts/build_fixture_matrix.py` runs in sharded mode it writes two
tables into `main.genome.db` so bench code matches the surface the live
ingest path (`scripts/ingest_all.py`) produces:

- **`fingerprint_index`** — one row per gene_id, used by
  `ShardRouter.route` to decide which shards to open for a query.
- **`source_index`** — one row per (gene_id, shard_name), used by
  `helix_context/context_packet.py::_lookup_source_row` for packet
  freshness and authority decisions (PR #113).

`source_index` rows are written alongside the fingerprint payload from the
shard's `genes` table, with conservative defaults for any field the
shard build didn't observe yet:

| Field | Build-time value |
|---|---|
| `observed_at` | shard `genes.observed_at` if set, else build time |
| `last_verified_at` | shard `genes.last_verified_at` if set, else build time |
| `mtime`, `content_hash`, `repo_root`, `source_kind`, `support_span` | passed through from the shard's `genes` row (may be NULL) |
| `volatility_class` | shard value or `'medium'` |
| `authority_class` | shard value or `'primary'` |
| `invalidated_at` | always NULL at build time |
| `updated_at` | build time |

These defaults are conservative on purpose: a bench fixture exercises
freshness logic without forcing every classifier to run during the build.
A real ingest path will overwrite the row on its next observation cycle.
