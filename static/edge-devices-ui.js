import { state, api, app, topbar, escapeHtml as esc, notify, openModal, jsonOptions, closeModal, downloadArtifact } from "./app.js?v=0.27.0";
import { isAdmin } from "./task-ui.js?v=0.27.0";

let devices = [], projects = [], requestSerial = 0, listRequest = null, listReceivedAt = 0, listReadFailed = false, listCompletedSerial = 0, tokenSetup = null;
const DEVICE_REFRESH_MS = 5000, DEVICE_REFRESH_STALE_MS = 15000;
const dateText = value => value ? new Date(value).toLocaleString("zh-CN") : "暂无记录";
const numberText = value => Math.max(0, Number(value) || 0).toLocaleString("zh-CN");
const pathFor = device => `/api/edge/devices/${encodeURIComponent(device.id)}`;
const terminalStates = new Set(["succeeded", "completed", "failed", "cancelled", "unknown"]);
const stageNames = { queued: "等待部署", connecting: "连接设备", checking: "检查设备", uploading: "传输应用包", installing: "安装应用", starting: "启动应用", verifying: "验证运行状态", running: "正在部署", succeeded: "部署完成", completed: "部署完成", failed: "部署失败", cancelled: "已取消", unknown: "结果待核实", connection_lost: "连接中断", interrupted: "任务被中断", preparation: "准备应用包" };
const finalStageNames = { ...stageNames, running: "应用已启动" };

export function remoteServerURL(value) {
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash
        || host === "localhost" || host.endsWith(".localhost") || /^127\./.test(host)
        || ["0.0.0.0", "::", "::1"].includes(host)) return "";
    return url.href.replace(/\/$/, "");
  } catch (_) { return ""; }
}

export function deviceConnection(device, now = Date.now(), { stale = false, observedAt = now } = {}) {
  if (device.revoked_at || device.enabled === false || device.connection?.state === "revoked") return { kind: "revoked", label: "已撤销接入" };
  if (stale) return { kind: "unknown", label: "连接状态待确认" };
  const connection = device.connection;
  if (connection?.state === "reconnect_required") return { kind: "reconnect", label: "等待使用新令牌连接" };
  if (connection?.state === "never_connected" || (!connection && !device.last_seen_at)) return { kind: "waiting", label: "尚未连接" };
  const age = connection ? (typeof connection.age_seconds === "number" ? connection.age_seconds * 1000 + Math.max(0, now - observedAt) : NaN)
    : now - new Date(device.last_seen_at).getTime();
  const timeout = connection ? Math.max(1, Number(connection.timeout_seconds) || 90) * 1000 : 90000;
  return (!connection || connection.state === "online") && Number.isFinite(age) && age >= -60000 && age < timeout
    ? { kind: "online", label: "在线 · 已连接" } : { kind: "offline", label: "离线 · 联系已超时" };
}

export function deviceDiagnosis(device, status = deviceConnection(device)) {
  const ingestion = device.ingestion || {}, pending = ingestion.pending_images ?? device.heartbeat?.pending_images;
  if (status.kind === "unknown") return { kind: "warning", title: "暂时无法确认设备连接", text: "设备状态更新失败或尚未返回。请检查浏览器到 CVSprout 的连接并点击刷新；以下计数保留上次结果。" };
  if (status.kind === "revoked") return { kind: "muted", title: "设备接入已撤销", text: "此令牌无法再上传图片。已导入数据保留，需要恢复时请创建新的设备接入。" };
  if (status.kind === "reconnect") return { kind: "warning", title: "请更新设备上的令牌", text: "旧令牌已经失效。将新 device_token 保存到 CVSprout Capture 应用配置并重启应用，收到有效请求后会自动显示已连接。" };
  if (status.kind === "waiting") return { kind: "warning", title: "尚未收到设备的有效连接", text: "在设备 App Center 启动 CVSprout Capture，并确认已保存 server_url 和 device_token。地址须为设备能访问的服务器地址，不能使用 localhost；令牌请使用此设备卡片生成的完整 device_token，不是网页登录密码或管理员 API Key。仅创建接入或打开采集开关不会连接设备，请同时检查网络与应用日志。" };
  if (status.kind === "offline") return { kind: "warning", title: "设备暂时失去联系", text: "请检查采集应用是否仍在运行，以及设备到 CVSprout 的网络、服务地址和令牌。" + (Number(pending) > 0 ? " 上次上报的队列仍有图片，恢复连接后会继续补传。" : "") };
  const details = {
    waiting_connection: ["warning", "等待采集应用同步配置", "请确认设备启动的是 CVSprout Capture，并检查服务地址和令牌。"],
    waiting_heartbeat: ["warning", "已连接，等待采集应用心跳", "服务器已收到设备请求，尚未收到近期运行状态。请稍候；若持续不变，请检查采集应用日志。"],
    waiting_first_image: ["muted", "已连接，等待第一张图片", "连接正常。图片会按采集间隔自动上传；若持续没有图片，请检查设备采集应用的相机帧与上传日志。"],
    waiting_camera: ["warning", "已连接，正在等待相机画面", "采集应用尚未获得可用的相机帧。请检查设备相机预览和帧服务，以及应用运行日志。"],
    no_recent_images: ["warning", "已连接，但近期没有收到新图片", "最近的图片上传已超过预期采集间隔。请检查设备相机画面、采集数量上限与应用日志；连接在线并不代表图片上传正常。"],
    receiving: ["success", "已收到设备图片", "可以在项目数据页查看已导入的图片，并继续标注和训练。相同内容的图片会去重，每次采集仍保留记录。"],
    upload_pending: ["warning", "图片已采集，正在等待上传", "设备队列中还有图片，上传成功后这里会自动更新。若队列持续增长，请检查网络和最近上报的错误。"],
    paused: ["muted", "新图片采集已暂停", "在“采集设置”中启用新图片采集。已进入设备队列的图片仍会继续上传。"],
    archived: ["warning", "项目已归档，采集已暂停", "请先恢复项目，再检查采集设置。设备中的待上传图片会保留。"],
    session_complete: ["muted", "本次启动的采集数量已达上限", "如需继续采集，请调整设备应用的 max_session_images 参数并重启；设为 0 表示不限。"],
    queue_full: ["warning", "设备离线队列已满", "应用保留待上传图片并暂停新增。请先恢复上传或释放设备存储空间，不要删除尚未确认的队列文件。"],
    error: ["warning", "采集或上传遇到问题", "设备仍在连接服务器，但尚未完成图片上传。请查看最近上报的错误，并检查设备应用日志。"],
  };
  const fallback = device.project_archived ? "archived" : device.capture_enabled === false ? "paused"
    : device.heartbeat?.last_error ? "error" : Number(pending) > 0 ? "upload_pending"
    : Number(device.capture_count) > 0 ? "receiving" : "waiting_first_image";
  const [kind, title, explanation] = details[ingestion.state] || details[fallback];
  const code = ingestion.last_error || (ingestion.heartbeat_fresh === false ? "" : device.heartbeat?.last_error) || "";
  const actions = {
    server_unreachable_or_invalid_response: "请确认服务已监听局域网地址、防火墙允许访问、server_url 可从设备访问，HTTPS 证书有效。",
    server_redirect_refused: "服务地址发生了跳转，请将设备的 server_url 改为最终的 HTTP / HTTPS 地址。",
    http_401: "设备令牌无效。请将最新 device_token 保存到采集应用配置并重启。",
    http_403: "服务器拒绝了请求，请检查设备接入权限和服务端日志。",
    http_409: "请检查项目是否归档，以及服务器返回的采集冲突记录。",
    http_413: "图片超过上传大小限制，请降低设备应用 JPEG 质量或检查服务器上传限制。",
    spool_full_capture_paused: "设备队列已满。请先恢复上传或释放存储空间，应用会保留尚未确认的图片。",
    image_exceeds_server_limit: "采集图片过大，请降低设备应用的 JPEG 质量后重启。",
    capture_spool_corrupt: "队列记录损坏，已保留原文件。请停止采集并联系维护者恢复，避免手动删除队列。",
    spool_storage_error: "设备存储发生错误，请检查剩余空间和存储状态，并保留未上传的队列。",
  };
  return { kind, title, text: actions[code] || explanation };
}

function connectionOptions() {
  return { stale: listReadFailed || Boolean(listReceivedAt && Date.now() - listReceivedAt > DEVICE_REFRESH_STALE_MS), observedAt: listReceivedAt || Date.now() };
}

function captureSetting(device) {
  return device.project_archived ? "采集设置：项目已归档，已暂停新采集" : device.capture_enabled === false
    ? "采集设置：已暂停新采集，队列继续上传" : `采集设置：已启用，每 ${numberText(device.capture_interval_seconds)} 秒一张`;
}

