# Cymatix Launcher — Supervisor Process + Control UI

A standalone supervisor + dashboard that solves the "who starts cymatix"
problem and gives a live view of what's actually running.

**Status:** Implemented in the repo and shipped as `cymatix-launcher`.
**Maturity:** Beta; this document is now the architecture/reference doc for the shipped launcher.
**Depends on:** session registry (for party/participant counts on the dashboard).
**Related:** [`SESSION_REGISTRY.md`](SESSION_REGISTRY.md), [`RESTART_PROTOCOL.md`](RESTART_PROTOCOL.md).

---

## Motivation

Without the launcher, running a cymatix-context server means typing
`python -m uvicorn cymatix_context.server:app --host 127.0.0.1 --port 11437`
into a terminal and hoping nothing kills the shell. The gaps:

1. **No supervisor.** If the cymatix process dies — OOM, crash, deliberate
   kill — nothing brings it back. Empirically confirmed 2026-04-10:
   the live server at `:11437` was a backgrounded bash subprocess
   started by one Claude Code session, and would have died with that
   session's shell.
2. **No lifecycle controls.** Restarting cymatix (for code changes, model
   swaps, config reloads) requires manual process management.
3. **No live status surface.** `GET /stats` and `GET /health` exist, but
   you have to curl them. There is no at-a-glance view of what's loaded,
   who's connected, and what tools are active.
4. **Non-Claude users are left without a story.** Anyone using
   cymatix-context without a Claude Code agent to orchestrate the process
   has no straightforward "launch the thing" experience.

The launcher solves all four with one small supervisor process.

## Non-goals (to keep scope tight)

- Not a service/daemon installer. The launcher is a foreground app you
  run manually. Installing as a Windows service / systemd unit / macOS
  LaunchAgent is a follow-up.
- Not an observability dashboard. No charts, no time-series graphs, no
  historical drill-down. Only current state.
- Not an admin console. Admin actions like vacuum, consolidate, and
  compressor pause stay on the existing `/admin/*` endpoints and are not
  exposed through the launcher UI.
- Not a config editor. `cymatix.toml` stays file-edited; launcher only
  reads it.
- Not a multi-cymatix manager. The launcher manages exactly one cymatix
  child process. Federation / multi-instance management is out of scope.

Two clarifications, added with the host status panel: "not an observability
dashboard" still rules out charts, time series and historical drill down, but
read only diagnosis of current host, server, MCP and skill evidence is in
scope; and everything here describes a local, unauthenticated operator tool,
while a separately secured hosted console is a later scope whose controls do
not exist today.

## Architecture

Two processes, two ports:

```
┌─────────────────────────────────────┐
│  cymatix-launcher      :11438       │  ← supervisor + UI (long-lived)
│  - FastAPI + Jinja templates        │
│  - plain JS fetch polling           │
│  - subprocess.Popen for cymatix     │
│  - psutil for liveness / cleanup    │
└────────────┬────────────────────────┘
             │
             │ spawns / monitors
             ▼
┌─────────────────────────────────────┐
│  cymatix-context      :11437        │  ← the actual server (restartable)
│  - unchanged                         │
└─────────────────────────────────────┘
```

The launcher is a separate process with its own lifecycle. When you
close the launcher, cymatix stops cleanly. When you click **Restart**, the
launcher announces the restart via `POST /admin/announce_restart`, waits
750ms, terminates the child, and spawns a fresh one.

The launcher NEVER imports `cymatix_context.server` directly. It talks to
cymatix exclusively over HTTP at `http://127.0.0.1:11437`. This keeps the
launcher's own process memory light (no knowledge store load, no model) and means
the launcher keeps working when cymatix is stopped.

## Tech stack

No new heavy dependencies. Everything is a small addition under a new
optional extras group.

| Layer | Choice | New dep? | Rationale |
|---|---|---|---|
| Backend | FastAPI | already a core dep | Match the existing project stack |
| Templates | Jinja2 | **new** (~350KB) | Declarative HTML, no layout math in Python |
| Reactivity | plain JS `fetch` in `static/launcher.js` | none | Polls a server rendered HTML partial plus the JSON state, no build step |
| Styling | CSS custom properties | none | All visual tokens in `:root {}` |
| Process control | `subprocess` + `psutil` | `psutil` **new** (~1MB) | Cross-platform liveness, kill trees |
| Window (optional) | `pywebview` | **new, optional** | Native window wrapper — opt-in only |

New optional extra in `pyproject.toml`:

```toml
[project.optional-dependencies]
launcher = ["jinja2>=3.1", "psutil>=5.9"]
launcher-native = ["jinja2>=3.1", "psutil>=5.9", "pywebview>=5.0"]
```

Users run `pip install cymatix-context[launcher]` for browser-based UI, or
`pip install cymatix-context[launcher-native]` for a native window.

