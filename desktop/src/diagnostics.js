'use strict';

// "Copy diagnostics" for support tickets: versions, launcher state, lanes
// and the launcher's recent stderr — with tokens and secrets redacted.

const SECRET_KEYS = /token|secret|password|api[_-]?key|authorization/i;

function redactValue(value) {
  if (Array.isArray(value)) return value.map(redactValue);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => (
      [k, SECRET_KEYS.test(k) && typeof v !== 'object' ? '<redacted>' : redactValue(v)]
    )));
  }
  return value;
}

function redactText(text, token) {
  let out = String(text || '');
  if (token) out = out.split(token).join('<redacted>');
  return out.replace(/(Bearer\s+)\S+/gi, '$1<redacted>');
}

function buildDiagnostics({ versions, state, lanes, stderrTail, token }) {
  const body = {
    generated_at: new Date().toISOString(),
    versions,
    lanes: redactValue(lanes),
    state: redactValue(state),
  };
  return [
    '## Cymatix diagnostics',
    '```json',
    redactText(JSON.stringify(body, null, 2), token),
    '```',
    '## Launcher stderr (tail)',
    '```',
    redactText(stderrTail, token),
    '```',
  ].join('\n');
}

module.exports = { buildDiagnostics, redactText, redactValue };
