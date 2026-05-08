# Obsidian Vault Export — Full Design (v1.1+, deferred)

> **STATUS: STUBBED FOR FUTURE WORK.** v1 ships read-only export + diagnostic traces only — see [`2026-05-06-obsidian-vault-export-design.md`](./2026-05-06-obsidian-vault-export-design.md).
>
> The full design preserved below — including authored delta-sync, watcher/validator/shadow store, inbox ingest, `_unresolved/` resolution, and edit-rejection logging — is the **v1.1+ target**. It is not being implemented in v1; the anti-ship reviewer's case (cut scope to read-only, observe operator usage before committing to bidirectional sync) was accepted by the operator on 2026-05-06.
>
> This document is preserved as the design-of-record for the deferred features. v1.1 work should start from this design (refreshed to reflect any v1 implementation changes), not from a clean sheet.

---

# Obsidian Vault Export — Design Spec (v3.5, deferred to v1.1+)

**Date:** 2026-05-06
**Status:** Stubbed for v1.1+ (post multi-agent review + 3-reviewer final pass with opposing leads)
**Related:**
- Discussion #34 (this design's origin)
- Discussion #33 (typed-edges proposal — feeds the schema)
- PR #32 (WAL bloat fix — assumed merged)
- PR #36 (per-stage telemetry — feeds `_traces/`)

---

## Goals

1. **Browsable corpus** — operator navigates the genome as an Obsidian vault: search, graph view, Dataview queries, backlinks. No helix internals required.
2. **Diagnostic console** — every `/context` call exports a trace markdown showing fingerprint route + per-stage timing + foveated rank assignments.
3. **Curation surface** — operator edits a small set of authored frontmatter fields and changes flow back into helix via authored delta-sync.
4. **Inbox ingest** — operator drops new `.md` files into `_inbox/`; helix ingests them as new genes attributed to the operator's session.

## Non-goals (v1)

- Bidirectional sync of *computed* fields. Computed = helix-authoritative, read-only in vault.
- Editing gene *content* via Obsidian. The vault never accepts body changes; gene_id is the content hash.
- Real-time / sub-second propagation. Eventual consistency on the order of seconds.
- Multi-vault federation; one vault per helix instance.
- Cross-agent trace isolation. All agents see all `_traces/`. Multi-tenant deployments must run with `traces.trigger_only = true` and accept the disclosure surface.
- Obsidian plugin. Plain markdown + frontmatter only.
- Conflict resolution beyond LWW on the small whitelisted authored field set.

---

## Invariants (must hold across all subsystems)

These are the load-bearing properties. Every component below is constrained by them.

**I-1: Vault failures never degrade retrieval.** If the vault writer crashes, the watcher loops, the pruner deadlocks, or the disk fills — the genome stays correct, `/context` keeps serving, no agent-visible state is corrupted. The vault is a downstream consumer, not a critical path.

**I-2: Pinning does NOT override `live_truth_score`.** A pinned gene with a decayed `live_truth_score` still appears in the `stale_risk` bucket of `/context/packet`, just like any other stale gene. Pinning protects against auto-demotion (chromatin tier drop) and quarantine-by-policy. It does NOT vouch for freshness. Agents must still verify pinned-but-stale genes against their refresh_targets.

**I-3: Authorship surfaces to the retrieval response.** `/context/packet` response items include `attribution.participant_handle` (existing field on `Gene`) so agents can distinguish operator-authored ground truth from agent-replicated speculation. This is a small change in the response projection, not the storage layer.

**I-4: All vault writes are atomic from the watcher's POV.** Operator-visible files are either fully helix-written or fully operator-written, never a mix. Achieved via vault-root lockfile + per-write tmp+rename.

**I-5: Computed fields are read-only.** The validator rejects any operator edit touching them and rolls back from the genome. Operator-visible signal is required; silent revert is forbidden.

**I-6: Vault rendered content is recoverable from `genome.db` alone; vault-side operator artifacts require backing up the vault folder.** Authored fields persist in `gene_attribution.notes` (in `genome.db`); from those, every gene markdown file in `genes/` can be re-rendered. The vault folder additionally contains: `vault.db` (sibling state — last_exported_disk_hash, vault_path mapping, shadow_authored cache; rebuildable from genome on first export), pinned traces (`_traces-pinned/` — vault-only), in-flight inbox files (`_inbox/` — vault-only), and operator-authored typed-edge wikilinks not yet validated. The README documents this asymmetry and recommends backup of the **whole vault folder** for operators who depend on the vault-only artifacts.

**I-7: Vault is single-party scoped.** A vault represents exactly one `party_id`'s view of the genome — the value of `vault.party_id` in `helix.toml` (defaults to the server's primary party). The exporter only emits genes whose `party_id` matches. The validator only accepts edits to genes whose `party_id` matches. The inbox forces ingested genes' `party_id` to the vault's party. Multi-party deployments either run separate helix instances per party (each with its own vault) or run with the vault disabled. This eliminates cross-party effects through vault edits (a quarantine_reason set in party A's vault cannot affect party B's retrieval, because B's genes are not in A's vault and validator rejects edits to genes outside the configured party).

---

## Architecture

**Single-process embedded.** All vault concerns run inside the existing helix server, mirroring `replication.py`'s lifecycle.

