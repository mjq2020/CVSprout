import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../static/model-preparation-ui.js', import.meta.url), 'utf8').replace(/^export /gm, '');
const appSource = fs.readFileSync(new URL('../static/app.js', import.meta.url), 'utf8');
const extract = (start, end) => { const a = appSource.indexOf(start), b = appSource.indexOf(end, a); assert(a >= 0 && b > a); return appSource.slice(a, b); };
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const model = (changes = {}) => ({ id: 'qwen-small', name: 'Qwen 3.5', enabled: true, configured: false, modes: ['auto', 'assist'], capabilities: ['检测框'], tasks: ['detect'], preparation: { mode: 'download', downloadable: true, installed: false, directory: '/models/qwen', files: ['config.json', 'model.safetensors'], missing_files: ['model.safetensors'], size_bytes: 2 * 1024 ** 3, source_url: 'https://example.com/model', state: { status: 'missing' } }, ...changes });
const defer = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const settle = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
function fixture(initial = model()) {
  const nodes = new Map(), timers = new Map(), calls = [], updates = [];
  let current = true, allowed = true, nextTimer = 0, handler;
  const container = { isConnected: true, innerHTML: '', querySelector(selector) { if (!nodes.has(selector)) nodes.set(selector, { textContent: '', innerHTML: '', hidden: false, disabled: false, open: false }); return nodes.get(selector); } };
  const context = vm.createContext({ URL, AbortController, console, setTimeout: fn => { timers.set(++nextTimer, fn); return nextTimer; }, clearTimeout: id => timers.delete(id) });
  vm.runInContext(source, context);
  const controller = context.mountModelPreparation(container, { model: initial, escapeHtml: esc, canPrepare: () => allowed, isCurrent: () => current, api: async (path, options) => { const call = { path, ...options }; calls.push(call); return handler ? handler(call) : initial; }, onChange: (...args) => updates.push(args) });
  return { context, controller, container, calls, updates, timers, q: selector => container.querySelector(selector), set current(value) { current = value; }, set allowed(value) { allowed = value; }, set handler(value) { handler = value; }, async tick() { const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(fn => fn()); await settle(); } };
}

