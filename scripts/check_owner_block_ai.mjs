import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const appSource = fs.readFileSync(new URL('../static/app.js', import.meta.url), 'utf8');
const taskSource = fs.readFileSync(new URL('../static/task-ui.js', import.meta.url), 'utf8');
const preparationSource = fs.readFileSync(new URL('../static/model-preparation-ui.js', import.meta.url), 'utf8').replace(/^export /gm, '');
const extract = (source, start, end) => {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert(a >= 0 && b > a, `Missing source boundaries: ${start}`);
  return source.slice(a, b);
};
const actualFunctions = [preparationSource,
  extract(taskSource, 'function projectTask()', '\nfunction taskFieldsMarkup('),
  extract(appSource, 'function canPrepareAnnotationModels()', '\nasync function renderModelsPage('),
  extract(appSource, 'function aiPromptConfig(', '\nfunction openExportModal('),
].join('\n');
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const sam3 = { id: 'sam3', name: 'SAM 3', provider_type: 'local_sam3', enabled: true, configured: true, tasks: ['detect', 'segment'], modes: ['auto'], capabilities: ['检测框'], description: 'SAM 3' };
const asset = (id, blockId = null, changes = {}) => ({ id, filename: `${id}.png`, status: 'unlabeled', review_status: 'draft', work_block_id: blockId, ...changes });
const block = (id, ids, changes = {}) => ({ id, project_id: 1, name: `任务块 ${id}`, asset_ids: ids, status: 'in_progress', owner_key: 'user:1', human_started: 1, is_mine: true, ...changes });
const plain = value => JSON.parse(JSON.stringify(value));

// Exercise the actual modal renderer, event handlers, preview configuration,
// submission payload, and lock lifecycle with small DOM/service doubles.
function fixture({ assets = [asset(1, 7)], blocks = [block(7, [1])], active = null, section = 'ai', currentId = null, selected = [], role = 'annotator', model = sam3, draft = false, assistPoints = [], blockMetadataEnabled = blocks.length > 0 } = {}) {
  let modal, preview, scope, handler;
  const nodes = new Map(), calls = [], events = [], notices = [];
  const state = { currentSection: section, project: { id: 1, name: '测试项目', task_type: model.provider_type === 'local_sam2' ? 'segment' : 'detect', labels: [{ id: 1, name: 'cat' }] },
    assets, models: [model], workBlocks: { enabled: blockMetadataEnabled, blocks }, activeWorkBlock: active, activeWorkBlockId: active?.id || null,
    selectedAssetIds: new Set(), asset: assets.find(item => item.id === currentId) || null, activeLabel: { id: 1, name: 'cat' },
    assistPoints, session: { role }, selectionEpoch: 1, jobs: [], lockOwned: Boolean(currentId), authMode: 'accounts' };
  const node = () => ({ isConnected: true, disabled: false, checked: false, value: '', textContent: '', innerHTML: '', hidden: false,
    classList: { add() {}, toggle() {} }, addEventListener(type, callback) { this[`on${type}`] = callback; }, setAttribute() {}, replaceChildren() {}, prepend() {}, scrollIntoView() {} });
  const q = selector => { if (!nodes.has(selector)) nodes.set(selector, node()); return nodes.get(selector); };
  const radios = ['current', 'selected', 'unlabeled', 'all'].map(value => {
    const radio = { ...node(), value };
    Object.defineProperty(radio, 'checked', { get: () => scope === value, set: checked => { if (checked) scope = value; else if (scope === value) scope = null; } });
    return radio;
  });
  const querySelector = selector => {
    if (selector === 'input[name="scope"]:checked') return radios.find(radio => radio.checked) || null;
    if (selector === 'input[name="scope"]:not([disabled])') return radios.find(radio => !radio.disabled) || null;
    const match = selector.match(/^input\[name="scope"\]\[value="(\w+)"\]$/);
    return match ? radios.find(radio => radio.value === match[1]) : q(selector);
  };
  const sourceModal = { ...node(), querySelector };
  const context = vm.createContext({ state, URL, escapeHtml, console,
    document: { querySelector, querySelectorAll: selector => selector === 'input[name="scope"]' ? radios : selector.startsWith('input[name="scope"],') ? [...radios, q('#include-reviewed'), q('#replace-auto')] : [], createElement: node },
    modalRoot: { querySelector: () => sourceModal }, app: { querySelector: () => null }, location: {},
    localStorage: { getItem: () => null, setItem() {} }, hasAnnotationDraft: () => draft,
    openModal: config => { modal = config; }, closeModal: () => { events.push('close'); sourceModal.isConnected = false; },
    aiPreviewMarkup: () => '<section data-ai-preview></section>', mountAiPreview: (_container, dependencies) => { preview = dependencies; return { refresh() {}, dispose() {} }; },
    mountModelPreparation: () => { throw Error('Ready fixture models do not prepare weights'); },
    notify: (...args) => notices.push(args), jsonOptions: (method, payload) => ({ method, body: JSON.stringify(payload) }),
    api: async (path, options) => {
      calls.push({ path, options, payload: options?.body ? JSON.parse(options.body) : null });
      if (handler) return handler(path, options);
      return path.endsWith('/jobs') ? { id: 'job-1', status: 'queued' } : state.project;
    },
    releaseCurrentLock: async id => { events.push(`release:${id}`); state.lockOwned = false; },
    acquireAssetLock: async id => { events.push(`acquire:${id}`); state.lockOwned = true; },
    setSectionTab() {}, resetHistoryNavigation() {}, renderAiPage() {}, renderWorkspace() {}, startJobPolling() {}, refreshInspector() {},
  });
  vm.runInContext(actualFunctions, context);
  context.openAiModal(selected.length ? selected : null);
  if (modal) {
    scope = modal.body.match(/name="scope" value="(\w+)" checked/)?.[1];
    q('#ai-prompt').value = 'cat'; q('#confidence').value = '35'; q('#replace-auto').checked = true;
    q('#include-reviewed').checked = false;
    modal.onReady();
  }
  return { state, context, q, radios, calls, events, notices, sourceModal,
    get modal() { return modal; }, get preview() { return preview; }, set handler(value) { handler = value; },
    choose(value) { scope = value; radios.find(radio => radio.value === value).onchange(); },
    review(value) { q('#include-reviewed').checked = value; q('#include-reviewed').onchange(); },
    get job() { return calls.find(call => call.path.endsWith('/jobs')); },
  };
}

