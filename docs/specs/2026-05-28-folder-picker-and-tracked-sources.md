# Folder Picker + Tracked-Sources Registry (1.0 UX)

**Status:** draft for sign-off, 2026-05-28. Brainstorm artifact — no code yet. Authored after the v0.6.0 release lands the engine-side bench/SQL/registration fixes; this spec covers the human-facing UX gap blocking 1.0.

## 1. Goals + non-goals

**Goals**
- Replace the hardcoded `_DEFAULT_SOURCES` list in `scripts/ingest_all.py:223` and the hardcoded `PROFILES[*]["roots"]` lists in `scripts/build_fixture_matrix.py` as the authoritative source of "what's tracked" for normal operator use. CLI specs and PROFILES stay as escape hatches.
- Give a non-Python user a way to add/remove tracked folders using their OS file explorer (Windows `IFileDialog` / macOS `NSOpenPanel` / Linux GTK) reachable via the existing launcher web dashboard + system tray.
- Land a structured `tracked_sources.json` registry with per-source metadata (label, category, filters, status, ingest stats) that is the single read source for ingest, fixture builds, and (post-1.0) federation role bindings.
- Surface tracked-source status in the launcher dashboard so the user can see what's pending, what's ingested, when, and how many genes each source produced.
- Make folder add → ingest-on-next-cycle the default flow. Explicit "Ingest now" is a separate one-click action; never auto-launch a heavy ingest from a folder-add.

**Non-goals**
- No Windows shell extension / macOS Finder extension (right-click → "Track in helix"). Per-OS native installer / signing work that 1.0 does not need. Filed as post-1.0 enhancement.
- No browser-side folder picker via `<input webkitdirectory>` or File System Access API. Browsers do not return real filesystem paths from these — the round-trip through the tray-spawned native dialog is required, not optional.
- No federation/access-control UI in this spec. Schema reserves the seam (`acl` field, `category` field already exists); the SSS role-binding UI is a separate post-1.0 spec.
- No changes to ingest scheduling, shard layout, or bench-profile semantics. `enterprise_rag_*` profiles in `build_fixture_matrix.py` stay as-is; only the "default operator profile" reads from the new registry.
- No new asset generation (no Claude Design pass). Reuse the existing warm-scheme palette tokens in `launcher.css`. Defer fresh visual identity work to the federation UI spec, where it pays off across multiple surfaces.
- No tray-resident folder picker for the install-time first-run flow. That ships as part of a separate onboarding spec; this spec assumes the launcher is already installed and running.

## 2. Surface area

| File:line | Change (what, not how) |
|---|---|
| `helix_context/launcher/sources_registry.py` (new) | Reader/writer for `tracked_sources.json`. CRUD: `add`, `update`, `remove`, `list`, `get`. Atomic write (temp + rename). Version-tagged schema. |
| `helix_context/launcher/folder_picker.py` (new) | OS-native folder dialog wrapper. Spawns a short-lived subprocess (tkinter `filedialog.askdirectory` is the v1 implementation — stdlib, cross-platform, no extra deps). Returns chosen path via stdout JSON or empty on cancel. |
| `helix_context/launcher/app.py:114` (route block) | Add 5 routes: `GET /api/sources`, `POST /api/sources/pick`, `POST /api/sources/create`, `PATCH /api/sources/{id}`, `DELETE /api/sources/{id}`. See §5 for contracts. |
| `helix_context/launcher/templates/dashboard.html:6-23` | Add 5th tab `Sources` to the `<nav class="tab-bar">`. Tab icon glyph TBD (current ones are `+` `o` `x` `~`; use `>` for sources). |
| `helix_context/launcher/templates/components/sources_panel.html` (new) | Server-rendered partial: table of tracked sources + Add Folder button + per-row action menu. Polled via existing `/api/state/panels` polling shape. |
| `helix_context/launcher/templates/components/panels.html` | Include `sources_panel.html` under the new tab data-key. |
| `helix_context/launcher/static/launcher.js` | Add: button-click handler for "Add Folder" (POSTs to `/api/sources/pick`, then opens confirm-metadata modal); per-row pause/resume/remove handlers; remove-confirmation modal. |
| `helix_context/launcher/static/launcher.css` | New section for sources panel: table row hover, status pill colors (active=warm-green, paused=warm-amber, error=warm-red). All from existing palette tokens. |
| `helix_context/launcher/tray.py:608` (`_build_menu`) | Add `Open dashboard → Sources` quick-link menu item under existing `Open dashboard`. |
| `helix_context/launcher/collector.py` | Extend `collect()` output with `sources` block: count by status, last-ingest timestamp, total genes attributable to tracked sources. Used by the panel poll. |
| `scripts/ingest_all.py:223` | Make `_DEFAULT_SOURCES` fall back to `sources_registry.list(status=active)` when no `--source` flag is passed. Existing CLI `--source path=label:category` flow unchanged and takes precedence. |
| `scripts/ingest_all.py` (CLI) | New flag `--use-registry/--no-registry` (default `--use-registry`). When the registry is empty AND no `--source` flags are passed, fall back to the old hardcoded list with a deprecation warning. |
| `tests/test_sources_registry.py` (new) | Round-trip CRUD, schema-version migration, concurrent-write safety, atomic-write crash-recovery. |
| `tests/test_folder_picker.py` (new) | Subprocess-spawn contract: cancel returns empty, valid path returns JSON, invalid path errors cleanly. Skipped under `pytest -m no_gui` (CI). |
| `tests/test_launcher_sources_api.py` (new) | Route-level: list/create/update/delete; auth header presence; conflict on duplicate path. |
| `docs/architecture/SOURCES_REGISTRY.md` (new) | One-page narrative: schema, lifecycle, federation forward-compat. |

