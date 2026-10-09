'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { laneHealth } = require('../src/health');
const { buildDiagnostics } = require('../src/diagnostics');
const { buildInstallCommand, validateInstall } = require('../src/mcp');
const { ensureUserConfig } = require('../src/userconfig');

const lane = (name, running, extra = {}) => ({ name, running, autostart: true, primary: name === 'stable', ...extra });

test('laneHealth rolls lanes up to one state', () => {
  assert.equal(laneHealth(null), 'error');                      // launcher unreachable
  assert.equal(laneHealth([lane('stable', false)]), 'error');   // primary down
  assert.equal(laneHealth([lane('stable', true), lane('bench', false)]), 'warn');
  assert.equal(laneHealth([lane('stable', true), lane('staging', false, { autostart: false })]), 'ok');
  assert.equal(laneHealth([lane('stable', true), lane('bench', true)]), 'ok');
});

test('diagnostics redact secrets', () => {
  const text = buildDiagnostics({
    versions: { app: '0.1.0', electron: '33' },
    state: { cymatix: { running: true }, token: 'abc', nested: { admin_token: 'zzz' } },
    lanes: [lane('stable', true)],
    stderrTail: 'Authorization: Bearer tok-999\nall good',
    token: 'tok-999',
  });
  assert.ok(!text.includes('tok-999') && !text.includes('zzz') && !text.includes('"abc"'));
  assert.match(text, /<redacted>/);
  assert.match(text, /all good/);
});

test('validateInstall checks host and lane shape; the CLI owns the host list', () => {
  assert.equal(validateInstall({ host: 'claude-code', lane: 'staging' }), null);
  assert.equal(validateInstall({ host: 'a-host-added-later', lane: 'stable' }), null);
  assert.match(validateInstall({ host: 'evil; rm -rf', lane: 'stable' }), /host/);
  assert.match(validateInstall({ host: 'cursor', lane: '../x' }), /lane/);
  assert.match(validateInstall({}), /host/);
});

test('buildInstallCommand: dev uses python, packaged points chats at the engine', () => {
  const dev = buildInstallCommand({ host: 'cursor', lane: 'bench' },
    { packaged: false, python: 'python', repoRoot: '/repo' });
  assert.equal(dev.command, 'python');
  assert.deepEqual(dev.args, ['-m', 'cymatix_context.desktop_entry', 'cli', 'mcp', 'install',
    '--host', 'cursor', '--lane', 'bench']);
  const pkg = buildInstallCommand({ host: 'claude-desktop', lane: 'stable' },
    { packaged: true, engine: 'C:/App/engine/cymatix-engine.exe' });
  assert.equal(pkg.command, 'C:/App/engine/cymatix-engine.exe');
  assert.deepEqual(pkg.args.slice(-4), ['--server-command', 'C:/App/engine/cymatix-engine.exe',
    '--server-arg', 'mcp']);
});

test('ensureUserConfig seeds a config in userData once', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cymatix-desktop-'));
  const p = ensureUserConfig(dir);
  const text = fs.readFileSync(p, 'utf8');
  assert.match(text, /\[genome\]/);
  assert.ok(text.includes(JSON.stringify(path.join(dir, 'genomes', 'main', 'genome.db'))));
  fs.writeFileSync(p, '# user edited\n');
  ensureUserConfig(dir);
  assert.equal(fs.readFileSync(p, 'utf8'), '# user edited\n');
});
