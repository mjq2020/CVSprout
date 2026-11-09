import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../static/i18n.js', import.meta.url), 'utf8')
  .replace(/^import .*;\n/, '').replace(/^export /gm, '');
const messages = JSON.parse(readFileSync(new URL('../static/locales/en.json', import.meta.url), 'utf8'));
function browser({ stored = null, languages = ['zh-CN'], denied = false, search = '' } = {}) {
  const listeners = {}, events = {}, root = {}, location = { href: `http://localhost/${search}#/projects/7/data`, search, reload() { this.reloads = (this.reloads || 0) + 1; } };
  const document = { documentElement: root, addEventListener(type, callback) { (listeners[type] ??= []).push(callback); } };
  const context = vm.createContext({ messages, URL, URLSearchParams, document, location,
    navigator: { languages }, history: { replaceState(_s, _t, url) { location.href = String(url); } },
    get localStorage() { if (denied) throw Error('storage denied'); return { getItem: () => stored, setItem: (_key, value) => { stored = value; } }; },
    CustomEvent: class { constructor(type) { this.type = type; this.defaultPrevented = false; } preventDefault() { this.defaultPrevented = true; } },
    window: { location, dispatchEvent(event) { events[event.type]?.(event); return !event.defaultPrevented; } },
  });
  vm.runInContext(source + '\ninstallLanguageControls(); globalThis.api = {t,ta,getLocale,setLocale,languageControlsMarkup,accountDisplayName,accountNameFromInput};', context);
  return { ...context.api, root, document, location, events, stored: () => stored,
    fire(type, target) { for (const callback of listeners[type] || []) callback({ target }); } };
}

assert.equal(browser().getLocale(), 'zh-CN');
assert.equal(browser({ languages: ['fr', 'en-GB'] }).getLocale(), 'en');
assert.equal(browser({ languages: ['fr'] }).getLocale(), 'zh-CN');
assert.equal(browser({ stored: 'zh-CN', languages: ['en'] }).getLocale(), 'zh-CN');
assert.equal(browser({ stored: 'invalid', languages: ['en'] }).getLocale(), 'en');
assert.equal(browser({ stored: 'zh-CN', search: '?lang=en' }).getLocale(), 'en');
assert.equal(browser({ denied: true, languages: ['en'] }).getLocale(), 'en');

const b = browser();
assert.equal(b.t('项目不存在'), '项目不存在');
assert.equal(b.setLocale('en-US'), true);
assert.equal(b.root.lang, 'en');
assert.equal(b.stored(), 'en');
assert.match(b.document.title, /Annotation Platform/);
assert.equal(b.t('项目不存在'), 'Project does not exist');
assert.equal(b.accountDisplayName('管理员'), 'Administrator');
assert.equal(b.accountDisplayName('Administrator'), 'Administrator');
assert.equal(b.accountDisplayName('管理员张三'), '管理员张三');
assert.equal(b.accountDisplayName('项目不存在'), '项目不存在');
assert.equal(b.accountNameFromInput('Administrator', '管理员'), '管理员');
assert.equal(b.accountNameFromInput('张三', '管理员'), '张三');
const chinese = browser();
assert.equal(chinese.accountDisplayName('管理员'), '管理员');
assert.equal(chinese.accountDisplayName('Administrator'), '管理员');
assert.equal(chinese.accountNameFromInput('管理员', 'Administrator'), 'Administrator');
assert.equal(b.t('Missing catalog entry'), 'Missing catalog entry');
assert.equal(b.t('显示{0}', ['{1} 中文用户数据']), 'Show {1} 中文用户数据');
assert.equal(b.ta('"{0}"', ['&lt;用户&gt;']), '&quot;&lt;用户&gt;&quot;');
assert.equal(b.setLocale('fr'), false);
assert.equal(b.getLocale(), 'en');
assert.match(b.location.href, /\?lang=en#\/projects\/7\/data$/);

const limited = browser({ denied: true });
limited.setLocale('en');
assert.equal(limited.getLocale(), 'en');
assert.match(limited.location.href, /lang=en/);

const control = { value: 'en', matches: selector => selector === '[data-language-select]' };
const blocked = browser();
blocked.events['cvsprout-before-language-change'] = event => event.preventDefault();
blocked.fire('change', control);
assert.equal(blocked.getLocale(), 'zh-CN');
assert.equal(control.value, 'zh-CN');
assert.equal(blocked.location.reloads, undefined);
delete blocked.events['cvsprout-before-language-change'];
control.value = 'en'; blocked.fire('change', control);
assert.equal(blocked.location.reloads, 1);

const input = { validity: { valueMissing: true }, setCustomValidity(message) { this.message = message; } };
blocked.fire('invalid', input);
assert.equal(input.message, 'Please fill out this field.');
blocked.fire('input', input);
assert.equal(input.message, '');

for (const [key, value] of Object.entries(messages)) {
  assert.ok(value.trim(), `Empty translation: ${key}`);
  assert.ok(!/[\u3400-\u9fff]/.test(value), `Untranslated English entry: ${key}`);
  assert.deepEqual((value.match(/\{\d+\}/g) || []).sort(), (key.match(/\{\d+\}/g) || []).sort(), `Placeholder mismatch: ${key}`);
}
console.log('I18n: locale detection, persistence, interpolation, attribute escaping, guarded switching, validation and catalog integrity passed.');
