import { withI18n, stripI18nImports } from './i18n-test-support.mjs';
// Run the real project-device picker and system deployment controller with isolated API/DOM adapters.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = stripI18nImports(readFileSync(new URL('../static/edge-devices-ui.js', import.meta.url), 'utf8'))
  .replace(/^import .*;\n/gm, '').replaceAll('export async function ', 'async function ').replaceAll('export function ', 'function ');
const esc = value => String(value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const clone = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const tick = () => new Promise(resolve => setImmediate(resolve));
const project = { id: 1, name: 'Factory <A>' }, run = { id: 101, project_id: 1, name: 'Detector' };
const conversion = { id: 7, training_run_id: 101, status: 'completed', format: 'rknn', settings: { rknn: { deployment_profile: 'recamera_system' } } };
const online = { id: 'camera-1', name: 'Camera <one>', project_id: 1, enabled: true, connection: { state: 'online', age_seconds: 1, timeout_seconds: 90 } };
const offline = { ...online, id: 'camera-offline', name: 'Offline', connection: { state: 'offline', age_seconds: 100 } };
const defaultModelName = 'yolo11-run101-conv7';
const release = { default_model_name: defaultModelName, id: 20, conversion_id: 7, project_id: 1, filename: 'current.rknn', status: 'ready', contract: { algorithm: 'yolo11' } };
const system = { device_id: online.id, project_id: 1, capabilities: { model_api: true, algorithms: ['yolo11'] }, current: { sModel: 'old.rknn', sStatus: 'running', iFPS: 30 }, system_status: { fresh: true, age_seconds: 1 },
  conversions: [{ default_model_name: defaultModelName, id: 7, training_run_id: 101, compatible: true, model_arch: 'yolo11n.pt' }, { id: 8, training_run_id: 102, compatible: true, model_arch: 'yolo11s.pt' }],
  releases: [release, { ...release, id: 21, conversion_id: 8, filename: 'other.rknn' }], deployments: [], can_deploy: true };

function fixture() {
  let modal = null, options = null, active = null, now = 1800000000000;
  const calls = [], notifications = [], timers = new Set();
  const state = { currentSection: 'train', project: clone(project), selectionEpoch: 1, session: { role: 'admin' } };
  const data = { devices: [clone(online), clone(offline), { ...clone(online), id: 'other-project', project_id: 2 }], system: clone(system), hook: null };
  class Element {
    constructor(tag, attrs = {}) { this.tag = tag; this.attrs = attrs; this.id = attrs.id || ''; this.children = []; this.parent = null; this.connected = false; this.value = attrs.value || ''; this.hidden = 'hidden' in attrs; this.disabled = 'disabled' in attrs; this.checked = false; this.textContent = ''; this.html = ''; this.dataset = Object.fromEntries(Object.entries(attrs).filter(([key]) => key.startsWith('data-')).map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase()), value])); }
    get isConnected() { return this.parent ? this.parent.isConnected : this.connected; }
    set innerHTML(html) {
      this.html = html; this.children.forEach(child => { child.parent = null; }); this.children = [];
      const stack = [this];
      for (const [, close, tag, raw] of html.matchAll(/<(\/?)([a-z][\w-]*)([^>]*)>/g)) {
        if (close) { for (let i = stack.length - 1; i > 0; i--) if (stack[i].tag === tag) { stack.length = i; break; } continue; }
        const attrs = {}; for (const [, name, value] of raw.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) attrs[name] = value ?? '';
        const child = new Element(tag, attrs); child.parent = stack.at(-1); child.parent.children.push(child);
        if (!['input', 'img', 'br', 'hr'].includes(tag) && !raw.trimEnd().endsWith('/')) stack.push(child);
      }
      const selects = this.tag === 'select' ? [this] : this.querySelectorAll('select');
      selects.forEach(select => { select.value = select.querySelector('option')?.value || ''; });
    }
    get innerHTML() { return this.html; }
    querySelectorAll(selector) { return this.children.flatMap(child => [child, ...child.querySelectorAll('*')]).filter(child => {
      if (selector === '*') return true;
      if (selector.startsWith('#')) return child.id === selector.slice(1);
      if (selector.startsWith('.')) return (child.attrs.class || '').split(' ').includes(selector.slice(1));
      if (selector.startsWith('[')) return selector.slice(1, -1) in child.attrs;
      return child.tag === selector;
    }); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    addEventListener() {} setAttribute(key, value) { this.attrs[key] = value; } removeAttribute(key) { delete this.attrs[key]; }
    focus() { active = this; }
  }
  const document = { hidden: false, addEventListener() {}, get activeElement() { return active; }, querySelector: selector => selector === '.modal' ? modal : modal?.querySelector(selector), getElementById: id => modal?.querySelector(`#${id}`) };
  const closeModal = () => { options?.onClose?.(); if (modal) modal.connected = false; modal = null; };
  const context = vm.createContext(withI18n({ URL, Number, Set, Map, JSON, Promise, encodeURIComponent, state, document, esc,
    Date: class extends Date { static now() { return now; } },
    isAdmin: () => state.session?.role === 'admin', notify: (...args) => notifications.push(args), closeModal,
    jsonOptions: (method, body) => ({ method, body: JSON.stringify(body) }),
    openModal: config => { closeModal(); options = config; modal = new Element('div', { class: 'modal' }); modal.connected = true; modal.innerHTML = `${config.body}<button id="modal-submit"></button>`; config.onReady?.(); },
    setInterval: callback => { timers.add(callback); return callback; }, clearInterval: callback => timers.delete(callback), setTimeout,
    api: async (path, config) => { calls.push({ path, config }); if (data.hook) { const response = data.hook(path, config); if (response !== undefined) return await response; }
      if (config?.method === 'POST') {
        if (path === '/api/edge/model-releases') { data.system.releases = [clone(release)]; return clone(release); }
        if (path.endsWith('/model-deployments')) {
          const payload = JSON.parse(config.body), modelName = payload.model_name || defaultModelName;
          const job = { id: 30, status: 'queued', stage: 'queued', ...payload, model_name: modelName, filename: `${modelName}-r${payload.release_id}.rknn` };
          data.system.deployments = [job]; return clone(job);
        }
        return { id: 30 };
      }
      if (path.startsWith('/api/edge/devices?')) return { devices: clone(data.devices) };
      if (path.includes('/system-models')) return clone(data.system);
      throw new Error(`Unexpected API ${path}`);
    },
  }));
  vm.runInContext(source, context);
  return { context, state, data, calls, notifications, document, timers, close: closeModal,
    q: id => modal?.querySelector(`#${id}`), get modal() { return modal; }, get options() { return options; },
    async open(input = { project, run, conversion }) { await context.openProjectDeployment(input); },
    select(id = online.id) { const select = this.q('edge-project-device-select'); select.value = id; select.onchange(); },
    async submit() { const original = modal; try { await options.onSubmit(); } finally { if (original.isConnected) original.querySelector('#modal-submit').disabled = false; await new Promise(resolve => setTimeout(resolve, 5)); } },
    async poll() { for (const timer of [...timers]) timer(); await tick(); },
    advance(milliseconds) { now += milliseconds; },
  };
}

