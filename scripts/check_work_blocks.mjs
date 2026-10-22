// Exercise the actual browser request, ownership, and workflow guards without a server.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const app = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");
const blocks = readFileSync(new URL("../static/work-blocks-ui.js", import.meta.url), "utf8").replace(/^export /gm, "");
const context = vm.createContext({ URLSearchParams });
vm.runInContext(blocks, context);
const { scopeWorkBlockRequest: scope, annotationBlockURL: url } = context;
const state = { currentSection: "annotate", activeWorkBlockId: 7, activeWorkBlock: { project_id: 1 } };
assert.equal(scope("/api/projects/1/assets", state), "/api/projects/1/assets?work_block_id=7");
assert.equal(scope("/api/projects/1/assets?status=draft&work_block_id=99", state), "/api/projects/1/assets?status=draft&work_block_id=7");
for (const path of ["/api/assets/10", "/api/projects/1/assets/10", "/api/projects/1/jobs", "/api/projects/2/assets"]) assert.equal(scope(path, state), path);
assert.equal(scope("/api/projects/1/assets", state, "POST"), "/api/projects/1/assets");
assert.equal(scope("/api/projects/1/assets", { ...state, currentSection: "data" }), "/api/projects/1/assets");
assert.equal(scope("/api/projects/1/assets", { ...state, activeWorkBlockId: null }), "/api/projects/1/assets");
assert.equal(url(1, 7, 10), "#/projects/1/annotate?block=7&asset=10");
assert.equal(url(1, 7), "#/projects/1/annotate?block=7");
assert.equal(url(1, null, 10), "#/projects/1/annotate?asset=10");

function extract(start, end) {
  const begin = app.indexOf(start), finish = app.indexOf(end, begin);
  assert(begin >= 0 && finish > begin, `Missing function boundary ${start}`);
  return app.slice(begin, finish);
}

const calls = [];
const requestState = { ...state, session: { user_id: 1 } };
const requestContext = vm.createContext({
  state: requestState, Headers, scopeWorkBlockRequest: scope, actorId: "session-actor", editorToken: "page-one-token",
  fetch: async (path, options) => { calls.push({ path, options }); return { ok: true, status: 200, json: async () => path.startsWith("/api/assets/") ? { id: 10 } : [{ id: 10 }, { id: 11 }] }; },
});
vm.runInContext(extract("async function api(", "\nfunction jsonOptions("), requestContext);
await requestContext.api("/api/projects/1/assets");
assert.equal(calls.at(-1).path, "/api/projects/1/assets?work_block_id=7");
assert.equal(calls.at(-1).options.headers.get("X-Edit-Token"), "page-one-token");
assert.equal(calls.at(-1).options.headers.get("X-MDP-Client"), "browser");
requestContext.editorToken = "page-two-token";
await requestContext.api("/api/assets/10/lock", { method: "DELETE", keepalive: true });
assert.equal(calls.at(-1).options.headers.get("X-Edit-Token"), "page-two-token");
requestState.activeWorkBlockId = null; requestState.legacyReviewAssetId = 10;
const legacy = await requestContext.api("/api/projects/1/assets");
assert.equal(calls.at(-1).path, "/api/assets/10");
assert.equal(legacy.length, 1); assert.equal(legacy[0].id, 10);

const editor = {
  asset: { id: 10, review_status: "draft", negative_confirmed: false }, annotations: [],
  lockOwned: true, readOnly: false, savingAnnotation: false, workflowBusy: false,
  saveFeedback: { status: "idle" }, activeWorkBlock: { is_mine: true, status: "in_progress" },
};
let task = "detect";
const workflowContext = vm.createContext({ state: editor, projectTask: () => task, canReview: () => false, isAdmin: () => false, hasAnnotationDraft: () => false });
vm.runInContext(extract("function canEditCurrentAsset(", "\nfunction hasAnnotationDraft("), workflowContext);
vm.runInContext(extract("function workflowControlsMarkup(", "\nfunction refreshWorkflowState("), workflowContext);
const markup = () => workflowContext.workflowControlsMarkup();
const disabled = (html, action) => new RegExp(`data-action="${action}"[^>]*disabled`).test(html);
assert(markup().includes('data-action="confirm_empty"'));
assert(disabled(markup(), "submit")); assert(!disabled(markup(), "confirm_empty"));
editor.asset.negative_confirmed = true;
assert(!disabled(markup(), "submit")); assert(!markup().includes('data-action="confirm_empty"'));
task = "classify";
assert(!markup().includes('data-action="confirm_empty"')); assert(disabled(markup(), "submit"));
task = "detect"; editor.activeWorkBlock.is_mine = false;
assert.equal(workflowContext.canEditCurrentAsset(), false);
assert(disabled(markup(), "submit")); assert(!markup().includes('data-action="confirm_empty"'));
editor.activeWorkBlock.is_mine = true; editor.legacyReviewAssetId = 10;
assert.equal(workflowContext.canEditCurrentAsset(), false);
editor.legacyReviewAssetId = null; editor.asset.review_status = "approved"; editor.activeWorkBlock.status = "completed";
assert(!markup().includes('data-action="reopen"'));

// A successful claim from a page or account that is no longer active must not navigate.
for (const transition of ["session", "route"]) {
  let resolve;
  const claim = new Promise(done => { resolve = done; });
  const button = { dataset: { blockClaim: "7" }, disabled: false, isConnected: true };
  const fixture = { project: { id: 1 }, currentSection: "blocks", selectionEpoch: 2, session: { user_id: 1 }, workBlocks: { blocks: [] } };
  const location = { hash: "unchanged" }, messages = [];
  const harness = vm.createContext({ URLSearchParams, location, document: {
    querySelector: selector => selector === "[data-block-refresh]" ? {} : null,
    querySelectorAll: selector => selector === "[data-block-claim]" ? [button] : [],
  } });
  vm.runInContext(blocks, harness);
  harness.bindWorkBlocks({ state: fixture, api: () => claim, jsonOptions: (method, body) => ({ method, body }),
    notify: message => messages.push(message), refresh: async () => {}, render: () => {} });
  const pending = button.onclick({ currentTarget: button });
  assert.equal(button.disabled, true);
  if (transition === "session") fixture.session = { user_id: 2 };
  else fixture.selectionEpoch++;
  resolve({ id: 7, name: "任务块 7" }); await pending;
  assert.equal(location.hash, "unchanged"); assert.equal(messages.length, 0);
}

console.log("PASS block-scoped API/navigation and legacy single-image review; page-specific lock headers; negative-confirmation/classification/ownership guards; completed block readonly; stale claim responses cannot navigate another account/page.");
