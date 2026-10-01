'use strict';

// Cymatix desktop: an Electron window + tray around the Python launcher,
// which runs as a headless sidecar (see sidecar.js). The window shows the
// launcher's own dashboard (?embedded=1) from 127.0.0.1, authenticated by a
// per-launch token set as a cookie; native actions go through preload.js.

const {
  app, BrowserWindow, Menu, Tray, clipboard, ipcMain, nativeImage, session, shell,
} = require('electron');
const { execFile } = require('node:child_process');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const { buildDiagnostics } = require('./diagnostics');
const { HEALTH_LABEL, laneHealth } = require('./health');
const { buildInstallCommand, validateInstall } = require('./mcp');
const { LauncherSidecar, engineCommand } = require('./sidecar');
const { ensureUserConfig } = require('./userconfig');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const BRAND_DIR = app.isPackaged
  ? path.join(process.resourcesPath, 'brand')
  : path.join(REPO_ROOT, 'assets', 'brand');
const ICONS = path.join(BRAND_DIR, 'generated');
const ENGINE_EXE = path.join(process.resourcesPath || '', 'engine',
  process.platform === 'win32' ? 'cymatix-engine.exe' : 'cymatix-engine');
const LAUNCHER_LOGS = path.join(os.homedir(), '.cymatix', 'launcher');
const TOKEN_COOKIE = 'cymatix_launcher_token';
const POLL_MS = 5000;

let win = null;
let tray = null;
let sidecar = null;
let health = null;
let lanes = [];
let lanesKey = '';
let pollTimer = null;
let quitting = false;
let shutdownDone = false;

function log(...args) {
  console.log('[cymatix-desktop]', ...args);
}

function brandIcon(name) {
  return nativeImage.createFromPath(path.join(ICONS, name));
}

function trayImage(state) {
  if (process.platform === 'darwin') {
    const img = brandIcon('tray-template-32.png').resize({ width: 16, height: 16 });
    img.setTemplateImage(true);
    return img;
  }
  return brandIcon(`tray-${state}-32.png`);
}

function isLauncherUrl(url) {
  if (!sidecar || !sidecar.baseUrl) return false;
  try {
    return new URL(url).origin === new URL(sidecar.baseUrl).origin;
  } catch {
    return false;
  }
}

// ── window ────────────────────────────────────────────────────────────

function showLoading(status, error = '') {
  if (!win) return;
  const hash = new URLSearchParams({
    status,
    error,
    splash: pathToFileURL(path.join(BRAND_DIR, 'cymatix-granulated.png')).href,
  }).toString();
  win.loadFile(path.join(__dirname, 'loading.html'), { hash }).catch((err) => log('loading page', err));
}

function createWindow() {
  win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    show: false,
    title: 'Cymatix',
    backgroundColor: '#090c10',
    icon: path.join(ICONS, process.platform === 'win32' ? 'icon.ico' : 'icon-512.png'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  // No File/Edit/View menu bar on Windows/Linux; macOS keeps its app menu.
  if (process.platform !== 'darwin') win.removeMenu();
  win.once('ready-to-show', () => win.show());
  win.on('close', (event) => {
    if (!quitting) { // close to tray
      event.preventDefault();
      win.hide();
    }
  });
  // Origin lock: the window only ever shows the launcher or the local
  // loading page; any other http(s) link opens in the user's browser.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url) && !isLauncherUrl(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (event, url) => {
    if (isLauncherUrl(url) || url.startsWith('file:')) return;
    event.preventDefault();
    if (/^https?:/i.test(url)) shell.openExternal(url);
  });
  showLoading('Starting Cymatix…');
}

