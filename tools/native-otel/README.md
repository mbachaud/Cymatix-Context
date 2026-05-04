# Native observability binaries

Layout:

```
tools/native-otel/
  .versions              pinned versions + SHA256 per platform (TOML)
  configs/               rendered runtime configs (gitignored except .gitkeep)
  collector/             otelcol-contrib binary  (gitignored)
  prometheus/            prometheus + promtool   (gitignored)
  tempo/                 tempo binary            (gitignored)
  loki/                  loki binary             (gitignored)
  grafana/               grafana-server binary   (gitignored)
```

## Install

Windows (PowerShell):

```powershell
scripts/install-native-observability.ps1
```

Linux/macOS:

```bash
bash scripts/install-native-observability.sh
```

The install script reads `.versions`, downloads each binary from the
official release page, verifies SHA256 against the platform-specific
hash, extracts to the per-service folder, and renders runtime configs
(host + path substitution) into `configs/`.

Idempotent — re-runs skip components whose binary hash matches the pinned
value. Bumping a version in `.versions` and re-running upgrades only that
component.

## State

Per-user observability state (TSDB, traces, logs) lives outside the repo
under `platformdirs.user_data_dir("helix-context") / "observability"`:

- Windows: `%LOCALAPPDATA%\helix-context\observability\`
- Linux:   `~/.local/share/helix-context/observability/`
- macOS:   `~/Library/Application Support/helix-context/observability/`

## Uninstall

`Remove-Item -Recurse tools/native-otel/{collector,prometheus,tempo,loki,grafana}`
(or the equivalent `rm -rf` on POSIX). The `configs/` dir clears itself
on next install. Per-user state at `platformdirs.user_data_dir(...)/observability`
is left in place — delete manually if you want a fully clean slate.

See `docs/specs/2026-05-04-native-observability-sidecar-design.md`.
