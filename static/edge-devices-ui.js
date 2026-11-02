import { state, api, app, topbar, escapeHtml as esc, notify, openModal, jsonOptions, closeModal, downloadArtifact } from "./app.js?v=0.26.1";
import { isAdmin } from "./task-ui.js?v=0.26.1";

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
    <ol class="edge-workflow" aria-label="设备与训练工作流"><li><span>1</span><strong>连接与采集</strong><small>安装采集应用，图片自动导入</small></li><li><span>2</span><strong>标注与训练</strong><small>沿用项目的审核和数据版本</small></li><li><span>3</span><strong>部署回设备</strong><small>转换 RV1126B 模型并发布应用</small></li></ol>
    <p class="edge-page-note">两个应用可独立运行：采集应用负责上传原图，推理应用使用打包的模型。图片导入后由管理员创建新的任务块，已领取的任务块保持原样。</p>
    <p class="edge-refresh-note">页面可见时每 5 秒更新连接和采集状态。<span id="edge-refresh-time">正在读取状态…</span></p><div id="edge-page-feedback" class="inline-error" role="status" hidden></div><div id="edge-device-results"><p class="hint">正在读取设备…</p></div>
    <details class="edge-help"><summary>首次连接前需要准备什么？</summary><p>使用支持 Python SDK 与 App Center 的 reCamera Pro 固件。让设备能访问 CVSprout 的局域网地址或 HTTPS 域名；使用启动脚本时，在 <code>.env</code> 设置 <code>MDP_BIND_ADDRESS=0.0.0.0</code> 并重新启动。连接设备后，将服务地址和一次性显示的设备令牌填入采集应用配置。</p><p>远程部署还需要 CVSprout 服务器能够通过 SSH 访问设备，并由管理员配置可信主机和密钥。也可下载推理应用包，在设备的 App Center 手动安装。</p></details>
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

