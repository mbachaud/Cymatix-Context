'use strict';

// One status for the tray icon and the taskbar/dock badge.
//   error: launcher unreachable, or the primary lane is down
//   warn:  a lane that should be running (autostart) is not
//   ok:    everything that should run is running
function laneHealth(lanes) {
  if (!Array.isArray(lanes) || lanes.length === 0) return 'error';
  const primary = lanes.find((l) => l.primary);
  if (!primary || !primary.running) return 'error';
  if (lanes.some((l) => !l.primary && l.autostart && !l.running)) return 'warn';
  return 'ok';
}

const HEALTH_LABEL = {
  ok: 'All lanes running',
  warn: 'A lane is stopped',
  error: 'Cymatix is not running',
};

module.exports = { HEALTH_LABEL, laneHealth };