## No hardcoded UI — what this means in practice

The user's explicit requirement: no Tkinter, no hardcoded UIs. Translated
into concrete rules for this codebase:

1. **No layout math in Python.** Widths, heights, margins, padding,
   grid tracks — all live in CSS, never in Python.
2. **No colors in Python.** Every color is a CSS custom property
   defined in one `:root {}` block. Python never touches pixel values.
3. **No imperative draw calls.** No `canvas.draw_rect`, no
   `immediate_mode.button`. The UI is declarative HTML rendered from
   Jinja templates. Python produces data; templates produce markup.
4. **Components are named and reusable.** Each panel is its own
   template file (`templates/components/*.html`), included from the
   main dashboard. Changing one panel's layout is one file edit.
5. **Theming happens in one place.** Switching to a dark theme means
   flipping CSS variables in one block, not hunting through Python code.

If a future contributor is tempted to write `x=5, y=10, width=200` in
Python, they are doing it wrong.

## Project layout

```
cymatix_context/
  launcher/
    __init__.py
    app.py              # FastAPI app factory + CLI entry point (main())
    supervisor.py       # cymatix subprocess lifecycle (Start/Restart/Stop)
    state.py            # ~/.cymatix/launcher/state.json read/write + adoption
    models.py           # Pydantic models for launcher state + API responses
    templates/
      layout.html       # base template (head, script and css links, body shell)
      dashboard.html    # extends layout.html — full dashboard
      components/
        controls.html   # start/restart/stop buttons
        status_banner.html
        parties_panel.html
        participants_panel.html
        models_panel.html
        tools_panel.html
        genes_panel.html
        tokens_panel.html
        graph_summary_panel.html   # read-only graph layer counts
        host_status_panel.html     # read-only host readiness
    static/
      launcher.css      # one :root{} block + component classes
      launcher.js       # the dashboard poll loop, plain JS, no build step
```

## Entry point

New console script in `pyproject.toml`:

```toml
[project.scripts]
cymatix = "cymatix_context.server:main"            # unchanged
cymatix-launcher = "cymatix_context.launcher.app:main"   # NEW
```

Usage:

```bash
# Browser mode (default)
cymatix-launcher

# System tray icon — persistent, "close to tray" experience
# (requires [launcher-tray] extra)
cymatix-launcher --tray

# Close-to-tray with native window (Windows only — see platform notes)
cymatix-launcher --tray --native

# Native window (requires [launcher-native] extra)
cymatix-launcher --native

# Install as a system service (systemd / launchd / NSSM recipe)
cymatix-launcher install-service
cymatix-launcher uninstall-service
cymatix-launcher install-service --dry-run

# Don't auto-start cymatix; just show the UI with a Start button
cymatix-launcher --no-autostart

# Use a non-default cymatix port
cymatix-launcher --cymatix-port 11439

# Use a non-default launcher port
cymatix-launcher --port 11438
```

## Launcher REST API (on :11438)

The launcher binds `127.0.0.1` by default, so in the default configuration
every endpoint is reachable only from this machine. That default is not
authorization, and the bind is not exclusive: `_parse_args` accepts a
`--host` override (`cymatix_context/launcher/app.py:470`) and `_run_uvicorn`
passes whatever it receives straight to uvicorn
(`cymatix_context/launcher/app.py:1494-1496`), so an operator can bind a
routable interface.

There is no authentication and no authorization anywhere in the launcher. The
state routes perform no caller policy check of any kind
(`cymatix_context/launcher/app.py:201-224`), and neither do the control
routes. The current trust boundary is therefore the network reachability of
the bind address: whoever can reach it is a full operator. Do not expose the
launcher port beyond the local machine. Hosted exposure needs the separate
authenticated console scope, not a flag change here.

### `GET /`

Renders the full dashboard HTML. One of two HTML endpoints; the other is
`GET /api/state/panels` below. Everything else answers JSON.

### `GET /api/state`

The JSON view of the same collector snapshot the dashboard renders. The
browser fetches it once per poll from `refreshControls`
(`static/launcher.js:216-253`) to drive the status dot, the status label and
the enabled state of the Start / Restart / Stop buttons. It is also the
endpoint programmatic consumers and debugging should use. The dashboard
panels themselves are HTML and come from `GET /api/state/panels`, not from
this document.

Response:

```json
{
  "cymatix": {
    "running": true,
    "pid": 56792,
    "port": 11437,
    "uptime_s": 412.5,
    "version": "0.4.0b2",
    "last_restart_reason": "session registry DAL fix",
    "last_restart_at": 1775881217.9
  },
  "parties": {
    "count": 1,
    "party_ids": ["max@local"]
  },
  "participants": {
    "count": 3,
    "handles": ["taude", "laude", "raude"]
  },
  "models": {
    "loaded": [
      {"name": "gemma4:e4b", "size_mb": 4200, "source": "ollama"}
    ]
  },
  "tools": [
    {"name": "ribosome", "kind": "decoder", "status": "running"},
    {"name": "cpu_tagger", "kind": "encoder", "status": "idle"},
    {"name": "splade", "kind": "encoder", "status": "running"},
    {"name": "sema", "kind": "encoder", "status": "idle"}
  ],
  "genes": {
    "raw_chars": 47086009,
    "compressed_chars": 17516283,
    "compression_ratio": 2.69,
    "total": 8107
  },
  "tokens": {
    "session": 184293,
    "lifetime": 1_734_551,
    "tracked": true
  }
}
```

Panels whose underlying data is empty (e.g., `tools` empty, `models`
empty, `parties.count == 0`) are simply absent from the response. The
dashboard then conditionally renders them.

### `GET /api/state/panels`

The server rendered HTML partial for the panel area, produced from the same
`collector.collect()` snapshot as `GET /` and `GET /api/state`. The browser
fetches it once per poll from `fetchPanels`
(`static/launcher.js:192-214`) and swaps it into `#panels`. A request whose
`sec-fetch-mode` header is `navigate` is redirected to `/` with a 303, so the
partial is never a landing page.

### `POST /api/control/start`

Start the cymatix child if not running. Returns 200 with new state on
success, 409 if already running.

### `POST /api/control/stop`

Announce via cymatix `/admin/announce_restart`, wait 750ms, terminate the
process tree, wait for port 11437 to be free. Returns 200 when the
process is fully down, 408 if port never releases within 10s.

### `POST /api/control/restart`

Combined stop + start in sequence. Announces with `reason="manual
restart from launcher"`. Returns 200 when new cymatix answers `GET /stats`.

## Data sources (how the launcher populates `/api/state`)

Every field comes from an existing endpoint (or a trivial extension of
one). No new endpoints on the cymatix side are strictly required for the
first launcher slice, with two exceptions flagged below.

| State field | Source |
|---|---|
| `cymatix.running` | `psutil` check on stored PID + `GET /stats` reachability |
| `cymatix.pid` | launcher state file |
| `cymatix.port` | launcher config |
| `cymatix.uptime_s` | stored `start_time` subtracted from `now()` |
| `cymatix.version` | read from `cymatix_context.__version__` in child's env (or GET /stats if a version field is added — see note below) |
| `parties.count` | derived — count unique `party_id` values in `GET /sessions?status=all` |
| `parties.party_ids` | same query, projected to unique set |
| `participants.count` | `GET /sessions?status=active`, count |
| `participants.handles` | same, projected to handle list |
| `models.loaded` | Ollama `GET /api/ps` (from cymatix.toml `[ribosome] base_url`) + cymatix `/health` for non-Ollama backends |
| `tools` | **NEW endpoint on cymatix side** — `GET /admin/components` returns the running subsystem list. See below. |
| `genes.raw_chars` | `GET /stats` → `total_chars_raw` |
| `genes.compressed_chars` | `GET /stats` → `total_chars_compressed` |
| `genes.compression_ratio` | `GET /stats` → `compression_ratio` |
| `genes.total` | `GET /stats` → `total_genes` |
| `tokens.session` | **NEW endpoint on cymatix side** — session counter (see below) |
| `tokens.lifetime` | **NEW endpoint on cymatix side** — persisted counter (see below) |

### Two small cymatix-side additions

**`GET /admin/components`** — new endpoint. Returns the list of running
subsystems with their current status:

```json
{
  "components": [
    {"name": "ribosome", "kind": "decoder", "status": "running"},
    {"name": "cpu_tagger", "kind": "encoder", "status": "idle"},
    {"name": "splade", "kind": "encoder", "status": "running"},
    {"name": "sema", "kind": "encoder", "status": "idle"}
  ]
}
```