// Authorization, project scope and freshness are independent from the capture setting.
{
  const h = fixture(), eligible = h.context.projectDeviceAvailability;
  assert(eligible(online, 1).selectable);
  assert(eligible({ ...online, capture_enabled: false }, 1).selectable);
  for (const device of [offline, { ...online, enabled: false }, { ...online, enabled: undefined }, { ...online, revoked_at: 'now' }, { ...online, project_archived: true }, { ...online, project_id: 2 }]) assert(!eligible(device, 1).selectable);
  assert(!eligible(online, 1, { stale: true }).selectable);
  for (const mutate of [() => { h.state.currentSection = 'data'; }, () => { h.state.session = {}; }, () => { h.state.project = { id: 2 }; }, () => { h.state.project.archived_at = 'now'; }]) {
    h.state.currentSection = 'train'; h.state.session = { role: 'admin' }; h.state.project = clone(project); mutate(); await h.open(); assert.equal(h.calls.length, 0);
  }
  h.state.project = clone(project); h.state.currentSection = 'train'; h.state.session = { role: 'admin' };
  await h.open({ project, run, conversion: { ...conversion, training_run_id: 102 } }); assert.equal(h.calls.length, 0);
  await h.open({ project, run, conversion: { ...conversion, settings: { rknn: { deployment_profile: 'generic' } } } }); assert.equal(h.calls.length, 0);
}

