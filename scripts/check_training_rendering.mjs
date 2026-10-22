import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../static/app.js', import.meta.url), 'utf8');
const extract = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
const nav = fs.readFileSync(new URL('../static/workspace-navigation.js', import.meta.url), 'utf8');
const tabCode = nav.slice(nav.indexOf('export function sectionTabsMarkup('), nav.indexOf('export function bindSectionTabs(')).replaceAll('export function', 'function');
const renderCode = extract('const trainingBackgroundConversions = new Map();', '\nfunction captureTrainingViewState(');
const conversionCode = extract('async function loadRunConversions(', '\nfunction renderTrainingLogConsole(');
const syncCode = extract('function trainingSyncMarkup(', '\nasync function refreshTrainingEnvironment(');
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b; }); return { promise, resolve, reject }; };
function fixture() {
  const state = { project: { id: 1 }, selectionEpoch: 1, session: {}, currentSection: 'train', trainingSelectionRequest: 1,
    training: { availability: { available: true }, total: 1 }, trainingCatalog: { models: [], tasks: {} },
    selectedTrainingRun: { id: 7, status: 'completed', _conversionsHydrated: true, conversions: [] },
    trainingMetricsHistory: { 7: { attemptedAt: 1 } }, pipelineUpdatedAt: 1000 };
  let page = null, writes = 0, bindings = 0, composing = false, tab = 'overview';
  const calls = [], notices = [], app = {};
  Object.defineProperty(app, 'innerHTML', { set(value) {
    writes++; const slot = { innerHTML: '' }; page = { html: value, querySelector: selector => selector === '[data-training-sync-slot]' ? slot : null, querySelectorAll: () => ['overview', 'details', 'conversions'].map(id => ({ dataset: { sectionTab: `train:${id}` }, textContent: id })) };
  }});
  const context = vm.createContext({ state, app, console,
    document: { querySelector: selector => selector === '.project-page' ? page : composing ? {} : null },
    deferRenderForNavigation: () => false, captureTrainingViewState: () => null,
    getSectionTab: () => tab, monitoredTrainingRun: () => state.selectedTrainingRun, trainingReadiness: () => ({ ready: true, runtimeHealthy: true, reason: 'ready' }),
    escapeHtml: value => String(value ?? ''), icon: () => '', projectPageShell: (_section, content) => content,
    trainingRunCard: run => JSON.stringify({ status: run.status, conversions: run.conversions, loading: Boolean(run._conversionsLoading), error: run._conversionError || '' }) + context.sectionTabsMarkup('train', ['overview', 'details', 'conversions'].map(id => [id,id]), tab, String) + ['overview', 'details', 'conversions'].map(id => `<div ${context.sectionPanelAttributes('train', id, tab)}>${id}</div>`).join(''),
    trainingListMarkup: () => '', trainingStatusLabel() {}, trainingTaskInfo() {}, formatDateTime() {}, trainingTaskCapabilityCard() {}, trainingCatalogModelCard() {}, trainingLicenseMarkup: () => '',
    bindCommonProjectEvents: () => { bindings++; }, bindTrainingPageEvents() {}, restoreTrainingViewState() {},
    pipelineApi: () => { const d = deferred(); calls.push(d); return d.promise; }, notify: (...args) => notices.push(args),
  });
  vm.runInContext(tabCode + '\n' + renderCode + '\n' + conversionCode + '\n' + syncCode, context);
  return { state, context, calls, notices, render: () => context.renderTrainingPage(), load: options => context.loadRunConversions(7, options),
    get page() { return page; }, get writes() { return writes; }, get bindings() { return bindings; },
    set composing(value) { composing = value; }, set tab(value) { tab = value; }, unmount: () => { page = null; } };
}
{
  const h = fixture(); h.render(); const page = h.page;
  h.render(); assert.equal(h.writes, 1); assert.equal(h.page, page); assert.equal(h.bindings, 1);
  h.state.pipelineUpdatedAt = 2000; h.render(); assert.equal(h.writes, 1); assert(h.page.querySelector('[data-training-sync-slot]').innerHTML.includes('最近更新'));
  h.state.pipelineConnectionLost = true; h.render(); assert.equal(h.writes, 1); assert(h.page.querySelector('[data-training-sync-slot]').innerHTML.includes('自动重连'));
  h.state.pipelineConnectionLost = false; h.render(); assert.equal(h.writes, 1); assert(!h.page.querySelector('[data-training-sync-slot]').innerHTML.includes('自动重连'));
  h.tab = 'details'; h.render(); assert.equal(h.writes, 2, 'A different panel must render normally');
  h.state.selectedTrainingRun.status = 'running'; h.render(); assert.equal(h.writes, 3, 'Real run changes must render');
  h.state.pipelineManualRefreshing = true; h.render(); assert.equal(h.writes, 4, 'Manual refresh keeps its visible busy feedback');
  h.composing = true; h.state.trainingSelectionError = 'new error'; h.render(); assert.equal(h.writes, 4, 'Composition defers changing content');
  h.composing = false; h.render(); assert.equal(h.writes, 5, 'Deferred errors are shown after composition');
}
{
  const h = fixture(); h.render(); h.tab = 'details'; h.context.syncTrainingTabCache('details'); h.render();
  assert.equal(h.writes, 1, 'A tab selection already applied to existing DOM must not trigger an extra poll replacement');
  h.tab = 'conversions'; h.context.syncTrainingTabCache('conversions'); h.render(); assert.equal(h.writes, 1);
}
{
  const h = fixture(); h.render();
  for (const change of [() => h.state.selectionEpoch++, () => { h.state.session = {}; }, () => h.state.project.id++, () => h.unmount()]) {
    const before = h.writes; change(); h.render(); assert.equal(h.writes, before + 1, 'Identical HTML must not suppress a new route, session, project or remount');
  }
}
{
  const h = fixture(); h.render(); const request = h.load({ force: true, render: true, background: true });
  assert.equal(h.writes, 1); assert(h.state.selectedTrainingRun._conversionsLoading);
  h.render(); assert.equal(h.writes, 1, 'An unrelated poll render cannot flash background conversion loading');
  h.calls[0].resolve({ conversions: [] }); await request; assert.equal(h.writes, 1, 'Empty unchanged background results keep existing nodes');
  const changed = h.load({ force: true, render: true, background: true });
  h.calls[1].resolve({ conversions: [{ id: 3, status: 'completed' }] }); await changed; assert.equal(h.writes, 2, 'New conversion results remain visible');
}
{
  const h = fixture(); h.state.selectedTrainingRun._conversionError = 'previous error'; h.render();
  const request = h.load({ force: true, render: true, background: true });
  assert.equal(h.state.selectedTrainingRun._conversionError, 'previous error', 'Background refresh does not briefly clear an unresolved error');
  h.render(); assert.equal(h.writes, 1); h.calls[0].resolve({ conversions: [] }); await request;
  assert.equal(h.state.selectedTrainingRun._conversionError, ''); assert.equal(h.writes, 2, 'Successful retry clears the old error');
  const failed = h.load({ force: true, render: true, background: true }); h.calls[1].reject(new Error('offline')); await failed;
  assert(h.state.selectedTrainingRun._conversionError.includes('offline')); assert.equal(h.writes, 3, 'Background failures remain visible');
}
for (const options of [{ force: true, render: true }, { force: true, render: true, background: true }]) {
  const h = fixture(); if (options.background) h.state.selectedTrainingRun._conversionsHydrated = false;
  h.render(); const request = h.load(options); assert.equal(h.writes, 2, 'Explicit or first refresh shows loading');
  h.calls[0].resolve({ conversions: [] }); await request; assert.equal(h.writes, 3, 'Loading state is removed when complete');
}
{
  const h = fixture(); h.render(); const request = h.load({ force: true, render: true, background: true });
  h.state.selectionEpoch++; h.state.currentSection = 'data'; h.calls[0].resolve({ conversions: [{ id: 99 }] }); await request;
  assert.equal(h.writes, 1); assert.equal(h.state.selectedTrainingRun.conversions.length, 0, 'Late conversion responses cannot update a different route');
}
console.log('PASS training render stability: unchanged DOM and sync-only updates, real content changes, route/session isolation, composition guard, background conversion results/errors, explicit and first loading, stale response isolation');