function cardValues(device, status) {
  const ingestion = device.ingestion || {}, heartbeat = device.heartbeat || {};
  const pending = ingestion.pending_images ?? heartbeat.pending_images ?? heartbeat.queue_length;
  const diagnosis = deviceDiagnosis(device, status);
  return {
    name: device.name, project: device.project_name || `项目 ${device.project_id}`,
    captures: numberText(device.capture_count), assets: numberText(device.asset_count), pending: pending == null ? "—" : numberText(pending),
    pendingLabel: ingestion.heartbeat_fresh === false ? "待上传（上次上报）" : "设备待上传",
    setting: captureSetting(device), contact: `最近联系：${dateText(device.connection?.last_contact_at || device.last_seen_at)}`,
    heartbeat: `最近心跳：${dateText(device.connection?.last_heartbeat_at)}`, uploaded: `最近收到图片：${dateText(device.last_capture_at)}`,
    version: `采集应用版本：${heartbeat.app_version || "尚未上报"}`,
    diagnosisTitle: diagnosis.title, diagnosisText: diagnosis.text,
    error: ingestion.last_error || (ingestion.heartbeat_fresh === false ? "" : heartbeat.last_error) || "",
    diagnosisKind: diagnosis.kind,
  };
}

function pageContext(node = document.querySelector("#edge-device-results")) {
  const epoch = state.selectionEpoch, session = state.session;
  return () => state.currentSection === "devices" && state.selectionEpoch === epoch && state.session === session
    && Boolean(node?.isConnected) && document.getElementById(node.id) === node;
}

export async function renderDevicesPage() {
  if (!isAdmin()) { location.hash = "#/"; return; }
  state.currentSection = "devices"; state.project = null; state.asset = null; state.annotations = [];
  state.selectedAnnotation = null; state.lockOwned = false;
  devices = []; listReceivedAt = 0; listReadFailed = false; requestSerial++;
  app.innerHTML = `<div class="app-shell">${topbar("devices")}<main class="dashboard edge-devices-page"><div class="dashboard-inner">
    <div class="page-head"><div><h1>边缘设备</h1><p class="page-subtitle">让 reCamera Pro 采集的图片进入项目，把训练后的模型部署回设备。</p></div><div class="edge-actions"><button class="btn ghost" id="refresh-edge-devices">刷新</button><button class="btn primary" id="add-edge-device">连接设备</button></div></div>
    <ol class="edge-workflow" aria-label="设备与训练工作流"><li><span>1</span><strong>连接与采集</strong><small>安装联动应用，图片自动导入</small></li><li><span>2</span><strong>标注与训练</strong><small>沿用项目的审核和数据版本</small></li><li><span>3</span><strong>部署回设备</strong><small>转换 RV1126B 模型并发布应用</small></li></ol>
    <p class="edge-page-note">安装一个 CVSprout 联动应用即可采集原图并传输系统模型；推理由设备内置系统运行。图片导入后由管理员创建新的任务块，已领取的任务块保持原样。</p>
    <p class="edge-refresh-note">页面可见时每 5 秒更新连接和采集状态。<span id="edge-refresh-time">正在读取状态…</span></p><div id="edge-page-feedback" class="inline-error" role="status" hidden></div><div id="edge-device-results"><p class="hint">正在读取设备…</p></div>
    <details class="edge-help"><summary>首次连接前需要准备什么？</summary><p>使用支持 Python SDK 与 App Center 的 reCamera Pro 固件。让设备能访问 CVSprout 的局域网地址或 HTTPS 域名；使用启动脚本时，在 <code>.env</code> 设置 <code>MDP_BIND_ADDRESS=0.0.0.0</code> 并重新启动。连接设备后，将服务地址和一次性显示的设备令牌填入采集应用配置。</p><p>系统模型任务由联动应用主动领取，设备离线时保留等待。请在设备端完成系统模型管理配置；旧版独立推理应用的 SSH 部署记录仍可查看。</p></details>
  </div></main></div>`;
  const current = pageContext();
  app.addEventListener("click", handlePackageDownload);
  document.querySelector("#add-edge-device").onclick = () => openDeviceSettings();
  document.querySelector("#refresh-edge-devices").onclick = async event => {
    const button = event.currentTarget; button.disabled = true;
    try { await loadDevices(); } finally { if (current()) button.disabled = false; }
  };
  await loadDevices();
  if (!current()) return;
  const timer = setInterval(() => {
    if (!current()) { clearInterval(timer); return; }
    if (!document.hidden) { refreshConnectionDisplay(); void loadDevices({ background: true }); }
  }, DEVICE_REFRESH_MS);
  state.pollTimer = timer;
}

async function loadDevices({ background = false } = {}) {
  const node = document.querySelector("#edge-device-results"), currentPage = pageContext(node);
  if (!currentPage() || (background && document.hidden)) return;
  // A manual/mutation refresh follows the pending request; polling shares it.
  // This prevents overlapping requests without losing a just-saved change.
  if (listRequest?.current()) {
    if (!background) listRequest.refreshAgain = true;
    return listRequest.promise;
  }
  const serial = ++requestSerial, current = () => currentPage() && serial === requestSerial;
  const operation = { current, refreshAgain: false, promise: null };
  listRequest = operation;
  operation.promise = (async () => {
    if (!background) node.setAttribute("aria-busy", "true");
    try {
      const result = await api("/api/edge/devices");
      if (!current()) return;
      if (!Array.isArray(result.devices)) throw new Error("服务器未返回有效的设备列表。");
      devices = result.devices; listReceivedAt = Date.now(); listReadFailed = false; listCompletedSerial = serial;
      const feedback = document.querySelector("#edge-page-feedback"); feedback.hidden = true;
      const updated = document.querySelector("#edge-refresh-time"); if (updated) updated.textContent = `最近更新：${new Date(listReceivedAt).toLocaleTimeString("zh-CN")}`;
      renderDeviceList(node); refreshTokenStatus();
    } catch (error) {
      if (error.status === 401 || !current()) return;
      listReadFailed = true;
      const feedback = document.querySelector("#edge-page-feedback");
      feedback.textContent = `设备状态更新失败：${error.message}。已保留上次数据，连接状态暂无法确认。`; feedback.hidden = false;
      refreshConnectionDisplay();
      if (!devices.length && !background) node.innerHTML = '<p class="hint">请点击上方“刷新”重试。</p>';
    } finally {
      if (current()) node.removeAttribute("aria-busy");
      if (listRequest === operation) listRequest = null;
    }
    if (current() && operation.refreshAgain) await loadDevices();
  })();
  return operation.promise;
}

export function deviceCardMarkup(device, options = {}) {
  const status = deviceConnection(device, Date.now(), options), values = cardValues(device, status);
  const field = (name, tag = "span") => `<${tag} data-edge-value="${name}">${esc(values[name])}</${tag}>`;
  const editHidden = status.kind === "revoked" ? " hidden" : "";
  return `<article class="edge-device-card" data-edge-device="${esc(device.id)}"><div class="edge-card-heading"><div>${field("name", "h2")}<a data-edge-value="project" href="#/projects/${Number(device.project_id)}/data">${esc(values.project)}</a></div><span class="edge-connection ${status.kind}" role="status">${esc(status.label)}</span></div>
    <p class="edge-capture-setting" data-edge-value="setting">${esc(values.setting)}</p>
    <dl class="edge-device-stats"><div><dt>收到的采集记录</dt>${field("captures", "dd")}</div><div><dt>关联的项目图片</dt>${field("assets", "dd")}</div><div>${field("pendingLabel", "dt")}${field("pending", "dd")}</div></dl>
    <div class="edge-device-diagnosis ${values.diagnosisKind}">${field("diagnosisTitle", "strong")}${field("diagnosisText", "p")}</div>
    <div class="edge-device-detail">${field("contact")}${field("heartbeat")}${field("uploaded")}${field("version")}</div>
    <details class="edge-diagnostic-details"${values.error ? "" : " hidden"}><summary>查看最近上报的错误</summary>${field("error", "code")}</details>
    <div class="edge-card-actions"><button class="btn ghost" data-edge-captures="${esc(device.id)}">采集记录</button><button class="btn ghost" data-edge-settings="${esc(device.id)}"${editHidden}>采集设置</button><button class="btn primary" data-edge-model="${esc(device.id)}"${editHidden}>模型部署</button><details class="edge-more" data-edge-edit-control${editHidden}><summary>更多</summary><div><button data-edge-rotate="${esc(device.id)}">重置设备令牌</button><button class="danger" data-edge-revoke="${esc(device.id)}">撤销接入</button></div></details></div></article>`;
}

function patchDeviceCard(card, device) {
  const status = deviceConnection(device, Date.now(), connectionOptions()), values = cardValues(device, status);
  const badge = card.querySelector(".edge-connection");
  badge.className = `edge-connection ${status.kind}`;
  if (badge.textContent !== status.label) badge.textContent = status.label;
  card.querySelectorAll("[data-edge-value]").forEach(node => {
    const value = String(values[node.dataset.edgeValue] ?? "");
    if (node.textContent !== value) node.textContent = value;
  });
  card.querySelector('[data-edge-value="project"]').href = `#/projects/${Number(device.project_id)}/data`;
  card.querySelector(".edge-device-diagnosis").className = `edge-device-diagnosis ${values.diagnosisKind}`;
  card.querySelector(".edge-diagnostic-details").hidden = !values.error;
  card.querySelectorAll("[data-edge-settings], [data-edge-model], [data-edge-edit-control]").forEach(node => { node.hidden = status.kind === "revoked"; });
}

