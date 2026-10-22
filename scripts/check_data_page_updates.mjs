// Exercise the actual data-page rendering and delegated module bindings with a small DOM boundary.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { reviewQueueMarkup, batchReviewSelectionMarkup } from "../static/review-ui.js";

const appSource = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");
const code = appSource.slice(appSource.indexOf("function filteredDataAssets("), appSource.indexOf("\nfunction renderAiPage("));
assert(code.includes("function renderDataPage("));
const listeners = new Map(), actions = { review: [], deletion: [], ai: [], uploads: 0, exports: 0, appWrites: 0, bindings: 0 };
const document = { activeElement: null, addEventListener(type, handler) { const handlers = listeners.get(type) || []; handlers.push(handler); listeners.set(type, handlers); } };
const decode = text => text.replaceAll("&quot;", '"').replaceAll("&#39;", "'").replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&amp;", "&");
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
const dataKey = name => name.slice(5).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
const voidTags = new Set(["input", "img", "br", "hr", "meta", "link"]);

class Element {
  constructor(tag, attrs = {}) {
    this.tag = tag; this.parentElement = null; this.children = []; this.attrs = new Map(); this.dataset = {};
    this.classes = new Set(); this.listeners = new Map(); this.disabled = false; this.checked = false; this.hidden = false; this.value = ""; this.scrollTop = 0; this._text = "";
    this.classList = { toggle: (name, force) => { const add = force ?? !this.classes.has(name); if (add) this.classes.add(name); else this.classes.delete(name); return add; }, contains: name => this.classes.has(name) };
    for (const [name, value] of Object.entries(attrs)) this.setAttribute(name, value);
  }
  get isConnected() { return this.tag === "document" || Boolean(this.parentElement?.isConnected); }
  get firstElementChild() { return this.children[0] || null; }
  setAttribute(name, value) {
    this.attrs.set(name, String(value));
    if (name === "class") this.classes = new Set(value.split(/\s+/));
    else if (name.startsWith("data-")) this.dataset[dataKey(name)] = String(value);
    else if (["disabled", "checked", "hidden"].includes(name)) this[name] = true;
    else if (name === "value") this.value = String(value);
    else if (name === "id") this.id = value;
  }
  getAttribute(name) { return this.attrs.get(name) ?? null; }
  matches(selector) {
    if (selector.includes(",")) return selector.split(",").some(part => this.matches(part.trim()));
    if (selector.startsWith("#")) return this.id === selector.slice(1);
    if (selector.startsWith(".")) return this.classes.has(selector.slice(1));
    const attribute = selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
    if (attribute) return this.attrs.has(attribute[1]) && (attribute[2] == null || this.attrs.get(attribute[1]) === attribute[2]);
    return this.tag === selector;
  }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; }
  querySelectorAll(selector) { const result = []; for (const child of this.children) { if (child.matches(selector)) result.push(child); result.push(...child.querySelectorAll(selector)); } return result; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  insertBefore(node, before) { node.remove(); const index = before ? this.children.indexOf(before) : this.children.length; assert(index >= 0); this.children.splice(index, 0, node); node.parentElement = this; return node; }
  remove() { if (this.parentElement) { const children = this.parentElement.children; children.splice(children.indexOf(this), 1); this.parentElement = null; } }
  replaceChildren(...nodes) { for (const child of this.children) child.parentElement = null; this.children = []; this._text = ""; for (const node of nodes) this.insertBefore(node, null); }
  replaceWith(node) { const parent = this.parentElement; parent.insertBefore(node, this); this.remove(); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(""); }
  set innerHTML(html) {
    if (this.id === "app") actions.appWrites++;
    this.replaceChildren(); this._html = html;
    const stack = [this];
    for (const token of html.match(/<[^>]*>|[^<]+/g) || []) {
      if (token.startsWith("</")) { if (stack.length > 1) stack.pop(); continue; }
      if (token.startsWith("<")) {
        const tag = token.match(/^<([\w-]+)/)?.[1]; if (!tag) continue;
        const attrs = {};
        const attributeSource = token.slice(tag.length + 1).replace(/\/?\s*>$/, "");
        for (const match of attributeSource.matchAll(/([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s]+)))?/g)) attrs[match[1]] = decode(match[2] ?? match[3] ?? match[4] ?? "");
        const node = new Element(tag, attrs); stack.at(-1).insertBefore(node, null);
        if (!voidTags.has(tag) && !token.endsWith("/>")) stack.push(node);
      } else stack.at(-1)._text += decode(token);
    }
  }
  get innerHTML() { return this._html || ""; }
  focus() { document.activeElement = this; }
  addEventListener(type, handler) { const handlers = this.listeners.get(type) || []; handlers.push(handler); this.listeners.set(type, handlers); }
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
}
const root = new Element("document"), app = new Element("div", { id: "app" }); root.insertBefore(app, null);
document.querySelector = selector => root.querySelector(selector);
document.querySelectorAll = selector => root.querySelectorAll(selector);
document.createElement = tag => {
  if (tag !== "template") return new Element(tag);
  const fragment = new Element("fragment");
  return { content: fragment, set innerHTML(html) { fragment.innerHTML = html; } };
};
function dispatch(type, target, extra = {}) {
  const event = { target, ...extra };
  for (let node = target; node; node = node.parentElement) {
    if (!type.startsWith("composition")) node[`on${type}`]?.(event);
    for (const listener of node.listeners.get(type) || []) listener(event);
  }
  for (const listener of listeners.get(type) || []) listener(event);
}
function delegatedBinding(file, start, end, mocks) {
  const source = readFileSync(new URL(`../static/${file}`, import.meta.url), "utf8");
  const from = source.indexOf(start), to = source.indexOf(end, from); assert(from >= 0 && to > from);
  const scope = vm.createContext({ document, ...mocks });
  vm.runInContext(`let context; let bound = false;\n${source.slice(from, to).replace("export function", "function")}`, scope);
  return scope[start.match(/function (\w+)/)[1]];
}
const bindReviewPage = delegatedBinding("review-ui.js", "export function bindReviewPage(", "\nfunction openJobReview(", {
  openBatchReview: action => actions.review.push(action), openReviewPicker() {}, openJobReview() {},
});
const bindAssetManagement = delegatedBinding("asset-management-ui.js", "export function bindAssetManagement(", "\nasync function openDeleteAssets(", {
  openDeleteAssets: ids => actions.deletion.push([...ids]),
});
const assets = Array.from({ length: 450 }, (_, index) => ({ id: index + 1, filename: `cam_${String(index + 1).padStart(3, "0")}${[6, 7].includes(index) ? "_中文" : ""}.png`,
  width: 100, height: 80, size_bytes: 1000, status: "labeled", review_status: index % 10 === 0 ? "submitted" : "draft", annotation_count: 1 }));
