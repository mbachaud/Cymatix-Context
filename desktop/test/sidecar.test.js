'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const path = require('node:path');

const {
  LauncherSidecar,
  backoffMs,
  engineCommand,
  parseReadyLine,
} = require('../src/sidecar');

test('parseReadyLine reads the launcher handshake', () => {
  assert.deepEqual(
    parseReadyLine('CYMATIX_LAUNCHER_READY {"host": "127.0.0.1", "port": 50123, "pid": 7}'),
    { host: '127.0.0.1', port: 50123, pid: 7 },
  );
  assert.equal(parseReadyLine('INFO: Uvicorn running'), null);
  assert.equal(parseReadyLine('CYMATIX_LAUNCHER_READY not-json'), null);
  assert.equal(parseReadyLine('CYMATIX_LAUNCHER_READY {"port": 0}'), null);
});

test('backoff doubles and caps', () => {
  assert.deepEqual([0, 1, 2, 3, 10].map(backoffMs), [1000, 2000, 4000, 8000, 30000]);
});

test('engineCommand: dev runs the repo through python', () => {
  const cmd = engineCommand({ packaged: false, repoRoot: '/repo', python: 'py', platform: 'linux' });
  assert.equal(cmd.command, 'py');
  assert.deepEqual(cmd.args.slice(0, 3), ['-m', 'cymatix_context.desktop_entry', 'launcher']);
  assert.ok(cmd.args.includes('--headless'));
  assert.deepEqual(cmd.args.slice(cmd.args.indexOf('--port'), cmd.args.indexOf('--port') + 2),
    ['--port', '0']);
  assert.equal(cmd.cwd, '/repo');
  assert.equal(cmd.env.PYTHONPATH, '/repo');
});

test('engineCommand: packaged runs the bundled engine from userData', () => {
  const win = engineCommand({
    packaged: true, resourcesPath: 'C:/App/resources', userData: 'C:/Users/u/AppData/Roaming/Cymatix',
    platform: 'win32',
  });
  assert.equal(win.command, path.join('C:/App/resources', 'engine', 'cymatix-engine.exe'));
  assert.equal(win.args[0], 'launcher');
  assert.equal(win.cwd, 'C:/Users/u/AppData/Roaming/Cymatix');
  const mac = engineCommand({ packaged: true, resourcesPath: '/App/Resources', userData: '/u', platform: 'darwin' });
  assert.equal(mac.command, path.join('/App/Resources', 'engine', 'cymatix-engine'));
});

function fakeChild() {
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.pid = 4242;
  child.killed = false;
  child.kill = () => { child.killed = true; setImmediate(() => child.emit('exit', null, 'SIGTERM')); };
  return child;
}

function harness() {
  const spawned = [];
  const timers = [];
  const calls = [];
  const sidecar = new LauncherSidecar({
    command: 'engine', args: ['launcher', '--headless'], cwd: '/tmp', env: { A: '1' },
    spawnFn: (cmd, args, opts) => {
      const child = fakeChild();
      spawned.push({ cmd, args, opts, child });
      return child;
    },
    setTimeoutFn: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeoutFn: () => {},
    fetchFn: async (url, init) => {
      calls.push({ url, init });
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    },
    token: 'tok-123',
  });
  return { sidecar, spawned, timers, calls };
}

test('start passes the token by env only and resolves on READY', async () => {
  const { sidecar, spawned } = harness();
  const ready = sidecar.start();
  const { args, opts, child } = spawned[0];
  assert.ok(!args.join(' ').includes('tok-123'), 'token must not be in argv');
  assert.equal(opts.env.CYMATIX_LAUNCHER_TOKEN, 'tok-123');
  // The launcher watches this PID and shuts down if the app dies.
  assert.equal(opts.env.CYMATIX_DESKTOP_PARENT_PID, String(process.pid));
  assert.equal(opts.env.A, '1');
  assert.equal(opts.windowsHide, true);
  child.stdout.write('INFO starting\nCYMATIX_LAUNCHER_READY {"port": 50123, "pid": 4242}\n');
  const info = await ready;
  assert.equal(info.port, 50123);
  assert.equal(sidecar.baseUrl, 'http://127.0.0.1:50123');
});

test('request sends the bearer token', async () => {
  const { sidecar, spawned, calls } = harness();
  const ready = sidecar.start();
  spawned[0].child.stdout.write('CYMATIX_LAUNCHER_READY {"port": 50123}\n');
  await ready;
  await sidecar.request('GET', '/api/lanes');
  assert.equal(calls[0].url, 'http://127.0.0.1:50123/api/lanes');
  assert.equal(calls[0].init.headers.Authorization, 'Bearer tok-123');
});

test('unexpected exit restarts with backoff', async () => {
  const { sidecar, spawned, timers } = harness();
  const exits = [];
  sidecar.on('exit', (e) => exits.push(e));
  const ready = sidecar.start();
  spawned[0].child.stdout.write('CYMATIX_LAUNCHER_READY {"port": 50123}\n');
  await ready;
  spawned[0].child.emit('exit', 1, null);
  assert.equal(exits.length, 1);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, 1000);
  timers[0].fn();
  assert.equal(spawned.length, 2, 'restarted');
});

test('stop asks the launcher to shut down and does not restart', async () => {
  const { sidecar, spawned, timers, calls } = harness();
  const ready = sidecar.start();
  spawned[0].child.stdout.write('CYMATIX_LAUNCHER_READY {"port": 50123}\n');
  await ready;
  const stopped = sidecar.stop({ timeoutMs: 1000 });
  await new Promise((r) => setImmediate(r));
  assert.equal(calls[0].url, 'http://127.0.0.1:50123/api/shutdown');
  assert.equal(calls[0].init.method, 'POST');
  const timersBeforeExit = timers.length; // the stop's kill-deadline timer
  spawned[0].child.emit('exit', 0, null);
  await stopped;
  assert.equal(timers.length, timersBeforeExit, 'no restart timer after a requested stop');
  assert.equal(spawned.length, 1, 'no restart after a requested stop');
});

test('stderr tail is kept for diagnostics', async () => {
  const { sidecar, spawned } = harness();
  sidecar.start().catch(() => {});
  spawned[0].child.stderr.write('Traceback: boom\n');
  await new Promise((r) => setImmediate(r));
  assert.match(sidecar.stderrTail(), /boom/);
});