// The selector scopes, preserves selection/focus, shares requests and fails closed.
{
  const h = fixture(); await h.open(); assert.equal(h.calls[0].path, '/api/edge/devices?project_id=1');
  assert.equal(h.q('modal-submit').disabled, false); assert.equal(h.q('edge-project-device-select').value, online.id); assert(!h.q('edge-project-device-select').innerHTML.includes('other-project'));
  assert(h.q('edge-project-device-select').innerHTML.includes('camera-offline" disabled'));
  h.select(); const select = h.q('edge-project-device-select'); select.focus(); assert.equal(h.q('modal-submit').disabled, false);
  await h.poll(); assert.equal(h.q('edge-project-device-select'), select); assert.equal(select.value, online.id); assert.equal(h.document.activeElement, select);
  const pending = deferred(); h.data.hook = () => pending.promise;
  const count = h.calls.length; await h.poll(); await h.poll(); assert.equal(h.calls.length, count + 1, 'No overlapping poll');
  h.advance(16000); await h.poll(); assert.equal(h.q('modal-submit').disabled, true); assert(h.q('edge-project-device-status').innerHTML.includes('连接状态待确认'));
  pending.reject(new Error('network lost')); await tick(); assert.equal(h.q('edge-project-device-feedback').hidden, false); assert.equal(h.q('modal-submit').disabled, true);
  h.data.hook = null; await h.poll(); assert.equal(select.value, online.id); assert.equal(h.q('modal-submit').disabled, false);
  h.data.hook = () => Promise.reject(new Error('network lost'));
  await assert.rejects(h.submit(), /尚未连接/); assert.equal(h.q('modal-submit').disabled, true, 'Shared modal finally cannot re-enable failed selection');
  h.close();
}

// Continue rechecks connectivity; never silently switches to a different device.
{
  const h = fixture(); await h.open(); h.select(); h.data.devices[0] = clone(offline); h.data.devices[0].id = online.id;
  await assert.rejects(h.submit(), /尚未连接/); assert(h.q('edge-project-device-content')); assert.equal(h.q('edge-project-device-select').value, online.id); assert.equal(h.q('modal-submit').disabled, true);
}

// The locked conversion survives hostile/unfiltered responses and all mutation paths.
{
  const h = fixture(); await h.open(); h.select(); await h.submit();
  assert(h.q('edge-system-content')); assert(h.calls.some(call => call.path.endsWith('/system-models?conversion_id=7')));
  assert.equal(h.q('edge-system-conversion'), null); assert.equal(h.q('edge-system-release'), null); assert.equal(h.q('edge-system-build'), null);
  assert(!h.q('edge-system-release-detail').textContent.includes('other.rknn'));
  assert.equal(h.q('edge-system-mode').value, 'upload'); assert.equal(h.q('modal-submit').disabled, false);
  h.q('edge-system-mode').value = 'activate'; h.q('edge-system-mode').onchange(); assert.equal(h.q('modal-submit').disabled, true);
  await assert.rejects(h.submit(), /确认允许替换/);
  h.q('edge-system-confirm').checked = true; h.q('edge-system-confirm').onchange();
  h.data.system.current.sModel = 'changed.rknn'; await assert.rejects(h.submit(), /确认允许替换/, 'New actual device model invalidates activation consent');
  h.q('edge-system-confirm').checked = true; h.q('edge-system-fps').value = '24'; await h.submit();
  const created = h.calls.find(call => call.path.endsWith('/model-deployments') && call.config);
  assert.deepEqual(JSON.parse(created.config.body), { release_id: 20, activate: true, fps: 24, model_name: defaultModelName });
  h.close();
}

