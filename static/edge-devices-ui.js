import { state, api, app, topbar, escapeHtml as esc, notify, openModal, jsonOptions, closeModal, downloadArtifact } from "./app.js?v=0.26.0";
import { isAdmin } from "./task-ui.js?v=0.26.0";

let devices = [], projects = [], requestSerial = 0, renderedDevicesSignature = "";
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

export function deviceConnection(device, now = Date.now()) {
  if (device.revoked_at || device.enabled === false) return { kind: "revoked", label: "已撤销接入" };
  if (device.project_archived) return { kind: "paused", label: "项目已归档 · 采集暂停" };
  if (!device.last_seen_at) return { kind: "waiting", label: "等待首次连接" };
  const age = now - new Date(device.last_seen_at).getTime();
  return Number.isFinite(age) && age >= -60000 && age < 180000
    ? { kind: "recent", label: device.capture_enabled ? "最近在线 · 采集已启用" : "最近在线 · 采集暂停" }
    : { kind: "offline", label: "暂未收到心跳" };
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
  renderedDevicesSignature = "";
  app.innerHTML = `<div class="app-shell">${topbar("devices")}<main class="dashboard edge-devices-page"><div class="dashboard-inner">
    <div class="page-head"><div><h1>边缘设备</h1><p class="page-subtitle">让 reCamera Pro 采集的图片进入项目，把训练后的模型部署回设备。</p></div><div class="edge-actions"><button class="btn ghost" id="refresh-edge-devices">刷新</button><button class="btn primary" id="add-edge-device">连接设备</button></div></div>
    <ol class="edge-workflow" aria-label="设备与训练工作流"><li><span>1</span><strong>连接与采集</strong><small>安装采集应用，图片自动导入</small></li><li><span>2</span><strong>标注与训练</strong><small>沿用项目的审核和数据版本</small></li><li><span>3</span><strong>部署回设备</strong><small>转换 RV1126B 模型并发布应用</small></li></ol>
    <p class="edge-page-note">两个应用可独立运行：采集应用负责上传原图，推理应用使用打包的模型。图片导入后由管理员创建新的任务块，已领取的任务块保持原样。</p>
    <div id="edge-page-feedback" class="inline-error" role="status" hidden></div><div id="edge-device-results" aria-live="polite"><p class="hint">正在读取设备…</p></div>
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
  state.pollTimer = setInterval(() => {
    if (current() && !document.hidden && !document.querySelector(".modal")) void loadDevices({ background: true });
  }, 15000);
}

async function loadDevices({ background = false } = {}) {
  const node = document.querySelector("#edge-device-results"), currentPage = pageContext(node), serial = ++requestSerial;
  const current = () => currentPage() && serial === requestSerial;
  if (!currentPage()) return;
  if (!background) node.setAttribute("aria-busy", "true");
  try {
    const result = await api("/api/edge/devices");
    if (!current()) return;
    const next = result.devices || [], feedback = document.querySelector("#edge-page-feedback");
    feedback.hidden = true;
    // Keep keyboard focus, scroll position and expanded controls stable during heartbeats.
    const focused = node.contains(document.activeElement), unchanged = JSON.stringify(next) === renderedDevicesSignature;
    devices = next;
    node.querySelectorAll("[data-edge-device]").forEach(card => {
      const device = devices.find(item => item.id === card.dataset.edgeDevice), badge = card.querySelector(".edge-connection");
      if (device && badge) { const status = deviceConnection(device); badge.className = `edge-connection ${status.kind}`; badge.textContent = status.label; }
    });
    if (background && focused) return;
    if (!background || !unchanged) renderDeviceList(node);
  } catch (error) {
    if (error.status === 401 || !current()) return;
    const feedback = document.querySelector("#edge-page-feedback");
    feedback.textContent = `设备列表更新失败：${error.message}`; feedback.hidden = false;
    if (!devices.length && !background) node.innerHTML = '<p class="hint">请点击上方“刷新”重试。</p>';
  } finally { if (current()) node.removeAttribute("aria-busy"); }
}

export function deviceCardMarkup(device) {
  const status = deviceConnection(device), revoked = status.kind === "revoked";
  const queue = device.heartbeat?.pending_images ?? device.heartbeat?.queue_length;
  return `<article class="edge-device-card" data-edge-device="${esc(device.id)}"><div class="edge-card-heading"><div><h2>${esc(device.name)}</h2><a href="#/projects/${Number(device.project_id)}/data">${esc(device.project_name || `项目 ${device.project_id}`)}</a></div><span class="edge-connection ${status.kind}">${status.label}</span></div>
    <dl class="edge-device-stats"><div><dt>收到的采集记录</dt><dd>${numberText(device.capture_count)}</dd></div><div><dt>关联的项目图片</dt><dd>${numberText(device.asset_count)}</dd></div><div><dt>设备待上传</dt><dd>${queue == null ? "—" : numberText(queue)}</dd></div></dl>
    <div class="edge-device-detail"><span>最近心跳：${esc(dateText(device.last_seen_at))}</span><span>最近采集：${esc(dateText(device.last_capture_at))}</span><span>${revoked ? "令牌已失效" : device.project_archived ? "项目已归档，已暂停新采集" : device.capture_enabled ? `每 ${numberText(device.capture_interval_seconds)} 秒采集一张` : "已暂停新采集，队列继续上传"}</span>${device.heartbeat?.last_error ? `<span class="feedback-error">最近上报：${esc(device.heartbeat.last_error)}</span>` : ""}</div>
    <div class="edge-card-actions"><button class="btn ghost" data-edge-captures="${esc(device.id)}">采集记录</button>${revoked ? "" : `<button class="btn ghost" data-edge-settings="${esc(device.id)}">采集设置</button><button class="btn primary" data-edge-model="${esc(device.id)}">模型部署</button><details class="edge-more"><summary>更多</summary><div><button data-edge-rotate="${esc(device.id)}">重置设备令牌</button><button class="danger" data-edge-revoke="${esc(device.id)}">撤销接入</button></div></details>`}</div></article>`;
}

function renderDeviceList(node) {
  node.innerHTML = devices.length ? `<div class="edge-device-grid">${devices.map(deviceCardMarkup).join("")}</div>`
    : '<div class="edge-empty"><strong>连接你的第一台 reCamera Pro</strong><p>选择图片要进入的项目，获取专属令牌，即可开始自动采集。</p><button class="btn primary" id="empty-connect-device">连接设备</button></div>';
  renderedDevicesSignature = JSON.stringify(devices);
  node.querySelector("#empty-connect-device")?.addEventListener("click", () => openDeviceSettings());
  const bind = (attribute, handler) => node.querySelectorAll(`[${attribute}]`).forEach(button => {
    button.onclick = () => { button.closest("details")?.removeAttribute("open"); const device = devices.find(item => item.id === button.getAttribute(attribute)); if (device) handler(device); };
  });
  bind("data-edge-settings", openDeviceSettings); bind("data-edge-captures", openCaptureRecords);
  bind("data-edge-model", openDeviceDeployment); bind("data-edge-rotate", openRotateToken); bind("data-edge-revoke", openRevokeDevice);
}

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
  openModal({ title: "配置采集应用", subtitle: "设备令牌只显示这一次。请现在复制到 reCamera Pro 的采集应用配置中。", body: `<div class="edge-token-setup"><p>在设备 App Center 安装 <strong>CVSprout Capture</strong>，然后填写：</p><div class="field"><label for="edge-copy-url">server_url · CVSprout 地址</label><input id="edge-copy-url" readonly value="${esc(device.server_url || "")}"/></div><div class="field"><label for="edge-copy-token">device_token · 设备令牌</label><div class="edge-token-control"><input id="edge-copy-token" type="password" readonly autocomplete="off" spellcheck="false"/><button class="btn ghost" type="button" id="edge-copy-token-button">复制令牌</button></div><label class="check-line"><input id="edge-show-token" type="checkbox"/>显示令牌</label></div><p class="hint">令牌只用于设备上传，不是管理员登录凭证。关闭后无法再次查看；丢失时可在设备卡片重置。</p><div id="edge-capture-download"></div></div>`, submitText: "我已保存配置", onReady: () => {
      const input = document.querySelector("#edge-copy-token"), modal = document.querySelector(".modal"); input.value = token;
      modal.addEventListener("click", handlePackageDownload);
      document.querySelector("#edge-show-token").onchange = event => { input.type = event.target.checked ? "text" : "password"; };
      document.querySelector("#edge-copy-token-button").onclick = async () => {
        try { if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable"); await navigator.clipboard.writeText(input.value); if (modal.isConnected) notify("设备令牌已复制。"); }
        catch (_) { if (modal.isConnected) { input.type = "text"; document.querySelector("#edge-show-token").checked = true; input.focus(); input.select(); notify("请按 Ctrl+C 或使用系统菜单复制选中的令牌。", "warning"); } }
      };
      void api(`${pathFor(device)}/deployment-options`).then(result => {
        if (modal.isConnected && result.capture_app?.download_url) document.querySelector("#edge-capture-download").innerHTML = downloadLink(result.capture_app.download_url, "下载采集应用包");
      }).catch(() => {});
    }, onClose: () => { const input = document.querySelector("#edge-copy-token"); if (input) input.value = ""; token = ""; }, onSubmit: async () => closeModal() });
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
