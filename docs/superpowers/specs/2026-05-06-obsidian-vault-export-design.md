# Obsidian Vault Export — v1 (Read-only) Design Spec

**Date:** 2026-05-06
**Status:** Draft (ready for plan)
**Scope:** v1 ships read-only export + diagnostic traces. Authored delta-sync, watcher, validator, inbox ingest, and the `_unresolved/` resolution loop are deferred to v1.1+; their full design lives in [`2026-05-06-obsidian-vault-export-full-design-v1.1plus.md`](./2026-05-06-obsidian-vault-export-full-design-v1.1plus.md).

**Related:**
- Discussion #34 (this design's origin)
- Discussion #33 (typed-edges proposal — informs frontmatter shape; placeholders only in v1)
- PR #32 (WAL bloat fix — assumed merged)
- PR #36 (per-stage telemetry — feeds `_traces/`)

---

## Why read-only first

The full design (preserved alongside this spec) bundles four features: read-only browse, diagnostic traces, authored delta-sync, and inbox ingest. Three reviewers in the final-pass review unanimously called the architecture sound, but the anti-ship reviewer made a strong case that the curation/watcher/shadow-store stack is hypothesis-driven (we built it before observing real operator usage) and should ship after we have data.

v1 delivers Goals 1 + 2 from the full design — a complete browsable corpus and the diagnostic console — without paying for the bidirectional sync's correctness/operational burden. Goals 3 (curation write-back) and 4 (inbox ingest) ship in v1.1 once we know what operators actually edit and how often.

## Goals

1. **Browsable corpus** — operator navigates the genome as an Obsidian vault: search, graph view, Dataview queries, backlinks. No helix internals required.
2. **Diagnostic console** — every `/context` call exports a trace markdown showing fingerprint route + per-stage timing + foveated rank assignments.

## Non-goals (v1 — all deferred to v1.1+)

- Authored delta-sync of any kind (no watcher, no validator, no shadow store).
- Editing any field in Obsidian and having it flow back. Authored fields are rendered as cosmetic placeholders in frontmatter (always empty/false in v1).
- Inbox ingest (`_inbox/` folder is not present in v1).
- `_unresolved/` resolution loop.
- Edit-rejection logging (no edits to reject).
- Pinning of genes (no `pinned: true` write-back; trace pinning is separate and stays).
- The retrieval-contract changes from the full design (authorship discriminator surfacing, quarantine_reason enforcement, pinning vs. staleness handling). v1 makes no changes to `/context`, `/context/packet`, or `query_genes` — the vault is purely a derived view.

Also out of scope: real-time / sub-second propagation; multi-vault federation; cross-helix-instance vault sharing; Obsidian plugin.

## Invariants (v1)

These constrain every component below.

**I-1: Vault failures never degrade retrieval.** If the writer crashes, the pruner deadlocks, or the disk fills — the genome stays correct, `/context` keeps serving, no agent-visible state is corrupted.

**I-4: All vault writes are atomic from any observer's POV.** Files are either fully helix-written or absent — never partially written. Achieved via vault-root lockfile + per-write tmp+rename. (Numbering kept consistent with the full design for cross-reference.)

**I-6: Vault rendered content is recoverable from `genome.db` alone.** The exception is pinned traces (`_traces-pinned/`) which exist only in the vault. README documents this and recommends backing up the vault folder for operators who depend on pinned traces.

**I-7: Vault is single-party scoped.** A vault represents exactly one `party_id`'s view of the genome. The exporter only emits genes whose `party_id` matches `vault.party_id` (defaults to the server's primary party). Multi-party deployments either run separate helix instances per party or run with the vault disabled.

