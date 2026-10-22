import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");
const code = source.slice(source.indexOf("async function route("), source.indexOf("\nasync function ensureAuthenticated("));
function fixture() {
  let resolve; const release = new Promise(done => { resolve = done; });
  const state = { currentSection: "data", project: { id: 1 }, asset: { id: 10 }, authReady: true,
    lockOwned: true, selectionEpoch: 1, pollTimer: "old-poll", lockTimer: "old-lock", collaborationTimer: "old-collaboration" };
  const cleared = [], loaded = [], ui = { draft: false, cancelled: 0, notices: [], restoredHash: null };
  const location = { hash: "#/projects/2/data" };
  const context = vm.createContext({ state, location, URLSearchParams, app: {},
    authCleanup: null, canvasObserver: null, annotationMenu: null, canvasGuides: null,
    hasAnnotationDraft: () => ui.draft, closeWorkspaceNavigation() {}, closeModal() {},
    annotationBlockURL: () => "#/projects/1/annotate?asset=10", history: { replaceState: (_, __, hash) => { ui.restoredHash = hash; } },
    notify: message => ui.notices.push(message), cancelWorkspaceLoad: () => ui.cancelled++,
    clearInterval: timer => cleared.push(timer), releaseCurrentLock: () => release,
    ensureAuthenticated: async () => true,
    loadProjectSection: async (id, section) => { loaded.push([id, section]); state.pollTimer = "new-poll"; state.lockTimer = "new-lock"; state.collaborationTimer = "new-collaboration"; },
  });
  vm.runInContext(code, context);
  return { state, ui, loaded, cleared, location, resolve, route: context.route };
}
{
  const h = fixture();
  const previousRoute = h.route();
  h.state.lockOwned = false;
  h.location.hash = "#/projects/3/overview";
  await h.route();
  assert.deepEqual(h.loaded, [[3, "overview"]]);
  h.resolve(); await previousRoute;
  assert.deepEqual(h.loaded, [[3, "overview"]], "A late lock release must not resume an older route");
  assert.equal(h.state.pollTimer, "new-poll"); assert.equal(h.state.lockTimer, "new-lock");
  assert.equal(h.state.collaborationTimer, "new-collaboration");
  assert(!h.cleared.some(timer => String(timer).startsWith("new-")), "A stale navigation cannot clear the next page's timers");
}
{
  const h = fixture(); h.state.currentSection = "annotate"; h.ui.draft = true;
  await h.route();
  assert.equal(h.state.selectionEpoch, 1); assert.equal(h.ui.cancelled, 0);
  assert.equal(h.ui.restoredHash, "#/projects/1/annotate?asset=10");
  assert.equal(h.loaded.length, 0); assert.equal(h.cleared.length, 0); assert.equal(h.ui.notices.length, 1);
}
console.log("PASS route transitions: stale lock release preserves the new page/timers; unsaved annotation guard retains the current route");