function showWindow() {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

// ── engine ────────────────────────────────────────────────────────────

async function onEngineReady() {
  const base = sidecar.baseUrl;
  log('launcher ready at', base);
  await session.defaultSession.cookies.set({
    url: base, name: TOKEN_COOKIE, value: sidecar.token, httpOnly: true, sameSite: 'strict',
  });
  if (win) await win.loadURL(`${base}/?embedded=1`);
  poll();
}

async function startEngine() {
  const userData = app.getPath('userData');
  if (app.isPackaged) ensureUserConfig(userData);
  sidecar = new LauncherSidecar(engineCommand({
    packaged: app.isPackaged,
    repoRoot: REPO_ROOT,
    python: process.env.CYMATIX_PYTHON,
    resourcesPath: process.resourcesPath,
    userData,
    platform: process.platform,
  }));
  sidecar.on('ready', () => onEngineReady().catch((err) => log('ready handling failed', err)));
  sidecar.on('restarting', ({ delay }) => {
    setHealth('error');
    showLoading(`Cymatix stopped. Restarting in ${Math.round(delay / 1000)} s…`,
      sidecar.stderrTail().split('\n').slice(-12).join('\n'));
  });
  sidecar.on('error', (err) => log('sidecar error', err.message));
  sidecar.on('log', (msg) => log(msg));
  try {
    await sidecar.start();
  } catch (err) {
    showLoading('Cymatix could not start.', String(err.message || err));
  }
}

// ── health: tray icon, badge, lanes menu ──────────────────────────────

function setHealth(state) {
  if (state === health) return;
  health = state;
  if (tray) {
    tray.setImage(trayImage(state));
    tray.setToolTip(`Cymatix: ${HEALTH_LABEL[state]}`);
  }
  if (process.platform === 'win32' && win) {
    win.setOverlayIcon(state === 'ok' ? null : brandIcon(`badge-${state}.png`), HEALTH_LABEL[state]);
  } else if (process.platform === 'darwin' && app.dock) {
    app.dock.setBadge(state === 'ok' ? '' : '!');
  } else {
    app.setBadgeCount(state === 'ok' ? 0 : 1);
  }
}

async function poll() {
  if (!pollTimer) pollTimer = setInterval(poll, POLL_MS);
  try {
    const body = await sidecar.request('GET', '/api/lanes');
    lanes = Array.isArray(body.lanes) ? body.lanes : [];
    setHealth(laneHealth(lanes));
  } catch {
    lanes = [];
    setHealth('error');
  }
  const key = JSON.stringify(lanes.map((l) => [l.name, l.port, l.running]));
  if (key !== lanesKey) {
    lanesKey = key;
    buildTrayMenu();
  }
}

function laneAction(name, action) {
  sidecar.request('POST', `/api/control/lanes/${encodeURIComponent(name)}/${action}`)
    .catch((err) => log(`lane ${name} ${action} failed`, err.message))
    .finally(poll);
}

function buildTrayMenu() {
  if (!tray) return;
  const laneItems = lanes.length ? lanes.map((l) => ({
    label: `${l.name} :${l.port}  (${l.running ? 'running' : 'stopped'})`,
    submenu: [
      { label: 'Start', enabled: !l.running, click: () => laneAction(l.name, 'start') },
      { label: 'Restart', enabled: l.running, click: () => laneAction(l.name, 'restart') },
      { label: 'Stop', enabled: l.running, click: () => laneAction(l.name, 'stop') },
    ],
  })) : [{ label: 'Launcher not reachable', enabled: false }];
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Open Cymatix', click: showWindow },
    { type: 'separator' },
    { label: 'Lanes', submenu: laneItems },
    { label: 'Copy diagnostics', click: () => copyDiagnostics().catch((e) => log(e)) },
    { label: 'Open logs folder', click: () => shell.openPath(LAUNCHER_LOGS) },
    { type: 'separator' },
    { label: 'Quit Cymatix', click: () => { quitting = true; app.quit(); } },
  ]));
}

function createTray() {
  tray = new Tray(trayImage('error'));
  tray.setToolTip('Cymatix');
  tray.on('click', showWindow);
  buildTrayMenu();
}

// ── native actions (preload bridge) ───────────────────────────────────

