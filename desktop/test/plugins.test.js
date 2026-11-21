'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { pluginTrayItems, pluginsKey } = require('../src/plugins');
const { laneHealth } = require('../src/health');

const loaded = (name, menu, extra = {}) => ({ name, status: 'loaded', menu, ...extra });
const lane = (name, running, extra = {}) => ({ name, running, autostart: true, primary: name === 'stable', ...extra });

test('returns [] for null/invalid payload', () => {
  for (const bad of [null, undefined, 5, 'x', {}, { plugins: 'no' }, { plugins: [null, 3] }]) {
    assert.deepEqual(pluginTrayItems(bad, () => {}), []);
  }
});

test('skips plugins that are not loaded or have no menu', () => {
  const payload = { plugins: [
    { name: 'a', status: 'disabled', menu: [{ id: 'x', label: 'X', enabled: true }] },
    loaded('b', []),
    loaded('c', undefined),
  ] };
  assert.deepEqual(pluginTrayItems(payload, () => {}), []);
});

test('maps items and calls act(name,id) on click', () => {
  const calls = [];
  const payload = { plugins: [loaded('demo', [{ id: 'go', label: 'Go', enabled: true }])] };
  const items = pluginTrayItems(payload, (n, i) => calls.push([n, i]));
  assert.equal(items[0].type, 'separator');
  assert.equal(items[1].label, 'Go');
  assert.equal(items[1].enabled, true);
  items[1].click();
  assert.deepEqual(calls, [['demo', 'go']]);
});

test('disabled items are inert, bad items are skipped and labels coerced', () => {
  const payload = { plugins: [loaded('demo', [
    { id: 'off', label: 'Off', enabled: false },
    { label: 'no id', enabled: true },
    { id: 7, label: 'numeric id', enabled: true },
    null,
    { id: 'n', label: 42, enabled: true },
  ])] };
  const items = pluginTrayItems(payload, () => {}).slice(1);
  assert.equal(items.length, 2);
  assert.deepEqual(items[0], { label: 'Off', enabled: false });
  assert.equal('click' in items[0], false);
  assert.equal(items[1].label, '42');
  assert.equal(typeof items[1].click, 'function');
});

test('separator only when items exist', () => {
  assert.deepEqual(pluginTrayItems({ plugins: [] }, () => {}), []);
  const only = pluginTrayItems({ plugins: [loaded('p', [{ id: 'a', label: 'A', enabled: true }])] }, () => {});
  assert.equal(only.filter((i) => i.type === 'separator').length, 1);
});

test('pluginsKey changes with menu content', () => {
  const a = { plugins: [loaded('p', [{ id: 'a', label: 'A', enabled: true }])] };
  const b = { plugins: [loaded('p', [{ id: 'a', label: 'A', enabled: false }])] };
  assert.notEqual(pluginsKey(a), pluginsKey(b));
  assert.equal(pluginsKey(a), pluginsKey(JSON.parse(JSON.stringify(a))));
});

test('plugin warn turns green to amber', () => {
  const lanes = [lane('stable', true)];
  for (const level of ['warn', 'error']) {
    const plugins = [loaded('p', [], { status_info: { level, text: 't' } })];
    assert.equal(laneHealth(lanes, plugins), 'warn');
  }
});

test('plugin warn does not change red', () => {
  const plugins = [loaded('p', [], { status_info: { level: 'error', text: 't' } })];
  assert.equal(laneHealth([lane('stable', false)], plugins), 'error');
  assert.equal(laneHealth(null, plugins), 'error');
});

test('disabled plugin status ignored', () => {
  const plugins = [{ name: 'p', status: 'disabled', status_info: { level: 'error', text: 't' } },
    loaded('q', [], { status_info: { level: 'ok', text: '' } }), null, 3];
  assert.equal(laneHealth([lane('stable', true)], plugins), 'ok');
  assert.equal(laneHealth([lane('stable', true)]), 'ok');
});
