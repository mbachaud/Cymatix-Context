'use strict';

// Packaged installs keep config and knowledge stores in the app's userData
// directory, never inside the app bundle, so updates cannot wipe corpora.
// Seed a cymatix.toml there on first run; never overwrite the user's edits.

const fs = require('node:fs');
const path = require('node:path');

function ensureUserConfig(userData) {
  const file = path.join(userData, 'cymatix.toml');
  if (fs.existsSync(file)) return file;
  const genome = path.join(userData, 'genomes', 'main', 'genome.db');
  fs.mkdirSync(path.dirname(genome), { recursive: true });
  fs.writeFileSync(file, [
    '# Created by the Cymatix desktop app. Edit freely; it is never overwritten.',
    '# Reference: https://github.com/mbachaud/Cymatix-Context/blob/master/docs/config-reference.md',
    '',
    '[genome]',
    `path = ${JSON.stringify(genome)}`,
    '',
    '# Keep tracked folders fresh while you edit them:',
    '# [sync]',
    '# enabled = true',
    '# roots = ["C:/path/to/your/docs"]',
    '',
  ].join('\n'), 'utf8');
  return file;
}

module.exports = { ensureUserConfig };