```
helix-context (FastAPI server)
│
├── lifespan startup
│   ├── existing: genome, ribosome, replication, telemetry
│   └── NEW: vault.start() — clear stale sentinels, init shadow store, wire threads
│
├── helix_context/vault/   (NEW package)
│   ├── __init__.py        — VaultManager (public API)
│   ├── writer.py          — gene → markdown (snapshot + incremental + trace)
│   ├── watcher.py         — filesystem watcher (watchdog lib)
│   ├── validator.py       — patch whitelist + supersedes target check
│   ├── pruner.py          — TTL prune + pre-prune rollup + reconciliation
│   ├── schema.py          — frontmatter shape, computed/authored classifier
│   ├── inbox.py           — _inbox/*.md → upsert_gene
│   ├── shadow.py          — shadow store (SQLite-backed; see §State Tracking)
│   └── locking.py         — vault-root filelock for CLI/server coordination
│
├── HTTP additions
│   ├── POST /export/obsidian              — trigger snapshot export
│   ├── POST /vault/sync                   — force inbound sync of pending edits
│   ├── GET  /vault/status                 — last export, watch state, pruner state
│   ├── POST /vault/trace                  — write trace for a recent request_id
│   ├── POST /vault/traces/{id}/pin        — move trace to _traces-pinned/
│   ├── POST /vault/traces/{id}/unpin      — move back to _traces/ (resets TTL)
│   └── POST /vault/watcher/reset          — clear circuit-breaker, reattempt
│
└── CLI: new `helix-vault` script
    ├── helix-vault {export,sync,watch,trace,status}
    ├── helix-vault {pin,unpin} <request_id>
    ├── helix-vault reset-watcher
    └── helix-vault prune [--dry-run]
```

The CLI talks to the running server over HTTP (no shared SQLite handle). When the server isn't running, the CLI exits with a clear error rather than racing on the file. This avoids the concurrent-CLI-vs-server file-write conflicts entirely.

**Threading model:** `VaultManager` owns three daemon threads — writer, watcher, pruner. They communicate through an in-memory event bus and the genome's existing read connection. A circuit breaker disables the watcher if it crashes 3x in 5min and emits a structured log event. The exporter and pruner use simple try/except + sleep loops with exponential backoff on failure.

---

## Frontmatter schema

The frontmatter is the load-bearing surface — Dataview queries it, the validator enforces it, the writer renders it.

### Computed (read-only in vault)

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
co_activation_partners: 7        # count, not list
party_id: swift_wing21
participant_handle: laude
```

### Authored (delta-synced via watcher → validator)

```yaml
operator_notes: |
  Free-form prose; flows back as gene_attribution.notes.operator_notes.
operator_tags: [reviewed, high-priority]
pinned: false                    # see I-2: does NOT override live_truth_score
quarantine_reason: null          # if non-null → excluded from /context (see Retrieval Contract)

# Typed edges (Discussion #33)
supersedes: ['[[gene-old456]]']  # validator requires target gene_id to exist
contradicts: []
implements: ['[[spec-auth-flow]]']
documented_by: []
tests: []
```

### Render-only (markdown body sections)

- `## Typed edges` — sparse wikilinks (the 5 typed-edge fields above as `[[wikilinks]]`). Co-activation and harmonic_links stay in frontmatter as data, NOT as wikilinks (hairball avoidance).
- `## Backlinks` — Obsidian populates from any `[[gene-abc123def456]]` reference elsewhere in the vault.
- `## Last retrieval` (if available) — 12-signal tier scores from the most recent `/context` call. Body section, not frontmatter, because the data is too volatile for stable Dataview queries.

### Validator rules — `helix_context/vault/schema.py`

```python
COMPUTED_FIELDS = frozenset([
    "gene_id", "chromatin", "domains", "content_type",
    "source_id", "source_lines", "content_sha256",
    "last_seen", "last_seen_ts", "live_truth_score",
    "co_activation_partners", "party_id", "participant_handle",
])

AUTHORED_FIELDS = frozenset([
    "operator_notes", "operator_tags", "pinned", "quarantine_reason",
    "supersedes", "contradicts", "implements", "documented_by", "tests",
])
```

Validator parses the diff between on-disk frontmatter and the shadow store. Per-key:

| Key class | Action |
|---|---|
| In `COMPUTED_FIELDS` | Reject. Roll back from genome. Append rejection record to `_meta/edit-rejections.md` so operator has visible signal (per I-5). |
| In `AUTHORED_FIELDS` | Validate value (see below). If valid: accept; queue update to `gene_attribution.notes`. If invalid: reject, append to `_meta/edit-rejections.md`. |
| Unknown key | Log warning; ignore. |