## 3. `tracked_sources.json` schema

Location: `${HELIX_CONTEXT_HOME:-~/.helix-context}/tracked_sources.json`. The launcher already writes state to `HELIX_CONTEXT_HOME` (see `helix_context/launcher/state.py`); the registry sits next to it.

```jsonc
{
  "schema_version": 1,
  "host_label": "max-rig",                 // matches host_labels.host_label_for() — informational only
  "default_category": "participant",
  "sources": [
    {
      "id": "src_a1b2c3d4",                // ULID-ish, generated on add
      "path": "F:/Projects/helix-context", // normalized forward-slash, OS-absolute
      "label": "helix-context",            // free text, defaults to basename
      "category": "participant",           // participant | org | reference | agent
      "filters": {
        "include_ext": null,               // null = all (subject to ingest denylist); list overrides
        "exclude_glob": [".git/**", "node_modules/**", "*.db", "*.sqlite*"]
      },
      "recursion_depth": null,             // null = unlimited; int = max depth from path
      "status": "active",                  // active | paused | error | removed
      "added_at": "2026-05-28T22:14:03Z",
      "last_ingested_at": "2026-05-28T22:30:11Z",
      "last_ingested_genes": 12847,
      "last_ingested_status": "ok",        // ok | partial | failed
      "last_error": null,
      "acl": null,                         // reserved for post-1.0 SSS federation
      "notes": null                        // free text, operator-editable
    }
  ]
}
```

**Reserved field semantics.** `acl: null` means "no access control configured — fall back to host-level permission". When SSS federation lands, `acl` becomes `{roles: ["role_xyz", ...], inherit: bool}` referencing the federation role table; the registry schema bumps to `schema_version: 2` with an automatic migration that leaves `acl: null` for every existing entry. No data loss, no operator action required.

**Concurrency.** Single launcher process owns the registry. Writes go through `sources_registry.py`'s in-process lock + atomic temp+rename. External edits (operator hand-edits the JSON) are detected on next read via mtime check; the launcher reloads and logs the diff. No file-lock primitives — the launcher is the only writer in normal operation.

## 4. UX flow

### 4.1 Adding a folder (golden path)

```
User opens dashboard (tray → Open dashboard, or http://localhost:<launcher_port>/)
  → clicks "Sources" tab
  → clicks "Add Folder" button
  → browser POST /api/sources/pick (no body)
  → launcher spawns folder_picker.py subprocess (tkinter native dialog)
  → OS folder dialog appears (modal, in front of browser)
  → user navigates Win Explorer / Finder, picks a folder
  → subprocess writes {"path": "F:/Projects/Foo"} to stdout, exits 0
  → launcher returns {"path": "F:/Projects/Foo", "exists": true, "kind": "directory"} to browser
  → browser opens "Confirm tracking" modal pre-filled with:
      label = basename
      category = default_category (participant)
      filters = global defaults
      recursion = unlimited
  → user adjusts if desired, clicks "Track this folder"
  → browser POST /api/sources/create with the form payload
  → launcher writes registry entry, returns the persisted record
  → browser dismisses modal, refreshes sources panel
  → user sees new row with status="active", last_ingested_at=null
  → next scheduled ingest (or manual "Ingest now" from row menu) picks it up
```