(Invariants I-2, I-3, I-5 from the full design are not relevant in v1 because there's no operator write-back. They re-enter scope in v1.1.)

---

## Architecture

**Single-process embedded.** Mirrors `replication.py`'s lifecycle.

```
helix-context (FastAPI server)
│
├── lifespan startup
│   └── NEW: vault.start() — clear stale sentinels, init vault.db, start writer + pruner threads
│
├── helix_context/vault/   (NEW package — v1 surface only)
│   ├── __init__.py        — VaultManager (public API)
│   ├── writer.py          — gene → markdown (snapshot + incremental + trace)
│   ├── pruner.py          — TTL prune + pre-prune rollup + _stale/ refresh
│   ├── schema.py          — frontmatter shape (computed fields + cosmetic authored placeholders)
│   └── locking.py         — vault-root filelock for CLI/server coordination
│
├── HTTP additions (v1)
│   ├── POST /export/obsidian              — trigger snapshot export
│   ├── GET  /vault/status                 — last export, pruner state, file counts, disk usage
│   ├── POST /vault/trace                  — write trace for a recent request_id
│   ├── POST /vault/traces/{id}/pin        — move trace to _traces-pinned/
│   └── POST /vault/traces/{id}/unpin      — move back, reset TTL
│
└── CLI: new `helix-vault` script (v1 subcommands only)
    ├── helix-vault export [--full]
    ├── helix-vault trace [--last N | <request_id>]
    ├── helix-vault {pin,unpin} <request_id>
    ├── helix-vault status
    └── helix-vault prune [--dry-run]
```

**Threading model:** `VaultManager` owns two daemon threads — writer (handles export requests) and pruner (handles TTL + `_stale/` refresh). Both use try/except + sleep loops with exponential backoff on failure.

The CLI talks to the running server over HTTP. When the server isn't running, CLI exits with a clear error rather than racing on the file. This prevents concurrent CLI/server file-write conflicts.

**Modules NOT in v1** (preserved in the full-design doc for v1.1): `watcher.py`, `validator.py`, `shadow.py`, `inbox.py`. Their absence is the entire point of the v1 scope cut.

---

## Frontmatter schema

### Computed fields (rendered every export, helix is authoritative)

```yaml
gene_id: abc123def456
chromatin: euchromatin
domains: [auth, jwt, security]
content_type: code
source_id: helix_context/auth/middleware.py
source_lines: 42-89
content_sha256: 7f3a1c...
last_seen: 2026-05-06T20:45:00Z
last_seen_ts: 1736198700.0
live_truth_score: 0.92
co_activation_partners: 7
party_id: swift_wing21
participant_handle: laude
```

### Authored fields (cosmetic placeholders in v1)

The full design defines these as bidirectional. **In v1 they are rendered with stub values and ignored on read-back** (because there's no read-back). Operators editing them sees no effect — and v1 makes no claim about persistence. This is documented prominently in the README the exporter generates.

```yaml
# v1: rendered for forward-compat with v1.1; edits ignored
operator_notes: ""
operator_tags: []
pinned: false
quarantine_reason: null
supersedes: []
contradicts: []
implements: []
documented_by: []
tests: []
# v1.1+ activates these via watcher + validator + shadow store
```

The choice to render them as stubs (rather than omit) keeps the v1 → v1.1 transition smooth: existing vaults don't need re-export when v1.1 adds write-back; the keys are already present.

### Render-only body sections

- `## Typed edges` — sparse wikilinks (the 5 typed-edge fields above as `[[wikilinks]]`). In v1 these are always empty (because nothing populates them). They appear as a section header with "(none yet — v1.1 enables operator-authored edges)" placeholder text, so the section position in the file is stable.
- `## Backlinks` — Obsidian populates from any `[[gene-...]]` reference elsewhere. Works automatically.
- `## Last retrieval` (when available) — 12-signal tier scores from the most recent `/context` call. Volatile; in body, not frontmatter.

---

## Vault layout

```
~/.helix/vault/                         # path configurable; created with mode=0o700
├── README.md                           # generated; explains layout, last export ts,
│                                       # backup notes per I-6, v1 vs. v1.1 caveat
│                                       # for authored fields
├── vault.db                            # sibling SQLite (NOT in genome.db — see State Tracking)
├── .helix-state.json                   # small top-level state (~100 bytes)
├── .helix-syncing                      # sentinel during writes (cleared at startup)
│
├── genes/
│   ├── auth/
│   │   ├── middleware-7f3a1c.md
│   │   └── jwt-verify-9b2e44.md
│   ├── core/
│   │   └── ab/                         # 2-level fan-out at vault.fan_out_threshold (5000)
│   └── _orphan/                        # genes with no domain
│
├── _traces/
│   └── 2026-05-06T22-14-06_abc12345_exp1736371846.md
│       # filename includes expires_at as unix epoch — pruner filters by name
│
├── _traces-pinned/                     # pruner skips this folder; subject to
│   └── ...                             # vault.traces.max_retention_hours_hard
│
├── _stale/                             # genes with live_truth_score < threshold
│   └── ...                             # symlinks (POSIX) or pointer notes (Windows)
│
├── _sessions/
│   └── laude.md                        # per-participant activity log
│
└── _meta/
    ├── trace-rollups/
    │   └── 2026-05-06/
    │       ├── 14.md                   # hour-sharded; each file ≤ 3600 rows
    │       └── 15.md
    ├── co-activation-clusters.md
    ├── chromatin-tier-counts.md
    ├── party-id-attribution.md
    └── 12-signal-stats.md
```

**Folders NOT in v1**: `_inbox/`, `_unresolved/`, `_meta/edit-rejections.md`. These ship in v1.1 alongside the watcher/validator stack.

**Filename convention for genes:** `<source_stem>-<short_id>.md`. `short_id` is the first 6 chars of the gene_id. Conflicts deduped by suffix.

**Path safety:** the writer constructs paths as `vault_root / domain / f"{stem}-{short_id}.md"`. Before any write, the result is `Path(...).resolve()` and asserted to start with `vault_root.resolve()`. Genes producing paths outside the vault are written to `_orphan/` with sanitized basename.

**File permissions:** vault root + all subdirectories created with `mode=0o700`. Startup checks existing permissions and warns if they're broader.

---

## Outbound data flow (gene → markdown)

```
genome.db
  │
  ├── full_export()                      [CLI: helix-vault export --full]
  │     ↓
  │   acquire vault-root lock
  │     ↓
  │   query all genes (paginated, 500/batch)
  │     ↓
  │   for each gene: render_gene_markdown(gene) → write_atomic(path, content)
  │     ↓
  │   release lock; update vault_state
  │
  ├── incremental_export()               [post-ingest hook + manual trigger]
  │     ↓
  │   query genes WHERE last_seen > last_export_ts (uses idx_genes_last_seen)
  │     ↓
  │   same render → write loop, only changed
  │
  └── trace_export(request_id, fingerprint, fov_ranks)
        ↓
      filename: 2026-05-06T22-14-06_<id>_exp<expires_unix>.md
        ↓
      render_trace_markdown(...) → write atomic to _traces/
```

### `render_gene_markdown(gene)`

```python
def render_gene_markdown(gene: Gene, ctx: ExportContext) -> str:
    fm = build_frontmatter(gene, ctx)   # computed + cosmetic authored stubs
    body = build_body(gene, ctx)
    return f"---\n{yaml.safe_dump(fm)}---\n\n{body}"
```

`build_frontmatter` reads from `genes`, `gene_attribution`, `epigenetics`, `harmonic_links`, `co_activation`. Authored-field placeholders are produced from a static schema, not from `gene_attribution.notes` (because we don't read those keys back in v1).

### Atomic writes (per I-4)

Every file write goes through `write_atomic(path, content)`:

1. Acquire vault-root lock (`vault.lock`, via `filelock` library).
2. Write to `path.tmp`.
3. Touch `.helix-syncing` sentinel in vault root.
4. `os.replace(path.tmp, path)` — atomic on POSIX and Windows.
5. Remove `.helix-syncing`.
6. Release vault-root lock.

The sentinel is forward-compat with v1.1's watcher (which will check it for self-event suppression). In v1 there's no watcher to deceive, but the sentinel is cheap to maintain and protects against any external file-watcher (e.g., a sync tool) misinterpreting in-flight writes.

**Stale sentinel cleanup:** `vault.start()` removes any pre-existing `.helix-syncing` file unconditionally. If a previous process crashed mid-write, the sentinel is stale; clearing it is safe because nothing else is running yet.

**Body redaction (optional):** `vault.redact_body = true` (default `false`) replaces gene body content with `{ "redacted": true, "sha256": "...", "byte_count": N }` and a one-line excerpt. Recommended for any vault that might be observed by Obsidian Sync, iCloud Drive, Dropbox, etc. README warns about this prominently.

### State tracking — sibling `vault.db`

State lives in a SQLite file at the vault root, NOT in `genome.db`:

```sql
-- vault.db schema (v1)
CREATE TABLE IF NOT EXISTS vault_state (
  gene_id                  TEXT PRIMARY KEY,
  vault_path               TEXT NOT NULL,   -- relative to vault root
  last_exported_ts         REAL NOT NULL,
  last_exported_disk_hash  TEXT             -- sha256 of full file content
);

CREATE INDEX IF NOT EXISTS idx_vault_state_path ON vault_state(vault_path);

-- separate addition to genome.db (NOT in vault.db)
CREATE INDEX IF NOT EXISTS idx_genes_last_seen ON genes(last_seen);  -- for incremental
```

**Why a sibling, not a genome.db table:** lifecycle separation (drop the vault folder cleanly without touching the canonical store), backup/restore symmetry, vault-path migration is a folder move, and v1.1 will add a `shadow_authored` column to the same table — keeping the schema in `vault.db` keeps it cohesive.

**`last_exported_disk_hash`** is included in v1 even though no watcher reads it. It's cheap to compute (already needed for change-detection within incremental_export) and unlocks v1.1's content-hash self-event sentinel without a schema migration.

**Top-level vault state** (`.helix-state.json`):

```json
{
  "schema_version": 1,
  "last_full_export_ts": 1736198700.0,
  "last_incremental_export_ts": 1736198820.0,
  "exported_gene_count": 3683,
  "fan_out_engaged_domains": ["core"]
}
```

`schema_version` mismatch → refuse-to-start with structured log + operator-runs-`helix-vault migrate` message. v1 ships with no migrations defined; v1.1 will add the first migration as part of activating authored delta-sync.

### Trace markdown shape

Filename: `<iso8601>_<request_id>_exp<expires_unix>.md`. Pruner uses the `_exp<n>` suffix to filter expired traces without parsing frontmatter.

```markdown
---
request_id: abc12345
created_at: 2026-05-06T22:14:06Z
expires_at: 2026-05-08T22:14:06Z
pinned: false
trigger_reason: latency_outlier
total_latency_ms: 18432
health_status: sparse
---

# Trace: abc12345

## Per-stage timing
| stage | ms |
|---|---|
| extract | 12 |
| express | 45 |
| rerank | 12_400 |
| splice | 5_800 |
| assemble | 175 |

## Fingerprint route
*(from /fingerprint endpoint — paths considered)*

## Foveated rank assignments
*(top-K with their scores + budget tier)*

## Final budget genes
- [[middleware-7f3a1c]] (rank 1, score 0.92)
- ...
```

### Folder fan-out (eager migration)

`vault.fan_out_threshold` (default 5000) caps per-folder file count. When an incremental export would push a domain folder past the threshold, the writer:

1. Acquires the vault-root lock for the duration of the migration.
2. Migrates every existing file in that domain from `<domain>/<stem>-<id>.md` to `<domain>/<first2chars>/<stem>-<id>.md` via `os.replace()` per file.
3. Updates `vault_state.vault_path` for every affected gene_id.
4. Records the engaged domain in `.helix-state.json`.
5. Writes the new gene at the post-migration path.
6. Releases the lock.

Eager (not lazy) so wikilinks never break in a transitional window. Bounded latency hit on the cycle that crosses the threshold; only fires once per threshold crossing per domain.

### Performance budgets

- Render+write per gene: <2ms target
- Full export at 3683 genes: <10s; at 100K genes: <5min (cursor-streamed, not memory-loaded)
- Incremental export: typically <100 changed genes, <500ms target
- Trace export: <50ms (off the /context hot path; written async)

The export holds the genome's read connection only for individual statements (PR #32's `isolation_level=None` reader). Never holds a long-lived transaction.

---

## Lifecycle: TTL pruning + rollup + `_stale/` refresh

### Configuration (`helix.toml`)

```toml
[vault]
enabled = true
path = "~/.helix/vault"
party_id = ""                     # per I-7; empty = use server's primary party
fan_out_threshold = 5000
redact_body = false               # see I-1; recommended true for cloud-synced setups
stale_threshold = 0.5             # _stale/ population threshold (live_truth_score)

[vault.traces]
enabled = true                    # auto-export every /context call
retention_hours = 48              # default test value
max_retention_hours_hard = 720    # 30 days; force-deletes pinned past this; null disables
max_count = 10000                 # safety cap on burst floods
rollup_enabled = true
rollup_shard = "hour"             # daily | hour
prune_interval_minutes = 60
trigger_only = false              # if true, only export on threshold (latency, sparse health)
```

### Pruner loop

1. Sleep `prune_interval_minutes`.
2. Walk `_traces/` (NOT `_traces-pinned/`).
3. For each filename, parse `_exp<unix>` suffix. If `unix < now()` → mark for prune. **No frontmatter parse for unexpired files.**
4. For pinned files (those without `_exp` because pinning strips the suffix), check `vault.traces.max_retention_hours_hard`. If file's mtime + max_retention_hours_hard < now() → mark for force-prune with a loud structured log event.
5. Before deleting marked files, append a one-line summary to `_meta/trace-rollups/<date>/<hour>.md`.
6. Delete marked files.
7. Refresh `_stale/`: query the genome for genes where `live_truth_score < vault.stale_threshold` and `chromatin = 'euchromatin'`. Add new entries (symlinks on POSIX, pointer notes on Windows). Remove entries whose live_truth_score has recovered.

### `_stale/` lifecycle

Populated by the pruner (and by every full export), not by any watcher. POSIX uses symlinks to the canonical path under `genes/<domain>/...`; Windows non-admin uses a pointer note containing `[[gene-...]]` and a one-line "stale since {date}" header.

`_stale/` is a read-only operator view. v1 has no watcher, so even if an operator edits a Windows pointer note, nothing happens. Operators who want the canonical path follow the wikilink.

### Pin / unpin

- `POST /vault/traces/{id}/pin` (or `helix-vault pin <id>`): renames the file from `<ts>_<id>_exp<exp>.md` to `<ts>_<id>.md` and moves to `_traces-pinned/`. Strips the `_exp` suffix so subsequent prune cycles ignore it (subject to `max_retention_hours_hard`).
- `POST /vault/traces/{id}/unpin`: moves back to `_traces/` and adds a fresh `_exp<unix>` suffix (TTL resets).
- Operator can also drag the file from `_traces/` to `_traces-pinned/` in Obsidian — but in v1 there's no watcher to detect the move. The operator should call `helix-vault pin` for the rename-with-suffix-strip behavior, or accept that drag-pinned files keep their `_exp` suffix in the filename (cosmetic; pruner already skips the folder).

---

## Telemetry

New OTel histograms (using PR #36's pattern):
- `helix_vault_export_seconds{kind="full|incremental|trace"}`
- `helix_vault_pruner_seconds`

New gauges:
- `helix_vault_file_count{folder="genes|traces|traces_pinned|stale"}`
- `helix_vault_disk_bytes`

New counters:
- `helix_vault_force_prune_total` — incremented when `max_retention_hours_hard` overrides a pin

Structured log events:
- `vault_force_prune` — `{request_id, mtime, max_retention_hours_hard}`
- `vault_export_partial` — emitted when an export completes with skipped genes (logs gene_ids)
- `vault_disabled` — vault failed to start; retrieval continues

(v1.1 adds: `helix_vault_inbound_validation_seconds`, `helix_vault_watcher_state`, `helix_vault_edit_rejections_total`, `helix_vault_shadow_drift_total`, `vault_watcher_crash`, `vault_watcher_circuit_broken`, `vault_watcher_recovered`.)

---

## Failure modes & error handling

| Failure | Detection | Response |
|---|---|---|
| Vault path missing/unwritable | startup check | Log error, disable vault entirely; retrieval continues (per I-1) |
| Stale `.helix-syncing` after crash | startup check | Unconditional cleanup at `vault.start()` |
| Disk full during export | `OSError` on write | Log, mark export as partial (`vault_export_partial` event), retry next cycle |
| Symlink unsupported (Windows non-admin) | `os.symlink` raises | Fall back to pointer-note format for `_stale/` |
| Concurrent CLI + server | server-only writes; CLI talks via HTTP | No conflict possible by design |
| Pruner sees corrupt trace filename | regex doesn't match `_exp<n>` | Use mtime + 24h fallback; if mtime older than 30d, prune anyway |
| Schema version mismatch | startup check | Refuse-to-start; operator runs `helix-vault migrate` |

(v1.1 adds: watcher crash recovery, validator rejection, shadow drift, cross-party edit attempts, supersedes target validation, body-edit rejection.)

---

## Testing strategy

### Unit tests
- `schema.py` — frontmatter shape, authored-field placeholder generation
- `writer.py` — `render_gene_markdown` / `render_trace_markdown` produce expected YAML+body; path traversal sanitization
- `pruner.py` — TTL math via filename suffix; rollup append (hour-sharded); pinned skip; `max_retention_hours_hard` force-prune; `_stale/` add/remove

### Integration tests
- Round-trip: write gene → export → parse vault file → assert frontmatter matches (computed fields populated, authored fields are placeholders)
- Trace export: `/context` call → assert trace file present in `_traces/` with correct `_exp<n>` suffix
- TTL prune via filename: write trace with `_exp<past>` → prune deletes + rollup line in correct hour shard
- `max_retention_hours_hard`: pinned trace with old mtime → force-pruned + structured log event
- Pin/unpin round-trip: pin → assert in `_traces-pinned/` with stripped suffix; unpin → assert back in `_traces/` with fresh suffix
- `_stale/` population: gene with `live_truth_score < threshold` → assert symlink/pointer-note present; recovery → entry removed
- Path traversal: gene with `source_id: "../../etc/passwd"` (test fixture) → write blocked, gene lands in `_orphan/`
- Folder fan-out: synthetically populate a domain past threshold → assert all files migrated atomically; wikilinks stable
- Single-party scope (I-7): two parties' genes in genome → vault export only includes operator's party
- File permissions: assert `~/.helix/vault/` and subdirs created at `mode=0o700`

### Live (live-marked, requires running server)
- 60s soak with sustained `/context` traffic; assert pruner keeps `_traces/` bounded; no genome corruption; vault and retrieval both functioning

---

## v1.1 work (deferred)

The following are explicitly out of v1 scope. Their full design lives in [`2026-05-06-obsidian-vault-export-full-design-v1.1plus.md`](./2026-05-06-obsidian-vault-export-full-design-v1.1plus.md). v1.1 work should start from that design (refreshed against v1's implementation choices), not from scratch.

- **Watcher** (`watcher.py`) — observe filesystem events
- **Validator** (`validator.py`) — patch whitelist + supersedes target check + per-gene in-flight gate
- **Shadow store** (additional column on `vault_state`) — last-known authored values for diff
- **Inbox ingest** (`inbox.py`) — `_inbox/*.md` → `genome.upsert_gene`
- **`_unresolved/` resolution loop** — explicit-only matching via frontmatter `resolves: ['[[...]]']`
- **`_meta/edit-rejections.md`** — operator-visible signal for rejected edits
- **Retrieval contract changes** — authorship surfacing in `/context/packet`, quarantine_reason enforcement across all read paths, pinning vs. live_truth_score handling
- **Circuit breaker for the watcher** — auto-probe + manual reset endpoint

The full design's invariants I-2, I-3, I-5 re-enter scope when these features land.

## Out of scope (permanent, for clarity)

- Multi-vault per helix instance
- Cross-helix-instance vault sharing/federation
- Custom Obsidian plugin
- Bidirectional content sync (only authored frontmatter fields, even in v1.1)
- Real-time / sub-second propagation
- Per-trace ACLs (single-user-laptop threat model; multi-tenant deployments use `trigger_only = true` and accept the disclosure surface documented in the full design)

---

*v1 spec produced 2026-05-06 by scope-cutting from the full v3.5 design after a 3-reviewer final pass with opposing leads. The anti-ship reviewer's case (cut to read-only, observe operator usage before committing to bidirectional sync) was the operator's chosen path.*
