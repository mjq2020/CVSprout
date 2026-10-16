import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../static/app.js', import.meta.url), 'utf8');
const start = source.indexOf('async function ensureAuthenticated(');
const end = source.indexOf('\nfunction renderLoginScreen(', start);
assert(start >= 0 && end > start, 'Authentication entry must be present');

function harness(fetch) {
  const state = {selectionEpoch: 1, authReady: false, session: null};
  const app = {innerHTML: 'original page'};
  let loginRenders = 0;
  const context = vm.createContext({state, app, fetch, icon: () => '', renderLoginScreen: () => {
    loginRenders++; state.session = null; state.authReady = false;
  }});
  vm.runInContext(source.slice(start, end) + '\nglobalThis.check = ensureAuthenticated;', context);
  return {state, app, check: context.check, loginRenders: () => loginRenders};
}
const response = (data, ok = true) => ({ok, json: async () => data});
const oldSession = {username: 'previous-user'};
const newSession = {username: 'new-login', role: 'reviewer'};

for (const stage of ['health response', 'health body', 'session response', 'session body']) {
  for (const failure of [false, true]) {
    let started, release;
    const waiting = new Promise(resolve => { started = resolve; });
    const gate = new Promise(resolve => { release = resolve; });
    async function pause(at, value) {
      if (at !== stage) return value;
      started(); await gate;
      if (failure) throw new Error('Old connection failed');
      return value;
    }
    const h = harness(async path => {
      const health = path === '/api/health', name = health ? 'health' : 'session';
      return pause(`${name} response`, {ok: true, json: () => pause(`${name} body`, health ? {auth_mode: 'accounts'} : oldSession)});
    });
    const pending = h.check(1);
    await waiting;
    h.state.selectionEpoch = 2; h.state.session = newSession; h.state.authReady = true;
    const snapshot = JSON.stringify(h.state);
    release(); assert.equal(await pending, false);
    assert.equal(JSON.stringify(h.state), snapshot, `${stage}: old response must not change the new login`);
    assert.equal(h.loginRenders(), 0); assert.equal(h.app.innerHTML, 'original page');
  }
}

let started, release;
const waiting = new Promise(resolve => { started = resolve; });
const gate = new Promise(resolve => { release = resolve; });
const stale401 = harness(async path => {
  if (path === '/api/health') return response({auth_mode: 'accounts'});
  started(); await gate; return response({}, false);
});
const pending401 = stale401.check(1); await waiting;
stale401.state.selectionEpoch = 2; stale401.state.session = newSession; stale401.state.authReady = true;
release(); assert.equal(await pending401, false); assert.equal(stale401.loginRenders(), 0);
assert.equal(stale401.state.session, newSession); assert.equal(stale401.state.authReady, true);

const fresh = harness(async path => response(path === '/api/health' ? {auth_mode: 'accounts'} : oldSession));
assert.equal(await fresh.check(1), true); assert.equal(fresh.state.session, oldSession);
const signedOut = harness(async path => path === '/api/health' ? response({auth_mode: 'accounts'}) : response({}, false));
assert.equal(await signedOut.check(1), false); assert.equal(signedOut.loginRenders(), 1);
const unavailable = harness(async () => response({}, false));
assert.equal(await unavailable.check(1), false); assert.match(unavailable.app.innerHTML, /服务不可用/);
console.log('PASS authentication response/body races, stale 401 and network failures preserve the new session; current login and service-error flows work');
