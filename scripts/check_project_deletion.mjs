// Exercise the real project cards, deletion flow, HTTP wrapper, and modal lifecycle without deleting real data.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");
function extract(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert(from >= 0 && to > from, `Cannot locate ${start}`);
  return source.slice(from, to);
}
const code = [
  extract("function escapeHtml(", "\nfunction notify("),
  extract("function projectCard(", "\nfunction openProjectModal("),
  extract("function openModal(", "\nasync function refreshCurrentAssetFromJob("),
].join("\n");
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const project = (id, name) => ({ id, name, asset_count: 3, labeled_count: 2, approved_count: 1, label_count: 2, task_type: "detect" });

function fixture({ name = "园区安全帽", archived = false, admin = true } = {}) {
  const state = { projects: [project(7, name), project(8, "保留项目")], projectsArchived: archived,
    authReady: true, currentSection: "projects", selectionEpoch: 5, session: { role: "admin" } };
  const role = { admin }, location = { hash: "#/projects" };
  const ui = { current: null, opened: [], closed: 0, refreshes: 0, notices: [], logins: [] };
  const server = { requests: [], gate: null, status: 200, data: { deleted: true, project_id: 7, cleanup: { status: "completed", pending_count: 0 } } };
  const document = { activeElement: null, querySelector: selector => ui.current?.nodes.get(selector) || null };
  class Element {
    constructor() {
      this.isConnected = true; this.disabled = false; this.value = "browser-restored-value"; this.innerHTML = "";
      this.hidden = false; this.inert = false; this.listeners = new Map(); this.attributes = new Map();
      this.classes = new Set(["btn", "primary"]);
      this.classList = { add: value => this.classes.add(value), remove: value => this.classes.delete(value) };
    }
    addEventListener(type, listener) { this.listeners.set(type, listener); }
    setAttribute(name, value) { this.attributes.set(name, value); }
    replaceChildren() { this.innerHTML = ""; }
    focus() { document.activeElement = this; }
    getClientRects() { return this.isConnected ? [{}] : []; }
    closest() { return null; }
    dispatch(type, event = {}) { return this.listeners.get(type)?.(event); }
  }
  const modalRoot = {
    set innerHTML(html) {
      if (ui.current) for (const node of ui.current.nodes.values()) node.isConnected = false;
      ui.current = null;
      if (!html) { ui.closed++; return; }
      const nodes = new Map([".modal", ".modal-body", "#modal-submit", "#modal-cancel", "#modal-close", "#modal-feedback", "#project-delete-name"].map(selector => [selector, new Element()]));
      const modal = nodes.get(".modal"), input = nodes.get("#project-delete-name"), submit = nodes.get("#modal-submit");
      modal.parentElement = new Element();
      modal.querySelector = selector => selector === "[autofocus]" ? input : nodes.get(selector) || null;
      modal.querySelectorAll = () => [...nodes.values()];
      submit.innerHTML = "永久删除项目";
      nodes.get("#modal-feedback").hidden = true;
      ui.current = { html, nodes, modal, input, submit, cancel: nodes.get("#modal-cancel"), close: nodes.get("#modal-close"),
        body: nodes.get(".modal-body"), feedback: nodes.get("#modal-feedback") };
      ui.opened.push(ui.current);
    },
  };
  const context = vm.createContext({
    state, location, document, modalRoot, modalCleanup: null, modalReturnFocus: null, annotationMenu: null, dashboardRequest: 3,
    Headers, actorId: "admin", editorToken: "test", TASK_LABELS: { detect: "目标检测" },
    isAdmin: () => role.admin, icon: () => "", scopeWorkBlockRequest: path => path, setTimeout: () => 0,
    notify: (message, type = "success") => ui.notices.push({ message, type }),
    loadDashboard: async () => { ui.refreshes++; }, renderLoginScreen: message => ui.logins.push(message), renderAccountPage() {},
    fetch: async (path, options) => {
      server.requests.push({ path, ...options });
      if (server.gate) await server.gate.promise;
      return { ok: server.status < 400, status: server.status, json: async () => structuredClone(server.data) };
    },
  });
  vm.runInContext(code, context);
  const open = (id = 7) => { context.openProjectDeleteModal(id); return ui.current; };
  const type = (value, event = "input") => { ui.current.input.value = value; ui.current.input.dispatch(event); };
  return { context, state, role, location, ui, server, open, type, name };
}