// Recheck immediately before deployment, with safe retries after failures and local expiry.
{
  const h = fixture(); await h.open(); h.select(); await h.submit();
  h.data.devices = [clone(offline)]; h.data.devices[0].id = online.id;
  await assert.rejects(h.submit(), /离线/); assert.equal(h.q('modal-submit').disabled, true); assert(!h.calls.some(call => call.config));
  h.data.devices = [clone(online)]; await h.poll(); assert.equal(h.q('modal-submit').disabled, false);
  const pending = deferred(); h.data.hook = path => path.includes('/system-models') ? pending.promise : undefined;
  h.advance(16000); await h.poll(); assert.equal(h.q('modal-submit').disabled, true);
  pending.reject(new Error('bad status')); await tick(); assert.equal(h.q('modal-submit').disabled, true); assert.equal(h.q('edge-system-feedback').hidden, false);
  h.data.hook = null; await h.poll(); assert.equal(h.q('modal-submit').disabled, false);
  await h.submit(); const post = h.calls.find(call => call.config); assert.deepEqual(JSON.parse(post.config.body), { release_id: 20, activate: false, fps: 30, model_name: defaultModelName }); h.close();
}

// Project/session/navigation/close changes prevent late response DOM writes or follow-up mutations.
for (const scope of ['project', 'epoch', 'session', 'close']) {
  const h = fixture(), pending = deferred(); h.data.hook = () => pending.promise;
  const opening = h.open(); await tick(); const original = h.modal, originalHTML = h.q('edge-project-device-status').innerHTML;
  if (scope === 'project') h.state.project = { id: 2 }; if (scope === 'epoch') h.state.selectionEpoch++; if (scope === 'session') h.state.session = { role: 'admin' }; if (scope === 'close') h.close();
  pending.resolve({ devices: [clone(online)] }); await opening;
  assert.equal(original.querySelector('#edge-project-device-status').innerHTML, originalHTML); assert.equal(h.timers.size, 0);
}
for (const action of ['build', 'deploy']) {
  const h = fixture(); await h.open(); h.select(); await h.submit();
  const pending = deferred(); h.data.hook = path => path.includes('/system-models') ? pending.promise : undefined;
  if (action === 'build') h.data.system.releases = [];
  const work = h.submit(); await tick();
  h.state.project = { id: 2 }; pending.resolve(clone(system)); await work;
  assert(!h.calls.some(call => call.config), `No ${action} after project change during preflight`); h.close();
}