const state = { currentSection: "data", project: { id: 9 }, selectionEpoch: 4, session: { role: "admin" },
  assets, dataSearch: "", dataStatus: "all", assetRenderLimit: 200, selectedAssetIds: new Set(), workBlocks: { enabled: false, unassigned_count: 0 } };
const location = { hash: "#/projects/9/data" };
const context = vm.createContext({ state, document, app, location, history: { replaceState: (_a, _b, path) => { location.hash = path; } },
  escapeHtml, icon: () => "", projectTask: () => "detect", isAdmin: () => state.session.role === "admin", canReview: () => state.session.role !== "annotator",
  formatBytes: size => `${size} B`, reviewStatusName: status => status, reviewQueueMarkup, batchReviewSelectionMarkup,
  projectPageShell: (_section, body) => `<div class="app-shell"><aside id="test-navigation"></aside><main class="project-page">${body}</main></div>`,
  openUploadModal: () => { actions.uploads++; }, openExportModal: () => { actions.exports++; }, openAiModal: ids => actions.ai.push([...ids]),
  bindCommonProjectEvents: () => { actions.bindings++; const deps = { state, renderDataPage: context.renderDataPage }; bindReviewPage(deps); bindAssetManagement(deps); },
});
vm.runInContext(code, context);
const node = selector => document.querySelector(selector);
const card = id => node(`[data-open-asset="${id}"]`)?.closest(".data-asset-card");
const search = text => { const input = node("#data-search"); input.value = text; dispatch("input", input, { isComposing: false }); };
context.renderDataPage();
assert.equal(actions.appWrites, 1); assert.equal(node("#data-results").children.length, 200);
assert.equal(listeners.get("click").length, 2, "Review and asset management each install one delegated listener");
const page = node(".project-page"), grid = node("#data-results"), input = node("#data-search"), status = node("#data-status"), navigation = node("#test-navigation");
const first = card(1), firstImage = first.querySelector("img");
input.focus(); input.value = "cam"; input.setSelectionRange(1, 2); page.scrollTop = 1100;
dispatch("input", input, { isComposing: false });
assert.equal(actions.appWrites, 1); assert.equal(node("#data-search"), input); assert.equal(document.activeElement, input);
assert.equal(input.selectionStart, 1); assert.equal(input.selectionEnd, 2); assert.equal(page.scrollTop, 1100);
assert.equal(card(1), first); assert.equal(card(1).querySelector("img"), firstImage); assert.equal(node("#test-navigation"), navigation);
assert.equal(node(".data-result-count").textContent, "450 / 450 张");