function refreshConnectionDisplay() {
  const node = document.querySelector("#edge-device-results");
  if (!pageContext(node)()) return;
  node.querySelectorAll("[data-edge-device]").forEach(card => {
    const device = devices.find(item => item.id === card.dataset.edgeDevice); if (device) patchDeviceCard(card, device);
  });
  refreshTokenStatus();
}

function renderDeviceList(node) {
  if (!devices.length && node.querySelector(".edge-empty")) return;
  let grid = node.querySelector(".edge-device-grid");
  if (!grid || !devices.length) {
    node.innerHTML = devices.length ? `<div class="edge-device-grid">${devices.map(device => deviceCardMarkup(device, connectionOptions())).join("")}</div>`
      : '<div class="edge-empty"><strong>连接你的第一台 reCamera Pro</strong><p>选择图片要进入的项目，获取专属令牌，即可开始自动采集。</p><button class="btn primary" id="empty-connect-device">连接设备</button></div>';
  } else {
    const cards = new Map([...grid.querySelectorAll("[data-edge-device]")].map(card => [card.dataset.edgeDevice, card]));
    for (const device of devices) {
      const existing = cards.get(device.id);
      if (existing) { patchDeviceCard(existing, device); cards.delete(device.id); }
      else grid.insertAdjacentHTML("beforeend", deviceCardMarkup(device, connectionOptions()));
    }
    for (const card of cards.values()) card.remove();
  }
  node.querySelector("#empty-connect-device")?.addEventListener("click", () => openDeviceSettings());
  const bind = (attribute, handler) => node.querySelectorAll(`[${attribute}]`).forEach(button => {
    button.onclick = () => { button.closest("details")?.removeAttribute("open"); const device = devices.find(item => item.id === button.getAttribute(attribute)); if (device) handler(device); };
  });
  bind("data-edge-settings", openDeviceSettings); bind("data-edge-captures", openCaptureRecords);
  bind("data-edge-model", openDeviceDeployment); bind("data-edge-rotate", openRotateToken); bind("data-edge-revoke", openRevokeDevice);
}

function refreshTokenStatus() {
  if (!tokenSetup?.node.isConnected) return;
  const device = (listCompletedSerial >= tokenSetup.minimumSerial ? devices.find(item => item.id === tokenSetup.device.id) : null) || tokenSetup.device;
  const status = deviceConnection(device, Date.now(), connectionOptions()), diagnosis = deviceDiagnosis(device, status);
  const node = tokenSetup.node;
  node.className = `edge-setup-connection ${status.kind}`;
  const title = node.querySelector("strong"), text = node.querySelector("p");
  const label = status.kind === "online" ? "已确认设备连接成功" : status.kind === "waiting" ? "等待设备首次连接…" : status.label;
  if (title.textContent !== label) title.textContent = label;
  if (text.textContent !== diagnosis.text) text.textContent = diagnosis.text;
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.currentSection === "devices") { refreshConnectionDisplay(); void loadDevices({ background: true }); }
});

async function openDeviceSettings(device = null) {
  if (!isAdmin()) return;
  const current = pageContext();
  try {
    if (!device) { projects = await api("/api/projects?archived=false"); if (!current()) return; }
    if (!device && !projects.length) { notify("请先创建一个接收设备图片的项目。", "warning"); return; }
    const suggestedURL = remoteServerURL(device?.server_url || location.origin);
    openModal({ title: device ? "采集设置" : "连接 reCamera Pro", subtitle: device ? "配置会在设备下一次读取时生效。" : "每台设备绑定一个项目，设备令牌只允许向这个项目上传图片。", body: `<div class="edge-settings-form"><div class="field"><label for="edge-device-name">设备名称</label><input id="edge-device-name" maxlength="120" value="${esc(device?.name || "")}" placeholder="例如：车间入口相机" autofocus/></div>
      ${device ? `<p class="hint">接收项目：${esc(device.project_name || `项目 ${device.project_id}`)}。更换项目请创建新的设备接入。</p>` : `<div class="field"><label for="edge-device-project">接收图片的项目</label><select id="edge-device-project">${projects.map(project => `<option value="${Number(project.id)}">${esc(project.name)}</option>`).join("")}</select></div>`}
      <div class="field"><label for="edge-server-url">设备可访问的 CVSprout 地址</label><input id="edge-server-url" type="url" value="${esc(suggestedURL)}" placeholder="http://192.168.1.20:8000" autocomplete="url"/><p class="hint">填写服务器的局域网地址或 HTTPS 域名。设备上的 localhost / 127.0.0.1 指向设备自身，不能用于连接你的电脑。</p></div>
      <div class="field"><label for="edge-capture-interval">采集间隔（秒）</label><input id="edge-capture-interval" type="number" min="1" max="86400" step="1" value="${Number(device?.capture_interval_seconds || 10)}"/></div><label class="check-line"><input id="edge-capture-enabled" type="checkbox" ${device?.capture_enabled === false ? "" : "checked"}/>启用新图片采集</label><p class="hint">暂停采集后，已进入设备队列的图片仍会继续上传。</p>
      <details class="edge-advanced"><summary>离线队列上限</summary><div class="form-grid"><div class="field"><label for="edge-queue-count">最多暂存图片数</label><input id="edge-queue-count" type="number" min="1" max="10000" step="1" value="${Number(device?.max_pending_images || 500)}"/></div><div class="field"><label for="edge-queue-mib">最多暂存空间（MiB）</label><input id="edge-queue-mib" type="number" min="1" max="8192" step="1" value="${Math.ceil(Number(device?.max_pending_bytes || 268435456) / 1048576)}"/></div></div><p class="hint">实际容量取这里与设备应用本地设置中较小的值。离线时先保留队列中的图片；达到上限后暂停新增，连接恢复后自动补传。</p></details></div>`, submitText: device ? "保存设置" : "创建并获取令牌", onSubmit: async () => {
        const name = document.querySelector("#edge-device-name").value.trim();
        const server_url = remoteServerURL(document.querySelector("#edge-server-url").value.trim());
        const capture_interval_seconds = Number(document.querySelector("#edge-capture-interval").value);
        const max_pending_images = Number(document.querySelector("#edge-queue-count").value);
        const mib = Number(document.querySelector("#edge-queue-mib").value);
        if (!name) throw new Error("请输入设备名称。");
        if (!server_url) throw new Error("请填写设备可访问的 HTTP / HTTPS 地址，不要使用本机回环或监听地址。");
        if (!Number.isInteger(capture_interval_seconds) || capture_interval_seconds < 1 || capture_interval_seconds > 86400) throw new Error("采集间隔应为 1–86400 秒之间的整数。");
        if (!Number.isInteger(max_pending_images) || max_pending_images < 1 || max_pending_images > 10000 || !Number.isInteger(mib) || mib < 1 || mib > 8192) throw new Error("队列上限应为 1–10000 张、1–8192 MiB。");
        const payload = { name, server_url, capture_interval_seconds, capture_enabled: document.querySelector("#edge-capture-enabled").checked, max_pending_images, max_pending_bytes: mib * 1048576 };
        if (!device) payload.project_id = Number(document.querySelector("#edge-device-project").value);
        const modal = document.querySelector(".modal");
        const result = await api(device ? pathFor(device) : "/api/edge/devices", jsonOptions(device ? "PATCH" : "POST", payload));
        if (modal?.isConnected) { closeModal(); if (!device) showDeviceToken(result, result.device_token); }
        notify(device ? "采集设置已保存。" : "设备接入已创建，请完成应用配置。");
        if (current()) await loadDevices();
      }});
  } catch (error) { if (error.status !== 401 && current()) notify(error.message, "error"); }
}

export async function copyDeviceToken(value, container, isCurrent = () => true) {
  if (typeof value !== "string" || !value || !container?.isConnected || !isCurrent()) return false;
  // On LAN HTTP, Clipboard API is absent. This branch must execute before any
  // await so the fallback keeps the copy button's trusted user activation.
  const copySynchronously = () => {
    if (!container.isConnected || !isCurrent() || typeof document.execCommand !== "function") return false;
    const previous = document.activeElement, selection = document.getSelection?.();
    const ranges = [];
    for (let index = 0; selection && index < selection.rangeCount; index++) ranges.push(selection.getRangeAt(index).cloneRange());
    let inputSelection = null;
    try {
      if (typeof previous?.selectionStart === "number") inputSelection = [previous.selectionStart, previous.selectionEnd, previous.selectionDirection];
    } catch (_) { /* Some input types do not expose a text selection. */ }
    const temporary = document.createElement("textarea");
    temporary.value = value; temporary.readOnly = true; temporary.tabIndex = -1;
    temporary.setAttribute("data-edge-clipboard", "");
    temporary.style.cssText = "position:fixed;left:-9999px;top:0;width:1px;height:1px;padding:0;border:0;font-size:16px;";
    try {
      // Keep the temporary control inside the modal's focus boundary.
      container.appendChild(temporary); temporary.focus({ preventScroll: true }); temporary.select();
      temporary.setSelectionRange(0, temporary.value.length);
      return document.execCommand("copy") === true;
    } catch (_) { return false; }
    finally {
      temporary.value = ""; temporary.remove();
      try {
        if (selection) { selection.removeAllRanges(); ranges.forEach(range => selection.addRange(range)); }
        if (previous?.isConnected) {
          previous.focus({ preventScroll: true });
          if (inputSelection) previous.setSelectionRange(...inputSelection);
        }
      } catch (_) { /* A closed dialog no longer has a focus/selection target. */ }
    }
  };
  if (typeof navigator.clipboard?.writeText === "function") {
    try { await navigator.clipboard.writeText(value); return true; }
    catch (_) { /* Permission denied: try the selection-based browser copy. */ }
  }
  return copySynchronously();
}

