import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const appSource = fs.readFileSync(new URL('../static/app.js', import.meta.url), 'utf8');
const taskSource = fs.readFileSync(new URL('../static/task-ui.js', import.meta.url), 'utf8');
const preparationSource = fs.readFileSync(new URL('../static/model-preparation-ui.js', import.meta.url), 'utf8').replace(/^export /gm, '');
function extract(source, start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert(a >= 0 && b > a, `Missing function boundaries: ${start}`);
  return source.slice(a, b);
}
const actualFunctions = [
  preparationSource,
  extract(taskSource, 'function projectTask()', '\nfunction taskFieldsMarkup('),
  extract(appSource, 'function canPrepareAnnotationModels()', '\nasync function renderModelsPage('),
  extract(appSource, 'function aiPromptConfig(', '\nfunction openExportModal('),
].join('\n');
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const sam3 = (changes = {}) => ({
  id: 'sam3', name: 'SAM 3', provider_type: 'local_sam3', description: '按任务输出检测框或轮廓',
  enabled: true, configured: true, tasks: ['detect', 'segment'], modes: ['auto'], capabilities: ['检测框', '多边形'],
  preparation: { mode: 'manual', installed: true, downloadable: false, files: ['sam3.pt'], missing_files: [] },
  ...changes,
});
function fixture(task, model = sam3(), saved = {}, role = 'annotator') {
  let modal;
  const state = { currentSection: 'ai', project: { id: 1, name: '测试', task_type: task, labels: [{ name: 'cat' }, { name: 'dog' }] },
    models: [model], assets: [{ id: 1, status: 'unlabeled', review_status: 'draft' }], assistPoints: [], session: { role }, selectionEpoch: 1 };
  const context = vm.createContext({ state, URL, escapeHtml,
    localStorage: { getItem: () => JSON.stringify(saved) }, openModal: config => { modal = config; },
    aiPreviewMarkup: () => '', notify: () => {}, document: {}, modalRoot: {},
    api: () => { throw Error('Opening the picker must not download or run inference'); },
  });
  vm.runInContext(actualFunctions, context);
  context.openAiModal();
  return { state, context, model, get modal() { return modal; } };
}
const samCard = body => body.match(/<button[^>]*data-model-id="sam3"[^>]*>/)?.[0];

// Exercise the actual shared task filter and AI modal renderer, rather than a copied predicate.
for (const task of ['detect', 'segment', 'mixed', 'classify', 'pose']) {
  const h = fixture(task), expected = ['detect', 'segment', 'mixed'].includes(task);
  assert.equal(h.context.modelFitsProject(h.model), expected, task);
  assert.equal(Boolean(samCard(h.modal.body)), expected, `${task}: actual picker card`);
  if (!expected) continue;
  assert(!samCard(h.modal.body).includes('disabled'), `${task}: ready SAM3 is selectable`);
  assert(samCard(h.modal.body).includes('aria-pressed="true"'), `${task}: sole ready model is selected`);
  const prompt = h.context.aiPromptConfig(h.model);
  assert.equal(prompt.label, `${task === 'detect' ? '检测' : '分割'}目标类别（英文，逗号分隔）`);
  assert(prompt.hint.includes(task === 'detect' ? '结果为矩形框。' : '结果为多边形轮廓。'));
  assert(h.modal.body.includes(prompt.label), `${task}: task-specific label reaches the real modal`);
  assert(h.modal.body.includes(prompt.hint), `${task}: task-specific output hint reaches the real modal`);
  assert.equal(prompt.value, 'cat, dog');
  assert.equal(prompt.readOnly, undefined);
  assert(prompt.hint.includes('新类别需由管理员添加'));
}

// Missing manual weights remain inspectable; disabling a model preserves the visible setup card but prevents selection.
for (const task of ['detect', 'segment', 'mixed']) {
  const missing = sam3({ configured: false, preparation: { mode: 'manual', installed: false, downloadable: false, files: ['sam3.pt'], missing_files: ['sam3.pt'] } });
  const h = fixture(task, missing, { model_id: 'sam3', prompt: 'cat' });
  assert(h.context.annotationModelSelectable(missing, false), `${task}: manual preparation does not require download permission`);
  assert(!samCard(h.modal.body).includes('disabled'));
  assert(samCard(h.modal.body).includes('aria-pressed="true"'));
  assert(h.modal.body.includes('需手动准备文件'));
  assert(h.modal.body.includes('id="ai-model-preparation"'));
  const disabled = fixture(task, sam3({ enabled: false }), { model_id: 'sam3' });
  assert(samCard(disabled.modal.body).includes('disabled'));
  assert(samCard(disabled.modal.body).includes('aria-pressed="false"'));
}

// Saved preferences cannot force a task-incompatible model into the picker.
for (const task of ['classify', 'pose']) {
  const h = fixture(task, sam3(), { model_id: 'sam3' });
  assert.equal(samCard(h.modal.body), undefined);
  assert(h.modal.body.includes('尚未配置'));
}
// Permissions and other prompt types retain their existing behavior.
{
  const h = fixture('detect', sam3(), {}, 'admin');
  assert(h.context.aiPromptConfig(h.model).hint.includes('预览可尝试新类别'));
  const dino = h.context.aiPromptConfig({ provider_type: 'local_grounding_dino' });
  assert.equal(dino.label, '检测目标类别（英文，逗号分隔）');
  assert(dino.hint.startsWith('Grounding DINO'));
  const yolo = h.context.aiPromptConfig({ provider_type: 'local_yolo' });
  assert.equal(yolo.readOnly, true);
  assert.equal(yolo.value, 'cat, dog');
}
console.log('PASS SAM3 task filtering and actual AI picker: detect/segment/mixed, incompatible saved selections, ready/manual/disabled states, task-specific prompt and output hints, unchanged role and other-model behavior');