// SAM3's auto-only mode works for the owner's current image and the full block;
// the task-block detail is fresher than its older listing record.
for (const scope of ['current', 'all', 'unlabeled']) {
  const own = block(7, [1, 2]);
  const h = fixture({ assets: [asset(1, 7), asset(2, 7, { review_status: 'rejected' })], blocks: [block(7, [1, 2], { is_mine: false })], active: own, section: 'annotate', currentId: 1 });
  assert(h.modal.body.includes('本块全部图片'));
  assert(h.modal.body.includes('本块未标注'));
  assert(!h.modal.body.includes('<strong>全部数据</strong>'));
  h.choose(scope);
  assert.equal(h.q('#modal-submit').disabled, false);
  assert(h.q('#ai-scope-summary').innerHTML.includes('人工绘制和人工校正的标注始终保留'));
  await h.modal.onSubmit();
  assert.equal(h.job.payload.mode, 'auto');
  assert.deepEqual(h.job.payload.asset_ids, scope === 'current' ? [1] : [1, 2]);
  assert.equal(h.job.payload.replace_existing_auto, true);
  assert.deepEqual(h.events, ['release:1', 'close']);
}

// AI-page and explicit selections evaluate each image, independent of the open
// block. Foreign, released, completed, reviewed, and unknown blocks are skipped.
for (const selected of [[], [1, 2, 3, 4, 5, 6, 7, 8, 9]]) {
  const h = fixture({ assets: [asset(1, 7), asset(2, 8), asset(3, 9), asset(4, 10), asset(5, 7, { review_status: 'submitted' }),
    asset(6, 7, { review_status: 'approved' }), asset(7, 404), asset(8), asset(9, 11)],
  blocks: [block(7, [1, 5, 6]), block(8, [2], { is_mine: false, owner_key: 'user:2' }),
    block(9, [3], { is_mine: false, owner_key: null, status: 'available' }), block(10, [4], { status: 'completed' }),
    block(11, [9], { is_mine: false, owner_key: null, human_started: 0, status: 'available' })], selected });
  if (!selected.length) h.choose('all');
  for (const reason of ['由其他成员负责', '已交回且进入人工流程', '任务块已完成', '任务块内待审核或已通过', '任务块信息未加载']) assert(h.q('#ai-scope-summary').innerHTML.includes(reason));
  assert(h.q('#modal-submit').textContent.includes('3 张'));
  await h.modal.onSubmit();
  assert.deepEqual(h.job.payload.asset_ids, [1, 8, 9]);
}
{
  const h = fixture({ assets: [asset(1, 7), asset(2, 7), asset(3, 8)], blocks: [block(7, [1, 2]), block(8, [3], { is_mine: false })], selected: [2, 3] });
  await h.modal.onSubmit(); assert.deepEqual(h.job.payload.asset_ids, [2]);
}

// Legacy/missing asset membership can be resolved from the server's block IDs,
// but absent metadata is never treated as an unassigned asset in a block project.
{
  const h = fixture({ assets: [asset(1, undefined), asset(2, undefined), asset(3, null), asset(4, 9)],
    blocks: [block(7, [1]), { ...block(9, [4]), human_started: undefined }] });
  delete h.state.assets[0].work_block_id; delete h.state.assets[1].work_block_id;
  delete h.state.workBlocks.blocks[1].human_started;
  h.choose('all');
  assert(h.q('#ai-scope-summary').innerHTML.includes('2 张（任务块信息未加载'));
  await h.modal.onSubmit(); assert.deepEqual(h.job.payload.asset_ids, [1, 3]);
}