function showDeviceToken(device, token) {
  openModal({ title: "配置 CVSprout 联动应用", subtitle: "设备令牌只显示这一次。请现在复制到 reCamera Pro 的采集应用配置中。", body: `<div class="edge-token-setup"><p>在设备 App Center 安装 <strong>CVSprout Connector</strong>，然后填写：</p><div class="field"><label for="edge-copy-url">server_url · CVSprout 地址</label><input id="edge-copy-url" readonly value="${esc(device.server_url || "")}"/></div><div class="field"><label for="edge-copy-token">device_token · 设备令牌</label><div class="edge-token-control"><input id="edge-copy-token" type="password" readonly autocomplete="off" spellcheck="false"/><button class="btn ghost" type="button" id="edge-copy-token-button">复制令牌</button></div><label class="check-line"><input id="edge-show-token" type="checkbox"/>显示令牌</label></div><p class="hint">请完整复制以 cvsprout_device_ 开头的设备专用令牌，不要填写网页登录密码或管理员 API Key。关闭后无法再次查看；丢失时可在设备卡片重置。</p><div id="edge-capture-download"></div><div id="edge-setup-connection" class="edge-setup-connection waiting" role="status"><strong>等待设备首次连接…</strong><p>保存参数并启动设备上的采集应用后，这里会自动确认连接。请保留此窗口，令牌仍可复制。</p></div></div>`, submitText: "我已保存配置", onReady: () => {
      const input = document.querySelector("#edge-copy-token"), modal = document.querySelector(".modal"); input.value = token;
      tokenSetup = { device, minimumSerial: requestSerial + 1, node: modal.querySelector("#edge-setup-connection") }; refreshTokenStatus();
      void loadDevices({ background: true });
      modal.addEventListener("click", handlePackageDownload);
      document.querySelector("#edge-show-token").onchange = event => { input.type = event.target.checked ? "text" : "password"; };
      const copyButton = document.querySelector("#edge-copy-token-button");
      let copying = false;
      copyButton.onclick = async () => {
        if (copying) return;
        copying = true; copyButton.setAttribute("aria-busy", "true");
        let copied = false;
        try { copied = await copyDeviceToken(input.value, modal, () => modal.isConnected && input.isConnected); }
        catch (_) { /* A browser clipboard restriction must not claim success. */ }
        finally { copying = false; if (copyButton.isConnected) copyButton.removeAttribute("aria-busy"); }
        if (modal.isConnected) notify(copied ? "设备令牌已复制。" : "浏览器未能复制令牌。请勾选“显示令牌”，选中完整内容后手动复制。", copied ? "success" : "warning");
      };
      void api(`${pathFor(device)}/deployment-options`).then(result => {
        if (modal.isConnected && result.capture_app?.download_url) document.querySelector("#edge-capture-download").innerHTML = downloadLink(result.capture_app.download_url, "下载联动应用包");
      }).catch(() => {});
    }, onClose: () => { tokenSetup = null; const input = document.querySelector("#edge-copy-token"); if (input) input.value = ""; token = ""; }, onSubmit: async () => closeModal() });
}

function openRotateToken(device) {
  const current = pageContext();
  openModal({ title: "重置设备令牌", subtitle: device.name, body: '<p>重置后旧令牌立即失效。请将新令牌更新到设备的采集应用配置，设备才能继续上传。项目中已导入的图片会保留。</p>', submitText: "重置并显示新令牌", onSubmit: async () => {
    const modal = document.querySelector(".modal"), result = await api(`${pathFor(device)}/rotate-token`, jsonOptions("POST", {}));
    if (modal?.isConnected) { closeModal(); showDeviceToken(result.device || device, result.device_token); }
    if (current()) await loadDevices();
  }});
}

function openRevokeDevice(device) {
  const current = pageContext();
  openModal({ title: "撤销设备接入", subtitle: device.name, body: `<p>设备将无法继续上传或读取配置；已导入的图片和采集记录保留。需要恢复时，请创建新的接入。</p><div class="field"><label for="edge-revoke-name">输入设备名称“${esc(device.name)}”确认</label><input id="edge-revoke-name" autocomplete="off" autofocus/></div>`, submitText: "撤销接入", onReady: () => {
    const input = document.querySelector("#edge-revoke-name"), button = document.querySelector("#modal-submit"); input.value = ""; button.disabled = true;
    input.oninput = () => { button.disabled = input.value !== device.name; };
  }, onSubmit: async () => {
    if (document.querySelector("#edge-revoke-name").value !== device.name) throw new Error("请输入完整设备名称确认。");
    const modal = document.querySelector(".modal"); await api(pathFor(device), { method: "DELETE" });
    if (modal?.isConnected) closeModal(); notify("设备接入已撤销。"); if (current()) await loadDevices();
  }});
}

function captureRecordMarkup(item, device) {
  const assetId = Number(item.asset_id), available = Number.isSafeInteger(assetId) && assetId > 0;
  const filename = item.filename || `图片 ${assetId}`;
  const thumbnail = available ? `<button type="button" class="edge-record-thumb" data-edge-preview="${assetId}" data-edge-capture="${esc(item.capture_id)}" aria-label="预览 ${esc(filename)}"><span data-edge-image-status>正在加载…</span><img data-edge-image src="/api/assets/${assetId}/thumbnail?size=320" alt="" loading="lazy" decoding="async" width="320" height="240"/><span class="edge-record-thumb-action">查看大图</span></button>`
    : '<div class="edge-record-thumb unavailable"><span>原图已删除</span></div>';
  const title = available ? `<a href="#/projects/${Number(device.project_id)}/annotate?asset=${assetId}">${esc(filename)}</a>`
    : `<span>原图已删除${item.filename ? ` · ${esc(item.filename)}` : ""}</span>`;
  return `<article>${thumbnail}<div class="edge-record-info"><div class="edge-record-heading">${title}<span>采集：${esc(dateText(item.captured_at))}</span></div><p>${available ? item.duplicate ? "已有相同图片 · 已关联" : "图片已导入" : "采集记录保留"}${item.model_release ? ` · 模型 ${esc(item.model_release)}` : ""}</p><small>收到时间：${esc(dateText(item.received_at))} · 采集 ID：${esc(item.capture_id)}</small></div></article>`;
}

function bindCaptureImages(root) {
  for (const image of root.querySelectorAll("[data-edge-image]")) {
    const status = image.parentElement.querySelector("[data-edge-image-status]");
    const loaded = () => { image.classList.add("loaded"); image.hidden = false; status.hidden = true; };
    const failed = () => { image.hidden = true; status.hidden = false; status.textContent = "图片暂不可用"; };
    image.addEventListener("load", loaded, { once: true });
    image.addEventListener("error", failed, { once: true });
    if (image.complete) image.naturalWidth ? loaded() : failed();
  }
}

function showCapturePreview(node, item, device, trigger) {
  const preview = node.querySelector("#edge-record-preview"), list = node.querySelector("#edge-record-page");
  const assetId = Number(item.asset_id), filename = item.filename || `图片 ${assetId}`;
  const body = node.closest(".modal-body"), scrollTop = body.scrollTop;
  list.hidden = true; preview.hidden = false;
  preview.innerHTML = `<div class="edge-record-preview-heading"><button type="button" class="btn ghost" data-edge-preview-back>返回记录</button><strong>${esc(filename)}</strong><a class="btn ghost link-btn" href="#/projects/${Number(device.project_id)}/annotate?asset=${assetId}">前往标注</a></div><div class="edge-record-preview-image"><span data-edge-image-status role="status">正在加载原图…</span><img data-edge-image src="/api/assets/${assetId}/image" alt="${esc(filename)}" decoding="async"/></div><p class="hint">采集：${esc(dateText(item.captured_at))} · 收到：${esc(dateText(item.received_at))}</p>`;
  bindCaptureImages(preview);
  const back = preview.querySelector("[data-edge-preview-back]");
  const restore = () => {
    preview.hidden = true; preview.replaceChildren(); list.hidden = false;
    node.removeEventListener("keydown", escapePreview);
    trigger.focus({ preventScroll: true }); body.scrollTop = scrollTop;
  };
  const escapePreview = event => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); restore(); }
  };
  back.onclick = restore; node.addEventListener("keydown", escapePreview);
  body.scrollTop = 0; back.focus({ preventScroll: true });
}