**Authored field validation:**
- `pinned`: must be `bool`.
- `quarantine_reason`: must be `null` or `str`. If `str`, length ≤ 500 chars.
- `operator_tags`: list of strings, each matching `^[a-zA-Z0-9_/-]+$` (no embedded YAML).
- `operator_notes`: string ≤ 10_000 chars; reject if first non-whitespace char is YAML structural (`-`, `?`, `:`, `[`, `{`, `&`, `*`, `!`, `|`, `>`, `'`, `"`, `%`, `@`, `` ` ``).
- `supersedes` / `contradicts` / `implements` / `documented_by` / `tests`: list of `[[wikilink]]` strings. For each wikilink, extract the embedded gene_id and verify it exists in the genome **AND has the same `party_id` as the vault** (per I-7). Missing or cross-party targets → reject with reason class `missing_supersedes_target`. The rejection record names only the field, not the queried gene_id, so it cannot be used as an enumeration oracle for genes in other parties.

**Body edit detection:** the validator computes `content_sha256(disk_body)` on every save. If it diverges from the frontmatter `content_sha256`, log "operator edited body of gene X" and roll back. Body changes are always rejected (gene_id is the content hash; a body change is a different gene).

---

## Vault layout

```
~/.helix/vault/                         # path configurable via helix.toml
                                        # created with mode=0o700; checked at startup
├── README.md                           # generated; explains layout, last export ts,
│                                       # backup notes (per I-6)
├── genes/
│   ├── auth/                           # by primary domain
│   │   ├── middleware-7f3a1c.md
│   │   └── jwt-verify-9b2e44.md
│   ├── core/
│   │   ├── ab/                         # 2-level fan-out engaged when domain
│   │   │   └── helix-context-ab12cd.md # exceeds vault.fan_out_threshold (default 5000)
│   │   └── cd/
│   └── _orphan/                        # genes with no domain
│
├── _traces/                            # every-call diagnostic exports
│   └── 2026-05-06T22-14-06_abc12345_exp1736371846.md
│       # filename includes expires_at as unix epoch — pruner filters by name
│
├── _traces-pinned/                     # pruner skips this folder; subject to
│   └── ...                             # vault.traces.max_retention_hours_hard
│
├── _inbox/                             # operator drops new notes; helix ingests
│   └── auth-rate-limit.md
│   └── _failed/                        # parse/ingest failures; pruned on cycle
│       └── bad-frontmatter.md
│       └── bad-frontmatter.error.txt
│
├── _unresolved/                        # agent-flagged knowledge gaps
│   └── rate-limiting-policy.md         # see Inbound flow §_unresolved/ matching
│
├── _stale/                             # genes with live_truth_score < threshold
│   └── ...                             # symlinks (POSIX) or pointer notes (Windows)
│
├── _sessions/
│   └── laude.md                        # per-participant activity log
│
├── _meta/
│   ├── trace-rollups/
│   │   └── 2026-05-06/
│   │       ├── 14.md                   # hour-sharded; each file ≤ 3600 rows
│   │       └── 15.md
│   ├── edit-rejections.md              # validator rejections (operator-visible signal)
│   ├── co-activation-clusters.md
│   ├── chromatin-tier-counts.md
│   ├── party-id-attribution.md
│   └── 12-signal-stats.md
│
└── .helix-syncing                      # sentinel during writes (cleared at startup)
```

**Folder fan-out:** `vault.fan_out_threshold` (default 5000) caps per-folder file count. Migration is **eager** — when an incremental export would push a domain folder past the threshold, the writer:

1. Acquires the vault-root lock for the duration of the migration.
2. Migrates **every** existing file in that domain from `<domain>/<stem>-<id>.md` to `<domain>/<first2chars>/<stem>-<id>.md` in a single batch via `os.replace()` per file.
3. Updates `vault_state.vault_path` for every affected gene_id.
4. Records `fan_out_engaged_domains` in `.helix-state.json`.
5. Writes the new gene at the post-migration path.
6. Releases the lock.

This avoids the "intermediate state" where some genes are at flat paths and others at fan-out paths during the transition window — wikilinks would break for any gene linking by short-id stem during that window. Eager migration imposes a latency hit on the cycle that crosses the threshold (proportional to the domain's file count), but bounded and only fires once per threshold crossing per domain.

**Path safety:** the writer constructs paths as `vault_root / domain / f"{stem}-{short_id}.md"`. Before any write, the result is `Path(...).resolve()` and asserted to start with `vault_root.resolve()`. Any source_id producing a path outside the vault is logged and the gene is written to `_orphan/` with the source_id sanitized to its filename basename.

**File permissions:** vault root + all subdirectories created with `mode=0o700`. Startup checks existing permissions and warns if they're broader. `_traces/` is identical permissions; nothing about traces gets stricter perms (would require special-case logic that doesn't justify itself for a single-user-laptop threat model).

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
  │   release lock; update shadow store
  │
  ├── incremental_export()               [watch mode + post-ingest hook]
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
    fm = build_frontmatter(gene, ctx)
    body = build_body(gene, ctx)
    return f"---\n{yaml.safe_dump(fm)}---\n\n{body}"
```

`build_frontmatter` reads from `genes`, `gene_attribution`, `epigenetics`, `harmonic_links`, `co_activation` tables. Authored fields live in `gene_attribution.notes` JSON column under explicit keys (`operator_notes`, `operator_tags`, `pinned`, `quarantine_reason`, `supersedes`, etc.). No new genome schema migration is required for v1 — the existing `notes` JSON adopts a documented convention.

### Atomic writes (per I-4)

Every file write goes through `write_atomic(path, content)`:

1. Acquire vault-root lock (`vault.lock`, via `filelock` library).
2. Write to `path.tmp`.
3. Touch `.helix-syncing` sentinel in vault root.
4. `os.replace(path.tmp, path)` — atomic on POSIX and Windows.
5. Remove `.helix-syncing`.
6. Release vault-root lock.

The lock + sentinel together prevent both:
- Two writers racing on the sentinel itself.
- The watcher observing intermediate states.

**Stale sentinel cleanup:** `vault.start()` removes any pre-existing `.helix-syncing` file unconditionally. If a previous process crashed mid-write, the sentinel is stale; clearing it is safe because nothing else is running yet.

**Body redaction (optional):** `vault.redact_body = true` (default `false`) replaces gene body content with `{ "redacted": true, "sha256": "...", "byte_count": N }` and a one-line excerpt of the first whitespace-separated tokens. Recommended for any vault that might be observed by `Obsidian Sync`, `iCloud Drive`, `Dropbox`, etc. The README warns about this prominently.

### State tracking — sibling `vault.db`

The original draft used a `~/.helix/vault/.helix-state.json` file with an unbounded `known_gene_files` map. At >50K genes the JSON parse alone exceeds the export budget. v1 stores this state in a **sibling SQLite file** at the vault root, NOT in `genome.db`:

```
~/.helix/vault/
├── vault.db              # vault-lifecycle state; deleted with the vault
├── .helix-state.json     # small top-level state (~100 bytes)
└── ...
```

```sql
-- vault.db schema
CREATE TABLE IF NOT EXISTS vault_state (
  gene_id                  TEXT PRIMARY KEY,
  vault_path               TEXT NOT NULL,   -- relative to vault root
  last_exported_ts         REAL NOT NULL,
  last_exported_disk_hash  TEXT,            -- sha256 of full file content; self-event sentinel
  shadow_authored          TEXT             -- JSON of last-known authored field values
);

CREATE INDEX IF NOT EXISTS idx_vault_state_path ON vault_state(vault_path);

-- separate addition to genome.db (NOT in vault.db)
CREATE INDEX IF NOT EXISTS idx_genes_last_seen ON genes(last_seen);  -- for incremental
```

**Why a sibling, not a genome.db table:**

- **Lifecycle separation.** The vault is a derived view of the genome. Coupling vault state to `genome.db` means deleting/rebuilding the vault touches the canonical store. With `vault.db` as a sibling, `rm -rf ~/.helix/vault/` cleanly drops all vault state without touching `genome.db`.
- **Backup/restore symmetry.** Backing up the vault folder now captures everything needed to restore the vault — `vault.db`, pinned traces, inbox files. Restoring on a different machine doesn't require unpacking vault-coupled tables out of `genome.db`.
- **Path migration.** Moving the vault to a new disk or directory is a folder move; `vault.db` travels with it. If state lived in `genome.db`, vault paths would become stale on every move.
- **Cascade deletes via genome FK** are not lost — the pruner's reconciliation sweep already detects "gene exists in `vault_state` but not in genome" and cleans up. The FK was a convenience, not a correctness mechanism.

The shadow store is `vault.db: vault_state.shadow_authored`. Reading/writing per-gene is O(1) via the primary key. WAL mode is enabled on `vault.db` for the same reasons it's enabled on `genome.db`; the journal_size_limit + isolation_level=None patterns from PR #32 apply identically.

**Top-level vault state** (last full/incremental export ts, schema version) lives in a small JSON `~/.helix/vault/.helix-state.json` — bounded size, ~100 bytes:

```json
{
  "schema_version": 1,
  "last_full_export_ts": 1736198700.0,
  "last_incremental_export_ts": 1736198820.0,
  "exported_gene_count": 3683,
  "fan_out_engaged_domains": ["core"]
}
```

**Schema migration:** if `schema_version` doesn't match the current code, the vault enters refuse-to-start mode and logs a structured event. Operator runs `helix-vault migrate` to upgrade. v1 ships with no migrations defined; future releases add migration steps explicitly.

### Trace markdown shape

Filename: `<iso8601>_<request_id>_exp<expires_unix>.md`. The pruner uses the `_exp<n>` suffix to filter expired traces without parsing frontmatter (see Lifecycle).

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

### Performance budgets

- Render+write per gene: <2ms target
- Full export at 3683 genes: <10s; at 100K genes: <5min (budget-limited; uses cursor over `genes` table, doesn't load all at once)
- Incremental export: typically <100 changed genes, <500ms target
- Trace export: <50ms (synchronous to /context call but off the hot path)

The export holds the genome's read connection only for individual statements (PR #32's `isolation_level=None` reader). It never holds a long-lived transaction.

---

## Inbound data flow (watcher → validator → genome)

```
filesystem event (operator saved a file)
  │
  ├── if .helix-syncing exists in vault root: defer event 100ms, retry
  ├── if path starts with `.` or matches sync sentinel: ignore
  ├── if path is in _traces-pinned/: ignore
  ├── if path is in _stale/: ignore (read-only operator view; see _stale/ Lifecycle)
  ├── if path is in _meta/: ignore (rollups/aggregations are helix-owned, read-only)
  ├── if path is in _sessions/: ignore (helix-owned, read-only)
  ├── if path is in _unresolved/: see _unresolved/ Resolution above (resolves: only)
  ├── if path is in _inbox/ (but NOT _inbox/_failed/): → inbox.ingest_new_gene(path)
  ├── if path is a gene file (matches genes/**/*.md): → validator.process_edit(path)
  └── else: ignore
```

### Sentinel suppression — the actual mechanism

The watcher checks `.helix-syncing` *file existence* (not path matching the sentinel name). When the sentinel exists, events are deferred to a small queue and replayed 100ms after the sentinel is removed. This avoids both spurious validator runs during helix-side writes and dropping legitimate operator events that happened to overlap.

### `validator.process_edit(path)`

A per-gene in-flight set guards the function. If a gene_id is currently being processed, additional events for the same gene are coalesced (latest disk state wins; one validator run per gene at a time).

**Self-event suppression via content hash, not time window.** The original draft used a 1-second time window to suppress events caused by helix's own writes. Time windows are fragile under load (a slow incremental export can exceed the window; a paused process can fire an event after the window closes). Replaced with a content-hash sentinel:

- Before any rename, the writer computes `disk_hash = sha256(full_file_content)` (frontmatter + body, exactly the bytes about to land on disk).
- The writer records `vault_state.last_exported_disk_hash[gene_id] = disk_hash` in the same transaction as the exported gene's path/shadow update.
- When the watcher fires for a gene file, the validator computes `sha256(current_file_content)` and compares against `last_exported_disk_hash`.
- Match → self-event from a recent helix write; skip without further processing.
- Mismatch → operator edit; proceed with the validator pipeline below.

This is robust to slow exports, burst events, and process pauses. It does require one extra column on `vault_state` (`last_exported_disk_hash TEXT`).

1. Parse frontmatter from on-disk file. If parse fails → log + skip; operator sees the parse error in their editor.
2. **Party-ownership check (per I-7):** look up the gene's current `party_id` in the genome. If it doesn't match `vault.party_id`, reject the edit with reason `cross_party_edit_attempt`, append to `edit-rejections.md`, and roll back from genome state. This is the primary defense against cross-party effects via vault edits.
3. Read shadow_authored from `vault_state` table for this gene_id.
4. Compute diff between disk frontmatter and shadow_authored.
5. **Reconciliation guard:** also compare disk frontmatter's computed fields against current genome state. If they diverge (i.e., genome was updated since last export but vault wasn't refreshed yet), trigger an incremental export of this gene FIRST, update shadow, then retry the diff. The in-flight set above prevents the guard's export from triggering recursive validation. Prevents silent reverts when shadow is stale.
6. For each changed key:
   - In `COMPUTED_FIELDS` → reject + append to `_meta/edit-rejections.md` (per I-5) + roll back.
   - In `AUTHORED_FIELDS` → run validation (see Validator Rules above). If valid: accept. If invalid: reject + append to `edit-rejections.md`.
   - Unknown → log + ignore.