// Ownership never follows administrator privileges. All-protected selections
// can still be previewed without releasing edit locks or creating jobs.
for (const role of ['annotator', 'admin']) {
  const h = fixture({ blocks: [block(7, [1], { is_mine: false, owner_key: 'user:2' })], role });
  assert.equal(h.q('#modal-submit').disabled, true);
  assert.deepEqual(plain(h.preview.getAssets().map(item => item.id)), [1]);
  assert.deepEqual(plain(h.preview.getPayload().asset_ids), [1]);
  assert.equal(h.calls.length, 0); assert.equal(h.events.length, 0);
  await assert.rejects(h.modal.onSubmit(), /没有可处理图片/);
  assert.equal(h.calls.length, 0);
}

// The reviewer override only changes unassigned data. Block-review protection
// cannot be bypassed, while read-only previews preserve their prior opt-in.
for (const role of ['annotator', 'reviewer', 'admin']) {
  const h = fixture({ assets: [asset(1, 7, { review_status: 'submitted' }), asset(2, 7, { review_status: 'approved' }),
    asset(3, null, { review_status: 'approved' }), asset(4)], blocks: [block(7, [1, 2])], role });
  h.choose('all'); h.review(true);
  const reviewer = role !== 'annotator';
  assert.deepEqual(plain(h.preview.getAssets().map(item => item.id)), reviewer ? [1, 2, 3, 4] : [4]);
  assert.equal(h.preview.getPayload().include_reviewed, reviewer);
  assert(h.modal.body.includes('重新标注未分块的待审核或已通过图片'));
  await h.modal.onSubmit();
  assert.deepEqual(h.job.payload.asset_ids, reviewer ? [3, 4] : [4]);
  assert.equal(h.job.payload.include_reviewed, reviewer);
}
{
  const h = fixture({ assets: [asset(1, null, { review_status: 'approved' }), asset(2)], blocks: [], role: 'reviewer' });
  h.choose('all'); assert(h.q('#modal-submit').textContent.includes('1 张'));
  h.review(true); await h.modal.onSubmit(); assert.deepEqual(h.job.payload.asset_ids, [1, 2]);
}

// Existing SAM2 point/box assist remains single-image and validates hints.
{
  const model = { ...sam3, id: 'sam2', provider_type: 'local_sam2', modes: ['assist'] };
  const h = fixture({ assets: [asset(1, 7), asset(2, 7)], blocks: [block(7, [1, 2])], model, section: 'annotate', active: block(7, [1, 2]), currentId: 1, assistPoints: [{ x: 3, y: 4, label: 1 }] });
  assert(h.radios.find(radio => radio.value === 'all').disabled);
  await h.modal.onSubmit(); assert.equal(h.job.payload.mode, 'assist'); assert.deepEqual(h.job.payload.asset_ids, [1]);
  assert.deepEqual(h.job.payload.hints, { points: [{ x: 3, y: 4, label: 1 }], label: 'cat' });
  const noHints = fixture({ model, section: 'annotate', currentId: 1 });
  assert(noHints.q('#modal-submit').disabled);
  assert.throws(() => noHints.preview.getPayload(), /返回画布/);
}

// Draft, stale route, changed ownership, busy preview, and failed creation keep
// the existing editing/lock protections intact.
{
  const h = fixture({ section: 'annotate', currentId: 1, draft: true });
  assert.equal(h.modal, undefined); assert(h.notices[0][0].includes('手动标注'));
}
{
  const h = fixture(); h.state.selectionEpoch += 1;
  await assert.rejects(h.modal.onSubmit(), /页面已切换/); assert.equal(h.calls.length, 0);
}
{
  const own = block(7, [1]);
  const h = fixture({ section: 'annotate', currentId: 1, active: own });
  own.is_mine = false; own.owner_key = 'user:2';
  await assert.rejects(h.modal.onSubmit(), /没有可处理图片/); assert.equal(h.events.length, 0);
}
{
  const h = fixture(); h.preview.onChange({ busy: true, hasFreshResult: false, stale: false });
  assert(h.q('#modal-submit').disabled);
  await assert.rejects(h.modal.onSubmit(), /等待预览完成/); assert.equal(h.calls.length, 0);
}
{
  const h = fixture({ section: 'annotate', currentId: 1, active: block(7, [1]) });
  h.handler = () => { throw Error('任务块已被转交'); };
  await assert.rejects(h.modal.onSubmit(), /已被转交/);
  assert.deepEqual(h.events, ['release:1', 'acquire:1']); assert.equal(h.state.readOnly, false);
}
console.log('PASS owner task-block AI actual modal: SAM3 auto current/block scopes, per-image ownership filters and skip reasons, mixed selections, reviewed/unknown protection, read-only preview permissions, legacy unassigned override, SAM2 assist, drafts, stale context, busy preview and lock restoration');
