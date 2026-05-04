# Native Observability Sidecar — Design Spec

**Date:** 2026-05-04
**Status:** Draft, awaiting review
**Author:** max + Claude (brainstormed 2026-05-04)
**Tracking PR:** TBD

## 1. Problem

The helix-context observability stack — OTel Collector, Prometheus, Tempo, Loki, Grafana — ships today as a Docker Compose deployment at [`deploy/otel/docker-compose.yml`](../../deploy/otel/docker-compose.yml). Running it requires Docker Desktop, which is a 2GB+ install with licensing friction for a Windows desktop tool whose primary user touches it via a system tray icon. External feedback (Jed Thompson, 2026-05-03) flagged this as the single biggest install-friction blocker.

The Docker pipeline itself works correctly — helix-context's existing OTel exporter sends to `localhost:4317`, signals reach Prometheus and dashboards render in Grafana. The problem is the Docker dependency, not the observability shape.

## 2. Goal

Replace the Docker Compose stack with native binaries running as subprocesses of the helix tray launcher. Docker becomes optional, kept as an alternate path for users who specifically want a containerized deployment (production-shape testing, fallback for environments where native binaries don't fit). Wire format, ports, and dashboard provisioning are unchanged — only the receiver runtime swaps.

## 3. Non-goals

- Cross-platform validation. Implementation is *capable* of running on macOS/Linux (Python launcher, multi-platform binary download), but only tested on Windows 11. Validation on other OSes is follow-up work.
- Auto-update of native binaries. Updates happen by re-running the install script after bumping pinned versions.
- Windows service registration / install-as-service flow. Adding now would conflate scope.
- Migration of user-edited Grafana state. User confirmed all dashboard work happens at the JSON-file level (committed to `deploy/otel/grafana/dashboards/`), so the user-state-in-Docker-volume path is not in play.

## 4. Architecture

```
┌────────────────────────────────────────────────────┐
│  Start-helix-tray.bat  (Windows entry point)       │
│  └─→ Python tray launcher                          │
│       ├─ helix-context server (existing)           │
│       ├─ OTel collector binary  (NEW: subprocess)  │
│       ├─ Prometheus binary       (NEW: subprocess) │
│       ├─ Tempo binary            (NEW: subprocess) │
│       ├─ Loki binary             (NEW: subprocess) │
│       └─ Grafana binary          (NEW: subprocess) │
│                                                     │
│  All children grouped under a Windows Job Object   │
│  → tray quit (clean or abnormal) → all children    │
│  terminate.                                        │
└────────────────────────────────────────────────────┘
```

Helix-context's exporter sends OTel signals to `localhost:4317` exactly as today. The receiver is now a native binary instead of a container. Wire format, OTLP/gRPC port, Prometheus scrape, dashboard provisioning, and the OTel pipeline shape are all unchanged. Datasource and component config files are *templated* (hostnames + state-dir paths only — no structural diff); see §5 + §6.3.

## 5. File layout

### Tracked in repo

```
deploy/otel/
  docker-compose.yml           # UNCHANGED — alternate install path
  otel-collector-config.yaml   # SOURCE — Docker uses verbatim; native uses as template (see §6.3)
  prometheus.yml               # SOURCE — Docker uses verbatim; native uses as template (see §6.3)
  tempo.yaml                   # SOURCE — Docker uses verbatim; native uses as template (see §6.3)
  loki-config.yaml             # NEW — explicit config required for native (Docker image
                               #       previously used built-in default; we make it explicit
                               #       so both runtimes share one source of truth)
  grafana/
    dashboards/                # UNCHANGED — provisioned dashboards (JSON, runtime-agnostic)
    provisioning/
      dashboards/              # UNCHANGED — dashboard provisioning YAML
      datasources/             # SOURCE — Docker uses verbatim; native templates the
                               #          datasource URLs (Docker DNS → localhost)

tools/native-otel/
  .versions                    # NEW — pinned versions + SHA256 per platform
  configs/                     # NEW — rendered runtime configs (output of §6.3 templating
                               #       step; each file mirrors a deploy/otel source with
                               #       hostnames + paths substituted for native runtime)
  README.md                    # NEW — explains layout, points at install script

scripts/
  install-native-observability.ps1   # NEW — Windows install script
  install-native-observability.sh    # NEW — Linux/macOS install script (capable, untested)

helix_context/launcher/
  observability_supervisor.py  # NEW — owns the 5 subprocess lifecycles
  observability_health.py      # NEW — port-bind / HTTP health checks
```

**Why configs are templated, not "unchanged":** the `deploy/otel/*.yaml` files in
their current form bake in Docker-Compose service-DNS hostnames (`tempo:4317`,
`http://prometheus:9090/api/v1/write`, `http://loki:3100/otlp`, `otel-collector:8889`)
and Linux-container absolute paths (`/var/tempo/traces`, `/var/tempo/wal`,
`/var/tempo/generator/wal`). These work for Docker but not for native binaries
running on the host. The native install path templates these to `localhost:*` URLs
and `%LOCALAPPDATA%\helix-context\observability\<service>\…` paths during the
install script's render step. Docker keeps reading the originals byte-for-byte;
native reads the rendered copies in `tools/native-otel/configs/`. Wire format,
ports, dashboards, and OTel pipeline shape are bit-for-bit identical between the
two runtimes — only host/path strings differ.

### Gitignored, populated by install script

```
tools/native-otel/
  collector/otelcol-contrib.exe
  prometheus/prometheus.exe
  tempo/tempo.exe
  loki/loki.exe
  grafana/bin/grafana-server.exe
```

`.gitignore` adds `tools/native-otel/{collector,prometheus,tempo,loki,grafana}/`.

### Per-user state (not in repo)

```
%LOCALAPPDATA%\helix-context\observability\
  prometheus\          # TSDB
  tempo\               # traces
  loki\                # logs
  grafana\             # SQLite (mostly empty — provisioning is file-based)
  launcher.log         # supervisor log: spawn/exit/health events per service
```

On Linux: `~/.local/share/helix-context/observability/`. On macOS: `~/Library/Application Support/helix-context/observability/`. Resolved via `platformdirs.user_data_dir()`.

## 6. Bootstrap script (`install-native-observability.ps1`)

### Behavior

1. Read `tools/native-otel/.versions` (TOML or simple key=value):
   ```
   otelcol-contrib  = "0.105.0"
   prometheus       = "2.54.1"
   tempo            = "2.6.0"
   loki             = "3.2.0"
   grafana          = "11.3.0"
   ```
2. For each component:
   - Compute target binary path (`tools/native-otel/<service>/<exe>`).
   - If present and SHA256 matches the platform-specific hash in `.versions` → skip.
   - Otherwise: download release tarball/zip from the official release URL, verify SHA256, extract to `tools/native-otel/<service>/`, log "installed" or "updated".
   - SHA256 mismatch → fail loud, leave previous binary untouched.
3. After all components installed, render runtime configs from the
   `deploy/otel/` sources into `tools/native-otel/configs/<service>.yaml`,
   substituting:
   - **Hostnames** — Docker service DNS (`tempo`, `prometheus`, `loki`,
     `otel-collector`) → `localhost`. Affects `otel-collector-config.yaml`
     (exporters), `prometheus.yml` (scrape targets), `tempo.yaml`
     (`metrics_generator.storage.remote_write` URL), and the Grafana
     provisioning datasources YAML.
   - **State-dir paths** — Linux container paths (`/var/tempo/...`,
     `/var/loki/...`) → platform-appropriate user-state directory resolved
     via `platformdirs.user_data_dir("helix-context")` (`%LOCALAPPDATA%\
     helix-context\observability\<service>\` on Windows, equivalents on
     Linux/macOS — see §5).
   The render is a straight string-template substitution (Jinja2 already a
   `launcher` extra dep, so reuse it). No structural changes to any config —
   the diff between Docker source and native render is hostnames + paths only.

### Pinned hashes

`.versions` carries SHA256 for `windows_amd64`, `linux_amd64`, `darwin_arm64`, `darwin_amd64`. Hashes match the values published by each project's release page. The script picks the hash matching the runtime platform.

### Idempotency

Re-runs are safe. Bumping a version in `.versions` and re-running upgrades only that component. No uninstall flow — manual `Remove-Item` of `tools/native-otel/<service>/` if needed.

## 7. Tray launcher integration

### Lifecycle

`Start-helix-tray.bat` already runs the Python tray launcher. We extend the launcher's startup path:

1. **First-launch detection.** If `tools/native-otel/` is missing, any
   component binary absent, OR any rendered config in
   `tools/native-otel/configs/` absent, prompt the user: *"Native
   observability is not installed. Run `scripts/install-native-observability.ps1`
   now? (Y/n)"*. On accept, run the script and continue. On decline, set
   observability state to "skipped" and continue. (The render step in §6.3
   is part of "installed" — supervisor refuses to spawn a binary whose
   config wasn't rendered, so we don't get a half-installed state.)

2. **Port pre-flight.** For each service, check whether its port is already bound:
   - Collector: 4317 (OTLP/gRPC), 4318 (OTLP/HTTP), 8889 (Prom scrape)
   - Prometheus: 9090
   - Tempo: 3200
   - Loki: 3100
   - Grafana: 3000

   If a port is already bound, treat the existing instance as authoritative; skip spawning that service. Log "external instance detected on :PORT; not spawning."

3. **Spawn order.** Phase 1: parallel-spawn `prometheus`, `tempo`, `loki`. Phase 2: wait for all three to be ready (port-bind poll, 1s interval, 30s timeout). Phase 3: spawn `collector`. Phase 4: wait for collector ready. Phase 5: spawn `grafana`.

4. **Subprocess invocation.** Each binary launched with:
   - **Windows:** `subprocess.Popen(args, creationflags=CREATE_NO_WINDOW)` — required to suppress console window flash, per the project's Windows subprocess convention.
   - **Linux/macOS:** `subprocess.Popen(args, start_new_session=True)` — creates new POSIX process group for clean signal-cascade on cleanup.
   - stdout/stderr redirected to `%LOCALAPPDATA%/helix-context/observability/<service>.log` (rotated at 10MB, last 3 retained).

5. **Process supervision.** A single `ObservabilitySupervisor` class in `helix_context/launcher/observability_supervisor.py` owns all 5 child PIDs. Tray exposes:
   - Tray menu: `Observability ▸ Status` (per-service green/red dot), `Observability ▸ Restart [service]`, `Observability ▸ Open log directory`.
   - Internal: `supervisor.shutdown()` called from tray Quit handler. Sends SIGTERM equivalent to each child, waits 5s, escalates to SIGKILL.

6. **Cleanup guarantee (Windows).** Children are added to a Windows Job Object created with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Tray death — clean exit, force-quit, or crash — automatically cleans up all children at the OS level. Linux/macOS uses POSIX process groups for the same guarantee.

7. **Health checks during run.** Supervisor polls each service's health endpoint (`/-/healthy` for Prom, `/ready` for Tempo, `/ready` for Loki, `/api/health` for Grafana, `/health` already supported by collector via the `health_check` extension) every 30 seconds. Failure → log + tray indicator turns red. No auto-restart in v1; right-click → "Restart [service]" is manual.

### Failure modes

- **Bootstrap declined or failed.** Helix continues without observability. Tray status shows "Observability: skipped (run install script to enable)". OTel exporter inside helix-context drops signals silently (existing behavior, non-blocking).
- **Per-service start failure.** Logged to launcher.log + tray log. Other services proceed. Tray menu shows red dot for the failed service. helix-context starts normally.
- **Port collision unresolvable.** External instance assumed authoritative; service not spawned. Tray menu shows "external" indicator (different from green/red — informational only).
- **Crash mid-run.** Logged. Tray indicator goes red. User can right-click → "Restart [service]".

### Opt-out

`HELIX_OBSERVABILITY=0` env var (set in shell, or by adding `set
"HELIX_OBSERVABILITY=0"` to `Start-helix-tray.bat` alongside the existing
`HELIX_OTEL_*` block — the var is not currently set in that file): skip all
native observability auto-start logic, including the first-launch prompt.
Existing Docker-compose path is unaffected by this var. Existing
`HELIX_OTEL_ENABLED=0` (which gates the helix-context exporter itself,
already implemented in `helix_context/telemetry.py`) and the new
`HELIX_OBSERVABILITY=0` are independent: the former silences the producer,
the latter skips spawning the receiver stack. Either alone is sufficient
for "no observability"; both are honored.

## 8. Documentation

### README changes ([README.md](../../README.md))

Add a new subsection under existing "Quick Start ▸ Launch":

> #### Native observability (default)
> First launch prompts to install ~500MB of native binaries (Prometheus, Tempo, Loki, Grafana, OTel Collector) into `tools/native-otel/`. Tray manages their lifecycle — quit the tray to stop everything. Right-click the tray icon → Observability ▸ Status to see per-service health.
>
> To skip: `set HELIX_OBSERVABILITY=0` before running `Start-helix-tray.bat`.

Existing Docker-compose instructions move to a footnote:

> **Advanced — Docker stack.** For production-shape deployment, multi-host setups, or environments where native binaries don't fit, `docker-compose up -d` in `deploy/otel/` runs the same stack containerized. Wire format, ports, and dashboard provisioning are identical. See [deploy/otel/README.md](../../deploy/otel/README.md) for details.

### `deploy/otel/README.md` (new)

Short doc explaining: this is the alternate Docker path; same configs, same dashboards, same ports as the native sidecar; useful for production / fallback.

### `tools/native-otel/README.md` (new)

Layout explanation, install script invocation, version-update procedure, where state lives.

## 9. Test plan

### Unit
- `tests/test_install_observability.py` — mock release-URL HTTP, inject corrupt download, assert SHA256 verification fails loud and leaves prior binary untouched.
- `tests/test_observability_config_render.py` — feed each `deploy/otel/*.yaml` source through the render step, assert: (a) every Docker DNS hostname is rewritten to `localhost`, (b) every Linux container path is rewritten to a platform-appropriate user-state path, (c) the rendered YAML still parses and preserves the structural diff with the source as exactly hostnames + paths (no other keys touched). Catches accidental config drift on either runtime.
- `tests/test_observability_supervisor.py` — mock `subprocess.Popen`, assert spawn order, assert Job Object setup on Windows, assert cleanup cascade on shutdown, assert refusal to spawn when rendered config is absent (per §7.1).
- `tests/test_observability_health.py` — port-bind poll behavior, HTTP-endpoint poll behavior, timeout boundaries.

### Integration (Windows-only)
1. **Clean-machine first launch.** Fresh checkout, no `tools/native-otel/`, no Docker. Run `Start-helix-tray.bat`. Verify: install script prompted, accept-path installs all 5 binaries, tray launches with all green status, helix-context emits a metric, metric visible at `http://localhost:8889/metrics` and in a Grafana panel.
2. **Re-launch.** Quit tray, re-run. Verify all binaries skip download (already present), services come back up cleanly.
3. **Port-collision.** Manually start a separate Prometheus on :9090, then run tray. Verify supervisor logs "external instance detected on :9090", does not spawn its own Prometheus, other services start normally.
4. **Per-service failure.** Replace one binary with a deterministic-failure stand-in (zero-byte file, or script that exits 1 — see §11.6 for the open question on which to pick), launch. Verify supervisor logs the spawn error, marks service red, helix-context starts normally.
5. **Opt-out.** `set HELIX_OBSERVABILITY=0`, launch. Verify no observability process spawned, no install prompt, helix-context starts normally.
6. **Docker-compose path still works.** `cd deploy/otel && docker-compose up -d`. Verify identical behavior to today.

### Bench regression (gate)
Re-run a short GPQA suite (n=20, mode=on) with native observability vs the existing Docker-compose path. Assert p95 latency delta ≤ 5s (within noise). Confirms the runtime-receiver swap doesn't introduce latency in helix-context's hot path.

## 10. Rollout

1. Land this PR with native observability as the default for new installs.
2. README points new users to `Start-helix-tray.bat`; existing Docker users explicitly know the compose path is preserved.
3. No migration required. Users who don't run the install script keep using Docker exactly as before — the code paths don't conflict.
4. Telemetry confirms native vs Docker adoption (collector reports a `helix_observability_runtime` label = `native|docker|skipped`, populated by the launcher at startup).

## 11. Open questions / risks

1. **Grafana Windows binary licensing.** Grafana is AGPL; redistributing the binary in our repo would propagate the license. The bootstrap script downloads from `grafana.com/grafana/download` at install time — user-side download, not redistribution by us. Confirm this is the right interpretation before committing.
2. **Loki on Windows is less battle-tested** than the other components. If startup is flaky we may degrade to "Loki disabled by default, opt-in via env var." Defer this decision to bench validation. Related: native Loki requires an explicit config file (Docker uses the image's built-in default at `/etc/loki/local-config.yaml`); we add `deploy/otel/loki-config.yaml` so both runtimes share the same source — confirm during plan-writing that the docker-compose path is updated to mount this file (preserves "zero functional change" intent).
3. **Job Object behavior with Python.** `pywin32` exposes Job Object APIs; need to verify the kill-on-close flag actually fires when the tray Python process is force-killed (not just on clean exit). Test in integration phase. `pywin32` is not currently a dep — added under a new `launcher-observability` extra (or rolled into `launcher-tray`); plan-writing should pick the placement.
4. **First-launch prompt UX.** A blocking "Y/n" prompt in a tray-context is awkward. Likely better as a balloon notification or a tray-menu pulse-state until the user clicks "Install observability." Decide during plan-writing.
5. **New dependencies introduced.** `platformdirs` (state-dir resolution, cross-platform), `pywin32` (Job Object APIs, Windows-only — guard import behind `sys.platform == "win32"` per global preference). Both are net-new to the project. Plan-writing should decide which optional-dependency extra (`launcher`, `launcher-tray`, or a new `launcher-observability`) carries them.
6. **Test plan integration item 4 ("corrupt one binary on disk").** A corrupt exe on Windows often fails at process-start with an opaque OS error rather than a clean spawn failure that the supervisor can classify. Plan-writing should pick a deterministic failure mode for this test (e.g., zero-byte file, or replace exe with a script that exits 1) so the supervisor's red-dot path is exercised reliably.

## 12. Related work

- ABSTAIN tier (PR #15, merged 2026-05-04) — recent observability-adjacent ship.
- Foveated splice spec ([2026-05-03](2026-05-03-foveated-splice-design.md)) — next PR after this one.
- Code-aware extractor (Jed's incoming branch) — separate effort, no overlap.

## 13. Review checklist for spec reviewer

- [ ] Architecture preserves OTel wire format, ports, and dashboard provisioning bit-for-bit
- [ ] File layout split (binaries/state/provisioning) is consistent with the file-layout decisions
- [ ] Config templating story is clear: which files are byte-identical between Docker and native, which are templated, what gets substituted (§5 + §6.3)
- [ ] First-launch UX described concretely (prompt vs notification — open question §11.4)
- [ ] Cross-platform claim is bounded (capable, untested) and §3 non-goals match
- [ ] Test plan covers: install verification, supervisor lifecycle, port collision, per-service failure, opt-out
- [ ] Docker-compose path explicitly preserved with zero functional changes (including: if `loki-config.yaml` is added, docker-compose mounts it so behavior remains identical)
- [ ] AGPL/Grafana redistribution concern flagged (§11.1)
- [ ] New dependencies (`platformdirs`, `pywin32`) and their extra placement called out (§11.5)
