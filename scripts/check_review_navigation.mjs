// Run the real application workflow/API/navigation functions with controlled HTTP and DOM boundaries.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { annotationBlockURL, scopeWorkBlockRequest } from "../static/work-blocks-ui.js";
import { shouldContinueReview } from "../static/review-ui.js";

const source = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");
function extract(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert(from >= 0 && to > from, `Cannot locate ${start}`);
  return source.slice(from, to);
}
const functions = [
  extract("async function api(", "\nfunction notify("),
  extract("function workflowControlsMarkup(", "\nfunction refreshWorkflowState("),
  extract("async function loadAnnotations(", "\nfunction preloadNearbyAssets("),
  extract("async function selectAsset(", "\nasync function acquireAssetLock("),
  extract("async function updateWorkflow(", "\nasync function deleteSelectedAnnotation("),
].join("\n");
const image = (id, extra = {}) => ({ id, filename: `${id}.jpg`, revision: 3, review_status: "submitted", ...extra });
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const tick = () => new Promise(resolve => setImmediate(resolve));

function fixture({ blockId = null, initial = [image(1), image(2)], fresh = initial, assetId = 1,
  workflowFailure = false, queueFailure = false, annotationFailure = false, annotationGate = null, queueGate = null } = {}) {
  const state = { authReady: true, session: { actor: "reviewer" }, currentSection: "annotate", project: { id: 9, labels: [] },
    activeWorkBlockId: blockId, activeWorkBlock: blockId ? { id: blockId, project_id: 9, status: "submitted" } : null,
    assets: structuredClone(initial), selectionEpoch: 10, annotations: [{ id: 100 }], annotationsLoadedFor: assetId,
    workflowBusy: false, savingAnnotation: false, lockOwned: false, polygon: [], saveFeedback: { status: "idle" } };
  state.asset = state.assets.find(asset => asset.id === assetId);
  const server = structuredClone(fresh), requests = [], notices = [], renders = [], locks = [];
  const location = { hash: "unchanged" }, reason = { value: "" }, ui = { modal: null, closed: 0, draft: false };
  const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => structuredClone(data) });
  const context = vm.createContext({
    state, location, Headers, Date, actorId: "reviewer", editorToken: "current-page", annotationBlockURL, scopeWorkBlockRequest, shouldContinueReview,
    document: { querySelector: selector => selector === "#reject-reason" ? reason : null },
    history: { replaceState: (_state, _title, path) => { location.hash = path; } },
    hasAnnotationDraft: () => ui.draft, canReview: () => true, projectTask: () => "detect", isAdmin: () => true, canEditCurrentAsset: () => false,
    notify: (message, type) => { notices.push({ message, type }); },
    openModal: options => { ui.modal = options; }, closeModal: () => { ui.closed++; },
    refreshWorkflowState() {}, refreshInspector() {}, refreshShapes() {}, preloadNearbyAssets() {}, cancelShapeEdit() {},
    renderWorkspace: () => { renders.push({ asset: state.asset.id, busy: state.workflowBusy, loaded: state.annotationsLoadedFor }); },
    releaseCurrentLock: async id => { locks.push(["release", id]); state.lockOwned = false; },
    acquireAssetLock: async id => { locks.push(["acquire", id]); state.lockOwned = true; },
    fetch: async (path, options) => {
      requests.push({ path, body: options.body ? JSON.parse(options.body) : null, busy: state.workflowBusy });
      if (path.endsWith("/workflow")) {
        if (workflowFailure) return response({ detail: "图片版本已更新" }, 409);
        const id = Number(path.split("/")[3]), body = JSON.parse(options.body);
        const saved = server.find(asset => asset.id === id);
        saved.review_status = { approve: "approved", reject: "rejected", submit: "submitted" }[body.action] || saved.review_status;
        saved.revision++;
        return response(saved);
      }
      if (path.endsWith("/review-events")) return response([]);
      if (path.includes("/work-blocks/")) return response({ id: blockId, project_id: 9, status: "submitted" });
      if (path.startsWith("/api/projects/9/assets")) {
        if (queueGate) await queueGate.promise;
        return queueFailure ? response({ detail: "队列暂不可用" }, 503) : response(server);
      }
      if (path.endsWith("/annotations")) {
        if (annotationGate) await annotationGate.promise;
        return annotationFailure ? response({ detail: "标注读取失败" }, 503) : response([{ id: 200, asset_id: state.asset.id }]);
      }
      assert.fail(`Unexpected HTTP request ${path}`);
    },
  });
  vm.runInContext(functions, context);
  return { context, state, server, requests, notices, renders, locks, location, reason, ui };
}

