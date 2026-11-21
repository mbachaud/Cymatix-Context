'use strict';

// Tray items contributed by plugins, read from the launcher's GET /api/plugins.
// Only loaded plugins contribute. A disabled item is shown greyed and inert
// (the launcher would still run its action, so no click handler is attached).
// Malformed entries are skipped, never thrown.

function isObject(v) {
  return v !== null && typeof v === 'object';
}

function loadedPlugins(payload) {
  if (!isObject(payload) || !Array.isArray(payload.plugins)) return [];
  return payload.plugins.filter((p) => isObject(p) && p.status === 'loaded');
}

function pluginTrayItems(payload, act) {
  const items = [];
  for (const plugin of loadedPlugins(payload)) {
    if (typeof plugin.name !== 'string' || !Array.isArray(plugin.menu)) continue;
    for (const entry of plugin.menu) {
      if (!isObject(entry) || typeof entry.id !== 'string' || !entry.id) continue;
      const label = String(entry.label);
      if (entry.enabled === false) {
        items.push({ label, enabled: false });
      } else {
        const name = plugin.name;
        const id = entry.id;
        items.push({ label, enabled: true, click: () => act(name, id) });
      }
    }
  }
  return items.length ? [{ type: 'separator' }, ...items] : [];
}

// Stable key of what the tray shows, so the menu is rebuilt only on change.
function pluginsKey(payload) {
  const noop = () => {};
  return JSON.stringify(pluginTrayItems(payload, noop)
    .map((i) => [i.type || '', i.label || '', i.enabled !== false]));
}

module.exports = { pluginTrayItems, pluginsKey };