function openCaptureRecords(device) {
  let offset = 0, serial = 0;
  openModal({ title: `${device.name} · 采集记录`, subtitle: "点击缩略图查看大图；同一内容的图片只保留一份，每一次采集仍可追溯。", wide: true, body: '<div id="edge-capture-records" aria-live="polite">正在读取…</div>', submitText: "关闭", onSubmit: async () => closeModal(), onReady: () => { void load(); } });
  async function load() {
    const node = document.querySelector("#edge-capture-records"), request = ++serial;
    if (!node?.isConnected) return;
    const restoreFocus = node.contains(document.activeElement);
    const loadingFocus = node.closest(".modal").querySelector("#modal-close");
    // Keep keyboard focus within the dialog while replacing a paginated list.
    if (restoreFocus) loadingFocus.focus({ preventScroll: true });
    node.inert = true; node.setAttribute("aria-busy", "true");
    try {
      const result = await api(`${pathFor(device)}/captures?limit=25&offset=${offset}`);
      if (!node.isConnected || request !== serial) return;
      node.innerHTML = `<div id="edge-record-preview" hidden></div><div id="edge-record-page"><div class="edge-record-list">${(result.items || []).map(item => captureRecordMarkup(item, device)).join("") || '<p class="hint">尚未收到图片。请确认采集应用正在运行，地址和令牌已保存。</p>'}</div><div class="edge-record-pagination"><span>共 ${numberText(result.total)} 条记录</span><button class="btn ghost" id="edge-record-prev" ${offset === 0 ? "disabled" : ""}>上一页</button><button class="btn ghost" id="edge-record-next" ${offset + 25 >= result.total ? "disabled" : ""}>下一页</button></div></div>`;
      bindCaptureImages(node);
      for (const button of node.querySelectorAll("[data-edge-preview]")) {
        button.onclick = () => {
          const item = (result.items || []).find(record => String(record.capture_id) === button.dataset.edgeCapture);
          if (item) showCapturePreview(node, item, device, button);
        };
      }
      node.querySelector("#edge-record-prev").onclick = () => { offset = Math.max(0, offset - 25); void load(); };
      node.querySelector("#edge-record-next").onclick = () => { offset += 25; void load(); };
      node.closest(".modal-body").scrollTop = 0;
    } catch (error) {
      if (node.isConnected && request === serial && error.status !== 401) {
        node.innerHTML = `<p class="inline-error">${esc(error.message)}</p><button class="btn ghost" id="edge-record-retry">重试</button>`;
        node.querySelector("#edge-record-retry")?.addEventListener("click", load);
      }
    } finally {
      if (node.isConnected && request === serial) {
        node.inert = false; node.removeAttribute("aria-busy");
        if (restoreFocus && document.activeElement === loadingFocus) {
          (node.querySelector("#edge-record-retry, [data-edge-preview], #edge-record-prev:not([disabled]), #edge-record-next:not([disabled])") || loadingFocus).focus();
        }
      }
    }
  }
}

function downloadLink(value, label) {
  // Package URLs must stay on our authenticated API origin, never an external or script URL.
  if (typeof value !== "string" || !value.startsWith("/api/edge/") || value.includes("\\")) return "";
  return `<a class="btn ghost link-btn" href="${esc(value)}" data-edge-package download>${esc(label)}</a>`;
}

async function handlePackageDownload(event) {
  const link = event.target.closest("[data-edge-package]");
  if (!link || event.defaultPrevented) return;
  event.preventDefault();
  if (link.getAttribute("aria-busy") === "true") return;
  const url = link.getAttribute("href");
  if (!downloadLink(url, "download")) return;
  link.setAttribute("aria-busy", "true");
  try { await downloadArtifact(url, "cvsprout-app.tar.gz"); }
  catch (error) { if (link.isConnected) notify(error.message, "error"); }
  finally { if (link.isConnected) link.removeAttribute("aria-busy"); }
}

function deploymentHistoryMarkup(items = []) {
  return items.length ? `<ol class="edge-deployment-history">${items.slice(0, 6).map(item => `<li><strong>${esc(stageNames[item.status] || stageNames[item.stage] || item.status)}</strong><span>${esc(dateText(item.created_at))}</span>${item.error ? `<p class="feedback-error">${esc(item.error)}</p>` : item.stage ? `<small>${esc(finalStageNames[item.stage] || item.stage)}</small>` : ""}</li>`).join("")}</ol>` : '<p class="hint">还没有从 CVSprout 发起的部署记录。</p>';
}

const systemTerminalStates = new Set(["succeeded", "failed", "unknown", "cancelled"]);
const systemStageNames = { queued: "等待设备领取", downloading: "设备正在下载模型", uploading: "正在写入系统模型库", uploaded: "已加入系统模型库", activating: "正在切换系统模型", activated: "已请求启用，等待运行验证", verified: "已验证系统推理运行", failed: "部署失败", unknown: "结果待核实", cancelled: "已取消" };
const systemErrorNames = { system_api_unavailable: "设备本机模型接口不可用，请检查设备系统服务", system_api_unauthorized: "设备本机模型接口未授权，请检查固件的本机应用访问权限", system_api_unsupported: "设备固件尚未提供兼容的系统模型接口", system_model_incompatible: "模型与设备系统后处理不兼容，请检查转换用途和算法", system_model_busy: "系统模型服务正忙，请检查设备任务", artifact_download_failed: "设备下载模型失败，请检查连接", artifact_integrity_failed: "模型文件校验失败，请检查发布文件和设备存储", insufficient_storage: "设备存储空间不足，请清理后重试", upload_failed: "模型导入失败，请在设备模型库核实结果", activation_failed: "请求启用失败，请在设备核实当前模型", verification_failed: "尚未验证模型正常运行，请检查实际 FPS 与设备日志", rollback_failed: "恢复原模型失败，请在设备核实当前运行模型", lease_expired: "设备任务联系超时，需要核实实际执行结果", credential_changed: "设备令牌已更新，需要使用新令牌恢复连接并核实任务", deployment_interrupted: "设备部署被中断，需要核实实际执行结果", admin_resolved_unknown: "管理员已核实并解除占用，设备模型未被回滚" };
function systemErrorText(code) { return systemErrorNames[code] ? `${systemErrorNames[code]}（${code}）` : String(code || ""); }

export function systemDeploymentLabel(job) {
  if (job.status === "unknown" || job.lease_expired) return "结果待核实 · 请检查设备";
  if (job.status === "failed") return "部署失败";
  if (job.status === "cancelled") return "已取消";
  if (job.status === "succeeded") return job.activate && job.stage === "verified" ? "已验证系统推理运行" : !job.activate && job.stage === "uploaded" ? "已加入系统模型库" : "已完成 · 运行状态待核实";
  return systemStageNames[job.stage] || (job.status === "queued" ? "等待设备领取" : "设备正在处理");
}

export function systemModelStatusMarkup(data, now = Date.now(), observedAt = now) {
  const report = data.system_status, current = data.current, capabilities = data.capabilities;
  const fresh = report?.fresh === true && Number(report.age_seconds || 0) + Math.max(0, now - observedAt) / 1000 <= 90;
  const title = !report ? "等待联动应用上报系统能力" : !fresh ? "系统状态已过期，等待设备联系" : report.error_code ? "系统模型服务暂不可用" : current?.bExternalHold ? "系统推理正在等待 NPU" : current?.sStatus === "running" ? "系统推理运行中" : current?.sStatus === "error" ? "系统推理出现错误" : "系统推理未运行";
  const kind = fresh && !report?.error_code && current?.sStatus === "running" && !current?.bExternalHold ? "success" : "warning";
  return `<div class="edge-device-diagnosis ${kind}"><strong>${esc(title)}</strong><p>${!capabilities?.model_api ? "请安装或升级 CVSprout Connector，并确认设备固件提供可授权访问的系统模型管理接口。" : !fresh ? "以下模型和参数为上次上报，不能据此确认设备正在运行；部署任务会等待设备恢复连接。" : current?.bExternalHold ? "NPU 正被其他应用或任务使用，请在设备应用中心检查占用；采集上传可继续运行。" : "模型上传与实际运行分别确认；仅加入模型库不会切换当前推理模型。"}</p></div>
    <dl class="edge-system-status"><div><dt>${fresh ? "系统模型" : "上次上报的模型"}</dt><dd>${esc(current?.sModel || "未选择")}</dd></div><div><dt>目标 / 实际 FPS</dt><dd>${current?.iFPS == null ? "—" : numberText(current.iFPS)} / ${fresh && !report?.error_code && current?.sStatus === "running" && current?.iActualFPS != null ? numberText(current.iActualFPS) : "—"}</dd></div><div><dt>NPU 状态</dt><dd>${!fresh || report?.error_code ? "待确认" : current?.bExternalHold ? "其他应用占用" : "未上报外部占用"}</dd></div><div><dt>系统状态上报</dt><dd>${esc(dateText(report?.reported_at))}</dd></div></dl>
    <p class="hint">设备支持的系统算法：${esc(capabilities?.algorithms?.join("、") || "尚未上报")}${report?.error_code ? ` · 最近上报：${esc(systemErrorText(report.error_code))}` : ""}</p>`;
}

