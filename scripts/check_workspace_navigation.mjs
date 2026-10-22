// Exercise actual navigation markup and event handlers with controlled browser boundaries.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const navigationSource = readFileSync(new URL("../static/workspace-navigation.js", import.meta.url), "utf8").replaceAll("export function ", "function ");
const appSource = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");
const topbar = appSource.slice(appSource.indexOf("function topbar("), appSource.indexOf("\nasync function route("));
assert(topbar.length > 0);

function fixture({ mobile = false, hash = "#/projects/7/train" } = {}) {
  const viewport = { mobile }, location = { hash }, tasks = [], storage = new Map(), events = new Map(), windowEvents = new Map();
  const theme = { preference: "system", calls: [] }, ui = { nodes: [], current: null, html: "", mediaListener: null };
  const state = { session: { user_id: 1, display_name: "测试管理员", role: "admin" }, authMode: "accounts", currentSection: "train",
    project: { id: 7, name: "园区视觉 <标注>", asset_count: 42, task_type: "detect" }, health: { annotation_workers: { status: "online" } } };
  const document = { activeElement: null, body: { isConnected: true, matches: () => false }, documentElement: { dataset: { themePreference: "system" }, textContent: "APPLICATION CONTENT" }, addEventListener: (type, handler) => { const list = events.get(type) || []; list.push(handler); events.set(type, list); } };
  class Element {
    constructor(tag, parent = null, { classes = [], dataset = {}, attrs = {}, id = "" } = {}) {
      this.tag = tag; this.parent = parent; this.parentElement = parent; this.dataset = dataset; this.attrs = new Map(Object.entries(attrs)); this.id = id;
      this.classes = new Set(classes); this.isConnected = true; this.inert = false; this.open = false; this.disabled = false; this.value = "";
      this.classList = { contains: value => this.classes.has(value), toggle: (value, force) => { const add = force ?? !this.classes.has(value); if (add) this.classes.add(value); else this.classes.delete(value); return add; } };
      ui.nodes.push(this);
    }
    matches(selector) {
      if (selector === ".workspace-navigation a") return this.tag === "a" && !!this.closest(".workspace-navigation");
      if (selector.startsWith(".")) return this.classes.has(selector.slice(1));
      if (selector.startsWith("#")) return this.id === selector.slice(1);
      if (selector === "[data-navigation-menu]") return !!this.dataset.navigationMenu;
      if (selector === "[data-theme-select]") return this.dataset.themeSelect != null;
      if (selector === "[inert]") return this.inert;
      if (selector === "details:not([open])") return this.tag === "details" && !this.open;
      if (selector === "[data-toggle-navigation]") return this.dataset.toggleNavigation != null;
      if (selector === "[data-close-navigation]") return this.dataset.closeNavigation != null;
      return this.tag === selector;
    }
    closest(selector) { for (let node = this; node; node = node.parent) if (node.matches(selector)) return node; return null; }
    contains(node) { for (; node; node = node.parent) if (node === this) return true; return false; }
    setAttribute(key, value) { this.attrs.set(key, value); }
    getAttribute(key) { return this.attrs.get(key) ?? null; }
    removeAttribute(key) { this.attrs.delete(key); }
    focus() { document.activeElement = this; }
    getClientRects() {
      // Chromium can retain layout boxes for descendants of closed details.
      // The application must reject those through the independent visibility guard.
      if (this.matches(".navigation-collapse") && viewport.mobile || this.matches(".navigation-close") && !viewport.mobile) return [];
      if (this.matches(".navigation-toggle") && !viewport.mobile) return [];
      return this.isConnected ? [{}] : [];
    }
    querySelector(selector) {
      if (selector === "summary" || selector === ":scope > summary") return this.summary;
      return this.querySelectorAll(selector)[0] || null;
    }
    querySelectorAll(selector) {
      const children = ui.nodes.filter(node => node !== this && this.contains(node));
      if (selector === "[data-navigation-menu][open]") return children.filter(node => node.dataset.navigationMenu && node.open);
      if (selector === '[aria-current="page"]') return children.filter(node => node.attrs.get("aria-current") === "page");
      if (selector.includes("a[href],button")) {
        assert(selector.includes("summary") && selector.includes("select"), "Mobile trap includes newly added footer controls");
        return children.filter(node => ["a", "button", "summary", "select"].includes(node.tag));
      }
      return children.filter(node => node.matches(selector));
    }
  }
  document.querySelectorAll = selector => {
    const current = ui.current; if (!current) return [];
    if (selector === ".app-shell > main, .app-shell > .topbar") return [current.main, current.header];
    if (selector.includes("[data-navigation-menu][open]")) return [current.themeMenu, current.accountMenu].filter(node => node.open);
    if (selector === ".workspace-navigation a") return ui.nodes.filter(node => node.tag === "a" && current.nav.contains(node));
    if (selector === "[data-theme-preference]") return [document.documentElement];
    if (selector === "[data-navigation-theme-label]") return [current.themeLabel];
    const summary = selector.match(/^\[data-navigation-menu="(theme|account)"\] > summary$/);
    if (summary) return [current[`${summary[1]}Menu`].summary];
    return ui.nodes.filter(node => node.isConnected && node.matches(selector));
  };
  document.querySelector = selector => document.querySelectorAll(selector)[0] || null;
  const media = { get matches() { return viewport.mobile; }, addEventListener: (_type, fn) => { ui.mediaListener = fn; } };
  const window = { matchMedia: () => media, addEventListener: (type, handler) => windowEvents.set(type, handler),
    dispatchEvent: event => windowEvents.get(event.type)?.(event), history: { replaceState() {} },
    CVSproutTheme: { getPreference: () => theme.preference, getTheme: () => theme.preference === "dark" ? "dark" : "light",
      setPreference: value => { theme.calls.push(value); theme.preference = value; } } };
  const esc = value => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;");
  const context = vm.createContext({ window, document, location, state, Event, URLSearchParams, queueMicrotask: fn => tasks.push(fn),
    getComputedStyle: node => ({ visibility: viewport.mobile && node.closest(".workspace-navigation") && !ui.current.nav.classList.contains("is-open") ? "hidden" : "visible" }), sessionStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    icon: name => `<svg data-icon="${name}"></svg>`, escapeHtml: esc, isAdmin: () => state.session.role === "admin", projectTask: () => "detect", TASK_LABELS: { detect: "目标检测" } });
  vm.runInContext(navigationSource + "\n" + topbar, context);
  const flush = () => { while (tasks.length) tasks.shift()(); };
  function mount(html) {
    for (const node of ui.nodes) node.isConnected = false;
    ui.nodes = []; ui.html = html;
    const nav = new Element("aside", null, { id: "workspace-navigation", classes: ["workspace-navigation", ...(/class="workspace-navigation is-collapsed/.test(html) ? ["is-collapsed"] : [])], dataset: { navigationKey: html.match(/data-navigation-key="([^"]+)"/)[1] } });
    const main = new Element("main"), header = new Element("header"), backdrop = new Element("button", null, { classes: ["navigation-backdrop"], dataset: { closeNavigation: "" } });
    const toggle = new Element("button", header, { classes: ["navigation-toggle"], dataset: { toggleNavigation: "" } });
    const brand = new Element("a", nav, { attrs: { href: "#/" } });
    const close = new Element("button", nav, { classes: ["navigation-close"], dataset: { closeNavigation: "" } });
    const active = new Element("a", nav, { attrs: { href: location.hash, ...(location.hash !== "#/account" ? { "aria-current": "page" } : {}) } });
    const themeMenu = new Element("details", nav, { dataset: { navigationMenu: "theme" } }); themeMenu.open = /data-navigation-menu="theme" open/.test(html);
    const themeSummary = new Element("summary", themeMenu); themeMenu.summary = themeSummary;
    const select = new Element("select", themeMenu, { dataset: { themeSelect: "" } }); select.value = theme.preference;
    const themeLabel = new Element("small", themeSummary);
    const accountMenu = new Element("details", nav, { dataset: { navigationMenu: "account" } }); accountMenu.open = /data-navigation-menu="account" open/.test(html);
    const accountSummary = new Element("summary", accountMenu); accountMenu.summary = accountSummary;
    const accountLink = new Element("a", accountMenu, { attrs: { href: "#/account", ...(location.hash === "#/account" ? { "aria-current": "page" } : {}) } });
    const logout = new Element("button", accountMenu, { id: "session-button" });
    const collapse = new Element("button", nav, { classes: ["navigation-collapse"], dataset: { toggleNavigation: "" } });
    ui.current = { nav, main, header, backdrop, toggle, brand, close, active, themeMenu, themeSummary, select, themeLabel, accountMenu, accountSummary, accountLink, logout, collapse };
    flush(); return ui.current;
  }
  const render = () => mount(context.topbar());
  function dispatch(type, target, extra = {}) {
    const event = { target, prevented: false, stopped: false, preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }, ...extra };
    for (const handler of events.get(type) || []) handler(event); flush(); return event;
  }
  context.installWorkspaceNavigation(); context.installWorkspaceNavigation();
  assert.equal(events.get("click").length, 1, "Installation is idempotent");
  return { context, state, location, viewport, ui, theme, storage, document, window, render, dispatch, flush };
}

{
  const h = fixture(); h.render();
  const header = h.ui.html.match(/<header[\s\S]*?<\/header>/)[0];
  assert(header.includes("训练与转换")); assert(!/session-button|标注服务正常|测试管理员|园区视觉/.test(header), "Mobile header contains only the current page name and navigation access");
  assert(h.ui.html.includes("园区视觉 &lt;标注>")); assert(h.ui.html.includes("目标检测")); assert(h.ui.html.includes("42 张图片"));
  assert.equal((h.ui.html.match(/id="session-button"/g) || []).length, 1);
  assert.equal((h.ui.html.match(/href="#\/account"/g) || []).length, 1);
  assert.equal((h.ui.html.match(/data-theme-select/g) || []).length, 1);
  assert(h.ui.html.indexOf("navigation-footer") < h.ui.html.indexOf("标注服务正常"));
  for (const value of ["light", "dark", "system"]) assert(h.ui.html.includes(`option value="${value}"`));
  h.state.session = { display_name: "标注员", role: "annotator" }; h.state.authMode = "disabled"; h.render();
  assert(!h.ui.html.includes('href="#/models"')); assert(!h.ui.html.includes('href="#/users"')); assert(!h.ui.html.includes('id="session-button"'));
  assert(h.ui.html.includes("本机开发模式"));
}

// Collapsing remains available without a desktop header and persists per account/workspace.
{
  const h = fixture(), nav = h.render();
  h.dispatch("click", nav.collapse);
  assert(nav.nav.classList.contains("is-collapsed")); assert.equal(nav.collapse.getAttribute("aria-expanded"), "false");
  assert.equal(h.storage.get(nav.nav.dataset.navigationKey), "true");
  h.dispatch("click", nav.collapse); assert(!nav.nav.classList.contains("is-collapsed"));
  h.location.hash = "#/projects/7/annotate"; assert(h.render().nav.classList.contains("is-collapsed"), "Canvas starts compact without changing the regular workspace preference");
  h.location.hash = "#/projects/7/train"; assert(!h.render().nav.classList.contains("is-collapsed"));
}

// Theme preference updates immediately; polling rerenders retain the open menu and exact focused control.
for (const target of ["select", "logout", "accountLink", "themeSummary"]) {
  const h = fixture(), initial = h.render();
  initial[target === "select" || target === "themeSummary" ? "themeMenu" : "accountMenu"].open = true;
  initial[target].focus();
  if (target === "select") {
    initial.select.value = "dark"; h.dispatch("change", initial.select);
    assert.deepEqual(h.theme.calls, ["dark"]); assert.equal(initial.themeLabel.textContent, "深色");
  }
  const updated = h.render();
  assert(updated[target === "select" || target === "themeSummary" ? "themeMenu" : "accountMenu"].open);
  assert.equal(h.document.activeElement, updated[target], `Polling retains focused ${target}`);
}
{
  const h = fixture(), nav = h.render();
  h.theme.preference = "light"; h.window.dispatchEvent(new Event("cvsprout-theme-change"));
  assert.equal(nav.select.value, "light"); assert.equal(nav.themeLabel.textContent, "浅色");
  assert.equal(h.document.documentElement.textContent, "APPLICATION CONTENT", "Changing theme must not overwrite the root element's data-theme-preference marker or clear the application");
  assert.equal(h.document.documentElement.dataset.themePreference, "system");
  nav.select.value = "invalid"; h.dispatch("change", nav.select); assert.deepEqual(h.theme.calls, []);
  nav.themeMenu.open = true; h.dispatch("click", nav.main); assert.equal(nav.themeMenu.open, false);
  nav.accountMenu.open = true; h.dispatch("keydown", nav.logout, { key: "Escape" });
  assert.equal(nav.accountMenu.open, false); assert.equal(h.document.activeElement, nav.accountSummary);
}

// Mobile drawer retains background inertness, focus boundaries including native selects, Escape, and focus return.
{
  const h = fixture({ mobile: true, hash: "#/account" }); h.state.currentSection = "account";
  const nav = h.render();
  assert.equal(nav.accountMenu.open, false); assert.equal(nav.accountLink.getClientRects().length, 1);
  assert.equal(h.context.navigationElementVisible(nav.accountLink), false, "Closed details content is invisible even when it has a layout rectangle");
  assert.equal(nav.nav.querySelector('[aria-current="page"]'), nav.accountLink, "DOM queries also return links hidden inside closed details");
  h.dispatch("click", nav.toggle);
  assert.equal(h.document.activeElement, nav.accountSummary, "Opening the account-page drawer focuses its visible menu summary instead of a hidden active link");
  assert.equal(nav.nav.inert, false); assert.equal(nav.header.inert, true);
  assert.equal(h.context.navigationElementVisible(nav.accountSummary), true, "The closed details' direct summary remains visible");
  assert.equal(h.context.navigationElementVisible(nav.select), false, "The native theme select in another closed menu is also excluded");
  nav.accountMenu.open = true;
  assert.equal(h.context.navigationElementVisible(nav.accountLink), true, "Opening details makes its content keyboard reachable");
}
{
  const h = fixture({ mobile: true }), nav = h.render();
  assert.equal(nav.nav.inert, true);
  h.dispatch("click", nav.toggle);
  assert.equal(h.context.navigationIsOpen(), true); assert.equal(nav.nav.inert, false);
  assert.equal(nav.main.inert, true); assert.equal(nav.header.inert, true); assert.equal(nav.backdrop.hidden, false);
  assert.equal(nav.nav.getAttribute("role"), "dialog"); assert.equal(h.document.activeElement, nav.active);
  nav.accountSummary.focus(); const forward = h.dispatch("keydown", nav.accountSummary, { key: "Tab", shiftKey: false });
  assert(forward.prevented); assert.equal(h.document.activeElement, nav.brand);
  const backward = h.dispatch("keydown", nav.brand, { key: "Tab", shiftKey: true });
  assert(backward.prevented); assert.equal(h.document.activeElement, nav.accountSummary);
  nav.themeMenu.open = true; nav.select.focus();
  h.dispatch("keydown", nav.select, { key: "Escape" });
  assert.equal(nav.themeMenu.open, false); assert.equal(h.context.navigationIsOpen(), true); assert.equal(h.document.activeElement, nav.themeSummary);
  h.dispatch("keydown", nav.themeSummary, { key: "Escape" });
  assert.equal(h.context.navigationIsOpen(), false); assert.equal(nav.nav.inert, true);
  assert.equal(nav.main.inert, false); assert.equal(nav.header.inert, false); assert.equal(h.document.activeElement, nav.toggle);
  h.dispatch("click", nav.toggle); nav.accountMenu.open = true; h.dispatch("click", nav.accountLink);
  assert.equal(h.context.navigationIsOpen(), false); assert.equal(nav.accountMenu.open, false);
  h.dispatch("click", nav.toggle); h.viewport.mobile = false; h.ui.mediaListener();
  assert.equal(nav.nav.inert, false); assert.equal(nav.main.inert, false); assert.equal(nav.nav.getAttribute("role"), null);
}

// Resize only moves focus when a navigation control becomes hidden/inert; editing elsewhere keeps its focus.
{
  const h = fixture(), nav = h.render();
  nav.themeMenu.open = true; nav.select.focus();
  h.viewport.mobile = true; h.ui.mediaListener();
  assert.equal(nav.nav.inert, true); assert.equal(nav.themeMenu.open, false);
  assert.equal(h.document.activeElement, nav.toggle, "Hidden desktop theme select returns focus to the mobile navigation button");
  h.viewport.mobile = false; h.ui.mediaListener();
  assert.equal(h.document.activeElement, nav.collapse, "Hidden mobile toggle returns focus to the desktop collapse button");
  nav.main.focus(); h.viewport.mobile = true; h.ui.mediaListener();
  assert.equal(h.document.activeElement, nav.main, "Resizing does not steal focus from content editing");
  h.dispatch("click", nav.toggle); nav.accountMenu.open = true; nav.logout.focus();
  h.viewport.mobile = false; h.ui.mediaListener();
  assert.equal(h.document.activeElement, nav.accountSummary, "A closed account menu restores its visible summary");
}

// Chromium may blur the CSS-hidden control to body before dispatching the media-query event.
for (const mobile of [false, true]) {
  const h = fixture({ mobile }), nav = h.render(), original = mobile ? nav.toggle : nav.select;
  if (!mobile) nav.themeMenu.open = true;
  original.focus(); h.viewport.mobile = !mobile;
  assert.equal(h.context.navigationElementVisible(original), false);
  h.document.activeElement = h.document.body;
  h.dispatch("focusout", original, { relatedTarget: null });
  h.ui.mediaListener();
  assert.equal(h.document.activeElement, mobile ? nav.collapse : nav.toggle, "A breakpoint-induced blur is restored even when the media handler initially sees body");
}
for (const cause of ["menu-close", "window-blur", "content-click"]) {
  const h = fixture(), nav = h.render(); nav.themeMenu.open = true; nav.select.focus();
  if (cause === "menu-close") nav.themeMenu.open = false;
  if (cause === "content-click") h.viewport.mobile = true;
  h.document.activeElement = cause === "content-click" ? nav.main : h.document.body;
  h.dispatch("focusout", nav.select, { relatedTarget: cause === "content-click" ? nav.main : null });
  h.viewport.mobile = true; h.ui.mediaListener();
  assert.equal(h.document.activeElement, cause === "content-click" ? nav.main : h.document.body, `${cause} does not create a stale resize focus candidate`);
}

console.log("PASS workspace navigation: single mobile context, sidebar account/status, role-aware management/logout, per-account collapse, native theme control and external sync, polling focus/menu retention, mobile inert/focus/Tab/Escape and resize behavior");
