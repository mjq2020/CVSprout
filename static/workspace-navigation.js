import { enterWorkspaceContent } from "./workspace-feedback.js?v=0.23.0";

const workflow = [
  ["overview", "项目概览", "chart"], ["data", "导入与数据", "database"], ["ai", "AI 标注", "magic"],
  ["blocks", "标注任务块", "users"], ["annotate", "标注与审核", "polygon"], ["versions", "数据版本", "layers"], ["train", "训练与转换", "cpu"],
];
const mobile = () => window.matchMedia("(max-width: 900px)").matches;
let drawerOpen = false, initialized = false, returnFocus = null;
const identity = session => session?.user_id || session?.username || session?.actor || "local";
const read = (key, fallback) => { try { const value = sessionStorage.getItem(key); return value == null ? fallback : JSON.parse(value); } catch (_) { return fallback; } };
const save = (key, value) => { try { sessionStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* Optional browser preference. */ } };

export function currentProjectRoute() {
  const match = location.hash.match(/^#\/projects\/(\d+)(?:\/(overview|data|ai|blocks|annotate|versions|train))?/);
  return match ? { id: Number(match[1]), section: match[2] || "overview" } : null;
}

const themeLabels = { light: "浅色", dark: "深色", system: "跟随系统" };
const themePreference = () => window.CVSproutTheme?.getPreference?.() || "system";

export function workspaceNavigationMarkup({ session, project, admin, active, authMode = "disabled", workerOnline = false, taskLabel = "", icon, escapeHtml: esc }) {
  const route = currentProjectRoute(), canvas = route?.section === "annotate";
  const key = `visionforge-navigation:${identity(session)}:${canvas ? "canvas" : "main"}`;
  const collapsed = Boolean(read(key, canvas));
  const isMobile = mobile(), open = isMobile && drawerOpen;
  const projectName = route && project?.id === route.id ? project.name : "正在读取项目…";
  const displayName = session?.display_name || (authMode === "disabled" ? "开发管理员" : "当前账号");
  const role = { admin: "管理员", annotator: "标注员", reviewer: "审核员" }[session?.role] || (authMode === "disabled" ? "开发模式" : "");
  const status = workerOnline ? "标注服务正常" : "标注服务暂未就绪", preference = themePreference();
  const link = (href, label, iconName, selected, extra = "") => `<a href="${href}" class="navigation-link ${selected ? "active" : ""}" ${selected ? 'aria-current="page"' : ""} aria-label="${esc(label)}" title="${esc(label)}" ${extra}>${icon(iconName)}<span>${esc(label)}</span></a>`;
  const focused = document.activeElement;
  const focusedLink = focused?.closest(".workspace-navigation a")?.getAttribute("href");
  const focusedToggle = focused?.matches("[data-toggle-navigation]") ? focused.classList.contains("navigation-collapse") ? ".navigation-collapse" : ".navigation-toggle" : null;
  const focusedFooter = focused?.closest("[data-navigation-menu]")?.dataset.navigationMenu;
  const focusedControl = focused?.matches("[data-theme-select]") ? "[data-theme-select]" : focused?.matches("#session-button") ? "#session-button" : null;
  const expandedMenus = new Set([...document.querySelectorAll(".workspace-navigation [data-navigation-menu][open]")].map(menu => menu.dataset.navigationMenu));
  queueMicrotask(() => {
    syncNavigation();
    if (!focused?.isConnected) {
      const next = focusedControl ? document.querySelector(focusedControl) : focusedLink ? [...document.querySelectorAll(".workspace-navigation a")].find(link => link.getAttribute("href") === focusedLink) : focusedToggle ? document.querySelector(focusedToggle) : focusedFooter ? document.querySelector(`[data-navigation-menu="${focusedFooter}"] > summary`) : null;
      next?.focus({ preventScroll: true });
    }
  });
  return `<aside id="workspace-navigation" class="workspace-navigation ${collapsed ? "is-collapsed" : ""} ${open ? "is-open" : ""}" data-navigation-key="${esc(key)}" aria-label="应用导航" ${isMobile ? open ? 'role="dialog" aria-modal="true"' : "inert" : ""}>
    <a class="navigation-brand" href="#/" aria-label="CVSprout 项目工作台"><span class="brand-mark">${icon("logo")}</span><span>CVSprout<small>数据工作台</small></span></a><button class="navigation-close tool" data-close-navigation aria-label="关闭导航">${icon("close")}</button>
    <div class="navigation-scroll"><nav class="application-links" aria-label="工作台">${link("#/", "所有项目", "folder", !route && active === "projects")}</nav>
      ${route ? `<div class="navigation-project"><strong title="${esc(projectName)}">${esc(projectName)}</strong>${project ? `<small>${esc(taskLabel)}<span>${project.asset_count || 0} 张图片</span></small>` : ""}</div><nav class="workflow-links" aria-label="项目工作流">${workflow.map(([id, label, name]) => link(`#/projects/${route.id}/${id}`, label, name, route.section === id, `data-workflow-link="${id}"`)).join("")}</nav>` : ""}
      ${admin ? `<nav class="management-links" aria-label="管理">${link("#/models", "标注模型服务", "cpu", active === "models")}${link("#/users", "用户管理", "users", active === "users")}</nav>` : ""}
    </div>
    <div class="navigation-footer">
      <div class="navigation-service-status" title="${status}" aria-label="${status}"><span class="status-dot ${workerOnline ? "" : "warning"}" aria-hidden="true"></span><span>${status}</span></div>
      <details class="navigation-menu navigation-theme" data-navigation-menu="theme" ${expandedMenus.has("theme") ? "open" : ""}><summary class="navigation-footer-trigger" title="主题设置：${themeLabels[preference]}" aria-label="主题设置">${icon("sliders")}<span>主题</span><small data-navigation-theme-label>${themeLabels[preference]}</small></summary><div class="navigation-popover"><label for="navigation-theme-select">外观主题</label><select id="navigation-theme-select" data-theme-select>${Object.entries(themeLabels).map(([value, label]) => `<option value="${value}" ${preference === value ? "selected" : ""}>${label}</option>`).join("")}</select></div></details>
      <details class="navigation-menu navigation-account" data-navigation-menu="account" ${expandedMenus.has("account") ? "open" : ""}><summary class="navigation-footer-trigger" title="${esc(displayName)} · ${role}" aria-label="账号菜单：${esc(displayName)}"><span class="navigation-avatar">${esc(displayName.slice(0, 1))}</span><span class="navigation-identity"><strong>${esc(displayName)}</strong><small>${role}</small></span>${icon("chevronRight")}</summary><div class="navigation-popover"><div class="navigation-account-heading"><strong>${esc(displayName)}</strong><small>${role}</small></div>${session?.user_id ? `<a href="#/account" class="navigation-account-link" ${active === "account" || location.hash === "#/account" ? 'aria-current="page"' : ""}>${icon("shield")}账号与安全</a>` : ""}${authMode !== "disabled" ? `<button id="session-button">退出登录</button>` : `<span class="navigation-local-mode">本机开发模式</span>`}</div></details>
    </div>
    <button class="navigation-collapse" data-toggle-navigation aria-controls="workspace-navigation" aria-expanded="${!collapsed}" title="${collapsed ? "展开导航" : "收起导航"}">${icon("arrowLeft")}<span>收起导航</span></button>
  </aside><button class="navigation-backdrop" data-close-navigation aria-label="关闭导航" tabindex="-1" ${open ? "" : "hidden"}></button>`;
}

export function navigationToggleMarkup(icon) {
  return `<button class="btn ghost icon-btn navigation-toggle" data-toggle-navigation aria-label="展开或收起导航" aria-controls="workspace-navigation" aria-expanded="false">${icon("grid")}</button>`;
}

function syncNavigation() {
  const nav = document.querySelector("#workspace-navigation");
  if (!nav) return;
  const isMobile = mobile(), open = isMobile && drawerOpen;
  nav.classList.toggle("is-open", open); nav.inert = isMobile && !open;
  if (open) { nav.setAttribute("role", "dialog"); nav.setAttribute("aria-modal", "true"); }
  else { nav.removeAttribute("role"); nav.removeAttribute("aria-modal"); }
  const backdrop = document.querySelector(".navigation-backdrop"); if (backdrop) backdrop.hidden = !open;
  document.querySelectorAll(".app-shell > main, .app-shell > .topbar").forEach(node => { node.inert = open || node.dataset.workspaceLoading === "true"; });
  document.querySelectorAll("[data-toggle-navigation]").forEach(button => { const expanded = isMobile ? open : !nav.classList.contains("is-collapsed"); button.setAttribute("aria-expanded", String(expanded)); button.title = expanded ? "收起导航" : "展开导航"; button.setAttribute("aria-label", button.title); });
}

export function closeWorkspaceNavigation({ focus = false } = {}) {
  const wasOpen = drawerOpen;
  drawerOpen = false; syncNavigation();
  document.querySelectorAll("[data-navigation-menu][open]").forEach(menu => { menu.open = false; });
  if (focus) (returnFocus?.isConnected ? returnFocus : document.querySelector(".navigation-toggle"))?.focus({ preventScroll: true });
  returnFocus = null;
  if (wasOpen) queueMicrotask(() => window.dispatchEvent(new Event("workspace-navigation-closed")));
}

export function navigationIsOpen() { return mobile() && drawerOpen; }

function navigationElementVisible(node) {
  if (!node?.isConnected || node.closest("[inert]") || !node.getClientRects().length || getComputedStyle(node).visibility === "hidden") return false;
  // Closed details may retain descendant layout boxes in Chromium. Only their
  // direct summary (and its contents) remains visible and keyboard reachable.
  for (let ancestor = node.parentElement; ancestor; ancestor = ancestor.parentElement) {
    if (ancestor.matches("details:not([open])") && !ancestor.querySelector(":scope > summary")?.contains(node)) return false;
  }
  return true;
}

export function installWorkspaceNavigation() {
  if (initialized) return; initialized = true;
  let layoutMobile = mobile(), resizeFocus = null;
  document.addEventListener("focusout", event => {
    // CSS can hide the focused control and move focus to body before the media
    // query event runs. Keep only that pending breakpoint transition's target.
    if (mobile() === layoutMobile || event.relatedTarget || !event.target.isConnected || navigationElementVisible(event.target)) return;
    if (event.target.closest(".workspace-navigation") || event.target.matches(".navigation-toggle")) resizeFocus = event.target;
  }, true);
  document.addEventListener("click", event => {
    const nav = document.querySelector("#workspace-navigation"); if (!nav) return;
    nav.querySelectorAll("[data-navigation-menu][open]").forEach(menu => { if (!menu.contains(event.target)) menu.open = false; });
    if (event.target.closest("[data-close-navigation]")) { closeWorkspaceNavigation({ focus: true }); return; }
    const toggle = event.target.closest("[data-toggle-navigation]");
    if (toggle) {
      if (mobile()) {
        if (drawerOpen) { closeWorkspaceNavigation({ focus: true }); return; }
        drawerOpen = true; returnFocus = toggle; syncNavigation();
        const currentLinks = [...nav.querySelectorAll('[aria-current="page"]')];
        const visibleCurrent = currentLinks.find(navigationElementVisible);
        (visibleCurrent || currentLinks[0]?.closest("[data-navigation-menu]")?.querySelector("summary") || nav.querySelector("a"))?.focus({ preventScroll: true });
      } else {
        const collapsed = nav.classList.toggle("is-collapsed"); save(nav.dataset.navigationKey, collapsed); syncNavigation();
      }
      return;
    }
    if (nav.contains(event.target) && event.target.closest("a")) closeWorkspaceNavigation();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      const menu = document.querySelector("[data-navigation-menu][open]");
      if (menu) { event.preventDefault(); event.stopPropagation(); menu.open = false; menu.querySelector("summary")?.focus(); return; }
    }
    if (!navigationIsOpen()) return;
    const nav = document.querySelector("#workspace-navigation"); if (!nav) return;
    event.stopPropagation();
    if (event.key === "Escape") { event.preventDefault(); closeWorkspaceNavigation({ focus: true }); return; }
    if (event.key !== "Tab") return;
    const focusable = [...nav.querySelectorAll('a[href],button:not([disabled]),summary,select:not([disabled])')].filter(navigationElementVisible);
    const first = focusable[0], last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }, true);
  document.addEventListener("change", event => {
    if (!event.target.matches("[data-theme-select]") || !Object.hasOwn(themeLabels, event.target.value)) return;
    window.CVSproutTheme?.setPreference(event.target.value);
    syncThemePreference();
  });
  window.addEventListener("cvsprout-theme-change", syncThemePreference);
  window.addEventListener("hashchange", () => closeWorkspaceNavigation());
  window.matchMedia("(max-width: 900px)").addEventListener("change", () => {
    const focused = document.activeElement === document.body && resizeFocus?.isConnected ? resizeFocus : document.activeElement;
    resizeFocus = null; layoutMobile = mobile();
    const nav = document.querySelector("#workspace-navigation");
    const navigationFocused = nav?.contains(focused), toggleFocused = focused?.matches(".navigation-toggle");
    closeWorkspaceNavigation(); syncNavigation();
    if (!navigationFocused && !toggleFocused) return;
    if (navigationElementVisible(focused)) return;
    const target = mobile() ? document.querySelector(".navigation-toggle") : focused.closest("[data-navigation-menu]")?.querySelector("summary") || document.querySelector(".navigation-collapse");
    if (navigationElementVisible(target)) target.focus({ preventScroll: true });
  });
}