// Offline/revoked devices retain history management, but history mode cannot publish/deploy.
{
  const h = fixture(); h.state.currentSection = 'devices';
  h.data.system.deployments = [{ id: 30, status: 'queued', filename: 'current.rknn' }, { id: 31, status: 'unknown', lease_expired: true, updated_at: 'one' }];
  await h.context.openDeviceDeployment(online, { current: () => h.state.currentSection === 'devices', historyOnly: true });
  assert.equal(h.q('modal-submit').hidden, true); assert.equal(h.q('edge-system-build'), null); assert.equal(h.q('edge-system-deploy-section').hidden, true);
  await h.options.onSubmit(); assert(!h.calls.some(call => call.config));
  const cancel = h.q('edge-system-content').querySelectorAll('[data-edge-cancel-model]')[0]; await cancel.onclick(); assert(h.calls.some(call => call.path.endsWith('/30/cancel')));
  const resolve = h.q('edge-system-content').querySelectorAll('[data-edge-resolve-model]')[0]; resolve.onclick();
  h.q('edge-system-resolve-name').value = online.name; h.q('edge-system-resolve-ack').checked = true; h.q('edge-system-resolve-name').oninput();
  await h.q('edge-system-resolve-submit').onclick(); assert(h.calls.some(call => call.path.endsWith('/31/resolve')));
  const count = h.calls.length; h.state.currentSection = 'train'; await cancel.onclick(); assert.equal(h.calls.length, count); h.close();
  const revokedMarkup = h.context.deviceCardMarkup({ ...online, revoked_at: 'now' }); assert(revokedMarkup.includes('data-edge-model-history="camera-1">')); assert(revokedMarkup.includes('前往项目部署'));
}
console.log('PASS project deployment: project/admin scope, online-only device selection, stable focus, single-flight polling and expiry, failed/late response guards, locked model/release, preflight checks, activation consent, upload-only payload, offline history cancel/resolve');

// Deployment publishes only the chosen conversion, reuses ready versions, and rejects mismatched replies.
for (const hasRelease of [false, true]) {
  const h = fixture(); if (!hasRelease) h.data.system.releases = [clone(system.releases[1])];
  await h.open(); await h.submit(); await h.submit();
  const writes = h.calls.filter(call => call.config?.method === 'POST');
  assert.deepEqual(writes.map(call => call.path), [...(hasRelease ? [] : ['/api/edge/model-releases']), '/api/edge/devices/camera-1/model-deployments']);
  if (!hasRelease) assert.deepEqual(JSON.parse(writes[0].config.body), { conversion_id: 7 });
  assert.deepEqual(JSON.parse(writes.at(-1).config.body), { release_id: 20, activate: false, fps: 30, model_name: defaultModelName });
  assert(h.q('edge-system-progress').innerHTML.includes('等待设备领取'));
  assert.equal(h.q('modal-submit').disabled, true);
  await assert.rejects(h.submit(), /设备仍有/);
  assert.equal(h.calls.filter(call => call.path.endsWith('/model-deployments') && call.config).length, 1);
  h.close();
}
{
  const h = fixture(); h.data.system.releases = []; await h.open(); await h.submit();
  h.data.hook = path => path === '/api/edge/model-releases' ? { ...release, conversion_id: 8 } : undefined;
  await assert.rejects(h.submit(), /当前转换不一致/);
  assert(!h.calls.some(call => call.path.endsWith('/model-deployments') && call.config));
  h.data.hook = path => path === '/api/edge/model-releases' ? Promise.reject(new Error('model checksum failed')) : undefined;
  await assert.rejects(h.submit(), /checksum failed/);
  assert(h.q('edge-system-progress').innerHTML.includes('checksum failed')); assert.equal(h.q('modal-submit').disabled, false);
  h.data.hook = null; await h.submit(); assert(h.q('edge-system-progress').innerHTML.includes('等待设备领取')); h.close();
}

// A long model preparation cannot authorize a later deployment after scope or device state changes.
for (const change of ['close', 'project', 'offline', 'new-model', 'busy']) {
  const h = fixture(); h.data.system.releases = []; await h.open(); await h.submit();
  h.q('edge-system-mode').value = 'activate'; h.q('edge-system-mode').onchange(); h.q('edge-system-confirm').checked = true;
  const publishing = deferred(); h.data.hook = path => path === '/api/edge/model-releases' ? publishing.promise : undefined;
  const operation = h.submit(); await tick();
  assert(h.q('edge-system-progress').innerHTML.includes('正在准备模型'));
  if (change === 'close') h.close();
  if (change === 'project') h.state.project = { id: 2 };
  if (change === 'offline') h.data.devices = [{ ...offline, id: online.id }];
  if (change === 'new-model') h.data.system.current.sModel = 'another-model.rknn';
  if (change === 'busy') h.data.system.deployments = [{ id: 99, status: 'running', stage: 'uploading' }];
  h.data.system.releases = [clone(release)]; publishing.resolve(clone(release));
  if (['close', 'project'].includes(change)) await operation;
  else await assert.rejects(operation, /离线|确认允许替换|设备仍有/);
  assert(!h.calls.some(call => call.path.endsWith('/model-deployments') && call.config)); h.close();
}