async function copyDiagnostics() {
  const [state, lanesBody] = await Promise.all([
    sidecar.request('GET', '/api/state').catch((err) => ({ error: err.message })),
    sidecar.request('GET', '/api/lanes').catch((err) => ({ error: err.message })),
  ]);
  clipboard.writeText(buildDiagnostics({
    versions: {
      app: app.getVersion(),
      electron: process.versions.electron,
      chrome: process.versions.chrome,
      platform: `${process.platform}-${process.arch}`,
      packaged: app.isPackaged,
    },
    state,
    lanes: lanesBody.lanes || lanesBody,
    stderrTail: sidecar.stderrTail(),
    token: sidecar.token,
  }));
  return { message: 'Diagnostics copied to the clipboard.' };
}

function installMcp(req) {
  const problem = validateInstall(req);
  if (problem) return Promise.reject(new Error(problem));
  const cmd = buildInstallCommand(req, {
    packaged: app.isPackaged,
    python: process.env.CYMATIX_PYTHON,
    repoRoot: REPO_ROOT,
    engine: ENGINE_EXE,
  });
  return new Promise((resolve, reject) => {
    execFile(cmd.command, cmd.args, {
      cwd: app.isPackaged ? app.getPath('userData') : REPO_ROOT,
      env: { ...process.env, ...cmd.env },
      windowsHide: true,
      timeout: 60000,
    }, (err, stdout, stderr) => {
      if (err) return reject(new Error((stderr || err.message).trim().split('\n').slice(-3).join(' ')));
      const lines = stdout.trim().split('\n');
      return resolve({ message: lines.slice(0, 1).concat(lines.slice(-1)).join(' ') });
    });
  });
}

function registerIpc() {
  const guard = (handler) => async (event, ...args) => {
    // Only the launcher page in our own window may call native actions.
    if (!event.senderFrame || !isLauncherUrl(event.senderFrame.url)) {
      throw new Error('not allowed from this page');
    }
    return handler(...args);
  };
  ipcMain.handle('cymatix:install-mcp', guard((req) => installMcp(req)));
  ipcMain.handle('cymatix:copy-diagnostics', guard(() => copyDiagnostics()));
  ipcMain.handle('cymatix:open-logs', guard(async () => {
    const err = await shell.openPath(LAUNCHER_LOGS);
    if (err) throw new Error(err);
    return { message: `Opened ${LAUNCHER_LOGS}` };
  }));
  ipcMain.handle('cymatix:get-login', guard(() => app.getLoginItemSettings().openAtLogin));
  ipcMain.handle('cymatix:set-login', guard((enabled) => {
    app.setLoginItemSettings({ openAtLogin: Boolean(enabled) });
    return { message: enabled ? 'Cymatix will start at login.' : 'Start at login turned off.' };
  }));
}

// ── lifecycle ─────────────────────────────────────────────────────────

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', showWindow);

  app.whenReady().then(() => {
    if (process.platform === 'win32') app.setAppUserModelId('com.cymatixcontext.desktop');
    // The dashboard needs no camera, mic, notifications or geolocation.
    session.defaultSession.setPermissionRequestHandler((_wc, _perm, callback) => callback(false));
    registerIpc();
    createWindow();
    createTray();
    setHealth('error');
    startEngine();
    if (app.isPackaged) {
      try {
        require('electron-updater').autoUpdater.checkForUpdatesAndNotify()
          .catch((err) => log('update check failed', err.message));
      } catch (err) {
        log('electron-updater unavailable', err.message);
      }
    }
  });

  app.on('activate', showWindow);
  // Closing the window hides it; the tray keeps the app (and lanes) alive.
  app.on('window-all-closed', () => {});

  app.on('before-quit', (event) => {
    if (shutdownDone) return;
    event.preventDefault();
    quitting = true;
    if (pollTimer) clearInterval(pollTimer);
    const stop = sidecar ? sidecar.stop() : Promise.resolve();
    stop.catch((err) => log('shutdown failed', err.message)).finally(() => {
      shutdownDone = true;
      app.quit();
    });
  });
}