// A fresh, block-scoped queue skips images already reviewed and leases held by another user or tab.
{
  const gate = deferred(), future = new Date(Date.now() + 60000).toISOString();
  const run = fixture({ blockId: 7, initial: [1, 2, 3, 4, 5].map(id => image(id)), annotationGate: gate,
    fresh: [image(1), image(2, { review_status: "approved" }), image(3, { lock_owner: "someone", lock_expires_at: future }),
      image(4, { lock_owner: "reviewer", lock_token: "other-tab", lock_expires_at: future }), image(5)] });
  const pending = run.context.updateWorkflow("approve");
  await tick();
  assert.equal(run.state.asset.id, 5);
  assert.equal(run.state.workflowBusy, true, "Workflow stays busy through next-image loading");
  assert.equal(run.state.annotationsLoadedFor, null);
  assert(run.requests.some(request => request.path === "/api/projects/9/assets?work_block_id=7"), "The actual API wrapper retains the current block scope");
  assert(run.requests.every(request => request.busy), "All workflow, queue and annotation requests happen under the busy guard");
  const count = run.requests.filter(request => request.path.endsWith("/workflow")).length;
  await run.context.updateWorkflow("approve");
  await run.context.selectAsset(2);
  assert.equal(run.requests.filter(request => request.path.endsWith("/workflow")).length, count, "Another approval cannot overlap automatic navigation");
  assert.equal(run.state.asset.id, 5, "Manual navigation cannot overlap automatic navigation");
  gate.resolve(); await pending;
  assert.equal(run.state.workflowBusy, false);
  assert.equal(run.state.annotationsLoadedFor, 5);
  assert.equal(run.state.assetFilter, "submitted");
  assert.equal(run.location.hash, "#/projects/9/annotate?block=7&asset=5");
}

// Current-page leases and expired leases remain usable, and the queue wraps around.
for (const lease of [{ lock_owner: "reviewer", lock_token: "current-page", lock_expires_at: new Date(Date.now() + 60000).toISOString() },
  { lock_owner: "someone", lock_expires_at: "2000-01-01T00:00:00Z" }]) {
  const run = fixture({ assetId: 2, initial: [image(1, lease), image(2)] });
  await run.context.updateWorkflow("approve");
  assert.equal(run.state.asset.id, 1);
}

for (const blockId of [null, 7]) {
  const run = fixture({ blockId, initial: [image(1)] });
  await run.context.updateWorkflow("approve");
  assert.equal(run.location.hash, blockId ? "#/projects/9/blocks" : "#/projects/9/data?status=submitted");
  assert(run.notices.some(notice => notice.message.includes("审核完毕")));
  assert.equal(run.state.workflowBusy, false);
}

for (const mode of ["workflowFailure", "queueFailure"]) {
  const run = fixture({ [mode]: true });
  await run.context.updateWorkflow("approve");
  assert.equal(run.state.asset.id, 1);
  assert.equal(run.location.hash, "unchanged", "Failed review/queue refresh must not jump to another image");
  assert.equal(run.state.workflowBusy, false);
  assert(run.notices.some(notice => notice.type === (mode === "workflowFailure" ? "error" : "warning")));
  assert.equal(run.state.asset.review_status, mode === "workflowFailure" ? "submitted" : "approved");
}

