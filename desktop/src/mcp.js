'use strict';

// "Connect a chat": run `cymatix mcp install` for a host + lane. The host
// list comes from the CLI's own install table (the dashboard offers exactly
// those ids), so no host names live here. This module only checks the
// *shape* of the choices before they reach a command line; the CLI rejects
// any host it does not support.

const HOST_RE = /^[a-z0-9][a-z0-9-]{0,31}$/;
const LANE_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;

function validateInstall(req) {
  if (!req || !HOST_RE.test(req.host || '')) return `invalid host ${JSON.stringify(req && req.host)}`;
  if (!LANE_RE.test(req.lane || '')) return `invalid lane name ${JSON.stringify(req.lane)}`;
  return null;
}

// Dev: the repo's CLI through Python, writing the canonical python entry.
// Packaged: the bundled engine, and the written entry starts that engine
// (`cymatix-engine mcp`), so chats work without a Python install.
function buildInstallCommand({ host, lane }, { packaged, python, repoRoot, engine }) {
  const cli = ['mcp', 'install', '--host', host, '--lane', lane];
  if (packaged) {
    return {
      command: engine,
      args: ['cli', ...cli, '--server-command', engine, '--server-arg', 'mcp'],
      env: {},
    };
  }
  return {
    command: python || 'python',
    args: ['-m', 'cymatix_context.desktop_entry', 'cli', ...cli],
    env: { PYTHONPATH: repoRoot },
  };
}

module.exports = { HOST_RE, buildInstallCommand, validateInstall };
