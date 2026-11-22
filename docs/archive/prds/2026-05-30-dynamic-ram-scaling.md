# PRD: Dynamic, RAM-aware SQLite memory scaling

- **Date:** 2026-05-30
- **Author:** Max (w/ Claude)
- **Status:** IMPLEMENTED on branch `perf/dynamic-mem-budget` (off origin/master
  b7af5b0). §7 decision: **`auto` default approved.** Budget + wiring landed
  TDD-first; 212 unit tests green. End-to-end 100-shard perf smoke deferred until
  the recall@200 daemon frees port 11437.
- **Touches:** config defaults, feature flags, retrieval pipeline behavior, the
  100-shard commit-charge ceiling fix → PRD-first, signed off before implementing.

### Implementation notes (delta from the design above)
- **Did not add `system_ram_available_gb` to `HardwareInfo`** (PRD §3.1 item 1):
  `sqlite_memory_budget()` probes `psutil.virtual_memory().available` directly
  (or takes an explicit `available_bytes` for tests), so the frozen dataclass is
  left untouched — avoids churning every `HardwareInfo` constructor for a value
  the budget already reads. Can be added later purely for observability.
- **SQLite clamps a 2 GiB mmap request to the build max** (`2147418112` on
  sqlite 3.50.4); the `2 GiB` per-shard cap is therefore a request, not a
  guarantee — SQLite maps lazily ≤ file size regardless. Tests assert mmap is
  *enabled* + the exactly-round-tripping `cache_size`, not the literal mmap int.