### 4.2 Per-row actions

Right-side menu on each row:

| Action | Effect |
|---|---|
| **Pause** | `status` → `paused`. Next ingest skips this path. Existing genes remain queryable. |
| **Resume** | `paused` → `active`. |
| **Ingest now** | Triggers an out-of-band ingest pass for just this source (background task; status row shows spinner; updates `last_ingested_*` on completion). |
| **Edit metadata** | Modal to edit label, category, filters, recursion. Path is immutable (a path change is a remove + add). |
| **Remove (keep data)** | `status` → `removed`. Hidden from default list; genes retained, still queryable. Toggle "Show removed" reveals. |
| **Forget (delete data)** | Destructive. Confirmation modal naming the folder. Removes registry entry AND deletes all genes whose `source_id`/`repo_root` resolves under this path. Async (can be long); progress shown in panel. |

### 4.3 Cancel / error paths

- User cancels OS dialog → subprocess exits 0 with empty stdout → launcher returns `{"path": null, "cancelled": true}` → browser silently no-ops (no error toast).
- Subprocess crashes (timeout 60s) → launcher returns 500 with diagnostic → browser shows "Couldn't open folder picker. Open `~/.helix-context/launcher.log` for details." with a copy-path button.
- Path already tracked → `POST /api/sources/create` returns 409 with the existing record → browser shows "This folder is already tracked as `<label>`" with a "Jump to row" link.
- Path doesn't exist when re-checked at create time (rare race) → 400 with diagnostic.

### 4.4 Tray quick-access

`tray.py:_build_menu` gains one new item:
```
Open dashboard
  ├ Overview      (current default)
  └ Sources       (new — opens dashboard with #sources fragment)
```
No new tray sub-menu for source management itself — the dashboard is the canonical surface. The tray quick-link exists purely so a user who lives in the tray (most operators after first-run) doesn't have to remember the URL.

## 5. API contract

All routes are launcher-local (loopback). No auth in v1.0; the launcher already binds 127.0.0.1 only. If `HELIX_LAUNCHER_BIND_HOST` is set to a non-loopback address, all `/api/sources/*` routes 403 unless `HELIX_LAUNCHER_TRUSTED=1` is also set (explicit opt-in for remote dashboard use; federation will replace this with proper auth).

```
GET /api/sources
  → 200 {"sources": [...], "schema_version": 1, "default_category": "participant"}
     Honors ?status=active|paused|removed|all (default: active+paused, hides removed)

POST /api/sources/pick
  → 200 {"path": "F:/Projects/Foo", "exists": true, "kind": "directory", "cancelled": false}
  → 200 {"path": null, "cancelled": true}                       on cancel
  → 500 {"error": "...", "detail": "..."}                       on subprocess failure
  → 408 {"error": "picker_timeout"}                             after 60s

POST /api/sources/create
  body: {"path": str, "label"?: str, "category"?: str, "filters"?: {...}, "recursion_depth"?: int|null}
  → 201 {<full source record>}
  → 400 {"error": "path_not_found"|"path_not_directory"|"invalid_category"|...}
  → 409 {"error": "already_tracked", "existing": {<existing record>}}

PATCH /api/sources/{id}
  body: any subset of {label, category, filters, recursion_depth, status, notes}
  → 200 {<updated record>}
  → 404 {"error": "not_found"}
  → 400 {"error": "immutable_field", "field": "path"}

DELETE /api/sources/{id}?mode=soft|forget
  soft   → 200 {"status": "removed"}            (default; status flip only)
  forget → 202 {"status": "forgetting", "task_id": "..."}   (async data delete)
  → 404 if missing
```

## 6. Folder-picker subprocess contract

`folder_picker.py` is a small standalone script — not part of the launcher process — invoked as:

```bash
python -m helix_context.launcher.folder_picker --initial-dir "F:/Projects" --title "Pick a folder to track"
```

Stdin: unused. Stdout: single JSON line. Exit code: 0 on success or cancel; non-zero on error.

```json
{"path": "F:/Projects/Foo", "cancelled": false}
{"path": null, "cancelled": true}
{"error": "tkinter_unavailable", "detail": "..."}   // exit 2
```