// Both lists expose deletion only to administrators, and arbitrary names remain escaped.
for (const archived of [false, true]) {
  const h = fixture({ archived, name: '<img src=x onerror="bad()"> & 项目' });
  assert(h.context.projectCard(h.state.projects[0]).includes('data-delete-project="7"'));
  h.role.admin = false;
  assert(!h.context.projectCard(h.state.projects[0]).includes("data-delete-project"));
  h.open(); assert.equal(h.ui.current, null); assert.equal(h.server.requests.length, 0);
  h.role.admin = true;
  const modal = h.open();
  assert(!modal.html.includes("<img src=x")); assert(modal.html.includes("&lt;img"));
}

// No prefilled confirmation, exact case/spacing comparison, and validation on submission itself.
{
  const h = fixture({ name: " CVSprout 示例 " }), modal = h.open();
  assert.equal(modal.input.value, "", "Any browser-restored input is explicitly cleared");
  assert.equal(modal.submit.disabled, true);
  const inputTag = modal.html.match(/<input[^>]*id="project-delete-name"[^>]*>/)?.[0];
  assert(inputTag?.includes('autocomplete="off"'));
  assert(!/\b(?:value|placeholder)=/.test(inputTag), "The complete name is never prefilled or suggested as placeholder text");
  for (const text of ["", "CVSprout 示例", " cvsprout 示例 ", " CVSprout 示例  "]) {
    h.type(text); assert.equal(modal.submit.disabled, true);
    await modal.submit.onclick(); assert.equal(h.server.requests.length, 0);
  }
  h.type(h.name, "change"); assert.equal(modal.submit.disabled, false);
  for (const text of ["图片", "标注", "数据版本", "训练", "模型转换", "无法撤销"]) assert(modal.html.includes(text));
  h.type("changed without dispatch"); modal.input.value = "unexpected";
  await modal.submit.onclick(); assert.equal(h.server.requests.length, 0);
}

// Real DELETE request, JSON confirmation, success refresh, and partial filesystem cleanup remains a successful deletion.
for (const cleanup of [{ status: "completed", pending_count: 0 }, { status: "pending", pending_count: 2 }, { status: "pending", pending_count: 0, audit_updated: false }]) {
  const h = fixture({ name: " 精确名称 " }), modal = h.open();
  h.server.data.cleanup = cleanup;
  h.type(h.name); await modal.submit.onclick();
  assert.equal(h.server.requests.length, 1);
  const request = h.server.requests[0];
  assert.equal(request.path, "/api/projects/7"); assert.equal(request.method, "DELETE");
  assert.deepEqual(JSON.parse(request.body), { confirmation_name: h.name });
  assert.equal(request.headers.get("Content-Type"), "application/json");
  assert.equal(h.ui.current, null); assert.equal(h.ui.refreshes, 1);
  assert.deepEqual(Array.from(h.state.projects, item => item.id), [8]);
  assert.equal(h.ui.notices[0].message, cleanup.status === "pending" ? "项目已删除，部分文件待管理员清理" : "项目已删除");
  assert.equal(h.ui.notices[0].type, cleanup.status === "pending" ? "warning" : "success");
}

// A pending deletion locks submit and cancellation; another click cannot issue a second DELETE.
{
  const h = fixture(), modal = h.open(); h.type(h.name); h.server.gate = deferred();
  const pending = modal.submit.onclick();
  assert.equal(h.server.requests.length, 1); assert.equal(modal.submit.disabled, true);
  assert.equal(modal.cancel.disabled, true); assert.equal(modal.close.disabled, true); assert.equal(modal.body.inert, true);
  await modal.submit.onclick(); modal.cancel.onclick();
  assert.equal(h.server.requests.length, 1); assert.equal(h.ui.current, modal); assert.equal(h.ui.refreshes, 0);
  h.server.gate.resolve(); await pending;
  assert.equal(h.ui.refreshes, 1); assert.equal(h.ui.current, null);
}