7. Verify `content_sha256(disk_body)` matches frontmatter `content_sha256`. Mismatch → reject body change + append to `edit-rejections.md` + roll back.
8. If any accepted edits, batch into a single `genome.update_attribution_notes(gene_id, notes_dict)` call.
9. Update shadow_authored in `vault_state`.
10. Remove gene_id from in-flight set.

### `_meta/edit-rejections.md` — operator-visible signal (per I-5)

Append-only markdown file. Each rejection records the field name and reason class only — **no field values, attempted values, or current genome values**. This prevents the rejection log from becoming a side channel that leaks operator intent or current genome state to anything observing the vault (cloud-sync, other local processes, downstream agents reading the vault).

```markdown
## 2026-05-06T22:14:06Z — gene auth/middleware-7f3a1c.md

**Reason:** computed_field — helix is authoritative
**Rejected fields:** chromatin
**Action:** Reverted from genome state. To prevent auto-demotion, edit `pinned: true` instead.
```

Reason classes (closed set): `computed_field`, `invalid_value`, `body_edit`, `missing_supersedes_target`, `cross_party_edit_attempt`, `yaml_structural_in_authored_value`. Rotated daily; old days move to `_meta/edit-rejections-archive/<date>.md`. The active file is the last 24h. Operator sees their rejected edits in Obsidian; no surprises and no value disclosure.