Status derivation: a subsystem is `running` if it has processed a call
in the last 60 seconds, `idle` otherwise. A subsystem that is disabled
in config is simply absent from the list (matches the "only
active/online" policy).

**Token tracking** — flagged in the original ask as "questionable but
maybe do it anyway." Treating it as optional Phase 2:

- `cymatix_context.metrics` module accumulates `tokens_in` and
  `tokens_out` counters on every `/v1/chat/completions` call.
- Session counter: in-memory, reset on cymatix process restart.
- Lifetime counter: persisted to a tiny `metrics.json` next to
  `genome.db`, updated every N seconds.
- New endpoint: `GET /metrics/tokens` returns both counters.
- Launcher's `/api/state` includes `tokens: null` until this lands,
  and the tokens panel simply doesn't render.

Recommendation: ship the launcher without token tracking first, add
metrics in a follow-up. The launcher does not block on this.

## Dashboard layout

A single page, rendered by `dashboard.html`. Layout in CSS grid, with
one header row + one content area that stacks the panels. All measurements
in CSS variables; no pixel values in Python.

```
┌─────────────────────────────────────────────────────────────┐
│  Cymatix Launcher      [Status: running, pid 56792, 6m42s]  │
│  [▶ Start]  [↻ Restart]  [■ Stop]                          │
├─────────────────────────────────────────────────────────────┤
│  Parties connected: 1                                       │
│    └─ max@local                                             │
│                                                             │
│  Participants: 3                                            │
│    ├─ taude   (active, 4s)                                  │
│    ├─ laude   (active, 12s)                                 │
│    └─ raude   (idle, 45s)                                   │
│                                                             │
│  Models loaded:                                             │
│    • gemma4:e4b   4.2 GB   (ollama)                         │
│                                                             │
│  Tools:                                                     │
│    • ribosome    decoder  running                           │
│    • splade      encoder  running                           │
│    • cpu_tagger  encoder  idle                              │
│    • sema        encoder  idle                              │
│                                                             │
│  Genes:  raw 44.9 MB  →  compressed 16.7 MB  (2.69×)        │
│          total 8,107 genes                                  │
│                                                             │
│  Tokens:  session 184.2K  |  lifetime 1.73M                 │
│          (only if tokens panel is enabled)                  │
└─────────────────────────────────────────────────────────────┘
```

Panels hide themselves conditionally:

- No participants registered → participants panel omitted
- No models reported by cymatix → models panel omitted
- `tools` list empty → tools panel omitted
- Token tracking not yet shipped → tokens panel omitted
- No active genome file on disk → graph summary panel omitted
- cymatix not running → everything below the controls is replaced with a
  single "cymatix is stopped" banner

This matches the user's explicit rule: "any data not active/online
doesn't need to be displayed."

## Polling model

The shipped dashboard uses plain JavaScript, not HTMX. HTMX is not vendored,
not served and not a dependency; `static/launcher.js` is the only script the
page loads.

`dashboard.html` declares the poll target and cadence as data attributes on
the panel container:

```html
<section id="panels" class="panels"
         data-active-tab="overview"
         data-poll-url="/api/state/panels"
         data-poll-interval-ms="2000">
  {% include "components/panels.html" %}
</section>
```

`startPolling` (`static/launcher.js:255-267`) reads those attributes, runs one
immediate pass and then a `setInterval` at the declared interval. That is the
only interval that drives the panels: there is no second loop and no status
specific endpoint. Each pass ages the rendered observation and then issues
two requests:

| Request | Function | What it updates |
|---|---|---|
| `GET /api/state/panels` (HTML) | `fetchPanels` (`:192-214`) | The whole panel area, swapped into `#panels` |
| `GET /api/state` (JSON) | `refreshControls` (`:216-253`) | Status dot, status label, control button enablement |

Both requests have an in flight guard and both run under a deadline.
`fetchPanels` returns early while `inFlight` is set and `refreshControls`
returns early while `controlsInFlight` is set (`:28`, `:193-194`,
`:217-218`, `:250-251`), so neither slow collections nor slow control reads
can stack up. Both go through `fetchBounded` (`:62-77`), which starts an
`AbortController` timer at `fetchDeadlineMs` (`:24`, four poll intervals
with a 5000 ms floor), covers the body read as well as the response, and
clears the timer in `finally`. A hung connection is aborted by the page
rather than left outstanding until the browser gives up.

A failed, aborted or non `ok` HTML fetch sets `data-stale="true"` on the
panel container. That attribute does two things: it fades the container
(`static/launcher.css:383-385`) and it prints a visible notice above the
grid, "Live updates are not arriving. The panels below are the last answer
the server gave." (`static/launcher.css:391-400`). A later successful fetch
removes the attribute and both effects go with it.

Independently of the fetch state, the host status card ages itself.
`markObservationAge` (`:43-57`) compares local monotonic elapsed time since
the last panel swap against the `data-fresh-for-s` the server serialised,
and sets `data-observation="expired"` on the card once the observation the
page is showing has outlived its freshness. It runs on every swap, in the
`finally` of every HTML fetch and on every interval tick, so an observation
still expires visibly while the fetches are failing. It reads two numbers
and sets one attribute: no readiness is decided in the browser, and nothing
is fetched.

The poll interval is not a CLI flag. There is no `--poll-interval` option and
no `--launcher-poll-interval` CSS variable. The 2000 ms value is the
`data-poll-interval-ms` attribute in `dashboard.html`; change it there.

## Host status panel: observation semantics

The host status panel is a read only diagnostic view of the same evidence the
`cymatix-status` CLI reports: which MCP host profile was discovered, whether
its configuration is canonical, whether the server endpoint is reachable and
healthy, whether the MCP registry shows a live entry, whether the portable
skill is installed and enabled, and the two readiness values derived from
those. It adds one additive key to the collector snapshot and no new route.

What the panel is, stated precisely:

- **Observation, not truth.** Every field is evidence from one completed
  observation of this machine at a stated time, shown with that time, so a
  value that was true a minute ago is never presented as a current fact.
  Three stamps are kept apart: last attempt (a refresh was admitted), last
  success (the most recent schema valid observation completed) and the
  observation time of the evidence on screen. A cache hit advances none.
- **Fresh, stale, unavailable.** Fresh means the shown observation is inside
  its lifetime. Stale means the latest refresh failed, timed out or was
  invalid and the previous approved evidence remains on screen with its
  original times. Unavailable means there is no approved observation yet.
  Stale and unavailable render neutral even when a retained value is true.
- **A clean negative is a success.** Unreachable, unknown or unhealthy
  evidence that was collected without error is a successful observation. The
  panel never turns green merely because a refresh completed.
- **Dimensions stay independent.** Healthy HTTP does not prove an
  authenticated MCP connection, registry evidence is labelled registry only,
  skill presence does not prove activation, a stopped child does not gate
  direct MCP readiness, and unknown is never coerced to false.
- **Supervised child liveness is separate.** The launcher field reports the
  child as the supervisor sees it, stamped at its own sampling time, not a
  page reachability check. The panel never requests the launcher's own state
  endpoint, so it cannot recurse through the route that serves it.
- **Shared collection.** `GET /`, `GET /api/state/panels` and `GET /api/state`
  share one bounded single flight collection with a completion time lifetime.
  Callers wait a finite budget, then take retained evidence or unavailable.

Probe safety, as the panel actually enforces it:

- **Self poll prevention is two rules.** The refresh runs with
  `launcher_url=None`, so the launcher state endpoint is never requested,
  and it passes the launcher's configured address (`CYMATIX_LAUNCHER_URL`,
  else the default) as a denied origin, so a discovered or default server
  URL that resolves to that address is refused before any request. Loopback
  is not the test on its own: the launcher is loopback too.
