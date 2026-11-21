'use strict';

// One status for the tray icon and the taskbar/dock badge.
//   error: launcher unreachable, or the primary lane is down
//   warn:  a lane that should be running (autostart) is not, or a loaded
//          plugin reports a warn/error status (never turns red)
//   ok:    everything that should run is running
function laneHealth(lanes, plugins) {
  if (!Array.isArray(lanes) || lanes.length === 0) return 'error';
  const primary = lanes.find((l) => l.primary);
  if (!primary || !primary.running) return 'error';
  if (lanes.some((l) => !l.primary && l.autostart && !l.running)) return 'warn';
  if (Array.isArray(plugins) && plugins.some((p) => p && typeof p === 'object'
    && p.status === 'loaded' && p.status_info
    && (p.status_info.level === 'warn' || p.status_info.level === 'error'))) return 'warn';
  return 'ok';
}

const HEALTH_LABEL = {
  ok: 'All lanes running',
  warn: 'A lane is stopped or a plugin needs attention',
  error: 'Cymatix is not running',
};

module.exports = { HEALTH_LABEL, laneHealth };