// Chinese composition is not committed or overwritten by an intervening page refresh.
const chineseCard = card(7);
dispatch("compositionstart", input); input.value = "中文"; dispatch("input", input, { isComposing: true });
context.renderDataPage();
assert.equal(state.dataSearch, "cam"); assert.equal(input.value, "中文"); assert.equal(grid.children.length, 200);
dispatch("compositionend", input); dispatch("input", input, { isComposing: false });
assert.equal(state.dataSearch, "中文"); assert.equal(grid.children.length, 2); assert.equal(card(7), chineseCard);
assert.equal(document.activeElement, input); assert.equal(page.scrollTop, 1100); assert.equal(actions.appWrites, 1);

// Select-all uses the latest filtering, and both old and newly generated batch-review controls remain delegated.
search(""); status.value = "submitted"; dispatch("change", status);
assert.equal(node("#data-status"), status); assert.equal(grid.children.length, 45);
dispatch("click", node("#select-visible")); assert.equal(state.selectedAssetIds.size, 45);
assert([...state.selectedAssetIds].every(id => state.assets.find(asset => asset.id === id).review_status === "submitted"));
dispatch("click", node('[data-batch-review="approve"]')); assert.deepEqual(actions.review, ["approve"]);
dispatch("click", node("#delete-selected-assets")); assert.equal(actions.deletion[0].length, 45);
dispatch("click", node("#clear-selection")); assert.equal(state.selectedAssetIds.size, 0);
assert.equal(node("#annotate-selection").disabled, true);

// Review shortcuts call the actual delegated render callback, which keeps their own button and the toolbar intact.
const reviewAll = node('[data-review-filter="all"]'), reviewSubmitted = node('[data-review-filter="submitted"]');
dispatch("click", reviewAll); assert.equal(state.dataStatus, "all"); assert.equal(node('[data-review-filter="all"]'), reviewAll);
assert.equal(grid.children.length, 200); assert.equal(node("#data-search"), input); assert.equal(actions.appWrites, 1);
const originalCards = [...grid.children], more = node("#load-more-data");
page.scrollTop = 12000; more.focus(); dispatch("click", more);
assert.equal(grid.children.length, 400); assert.equal(page.scrollTop, 12000); assert.equal(document.activeElement, more);
originalCards.forEach((original, index) => assert.equal(grid.children[index], original, "Load-more preserves every existing card and image node"));
assert.equal(node("#load-more-data"), more); assert.equal(actions.appWrites, 1);
const addedCheckbox = node('[data-select-asset="350"]'); addedCheckbox.checked = true; dispatch("change", addedCheckbox);
assert(state.selectedAssetIds.has(350)); assert(card(350).classList.contains("selected"));
dispatch("click", node('[data-batch-review="submit"]')); assert.deepEqual(actions.review, ["approve", "submit"]);
dispatch("click", node('[data-delete-asset="350"]')); assert.deepEqual(actions.deletion.at(-1), [350]);
dispatch("click", node("#annotate-selection")); assert.deepEqual(actions.ai.at(-1), [350]);
dispatch("click", node('[data-open-asset="350"]')); assert.equal(location.hash, "#/projects/9/annotate?asset=350");
location.hash = "#/projects/9/data";
dispatch("click", more); assert.equal(grid.children.length, 450); assert.equal(more.hidden, true);
originalCards.forEach((original, index) => assert.equal(grid.children[index], original));