- **Redirect confinement, not just URL validation.** The loopback rule is
  applied once, before the first request. A probe therefore refuses to
  follow a redirect rather than letting a local endpoint hand it a remote
  address, and an answer that arrives from a different origin than the one
  requested is discarded instead of shown as local evidence.
- **A finite positive timeout budget.** Probe timeouts are validated, not
  merely parsed: zero, negative, NaN and infinity fall back to the 10 second
  default and anything above the 60 second ceiling is clamped to it, per
  request and without mutating any global.
- **Response bytes stay capped.** A response body is read to the 64 KiB cap
  plus one byte, for success and HTTP error alike, and a non object or
  malformed body leaves the endpoint reachable with unknown health rather
  than claiming a transport failure.
- **Diagnostic logging carries no values.** The refresh, the projection, the
  native config reads and the probe log an exception type name and nothing
  else. A path, a URL, a token or a traceback in a log line would defeat the
  allowlist the panel applies to the page.
- **What is not claimed.** The refresh deadline fences publication and marks
  the observation timed out; it does not cancel a blocked file or socket
  read, and the worker keeps the only refresh slot until it returns. There
  is no total read byte guarantee for native configuration files. A launcher
  started on another bind without setting `CYMATIX_LAUNCHER_URL` still denies
  the default address rather than its real one.

Diagnostics scope, as a boundary: the panel reads and nothing else. It
installs, enables, repairs, ingests and controls nothing, so it has no
mutation surface. Only an approved typed projection reaches the browser;
native configuration contents, environment mappings, credentials, inspected
paths, raw payloads and raw error text are dropped before rendering, before
the JSON response and before anything is cached, with escaping applied on top
of that allowlist rather than instead of it. Guidance is a fixed approved
vocabulary rendered as escaped text, never marked safe, linked or executable.
The panel inherits the trust boundary above: local operator diagnosis on an
unauthenticated local port, not an access controlled status feed and not the
hosted console.

## Orphan adoption

The launcher adopts already-running cymatix processes in two situations:

**1. On launcher startup** — `supervisor.adopt()` runs a two-stage check:

   1. **State file**: if `~/.cymatix/launcher/state.json` has a `cymatix_pid`,
      verify the process is alive and the command line still matches
      `cymatix_context.server:app`. If yes → adopted, no further scan.
   2. **Orphan scan**: use `psutil.net_connections()` to find the PID
      listening on `cymatix_host:cymatix_port`. Verify its command line
      matches cymatix uvicorn. Walk up to the uvicorn parent process (the
      one `subprocess.Popen` would hand us). Write its PID + command
      line to the state file.

   This means: **the launcher adopts any cymatix running outside of it,
   as long as the port matches**. No coordination required between
   externally-started cymatix processes and the launcher.

