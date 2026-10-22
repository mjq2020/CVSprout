import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../static/theme.js', import.meta.url), 'utf8');
const key = 'cvsprout-theme';
function browser({ saved = null, dark = false, blockedStorage = false, blockedWrite = false, legacyMedia = false, noMedia = false } = {}) {
  const events = [];
  const listeners = new Map();
  let systemListener;
  const storage = {
    getItem: () => saved,
    setItem(name, value) { assert.equal(name, key); if (blockedWrite) throw Error('storage full'); saved = value; },
  };
  const media = { matches: dark };
  media[legacyMedia ? 'addListener' : 'addEventListener'] = (...args) => { systemListener = args.at(-1); };
  const root = { dataset: {}, style: {} };
  const window = {
    get localStorage() { if (blockedStorage) throw Error('storage denied'); return storage; },
    addEventListener: (name, fn) => listeners.set(name, fn),
    dispatchEvent: event => events.push(event),
  };
  if (!noMedia) window.matchMedia = query => { assert.equal(query, '(prefers-color-scheme: dark)'); return media; };
  const context = vm.createContext({ window, document: { documentElement: root }, CustomEvent: class {
    constructor(type, options) { this.type = type; this.detail = options.detail; }
  } });
  vm.runInContext(source, context);
  return {
    api: window.CVSproutTheme, root, events, saved: () => saved,
    system(value) { media.matches = value; systemListener?.(); },
    sync(value, name = key, storageArea = storage) { listeners.get('storage')({ key: name, newValue: value, storageArea }); },
    reloadScript() { vm.runInContext(source, context); return window.CVSproutTheme; },
  };
}

// The first synchronous execution resolves the preference without waiting for DOMContentLoaded.
for (const dark of [false, true]) {
  const b = browser({ dark });
  assert.equal(b.api.getPreference(), 'system');
  assert.equal(b.api.getTheme(), dark ? 'dark' : 'light');
  assert.equal(b.root.dataset.theme, b.api.getTheme());
  assert.equal(b.root.style.colorScheme, b.api.getTheme());
  assert.equal(b.root.dataset.themePreference, 'system');
  b.system(!dark);
  assert.equal(b.api.getTheme(), dark ? 'light' : 'dark');
}
for (const saved of ['light', 'dark', 'system', 'invalid', null]) {
  const b = browser({ saved, dark: true });
  assert.equal(b.api.getPreference(), ['light', 'dark'].includes(saved) ? saved : 'system');
  assert.equal(b.api.getTheme(), saved === 'light' ? 'light' : 'dark');
}
const b = browser();
b.api.setPreference('dark');
assert.equal(b.saved(), 'dark');
assert.equal(b.root.dataset.theme, 'dark');
assert.equal(b.root.dataset.themePreference, 'dark');
b.system(false);
assert.equal(b.api.getTheme(), 'dark', 'explicit choice must survive a system change');
b.api.setPreference('system');
assert.equal(b.api.getTheme(), 'light');
assert.equal(b.events.at(-1).type, 'cvsprout-theme-change');
assert.equal(b.events.at(-1).detail.preference, 'system');
assert.equal(b.events.at(-1).detail.theme, 'light');
const eventCount = b.events.length;
b.api.setPreference('system');
assert.equal(b.events.length, eventCount, 'unchanged choice must not trigger UI updates');
b.api.setPreference('garbage');
assert.equal(b.saved(), 'system');
assert.equal(b.reloadScript(), b.api, 'accidental duplicate script must not install more listeners');
assert.ok(Object.isFrozen(b.api));

// Only localStorage changes for the theme are synchronized, including clear/remove.
b.sync('dark');
assert.equal(b.api.getTheme(), 'dark');
b.sync('light', 'unrelated');
assert.equal(b.api.getTheme(), 'dark');
b.sync('light', key, {});
assert.equal(b.api.getTheme(), 'dark', 'sessionStorage must not change theme');
b.sync(null);
assert.equal(b.api.getPreference(), 'system');
b.api.setPreference('dark');
b.sync(null, null);
assert.equal(b.api.getPreference(), 'system');
b.sync('invalid');
assert.equal(b.api.getPreference(), 'system');

for (const options of [{ blockedStorage: true }, { blockedWrite: true }, { noMedia: true }]) {
  const limited = browser(options);
  limited.api.setPreference('dark');
  assert.equal(limited.api.getTheme(), 'dark');
  limited.api.setPreference('light');
  assert.equal(limited.api.getTheme(), 'light');
}
const legacy = browser({ legacyMedia: true });
legacy.system(true);
assert.equal(legacy.api.getTheme(), 'dark');

// A deferred or late script would cause a flash of the wrong theme.
const html = readFileSync(new URL('../static/index.html', import.meta.url), 'utf8');
const themeTag = html.match(/<script[^>]*src="\/static\/theme\.js[^>]*><\/script>/)?.[0];
assert.ok(themeTag);
assert.ok(!/\b(async|defer|type)\s*[=>]/.test(themeTag));
assert.ok(html.indexOf(themeTag) < html.indexOf('<link rel="stylesheet"'));
assert.ok(html.indexOf('/static/themes.css') > html.indexOf('/static/work-blocks.css'));
console.log('Theme checks passed: initial paint, persistence, system changes, tab sync and storage fallbacks.');