### `inbox.ingest_new_gene(path)`

1. Read file: parse frontmatter (if any) + body.
2. **Strip server-controlled fields** from frontmatter: `gene_id`, `party_id`, `participant_handle`, `chromatin`, `last_seen`, `live_truth_score`, all other `COMPUTED_FIELDS`. These are forced from server state.
3. Build a synthetic Gene with:
   - `content` = body
   - `content_type` = inferred from filename (`.md` → "doc")
   - `source_id` = `_inbox/<filename>` (operator-attributed)
   - `domains` = from frontmatter `domains` field or `[]`
   - `party_id` = the operator's current party (from server state, not from file)
   - `participant_handle` = "operator" (or operator-configured handle, from server state)
   - Authored fields from frontmatter (validated as above)
4. `genome.upsert_gene(synthetic_gene)`.
5. After successful ingest, move the inbox file to `genes/<domain>/<gene_id>.md`.
6. If ingest fails, move to `_inbox/_failed/<filename>` with `<filename>.error.txt` sibling. Pruned on the same cycle as `_traces/`.

### `_unresolved/` matching — explicit only, no heuristics

The original draft promised "auto-removed when matching gene appears" without defining "matching." That's a footgun for agent reasoning. v1 makes matching **explicit only**:

- An `_unresolved/` file resolves only when an operator-authored or agent-authored gene includes a frontmatter `resolves: ['[[unresolved-rate-limiting-policy]]']` reference to it.
- Helix can SUGGEST matches via fuzzy domain+content match, but suggestions are written as a comment in the unresolved file (`<!-- helix suggests: gene-abc123 -->`); they are NEVER auto-applied.
- The operator (or an agent with explicit `resolves:`) is the only actor that can close an unresolved expectation.

This trades convenience for safety: the loop closes only when intent is clear. Agents reading `_unresolved/` see only operator-confirmed answers, not heuristic ones.

### Watcher event loss recovery

`watchdog` drops events under burst load or OS queue overflow. Defense: the pruner cycle (default 60min) runs a reconciliation sweep:

1. List all gene files on disk.
2. For each, look up shadow store; if disk frontmatter diverges from shadow + genome agrees, validate as if it were a fresh save.
3. List all gene_ids in `vault_state`; for each, verify the file exists. Missing → reset shadow state for next export.
4. Walk `_inbox/`; for any file present, queue an ingest attempt.

This catches missed events within one prune cycle.

### Circuit breaker

Watcher maintains a ring buffer of crash timestamps. On 3 crashes within 5 minutes:

1. Watcher exits cleanly.
2. Structured log event: `{event: "vault_watcher_circuit_broken", crash_count: 3, last_reason: str(exc), reconciliation_will_continue: true}`.
3. `helix_vault_watcher_state` gauge → 2 (circuit-broken).
4. **Auto-probe:** every `vault.watcher.probe_interval_minutes` (default 15), attempt to restart watcher. If it stays alive 60s, declare recovered; if it crashes again, increment the buffer.
5. **Manual reset:** `POST /vault/watcher/reset` clears the ring buffer immediately.