// Accepted tasks survive a delayed read; polling errors mark displayed progress stale.
{
  const h = fixture(); await h.open(); await h.submit();
  h.data.hook = (path, config) => path.endsWith('/model-deployments') && config ? { id: 30, status: 'queued', stage: 'queued' } : undefined;
  await h.submit(); assert(h.q('edge-system-progress').innerHTML.includes('等待设备领取')); assert.equal(h.q('modal-submit').disabled, true);
  h.data.hook = path => path.includes('/system-models') ? Promise.reject(new Error('offline')) : undefined;
  await h.poll(); assert(h.q('edge-system-progress').innerHTML.includes('进度更新中断')); assert.equal(h.q('modal-submit').disabled, true);
  h.data.hook = null; h.data.system.deployments = [{ id: 30, status: 'running', stage: 'downloading', activate: false }];
  await h.poll(); assert(h.q('edge-system-progress').innerHTML.includes('设备正在下载模型')); assert(!h.q('edge-system-progress').innerHTML.includes('进度更新中断')); h.close();
}

// Never silently select another device after the chosen device disappears.
{
  const h = fixture(); await h.open(); assert.equal(h.q('edge-project-device-select').value, online.id);
  h.data.devices = [{ ...online, id: 'replacement' }]; await h.poll(); await h.poll();
  assert.equal(h.q('edge-project-device-select').value, ''); assert.equal(h.q('modal-submit').disabled, true); h.close();
}

// Stage counts represent device receipts; activation is complete only after verified success.
{
  const h = fixture(), markup = h.context.systemDeploymentProgressMarkup;
  for (const [stage, count] of [['queued', 0], ['downloading', 1], ['uploading', 2], ['activating', 3], ['activated', 4]]) {
    const html = markup({ status: stage === 'queued' ? 'queued' : 'running', stage, activate: true });
    assert(html.includes(`value="${count}"`)); assert(html.includes(`已完成 ${count} / 5 个阶段`)); assert(!html.includes('5 / 5')); assert(!html.includes('%'));
  }
  assert(markup({ status: 'succeeded', stage: 'verified', activate: true }).includes('value="5"'));
  assert(markup({ status: 'succeeded', stage: 'uploaded', activate: false }).includes('已完成 3 / 3'));
  for (const job of [{ status: 'unknown', stage: 'unknown' }, { status: 'failed', stage: 'failed' }, { status: 'cancelled', stage: 'cancelled' }, { status: 'running', stage: 'activated', lease_expired: true }, { status: 'succeeded', stage: 'activated', activate: true }]) {
    const html = markup(job); assert(!html.includes('<progress')); assert(!html.includes('class="done"'));
  }
  const html = markup({ status: 'running', stage: 'uploading', filename: '<img src=x>', activate: false }, { stale: true });
  assert(html.includes('&lt;img')); assert(!html.includes('<img')); assert(!html.includes('class="current"'));
}
console.log('PASS simplified deployment: automatic release/reuse, mismatch and preparation failures, no duplicate submission, scope/device/consent changes during preparation, retained accepted tasks, stale progress, no device switch, truthful stage counts and escaping');