export function systemDeploymentHistoryMarkup(items = []) {
  return items.length ? `<ol class="edge-deployment-history">${items.slice(0, 8).map(job => `<li><strong>${esc(systemDeploymentLabel(job))}</strong><span>${esc(dateText(job.created_at))}</span><p>${esc(job.filename || "模型")} · ${job.activate ? "上传并启用" : "仅加入模型库"}${job.status === "queued" ? " · 设备离线时保留等待，恢复连接后自动领取" : ""}${job.error_code ? ` · ${esc(systemErrorText(job.error_code))}` : ""}</p>${job.status === "queued" ? `<button type="button" class="btn ghost" data-edge-cancel-model="${esc(job.id)}">取消等待任务</button>` : job.lease_expired && ["running", "unknown"].includes(job.status) ? `<button type="button" class="btn ghost" data-edge-resolve-model="${esc(job.id)}">核实并解除占用</button>` : ""}</li>`).join("")}</ol>` : '<p class="hint">暂无系统模型部署记录。</p>';
}

async function openDeviceDeployment(device) {
  const pageCurrent = pageContext();
  let data = null, node, modal, monitor = null, request = null, requestSerial = 0, creating = false, submitting = false, receivedAt = 0;
  let conversionSignature = "", releaseSignature = "", statusSignature = "", jobsSignature = "";
  const alive = () => pageCurrent() && Boolean(node?.isConnected) && Boolean(modal?.isConnected);
  openModal({ title: `${device.name} · 系统模型部署`, subtitle: "一个联动应用完成图片采集与模型传输，模型由设备内置系统推理运行。", wide: true,
    body: '<div id="edge-system-content"><p class="hint">正在读取设备能力与模型…</p></div>', submitText: "加入设备模型库",
    onReady: () => { document.querySelector("#modal-submit").disabled = true; },
    onClose: () => { clearInterval(monitor); requestSerial++; }, onSubmit: async () => {
      if (!alive() || !data || creating || submitting || !data.can_deploy) throw new Error(data?.blocked_reason || "请等待设备能力检查完成。");
      const release_id = Number(node.querySelector("#edge-system-release").value);
      const activate = node.querySelector("#edge-system-mode").value === "activate";
      const fps = activate ? Number(node.querySelector("#edge-system-fps").value) : 30;
      if (!data.releases?.some(item => Number(item.id) === release_id && item.status === "ready" && releaseCompatible(item))) throw new Error("请先发布并选择设备支持的系统模型版本。");
      if (!Number.isInteger(fps) || fps < 1 || fps > 60) throw new Error("目标 FPS 应为 1–60 的整数。");
      if (activate && !node.querySelector("#edge-system-confirm").checked) throw new Error("请确认允许替换当前系统推理模型。");
      if ((data.deployments || []).some(job => !systemTerminalStates.has(job.status) || job.status === "unknown")) throw new Error("设备仍有等待、执行或结果待核实的任务，请先处理后再部署。");
      submitting = true; syncSubmit();
      try {
        await api(`${pathFor(device)}/model-deployments`, jsonOptions("POST", { release_id, activate, fps }));
        if (!alive()) return;
        node.querySelector("#edge-system-confirm").checked = false;
        notify(activate ? "部署任务已创建；设备将上传、切换模型并验证运行状态。" : "模型入库任务已创建，当前系统推理模型保持不变。");
        await refresh(true);
      } finally { submitting = false; if (alive()) { syncSubmit(); setTimeout(syncSubmit, 0); } }
    }});
  node = document.querySelector("#edge-system-content"); modal = document.querySelector(".modal");
  modal.addEventListener("click", handlePackageDownload);
  await refresh();
  if (!alive()) return;
  monitor = setInterval(() => { if (alive() && !document.hidden) { if (data) renderStatus(); void refresh(); } }, 5000);

  function mount() {
    node.innerHTML = `<div id="edge-system-feedback" class="inline-error" role="status" hidden></div><section class="edge-deploy-step"><h3><span>1</span>确认设备系统能力</h3><div id="edge-system-state"></div><div class="edge-capture-package">${downloadLink("/api/edge/apps/capture/download", "下载 / 升级联动应用")}<span class="hint">沿用 cvsprout-capture；一个应用包即可完成采集与系统模型联动。</span></div></section>
      <section class="edge-deploy-step"><h3><span>2</span>发布系统模型版本</h3><p class="hint">在“训练与转换”中选择 RKNN → reCamera Pro 系统模型。当前支持 YOLOv8 / YOLO11 目标检测；普通 RKNN 或旧版推理应用包不能直接作为系统模型。</p><div class="edge-build-row"><div class="field"><label for="edge-system-conversion">兼容的转换结果</label><select id="edge-system-conversion"></select></div><button type="button" class="btn ghost" id="edge-system-build">发布模型版本</button></div><p id="edge-system-build-feedback" class="hint" role="status"></p><details class="edge-advanced"><summary>查看转换兼容性</summary><div id="edge-system-compatibility"></div></details><a class="edge-text-link" href="#/projects/${Number(device.project_id)}/train">前往训练与转换</a></section>
      <section class="edge-deploy-step"><h3><span>3</span>选择部署方式</h3><div class="field"><label for="edge-system-release">系统模型版本</label><select id="edge-system-release"></select></div><p id="edge-system-release-detail" class="hint"></p><div class="form-grid"><div class="field"><label for="edge-system-mode">部署方式</label><select id="edge-system-mode"><option value="upload">仅上传到系统模型库</option><option value="activate">上传并启用系统模型</option></select></div><div class="field" id="edge-system-fps-field" hidden><label for="edge-system-fps">目标 FPS</label><input id="edge-system-fps" type="number" min="1" max="60" step="1" value="${Number(data.current?.iFPS) >= 1 && Number(data.current?.iFPS) <= 60 ? Number(data.current.iFPS) : 30}"/></div></div><p id="edge-system-mode-note" class="hint">只把模型加入设备模型库，不切换当前运行的模型。</p><label id="edge-system-confirm-label" class="check-line edge-uncertain-confirm" hidden><input type="checkbox" id="edge-system-confirm"/>允许替换当前系统推理模型，切换时推理可能短暂中断</label><p id="edge-system-blocked" class="edge-deploy-notice" hidden></p><div id="edge-system-jobs" aria-live="polite"></div></section>
      <section id="edge-system-resolve" class="edge-deploy-notice" hidden><h3>核实并解除占用</h3><p>请先到设备核实模型状态，并确认原部署应用已经停止操作。此操作只把平台任务标记为失败并解除占用，不会回滚模型，也不会停止设备正在执行的操作。</p><div class="field"><label for="edge-system-resolve-name">输入设备名称「${esc(device.name)}」</label><input id="edge-system-resolve-name" autocomplete="off"/></div><label class="check-line"><input id="edge-system-resolve-ack" type="checkbox"/>我已在设备核实状态，并确认原部署操作已停止</label><p id="edge-system-resolve-feedback" role="status"></p><div class="edge-build-row"><button type="button" class="btn ghost" id="edge-system-resolve-cancel">返回</button><button type="button" class="btn primary" id="edge-system-resolve-submit" disabled>标记失败并解除占用</button></div></section>
      <details class="edge-advanced"><summary>旧版推理应用部署</summary><p>旧的 cvsprout-inference 安装包与历史任务仍可查看。新的系统模型部署无需安装独立推理应用。</p><button type="button" class="btn ghost" id="edge-system-legacy">查看旧版应用部署与记录</button></details>`;
    node.querySelector("#edge-system-build").onclick = buildRelease;
    node.querySelector("#edge-system-legacy").onclick = () => openLegacyDeviceDeployment(device);
    node.querySelector("#edge-system-mode").onchange = () => { node.querySelector("#edge-system-confirm").checked = false; syncSubmit(); };
    node.querySelector("#edge-system-release").onchange = () => { node.querySelector("#edge-system-confirm").checked = false; updateReleaseDetail(); syncSubmit(); };
    node.querySelector("#edge-system-confirm").onchange = syncSubmit;
    node.querySelector("#edge-system-fps").oninput = syncSubmit;
    node.querySelector("#edge-system-conversion").onchange = syncSubmit;
  }
  function releaseCompatible(release) { return data.capabilities?.model_api === true && data.capabilities.algorithms?.includes(release.contract?.algorithm || release.model_info?.algorithm); }
  function updateOptions(selector, items, signatureName, empty, label) {
    const signature = JSON.stringify(items), previous = signatureName === "conversion" ? conversionSignature : releaseSignature;
    if (signature === previous) return;
    const select = node.querySelector(selector), selected = select.value;
    select.innerHTML = `<option value="">${esc(empty)}</option>${items.map(item => `<option value="${esc(item.id)}" ${item.compatible === false ? "disabled" : ""}>${esc(label(item))}</option>`).join("")}`;
    if (items.some(item => String(item.id) === selected && item.compatible !== false)) select.value = selected;
    else if (items.some(item => item.compatible !== false)) select.value = String(items.find(item => item.compatible !== false).id);
    if (signatureName === "release" && select.value !== selected) node.querySelector("#edge-system-confirm").checked = false;
    if (signatureName === "conversion") conversionSignature = signature; else releaseSignature = signature;
  }
  function updateReleaseDetail() {
    const release = data.releases?.find(item => String(item.id) === node.querySelector("#edge-system-release").value);
    node.querySelector("#edge-system-release-detail").textContent = release ? `${release.filename} · ${(Number(release.size_bytes || 0) / 1048576).toFixed(1)} MiB · SHA-256 ${release.sha256 || "—"}` : "先从兼容的转换结果发布模型版本。";
  }
  function renderStatus() {
    const markup = systemModelStatusMarkup(data, Date.now(), receivedAt);
    if (markup !== statusSignature) { node.querySelector("#edge-system-state").innerHTML = markup; statusSignature = markup; }
  }
  function update() {
    renderStatus();
    updateOptions("#edge-system-conversion", data.conversions || [], "conversion", "选择转换结果", item => `转换 #${item.id} · ${item.model_arch || "模型"}${item.compatible ? "" : ` · ${item.reason || "不兼容"}`}`);
    updateOptions("#edge-system-release", (data.releases || []).filter(item => item.status === "ready").map(item => ({ ...item, compatible: releaseCompatible(item) })), "release", "选择系统模型版本", item => `${item.filename} · 转换 #${item.conversion_id}${item.compatible ? "" : " · 设备尚未报告支持此算法"}`);
    node.querySelector("#edge-system-compatibility").innerHTML = (data.conversions || []).length ? `<ul>${data.conversions.map(item => `<li>转换 #${Number(item.id)}：${esc(item.compatible ? "可发布系统模型" : item.reason || "不兼容")}</li>`).join("")}</ul>` : '<p class="hint">当前项目尚无转换结果。</p>';
    const jobs = JSON.stringify(data.deployments || []);
    if (jobs !== jobsSignature) { node.querySelector("#edge-system-jobs").innerHTML = systemDeploymentHistoryMarkup(data.deployments); jobsSignature = jobs; bindCancel(); }
    updateReleaseDetail(); syncSubmit();
  }
  function syncSubmit() {
    if (!alive() || !node.querySelector("#edge-system-mode")) return;
    const activate = node.querySelector("#edge-system-mode").value === "activate", fps = Number(node.querySelector("#edge-system-fps").value);
    node.querySelector("#edge-system-fps-field").hidden = !activate;
    node.querySelector("#edge-system-confirm-label").hidden = !activate;
    node.querySelector("#edge-system-mode-note").textContent = activate ? `将替换设备当前系统模型“${data.current?.sModel || "未选择"}”。只有设备报告运行验证通过后才显示部署成功。` : "只把模型加入设备模型库，不切换当前运行的模型。";
    const pending = (data.deployments || []).some(job => !systemTerminalStates.has(job.status) || job.status === "unknown");
    const blocked = node.querySelector("#edge-system-blocked");
    blocked.textContent = data.blocked_reason || (pending ? "已有部署任务等待处理或结果待核实，请先完成、取消等待任务或检查设备。" : ""); blocked.hidden = !blocked.textContent;
    const button = modal.querySelector("#modal-submit");
    button.disabled = creating || submitting || !data.can_deploy || pending || !node.querySelector("#edge-system-release").value || (activate && (!node.querySelector("#edge-system-confirm").checked || !Number.isInteger(fps) || fps < 1 || fps > 60));
    if (!submitting && !button.innerHTML.includes("spinner")) button.textContent = activate ? "上传并启用" : "加入设备模型库";
    node.querySelector("#edge-system-build").disabled = creating || !node.querySelector("#edge-system-conversion").value;
  }
  async function refresh(force = false) {
    if (!alive()) return;
    if (request) { await request; if (force && alive()) return refresh(); return; }
    const serial = ++requestSerial;
    request = (async () => {
      try {
        const result = await api(`${pathFor(device)}/system-models`);
        if (!alive() || serial !== requestSerial) return;
        if (data && data.current?.sModel !== result.current?.sModel) {
          const confirmation = node.querySelector("#edge-system-confirm"); if (confirmation) confirmation.checked = false;
        }
        data = result; receivedAt = Date.now();
        if (!node.querySelector("#edge-system-state")) mount();
        node.querySelector("#edge-system-feedback").hidden = true; update();
      } catch (error) {
        if (!alive() || error.status === 401 || serial !== requestSerial) return;
        if (!data) { node.innerHTML = `<p class="inline-error">${esc(error.message)}</p><button class="btn ghost" id="edge-system-retry">重新读取</button>`; node.querySelector("#edge-system-retry").onclick = () => refresh(); }
        else { data = { ...data, can_deploy: false, blocked_reason: "系统状态更新失败，请恢复连接后再创建部署。", system_status: data.system_status ? { ...data.system_status, fresh: false } : null }; const feedback = node.querySelector("#edge-system-feedback"); feedback.textContent = `系统状态更新失败：${error.message}`; feedback.hidden = false; update(); }
      }
    })();
    try { await request; } finally { request = null; }
  }
  async function buildRelease() {
    if (creating || !alive()) return;
    const conversion_id = Number(node.querySelector("#edge-system-conversion").value);
    if (!data.conversions?.some(item => item.id === conversion_id && item.compatible)) return;
    const feedback = node.querySelector("#edge-system-build-feedback");
    creating = true; feedback.textContent = "正在校验并发布系统模型版本…"; syncSubmit();
    try {
      const release = await api("/api/edge/model-releases", jsonOptions("POST", { conversion_id }));
      if (!alive()) return;
      await refresh(true); if (!alive()) return;
      node.querySelector("#edge-system-release").value = String(release.id); updateReleaseDetail();
      feedback.textContent = "系统模型版本已发布，可以创建设备部署任务。";
    } catch (error) { if (alive() && error.status !== 401) feedback.textContent = `发布失败：${error.message}`; }
    finally { creating = false; if (alive()) syncSubmit(); }
  }
  function bindCancel() {
    node.querySelectorAll("[data-edge-cancel-model]").forEach(button => { button.onclick = async () => {
      if (button.disabled) return; button.disabled = true;
      try { await api(`${pathFor(device)}/model-deployments/${encodeURIComponent(button.dataset.edgeCancelModel)}/cancel`, jsonOptions("POST", {})); if (alive()) await refresh(true); }
      catch (error) { if (alive() && error.status !== 401) notify(error.message, "error"); }
      finally { if (button.isConnected) button.disabled = false; }
    }; });
    node.querySelectorAll("[data-edge-resolve-model]").forEach(button => { button.onclick = () => {
      const job = data.deployments.find(item => String(item.id) === button.dataset.edgeResolveModel);
      if (!job?.lease_expired || !["running", "unknown"].includes(job.status)) return;
      const panel = node.querySelector("#edge-system-resolve"), name = node.querySelector("#edge-system-resolve-name"), ack = node.querySelector("#edge-system-resolve-ack"), submit = node.querySelector("#edge-system-resolve-submit"), feedback = node.querySelector("#edge-system-resolve-feedback");
      panel.hidden = false; name.value = ""; ack.checked = false; feedback.textContent = ""; submit.disabled = true;
      const sync = () => { submit.disabled = name.value !== device.name || !ack.checked; };
      name.oninput = sync; ack.onchange = sync; name.focus();
      node.querySelector("#edge-system-resolve-cancel").onclick = () => { panel.hidden = true; if (button.isConnected) button.focus(); };
      submit.onclick = async () => {
        if (submit.disabled || name.value !== device.name || !ack.checked) return;
        submit.disabled = true; name.disabled = true; ack.disabled = true;
        try {
          await api(`${pathFor(device)}/model-deployments/${encodeURIComponent(job.id)}/resolve`, jsonOptions("POST", { confirmation_name: name.value, acknowledged: true, expected_updated_at: job.updated_at }));
          if (!alive()) return;
          panel.hidden = true; notify("任务已标记失败并解除占用；设备模型保持当前实际状态。"); await refresh(true);
        } catch (error) { if (alive() && error.status !== 401) feedback.textContent = error.message; }
        finally { if (alive()) { name.disabled = false; ack.disabled = false; sync(); } }
      };
    }; });
  }
}