The reconciliation pass continues to run even when the watcher is circuit-broken, so on-disk drift is still recoverable.

---

## Lifecycle: TTL pruning + rollup + reconciliation

Configured via `helix.toml`:

```toml
[vault]
enabled = true
path = "~/.helix/vault"
party_id = ""                     # per I-7; empty = use server's primary party_id
watch_enabled = false             # opt-in for D-capable mode
inbox_enabled = true
fan_out_threshold = 5000          # split domain folders above this count
redact_body = false               # see I-1; recommended true for cloud-synced setups

[vault.traces]
enabled = true
retention_hours = 48              # default test value
max_retention_hours_hard = 720    # 30 days; force-deletes pinned past this
                                  # set null to disable hard cap
max_count = 10000                 # safety cap on burst floods
rollup_enabled = true
rollup_shard = "hour"             # daily | hour ; hour caps row count per file
prune_interval_minutes = 60
trigger_only = false              # if true, only export on threshold

[vault.watcher]
probe_interval_minutes = 15       # circuit-breaker auto-probe cadence
```

### Pruner loop

1. Sleep `prune_interval_minutes`.
2. Walk `_traces/` (NOT `_traces-pinned/`).
3. For each filename, parse `_exp<unix>` suffix. If `unix < now()` → mark for prune. **No frontmatter parse for unexpired files.**
4. For pinned files (those without `_exp` because pinning strips the suffix), check `vault.traces.max_retention_hours_hard`. If file's mtime + max_retention_hours_hard < now() → mark for force-prune with a loud structured log event.
5. Before deleting marked files, append a one-line summary to `_meta/trace-rollups/<date>/<hour>.md`.
6. Delete marked files.
7. Walk `_inbox/_failed/` and `_meta/edit-rejections-archive/` with the same retention policy.
8. Run watcher event-loss reconciliation sweep (see Inbound flow above).

### `_stale/` population and lifecycle

`_stale/` is a synthetic operator view, populated by the exporter on every full or incremental export — not by the watcher. Lifecycle:

1. After an export cycle, the writer queries the genome for genes where `live_truth_score < vault.stale_threshold` (default `0.5`) and `chromatin = 'euchromatin'` (heterochromatin is already excluded from retrieval; flagging it as stale is noise).
2. For each match, the writer creates an entry in `_stale/<source_stem>-<short_id>.md`:
   - **POSIX:** symlink to the canonical path under `genes/<domain>/...`
   - **Windows non-admin:** a pointer note containing `[[gene-...]]` and a one-line "stale since {date}" header
3. Entries are removed when the gene's `live_truth_score` recovers above the threshold (next export cycle).
4. The watcher explicitly excludes `_stale/` from observed paths. Operator edits to `_stale/` pointer notes (Windows fallback case) are not validated and not synced. Operators who want to edit a stale gene's authored fields should follow the `[[wikilink]]` to the canonical `genes/<domain>/...` file.

`_stale/` is purely read-side. Browsing it never affects retrieval state — co-activation counts and `last_seen` updates only fire on the agent retrieval path, not on filesystem reads.

### Pin / unpin mechanism

- `POST /vault/traces/{id}/pin` (or `helix-vault pin <id>`): renames file from `<ts>_<id>_exp<exp>.md` to `<ts>_<id>.md` and moves to `_traces-pinned/`. Strips the `_exp` suffix so subsequent prune cycles ignore it (subject to `max_retention_hours_hard`).
- `POST /vault/traces/{id}/unpin` (or `helix-vault unpin <id>`): moves file back to `_traces/` and adds a fresh `_exp<unix>` suffix (TTL resets from now).
- Operator-side alternative: drag the file from `_traces/` to `_traces-pinned/` in Obsidian. The watcher detects the move, strips `_exp` from the filename. Same outcome.

---

## Retrieval contract (changes outside the vault package)

These are the touch-points where the vault changes affect the agent-facing API. All small, intentional.

**`/context/packet` response — authorship surfacing (per I-3):**
Each item in `verified` and `stale_risk` includes `attribution: { participant_handle, party_id, content_authored_by_operator: bool }`. The `content_authored_by_operator` flag is `true` iff the gene was ingested via `_inbox/` (i.e., `gene_attribution.notes.source_path` starts with `_inbox/`). Agents can use this to weight operator-authored genes higher or treat them as ground truth.