**2. On Start button click** — `supervisor.start()` checks port
availability. If the port is busy:

   - If the occupying process IS a cymatix uvicorn → adopt it (same
     behavior as the startup orphan scan), return its PID, no spawn
   - If the occupying process is something else (another dev server,
     unrelated Python script) → raise `SupervisorError` and record it
     as a `last_error` for the diagnostics panel

This fixes the common UX trap where the launcher and a developer's
`python -m uvicorn cymatix_context.server:app` race on port 11437 and
the Start button returns a 500. Instead the launcher quietly adopts
the external cymatix and the dashboard shows it as running.

### Why we adopt without confirming

A user asking "Start this" when a cymatix is already running on the
target port almost always means "make the launcher aware of the
existing cymatix," not "start a second cymatix" (which would fail anyway
since both can't bind the same port). Silent adoption matches the
user's intent; a confirmation prompt would be friction for the
common case.

If you want to explicitly refuse adoption and fail instead, kill
the orphan first, then click Start.

## Diagnostics panel

A footer panel on the dashboard that always renders, regardless of
cymatix state. Surfaces three kinds of information:

**Last error:** if any `start`/`stop`/`restart` operation failed since
the launcher started, its error message is displayed in a red-bordered
strip. Cleared when the next operation succeeds. Fed by
`supervisor.get_last_error()`.

**Orphan warning:** when `supervisor.is_running()` is False but an
unmanaged cymatix is detected on the port (e.g. started from a terminal
outside the launcher), a yellow-bordered strip says:

> **Orphan cymatix detected** — PID X is listening on port 11437 but
> is not managed by this launcher. Click *Start* to adopt it.

**Paths:** the state file location and the cymatix log location are
always displayed so you can tail the log or inspect the state file
without hunting for them.

## State file

`~/.cymatix/launcher/state.json` (atomic write + rename):

```json
{
  "cymatix_pid": 56792,
  "cymatix_port": 11437,
  "cymatix_start_time": 1775881217.9,
  "cymatix_command": ["python", "-m", "uvicorn", "cymatix_context.server:app", "--host", "127.0.0.1", "--port", "11437"],
  "launcher_pid": 48213,
  "launcher_start_time": 1775881200.0,
  "last_restart_reason": "session registry DAL fix",
  "last_restart_at": 1775881217.9
}
```

On launcher startup:

1. Read state file.
2. If `cymatix_pid` is set, check `psutil.pid_exists(pid)` + verify the
   process command line matches expected uvicorn invocation.
3. If alive and matching, **adopt** the process (no spawn, just track).
4. If alive but mismatch (PID reused for something else), clear and
   spawn fresh.
5. If not alive, clear and spawn fresh (unless `--no-autostart`).

This means you can restart the launcher without killing cymatix. The
launcher reattaches to the already-running cymatix on next start. Nice
UX property.

## Supervisor lifecycle

Pseudocode for `supervisor.py`:

```python
class CymatixSupervisor:
    def __init__(self, state: LauncherState, config: LauncherConfig):
        self.state = state
        self.config = config
        self._cymatix_pid: int | None = None

    def start(self) -> None:
        if self.is_running():
            raise AlreadyRunning()
        proc = subprocess.Popen(
            self._command(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=open(self.config.log_path, "ab"),
            stderr=subprocess.STDOUT,
        )
        self._cymatix_pid = proc.pid
        self.state.write(cymatix_pid=proc.pid, cymatix_start_time=time.time())
        self._wait_for_ready(timeout=30)

    def stop(self, reason: str = "manual stop from launcher") -> None:
        if not self.is_running():
            return
        self._announce_restart(reason, expected_downtime_s=10)
        time.sleep(0.75)
        self._kill_tree(self._cymatix_pid)
        self._wait_for_port_free(port=self.config.cymatix_port, timeout=10)
        self._cymatix_pid = None
        self.state.clear_cymatix()

    def restart(self, reason: str) -> None:
        self.stop(reason=reason)
        self.start()

    def is_running(self) -> bool:
        if self._cymatix_pid is None:
            return False
        if not psutil.pid_exists(self._cymatix_pid):
            self._cymatix_pid = None
            self.state.clear_cymatix()
            return False
        return True
```

### Graceful kill on Windows vs POSIX

