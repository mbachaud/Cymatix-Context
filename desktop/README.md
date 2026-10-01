# Cymatix desktop

> **Experimental.** A first desktop shell around the existing launcher.
> Signing and auto-update are wired but not yet exercised end to end.

An Electron window and tray around the Cymatix launcher. Nothing in the
engine is rewritten: the app runs the Python launcher as a child process
(the *sidecar*) and shows the launcher's own dashboard.

```
Electron main ──spawn──▶ cymatix-engine launcher --headless --port 0
     │                     │  prints CYMATIX_LAUNCHER_READY {"port": …}
     │                     └─ supervises the lanes (stable 11437, bench, staging…)
     ├─ BrowserWindow ──▶ http://127.0.0.1:<port>/?embedded=1   (token cookie)
     ├─ Tray + badge  ◀── GET /api/lanes every 5 s
     └─ preload bridge (window.cymatix): MCP setup, diagnostics, logs, login item
```

## How it fits together

- **Sidecar** (`src/sidecar.js`): starts the launcher on a free port with a
  per-launch token passed by environment variable. It waits for the READY
  line and restarts the launcher with backoff if it dies. On quit it calls
  `POST /api/shutdown`, which stops the lanes this launcher started and
  leaves adopted ones running.
- **Window**: the launcher dashboard in embedded mode, which adds a
  collapsible rail of native actions. Settings are `contextIsolation`,
  `sandbox` and no `nodeIntegration`. Navigation is locked to the launcher's
  origin, and other links open in your browser.
- **Trust boundary.** The page talks to the launcher over loopback HTTP,
  authenticated by the per-launch token held in an httpOnly cookie. This
  deliberately departs from "renderer only through ipcMain" so the whole
  existing dashboard can be reused. Only native actions go through IPC,
  and each IPC handler checks that the call comes from the launcher's
  origin.
- **Tray and badge** (`src/health.js`):
  - green when everything that should run is running;
  - amber when an autostart lane is stopped;
  - red when the launcher or the primary lane is down.

  The state is shown as the tray icon, the Windows taskbar overlay, the
  macOS dock badge, or the Linux badge count.
- **Connect a chat**: runs `cymatix mcp install --host … --lane …`. In
  packaged builds the written entry starts the bundled engine
  (`cymatix-engine mcp`), so chats work without Python.
- **Data**: packaged builds keep `cymatix.toml` and every store in the app's
  user-data folder, never inside the app bundle, so updates cannot wipe them.

## Develop

From a checkout with the launcher extra installed (`pip install -e ".[launcher]"`):

```bash
cd desktop
npm install
npm test
npm start
```

`npm start` runs the repo's launcher through `python` (set `CYMATIX_PYTHON`
to use another interpreter).

## Package

```bash
pip install pyinstaller
npm run build:engine
npm run dist
```

CI (`.github/workflows/desktop.yml`) builds win-x64, mac-arm64, mac-x64,
linux-x64 and linux-arm64 on a `desktop-v*` tag. Signing (Azure Trusted
Signing, Apple Developer ID + notarization) runs only when its secrets are
configured. Updates use `electron-updater` with GitHub Releases.

Icons come from `assets/brand/` (see its README). They are rendered by
`scripts/build_brand_icons.py` and never edited by hand.
