// Deferred responses exercise the real polling and refresh functions without a server.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
const source = readFileSync(new URL('../static/app.js', import.meta.url), 'utf8');
const extract = (start, end) => {
  const first = source.indexOf(start), last = source.indexOf(end, first);
  assert(first >= 0 && last > first, `Missing function boundary: ${start}`);
  return source.slice(first, last);
};
const code = extract('function startJobPolling()', '\nfunction startCollaborationPolling(') + '\n' +
  extract('async function refreshProjectSummary(', '\nfunction defaultAttributes(') + '\n' +
  extract('async function reloadWorkspaceData(', '\nasync function refreshProjectSummary(');
const deferred = () => { let resolve, reject; const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; }); return { promise, resolve, reject }; };
const tick = () => new Promise(resolve => setImmediate(resolve));
const job = (status = 'running', processed = 0) => ({ id: 1, status, processed, created_count: processed });
function harness(section = 'ai') {
  const state = { project: { id: 1, name: 'Original' }, session: { user_id: 1 }, selectionEpoch: 1, currentSection: section,
    jobs: [job()], health: { marker: 'original' }, assets: [{ id: 10 }], asset: { id: 10 }, analytics: {}, versions: [],
    lockOwned: true, savingAnnotation: false, workflowBusy: false, annotations: [], pollTimer: null };
  const calls = [], events = [], timers = new Map(), cleared = [];
  let serial = 0;
  const context = vm.createContext({ state, console,
    api: path => { const call = { path, ...deferred() }; calls.push(call); return call.promise; },
    safeApi: path => context.api(path), fallbackAnalytics: () => ({ fallback: true }),
    setInterval: callback => { const id = ++serial; timers.set(id, callback); return id; },
    clearInterval: id => { cleared.push(id); timers.delete(id); },
    navigationIsOpen: () => Boolean(context.drawer), hasAnnotationDraft: () => Boolean(context.draft), annotationOverlayOpen: () => Boolean(context.overlay),
    renderProjectSection: section => events.push(['page', section]), refreshJobActivity: () => events.push(['jobs']),
    refreshCurrentAssetFromJob: async () => events.push(['asset']), refreshInspector: () => events.push(['inspector']),
    renderWorkspace: () => events.push(['workspace']), loadAnnotations: async (...args) => events.push(['annotations', ...args]),
    acquireAssetLock: async (...args) => events.push(['lock', ...args]), releaseCurrentLock: async () => events.push(['release']),
    notify: (...args) => events.push(['notify', ...args]),
  });
  vm.runInContext(code, context);
  return { state, context, calls, events, timers, cleared, start: context.startJobPolling,
    answer(path, value) { const call = calls.find(item => item.path === path && !item.answered); assert(call, `Missing request ${path}`); call.answered = true; call.resolve(value); },
    async answerJobs(status = 'running', processed = 0) { this.answer('/api/projects/1/jobs', [job(status, processed)]); await tick(); },
    async answerHealth() { this.answer('/api/health', { marker: 'fresh' }); await tick(); },
    answerSummary() { this.answer('/api/projects/1', { id: 1, name: 'Fresh' }); this.answer('/api/projects/1/assets', [{ id: 11 }]); this.answer('/api/projects/1/analytics', { updated: true }); this.answer('/api/projects/1/versions', [{ id: 5 }]); },
  };
}

// Normal progress and completion still update the view, announce results, and stop.
{
  const h = harness(); h.start(); const timer = h.state.pollTimer;
  await h.answerJobs('running', 1); await h.answerHealth();
  assert.equal(h.state.jobs[0].processed, 1); assert.equal(h.state.health.marker, 'fresh');
  assert.deepEqual(h.events, [['page', 'ai']]);
  h.timers.get(timer)(); await h.answerJobs('completed', 2); h.answerSummary(); await tick();
  assert.equal(h.state.project.name, 'Fresh'); assert.equal(h.state.assets[0].id, 11);
  assert(h.events.some(event => event[0] === 'notify')); assert.equal(h.state.pollTimer, null); assert(!h.timers.has(timer));
}

// Leaving for the same project, returning to the same section, or changing account
// invalidates pending jobs before they can even request health or change state.
for (const change of [
  h => { h.state.selectionEpoch++; h.state.currentSection = 'overview'; },
  h => { h.state.selectionEpoch++; },
  h => { h.state.session = { user_id: 2 }; },
]) {
  const h = harness(); h.start(); change(h); h.state.pollTimer = 999;
  const snapshot = JSON.stringify(h.state); await h.answerJobs('completed', 20);
  assert.equal(JSON.stringify(h.state), snapshot); assert.deepEqual(h.events, []); assert.equal(h.calls.length, 1);
  assert(!h.cleared.includes(999), 'Old completion cannot stop the new page timer');
}