function showDeviceToken(device, token) {
  openModal({ title: "配置采集应用", subtitle: "设备令牌只显示这一次。请现在复制到 reCamera Pro 的采集应用配置中。", body: `<div class="edge-token-setup"><p>在设备 App Center 安装 <strong>CVSprout Capture</strong>，然后填写：</p><div class="field"><label for="edge-copy-url">server_url · CVSprout 地址</label><input id="edge-copy-url" readonly value="${esc(device.server_url || "")}"/></div><div class="field"><label for="edge-copy-token">device_token · 设备令牌</label><div class="edge-token-control"><input id="edge-copy-token" type="password" readonly autocomplete="off" spellcheck="false"/><button class="btn ghost" type="button" id="edge-copy-token-button">复制令牌</button></div><label class="check-line"><input id="edge-show-token" type="checkbox"/>显示令牌</label></div><p class="hint">请完整复制以 cvsprout_device_ 开头的设备专用令牌，不要填写网页登录密码或管理员 API Key。关闭后无法再次查看；丢失时可在设备卡片重置。</p><div id="edge-capture-download"></div><div id="edge-setup-connection" class="edge-setup-connection waiting" role="status"><strong>等待设备首次连接…</strong><p>保存参数并启动设备上的采集应用后，这里会自动确认连接。请保留此窗口，令牌仍可复制。</p></div></div>`, submitText: "我已保存配置", onReady: () => {
      const input = document.querySelector("#edge-copy-token"), modal = document.querySelector(".modal"); input.value = token;
      tokenSetup = { device, minimumSerial: requestSerial + 1, node: modal.querySelector("#edge-setup-connection") }; refreshTokenStatus();
      void loadDevices({ background: true });
      modal.addEventListener("click", handlePackageDownload);
      document.querySelector("#edge-show-token").onchange = event => { input.type = event.target.checked ? "text" : "password"; };
      document.querySelector("#edge-copy-token-button").onclick = async () => {
        try { if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable"); await navigator.clipboard.writeText(input.value); if (modal.isConnected) notify("设备令牌已复制。"); }
        catch (_) { if (modal.isConnected) { input.type = "text"; document.querySelector("#edge-show-token").checked = true; input.focus(); input.select(); notify("请按 Ctrl+C 或使用系统菜单复制选中的令牌。", "warning"); } }
      };
      void api(`${pathFor(device)}/deployment-options`).then(result => {
        if (modal.isConnected && result.capture_app?.download_url) document.querySelector("#edge-capture-download").innerHTML = downloadLink(result.capture_app.download_url, "下载采集应用包");
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
  const title = available ? `<a href="#/projects/${Number(device.project_id)}/annotate?asset=${assetId}">${esc(item.filename || `图片 ${assetId}`)}</a>`
    : `<span>原图已删除${item.filename ? ` · ${esc(item.filename)}` : ""}</span>`;
  return `<article><div>${title}<span>${esc(dateText(item.captured_at))}</span></div><p>${available ? item.duplicate ? "已有相同图片 · 已关联" : "图片已导入" : "采集记录保留"}${item.model_release ? ` · 模型 ${esc(item.model_release)}` : ""}</p><small>收到时间：${esc(dateText(item.received_at))} · 采集 ID：${esc(item.capture_id)}</small></article>`;
}

function openCaptureRecords(device) {
  let offset = 0, serial = 0;
  openModal({ title: `${device.name} · 采集记录`, subtitle: "同一内容的图片只保留一份，每一次采集仍可追溯。", wide: true, body: '<div id="edge-capture-records" aria-live="polite">正在读取…</div>', submitText: "关闭", onSubmit: async () => closeModal(), onReady: () => { void load(); } });
  async function load() {
    const node = document.querySelector("#edge-capture-records"), request = ++serial;
    if (!node?.isConnected) return;
    try {
      const result = await api(`${pathFor(device)}/captures?limit=25&offset=${offset}`);
      if (!node.isConnected || request !== serial) return;
      node.innerHTML = `<div class="edge-record-list">${(result.items || []).map(item => captureRecordMarkup(item, device)).join("") || '<p class="hint">尚未收到图片。请确认采集应用正在运行，地址和令牌已保存。</p>'}</div><div class="edge-record-pagination"><span>共 ${numberText(result.total)} 条记录</span><button class="btn ghost" id="edge-record-prev" ${offset === 0 ? "disabled" : ""}>上一页</button><button class="btn ghost" id="edge-record-next" ${offset + 25 >= result.total ? "disabled" : ""}>下一页</button></div>`;
      node.querySelector("#edge-record-prev").onclick = () => { offset = Math.max(0, offset - 25); void load(); };
      node.querySelector("#edge-record-next").onclick = () => { offset += 25; void load(); };
    } catch (error) { if (node.isConnected && request === serial && error.status !== 401) node.innerHTML = `<p class="inline-error">${esc(error.message)}</p><button class="btn ghost" id="edge-record-retry">重试</button>`; node.querySelector("#edge-record-retry")?.addEventListener("click", load); }
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

async function openDeviceDeployment(device) {
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
    if (node.isConnected && error.status !== 401) { node.innerHTML = `<p class="inline-error">${esc(error.message)}</p><button class="btn ghost" id="edge-model-retry">重新检查</button>`; node.querySelector("#edge-model-retry").onclick = () => openDeviceDeployment(device); }
  }
  function mount() {
    const conversions = options.conversions || [], compatible = conversions.filter(item => item.compatible);
    const sshReady = options.deployment?.ssh_available && options.deployment?.known_hosts_configured;
    node.innerHTML = `<section class="edge-deploy-step"><h3><span>1</span>生成推理应用包</h3><p class="hint">在项目“训练与转换”中先完成 RKNN 转换，目标芯片选择 RV1126B。只有输入、输出约定兼容的目标检测模型可以打包。</p><div class="edge-build-row"><div class="field"><label for="edge-conversion">已转换的模型</label><select id="edge-conversion">${compatible.length ? "" : '<option value="">暂无兼容的转换结果</option>'}${conversions.map(item => `<option value="${Number(item.id)}" ${item.compatible ? "" : "disabled"}>转换 #${Number(item.id)} · ${esc(item.model_arch || "模型")} · ${Number(item.image_size) || "—"} px · ${esc(item.precision || "RKNN")}${item.compatible ? "" : ` · ${esc(item.reason || "不兼容")}`}</option>`).join("")}</select></div><button class="btn primary" id="edge-build-release" ${compatible.length ? "" : "disabled"}>生成应用包</button></div><p id="edge-build-feedback" class="hint" role="status"></p>${conversions.some(item => !item.compatible) ? `<details class="edge-advanced"><summary>查看未满足的模型条件</summary><ul>${conversions.filter(item => !item.compatible).map(item => `<li>转换 #${Number(item.id)}：${esc(item.reason || "模型约定不兼容")}</li>`).join("")}</ul></details>` : ""}<a class="edge-text-link" href="#/projects/${Number(device.project_id)}/train">前往训练与转换</a></section>
      <section class="edge-deploy-step"><h3><span>2</span>选择应用版本</h3><div class="field"><label for="edge-release">可部署的推理应用包</label><select id="edge-release"></select></div><div id="edge-release-detail"></div>${options.capture_app?.download_url ? `<div class="edge-capture-package">${downloadLink(options.capture_app.download_url, "下载采集应用包")}<span class="hint">采集应用独立安装，不随模型升级替换。</span></div>` : ""}</section>
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