**Quarantine enforcement — all read paths (per I-5 / quarantine_reason):**
Adds `AND COALESCE(json_extract(gene_attribution.notes, '$.quarantine_reason'), '') = ''` to the existing party-scoping clause in `query_genes`. To ensure quarantine is honored everywhere — not just the primary retrieval path — the same clause is added to:
- `query_genes` (primary `/context` retrieval)
- The co-activation expansion join (so quarantined genes don't elevate via graph-neighbor scoring)
- `/context/refresh-plan` (quarantined genes never become refresh targets)
- `/consolidate` (quarantined genes are not consolidated)
- The `_sema_cache` build queries (quarantined genes excluded from the hot ΣĒMA matrix)

Per I-7, quarantine_reason is set within the operator's own party only; the SQL clause already has party scoping, so a quarantine in one party's vault cannot affect another party's retrieval. Helix only checks for non-empty `quarantine_reason`; the value itself is opaque.

**`/context/packet` — pinned-but-stale handling (per I-2):**
The packet builder consults `live_truth_score` first, THEN considers `pinned` for de-duplication tiebreaks among genes that pass the freshness threshold. Pinning never elevates a stale gene into `verified`. A pinned gene with `live_truth_score` below the stale threshold appears in `stale_risk` like any other stale gene, with the pin reflected as `attribution.pinned: true` for agent awareness.

These three changes together preserve helix's freshness-aware retrieval guarantee while enabling the curation surface.

---

## Operator surface

### CLI

```
helix-vault export [--full] [--path PATH]
helix-vault sync                              # force inbound sync of pending edits
helix-vault watch                             # start watcher in foreground (debug only)
helix-vault trace [--last N | <request_id>]  # write trace files manually
helix-vault {pin,unpin} <request_id>
helix-vault status                            # last export, watcher state, disk usage
helix-vault prune [--dry-run]
helix-vault reset-watcher                     # clear circuit-breaker
helix-vault migrate                           # schema_version migration
```

CLI talks to the running server over HTTP. If the server isn't running, it exits with a clear error rather than racing on the file.

### HTTP endpoints

| Endpoint | Purpose |
|---|---|
| `POST /export/obsidian` | Trigger snapshot export (full or incremental) |
| `POST /vault/sync` | Force inbound sync of pending vault edits |
| `GET /vault/status` | Last export ts, watcher state, pruner state, file counts, disk usage |
| `POST /vault/trace` | Manually write a trace for a recent request_id |
| `POST /vault/traces/{id}/pin` | Move trace to `_traces-pinned/` |
| `POST /vault/traces/{id}/unpin` | Move back; reset TTL |
| `POST /vault/watcher/reset` | Clear circuit-breaker, reattempt watcher |

### Telemetry

New OTel histograms:
- `helix_vault_export_seconds{kind="full|incremental|trace"}`
- `helix_vault_inbound_validation_seconds`
- `helix_vault_pruner_seconds`
- `helix_vault_reconciliation_seconds`

New gauges:
- `helix_vault_file_count{folder="genes|traces|inbox|failed|..."}`
- `helix_vault_disk_bytes`
- `helix_vault_watcher_state` (0=disabled, 1=running, 2=circuit-broken)
- `helix_vault_inbox_failed_count`

New counters:
- `helix_vault_edit_rejections_total{reason="computed_field|invalid_value|body_edit|missing_supersedes_target|cross_party_edit_attempt|yaml_structural_in_authored_value"}`
- `helix_vault_shadow_drift_total` (incremented when reconciliation guard fires)
- `helix_vault_force_prune_total` (incremented when `max_retention_hours_hard` overrides a pin)

Structured log events (in addition to histograms):
- `vault_watcher_crash` — `{reason, stack_trace, crash_count, circuit_broken}`
- `vault_watcher_circuit_broken` — `{crash_count, last_reason}`
- `vault_watcher_recovered` — emitted by auto-probe when watcher restart stays alive 60s
- `vault_force_prune` — emitted when `max_retention_hours_hard` deletes a pinned trace
- `vault_export_partial` — emitted when an export completes with skipped genes (logs gene_ids)

---

## Failure modes & error handling

| Failure | Detection | Response |
|---|---|---|
| Watcher crashes | thread dies | Restart up to 3x in 5min; circuit-break with auto-probe + manual reset |
| Stale `.helix-syncing` after crash | startup check | Unconditional cleanup at `vault.start()` |
| Vault path missing/unwritable | startup check | Log error, disable vault entirely; retrieval continues (per I-1) |
| Disk full during export | `OSError` on write | Log, mark export as partial (`vault_export_partial` event), retry next cycle |
| Frontmatter parse error | YAML exception in validator | Log gene_id, skip that file, continue |
| Operator edits computed field | validator diff | Append to `edit-rejections.md`, roll back from genome |
| Operator edits gene body | content_sha256 diverges | Append to `edit-rejections.md`, roll back |
| Operator edits invalid value (regex/length/YAML structural) | validator predicate | Append to `edit-rejections.md`, roll back |
| Inbox file unparseable | parse failure | Move to `_inbox/_failed/<filename>` with `.error.txt` sibling; pruned on cycle |
| Inbox file collides with existing gene_id | post-hash check | Reject and move to `_failed/`; operator's content was a duplicate |
| Pruner sees corrupt trace filename | regex doesn't match `_exp<n>` | Use mtime + 24h fallback; if mtime older than 30d, prune anyway |
| Symlink unsupported (Windows non-admin) | `os.symlink` raises | Fall back to pointer-note format |
| Concurrent CLI + server | server-only writes; CLI talks via HTTP | No conflict possible by design |
| Watcher event loss (burst, queue overflow) | reconciliation sweep | Periodic compare of disk vs. shadow vs. genome; repair divergences |
| Schema version mismatch | startup check | Refuse-to-start; operator runs `helix-vault migrate` |
| supersedes target missing or cross-party | validator | Reject edit; append to `edit-rejections.md` (no value disclosure) |
| Cross-party edit attempt | validator step 2 | Reject; rollback; reason class `cross_party_edit_attempt` |
| Validator concurrent re-entry on same gene | per-gene in-flight set | Coalesce duplicate events; latest disk state wins; export-source events suppressed for 1s |

---

## Testing strategy

### Unit tests
- `schema.py` — field classification, validator predicates (regex, length, YAML structural)
- `writer.py` — render_gene_markdown / render_trace_markdown produce expected YAML+body; path traversal sanitization
- `pruner.py` — TTL math via filename suffix, rollup append (hour-sharded), pinned skip, `max_retention_hours_hard` force-prune
- `inbox.py` — synthetic gene construction; `gene_id` rejection; `party_id` strip; error paths
- `shadow.py` — shadow store init from genome; reconciliation guard

### Integration tests
- Round-trip: write gene → export → parse vault file → assert frontmatter matches
- Authored delta-sync: edit `pinned: true` in vault → trigger sync → assert genome reflects
- Computed-field rejection: edit `chromatin` in vault → assert rolled back + edit-rejections.md row
- Body edit rejection: change body → content_sha256 mismatch → rolled back
- supersedes target validation: edit with non-existent target → reject + edit-rejections.md row
- Inbox ingest: drop `_inbox/test.md` with `party_id: malicious` → assert party_id stripped, gene attributed to server's party
- Inbox path traversal: drop `_inbox/test.md` with `source_id: ../../etc/passwd` in frontmatter (if attempted) → write blocked
- TTL prune via filename: write trace with `_exp<past>` → prune deletes + rollup line in correct hour shard
- max_retention_hours_hard: pinned trace with old mtime → force-pruned + structured log
- Concurrent watcher + writer: rapid export + edit → no feedback loop, no spurious rollbacks
- Reconciliation: artificially divergent shadow (vs. disk) → reconciliation pass detects + repairs
- Circuit breaker: induce 3 watcher crashes → assert circuit-broken; auto-probe restores; manual reset works
- Stale sentinel: pre-create `.helix-syncing` before startup → assert cleared
- /context/packet authorship: ingest via inbox → query → assert `attribution.content_authored_by_operator: true`
- Quarantine enforcement: set `quarantine_reason: "..."` → query → gene excluded from `/context`, `/context/refresh-plan`, `/consolidate`, and the `_sema_cache` rebuild
- Pinning vs. staleness: pin a gene + decay live_truth_score → assert it appears in `stale_risk`, not `verified`
- Cross-party edit rejection (per I-7): vault configured for party A → operator edits a gene with `party_id: B` → assert reject + `cross_party_edit_attempt` reason class
- Cross-party supersedes target: try to set `supersedes: ['[[gene-from-party-B]]']` → assert reject + `missing_supersedes_target` (gene_id NOT echoed in rejection record)
- Validator in-flight gate: rapid double-save on same gene → assert exactly one validator run, no double-write to `vault_state`
- Edit-rejection log redaction: trigger any rejection → assert `_meta/edit-rejections.md` contains field name + reason class but NOT the attempted value or current genome value

### Live (live-marked, requires running server)
- 60s multi-agent simulated load + watcher + pruner running concurrently; assert no crashes, no genome corruption, watcher state stays at 1

---

## v1.1 / future work (deferred, tracked)

- **Audit log** — structured per-rejection record beyond `_meta/edit-rejections.md` (Splunk/ELK-shaped)
- **Schema migration tooling** — `helix-vault migrate` v1 ships as a stub; v1.1 adds first migration
- **Backup/restore tooling** — `helix-vault backup [--include-pinned]`
- **Multi-agent trace isolation** — per-agent `_traces/<participant_handle>/` partitioning
- **Obsidian plugin** — adds live-update animations, custom panes, edit-rejection toasts
- **Bidirectional content sync** — explicitly rejected for v1 (see Non-goals)
- **`_unresolved/` heuristic auto-suggestion improvements** — currently suggestion-as-comment; v1.1 could add an "accept suggestion" CLI subcommand
- **Vault federation** — multi-machine deployments sharing a vault via cloud-sync (Obsidian Sync, Syncthing). Requires conflict-resolution work beyond LWW.

---

## Out of scope (for clarity)

- Multi-vault per helix instance
- Cross-helix-instance vault sharing/federation
- Custom Obsidian plugin (v1)
- Bidirectional content sync (only frontmatter authored fields)
- Real-time/sub-second propagation
- Conflict resolution beyond LWW
- Per-trace ACLs (single-user-laptop threat model; multi-tenant deployments use `trigger_only = true`)

---

*Spec v2 incorporates findings from a multi-persona review pass on 2026-05-06
(structural / security / scalability / agent-safety / operations).
The original v1 draft had 33 distinct findings across the 5 reviewers;
all "must-fix" items are addressed inline above.

A second targeted re-review (security + agent-safety) verified all v1 fixes
and surfaced 7 new findings. The four Important new findings — cross-party
edit attempts, quarantine read-path coverage, validator concurrent re-entry,
and edit-rejection value disclosure — are addressed by Invariant I-7
(single-party scoped vault), the per-gene in-flight gate in `validator.process_edit`,
expanded quarantine clauses across all read paths, and value-redaction in
`_meta/edit-rejections.md`. The three Minor findings are folded into the
appropriate sections.

A third 3-reviewer pass with opposing leads (pro-ship / anti-ship / independent)
produced unanimous "architecture is sound" but split on scope. The independent
reviewer's 3 specific concerns are addressed in v3.5: (a) replace the 1-second
self-event suppression window with a content-hash sentinel
(`vault_state.last_exported_disk_hash`), (b) define `_stale/` population +
lifecycle + watcher exclusion explicitly, (c) make fan-out migration eager
rather than lazy. The independent reviewer's "alternative axis" question
(vault state in `genome.db` vs. sibling `vault.db`) is addressed by moving
to a sibling `vault.db` for cleaner lifecycle separation.

The anti-ship reviewer's case for shrinking v1 scope (read-only export +
traces only, defer the curation/watcher/shadow stack to v1.1) was acknowledged
as a defensible product call; the operator's directive was Path 1 (ship full
v3 + 4 minor revisions), so v3.5 retains the full scope with refinements.*