**Why subprocess, not in-process.** tkinter `Tk()` is single-instance per process and conflicts with the existing pystray main loop in `tray.py`. Launching the dialog in a subprocess sidesteps the main-loop ownership question entirely and means a picker crash can't take down the launcher. The launcher passes `--initial-dir` based on the OS's "last used folder" memory (we maintain this in `tracked_sources.json` meta).

**Cross-platform notes.**
- Windows: tkinter calls into the native common dialog. Looks like a normal File Explorer "Select Folder" prompt.
- macOS: tkinter uses the Cocoa folder panel. Native-looking. (Note: tkinter on macOS needs the system's `python.org` build or a Tk-bundled venv — Apple's stock Python 3 has a known broken Tk. Document in SETUP.md.)
- Linux: tkinter falls through to the GTK or X11 file chooser depending on what's installed. Functional but less polished. Acceptable for 1.0; a Qt-based picker upgrade is a post-1.0 nice-to-have.

**Upgrade path.** Swapping the subprocess script for a pywin32 `IFileDialog` call on Windows / a PyObjC `NSOpenPanel` call on macOS is a drop-in replacement that doesn't change the API contract. Defer until the tkinter version is in users' hands and we have feedback on what's not native enough.

## 7. Federation forward-compat

The `category` field in each source record (`participant` / `org` / `reference` / `agent`) is the same enum already used by `_parse_source_arg` in `scripts/ingest_all.py:187`. Keeping that semantic alignment means the registry can be the single source-of-truth that the SSS federation UI binds roles onto, without a parallel "tracked things" concept appearing later.

When `docs/architecture/FEDERATION_LOCAL.md`'s SSS role machinery lands, the federation UI adds a "Permissions" tab to each source row's edit modal that writes into the reserved `acl` field. No migration needed; `acl: null` continues to mean "fall back to host-level access."

**What this spec deliberately doesn't decide.** Whether `acl` lives in `tracked_sources.json` or in a federation-owned sidecar file referenced by source id. Both are viable; the federation spec picks the answer. The registry just promises the field name will be stable and writes happen through a single API.

## 8. Test plan

### 8.1 `tests/test_sources_registry.py`

- `test_round_trip_add_list_get_remove` — add 3 sources, list returns 3 active, remove 1 → 2 active + 1 removed; `list(status="all")` returns 3.
- `test_atomic_write_survives_crash` — monkeypatch `os.replace` to raise after temp write; verify original file is intact and recoverable on next load.
- `test_concurrent_writes_serialized` — two threads calling `add()` simultaneously; both succeed, both records persisted, no JSON corruption.
- `test_schema_version_migration_v1_to_v2_placeholder` — load a synthetic v2 file with `acl` populated; v1 reader rejects with a clear "registry was written by a newer launcher" error rather than silently dropping fields.
- `test_duplicate_path_normalized` — add `F:/Projects/Foo` and `f:/projects/foo/` (Windows case + trailing slash); second add raises `AlreadyTracked` with the first record's id.
- `test_external_edit_detected_on_reload` — write the file by hand, call `list()`, verify the launcher's in-memory cache reloaded.

### 8.2 `tests/test_folder_picker.py`

- `test_subprocess_cancel_returns_empty` — invoke with `HELIX_PICKER_TEST_CANCEL=1` env (test harness short-circuits the dialog); subprocess exits 0, stdout `{"path": null, "cancelled": true}`.
- `test_subprocess_success_returns_path` — env `HELIX_PICKER_TEST_PATH=<tmpdir>`; subprocess exits 0, stdout `{"path": "<tmpdir>", "cancelled": false}`.
- `test_subprocess_tkinter_unavailable` — env that breaks the `import tkinter`; subprocess exits 2 with `{"error": "tkinter_unavailable", ...}`.
- `test_subprocess_timeout` — launcher kills subprocess after 60s; verify cleanup of child + parent returns 408.
- All tests marked `@pytest.mark.gui` and skipped under CI's `-m "not gui"`.

### 8.3 `tests/test_launcher_sources_api.py`

- `test_GET_sources_filter_status` — pre-seed registry with mix of statuses; `GET /api/sources?status=active` returns only active.
- `test_POST_create_validates_path` — non-existent path → 400.
- `test_POST_create_409_on_duplicate` — second create of same path returns the existing record.
- `test_PATCH_immutable_path` — attempt to PATCH `path` field returns 400.
- `test_DELETE_soft_vs_forget` — soft flips status; forget enqueues an async task and returns 202 with task id.
- `test_DELETE_forget_actually_deletes_genes` — populate a small genome with genes whose `repo_root` is under the source path; forget completes; query returns zero matches.