// If the queue response is lost, reconcile the accepted task without resubmitting.
{
  const h = fixture(); await h.open(); await h.submit();
  h.data.hook = (path, config) => {
    if (!path.endsWith('/model-deployments') || !config) return undefined;
    h.data.system.deployments = [{ id: 30, release_id: 20, filename: `${defaultModelName}-r20.rknn`, model_name: defaultModelName, status: 'queued', stage: 'queued', activate: false, fps: 30 }];
    return Promise.reject(new Error('response lost'));
  };
  await h.submit(); assert(h.q('edge-system-progress').innerHTML.includes('等待设备领取'));
  assert(!h.q('edge-system-progress').innerHTML.includes('response lost'));
  assert.equal(h.q('edge-system-deploy-section').hidden, true); assert.equal(h.q('modal-submit').hidden, true);
  assert.equal(h.calls.filter(call => call.path.endsWith('/model-deployments') && call.config).length, 1);
  h.close();
}
console.log('PASS lost deployment response reconciliation and progress-only display while tasks are active');

// Model names initialize from trusted API defaults once; polling preserves edits, focus and consent rules.
{
  const h = fixture(); await h.open(); await h.submit();
  const input = h.q('edge-system-model-name');
  assert.equal(input.value, defaultModelName); assert(h.q('edge-system-model-name-preview').textContent.includes(`${defaultModelName}-r20.rknn`));
  assert(h.q('edge-system-release-detail').textContent.includes('发布版本 #20')); assert(!h.q('edge-system-release-detail').textContent.includes(release.filename));
  input.value = 'Assembly-Line_2'; input.oninput(); input.focus(); input.selectionStart = 4; input.selectionEnd = 8;
  h.data.system.conversions[0].default_model_name = 'another-default'; await h.poll();
  assert.equal(h.q('edge-system-model-name'), input); assert.equal(input.value, 'Assembly-Line_2'); assert.equal(h.document.activeElement, input); assert.equal(input.selectionStart, 4); assert.equal(input.selectionEnd, 8);
  assert(h.q('edge-system-model-name-preview').textContent.includes('Assembly-Line_2-r20.rknn'));
  h.q('edge-system-mode').value = 'activate'; h.q('edge-system-mode').onchange(); h.q('edge-system-confirm').checked = true; h.q('edge-system-confirm').onchange();
  input.value = 'Assembly-Line_3'; input.oninput(); assert.equal(h.q('edge-system-confirm').checked, false); assert.equal(h.q('modal-submit').disabled, true); h.close();
}
{
  const h = fixture(); delete h.data.system.conversions[0].default_model_name; await h.open(); await h.submit();
  assert.equal(h.q('edge-system-model-name').value, defaultModelName, 'Release supplies the same trusted default when the candidate field is absent'); h.close();
}

// Reject unsafe names inline and on direct submit; empty names remain an explicit server-default request.
{
  const h = fixture(); await h.open(); await h.submit(); const input = h.q('edge-system-model-name');
  for (const invalid of ['中文模型', '../model', 'model.rknn', 'two words', '_leading', '-leading', 'model/child', 'A'.repeat(65), '<script>', 'model\nline']) {
    input.value = invalid; input.oninput(); assert.equal(h.q('modal-submit').disabled, true, invalid);
    assert.equal(input.attrs['aria-invalid'], 'true'); assert.equal(h.q('edge-system-model-name-error').hidden, false); assert.equal(h.q('edge-system-model-name-preview').hidden, true);
    await assert.rejects(h.submit(), /模型名称|英文字母/); assert.equal(h.q('modal-submit').disabled, true); assert(!h.calls.some(call => call.config));
  }
  for (const valid of ['', '   ', 'A', '2', 'Model_Name-2', 'A'.repeat(64), '  Mixed-Name_1  ']) {
    input.value = valid; input.oninput(); assert.equal(input.attrs['aria-invalid'], 'false'); assert.equal(h.q('edge-system-model-name-error').hidden, true); assert.equal(h.q('modal-submit').disabled, false, valid);
  }
  input.value = '   '; input.oninput(); await h.poll(); assert.equal(input.value, '   ', 'Polling must not restore a cleared default');
  assert(h.q('edge-system-model-name-preview').textContent.includes(`${defaultModelName}-r20.rknn`));
  await h.submit(); const sent = JSON.parse(h.calls.find(call => call.path.endsWith('/model-deployments') && call.config).config.body);
  assert.equal(sent.model_name, ''); assert(h.q('edge-system-progress').innerHTML.includes(`${defaultModelName}-r20.rknn`)); h.close();
}

