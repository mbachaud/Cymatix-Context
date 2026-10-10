# Dashboard UI/UX pass: implementation plan

Status: approved design canvas (https://claude.ai/artifact/H7umvtFhLvKJoxVGZa4hsR).

| Part | State |
|---|---|
| PR 1: toggle, compact switchboard, Chunks, tooltips, Open folder, verbatim agent names | done (#485) |
| PR 2: Delivery panel (chunks, characters, packet latency) | done |
| PR 3: per-store sidecar, Freeze = read-only, sync folders, folder picker | done |
| PR 4a: Enable/Stop observability from the desktop shell | done |
| PR 4b: live-update cycle | measured; focus-preserving panel patch done, push stream not needed (see "Measured") |
| PR 4c: graph renderer stages 1 and 2, OTel-backed charts (`/api/obs/*` proxy) | not started |

Scope: the launcher dashboard (`cymatix_context/launcher`) as shown in the Electron shell (`desktop/`). The controls block lives in `dashboard.html` and is updated by `refreshControls()` in `launcher.js`; everything under `#panels` is server-rendered HTML swapped every 2 s from `/api/state/panels`.

## Wiring found

| Area | Where it is wired today | Consequence |
|---|---|---|
| Start/Stop/Restart | `components/controls.html` (three `data-action` buttons), click handler and `sendControl()` in `launcher.js`, `refreshControls()` flips `disabled` from `/api/state` | One toggle is a template + JS change. No server change. `state.cymatix.start_pending` already exists for the Starting state. |
| Header overlap | `.controls` grid in `launcher.css` (`1fr auto`), no wrap below ~900 px | CSS fix only. |
| Switchboard | `collector._switchboard_panel()` builds 12 `{label,value,group,description}`; `switchboard_panel.html` renders all | Add an `active` flag in the collector; template folds inactive ones. |
| Diagnostics log folder | Electron already exposes `window.cymatix.openLogs()` (preload, `cymatix:open-logs` IPC); only the rail uses it | Add a delegated handler in the main click listener. The button must survive the 2 s panel swap, so visibility is a body class set once, not per-element JS. |
| Genes label | Templates (`controls`, `genes_panel`, `graph_summary_panel`), collector keys stay `genes` | Labels only. Tests assert on keys, not labels. |
| Component tooltips | `tools_panel.html` renders `name/kind/status/backend` from `/admin/components` | Prefer a server `description` field when present; else a launcher-side glossary. |
| Agent naming | `host_labels.py` (`_VENDOR_MAP`, `_HOST_MAP`), `model_labels.py` (`_MODEL_MAP`), `collector._build_tooltip`, rail `<option>` list in `layout.html`, `HOSTS` set in `desktop/src/mcp.js` | Echo announced values verbatim. Rail host list comes from `cmd_mcp._TARGETS` (the CLI's own table). `mcp.js` keeps a shape check, the CLI is the allowlist. |
| Per-store sync/freeze | Server `[sync]` is global; `swap-db` takes `read_only`; launcher `genome_registry.py` lists stores | PR 3. Needs a sidecar file per store plus new launcher routes and an Electron folder-picker IPC. |
| Delivery panel | `/metrics/tokens` and the pipeline ring (`stage`, `ms`, `ts` only) | PR 2. Server must add `delivered_chunks`, `delivered_chars`, `tiers` to ring events. |
| Graph summary / live cycle / OTel | `graph_summary.py`; two 2 s polls; `ObservabilitySupervisor` only in the tray | PR 4, after the measurement spike. |

## PR 1 (this branch): quick wins and naming

1. Single Start/Stop toggle (`data-action="toggle"`), Restart kept secondary; pending states from `start_pending`; header wraps instead of overlapping.
2. Diagnostics "Open folder" (desktop bridge only) and "Copy path".
3. "Genes" to "Chunks" in labels.
4. Component tooltips with server description or glossary fallback.
5. Agent labels verbatim (delete the three maps); rail host list from the CLI table; `mcp.js` no longer hardcodes names.
6. Compact switchboard: inactive flags fold behind "N switched off".

Tests first for each Python-visible change (`tests/test_launcher_*`, `tests/test_host_labels.py` rewritten to assert verbatim echo), plus `desktop/test/support.test.js` for the `mcp.js` change.

## PR 2: delivery metrics

Server: add `delivered_chunks`, `delivered_chars`, `tiers` to the events `get_recent_pipeline_events()` returns (assemble stage). Launcher: `_delivery_panel()` computes last/avg/p95 from the ring; `tokens_panel.html` becomes `delivery_panel.html`. Monitoring pipeline table gains the new columns.

## PR 3: per-store sync and Freeze

Sidecar `<store>.cymatix.json` (`{sync:{enabled,roots,interval_s}, frozen}`), read and written by the launcher only. New routes `POST /api/genome/settings`, `POST /api/genome/freeze`. Freeze: refuse ingest, pause sync, open with `read_only` on swap. Electron `cymatix:pick-folder` IPC (`dialog.showOpenDialog`) guarded like the other handlers. Active-store card shows sync; other stores show saved settings.

## Measured: the 2 s refresh (2026-10-05)

Stub launcher with 40 agents, 20 packets, 3 stores, in the browser pane (not the packaged app):

- `/api/state/panels` is 95.9 KB (1,464 DOM nodes) and `/api/state` is 22.1 KB per tick, about 59 KB/s on loopback.
- Parse plus swap took a median 11.1 ms (max 17.6 ms) over 60 swaps.
- Every swap dropped keyboard focus (`document.activeElement` fell back to `body`), and replaced every node, so hover and in-panel scroll reset too, even with identical HTML.

Result: bytes and CPU are small enough that a push stream is not justified. The user-visible cost was lost interaction state, so `launcher.js` now replaces only the panels whose HTML changed and hands focus back to a replaced control (`patchPanels`, `focusKey`). After the change a focused Diagnostics button kept focus across several polls and an unchanged panel kept its DOM node. Not measured: a much larger store, the Electron renderer, memory over a long session.

## PR 4: live cycle, renderer, OTel

Measurement spike first (bytes per tick, swap cost, lost UI state); then keyed DOM patching. Graph summary stage 1 bars, stage 2 sampled neighbourhood (read-only SQLite, capped at 300 nodes). Launcher `/api/obs/*` proxy to Prometheus/Loki with ring fallback; "Enable observability" via the engine, pending a spike on whether the packaged engine can host the supervisor.
