import { TASK_LABELS, projectTask, isAdmin, canReview, taskAllowsTool, modelFitsProject, taskFieldsMarkup, bindTaskFields, readTaskFields, annotationToolsMarkup, taskInspectorMarkup, keypointInspectorMarkup, keypointShapesMarkup, chooseTaskLabel, poseCanvasClick, finishKeypoints, undoKeypointDraft, hasTaskSaveDraft, retryTaskSave, discardTaskSaveDraft, bindTaskInspector, exportFormatMarkup } from "./task-ui.js?v=0.21.0";
import { renderUsersPage } from "./accounts-ui.js?v=0.21.0";
import { mountSetup, mountModelDownloads } from "./setup-ui.js?v=0.21.0";
import { mountAuthScreen } from "./auth-ui.js?v=0.21.0";
import { renderAccountPage } from "./security-ui.js?v=0.21.0";
import { bindAssetManagement } from "./asset-management-ui.js?v=0.21.0";
import { batchReviewSelectionMarkup, reviewQueueMarkup, bindReviewPage, reviewContinuationMarkup, shouldContinueReview, openWorkBlockSubmit } from "./review-ui.js?v=0.21.0";
import { BLOCK_STATUS, annotationBlockURL, scopeWorkBlockRequest, workBlocksMarkup, bindWorkBlocks } from "./work-blocks-ui.js?v=0.21.0";
import { versionSamplesMarkup, openVersionImages } from "./dataset-ui.js?v=0.21.0";
import { openDatasetGeneration, versionAugmentationMarkup } from "./dataset-generation.js?v=0.21.0";
import { createHistoryNavigation, resetHistoryNavigation } from "./history-navigation.js?v=0.21.0";
import { mountOverviewTraining } from "./overview-training.js?v=0.21.0";
import { aiPreviewMarkup, mountAiPreview } from "./ai-preview-ui.js?v=0.21.0";
import { installAnnotationShortcuts, annotationShortcutsHelp, annotationShortcutHint } from "./annotation-shortcuts.js?v=0.21.0";
import { mountCanvasGuides } from "./canvas-guides.js?v=0.21.0";
import { createAnnotationContextMenu, findAnnotationsAtPoint } from "./annotation-context-menu.js?v=0.21.0";
import { currentProjectRoute, workspaceNavigationMarkup, navigationToggleMarkup, installWorkspaceNavigation, closeWorkspaceNavigation, navigationIsOpen, getSectionTab, setSectionTab, sectionTabsMarkup, sectionPanelAttributes, bindSectionTabs } from "./workspace-navigation.js?v=0.21.0";

const app = document.querySelector("#app");
const modalRoot = document.querySelector("#modal-root");
const toastRoot = document.querySelector("#toast-root");
let modalReturnFocus = null;
let authCleanup = null;
let modalCleanup = null;
let dashboardRequest = 0;
let modelsRequest = 0;
let canvasObserver = null;
let navigationRenderPending = null;
let canvasView = null;
let canvasGuides = null;
let annotationMenu = null;
let annotationMenuContext = null;

const state = {
  project: null,
  projects: [],
  projectQuery: "",
  projectTaskFilter: "all",
  projectsArchived: false,
  projectSort: "updated",
  queueCollapsed: window.innerWidth < 1200,
  annotationFocus: false,
  annotationPanelRestore: null,
  inspectorCollapsed: window.innerWidth < 760,
  assets: [],
  workBlocks: { enabled: false, blocks: [], unassigned_count: 0 },
  workBlockFilter: "mine",
  workBlocksRequest: 0,
  activeWorkBlockId: null,
  activeWorkBlock: null,
  legacyReviewAssetId: null,
  annotations: [],
  models: [],
  jobs: [],
  auditLogs: [],
  asset: null,
  activeLabel: null,
  selectedAnnotation: null,
  selectionIntent: false,
  lastDrawingTool: null,
  tool: "select",
  assetFilter: "all",
  assetRenderLimit: 200,
  assetListScroll: 0,
  inspectorTab: "objects",
  zoom: 1,
  drawing: null,
  polygon: [],
  poseDraft: null,
  keypointPlacement: null,
  shapeEdit: null,
  savingAnnotation: false,
  pendingAnnotation: null,
  pendingTaskEdit: null,
  pendingObjectEdit: null,
  advancedAnnotationDirty: false,
  advancedDraft: null,
  assistPoints: [],
  pollTimer: null,
  lockTimer: null,
  collaborationTimer: null,
  lockOwned: false,
  readOnly: false,
  readOnlyReason: "",
  saveFeedback: { status: "idle", message: "" },
  workflowBusy: false,
  handedOffAssetId: null,
  jobActionIds: new Set(),
  authReady: false,
  authMode: null,
  session: null,
  health: null,
  currentSection: "projects",
  analytics: null,
  versions: [],
  training: { availability: { available: false, reason: "正在检测训练环境" }, runs: [] },
  trainingCatalog: { models: [], tasks: {}, features: {} },
  trainingResources: { default_device: "auto", devices: [] },
  trainingFilters: { status: "all", task: "all", query: "" },
  trainingPage: { page: 1, loading: false, error: "", request: 0 },
  selectedTrainingId: null,
  selectedTrainingRun: null,
  trainingSelectionLoading: false,
  trainingSelectionError: "",
  trainingSelectionRequest: 0,
  pipelineUpdatedAt: 0,
  pipelineConnectionLost: false,
  pipelineManualRefreshing: false,
  trainingEnvironmentLoading: false,
  trainingListOpen: window.innerWidth >= 900,
  expandedTrainingRuns: new Set(),
  trainingLogs: {},
  trainingMetricsHistory: {},
  conversions: [],
  conversionCounts: {},
  projectConversionsAvailable: false,
  dataSearch: "",
  dataStatus: "all",
  selectedAssetIds: new Set(),
  reviewEvents: [],
  selectionEpoch: 0,
  copiedAnnotation: null,
  showAnnotations: true,
  spacePressed: false,
  pan: null,
};

function createBrowserActorId() {
  if (globalThis.crypto?.randomUUID) return `operator-${globalThis.crypto.randomUUID().slice(0, 8)}`;
  if (globalThis.crypto?.getRandomValues) {
    const bytes = new Uint8Array(8);
    globalThis.crypto.getRandomValues(bytes);
    return `operator-${Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("")}`;
  }
  // Legacy HTTP browsers may expose neither Web Crypto API. This identifier is only
  // used for development-mode edit leases and audit display, never for authentication.
  return `operator-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

const actorId = sessionStorage.getItem("visionforge-actor-id") || createBrowserActorId();
sessionStorage.setItem("visionforge-actor-id", actorId);
// Created for this page, never persisted: duplicated tabs must not share a lease.
const editorToken = globalThis.crypto?.randomUUID?.() || createBrowserActorId();

// Keep each multipart request small enough to recover and retry independently.
// A few bounded parallel requests also let the server validate images off the
// event loop while job-status polling stays responsive.
const UPLOAD_BATCH_FILE_LIMIT = 24;
const UPLOAD_BATCH_BYTE_LIMIT = 32 * 1024 * 1024;
const UPLOAD_CONCURRENCY = 3;

const icons = {
  logo: '<path d="M5 7.5 12 3l7 4.5v9L12 21l-7-4.5z"/><path d="m8.5 10 3.5-2 3.5 2v4L12 16l-3.5-2z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
  folder: '<path d="M3 7h6l2 2h10v10H3z"/><path d="M3 7V5h7l2 2"/>',
  users: '<circle cx="9" cy="7" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 4a3 3 0 0 1 0 6M18 14a5 5 0 0 1 3 4v3"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z"/><path d="m8 12 3 3 5-6"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 15-5-5L5 20"/>',
  upload: '<path d="M12 16V4m0 0L7 9m5-5 5 5"/><path d="M5 15v5h14v-5"/>',
  box: '<rect x="4" y="4" width="16" height="16" rx="1"/><path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/>',
  polygon: '<path d="m12 3 8 6-3 11H7L4 9z"/><circle cx="12" cy="3" r="1"/><circle cx="20" cy="9" r="1"/><circle cx="17" cy="20" r="1"/><circle cx="7" cy="20" r="1"/><circle cx="4" cy="9" r="1"/>',
  cursor: '<path d="m5 3 14 9-7 1-3 7z"/>',
  expand: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m8 0h5v-5"/>',
  hand: '<path d="M8 13V5a2 2 0 0 1 4 0v7-9a2 2 0 0 1 4 0v9-7a2 2 0 0 1 4 0v10c0 4-3 7-7 7h-1c-3 0-5-2-7-5l-2-4a2 2 0 0 1 3-2l2 2Z"/>',
  magic: '<path d="m15 4 5 5L8 21l-5-5zM13 6l5 5M6 4v3M4.5 5.5h3M19 16v4M17 18h4"/>',
  download: '<path d="M12 3v13m0 0 5-5m-5 5-5-5M4 21h16"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3m3 0-1 14H7L6 7M10 11v6m4-6v6"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  arrowLeft: '<path d="m15 18-6-6 6-6"/>',
  arrowRight: '<path d="m9 18 6-6-6-6"/>',
  grid: '<rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><rect x="14" y="14" width="6" height="6" rx="1"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/>',
  target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>',
  keyboard: '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M5 9h1M9 9h1M13 9h1M17 9h1M5 12h1M9 12h1M13 12h1M17 12h1M7 16h10"/>',
  chart: '<path d="M4 20V10m5 10V4m6 16v-7m5 7V7"/>',
  database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
  play: '<path d="m8 5 11 7-11 7z"/>',
  cpu: '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M9 2v3m6-3v3M9 19v3m6-3v3M2 9h3m-3 6h3m14-6h3m-3 6h3"/>',
  chevronRight: '<path d="m9 18 6-6-6-6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  warning: '<path d="M12 3 2.5 20h19z"/><path d="M12 9v4m0 3h.01"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 9A7 7 0 0 1 18 6l2 6M18 15a7 7 0 0 1-11.9 3L4 12"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 13v6H5V6h6"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/><circle cx="4" cy="18" r="1"/>',
  terminal: '<path d="m5 7 4 4-4 4M11 17h8"/><rect x="3" y="4" width="18" height="16" rx="2"/>',
  sliders: '<path d="M4 6h7M15 6h5M4 12h2M10 12h10M4 18h10M18 18h2"/><circle cx="13" cy="6" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="16" cy="18" r="2"/>',
  branch: '<circle cx="6" cy="5" r="2"/><circle cx="18" cy="7" r="2"/><circle cx="18" cy="17" r="2"/><path d="M8 5h2a4 4 0 0 1 4 4v4a4 4 0 0 0 4 4M14 10a3 3 0 0 1 3-3"/>',
  chip: '<path d="M9 3v3m6-3v3M9 18v3m6-3v3M3 9h3m-3 6h3m12-6h3m-3 6h3"/><rect x="6" y="6" width="12" height="12" rx="3"/><path d="M10 10h4v4h-4z"/>',
};

function icon(name, cls = "") {
  return `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true">${icons[name] || ""}</svg>`;
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

async function api(path, options = {}) {
  const legacyAssetList = (options.method || "GET").toUpperCase() === "GET" && state.currentSection === "annotate"
    && state.legacyReviewAssetId && /^\/api\/projects\/\d+\/assets(?:\?|$)/.test(path);
  if (legacyAssetList) path = `/api/assets/${state.legacyReviewAssetId}`;
  path = scopeWorkBlockRequest(path, state, options.method || "GET");
  const requestSession = state.session;
  const assertSessionCurrent = () => {
    if (state.session === requestSession) return;
    const error = new Error("登录状态已更新，请继续使用当前账号。");
    error.staleSession = true; error.status = 409; throw error;
  };
  const headers = new Headers(options.headers || {});
  headers.set("X-Actor-ID", actorId);
  headers.set("X-Edit-Token", editorToken);
  headers.set("X-MDP-Client", "browser");
  let response;
  try { response = await fetch(path, { ...options, headers, credentials: "same-origin" }); }
  catch (cause) {
    assertSessionCurrent();
    const error = new Error("连接中断，操作结果暂时无法确认。请检查网络，刷新确认后再重试。");
    error.technicalMessage = cause.message; throw error;
  }
  assertSessionCurrent();
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    let responseBody = null;
    try {
      responseBody = await response.json();
      message = typeof responseBody.detail === "string"
        ? responseBody.detail
        : Array.isArray(responseBody.detail)
          ? responseBody.detail.map(item => item.msg).filter(Boolean).join("；") || message
          : responseBody.detail?.message || message;
    } catch (_) { /* keep fallback */ }
    assertSessionCurrent();
    if (response.status === 401 && state.session === requestSession) {
      state.authReady = false;
      closeModal(); renderLoginScreen("登录已过期，请重新登录后继续。");
    }
    if (responseBody?.detail?.code === "password_change_required" && state.session === requestSession) {
      if (state.session) state.session.must_change_password = true;
      closeModal();
      if (location.hash !== "#/account") location.hash = "#/account";
      else if (state.currentSection !== "account") void renderAccountPage();
    }
    const technicalMessage = message;
    if (Array.isArray(responseBody?.detail)) message = "填写的信息不符合要求，请检查必填项、数值范围和格式。";
    else if (response.status === 423) message = "这张图片正由其他成员或页面编辑，请先处理下一张，稍后再回来。";
    else if (!/[\u4e00-\u9fff]/.test(message)) message = ({403:"当前账号没有这项操作的权限，请联系管理员。",404:"未找到这项数据，可能已被移除。请返回列表刷新。",413:"上传的文件太大，请减少文件数量或拆分后重试。",429:"操作过于频繁，请稍后重试。"})[response.status] || "操作未完成。请检查填写的信息；如果仍然失败，可展开技术详情并联系管理员。";
    const error = new Error(message);
    error.status = response.status;
    error.details = responseBody?.detail;
    error.technicalMessage = technicalMessage !== message ? technicalMessage : "";
    throw error;
  }
  if (response.status === 204) return null;
  const data = await response.json();
  assertSessionCurrent();
  return legacyAssetList ? [data] : data;
}

function jsonOptions(method, body) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function notify(message, type = "success") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.setAttribute("role", type === "error" ? "alert" : "status");
  node.innerHTML = `<span class="status-dot"></span><div>${escapeHtml(message)}</div><button class="toast-dismiss" aria-label="关闭提示">${icon("close")}</button>`;
  node.querySelector("button").onclick = () => node.remove();
  toastRoot.append(node);
  while (toastRoot.children.length > 4) toastRoot.firstElementChild.remove();
  if (type !== "error") setTimeout(() => node.remove(), 5500);
  return node;
}

function renderLoadError(error) {
  if (error.status === 401 || error.staleSession || error.details?.code === "password_change_required") return;
  app.innerHTML = `<div class="app-shell">${topbar()}<main class="dashboard"><div class="dashboard-inner"><div class="empty-state"><strong>页面暂时未能加载</strong><p>${escapeHtml(error.message)}</p><button class="btn primary" id="retry-page-load">重新加载</button> <a href="#/" class="btn ghost link-btn">返回项目列表</a></div></div></main></div>`;
  document.querySelector("#retry-page-load").onclick = route;
}

function topbar(active = "projects") {
  const workerOnline = state.health?.annotation_workers?.status === "online";
  const route = currentProjectRoute(), project = route && state.project?.id === route.id ? state.project : null;
  const contextName = route ? project?.name || "正在读取项目…" : ({ models: "标注模型服务", users: "用户管理", account: "账号与安全" })[state.currentSection] || "项目工作台";
  return `${workspaceNavigationMarkup({ session: state.session, project, admin: isAdmin(), active: state.currentSection === "account" ? "account" : active, icon, escapeHtml })}<header class="topbar workspace-topbar">
    ${navigationToggleMarkup(icon)}<div class="workspace-context">${route ? `<a href="#/" class="workspace-back" aria-label="返回所有项目" title="返回所有项目">${icon("arrowLeft")}</a>` : ""}<div><strong title="${escapeHtml(contextName)}">${escapeHtml(contextName)}</strong>${project ? `<small>${project.asset_count || 0} 张图片 · ${escapeHtml(TASK_LABELS[projectTask()] || "视觉标注")}</small>` : '<small>CVSprout · 数据工作台</small>'}</div></div>
    <div class="topbar-spacer"></div><span class="session-identity">${escapeHtml(state.session?.display_name || (state.authMode === "disabled" ? "开发管理员" : "管理员"))}<small>${{admin:"管理员",annotator:"标注员",reviewer:"审核员"}[state.session?.role] || ""}</small></span>
    <div class="system-status"><span class="status-dot ${workerOnline ? "" : "warning"}"></span>${workerOnline ? "标注服务正常" : "标注服务暂未就绪"}</div><details class="account-menu"><summary class="avatar" aria-label="账户菜单" title="账户菜单">${escapeHtml(state.session?.display_name?.slice(0,1) || "VF")}</summary><div class="account-popover"><strong>${escapeHtml(state.session?.display_name || "开发管理员")}</strong><span>${{admin:"管理员",annotator:"标注员",reviewer:"审核员"}[state.session?.role] || "开发模式"}</span>${state.session?.user_id ? `<a class="account-security-link" href="#/account">账号与安全</a>` : ""}${state.authMode !== "disabled" ? `<button id="session-button">退出登录</button>` : ""}</div></details>
  </header>`;
}

async function route() {
  if (state.currentSection === "annotate" && state.asset && state.authReady && !state.session?.must_change_password
      && (state.savingAnnotation || state.workflowBusy || hasAnnotationDraft())) {
    history.replaceState(null, "", annotationBlockURL(state.project.id, state.activeWorkBlockId, state.asset.id));
    closeWorkspaceNavigation();
    notify(state.savingAnnotation || state.workflowBusy ? "正在保存，请完成后再切换页面。" : "还有未保存的标注，请先完成或按 Esc 取消草稿，再切换页面。", "warning");
    return;
  }
  authCleanup?.(); authCleanup = null;
  canvasObserver?.disconnect();
  closeWorkspaceNavigation();
  annotationMenu?.close({ restoreFocus: false, force: true });
  canvasGuides?.destroy(); canvasGuides = null;
  const routeEpoch = ++state.selectionEpoch;
  closeModal();
  clearInterval(state.collaborationTimer); state.collaborationTimer=null;
  if (state.lockOwned && state.asset) await releaseCurrentLock();
  clearInterval(state.pollTimer);
  state.pollTimer = null;
  clearInterval(state.lockTimer);
  state.lockTimer = null;
  if (!(await ensureAuthenticated(routeEpoch))) return;
  if (routeEpoch !== state.selectionEpoch) return;
  if (state.session?.must_change_password) {
    if (location.hash !== "#/account") history.replaceState(null, "", "#/account");
    return renderAccountPage();
  }
  if (location.hash === "#/account") return renderAccountPage();
  const match = location.hash.match(/^#\/projects\/(\d+)(?:\/(overview|data|blocks|annotate|ai|versions|train))?/);
  if (match) {
    const section = match[2] || "overview";
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    if (section === "annotate") return loadWorkspace(Number(match[1]), Number(params.get("asset")) || null, routeEpoch);
    return loadProjectSection(Number(match[1]), section, routeEpoch);
  }
  if (location.hash === "#/users") return renderUsersPage();
  if (location.hash === "#/models") return renderModelsPage();
  return loadDashboard();
}

async function ensureAuthenticated(epoch = state.selectionEpoch) {
  const current = () => epoch === state.selectionEpoch;
  if (!current()) return false;
  if (state.authReady) return true;
  try {
    const healthResponse = await fetch("/api/health", { credentials: "same-origin" });
    if (!current()) return false;
    if (!healthResponse.ok) throw new Error("Health check failed");
    const health = await healthResponse.json();
    if (!current()) return false;
    state.health = health;
    state.authMode = health.auth_mode;
    if (health.setup_required) {
      mountSetup(app, { escapeHtml, onComplete: () => renderLoginScreen("管理员已创建，请登录。", false, true) });
      return false;
    }
    if (state.authMode !== "disabled") {
      const response = await fetch("/api/session", { credentials: "same-origin" });
      if (!current()) return false;
      if (!response.ok) { renderLoginScreen(); return false; }
      const session = await response.json();
      if (!current()) return false;
      state.session = session;
    }
    state.authReady = true;
    return true;
  } catch (_) {
    if (!current()) return false;
    app.innerHTML = `<div class="login-shell"><div class="login-card"><span class="brand-mark">${icon("logo")}</span><h1>服务不可用</h1><p>无法连接 CVSprout API，请检查服务是否已启动。</p></div></div>`;
    return false;
  }
}

function renderLoginScreen(error = "", useToken = false, notice = false) {
  annotationMenu?.close({ restoreFocus: false, force: true });
  canvasGuides?.destroy(); canvasGuides = null;
  clearInterval(state.pollTimer); clearInterval(state.lockTimer); clearInterval(state.collaborationTimer);
  state.lockOwned = false; state.authReady = false; state.session = null;
  state.currentSection = "login"; state.project = null; state.asset = null;
  state.selectionEpoch++; dashboardRequest++; closeModal();
  authCleanup?.(); authCleanup = null;
  const epoch = state.selectionEpoch;
  authCleanup = mountAuthScreen(app, {
    authMode: state.authMode, useToken: state.authMode === "token" || useToken,
    message: error, notice, icon, escapeHtml,
    isCurrent: () => state.currentSection === "login" && state.selectionEpoch === epoch,
    onLogin: async session => { state.session = session; state.authReady = true; await route(); },
  });
}

async function logout() {
  if (state.currentSection === "annotate" && (state.savingAnnotation || state.workflowBusy || hasAnnotationDraft())) return notify("当前标注尚未完成，请先保存或放弃本地草稿，再退出登录。", "warning");
  try {
    await releaseCurrentLock();
    await api("/api/session", { method: "DELETE" });
    state.authReady = false; state.session = null; renderLoginScreen();
  } catch (error) { if (error.status !== 401) notify("退出登录未完成，请检查网络后重试。", "error"); }
}

async function loadDashboard() {
  const request = ++dashboardRequest;
  const epoch = state.selectionEpoch, session = state.session;
  const current = () => request === dashboardRequest && epoch === state.selectionEpoch && session === state.session && state.currentSection === "projects";
  state.currentSection = "projects"; state.project = null; state.asset = null; state.annotations = []; state.lockOwned = false; state.readOnly = false; state.assistPoints = []; state.drawing = null; state.polygon = []; state.shapeEdit = null;
  app.innerHTML = `<div class="app-shell">${topbar()}<main class="dashboard"><div class="dashboard-inner"><div class="skeleton" style="height:180px"></div></div></main></div>`;
  try {
    const projects = await api(`/api/projects?archived=${state.projectsArchived && isAdmin()}`);
    if (!current()) return;
    state.projects = projects; renderDashboard();
  } catch (error) {
    if (current()) renderLoadError(error);
  }
}

function filteredProjects() {
  const query = state.projectQuery.trim().toLocaleLowerCase();
  const projects = state.projects.filter(project =>
    (!query || `${project.name} ${project.description}`.toLocaleLowerCase().includes(query)) &&
    (state.projectTaskFilter === "all" || (project.task_type || "mixed") === state.projectTaskFilter));
  return state.projectSort === "images" ? projects.sort((a, b) => b.asset_count - a.asset_count) : projects;
}

function renderDashboard() {
  const totalImages = state.projects.reduce((sum, project) => sum + project.asset_count, 0);
  const submitted = state.projects.reduce((sum, project) => sum + project.submitted_count, 0);
  app.innerHTML = `<div class="app-shell">${topbar()}
    <main class="dashboard project-dashboard"><div class="dashboard-inner">
      <div class="page-head"><div><h1>项目工作台</h1><p class="page-subtitle">AI 完成初标，协作校正与审核，让数据进入训练。</p></div>
        ${isAdmin() ? `<button class="btn primary" id="new-project">${icon("plus")}新建项目</button>` : ""}</div>
      <div class="workspace-summary"><span><strong>${state.projects.length}</strong> 个${state.projectsArchived ? "归档" : "进行中"}项目</span><span><strong>${totalImages.toLocaleString()}</strong> 张图片</span><span><strong>${submitted.toLocaleString()}</strong> 张待审核</span><div class="workflow-caption" aria-label="工作流程">导入数据 <span>›</span> AI 标注 <span>›</span> 校正审核 <span>›</span> 数据版本 <span>›</span> 训练 <span>›</span> 转换与导出</div></div>
      <div class="project-library-toolbar">
        <div class="library-views" aria-label="项目列表"><button data-project-view="active" class="${state.projectsArchived ? "" : "active"}" aria-pressed="${!state.projectsArchived}">我的项目</button>${isAdmin() ? `<button data-project-view="archived" class="${state.projectsArchived ? "active" : ""}" aria-pressed="${state.projectsArchived}">已归档</button>` : ""}</div>
        <div class="library-search-controls"><div class="search">${icon("search")}<input type="search" id="project-search" aria-label="搜索项目" placeholder="搜索名称或描述" value="${escapeHtml(state.projectQuery)}" /></div><select id="project-sort" aria-label="项目排序"><option value="updated">最近更新</option><option value="images" ${state.projectSort === "images" ? "selected" : ""}>图片最多</option></select></div>
      </div>
      <div class="project-filter-row"><div class="task-filters" aria-label="任务类型">${[["all","全部任务"],...Object.entries(TASK_LABELS)].map(([value,label]) => `<button data-task-filter="${value}" class="${state.projectTaskFilter === value ? "active" : ""}" aria-pressed="${state.projectTaskFilter === value}">${label}</button>`).join("")}</div><span id="project-result-count" role="status"></span></div>
      ${state.projectsArchived ? `<p class="archive-note">归档项目从默认列表收起，图片、标注和任务记录仍然保留，可随时恢复。</p>` : ""}
      <section class="dataset-project-grid" id="project-results" aria-label="项目列表"></section>
    </div></main></div>`;
  document.querySelector("#new-project")?.addEventListener("click", openProjectModal);
  document.querySelector("#project-search").oninput = event => { state.projectQuery = event.target.value; renderProjectResults(); };
  document.querySelector("#project-sort").onchange = event => { state.projectSort = event.target.value; renderProjectResults(); };
  document.querySelectorAll("[data-task-filter]").forEach(button => button.onclick = () => {
    state.projectTaskFilter = button.dataset.taskFilter;
    document.querySelectorAll("[data-task-filter]").forEach(item => { const active = item === button; item.classList.toggle("active", active); item.setAttribute("aria-pressed", String(active)); });
    renderProjectResults();
  });
  document.querySelectorAll("[data-project-view]").forEach(button => button.onclick = () => {
    state.projectsArchived = button.dataset.projectView === "archived";
    state.projectQuery = ""; state.projectTaskFilter = "all";
    void loadDashboard();
  });
  renderProjectResults();
}

function renderProjectResults() {
  const projects = filteredProjects();
  const filtering = state.projectQuery.trim() || state.projectTaskFilter !== "all";
  document.querySelector("#project-result-count").textContent = `${projects.length} 个项目`;
  document.querySelector("#project-results").innerHTML = projects.length ? projects.map(projectCard).join("") : `<div class="empty-state"><span class="card-icon">${icon("folder")}</span><strong>${filtering ? "没有符合条件的项目" : state.projectsArchived ? "暂无归档项目" : "从第一批图片开始"}</strong><p>${filtering ? "试试其他名称或任务类型。" : state.projectsArchived ? "归档的项目会显示在这里。" : "创建项目并选择任务类型，然后导入图片，开始 AI 标注。"}</p>${filtering ? `<button class="btn ghost" id="reset-project-filters">清除筛选</button>` : isAdmin() && !state.projectsArchived ? `<button class="btn primary" id="empty-create-project">新建项目</button>` : ""}</div>`;
  document.querySelector("#reset-project-filters")?.addEventListener("click", () => { state.projectQuery = ""; state.projectTaskFilter = "all"; renderDashboard(); });
  document.querySelector("#empty-create-project")?.addEventListener("click", openProjectModal);
  document.querySelectorAll("[data-upload-project]").forEach(button => button.onclick = () => {
    sessionStorage.setItem("visionforge-open-upload", "1");
    location.hash = `#/projects/${button.dataset.uploadProject}/data`;
  });
  document.querySelectorAll("[data-archive-project]").forEach(button => button.onclick = async () => {
    button.disabled = true;
    try {
      await api(`/api/projects/${button.dataset.archiveProject}/archive`, jsonOptions("PATCH", { archived: !state.projectsArchived }));
      notify(state.projectsArchived ? "项目已恢复到我的项目" : "项目已归档，可在已归档列表恢复");
      await loadDashboard();
    } catch (error) { button.disabled = false; notify(error.message, "error"); }
  });
}

function projectCard(project) {
  const percent = project.asset_count ? Math.round(project.labeled_count / project.asset_count * 100) : 0;
  const approved = project.asset_count ? Math.min(percent, Math.round(project.approved_count / project.asset_count * 100)) : 0;
  const base = `#/projects/${project.id}`;
  const nextSection = !project.asset_count ? "data" : project.labeled_count < project.asset_count ? "ai" : project.approved_count < project.asset_count ? "annotate" : "versions";
  const nextLabel = {data:"导入数据",ai:"AI 标注",annotate:"标注与审核",versions:"生成数据版本"}[nextSection];
  const previews = project.preview_asset_ids || [];
  return `<article class="dataset-project-card">
    <a class="project-filmstrip" href="${base}/overview" aria-label="打开项目 ${escapeHtml(project.name)}">${previews.length ? previews.map(id => `<img src="/api/assets/${id}/thumbnail?size=480" alt="" loading="lazy" />`).join("") : `<span>${icon("image")}等待导入图片</span>`}<span class="filmstrip-count">${project.asset_count.toLocaleString()} 张图片</span></a>
    <div class="dataset-project-body"><div class="project-title-row"><h2><a href="${base}/overview">${escapeHtml(project.name)}</a></h2><span class="task-badge">${TASK_LABELS[project.task_type || "mixed"]}</span></div>
    <p class="project-description">${escapeHtml(project.description || "进入项目查看数据、标注和训练记录")}</p>
    <div class="project-progress-label"><span>已标注 <strong>${project.labeled_count} / ${project.asset_count}</strong></span><strong>${percent}%</strong></div>
    <div class="dataset-progress" role="progressbar" aria-label="已标注比例" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><span class="approved" style="width:${approved}%"></span><span style="width:${Math.max(0, percent - approved)}%"></span></div>
    <div class="project-review-counts"><span>${project.label_count} 个类别</span><span>${project.submitted_count || 0} 待审核</span><span class="approved-count">${project.approved_count || 0} 已通过</span></div>
    <div class="project-card-actions">${state.projectsArchived ? `<button class="btn primary" data-archive-project="${project.id}">恢复项目</button><a class="btn ghost link-btn" href="${base}/overview">查看项目</a>` : `<a class="btn primary link-btn" href="${base}/${nextSection}">${icon(nextSection === "ai" ? "magic" : "arrowRight")}${nextLabel}</a>${nextSection !== "data" ? `<button class="btn ghost" data-upload-project="${project.id}">${icon("upload")}导入数据</button>` : ""}${isAdmin() ? `<button class="project-archive-action" data-archive-project="${project.id}" aria-label="归档项目 ${escapeHtml(project.name)}" title="收起项目，可从归档列表恢复">归档</button>` : ""}`}</div></div>
  </article>`;
}

function openProjectModal() {
  if (!isAdmin()) return notify("请联系管理员创建项目", "error");
  openModal({ title: "新建标注项目", subtitle: "选择任务类型，设置类别，创建后即可导入图片。", body: `
    <div class="field"><label>项目名称</label><input type="text" id="project-name" placeholder="例如：园区安全帽检测" maxlength="100" autofocus /></div>
    ${taskFieldsMarkup()}
    <div class="field"><label>需要识别的类别（用逗号分隔）</label><input type="text" id="project-labels" placeholder="person, helmet" /><div class="hint">例如 person, helmet（人、安全帽），或 合格, 不合格。类别名称应与使用的 AI 模型一致；创建后管理员仍可调整。</div></div>
    <div class="field"><label>项目描述</label><textarea id="project-description" placeholder="记录数据来源、场景和标注目标"></textarea></div>`,
    onReady: bindTaskFields,
    submitText: "创建并导入数据", onSubmit: async () => {
      const name = document.querySelector("#project-name").value.trim();
      if (!name) throw new Error("请输入项目名称");
      const labels = document.querySelector("#project-labels").value.split(/[,，]/).map(value => value.trim()).filter(Boolean);
      if (!labels.length) throw new Error("请至少填写一个标签类别");
      const project = await api("/api/projects", jsonOptions("POST", { name, description: document.querySelector("#project-description").value.trim(), labels, ...readTaskFields() }));
      sessionStorage.setItem("visionforge-open-upload", "1");
      closeModal(); location.hash = `#/projects/${project.id}/data`;
    },
  });
}

async function renderModelsPage() {
  const request = ++modelsRequest, epoch = state.selectionEpoch, session = state.session;
  const current = () => request === modelsRequest && epoch === state.selectionEpoch && session === state.session && state.currentSection === "models";
  state.currentSection = "models"; state.project = null; state.asset = null; state.annotations = []; state.lockOwned = false; state.readOnly = false; state.assistPoints = []; state.drawing = null; state.polygon = []; state.shapeEdit = null;
  app.innerHTML = `<div class="app-shell">${topbar("models")}<main class="dashboard"><div class="dashboard-inner"><div class="skeleton" style="height:180px"></div></div></main></div>`;
  try {
    const models = await api("/api/models");
    if (!current()) return;
    state.models = models;
    const configured = state.models.filter((model) => model.configured).length;
    app.innerHTML = `<div class="app-shell">${topbar("models")}<main class="dashboard"><div class="dashboard-inner">
      <div class="page-head"><div><div class="eyebrow">Model registry</div><h1>标注模型服务</h1><p class="page-subtitle">选择与项目任务匹配的 AI 模型。连接配置由管理员维护，标注时可以直接选择已配置的服务。</p></div><button class="btn primary" id="new-model" ${isAdmin()?"":"disabled"}>${icon("plus")}接入模型</button></div>
      <section class="overview-strip"><div class="metric"><div class="metric-label">模型服务</div><div class="metric-value">${state.models.length}</div></div><div class="metric"><div class="metric-label">已配置</div><div class="metric-value">${configured}</div></div><div class="metric"><div class="metric-label">全自动</div><div class="metric-value">${state.models.filter(m => m.modes.includes("auto")).length}</div></div><div class="metric"><div class="metric-label">半自动</div><div class="metric-value">${state.models.filter(m => m.modes.includes("assist")).length}</div></div></section>
      <div class="section-head"><h2>模型目录</h2></div><section class="project-grid">${state.models.map((model) => { const local = model.provider_type.startsWith("local_"); return `<article class="project-card model-service-card" style="min-height:245px"><span class="card-icon">${icon("magic")}</span><span class="badge">${!model.enabled ? "已停用" : model.last_health_status === "healthy" ? "健康" : model.configured ? (local ? "本地可用" : "待检测") : (local ? "权重缺失" : "待配置")}</span><h3>${escapeHtml(model.name)}</h3><p>${escapeHtml(model.description)}</p><div class="capabilities">${model.modes.map(mode => `<span>${mode === "auto" ? "全自动" : "半自动"}</span>`).join("")}${model.capabilities.map(c => `<span>${escapeHtml(c)}</span>`).join("")}</div>${model.availability_error ? `<p class="hint">模型暂不可用，${isAdmin() ? "请展开连接信息检查配置。" : "请联系管理员检查配置。"}</p>` : ""}<details class="technical-details"><summary>连接信息${model.availability_error ? "与不可用原因" : ""}</summary><div class="model-endpoint">${escapeHtml(local ? `本地权重 · ${model.model_name}` : model.endpoint || "尚未配置服务地址")}</div>${model.availability_error ? `<pre>${escapeHtml(model.availability_error)}</pre>` : ""}</details><div class="card-meta"><span>${escapeHtml(model.family)}</span><span class="model-actions">${isAdmin()?`<button class="mini-action" data-toggle-model="${model.id}">${model.enabled?"停用":"启用"}</button>`:""}<button class="mini-action" data-health-model="${model.id}" ${model.configured ? "" : "disabled"}>检测</button>${local ? `<span class="capabilities"><span>内置</span></span>` : isAdmin() ? `<button class="mini-action" data-edit-model="${model.id}">配置</button>` : ""}</span></div></article>`; }).join("")}</section>
    </div></main></div>`;
    if (isAdmin()) {
      const importButton = document.createElement("button");
      importButton.className = "btn";
      importButton.textContent = "导入训练权重";
      document.querySelector("#new-model").before(importButton);
      importButton.onclick = () => openModal({ title: "导入训练权重", subtitle: "导入你自己训练或确认来源的 Ultralytics .pt 权重，最多 2 GiB。",
        body: '<div class="field"><label for="checkpoint-file">模型文件</label><input id="checkpoint-file" type="file" accept=".pt"></div>',
        submitText: "导入", onSubmit: async () => {
          const file = document.querySelector("#checkpoint-file").files[0];
          if (!file) throw new Error("请选择 .pt 文件");
          const body = new FormData(); body.append("file", file);
          await api("/api/model-downloads/import/checkpoint", { method: "POST", body });
          closeModal(); notify("权重已导入，可以在训练模型目录中查看。"); await renderModelsPage();
        } });
      const downloadButton = document.createElement("button");
      downloadButton.className = "btn";
      downloadButton.textContent = "下载基础模型";
      document.querySelector("#new-model").before(downloadButton);
      downloadButton.onclick = () => {
        openModal({ title: "下载基础模型", wide: true,
          body: '<div id="model-download-panel" role="region" aria-label="基础模型下载">正在读取模型目录…</div>',
          submitText: "完成", onSubmit: async () => { closeModal(); await renderModelsPage(); },
          onReady: () => mountModelDownloads(document.querySelector("#model-download-panel"), { api, escapeHtml, notify }) });
      };
    }
    document.querySelector("#new-model").onclick = () => openModelModal();
    document.querySelectorAll("[data-toggle-model]").forEach(button => button.onclick = async () => {
      const model = state.models.find(item => item.id === button.dataset.toggleModel);
      button.disabled = true;
      try {
        await api(`/api/models/${model.id}/enabled`, jsonOptions("PATCH", { enabled: !model.enabled }));
        if (current()) { notify(model.enabled ? "模型已停用" : "模型已启用"); await renderModelsPage(); }
      } catch (error) { if (current()) notify(error.message, "error"); }
      finally { if (button.isConnected) button.disabled = false; }
    });
    document.querySelectorAll("[data-edit-model]").forEach(button => button.onclick = () => openModelModal(state.models.find(model => model.id === button.dataset.editModel)));
    document.querySelectorAll("[data-health-model]").forEach(button => button.onclick = () => checkModelHealth(button.dataset.healthModel));
  } catch (error) { if (current()) renderLoadError(error); }
}

function openModelModal(model = null) {
  if (!isAdmin()) return notify("需要管理员权限", "error");
  const epoch = state.selectionEpoch, session = state.session;
  const current = () => epoch === state.selectionEpoch && session === state.session && state.currentSection === "models";
  const editing = Boolean(model);
  const modes = model?.modes || ["auto"];
  openModal({ title: editing ? "配置模型服务" : "接入模型服务", subtitle: "平台调用 OpenAI 兼容视觉接口，或统一的远程检测/分割协议。", wide: true, body: `
    <div class="form-grid"><div class="field"><label>服务 ID</label><input type="text" id="model-id" value="${escapeHtml(model?.id || "")}" placeholder="factory-detector" ${editing ? "disabled" : ""}/></div><div class="field"><label>显示名称</label><input type="text" id="model-name" value="${escapeHtml(model?.name || "")}" placeholder="产线目标检测" /></div></div>
    <div class="form-grid"><div class="field"><label>适配协议</label><select id="model-provider"><option value="remote_detection" ${model?.provider_type === "remote_detection" ? "selected" : ""}>远程目标检测</option><option value="remote_segmentation" ${model?.provider_type === "remote_segmentation" ? "selected" : ""}>远程自动分割</option><option value="remote_classification" ${model?.provider_type === "remote_classification" ? "selected" : ""}>远程图片分类</option><option value="remote_keypoints" ${model?.provider_type === "remote_keypoints" ? "selected" : ""}>远程关键点</option><option value="interactive_segmentation" ${model?.provider_type === "interactive_segmentation" ? "selected" : ""}>交互式分割</option><option value="openai_vision" ${model?.provider_type === "openai_vision" ? "selected" : ""}>OpenAI 兼容视觉模型</option></select></div><div class="field"><label>模型族</label><input type="text" id="model-family" value="${escapeHtml(model?.family || "自定义视觉模型")}" /></div></div>
    <div class="field"><label>推理端点</label><input type="text" id="model-endpoint" value="${escapeHtml(model?.endpoint || "")}" placeholder="http://inference:8100/predict" /></div>
    <div class="form-grid"><div class="field"><label>后端模型名</label><input type="text" id="model-backend-name" value="${escapeHtml(model?.model_name || "")}" placeholder="模型服务需要时填写" /></div><div class="field"><label>密钥环境变量</label><input type="text" id="model-secret-env" value="${escapeHtml(model?.api_key_env || "")}" placeholder="VISION_MODEL_API_KEY" /></div></div>
    <div class="field"><label>描述</label><textarea id="model-description">${escapeHtml(model?.description || "")}</textarea></div>
    <div class="field"><label>能力（逗号分隔）</label><input type="text" id="model-capabilities" value="${escapeHtml((model?.capabilities || ["检测框"]).join(", "))}" /></div>
    <div class="scope-row"><label class="scope-option"><input type="checkbox" id="mode-auto" ${modes.includes("auto") ? "checked" : ""}/>全自动批量标注</label><label class="scope-option"><input type="checkbox" id="mode-assist" ${modes.includes("assist") ? "checked" : ""}/>半自动交互标注</label><label class="scope-option"><input type="checkbox" id="model-enabled" ${model?.enabled !== false ? "checked" : ""}/>启用服务</label></div>`,
    submitText: "保存配置",
    onReady: () => {
      if (editing && !model.builtin) {
        const actions = document.querySelector(".modal-actions");
        const remove = document.createElement("button"); remove.className = "btn danger modal-delete"; remove.textContent = "删除服务";
        remove.onclick = async () => {
          if (!window.confirm(`确定删除 ${model.name}？`)) return;
          await api(`/api/models/${model.id}`, { method: "DELETE" }); if (current()) { closeModal(); notify("模型服务已删除"); renderModelsPage(); }
        };
        actions.prepend(remove);
      }
    },
    onSubmit: async () => {
      const selectedModes = [document.querySelector("#mode-auto").checked ? "auto" : null, document.querySelector("#mode-assist").checked ? "assist" : null].filter(Boolean);
      if (!selectedModes.length) throw new Error("至少选择一种标注模式");
      const payload = {
        id: (model?.id || document.querySelector("#model-id").value).trim(), name: document.querySelector("#model-name").value.trim(), family: document.querySelector("#model-family").value.trim(), provider_type: document.querySelector("#model-provider").value,
        description: document.querySelector("#model-description").value.trim(), endpoint: document.querySelector("#model-endpoint").value.trim(), model_name: document.querySelector("#model-backend-name").value.trim(), api_key_env: document.querySelector("#model-secret-env").value.trim(),
        capabilities: document.querySelector("#model-capabilities").value.split(",").map(value => value.trim()).filter(Boolean), modes: selectedModes, timeout_seconds: model?.timeout_seconds || 180, enabled: document.querySelector("#model-enabled").checked, config: model?.config || { badge: "自定义" },
      };
      if (!payload.id || !payload.name || !payload.endpoint || !payload.capabilities.length) throw new Error("请完整填写 ID、名称、端点和能力");
      await api(editing ? `/api/models/${model.id}` : "/api/models", jsonOptions(editing ? "PUT" : "POST", payload)); if (current()) { closeModal(); notify("模型服务配置已保存"); renderModelsPage(); }
    },
  });
}

async function checkModelHealth(modelId) {
  const epoch = state.selectionEpoch, session = state.session;
  const current = () => epoch === state.selectionEpoch && session === state.session && state.currentSection === "models";
  const button = document.querySelector(`[data-health-model="${CSS.escape(modelId)}"]`);
  if (button?.disabled) return;
  if (button) { button.disabled = true; button.textContent = "检测中…"; }
  try {
    notify("正在检测模型服务，请稍候…", "info");
    const result = await api(`/api/models/${modelId}/health`, { method: "POST" });
    if (!current()) return;
    notify(result.status === "healthy" ? "模型服务连接正常" : `连接异常：${result.error || "未知错误"}`, result.status === "healthy" ? "success" : "error");
    await renderModelsPage();
  } catch (error) { if (current()) notify(error.message, "error"); }
  finally { if (button?.isConnected) { button.disabled = false; button.textContent = "检测"; } }
}

async function safeApi(path, fallback) {
  try { return await api(path); }
  catch (error) {
    if ([404, 405].includes(error.status) || String(error.message).includes("404") || String(error.message).includes("405") || error.message === "Not Found") return fallback;
    throw error;
  }
}

function resetProjectInteraction(projectId, section) {
  state.saveFeedback = { status:"idle", message:"" }; state.readOnlyReason = ""; state.handedOffAssetId = null;
  state.workflowBusy = false; state.reviewBatchToken = null;
  const changedProject = state.project?.id !== projectId;
  state.currentSection = section;
  state.activeWorkBlockId = null; state.activeWorkBlock = null; state.legacyReviewAssetId = null;
  if (section !== "annotate") state.asset = null;
  state.annotations = [];
  state.selectedAnnotation = null;
  state.reviewEvents = [];
  state.shapeEdit = null;
  state.drawing = null;
  state.polygon = [];
  state.assistPoints = [];
  state.savingAnnotation = false;
  state.pendingAnnotation = null; state.pendingTaskEdit = null; state.pendingObjectEdit = null; state.selectionIntent = false; state.advancedAnnotationDirty = false; state.advancedDraft = null;
  state.lockOwned = false;
  state.readOnly = false;
  if (changedProject) {
    state.workBlocks = { enabled: false, blocks: [], unassigned_count: 0 };
    state.workBlockFilter = "mine";
    state.asset = null;
    state.activeLabel = null;
    state.assetFilter = "all";
    state.assetRenderLimit = 200;
    state.assetListScroll = 0;
    state.inspectorTab = "objects";
    state.dataSearch = "";
    state.dataStatus = "all";
    state.selectedAssetIds = new Set();
    state.trainingFilters = { status: "all", task: "all", query: "" };
    state.trainingPage = { page: 1, loading: false, error: "", request: 0 };
    state.selectedTrainingId = null; state.selectedTrainingRun = null;
    state.trainingSelectionLoading = false; state.trainingSelectionError = "";
    state.trainingSelectionRequest += 1;
    state.trainingListOpen = window.innerWidth >= 900;
    state.selectedTrainingVersion = null;
    state.expandedTrainingRuns = new Set();
    state.trainingLogs = {};
    state.trainingMetricsHistory = {};
    state.conversions = [];
    state.conversionCounts = {};
    state.projectConversionsAvailable = false;
    state.zoom = 1;
    state.tool = "select";
  }
}

function projectSubnav(active) {
  // Project workflow links are part of the shared left navigation.
  return "";
}

function projectPageShell(section, content) {
  return `<div class="app-shell">${topbar("projects")}${projectSubnav(section)}<main class="project-page"><div class="project-page-inner">${content}</div></main></div>`;
}

function fallbackAnalytics(project, assets) {
  const review = { draft: 0, in_progress: 0, submitted: 0, approved: 0, rejected: 0 };
  assets.forEach(asset => { review[asset.review_status] = (review[asset.review_status] || 0) + 1; });
  return {
    summary: {
      assets: assets.length,
      labeled: assets.filter(asset => asset.status === "labeled").length,
      annotations: assets.reduce((sum, asset) => sum + asset.annotation_count, 0),
      approved: review.approved,
    },
    workflow: { unlabeled: assets.filter(asset => asset.status === "unlabeled").length, ...review },
    review,
    labels: project.labels.map(label => ({ ...label, annotation_count: 0, asset_count: 0 })),
    quality: {
      unlabeled: assets.filter(asset => asset.status === "unlabeled").length,
      unapproved: assets.length - review.approved,
      rejected: review.rejected,
    },
  };
}

async function loadProjectSection(projectId, section, routeEpoch = state.selectionEpoch) {
  resetProjectInteraction(projectId, section);
  if (section === "train") restoreTrainingNavigation(projectId);
  if (section === "data") {
    const status = new URLSearchParams(location.hash.split("?")[1] || "").get("status");
    if (["all", "unlabeled", "labeled", "draft", "in_progress", "submitted", "approved", "rejected"].includes(status)) {
      state.dataStatus = status; state.dataSearch = ""; state.assetRenderLimit = 200;
    }
  }
  app.innerHTML = `<div class="app-shell">${topbar()}<main class="dashboard"><div class="dashboard-inner"><div class="skeleton" style="height:70vh"></div></div></main></div>`;
  try {
    const [project, assets, jobs, models, analytics, versions, training, workBlocks] = await Promise.all([
      api(`/api/projects/${projectId}`),
      api(`/api/projects/${projectId}/assets`),
      api(`/api/projects/${projectId}/jobs`),
      api("/api/models"),
      safeApi(`/api/projects/${projectId}/analytics`, null),
      safeApi(`/api/projects/${projectId}/versions`, []),
      section === "train" ? pipelineApi(trainingListURL(projectId, state.trainingPage.page, state.trainingFilters) + '&include_availability=false').catch(() => ({runs:[],total:0,counts:{},_loadError:'训练记录暂时无法加载，正在自动重试。'})) : Promise.resolve({ availability: {}, runs: [] }),
      api(`/api/projects/${projectId}/work-blocks`),
    ]);
    if (routeEpoch !== state.selectionEpoch) return;
    state.project = project;
    state.assets = assets;
    state.workBlocks = workBlocks;
    state.jobs = jobs;
    state.models = models;
    state.analytics = analytics || fallbackAnalytics(project, assets);
    state.versions = Array.isArray(versions) ? versions : versions.versions || [];
    state.training = Array.isArray(training) ? { availability: { available: true }, runs: training } : { ...training, availability: training.availability ?? state.training?.availability ?? {} };
    if (section === "train") {
      state.trainingCatalog = normalizeTrainingCatalog(state.trainingCatalog, state.training?.availability);
      state.trainingResources = normalizeTrainingResources(state.trainingResources, state.trainingCatalog, state.training?.availability);
      applyTrainingListResponse(training);
      state.trainingPage.error = training._loadError || '';
      state.trainingEnvironmentLoading = true;
      await selectTrainingMonitor(state.selectedTrainingId || state.training.runs[0]?.id || null, false, { render: false });
    }
    if (routeEpoch !== state.selectionEpoch) return;
    state.activeLabel = project.labels[0] || null; state.tool = "select"; state.poseDraft = null; state.keypointPlacement = null; state.inspectorTab = "objects";
    renderProjectSection(section);
    if (sessionStorage.getItem("visionforge-open-upload") === "1") {
      sessionStorage.removeItem("visionforge-open-upload");
      openUploadModal();
    }
    if (section === "ai" || section === "overview") startJobPolling();
    if (section === "blocks") {
      state.pollTimer = setInterval(() => {
        if (state.currentSection === "blocks" && state.project?.id === projectId && !document.hidden && !modalRoot.children.length) void refreshWorkBlocks().catch(() => {});
      }, 10000);
    }
    if (section === "train") { startPipelinePolling(); void refreshTrainingEnvironment(); }
  } catch (error) {
    if (routeEpoch !== state.selectionEpoch) return;
    renderLoadError(error);
  }
}

function renderProjectSection(section = state.currentSection) {
  state.currentSection = section;
  if (section === "blocks") return renderWorkBlocksPage();
  if (section === "data") return renderDataPage();
  if (section === "ai") return renderAiPage();
  if (section === "versions") return renderVersionsPage();
  if (section === "train") return renderTrainingPage();
  return renderProjectOverview();
}

async function refreshWorkBlocks() {
  const projectId = state.project.id, epoch = state.selectionEpoch;
  const request = ++state.workBlocksRequest;
  const blocks = await api(`/api/projects/${projectId}/work-blocks`);
  if (request !== state.workBlocksRequest || state.project?.id !== projectId || state.selectionEpoch !== epoch || state.currentSection !== "blocks") return;
  state.workBlocks = blocks;
  renderWorkBlocksPage();
}

function renderWorkBlocksPage() {
  const scroll = document.querySelector(".project-page")?.scrollTop || 0;
  app.innerHTML = projectPageShell("blocks", workBlocksMarkup({ state, isAdmin, escapeHtml, icon }));
  document.querySelector(".project-page").scrollTop = scroll;
  bindCommonProjectEvents();
  bindWorkBlocks({ state, api, jsonOptions, openModal, closeModal, notify, escapeHtml,
    refresh: refreshWorkBlocks, render: renderWorkBlocksPage,
    submitBlock: block => openWorkBlockSubmit(block, refreshWorkBlocks),
  });
}

function completionPercent() {
  const total = state.project?.asset_count || 0;
  return total ? Math.round((state.project?.approved_count || 0) / total * 100) : 0;
}

function renderProjectOverview() {
  const previousScroll = document.querySelector("#overview-training-content") ? document.querySelector(".project-page")?.scrollTop || 0 : 0;
  const summary = state.analytics?.summary || {};
  const workflow = state.analytics?.workflow || {};
  const review = state.analytics?.review || {};
  const total = summary.asset_count ?? summary.assets ?? state.assets.length;
  const labeled = summary.labeled_count ?? summary.labeled ?? state.assets.filter(asset => asset.status === "labeled").length;
  const approved = review.approved ?? summary.approved ?? state.assets.filter(asset => asset.review_status === "approved").length;
  const annotations = summary.annotation_count ?? summary.annotations ?? state.assets.reduce((sum, asset) => sum + asset.annotation_count, 0);
  const running = state.jobs.filter(job => ["queued", "running"].includes(job.status));
  const base = `#/projects/${state.project.id}`;
  const awaitingSubmission = state.assets.filter(asset => asset.annotation_count > 0 && ["draft", "in_progress"].includes(asset.review_status)).length;
  const next = !total
    ? { label: "导入第一批数据", href: `${base}/data`, icon: "upload", copy: "上传图片或 ZIP，建立工作数据集。" }
    : running.length
      ? { label: "查看 AI 标注进度", href: `${base}/ai?tab=history`, icon: "magic", copy: `${running.length} 个任务正在排队或标注。可以先查看已保存的结果，任务结束后按批次提交审核。` }
      : review.rejected
        ? { label: "处理退回的图片", href: `${base}/data?status=rejected`, icon: "polygon", copy: `${review.rejected} 张图片需要修改。打开图片查看审核意见，校正后再次提交。` }
        : review.submitted && canReview()
          ? { label: "审核已提交的图片", href: `${base}/data?status=submitted`, icon: "check", copy: `${review.submitted} 张图片等待审核。检查后可单张或批量通过，也可以退回并说明修改要求。` }
          : awaitingSubmission
            ? { label: "检查结果并提交审核", href: `${base}/ai?tab=history`, icon: "check", copy: `${awaitingSubmission} 张图片已有标注但尚未提交。检查结果后，从任务历史或批量选择入口提交审核。` }
            : labeled < total
              ? { label: "AI 批量标注", href: `${base}/ai?tab=configure`, icon: "magic", copy: `还有 ${total - labeled} 张图片未标注，先由 AI 生成结果，再按需校正。` }
              : state.versions.length
                ? { label: "查看训练与模型", href: `${base}/train`, icon: "cpu", copy: `已保存 ${state.versions.length} 个数据版本。${canReview() ? "可以选择版本训练模型，再转换或下载模型。" : "可以查看训练进度并下载已完成的模型。"}` }
                : approved
                  ? { label: canReview() ? "生成数据版本" : "查看数据版本", href: `${base}/versions`, icon: "layers", copy: `已有 ${approved} 张图片审核通过。${canReview() ? "可先用这部分数据生成版本，无需等待所有图片审核完成。" : "管理员或审核员可以将它们保存为训练数据版本。"}` }
                  : { label: "查看审核进度", href: `${base}/data?status=submitted`, icon: "check", copy: "图片已提交，等待审核员确认。可以继续导入新图片，或查看当前协作进度。" };
  app.innerHTML = projectPageShell("overview", `
    <div class="page-head compact-head"><div><div class="eyebrow">项目概览</div><h1>${escapeHtml(state.project.name)}</h1><p class="page-subtitle">${escapeHtml(state.project.description || "从原始数据到可部署模型的完整视觉数据流程。")}</p></div><a class="btn primary link-btn" href="${next.href}">${icon(next.icon)}${next.label}</a></div>
    <section class="overview-strip project-metrics"><div class="metric"><div class="metric-label">图片总数</div><div class="metric-value">${Number(total).toLocaleString()}</div></div><div class="metric"><div class="metric-label">标注对象</div><div class="metric-value">${Number(annotations).toLocaleString()}</div></div><div class="metric"><div class="metric-label">已标注图片</div><div class="metric-value">${Number(labeled).toLocaleString()}</div></div><div class="metric"><div class="metric-label">审核完成</div><div class="metric-value">${Number(approved).toLocaleString()}</div></div></section>
    <div class="project-overview-grid"><section class="surface workflow-surface"><div class="surface-head"><div><span class="eyebrow">建议下一步</span><h2>${next.label}</h2></div><span class="completion-ring" style="--progress:${completionPercent() * 3.6}deg"><strong>${completionPercent()}%</strong><small>审核通过</small></span></div><p>${next.copy}</p><a class="btn primary link-btn" href="${next.href}">${icon(next.icon)}开始</a><div class="workflow-funnel"><a href="${base}/data?status=all"><strong>${total}</strong><span>已导入</span></a><i></i><a href="${base}/data?status=unlabeled"><strong>${workflow.unlabeled ?? total - labeled}</strong><span>未标注</span></a><i></i><a href="${base}/data?status=submitted"><strong>${review.submitted || 0}</strong><span>待审核</span></a><i></i><a href="${base}/data?status=approved"><strong>${approved}</strong><span>已通过</span></a></div>${approved && !state.versions.length ? `<p class="workflow-version-ready">已有 ${approved} 张审核通过的图片，可用于<a href="${base}/versions">生成数据版本</a>。</p>` : ""}</section>
      <section class="surface"><div class="surface-head"><div><span class="eyebrow">质量检查</span><h2>数据质量</h2></div><a href="#/projects/${state.project.id}/data">查看数据</a></div>${qualityChecklistMarkup()}</section>
      <section class="surface overview-training"><div class="surface-head"><div><h2>训练记录</h2></div><a href="${base}/train">训练与模型</a></div><div id="overview-training-content" aria-live="polite"><div class="empty-compact">正在加载训练记录…</div></div></section>
      <section class="surface"><div class="surface-head"><div><span class="eyebrow">任务动态</span><h2>运行中的任务</h2></div><a href="#/projects/${state.project.id}/ai?tab=history">任务中心</a></div>${running.length ? running.map(projectJobCard).join("") : `<div class="empty-compact">当前没有运行中的 AI 任务</div>`}</section>
      <section class="surface"><div class="surface-head"><div><span class="eyebrow">数据版本</span><h2>最近版本</h2></div><a href="#/projects/${state.project.id}/versions">全部版本</a></div>${state.versions.length ? state.versions.slice(0, 3).map(versionCompactCard).join("") : `<div class="empty-compact">审核通过后，由管理员或审核员创建数据版本，用于后续训练。</div>`}</section></div>`);
  bindCommonProjectEvents();
  const projectId = state.project.id, epoch = state.selectionEpoch;
  mountOverviewTraining({ projectId, userId: state.session?.user?.id || state.session?.actor || "local", api, escapeHtml, statusLabel: trainingStatusLabel, isCurrent: () => state.project?.id === projectId && state.selectionEpoch === epoch && state.currentSection === "overview" });
  document.querySelector(".project-page").scrollTop = previousScroll;
}

function qualityChecklistMarkup() {
  if (!state.assets.length) return `<div class="empty-compact">导入图片后，这里会检查标注、审核和文件状态。</div>`;
  const quality = state.analytics?.quality || {};
  const summary = state.analytics?.summary || {};
  const review = state.analytics?.review || {};
  const unlabeled = summary.unlabeled_count ?? state.assets.filter(asset => asset.status === "unlabeled").length;
  const unapproved = (summary.asset_count ?? state.assets.length) - Number(review.approved || 0);
  const issueCount = Number(quality.missing_file_count || 0) + Number(quality.empty_labeled_count || 0) + Number(quality.approved_without_annotations_count || 0) + Number(quality.duplicate_hash_group_count || 0) + Number(quality.tiny_annotation_count || 0);
  const checks = [
    [Number(unlabeled) === 0, "所有图片已有标注", unlabeled ? `${unlabeled} 张未标注` : "已完成"],
    [Number(unapproved) === 0, "所有标注已经审核", unapproved ? `${unapproved} 张未通过` : "已完成"],
    [Number(review.rejected || 0) === 0, "没有待修改的退回图片", review.rejected ? `${review.rejected} 张待修改` : "正常"],
    [issueCount === 0, "文件和标注位置检查", issueCount ? `${issueCount} 个问题` : "正常"],
    [state.project.labels.length > 0, "已设置标注类别", `${state.project.labels.length} 个类别`],
  ];
  return `<div class="quality-list">${checks.map(([ok, label, detail]) => `<div class="quality-row ${ok ? "ok" : "attention"}"><span>${ok ? icon("check") : icon("warning")}</span><div><strong>${label}</strong><small>${detail}</small></div></div>`).join("")}</div>`;
}

function filteredDataAssets() {
  const search = state.dataSearch.trim().toLocaleLowerCase();
  return state.assets.filter(asset => {
    const matchesSearch = !search || asset.filename.toLocaleLowerCase().includes(search);
    const matchesStatus = state.dataStatus === "all"
      || (state.dataStatus === "labeled" || state.dataStatus === "unlabeled" ? asset.status === state.dataStatus : asset.review_status === state.dataStatus);
    return matchesSearch && matchesStatus;
  });
}

function renderDataPage() {
  const filtered = filteredDataAssets();
  const visible = filtered.slice(0, state.assetRenderLimit);
  app.innerHTML = projectPageShell("data", `
    <div class="page-head compact-head"><div><div class="eyebrow">图片与标注</div><h1>数据管理</h1><p class="page-subtitle">导入图片并运行 AI 标注；检查结果后可批量提交审核，审核员可批量通过或退回。</p></div><div class="page-head-actions"><button class="btn ghost" id="data-export" ${state.assets.length ? "" : "disabled"}>${icon("download")}下载当前数据</button><button class="btn primary" id="data-upload">${icon("upload")}导入数据</button></div></div>
    <div class="work-block-entry"><span>${state.workBlocks.enabled ? `已启用任务块，领取后仅修改自己负责的图片。${state.workBlocks.unassigned_count ? `还有 ${state.workBlocks.unassigned_count} 张未分块。` : ""}` : "多人协作时，可以将图片分成任务块，由成员独立领取。"}</span><a class="btn ghost link-btn" href="#/projects/${state.project.id}/blocks">${state.workBlocks.enabled ? "我的任务 / 领取" : "管理任务块"}</a></div>
    ${reviewQueueMarkup(state, canReview())}
    <section class="data-toolbar surface"><div class="search data-search">${icon("search")}<input id="data-search" aria-label="按文件名搜索图片" placeholder="按文件名搜索" value="${escapeHtml(state.dataSearch)}" /></div><select id="data-status" aria-label="状态筛选"><option value="all">全部状态</option><option value="unlabeled" ${state.dataStatus === "unlabeled" ? "selected" : ""}>未标注</option><option value="labeled" ${state.dataStatus === "labeled" ? "selected" : ""}>已标注</option><option value="draft" ${state.dataStatus === "draft" ? "selected" : ""}>草稿</option><option value="in_progress" ${state.dataStatus === "in_progress" ? "selected" : ""}>标注中</option><option value="submitted" ${state.dataStatus === "submitted" ? "selected" : ""}>待审核</option><option value="approved" ${state.dataStatus === "approved" ? "selected" : ""}>已通过</option><option value="rejected" ${state.dataStatus === "rejected" ? "selected" : ""}>已退回</option></select><span class="data-result-count">${filtered.length.toLocaleString()} / ${state.assets.length.toLocaleString()} 张</span><div class="toolbar-spacer"></div><button class="btn ghost" id="select-visible">选择全部 ${filtered.length} 张结果</button><button class="btn ghost" id="clear-selection" ${state.selectedAssetIds.size ? "" : "disabled"}>清除选择</button><button class="btn primary" id="annotate-selection" ${state.selectedAssetIds.size ? "" : "disabled"}>${icon("magic")}AI 标注 ${state.selectedAssetIds.size || ""}</button>${isAdmin() ? `<button class="btn ghost danger" id="delete-selected-assets" ${!state.selectedAssetIds.size || state.selectedAssetIds.size > 500 ? "disabled" : ""} title="每次最多删除 500 张图片">${icon("trash")}删除所选</button>` : ""}</section>
    <div id="selection-slot">${dataSelectionMarkup()}</div>
    <section class="asset-data-grid">${visible.length ? visible.map(dataAssetCard).join("") : `<div class="empty-state"><span class="card-icon">${icon("image")}</span><strong>${state.assets.length ? "没有符合条件的图片" : "还没有导入图片"}</strong><p>${state.assets.length ? "调整搜索或筛选条件后再试。" : "点击“导入数据”添加图片，然后开始 AI 标注。"}</p>${state.assets.length ? `<button class="btn ghost" id="reset-data-filters">清除筛选</button>` : `<button class="btn primary" id="empty-data-upload">导入图片</button>`}</div>`}</section>
    ${visible.length < filtered.length ? `<button class="btn ghost load-more-data" id="load-more-data">再显示 ${Math.min(200, filtered.length - visible.length)} 张</button>` : ""}`);
  bindCommonProjectEvents();
  document.querySelector("#data-upload").onclick = openUploadModal;
  document.querySelector("#data-export").onclick = openExportModal;
  document.querySelector("#empty-data-upload")?.addEventListener("click", openUploadModal);
  document.querySelector("#reset-data-filters")?.addEventListener("click", () => {
    state.dataSearch = ""; state.dataStatus = "all"; state.assetRenderLimit = 200;
    history.replaceState(null, "", `#/projects/${state.project.id}/data`); renderDataPage();
  });
  const dataSearch = document.querySelector("#data-search");
  let composingDataSearch = false;
  const searchData = () => {
    const start = dataSearch.selectionStart, end = dataSearch.selectionEnd;
    state.dataSearch = dataSearch.value; state.assetRenderLimit = 200; renderDataPage();
    const input = document.querySelector("#data-search"); input.focus({ preventScroll: true }); input.setSelectionRange(start, end);
  };
  dataSearch.oncompositionstart = () => { composingDataSearch = true; };
  dataSearch.oncompositionend = () => { composingDataSearch = false; searchData(); };
  dataSearch.oninput = (event) => { if (!event.isComposing && !composingDataSearch) searchData(); };
  document.querySelector("#data-status").onchange = (event) => { state.dataStatus = event.target.value; state.assetRenderLimit = 200; renderDataPage(); };
  document.querySelector("#load-more-data")?.addEventListener("click", () => { state.assetRenderLimit += 200; renderDataPage(); });
  document.querySelector("#select-visible").onclick = () => { filtered.forEach(asset => state.selectedAssetIds.add(asset.id)); refreshDataSelectionUI(); };
  document.querySelector("#clear-selection").onclick = () => { state.selectedAssetIds.clear(); refreshDataSelectionUI(); };
  document.querySelector("#annotate-selection").onclick = () => openAiModal([...state.selectedAssetIds]);
  document.querySelectorAll("[data-select-asset]").forEach(input => input.onchange = (event) => { const id = Number(input.dataset.selectAsset); event.target.checked ? state.selectedAssetIds.add(id) : state.selectedAssetIds.delete(id); refreshDataSelectionUI(); });
  document.querySelectorAll("[data-open-asset]").forEach(button => button.onclick = () => { location.hash = `#/projects/${state.project.id}/annotate?asset=${button.dataset.openAsset}`; });
}

function dataSelectionMarkup() {
  return batchReviewSelectionMarkup(state, canReview());
}

function refreshDataSelectionUI() {
  document.querySelectorAll("[data-select-asset]").forEach(input => {
    const selected = state.selectedAssetIds.has(Number(input.dataset.selectAsset));
    input.checked = selected; input.closest(".data-asset-card")?.classList.toggle("selected", selected);
  });
  const button = document.querySelector("#annotate-selection");
  if (button) { button.disabled = !state.selectedAssetIds.size; button.innerHTML = `${icon("magic")}AI 标注 ${state.selectedAssetIds.size || ""}`; }
  const clear = document.querySelector("#clear-selection"); if (clear) clear.disabled = !state.selectedAssetIds.size;
  const deleteButton = document.querySelector("#delete-selected-assets"); if (deleteButton) deleteButton.disabled = !state.selectedAssetIds.size || state.selectedAssetIds.size > 500;
  const slot = document.querySelector("#selection-slot"); if (slot) slot.innerHTML = dataSelectionMarkup();
}

function dataAssetCard(asset) {
  return `<article class="data-asset-card ${state.selectedAssetIds.has(asset.id) ? "selected" : ""}"><label class="asset-check" title="选择图片"><input type="checkbox" data-select-asset="${asset.id}" ${state.selectedAssetIds.has(asset.id) ? "checked" : ""}/><span></span></label><button class="data-thumb" data-open-asset="${asset.id}" aria-label="打开 ${escapeHtml(asset.filename)}"><img alt="" loading="lazy" decoding="async" src="/api/assets/${asset.id}/thumbnail?size=360" onerror="this.onerror=null;this.src='/api/assets/${asset.id}/image'" /></button><div class="data-card-body"><strong title="${escapeHtml(asset.filename)}">${escapeHtml(asset.filename)}</strong><span>${asset.width}×${asset.height} · ${formatBytes(asset.size_bytes)}</span><div><span class="review-mini ${asset.review_status}">${reviewStatusName(asset.review_status)}</span><small>${projectTask() === "classify" ? asset.annotation_count ? "已分类" : "待分类" : asset.annotation_count ? `${asset.annotation_count} 个标注` : "尚无标注"}</small></div></div>${isAdmin() ? `<div class="data-card-delete"><button class="btn ghost danger" data-delete-asset="${asset.id}" aria-label="删除图片 ${escapeHtml(asset.filename)}">${icon("trash")}删除图片</button></div>` : ""}</article>`;
}

function renderAiPage() {
  if (deferRenderForNavigation("ai")) return;
  const tab = getSectionTab(state.session, state.project.id, "ai", "configure");
  const focusedTab = document.activeElement?.closest('[data-section-tab^="ai:"]')?.id;
  const history = createHistoryNavigation({ projectId: state.project.id, kind: "ai", session: state.session, records: state.jobs, pageSize: 8, escapeHtml,
    searchText: job => `${state.models.find(model => model.id === job.model_id)?.name || "AI 标注"} 任务 #${job.id}`,
    statuses: [["queued", "排队中"], ["running", "标注中"], ["completed", "已完成"], ["failed", "未完成"], ["cancelled", "已取消"]] });
  if (!history) return;
  const available = state.models.filter(model => model.enabled && model.configured && modelFitsProject(model));
  const modelsExpanded = Boolean(app.querySelector('.ai-model-strip')?.closest('details')?.open);
  app.innerHTML = projectPageShell("ai", `
    <div class="page-head compact-head"><div><div class="eyebrow">AI 标注</div><h1>AI 标注中心</h1><p class="page-subtitle">由 AI 批量完成${escapeHtml(TASK_LABELS[projectTask()])}标注，成员按需校正，审核通过后进入训练。</p></div><button class="btn primary" id="new-ai-job" ${available.length && state.assets.length ? "" : "disabled"}>${icon("magic")}开始 AI 标注</button></div><div class="ai-page-tabs">${sectionTabsMarkup("ai", [["configure", "配置标注任务"], ["history", `任务历史（${state.jobs.length}）`]], tab, escapeHtml)}</div><div ${sectionPanelAttributes("ai", "configure", tab)}>${!state.assets.length ? `<div class="step-guidance"><strong>先导入图片</strong><span>导入后才能让 AI 标注。</span><a href="#/projects/${state.project.id}/data">前往导入</a></div>` : !available.length ? `<div class="step-guidance"><strong>暂时没有可用模型</strong><span>${isAdmin() ? "请先配置支持当前任务类型的模型。" : "请联系管理员配置模型后再继续。"}</span>${isAdmin() ? `<a href="#/models">配置模型</a>` : ""}</div>` : ""}
    <section class="surface ai-config-intro"><div><h2>先试标，再批量处理</h2><p>选择图片和 AI 模型，核对类别与识别阈值。可先选 1–3 张样本预览，调整提示词和置信度；预览满意后，沿用当前参数启动正式标注。</p><div class="ai-config-counts"><span>项目图片 <strong>${state.assets.length}</strong> 张</span><span>可用模型 <strong>${available.length}</strong> 个</span><span>运行中 <strong>${state.jobs.filter(job => ["queued", "running"].includes(job.status)).length}</strong> 个任务</span></div></div><button class="btn primary" id="configure-ai-start" ${available.length && state.assets.length ? "" : "disabled"}>${icon("sliders")}配置与预览效果</button></section>
    <details class="advanced-settings" ${modelsExpanded ? "open" : ""}><summary>查看本项目可用的 ${available.length} 个 AI 模型</summary><section class="ai-model-strip">${available.length ? available.map(model => `<article><span class="card-icon">${icon("magic")}</span><div><strong>${escapeHtml(model.name)}</strong><small>${model.modes.includes("auto") ? "批量自动标注" : "交互辅助"} · ${model.capabilities.map(escapeHtml).join(" / ")}</small></div><span class="status-dot"></span></article>`).join("") : `<div class="empty-state"><p>没有可用的标注模型。请联系管理员配置。</p></div>`}</section></details>
    </div><div ${sectionPanelAttributes("ai", "history", tab)}><section class="surface ai-review-entry"><div><h2>跨任务选择与审核</h2><p>单次任务可从下方历史记录提交审核。需要合并多个任务的图片，或批量通过、退回时，可在这里选择。</p><span>待审核 ${state.assets.filter(asset => asset.review_status === "submitted").length} 张 · 已通过 ${state.assets.filter(asset => asset.review_status === "approved").length} 张</span></div><button class="btn primary" data-review-picker ${state.assets.length ? "" : "disabled"}>选择图片，批量提交与审核</button></section>
    <div class="section-head project-section-head"><div><h2>任务历史</h2><p>检查结果后，点击对应任务的“提交审核”，自动使用该任务已保存的图片范围。</p></div>${isAdmin() ? `<a class="btn ghost link-btn" href="#/models">管理标注模型</a>` : ""}</div>
    <section class="history-list" ${history.attributes}>${history.toolbar}<div class="task-table surface">${history.items.length ? history.items.map(projectJobCard).join("") : state.jobs.length ? history.empty : `<div class="empty-state borderless"><span class="card-icon">${icon("magic")}</span><strong>还没有 AI 标注任务</strong><p>先预览样本效果，确认提示词和置信度后再批量运行。</p></div>`}</div>${history.footer}</section></div>`);
  bindCommonProjectEvents();
  bindSectionTabs(app, "ai", { selected: tab, onChange: value => setSectionTab(state.session, state.project.id, "ai", value) });
  document.querySelector("#configure-ai-start")?.addEventListener("click", () => openAiModal());
  document.querySelector("#new-ai-job")?.addEventListener("click", () => openAiModal());
  bindJobActionButtons();
  history.bind(app, renderAiPage);
  if (focusedTab) document.getElementById(focusedTab)?.focus({ preventScroll: true });
}

function projectJobCard(job) {
  const progress = job.total ? Math.round(job.processed / job.total * 100) : 0;
  const model = state.models.find(item => item.id === job.model_id);
  const statusName = { queued:"排队中", running:"标注中", completed:"已完成", failed:"未完成", cancelled:"已取消" }[job.status] || job.status;
  const finished = ["completed", "failed", "cancelled"].includes(job.status);
  const created = new Date(job.created_at);
  const createdText = Number.isFinite(created.getTime()) ? created.toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "";
  const pendingService = job.status === "queued" && state.health?.annotation_workers?.status !== "online";
  const message = job.status === "completed" ? (job.created_count ? "请检查 AI 结果，确认无误后提交审核。" : "没有生成可用标注。请检查类别是否匹配，或换模型、降低置信度后用少量图片重试。") : job.status === "failed" ? "任务未完成，已保存的结果仍会保留。查看失败原因后重试；无法解决时联系管理员。" : job.status === "cancelled" ? "任务已停止，已完成的结果仍会保留。" : pendingService ? "任务已保存，等待标注服务。请联系管理员检查服务状态。" : job.status === "queued" ? "等待前面的任务完成，可以离开此页稍后查看。" : "正在生成标注，可以继续浏览其他图片。";
  return `<article class="task-row" data-job-id="${job.id}" data-history-record="${job.id}"><span class="task-type-icon">${icon("magic")}</span><div class="task-main"><div class="job-head"><strong>${escapeHtml(model?.name || "AI 标注")}</strong><span class="job-status ${job.status}">${statusName}</span></div><div class="task-meta"><span>任务 #${job.id}</span>${createdText ? `<time class="task-created" datetime="${escapeHtml(job.created_at)}">创建于 ${escapeHtml(createdText)}</time>` : ""}<span>已处理 ${job.processed} / ${job.total || "?"} 张图片</span><span>生成 ${job.created_count} 个标注</span></div><div class="progress-mini"><span style="width:${progress}%"></span></div><p class="job-next-step">${message}</p>${job.error || job.progress_message ? `<details class="technical-details"><summary>${job.error ? "查看失败原因" : "查看处理详情"}</summary><pre>${escapeHtml(job.error || job.progress_message)}</pre></details>` : ""}</div><strong class="task-progress">${progress}%</strong><div class="job-actions"><button class="btn primary" data-job-review="${job.id}" ${finished ? "" : "disabled"} title="${finished ? "预览本任务已处理图片的当前标注，再提交审核" : "请等待任务结束后再提交审核"}">${finished ? "提交审核" : "等待任务结束后提交"}</button>${["queued","running"].includes(job.status) ? `<button class="mini-action" data-cancel-job="${job.id}" ${state.jobActionIds.has(job.id) ? "disabled" : ""}>取消任务</button>` : ""}${["failed","cancelled"].includes(job.status) ? `<button class="mini-action" data-retry-job="${job.id}" ${state.jobActionIds.has(job.id) ? "disabled" : ""}>重试任务</button>` : ""}${job.status === "completed" || job.created_count ? `<a class="btn ghost link-btn" href="#/projects/${state.project.id}/annotate">打开校正页</a>` : ""}</div></article>`;
}

function versionCompactCard(version) {
  const split = version.splits || version.split_counts || {};
  return `<article class="version-compact"><span>V${version.version_number || version.number || version.id}</span>${versionSamplesMarkup(version, escapeHtml, true)}<div><strong>${escapeHtml(version.name || `数据版本 ${version.version_number || version.id}`)}</strong><small>${version.asset_count || 0} 张 · ${version.annotation_count || 0} 个对象 · 训练 ${splitAssetCount(split, "train")} / 验证 ${splitAssetCount(split, "valid")} / 测试 ${splitAssetCount(split, "test")}</small>${versionAugmentationMarkup(version, escapeHtml, true)}</div><span class="immutable-pill">已保存</span></article>`;
}

function renderVersionsPage() {
  const history = createHistoryNavigation({ projectId: state.project.id, kind: "versions", session: state.session, records: state.versions, pageSize: 6, escapeHtml,
    searchText: version => `V${version.version_number || version.number || version.id} ${version.name || ""} ${version.description || ""}` });
  if (!history) return;
  const total = state.assets.length;
  const approved = state.assets.filter(asset => asset.review_status === "approved").length;
  const allowed = canReview();
  const reason = !allowed ? "数据版本由审核员或管理员生成。你可以查看和下载已有版本。" : !total ? "请先导入图片，再完成标注与审核。" : !approved ? "还没有审核通过的图片。建议先检查标注并通过审核，再生成训练版本。" : `已有 ${approved} 张图片审核通过，可以生成训练版本。`;
  app.innerHTML = projectPageShell("versions", `
    <div class="page-head compact-head"><div><h1>数据版本</h1><p class="page-subtitle">把当前图片和标注保存成一份固定的数据，用于训练或下载。之后修改标注，不会影响已生成的版本。</p></div><button class="btn primary" id="create-version" ${allowed && total ? "" : "disabled"} title="${escapeHtml(reason)}">${icon("plus")}生成数据版本</button></div>
    <div class="version-principle"><span>${icon("layers")}</span><div><strong>下一步：${approved ? "保存审核结果，用于训练" : total ? "完成标注审核" : "导入第一批图片"}</strong><p>${escapeHtml(reason)}</p></div>${!approved ? `<a class="btn ghost link-btn" href="#/projects/${state.project.id}/${total ? "annotate" : "data"}">${total ? "查看标注" : "导入图片"}</a>` : ""}</div>
    <section class="history-list" ${history.attributes}>${history.toolbar}<div class="version-list">${history.items.length ? history.items.map(versionCard).join("") : state.versions.length ? history.empty : `<div class="empty-state"><span class="card-icon">${icon("layers")}</span><strong>还没有数据版本</strong><p>${allowed && total ? "默认只保存审核通过的图片。生成后，可以直接去训练或下载数据。" : escapeHtml(reason)}</p>${allowed && total ? `<button class="btn primary" id="create-first-version">生成第一个版本</button>` : ""}</div>`}</div>${history.footer}</section>`);
  bindCommonProjectEvents();
  document.querySelector("#create-version")?.addEventListener("click", openVersionModal);
  document.querySelector("#create-first-version")?.addEventListener("click", openVersionModal);
  document.querySelectorAll("[data-version-view]").forEach(button => button.onclick = () => { const version = state.versions.find(item => item.id === Number(button.dataset.versionView)); if (version) openVersionImages(version, { state, api, openModal, closeModal, escapeHtml }); });
  document.querySelectorAll("[data-version-export]").forEach(button => button.onclick = () => openVersionExportModal(Number(button.dataset.versionExport)));
  document.querySelectorAll("[data-version-train]").forEach(link => link.onclick = () => { state.selectedTrainingVersion = Number(link.dataset.versionTrain); });
  history.bind(app, renderVersionsPage);
}

function versionCard(version) {
  const split = version.splits || version.split_counts || {};
  const number = version.version_number || version.number || version.id;
  const hash = version.content_hash || version.sha256 || "";
  return `<article class="version-card" data-history-record="${version.id}"><div class="version-number">V${number}</div><div class="version-main"><div class="version-title"><div><h2>${escapeHtml(version.name || `数据版本 ${number}`)}</h2><p>${escapeHtml(version.description || (version.approved_only ? "仅包含审核通过的图片" : "包含未审核的图片，训练前请确认数据质量"))}</p></div><span class="immutable-pill">${icon("check")}已保存</span></div>${versionSamplesMarkup(version, escapeHtml)}<div class="version-stats"><div><strong>${version.asset_count || 0}</strong><span>图片</span></div><div><strong>${version.annotation_count || 0}</strong><span>标注</span></div><div><strong>${splitAssetCount(split, "train")}</strong><span>用于训练</span></div><div><strong>${splitAssetCount(split, "valid")}</strong><span>用于验证</span></div><div><strong>${splitAssetCount(split, "test")}</strong><span>用于测试</span></div></div><div class="split-bar"><span class="train" style="width:${splitPercent(split, "train")}%"></span><span class="valid" style="width:${splitPercent(split, "valid")}%"></span><span class="test" style="width:${splitPercent(split, "test")}%"></span></div>${versionAugmentationMarkup(version, escapeHtml)}<div class="version-meta"><span>${new Date(version.created_at).toLocaleString()}</span><button class="btn ghost" data-version-view="${version.id}">${icon("image")}查看版本图片</button><button class="btn ghost" data-version-export="${version.id}">${icon("download")}下载数据</button>${canReview() ? `<a class="btn primary link-btn" data-version-train="${version.id}" href="#/projects/${state.project.id}/train">用此版本训练</a>` : ""}</div>${hash ? `<details class="pipeline-details"><summary>版本技术信息</summary><p>创建人：${escapeHtml(version.created_by || "系统")}</p><p>内容校验值用于确认数据一致，日常操作无需填写。</p><code>${escapeHtml(hash)}</code></details>` : ""}</div></article>`;
}

function splitAssetCount(split, name) {
  const value = split?.[name];
  return Number(typeof value === "object" ? value?.asset_count : value || 0);
}

function splitPercent(split, name) {
  const total = splitAssetCount(split, "train") + splitAssetCount(split, "valid") + splitAssetCount(split, "test");
  return total ? splitAssetCount(split, name) / total * 100 : 0;
}

function openVersionModal() {
  if (!canReview()) return notify("请由审核员或管理员生成数据版本", "error");
  const projectId = state.project.id, section = state.currentSection;
  openDatasetGeneration({ state, api, jsonOptions, openModal, closeModal, escapeHtml, notify, onCreated: version => {
      state.versions.unshift(version); notify(`数据版本已生成，实际保存 ${version.asset_count || 0} 张图片。可以查看分组、去训练或下载数据。`);
      resetHistoryNavigation(projectId, "versions");
      if (section === "versions") {
        renderVersionsPage();
        const card = app.querySelector(`[data-history-kind="versions"] [data-history-record="${version.id}"]`);
        card?.scrollIntoView({ block: "start", behavior: "auto" });
        const heading = card?.querySelector("h2");
        if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
      } else renderProjectSection(section);
    } });
}

function openVersionExportModal(versionId) {
  const version = state.versions.find(item => item.id === versionId);
  openModal({ title: `下载 ${version?.name || `V${versionId}`}`, subtitle: `下载 ${version?.asset_count || 0} 张图片及其标注，内容来自这个已保存的版本。`, body: `<div class="field"><label>数据格式</label>${exportFormatMarkup("version-format", version?.stats?.task_type)}</div><p class="hint">在本平台训练无需下载。需要交给其他工具时，按对方要求选择格式；不确定时可保持默认。</p><div class="export-note">${icon("check")}下载文件是 ZIP 压缩包，包含图片、标注、标签名称以及训练 / 验证 / 测试划分。</div>`, submitText: "下载 ZIP 压缩包", onSubmit: async () => { const format = document.querySelector('input[name="version-format"]:checked').value; await downloadArtifact(`/api/versions/${versionId}/export/${format}`, `dataset-${versionId}.zip`); closeModal(); } });
}

async function downloadArtifact(url, fallbackName = "download") {
  const progressNotice = notify("正在准备下载，文件较大时请稍候。", "info");
  try {
  let response;
  try { response = await fetch(url, { credentials: "same-origin" }); }
  catch (cause) { const error = new Error("下载连接中断，请检查网络后重新下载。"); error.technicalMessage = cause.message; throw error; }
  if (!response.ok) {
    let detail = "";
    try { const body = await response.json(); detail = typeof body.detail === "string" ? body.detail : Array.isArray(body.detail) ? body.detail.map(item => item.msg || "参数不正确").join("；") : ""; } catch (_) { /* The status still explains non-JSON failures. */ }
    const fallback = response.status === 401 ? "登录已过期，请重新登录后下载。" : response.status === 403 ? "当前账号无权下载此文件，请联系管理员。" : "暂时无法下载，请刷新后重试；仍失败时请联系管理员。";
    const error = new Error(detail && /[\u4e00-\u9fff]/.test(detail) && detail.length < 240 ? detail : fallback);
    if (detail && error.message !== detail) error.technicalMessage = detail;
    throw error;
  }
  const disposition = response.headers.get("content-disposition") || "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  let filename = disposition.match(/filename="([^"]+)"/i)?.[1] || disposition.match(/filename=([^;]+)/i)?.[1]?.trim() || fallbackName;
  if (encoded) { try { filename = decodeURIComponent(encoded); } catch (_) { /* Use the plain fallback filename. */ } }
  let blob;
  try { blob = await response.blob(); }
  catch (cause) { const error = new Error("文件尚未完整接收，请检查网络后重新下载。"); error.technicalMessage = cause.message; throw error; }
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl; anchor.download = filename.replace(/[\\/]/g, "_");
  document.body.append(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
  notify("已交给浏览器下载，可在浏览器的下载列表查看。");
  } finally { progressNotice.remove(); }
}

import { trainingChartMarkup, evaluationMetric } from "./training-charts.js?v=0.21.0";
import { trainingListURL, trainingListMarkup, trainingPreferenceKey } from "./training-navigation.js?v=0.21.0";
import { conversionFormatName, conversionFormatDescription, conversionFormats, conversionFormatCards, conversionSettingsEntries, conversionSettingsSummary, mountConversionForm } from "./conversion-ui.js?v=0.21.0";

const trainingChartViews = new Map();
const trainingDetailViews = new Map();
let pipelineRefreshRequest = null;
let pipelineRefreshSerial = 0;
let trainingEnvironmentRequest = null;
let pipelinePollingKey = "";
let pipelinePollNow = null;
let trainingSearchTimer = null;

async function pipelineApi(path, timeoutMs = 12000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try { return await api(path, { cache: "no-store", signal: controller.signal }); }
  finally { clearTimeout(timeout); }
}

function monitoredTrainingRun() {
  return state.selectedTrainingRun?.id === state.selectedTrainingId ? state.selectedTrainingRun : null;
}

function trainingRunById(id) { return monitoredTrainingRun()?.id === id ? state.selectedTrainingRun : state.training.runs.find(run => run.id === id); }

function trainingContextGuard() {
  const projectId = state.project?.id, epoch = state.selectionEpoch, session = state.session;
  return () => state.project?.id === projectId && state.selectionEpoch === epoch && state.session === session && state.currentSection === "train";
}

function restoreTrainingNavigation(projectId) {
  clearTimeout(trainingSearchTimer); trainingSearchTimer = null;
  state.pipelineUpdatedAt = 0; state.pipelineConnectionLost = false; state.pipelineManualRefreshing = false; state.trainingEnvironmentLoading = false;
  let saved = {};
  try { saved = JSON.parse(sessionStorage.getItem(trainingPreferenceKey(state.session, projectId)) || "{}"); } catch (_) { /* Preferences are optional. */ }
  if (!saved || typeof saved !== "object" || Array.isArray(saved)) saved = {};
  const statuses = ["all", "active", "queued", "running", "completed", "failed", "cancelled"];
  state.trainingFilters = { query: String(saved.filters?.query || "").slice(0, 200), status: statuses.includes(saved.filters?.status) ? saved.filters.status : "all", task: ["all", "detect", "segment", "pose", "classify"].includes(saved.filters?.task) ? saved.filters.task : "all" };
  state.trainingPage.page = Number.isSafeInteger(saved.page) && saved.page > 0 && saved.page <= 2147483647 ? saved.page : 1;
  state.trainingPage.loading = false; state.trainingPage.error = "";
  const urlId = new URLSearchParams(location.hash.split("?")[1] || "").get("run");
  const id = Number(urlId || saved.runId);
  state.selectedTrainingId = Number.isSafeInteger(id) && id > 0 ? id : null;
}

function rememberTrainingNavigation() {
  if (!state.project) return;
  try { sessionStorage.setItem(trainingPreferenceKey(state.session, state.project.id), JSON.stringify({ runId: state.selectedTrainingId, page: state.trainingPage.page, filters: state.trainingFilters })); } catch (_) { /* Private browsing may disallow persistence. */ }
  const url = `#/projects/${state.project.id}/train${state.selectedTrainingId ? `?run=${state.selectedTrainingId}` : ""}`;
  if (state.currentSection === "train" && location.hash !== url) history.replaceState(null, "", url);
}

function applyTrainingListResponse(response) {
  state.training = { ...response, availability: response.availability ?? state.training?.availability ?? {} };
  state.training.pagination ||= { page: 1, page_size: 10, total: response.runs?.length || 0, total_pages: 1 };
  state.training.total ??= state.training.pagination.total;
  state.trainingPage.page = state.training.pagination.page;
  state.trainingPage.loading = false; state.trainingPage.error = "";
}

async function loadTrainingList({ page = state.trainingPage.page, locateId = null } = {}) {
  if (!state.project || state.currentSection !== "train") return;
  clearTimeout(trainingSearchTimer); trainingSearchTimer = null;
  const projectId = state.project?.id, epoch = state.selectionEpoch;
  const request = ++state.trainingPage.request;
  const current = () => state.project?.id === projectId && state.selectionEpoch === epoch && state.trainingPage.request === request && state.currentSection === "train";
  state.trainingPage.loading = true; state.trainingPage.error = "";
  state.trainingPage.page = page;
  rememberTrainingNavigation(); renderTrainingPage();
  try {
    const response = await pipelineApi(trainingListURL(projectId, page, state.trainingFilters, locateId) + '&include_availability=false');
    if (!current()) return;
    applyTrainingListResponse(response); rememberTrainingNavigation();
    if (locateId && response.locate_found === false) notify("当前训练记录已不在本项目列表中，请刷新右侧详情。", "warning");
    renderTrainingPage(); startPipelinePolling();
    if (locateId && response.locate_found) document.querySelector(`[data-select-training="${locateId}"]`)?.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "instant" });
  } catch (error) {
    if (!current()) return;
    state.trainingPage.loading = false; state.trainingPage.error = `记录列表加载失败：${error.message} 当前训练详情仍可查看。`;
    renderTrainingPage();
  }
}

async function selectTrainingMonitor(id, scroll = false, { render = true } = {}) {
  if (!state.project || state.currentSection !== "train") return;
  const projectId = state.project?.id, epoch = state.selectionEpoch;
  const request = ++state.trainingSelectionRequest;
  const current = () => state.project?.id === projectId && state.selectionEpoch === epoch && state.trainingSelectionRequest === request && state.currentSection === "train";
  state.selectedTrainingId = id; state.selectedTrainingRun = null;
  state.trainingSelectionError = ""; state.trainingSelectionLoading = Boolean(id);
  if (window.innerWidth < 900 && id) state.trainingListOpen = false;
  rememberTrainingNavigation();
  if (render) renderTrainingPage();
  if (!id) return;
  try {
    const run = await pipelineApi(`/api/training-runs/${id}`);
    if (!current()) return;
    if (run.project_id !== projectId) throw new Error("这条训练记录不属于当前项目，请从左侧选择本项目的记录。");
    state.selectedTrainingRun = run; state.trainingSelectionLoading = false;
    state.pipelineUpdatedAt = Date.now();
    void loadRunConversions(id, { render: true });
  } catch (error) {
    if (!current()) return;
    state.trainingSelectionLoading = false; state.trainingSelectionError = `无法打开训练 ${id}：${error.message} 请重试，或从左侧重新选择。`;
  }
  if (current() && render) {
    renderTrainingPage(); startPipelinePolling();
    if (scroll) { document.querySelector("#training-selected-heading")?.focus({ preventScroll: true }); document.querySelector(".training-selected-detail")?.scrollIntoView({ behavior: "instant", block: "start" }); }
  }
}

function trainingMonitorMarkup(run) {
  if (!run) return "";
  return `<section class="training-monitor surface" id="training-monitor" aria-labelledby="training-monitor-heading"><div class="training-monitor-head"><div><h2 id="training-monitor-heading">训练曲线与进度</h2><p>损失（Loss）反映预测误差，通常越低越好。结合验证表现判断效果。</p></div></div>${trainingChartMarkup(run, state.trainingMetricsHistory[run.id], trainingChartViews.get(run.id))}</section>`;
}

const TRAINING_TASK_META = {
  detect: { name: "目标检测", short: "检测", icon: "box", description: "矩形框定位与分类", schema: "矩形框 / 多边形可转外接框" },
  segment: { name: "实例分割", short: "分割", icon: "polygon", description: "逐实例像素级轮廓", schema: "多边形；矩形框可降级转轮廓" },
  classify: { name: "图像分类", short: "分类", icon: "image", description: "整张图片类别预测", schema: "需要图像级分类标签" },
  pose: { name: "姿态估计", short: "姿态", icon: "target", description: "目标关键点定位", schema: "需要关键点标注" },
  obb: { name: "旋转框检测", short: "OBB", icon: "box", description: "带方向的目标检测", schema: "需要旋转框标注" },
};

function normalizeTrainingCatalog(raw, availability = {}) {
  const source = raw && typeof raw === "object" ? raw : {};
  const sourceTasks = source.tasks && typeof source.tasks === "object" ? source.tasks : availability?.tasks || {};
  const tasks = {};
  Object.entries(sourceTasks).forEach(([id, value]) => {
    const config = typeof value === "boolean" ? { available: value } : value || {};
    tasks[id] = { id, ...TRAINING_TASK_META[id], ...config, available: config.available !== false };
  });
  if (!Object.keys(tasks).length) {
    ["detect", "segment"].forEach(id => { tasks[id] = { id, ...TRAINING_TASK_META[id], available: Boolean(availability?.available), reason: availability?.reason || "" }; });
  }
  const suppliedModels = Array.isArray(source.models) ? source.models : [];
  const models = suppliedModels.map((model, index) => normalizeTrainingModel(model, index));
  if (!models.length) {
    Object.entries(availability?.base_models || {}).forEach(([task, model], index) => {
      const path = model?.path || "";
      const filename = path.split("/").pop() || (task === "segment" ? "yolo26n-seg.pt" : "yolo26n.pt");
      models.push(normalizeTrainingModel({ id: filename, name: filename.replace(/\.pt$/i, ""), model_arch: filename, path, task, family: "Ultralytics YOLO", available: model?.exists !== false, reason: model?.exists === false ? `服务器未找到 ${filename}` : "", size_bytes: model?.size_bytes, recommended: true }, index));
    });
  }
  return {
    ...source,
    tasks,
    models,
    features: source.features || {},
    conversion_formats: source.conversion_formats || availability?.conversion_formats || {},
  };
}

function normalizeTrainingModel(model, index = 0) {
  const path = model.path || "";
  const architecture = model.model_arch || model.filename || path.split("/").pop() || model.id || `model-${index + 1}.pt`;
  const task = Array.isArray(model.tasks) ? model.tasks[0] : model.task || "detect";
  return {
    ...model,
    id: String(model.id || architecture),
    model_arch: architecture,
    name: model.name || architecture.replace(/\.pt$/i, ""),
    task,
    tasks: Array.isArray(model.tasks) ? model.tasks : [task],
    family: model.family || "YOLO",
    available: model.available !== false && model.exists !== false,
    defaults: model.defaults || {},
  };
}

function normalizeTrainingResources(raw, catalog = {}, availability = {}) {
  const source = raw?.resources || raw || catalog?.resources || {};
  const devices = Array.isArray(source.devices) ? [...source.devices] : [];
  if (source.cpu && !devices.some(device => device.id === "cpu")) devices.push({ id: "cpu", type: "cpu", name: source.cpu.name || "CPU", available: source.cpu.available !== false, ...source.cpu });
  if (!devices.length) devices.push({ id: "cpu", type: "cpu", name: "CPU", available: true });
  return { ...source, default_device: source.default_device || (availability?.cuda_available ? "auto" : "cpu"), devices };
}

function trainingTaskInfo(task) { return { id: task, ...(TRAINING_TASK_META[task] || { name: task, short: task, icon: "cpu", description: "自定义视觉任务", schema: "请确认数据格式兼容" }), ...(state.trainingCatalog?.tasks?.[task] || {}) }; }
function trainingModelSupports(model, task) { return (model.tasks || [model.task]).includes(task); }
function taskModels(task, availableOnly = false) { return (state.trainingCatalog?.models || []).filter(model => trainingModelSupports(model, task) && (!availableOnly || model.available)); }
function taskAvailable(task) { const config = trainingTaskInfo(task); return config.available !== false && taskModels(task, true).length > 0; }
function versionDefaultTask(version) { return version?.stats?.task_type || "detect"; }
function trainingDataIssue(version) {
  const split = version?.splits || version?.stats?.splits || {};
  if (version?.annotation_count === 0) return "这个版本还没有标注，暂时不能训练。请完成标注后重新生成数据版本。";
  if (split.valid !== undefined && !splitAssetCount(split, "valid")) return "这个版本没有验证图片，暂时不能训练。请补充已审核图片，或提高验证比例后重新生成数据版本。";
  if (split.train?.annotation_count === 0 || split.valid?.annotation_count === 0) return "训练或验证图片没有有效标注。请补充标注，或调整图片划分后重新生成数据版本。";
  return "";
}
function countConversions(runs) { return Number.isFinite(Number(state.conversionCounts?.completed)) ? Number(state.conversionCounts.completed) : runs.reduce((sum, run) => sum + (run.conversions?.filter(item => item.status === "completed").length || 0), 0); }

function renderTrainingPage() {
  if (deferRenderForNavigation("train") || document.querySelector('#training-search[data-composing="true"]')) return;
  const viewSnapshot = captureTrainingViewState();
  const monitored = monitoredTrainingRun();
  const availability = state.training?.availability || { available: false, reason: "训练环境不可用" };
  const models = state.trainingCatalog?.models || [];
  const tasks = Object.keys(state.trainingCatalog?.tasks || {});
  const worker = state.trainingResources?.worker || {};
  const runtimeHealthy = Boolean(availability.available && models.some(model => model.available) && (worker.embedded_enabled || Number(worker.online_count || 0) > 0));
  const allowed = canReview(), createReady = allowed && runtimeHealthy && state.versions.length;
  const blockReason = !allowed ? "训练与模型转换由审核员或管理员操作，你可以查看记录和下载已有模型。" : !state.versions.length ? "请先生成数据版本，再开始训练。" : !runtimeHealthy ? state.trainingEnvironmentLoading ? "正在检查训练环境，已有训练记录与进度会照常更新。" : "训练服务暂不可用，已有训练记录仍可查看。请联系管理员检查服务后刷新。" : "数据与训练服务已就绪，首次训练可使用推荐模型和默认参数。";
  let detail = state.trainingSelectionLoading ? '<div class="training-detail-empty surface" role="status"><span class="spinner"></span><strong>正在读取所选训练…</strong><p>请稍候，操作按钮将在记录确认后显示。</p></div>' : state.trainingSelectionError ? `<div class="training-detail-empty surface" role="alert"><strong>暂时无法显示这次训练</strong><p>${escapeHtml(state.trainingSelectionError)}</p><button class="btn ghost" id="retry-selected-training">重新读取</button></div>` : monitored ? trainingRunCard(monitored) : `<div class="training-detail-empty surface"><span class="card-icon">${icon("cpu")}</span><strong>${state.training.total ? "选择一条训练记录" : "还没有训练记录"}</strong><p>${state.training.total ? "从左侧选择后，这里会显示曲线、参数及模型文件。" : escapeHtml(blockReason)}</p>${createReady ? '<button class="btn primary" id="empty-new-training">开始第一次训练</button>' : !state.versions.length ? `<a class="btn ghost link-btn" href="#/projects/${state.project.id}/versions">去生成数据版本</a>` : ""}</div>`;
  app.innerHTML = projectPageShell("train", `<div class="page-head compact-head"><div><h1>训练与模型导出</h1><p class="page-subtitle">每次训练独立保存。选择左侧记录，查看曲线、参数和模型文件。</p></div><div class="training-page-actions"><button class="btn ghost" id="refresh-training" ${state.pipelineManualRefreshing ? "disabled" : ""}>${state.pipelineManualRefreshing ? '<span class="spinner"></span>刷新中' : `${icon("refresh")}刷新`}</button><button class="btn primary" id="new-training" ${createReady ? "" : "disabled"} title="${escapeHtml(blockReason)}">${icon("play")}开始新训练</button></div></div>${trainingSyncMarkup()}${!createReady ? `<p class="inline-notice training-start-notice" role="status">${escapeHtml(blockReason)}</p>` : ""}<div class="training-workbench">${trainingListMarkup(state, { escapeHtml, trainingStatusLabel, trainingTaskInfo, formatDateTime, icon })}<section class="training-selected-detail" data-selected-run="${monitored?.id || ""}" aria-label="所选训练详情">${detail}</section></div><details class="training-runtime-details surface pipeline-details" data-pipeline-detail="catalog"><summary>${runtimeHealthy ? "训练环境与可用模型" : "训练环境暂不可用 · 查看原因"}</summary><p>${escapeHtml(blockReason)}</p>${!runtimeHealthy ? `<p>${escapeHtml(availability.reason || "没有在线的训练执行服务或可用模型。")}</p>` : ""}<div class="task-capability-grid">${tasks.map(trainingTaskCapabilityCard).join("")}</div><div class="catalog-model-strip">${models.map(trainingCatalogModelCard).join("")}</div>${trainingLicenseMarkup()}</details>`);
  bindCommonProjectEvents(); bindTrainingPageEvents(); restoreTrainingViewState(viewSnapshot);
  if (monitored && !state.trainingMetricsHistory[monitored.id]?.attemptedAt) void loadTrainingMetrics(monitored.id, { render: true });
}

function captureTrainingViewState() {
  const pageNode = document.querySelector(".project-page");
  const active = document.activeElement;
  const focus = app.contains(active) && (active?.id || active?.dataset.selectTraining) ? { id: active.id, runId: active.dataset.selectTraining, start: active.selectionStart, end: active.selectionEnd } : null;
  const logs = {};
  document.querySelectorAll("[data-run-log]").forEach(node => { logs[node.dataset.runLog] = { top: node.scrollTop, atEnd: node.scrollHeight - node.scrollTop - node.clientHeight < 18 }; });
  const details = [...document.querySelectorAll("[data-pipeline-detail][open]")].map(node => node.dataset.pipelineDetail);
  const values = document.querySelector(".training-values-scroll");
  const displayedId = Number(document.querySelector("[data-selected-run]")?.dataset.selectedRun) || null;
  if (displayedId) trainingDetailViews.set(displayedId, { details, valuesTop: values?.scrollTop || 0 });
  return { pageTop: pageNode?.scrollTop || 0, windowTop: window.scrollY, focus, logs, details, valuesTop: values?.scrollTop || 0, monitoredId: displayedId, sidebarTop: document.querySelector(".training-records")?.scrollTop || 0 };
}

function restoreTrainingViewState(snapshot) {
  if (!snapshot) return;
  const pageNode = document.querySelector(".project-page");
  if (pageNode) pageNode.scrollTop = snapshot.pageTop;
  window.scrollTo({ top: snapshot.windowTop || 0, behavior: "instant" });
  const savedDetail = snapshot.monitoredId === state.selectedTrainingId ? snapshot : trainingDetailViews.get(state.selectedTrainingId) || {};
  ([...(savedDetail.details || []), ...(snapshot.details || []).filter(key => key === "catalog")]).forEach(key => { const node = document.querySelector(`[data-pipeline-detail="${CSS.escape(key)}"]`); if (node) node.open = true; });
  Object.entries(snapshot.logs || {}).forEach(([id, saved]) => {
    const node = document.querySelector(`[data-run-log="${CSS.escape(id)}"]`);
    if (node) node.scrollTop = saved.atEnd ? node.scrollHeight : saved.top;
  });
  if (savedDetail) {
    const values = document.querySelector(".training-values-scroll");
    if (values) values.scrollTop = savedDetail.valuesTop || 0;
  }
  const sidebar = document.querySelector(".training-records"); if (sidebar) sidebar.scrollTop = snapshot.sidebarTop;
  if (snapshot.focus) {
    const node = snapshot.focus.id ? document.getElementById(snapshot.focus.id) : document.querySelector(`[data-select-training="${CSS.escape(snapshot.focus.runId)}"]`);
    if (node) {
      node.focus({ preventScroll: true });
      if (typeof node.setSelectionRange === "function" && Number.isInteger(snapshot.focus.start)) node.setSelectionRange(snapshot.focus.start, snapshot.focus.end);
    }
  }
}

function trainingTaskCapabilityCard(task) {
  const info = trainingTaskInfo(task);
  const models = taskModels(task);
  const available = taskAvailable(task);
  return `<article class="task-capability ${available ? "available" : "unavailable"}"><span>${icon(info.icon)}</span><div><strong>${escapeHtml(info.name)}</strong><small>${escapeHtml(info.description || info.schema || "视觉训练任务")}</small></div><em>${available ? `${models.filter(model => model.available).length} 个模型` : escapeHtml(info.reason || "当前数据结构不兼容")}</em></article>`;
}

function trainingCatalogModelCard(model) {
  const task = trainingTaskInfo(model.task);
  const size = model.parameters || model.parameter_count ? `${formatCompactNumber(model.parameters || model.parameter_count)} 参数` : model.size_bytes ? formatBytes(model.size_bytes) : "本地权重";
  return `<article class="catalog-model ${model.available ? "" : "unavailable"}" title="${escapeHtml(model.reason || model.description || "")}"><div><strong>${escapeHtml(model.name)}</strong>${model.recommended ? `<span>推荐</span>` : ""}</div><p>${escapeHtml(model.family)} · ${escapeHtml(task.short)} · ${escapeHtml(size)}</p><small>${model.available ? "可训练" : escapeHtml(model.reason || "当前不可用")}</small></article>`;
}

function trainingLicenseMarkup() {
  const licensing = state.trainingCatalog?.licensing;
  const notice = typeof licensing === "string" ? licensing : licensing?.notice || licensing?.summary || "Ultralytics 权重及其训练产物用于生产环境前，请由部署方确认适用的 AGPL-3.0 或 Enterprise 许可。";
  return `<div class="training-license-note">${icon("warning")}<span><strong>生产许可提示</strong>${escapeHtml(notice)}</span></div>`;
}

function bindTrainingPageEvents() {
  const selected = monitoredTrainingRun();
  bindSectionTabs(app, "train", { selected: getSectionTab(state.session, state.project.id, "train", "overview"), onChange: tab => {
    setSectionTab(state.session, state.project.id, "train", tab);
    if (tab === "details" && selected && app.querySelector(`[data-training-log-detail="${selected.id}"][open]`)) void loadTrainingLogs(selected.id, { append: true, render: true });
  } });
  const view = selected ? trainingChartViews.get(selected.id) || {} : {};
  if (selected) trainingChartViews.set(selected.id, view);
  document.querySelectorAll("[data-select-training]").forEach(button => button.onclick = () => selectTrainingMonitor(Number(button.dataset.selectTraining), window.innerWidth < 900));
  document.querySelector("#retry-selected-training")?.addEventListener("click", () => selectTrainingMonitor(state.selectedTrainingId));
  document.querySelector("#toggle-training-list")?.addEventListener("click", () => { state.trainingListOpen = !state.trainingListOpen; renderTrainingPage(); });
  document.querySelector("#training-prev-page")?.addEventListener("click", () => loadTrainingList({ page: Math.max(1, state.trainingPage.page - 1) }));
  document.querySelector("#training-next-page")?.addEventListener("click", () => loadTrainingList({ page: state.trainingPage.page + 1 }));
  document.querySelector("#retry-training-list")?.addEventListener("click", () => loadTrainingList());
  document.querySelector("#clear-training-filters")?.addEventListener("click", () => { state.trainingFilters = { status: "all", task: "all", query: "" }; void loadTrainingList({ page: 1 }); });
  document.querySelector("#locate-selected-training")?.addEventListener("click", () => { state.trainingFilters = { status: "all", task: "all", query: "" }; void loadTrainingList({ page: 1, locateId: state.selectedTrainingId }); });
  document.querySelector("#training-curve")?.addEventListener("change", event => { view.metric = event.target.value; renderTrainingPage(); });
  document.querySelector("#refresh-training-metrics")?.addEventListener("click", () => loadTrainingMetrics(selected.id, { render: true, force: true }));
  document.querySelector("#all-training-values")?.addEventListener("click", () => { view.allRows = true; renderTrainingPage(); });
  document.querySelector(".training-values")?.addEventListener("toggle", event => { if (event.target.isConnected) view.tableOpen = event.target.open; });
  document.querySelectorAll("[data-monitor-training]").forEach(button => button.onclick = () => selectTrainingMonitor(Number(button.dataset.monitorTraining), true));
  document.querySelectorAll("[data-training-log-detail]").forEach(detail => detail.addEventListener("toggle", () => { if (detail.open && detail.isConnected && !detail.closest("[hidden]")) void loadTrainingLogs(Number(detail.dataset.trainingLogDetail), { append: true, render: true }); }));
  document.querySelector("#new-training")?.addEventListener("click", () => openTrainingModal());
  document.querySelector("#empty-new-training")?.addEventListener("click", () => openTrainingModal());
  document.querySelector("#refresh-training")?.addEventListener("click", refreshTrainingWorkspace);
  const query = document.querySelector("#training-search");
  if (query) {
    const projectId = state.project.id, epoch = state.selectionEpoch;
    const scheduleSearch = () => {
      state.trainingFilters.query = query.value; state.trainingPage.request += 1;
      clearTimeout(trainingSearchTimer); trainingSearchTimer = null;
      rememberTrainingNavigation();
      if (query.dataset.composing === "true") return;
      trainingSearchTimer = setTimeout(() => {
        trainingSearchTimer = null;
        if (state.project?.id === projectId && state.selectionEpoch === epoch && state.currentSection === "train") void loadTrainingList({ page: 1 });
      }, 350);
    };
    query.addEventListener("compositionstart", () => { query.dataset.composing = "true"; clearTimeout(trainingSearchTimer); trainingSearchTimer = null; state.trainingPage.request += 1; });
    query.addEventListener("compositionend", () => { query.dataset.composing = "false"; scheduleSearch(); });
    query.oninput = scheduleSearch;
  }
  const taskFilter = document.querySelector("#training-task-filter");
  if (taskFilter) taskFilter.onchange = () => { state.trainingFilters.task = taskFilter.value; applyTrainingFilters(); };
  const statusFilter = document.querySelector("#training-status-filter");
  if (statusFilter) statusFilter.onchange = () => { state.trainingFilters.status = statusFilter.value; applyTrainingFilters(); };
  document.querySelectorAll("[data-toggle-training]").forEach(button => button.onclick = () => toggleTrainingDetails(Number(button.dataset.toggleTraining)));
  document.querySelectorAll("[data-cancel-training]").forEach(button => button.onclick = () => confirmCancelTraining(Number(button.dataset.cancelTraining)));
  document.querySelectorAll("[data-retry-training]").forEach(button => button.onclick = () => confirmRetryTraining(Number(button.dataset.retryTraining)));
  document.querySelectorAll("[data-clone-training]").forEach(button => button.onclick = () => openTrainingModal(trainingRunById(Number(button.dataset.cloneTraining))));
  document.querySelectorAll("[data-refresh-run-conversions]").forEach(button => button.onclick = () => loadRunConversions(Number(button.dataset.refreshRunConversions), { force: true, render: true }));
  document.querySelectorAll("[data-refresh-logs]").forEach(button => button.onclick = () => loadTrainingLogs(Number(button.dataset.refreshLogs), { append: true, render: true, force: true }));
  document.querySelectorAll("[data-download-deployment]").forEach(button => button.onclick = () => downloadArtifact(`/api/training-runs/${button.dataset.downloadDeployment}/deployment`, `cvsprout-model-${button.dataset.downloadDeployment}.zip`).catch(error => notify(error.message, "error")));
  document.querySelectorAll("[data-download-training]").forEach(button => button.onclick = async () => { button.disabled = true; try { await downloadArtifact(`/api/training-runs/${button.dataset.downloadTraining}/artifact`, `model-${button.dataset.downloadTraining}.pt`); } catch (error) { notify(error.message, "error"); } finally { button.disabled = false; } });
  document.querySelectorAll("[data-convert-training]").forEach(button => button.onclick = () => openConversionModal(Number(button.dataset.convertTraining)));
  document.querySelectorAll("[data-download-conversion]").forEach(button => button.onclick = async () => { button.disabled = true; try { await downloadArtifact(`/api/conversions/${button.dataset.downloadConversion}/artifact`, `converted-model-${button.dataset.downloadConversion}`); } catch (error) { notify(error.message, "error"); } finally { button.disabled = false; } });
  document.querySelectorAll("[data-download-conversion-manifest]").forEach(button => button.onclick = async () => { button.disabled = true; try { await downloadArtifact(`/api/conversions/${button.dataset.downloadConversionManifest}/manifest`, `conversion-${button.dataset.downloadConversionManifest}-deployment.json`); } catch (error) { notify(error.message, "error"); } finally { button.disabled = false; } });
  document.querySelectorAll("[data-conversion-logs]").forEach(button => button.onclick = () => openConversionLogModal(Number(button.dataset.conversionLogs)));
  document.querySelectorAll("[data-cancel-conversion]").forEach(button => button.onclick = () => confirmCancelConversion(Number(button.dataset.cancelConversion)));
  document.querySelectorAll("[data-retry-conversion]").forEach(button => button.onclick = () => retryConversion(Number(button.dataset.retryConversion), button));
}

function applyTrainingFilters() {
  void loadTrainingList({ page: 1 });
}

function trainingRunCard(run) {
  const tab = getSectionTab(state.session, state.project.id, "train", "overview");
  const task = trainingTaskInfo(run.task || "detect");
  const displayStatus = run.cancel_requested && ["queued", "running"].includes(run.status) ? "cancelling" : run.status;
  const version = state.versions.find(item => item.id === run.version_id || item.id === run.dataset_version_id);
  const modelName = run.model_name || run.model_arch || String(run.settings?.base_model || "模型未记录").split("/").pop();
  const active = ["queued", "running", "cancelling"].includes(displayStatus);
  const retryable = ["failed", "cancelled"].includes(run.status) && (run.lineage ? run.lineage.retryable : Number(run.attempt || 0) < Number(run.max_retries ?? 2));
  const manage = canReview();
  return `<div class="training-selected-header surface"><div class="training-selected-title"><span class="task-type-icon">${icon(task.icon)}</span><div><h2 id="training-selected-heading" tabindex="-1">${escapeHtml(run.name || `${modelName} 训练`)}</h2><p>${escapeHtml(task.name)} · ${escapeHtml(modelName)} · ${escapeHtml(version?.name || `数据版本 ${run.version_id}`)}</p><p><time datetime="${escapeHtml(run.created_at || "")}">${escapeHtml(formatDateTime(run.created_at))}</time> · ${formatRunTime(run)}</p></div><span class="job-status ${escapeHtml(displayStatus)}">${trainingStatusLabel(displayStatus)}</span></div><div class="training-selected-actions" aria-label="当前训练的操作">${manage && active && !run.cancel_requested ? `<button class="btn danger" data-cancel-training="${run.id}">取消这次训练</button>` : ""}${manage && retryable ? `<button class="btn ghost" data-retry-training="${run.id}">${icon("refresh")}按原配置重试</button>` : ""}${manage && !active ? `<button class="btn ghost" data-clone-training="${run.id}">${icon("branch")}修改配置再训练</button>` : ""}${run.status === "completed" && run.artifact?.available ? `<button class="btn primary" data-download-training="${run.id}">${icon("download")}下载模型（${formatBytes(run.artifact.size_bytes)}）</button><button class="btn ghost" data-download-deployment="${run.id}">${icon("download")}下载推理部署包</button>${manage ? `<button class="btn ghost" data-convert-training="${run.id}">${icon("external")}转换并导出</button>` : ""}` : ""}</div>${run.status === "completed" && !run.artifact?.available ? `<p class="hint training-artifact-notice">${escapeHtml(run.artifact?.error || "这次训练暂无可下载的模型文件。可查看运行日志，或请管理员检查模型保存位置。")}</p>` : ""}${run.error ? `<div class="training-run-failure"><p>${escapeHtml(pipelineFailureAdvice(run.error))}</p><details class="pipeline-details pipeline-error" data-pipeline-detail="run-error-${run.id}"><summary>查看失败原因</summary><pre>${escapeHtml(run.error)}</pre></details></div>` : ""}</div>${sectionTabsMarkup("train", [["overview", "训练概况"], ["details", "参数与日志"], ["conversions", "模型转换"]], tab, escapeHtml)}<div class="training-tab-panel" ${sectionPanelAttributes("train", "overview", tab)}>${trainingMonitorMarkup(run)}</div><div class="training-tab-panel" ${sectionPanelAttributes("train", "details", tab)}><section class="training-configuration surface" aria-label="当前训练的参数与日志">${trainingRunDetails(run, version)}</section></div><div class="training-tab-panel" ${sectionPanelAttributes("train", "conversions", tab)}><section class="training-selected-conversions surface"><div class="conversion-title"><div><h3>这次训练的模型转换</h3><p>下载和转换文件均来自上方选中的训练记录。</p></div><button class="btn ghost" data-refresh-run-conversions="${run.id}">刷新</button></div>${run._conversionError ? `<p class="inline-error" role="alert">${escapeHtml(run._conversionError)}</p>` : ""}${(run.conversions || []).length ? `<div class="conversion-list">${run.conversions.map(conversionRow).join("")}</div>` : `<p class="hint">${run._conversionsLoading ? "正在读取转换记录…" : run.status === "completed" && run.artifact?.available ? "还没有转换记录。需要其他格式时，点击上方“转换并导出”。" : "训练完成并保存模型后，可以在这里查看转换文件。"}</p>`}</section></div>`;
}

function pipelineFailureAdvice(error) {
  const message = String(error || "");
  if (/out of memory|显存|内存不足/i.test(message)) return "计算内存不足。请修改配置，减小每批图片数或图像尺寸后重新训练。";
  if (/missing|not found|不存在|未找到|文件缺失/i.test(message)) return "所需模型或数据文件不可用。请查看失败原因，并请管理员确认文件后再重试。";
  if (/permission|权限/i.test(message)) return "无法访问所需文件或资源。请联系管理员检查权限后重试。";
  if (/network|download|connection|下载|连接/i.test(message)) return "无法连接或下载所需资源。请确认服务器网络恢复后再重试。";
  if (/compatible|不兼容|验证集|训练集|标注/i.test(message)) return "请检查失败原因中的数据要求，修正标注或数据划分后生成新版本再训练。";
  return "任务未完成。先查看失败原因；修正问题后可重试，或请管理员协助。";
}

function trainingMetricsMarkup(run) {
  const metrics = run.metrics || {}, primary = evaluationMetric(run.task, metrics);
  const suffix = run.task === "segment" ? "M" : run.task === "pose" ? "P" : "B";
  const values = [[primary.label, primary.value, primary.description], ...(run.task === "classify" ? [["前五项命中率", metrics["metrics/accuracy_top5"] ?? metrics.top5, "正确类别出现在前五个预测中的比例，越高越好。"]] : [["预测准确率", metrics[`metrics/precision(${suffix})`] ?? metrics.precision, "预测出的目标中正确的比例，越高越好。"], ["目标找全率", metrics[`metrics/recall(${suffix})`] ?? metrics.recall, "实际目标中被找到的比例，越高越好。"]])];
  const present = values.filter(([, value]) => value != null && value !== "" && Number.isFinite(Number(value)));
  if (!present.length) return "";
  return `<div class="metric-chips">${present.map(([label, value, description]) => `<span title="${escapeHtml(description)}"><small>${escapeHtml(label)}</small><strong>${formatMetric(value)}</strong></span>`).join("")}</div><p class="pipeline-metric-hint">${run.status === "completed" ? "训练程序保存的最终验证表现。" : "最近记录的验证表现。"}数值通常越高越好，部署前仍需用实际场景图片检查。</p>`;
}

function trainingRunDetails(run, version) {
  const settings = run.settings || {};
  const artifact = run.artifact || {};
  const parentId = run.lineage?.parent_run_id || run.parent_run_id;
  const resolvedDevice = run.resource?.resolved_device || run.resource?.requested_device || settings.device || "auto";
  return `<details class="pipeline-details" data-pipeline-detail="run-settings-${run.id}"><summary>训练参数与技术信息</summary><div class="run-detail-grid"><section><div class="detail-heading">${icon("branch")}数据与训练来源</div><dl class="run-properties"><div><dt>冻结版本</dt><dd>${escapeHtml(version?.name || `V${run.version_id}`)}</dd></div><div><dt>内容哈希</dt><dd><code>${escapeHtml(settings.version_content_hash?.slice(0, 16) || version?.content_hash?.slice(0, 16) || "—")}</code></dd></div><div><dt>基础权重哈希</dt><dd><code>${escapeHtml(settings.base_model_sha256?.slice(0, 16) || "—")}</code></dd></div><div><dt>配置哈希</dt><dd><code>${escapeHtml(run.config_hash?.slice(0, 16) || "—")}</code></dd></div><div><dt>重试记录</dt><dd>${parentId ? `Run #${parentId} → ` : ""}Run #${run.id}</dd></div><div><dt>创建时间</dt><dd>${formatDateTime(run.created_at)}</dd></div><div><dt>开始 / 结束</dt><dd>${formatDateTime(run.started_at)} / ${formatDateTime(run.finished_at)}</dd></div>${artifact.sha256 ? `<div><dt>训练权重校验</dt><dd><code>${escapeHtml(artifact.sha256.slice(0, 16))}</code></dd></div>` : ""}</dl></section><section><div class="detail-heading">${icon("sliders")}训练参数</div><dl class="run-properties"><div><dt>训练轮数 / 每批图片数</dt><dd>${run.total_epochs || settings.epochs || "—"} / ${settings.batch_size ?? "—"}</dd></div><div><dt>图像尺寸</dt><dd>${settings.image_size || settings.imgsz || "—"}</dd></div><div><dt>设备 / 策略</dt><dd>${escapeHtml(resolvedDevice)} / ${escapeHtml(settings.resource_profile || "balanced")}</dd></div><div><dt>优化器 / LR</dt><dd>${escapeHtml(settings.optimizer || "auto")} / ${settings.lr0 ?? "auto"}</dd></div><div><dt>权重衰减</dt><dd>${settings.weight_decay ?? "—"}</dd></div><div><dt>随机种子</dt><dd>${settings.seed ?? "—"}${settings.deterministic ? " · 可复现" : ""}</dd></div><div><dt>早停 / 重试</dt><dd>${settings.patience ?? "—"} / ${run.lineage?.max_retries ?? run.max_retries ?? "—"}</dd></div></dl></section></div></details><details class="pipeline-details" data-training-log-detail="${run.id}" data-pipeline-detail="run-log-${run.id}"><summary>原始运行日志（排查问题时查看）</summary><section class="run-log-section"><div class="detail-heading"><span>${icon("terminal")}运行日志</span><button class="mini-action" data-refresh-logs="${run.id}">${icon("refresh")}刷新</button></div>${renderTrainingLogConsole(run)}</section></details>`;
}

function conversionRow(item) {
  const displayStatus = item.cancel_requested && ["queued", "running"].includes(item.status) ? "cancelling" : item.status;
  const status = item.status === "completed" && !item.artifact?.available ? "已完成" : trainingStatusLabel(displayStatus, true);
  const rawProgress = Number(item.progress || 0);
  const progress = Math.round(Math.max(0, Math.min(100, rawProgress <= 1 ? rawProgress * 100 : rawProgress)));
  const attempt = Number(item.lineage?.attempt ?? item.attempt ?? 0);
  const retryable = ["failed", "cancelled"].includes(item.status) && (item.lineage ? item.lineage.retryable : attempt < Number(item.max_retries ?? 2));
  const details = conversionSettingsEntries(item);
  const available = item.status === "completed" && item.artifact?.available;
  const message = item.status === "failed" ? "转换未完成。查看失败原因，解决后可重试；原始模型仍可下载。" : item.status === "queued" ? "等待可用资源，无需重复提交。" : item.status === "completed" ? available ? "转换完成。部署前请用实际图片验证预测结果。" : "转换已结束，但模型文件不可下载。请查看日志或请管理员检查文件。" : item.status === "cancelled" ? "转换已取消，原始模型仍会保留。" : "正在转换，可以离开页面，任务会继续运行。";
  return `<article class="conversion-row conversion-record"><div class="conversion-record-heading"><div><strong>${escapeHtml(conversionFormatName(item.format))}</strong>${attempt ? `<span class="conversion-attempt">第 ${attempt} 次重试</span>` : ""}<p>${escapeHtml(conversionSettingsSummary(item))}</p></div><span class="job-status ${escapeHtml(displayStatus)}">${escapeHtml(status)}</span></div><p class="conversion-record-feedback">${escapeHtml(message)}</p>${["queued", "running", "cancelling"].includes(displayStatus) ? `<div class="conversion-record-progress"><progress max="100" value="${progress}" aria-label="转换进度"></progress><span>${progress}%</span></div>` : ""}<details class="pipeline-details conversion-settings" data-pipeline-detail="conversion-settings-${item.id}"><summary>查看转换设置与时间</summary><dl class="run-properties">${details.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(String(value))}</dd></div>`).join("")}<div><dt>提交时间</dt><dd>${escapeHtml(formatDateTime(item.created_at))}</dd></div><div><dt>开始 / 结束</dt><dd>${escapeHtml(formatDateTime(item.started_at))} / ${escapeHtml(formatDateTime(item.finished_at))}</dd></div></dl></details>${item.error ? `<details class="pipeline-details pipeline-error conversion-error" data-pipeline-detail="conversion-error-${item.id}"><summary>查看失败原因</summary><pre>${escapeHtml(item.error)}</pre></details>` : ""}<div class="conversion-actions"><button class="mini-action" data-conversion-logs="${item.id}">${icon("terminal")}查看日志</button>${canReview() && ["queued", "running"].includes(item.status) && !item.cancel_requested ? `<button class="mini-action danger" data-cancel-conversion="${item.id}">取消转换</button>` : ""}${canReview() && retryable ? `<button class="mini-action" data-retry-conversion="${item.id}">${icon("refresh")}按原设置重试</button>` : ""}${available ? `<button class="mini-action" data-download-conversion="${item.id}">${icon("download")}下载模型（${formatBytes(item.artifact.size_bytes)}）</button>` : ""}${available && item.manifest_available ? `<button class="mini-action" data-download-conversion-manifest="${item.id}">${icon("download")}下载部署说明</button>` : ""}</div>${available && item.manifest_available ? '<p class="hint conversion-manifest-hint">部署说明包含输入预处理、输出结构及标签信息，可交给设备端开发人员。</p>' : ""}</article>`;
}

function trainingStatusLabel(status, conversion = false) { return ({ queued: "排队中", pending: "等待中", running: conversion ? "转换中" : "训练中", cancelling: "正在取消", completed: conversion ? "可下载" : "已完成", failed: "失败", cancelled: "已取消" })[status] || status || "未知"; }
function formatMetric(value) { const number = Number(value); return value != null && value !== "" && Number.isFinite(number) ? number.toFixed(3) : "—"; }
function formatCompactNumber(value) { const number = Number(value); if (!Number.isFinite(number)) return "—"; if (number >= 1e9) return `${(number / 1e9).toFixed(1)}B`; if (number >= 1e6) return `${(number / 1e6).toFixed(1)}M`; if (number >= 1e3) return `${(number / 1e3).toFixed(1)}K`; return String(number); }
function formatDateTime(value) { if (!value) return "—"; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString(); }
function formatRunTime(run) { if (!run.started_at) return "尚未开始"; const start = new Date(run.started_at); const end = run.finished_at ? new Date(run.finished_at) : new Date(); if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return "时长未知"; const seconds = Math.max(0, Math.round((end - start) / 1000)); return formatDuration(seconds); }
function formatDuration(seconds) { if (!Number.isFinite(Number(seconds))) return "时长未知"; if (seconds < 60) return `${seconds} 秒`; if (seconds < 3600) return `${Math.ceil(seconds / 60)} 分钟`; return `${(seconds / 3600).toFixed(1)} 小时`; }

function trainingTaskChoices(selectedTask) {
  const tasks = Object.keys(state.trainingCatalog?.tasks || {});
  return tasks.map(task => { const info = trainingTaskInfo(task); const models = taskModels(task, true); const available = taskAvailable(task); return `<label class="training-task-option ${available ? "" : "unavailable"}"><input type="radio" name="train-task" value="${escapeHtml(task)}" ${task === selectedTask ? "checked" : ""} ${available ? "" : "disabled"}/><span>${icon(info.icon)}</span><div><strong>${escapeHtml(info.name)}</strong><small>${available ? `${models.length} 个兼容模型 · ${escapeHtml(info.schema || info.description)}` : escapeHtml(info.reason || "当前标注结构不支持")}</small></div></label>`; }).join("");
}

function trainingDeviceChoices(selected = "auto") {
  const resources = state.trainingResources || {};
  const devices = [{ id: "auto", type: "auto", name: "自动调度", available: true, reason: "优先选择空闲 GPU，无可用 GPU 时回退 CPU" }, ...(resources.devices || [])];
  const unique = [...new Map(devices.map(device => [device.id, device])).values()];
  return unique.map(device => { const memory = device.total_memory_bytes ? `${formatBytes(device.free_memory_bytes || 0)} / ${formatBytes(device.total_memory_bytes)} 可用` : device.type === "cpu" ? `${device.logical_cores || device.cores || "—"} 核 CPU` : device.reason || "由 Worker 决定"; return `<label class="resource-option ${device.available === false ? "unavailable" : ""}"><input type="radio" name="train-device" value="${escapeHtml(device.id)}" ${device.id === selected ? "checked" : ""} ${device.available === false ? "disabled" : ""}/><span>${icon(device.type === "cpu" ? "cpu" : "chip")}</span><div><strong>${escapeHtml(device.name || device.id)}</strong><small>${escapeHtml(memory)}${Number.isFinite(Number(device.utilization_percent)) ? ` · ${Number(device.utilization_percent).toFixed(0)}% 使用率` : ""}</small></div></label>`; }).join("");
}

function trainingResourceProfileOptions(selected = "balanced") {
  const supplied = state.trainingCatalog?.resource_profiles;
  const defaults = {
    balanced: { name: "均衡", description: "优先 GPU，不可用时安全回退" },
    performance: { name: "吞吐优先", description: "优先空闲显存最大的 GPU" },
    low_memory: { name: "低显存", description: "优先空闲显存比例最高的设备" },
  };
  const profiles = supplied && typeof supplied === "object" ? supplied : defaults;
  return Object.entries(profiles).map(([id, config]) => {
    const item = typeof config === "string" ? { name: config } : config || {};
    return `<option value="${escapeHtml(id)}" ${id === selected ? "selected" : ""} title="${escapeHtml(item.description || "")}">${escapeHtml(item.name || item.label || id)}</option>`;
  }).join("");
}

function openTrainingModal(templateRun = null) {
  if (!canReview()) return notify("请由审核员或管理员开始训练", "error");
  if (!state.versions.length) return notify("请先到“数据版本”保存一份训练数据", "error");
  const current = trainingContextGuard(), projectId = state.project.id;
  const settings = templateRun?.settings || {};
  const versionId = templateRun?.version_id || templateRun?.dataset_version_id || state.selectedTrainingVersion || state.versions[0].id;
  const version = state.versions.find(item => item.id === versionId) || state.versions[0];
  const requestedTask = templateRun?.task || versionDefaultTask(version);
  const initialTask = requestedTask;
  const selectedDevice = settings.device || state.trainingResources?.default_device || "auto";
  const formats = availableConversionFormats();
  openModal({ title: templateRun ? "复用训练配置" : "开始新训练", subtitle: "选择审核后的数据版本和推荐模型。提交后将在后台训练，可以离开页面。", wide: true, body: `
    <section class="wizard-section"><div class="wizard-section-title"><span>1</span><div><strong>选择训练数据</strong><small>使用已保存的数据版本，之后修改标注不会影响本次训练</small></div></div><div class="form-grid"><div class="field"><label>数据版本</label><select id="train-version">${state.versions.map(item => `<option value="${item.id}" ${item.id === version.id ? "selected" : ""}>${escapeHtml(item.name || `V${item.version_number || item.id}`)} · ${item.asset_count || 0} 张 / ${item.annotation_count || 0} 标注</option>`).join("")}</select></div><div class="field"><label>训练名称</label><input type="text" id="train-name" maxlength="100" value="${escapeHtml(templateRun ? `${templateRun.name || "训练"}（重新训练）` : `${state.project.name} 第 ${Number(state.training.total || 0) + 1} 次训练`)}" /></div></div><details class="pipeline-details"><summary>训练任务（已按数据版本选择）</summary><div class="training-task-grid">${trainingTaskChoices(initialTask)}</div></details><div class="training-compatibility" id="training-compatibility"></div></section>
    <section class="wizard-section"><div class="wizard-section-title"><span>2</span><div><strong>选择起始模型</strong><small>已选好当前可用的推荐模型。它已有基础识别能力，将继续学习你的数据。</small></div></div><div class="training-model-picker" id="training-model-picker"></div><div class="hint" id="train-model-hint"></div></section>
    <section class="wizard-section"><div class="wizard-section-title"><span>3</span><div><strong>训练设置</strong><small>首次使用可保持推荐值。每轮会学习一遍训练图片，轮数越多通常耗时越长。</small></div></div><div class="form-grid"><div class="field"><label>训练轮数</label><input type="number" id="train-epochs" min="1" max="10000" value="${settings.epochs ?? 50}" /></div></div><details class="advanced-training pipeline-details"><summary>${icon("sliders")}高级训练设置（可保持默认）</summary><div class="form-grid three"><div class="field"><label>图像尺寸</label><input type="number" id="train-imgsz" min="32" max="4096" step="32" value="${settings.image_size || settings.imgsz || 640}" /></div><div class="field"><label>每批图片数</label><input type="number" id="train-batch" min="-1" max="4096" value="${settings.batch_size ?? -1}" /><div class="hint">-1 表示自动选择；内存不足时可改小。</div></div><div class="field"><label>效果不再提升时等待轮数</label><input type="number" id="train-patience" min="0" max="10000" value="${settings.patience ?? 25}" /></div></div><div class="resource-grid" id="training-resource-grid">${trainingDeviceChoices(selectedDevice)}</div><div class="form-grid three"><div class="field"><label>优化器</label><select id="train-optimizer"><option value="auto">自动选择</option>${["SGD", "Adam", "AdamW", "NAdam", "RAdam", "RMSProp", "MuSGD"].map(value => `<option value="${value}" ${settings.optimizer === value ? "selected" : ""}>${value}</option>`).join("")}</select></div><div class="field"><label>初始学习率</label><input type="number" id="train-lr0" min="0.0000001" max="1" step="0.0001" value="${settings.lr0 ?? 0.01}" /></div><div class="field"><label>权重衰减</label><input type="number" id="train-weight-decay" min="0" max="1" step="0.0001" value="${settings.weight_decay ?? 0.0005}" /></div><div class="field"><label>数据读取进程数</label><input type="number" id="train-workers" min="0" max="64" value="${settings.workers ?? 4}" /></div><div class="field"><label>随机种子</label><input type="number" id="train-seed" min="0" max="2147483647" value="${settings.seed ?? 42}" /></div><div class="field"><label>资源策略</label><select id="train-resource-profile">${trainingResourceProfileOptions(settings.resource_profile || "balanced")}</select></div><div class="field"><label>数据缓存</label><select id="train-cache"><option value="false">关闭</option><option value="ram" ${settings.cache === "ram" ? "selected" : ""}>内存</option><option value="disk" ${settings.cache === "disk" ? "selected" : ""}>磁盘</option></select></div><div class="field"><label>每隔多少轮保存</label><input type="number" id="train-save-period" min="-1" max="10000" value="${settings.save_period ?? -1}" /><div class="hint">-1 仅保留最佳模型和最后一轮模型</div></div><div class="field"><label>冻结前 N 层</label><input type="number" id="train-freeze" min="0" max="1000" placeholder="不冻结" value="${Number.isInteger(settings.freeze) ? settings.freeze : ""}" /></div><div class="field"><label>关闭 Mosaic</label><input type="number" id="train-close-mosaic" min="0" max="10000" value="${settings.close_mosaic ?? 10}" /><div class="hint">最后多少轮停止拼图增强</div></div><div class="field"><label>最多允许重试次数</label><input type="number" id="train-max-retries" min="0" max="10" value="${templateRun?.max_retries ?? settings.max_retries ?? 2}" /></div></div><div class="check-stack"><label class="check-line"><input id="train-deterministic" type="checkbox" ${settings.deterministic === false ? "" : "checked"}/>尽量保持多次训练结果一致（可能稍慢）</label><label class="check-line"><input id="train-amp" type="checkbox" ${settings.amp === false ? "" : "checked"}/>启用自动混合精度（运行时支持时）</label><label class="check-line"><input id="train-cos-lr" type="checkbox" ${settings.cos_lr ? "checked" : ""}/>使用余弦学习率调度</label></div></details></section>
    <div class="training-submit-summary" id="training-submit-summary">正在检查配置兼容性…</div><div class="export-note">${icon("database")}提交后会自动保存训练记录。完成后可下载原始模型，或转换为：${formats.length ? formats.map(value => escapeHtml(value.toUpperCase())).join("、") : "当前无可用转换器"}</div>`, submitText: "开始训练", onReady: () => {
      let selectedModelId = templateRun ? String(state.trainingCatalog.models.find(model => model.model_arch === templateRun.model_arch)?.id || "") : "";
      let compatibilityEpoch = 0;
      const compatibilityCache = new Map();
      const update = async ({ applyDefaults = false } = {}) => {
        const epoch = ++compatibilityEpoch;
        const submit = document.querySelector("#modal-submit");
        submit.disabled = true;
        const selectedVersion = state.versions.find(item => item.id === Number(document.querySelector("#train-version").value));
        const task = document.querySelector('input[name="train-task"]:checked')?.value;
        const info = trainingTaskInfo(task);
        const compatibleModels = taskModels(task);
        let selectedModel = compatibleModels.find(model => String(model.id) === selectedModelId && model.available) || compatibleModels.find(model => model.recommended && model.available) || compatibleModels.find(model => model.available);
        selectedModelId = selectedModel ? String(selectedModel.id) : "";
        const picker = document.querySelector("#training-model-picker");
        picker.innerHTML = compatibleModels.length ? compatibleModels.map(model => trainingModelPickerCard(model, String(model.id) === selectedModelId)).join("") : `<div class="modal-warning">${icon("warning")}模型目录中没有适用于${escapeHtml(info.name)}的权重。</div>`;
        picker.querySelectorAll('input[name="train-model"]').forEach(input => input.onchange = () => { selectedModelId = input.value; applyModelDefaults(state.trainingCatalog.models.find(model => String(model.id) === input.value)); updateTrainingSubmitSummary(); });
        if (applyDefaults && selectedModel) applyModelDefaults(selectedModel);
        let compatibility = { compatible: false, warnings: [] };
        try {
          if (!compatibilityCache.has(selectedVersion.id)) compatibilityCache.set(selectedVersion.id, await api(`/api/versions/${selectedVersion.id}/training-compatibility`));
          compatibility = compatibilityCache.get(selectedVersion.id).tasks[task] || compatibility;
        } catch (error) { compatibility.warnings = [error.message]; }
        if (epoch !== compatibilityEpoch || document.querySelector("#modal-submit") !== submit) return;
        const warnings = [...(compatibility.warnings || [])];
        const dataIssue = trainingDataIssue(selectedVersion);
        if (dataIssue) warnings.unshift(dataIssue);
        if (!compatibility.compatible) warnings.push(compatibility.reason || "当前数据版本与所选训练任务不兼容。");
        if (!info.available) warnings.push(info.reason || "该任务当前不可训练。");
        document.querySelector("#training-compatibility").innerHTML = warnings.length ? `${icon("warning")}<span>${warnings.map(escapeHtml).join(" ")}${dataIssue ? ` <a href="#/projects/${state.project.id}/versions" id="training-fix-version">去数据版本页</a>` : ""}</span>` : `${icon("check")}<span>${escapeHtml(info.name)}与当前数据版本兼容。</span>`;
        document.querySelector("#training-fix-version")?.addEventListener("click", closeModal);
        document.querySelector("#train-model-hint").textContent = selectedModel ? "已选择可用模型，首次训练可保持这个选择。" : (info.reason || "暂无可用模型，请联系管理员配置后重试。");
        submit.disabled = !selectedVersion || !taskAvailable(task) || !selectedModel || !compatibility.compatible || Boolean(dataIssue);
        updateTrainingSubmitSummary();
      };
      document.querySelectorAll('input[name="train-task"]').forEach(input => input.onchange = () => { selectedModelId = ""; update({ applyDefaults: true }); });
      document.querySelector("#train-version").onchange = () => { const selectedVersion = state.versions.find(item => item.id === Number(document.querySelector("#train-version").value)); const taskInput = document.querySelector(`input[name="train-task"][value="${CSS.escape(versionDefaultTask(selectedVersion))}"]`); if (taskInput) taskInput.checked = true; selectedModelId = ""; update({ applyDefaults: true }); };
      ["train-epochs", "train-imgsz", "train-batch"].forEach(id => document.querySelector(`#${id}`).oninput = updateTrainingSubmitSummary);
      document.querySelectorAll('input[name="train-device"]').forEach(input => input.onchange = updateTrainingSubmitSummary);
      update({ applyDefaults: !templateRun });
    }, onSubmit: async () => {
      const task = document.querySelector('input[name="train-task"]:checked')?.value;
      const modelId = document.querySelector('input[name="train-model"]:checked')?.value;
      const model = state.trainingCatalog.models.find(item => String(item.id) === modelId);
      if (!model) throw new Error("请选择可用的预训练模型");
      const cacheValue = document.querySelector("#train-cache").value;
      const freezeValue = document.querySelector("#train-freeze").value.trim();
      const payload = { version_id: Number(document.querySelector("#train-version").value), name: document.querySelector("#train-name").value.trim(), task, model_arch: model.model_arch, epochs: Number(document.querySelector("#train-epochs").value), image_size: Number(document.querySelector("#train-imgsz").value), batch_size: Number(document.querySelector("#train-batch").value), device: document.querySelector('input[name="train-device"]:checked')?.value || "auto", workers: Number(document.querySelector("#train-workers").value), patience: Number(document.querySelector("#train-patience").value), optimizer: document.querySelector("#train-optimizer").value, lr0: Number(document.querySelector("#train-lr0").value), weight_decay: Number(document.querySelector("#train-weight-decay").value), seed: Number(document.querySelector("#train-seed").value), deterministic: document.querySelector("#train-deterministic").checked, amp: document.querySelector("#train-amp").checked, cache: cacheValue === "false" ? false : cacheValue, save_period: Number(document.querySelector("#train-save-period").value), freeze: freezeValue === "" ? null : Number(freezeValue), cos_lr: document.querySelector("#train-cos-lr").checked, close_mosaic: Number(document.querySelector("#train-close-mosaic").value), max_retries: Number(document.querySelector("#train-max-retries").value), resource_profile: document.querySelector("#train-resource-profile").value };
      if (!payload.name) throw new Error("请输入训练名称，方便之后查找");
      validateTrainingPayload(payload);
      const run = await api(`/api/projects/${projectId}/training-runs`, jsonOptions("POST", payload));
      if (!current()) return;
      closeModal(); notify("训练已加入队列。右侧显示这次新训练，无需重复提交。"); await revealNewTraining(run.id);
    },
  });
}

function validateTrainingPayload(payload) {
  if (!Number.isInteger(payload.version_id) || payload.version_id < 1) throw new Error("请选择有效的数据版本");
  if (!payload.task || !taskAvailable(payload.task)) throw new Error("所选训练任务当前不可用");
  if (!Number.isInteger(payload.epochs) || payload.epochs < 1 || payload.epochs > 10000) throw new Error("训练轮数必须是 1–10000 之间的整数");
  if (!Number.isInteger(payload.image_size) || payload.image_size < 32 || payload.image_size > 4096) throw new Error("图像尺寸必须在 32–4096 之间");
  if (!Number.isInteger(payload.batch_size) || payload.batch_size === 0 || payload.batch_size < -1 || payload.batch_size > 4096) throw new Error("每批图片数必须为 -1（自动）或 1–4096 之间的整数，请在高级训练设置中修改");
  if (!Number.isInteger(payload.workers) || payload.workers < 0 || payload.workers > 64) throw new Error("数据读取进程数必须在 0–64 之间，请在高级训练设置中修改");
  if (!Number.isFinite(payload.lr0) || payload.lr0 <= 0 || payload.lr0 > 1) throw new Error("初始学习率必须大于 0 且不超过 1");
  if (!Number.isFinite(payload.weight_decay) || payload.weight_decay < 0 || payload.weight_decay > 1) throw new Error("权重衰减必须在 0–1 之间，请在高级训练设置中修改");
  if (!Number.isInteger(payload.save_period) || payload.save_period === 0 || payload.save_period < -1) throw new Error("保存周期必须为 -1 或正整数");
  if (payload.freeze !== null && (!Number.isInteger(payload.freeze) || payload.freeze < 0 || payload.freeze > 1000)) throw new Error("冻结层数必须在 0–1000 之间");
  if (!Number.isInteger(payload.close_mosaic) || payload.close_mosaic < 0 || payload.close_mosaic > 10000) throw new Error("Mosaic 关闭周期必须在 0–10000 之间");
  if (!Number.isInteger(payload.max_retries) || payload.max_retries < 0 || payload.max_retries > 10) throw new Error("最多允许重试次数必须在 0–10 之间");
  if (payload.resource_profile === "performance" && payload.device === "cpu") throw new Error("吞吐优先策略不能与 CPU 设备组合");
  if (payload.resource_profile === "performance" && payload.device === "auto" && !state.trainingResources?.cuda_available) throw new Error("吞吐优先策略需要可用的 CUDA GPU");
}

function trainingModelPickerCard(model, selected) {
  const size = model.parameters || model.parameter_count ? `${formatCompactNumber(model.parameters || model.parameter_count)} 参数` : model.size_bytes ? formatBytes(model.size_bytes) : "大小未知";
  return `<label class="training-model-option ${model.available ? "" : "unavailable"}"><input type="radio" name="train-model" value="${escapeHtml(model.id)}" ${selected ? "checked" : ""} ${model.available ? "" : "disabled"}/><div><div><strong>${escapeHtml(model.name)}</strong>${model.recommended ? `<span>推荐</span>` : ""}</div><p>${escapeHtml(model.family)} · ${escapeHtml(size)}</p><small>${model.available ? escapeHtml(model.description || model.model_arch) : escapeHtml(model.reason || "本机权重不可用")}</small></div></label>`;
}

function applyModelDefaults(model) {
  if (!model) return;
  const defaults = model.defaults || {};
  const mapping = { epochs: "train-epochs", image_size: "train-imgsz", batch_size: "train-batch", patience: "train-patience", workers: "train-workers", lr0: "train-lr0", weight_decay: "train-weight-decay" };
  Object.entries(mapping).forEach(([key, id]) => { if (defaults[key] !== undefined && document.querySelector(`#${id}`)) document.querySelector(`#${id}`).value = defaults[key]; });
}

function updateTrainingSubmitSummary() {
  const node = document.querySelector("#training-submit-summary");
  if (!node) return;
  const task = document.querySelector('input[name="train-task"]:checked')?.value;
  const modelId = document.querySelector('input[name="train-model"]:checked')?.value;
  const model = state.trainingCatalog.models.find(item => String(item.id) === modelId);
  const device = document.querySelector('input[name="train-device"]:checked')?.value || "auto";
  const epochs = document.querySelector("#train-epochs")?.value;
  const batch = document.querySelector("#train-batch")?.value;
  const imageSize = document.querySelector("#train-imgsz")?.value;
  const deviceLabel = device === "auto" ? "自动选择计算设备" : (state.trainingResources?.devices || []).find(item => item.id === device)?.name || (device === "cpu" ? "处理器（CPU，训练通常较慢）" : device);
  node.innerHTML = `${icon(document.querySelector("#modal-submit")?.disabled ? "warning" : "check")}<span><strong>${escapeHtml(trainingTaskInfo(task).name)} · ${escapeHtml(model?.name || "尚无可用模型")}</strong><small>训练 ${escapeHtml(String(epochs || "—"))} 轮，图片尺寸 ${escapeHtml(String(imageSize || "—"))} 像素；${Number(batch) === -1 ? "每批数量自动选择" : `每批 ${escapeHtml(String(batch || "—"))} 张`}。${escapeHtml(deviceLabel)}</small></span>`;
}

function openConversionModal(trainingId) {
  if (!canReview()) return notify("模型转换由审核员或管理员操作。", "error");
  const current = trainingContextGuard();
  const projectId = state.project.id;
  const run = trainingRunById(trainingId);
  if (!run || run.status !== "completed" || !run.artifact?.available) return notify("请先完成训练，生成可用模型后再转换。", "error");
  let form = null;
  openModal({ title: "转换并导出模型", subtitle: `为“${run.name || `训练 ${trainingId}`}”选择部署格式。原始模型仍会保留。`, wide: true, body: `<div id="conversion-options-content"><p role="status"><span class="spinner"></span>正在读取转换环境与校准数据…</p>${conversionFormatCards(state.trainingCatalog?.conversion_formats, escapeHtml)}<p class="hint">配置加载完成后才能提交，避免使用过期的环境或数据状态。</p></div>`, submitText: "开始转换", onReady: () => {
    const container = document.querySelector("#conversion-options-content");
    const submit = document.querySelector("#modal-submit");
    submit.disabled = true;
    const load = async () => {
      if (!current() || !container.isConnected) return;
      form = null; submit.disabled = true;
      container.innerHTML = `<p role="status"><span class="spinner"></span>正在读取转换环境与校准数据…</p>${conversionFormatCards(state.trainingCatalog?.conversion_formats, escapeHtml)}`;
      try {
        const [options, resources] = await Promise.all([api(`/api/training-runs/${trainingId}/conversion-options`), api("/api/training/resources")]);
        if (!current() || !container.isConnected) return;
        form = mountConversionForm(container, { run, options, resources: normalizeTrainingResources(resources), escapeHtml, onValidityChange: valid => { if (submit.isConnected) submit.disabled = !valid; } });
      } catch (error) {
        if (!current() || !container.isConnected) return;
        container.innerHTML = `${conversionFormatCards(state.trainingCatalog?.conversion_formats, escapeHtml)}<div class="conversion-validation" role="alert"><p>转换选项加载失败：${escapeHtml(error.message)}</p><p>请重新加载；当前尚未提交转换任务。</p><button class="btn ghost" id="retry-conversion-options">重新加载</button></div>`;
        container.querySelector("#retry-conversion-options").onclick = load;
      }
    };
    void load();
  }, onSubmit: async () => {
    if (!current()) return;
    if (!form) throw new Error("请等待转换配置加载完成后再提交。");
    const payload = form.payload();
    await api(`/api/training-runs/${trainingId}/conversions`, jsonOptions("POST", payload));
    if (!current()) return;
    setSectionTab(state.session, projectId, "train", "conversions");
    closeModal(); notify(`${conversionFormatName(payload.format)} 转换已加入队列。完成后可在这次训练的转换记录中下载。`);
    await refreshPipelineData(); startPipelinePolling();
  } });
}

function conversionFormatEntries() {
  const formats = state.trainingCatalog?.conversion_formats || state.training?.availability?.conversion_formats || {};
  return conversionFormats(formats).map(config => [config.format, config]);
}
function availableConversionFormats() { return conversionFormatEntries().filter(([, value]) => value.available).map(([name]) => name); }
function formatDescription(format) { return conversionFormatDescription(format); }

function confirmCancelTraining(id) {
  if (!canReview()) return notify("请由审核员或管理员取消训练。", "error");
  const run = trainingRunById(id);
  openModal({ title: "取消这次训练？", subtitle: run?.name || "训练任务", body: `<div class="modal-warning">${icon("warning")}取消后，本次训练将停止，可能无法获得完整模型。已有数据版本和训练记录会保留，需要时可重新训练。停止需要一点时间。</div>`, submitText: "确认取消训练", onSubmit: async () => { await cancelTraining(id); closeModal(); } });
}

async function cancelTraining(id) {
  try { await api(`/api/training-runs/${id}/cancel`, { method: "POST" }); notify("已请求取消训练，请等待状态变为“已取消”。"); await refreshPipelineData(); }
  catch (error) { notify(error.message, "error"); throw error; }
}

function confirmRetryTraining(id) {
  if (!canReview()) return notify("请由审核员或管理员重试训练。", "error");
  const current = trainingContextGuard();
  const run = trainingRunById(id);
  openModal({ title: "按原配置重新训练？", subtitle: "将重新开始一次训练，使用相同的数据、模型和参数；原来的记录会保留。", body: `<div class="retry-summary"><strong>${escapeHtml(run?.name || `训练 ${id}`)}</strong><p>${run?.error ? "请先确认失败原因已解决。如果需要减小图片尺寸等参数，请关闭此窗口，选择“修改配置再训练”。" : "本次将从头训练，不会从上次停止的位置继续。"}</p>${run?.error ? `<details class="pipeline-details"><summary>上次失败原因</summary><pre>${escapeHtml(run.error)}</pre></details>` : ""}</div>`, submitText: "按原配置重试", onSubmit: async () => { const retried = await api(`/api/training-runs/${id}/retry`, { method: "POST" }); if (!current()) return; closeModal(); notify("已重新加入训练队列，右侧显示这次新训练。"); await revealNewTraining(retried.id); } });
}

async function revealNewTraining(id) {
  if (!state.project || state.currentSection !== "train") return;
  state.trainingFilters = { status: "all", task: "all", query: "" };
  await Promise.all([selectTrainingMonitor(id, window.innerWidth < 900), loadTrainingList({ page: 1, locateId: id })]);
}

function confirmCancelConversion(id) {
  if (!canReview()) return notify("请由审核员或管理员取消转换。", "error");
  const current = trainingContextGuard();
  openModal({ title: "取消这次模型转换？", subtitle: "取消后，本次将不再生成完整的转换文件。", body: `<p>原始训练模型和已有下载文件不受影响。需要时可以再次开始转换。</p>`, submitText: "确认取消转换", onSubmit: async () => { await api(`/api/conversions/${id}/cancel`, { method: "POST" }); if (!current()) return; closeModal(); notify("已请求取消转换，请等待状态变为“已取消”。"); await refreshPipelineData(); } });
}

async function cancelConversion(id) { try { await api(`/api/conversions/${id}/cancel`, { method: "POST" }); notify("已请求取消转换"); await refreshPipelineData(); } catch (error) { notify(error.message, "error"); } }
async function retryConversion(id, button) {
  if (!canReview()) return notify("请由审核员或管理员重试转换。", "error");
  const current = trainingContextGuard(), label = button?.innerHTML;
  if (button) { button.disabled = true; button.innerHTML = '<span class="spinner"></span>正在提交…'; }
  try { await api(`/api/conversions/${id}/retry`, { method: "POST" }); if (!current()) return; notify("已按原设置重新加入转换队列。"); await refreshPipelineData(); startPipelinePolling(); }
  catch (error) { if (current()) notify(error.message, "error"); }
  finally { if (button?.isConnected) { button.disabled = false; button.innerHTML = label; } }
}

function openConversionLogModal(id) {
  openModal({ title: "转换详细日志", subtitle: "供排查失败原因使用。可以把相关错误信息提供给管理员。", wide: true, body: `<div class="run-log-console loading" id="conversion-log-modal"><span class="spinner"></span>正在加载转换日志…</div>`, submitText: "关闭", onReady: async () => {
    const node = document.querySelector("#conversion-log-modal");
    try {
      const response = await api(`/api/conversions/${id}/logs?after_id=0&limit=1000`);
      const entries = response?.logs || response?.items || [];
      if (!node?.isConnected) return;
      node.classList.remove("loading");
      node.innerHTML = entries.length ? entries.map(entry => `<div class="log-line ${escapeHtml(entry.level || "info")}"><time>${escapeHtml(formatLogTime(entry.created_at))}</time><span>${escapeHtml(entry.level || "INFO")}</span><code>${escapeHtml(entry.message || entry.event || "")}</code></div>`).join("") : "当前还没有转换日志";
    } catch (error) {
      if (node?.isConnected) { node.classList.add("error"); node.textContent = `日志加载失败：${error.message}`; }
    }
  }, onSubmit: async () => closeModal() });
}

function toggleTrainingDetails(id) {
  if (state.expandedTrainingRuns.has(id)) state.expandedTrainingRuns.delete(id); else state.expandedTrainingRuns.add(id);
  renderTrainingPage();
  if (state.expandedTrainingRuns.has(id)) {
    loadRunConversions(id);
  }
}


async function loadTrainingMetrics(id, { render = false, force = false } = {}) {
  const projectId = state.project?.id, epoch = state.selectionEpoch;
  const current = () => state.project?.id === projectId && state.selectionEpoch === epoch;
  const previous = state.trainingMetricsHistory[id] || { history: [] };
  if (previous.loading) {
    if (force && previous.request) {
      await previous.request.catch(() => {});
      if (current() && !state.trainingMetricsHistory[id]?.loading) return loadTrainingMetrics(id, { render, force: true });
    }
    return;
  }
  if (!force && previous.attemptedAt && Date.now() - previous.attemptedAt < 10000) return;
  const pending = { ...previous, loading: true, error: "", attemptedAt: Date.now() };
  state.trainingMetricsHistory[id] = pending;
  if (render && state.currentSection === "train") renderTrainingPage();
  try {
    pending.request = pipelineApi(`/api/training-runs/${id}/metrics`);
    const response = await pending.request;
    if (!current() || state.trainingMetricsHistory[id] !== pending) return;
    state.trainingMetricsHistory[id] = { ...response, history: response?.history || [], latest: response?.latest || {}, loading: false, error: "", attemptedAt: pending.attemptedAt, fetchedAt: Date.now() };
  } catch (error) {
    if (!current() || state.trainingMetricsHistory[id] !== pending) return;
    state.trainingMetricsHistory[id] = { ...previous, attemptedAt: pending.attemptedAt, loading: false, error: `曲线暂时无法更新：${error.message} 请点击“刷新曲线”重试。` };
  } finally {
    if (state.trainingMetricsHistory[id] === pending) {
      state.trainingMetricsHistory[id] = { ...previous, loading: false, attemptedAt: 0 };
      if (state.project?.id === projectId && state.currentSection === "train") renderTrainingPage();
    }
  }
  if (render && current() && state.currentSection === "train") renderTrainingPage();
}

async function loadRunConversions(id, { force = false, render = false } = {}) {
  const projectId = state.project?.id, epoch = state.selectionEpoch, selection = state.trainingSelectionRequest;
  const run = monitoredTrainingRun();
  if (!run || run.id !== id || run._conversionsLoading || (!force && run._conversionsHydrated)) return;
  const current = () => state.project?.id === projectId && state.selectionEpoch === epoch && state.trainingSelectionRequest === selection && monitoredTrainingRun()?.id === id && state.currentSection === "train";
  const previousStatuses = new Map((run.conversions || []).map(item => [item.id, item.status]));
  run._conversionsLoading = true; run._conversionError = "";
  if (render) renderTrainingPage();
  try {
    const response = await pipelineApi(`/api/training-runs/${id}/conversions`);
    if (!current()) return;
    state.selectedTrainingRun.conversions = response?.conversions || [];
    state.selectedTrainingRun._conversionsHydrated = true;
    const finished = state.selectedTrainingRun.conversions.filter(item => ['queued','running'].includes(previousStatuses.get(item.id)) && ['completed','failed','cancelled'].includes(item.status));
    if (finished.some(item => item.status === 'failed')) notify('模型转换未完成，请查看转换记录中的失败原因。', 'error');
    else if (finished.some(item => item.status === 'completed')) notify('当前训练的模型转换完成，可以在“模型转换”标签下载。');
    else if (finished.length) notify('模型转换已取消，已有数据和文件会保留。');
  } catch (error) {
    if (current()) state.selectedTrainingRun._conversionError = `转换记录暂时无法更新：${error.message} 请点击右侧“刷新”重试。`;
  } finally {
    if (current()) { state.selectedTrainingRun._conversionsLoading = false; if (render) renderTrainingPage(); }
  }
}

function renderTrainingLogConsole(run) {
  const logState = state.trainingLogs[run.id];
  if (!logState) return `<div class="run-log-console loading"><span class="spinner"></span>展开后加载持久化训练日志</div>`;
  if (logState.loading && !logState.items?.length) return `<div class="run-log-console loading"><span class="spinner"></span>正在加载日志…</div>`;
  if (logState.error && !logState.items?.length) return `<div class="run-log-console error">${escapeHtml(logState.error)}</div>`;
  const entries = logState.items || [];
  if (!entries.length) return `<div class="run-log-console loading">当前还没有日志输出</div>`;
  return `<div class="run-log-console" data-run-log="${run.id}" role="log" aria-live="polite">${entries.map(entry => `<div class="log-line ${escapeHtml(entry.level || entry.stream || "info")}"><time>${escapeHtml(formatLogTime(entry.created_at || entry.timestamp))}</time><span>${escapeHtml(entry.level || entry.stream || "INFO")}</span><code>${escapeHtml(entry.message ?? entry.text ?? String(entry))}</code></div>`).join("")}</div>`;
}

function formatLogTime(value) { if (!value) return "--:--:--"; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleTimeString(); }

async function loadTrainingLogs(id, { append = true, render = false, force = false } = {}) {
  const previous = state.trainingLogs[id] || { items: [], cursor: 0 };
  if (previous.loading || !force && previous.attemptedAt && Date.now() - previous.attemptedAt < 5000) return;
  const projectId = state.project?.id, epoch = state.selectionEpoch;
  const current = () => state.project?.id === projectId && state.selectionEpoch === epoch;
  const pending = { ...previous, loading: true, error: "", attemptedAt: Date.now() };
  state.trainingLogs[id] = pending;
  if (render && state.currentSection === "train") renderTrainingPage();
  try {
    const after = append ? previous.cursor || 0 : 0;
    const response = await pipelineApi(`/api/training-runs/${id}/logs?after_id=${encodeURIComponent(after)}&limit=300`);
    if (!current() || state.trainingLogs[id] !== pending) return;
    const incoming = Array.isArray(response) ? response : response.logs || response.items || [];
    const items = append ? [...previous.items, ...incoming] : incoming;
    const deduplicated = [...new Map(items.map((entry, index) => [entry.id ?? `${entry.created_at || entry.timestamp || ""}-${entry.message || entry.text || index}`, entry])).values()];
    const last = deduplicated.at(-1);
    state.trainingLogs[id] = { items: deduplicated.slice(-1000), cursor: response.next_after_id ?? response.cursor ?? last?.id ?? after, loading: false, error: "", attemptedAt: pending.attemptedAt };
  } catch (error) {
    if (!current() || state.trainingLogs[id] !== pending) return;
    state.trainingLogs[id] = { ...previous, loading: false, error: `日志暂不可用：${error.message}`, attemptedAt: pending.attemptedAt };
  } finally {
    if (state.trainingLogs[id] === pending) state.trainingLogs[id] = { ...previous, loading: false, attemptedAt: 0 };
  }
  if (render && current() && state.currentSection === "train") renderTrainingPage();
}

function trainingSyncMarkup() {
  const time = state.pipelineUpdatedAt ? new Date(state.pipelineUpdatedAt).toLocaleTimeString('zh-CN', {hour12:false}) : '';
  return state.pipelineConnectionLost
    ? `<p id="pipeline-connection-notice" class="training-sync-status warning" role="status">状态更新暂时中断，正在自动重连。当前是上次状态，后台训练可能仍在运行。${time ? `最后更新 ${time}。` : ''}</p>`
    : `<p class="training-sync-status">${time ? `状态自动更新 · 最近更新 ${time}` : '正在同步训练状态…'}<span>切回此页面会立即同步</span></p>`;
}

async function refreshTrainingEnvironment() {
  const projectId = state.project?.id, epoch = state.selectionEpoch, session = state.session;
  const key = `${projectId}:${epoch}`;
  if (trainingEnvironmentRequest?.key === key) return trainingEnvironmentRequest.promise;
  const current = () => state.project?.id === projectId && state.selectionEpoch === epoch && state.session === session && state.currentSection === 'train';
  if (!current()) return;
  const request = {key}; trainingEnvironmentRequest = request;
  state.trainingEnvironmentLoading = true;
  let resourceSource = null;
  const applyResources = (resources, source) => {
    const incomingTime = Date.parse(resources?.snapshot_at || '');
    const currentTime = Date.parse(state.trainingResources?.snapshot_at || '');
    if (Number.isFinite(incomingTime) && Number.isFinite(currentTime)) {
      if (incomingTime < currentTime || incomingTime === currentTime && resourceSource === 'resources' && source === 'catalog') return;
    } else if (resourceSource === 'resources' && source === 'catalog') return;
    state.trainingResources = normalizeTrainingResources(resources, state.trainingCatalog, state.training?.availability);
    resourceSource = source;
  };
  request.promise = (async () => {
    const catalogWork = pipelineApi('/api/training/catalog', 45000).then(catalog => {
      if (!current()) return;
      state.trainingCatalog = normalizeTrainingCatalog(catalog, state.training?.availability);
      state.training.availability = {...state.training.availability, available:Boolean(catalog.backend?.available), reason:catalog.backend?.reason || '', conversion_formats:catalog.conversion_formats || {}};
      if (catalog.resources) applyResources(catalog.resources, 'catalog');
      renderTrainingPage();
    });
    const resourcesWork = pipelineApi('/api/training/resources').then(resources => {
      if (!current()) return;
      applyResources(resources, 'resources');
      renderTrainingPage();
    });
    const results = await Promise.allSettled([catalogWork, resourcesWork]);
    if (!current()) return;
    state.trainingEnvironmentLoading = false;
    if (results[0].status === 'rejected' && !state.training.availability?.available) state.training.availability = {...state.training.availability, available:false, reason:'训练环境暂时无法检查。已有记录仍可查看，请点击刷新重试。'};
    renderTrainingPage();
  })();
  try { await request.promise; }
  finally { if (trainingEnvironmentRequest === request) trainingEnvironmentRequest = null; }
}

async function refreshTrainingWorkspace() {
  if (state.pipelineManualRefreshing) return;
  state.pipelineManualRefreshing = true;
  const current = trainingContextGuard();
  const button = document.querySelector('#refresh-training');
  if (button) { button.disabled = true; button.innerHTML = `<span class="spinner"></span>刷新中`; }
  void refreshTrainingEnvironment();
  try {
    const refreshed = await refreshPipelineData();
    if (!current() || !refreshed) return;
    const selected = monitoredTrainingRun();
    if (selected) void loadTrainingMetrics(selected.id, {force:true, render:true});
    renderTrainingPage(); startPipelinePolling(); notify('训练状态已刷新，曲线与运行环境正在同步。');
  } catch (error) { if (current()) notify(error.message, 'error'); }
  finally { if (current()) { state.pipelineManualRefreshing = false; renderTrainingPage(); } }
}

async function refreshPipelineData(render = true) {
  const projectId = state.project?.id, epoch = state.selectionEpoch, session = state.session;
  if (!projectId || state.currentSection !== 'train') return false;
  const listAllowed = !state.trainingPage.loading && !trainingSearchTimer && !document.querySelector('#training-search[data-composing="true"]');
  const listRequest = state.trainingPage.request, selection = state.trainingSelectionRequest;
  const selectedId = !state.trainingSelectionLoading ? state.selectedTrainingId : null;
  if (!listAllowed && !selectedId) return false;
  const url = trainingListURL(projectId, state.trainingPage.page, state.trainingFilters);
  const key = `${projectId}:${epoch}:${listRequest}:${selection}:${listAllowed}:${selectedId}:${url}`;
  if (pipelineRefreshRequest?.key === key) return pipelineRefreshRequest.promise;
  const serial = ++pipelineRefreshSerial;
  const current = () => state.project?.id === projectId && state.selectionEpoch === epoch && state.session === session && state.currentSection === 'train' && serial === pipelineRefreshSerial;
  const listCurrent = () => current() && state.trainingPage.request === listRequest && !state.trainingPage.loading && !trainingSearchTimer && trainingListURL(projectId, state.trainingPage.page, state.trainingFilters) === url;
  const selectionCurrent = () => current() && state.trainingSelectionRequest === selection && state.selectedTrainingId === selectedId;
  const request = {key}; pipelineRefreshRequest = request;
  const display = () => { if (render && current()) renderTrainingPage(); };
  request.promise = (async () => {
    const listWork = !listAllowed ? Promise.resolve() : pipelineApi(url + '&include_availability=false').then(result => {
      if (!listCurrent()) return;
      applyTrainingListResponse(result); rememberTrainingNavigation();
      if (!selectedId) state.pipelineUpdatedAt = Date.now();
      display();
    }, error => {
      if (!listCurrent()) return;
      state.trainingPage.error = '记录列表暂时无法更新，保留上次结果，正在自动重试。';
      display(); throw error;
    });
    const detailWork = !selectedId ? Promise.resolve() : pipelineApi(`/api/training-runs/${selectedId}`).then(result => {
      if (!selectionCurrent()) return;
      if (result?.project_id !== projectId) throw new Error('训练记录不属于当前项目。');
      const previous = monitoredTrainingRun();
      state.selectedTrainingRun = {...result, conversions:previous?.conversions || [], _conversionsHydrated:previous?._conversionsHydrated, _conversionsLoading:previous?._conversionsLoading, _conversionError:previous?._conversionError};
      state.trainingSelectionError = ''; state.trainingSelectionLoading = false; state.pipelineUpdatedAt = Date.now();
      // Paint status before conversions, metrics or logs finish. Those requests
      // update their own panels and cannot hold a queued status on screen.
      display();
      const changed = previous?.current_epoch !== result.current_epoch || previous?.status !== result.status;
      void loadRunConversions(selectedId, {force:true, render:true});
      if (changed || ['queued','running','cancelling'].includes(result.status) || !state.trainingMetricsHistory[selectedId]?.fetchedAt) void loadTrainingMetrics(selectedId, {force:changed, render:true});
      const log = document.querySelector(`[data-training-log-detail="${selectedId}"][open]`);
      if (log && !log.closest('[hidden]')) void loadTrainingLogs(selectedId, {append:true, render:true});
    }, error => {
      if (!selectionCurrent()) return;
      if (!monitoredTrainingRun()) state.trainingSelectionError = `这次训练的详情暂时无法更新：${error.message}。正在自动重试。`;
      throw error;
    });
    const results = await Promise.allSettled([listWork, detailWork]);
    if (!current()) return false;
    const failure = results.find(result => result.status === 'rejected');
    state.pipelineConnectionLost = Boolean(failure);
    display();
    if (failure) throw failure.reason;
    return true;
  })();
  try { return await request.promise; }
  finally { if (pipelineRefreshRequest === request) pipelineRefreshRequest = null; }
}

function trainingHasActiveWork() {
  const selected = monitoredTrainingRun();
  return Number(state.training.counts?.active || 0) > 0 || ['queued','running','cancelling'].includes(selected?.status) || (selected?.conversions || []).some(item => ['queued','running'].includes(item.status));
}

function startPipelinePolling() {
  const projectId = state.project?.id, epoch = state.selectionEpoch;
  const key = `${projectId}:${epoch}`;
  if (!projectId || state.currentSection !== 'train') return;
  if (pipelinePollingKey === key && state.pollTimer && pipelinePollNow) { void pipelinePollNow(true); return; }
  clearInterval(state.pollTimer); state.pollTimer = null; pipelinePollingKey = key;
  let pending = false, lastAttempt = 0;
  const current = () => state.project?.id === projectId && state.selectionEpoch === epoch && state.currentSection === 'train';
  const poll = async (force = false) => {
    if (pending || document.hidden || !current()) return;
    if (!force && !trainingHasActiveWork() && Date.now() - lastAttempt < 15000) return;
    pending = true; lastAttempt = Date.now();
    const previous = new Map([...state.training.runs, ...(monitoredTrainingRun() ? [monitoredTrainingRun()] : [])].map(run => [run.id, run.status]));
    try {
      const refreshed = await refreshPipelineData();
      if (!current() || !refreshed) return;
      const visible = [...new Map([...state.training.runs, ...(monitoredTrainingRun() ? [monitoredTrainingRun()] : [])].map(run => [run.id, run])).values()];
      const finished = visible.filter(run => ['queued','running','cancelling'].includes(previous.get(run.id)) && ['completed','failed','cancelled'].includes(run.status));
      if (finished.some(run => run.status === 'failed')) notify('有任务未完成，请查看记录中的失败原因和处理建议。', 'error');
      else if (finished.some(run => run.status === 'completed')) notify('有训练已完成，从列表选择记录后可以下载模型。');
      else if (finished.length) notify('任务已取消，已有数据和记录会保留。');
      // Keep a slower idle poll: another member can start work at any time.
    } catch (_) {
      // The persistent status line distinguishes stale data from live progress.
      if (current()) { state.pipelineConnectionLost = true; renderTrainingPage(); }
    } finally { pending = false; }
  };
  pipelinePollNow = poll;
  state.pollTimer = setInterval(() => { void poll(); }, 3000);
  void poll(true);
}

function bindCommonProjectEvents() {
  revealActiveProjectTab();
  bindReviewPage({ state, api, jsonOptions, openModal, closeModal, notify, escapeHtml, renderDataPage, renderWorkBlocksPage, canReview, renderAiPage, renderWorkspace, loadAnnotations, refreshWorkflowState, refreshInspector });
  bindAssetManagement({ state, api, jsonOptions, openModal, closeModal, notify, escapeHtml, renderDataPage, isAdmin });
}

function revealActiveProjectTab() {
  const nav = document.querySelector(".navigation-scroll");
  const active = nav?.querySelector('[aria-current="page"]');
  if (!nav || !active || navigationIsOpen()) return;
  const bounds = nav.getBoundingClientRect(), item = active.getBoundingClientRect();
  if (item.bottom > bounds.bottom) nav.scrollTop += item.bottom - bounds.bottom + 12;
  else if (item.top < bounds.top) nav.scrollTop -= bounds.top - item.top + 12;
}

async function loadWorkspace(projectId, requestedAssetId = null, routeEpoch = state.selectionEpoch) {
  app.innerHTML = `<div class="app-shell">${topbar()}<main class="dashboard"><div class="dashboard-inner"><div class="skeleton" style="height:70vh"></div></div></main></div>`;
  resetProjectInteraction(projectId, "annotate");
  try {
    const workBlocks = await api(`/api/projects/${projectId}/work-blocks`);
    if (routeEpoch !== state.selectionEpoch) return;
    state.workBlocks = workBlocks;
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    let blockId = Number(params.get("block")) || null;
    if (workBlocks.enabled && !blockId && requestedAssetId) {
      const requested = await api(`/api/assets/${requestedAssetId}`);
      if (routeEpoch !== state.selectionEpoch) return;
      blockId = requested.work_block_id;
      if (!blockId && requested.project_id === projectId && ["submitted", "approved"].includes(requested.review_status)) state.legacyReviewAssetId = requestedAssetId;
    }
    if (workBlocks.enabled && !blockId && !state.legacyReviewAssetId) {
      location.replace(`#/projects/${projectId}/blocks`);
      return;
    }
    if (blockId) {
      const block = await api(`/api/projects/${projectId}/work-blocks/${blockId}`);
      if (routeEpoch !== state.selectionEpoch) return;
      state.activeWorkBlockId = block.id; state.activeWorkBlock = block;
      history.replaceState(null, "", annotationBlockURL(projectId, block.id, requestedAssetId));
    }
    const [project, assets, models, jobs, auditLogs] = await Promise.all([
      api(`/api/projects/${projectId}`), api(`/api/projects/${projectId}/assets`), api("/api/models"), api(`/api/projects/${projectId}/jobs`), api(`/api/projects/${projectId}/audit-logs?limit=100`),
    ]);
    if (routeEpoch !== state.selectionEpoch) return;
    state.project = project; state.assets = assets; state.models = models; state.jobs = jobs; state.auditLogs = auditLogs;
    state.activeLabel = project.labels[0] || null; state.tool = "select"; state.poseDraft = null; state.keypointPlacement = null; state.inspectorTab = "objects";
    state.asset = assets.find(asset => asset.id === requestedAssetId) || assets[0] || null;
    state.selectedAssetIds = new Set(); state.assetFilter = "all";
    if (state.legacyReviewAssetId) { state.readOnly = true; state.readOnlyReason = "历史图片仍可审核。若需修改，请重新打开后由管理员将图片加入任务块。"; }
    else if (state.asset) await acquireAssetLock(state.asset.id, routeEpoch);
    if (routeEpoch !== state.selectionEpoch) return;
    renderWorkspace();
    if (state.asset) await loadAnnotations(state.asset.id, routeEpoch);
    if (sessionStorage.getItem("visionforge-open-upload") === "1") {
      sessionStorage.removeItem("visionforge-open-upload");
      openUploadModal();
    }
    startJobPolling(); startCollaborationPolling();
  } catch (error) { if (routeEpoch === state.selectionEpoch) renderLoadError(error); }
}

function renderWorkspace() {
  if (deferRenderForNavigation("annotate")) return;
  annotationMenu?.close({ restoreFocus: false, force: true });
  canvasGuides?.destroy(); canvasGuides = null;
  rememberCanvasView();
  const currentAssetList = document.querySelector(".asset-list");
  if (currentAssetList) state.assetListScroll = currentAssetList.scrollTop;
  const project = state.project;
  const filteredAssets = workspaceAssets();
  let visibleAssets = filteredAssets.slice(0, state.assetRenderLimit);
  if (state.asset && filteredAssets.some(asset => asset.id === state.asset.id) && !visibleAssets.some(asset => asset.id === state.asset.id)) {
    visibleAssets = [state.asset, ...visibleAssets];
  }
  const remainingAssets = Math.max(0, filteredAssets.length - state.assetRenderLimit);
  const editable = canEditCurrentAsset();
  const currentIndex = filteredAssets.findIndex(asset => asset.id === state.asset?.id);
  app.innerHTML = `<div class="app-shell annotation-shell ${state.annotationFocus ? "is-focus" : ""}">${topbar()}${projectSubnav("annotate")}
    <main class="workspace annotation-workspace ${state.queueCollapsed ? "queue-collapsed" : ""} ${state.inspectorCollapsed ? "inspector-collapsed" : ""}">
        <div class="workspace-toolbar manual-workspace-toolbar">
          ${state.activeWorkBlock ? `<div class="annotation-block-scope"><a href="#/projects/${project.id}/blocks">任务块</a><strong>${escapeHtml(state.activeWorkBlock.name)}</strong><span>仅显示本块 ${state.assets.length} 张图片 · ${escapeHtml(state.activeWorkBlock.owner_name || "尚未领取")}</span><span>${BLOCK_STATUS[state.activeWorkBlock.status] || ""}</span>${!state.activeWorkBlock.is_mine ? `<span>查看模式${canReview() ? "，可审核已提交图片" : "，领取或转交后才能修改"}</span>` : ""}<a href="#/projects/${project.id}/blocks">交回 / 查看进度</a></div>` : ""}
          ${state.legacyReviewAssetId ? `<div class="annotation-block-scope"><strong>历史图片审核</strong><span>仅显示当前图片；需要重新标注时，请先将图片加入任务块。</span><a href="#/projects/${project.id}/blocks">管理任务块</a></div>` : ""}
          <div class="annotation-session-row">
            <button class="btn ghost panel-toggle" id="toggle-queue" aria-controls="asset-queue" aria-expanded="${!state.queueCollapsed}" aria-label="图片队列" title="展开或收起图片队列">${icon("folder")}<span>图片队列</span></button>
            <div class="asset-stepper"><button class="tool" id="previous-asset" title="上一张 (A / ←)" aria-label="上一张" ${!filteredAssets.length ? "disabled" : ""}>${icon("arrowLeft")}</button><span title="${escapeHtml(state.asset?.filename || "未选择图片")}">${state.asset ? `${currentIndex < 0 ? "—" : currentIndex + 1} / ${filteredAssets.length}` : "0 / 0"}</span><button class="tool" id="next-asset" title="下一张 (D / →)" aria-label="下一张" ${!filteredAssets.length ? "disabled" : ""}>${icon("arrowRight")}</button></div>
            <span class="review-badge ${state.asset?.review_status || "draft"}">${reviewStatusName(state.asset?.review_status)}</span>
            <span class="workflow-controls" id="workflow-controls">${workflowControlsMarkup()}</span>
            <label class="manual-label-picker" for="manual-label"><span>${projectTask() === "classify" ? "图片类别" : "新对象类别"}</span><select id="manual-label" ${canEditCurrentAsset() && project.labels.length && !hasAnnotationDraft() ? "" : "disabled"}><option value="" disabled ${projectTask() === "classify" && !state.annotations.length && !state.pendingAnnotation ? "selected" : ""}>选择类别…</option>${project.labels.map(label => `<option value="${label.id}" ${String(projectTask() === "classify" ? (state.pendingAnnotation?.labelId || state.annotations[0]?.label_id || "") : state.activeLabel?.id) === String(label.id) ? "selected" : ""}>${escapeHtml(label.name)}${label.shortcut ? ` (${escapeHtml(label.shortcut)})` : ""}</option>`).join("")}</select></label>
            <div class="annotation-session-actions">
              <button class="btn ghost" id="toggle-focus" aria-pressed="${state.annotationFocus}" aria-label="${state.annotationFocus ? "退出专注标注" : "专注标注"}" title="${state.annotationFocus ? "恢复导航和侧栏" : "收起导航和侧栏，扩大画布"}">${icon("expand")}<span>${state.annotationFocus ? "退出专注" : "专注标注"}</span></button>
              <button class="btn ghost panel-toggle" id="toggle-inspector" aria-controls="annotation-inspector" aria-expanded="${!state.inspectorCollapsed}" aria-label="标注详情" title="展开或收起标注详情">${icon("layers")}<span>标注详情</span></button>
              <details class="annotation-more-actions"><summary class="btn ghost" aria-label="更多标注操作">更多</summary><div><button class="btn ghost" id="save-next" ${state.asset ? "" : "disabled"} title="下一张未提交或未审核图片 (N)">下一张待标注 <kbd>N</kbd></button><button class="btn ghost" data-review-picker ${state.assets.length ? "" : "disabled"}>批量提交与审核</button>${reviewContinuationMarkup(canReview())}<button class="btn ghost" id="export-data" title="快速导出当前工作数据；正式训练建议从数据版本导出">${icon("download")}快速导出</button></div></details>
            </div>
          </div>
        </div>

      <aside class="workspace-panel asset-panel" id="asset-queue">
        <div class="panel-header"><span class="panel-title">${state.activeWorkBlockId ? "本块图片" : "图片队列"}</span><span class="counter">${state.activeWorkBlockId ? state.assets.filter(asset => asset.status === "labeled").length : project.labeled_count || 0} / ${state.activeWorkBlockId ? state.assets.length : project.asset_count || 0}</span></div>
        <div class="asset-filter"><select id="annotation-filter" aria-label="图片队列筛选"><option value="all">全部图片</option><option value="unlabeled" ${state.assetFilter === "unlabeled" ? "selected" : ""}>未标注</option><option value="labeled" ${state.assetFilter === "labeled" ? "selected" : ""}>已标注</option><option value="submitted" ${state.assetFilter === "submitted" ? "selected" : ""}>待审核</option><option value="approved" ${state.assetFilter === "approved" ? "selected" : ""}>已通过</option><option value="rejected" ${state.assetFilter === "rejected" ? "selected" : ""}>已驳回</option></select></div>
        <div class="asset-list">${filteredAssets.length ? `${visibleAssets.map(assetItem).join("")}${remainingAssets ? `<button class="load-more-assets" id="load-more-assets">再显示 ${Math.min(200, remainingAssets)} 张 <small>剩余 ${remainingAssets.toLocaleString()} 张</small></button>` : ""}` : `<div class="empty-state" style="padding:45px 10px;border:0"><p>当前筛选没有图片</p></div>`}</div>
        <div class="asset-footer"><button class="btn ghost" id="upload-assets">${icon("upload")}上传数据集</button></div>
      </aside>
      <section class="canvas-panel">
        <div class="workspace-guidance" id="workspace-guidance" ${workspaceGuidanceVisible() ? "" : "hidden"}>${workspaceGuidanceMarkup()}</div>
        <div id="job-activity-slot">${jobActivityMarkup()}</div>
        <div class="annotation-canvas-body">
          <nav class="annotation-tool-rail" aria-label="标注工具">
            <button class="btn ghost manual-tool ${state.tool === "select" ? "active" : ""}" data-tool="select" title="选择和调整对象 (V)" aria-pressed="${state.tool === "select"}" aria-keyshortcuts="V">${icon("cursor")}<span>选择</span><kbd>V</kbd></button>
            ${annotationToolsMarkup(editable && Boolean(project.labels.length))}
            <button class="btn ghost canvas-hand-tool ${state.tool === "pan" ? "active" : ""}" data-tool="pan" title="移动画布 (H)，或按住空格拖动" aria-pressed="${state.tool === "pan"}" aria-keyshortcuts="H">${icon("hand")}<span>平移</span><kbd>H</kbd></button>
            <button class="btn ghost" id="ai-annotate" title="运行 AI 标注或 AI 辅助分割">${icon("magic")}<span>AI 标注</span></button>
            <button class="btn ghost" id="annotation-shortcuts" title="查看标注快捷键 (?)" aria-keyshortcuts="?">${icon("keyboard")}<span>快捷键</span></button>
          </nav>
          <div class="canvas-scroll" id="canvas-scroll" tabindex="0" aria-label="图片画布，可用滚轮缩放，空格或鼠标中键拖动" aria-describedby="canvas-navigation-help">${canvasMarkup()}</div>
        </div>
        <div class="annotation-canvas-footer">
          <div class="canvas-status"><span id="annotation-save-status" role="status">${saveFeedbackText()}</span><span id="canvas-status-hint">${canvasStatusHint()}</span><button id="toggle-annotations" class="canvas-visibility-toggle" title="只改变标注显示，不删除标注" aria-pressed="${state.showAnnotations}">${state.showAnnotations ? "隐藏标注" : "显示标注"}</button></div>
          <div class="canvas-view-tools"><span id="canvas-navigation-help">滚轮缩放 · 空格 + 拖动移动图片</span><div class="zoom-control" aria-label="图片显示比例"><button id="zoom-out" aria-label="缩小图片" title="缩小图片 (-)" ${state.asset ? "" : "disabled"}>−</button><output id="zoom-value" aria-label="当前图片显示比例">${Math.round(state.zoom * 100)}%</output><button id="zoom-in" aria-label="放大图片" title="放大图片 (+)" ${state.asset ? "" : "disabled"}>+</button><button id="fit-canvas" title="显示整张图片 (F)" ${state.asset ? "" : "disabled"}>适配窗口</button><button id="actual-size-canvas" title="按原始尺寸显示图片，不改变标注位置" ${state.asset ? "" : "disabled"}>100%</button></div></div>
        </div>
      </section>
      <aside class="workspace-panel inspector" id="annotation-inspector">
        <div class="panel-header">${annotationInspectorHeaderMarkup()}</div>
        <div class="inspector-tabs"><button data-tab="objects" class="${state.inspectorTab === "objects" ? "active" : ""}">对象</button><button data-tab="review" class="${state.inspectorTab === "review" ? "active" : ""}">审核</button><button data-tab="jobs" class="${state.inspectorTab === "jobs" ? "active" : ""}">任务 ${runningJobCount() ? `(${runningJobCount()})` : ""}</button><button data-tab="audit" class="${state.inspectorTab === "audit" ? "active" : ""}">操作记录</button></div>
        <div class="inspector-body">${inspectorMarkup()}</div>
      </aside>
    </main></div>`;
  bindWorkspaceEvents();
  bindCanvasEvents();
  drawDraft();
  const assetList = document.querySelector(".asset-list");
  if (assetList) assetList.scrollTop = state.assetListScroll;
}

function assetItem(asset) {
  return `<div class="asset-item ${state.asset?.id === asset.id ? "active" : ""}" data-asset-id="${asset.id}"><div class="thumb"><img loading="lazy" decoding="async" src="/api/assets/${asset.id}/thumbnail?size=160" onerror="this.onerror=null;this.src='/api/assets/${asset.id}/image'" />${asset.status === "labeled" ? `<span class="done">${icon("check")}</span>` : ""}</div><div><div class="asset-name">${escapeHtml(asset.filename)}</div><div class="asset-info">${asset.annotation_count} 个对象 · ${asset.width}×${asset.height}</div><span class="review-mini ${asset.review_status}">${reviewStatusName(asset.review_status)}</span>${asset.lock_owner && asset.lock_expires_at > new Date().toISOString() ? `<span class="collaborator-badge">${asset.lock_owner === (state.session?.actor || actorId) ? state.asset?.id === asset.id && state.lockOwned ? "我正在校正" : "我的其他页面正在校正" : `协作中 · ${escapeHtml(asset.lock_owner.split(":")[1] || asset.lock_owner)}`}</span>` : ""}</div></div>`;
}

function refreshAssetQueueItem(assetId) {
  const asset = state.assets.find(item => item.id === assetId);
  const row = document.querySelector(`[data-asset-id="${assetId}"]`);
  if (asset && row) {
    row.outerHTML = assetItem(asset);
    document.querySelector(`[data-asset-id="${assetId}"]`)?.addEventListener("click", () => selectAsset(assetId));
  }
  const counter = document.querySelector(".asset-panel .counter");
  if (counter) counter.textContent = state.activeWorkBlockId ? `${state.assets.filter(asset => asset.status === "labeled").length} / ${state.assets.length}` : `${state.project.labeled_count || 0} / ${state.project.asset_count || 0}`;
}

function reviewStatusName(status) {
  return { draft: "草稿", in_progress: "标注中", submitted: "待审核", approved: "已通过", rejected: "已驳回" }[status] || "未选择";
}

function saveFeedbackText() {
  if (state.savingAnnotation || state.saveFeedback.status === "saving") return "正在保存…";
  return { idle: "修改后自动保存", saved: "已保存", error: "保存未完成" }[state.saveFeedback.status] || "修改后自动保存";
}

function setSaveFeedback(status, message = "") {
  state.saveFeedback = { status, message };
  refreshCanvasStatus();
}

function workspaceGuidanceVisible() {
  return state.saveFeedback.status === "error" || !state.asset || !state.project?.labels.length
    || state.readOnly || ["submitted", "approved", "rejected"].includes(state.asset?.review_status)
    || Boolean(state.pendingAnnotation || state.pendingTaskEdit || state.pendingObjectEdit || state.advancedAnnotationDirty || state.poseDraft || state.polygon.length)
    || ["polygon", "keypoints"].includes(state.tool);
}

function refreshWorkspaceGuidance() {
  const guidance = document.querySelector("#workspace-guidance");
  if (!guidance) return;
  guidance.hidden = !workspaceGuidanceVisible();
  guidance.innerHTML = workspaceGuidanceMarkup();
}

function workspaceGuidanceMarkup() {
  if (!state.asset) return `<strong>先导入图片</strong><span>导入后可直接手动标注，也可使用 AI 生成初稿。</span>`;
  if (state.saveFeedback.status === "error") return `<strong class="feedback-error">修改未能确认保存</strong><span>${escapeHtml(state.saveFeedback.message || "请检查网络后重试，离开图片前确认修改已保存。")}</span>${state.pendingAnnotation || state.pendingTaskEdit || state.pendingObjectEdit ? `<button class="mini-action" id="retry-annotation-save" ${state.savingAnnotation ? "disabled" : ""}>重试保存 <kbd>Enter</kbd></button>` : ""}${hasAnnotationDraft() ? `<button class="mini-action" data-cancel-draft>放弃本地草稿</button>` : ""}<button class="mini-action" id="reload-saved-annotations">重新加载已保存结果</button>`;
  const status = state.asset.review_status;
  if (status === "submitted") return `<strong>等待审核</strong><span>${canReview() ? "检查类别和标注位置，确认无误后点击“审核通过”；有问题请退回并填写原因。" : "这张图片已经提交。审核员确认前不能修改；需要修改时可以撤回提交。"}</span>`;
  if (status === "approved") return `<strong>审核已通过</strong><span>${canReview() ? "可在“数据版本”生成训练数据。如需修改，重新打开后需要再次提交审核。" : "这张图片已通过审核。若发现问题，请联系审核员重新打开。"}</span>`;
  if (state.readOnly) return `<strong>暂时不能修改</strong><span>${escapeHtml(state.readOnlyReason || "编辑权限暂时不可用，请先处理下一张图片，稍后再回来。")}</span>`;
  if (!state.project.labels.length) return `<strong>先添加标注类别</strong><span>${isAdmin() ? "在标注详情中添加类别，再开始绘制。" : "请联系管理员为项目添加类别。"}</span>`;
  if (state.poseDraft) return `<strong>手动添加关键点</strong><span>${escapeHtml(canvasStatusHint())} · Backspace 返回上一个点 · Enter 完成 · Esc 取消</span><button class="mini-action" data-skip-draft-keypoint>跳过此点</button><button class="mini-action" data-finish-draft-keypoints>完成并保存</button><button class="mini-action" data-cancel-draft>取消绘制</button>`;
  if (state.polygon.length) return `<strong>正在绘制轮廓</strong><span>已添加 ${state.polygon.length / 2} 个顶点，至少需要 3 个。单击继续添加，Enter 完成，Backspace 撤销上一点，Esc 取消。</span><button class="mini-action" data-finish-polygon ${state.polygon.length < 6 || state.savingAnnotation ? "disabled" : ""}>完成轮廓</button><button class="mini-action" data-cancel-draft>取消绘制</button>`;
  if (state.advancedAnnotationDirty) return `<strong>高级修改尚未应用</strong><span>点击“应用高级修改”保存，或放弃本次输入。</span><button class="mini-action" data-cancel-draft>放弃本地草稿</button>`;
  if (state.selectionIntent && state.selectedAnnotation && projectTask() !== "classify") return `<strong>正在编辑选中对象</strong><span>修改“对象类别”或按数字键重新分类；按 Delete / ⌫ 删除选中标注。选择绘制工具或取消选中后可继续标注新对象。</span>`;
  if (status === "rejected") return `<strong>请按反馈修改</strong><span>${escapeHtml(state.asset.review_reason || "请查看审核记录中的修改要求。")} 修改后请再次提交审核。</span>`;
  const instruction = { detect: "选择类别，点击“矩形框”或按 R，在物体周围拖动框选。", segment: "选择类别，点击“多边形”或按 P，沿边缘逐点点击，按 Enter 完成。", pose: "选择类别，点击“关键点”或按 K，先拖动框选对象，再按顺序放置点。", classify: "在上方“图片类别”或右侧类别列表中选择，也可按类别对应的数字键。", mixed: "选择类别后，按 R 画框或按 P 逐点画轮廓。" }[projectTask()];
  return state.annotations.length ? `<strong>可以继续手动标注</strong><span>${escapeHtml(instruction)} 完成的标注自动保存，检查后提交审核。</span>` : `<strong>从零开始手动标注</strong><span>${escapeHtml(instruction)} 完成后自动保存。</span><button class="mini-action" data-start-manual>开始手动标注</button>`;
}

function workflowControlsMarkup() {
  if (!state.asset) return "";
  const status = state.asset.review_status;
  const busy = state.workflowBusy || state.savingAnnotation;
  const ownBlock = !state.activeWorkBlock || state.activeWorkBlock.is_mine;
  if (status === "approved" && state.activeWorkBlock?.status === "completed") return `<span class="hint">本块已完成${isAdmin() ? "，如需修改请到任务块重新打开" : "，只读"}</span>`;
  if (["draft", "in_progress", "rejected"].includes(status)) {
    const emptyAllowed = projectTask() !== "classify";
    const reason = state.saveFeedback.status === "error" ? "请先重试保存或重新加载结果" : hasAnnotationDraft() ? "请先完成当前草稿" : !ownBlock ? "仅任务负责人可提交" : !state.annotations.length && !(emptyAllowed && state.asset.negative_confirmed) ? emptyAllowed ? "请标注目标，或确认无目标" : "请先选择图片类别" : !state.lockOwned ? "当前图片暂不可编辑" : "";
    const emptyControl = emptyAllowed && !state.annotations.length && !state.asset.negative_confirmed && ownBlock ? `<button class="btn ghost workflow-action" data-action="confirm_empty" ${busy || !canEditCurrentAsset() || hasAnnotationDraft() || state.saveFeedback.status === "error" ? "disabled" : ""}>确认无目标</button>` : !state.annotations.length && state.asset.negative_confirmed ? `<span class="hint">已确认无目标</span>` : "";
    return `${emptyControl}<button class="btn ghost workflow-action" data-action="submit" ${busy || reason ? "disabled" : ""}>${state.workflowBusy ? "正在提交…" : "提交审核"}</button>${reason ? `<span class="workflow-reason">${reason}</span>` : ""}`;
  }
  if (status === "submitted" && !canReview()) return ownBlock ? `<button class="btn ghost workflow-action" data-action="reopen" ${busy ? "disabled" : ""}>撤回并修改</button>` : `<span class="hint">等待审核，只读</span>`;
  if (status === "submitted") return `<button class="btn primary workflow-action" data-action="approve" ${busy ? "disabled" : ""}>审核通过</button><button class="btn danger workflow-action" data-action="reject" ${busy ? "disabled" : ""}>退回修改</button><button class="btn ghost workflow-action" data-action="reopen" ${busy ? "disabled" : ""}>撤回审核</button>`;
  if (status === "approved" && !canReview()) return `<span class="hint">已通过审核，只读</span>`;
  if (status === "approved") return `<button class="btn ghost workflow-action" data-action="reopen" ${busy ? "disabled" : ""}>重新打开修改</button>`;
  return "";
}

function refreshWorkflowState() {
  refreshWorkspaceGuidance();
  const controls = document.querySelector("#workflow-controls");
  if (controls) {
    controls.innerHTML = workflowControlsMarkup();
    controls.querySelectorAll(".workflow-action").forEach((button) => {
      button.onclick = () => updateWorkflow(button.dataset.action);
    });
  }
  const badge = document.querySelector(".workspace-toolbar .review-badge");
  if (badge && state.asset) {
    badge.className = `review-badge ${state.asset.review_status}`;
    badge.textContent = reviewStatusName(state.asset.review_status);
  }
}

function canvasMarkup() {
  if (!state.asset) return `<div class="canvas-empty"><span class="card-icon">${icon("image")}</span><strong>上传图片开始标注</strong><p>支持 JPG、PNG、WEBP、BMP 和 ZIP 数据集</p><button class="btn primary" id="empty-upload">${icon("upload")}上传数据</button></div>`;
  const asset = state.asset;
  return `<div class="canvas-stage"><div class="canvas-frame" data-asset-id="${asset.id}">
    <svg id="annotation-canvas" class="canvas-svg tool-${state.tool}" viewBox="0 0 ${asset.width} ${asset.height}" xmlns="http://www.w3.org/2000/svg" aria-label="${escapeHtml(asset.filename)} 的图片和标注">
      <image href="/api/assets/${asset.id}/image" x="0" y="0" width="${asset.width}" height="${asset.height}" />
      <g id="shape-layer">${shapesMarkup()}</g><g id="draft-layer"></g>
    </svg></div></div>`;
}

function shapesMarkup() {
  if (!state.showAnnotations) return "";
  let selectedHandles = "";
  return state.annotations.filter(item => item.shape_type !== "classification").map((item) => {
    const isSelected = state.selectedAnnotation?.id === item.id;
    const selected = isSelected ? "selected" : "";
    const color = item.label_color;
    let shape;
    if (["rectangle", "keypoints"].includes(item.shape_type)) {
      const [x1, y1, x2, y2] = item.points;
      shape = `<rect class="shape ${selected}" data-annotation-id="${item.id}" x="${x1}" y="${y1}" width="${x2 - x1}" height="${y2 - y1}" fill="${color}" fill-opacity=".10" stroke="${color}" stroke-width="2" />`;
    } else {
      const points = item.points.reduce((pairs, value, index) => index % 2 ? pairs : [...pairs, `${value},${item.points[index + 1]}`], []).join(" ");
      shape = `<polygon class="shape ${selected}" data-annotation-id="${item.id}" points="${points}" fill="${color}" fill-opacity=".12" stroke="${color}" stroke-width="2" />`;
    }
    const x = Math.min(...item.points.filter((_, i) => i % 2 === 0));
    const y = Math.min(...item.points.filter((_, i) => i % 2 === 1));
    const screenScale = 1 / Math.max(0.005, state.zoom);
    const chipHeight = 22 * screenScale;
    const fontSize = 12 * screenScale;
    const width = Math.max(70, item.label_name.length * 13 + 20) * screenScale;
    let handles = "";
    if (isSelected && canEditCurrentAsset() && (state.tool === "select" || state.selectionIntent)) {
      const vertices = ["rectangle", "keypoints"].includes(item.shape_type)
        ? [[item.points[0], item.points[1]], [item.points[2], item.points[1]], [item.points[2], item.points[3]], [item.points[0], item.points[3]], [(item.points[0]+item.points[2])/2,item.points[1]], [item.points[2],(item.points[1]+item.points[3])/2], [(item.points[0]+item.points[2])/2,item.points[3]], [item.points[0],(item.points[1]+item.points[3])/2]]
        : item.points.reduce((out, value, index) => index % 2 ? out : [...out, [value, item.points[index + 1]]], []);
      const cursors = ["nwse", "nesw", "nwse", "nesw", "ns", "ew", "ns", "ew"];
      handles = vertices.map((point, index) => `<circle class="shape-handle ${["rectangle", "keypoints"].includes(item.shape_type) ? `resize-${cursors[index]}` : ""}" data-annotation-id="${item.id}" data-handle-index="${index}" cx="${point[0]}" cy="${point[1]}" r="${6 * screenScale}" fill="#fff" stroke="${color}" stroke-width="3" vector-effect="non-scaling-stroke"/>`).join("");
    }
    if (item.shape_type === "keypoints") handles += keypointShapesMarkup(item, isSelected);
    if (isSelected) selectedHandles = handles;
    return `${shape}<g class="label-chip" data-annotation-id="${item.id}" role="button" tabindex="0" aria-label="选择 ${escapeHtml(item.label_name)} 对象"><title>点击选择对象，右键可修改类别和边界</title><rect x="${x}" y="${Math.max(0, y - chipHeight)}" width="${width}" height="${chipHeight}" rx="${3 * screenScale}" fill="${color}"/><text x="${x + 7 * screenScale}" y="${Math.max(15 * screenScale, y - 7 * screenScale)}" font-size="${fontSize}" font-family="sans-serif" fill="#07100d">${escapeHtml(item.label_name)}</text></g>${isSelected ? "" : handles}`;
  }).join("") + `<g class="selected-shape-handles">${selectedHandles}</g>`;
}

function canEditCurrentAsset() {
  return Boolean(
    state.asset && state.lockOwned && !state.readOnly && !state.savingAnnotation && !state.workflowBusy
    && !state.legacyReviewAssetId && (!state.activeWorkBlock || state.activeWorkBlock.is_mine)
    && !["submitted", "approved"].includes(state.asset.review_status)
  );
}

function hasAnnotationDraft() {
  return Boolean(state.pendingAnnotation || state.pendingTaskEdit || state.pendingObjectEdit || state.advancedAnnotationDirty
    || state.drawing || state.shapeEdit || state.polygon.length || state.poseDraft || state.keypointPlacement != null);
}

function annotationContextGuard() {
  const projectId = state.project?.id, assetId = state.asset?.id, epoch = state.selectionEpoch, session = state.session;
  return () => state.currentSection === "annotate" && state.project?.id === projectId
    && state.asset?.id === assetId && state.selectionEpoch === epoch && state.session === session && state.authReady;
}

function canvasStatusHint() {
  if (state.pan) return "正在移动图片，松开鼠标结束；标注位置不会改变";
  if (state.tool === "pan") return "拖动图片查看细节 · 滚轮缩放 · V 返回标注校正";
  if (state.savingAnnotation) return "正在保存标注修改，请稍候…";
  if (state.asset?.review_status === "submitted") return canReview() ? "请检查结果，再通过或退回修改" : "已提交，等待审核员确认";
  if (state.asset?.review_status === "approved") return "审核已通过；需要修改时请先重新打开";
  if (state.readOnly) return state.readOnlyReason || "暂不可编辑，请先处理下一张";
  if (state.keypointPlacement != null) return "在画布点击定位选中的关键点";
  if (state.poseDraft) return `请定位：${state.project.keypoint_schema.names[state.poseDraft.nextIndex] || "保存对象"}`;
  if (state.tool === "keypoints") return "先拖动框选对象，再按名称顺序点击关键点";
  if (projectTask() === "classify") return "选择图片类别或按数字键 · 自动保存 · A / D 切换图片";
  if (projectTask() === "pose") return "拖动关键点校正 · K 补充对象 · Delete / ⌫ 删除";
  if (state.savingAnnotation) return "正在保存标注修改…";
  if (state.shapeEdit) return state.shapeEdit.handleIndex == null ? "正在移动对象，松开鼠标保存" : "正在调整边界点，松开鼠标保存";
  if (state.tool === "rectangle") return "十字线辅助定位 · 左键拖动绘制 · 右键选择已有对象";
  if (state.tool === "polygon") return `单击添加顶点（已有 ${state.polygon.length / 2} 个） · 点击起点/双击/Enter 完成 · Backspace 撤销顶点`;
  if (["positive", "negative"].includes(state.tool)) return `已添加 ${state.assistPoints.length} 个交互提示点 · Esc 清空`;
  if (state.selectedAnnotation && state.selectionIntent && canEditCurrentAsset()) return "数字键改类别 · Delete / ⌫ 删除 · Esc 取消选中";
  return "点击类别标签或按 V 选择对象，再修改或删除 · ? 查看快捷键";
}

function inspectorMarkup() {
  if (state.inspectorTab === "jobs") return jobsMarkup();
  if (state.inspectorTab === "audit") return auditMarkup();
  if (state.inspectorTab === "review") return reviewMarkup();
  const selected = state.selectedAnnotation;
  const editingSelected = state.selectionIntent && selected && projectTask() !== "classify";
  const pendingLabel = state.pendingObjectEdit && state.pendingObjectEdit.annotationId === selected?.id ? state.pendingObjectEdit.labelId : null;
  return `${editingSelected ? "" : taskInspectorMarkup()}<section class="inspector-section"><h3>${editingSelected ? "修改选中对象类别" : projectTask() === "classify" ? "选择图片类别" : "选择新对象类别"}</h3>${state.project.labels.map((label) => `<div class="label-row ${(editingSelected ? (pendingLabel || selected.label_id) : state.activeLabel?.id) === label.id ? "active" : ""}" data-label-id="${label.id}" role="button" tabindex="0" aria-label="选择类别 ${escapeHtml(label.name)}"><span class="swatch" style="background:${label.color}"></span><span>${escapeHtml(label.name)}</span>${label.shortcut ? `<span class="shortcut">${escapeHtml(label.shortcut)}</span>` : ""}${isAdmin() && state.project.labels.length > 1 ? `<button class="label-delete" data-delete-label="${label.id}" title="删除类别">×</button>` : ""}</div>`).join("")}${isAdmin() ? `<button class="btn ghost inline-add" id="add-label">${icon("plus")}添加类别</button>` : `<p class="hint">需要新类别？请联系管理员添加。</p>`}</section>
  <section class="inspector-section"><h3>当前图片 · ${state.annotations.length} 个对象</h3>${state.annotations.length ? state.annotations.map((item, index) => `<div class="annotation-row ${selected?.id === item.id ? "active" : ""}" data-annotation-id="${item.id}" role="button" tabindex="0" aria-label="选择对象 ${index + 1} ${escapeHtml(item.label_name)}"><span class="swatch" style="background:${item.label_color}"></span><span>${index + 1}. ${escapeHtml(item.label_name)}</span><span class="source-pill ${item.source}">${item.human_modified ? "已校正" : item.source === "manual" ? "人工" : item.source === "auto" ? "AI" : "辅助"}</span></div>`).join("") : `<p class="hint">还没有标注。选择画布左侧的工具即可开始；也可以使用 AI 标注。</p>`}</section>
  ${selected ? `<section class="inspector-section"><h3>对象属性</h3>${pendingLabel ? `<p class="manual-draft-error" role="status">类别尚未保存：${escapeHtml(state.project.labels.find(label => label.id === pendingLabel)?.name || "所选类别")}。请重试保存或重新加载核对。</p>` : ""}${canEditCurrentAsset() && selected.shape_type !== "classification" ? `<div class="direct-edit-hint">可在画布中直接拖动对象，或拖动白色控制点调整边界。</div>` : ""}<details class="technical-details"><summary>查看标注来源与置信度</summary><p class="hint">置信度表示 AI 对结果的把握程度，仍需人工检查。</p><div class="property-grid"><div class="property"><small>来源</small><strong>${selected.model_id ? escapeHtml(selected.model_id) : "人工"}</strong></div><div class="property"><small>置信度</small><strong>${selected.confidence == null ? "—" : `${Math.round(selected.confidence * 100)}%`}</strong></div></div></details><div class="field compact"><label>标签（选择后自动保存）</label><select id="annotation-label" ${canEditCurrentAsset() && !hasAnnotationDraft() ? "" : "disabled"}>${state.project.labels.map(label => `<option value="${label.id}" ${label.id === (pendingLabel || selected.label_id) ? "selected" : ""}>${escapeHtml(label.name)}</option>`).join("")}</select></div>${keypointInspectorMarkup(selected)}${structuredAttributesMarkup(selected)}<details class="advanced-coordinates advanced-settings"><summary>高级：精确坐标与自定义属性</summary><div class="field compact" ${selected.shape_type === "classification" ? "hidden" : ""}><label>坐标（像素，逗号分隔）</label><textarea id="annotation-points" rows="3">${escapeHtml(state.advancedDraft?.annotationId === selected.id ? state.advancedDraft.points : selected.points.join(", "))}</textarea></div><div class="field compact"><label>属性 JSON</label><textarea id="annotation-attributes" rows="4">${escapeHtml(state.advancedDraft?.annotationId === selected.id ? state.advancedDraft.attributes : JSON.stringify(selected.attributes || {}, null, 2))}</textarea></div><button class="btn primary" id="save-annotation" ${canEditCurrentAsset() ? "" : "disabled"}>应用高级修改</button></details></section>` : ""}`;
}

function structuredAttributesMarkup(annotation) {
  const label = state.project.labels.find(item => item.id === annotation.label_id);
  const properties = label?.attributes_schema?.properties || {};
  const entries = Object.entries(properties);
  if (!entries.length) return "";
  return `<div class="structured-attributes"><h4>结构化属性</h4>${entries.map(([name, rule]) => {
    const value = annotation.attributes?.[name] ?? rule.default ?? "";
    if (rule.enum) return `<label><span>${escapeHtml(name)}</span><select data-attribute-name="${escapeHtml(name)}" data-attribute-type="${escapeHtml(rule.type || "string")}" ${canEditCurrentAsset() ? "" : "disabled"}>${rule.enum.map(option => `<option value="${escapeHtml(JSON.stringify(option))}" ${option === value ? "selected" : ""}>${escapeHtml(String(option))}</option>`).join("")}</select></label>`;
    if (rule.type === "boolean") return `<label class="attribute-boolean"><input type="checkbox" data-attribute-name="${escapeHtml(name)}" data-attribute-type="boolean" ${value ? "checked" : ""} ${canEditCurrentAsset() ? "" : "disabled"}/><span>${escapeHtml(name)}</span></label>`;
    return `<label><span>${escapeHtml(name)}</span><input type="${["number", "integer"].includes(rule.type) ? "number" : "text"}" data-attribute-name="${escapeHtml(name)}" data-attribute-type="${escapeHtml(rule.type || "string")}" value="${escapeHtml(String(value))}" ${canEditCurrentAsset() ? "" : "disabled"}/></label>`;
  }).join("")}</div>`;
}

function reviewMarkup() {
  if (!state.asset) return `<section class="inspector-section"><p class="hint">选择图片后查看审核信息。</p></section>`;
  const reason = state.asset.review_reason;
  return `<section class="inspector-section"><h3>当前审核状态</h3><div class="review-summary"><span class="review-badge ${state.asset.review_status}">${reviewStatusName(state.asset.review_status)}</span><strong>${state.annotations.length} 个对象</strong></div>${reason ? `<div class="review-reason"><strong>最近反馈</strong><p>${escapeHtml(reason)}</p></div>` : `<p class="hint">当前没有审核反馈。</p>`}${workflowControlsMarkup()}</section><section class="inspector-section"><h3>审核历史</h3>${state.reviewEvents.length ? state.reviewEvents.map(event => `<div class="review-event"><span class="status-dot"></span><div><strong>${reviewStatusName(event.from_status)} → ${reviewStatusName(event.to_status)}</strong>${event.reason ? `<p>${escapeHtml(event.reason)}</p>` : ""}<small>${escapeHtml(event.actor)} · ${new Date(event.created_at).toLocaleString()}</small></div></div>`).join("") : `<p class="hint">暂无审核状态变更。</p>`}</section>`;
}

function auditMarkup() {
  const actionNames = { create: "创建", update: "更新", delete: "删除", upload: "上传", enqueue: "创建任务", complete: "任务完成", retry: "任务重试", fail: "任务失败", submit: "提交审核", approve: "审核通过", reject: "审核驳回", reopen: "重新打开", cancel: "取消任务" };
  return `<section class="inspector-section"><h3>最近审计事件</h3>${state.auditLogs.length ? state.auditLogs.map(row => `<div class="audit-row"><div><strong>${escapeHtml(actionNames[row.action] || row.action)}</strong><span>${escapeHtml(row.resource_type)} #${escapeHtml(row.resource_id)}</span></div><small>${escapeHtml(row.actor)} · ${new Date(row.created_at).toLocaleString()}</small></div>`).join("") : `<p class="hint">当前项目暂无审计事件。</p>`}</section>`;
}

function jobsMarkup() {
  return `<section class="inspector-section"><h3>AI 标注任务</h3>${state.jobs.length ? state.jobs.map((job) => {
    const progress = job.total ? Math.round(job.processed / job.total * 100) : 0;
    const model = state.models.find(m => m.id === job.model_id);
    const statusName = { queued: "排队中", running: "运行中", completed: "已完成", failed: "失败", cancelled: "已取消" }[job.status] || job.status;
    const targets = job.settings?.target_labels || [];
    return `<div class="job-row"><div class="job-head"><strong>${escapeHtml(model?.name || job.model_id)}</strong><span class="job-status ${job.status}">${statusName}</span></div>${targets.length ? `<div class="job-targets">目标：${targets.map(escapeHtml).join("、")}</div>` : ""}<div class="card-meta" style="padding:0;border:0"><span>${job.processed}/${job.total || "?"} 图片 · ${job.created_count} 标注</span><span>${progress}%</span></div><div class="progress-mini" style="width:100%;margin-top:7px"><span style="width:${progress}%"></span></div>${job.progress_message ? `<div class="hint">${escapeHtml(job.progress_message)}</div>` : ""}${job.error ? `<div class="job-error">${escapeHtml(job.error)}</div>` : ""}<div class="job-actions">${["queued", "running"].includes(job.status) ? `<button class="mini-action" data-cancel-job="${job.id}">取消</button>` : ""}${["failed", "cancelled"].includes(job.status) ? `<button class="mini-action" data-retry-job="${job.id}">重试</button>` : ""}</div></div>`;
  }).join("") : `<p class="hint">还没有任务。点击画布左侧的“AI 标注”选择模型并创建任务。</p>`}</section>`;
}

function jobActivityMarkup() {
  const job = state.jobs.find(item => ["running", "queued"].includes(item.status));
  if (!job) return "";
  const progress = job.total ? Math.round(job.processed / job.total * 100) : 0;
  const queuedWithoutWorker = job.status === "queued" && state.health?.annotation_workers?.status !== "online";
  const title = queuedWithoutWorker ? "等待标注服务就绪" : job.status === "queued" ? "AI 标注任务已排队" : "AI 正在标注";
  const message = queuedWithoutWorker
    ? "任务已保存。请联系管理员启动标注服务，服务就绪后将继续处理。"
    : job.progress_message || "正在准备模型服务";
  return `<div class="job-activity ${job.status}"><span class="job-activity-icon">${icon("magic")}</span><div class="job-activity-copy"><strong>${title}</strong><span>${escapeHtml(message)} · ${job.processed}/${job.total || "?"} 张图片 · ${job.created_count} 个标注</span></div><div class="job-activity-progress"><span>${progress}%</span><div class="progress-mini"><span style="width:${progress}%"></span></div></div><button class="mini-action" id="open-job-inspector">查看任务</button></div>`;
}

function refreshJobActivity() {
  const slot = document.querySelector("#job-activity-slot");
  if (slot) slot.innerHTML = jobActivityMarkup();
  document.querySelector("#open-job-inspector")?.addEventListener("click", () => {
    state.inspectorTab = "jobs";
    renderWorkspace();
  });
  const jobsTab = document.querySelector('[data-tab="jobs"]');
  if (jobsTab) jobsTab.textContent = `任务 ${runningJobCount() ? `(${runningJobCount()})` : ""}`;
}

function bindWorkspaceEvents() {
  revealActiveProjectTab();
  bindReviewPage({ state, api, jsonOptions, openModal, closeModal, notify, escapeHtml, renderDataPage, canReview, renderAiPage, renderWorkspace, loadAnnotations, refreshWorkflowState, refreshInspector });
  document.querySelector("#toggle-queue").onclick = () => toggleAnnotationPanel("queue");
  document.querySelector("#toggle-inspector").onclick = () => toggleAnnotationPanel("inspector");
  document.querySelector("#toggle-focus").onclick = toggleAnnotationFocus;
  document.querySelector("#upload-assets")?.addEventListener("click", openUploadModal);
  document.querySelector("#upload-assets-top")?.addEventListener("click", openUploadModal);
  document.querySelector("#empty-upload")?.addEventListener("click", openUploadModal);
  document.querySelector("#ai-annotate").onclick = openAiModal;
  document.querySelector("#open-job-inspector")?.addEventListener("click", () => { state.inspectorTab = "jobs"; renderWorkspace(); });
  document.querySelector("#export-data").onclick = openExportModal;
  const assetList = document.querySelector(".asset-list");
  assetList?.addEventListener("scroll", () => { state.assetListScroll = assetList.scrollTop; }, { passive: true });
  document.querySelector("#annotation-filter")?.addEventListener("change", (event) => { state.assetFilter = event.target.value; state.assetRenderLimit = 200; state.assetListScroll = 0; renderWorkspace(); });
  document.querySelector("#load-more-assets")?.addEventListener("click", () => { state.assetRenderLimit += 200; renderWorkspace(); });
  document.querySelectorAll("[data-tool]").forEach((button) => button.onclick = () => setTool(button.dataset.tool));
  document.querySelector("#annotation-shortcuts")?.addEventListener("click", openAnnotationShortcutsHelp);
  document.querySelector("#manual-label")?.addEventListener("change", event => { void chooseNewAnnotationLabel(Number(event.target.value)); });
  document.querySelectorAll("[data-asset-id]").forEach((row) => row.onclick = () => selectAsset(Number(row.dataset.assetId)));
  document.querySelectorAll("[data-label-id]").forEach((row) => row.onclick = () => { void chooseWorkspaceLabel(Number(row.dataset.labelId)); });
  bindLabelDeleteButtons();
  document.querySelectorAll(".annotation-row").forEach((row) => row.onclick = () => selectAnnotation(Number(row.dataset.annotationId)));
  document.querySelectorAll("[data-tab]").forEach((button) => button.onclick = async () => {
    state.inspectorTab = button.dataset.tab;
    if (state.inspectorTab === "audit") {
      try { state.auditLogs = await api(`/api/projects/${state.project.id}/audit-logs?limit=100`); }
      catch (error) { notify(error.message, "error"); }
    }
    if (state.inspectorTab === "review" && state.asset) {
      try { state.reviewEvents = await api(`/api/assets/${state.asset.id}/review-events`); }
      catch (error) { notify(error.message, "error"); }
    }
    renderWorkspace();
  });
  document.querySelector("#add-label")?.addEventListener("click", openLabelModal);
  refreshAnnotationDeleteControls();
  bindInspectorObjectActions();
  document.querySelector("#save-annotation")?.addEventListener("click", saveSelectedAnnotation);
  document.querySelector("#annotation-label")?.addEventListener("change", event => { void changeObjectLabel(state.selectedAnnotation?.id, Number(event.target.value)); });
  bindStructuredAttributeEvents();
  bindTaskInspector();
  document.querySelectorAll(".workflow-action").forEach((button) => button.onclick = () => updateWorkflow(button.dataset.action));
  document.querySelectorAll("[data-cancel-job]").forEach(button => button.onclick = () => cancelJob(Number(button.dataset.cancelJob)));
  document.querySelectorAll("[data-retry-job]").forEach(button => button.onclick = () => retryJob(Number(button.dataset.retryJob)));
  document.querySelector("#zoom")?.addEventListener("input", (event) => {
    setCanvasZoom(Number(event.target.value) / 100);
  });
  document.querySelector("#fit-canvas")?.addEventListener("click", fitCanvas);
  document.querySelector("#actual-size-canvas")?.addEventListener("click", () => setCanvasZoom(1));
  document.querySelector("#zoom-in")?.addEventListener("click", () => setCanvasZoom(state.zoom * 1.25));
  document.querySelector("#zoom-out")?.addEventListener("click", () => setCanvasZoom(state.zoom / 1.25));
  document.querySelector("#toggle-annotations")?.addEventListener("click", () => { state.showAnnotations = !state.showAnnotations; refreshShapes(); const button = document.querySelector("#toggle-annotations"); if (button) { button.textContent = state.showAnnotations ? "隐藏标注" : "显示标注"; button.setAttribute("aria-pressed", String(state.showAnnotations)); } });
  document.querySelector("#previous-asset")?.addEventListener("click", () => navigateAsset(-1));
  document.querySelector("#next-asset")?.addEventListener("click", () => navigateAsset(1));
  document.querySelector("#save-next")?.addEventListener("click", () => navigateAsset(1, true));
  bindCanvasViewportEvents();
}

function bindStructuredAttributeEvents() {
  document.querySelectorAll("#annotation-points, #annotation-attributes").forEach(input => input.addEventListener("input", () => {
    state.advancedAnnotationDirty = true;
    state.advancedDraft = { annotationId: state.selectedAnnotation?.id, points: document.querySelector("#annotation-points")?.value || "", attributes: document.querySelector("#annotation-attributes")?.value || "" };
    refreshCanvasStatus(); refreshWorkflowState();
  }));
  document.querySelectorAll("[data-attribute-name]").forEach(input => input.addEventListener("change", async () => {
    const attributes = { ...(state.selectedAnnotation?.attributes || {}) };
    document.querySelectorAll("[data-attribute-name]").forEach(field => {
      const type = field.dataset.attributeType;
      let value = type === "boolean" ? field.checked : field.value;
      if (field.tagName === "SELECT") { try { value = JSON.parse(field.value); } catch (_) { /* string enum */ } }
      else if (["number", "integer"].includes(type)) value = Number(field.value);
      attributes[field.dataset.attributeName] = value;
    });
    const raw = document.querySelector("#annotation-attributes");
    if (raw) raw.value = JSON.stringify(attributes, null, 2);
    await saveSelectedAnnotation();
  }));
}

function canvasFitZoom(scroll = document.querySelector("#canvas-scroll")) {
  if (!scroll || !state.asset) return 1;
  return Math.max(0.005, Math.min((scroll.clientWidth - 48) / state.asset.width, (scroll.clientHeight - 48) / state.asset.height, 1));
}

function rememberCanvasView() {
  const scroll = document.querySelector("#canvas-scroll");
  const frame = scroll?.querySelector(".canvas-frame");
  if (!canvasView || !scroll || frame?.dataset.assetId !== String(canvasView.assetId)) return;
  // A resize can clamp scroll offsets before ResizeObserver updates the stage.
  // Keep the last valid image center until the new viewport has been applied.
  if (canvasView.viewport && (canvasView.viewport.width !== scroll.clientWidth || canvasView.viewport.height !== scroll.clientHeight)) return;
  canvasView.center = {
    x: scroll.scrollLeft / canvasView.zoom,
    y: scroll.scrollTop / canvasView.zoom,
  };
}

function applyCanvasView(imagePoint = canvasView?.center, viewportPoint = null) {
  const scroll = document.querySelector("#canvas-scroll");
  const frame = scroll?.querySelector(".canvas-frame");
  const stage = scroll?.querySelector(".canvas-stage");
  if (!scroll || !frame || !stage || !state.asset || !canvasView || !imagePoint) return;
  const width = scroll.clientWidth, height = scroll.clientHeight;
  const scaledWidth = state.asset.width * canvasView.zoom;
  const scaledHeight = state.asset.height * canvasView.zoom;
  // Half a viewport of space at every edge lets each image corner reach the center.
  stage.style.width = `${scaledWidth + width}px`;
  stage.style.height = `${scaledHeight + height}px`;
  frame.style.width = `${scaledWidth}px`;
  frame.style.height = `${scaledHeight}px`;
  frame.style.left = `${width / 2}px`;
  frame.style.top = `${height / 2}px`;
  scroll.scrollLeft = width / 2 + imagePoint.x * canvasView.zoom - (viewportPoint?.x ?? width / 2);
  scroll.scrollTop = height / 2 + imagePoint.y * canvasView.zoom - (viewportPoint?.y ?? height / 2);
  state.zoom = canvasView.zoom;
  canvasView.viewport = { width, height };
  rememberCanvasView();
  const value = document.querySelector("#zoom-value");
  if (value) value.textContent = `${state.zoom < 0.1 ? (state.zoom * 100).toFixed(1) : Math.round(state.zoom * 100)}%`;
  const fitButton = document.querySelector("#fit-canvas");
  if (fitButton) fitButton.setAttribute("aria-pressed", String(canvasView.fit));
  const actualButton = document.querySelector("#actual-size-canvas");
  if (actualButton) actualButton.setAttribute("aria-pressed", String(Math.abs(state.zoom - 1) < 0.001));
  scroll.classList.toggle("pan-ready", state.tool === "pan" || state.spacePressed);
  canvasGuides?.refresh();
}

function setCanvasZoom(zoom, anchor = null) {
  const scroll = document.querySelector("#canvas-scroll");
  const frame = scroll?.querySelector(".canvas-frame");
  if (!scroll || !frame || !state.asset || !canvasView) return;
  const viewportRect = scroll.getBoundingClientRect();
  const frameRect = frame.getBoundingClientRect();
  const viewportPoint = anchor
    ? { x: anchor.clientX - viewportRect.left - scroll.clientLeft, y: anchor.clientY - viewportRect.top - scroll.clientTop }
    : { x: scroll.clientWidth / 2, y: scroll.clientHeight / 2 };
  const imagePoint = {
    x: ((anchor?.clientX ?? viewportRect.left + scroll.clientLeft + viewportPoint.x) - frameRect.left) / canvasView.zoom,
    y: ((anchor?.clientY ?? viewportRect.top + scroll.clientTop + viewportPoint.y) - frameRect.top) / canvasView.zoom,
  };
  canvasView.zoom = Math.max(Math.min(0.02, canvasFitZoom(scroll)), Math.min(8, zoom));
  canvasView.fit = false;
  applyCanvasView(imagePoint, viewportPoint);
  refreshShapes(); drawDraft();
}

function fitCanvas() {
  const scroll = document.querySelector("#canvas-scroll");
  if (!state.asset || !scroll) return;
  canvasView = { assetId: state.asset.id, zoom: canvasFitZoom(scroll), fit: true, center: { x: state.asset.width / 2, y: state.asset.height / 2 } };
  applyCanvasView();
  refreshShapes(); drawDraft();
}

function isCanvasPanGesture(event) {
  return event.button === 1 || (event.button === 0 && (state.spacePressed || state.tool === "pan"));
}

function stopCanvasPan() {
  const scroll = document.querySelector("#canvas-scroll");
  const pointerId = state.pan?.pointerId;
  state.pan = null;
  if (pointerId != null && scroll?.hasPointerCapture(pointerId)) scroll.releasePointerCapture(pointerId);
  scroll?.classList.remove("panning");
  scroll?.classList.toggle("pan-ready", state.tool === "pan" || state.spacePressed);
  rememberCanvasView(); refreshCanvasStatus();
}

function bindCanvasViewportEvents() {
  const scroll = document.querySelector("#canvas-scroll");
  if (!scroll) return;
  canvasObserver?.disconnect();
  if (!state.asset) { canvasView = null; return; }
  state.pan = null;
  if (!canvasView || canvasView.assetId !== state.asset.id) {
    canvasView = { assetId: state.asset.id, zoom: canvasFitZoom(scroll), fit: true, center: { x: state.asset.width / 2, y: state.asset.height / 2 } };
  }
  if (canvasView.fit) canvasView.zoom = canvasFitZoom(scroll);
  applyCanvasView();
  let lastSize = `${scroll.clientWidth}:${scroll.clientHeight}`;
  canvasObserver = new ResizeObserver(() => {
    const nextSize = `${scroll.clientWidth}:${scroll.clientHeight}`;
    if (nextSize === lastSize || !scroll.isConnected || !canvasView) return;
    lastSize = nextSize;
    if (canvasView.fit) canvasView.zoom = canvasFitZoom(scroll);
    applyCanvasView(); refreshShapes(); drawDraft();
  });
  canvasObserver.observe(scroll);
  scroll.addEventListener("scroll", rememberCanvasView, { passive: true });
  scroll.addEventListener("wheel", event => {
    if (!state.asset) return;
    event.preventDefault();
    if (state.shapeEdit || state.drawing || state.pan) return;
    const pixels = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? scroll.clientHeight : 1);
    if (!pixels) return;
    setCanvasZoom(state.zoom * Math.exp(-Math.max(-240, Math.min(240, pixels)) * 0.0025), event);
  }, { passive: false });
  // Capture before SVG shapes/drawing tools; viewing must never mutate an annotation.
  scroll.addEventListener("pointerdown", event => {
    if (!isCanvasPanGesture(event) || state.pan) return;
    event.preventDefault(); event.stopPropagation();
    if (state.savingAnnotation || state.workflowBusy || annotationOverlayOpen()) return;
    if (state.shapeEdit || state.drawing && !state.drawing.complete) return;
    scroll.focus({ preventScroll: true });
    state.pan = { x: event.clientX, y: event.clientY, left: scroll.scrollLeft, top: scroll.scrollTop, pointerId: event.pointerId };
    scroll.setPointerCapture(event.pointerId); scroll.classList.add("panning");
    refreshCanvasStatus();
  }, { capture: true });
  scroll.addEventListener("pointermove", event => {
    if (!state.pan || state.pan.pointerId !== event.pointerId) return;
    event.preventDefault(); event.stopPropagation();
    scroll.scrollLeft = state.pan.left - (event.clientX - state.pan.x);
    scroll.scrollTop = state.pan.top - (event.clientY - state.pan.y);
  }, { capture: true });
  const stop = event => {
    if (!state.pan || state.pan.pointerId !== event.pointerId) return;
    event.preventDefault(); event.stopPropagation(); stopCanvasPan();
  };
  scroll.addEventListener("pointerup", stop, { capture: true });
  scroll.addEventListener("pointercancel", stop, { capture: true });
  scroll.addEventListener("lostpointercapture", event => { if (state.pan?.pointerId === event.pointerId) stopCanvasPan(); });
  scroll.addEventListener("auxclick", event => { if (event.button === 1) event.preventDefault(); });
}
function applyAnnotationLayout() {
  rememberCanvasView();
  const shell = document.querySelector(".annotation-shell"), workspace = document.querySelector(".annotation-workspace");
  if (!shell || !workspace) return;
  shell.classList.toggle("is-focus", state.annotationFocus);
  workspace.classList.toggle("queue-collapsed", state.queueCollapsed);
  workspace.classList.toggle("inspector-collapsed", state.inspectorCollapsed);
  document.querySelector("#toggle-queue")?.setAttribute("aria-expanded", String(!state.queueCollapsed));
  document.querySelector("#toggle-inspector")?.setAttribute("aria-expanded", String(!state.inspectorCollapsed));
  const focus = document.querySelector("#toggle-focus");
  if (focus) {
    focus.setAttribute("aria-pressed", String(state.annotationFocus));
    focus.setAttribute("aria-label", state.annotationFocus ? "退出专注标注" : "专注标注");
    focus.title = state.annotationFocus ? "恢复导航和侧栏" : "收起导航和侧栏，扩大画布";
    focus.querySelector("span").textContent = state.annotationFocus ? "退出专注" : "专注标注";
  }
  document.querySelector("#canvas-scroll")?.focus({ preventScroll: true });
}

function toggleAnnotationPanel(panel) {
  const key = panel === "queue" ? "queueCollapsed" : "inspectorCollapsed";
  state[key] = !state[key];
  if (window.innerWidth < 760 && !state[key]) state[panel === "queue" ? "inspectorCollapsed" : "queueCollapsed"] = true;
  applyAnnotationLayout();
}

function toggleAnnotationFocus() {
  if (!state.annotationFocus) {
    closeWorkspaceNavigation();
    state.annotationPanelRestore = { queueCollapsed: state.queueCollapsed, inspectorCollapsed: state.inspectorCollapsed };
    state.queueCollapsed = true; state.inspectorCollapsed = true; state.annotationFocus = true;
  } else {
    state.annotationFocus = false;
    if (state.annotationPanelRestore) Object.assign(state, state.annotationPanelRestore);
    state.annotationPanelRestore = null;
  }
  applyAnnotationLayout();
}

function workspaceAssets() {
  return state.assets.filter(asset => state.assetFilter === "all" ||
    (["unlabeled", "labeled"].includes(state.assetFilter) ? asset.status === state.assetFilter : asset.review_status === state.assetFilter));
}

async function navigateAsset(offset, skipReviewed = false) {
  if (!state.asset || state.savingAnnotation || state.workflowBusy) return;
  const currentIndex = state.assets.findIndex(asset => asset.id === state.asset.id);
  const candidates = new Set(workspaceAssets().filter(asset => !skipReviewed || !["submitted", "approved"].includes(asset.review_status)).map(asset => asset.id));
  let index = currentIndex + offset;
  while (index >= 0 && index < state.assets.length) {
    if (candidates.has(state.assets[index].id)) { await selectAsset(state.assets[index].id); return; }
    index += offset;
  }
  notify(offset > 0 ? "当前筛选已到最后一张" : "当前筛选已到第一张");
}

function bindCanvasEvents() {
  const svg = document.querySelector("#annotation-canvas");
  if (!svg) return;
  bindShapePointerEvents(svg);
  svg.addEventListener("pointerdown", onCanvasPointerDown);
  svg.addEventListener("pointermove", onCanvasPointerMove);
  svg.addEventListener("pointerup", onCanvasPointerUp);
  svg.addEventListener("pointercancel", () => { if (state.shapeEdit) cancelShapeEdit(); else cancelDrawing(); });
  svg.addEventListener("lostpointercapture", event => { if (state.shapeEdit?.pointerId === event.pointerId) cancelShapeEdit(); });
  svg.addEventListener("dblclick", onCanvasDoubleClick);
  svg.addEventListener("contextmenu", onCanvasContextMenu);
  canvasGuides?.destroy();
  canvasGuides = mountCanvasGuides({ state, canEditCurrentAsset, isContextMenuOpen: () => annotationOverlayOpen() || navigationIsOpen() });
}

function bindShapePointerEvents(root = document) {
  root.querySelectorAll(".shape").forEach((shape) => shape.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || isCanvasPanGesture(event) || state.tool !== "select" || state.keypointPlacement != null) return;
    event.stopPropagation(); startShapeEdit(event, Number(shape.dataset.annotationId), shape.dataset.handleIndex === undefined ? null : Number(shape.dataset.handleIndex));
  }));
  root.querySelectorAll(".shape-handle").forEach((handle) => handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || isCanvasPanGesture(event) || (state.tool !== "select" && !state.selectionIntent) || state.keypointPlacement != null) return;
    event.stopPropagation(); startShapeEdit(event, Number(handle.dataset.annotationId), Number(handle.dataset.handleIndex));
  }));
  root.querySelectorAll(".label-chip").forEach(chip => chip.addEventListener("pointerdown", event => {
    if (event.button !== 0 || isCanvasPanGesture(event)) return;
    // An unfinished drawing can cross existing labels without losing vertices.
    if (hasAnnotationDraft()) return;
    event.preventDefault(); event.stopPropagation();
    selectAnnotation(Number(chip.dataset.annotationId));
    document.querySelector("#canvas-scroll")?.focus({ preventScroll: true });
  }));
}

function svgPoint(event) {
  const svg = document.querySelector("#annotation-canvas");
  const point = svg.createSVGPoint(); point.x = event.clientX; point.y = event.clientY;
  const transformed = point.matrixTransform(svg.getScreenCTM().inverse());
  return { x: Math.max(0, Math.min(state.asset.width, transformed.x)), y: Math.max(0, Math.min(state.asset.height, transformed.y)) };
}

function onCanvasPointerDown(event) {
  if (event.button !== 0 || event.isPrimary === false || state.pan || isCanvasPanGesture(event) || annotationOverlayOpen()) return;
  if (state.savingAnnotation || state.workflowBusy) return;
  if ((state.pendingAnnotation && !state.poseDraft) || state.pendingTaskEdit || state.pendingObjectEdit || state.advancedAnnotationDirty) return notify("请先保存当前修改，或取消未保存草稿。", "warning");
  event.preventDefault();
  document.querySelector("#canvas-scroll")?.focus({ preventScroll: true });
  if (!state.activeLabel) return notify("请先选择标签", "error");
  if (["rectangle", "polygon", "keypoints", "positive", "negative"].includes(state.tool) && (state.readOnly || !state.lockOwned || ["submitted", "approved"].includes(state.asset?.review_status))) return notify("当前图片不可编辑", "error");
  const point = svgPoint(event);
  if (canEditCurrentAsset() && poseCanvasClick(point)) return;
  if (state.tool === "select") { clearObjectSelection(); return; }
  if (["rectangle", "polygon", "keypoints"].includes(state.tool) && !state.poseDraft && !state.polygon.length) {
    state.selectedAnnotation = null; state.selectionIntent = false; refreshShapes(); refreshInspector();
  }
  if (state.tool === "positive" || state.tool === "negative") {
    state.assistPoints.push({ x: Number(point.x.toFixed(2)), y: Number(point.y.toFixed(2)), label: state.tool === "positive" ? 1 : 0 });
    drawDraft(); return;
  }
  if (["rectangle", "keypoints"].includes(state.tool)) {
    state.drawing = { start: point, current: point, pointerId: event.pointerId, labelId: state.activeLabel.id }; event.currentTarget.setPointerCapture(event.pointerId); drawDraft();
  } else if (state.tool === "polygon") {
    if (state.polygon.length >= 6) {
      const scale = Math.max(Math.abs(event.currentTarget.getScreenCTM()?.a || 1), 0.01);
      const closeTolerance = 10 / scale;
      if (Math.hypot(point.x - state.polygon[0], point.y - state.polygon[1]) <= closeTolerance) {
        void finishPolygon(); return;
      }
    }
    state.polygon.push(point.x, point.y); drawDraft(point);
  }
}

function onCanvasPointerMove(event) {
  if (state.pan) return;
  if (state.shapeEdit) { if (state.shapeEdit.pointerId === event.pointerId) updateShapeEdit(svgPoint(event)); return; }
  if (["rectangle", "keypoints"].includes(state.tool) && state.drawing && !state.drawing.complete && state.drawing.pointerId === event.pointerId) { state.drawing.current = svgPoint(event); drawDraft(); }
  else if (state.tool === "polygon" && state.polygon.length) drawDraft(svgPoint(event));
}

async function onCanvasPointerUp(event) {
  if (state.pan || event.button !== 0) return;
  if (state.shapeEdit) { if (state.shapeEdit.pointerId === event.pointerId) await finishShapeEdit(event); return; }
  if (!["rectangle", "keypoints"].includes(state.tool) || !state.drawing) return;
  if (state.savingAnnotation || state.drawing.complete || state.drawing.pointerId !== event.pointerId) return;
  const draft = state.drawing;
  draft.current = svgPoint(event);
  const { start, current } = draft; draft.complete = true; drawDraft();
  const points = [Math.min(start.x, current.x), Math.min(start.y, current.y), Math.max(start.x, current.x), Math.max(start.y, current.y)];
  if (points[2] - points[0] < 4 || points[3] - points[1] < 4) { state.drawing = null; drawDraft(); return; }
  if (state.tool === "keypoints") {
    state.drawing = null; state.poseDraft = {points, labelId:draft.labelId, nextIndex:0, keypoints:state.project.keypoint_schema.names.map(()=>({x:0,y:0,visibility:0}))}; drawDraft(); refreshInspector(); return;
  }
  const guard = annotationContextGuard();
  const saved = await saveAnnotation("rectangle", points, [], draft.labelId);
  if (saved && guard() && state.drawing === draft) { state.drawing = null; drawDraft(); refreshWorkflowState(); }
}

function onCanvasDoubleClick(event) {
  if (state.spacePressed || state.pan || state.tool === "pan") return;
  if (state.tool !== "polygon" || state.polygon.length < 6) return;
  event.preventDefault();
  // The second pointerdown of a double click adds the same final point twice.
  if (state.polygon.length >= 8) state.polygon.splice(-2, 2);
  void finishPolygon();
}

function startShapeEdit(event, annotationId, handleIndex) {
  if (event.button !== 0 || event.isPrimary === false || state.pan || isCanvasPanGesture(event) || annotationOverlayOpen()) return;
  event.preventDefault();
  const annotation = state.annotations.find(item => item.id === annotationId);
  if (!annotation) return;
  if (state.savingAnnotation || state.workflowBusy) return;
  if (hasAnnotationDraft()) return notify("请先完成或取消当前草稿，再调整对象。", "warning");
  state.selectedAnnotation = annotation; state.selectionIntent = true;
  document.querySelector("#canvas-scroll")?.focus({ preventScroll: true });
  if (!canEditCurrentAsset()) { refreshShapes(); refreshInspector(); return; }
  const svg = document.querySelector("#annotation-canvas");
  svg?.setPointerCapture(event.pointerId);
  state.shapeEdit = {
    annotationId,
    handleIndex,
    pointerId: event.pointerId,
    start: svgPoint(event),
    zoom: state.zoom,
    original: [...annotation.points],
    originalKeypoints: (annotation.keypoints || []).map(point=>({...point})),
    version: annotation.version,
    moved: false,
  };
  refreshShapes(); refreshInspector(); refreshCanvasStatus();
}

function updateShapeEdit(point) {
  const edit = state.shapeEdit;
  const annotation = state.annotations.find(item => item.id === edit?.annotationId);
  if (!edit || !annotation) return;
  if (!edit.moved && Math.hypot(point.x - edit.start.x, point.y - edit.start.y) * edit.zoom < 3) return;
  let points = [...edit.original];
  if (edit.handleIndex >= 1000) {
    annotation.keypoints[edit.handleIndex-1000] = {x:Number(point.x.toFixed(2)),y:Number(point.y.toFixed(2)),visibility:annotation.keypoints[edit.handleIndex-1000].visibility || 2};
    edit.moved=true; refreshShapes(); refreshCanvasStatus(); return;
  }
  if (edit.handleIndex == null) {
    const xs = [...edit.original.filter((_, index) => index % 2 === 0), ...edit.originalKeypoints.filter(point=>point.visibility).map(point=>point.x)];
    const ys = [...edit.original.filter((_, index) => index % 2 === 1), ...edit.originalKeypoints.filter(point=>point.visibility).map(point=>point.y)];
    const requestedX = point.x - edit.start.x;
    const requestedY = point.y - edit.start.y;
    const dx = Math.max(-Math.min(...xs), Math.min(state.asset.width - Math.max(...xs), requestedX));
    const dy = Math.max(-Math.min(...ys), Math.min(state.asset.height - Math.max(...ys), requestedY));
    points = edit.original.map((value, index) => value + (index % 2 ? dy : dx));
    annotation.keypoints = edit.originalKeypoints.map(point=>point.visibility?{...point,x:point.x+dx,y:point.y+dy}:{...point});
  } else if (["rectangle","keypoints"].includes(annotation.shape_type)) {
    const [x1, y1, x2, y2] = edit.original;
    const minimumSize = 4;
    if ([0, 3, 7].includes(edit.handleIndex)) points[0] = Math.min(point.x, x2 - minimumSize);
    if ([1, 2, 5].includes(edit.handleIndex)) points[2] = Math.max(point.x, x1 + minimumSize);
    if ([0, 1, 4].includes(edit.handleIndex)) points[1] = Math.min(point.y, y2 - minimumSize);
    if ([2, 3, 6].includes(edit.handleIndex)) points[3] = Math.max(point.y, y1 + minimumSize);
  } else {
    points[edit.handleIndex * 2] = point.x;
    points[edit.handleIndex * 2 + 1] = point.y;
  }
  annotation.points = points.map(value => Number(value.toFixed(2)));
  state.selectedAnnotation = annotation;
  edit.moved = edit.moved || annotation.points.some((value, index) => value !== edit.original[index]);
  refreshShapes(); refreshCanvasStatus();
}

async function finishShapeEdit(event) {
  const edit = state.shapeEdit;
  if (!edit) return;
  state.shapeEdit = null;
  const svg = document.querySelector("#annotation-canvas");
  if (svg?.hasPointerCapture(event.pointerId)) svg.releasePointerCapture(event.pointerId);
  if (!edit.moved) { refreshShapes(); refreshCanvasStatus(); return; }
  const annotation = state.annotations.find(item => item.id === edit.annotationId);
  if (!annotation) return;
  const current = annotationContextGuard();
  state.savingAnnotation = true; setSaveFeedback("saving");
  try {
    const updated = await api(`/api/annotations/${annotation.id}`, jsonOptions("PATCH", {
      expected_version: edit.version,
      points: annotation.points, keypoints: annotation.keypoints || [],
    }));
    if (!current()) return;
    const index = state.annotations.findIndex(item => item.id === updated.id);
    state.annotations[index] = updated; state.selectedAnnotation = updated;
    state.asset.revision = updated.asset_revision; state.asset.review_status = "draft"; state.asset.negative_confirmed = false;
    setSaveFeedback("saved"); refreshWorkflowState();
  } catch (error) {
    if (!current()) return;
    annotation.points = edit.original; annotation.keypoints = edit.originalKeypoints; state.selectedAnnotation = annotation;
    setSaveFeedback("error", `${error.message} 本次修改未确认保存，请检查后重试。`);
  } finally {
    if (current()) { state.savingAnnotation = false; refreshShapes(); refreshInspector(); refreshCanvasStatus(); refreshWorkflowState(); }
  }
}

function cancelShapeEdit() {
  const edit = state.shapeEdit;
  if (!edit) return;
  const annotation = state.annotations.find(item => item.id === edit.annotationId);
  if (annotation) { annotation.points = edit.original; annotation.keypoints = edit.originalKeypoints; }
  state.shapeEdit = null;
  const svg = document.querySelector("#annotation-canvas");
  if (svg?.hasPointerCapture(edit.pointerId)) svg.releasePointerCapture(edit.pointerId);
  refreshShapes(); refreshInspector(); refreshCanvasStatus();
}

function refreshCanvasStatus() {
  canvasGuides?.refresh();
  refreshAnnotationDeleteControls();
  const feedback = document.querySelector("#annotation-save-status");
  if (feedback) { feedback.textContent = saveFeedbackText(); feedback.dataset.status = state.saveFeedback.status; }
  refreshWorkspaceGuidance();
  const status = document.querySelector("#canvas-status-hint");
  if (status) status.textContent = canvasStatusHint();
}

function drawDraft(cursor = null) {
  const layer = document.querySelector("#draft-layer");
  if (!layer) return;
  const color = state.project?.labels.find(item => item.id === (state.poseDraft?.labelId || state.pendingAnnotation?.labelId))?.color || state.activeLabel?.color || "#6be7bb";
  const screenScale = 1 / Math.max(0.005, state.zoom);
  let markup = state.assistPoints.map((point) => `<g class="assist-point"><circle cx="${point.x}" cy="${point.y}" r="${8 * screenScale}" fill="${point.label ? "#22c55e" : "#ef4444"}" fill-opacity=".88" stroke="#fff" stroke-width="2" vector-effect="non-scaling-stroke"/><text x="${point.x}" y="${point.y + 4 * screenScale}" text-anchor="middle" fill="#fff" font-size="${12 * screenScale}">${point.label ? "+" : "−"}</text></g>`).join("");
  if (state.drawing) {
    const { start, current } = state.drawing;
    markup += `<rect x="${Math.min(start.x, current.x)}" y="${Math.min(start.y, current.y)}" width="${Math.abs(current.x - start.x)}" height="${Math.abs(current.y - start.y)}" fill="${color}" fill-opacity=".1" stroke="${color}" stroke-width="2" stroke-dasharray="6 4" vector-effect="non-scaling-stroke"/>`;
  } else if (state.polygon.length) {
    const coords = [...state.polygon]; if (cursor) coords.push(cursor.x, cursor.y);
    const points = coords.reduce((pairs, value, i) => i % 2 ? pairs : [...pairs, `${value},${coords[i + 1]}`], []).join(" ");
    const vertices = state.polygon.reduce((out, value, i) => i % 2 ? out : out + `<circle class="${i === 0 ? "polygon-start" : ""}" cx="${value}" cy="${state.polygon[i + 1]}" r="${(i === 0 ? 7 : 4) * screenScale}" fill="${color}" stroke="${i === 0 ? "#fff" : color}" stroke-width="${i === 0 ? 2 : 0}" vector-effect="non-scaling-stroke"/>`, "");
    markup += `<polyline points="${points}" fill="none" stroke="${color}" stroke-width="2" stroke-dasharray="6 4" vector-effect="non-scaling-stroke"/>${vertices}`;
  }
  if (state.poseDraft) {
    const draft=state.poseDraft;
    markup += `<rect x="${draft.points[0]}" y="${draft.points[1]}" width="${draft.points[2]-draft.points[0]}" height="${draft.points[3]-draft.points[1]}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
    markup += draft.keypoints.filter(point=>point.visibility).map(point=>`<circle cx="${point.x}" cy="${point.y}" r="${5 * screenScale}" fill="${color}"/>`).join("");
  }
  layer.innerHTML = markup;
  refreshCanvasStatus();
  refreshWorkflowState();
}

async function finishPolygon() {
  if (state.savingAnnotation || state.workflowBusy) return;
  if (state.polygon.length < 6) return notify("至少添加 3 个顶点后再完成轮廓。", "warning");
  const points = state.polygon, current = annotationContextGuard();
  const saved = await saveAnnotation("polygon", [...points]);
  if (saved && current() && state.polygon === points) { state.polygon = []; drawDraft(); refreshWorkflowState(); }
}

function undoPolygonPoint() {
  if (!state.polygon.length || state.savingAnnotation || state.pendingAnnotation) return;
  state.polygon.splice(-2, 2); drawDraft(); refreshWorkflowState();
}

function cancelDrawing(clearPrompts = false) {
  if (state.savingAnnotation || state.workflowBusy) return;
  const failedDraft = state.pendingAnnotation || state.pendingTaskEdit || state.pendingObjectEdit;
  state.poseDraft = null; state.keypointPlacement = null; state.drawing = null; state.polygon = [];
  state.pendingAnnotation = null; state.pendingObjectEdit = null; state.advancedAnnotationDirty = false; state.advancedDraft = null;
  discardTaskSaveDraft();
  if (failedDraft) setSaveFeedback("error", "本地草稿已放弃。请重新加载服务器结果，核对已保存的内容后继续。");
  if (clearPrompts) state.assistPoints = [];
  drawDraft(); refreshInspector(); refreshWorkflowState();
}

async function saveAnnotation(shapeType, points, keypoints = [], labelId = null) {
  if (!canEditCurrentAsset()) { notify("当前图片暂不可编辑，请先确认编辑权限或等待保存完成。", "warning"); return null; }
  const label = state.project.labels.find(item => item.id === (labelId || state.activeLabel?.id));
  if (!label) { notify("请先选择标注类别。", "warning"); return null; }
  const current = annotationContextGuard(), assetId = state.asset.id;
  const pending = { ...(state.pendingAnnotation || { shapeType, labelId: label.id, attributes: defaultAttributes(label), expectedRevision: state.asset.revision }),
    points: [...points], keypoints: keypoints.map(item => ({...item})) };
  state.savingAnnotation = true; setSaveFeedback("saving"); refreshWorkflowState();
  try {
    const annotation = await api(`/api/assets/${assetId}/annotations`, jsonOptions("POST", { label_id: pending.labelId,
      shape_type: pending.shapeType, points: pending.points, keypoints: pending.keypoints, source: "manual", attributes: pending.attributes,
      expected_asset_revision: pending.expectedRevision }));
    if (!current()) return null;
    state.pendingAnnotation = null;
    state.annotations.push(annotation); state.selectedAnnotation = annotation; state.selectionIntent = false;
    const asset = state.assets.find(item => item.id === assetId);
    asset.status = "labeled"; asset.annotation_count += 1; asset.revision = annotation.asset_revision; asset.review_status = "draft"; asset.negative_confirmed = false; state.asset = asset;
    state.project.labeled_count = state.assets.filter(item => item.status === "labeled").length;
    refreshAssetQueueItem(assetId); setSaveFeedback("saved");
    return annotation;
  } catch (error) {
    if (current()) { state.pendingAnnotation = pending; setSaveFeedback("error", `${error.message} 草稿已保留，可重试保存；若提示版本变化，请重新加载已保存结果核对。`); }
    return null;
  } finally {
    if (current()) { state.savingAnnotation = false; refreshShapes(); refreshInspector(); refreshCanvasStatus(); refreshWorkflowState(); }
  }
}

async function retryPendingAnnotation() {
  if (state.savingAnnotation || state.workflowBusy) return;
  if (state.pendingObjectEdit) return persistObjectLabel(state.pendingObjectEdit);
  if (state.pendingTaskEdit) return retryTaskSave();
  if (state.poseDraft) return finishKeypoints();
  const pending = state.pendingAnnotation, current = annotationContextGuard();
  if (!pending) return;
  const saved = await saveAnnotation(pending.shapeType, pending.points, pending.keypoints, pending.labelId);
  if (saved && current()) { state.drawing = null; state.polygon = []; drawDraft(); refreshWorkflowState(); }
}

async function loadAnnotations(assetId, selectionEpoch = state.selectionEpoch) {
  try {
    const annotations = await api(`/api/assets/${assetId}/annotations`);
    if (selectionEpoch !== state.selectionEpoch || state.asset?.id !== assetId) return;
    state.annotations = annotations; state.selectedAnnotation = null; state.selectionIntent = false; refreshShapes(); refreshInspector();
    preloadNearbyAssets(assetId);
    refreshWorkflowState();
    return true;
  }
  catch (error) { notify(error.message, "error"); return false; }
}

function preloadNearbyAssets(assetId) {
  const index = state.assets.findIndex(asset => asset.id === assetId);
  [state.assets[index - 1], state.assets[index + 1], state.assets[index + 2]].filter(Boolean).forEach(asset => {
    const image = new Image(); image.src = `/api/assets/${asset.id}/image`;
  });
}

async function selectAsset(assetId) {
  if (state.asset?.id === assetId) return;
  if (!state.assets.some(asset => asset.id === assetId)) return;
  if (state.savingAnnotation || state.workflowBusy) return notify("正在保存或更新审核状态，请稍候。", "warning");
  if (hasAnnotationDraft()) return notify("当前还有未完成的标注，请先完成或按 Esc 取消，再切换图片。", "warning");
  const selectionEpoch = ++state.selectionEpoch;
  const previousAssetId = state.asset?.id;
  cancelShapeEdit();
  await releaseCurrentLock(previousAssetId);
  if (selectionEpoch !== state.selectionEpoch) return;
  state.asset = state.assets.find(a => a.id === assetId); state.annotations = []; state.selectedAnnotation = null; state.polygon = []; state.drawing = null; state.shapeEdit = null;
  state.assistPoints = []; state.poseDraft = null; state.keypointPlacement = null;
  state.reviewEvents = []; state.saveFeedback = { status:"idle", message:"" };
  state.pendingAnnotation = null; state.pendingTaskEdit = null; state.pendingObjectEdit = null; state.selectionIntent = false; state.advancedAnnotationDirty = false; state.advancedDraft = null;
  if (!state.asset) return;
  history.replaceState(null, "", annotationBlockURL(state.project.id, state.activeWorkBlockId, assetId));
  await acquireAssetLock(assetId, selectionEpoch);
  if (selectionEpoch !== state.selectionEpoch || state.asset?.id !== assetId) return;
  renderWorkspace(); await loadAnnotations(assetId, selectionEpoch);
}

async function acquireAssetLock(assetId, selectionEpoch = state.selectionEpoch, quiet = false) {
  clearInterval(state.lockTimer); state.lockTimer = null; state.lockOwned = false; state.readOnly = false; state.readOnlyReason = "";
  if (state.legacyReviewAssetId) { state.readOnly = true; state.readOnlyReason = "请由管理员将这张历史图片加入任务块后再修改。"; return false; }
  try {
    const lock = await api(`/api/assets/${assetId}/lock`, { method: "POST" });
    if (selectionEpoch !== state.selectionEpoch || state.asset?.id !== assetId) {
      if (state.asset?.id !== assetId) api(`/api/assets/${assetId}/lock`, { method: "DELETE", keepalive: true }).catch(() => {});
      return false;
    }
    state.lockOwned = true;
    const asset = state.assets.find(item => item.id === assetId);
    if (asset) asset.revision = lock.revision;
    state.lockTimer = setInterval(async () => {
      if (!state.lockOwned || state.workflowBusy || state.asset?.id !== assetId || selectionEpoch !== state.selectionEpoch) return;
      try { await api(`/api/assets/${assetId}/lock`, { method: "POST" }); }
      catch (error) { if (selectionEpoch !== state.selectionEpoch || state.asset?.id !== assetId) return; state.lockOwned = false; state.readOnly = true; state.readOnlyReason = `未能保持编辑权限：${error.message}`; renderWorkspace(); }
    }, 45000);
    return true;
  } catch (error) {
    if (selectionEpoch !== state.selectionEpoch || state.asset?.id !== assetId) return false;
    state.readOnly = true; state.readOnlyReason = error.message;
    if (!quiet && !["submitted", "approved"].includes(state.asset?.review_status)) notify(`${error.message}，当前仅供查看。`, "warning");
    return false;
  }
}

async function releaseCurrentLock(assetId = state.asset?.id) {
  clearInterval(state.lockTimer); state.lockTimer = null;
  if (!state.lockOwned || !assetId || state.asset?.id !== assetId) { state.lockOwned = false; return; }
  state.lockOwned = false;
  try {
    await api(`/api/assets/${assetId}/lock`, { method: "DELETE", keepalive: true });
    const asset = state.assets.find(item => item.id === assetId);
    if (asset && asset.lock_owner === (state.session?.actor || actorId)) { asset.lock_owner = null; asset.lock_expires_at = null; }
    if (state.asset?.id === assetId && state.asset.lock_owner === (state.session?.actor || actorId)) { state.asset.lock_owner = null; state.asset.lock_expires_at = null; }
  }
  catch (_) { /* the server lease will expire automatically */ }
}

function annotationOverlayOpen() { return Boolean(modalRoot.children.length || annotationMenu?.isOpen()); }

function annotationToDelete() {
  return projectTask() === "classify"
    ? state.annotations.find(item => item.shape_type === "classification")
    : state.annotations.find(item => item.id === state.selectedAnnotation?.id);
}

function annotationDeleteDisabledReason() {
  if (!state.asset) return "请先选择图片";
  if (state.savingAnnotation || state.workflowBusy) return "正在保存或更新审核状态，请稍候";
  if (hasAnnotationDraft()) return "请先完成当前修改，或按 Esc 取消草稿";
  if (!canEditCurrentAsset()) return ["submitted", "approved"].includes(state.asset.review_status)
    ? "请先撤回提交或重新打开图片，再删除标注"
    : state.readOnlyReason || "当前图片不可编辑";
  if (!annotationToDelete()) return projectTask() === "classify" ? "这张图片尚未分类" : "先按 V 选择标注，或点击标注上的类别标签";
  return "";
}

function annotationInspectorHeaderMarkup() {
  const selected = state.selectedAnnotation;
  return `<span class="panel-title">标注详情</span><div class="inspector-object-actions">${selected && selected.shape_type !== "classification" ? `<button class="mini-action" id="selected-object-menu" title="选中对象的更多操作" ${hasAnnotationDraft() || state.savingAnnotation || state.workflowBusy ? "disabled" : ""}>更多</button>` : ""}${annotationToDelete() ? annotationDeleteButtonMarkup("delete-annotation") : ""}</div>`;
}

function bindInspectorObjectActions() {
  document.querySelector("#selected-object-menu")?.addEventListener("click", event => {
    if (!state.selectedAnnotation || hasAnnotationDraft() || state.savingAnnotation || state.workflowBusy) return;
    const rect = event.currentTarget.getBoundingClientRect();
    openObjectMenu([state.selectedAnnotation], state.selectedAnnotation.id, rect.left, rect.bottom + 6);
  });
}

function annotationDeleteButtonMarkup(id) {
  const reason = annotationDeleteDisabledReason();
  const label = projectTask() === "classify" ? "清除类别" : "删除";
  return `<button id="${id}" class="mini-action danger annotation-delete-action" data-delete-selected aria-keyshortcuts="Delete Backspace" title="${escapeHtml(reason || "删除当前标注，保留图片 (Delete / Backspace)")}" ${reason ? "disabled" : ""}>${icon("trash")}<span>${label}</span></button>`;
}

function refreshAnnotationDeleteControls() {
  const menu = document.querySelector("#selected-object-menu");
  if (menu) menu.disabled = Boolean(hasAnnotationDraft() || state.savingAnnotation || state.workflowBusy);
  const reason = annotationDeleteDisabledReason();
  document.querySelectorAll("[data-delete-selected]").forEach(button => {
    button.disabled = Boolean(reason);
    button.title = reason || "删除当前标注，保留图片 (Delete / Backspace)";
    button.onclick = deleteSelectedAnnotation;
  });
}

function selectAnnotation(id) {
  if (state.savingAnnotation || state.workflowBusy || hasAnnotationDraft()) { notify("请先完成或取消当前修改，再选择其他对象。", "warning"); return false; }
  const selected = state.annotations.find(item => item.id === id);
  if (!selected) return false;
  state.selectedAnnotation = selected; state.selectionIntent = true; state.inspectorTab = "objects";
  refreshShapes(); refreshInspector(); refreshCanvasStatus();
  if (!annotationOverlayOpen()) document.querySelector("#canvas-scroll")?.focus({ preventScroll: true });
  return true;
}

function clearObjectSelection() {
  if (state.savingAnnotation || state.workflowBusy || hasAnnotationDraft()) return false;
  state.selectedAnnotation = null; state.selectionIntent = false;
  refreshShapes(); refreshInspector(); refreshCanvasStatus();
  document.querySelector("#canvas-scroll")?.focus({ preventScroll: true });
  return true;
}

function editSelectedObject(id) {
  if (!canEditCurrentAsset() || !selectAnnotation(id)) return false;
  if (["rectangle", "polygon", "keypoints"].includes(state.tool)) state.lastDrawingTool = state.tool;
  state.tool = "select";
  renderWorkspace();
  document.querySelector("#canvas-scroll")?.focus({ preventScroll: true });
  return true;
}

function continueManualDrawing() {
  if (state.savingAnnotation || state.workflowBusy || hasAnnotationDraft()) return false;
  if (!canEditCurrentAsset()) return clearObjectSelection();
  const tool = state.lastDrawingTool && taskAllowsTool(state.lastDrawingTool) ? state.lastDrawingTool : {detect:"rectangle",segment:"polygon",pose:"keypoints",mixed:"rectangle"}[projectTask()];
  if (tool) setTool(tool); else clearObjectSelection();
  return true;
}

async function chooseNewAnnotationLabel(labelId) {
  if (hasAnnotationDraft() || state.savingAnnotation || state.workflowBusy) { notify("请先完成或取消当前修改，再选择新对象类别。", "warning"); refreshInspector(); return; }
  if (projectTask() !== "classify") { state.selectionIntent = false; state.selectedAnnotation = null; refreshShapes(); }
  await chooseTaskLabel(labelId); refreshCanvasStatus();
}

async function chooseWorkspaceLabel(labelId) {
  if (state.selectionIntent && state.selectedAnnotation && projectTask() !== "classify") return changeObjectLabel(state.selectedAnnotation.id, labelId);
  return chooseNewAnnotationLabel(labelId);
}

async function changeObjectLabel(annotationId, labelId) {
  if (!canEditCurrentAsset() || hasAnnotationDraft()) {
    notify(hasAnnotationDraft() ? "请先保存或取消当前修改，再更改对象类别。" : "当前图片不可编辑。", "warning"); refreshInspector(); return false;
  }
  const annotation = state.annotations.find(item => item.id === annotationId), label = state.project.labels.find(item => item.id === labelId);
  if (!annotation || !label) return false;
  if (annotation.label_id === label.id) return true;
  state.selectedAnnotation = annotation; state.selectionIntent = true;
  return persistObjectLabel({ annotationId, labelId, version: annotation.version, current: annotationContextGuard() });
}

async function persistObjectLabel(pending) {
  if (!pending.current() || !canEditCurrentAsset()) return false;
  state.pendingObjectEdit = pending; state.savingAnnotation = true; setSaveFeedback("saving"); refreshInspector();
  try {
    // Relabel without sending coordinates or parsing the advanced editor's unrelated inputs.
    const updated = await api(`/api/annotations/${pending.annotationId}`, jsonOptions("PATCH", { expected_version: pending.version, label_id: pending.labelId }));
    if (!pending.current() || state.pendingObjectEdit !== pending) return false;
    const index = state.annotations.findIndex(item => item.id === updated.id);
    if (index >= 0) state.annotations[index] = updated;
    state.selectedAnnotation = updated; state.selectionIntent = true; state.pendingObjectEdit = null;
    state.asset.revision = updated.asset_revision; state.asset.review_status = "draft"; state.asset.negative_confirmed = false;
    const asset = state.assets.find(item => item.id === state.asset.id);
    if (asset) { asset.revision = updated.asset_revision; asset.review_status = "draft"; asset.negative_confirmed = false; }
    setSaveFeedback("saved"); refreshAssetQueueItem(state.asset.id);
    return true;
  } catch (error) {
    if (pending.current() && state.pendingObjectEdit === pending) {
      pending.error = error.message;
      setSaveFeedback("error", `类别修改未确认保存，已保留待保存的选择。可重试保存；若提示版本变化，请重新加载结果核对。${error.message}`);
      notify(error.message, "error");
    }
    return false;
  } finally {
    if (pending.current()) {
      state.savingAnnotation = false; refreshShapes(); refreshInspector(); refreshCanvasStatus(); refreshWorkflowState();
    }
  }
}

function copyObject(id) {
  const annotation = state.annotations.find(item => item.id === id);
  if (!annotation || hasAnnotationDraft() || state.savingAnnotation || state.workflowBusy) return false;
  state.copiedAnnotation = { ...JSON.parse(JSON.stringify(annotation)), _copiedProjectId: state.project.id };
  notify("已复制对象，可用 Ctrl / ⌘ + V 在本项目的可编辑图片上粘贴。");
  return true;
}

function openObjectMenu(annotations, selectedId, clientX, clientY) {
  if (annotationOverlayOpen() || state.pan || state.savingAnnotation || state.workflowBusy) return;
  if (hasAnnotationDraft()) return notify("请先完成或取消当前草稿，再操作已有对象。", "warning");
  const targets = annotations.filter(Boolean);
  if (!targets.length) return;
  annotationMenuContext = { current: annotationContextGuard(), versions: new Map(targets.map(item => [item.id, item.version])) };
  annotationMenu?.open({ annotations: targets, selectedId, clientX, clientY,
    readOnlyReason: canEditCurrentAsset() ? "" : state.readOnlyReason || "当前图片只读，可查看或复制对象。修改前需重新打开或获得编辑权限。" });
  canvasGuides?.refresh();
}

function onCanvasContextMenu(event) {
  if (event.shiftKey) return; // Shift + right-click keeps the browser menu available.
  event.preventDefault(); event.stopPropagation();
  if (state.pan || state.savingAnnotation || state.workflowBusy || annotationOverlayOpen()) return;
  if (state.tool === "polygon" && state.polygon.length && !state.pendingAnnotation) { undoPolygonPoint(); return; }
  if (hasAnnotationDraft()) return notify("请先完成或取消当前草稿，再操作已有对象。", "warning");
  if (!state.showAnnotations) return notify("标注已隐藏，请先点击“显示标注”。", "warning");
  const svg = document.querySelector("#annotation-canvas"), matrix = svg?.getScreenCTM();
  if (!matrix || !state.asset) return;
  const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
  const targetId = Number(event.target.closest?.("[data-annotation-id]")?.dataset.annotationId);
  const targeted = state.annotations.find(item => item.id === targetId);
  if (!targeted && (point.x < 0 || point.y < 0 || point.x > state.asset.width || point.y > state.asset.height)) return;
  const hits = findAnnotationsAtPoint(state.annotations, point, 5 / Math.max(state.zoom, 0.005));
  if (targeted && !hits.includes(targeted)) hits.unshift(targeted);
  if (!hits.length) return;
  openObjectMenu(hits, targeted?.id || hits[0].id, event.clientX, event.clientY);
}

function refreshShapes() {
  const layer = document.querySelector("#shape-layer"); if (layer) { layer.innerHTML = shapesMarkup(); bindCanvasEventsAfterRefresh(); }
}

function bindCanvasEventsAfterRefresh() {
  const layer = document.querySelector("#shape-layer");
  if (layer) bindShapePointerEvents(layer);
}

function refreshInspector() {
  refreshAnnotationDeleteControls();
  const header = document.querySelector(".inspector > .panel-header");
  if (header) {
    header.innerHTML = annotationInspectorHeaderMarkup();
    bindInspectorObjectActions();
    refreshAnnotationDeleteControls();
  }
  const body = document.querySelector(".inspector-body"); if (body) body.innerHTML = inspectorMarkup();
  document.querySelectorAll("[data-label-id]").forEach((row) => row.onclick = () => { void chooseWorkspaceLabel(Number(row.dataset.labelId)); });
  bindLabelDeleteButtons();
  document.querySelectorAll(".annotation-row").forEach((row) => row.onclick = () => selectAnnotation(Number(row.dataset.annotationId)));
  document.querySelector("#add-label")?.addEventListener("click", openLabelModal);
  document.querySelector("#save-annotation")?.addEventListener("click", saveSelectedAnnotation);
  document.querySelector("#annotation-label")?.addEventListener("change", event => { void changeObjectLabel(state.selectedAnnotation?.id, Number(event.target.value)); });
  bindStructuredAttributeEvents();
  bindTaskInspector();
  document.querySelectorAll(".workflow-action").forEach((button) => button.onclick = () => updateWorkflow(button.dataset.action));
  bindJobActionButtons();
}

function bindJobActionButtons() {
  document.querySelectorAll("[data-cancel-job]").forEach(button => button.onclick = () => cancelJob(Number(button.dataset.cancelJob)));
  document.querySelectorAll("[data-retry-job]").forEach(button => button.onclick = () => retryJob(Number(button.dataset.retryJob)));
}

function setTool(tool) {
  if (tool !== "pan" && !taskAllowsTool(tool)) return;
  if (state.savingAnnotation || state.workflowBusy) return;
  if (["rectangle", "polygon", "keypoints", "positive", "negative"].includes(tool) && !canEditCurrentAsset()) return notify("当前图片不可编辑，请先按页面提示恢复编辑。", "warning");
  const draftTool = state.poseDraft ? "keypoints" : state.polygon.length ? "polygon" : state.drawing ? "rectangle" : null;
  if (hasAnnotationDraft()) {
    if (tool !== "pan" && tool !== draftTool && tool !== state.tool) return notify("请先完成当前标注，或按 Esc 取消草稿后再切换工具。", "warning");
    state.tool = tool; renderWorkspace(); return;
  }
  if (["rectangle", "polygon", "keypoints"].includes(tool)) state.lastDrawingTool = tool;
  state.tool = tool; state.selectedAnnotation = null; state.selectionIntent = false; state.inspectorTab = "objects";
  renderWorkspace(); document.querySelector("#canvas-scroll")?.focus({ preventScroll: true });
}

function startManualAnnotation() {
  if (projectTask() === "classify") { document.querySelector("#manual-label")?.focus(); return; }
  setTool({detect:"rectangle",segment:"polygon",pose:"keypoints",mixed:"rectangle"}[projectTask()] || "select");
}

async function updateWorkflow(action) {
  if (!state.asset || state.workflowBusy || state.savingAnnotation) return;
  if (hasAnnotationDraft()) return notify("请先完成或取消当前草稿，再更新审核状态。", "warning");
  if (action === "submit" && state.saveFeedback.status === "error") return notify("修改尚未确认保存，请先重试保存或重新加载已保存结果。", "error");
  if (action === "confirm_empty") {
    const assetId = state.asset.id, revision = state.asset.revision;
    openModal({ title: "确认图片中没有待标注目标", subtitle: "请检查整张图片。AI 没有检出目标，不等于图片中没有目标。", body: `<p>确认“${escapeHtml(state.asset.filename)}”中没有本项目需要标注的目标。确认后仍需提交审核。</p>`, submitText: "确认无目标", onSubmit: async () => {
      if (state.asset?.id !== assetId || state.asset.revision !== revision) throw new Error("图片已更新，请关闭窗口并重新检查。");
      await performWorkflow("confirm_empty", ""); closeModal();
    } });
    return;
  }
  if (action === "reject") {
    openModal({ title: "退回图片，说明修改要求", subtitle: "标注员会看到这段反馈，修改后重新提交审核。", body: `<div class="field"><label for="reject-reason">需要修改的地方</label><textarea id="reject-reason" placeholder="例如：右侧还有一位行人未标注，请补上；左侧轮廓需要贴近边缘。" autofocus></textarea></div>`, submitText: "退回修改", onSubmit: async () => {
      const reason = document.querySelector("#reject-reason").value.trim();
      if (!reason) throw new Error("请填写修改要求，让标注员知道需要调整哪里。");
      await performWorkflow(action, reason); closeModal();
    } });
    return;
  }
  try { await performWorkflow(action, ""); } catch (error) { notify(error.message, "error"); }
}

async function performWorkflow(action, reason) {
  const assetId = state.asset.id; const projectId = state.project.id;
  const selectionEpoch = state.selectionEpoch;
  const revision = state.asset.revision;
  const isCurrent = () => state.authReady !== false && state.selectionEpoch === selectionEpoch
    && state.currentSection === "annotate" && state.project?.id === projectId && state.asset?.id === assetId;
  let continueReview = false;
  state.workflowBusy = true; refreshWorkflowState();
  try {
    const updated = await api(`/api/assets/${assetId}/workflow`, jsonOptions("POST", { action, expected_revision: revision, reason }));
    if (!isCurrent()) return;
    const index = state.assets.findIndex(asset => asset.id === updated.id);
    if (index >= 0) state.assets[index] = { ...state.assets[index], ...updated };
    if (index >= 0) state.asset = state.assets[index];
    if (["submit", "approve", "reject"].includes(action)) {
      state.tool = "select"; await releaseCurrentLock(assetId);
      if (!isCurrent()) return;
    }
    if (action === "reject") { state.handedOffAssetId = assetId; state.readOnly = true; state.readOnlyReason = "已交给标注员修改，你可以继续审核下一张图片。"; }
    if (action === "reopen" && !state.lockOwned) {
      await acquireAssetLock(assetId, selectionEpoch);
      if (!isCurrent()) return;
    }
    notify({ submit: "已提交，等待审核员确认。可以继续检查下一张。", approve: "审核已通过，可用于生成训练数据版本。", reject: "已退回修改，标注员可以看到你的反馈。", reopen: "已恢复为可修改状态，完成修改后请再次提交审核。", start: "已开始标注", confirm_empty: "已确认无目标，请提交审核。" }[action] || "状态已更新");
    try {
      const events = await api(`/api/assets/${assetId}/review-events`);
      if (isCurrent()) state.reviewEvents = events;
    } catch (_) { /* optional */ }
    if (state.activeWorkBlockId && isCurrent()) {
      try {
        const block = await api(`/api/projects/${projectId}/work-blocks/${state.activeWorkBlockId}`);
        if (isCurrent()) state.activeWorkBlock = block;
      } catch (_) { /* The collaboration refresh will retry block progress. */ }
    }
    if (isCurrent()) {
      continueReview = ["approve", "reject"].includes(action) && shouldContinueReview();
      renderWorkspace();
    }
  } finally {
    state.workflowBusy = false;
    if (state.authReady !== false && state.currentSection === "annotate") { refreshWorkflowState(); refreshInspector(); }
  }
  if (continueReview && isCurrent() && !state.savingAnnotation && !state.shapeEdit && !state.drawing && !state.polygon.length && !state.poseDraft) {
    const currentIndex = state.assets.findIndex(asset => asset.id === assetId);
    const queue = [...state.assets.slice(currentIndex + 1), ...state.assets.slice(0, currentIndex)];
    const candidates = queue.filter(asset => asset.id !== assetId && asset.review_status === "submitted"
      && !(asset.lock_owner && asset.lock_owner !== (state.session?.actor || actorId) && asset.lock_expires_at > new Date().toISOString()));
    const next = candidates[0];
    if (next) { state.assetFilter = "submitted"; await selectAsset(next.id); }
    else notify("当前没有其他可审核图片；已完成的审核结果已保存。");
  }
}

async function deleteSelectedAnnotation() {
  const annotation = annotationToDelete();
  if (!annotation || !canEditCurrentAsset()) return;
  if (hasAnnotationDraft()) return notify("请先完成或取消当前草稿，再删除对象。", "warning");
  const assetId = state.asset.id, current = annotationContextGuard();
  let deleted = false;
  state.savingAnnotation = true; setSaveFeedback("saving"); refreshWorkflowState();
  try {
    const result = await api(`/api/annotations/${annotation.id}?expected_version=${annotation.version}`, { method: "DELETE" });
    if (!current()) return;
    state.annotations = state.annotations.filter(item => item.id !== annotation.id); state.selectedAnnotation = null; state.selectionIntent = false;
    if (annotation.shape_type === "classification") state.activeLabel = null;
    const asset = state.assets.find(item => item.id === assetId) || state.asset;
    const updates = { negative_confirmed: false, annotation_count: state.annotations.length, revision: result.asset_revision, review_status: "draft", status: state.annotations.length ? "labeled" : "unlabeled" };
    Object.assign(asset, updates); Object.assign(state.asset, updates);
    state.project.labeled_count = state.assets.filter(item => item.status === "labeled").length;
    refreshAssetQueueItem(assetId); setSaveFeedback("saved"); deleted = true;
    notify(annotation.shape_type === "classification" ? "已清除图片类别，可以重新选择类别。" : `已删除“${annotation.label_name}”标注，图片保留。`);
  } catch (error) {
    if (current()) { setSaveFeedback("error", `删除未确认完成，请重新加载结果核对。${error.message}`); notify(`删除未完成：${error.message}`, "error"); }
  }
  finally {
    if (current()) {
      state.savingAnnotation = false; renderWorkspace();
      if (deleted) document.querySelector("#canvas-scroll")?.focus({ preventScroll: true });
    }
  }
}

async function saveSelectedAnnotation() {
  if (!state.selectedAnnotation || !canEditCurrentAsset()) return;
  if (state.pendingObjectEdit || state.pendingAnnotation || state.pendingTaskEdit || state.drawing || state.polygon.length || state.poseDraft || state.shapeEdit || state.keypointPlacement != null) return notify("请先完成或取消当前修改，再应用高级属性。", "warning");
  const annotation = state.selectedAnnotation, current = annotationContextGuard();
  state.savingAnnotation = true; setSaveFeedback("saving");
  let saved = false;
  try {
    const attributes = JSON.parse(document.querySelector("#annotation-attributes").value || "{}");
    const points = document.querySelector("#annotation-points").value.trim() ? document.querySelector("#annotation-points").value.split(",").map(value => Number(value.trim())) : [];
    if (points.some(value => !Number.isFinite(value))) throw new Error("坐标必须是以逗号分隔的数值");
    const updated = await api(`/api/annotations/${annotation.id}`, jsonOptions("PATCH", { expected_version: annotation.version, label_id: Number(document.querySelector("#annotation-label").value), points, attributes }));
    if (!current()) return;
    const index = state.annotations.findIndex(item => item.id === updated.id);
    if (index >= 0) state.annotations[index] = updated;
    state.selectedAnnotation = updated; state.asset.revision = updated.asset_revision; state.asset.review_status = "draft"; state.asset.negative_confirmed = false;
    state.advancedAnnotationDirty = false; state.advancedDraft = null;
    saved = true; setSaveFeedback("saved");
  } catch (error) { if (current()) setSaveFeedback("error", error instanceof SyntaxError ? "高级属性格式不正确，请修正后点击“应用高级修改”。" : error.message); }
  finally {
    if (current()) { state.savingAnnotation = false; if (saved) renderWorkspace(); else { refreshCanvasStatus(); refreshWorkflowState(); } }
  }
}

function createUploadBatches(files) {
  const batches = [];
  let current = [];
  let currentBytes = 0;
  for (const file of files) {
    const fileBytes = Math.max(file.size || 0, 1);
    if (current.length && (current.length >= UPLOAD_BATCH_FILE_LIMIT || currentBytes + fileBytes > UPLOAD_BATCH_BYTE_LIMIT)) {
      batches.push({ files: current, bytes: currentBytes });
      current = []; currentBytes = 0;
    }
    current.push(file); currentBytes += fileBytes;
  }
  if (current.length) batches.push({ files: current, bytes: currentBytes });
  return batches;
}

function uploadAssetBatch(projectId, batch, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    batch.files.forEach(file => form.append("files", file, file.webkitRelativePath || file.name));
    const request = new XMLHttpRequest();
    request.open("POST", `/api/projects/${projectId}/assets`);
    request.withCredentials = true;
    request.setRequestHeader("X-Actor-ID", actorId);
    request.setRequestHeader("X-MDP-Client", "browser");
    request.upload.onprogress = (event) => onProgress(event.loaded, event.lengthComputable ? event.total : batch.bytes, event.loaded >= event.total);
    request.upload.onload = () => onProgress(batch.bytes, batch.bytes, true);
    request.onerror = () => reject(new Error("上传连接中断，请重试"));
    request.onload = () => {
      let body = null;
      try { body = JSON.parse(request.responseText || "null"); } catch (_) { /* use fallback */ }
      if (request.status >= 200 && request.status < 300) return resolve(body);
      const detail = typeof body?.detail === "string" ? body.detail : body?.detail?.message;
      if (request.status === 401) { state.authReady = false; renderLoginScreen(detail || "请重新登录"); }
      reject(new Error(detail || `上传失败 (${request.status || "网络错误"})`));
    };
    request.send(form);
  });
}

async function uploadFilesInBatches(projectId, files, onProgress) {
  const batches = createUploadBatches(files);
  const batchState = batches.map(() => ({ status: "pending", loaded: 0 }));
  const results = new Array(batches.length);
  const errors = [];
  let nextBatch = 0;

  const emit = () => {
    const completedBatches = batchState.filter(item => item.status === "completed").length;
    const failedBatches = batchState.filter(item => item.status === "failed").length;
    const completedFiles = batchState.reduce((sum, item, index) => sum + (item.status === "completed" ? batches[index].files.length : 0), 0);
    const totalBytes = batches.reduce((sum, batch) => sum + batch.bytes, 0);
    const creditedBytes = batchState.reduce((sum, item, index) => {
      if (item.status === "completed") return sum + batches[index].bytes;
      // Keep headroom while the request body has arrived but Pillow/ZIP validation
      // and the database transaction are still running on the server.
      return sum + Math.min(item.loaded, batches[index].bytes * 0.9);
    }, 0);
    onProgress({
      completedBatches, failedBatches, completedFiles,
      totalBatches: batches.length, totalFiles: files.length,
      percent: totalBytes ? Math.round(creditedBytes / totalBytes * 100) : 0,
      validating: batchState.some(item => item.status === "validating"),
    });
  };

  const worker = async () => {
    while (true) {
      const index = nextBatch++;
      if (index >= batches.length) return;
      const batch = batches[index];
      batchState[index].status = "uploading"; emit();
      try {
        results[index] = await uploadAssetBatch(projectId, batch, (loaded, _total, validating) => {
          batchState[index].loaded = Math.min(loaded, batch.bytes);
          if (validating) batchState[index].status = "validating";
          emit();
        });
        batchState[index].status = "completed"; batchState[index].loaded = batch.bytes; emit();
      } catch (error) {
        batchState[index].status = "failed";
        errors.push({ batch, error }); emit();
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(UPLOAD_CONCURRENCY, batches.length) }, worker));
  return { results: results.filter(Boolean), errors };
}

function openUploadModal() {
  if (state.currentSection === "annotate" && (state.savingAnnotation || state.workflowBusy || hasAnnotationDraft())) return notify("请先完成或取消当前标注，再导入图片。", "warning");
  let files = []; let uploading = false; let complete = false; let totalCreated = 0; let totalDuplicates = 0; let nextSection = "ai";
  openModal({ title: "导入图片", subtitle: "选择图片、图片文件夹或 ZIP。同一项目中的重复图片会自动跳过。", body: `
    <div class="dropzone" id="dropzone"><span class="card-icon">${icon("upload")}</span><p>把图片或 ZIP 拖放到这里</p><small>支持 JPG、PNG、WEBP、BMP 和 ZIP 压缩包</small><div class="dataset-picker-actions"><button type="button" class="btn primary" id="pick-files">选择图片或 ZIP</button><button type="button" class="btn ghost" id="pick-folder">选择图片文件夹</button></div><input id="asset-files" type="file" accept="image/jpeg,image/png,image/webp,image/bmp,.zip" multiple hidden /><input id="asset-folder" type="file" accept="image/jpeg,image/png,image/webp,image/bmp" webkitdirectory multiple hidden /><div class="file-summary" id="file-summary"></div></div>
    <div class="upload-progress" id="upload-progress" hidden><div><span id="upload-progress-label">准备上传</span><strong id="upload-progress-percent">0%</strong></div><div class="progress-mini"><span id="upload-progress-bar" style="width:0%"></span></div><small id="upload-progress-detail"></small></div>`,
    submitText: "开始上传", onReady: () => {
      const input = document.querySelector("#asset-files"); const folder = document.querySelector("#asset-folder"); const zone = document.querySelector("#dropzone");
      const update = (list) => {
        if (uploading) return;
        const supported = new Set(["image/jpeg", "image/png", "image/webp", "image/bmp", "application/zip", "application/x-zip-compressed"]);
        const selected = [...list];
        files = selected.filter(file => supported.has(file.type) || /\.(jpe?g|png|webp|bmp|zip)$/i.test(file.name));
        const skipped = selected.length - files.length;
        const bytes = files.reduce((sum, file) => sum + file.size, 0);
        const examples = files.slice(0, 3).map(file => escapeHtml(file.webkitRelativePath || file.name)).join("、");
        document.querySelector("#file-summary").innerHTML = files.length ? `<strong>已选择 ${files.length.toLocaleString()} 个文件 · ${formatBytes(bytes)}</strong><small>${examples}${files.length > 3 ? ` 等 ${files.length.toLocaleString()} 个` : ""}${skipped ? ` · 已忽略 ${skipped} 个不支持文件` : ""}</small>` : selected.length ? `<strong class="feedback-error">没有可导入的文件</strong><small>已忽略 ${skipped} 个不支持的文件。请选择 JPG、PNG、WEBP、BMP 图片或 ZIP 压缩包。</small>` : "";
        document.querySelector("#modal-submit").disabled = !files.length;
      };
      document.querySelector("#modal-submit").disabled = true;
      document.querySelector("#pick-files").onclick = (event) => { event.stopPropagation(); input.click(); };
      document.querySelector("#pick-folder").onclick = (event) => { event.stopPropagation(); folder.click(); };
      input.onchange = () => update(input.files); folder.onchange = () => update(folder.files);
      zone.ondragover = (event) => { event.preventDefault(); if (!uploading) zone.classList.add("drag"); };
      zone.ondragleave = () => zone.classList.remove("drag");
      zone.ondrop = (event) => { event.preventDefault(); zone.classList.remove("drag"); update(event.dataTransfer.files); };
    }, onSubmit: async () => {
      if (complete) { closeModal(); location.hash = `#/projects/${state.project.id}/${nextSection}`; return; }
      if (!files.length) throw new Error("请选择图片或 ZIP 文件");
      uploading = true;
      const progressNode = document.querySelector("#upload-progress");
      progressNode.hidden = false;
      ["#modal-close", "#modal-cancel", "#pick-files", "#pick-folder"].forEach(selector => { const node = document.querySelector(selector); if (node) node.disabled = true; });
      const updateProgress = (progress) => {
        const percent = Math.min(progress.percent, 100);
        document.querySelector("#upload-progress-percent").textContent = `${percent}%`;
        document.querySelector("#upload-progress-bar").style.width = `${percent}%`;
        document.querySelector("#upload-progress-label").textContent = progress.validating ? "正在检查并保存图片，请稍候…" : "正在上传，请保持此页面打开…";
        document.querySelector("#upload-progress-detail").textContent = `已处理 ${progress.completedFiles.toLocaleString()} / ${progress.totalFiles.toLocaleString()} 个文件${progress.failedBatches ? "，有文件需要重试" : ""}`;
      };
      try {
        const { results, errors } = await uploadFilesInBatches(state.project.id, files, updateProgress);
        const created = results.reduce((sum, result) => sum + result.count, 0);
        const duplicates = results.reduce((sum, result) => sum + result.duplicate_count, 0);
        totalCreated += created; totalDuplicates += duplicates;
        const assetIds = results.flatMap(result => result.asset_ids);
        if (assetIds.length) await reloadAfterUpload(assetIds[0]);
        if (errors.length) {
          files = errors.flatMap(item => item.batch.files);
          const firstError = errors[0].error.message;
          document.querySelector("#file-summary").textContent = `待重试 ${files.length.toLocaleString()} 个文件`;
          document.querySelector("#modal-submit").textContent = "重试未完成文件";
          throw new Error(`本次新增 ${totalCreated} 张图片，跳过 ${totalDuplicates} 张重复图片。还有 ${files.length} 个文件未处理完成：${firstError} 已成功导入的图片会保留。`);
        }
        if (!assetIds.length) await reloadAfterUpload();
        complete = true;
        nextSection = state.assets.some(asset => asset.status === "unlabeled" && !["submitted", "approved"].includes(asset.review_status)) ? "ai" : "data";
        document.querySelector(".modal-body").innerHTML = `<div class="upload-complete" role="status"><span class="card-icon">${icon("check")}</span><h3>${totalCreated ? "图片已导入" : "这些图片已经在项目中"}</h3><p>新增 <strong>${totalCreated}</strong> 张图片，跳过 <strong>${totalDuplicates}</strong> 张重复图片。</p><p class="hint">${nextSection === "ai" ? "下一步：运行 AI 标注。完成后检查结果，再提交审核。" : "可以查看已有图片和标注，继续校正或审核。"}</p></div>`;
        document.querySelector("#modal-submit").textContent = nextSection === "ai" ? "开始 AI 标注" : "查看图片";
        document.querySelector("#modal-cancel").textContent = "关闭";
      } finally {
        uploading = false;
        ["#modal-close", "#modal-cancel", "#pick-files", "#pick-folder"].forEach(selector => { const node = document.querySelector(selector); if (node) node.disabled = false; });
      }
    },
  });
}

async function reloadAfterUpload(selectId = null) {
  if (state.currentSection === "annotate") return reloadWorkspaceData(selectId);
  const projectId = state.project.id;
  await refreshProjectSummary(projectId);
  if (state.project?.id === projectId) renderProjectSection(state.currentSection);
}

async function reloadWorkspaceData(selectId = null, { background = false } = {}) {
  if (state.currentSection !== "annotate" || !state.project) return;
  if (state.savingAnnotation || state.workflowBusy || hasAnnotationDraft()) return;
  const projectId = state.project.id;
  const epoch = state.selectionEpoch;
  const previousAssetId = state.asset?.id;
  if (selectId && previousAssetId && selectId !== previousAssetId && state.lockOwned) await releaseCurrentLock();
  const [project, assets, jobs, auditLogs] = await Promise.all([api(`/api/projects/${state.project.id}`), api(`/api/projects/${state.project.id}/assets`), api(`/api/projects/${state.project.id}/jobs`), api(`/api/projects/${state.project.id}/audit-logs?limit=100`)]);
  if (state.currentSection !== "annotate" || state.project?.id !== projectId || state.selectionEpoch !== epoch || state.savingAnnotation || state.workflowBusy || hasAnnotationDraft() || (background && annotationOverlayOpen())) return;
  state.project = project; state.assets = assets; state.jobs = jobs; state.auditLogs = auditLogs;
  if (selectId) state.asset = assets.find(a => a.id === selectId) || assets[0] || null;
  else if (state.asset) state.asset = assets.find(a => a.id === state.asset.id) || assets[0] || null;
  else state.asset = assets[0] || null;
  if (state.asset && !state.lockOwned) await acquireAssetLock(state.asset.id);
  renderWorkspace(); if (state.asset) await loadAnnotations(state.asset.id);
}

async function refreshProjectSummary(projectId) {
  const [project, assets, analytics, versions] = await Promise.all([
    api(`/api/projects/${projectId}`), api(`/api/projects/${projectId}/assets`),
    safeApi(`/api/projects/${projectId}/analytics`, null), safeApi(`/api/projects/${projectId}/versions`, []),
  ]);
  if (state.project?.id !== projectId) return;
  state.project = project; state.assets = assets;
  state.analytics = analytics || fallbackAnalytics(project, assets);
  state.versions = Array.isArray(versions) ? versions : versions.versions || [];
}

function defaultAttributes(label) {
  const schema = label?.attributes_schema || {};
  const required = schema.required || [];
  const properties = schema.properties || {};
  return Object.fromEntries(required.map((name) => {
    const rule = properties[name] || {};
    const value = rule.default ?? (rule.type === "boolean" ? false : rule.type === "number" || rule.type === "integer" ? 0 : rule.type === "array" ? [] : rule.type === "object" ? {} : "");
    return [name, value];
  }));
}

function bindLabelDeleteButtons() {
  document.querySelectorAll("[data-delete-label]").forEach((button) => button.onclick = async (event) => {
    event.stopPropagation();
    if (state.savingAnnotation || state.workflowBusy || hasAnnotationDraft()) return notify("请先完成或取消当前标注，再维护类别。", "warning");
    const labelId = Number(button.dataset.deleteLabel);
    const label = state.project.labels.find(item => item.id === labelId);
    if (!label || !window.confirm(`确定删除类别 ${label.name}？`)) return;
    try {
      await api(`/api/labels/${labelId}`, { method: "DELETE" });
      state.project.labels = state.project.labels.filter(item => item.id !== labelId);
      if (state.activeLabel?.id === labelId) state.activeLabel = state.project.labels[0] || null;
      notify("标签类别已删除");
      renderWorkspace();
    } catch (error) { notify(error.message, "error"); }
  });
}

function openLabelModal() {
  if (!isAdmin()) return notify("请由管理员维护标签类别", "error");
  if (state.savingAnnotation || state.workflowBusy || hasAnnotationDraft()) return notify("请先完成或取消当前标注，再维护类别。", "warning");
  const palette = ["#22c55e", "#38bdf8", "#f97316", "#a78bfa", "#f43f5e", "#facc15"];
  const color = palette[state.project.labels.length % palette.length];
  openModal({ title: "添加标签类别", subtitle: "模型输出的类别名必须与标签名称一致。", body: `
    <div class="field"><label>标签名称</label><input type="text" id="label-name" placeholder="例如：person" maxlength="80" /></div>
    <div class="field"><label>显示颜色</label><div class="color-line"><input type="color" id="label-color" value="${color}" /><input type="text" id="label-color-text" value="${color}" /></div></div>
    <div class="field"><label for="label-shortcut">类别数字键（可选）</label><input type="text" id="label-shortcut" maxlength="1" inputmode="numeric" pattern="[0-9]" placeholder="0–9，例如：2" /><div class="hint">使用本项目尚未分配的数字。留空时可通过点击选择类别。</div></div><details class="technical-details" id="label-advanced"><summary>高级属性校验（可留空）</summary><div class="field"><label>属性规则 JSON</label><textarea id="label-schema" placeholder='{"type":"object","properties":{"occluded":{"type":"boolean","default":false}},"required":["occluded"]}'></textarea><div class="hint">只有需要额外记录遮挡等属性时才配置。支持 properties、required、enum、type 和 additionalProperties。</div></div></details>`, submitText: "添加标签",
    onReady: () => { const picker = document.querySelector("#label-color"); const text = document.querySelector("#label-color-text"); picker.oninput = () => text.value = picker.value; text.oninput = () => { if (/^#[\da-f]{6}$/i.test(text.value)) picker.value = text.value; }; },
    onSubmit: async () => {
      const name = document.querySelector("#label-name").value.trim(); if (!name) throw new Error("请输入标签名称");
      const shortcut = document.querySelector("#label-shortcut").value.trim();
      if (shortcut && !/^[0-9]$/.test(shortcut)) throw new Error("类别快捷键请填写 0–9 中的一个数字，或留空。");
      if (shortcut && state.project.labels.some(label => String(label.shortcut) === shortcut)) throw new Error("这个数字已分配给其他类别，请换一个数字或留空。");
      let attributes_schema = {};
      try { attributes_schema = JSON.parse(document.querySelector("#label-schema").value.trim() || "{}"); }
      catch (_) { document.querySelector("#label-advanced").open = true; throw new Error("高级属性规则格式不正确，请检查 JSON；不需要额外属性时可清空。"); }
      const label = await api(`/api/projects/${state.project.id}/labels`, jsonOptions("POST", { name, color: document.querySelector("#label-color-text").value, shortcut, attributes_schema }));
      state.project.labels.push(label); state.activeLabel = label; closeModal(); renderWorkspace();
    },
  });
}

function aiPromptConfig(model) {
  if (model?.provider_type === "local_yolo") {
    return {
      label: "项目类别（自动读取）",
      value: state.project.labels.map(label => label.name).join(", "),
      placeholder: "请先设置项目类别",
      hint: "此模型只能识别它已训练过的类别，文字说明不会增加识别能力。请确认项目类别与模型一致；需要调整时请联系管理员。",
      readOnly: true,
    };
  }
  if (["local_grounding_dino", "local_sam3"].includes(model?.provider_type)) {
    const sam3 = model.provider_type === "local_sam3";
    return {
      label: `${sam3 ? "分割" : "检测"}目标类别（英文，逗号分隔）`,
      value: state.project.labels.map(label => label.name).join(", "),
      placeholder: "person, helmet, cup",
      hint: `${sam3 ? "SAM 3" : "Grounding DINO"} 会逐个查找这些目标。${isAdmin() ? "预览可尝试新类别，启动正式标注时才会加入项目。" : "请使用项目已有类别；新类别需由管理员添加。"}`,
    };
  }
  return {
    label: "自然语言标注要求",
    value: projectTask() === "classify" ? `请从以下类别中选择最适合整张图片的一个类别：${state.project.labels.map(label => label.name).join("、")}。每张图片只输出一个类别。` : `标注图像中所有属于以下类别的目标：${state.project.labels.map(label => label.name).join("、")}。尽量完整，不确定的目标不要输出。`,
    placeholder: projectTask() === "classify" ? "例如：根据外观判断整张图片为合格或不合格。" : "例如：找出画面中的所有人员和安全帽。",
    hint: `可用标签：${state.project.labels.map(label => label.name).join("、")}。模型只能使用当前项目标签保存结果。`,
  };
}

function parseTargetLabels(value) {
  const labels = [];
  const seen = new Set();
  for (const item of value.split(/[,，、;；\n]+/)) {
    const label = item.trim().replace(/^\.+|\.+$/g, "");
    const key = label.toLocaleLowerCase();
    if (label && !seen.has(key)) { seen.add(key); labels.push(label); }
  }
  return labels;
}

function openAiModal(preselectedAssetIds = null) {
  if (state.currentSection === "annotate" && (state.savingAnnotation || state.workflowBusy || hasAnnotationDraft())) return notify("请先完成或取消当前手动标注，再创建 AI 标注任务。", "warning");
  if (!Array.isArray(preselectedAssetIds)) preselectedAssetIds = null;
  const matchingModels = state.models.filter(modelFitsProject).sort((a, b) => Number(b.configured && b.enabled) - Number(a.configured && a.enabled));
  if (!state.assets.length) return notify("请先上传图片", "error");
  const projectId = state.project.id, projectName = state.project.name, selectionEpoch = state.selectionEpoch, session = state.session, section = state.currentSection;
  const contextCurrent = () => state.project?.id === projectId && state.selectionEpoch === selectionEpoch && state.session === session && state.currentSection === section;
  let sourceModal, preview;
  let previewState = { busy: false, hasFreshResult: false, stale: false };
  const modalCurrent = () => contextCurrent() && sourceModal?.isConnected && modalRoot.querySelector('.modal') === sourceModal;
  const explicitSelection = preselectedAssetIds?.length ? preselectedAssetIds : [...(state.selectedAssetIds || [])];
  let selectedModel = matchingModels.find(model => model.configured && model.enabled && (model.modes.includes("auto") || Boolean(state.asset && !explicitSelection.length)))?.id || null;
  let savedConfig = {};
  try { savedConfig = JSON.parse(localStorage.getItem(`visionforge-ai-config-${state.project.id}`) || "{}"); } catch (_) { /* ignore invalid browser state */ }
  if (matchingModels.some(model => model.id === savedConfig.model_id && model.configured && model.enabled)) selectedModel = savedConfig.model_id;
  const initialPrompt = aiPromptConfig(matchingModels.find(model => model.id === selectedModel));
  if (!initialPrompt.readOnly && savedConfig.model_id === selectedModel && savedConfig.prompt) initialPrompt.value = savedConfig.prompt;
  const initialConfidence = Number.isFinite(Number(savedConfig.confidence_threshold)) ? Math.max(0, Math.min(100, Math.round(Number(savedConfig.confidence_threshold) * 100))) : 35;
  const defaultScope = explicitSelection.length ? "selected" : state.asset ? "current" : "unlabeled";
  const eligibleUnlabeled = state.assets.filter(asset => asset.status === "unlabeled" && !["submitted", "approved"].includes(asset.review_status)).length;
  const scopeAssets = () => {
    const scope = document.querySelector('input[name="scope"]:checked')?.value || defaultScope;
    return state.assets.filter(asset => scope === "all" || (scope === "current" && asset.id === state.asset?.id) || (scope === "selected" && explicitSelection.includes(asset.id)) || (scope === "unlabeled" && asset.status === "unlabeled"));
  };
  const eligibleAssets = () => scopeAssets().filter(asset => document.querySelector("#include-reviewed")?.checked || !["submitted", "approved"].includes(asset.review_status));
  const scopeProblem = () => {
    const model = matchingModels.find(item => item.id === selectedModel);
    const scope = document.querySelector('input[name="scope"]:checked')?.value;
    if (!model?.configured || !model.enabled) return isAdmin() ? "尚无可用模型，请先到“标注模型”完成配置。" : "尚无可用模型，请联系管理员配置。";
    if (!eligibleAssets().length) return "当前范围内没有可处理图片。请选择其他范围；已提交或已通过的图片默认跳过。";
    const interactive = ["interactive_segmentation", "local_sam2"].includes(model.provider_type);
    if ((!model.modes.includes("auto") || interactive) && scope !== "current") return "此模型需要在单张图片上提供位置提示。请先打开图片，或选择支持批量标注的模型。";
    if (interactive && !state.assistPoints.length && !state.selectedAnnotation) return "请先返回画布：用“+”标记要保留的位置，或画一个粗框并选中，然后再运行 AI。";
    return "";
  };
  const readAiPayload = () => {
      const problem = scopeProblem(); if (problem) throw new Error(problem);
      const prompt = document.querySelector("#ai-prompt").value.trim(); if (!prompt) throw new Error("请输入标注要求");
      const scope = document.querySelector('input[name="scope"]:checked').value;
      const model = matchingModels.find(item => item.id === selectedModel);
      if (!model?.configured) throw new Error("请选择已配置的模型服务");
      const conceptModel = ["local_grounding_dino", "local_sam3"].includes(model.provider_type);
      const targetLabels = conceptModel ? parseTargetLabels(prompt) : null;
      if (conceptModel && !targetLabels.length) throw new Error("请至少填写一个目标类别");
      const scopedAssetIds = eligibleAssets().map(asset => asset.id);
      if (Array.isArray(scopedAssetIds) && !scopedAssetIds.length) throw new Error("所选范围内没有可处理的图片");
      let mode = scope === "current" && model.modes.includes("assist") ? "assist" : "auto";
      if (!model.modes.includes(mode)) throw new Error(`${model.name} 不支持当前处理范围所需的${mode === "auto" ? "全自动" : "半自动"}模式`);
      const interactive = ["interactive_segmentation", "local_sam2"].includes(model.provider_type);
      if (interactive && scope !== "current") throw new Error("交互式分割一次只能处理当前图片");
      if (interactive && !state.assistPoints.length && !state.selectedAnnotation) throw new Error("请先用正点/负点工具添加提示，或选中一个人工粗框");
      const hints = scope === "current" && mode === "assist" && state.assistPoints.length ? { points: state.assistPoints, label: state.activeLabel?.name } : null;
      return { model_id: selectedModel, prompt, target_labels: targetLabels, mode, asset_ids: scopedAssetIds, hint_annotation_id: interactive && state.selectedAnnotation ? state.selectedAnnotation.id : null, hints, replace_existing_auto: sourceModal.querySelector("#replace-auto").checked, confidence_threshold: Number(sourceModal.querySelector("#confidence").value) / 100, include_reviewed: sourceModal.querySelector("#include-reviewed").checked };
  };
  const workBlockAiProblem = () => {
    if (!state.activeWorkBlock?.human_started) return "";
    const scope = document.querySelector('input[name="scope"]:checked')?.value;
    const model = matchingModels.find(item => item.id === selectedModel);
    return state.activeWorkBlock.is_mine && scope === "current" && model?.modes.includes("assist") && canEditCurrentAsset() ? "" : "已领取的任务块不接受批量 AI 覆盖。负责人可在当前图片使用半自动辅助；这里仍可预览效果。";
  };
  const updateScopeSummary = () => {
    const model = matchingModels.find(item => item.id === selectedModel);
    const requiresCurrent = model && (!model.modes.includes("auto") || ["interactive_segmentation", "local_sam2"].includes(model.provider_type));
    document.querySelectorAll('input[name="scope"]').forEach(input => { input.disabled = input.value === "current" ? !state.asset : requiresCurrent || (input.value === "selected" && !explicitSelection.length); });
    const current = document.querySelector('input[name="scope"]:checked');
    if (current?.disabled) { current.checked = false; const fallback = document.querySelector('input[name="scope"]:not([disabled])'); if (fallback) fallback.checked = true; }
    const eligible = eligibleAssets();
    const skipped = scopeAssets().length - eligible.length;
    const reviewed = document.querySelector("#include-reviewed").checked ? eligible.filter(asset => ["submitted", "approved"].includes(asset.review_status)).length : 0;
    const problem = scopeProblem() || workBlockAiProblem();
    document.querySelector("#ai-scope-summary").innerHTML = `<strong>${problem ? "暂时无法启动" : `将处理 ${eligible.length} 张图片`}</strong><span>${escapeHtml(problem || `${skipped ? `已跳过 ${skipped} 张待审核或已通过图片。` : ""}${reviewed ? `${reviewed} 张图片的审核状态会重置，需要重新提交审核。` : "完成后请检查结果，再提交审核。"}`)}</span>`;
    const submit = sourceModal.querySelector("#modal-submit");
    submit.disabled = Boolean(problem || previewState.busy);
    submit.textContent = `${previewState.hasFreshResult ? "按预览参数标注" : "启动标注"} ${eligible.length} 张`;
    const feedback = sourceModal.querySelector("#ai-batch-preview-state");
    if (feedback) feedback.textContent = previewState.busy ? "正在预览，完成后可启动标注。" : previewState.hasFreshResult ? "当前参数已预览。启动后会保存正式标注。" : previewState.stale ? "参数或样本已修改，建议重新预览效果。" : "建议先预览效果，再启动正式标注。";
    document.querySelector("#ai-model-name").textContent = model?.name || "尚未配置";
  };
  const parametersChanged = () => { updateScopeSummary(); preview?.refresh(); };
  openModal({ title: "AI 标注 · 预览与批量运行", subtitle: "先用样本查看效果，调整参数后再处理整批图片。预览不会保存或覆盖现有标注。", wide: true, body: `
    <div class="ai-preview-layout"><section class="ai-parameter-panel" aria-label="AI 标注参数">
    <details class="advanced-settings ai-model-selector" id="ai-model-selector" ${selectedModel ? "" : "open"}><summary>标注模型：<span id="ai-model-name">${escapeHtml(matchingModels.find(model => model.id === selectedModel)?.name || "尚未配置")}</span> <small>更换模型</small></summary><p class="hint">不同模型擅长的内容不同。建议先用少量图片试标，确认结果后再处理整批图片。</p><div class="model-grid" id="model-grid">${matchingModels.map(model => `<button type="button" class="model-card ${model.id === selectedModel ? "selected" : ""} ${!model.configured || !model.enabled ? "unavailable" : ""}" data-model-id="${model.id}" aria-pressed="${model.id === selectedModel}" ${model.configured && model.enabled ? "" : "disabled"}><span class="badge">${!model.enabled ? "已停用" : model.configured ? "可用" : "未配置"}</span><h3>${escapeHtml(model.name)}</h3><p>${escapeHtml(model.description)}</p><div class="capabilities">${model.modes.map(mode => `<span>${mode === "auto" ? "全自动" : "半自动"}</span>`).join("")}${model.capabilities.map(c => `<span>${escapeHtml(c)}</span>`).join("")}</div></button>`).join("")}</div>${selectedModel ? "" : `<div class="job-error">${isAdmin() ? '尚无可用模型，请先到“标注模型”配置。' : '尚无可用模型，请联系管理员。'}</div>`}</details>
    <div class="field"><label id="ai-prompt-label" for="ai-prompt">${escapeHtml(initialPrompt.label)}</label><textarea id="ai-prompt" ${initialPrompt.readOnly ? "readonly" : ""} placeholder="${escapeHtml(initialPrompt.placeholder)}">${escapeHtml(initialPrompt.value)}</textarea><div class="hint" id="ai-prompt-hint">${escapeHtml(initialPrompt.hint)}</div></div>
    <div class="field"><label for="confidence">最低置信度</label><div class="range-line"><input id="confidence" type="range" min="0" max="100" step="1" value="${initialConfidence}" aria-describedby="ai-confidence-hint" /><span class="range-value" id="confidence-value">${initialConfidence}%</span></div><div class="hint" id="ai-confidence-hint">调低可发现更多目标，但可能增加误检；调高更严格，但可能漏标。调整后点击“预览效果”比较结果。</div></div>
    <div class="field"><label>处理范围</label><div class="scope-grid"><label class="scope-option"><input type="radio" name="scope" value="current" ${defaultScope === "current" ? "checked" : ""} ${state.asset ? "" : "disabled"}/><span><strong>当前图片</strong><small>${state.asset ? escapeHtml(state.asset.filename) : "未打开图片"}</small></span></label><label class="scope-option"><input type="radio" name="scope" value="selected" ${defaultScope === "selected" ? "checked" : ""} ${explicitSelection.length ? "" : "disabled"}/><span><strong>已选图片</strong><small>${explicitSelection.length} 张</small></span></label><label class="scope-option"><input type="radio" name="scope" value="unlabeled" ${defaultScope === "unlabeled" ? "checked" : ""}/><span><strong>所有未标注</strong><small>${eligibleUnlabeled} 张可处理</small></span></label><label class="scope-option"><input type="radio" name="scope" value="all" ${defaultScope === "all" ? "checked" : ""}/><span><strong>全部数据</strong><small>${state.assets.length} 张（已审核默认跳过）</small></span></label></div></div>
    <div class="ai-scope-summary" id="ai-scope-summary" role="status" aria-live="polite"></div>

    <details class="advanced-settings" id="ai-advanced"><summary>已有标注与审核设置</summary>
    <div class="check-stack"><label class="check-line"><input id="replace-auto" type="checkbox" checked />替换之前的 AI 结果，保留人工绘制和人工校正的标注</label><label class="check-line" ${canReview() ? "" : "hidden"}><input id="include-reviewed" type="checkbox" ${canReview()?"":"disabled"}/>重新标注待审核或已通过图片（会恢复为草稿，需要再次提交审核）</label></div></details>${state.assistPoints.length ? `<div class="assist-summary">当前画布已有 ${state.assistPoints.filter(point => point.label).length} 个正点、${state.assistPoints.filter(point => !point.label).length} 个负点，将作为半自动分割提示。</div>` : ""}</section>${aiPreviewMarkup()}</div>`,
    submitText: "启动标注任务", onClose: () => preview?.dispose(), onReady: () => {
      sourceModal = modalRoot.querySelector('.modal');
      sourceModal.classList.add("ai-preview-modal");
      const batchHint = document.createElement("span"); batchHint.id = "ai-batch-preview-state"; batchHint.className = "ai-batch-preview-state"; batchHint.setAttribute("role", "status");
      sourceModal.querySelector(".modal-actions").prepend(batchHint);
      document.querySelectorAll(".model-card").forEach(card => card.onclick = () => {
        const model = matchingModels.find(m => m.id === card.dataset.modelId); if (!model.configured || !model.enabled) return notify(`${model.name} 尚未配置，请联系管理员`, "error");
        const promptInput = document.querySelector("#ai-prompt");
        const previousConfig = aiPromptConfig(matchingModels.find(item => item.id === selectedModel));
        const wasDefault = promptInput.value === previousConfig.value;
        selectedModel = model.id; document.querySelectorAll(".model-card").forEach(c => { c.classList.toggle("selected", c.dataset.modelId === selectedModel); c.setAttribute("aria-pressed", String(c.dataset.modelId === selectedModel)); });
        const config = aiPromptConfig(model);
        document.querySelector("#ai-prompt-label").textContent = config.label;
        document.querySelector("#ai-prompt-hint").textContent = config.hint;
        promptInput.placeholder = config.placeholder;
        promptInput.readOnly = Boolean(config.readOnly);
        if (wasDefault || config.readOnly || previousConfig.readOnly) promptInput.value = config.value;
        if (!model.modes.includes("auto") && state.asset) document.querySelector('input[name="scope"][value="current"]').checked = true;
        parametersChanged();
      });
      document.querySelectorAll('input[name="scope"], #include-reviewed, #replace-auto').forEach(input => input.onchange = parametersChanged);
      sourceModal.querySelector("#ai-prompt").addEventListener("input", parametersChanged);
      updateScopeSummary();
      preview = mountAiPreview(sourceModal.querySelector("[data-ai-preview]"), {
        api, jsonOptions, escapeHtml, projectId, getAssets: eligibleAssets, getPayload: readAiPayload,
        isCurrent: modalCurrent, keypointSchema: state.project.keypoint_schema,
        onChange: value => { previewState = value; if (modalCurrent()) updateScopeSummary(); },
      });
      document.querySelector("#confidence").oninput = event => {
        document.querySelector("#confidence-value").textContent = `${event.target.value}%`;
        parametersChanged();
      };
    }, onSubmit: async () => {
      if (!modalCurrent()) throw new Error("页面已切换，请关闭此窗口，在当前项目重新开始 AI 标注。");
      if (previewState.busy) throw new Error("请等待预览完成，再启动正式标注。");
      const blockProblem = workBlockAiProblem();
      if (blockProblem) throw new Error(blockProblem);
      const payload = readAiPayload();
      const { prompt, target_labels: targetLabels, asset_ids: scopedAssetIds } = payload;
      const assetId = state.asset?.id;
      const releasesCurrentAsset = Boolean(assetId && scopedAssetIds.includes(assetId));
      if (releasesCurrentAsset) {
        await releaseCurrentLock(assetId);
        if (!modalCurrent()) return;
        state.readOnly = true; state.readOnlyReason = "AI 正在处理这张图片，请完成后再检查或校正。";
      }
      let job;
      try {
        job = await api(`/api/projects/${projectId}/jobs`, jsonOptions("POST", payload));
      } catch (error) {
        if (!modalCurrent()) return;
        if (releasesCurrentAsset && state.asset?.id === assetId) {
          await acquireAssetLock(assetId, selectionEpoch);
          if (!modalCurrent()) return;
          state.readOnly = !state.lockOwned;
        }
        throw error;
      }
      if (!modalCurrent()) {
        if (state.session === session) notify(`“${projectName}”的 AI 标注任务已创建，可返回该项目查看进度。`);
        return;
      }
      try { localStorage.setItem(`visionforge-ai-config-${projectId}`, JSON.stringify({ model_id:selectedModel, prompt, confidence_threshold:payload.confidence_threshold })); } catch (_) { /* Browser preferences do not affect accepted jobs. */ }
      setSectionTab(state.session, projectId, "ai", "history");
      state.jobs.unshift(job); state.assistPoints = []; state.inspectorTab = "jobs"; closeModal(); notify("AI 标注任务已创建，可在任务列表查看进度。");
      if (state.currentSection === "annotate") { renderWorkspace(); startJobPolling(); }
      else if (state.currentSection === "ai") {
        resetHistoryNavigation(projectId, "ai"); renderAiPage();
        app.querySelector(`[data-job-id="${job.id}"]`)?.scrollIntoView({ block: "start", behavior: "auto" });
        startJobPolling();
      }
      else location.hash = `#/projects/${projectId}/ai`;
      if (targetLabels) {
        try {
          const project = await api(`/api/projects/${projectId}`);
          if (contextCurrent()) {
            const activeLabelId = state.activeLabel?.id; state.project = project;
            state.activeLabel = project.labels.find(label => label.id === activeLabelId) || project.labels[0] || null;
            if (state.currentSection === "annotate") refreshInspector();
          }
        } catch (_) { if (contextCurrent()) notify("任务已创建，类别列表暂未刷新。稍后刷新页面即可，请勿重复启动。", "warning"); }
      }
    },
  });
}

function openExportModal() {
  const count = () => state.assets.filter(asset => !document.querySelector("#approved-only").checked || asset.review_status === "approved").length;
  openModal({ title:"导出当前数据", subtitle:"下载图片和标注文件。若要在本平台训练，请先到“数据版本”准备训练数据。", body:`
    <div class="field"><label>文件格式</label>${exportFormatMarkup("format", projectTask())}</div><label class="check-line"><input id="approved-only" type="checkbox" checked/>仅导出审核通过的图片（建议）</label><div class="inline-notice" id="export-summary" role="status"></div><details class="technical-details"><summary>与数据版本导出的区别</summary><p>这里下载的是当前数据。数据版本会保留生成时的图片与标注，方便重复训练和比较结果。</p></details>`,
    submitText:"下载数据 ZIP", onReady: () => {
      const update = () => { const total = count(); document.querySelector("#export-summary").textContent = total ? `将导出 ${total} 张图片及其标注。${document.querySelector("#approved-only").checked ? "" : "包含未审核数据，请先确认质量。"}` : "没有符合条件的图片。请先检查并审核图片，或取消“仅导出审核通过的图片”。"; document.querySelector("#modal-submit").disabled = !total; };
      document.querySelector("#approved-only").onchange = update; update();
    }, onSubmit: async () => {
      if (!count()) throw new Error("当前范围内没有可导出的图片，请先完成审核或调整导出范围。");
      const format = document.querySelector('input[name="format"]:checked').value;
      await downloadArtifact(`/api/projects/${state.project.id}/export/${format}?approved_only=${document.querySelector("#approved-only").checked}`, `${state.project.name}-${format}.zip`);
      closeModal();
    },
  });
}

async function changeJob(jobId, action) {
  if (!state.project || state.jobActionIds.has(jobId)) return;
  if (action === "cancel" && state.jobs.find(job => job.id === jobId)?.cancel_requested) {
    return notify("取消请求已接受，请等待当前图片处理结束。", "warning");
  }
  const projectId = state.project.id;
  const selectionEpoch = state.selectionEpoch;
  const section = state.currentSection;
  const isCurrent = () => state.authReady !== false && state.project?.id === projectId
    && state.selectionEpoch === selectionEpoch && state.currentSection === section;
  let acceptedJob = null;
  state.jobActionIds.add(jobId);
  document.querySelectorAll(`[data-${action}-job="${jobId}"]`).forEach(button => { button.disabled = true; button.textContent = "正在处理…"; });
  try {
    acceptedJob = await api(`/api/jobs/${jobId}/${action}`, { method: "POST" });
    notify(action === "cancel" ? "已请求取消。当前图片处理结束后停止，已完成结果会保留。" : "任务已重新排队，可在这里查看进度。");
    if (!isCurrent()) return;
    state.jobs = state.jobs.map(job => job.id === jobId ? acceptedJob : job);
    try {
      const jobs = await api(`/api/projects/${projectId}/jobs`);
      if (isCurrent()) state.jobs = jobs;
    } catch (error) {
      if (error.status !== 401 && isCurrent()) notify(`${action === "cancel" ? "取消请求已接受" : "任务已重新排队"}，但列表暂未刷新。请稍后刷新查看，无需重复操作。`, "warning");
    }
  } catch (error) { notify(error.message, "error"); }
  finally {
    state.jobActionIds.delete(jobId);
    if (isCurrent()) {
      if (acceptedJob) {
        if (section === "annotate") renderWorkspace(); else renderProjectSection(section);
        startJobPolling();
      } else {
        document.querySelectorAll(`[data-${action}-job="${jobId}"]`).forEach(button => { button.disabled = false; button.textContent = action === "cancel" ? "取消任务" : "重试任务"; });
      }
    }
  }
}
async function cancelJob(jobId) { await changeJob(jobId, "cancel"); }
async function retryJob(jobId) { await changeJob(jobId, "retry"); }

function openModal({ title, subtitle = "", body, submitText = "确定", wide = false, onReady, onSubmit, onClose }) {
  modalCleanup?.(); modalCleanup = onClose || null;
  annotationMenu?.close({ restoreFocus: false, force: true });
  modalReturnFocus = document.activeElement;
  modalRoot.innerHTML = `<div class="modal-backdrop"><div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-labelledby="modal-title" aria-describedby="modal-description"><div class="modal-head"><div><h2 id="modal-title">${escapeHtml(title)}</h2><p id="modal-description">${escapeHtml(subtitle)}</p></div><button class="tool" id="modal-close" aria-label="关闭对话框">${icon("close")}</button></div><div class="modal-body">${body}</div><div id="modal-feedback" class="modal-feedback" role="alert" tabindex="-1" hidden></div><div class="modal-actions"><button class="btn ghost" id="modal-cancel">取消</button><button class="btn primary" id="modal-submit">${escapeHtml(submitText)}</button></div></div></div>`;
  const modal = document.querySelector(".modal");
  const submit = modal.querySelector("#modal-submit");
  const cancel = modal.querySelector("#modal-cancel");
  const closeButton = modal.querySelector("#modal-close");
  const bodyElement = modal.querySelector(".modal-body");
  let busy = false;
  const close = () => { if (!busy) closeModal(); };
  closeButton.onclick = close; cancel.onclick = close;
  modal.parentElement.addEventListener("pointerdown", event => { if (event.target === modal.parentElement) close(); });
  submit.onclick = async () => {
    if (busy) return;
    const previous = submit.innerHTML;
    const feedback = modal.querySelector("#modal-feedback");
    feedback.hidden = true; feedback.replaceChildren();
    busy = true; submit.disabled = true; cancel.disabled = true; closeButton.disabled = true;
    bodyElement.inert = true; modal.setAttribute("aria-busy", "true");
    submit.innerHTML = `<span class="spinner"></span>正在处理…`;
    try { await onSubmit(); }
    catch (error) {
      if (!modal.isConnected && error.status !== 401) notify(error.message, "error");
      if (modal.isConnected) {
        feedback.innerHTML = `<strong>${escapeHtml(error.message)}</strong>${error.technicalMessage ? `<details class="technical-details"><summary>技术详情（可提供给管理员）</summary><pre>${escapeHtml(error.technicalMessage)}</pre></details>` : ""}`;
        feedback.hidden = false; feedback.focus();
      }
    } finally {
      busy = false; bodyElement.inert = false; modal.setAttribute("aria-busy", "false");
      if (modal.isConnected) {
        submit.disabled = false; cancel.disabled = false; closeButton.disabled = false;
        if (submit.innerHTML.includes("spinner")) submit.innerHTML = previous;
      }
    }
  };
  modal.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { event.preventDefault(); close(); return; }
    if (event.key !== "Tab") return;
    const focusable = [...modal.querySelectorAll('button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), summary, [href]')].filter(element => element.getClientRects().length && !element.closest("[inert]"));
    if (!focusable.length) return;
    const first = focusable[0]; const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  onReady?.(); setTimeout(() => { if (modal.isConnected) (modal.querySelector("[autofocus]") || modal.querySelector("#modal-close"))?.focus(); }, 30);
}

function closeModal() { const cleanup = modalCleanup; modalCleanup = null; cleanup?.(); modalRoot.innerHTML = ""; if (modalReturnFocus?.isConnected) modalReturnFocus.focus(); modalReturnFocus = null; }

async function refreshCurrentAssetFromJob(projectId) {
  const assetId = state.asset?.id;
  if (!assetId || state.currentSection !== "annotate" || state.savingAnnotation || state.workflowBusy || hasAnnotationDraft()) return;
  const epoch = state.selectionEpoch;
  try {
    const [asset, annotations] = await Promise.all([
      api(`/api/assets/${assetId}`),
      api(`/api/assets/${assetId}/annotations`),
    ]);
    if (state.project?.id !== projectId || state.asset?.id !== assetId || state.selectionEpoch !== epoch || state.currentSection !== "annotate" || state.savingAnnotation || state.workflowBusy || hasAnnotationDraft() || annotationOverlayOpen()) return;
    const index = state.assets.findIndex(item => item.id === assetId);
    if (index >= 0) state.assets[index] = { ...state.assets[index], ...asset };
    state.asset = index >= 0 ? state.assets[index] : asset;
    const selectedId = state.selectedAnnotation?.id;
    state.annotations = annotations;
    state.selectedAnnotation = annotations.find(item => item.id === selectedId) || null;
    refreshShapes();
    refreshInspector();
  } catch (_) { /* the final workspace refresh remains authoritative */ }
}

function startJobPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = null;
  if (!state.project || !state.jobs.some(job => ["queued", "running"].includes(job.status))) return;
  const projectId = state.project.id;
  let polling = false;
  let healthTick = 0;
  const poll = async () => {
    if (polling || navigationIsOpen() || state.project?.id !== projectId || (state.currentSection === "annotate" && (state.savingAnnotation || state.workflowBusy || hasAnnotationDraft() || annotationOverlayOpen()))) return;
    polling = true;
    try {
      const previous = new Map(state.jobs.map(job => [job.id, job]));
      const jobs = await api(`/api/projects/${projectId}/jobs`);
      if (++healthTick % 4 === 1) state.health = await api("/api/health");
      if (navigationIsOpen() || state.project?.id !== projectId || (state.currentSection === "annotate" && (state.savingAnnotation || state.workflowBusy || hasAnnotationDraft() || annotationOverlayOpen()))) return;
      state.jobs = jobs;
      const progressed = state.jobs.some(job => job.processed > (previous.get(job.id)?.processed || 0));
      if (state.currentSection === "annotate") {
        refreshJobActivity();
        if (progressed) await refreshCurrentAssetFromJob(projectId);
        else if (state.inspectorTab === "jobs") refreshInspector();
      } else if (["ai", "overview"].includes(state.currentSection)) {
        renderProjectSection(state.currentSection);
      }

      const transitioned = state.jobs.filter(job => {
        const old = previous.get(job.id);
        return old && ["queued", "running"].includes(old.status) && !["queued", "running"].includes(job.status);
      });
      const running = state.jobs.some(job => ["queued", "running"].includes(job.status));
      if (!running) {
        clearInterval(state.pollTimer); state.pollTimer = null;
        if (state.currentSection === "annotate") await reloadWorkspaceData(null, { background: true });
        else {
          await refreshProjectSummary(projectId);
          if (["ai", "overview"].includes(state.currentSection)) renderProjectSection(state.currentSection);
        }
        const completed = transitioned.filter(job => job.status === "completed");
        const failed = transitioned.filter(job => job.status === "failed");
        const cancelled = transitioned.filter(job => job.status === "cancelled");
        if (completed.length) {
          const created = completed.reduce((sum, job) => sum + job.created_count, 0);
          const processed = completed.reduce((sum, job) => sum + job.processed, 0);
          const detail = completed[0].progress_message?.replace(/^完成：?/, "");
          notify(created ? `AI 标注完成：处理 ${processed} 张图片，新增 ${created} 个标注` : `AI 标注完成：${detail || "未产生符合条件的结果"}`);
        }
        if (failed.length) notify(`AI 标注任务失败：${failed[0].error || "请查看任务详情"}`, "error");
        if (cancelled.length) notify("AI 标注任务已取消");
      }
    } catch (_) { /* keep polling through temporary network errors */ }
    finally { polling = false; }
  };
  void poll();
  state.pollTimer = setInterval(poll, 1200);
}

function startCollaborationPolling() {
  clearInterval(state.collaborationTimer);
  const projectId=state.project?.id;
  let polling=false;
  state.collaborationTimer=setInterval(async()=>{
    if(polling || document.hidden || hasAnnotationDraft() || annotationOverlayOpen() || state.currentSection!=="annotate" || state.project?.id!==projectId || state.workflowBusy || state.savingAnnotation || state.shapeEdit || state.drawing || state.polygon.length || state.poseDraft) return;
    polling=true;
    try {
      const epoch = state.selectionEpoch;
      const [assets, block] = await Promise.all([
        api(`/api/projects/${projectId}/assets`),
        state.activeWorkBlockId ? api(`/api/projects/${projectId}/work-blocks/${state.activeWorkBlockId}`) : Promise.resolve(null),
      ]);
      if(epoch !== state.selectionEpoch || hasAnnotationDraft() || annotationOverlayOpen() || state.project?.id!==projectId || state.currentSection!=="annotate" || state.workflowBusy || state.savingAnnotation || state.shapeEdit || state.drawing || state.polygon.length || state.poseDraft) return;
      const available = new Set(assets.map(item => item.id));
      const blockChanged = block && (block.revision !== state.activeWorkBlock?.revision || block.status !== state.activeWorkBlock?.status || block.is_mine !== state.activeWorkBlock?.is_mine);
      if (block) {
        const lostOwnership = state.activeWorkBlock?.is_mine && !block.is_mine;
        state.activeWorkBlock = block;
        if (lostOwnership) {
          clearInterval(state.lockTimer); state.lockTimer = null; state.lockOwned = false;
          state.readOnly = true; state.readOnlyReason = "任务块已交回或转交给其他成员，当前仅供查看。";
          notify(state.readOnlyReason, "warning");
        }
      }
      const removed = state.assets.some(item => !available.has(item.id));
      const asset=assets.find(item=>item.id===state.asset?.id), changed=asset && asset.revision!==state.asset.revision;
      state.assets=assets; if(asset) state.asset=asset;
      state.selectedAssetIds = new Set([...state.selectedAssetIds].filter(id => available.has(id)));
      if (!state.activeWorkBlockId && !state.legacyReviewAssetId) {
        state.project.asset_count = assets.length;
        state.project.labeled_count = assets.filter(item => item.status === "labeled").length;
      }
      if (state.asset && !asset) {
        clearInterval(state.lockTimer); state.lockTimer = null;
        state.lockOwned = false; state.asset = null; state.annotations = []; state.selectedAnnotation = null;
        state.reviewEvents = []; state.assistPoints = []; state.keypointPlacement = null; state.handedOffAssetId = null;
        state.readOnly = false; state.readOnlyReason = ""; state.saveFeedback = {status:"idle", message:""};
        notify(assets.length ? "当前图片已被管理员删除，已打开下一张图片。" : "当前图片已被管理员删除，项目中已没有图片。", "warning");
        if (assets.length) await selectAsset(assets[0].id);
        else {
          state.selectionEpoch += 1;
          history.replaceState(null, "", annotationBlockURL(projectId, state.activeWorkBlockId));
          renderWorkspace();
        }
        return;
      }
      if(changed) await loadAnnotations(asset.id);
      if (epoch !== state.selectionEpoch || state.currentSection !== "annotate" || state.project?.id !== projectId) return;
      if(state.asset && !state.legacyReviewAssetId && (!state.activeWorkBlock || state.activeWorkBlock.is_mine) && state.handedOffAssetId !== state.asset.id && !state.lockOwned && !["submitted","approved"].includes(state.asset.review_status)) {
        await acquireAssetLock(state.asset.id,state.selectionEpoch,true);
        if(state.lockOwned) renderWorkspace();
      }
      if (removed || blockChanged) renderWorkspace();
      else assets.slice(0,state.assetRenderLimit).forEach(item=>refreshAssetQueueItem(item.id));
    } catch(_) { /* Retry on the next collaborative refresh. */ }
    finally { polling=false; }
  },10000);
}

function runningJobCount() { return state.jobs.filter(job => ["queued", "running"].includes(job.status)).length; }
function formatBytes(bytes) { if (bytes < 1024) return `${bytes} B`; if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`; return `${(bytes / 1048576).toFixed(1)} MB`; }

window.addEventListener("hashchange", route);
window.addEventListener("beforeunload", event => {
  if (state.currentSection === "annotate" && (state.savingAnnotation || state.workflowBusy || hasAnnotationDraft())) {
    event.preventDefault(); event.returnValue = "";
  }
});
window.addEventListener("pagehide", () => {
  if (!state.lockOwned || !state.asset) return;
  fetch(`/api/assets/${state.asset.id}/lock`, { method: "DELETE", headers: { "X-Actor-ID": actorId, "X-Edit-Token": editorToken, "X-MDP-Client": "browser" }, credentials: "same-origin", keepalive: true });
});
document.addEventListener("keydown", event => {
  const chip = event.target.closest?.(".label-chip");
  if (chip && ["Enter", " "].includes(event.key)) {
    event.preventDefault(); event.stopPropagation();
    if (!event.repeat) selectAnnotation(Number(chip.dataset.annotationId));
    return;
  }
  if (!event.target.matches?.("[data-label-id], .annotation-row") || !["Enter", " "].includes(event.key)) return;
  event.preventDefault(); event.stopPropagation();
  if (!event.repeat) event.target.click();
});
document.addEventListener("click", async event => {
  if (event.target.closest("[data-start-manual]")) startManualAnnotation();
  if (event.target.closest("[data-finish-polygon]")) void finishPolygon();
  if (event.target.closest("[data-skip-draft-keypoint]")) document.querySelector("#skip-keypoint")?.click();
  if (event.target.closest("[data-finish-draft-keypoints]")) void finishKeypoints();
  if (event.target.closest("[data-cancel-draft]")) { cancelShapeEdit(); cancelDrawing(true); }
  if (event.target.closest("#retry-annotation-save")) void retryPendingAnnotation();
  if (event.target.closest("#session-button") && state.authMode !== "disabled") void logout();
  const reload = event.target.closest("#reload-saved-annotations");
  if (reload && state.asset && !state.savingAnnotation) {
    const assetId = state.asset.id, epoch = state.selectionEpoch;
    const isCurrent = () => state.currentSection === "annotate" && state.asset?.id === assetId && state.selectionEpoch === epoch;
    reload.disabled = true; state.savingAnnotation = true; refreshWorkflowState(); refreshCanvasStatus();
    try {
      const [asset, annotations] = await Promise.all([api(`/api/assets/${assetId}`), api(`/api/assets/${assetId}/annotations`)]);
      if (!isCurrent()) return;
      const index = state.assets.findIndex(item => item.id === assetId);
      state.asset = { ...state.asset, ...asset };
      if (index >= 0) state.assets[index] = state.asset;
      state.annotations = annotations; state.selectedAnnotation = null; state.selectionIntent = false;
      state.pendingAnnotation = null; state.pendingTaskEdit = null; state.pendingObjectEdit = null; state.selectionIntent = false; state.poseDraft = null; state.drawing = null; state.polygon = [];
      state.keypointPlacement = null; state.advancedAnnotationDirty = false; state.advancedDraft = null;
      if (["submitted", "approved"].includes(asset.review_status)) state.readOnly = true;
      setSaveFeedback("idle");
      notify("已重新加载服务器上的结果，未保存的修改已放弃。");
    } catch (error) {
      if (isCurrent()) setSaveFeedback("error", `重新加载未完成。${error.message}`);
    } finally {
      if (isCurrent()) { state.savingAnnotation = false; drawDraft(); refreshShapes(); refreshInspector(); refreshWorkflowState(); refreshCanvasStatus(); }
    }
  }
});
const menuTargetCurrent = id => {
  const context = annotationMenuContext, item = state.annotations.find(annotation => annotation.id === id);
  if (!context?.current() || !item || context.versions.get(id) !== item.version) {
    annotationMenu?.close({ restoreFocus: false, force: true });
    notify("对象状态已变化，请重新选择后操作。", "warning"); return false;
  }
  return true;
};
annotationMenu = createAnnotationContextMenu({
  state, escapeHtml, canEditCurrentAsset: () => canEditCurrentAsset() && !hasAnnotationDraft(),
  onSelect: id => menuTargetCurrent(id) && selectAnnotation(id),
  onEdit: id => menuTargetCurrent(id) && editSelectedObject(id),
  onChangeLabel: async (id, labelId) => {
    if (!menuTargetCurrent(id)) return false;
    const saved = await changeObjectLabel(id, labelId);
    if (state.pendingObjectEdit) annotationMenu.close({ restoreFocus: true, force: true });
    return saved;
  },
  onDelete: async id => {
    if (!menuTargetCurrent(id) || !selectAnnotation(id)) return false;
    await deleteSelectedAnnotation();
    return !state.annotations.some(item => item.id === id);
  },
  onCopy: id => menuTargetCurrent(id) && copyObject(id),
  onContinue: () => Boolean(annotationMenuContext?.current()) && continueManualDrawing(),
  onClose: () => { annotationMenuContext = null; canvasGuides?.refresh(); },
});
const annotationShortcuts = installAnnotationShortcuts({
  state, projectTask, taskAllowsTool, canEditCurrentAsset, hasDraft: hasAnnotationDraft,
  hasModal: annotationOverlayOpen, navigationIsOpen, openModal, closeModal, escapeHtml, notify,
  setTool, setCanvasZoom, fitCanvas, navigateAsset, chooseTaskLabel: chooseWorkspaceLabel, finishPolygon, finishKeypoints,
  undoPolygonPoint, undoKeypointDraft: () => undoKeypointDraft(), retryPendingAnnotation,
  pasteCopiedAnnotation, deleteSelectedAnnotation, clearObjectSelection, cancelDrawing, cancelShapeEdit, stopCanvasPan, refreshInspector,
});
function openAnnotationShortcutsHelp() { annotationShortcuts.openHelp(); }
export { openAnnotationShortcutsHelp, annotationShortcutHint, annotationShortcutsHelp };
window.matchMedia("(max-width: 759px)").addEventListener("change", event => {
  if (!event.matches) return;
  state.queueCollapsed = true; state.inspectorCollapsed = true;
  if (state.annotationPanelRestore) state.annotationPanelRestore = { queueCollapsed: true, inspectorCollapsed: true };
  if (state.currentSection === "annotate") applyAnnotationLayout();
});

async function pasteCopiedAnnotation() {
  if (!canEditCurrentAsset()) return notify("当前图片不可编辑", "error");
  if (hasAnnotationDraft()) return notify("请先完成或取消当前草稿，再粘贴对象。", "warning");
  const source = state.copiedAnnotation;
  if (!source || source._copiedProjectId !== state.project.id) return notify("请在当前项目重新复制对象。", "warning");
  if (source.shape_type === "classification") return chooseTaskLabel(source.label_id);
  const label = state.project.labels.find(item => item.id === source.label_id);
  if (!label) return notify("复制对象的类别已不存在，请重新选择对象复制。", "error");
  const points = (source.points || []).map((value, index) => Math.max(0, Math.min(index % 2 ? state.asset.height : state.asset.width, value + 8)));
  const keypoints = (source.keypoints || []).map(point => point.visibility ? { ...point, x: Math.max(0, Math.min(state.asset.width, point.x + 8)), y: Math.max(0, Math.min(state.asset.height, point.y + 8)) } : { ...point });
  state.pendingAnnotation = { shapeType: source.shape_type, points, keypoints, labelId: label.id,
    attributes: JSON.parse(JSON.stringify(source.attributes || defaultAttributes(label))), expectedRevision: state.asset.revision };
  const saved = await saveAnnotation(source.shape_type, points, keypoints, label.id);
  if (saved) notify("已粘贴对象，可继续拖动调整。");
}

function deferRenderForNavigation(section) {
  if (!navigationIsOpen()) return false;
  navigationRenderPending = { section, projectId: state.project?.id, epoch: state.selectionEpoch };
  return true;
}

window.addEventListener("workspace-navigation-closed", () => {
  const pending = navigationRenderPending; navigationRenderPending = null;
  const current = currentProjectRoute();
  if (!pending || pending.projectId !== state.project?.id || pending.epoch !== state.selectionEpoch || pending.section !== state.currentSection || current?.id !== pending.projectId || current?.section !== pending.section) return;
  if (pending.section === "annotate") renderWorkspace(); else renderProjectSection(pending.section);
});
const resumeTrainingUpdates = () => { if (!document.hidden && state.currentSection === 'train') void pipelinePollNow?.(true); };
document.addEventListener('visibilitychange', resumeTrainingUpdates);
window.addEventListener('focus', resumeTrainingUpdates);
window.addEventListener('online', resumeTrainingUpdates);
installWorkspaceNavigation();
route();

export { route, setSaveFeedback, renderLoginScreen, state, app, topbar, icon, api, jsonOptions, escapeHtml, canEditCurrentAsset, notify, renderWorkspace, refreshInspector, refreshShapes, refreshWorkflowState, refreshCanvasStatus, saveAnnotation, defaultAttributes, drawDraft, openModal, closeModal };