- The reconnect path at `knowledge_store.py:~3525` does not set cache/mmap (it
  didn't in v0.6.1 either) — left as a pre-existing follow-up, out of scope.

---

## 1. Problem

v0.6.1 (PR #173, the "RAM bundle") hard-codes SQLite to its most conservative
memory posture on **every** host, regardless of how much RAM is actually free:

| Knob | v0.6.1 value | Site |
|---|---|---|
| writer page cache | `PRAGMA cache_size=-2048` (2 MB) | `knowledge_store.py:541` |
| reader page cache | `PRAGMA cache_size=-4096` (4 MB) | `knowledge_store.py:578` |
| memory-mapped I/O | `PRAGMA mmap_size=0` (**OFF**) | `knowledge_store.py:542,579`; `shard_schema.py` |

These were set to survive the **105-shard / 48 GB commit-charge ceiling**
([[helix-100shard-context-commit-ceiling]]): the daemon hit a 104 GB commit
wall. The code comment is explicit that `mmap_size=0` is a *"fan-out safety
guard … so a 100-way parallel shard open can never map the whole corpus into
process commit."*

**But the real fix for that crisis was A1 — the BGE-M3 model singleton**
(120 GB → 7 GB, by collapsing 100× duplicated 2 GB models into one). `mmap=0`
and the 2/4 MB cache caps were belt-and-suspenders bundled in the same PR.

With A1 in place, the conservative I/O posture is now pure over-throttling. It
hurts the common case:

- **Max's 48 GB DDR4 rig** — right now sits at 28 GB *available*, 21.6 GB cached.
  SQLite is reading 46 GB of shards through a **4 MB** page cache with mmap off:
  every repeat read is a syscall, no OS-page-cache locality inside SQLite.
- **Joe's 128 GB unified (DGX Spark)** — has the headroom to mmap the **entire**
  46 GB corpus resident, but v0.6.1 forces syscall reads instead. (His 2.1×
  latency edge over Max was already partly "mmap-in-RAM vs pagefile" at the *OS*
  level — re-enabling SQLite mmap compounds it.)

We over-tuned for the pathological extreme and made the default bad for the
hosts we actually run on.

## 2. Goal / Non-goals

**Goal:** SQLite `mmap_size` and `cache_size` scale to the host — generous when
RAM is free, automatically throttled when it is scarce or shard count is high —
with the 105-shard / 48 GB case still provably safe, and a one-env-var escape
hatch back to exact v0.6.1 behavior.

**Non-goals:**
- No change to `HELIX_DENSE_MATRIX_DTYPE` (orthogonal heap lever, leave default `float32`).
- No change to A1 (model singleton stays — it's the load-bearing fix).
- Not touching GPU offload (that's Phase 4 / Joe's §5 rec, separate track).
- No new heavy dependency — `psutil>=5.9` is already a dep and already used by `hardware.py`.

## 3. Design

### 3.1 Build on `hardware.py` (don't reinvent)

`hardware.py` already:
- detects `system_ram_gb` (total) via `psutil.virtual_memory().total` (`_detect_cpu`),
- exposes a cached `HardwareInfo` frozen dataclass + `get_hardware()`,
- uses a `(device_type, mem_threshold) → settings` **tier table** (`_BATCH_TABLE`)
  to pick GPU batch sizes — the exact pattern we mirror for memory.

Two additions:

1. **Capture `available` RAM**, not just total. `HardwareInfo` gains
   `system_ram_available_gb` (`psutil.virtual_memory().available` at detect time).
   Available is the right basis: on a 48 GB box with 20 GB already in use we want
   to claim from the *free* pool, not the nameplate total.

2. **A budget function:**
   ```python
   def sqlite_memory_budget(n_shards: int) -> SqliteMemPlan:
       """Return per-connection mmap_size (bytes) and cache_size (KiB, SQLite
       negative-KiB convention) given the host and shard count."""
   ```
   Returns a small frozen dataclass `{mmap_bytes: int, cache_kib: int}`.

### 3.2 The budget formula

Computed **once** at daemon init (snapshot `available` before shards open;
`n_shards` is known up front from the router), so every shard gets the same
deterministic figure — no progressive drift as shards consume RAM.

```
avail     = psutil.virtual_memory().available          # bytes, snapshot at boot
reserve   = max(4 GiB, RESERVE_FRAC * avail)           # heap + model singleton + dense matrix + OS
budget    = max(0, avail - reserve)                    # total we hand to SQLite across all shards
per_shard = budget / max(1, n_shards)

mmap_bytes = clamp( MMAP_FRAC * per_shard,  0,  MMAP_ABS_CAP )   # SQLite maps lazily ≤ file size
cache_kib  = clamp( CACHE_FRAC * per_shard,  CACHE_MIN, CACHE_MAX ) / 1024
```

Profile constants (the `auto` profile):

| const | value | rationale |
|---|---|---|
| `RESERVE_FRAC` | 0.25 | protect Python heap, A1 model (~2 GB), dense matrix (~3.3 GB fp32 @100-shard) |
| `MMAP_FRAC` | 0.80 | most of per-shard budget → file-backed mmap (OS-reclaimable) |
| `MMAP_ABS_CAP` | 2 GiB | predictability ceiling; SQLite maps lazily so over-provisioning is cheap |
| `CACHE_FRAC` | 0.20 | a slice → private page cache |
| `CACHE_MIN / MAX` | 2 MB / 64 MB | floor = v0.6.1 writer; ceiling bounds 100× private cache |

**Worked examples** (real Onyx corpus: 101 leaf DBs, 46 GB, shards ~0.5–1.3 GB):

| Host | avail | reserve | budget | /100 shards | mmap/shard | cache/shard | net effect |
|---|---|---|---|---|---|---|---|
| **Max 48 GB** | 28 GB | 7 GB | 21 GB | 215 MB | ~172 MB | 43 MB | partial-file mmap; ~17 GB mapped + 4.3 GB cache, **under** 28 GB avail |
| **Joe 128 GB** | 110 GB | 27.5 GB | 82.5 GB | 825 MB | up to 660 MB → **full file** for most shards | 64 MB | near-whole-corpus resident |
| **105-shard / 48 GB stress** (low avail at boot) | e.g. 12 GB | 4 GB | 8 GB | 76 MB | ~61 MB | ~15 MB | auto-throttles; **and** mmap is file-backed (Win: not pagefile commit), so the old ceiling is not re-approached |

The formula self-differentiates: Max gets budget-limited partial mmap, Joe gets
full-corpus mmap, the stress case auto-throttles. **By construction the budget
never exceeds free RAM** (it's derived from `available` minus a reserve), so
`auto` cannot OOM the box — modulo the container caveat in §6.

### 3.3 Env surface

| var | default | meaning |
|---|---|---|
| `HELIX_MEM_PROFILE` | `auto` | `auto` \| `conservative` \| `aggressive` \| `<N>gb` (explicit total SQLite budget) |
| `HELIX_SQLITE_MMAP_SIZE` | — | hard per-conn override (bytes); wins over profile |
| `HELIX_SQLITE_CACHE_SIZE` | — | hard per-conn override (SQLite KiB convention); wins over profile |

- `conservative` reproduces **exactly v0.6.1** (`mmap=0`, cache 2/4 MB) — the escape hatch.
- `aggressive` raises `RESERVE_FRAC→0.15`, `MMAP_ABS_CAP→4 GiB` for RAM-rich hosts that want max locality (Joe could opt in).
- `<N>gb` lets an operator pin the total SQLite budget directly (e.g. inside a constrained container where psutil over-reports — see §6).

### 3.4 Wiring (exact sites)

1. `hardware.py`: add `system_ram_available_gb` to `HardwareInfo` + `_detect_cpu`;
   add `sqlite_memory_budget(n_shards)` + `SqliteMemPlan` + profile constants/table.
2. `knowledge_store.py:529–542` (writer) & `:567–579` (reader): replace the four
   hard-coded pragmas with values from the plan. Plan is resolved once and passed
   into `KnowledgeStore.__init__` (new optional `mem_plan=` kwarg; falls back to
   `sqlite_memory_budget(1)` for standalone/non-sharded use).
3. `shard_router.py`: resolve `sqlite_memory_budget(len(shard_names))` once at
   router init, thread the plan into each `_open_shard` → `KnowledgeStore`.
4. `shard_schema.py:open_main_db`: same plan for the main/router DB.

## 4. Test plan (TDD — write tests first, watch them fail)

`tests/test_mem_budget.py` (new):
- `auto` on a faked 28 GB-avail / 100-shard host → mmap ≈ 172 MB, cache within [2,64] MB.
- `auto` on faked 110 GB / 100-shard → mmap hits file-size/abs-cap regime, cache = 64 MB.
- `auto` on faked 12 GB / 105-shard → mmap small (< 100 MB); **never** exceeds budget.
- `conservative` → byte-identical to v0.6.1 (`mmap=0`, cache_size=-2048/-4096).
- `HELIX_SQLITE_MMAP_SIZE` / `HELIX_SQLITE_CACHE_SIZE` override beats profile.
- `<N>gb` explicit budget honored; `n_shards` division correct; `n_shards=1` path.
- psutil-raises fallback → conservative (never crash a daemon over a budget calc).

Extend `tests/test_ram_bundle.py`: the existing pragma assertions move to assert
*plan-derived* values under a faked host (keep a `conservative`-profile case
asserting the old constants so we prove the escape hatch is exact).

Mock `psutil.virtual_memory` (return a namedtuple with `.available`) — no real
host dependence in tests; deterministic.

## 5. Rollout

- One PR off **origin/master** (b7af5b0 / v0.6.1), branch e.g. `perf/dynamic-mem-budget`.
- Patch version bump 0.6.1 → 0.6.2 (behavior-affecting default change, additive + reversible).
- CHANGELOG: note the default flip + the `conservative` escape hatch prominently.
- **Sequencing:** implement/test AFTER the recall@200 diagnostic frees port 11437
  (the running daemon holds the shards open; a second daemon for an integration
  smoke would contend). Unit tests (mocked psutil) need no daemon and can be
  written anytime.

## 6. Risks

| risk | mitigation |
|---|---|
| **Container psutil over-reports host RAM** (no cgroup integration) → over-budget in a constrained container | `<N>gb` explicit budget + `conservative` escape hatch; reserve frac + file-backed/lazy mmap limit blast radius; document for container deploys |
| Default flip surprises an existing v0.6.1 user | additive + reversible via one env var; patch-bump + loud CHANGELOG; budget is provably ≤ free RAM |
| mmap re-enabled reintroduces the ceiling | A1 (the actual driver) stays; budget derived from `available`; Windows file-backed read mmap is not pagefile-commit-charged; stress-case row in §3.2 stays well under the old 104 GB wall |
| Per-shard variance (1.3 GB shards) | `MMAP_ABS_CAP` + SQLite lazy mapping (maps ≤ file size touched) bound any single shard |

## 7. THE sign-off decision

**Default profile.** I recommend **`auto`** (the dynamic budget) as the shipped
default, with `conservative` as the documented escape hatch. Rationale: you
explicitly said v0.6.1 is "too locked down," and `auto` is provably ≤ free RAM by
construction, so the downside is bounded and one env var away.

The alternative is keeping `conservative` (v0.6.1) as default and making `auto`
opt-in — safest, but it doesn't fix the out-of-box experience you flagged.

→ **Confirm `auto`-as-default (recommended) vs `conservative`-default + opt-in**,
and whether the §3.2 constants (RESERVE 0.25 / MMAP 0.80 / caps) look right to
you, and I'll implement TDD-first off origin/master.