async function openLegacyDeviceDeployment(device) {
  let options = null, releases = [], jobs = [], generating = false, monitor = null, jobRequest = 0;
  openModal({ title: `${device.name} · 模型部署`, subtitle: "将训练后的模型打包为 reCamera Pro App Center 应用。", wide: true,
    body: '<div id="edge-model-content" aria-live="polite">正在检查可用模型和部署连接…</div>', submitText: "部署到设备", onReady: () => { document.querySelector("#modal-submit").disabled = true; },
    onClose: () => { clearInterval(monitor); }, onSubmit: async () => {
      if (!options || generating) throw new Error("请等待模型检查或应用打包完成。");
      if (!options.deployment?.ssh_available || !options.deployment?.known_hosts_configured) throw new Error("服务器尚未完成 SSH 和可信设备主机配置，请先下载应用包手动安装。");
      if (jobs.some(item => !terminalStates.has(item.status))) throw new Error("此设备仍有待确认的部署，请等待当前任务结束后再操作。");
      if (jobs[0]?.status === "unknown" && !node.querySelector("#edge-verified-uncertain").checked) throw new Error("请先到设备 App Center 核实上次操作结果，再确认是否重新部署。");
      const release_id = Number(document.querySelector("#edge-release").value);
      const host = document.querySelector("#edge-ssh-host").value.trim(), username = document.querySelector("#edge-ssh-user").value.trim();
      const port = Number(document.querySelector("#edge-ssh-port").value);
      if (!release_id) throw new Error("请先生成或选择一个推理应用包。");
      if (!host || /[\s/@\\]/.test(host) || host.startsWith("-")) throw new Error("请填写设备 IP 或主机名，不要包含协议、路径或空格。");
      if (!/^[a-zA-Z_][a-zA-Z0-9_.-]*$/.test(username)) throw new Error("请填写有效的 SSH 用户名。");
      if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("SSH 端口应为 1–65535 之间的整数。");
      const modal = document.querySelector(".modal");
      await api(`${pathFor(device)}/deployments`, jsonOptions("POST", { release_id, host, port, username, replace: document.querySelector("#edge-replace-running").checked, start: true }));
      notify("部署任务已创建，安装和启动进度将在这里更新。");
      if (modal?.isConnected) { await refreshJobs(); setTimeout(syncSubmit, 0); }
    }});
  const node = document.querySelector("#edge-model-content"), modal = document.querySelector(".modal");
  modal.addEventListener("click", handlePackageDownload);
  try {
    const results = await Promise.all([api(`${pathFor(device)}/deployment-options`), api(`/api/edge/releases?device_id=${encodeURIComponent(device.id)}`), api(`${pathFor(device)}/deployments`)]);
    if (!node.isConnected) return;
    [options] = results; releases = (results[1].releases || []).filter(item => item.status === "ready"); jobs = results[2].deployments || [];
    mount();
    monitor = setInterval(() => { if (node.isConnected && !document.hidden) void refreshJobs(); }, 4000);
  } catch (error) {
    if (node.isConnected && error.status !== 401) { node.innerHTML = `<p class="inline-error">${esc(error.message)}</p><button class="btn ghost" id="edge-model-retry">重新检查</button>`; node.querySelector("#edge-model-retry").onclick = () => openLegacyDeviceDeployment(device); }
  }
  function mount() {
    const conversions = options.conversions || [], compatible = conversions.filter(item => item.compatible);
    const sshReady = options.deployment?.ssh_available && options.deployment?.known_hosts_configured;
    node.innerHTML = `<section class="edge-deploy-step"><h3><span>1</span>生成推理应用包</h3><p class="hint">在项目“训练与转换”中先完成 RKNN 转换，目标芯片选择 RV1126B。只有输入、输出约定兼容的目标检测模型可以打包。</p><div class="edge-build-row"><div class="field"><label for="edge-conversion">已转换的模型</label><select id="edge-conversion">${compatible.length ? "" : '<option value="">暂无兼容的转换结果</option>'}${conversions.map(item => `<option value="${Number(item.id)}" ${item.compatible ? "" : "disabled"}>转换 #${Number(item.id)} · ${esc(item.model_arch || "模型")} · ${Number(item.image_size) || "—"} px · ${esc(item.precision || "RKNN")}${item.compatible ? "" : ` · ${esc(item.reason || "不兼容")}`}</option>`).join("")}</select></div><button class="btn primary" id="edge-build-release" ${compatible.length ? "" : "disabled"}>生成应用包</button></div><p id="edge-build-feedback" class="hint" role="status"></p>${conversions.some(item => !item.compatible) ? `<details class="edge-advanced"><summary>查看未满足的模型条件</summary><ul>${conversions.filter(item => !item.compatible).map(item => `<li>转换 #${Number(item.id)}：${esc(item.reason || "模型约定不兼容")}</li>`).join("")}</ul></details>` : ""}<a class="edge-text-link" href="#/projects/${Number(device.project_id)}/train">前往训练与转换</a></section>
      <section class="edge-deploy-step"><h3><span>2</span>选择应用版本</h3><div class="field"><label for="edge-release">可部署的推理应用包</label><select id="edge-release"></select></div><div id="edge-release-detail"></div>${options.capture_app?.download_url ? `<div class="edge-capture-package">${downloadLink(options.capture_app.download_url, "下载联动应用包")}<span class="hint">采集应用独立安装，不随模型升级替换。</span></div>` : ""}</section>
      <section class="edge-deploy-step"><h3><span>3</span>部署到同一网络的设备</h3><p class="hint">由 CVSprout 服务器通过 SSH 和设备官方 App Center 完成安装。设备需要允许这些连接。</p>${sshReady ? "" : `<p class="edge-deploy-notice">${!options.deployment?.ssh_available ? "服务器尚未准备 SSH 部署工具。" : "服务器尚未配置可信设备主机文件。"}请管理员完成部署配置；现在可以下载应用包，打开设备 App Center 手动安装。</p>`}<div class="form-grid"><div class="field"><label for="edge-ssh-host">设备 IP / 主机名</label><input id="edge-ssh-host" placeholder="192.168.1.30" autocomplete="off" ${sshReady ? "" : "disabled"}/></div><div class="field"><label for="edge-ssh-port">SSH 端口</label><input id="edge-ssh-port" type="number" min="1" max="65535" value="22" ${sshReady ? "" : "disabled"}/></div><div class="field"><label for="edge-ssh-user">SSH 用户</label><input id="edge-ssh-user" value="root" autocomplete="off" ${sshReady ? "" : "disabled"}/></div></div><label class="check-line"><input id="edge-replace-running" type="checkbox" ${sshReady ? "" : "disabled"}/>若推理应用正在运行，允许停止并替换此应用</label><p class="hint">替换时推理会短暂中断，新的模型版本安装后自动启动。</p><label class="check-line edge-uncertain-confirm" id="edge-uncertain-label" ${jobs[0]?.status === "unknown" ? "" : "hidden"}><input id="edge-verified-uncertain" type="checkbox"/>我已到设备 App Center 核实上次不确定的操作结果，确认现在重新部署</label><div id="edge-deployment-history" aria-live="polite">${deploymentHistoryMarkup(jobs)}</div></section>`;
    node.querySelector("#edge-build-release").onclick = buildRelease;
    node.querySelector("#edge-release").onchange = renderRelease;
    node.querySelector("#edge-verified-uncertain").onchange = syncSubmit;
    renderReleaseOptions(); syncSubmit();
  }
  function renderReleaseOptions(selected = null) {
    const select = node.querySelector("#edge-release");
    select.innerHTML = releases.length ? releases.map(item => `<option value="${esc(item.id)}">${esc(item.version || item.id)} · 转换 #${Number(item.conversion_id)}</option>`).join("") : '<option value="">先生成推理应用包</option>';
    if (selected) select.value = selected;
    renderRelease();
  }
  function renderRelease() {
    const release = releases.find(item => String(item.id) === node.querySelector("#edge-release").value);
    node.querySelector("#edge-release-detail").innerHTML = release ? `<div class="edge-release-info">${downloadLink(release.download_url, "下载推理应用包")}<span>${(Number(release.size_bytes || 0) / 1048576).toFixed(1)} MiB</span></div><details class="edge-advanced"><summary>版本校验信息</summary><p>SHA-256：<code>${esc(release.sha256 || "—")}</code></p><p>设备端实际精度与速度请在目标设备上验证。</p></details>` : '<p class="hint">生成后可直接下载，或继续填写设备地址进行部署。</p>';
    syncSubmit();
  }
  function syncSubmit() {
    if (!modal.isConnected) return;
    document.querySelector("#modal-submit").disabled = generating || !options?.deployment?.ssh_available || !options?.deployment?.known_hosts_configured
      || !node.querySelector("#edge-release")?.value || jobs.some(item => !terminalStates.has(item.status))
      || jobs[0]?.status === "unknown" && !node.querySelector("#edge-verified-uncertain")?.checked;
  }
  async function buildRelease() {
    if (generating) return;
    const conversion_id = Number(node.querySelector("#edge-conversion").value);
    if (!options.conversions.some(item => Number(item.id) === conversion_id && item.compatible)) return;
    const button = node.querySelector("#edge-build-release"), feedback = node.querySelector("#edge-build-feedback");
    generating = true; button.disabled = true; button.textContent = "正在打包…"; feedback.classList.remove("feedback-error"); feedback.textContent = "正在校验模型并生成 App Center 安装包，请稍候。"; syncSubmit();
    try {
      const result = await api("/api/edge/releases", jsonOptions("POST", { conversion_id }));
      if (!node.isConnected) return;
      const release = result.release || result;
      releases = [release, ...releases.filter(item => item.id !== release.id)]; renderReleaseOptions(String(release.id));
      feedback.textContent = "应用包已就绪，可以下载或部署到设备。";
    } catch (error) { if (node.isConnected && error.status !== 401) { feedback.textContent = error.message; feedback.classList.add("feedback-error"); } }
    finally { generating = false; if (node.isConnected) { button.disabled = false; button.textContent = "生成应用包"; syncSubmit(); } }
  }
  async function refreshJobs() {
    if (!node.isConnected) return;
    const request = ++jobRequest;
    try {
      const result = await api(`${pathFor(device)}/deployments`);
      if (!node.isConnected || request !== jobRequest) return;
      const updated = result.deployments || [];
      if (JSON.stringify(updated) !== JSON.stringify(jobs)) { jobs = updated; node.querySelector("#edge-deployment-history").innerHTML = deploymentHistoryMarkup(jobs); }
      node.querySelector("#edge-uncertain-label").hidden = jobs[0]?.status !== "unknown";
      syncSubmit();
    } catch (error) { if (node.isConnected && request === jobRequest && error.status !== 401) { node.querySelector("#edge-deployment-history").textContent = `部署状态更新失败：${error.message}`; } }
  }
}