// A late health response must not mutate global health after a route or account change.
for (const change of [h => { h.state.selectionEpoch++; }, h => { h.state.session = { user_id: 2 }; }]) {
  const h = harness(); h.start(); await h.answerJobs(); change(h);
  const snapshot = JSON.stringify(h.state); await h.answerHealth();
  assert.equal(JSON.stringify(h.state), snapshot); assert.deepEqual(h.events, []);
}

// Superseding a polling loop in place also invalidates its in-flight request.
{
  const h = harness(); h.start(); const oldTimer = h.state.pollTimer; h.start(); const freshTimer = h.state.pollTimer;
  await h.answerJobs('completed', 7);
  assert.equal(h.state.jobs[0].status, 'running'); assert.equal(h.state.pollTimer, freshTimer); assert(!h.timers.has(oldTimer));
  await h.answerJobs('running', 2); await h.answerHealth(); assert.equal(h.state.jobs[0].processed, 2);
}

// Image selection invalidates one response, not the whole long-lived annotation loop.
{
  const h = harness('annotate'); h.start(); const timer = h.state.pollTimer;
  h.state.selectionEpoch++; h.state.asset = { id: 20 };
  await h.answerJobs('running', 1); assert.deepEqual(h.events, []);
  h.timers.get(timer)(); await h.answerJobs('running', 2); await h.answerHealth();
  assert.equal(h.state.jobs[0].processed, 2); assert(h.events.some(event => event[0] === 'asset'));
}

// Drawer, save/workflow, local draft and modal transitions block late paint/state writes.
for (const lock of ['drawer', 'savingAnnotation', 'workflowBusy', 'draft', 'overlay']) {
  const h = harness('annotate'); h.start();
  if (lock in h.state) h.state[lock] = true; else h.context[lock] = true;
  const snapshot = JSON.stringify(h.state); await h.answerJobs('completed', 8);
  assert.equal(JSON.stringify(h.state), snapshot, lock); assert.deepEqual(h.events, [], lock);
}

// Completion's secondary dataset refresh is guarded independently from the jobs fetch.
for (const change of [h => { h.state.selectionEpoch++; h.state.currentSection = 'overview'; }, h => { h.state.session = { user_id: 2 }; }]) {
  const h = harness(); h.start(); await h.answerJobs('completed', 3); await h.answerHealth();
  change(h); h.state.pollTimer = 999; const snapshot = JSON.stringify(h.state), count = h.events.length;
  h.answerSummary(); await tick();
  assert.equal(JSON.stringify(h.state), snapshot); assert.equal(h.events.length, count); assert(!h.cleared.includes(999));
}

// Failed requests keep the current loop available for its next tick.
{
  const h = harness(); h.start(); const timer = h.state.pollTimer; h.calls[0].answered = true; h.calls[0].reject(new Error('offline')); await tick();
  h.timers.get(timer)(); await h.answerJobs('running', 1); await h.answerHealth(); assert.equal(h.state.jobs[0].processed, 1);
}

// Annotation refresh may release or acquire an image lease. Navigation or a new
// draft during either await must not redraw or load annotations for the new page.
{
  const h = harness('annotate'), released = deferred(); h.context.releaseCurrentLock = () => released.promise;
  const pending = h.context.reloadWorkspaceData(11); h.state.selectionEpoch++; h.state.project = { id: 2 };
  released.resolve(); await pending; assert.equal(h.calls.length, 0); assert.deepEqual(h.events, []);
}
for (const change of [h => { h.state.selectionEpoch++; }, h => { h.state.session = { user_id: 2 }; }, h => { h.context.draft = true; }]) {
  const h = harness('annotate'), locked = deferred(); h.state.lockOwned = false;
  h.context.acquireAssetLock = (...args) => { h.events.push(['lock', ...args]); return locked.promise; };
  const pending = h.context.reloadWorkspaceData(null, { background: true });
  h.answer('/api/projects/1', { id: 1 }); h.answer('/api/projects/1/assets', [{ id: 10 }]);
  h.answer('/api/projects/1/jobs', []); h.answer('/api/projects/1/audit-logs?limit=100', []); await tick();
  assert.deepEqual(h.events, [['lock', 10, 1]]); change(h); const snapshot = JSON.stringify(h.state);
  locked.resolve(); await pending; assert.equal(JSON.stringify(h.state), snapshot); assert.equal(h.events.length, 1);
}
{
  const h = harness('annotate'); const pending = h.context.reloadWorkspaceData();
  h.answer('/api/projects/1', { id: 1 }); h.answer('/api/projects/1/assets', [{ id: 10 }]);
  h.answer('/api/projects/1/jobs', []); h.answer('/api/projects/1/audit-logs?limit=100', []); await pending;
  assert.deepEqual(h.events, [['workspace'], ['annotations', 10, 1]], 'Current refresh keeps the original annotation epoch');
}
console.log('PASS AI polling: live progress/completion, same-project route and session isolation, stale health/summary, loop replacement, image-switch continuity, drawer/edit guards, retry and late lease operations');
