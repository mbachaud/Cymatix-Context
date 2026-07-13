# Genome Fixture Matrix

**Status:** working operator matrix, 2026-05-28 *(EnterpriseRAG-Bench fixtures added; original four-profile matrix unchanged since 2026-05-13)*.

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

## Sharded `harmonic_links` seeding (issue #223)

Before this fix, every sharded fixture this builder produced shipped
**zero** `harmonic_links` rows — `seed_edges()` (`helix_context/retrieval/seeded_edges.py`)
existed but was only ever called from tests, and co-activation writes are
no-ops on the read-heavy sharded adapter (`helix_context/sharding.py`,
`ShardedGenomeAdapter.upsert_doc`). That left
`ShardRouter._expand_cross_shard_coactivation` (#120) — and the
`coact_reserved_slots` / `coact_link_boost` knobs gated behind it (#223,
PR #270) — permanently unreachable in any sharded receipt: a null result
from that path was a fixture artifact, not a measurement.

`build_profile_sharded` now seeds two edge classes automatically
(`HELIX_BFM_SEED_EDGES=0` to disable, default on):

- **Intra-shard** — inside `_build_one_shard`, right after a shard's
  ingest completes, `seed_edges()` runs over that shard's own gene_ids
  (capped at `SEEDING_CAP=200` genes, same O(n²) pairwise multi-signal
  gate as production).
- **Cross-shard** — once, after every shard is built and registered,
  `seed_cross_shard_edges()` buckets genes across ALL shards by shared
  domain/entity token and applies the same 2-of-4 signal gate to
  cross-shard pairs only, writing edges bidirectionally into each
  endpoint's owning shard (capped at `CROSS_SHARD_SEEDING_CAP=400`
  total; buckets bigger than `CROSS_SHARD_BUCKET_CAP=40` are skipped as
  too generic a token to be a meaningful signal — e.g. a single common
  domain word shared by half the corpus).

An already-built fixture can be backfilled without a full re-ingest via
`python scripts/reseed_sharded_fixture.py --profile-dir <profile dir>`
(reopens the registered shard `.db` files directly; both passes are
`ON CONFLICT DO NOTHING`, so it's idempotent).

---

## EnterpriseRAG-Bench fixtures (added 2026-05-20 onwards)

A separate fixture family that backs Layer 3 of [`BENCHMARKS.md`](BENCHMARKS.md)
— the leak-free cross-corpus retrieval benchmark. Distinct from the four
monolithic profiles above in that they index an **external** corpus
(Onyx-dot-app's [EnterpriseRAG-Bench](https://github.com/onyx-dot-app/EnterpriseRAG-Bench)
upstream repo) rather than `F:/Projects` — eliminating the own-code +
CLAUDE.md leak surface that the rebuilt matrix bench's `isolated=True`
mode also addresses.

### Fixture table

| Profile | Genes | Shards | Disk | Status | Source root |
|---|---:|---:|---:|---|---|
| `enterprise_rag_10k` | ~10 K | 1 (monolithic) | ~600 MB | active | `F:/tmp/enterprise_rag_10k/sources/...` (9 source subdirs) |
| `enterprise_rag_50k` | ~50 K | 1 | ~3 GB | active | `F:/tmp/enterprise_rag_50k/sources/...` |
| `enterprise_rag_500k` | (subset prep) | 1 | — | deprecated | `F:/tmp/enterprise_rag_500k/sources/...` — replaced by Onyx-full |
| `enterprise_rag_onyx_full` | ~850 K | 105 (auto-subsharded) | 42.6 GB | superseded by v2 | `F:/Projects/EnterpriseRAG-Bench-main/generated_data/sources/...` |
| **`enterprise_rag_onyx_full_2`** | **850,501** | **100 (Path-A)** | **47.24 GB** | **canonical** | same upstream as v1; Path-A profile with default `--auto-subshard-threshold-files=100000` |

### Source-root scope (all five fixtures)

All five share the same 9-source-root pattern under the bench's
`generated_data/sources/` subtree:

```text
{ERB_ROOT}/generated_data/sources/confluence
{ERB_ROOT}/generated_data/sources/fireflies
{ERB_ROOT}/generated_data/sources/github
{ERB_ROOT}/generated_data/sources/gmail
{ERB_ROOT}/generated_data/sources/google_drive
{ERB_ROOT}/generated_data/sources/hubspot
{ERB_ROOT}/generated_data/sources/jira
{ERB_ROOT}/generated_data/sources/linear
{ERB_ROOT}/generated_data/sources/slack
```

### Excluded from `sources/` ingest

The following live elsewhere in the upstream repo and must NOT be indexed
(they would either inflate retrieval with bench infrastructure or leak gold
into the corpus):

- `questions.jsonl` / `extra_questions.jsonl` — benchmark Q&A
- `answer_evaluation/` — evaluation infrastructure
- `uuid_index.json` — gold-path map (lives in `generated_data/` ROOT, not under `sources/`)
- `src/` — benchmark generation code
- `company_overview.md`, `employee_directory.yaml`, etc. — top-level metadata

A 2026-05-25 gold-path audit confirmed all 742 `expected_doc_ids` across
500 EnterpriseRAG-Bench questions resolve INTO `sources/` subdirs — zero
outside, zero missing — so this scope is the canonical Onyx
leaderboard-comparable corpus.

### Build profile + auto-subsharding behavior

The `enterprise_rag_onyx_full_2` profile is *labelled* as a Path-A
"fewer-larger-shards" rebuild, but the builder's default
`--auto-subshard-threshold-files=100000` decomposes `slack` and `gmail`
into per-channel / per-user subshards. **Both Max-rig (Windows + Ryzen +
48 GB) and Joe's spark-e92c (Linux + ARM64 + 118 GB) land at 100 shards
on this profile** — the topology is identical across hosts. Earlier
documentation that referenced "~12 shards" was wrong (aspirational design,
not actual partitioning).

To get true Path-A behavior (single shard per root, ~9 shards total) the
operator must pass `--auto-subshard-threshold-files=0` explicitly.

### Path-portability gotcha (Joe's Spark deploy, 2026-05-28)

The `enterprise_rag_onyx_full_2` profile entry in
`scripts/build_fixture_matrix.py` currently hardcodes Windows
`F:\Projects\EnterpriseRAG-Bench-main\generated_data\sources\…` paths in
its `roots` list, AND the builder checks those paths via raw
`os.path.exists()` which bypasses pathlib-only monkey-patching. On Linux
deploys without a shim, the build silently completes with **0 shards
ingested** because none of the Windows paths exist.

Joe's working shim on spark-e92c also patches
`os.path.{exists,isdir,isfile}` + `os.walk`/`os.scandir`/`os.listdir` and
overrides `bfm.PROFILES['enterprise_rag_onyx_full_2']['roots']` to Linux
paths (mirroring the existing `enterprise_rag_500k` override pattern).

The cleanest upstream fix when the profile folds into master is to make
roots derive from `os.environ.get("ERB_ROOT", DEFAULT)` so the profile is
OS-agnostic out of the box. <30 LOC change. Should land BEFORE the
`bench/int-5fixture` integration branch's eventual master merge, OR as
part of whatever PR folds in the v2 profile entry. See PR #161's
close-as-superseded note for the recovery recipe.

### Branch / PR routing

The fixture profiles + their build machinery live across several open or
recently-closed PRs. As of 2026-05-28:

| Component | Branch / PR | Notes |
|---|---|---|
| `enterprise_rag_10k` / `_50k` / `_500k` profiles | `bench/int-5fixture` integration branch | Not yet merged to master |
| `enterprise_rag_onyx_full` profile (v1) | `bench/int-5fixture` | Superseded; v1 fixture exists on disk but produces Wall-1 OOM at 105-shard scale |
| `enterprise_rag_onyx_full_2` profile (v2) | originally PR #161 (closed-as-superseded by #162) | Profile entry not on master; only on `feat/onyx-full-v2-build-bundle` |
| Shard salvage helper | merged to master via [PR #162](https://github.com/mbachaud/helix-context/pull/162) @ `478e893` | Re-registers already-complete shards on rebuild (kill+restart cycle) |
| Tagger ReDoS fix | merged to master via PR #162 @ `32a74ef` | Critical for ingest — prevents 60+min hangs on underscore-heavy JSON |
| FTS5 cleanup perf fix | merged to master via PR #162 @ `ec74434` | Daemon /health first-response from "hangs forever" → ms |
| Batched-IN SQL fix | [PR #163](https://github.com/mbachaud/helix-context/pull/163) | Required to get above n=5 on variant A retrieval — eliminates silent shard skips at 850K-gene scale |