// Review/deletion result refreshes change only the necessary metadata; queue/block information stays current.
const retainedCard = card(1), retainedImage = retainedCard.querySelector("img"), selectionAction = node('[data-batch-review="reject"]');
selectionAction.focus(); state.assets[0] = { ...state.assets[0], review_status: "approved", annotation_count: 4 };
state.workBlocks = { enabled: true, unassigned_count: 17 }; context.renderDataPage();
assert.equal(card(1), retainedCard); assert.equal(card(1).querySelector("img"), retainedImage);
assert(card(1).querySelector(".data-card-body").textContent.includes("4 个标注"));
assert.equal(reviewSubmitted.querySelector("strong").textContent, "44");
assert(node(".work-block-entry").textContent.includes("17 张未分块"));
assert.equal(node('[data-batch-review="reject"]'), selectionAction); assert.equal(document.activeElement, selectionAction);
assert.equal(page.scrollTop, 12000);
state.selectedAssetIds = new Set([351]); context.renderDataPage();
assert.equal(node('[data-batch-review="reject"]'), selectionAction, "Changing IDs with the same selection markup preserves the focused action");
assert.equal(document.activeElement, selectionAction); assert.equal(node('[data-select-asset="350"]').checked, false); assert.equal(node('[data-select-asset="351"]').checked, true);
state.selectedAssetIds = new Set([350]); context.renderDataPage();
state.assets = state.assets.filter(asset => ![2, 350].includes(asset.id)); state.selectedAssetIds.delete(350); context.renderDataPage();
assert.equal(card(2), undefined); assert.equal(card(350), undefined); assert.equal(card(1), retainedCard);
assert.equal(node("#delete-selected-assets").disabled, true); assert.equal(node(".data-result-count").textContent, "448 / 448 张");

// Empty-state actions use the same stable container; no new per-card listeners are required.
search("nothing-matches"); assert.equal(node("#select-visible").disabled, true);
dispatch("click", node("#reset-data-filters"));
assert.equal(state.dataSearch, ""); assert.equal(state.dataStatus, "all"); assert.equal(input.value, ""); assert.equal(document.activeElement, input);
assert.equal(grid.children.length, 200); assert.equal(actions.appWrites, 1);
state.assets = []; context.renderDataPage(); dispatch("click", node("#empty-data-upload"));
assert.equal(actions.uploads, 1); assert.equal(node("#data-export").disabled, true);
assert.equal(actions.bindings, 1); assert.equal(listeners.get("click").length, 2);

// Old event callbacks cannot mutate a newly selected project, and a genuinely new page still gets a fresh shell.
state.project = { id: 10 }; state.selectionEpoch++;
input.value = "stale search"; dispatch("input", input, { isComposing: false }); assert.equal(state.dataSearch, "");
context.renderDataPage(); assert.equal(actions.appWrites, 2); assert.equal(grid.isConnected, false);
assert.equal(listeners.get("click").length, 2, "Returning to a new data page never duplicates the module handlers");
const freshGrid = node("#data-results"); state.session = { role: "admin" }; context.renderDataPage();
assert.equal(actions.appWrites, 3); assert.equal(freshGrid.isConnected, false);

// The same selected IDs can gain reviewer permission without changing admin access or replacing the page.
state.assets = assets.slice(0, 5); state.selectedAssetIds = new Set([1]); state.session = { role: "annotator" }; context.renderDataPage();
const permissionGrid = node("#data-results"), beforePermissionWrites = actions.appWrites;
assert.equal(node('[data-batch-review="approve"]'), null); assert(node("#selection-slot").textContent.includes("批量通过和退回由审核员处理"));
state.session.role = "reviewer"; context.renderDataPage();
assert.equal(node("#data-results"), permissionGrid); assert.equal(actions.appWrites, beforePermissionWrites);
assert(node('[data-batch-review="approve"]')); assert(!node("#selection-slot").textContent.includes("批量通过和退回由审核员处理"));
assert.equal(node(".review-queue-shortcuts").querySelector("span").textContent, "审核队列");

console.log("PASS data page updates: stable search/toolbar/cards/images/scroll, Chinese composition, append-only load-more, live filtering and selections, delegated new review/delete/open controls, metadata/queue/block refresh, empty-state actions, stale project/session guards");