// The server's specific conflict reason appears in the existing modal; input is retained and retry works.
for (const detail of ["项目仍有运行中的训练任务，请先结束任务", { message: "图片正由其他成员编辑，请稍后重试" }, "不能删除 <script>bad()</script> 项目"]) {
  const h = fixture(), modal = h.open(); h.type(h.name);
  h.server.status = 409; h.server.data = { detail };
  await modal.submit.onclick();
  assert.equal(h.ui.current, modal); assert.equal(modal.input.value, h.name);
  assert.equal(modal.submit.disabled, false); assert.equal(modal.cancel.disabled, false); assert.equal(modal.body.inert, false);
  assert.equal(modal.feedback.hidden, false); assert.equal(h.ui.refreshes, 0);
  assert(modal.feedback.innerHTML.includes(h.context.escapeHtml(typeof detail === "string" ? detail : detail.message)));
  assert(!modal.feedback.innerHTML.includes("<script>"));
  h.type("不匹配"); await modal.submit.onclick(); assert.equal(h.server.requests.length, 1); assert.equal(modal.submit.disabled, true);
  h.type(h.name); h.server.status = 200; h.server.data = { deleted: true };
  await modal.submit.onclick(); assert.equal(h.server.requests.length, 2); assert.equal(h.ui.refreshes, 1);
}

// Cancel (button, Escape, or outside the dialog) never submits; retained event handlers cannot revive it.
for (const cancel of [modal => modal.cancel.onclick(), modal => modal.close.onclick(),
  modal => modal.modal.dispatch("keydown", { key: "Escape", preventDefault() {} }),
  modal => modal.modal.parentElement.dispatch("pointerdown", { target: modal.modal.parentElement })]) {
  const h = fixture(), modal = h.open(); h.type(h.name); cancel(modal);
  assert.equal(h.ui.current, null); await modal.submit.onclick(); assert.equal(h.server.requests.length, 0);
}

const invalidate = [
  h => { h.state.session = { role: "admin", renewed: true }; },
  h => { h.state.selectionEpoch++; },
  h => { h.location.hash = "#/models"; },
  h => { h.state.currentSection = "models"; },
  h => { h.state.projectsArchived = true; },
  h => { h.context.dashboardRequest++; },
  h => { h.state.projects[0].name = "其他名称"; },
  h => { h.role.admin = false; },
  h => { h.state.authReady = false; },
];
for (const change of invalidate) {
  // A stale confirmation cannot start a request.
  const before = fixture(), beforeModal = before.open(); before.type(before.name); change(before);
  await beforeModal.submit.onclick(); assert.equal(before.server.requests.length, 0); assert.equal(beforeModal.submit.disabled, true);
  // A stale successful response cannot close the current modal, alter the list, or refresh a different screen/account.
  const after = fixture(), afterModal = after.open(); after.type(after.name); after.server.gate = deferred();
  const pending = afterModal.submit.onclick(); change(after); after.server.gate.resolve(); await pending;
  assert.equal(after.ui.current, afterModal); assert.equal(after.ui.refreshes, 0); assert.equal(after.ui.notices.length, 0);
  assert.equal(after.state.projects.length, 2); assert.equal(afterModal.submit.disabled, true);
}

// When a route closes the old dialog, neither late success nor late error touches its replacement.
for (const status of [200, 409]) {
  const h = fixture(), oldModal = h.open(); h.type(h.name); h.server.gate = deferred();
  const pending = oldModal.submit.onclick();
  h.context.closeModal(); const replacement = h.open(8); h.type("保留项目");
  h.server.status = status; h.server.data = status === 200 ? { deleted: true } : { detail: "旧请求冲突" };
  h.server.gate.resolve(); await pending;
  assert.equal(h.ui.current, replacement); assert.equal(replacement.input.value, "保留项目");
  assert.equal(replacement.submit.disabled, false); assert.equal(replacement.feedback.hidden, true);
  assert.equal(h.ui.notices.length, 0); assert.equal(h.ui.refreshes, 0); assert.equal(h.state.projects.length, 2);
}

console.log("PASS project deletion: admin-only active/archived cards, empty exact-name confirmation, DELETE JSON, cleanup-pending success, busy guard, specific conflict feedback and retry, cancellation, stale route/list/session/modal protection");