// Rendering alone never downloads. Files are separate from runtime readiness.
{
  const h = fixture(); assert.equal(h.calls.length, 0); assert.equal(h.timers.size, 0);
  assert.equal(h.q('[data-preparation-start]').hidden, false); assert(h.q('[data-preparation-start]').textContent.includes('2.0 GiB'));
  assert.equal(h.context.annotationModelSelectable(model()), true);
  assert.equal(h.context.annotationModelSelectable(model(), false), false);
  assert.equal(h.context.annotationModelSelectable(model({ enabled: false })), false);
  assert.equal(h.context.annotationModelSelectable(model({ configured: true }), false), true);
  assert(h.context.modelPreparationStatus(model({ preparation: { installed: true }, availability_error: '缺少 transformers' })).includes('已检测到模型文件；缺少 transformers'));
  const details = h.context.modelPreparationDetails(model({ preparation: { directory: '<script>bad</script>', files: ['<img onerror=x>'], missing_files: ['<img onerror=x>'], source_url: 'javascript:bad()' } }), esc);
  assert(!details.includes('<script>')); assert(!details.includes('<img')); assert(!details.includes('href=')); assert(details.includes('缺失'));
}
// Manual files can be inspected without permitting a download, including a forged prepare call.
{
  const manual = model({ id: 'sam3', preparation: { mode: 'manual', downloadable: false, installed: false, directory: '/models/sam3', files: ['sam3.pt'], missing_files: ['sam3.pt'], state: { status: 'missing' } } });
  const h = fixture(manual); assert.equal(h.context.annotationModelSelectable(manual), true); assert.equal(h.q('[data-preparation-start]').hidden, true); assert.equal(h.q('[data-preparation-details]').open, true);
  await h.controller.prepare(); assert.equal(h.calls.length, 0);
  await h.controller.refresh(); assert.equal(h.calls[0].method, 'GET'); assert.equal(h.calls[0].path, '/api/models/sam3/preparation');
}
// Progress polls update existing controls, respect completion/runtime errors, and stop at terminal state.
{
  const h = fixture(), start = h.q('[data-preparation-start]'), details = h.q('[data-preparation-details]'); details.open = true;
  let count = 0;
  h.handler = () => ++count === 1 ? model({ preparation: { ...model().preparation, state: { status: 'downloading', downloaded: 1024 ** 3, total: 2 * 1024 ** 3 } } }) : model({ preparation: { ...model().preparation, installed: true, missing_files: [], state: { status: 'complete' } }, availability_error: '推理依赖未安装' });
  await h.controller.prepare(); assert.equal(h.calls[0].method, 'POST'); assert.equal(h.timers.size, 1); assert(h.q('[data-preparation-status]').textContent.includes('50%'));
  await h.controller.prepare(); assert.equal(h.calls.length, 1, 'Clicking an active shared download must not repeat POST');
  await h.tick(); assert.equal(h.calls[1].method, 'GET'); assert.equal(h.timers.size, 0); assert.equal(h.controller.getModel().configured, false); assert(h.q('[data-preparation-status]').textContent.includes('推理依赖未安装')); assert.equal(h.q('[data-preparation-start]'), start); assert.equal(details.open, true);
  h.handler = () => model({ configured: true, preparation: { ...model().preparation, installed: true, state: { status: 'complete' } } });
  await h.controller.refresh(); assert.equal(h.controller.getModel().configured, true); assert.equal(h.q('[data-preparation-status]').textContent, '模型已就绪');
}
// An explicit selection during the initial GET is remembered once; initial GET itself never prepares.
{
  const h = fixture(), gate = defer(); h.handler = call => call.method === 'GET' ? gate.promise : model({ preparation: { ...model().preparation, state: { status: 'downloading' } } });
  const first = h.controller.refresh(); await h.controller.prepare(); await h.controller.prepare(); assert.equal(h.calls.length, 1);
  gate.resolve(model()); await first; await settle(); assert.deepEqual(h.calls.map(call => call.method), ['GET', 'POST']);
}
// Duplicate requests, failed downloads, permission loss, and stale route/session/modal responses are guarded.
{
  const h = fixture(), gate = defer(); h.handler = () => gate.promise;
  const first = h.controller.prepare(); await h.controller.prepare(); assert.equal(h.calls.length, 1); assert.equal(h.q('[data-preparation-start]').disabled, true);
  gate.resolve(model({ preparation: { ...model().preparation, state: { status: 'failed', error: '网络中断，请重试' } } })); await first;
  assert.equal(h.q('[data-preparation-start]').textContent, '重试下载'); assert(h.q('[data-preparation-status]').textContent.includes('网络中断'));
  h.allowed = false; await h.controller.prepare(); assert.equal(h.calls.length, 1);
  h.allowed = true; h.handler = () => { throw Error('服务状态暂不可用'); }; await h.controller.refresh(); assert.equal(h.q('[data-preparation-error]').hidden, false); assert.equal(h.timers.size, 0);
}
for (const boundary of ['route', 'disconnect', 'dispose']) {
  const h = fixture(), gate = defer(); h.handler = () => gate.promise; const request = h.controller.prepare();
  if (boundary === 'route') h.current = false; else if (boundary === 'disconnect') h.container.isConnected = false; else h.controller.dispose();
  const status = h.q('[data-preparation-status]').textContent;
  gate.resolve(model({ configured: true })); await request; assert.equal(h.updates.length, 0); assert.equal(h.q('[data-preparation-status]').textContent, status); assert.equal(h.timers.size, 0);
  if (boundary === 'dispose') assert.equal(h.calls[0].signal.aborted, true);
}
// Execute the real modal markup: unavailable downloadable/manual models remain selectable; remote unconfigured ones do not.
{
  let modal;
  const models = [model({ id: 'qwen-large', preparation: { ...model().preparation, size_bytes: 70 * 1024 ** 3 } }), model(), model({ id: 'sam', preparation: { mode: 'manual', files: ['sam.pt'], missing_files: ['sam.pt'] } }), model({ id: 'remote', preparation: { mode: 'remote' } })];
  const state = { currentSection: 'ai', project: { id: 1, name: '测试', labels: [] }, models, assets: [{ id: 1, status: 'unlabeled' }], assistPoints: [], session: { role: 'annotator' }, selectionEpoch: 1 };
  const context = vm.createContext({ state, URL, localStorage: { getItem: () => null }, modelFitsProject: () => true, isAdmin: () => false, canReview: () => false, aiPromptConfig: () => ({ value: '识别目标', label: '要求', placeholder: '', hint: '' }), escapeHtml: esc, openModal: config => { modal = config; }, aiPreviewMarkup: () => '', notify: () => {}, document: {}, modalRoot: {} });
  vm.runInContext(source + '\n' + extract('function canPrepareAnnotationModels()', '\nasync function renderModelsPage(') + '\n' + extract('function openAiModal(', '\nfunction openExportModal('), context);
  context.openAiModal();
  assert(modal.body.includes('70.0 GiB · 选择后后台下载'));
  assert(/class="model-card selected [^"]*" data-model-id="qwen-small"/.test(modal.body), 'Prefer a small initial selection without downloading it');
  assert(!/data-model-id="sam"[^>]*disabled/.test(modal.body)); assert(/data-model-id="remote"[^>]*disabled/.test(modal.body));
  assert(modal.body.includes('id="ai-model-preparation"'));
}
console.log('PASS model preparation: explicit download vs read-only initial state, roles/manual policies, progress/runtime distinction, file safety, failure/retry, pending-selection intent, duplicate suppression, stale/disposed responses, stable controls and actual AI picker eligibility');
