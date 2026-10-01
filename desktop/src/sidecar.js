'use strict';

// The Python launcher as a child process: start it headless on a free port
// with a per-launch token, wait for its READY line, restart it with backoff
// if it dies, and shut it down cleanly (POST /api/shutdown, then kill).

const { spawn } = require('node:child_process');
const crypto = require('node:crypto');
const { EventEmitter } = require('node:events');
const path = require('node:path');
const readline = require('node:readline');

const READY_PREFIX = 'CYMATIX_LAUNCHER_READY ';
const STDERR_TAIL_LINES = 200;
const STABLE_AFTER_MS = 60000; // a run this long resets the backoff

function parseReadyLine(line) {
  if (typeof line !== 'string' || !line.startsWith(READY_PREFIX)) return null;
  try {
    const info = JSON.parse(line.slice(READY_PREFIX.length));
    return Number.isInteger(info.port) && info.port > 0 ? info : null;
  } catch {
    return null;
  }
}

function backoffMs(attempt) {
  return Math.min(30000, 1000 * 2 ** attempt);
}

// How to run the launcher. Dev: the repo checkout through Python. Packaged:
// the bundled PyInstaller engine, working directory = userData (where the
// app seeds cymatix.toml and the stores live, outside the app bundle).
function engineCommand({ packaged, repoRoot, python, resourcesPath, userData, platform }) {
  const launcherArgs = ['launcher', '--headless', '--host', '127.0.0.1', '--port', '0'];
  if (packaged) {
    const exe = platform === 'win32' ? 'cymatix-engine.exe' : 'cymatix-engine';
    return {
      command: path.join(resourcesPath, 'engine', exe),
      args: launcherArgs,
      cwd: userData,
      env: {},
    };
  }
  return {
    command: python || 'python',
    args: ['-m', 'cymatix_context.desktop_entry', ...launcherArgs],
    cwd: repoRoot,
    env: { PYTHONPATH: repoRoot },
  };
}

class LauncherSidecar extends EventEmitter {
  constructor({
    command, args, cwd, env = {},
    token = crypto.randomBytes(24).toString('hex'),
    spawnFn = spawn,
    fetchFn = globalThis.fetch,
    setTimeoutFn = setTimeout,
    clearTimeoutFn = clearTimeout,
    requestTimeoutMs = 15000,
  }) {
    super();
    Object.assign(this, { command, args, cwd, env, token, spawnFn, fetchFn,
      setTimeoutFn, clearTimeoutFn, requestTimeoutMs });
    this.child = null;
    this.port = null;
    this.stopping = false;
    this.attempt = 0;
    this.restartTimer = null;
    this._stderr = [];
  }

  get baseUrl() {
    return this.port ? `http://127.0.0.1:${this.port}` : null;
  }

  stderrTail() {
    return this._stderr.join('\n');
  }

  // Spawn the launcher; resolves with the READY info, rejects if it exits
  // first. Later restarts emit 'ready' again.
  start() {
    this.stopping = false;
    this.port = null;
    const startedAt = Date.now();
    const child = this.spawnFn(this.command, this.args, {
      cwd: this.cwd,
      // The token travels by environment, never argv (visible in process lists).
      env: { ...process.env, ...this.env, CYMATIX_LAUNCHER_TOKEN: this.token },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
    this.child = child;

    return new Promise((resolve, reject) => {
      let settled = false;
      readline.createInterface({ input: child.stdout }).on('line', (line) => {
        const info = parseReadyLine(line);
        if (info && !this.port) {
          this.port = info.port;
          this.emit('ready', info);
          if (!settled) { settled = true; resolve(info); }
        }
      });
      readline.createInterface({ input: child.stderr }).on('line', (line) => {
        this._stderr.push(line);
        if (this._stderr.length > STDERR_TAIL_LINES) this._stderr.shift();
      });
      child.on('error', (err) => {
        if (!settled) { settled = true; reject(err); }
        this.emit('error', err);
      });
      child.on('exit', (code, signal) => {
        this.child = null;
        this.port = null;
        this.emit('exit', { code, signal });
        if (!settled) {
          settled = true;
          reject(new Error(`launcher exited before READY (code ${code}): ${this.stderrTail().slice(-800)}`));
        }
        if (this.stopping) return;
        if (Date.now() - startedAt > STABLE_AFTER_MS) this.attempt = 0;
        const delay = backoffMs(this.attempt++);
        this.emit('restarting', { delay });
        this.restartTimer = this.setTimeoutFn(() => {
          this.restartTimer = null;
          this.start().catch((err) => this.emit('error', err));
        }, delay);
      });
    });
  }

  async request(method, urlPath, body) {
    if (!this.baseUrl) throw new Error('launcher is not running');
    const headers = { Authorization: `Bearer ${this.token}` };
    const init = { method, headers, signal: AbortSignal.timeout(this.requestTimeoutMs) };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const res = await this.fetchFn(`${this.baseUrl}${urlPath}`, init);
    if (!res.ok) throw new Error(`${method} ${urlPath} -> HTTP ${res.status}`);
    return res.json();
  }

  // Ask the launcher to stop the lanes it owns and exit; kill it if it has
  // not exited within timeoutMs.
  async stop({ timeoutMs = 20000 } = {}) {
    this.stopping = true;
    if (this.restartTimer) { this.clearTimeoutFn(this.restartTimer); this.restartTimer = null; }
    const child = this.child;
    if (!child) return;
    const exited = new Promise((resolve) => child.once('exit', resolve));
    try {
      await this.request('POST', '/api/shutdown');
    } catch (err) {
      this.emit('log', `shutdown request failed: ${err.message}`);
    }
    let deadline;
    const killed = new Promise((resolve) => {
      deadline = this.setTimeoutFn(() => {
        if (!child.killed) child.kill();
        resolve();
      }, timeoutMs);
    });
    await Promise.race([exited, killed]);
    this.clearTimeoutFn(deadline);
  }
}

module.exports = { LauncherSidecar, READY_PREFIX, backoffMs, engineCommand, parseReadyLine };