- **Windows:** `subprocess.Popen.terminate()` sends `SIGTERM` equivalent
  but uvicorn's Windows handler is unreliable. Use `taskkill /F /T /PID
  {pid}` via subprocess for reliable tree kill.
- **POSIX:** `os.killpg(os.getpgid(pid), signal.SIGTERM)` kills the
  whole process group cleanly.

Both paths are encapsulated in `_kill_tree(pid)`.

### Window console flash

All `subprocess.Popen` calls MUST include `creationflags=getattr(
subprocess, "CREATE_NO_WINDOW", 0)` to prevent the console window from
flashing when spawning cymatix on Windows (matches the user's global
CLAUDE.md rule and the existing project pattern).

## Failure modes

| Failure | Behavior |
|---|---|
| Launcher starts, cymatix PID in state file is dead | Clear state, spawn fresh cymatix (or stay idle if `--no-autostart`) |
| Launcher starts, cymatix PID in state file is alive but wrong command | Clear state, log warning, spawn fresh cymatix on different port if old one holds :11437 |
| cymatix refuses to start (port in use, import error, bad config) | Surface error in dashboard status banner; keep Start button enabled for retry |
| cymatix crashes while launcher is running | `is_running()` returns False on next poll → UI shows "stopped" → user clicks Start |
| Launcher itself crashes | cymatix keeps running (orphaned). Next launcher start adopts it from state file. |
| cymatix `/admin/announce_restart` fails during Stop | Log warning, proceed with kill anyway (announce is best-effort) |
| psutil not installed | Launcher refuses to start with a clear error pointing at `pip install cymatix-context[launcher]` |
| Port 11438 already in use | Launcher exits with error, suggests `--port` flag |
| Poll interval to `/api/state` exceeds cymatix response time | Dashboard shows stale data with a faded style; new poll supersedes |
| User closes browser tab but launcher keeps running | cymatix keeps running; reopen browser to resume. Launcher does not self-terminate on tab close. |
| User Ctrl+C's the launcher | Launcher stops cymatix cleanly via the same announce + kill path, then exits. |

## Native window mode

Opt-in via `--native`. Requires the `launcher-native` extras. Uses
`pywebview` to wrap a WebView (WebView2 on Windows, WebKit on macOS,
GTK WebKit on Linux) pointed at `http://127.0.0.1:11438/`. The window
is a thin shim — all rendering still happens in the browser engine,
all data flows through the same FastAPI endpoints.

```python
if args.native:
    import webview
    thread = start_fastapi_in_thread()
    webview.create_window(
        "Cymatix Launcher",
        f"http://127.0.0.1:{args.port}",
        width=1000, height=720,
        resizable=True,
    )
    webview.start()
```

If `pywebview` is not installed, `--native` prints a helpful error and
points at the install extras. Default behavior (no `--native`) opens
the user's default browser at the launcher URL.

## System tray mode

Opt-in via `--tray`. Requires the `launcher-tray` extras (pystray +
Pillow). Puts a persistent icon in the system notification area with
a menu for controlling cymatix. Uvicorn runs in a daemon thread; pystray
owns the main thread for its message pump.

```bash
pip install cymatix-context[launcher-tray]
cymatix-launcher --tray
```

Tray menu:

```
┌──────────────────┐
│ Open Dashboard   │  ← default action (left-click on icon)
│──────────────────│
│ Start cymatix    │  (disabled when running)
│ Restart cymatix  │  (disabled when stopped)
│ Stop cymatix     │  (disabled when stopped)
│──────────────────│
│ Quit             │  (stops cymatix + exits launcher)
└──────────────────┘
```

In tray mode the **tray icon is the persistent surface**. You can
open and close the browser tab freely — the launcher keeps running
because the icon is alive. Only clicking **Quit** from the tray menu
actually stops the launcher (and cymatix via the normal announce-then-
kill path).

**License note:** pystray is LGPL-3. It is installed as an optional
runtime dep by the user — the cymatix-context wheel itself does not
bundle pystray, so the core package stays Apache-2.0-clean. See
`pyproject.toml` for the `launcher-tray` extras definition.

### `--tray --native` combined (Windows only)

When both flags are set on Windows, the launcher runs in **close-to-tray**
mode: a native pywebview window AND a persistent tray icon, with the
window's close button intercepted to hide the window instead of
exiting. The tray menu gains `Show Window` / `Hide to Tray` items
and `Quit` is the only way to fully stop.

Threading model:

    - Main thread      pywebview (WebView2 message pump)
    - Background       uvicorn (daemon)
    - Background       pystray (daemon, tray icon message pump)

Close-to-tray flow:

    User clicks X          →  window.events.closing returns False
                              → window.hide()
    Tray "Show Window"     →  window.show()
    Tray "Hide to Tray"    →  window.hide()
    Tray "Quit"            →  set quitting flag
                              → window.destroy()
                              → closing handler returns True
                              → webview.start() returns
                              → main() exits
                              → daemon threads (uvicorn + pystray) die with process

**Not supported on macOS or Linux** in this release:

- macOS: pystray's Cocoa backend requires main-thread + NSApplication
  event loop, which directly conflicts with pywebview's WebKit main
  loop. The two libraries cannot share the main thread.
- Linux: depends on the pystray backend (AppIndicator can run from a
  thread, Xlib cannot). Opt-in once the Linux backend story is tested.

On these platforms, passing `--tray --native` returns exit code 2
with a clear error message. Pick one or the other.