function syncThemePreference() {
  const preference = themePreference();
  document.querySelectorAll("[data-theme-select]").forEach(select => { select.value = preference; });
  document.querySelectorAll("[data-navigation-theme-label]").forEach(label => { label.textContent = themeLabels[preference]; });
  const trigger = document.querySelector('[data-navigation-menu="theme"] > summary');
  if (trigger) trigger.title = `主题设置：${themeLabels[preference]}`;
}

const sectionTabs = new Map();
function sectionKey(session, projectId, scope) { return `visionforge-section-tab:${identity(session)}:${projectId}:${scope}`; }
export function getSectionTab(session, projectId, scope, fallback) {
  const key = sectionKey(session, projectId, scope);
  const route = currentProjectRoute();
  const requested = scope === "ai" && route?.id === projectId && route.section === "ai" ? new URLSearchParams(location.hash.split("?")[1] || "").get("tab") : null;
  if (["configure", "history"].includes(requested)) { sectionTabs.set(key, requested); save(key, requested); }
  if (!sectionTabs.has(key)) {
    const saved = read(key, fallback), allowed = scope === "train" ? ["overview", "details", "conversions"] : ["configure", "history"];
    sectionTabs.set(key, allowed.includes(saved) ? saved : fallback);
  }
  return sectionTabs.get(key);
}
export function setSectionTab(session, projectId, scope, tab) {
  const key = sectionKey(session, projectId, scope); sectionTabs.set(key, tab); save(key, tab);
  const route = currentProjectRoute();
  if (scope === "ai" && route?.id === projectId && route.section === "ai") {
    const [path, query = ""] = location.hash.split("?"), params = new URLSearchParams(query);
    params.set("tab", tab); window.history.replaceState(null, "", `${path}?${params}`);
  }
}
export function sectionTabsMarkup(scope, tabs, selected, esc) {
  return `<div class="section-tabs" role="tablist" aria-label="${scope === "train" ? "所选训练详情" : "AI 标注内容"}">${tabs.map(([id, label]) => `<button type="button" role="tab" id="section-tab-${scope}-${id}" data-section-tab="${scope}:${id}" aria-controls="section-panel-${scope}-${id}" aria-selected="${selected === id}" tabindex="${selected === id ? 0 : -1}">${esc(label)}</button>`).join("")}</div>`;
}
export function sectionPanelAttributes(scope, id, selected) { return `id="section-panel-${scope}-${id}" role="tabpanel" aria-labelledby="section-tab-${scope}-${id}" data-section-panel="${scope}:${id}" ${selected === id ? "" : "hidden"}`; }
export function bindSectionTabs(root, scope, { selected, onChange }) {
  const tabs = [...root.querySelectorAll(`[data-section-tab^="${scope}:"]`)];
  const select = (id, focus = false) => {
    const changed = id !== selected; selected = id;
    tabs.forEach(tab => { const active = tab.dataset.sectionTab === `${scope}:${id}`; tab.setAttribute("aria-selected", String(active)); tab.tabIndex = active ? 0 : -1; if (active && focus) tab.focus({ preventScroll: true }); });
    root.querySelectorAll(`[data-section-panel^="${scope}:"]`).forEach(panel => { panel.hidden = panel.dataset.sectionPanel !== `${scope}:${id}`; if (!panel.hidden && changed) enterWorkspaceContent(panel, "workspace-panel-enter"); });
    onChange?.(id);
  };
  tabs.forEach((tab, index) => {
    tab.onclick = () => select(tab.dataset.sectionTab.split(":")[1]);
    tab.onkeydown = event => {
      const offset = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
      if (!offset && !["Home", "End"].includes(event.key)) return;
      event.preventDefault(); event.stopPropagation();
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + offset + tabs.length) % tabs.length;
      select(tabs[next].dataset.sectionTab.split(":")[1], true);
    };
  });
  // The markup already reflects the persisted choice; avoid triggering data requests here.
  if (!tabs.some(tab => tab.dataset.sectionTab === `${scope}:${selected}`) && tabs[0]) select(tabs[0].dataset.sectionTab.split(":")[1]);
}