// The submitted name is trimmed and case-preserving; accepted task filenames take priority over release storage filenames.
{
  const h = fixture(); await h.open(); await h.submit();
  h.q('edge-system-model-name').value = '  FactoryA_Detector-2  '; h.q('edge-system-model-name').oninput();
  let accepted = false;
  h.data.hook = (path, config) => {
    if (path.endsWith('/model-deployments') && config) { accepted = true; return { id: 30, release_id: 20, status: 'queued', stage: 'queued', model_name: 'FactoryA_Detector-2', filename: 'FactoryA_Detector-2-r20.rknn' }; }
    if (accepted && path.includes('/system-models')) return Promise.reject(new Error('read delayed'));
  };
  await h.submit(); assert.equal(JSON.parse(h.calls.find(call => call.path.endsWith('/model-deployments') && call.config).config.body).model_name, 'FactoryA_Detector-2');
  assert(h.q('edge-system-progress').innerHTML.includes('FactoryA_Detector-2-r20.rknn')); assert(!h.q('edge-system-progress').innerHTML.includes(release.filename)); h.close();
}

// Preparation never resets user edits or deploys a changed name under a previous click.
{
  const h = fixture(); h.data.system.releases = []; await h.open(); await h.submit();
  const input = h.q('edge-system-model-name'); input.value = 'Before-Prepare'; input.oninput();
  assert(h.q('edge-system-model-name-preview').textContent.includes('Before-Prepare-r〈发布ID〉.rknn'));
  const publishing = deferred(); h.data.hook = path => path === '/api/edge/model-releases' ? publishing.promise : undefined;
  const operation = h.submit(); await tick(); input.value = 'After-Prepare'; input.oninput();
  h.data.system.releases = [clone(release)]; publishing.resolve(clone(release));
  await assert.rejects(operation, /模型名称已变化/); assert.equal(input.value, 'After-Prepare');
  assert(!h.calls.some(call => call.path.endsWith('/model-deployments') && call.config));
  assert(h.q('edge-system-model-name-preview').textContent.includes('After-Prepare-r20.rknn'));
  h.data.hook = null; await h.submit();
  assert.equal(JSON.parse(h.calls.find(call => call.path.endsWith('/model-deployments') && call.config).config.body).model_name, 'After-Prepare'); h.close();
}

// A simultaneous task for the same release under another name cannot satisfy lost-response reconciliation.
{
  const h = fixture(); await h.open(); await h.submit();
  h.data.hook = (path, config) => {
    if (!path.endsWith('/model-deployments') || !config) return undefined;
    h.data.system.deployments = [{ id: 30, release_id: 20, filename: 'Another-Name-r20.rknn', model_name: 'Another-Name', status: 'queued', stage: 'queued', activate: false, fps: 30 }];
    return Promise.reject(new Error('response lost'));
  };
  await assert.rejects(h.submit(), /response lost/);
  assert(!h.notifications.some(([message]) => message.includes('已确认创建'))); assert.equal(h.calls.filter(call => call.path.endsWith('/model-deployments') && call.config).length, 1); h.close();
}
{
  const h = fixture(); h.state.currentSection = 'devices';
  await h.context.openDeviceDeployment(online, { current: () => true, historyOnly: true });
  assert.equal(h.q('edge-system-model-name'), null); assert.equal(h.q('edge-system-model-name-preview'), null); h.close();
}
console.log('PASS deployment names: trusted algorithm defaults, stable edited input/focus, ASCII/64-character validation, empty default and trimming, activation re-consent, filename preview, task-name progress retention, preparation rename guard and exact lost-response reconciliation');