## Service install — `cymatix-launcher install-service`

Once the deploy templates are validated for your platform, the
launcher can install them for you in one command:

```bash
cymatix-launcher install-service
```

What it does (by platform):

| Platform | Action |
|---|---|
| Linux | Writes `~/.config/systemd/user/cymatix-launcher.service` with `ExecStart` substituted to the actual `cymatix-launcher` binary path. Prints the `systemctl --user daemon-reload && systemctl --user enable --now` next steps. |
| macOS | Writes `~/Library/LaunchAgents/io.cymatix.launcher.plist` with `ProgramArguments` and `StandardOutPath` substituted. Auto-migrates the pre-0.5 `com.swiftwing21.cymatix-launcher.plist` first (unload + remove, best-effort). Prints the `launchctl load` next step. |
| Windows | Prints the NSSM recipe. Does NOT install NSSM (licensing + download), does NOT register the service. User follows the printed steps. |

The installer deliberately **never runs** `systemctl enable`,
`launchctl load`, or `nssm install` — those are side effects that
deserve explicit user consent, and it makes the installer reversible.

Dry-run mode shows what would happen without writing anything:

```bash
cymatix-launcher install-service --dry-run
```

Uninstall removes the file and prints the disable command (again,
doesn't run it):

```bash
cymatix-launcher uninstall-service
```

Windows users: see [`deploy/windows/README.md`](../deploy/windows/README.md)
for the NSSM walkthrough.

## Cymatix-side endpoint: `POST /admin/shutdown`

Complements `POST /admin/announce_restart`. Where announce_restart
signals an *intentional restart*, shutdown signals a clean
*stop-and-stay-down*:

```bash
curl -X POST http://127.0.0.1:11437/admin/shutdown \
  -H "Content-Type: application/json" \
  -d '{"actor": "launcher", "reason": "user quit from tray menu"}'
```

Behavior:

1. Stamps `server_state.json` with `state=stopped`
2. Logs the shutdown reason
3. Fires `SIGINT` on the cymatix process
4. Uvicorn catches SIGINT and runs its graceful-shutdown path,
   invoking the lifespan cleanup (WAL checkpoint, token metrics flush,
   background tasks cancelled)
5. Returns `200` immediately — the actual shutdown happens
   asynchronously as uvicorn processes the signal
6. Callers poll `GET /stats` until connection refused to confirm
   the shutdown completed

This is the endpoint whose 404 prompted "someone was trying
/admin/shutdown" during an earlier test session — now it exists.

## Implementation checklist

Rough ordering for the first PR. Each is independently testable.

1. **Package scaffold.** `cymatix_context/launcher/` directory, empty
   modules, add `[launcher]` + `[launcher-native]` extras to
   `pyproject.toml`, add `cymatix-launcher` script entry.
2. **State module.** `state.py` with atomic read/write/clear of
   `~/.cymatix/launcher/state.json`.
3. **Supervisor module.** `supervisor.py` with Start / Stop / Restart
   / is_running / adopt. Unit tests with a dummy `sleep 60` child
   process.
4. **FastAPI app + CLI.** `app.py` with `main()`, argparse, browser
   launch, `/api/state` JSON endpoint, `/api/control/*` endpoints.
5. **Templates.** `layout.html` + `dashboard.html` + all panel
   components. One CSS file. (The original plan vendored HTMX here; the
   shipped dashboard uses plain JavaScript instead, see the polling section.)
6. **Dashboard polling wiring.** `/api/state/panels` server-rendered
   partial endpoint, 2s polling, conditional panel rendering (planned as
   HTMX, shipped as plain JavaScript).
7. **Cymatix data integration.** Launcher's state collector hits
   `GET /stats`, `GET /sessions`, `GET /health`, and Ollama
   `GET /api/ps`. All with timeouts.
8. **New cymatix endpoint: `GET /admin/components`.** Component
   introspection — small addition to `server.py`.
9. **Tests.** Unit tests for supervisor + state + adoption. Integration
   test with `TestClient` + a mocked subprocess.
10. **README update.** Quick Start gains a "Run it with the launcher"
    section pointing at this doc.

**Phase 2 (follow-up, not first slice):**

- Token metrics (`cymatix_context/metrics.py` + `GET /metrics/tokens`)
- Native window via `pywebview`
- Service/daemon install (systemd unit, Windows service, launchd plist)

## Related

- [`SESSION_REGISTRY.md`](SESSION_REGISTRY.md) — parties + participants
  feed the launcher's dashboard.
- [`RESTART_PROTOCOL.md`](RESTART_PROTOCOL.md) — the launcher uses
  `POST /admin/announce_restart` before every Stop/Restart so observer
  sessions (Claude panels, external agents) don't misread the outage.
- `cymatix_context/server.py::main` — existing thin entry point; stays
  unchanged. The launcher is additive.
