'use strict';

// The only native surface the dashboard gets. Each call is one ipcMain
// handler in main.js, which re-validates its arguments and checks that the
// request came from the launcher's own origin.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('cymatix', {
  installMcp: (host, lane) => ipcRenderer.invoke('cymatix:install-mcp', { host, lane }),
  copyDiagnostics: () => ipcRenderer.invoke('cymatix:copy-diagnostics'),
  openLogs: () => ipcRenderer.invoke('cymatix:open-logs'),
  pickFolder: () => ipcRenderer.invoke('cymatix:pick-folder'),
  getLaunchAtLogin: () => ipcRenderer.invoke('cymatix:get-login'),
  setLaunchAtLogin: (enabled) => ipcRenderer.invoke('cymatix:set-login', Boolean(enabled)),
});