// A stale queue response cannot move a user who has navigated elsewhere.
{
  const gate = deferred(), run = fixture({ queueGate: gate });
  const pending = run.context.updateWorkflow("approve"); await tick();
  run.state.selectionEpoch++;
  run.state.currentSection = "data";
  gate.resolve(); await pending;
  assert.equal(run.location.hash, "unchanged");
  assert(!run.requests.some(request => request.path.endsWith("/annotations")));
}

// Finishing a request from the previous page must not unlock a new page's active review.
{
  const previousGate = deferred(), currentGate = deferred(), run = fixture();
  const fetch = run.context.fetch;
  let mutations = 0;
  run.context.fetch = async (path, options) => {
    if (path.endsWith("/workflow")) await (++mutations === 1 ? previousGate.promise : currentGate.promise);
    return fetch(path, options);
  };
  const previous = run.context.updateWorkflow("approve"); await tick();
  run.state.selectionEpoch++;
  run.state.workflowBusy = false; run.state.workflowToken = null;
  run.state.asset = run.state.assets[1]; run.state.annotationsLoadedFor = 2;
  const current = run.context.updateWorkflow("approve"); await tick();
  assert.equal(mutations, 2);
  assert.equal(run.state.workflowBusy, true);
  previousGate.resolve(); await previous;
  assert.equal(run.state.workflowBusy, true, "An obsolete workflow's finally cannot clear the current request's busy guard");
  await run.context.updateWorkflow("approve");
  assert.equal(mutations, 2, "Duplicate approval remains blocked after the old request finishes");
  currentGate.resolve(); await current;
  assert.equal(run.state.workflowBusy, false);
  assert.equal(run.state.workflowToken, null);
}

// Review requires loaded annotations, including after a failed load on the next image.
for (const failure of [false, true]) {
  const run = fixture({ annotationFailure: failure });
  if (failure) { await run.context.updateWorkflow("approve"); assert.equal(run.state.asset.id, 2); }
  else run.state.annotationsLoadedFor = null;
  const before = run.requests.length;
  await run.context.updateWorkflow("approve"); await run.context.updateWorkflow("reject");
  assert.equal(run.requests.length, before);
  assert.equal(run.ui.modal, null);
  assert(run.notices.some(notice => notice.message.includes("尚未加载完成")));
  const markup = run.context.workflowControlsMarkup();
  for (const action of ["approve", "reject"]) assert(new RegExp(`data-action="${action}"[^>]*disabled`).test(markup));
}

for (const reason of ["", "   ", "  请补充漏标  "]) {
  const run = fixture();
  await run.context.updateWorkflow("reject");
  assert(run.ui.modal.body.includes("可选"));
  run.reason.value = reason;
  await run.ui.modal.onSubmit();
  const request = run.requests.find(item => item.path.endsWith("/workflow"));
  assert.equal(request.body.action, "reject");
  assert.equal(request.body.reason, reason.trim());
  assert.equal(run.state.asset.id, 2, "A rejection with an empty reason also advances");
}

for (const stale of ["asset", "revision", "selection", "session", "section"]) {
  const run = fixture();
  await run.context.updateWorkflow("reject");
  if (stale === "asset") run.state.asset = image(2);
  if (stale === "revision") run.state.asset.revision++;
  if (stale === "selection") run.state.selectionEpoch++;
  if (stale === "session") run.state.session = { actor: "another-reviewer" };
  if (stale === "section") run.state.currentSection = "data";
  await assert.rejects(run.ui.modal.onSubmit(), /图片已更新|页面已切换|登录状态|重新检查/);
  assert.equal(run.requests.length, 0, "A stale rejection dialog must not send a mutation");
}

console.log("PASS real review API/navigation: scoped fresh queue, locked/already-reviewed skips, automatic advance, serialized loads, failure guards, completion destinations, optional rejection notes, stale dialogs, annotations-load guard");