### 8.4 Manual / GUI smoke

Documented in a new section of `docs/ops/operator-runbooks.md`:
- Add a folder via dashboard → row appears → ingest now → genes count populates.
- Pause a source → next ingest skips it (verify via log line + unchanged gene count).
- Forget a source → genes gone, /context queries no longer return them.
- Cross-platform: same flow on Windows + macOS + Linux. (Joe's spark-e92c is the natural Linux smoke host.)

## 9. Acceptance criteria

- A user with no Python knowledge can install helix-context, open the tray, click "Open dashboard → Sources", click "Add Folder", pick a folder in Explorer/Finder, click "Track this folder", and see the row appear with `status=active` — without touching any config file or terminal command.
- `scripts/ingest_all.py` with no `--source` flags and an empty registry warns and falls back to `_DEFAULT_SOURCES`. With a populated registry, it ingests exactly the active entries with no warning.
- `tracked_sources.json` is the only mutable state required for the picker UX (other than per-ingest gene data in shards).
- All tests in §8.1, §8.3 green on CI; §8.2 tests pass when run locally with display.
- New dashboard panel renders correctly on Win/macOS/Linux at default browser zoom, passes the existing launcher CSS lint, and visually integrates with the warm scheme (manual sign-off).
- Tray menu adds at most 1 new top-level item; menu rebuild latency unchanged.
- `docs/architecture/SOURCES_REGISTRY.md` is published and linked from `docs/SETUP.md`.

## 10. Out of scope

- Shell extension (Win Explorer / macOS Finder right-click → Track). Post-1.0.
- Drag-and-drop folder onto the dashboard. Browsers expose `FileSystemDirectoryEntry` here but path resolution still requires a tray round-trip; defer until v1 of the picker has feedback.
- Scheduled/watcher-based auto-ingest on folder change. Today's ingest is manual or cron-driven; "watch this folder and rebuild on change" is a separate, much heavier spec.
- Multi-host registry sync. The registry is local-only; federation will introduce a remote registry concept. v1.0 ships local-only.
- Bench fixture profiles (`enterprise_rag_*`). Those keep their own `PROFILES` dict; the registry is for normal operator use.
- Folder picker for first-run onboarding (during the install flow itself). Separate onboarding spec.
- Authentication. Loopback-only binding is the v1.0 trust model. Federation spec brings real auth.

## 11. Open questions (need sign-off)

1. **Registry location.** Default `~/.helix-context/tracked_sources.json` — confirm vs putting it under the launcher's existing state dir (`helix_context/launcher/state.py` uses `${HELIX_CONTEXT_HOME}/launcher_state.json` today). Bundling next to `launcher_state.json` is cleaner; separating it keeps "user intent" (sources) from "launcher runtime state" (last-pid, last-port). My lean: **separate file**, both under `HELIX_CONTEXT_HOME`.
2. **Default category.** `participant` (current `_DEFAULT_SOURCES` default for `F:/Projects`) vs `reference`. The picker UI shows the dropdown either way; this is just what's pre-selected. My lean: **`participant`** — most users tracking their own folders want full lifecycle tracking, not reference-only.
3. **Forget-task surfacing.** Async task ID returned by `DELETE ?mode=forget` — is there an existing task-progress surface in the dashboard, or does this need a new "Background tasks" affordance? If new, the spec grows; if reusable, scope holds.
4. **macOS Tk caveat.** Document the python.org Python requirement in SETUP.md or ship a tkinter-bundled installer for macOS? My lean: **document for v1.0**, packaged installer is a post-1.0 polish item.
5. **Tray glyph for the Sources quick-link.** Current dashboard tabs use ASCII-art-ish glyphs (`+ o x ~`). Pick `>` for Sources or break convention and use a Unicode folder icon (`📁`)? My lean: **`>`** to stay consistent with the existing aesthetic; revisit when a real icon set lands with the federation UI.

---

**Sign-off needed on:** §1 goals/non-goals (especially "no shell extension in 1.0"), §3 schema (especially the `acl` reservation), §6 tkinter-subprocess approach (vs going straight to pywin32/PyObjC), and the §11 open questions.

**Out-of-scope reminder:** this spec does not implement anything. Per the PRD-first workflow, no code lands until these decisions are signed off.
