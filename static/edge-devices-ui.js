import { t, ta, getLocale } from "./i18n.js?v=0.29.3";
import { state, api, app, topbar, escapeHtml as esc, notify, openModal, jsonOptions, closeModal, downloadArtifact } from "cvsprout/app";
import { isAdmin } from "./task-ui.js?v=0.29.3";

let devices = [], projects = [], requestSerial = 0, listRequest = null, listReceivedAt = 0, listReadFailed = false, listCompletedSerial = 0, tokenSetup = null;
const DEVICE_REFRESH_MS = 5000, DEVICE_REFRESH_STALE_MS = 15000;
const dateText = value => value ? new Date(value).toLocaleString(getLocale()) : t("暂无记录");
const numberText = value => Math.max(0, Number(value) || 0).toLocaleString(getLocale());
const pathFor = device => `/api/edge/devices/${encodeURIComponent(device.id)}`;
const terminalStates = new Set(["succeeded", "completed", "failed", "cancelled", "unknown"]);
const stageNames = { queued: t("等待部署"), connecting: t("连接设备"), checking: t("检查设备"), uploading: t("传输应用包"), installing: t("安装应用"), starting: t("启动应用"), verifying: t("验证运行状态"), running: t("正在部署"), succeeded: t("部署完成"), completed: t("部署完成"), failed: t("部署失败"), cancelled: t("已取消"), unknown: t("结果待核实"), connection_lost: t("连接中断"), interrupted: t("任务被中断"), preparation: t("准备应用包") };
const finalStageNames = { ...stageNames, running: t("应用已启动") };

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
  if (device.revoked_at || device.enabled === false || device.connection?.state === "revoked") return { kind: "revoked", label: t("已撤销接入") };
  if (stale) return { kind: "unknown", label: t("连接状态待确认") };
  const connection = device.connection;
  if (connection?.state === "reconnect_required") return { kind: "reconnect", label: t("等待使用新令牌连接") };
  if (connection?.state === "never_connected" || (!connection && !device.last_seen_at)) return { kind: "waiting", label: t("尚未连接") };
  const age = connection ? (typeof connection.age_seconds === "number" ? connection.age_seconds * 1000 + Math.max(0, now - observedAt) : NaN)
    : now - new Date(device.last_seen_at).getTime();
  const timeout = connection ? Math.max(1, Number(connection.timeout_seconds) || 90) * 1000 : 90000;
  return (!connection || connection.state === "online") && Number.isFinite(age) && age >= -60000 && age < timeout
    ? { kind: "online", label: t("在线 · 已连接") } : { kind: "offline", label: t("离线 · 联系已超时") };
}

export function deviceDiagnosis(device, status = deviceConnection(device)) {
  const ingestion = device.ingestion || {}, pending = ingestion.pending_images ?? device.heartbeat?.pending_images;
  if (status.kind === "unknown") return { kind: "warning", title: t("暂时无法确认设备连接"), text: t("设备状态更新失败或尚未返回。请检查浏览器到 CVSprout 的连接并点击刷新；以下计数保留上次结果。") };
  if (status.kind === "revoked") return { kind: "muted", title: t("设备接入已撤销"), text: t("此令牌无法再上传图片。已导入数据保留，需要恢复时请创建新的设备接入。") };
  if (status.kind === "reconnect") return { kind: "warning", title: t("请更新设备上的令牌"), text: t("旧令牌已经失效。将新 device_token 保存到 CVSprout Capture 应用配置并重启应用，收到有效请求后会自动显示已连接。") };
  if (status.kind === "waiting") return { kind: "warning", title: t("尚未收到设备的有效连接"), text: t("在设备 App Center 启动 CVSprout Capture，并确认已保存 server_url 和 device_token。地址须为设备能访问的服务器地址，不能使用 localhost；令牌请使用此设备卡片生成的完整 device_token，不是网页登录密码或管理员 API Key。仅创建接入或打开采集开关不会连接设备，请同时检查网络与应用日志。") };
  if (status.kind === "offline") return { kind: "warning", title: t("设备暂时失去联系"), text: t("请检查采集应用是否仍在运行，以及设备到 CVSprout 的网络、服务地址和令牌。") + (Number(pending) > 0 ? t(" 上次上报的队列仍有图片，恢复连接后会继续补传。") : "") };
  const details = {
    waiting_connection: ["warning", t("等待采集应用同步配置"), t("请确认设备启动的是 CVSprout Capture，并检查服务地址和令牌。")],
    waiting_heartbeat: ["warning", t("已连接，等待采集应用心跳"), t("服务器已收到设备请求，尚未收到近期运行状态。请稍候；若持续不变，请检查采集应用日志。")],
    waiting_first_image: ["muted", t("已连接，等待第一张图片"), t("连接正常。图片会按采集间隔自动上传；若持续没有图片，请检查设备采集应用的相机帧与上传日志。")],
    waiting_camera: ["warning", t("已连接，正在等待相机画面"), t("采集应用尚未获得可用的相机帧。请检查设备相机预览和帧服务，以及应用运行日志。")],
    no_recent_images: ["warning", t("已连接，但近期没有收到新图片"), t("最近的图片上传已超过预期采集间隔。请检查设备相机画面、采集数量上限与应用日志；连接在线并不代表图片上传正常。")],
    receiving: ["success", t("已收到设备图片"), t("可以在项目数据页查看已导入的图片，并继续标注和训练。相同内容的图片会去重，每次采集仍保留记录。")],
    upload_pending: ["warning", t("图片已采集，正在等待上传"), t("设备队列中还有图片，上传成功后这里会自动更新。若队列持续增长，请检查网络和最近上报的错误。")],
    paused: ["muted", t("新图片采集已暂停"), t("在“采集设置”中启用新图片采集。已进入设备队列的图片仍会继续上传。")],
    archived: ["warning", t("项目已归档，采集已暂停"), t("请先恢复项目，再检查采集设置。设备中的待上传图片会保留。")],
    session_complete: ["muted", t("本次启动的采集数量已达上限"), t("如需继续采集，请调整设备应用的 max_session_images 参数并重启；设为 0 表示不限。")],
    queue_full: ["warning", t("设备离线队列已满"), t("应用保留待上传图片并暂停新增。请先恢复上传或释放设备存储空间，不要删除尚未确认的队列文件。")],
    error: ["warning", t("采集或上传遇到问题"), t("设备仍在连接服务器，但尚未完成图片上传。请查看最近上报的错误，并检查设备应用日志。")],
  };
  const fallback = device.project_archived ? "archived" : device.capture_enabled === false ? "paused"
    : device.heartbeat?.last_error ? "error" : Number(pending) > 0 ? "upload_pending"
    : Number(device.capture_count) > 0 ? "receiving" : "waiting_first_image";
  const [kind, title, explanation] = details[ingestion.state] || details[fallback];
  const code = ingestion.last_error || (ingestion.heartbeat_fresh === false ? "" : device.heartbeat?.last_error) || "";
  const actions = {
    server_unreachable_or_invalid_response: t("请确认服务已监听局域网地址、防火墙允许访问、server_url 可从设备访问，HTTPS 证书有效。"),
    server_redirect_refused: t("服务地址发生了跳转，请将设备的 server_url 改为最终的 HTTP / HTTPS 地址。"),
    http_401: t("设备令牌无效。请将最新 device_token 保存到采集应用配置并重启。"),
    http_403: t("服务器拒绝了请求，请检查设备接入权限和服务端日志。"),
    http_409: t("请检查项目是否归档，以及服务器返回的采集冲突记录。"),
    http_413: t("图片超过上传大小限制，请降低设备应用 JPEG 质量或检查服务器上传限制。"),
    spool_full_capture_paused: t("设备队列已满。请先恢复上传或释放存储空间，应用会保留尚未确认的图片。"),
    image_exceeds_server_limit: t("采集图片过大，请降低设备应用的 JPEG 质量后重启。"),
    capture_spool_corrupt: t("队列记录损坏，已保留原文件。请停止采集并联系维护者恢复，避免手动删除队列。"),
    spool_storage_error: t("设备存储发生错误，请检查剩余空间和存储状态，并保留未上传的队列。"),
  };
  return { kind, title, text: actions[code] || explanation };
}

function connectionOptions() {
  return { stale: listReadFailed || Boolean(listReceivedAt && Date.now() - listReceivedAt > DEVICE_REFRESH_STALE_MS), observedAt: listReceivedAt || Date.now() };
}

function captureSetting(device) {
  return device.project_archived ? t("采集设置：项目已归档，已暂停新采集") : device.capture_enabled === false
    ? t("采集设置：已暂停新采集，队列继续上传") : t("采集设置：已启用，每 {0} 秒一张", [numberText(device.capture_interval_seconds)]);
}

function cardValues(device, status) {
  const ingestion = device.ingestion || {}, heartbeat = device.heartbeat || {};
  const pending = ingestion.pending_images ?? heartbeat.pending_images ?? heartbeat.queue_length;
  const diagnosis = deviceDiagnosis(device, status);
  return {
    name: device.name, project: device.project_name || t("项目 {0}", [device.project_id]),
    captures: numberText(device.capture_count), assets: numberText(device.asset_count), pending: pending == null ? "—" : numberText(pending),
    pendingLabel: ingestion.heartbeat_fresh === false ? t("待上传（上次上报）") : t("设备待上传"),
    setting: captureSetting(device), contact: t("最近联系：{0}", [dateText(device.connection?.last_contact_at || device.last_seen_at)]),
    heartbeat: t("最近心跳：{0}", [dateText(device.connection?.last_heartbeat_at)]), uploaded: t("最近收到图片：{0}", [dateText(device.last_capture_at)]),
    version: t("采集应用版本：{0}", [heartbeat.app_version || t("尚未上报")]),
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

function projectDeploymentContext(projectId) {
  const epoch = state.selectionEpoch, session = state.session;
  return () => isAdmin() && state.currentSection === "train" && !state.project?.archived_at && Number(state.project?.id) === Number(projectId)
    && state.selectionEpoch === epoch && state.session === session;
}

function projectDeviceAvailability(device, projectId, { observedAt = Date.now(), stale = false } = {}) {
  const status = deviceConnection(device, Date.now(), { observedAt, stale });
  if (Number(device.project_id) !== Number(projectId)) return { ...status, selectable: false, reason: t("设备不属于当前项目") };
  if (device.enabled !== true || device.revoked_at) return { ...status, selectable: false, reason: t("设备接入已停用或撤销") };
  if (device.project_archived) return { ...status, selectable: false, reason: t("设备所属项目已归档") };
  return { ...status, selectable: status.kind === "online", reason: status.kind === "online" ? t("已连接，可以继续检查系统模型能力") : deviceDiagnosis(device, status).text };
}

export async function openProjectDeployment({ project, run, conversion }) {
  const current = projectDeploymentContext(project?.id);
  if (!current() || !Number.isSafeInteger(Number(project?.id)) || Number(project.id) < 1
      || Number(run?.project_id) !== Number(project.id) || Number(conversion?.training_run_id) !== Number(run?.id)
      || !Number.isSafeInteger(Number(conversion?.id)) || Number(conversion.id) < 1
      || conversion.status !== "completed" || conversion.format !== "rknn" || conversion.settings?.rknn?.deployment_profile !== "recamera_system") {
    notify(t("请从当前项目已完成的 reCamera 系统模型转换记录进入部署。"), "error"); return;
  }
  let node, modal, request = null, serial = 0, monitor = null, items = [], receivedAt = 0, failed = true, loading = false, signature = "", suggestedDevice = false;
  const alive = () => current() && Boolean(node?.isConnected) && Boolean(modal?.isConnected);
  const availability = device => projectDeviceAvailability(device, project.id, { observedAt: receivedAt, stale: failed || !receivedAt || Date.now() - receivedAt > DEVICE_REFRESH_STALE_MS });
  openModal({ title: t("选择部署设备"), subtitle: t("{0} · {1} · 转换 #{2}", [project.name || t("当前项目"), run.name || t("训练 {0}", [run.id]), conversion.id]), wide: true,
    body: `<div id="edge-project-device-content"><p class="hint">${t("选择当前项目的在线设备。模型已选定，下一步确认部署方式即可。")}</p><div id="edge-project-device-feedback" class="inline-error" role="status" hidden></div><div class="field"><label for="edge-project-device-select">${t("项目设备")}</label><select id="edge-project-device-select" aria-describedby="edge-project-device-status"><option value="">${t("正在读取设备…")}</option></select></div><div id="edge-project-device-status" class="edge-project-device-status" role="status"></div><div class="edge-actions"><button type="button" class="btn ghost" id="edge-project-device-refresh">${t("刷新设备状态")}</button><a class="edge-text-link" href="#/devices" id="edge-project-device-manage">${t("前往设备管理")}</a></div></div>`,
    submitText: t("继续部署"), onReady: () => { document.querySelector("#modal-submit").disabled = true; },
    onClose: () => { clearInterval(monitor); serial++; }, onSubmit: async () => {
      if (!alive()) return;
      const selected = node.querySelector("#edge-project-device-select").value;
      try {
        await refresh(true);
        if (!alive()) return;
        const device = items.find(item => String(item.id) === selected);
        if (!device || !availability(device).selectable || failed) throw new Error(t("所选设备尚未连接或状态已变化，请刷新并选择当前项目的在线设备。"));
        await openDeviceDeployment(device, { current, project, run, conversion });
      } finally { if (alive()) setTimeout(render, 0); }
    }});
  node = document.querySelector("#edge-project-device-content"); modal = document.querySelector(".modal");
  node.querySelector("#edge-project-device-select").onchange = render;
  node.querySelector("#edge-project-device-refresh").onclick = () => { void refresh(); };
  node.querySelector("#edge-project-device-manage").onclick = () => closeModal();
  await refresh();
  if (alive()) monitor = setInterval(() => { if (!alive()) { clearInterval(monitor); return; } if (!document.hidden) { render(); void refresh(); } }, DEVICE_REFRESH_MS);

  function render() {
    if (!alive()) return;
    const select = node.querySelector("#edge-project-device-select"), selected = select.value;
    const rows = items.map(device => ({ device, status: availability(device) }));
    const next = JSON.stringify(rows.map(({ device, status }) => [device.id, device.name, status.label, status.selectable]));
    if (next !== signature) {
      select.innerHTML = `<option value="">${t("请选择当前项目的在线设备")}</option>` + rows.map(({ device, status }) => `<option value="${esc(device.id)}" ${status.selectable ? "" : "disabled"}>${esc(device.name)} · ${esc(status.label)}</option>`).join("");
      if (items.some(item => String(item.id) === selected)) select.value = selected;
      else if (!suggestedDevice && !selected) {
        const available = rows.filter(({ status }) => status.selectable);
        if (available.length === 1) select.value = String(available[0].device.id);
      }
      if (rows.length) suggestedDevice = true;
      signature = next;
    }
    const chosen = rows.find(({ device }) => String(device.id) === select.value);
    const online = rows.filter(({ status }) => status.selectable).length;
    node.querySelector("#edge-project-device-status").innerHTML = !receivedAt && loading ? `<strong>${t("正在读取项目设备状态…")}</strong>` : !receivedAt && failed ? `<strong>${t("设备状态暂不可用，请刷新重试。")}</strong>` : chosen ? `<strong>${esc(chosen.device.name)} · ${esc(chosen.status.label)}</strong><p>${esc(chosen.status.reason)}</p>`
      : !items.length ? `<strong>${t("当前项目尚未绑定设备")}</strong><p>${t("请先到设备管理创建当前项目的接入，并启动设备上的 CVSprout Connector。")}</p>`
      : `<strong>${online ? t("有 {0} 台已连接设备可供选择", [online]) : t("当前项目没有可部署的在线设备")}</strong><ul>${rows.map(({ device, status }) => `<li>${esc(device.name)}${t("：")}${esc(status.label)}${status.selectable ? "" : ` · ${esc(status.reason)}`}</li>`).join("")}</ul>`;
    modal.querySelector("#modal-submit").disabled = loading || failed || !chosen?.status.selectable;
    node.querySelector("#edge-project-device-refresh").disabled = loading;
  }
  async function refresh(force = false) {
    if (!alive()) return;
    if (request) { await request; if (force && alive()) return refresh(); return; }
    const requestId = ++serial;
    loading = true; render();
    request = (async () => {
      try {
        const result = await api(`/api/edge/devices?project_id=${Number(project.id)}`);
        if (!alive() || requestId !== serial) return;
        if (!Array.isArray(result.devices)) throw new Error(t("服务器未返回有效的设备列表。"));
        items = result.devices.filter(device => Number(device.project_id) === Number(project.id));
        receivedAt = Date.now(); failed = false; node.querySelector("#edge-project-device-feedback").hidden = true;
      } catch (error) {
        if (!alive() || requestId !== serial) return;
        failed = true;
        const feedback = node.querySelector("#edge-project-device-feedback"); feedback.textContent = t("设备状态读取失败：{0}。恢复连接并刷新后才能继续。", [error.message]); feedback.hidden = false;
      } finally { if (alive() && requestId === serial) { loading = false; render(); } }
    })();
    try { await request; } finally { request = null; }
  }
}

export async function renderDevicesPage() {
  if (!isAdmin()) { location.hash = "#/"; return; }
  state.currentSection = "devices"; state.project = null; state.asset = null; state.annotations = [];
  state.selectedAnnotation = null; state.lockOwned = false;
  devices = []; listReceivedAt = 0; listReadFailed = false; requestSerial++;
  app.innerHTML = `<div class="app-shell">${topbar("devices")}<main class="dashboard edge-devices-page"><div class="dashboard-inner">
    <div class="page-head"><div><h1>${t("边缘设备")}</h1><p class="page-subtitle">${t("让 reCamera Pro 采集的图片进入项目，把训练后的模型部署回设备。")}</p></div><div class="edge-actions"><button class="btn ghost" id="refresh-edge-devices">${t("刷新")}</button><button class="btn primary" id="add-edge-device">${t("连接设备")}</button></div></div>
    <ol class="edge-workflow" aria-label="${ta("设备与训练工作流")}"><li><span>1</span><strong>${t("连接与采集")}</strong><small>${t("安装联动应用，图片自动导入")}</small></li><li><span>2</span><strong>${t("标注与训练")}</strong><small>${t("沿用项目的审核和数据版本")}</small></li><li><span>3</span><strong>${t("部署回设备")}</strong><small>${t("在项目训练与部署中选择模型与设备")}</small></li></ol>
    <p class="edge-page-note">${t("安装一个 CVSprout 联动应用即可采集原图并传输系统模型；推理由设备内置系统运行。图片导入后由管理员创建新的任务块，已领取的任务块保持原样。")}</p>
    <p class="edge-refresh-note">${t("页面可见时每 5 秒更新连接和采集状态。")}<span id="edge-refresh-time">${t("正在读取状态…")}</span></p><div id="edge-page-feedback" class="inline-error" role="status" hidden></div><div id="edge-device-results"><p class="hint">${t("正在读取设备…")}</p></div>
    <details class="edge-help"><summary>${t("首次连接前需要准备什么？")}</summary><p>${t("使用支持 Python SDK 与 App Center 的 reCamera Pro 固件。让设备能访问 CVSprout 的局域网地址或 HTTPS 域名；使用启动脚本时，在")} <code>.env</code> ${t("设置")} <code>MDP_BIND_ADDRESS=0.0.0.0</code> ${t("并重新启动。连接设备后，将服务地址和一次性显示的设备令牌填入采集应用配置。")}</p><p>${t("系统模型任务由联动应用主动领取，设备离线时保留等待。请在设备端完成系统模型管理配置；旧版独立推理应用的 SSH 部署记录仍可查看。")}</p></details>
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
      if (!Array.isArray(result.devices)) throw new Error(t("服务器未返回有效的设备列表。"));
      devices = result.devices; listReceivedAt = Date.now(); listReadFailed = false; listCompletedSerial = serial;
      const feedback = document.querySelector("#edge-page-feedback"); feedback.hidden = true;
      const updated = document.querySelector("#edge-refresh-time"); if (updated) updated.textContent = t("最近更新：{0}", [new Date(listReceivedAt).toLocaleTimeString(getLocale())]);
      renderDeviceList(node); refreshTokenStatus();
    } catch (error) {
      if (error.status === 401 || !current()) return;
      listReadFailed = true;
      const feedback = document.querySelector("#edge-page-feedback");
      feedback.textContent = t("设备状态更新失败：{0}。已保留上次数据，连接状态暂无法确认。", [error.message]); feedback.hidden = false;
      refreshConnectionDisplay();
      if (!devices.length && !background) node.innerHTML = `<p class="hint">${t("请点击上方“刷新”重试。")}</p>`;
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
    <dl class="edge-device-stats"><div><dt>${t("收到的采集记录")}</dt>${field("captures", "dd")}</div><div><dt>${t("关联的项目图片")}</dt>${field("assets", "dd")}</div><div>${field("pendingLabel", "dt")}${field("pending", "dd")}</div></dl>
    <div class="edge-device-diagnosis ${values.diagnosisKind}">${field("diagnosisTitle", "strong")}${field("diagnosisText", "p")}</div>
    <div class="edge-device-detail">${field("contact")}${field("heartbeat")}${field("uploaded")}${field("version")}</div>
    <details class="edge-diagnostic-details"${values.error ? "" : " hidden"}><summary>${t("查看最近上报的错误")}</summary>${field("error", "code")}</details>
    <div class="edge-card-actions"><button class="btn ghost" data-edge-captures="${esc(device.id)}">${t("采集记录")}</button><button class="btn ghost" data-edge-settings="${esc(device.id)}"${editHidden}>${t("采集设置")}</button><a class="btn primary link-btn" href="#/projects/${Number(device.project_id)}/train" data-edge-model="${esc(device.id)}"${editHidden}>${t("前往项目部署")}</a><details class="edge-more"><summary>${t("更多")}</summary><div><button data-edge-model-history="${esc(device.id)}">${t("系统模型状态与记录")}</button><button data-edge-legacy="${esc(device.id)}" data-edge-edit-control${editHidden}>${t("旧版应用部署记录")}</button><button data-edge-rotate="${esc(device.id)}" data-edge-edit-control${editHidden}>${t("重置设备令牌")}</button><button class="danger" data-edge-revoke="${esc(device.id)}" data-edge-edit-control${editHidden}>${t("撤销接入")}</button></div></details></div></article>`;
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
      : `<div class="edge-empty"><strong>${t("连接你的第一台 reCamera Pro")}</strong><p>${t("选择图片要进入的项目，获取专属令牌，即可开始自动采集。")}</p><button class="btn primary" id="empty-connect-device">${t("连接设备")}</button></div>`;
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
  bind("data-edge-model-history", device => openDeviceDeployment(device, { historyOnly: true })); bind("data-edge-legacy", openLegacyDeviceDeployment); bind("data-edge-rotate", openRotateToken); bind("data-edge-revoke", openRevokeDevice);
}

function refreshTokenStatus() {
  if (!tokenSetup?.node.isConnected) return;
  const device = (listCompletedSerial >= tokenSetup.minimumSerial ? devices.find(item => item.id === tokenSetup.device.id) : null) || tokenSetup.device;
  const status = deviceConnection(device, Date.now(), connectionOptions()), diagnosis = deviceDiagnosis(device, status);
  const node = tokenSetup.node;
  node.className = `edge-setup-connection ${status.kind}`;
  const title = node.querySelector("strong"), text = node.querySelector("p");
  const label = status.kind === "online" ? t("已确认设备连接成功") : status.kind === "waiting" ? t("等待设备首次连接…") : status.label;
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
    if (!device && !projects.length) { notify(t("请先创建一个接收设备图片的项目。"), "warning"); return; }
    const suggestedURL = remoteServerURL(device?.server_url || location.origin);
    openModal({ title: device ? t("采集设置") : t("连接 reCamera Pro"), subtitle: device ? t("配置会在设备下一次读取时生效。") : t("每台设备绑定一个项目，设备令牌只允许向这个项目上传图片。"), body: `<div class="edge-settings-form"><div class="field"><label for="edge-device-name">${t("设备名称")}</label><input id="edge-device-name" maxlength="120" value="${esc(device?.name || "")}" placeholder="${ta("例如：车间入口相机")}" autofocus/></div>
      ${device ? `<p class="hint">${t("接收项目：{0}。更换项目请创建新的设备接入。", [esc(device.project_name || t("项目 {0}", [device.project_id]))])}</p>` : `<div class="field"><label for="edge-device-project">${t("接收图片的项目")}</label><select id="edge-device-project">${projects.map(project => `<option value="${Number(project.id)}">${esc(project.name)}</option>`).join("")}</select></div>`}
      <div class="field"><label for="edge-server-url">${t("设备可访问的 CVSprout 地址")}</label><input id="edge-server-url" type="url" value="${esc(suggestedURL)}" placeholder="http://192.168.1.20:8000" autocomplete="url"/><p class="hint">${t("填写服务器的局域网地址或 HTTPS 域名。设备上的 localhost / 127.0.0.1 指向设备自身，不能用于连接你的电脑。")}</p></div>
      <div class="field"><label for="edge-capture-interval">${t("采集间隔（秒）")}</label><input id="edge-capture-interval" type="number" min="1" max="86400" step="1" value="${Number(device?.capture_interval_seconds || 10)}"/></div><label class="check-line"><input id="edge-capture-enabled" type="checkbox" ${device?.capture_enabled === false ? "" : "checked"}/>${t("启用新图片采集")}</label><p class="hint">${t("暂停采集后，已进入设备队列的图片仍会继续上传。")}</p>
      <details class="edge-advanced"><summary>${t("离线队列上限")}</summary><div class="form-grid"><div class="field"><label for="edge-queue-count">${t("最多暂存图片数")}</label><input id="edge-queue-count" type="number" min="1" max="10000" step="1" value="${Number(device?.max_pending_images || 500)}"/></div><div class="field"><label for="edge-queue-mib">${t("最多暂存空间（MiB）")}</label><input id="edge-queue-mib" type="number" min="1" max="8192" step="1" value="${Math.ceil(Number(device?.max_pending_bytes || 268435456) / 1048576)}"/></div></div><p class="hint">${t("实际容量取这里与设备应用本地设置中较小的值。离线时先保留队列中的图片；达到上限后暂停新增，连接恢复后自动补传。")}</p></details></div>`, submitText: device ? t("保存设置") : t("创建并获取令牌"), onSubmit: async () => {
        const name = document.querySelector("#edge-device-name").value.trim();
        const server_url = remoteServerURL(document.querySelector("#edge-server-url").value.trim());
        const capture_interval_seconds = Number(document.querySelector("#edge-capture-interval").value);
        const max_pending_images = Number(document.querySelector("#edge-queue-count").value);
        const mib = Number(document.querySelector("#edge-queue-mib").value);
        if (!name) throw new Error(t("请输入设备名称。"));
        if (!server_url) throw new Error(t("请填写设备可访问的 HTTP / HTTPS 地址，不要使用本机回环或监听地址。"));
        if (!Number.isInteger(capture_interval_seconds) || capture_interval_seconds < 1 || capture_interval_seconds > 86400) throw new Error(t("采集间隔应为 1–86400 秒之间的整数。"));
        if (!Number.isInteger(max_pending_images) || max_pending_images < 1 || max_pending_images > 10000 || !Number.isInteger(mib) || mib < 1 || mib > 8192) throw new Error(t("队列上限应为 1–10000 张、1–8192 MiB。"));
        const payload = { name, server_url, capture_interval_seconds, capture_enabled: document.querySelector("#edge-capture-enabled").checked, max_pending_images, max_pending_bytes: mib * 1048576 };
        if (!device) payload.project_id = Number(document.querySelector("#edge-device-project").value);
        const modal = document.querySelector(".modal");
        const result = await api(device ? pathFor(device) : "/api/edge/devices", jsonOptions(device ? "PATCH" : "POST", payload));
        if (modal?.isConnected) { closeModal(); if (!device) showDeviceToken(result, result.device_token); }
        notify(device ? t("采集设置已保存。") : t("设备接入已创建，请完成应用配置。"));
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
  openModal({ title: t("配置 CVSprout 联动应用"), subtitle: t("设备令牌只显示这一次。请现在复制到 reCamera Pro 的采集应用配置中。"), body: `<div class="edge-token-setup"><p>${t("在设备 App Center 安装")} <strong>CVSprout Connector</strong>${t("，然后填写：")}</p><div class="field"><label for="edge-copy-url">${t("server_url · CVSprout 地址")}</label><input id="edge-copy-url" readonly value="${esc(device.server_url || "")}"/></div><div class="field"><label for="edge-copy-token">${t("device_token · 设备令牌")}</label><div class="edge-token-control"><input id="edge-copy-token" type="password" readonly autocomplete="off" spellcheck="false"/><button class="btn ghost" type="button" id="edge-copy-token-button">${t("复制令牌")}</button></div><label class="check-line"><input id="edge-show-token" type="checkbox"/>${t("显示令牌")}</label></div><p class="hint">${t("请完整复制以 cvsprout_device_ 开头的设备专用令牌，不要填写网页登录密码或管理员 API Key。关闭后无法再次查看；丢失时可在设备卡片重置。")}</p><div id="edge-capture-download"></div><div id="edge-setup-connection" class="edge-setup-connection waiting" role="status"><strong>${t("等待设备首次连接…")}</strong><p>${t("保存参数并启动设备上的采集应用后，这里会自动确认连接。请保留此窗口，令牌仍可复制。")}</p></div></div>`, submitText: t("我已保存配置"), onReady: () => {
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
        if (modal.isConnected) notify(copied ? t("设备令牌已复制。") : t("浏览器未能复制令牌。请勾选“显示令牌”，选中完整内容后手动复制。"), copied ? "success" : "warning");
      };
      void api(`${pathFor(device)}/deployment-options`).then(result => {
        if (modal.isConnected && result.capture_app?.download_url) document.querySelector("#edge-capture-download").innerHTML = downloadLink(result.capture_app.download_url, t("下载联动应用包"));
      }).catch(() => {});
    }, onClose: () => { tokenSetup = null; const input = document.querySelector("#edge-copy-token"); if (input) input.value = ""; token = ""; }, onSubmit: async () => closeModal() });
}

function openRotateToken(device) {
  const current = pageContext();
  openModal({ title: t("重置设备令牌"), subtitle: device.name, body: `<p>${t("重置后旧令牌立即失效。请将新令牌更新到设备的采集应用配置，设备才能继续上传。项目中已导入的图片会保留。")}</p>`, submitText: t("重置并显示新令牌"), onSubmit: async () => {
    const modal = document.querySelector(".modal"), result = await api(`${pathFor(device)}/rotate-token`, jsonOptions("POST", {}));
    if (modal?.isConnected) { closeModal(); showDeviceToken(result.device || device, result.device_token); }
    if (current()) await loadDevices();
  }});
}

function openRevokeDevice(device) {
  const current = pageContext();
  openModal({ title: t("撤销设备接入"), subtitle: device.name, body: `<p>${t("设备将无法继续上传或读取配置；已导入的图片和采集记录保留。需要恢复时，请创建新的接入。")}</p><div class="field"><label for="edge-revoke-name">${t("输入设备名称“{0}”确认", [esc(device.name)])}</label><input id="edge-revoke-name" autocomplete="off" autofocus/></div>`, submitText: t("撤销接入"), onReady: () => {
    const input = document.querySelector("#edge-revoke-name"), button = document.querySelector("#modal-submit"); input.value = ""; button.disabled = true;
    input.oninput = () => { button.disabled = input.value !== device.name; };
  }, onSubmit: async () => {
    if (document.querySelector("#edge-revoke-name").value !== device.name) throw new Error(t("请输入完整设备名称确认。"));
    const modal = document.querySelector(".modal"); await api(pathFor(device), { method: "DELETE" });
    if (modal?.isConnected) closeModal(); notify(t("设备接入已撤销。")); if (current()) await loadDevices();
  }});
}

function captureRecordMarkup(item, device) {
  const assetId = Number(item.asset_id), available = Number.isSafeInteger(assetId) && assetId > 0;
  const filename = item.filename || t("图片 {0}", [assetId]);
  const thumbnail = available ? `<button type="button" class="edge-record-thumb" data-edge-preview="${assetId}" data-edge-capture="${esc(item.capture_id)}" aria-label="${ta("预览 {0}", [esc(filename)])}"><span data-edge-image-status>${t("正在加载…")}</span><img data-edge-image src="/api/assets/${assetId}/thumbnail?size=320" alt="" loading="lazy" decoding="async" width="320" height="240"/><span class="edge-record-thumb-action">${t("查看大图")}</span></button>`
    : `<div class="edge-record-thumb unavailable"><span>${t("原图已删除")}</span></div>`;
  const title = available ? `<a href="#/projects/${Number(device.project_id)}/annotate?asset=${assetId}">${esc(filename)}</a>`
    : `<span>${t("原图已删除{0}", [item.filename ? ` · ${esc(item.filename)}` : ""])}</span>`;
  return `<article>${thumbnail}<div class="edge-record-info"><div class="edge-record-heading">${title}<span>${t("采集：{0}", [esc(dateText(item.captured_at))])}</span></div><p>${available ? item.duplicate ? t("已有相同图片 · 已关联") : t("图片已导入") : t("采集记录保留")}${item.model_release ? t(" · 模型 {0}", [esc(item.model_release)]) : ""}</p><small>${t("收到时间：{0} · 采集 ID：{1}", [esc(dateText(item.received_at)), esc(item.capture_id)])}</small></div></article>`;
}

function bindCaptureImages(root) {
  for (const image of root.querySelectorAll("[data-edge-image]")) {
    const status = image.parentElement.querySelector("[data-edge-image-status]");
    const loaded = () => { image.classList.add("loaded"); image.hidden = false; status.hidden = true; };
    const failed = () => { image.hidden = true; status.hidden = false; status.textContent = t("图片暂不可用"); };
    image.addEventListener("load", loaded, { once: true });
    image.addEventListener("error", failed, { once: true });
    if (image.complete) image.naturalWidth ? loaded() : failed();
  }
}

function showCapturePreview(node, item, device, trigger) {
  const preview = node.querySelector("#edge-record-preview"), list = node.querySelector("#edge-record-page");
  const assetId = Number(item.asset_id), filename = item.filename || t("图片 {0}", [assetId]);
  const body = node.closest(".modal-body"), scrollTop = body.scrollTop;
  list.hidden = true; preview.hidden = false;
  preview.innerHTML = `<div class="edge-record-preview-heading"><button type="button" class="btn ghost" data-edge-preview-back>${t("返回记录")}</button><strong>${esc(filename)}</strong><a class="btn ghost link-btn" href="#/projects/${Number(device.project_id)}/annotate?asset=${assetId}">${t("前往标注")}</a></div><div class="edge-record-preview-image"><span data-edge-image-status role="status">${t("正在加载原图…")}</span><img data-edge-image src="/api/assets/${assetId}/image" alt="${esc(filename)}" decoding="async"/></div><p class="hint">${t("采集：{0} · 收到：{1}", [esc(dateText(item.captured_at)), esc(dateText(item.received_at))])}</p>`;
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
  openModal({ title: t("{0} · 采集记录", [device.name]), subtitle: t("点击缩略图查看大图；同一内容的图片只保留一份，每一次采集仍可追溯。"), wide: true, body: `<div id="edge-capture-records" aria-live="polite">${t("正在读取…")}</div>`, submitText: t("关闭"), onSubmit: async () => closeModal(), onReady: () => { void load(); } });
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
      node.innerHTML = `<div id="edge-record-preview" hidden></div><div id="edge-record-page"><div class="edge-record-list">${(result.items || []).map(item => captureRecordMarkup(item, device)).join("") || `<p class="hint">${t("尚未收到图片。请确认采集应用正在运行，地址和令牌已保存。")}</p>`}</div><div class="edge-record-pagination"><span>${t("共 {0} 条记录", [numberText(result.total)])}</span><button class="btn ghost" id="edge-record-prev" ${offset === 0 ? "disabled" : ""}>${t("上一页")}</button><button class="btn ghost" id="edge-record-next" ${offset + 25 >= result.total ? "disabled" : ""}>${t("下一页")}</button></div></div>`;
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
        node.innerHTML = `<p class="inline-error">${esc(error.message)}</p><button class="btn ghost" id="edge-record-retry">${t("重试")}</button>`;
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
  return items.length ? `<ol class="edge-deployment-history">${items.slice(0, 6).map(item => `<li><strong>${esc(stageNames[item.status] || stageNames[item.stage] || item.status)}</strong><span>${esc(dateText(item.created_at))}</span>${item.error ? `<p class="feedback-error">${esc(item.error)}</p>` : item.stage ? `<small>${esc(finalStageNames[item.stage] || item.stage)}</small>` : ""}</li>`).join("")}</ol>` : `<p class="hint">${t("还没有从 CVSprout 发起的部署记录。")}</p>`;
}

const systemTerminalStates = new Set(["succeeded", "failed", "unknown", "cancelled"]);
const systemStageNames = { queued: t("等待设备领取"), downloading: t("设备正在下载模型"), uploading: t("正在写入系统模型库"), uploaded: t("已加入系统模型库"), activating: t("正在切换系统模型"), activated: t("已请求启用，等待运行验证"), verified: t("已验证系统推理运行"), failed: t("部署失败"), unknown: t("结果待核实"), cancelled: t("已取消") };
const systemErrorNames = { system_api_unavailable: t("设备本机模型接口不可用，请检查设备系统服务"), system_api_unauthorized: t("设备本机模型接口未授权，请检查固件的本机应用访问权限"), system_api_unsupported: t("设备固件尚未提供兼容的系统模型接口"), system_model_incompatible: t("模型与设备系统后处理不兼容，请检查转换用途和算法"), system_model_busy: t("系统模型服务正忙，请检查设备任务"), artifact_download_failed: t("设备下载模型失败，请检查连接"), artifact_integrity_failed: t("模型文件校验失败，请检查发布文件和设备存储"), insufficient_storage: t("设备存储空间不足，请清理后重试"), upload_failed: t("模型导入失败，请在设备模型库核实结果"), activation_failed: t("请求启用失败，请在设备核实当前模型"), verification_failed: t("尚未验证模型正常运行，请检查实际 FPS 与设备日志"), rollback_failed: t("恢复原模型失败，请在设备核实当前运行模型"), lease_expired: t("设备任务联系超时，需要核实实际执行结果"), credential_changed: t("设备令牌已更新，需要使用新令牌恢复连接并核实任务"), deployment_interrupted: t("设备部署被中断，需要核实实际执行结果"), admin_resolved_unknown: t("管理员已核实并解除占用，设备模型未被回滚") };
function systemErrorText(code) { return systemErrorNames[code] ? `${systemErrorNames[code]}${t("（")}${code}${t("）")}` : String(code || ""); }

export function systemDeploymentLabel(job) {
  if (job.status === "unknown" || job.lease_expired) return t("结果待核实 · 请检查设备");
  if (job.status === "failed") return t("部署失败");
  if (job.status === "cancelled") return t("已取消");
  if (job.status === "succeeded") return job.activate && job.stage === "verified" ? t("已验证系统推理运行") : !job.activate && job.stage === "uploaded" ? t("已加入系统模型库") : t("已完成 · 运行状态待核实");
  return systemStageNames[job.stage] || (job.status === "queued" ? t("等待设备领取") : t("设备正在处理"));
}

export function systemModelStatusMarkup(data, now = Date.now(), observedAt = now) {
  const report = data.system_status, current = data.current, capabilities = data.capabilities;
  const fresh = report?.fresh === true && Number(report.age_seconds || 0) + Math.max(0, now - observedAt) / 1000 <= 90;
  const title = !report ? t("等待联动应用上报系统能力") : !fresh ? t("系统状态已过期，等待设备联系") : report.error_code ? t("系统模型服务暂不可用") : current?.bExternalHold ? t("系统推理正在等待 NPU") : current?.sStatus === "running" ? t("系统推理运行中") : current?.sStatus === "error" ? t("系统推理出现错误") : t("系统推理未运行");
  const kind = fresh && !report?.error_code && current?.sStatus === "running" && !current?.bExternalHold ? "success" : "warning";
  return `<div class="edge-device-diagnosis ${kind}"><strong>${esc(title)}</strong><p>${!capabilities?.model_api ? t("请安装或升级 CVSprout Connector，并确认设备固件提供可授权访问的系统模型管理接口。") : !fresh ? t("以下模型和参数为上次上报，不能据此确认设备正在运行；部署任务会等待设备恢复连接。") : current?.bExternalHold ? t("NPU 正被其他应用或任务使用，请在设备应用中心检查占用；采集上传可继续运行。") : t("模型上传与实际运行分别确认；仅加入模型库不会切换当前推理模型。")}</p></div>
    <dl class="edge-system-status"><div><dt>${fresh ? t("系统模型") : t("上次上报的模型")}</dt><dd>${esc(current?.sModel || t("未选择"))}</dd></div><div><dt>${t("目标 / 实际 FPS")}</dt><dd>${current?.iFPS == null ? "—" : numberText(current.iFPS)} / ${fresh && !report?.error_code && current?.sStatus === "running" && current?.iActualFPS != null ? numberText(current.iActualFPS) : "—"}</dd></div><div><dt>${t("NPU 状态")}</dt><dd>${!fresh || report?.error_code ? t("待确认") : current?.bExternalHold ? t("其他应用占用") : t("未上报外部占用")}</dd></div><div><dt>${t("系统状态上报")}</dt><dd>${esc(dateText(report?.reported_at))}</dd></div></dl>
    <p class="hint">${t("设备支持的系统算法：{0}{1}", [esc(capabilities?.algorithms?.join(t("、")) || t("尚未上报")), report?.error_code ? t(" · 最近上报：{0}", [esc(systemErrorText(report.error_code))]) : ""])}</p>`;
}

export function systemDeploymentHistoryMarkup(items = []) {
  return items.length ? `<ol class="edge-deployment-history">${items.slice(0, 8).map(job => `<li><strong>${esc(systemDeploymentLabel(job))}</strong><span>${esc(dateText(job.created_at))}</span><p>${esc(job.filename || t("模型"))} · ${job.activate ? t("上传并启用") : t("仅加入模型库")}${job.status === "queued" ? t(" · 设备离线时保留等待，恢复连接后自动领取") : ""}${job.error_code ? ` · ${esc(systemErrorText(job.error_code))}` : ""}</p>${job.status === "queued" ? `<button type="button" class="btn ghost" data-edge-cancel-model="${esc(job.id)}">${t("取消等待任务")}</button>` : job.lease_expired && ["running", "unknown"].includes(job.status) ? `<button type="button" class="btn ghost" data-edge-resolve-model="${esc(job.id)}">${t("核实并解除占用")}</button>` : ""}</li>`).join("")}</ol>` : `<p class="hint">${t("暂无系统模型部署记录。")}</p>`;
}

export function systemDeploymentProgressMarkup(job, { preparing = false, stale = false, message = "" } = {}) {
  if (preparing) return `<div class="edge-model-progress"><h3>${t("正在准备模型")}</h3><progress aria-label="${ta("正在校验并准备模型")}"></progress><p role="status">${t("正在校验模型文件，完成后自动发送到设备。")}</p></div>`;
  if (!job) return message ? `<div class="edge-model-progress warning"><h3>${t("部署尚未完成")}</h3><p role="status">${esc(message)}</p></div>` : "";
  const steps = [t("等待设备领取"), t("下载模型"), t("导入模型库"), ...(job.activate ? [t("启用模型"), t("验证运行")] : [])];
  const completed = job.status === "succeeded" && (job.activate ? job.stage === "verified" : job.stage === "uploaded");
  const uncertain = job.status === "unknown" || job.lease_expired || (job.status === "succeeded" && !completed);
  const stopped = uncertain || ["failed", "cancelled"].includes(job.status);
  const positions = { queued: 0, downloading: 1, uploading: 2, uploaded: job.activate ? 3 : 2, activating: 3, activated: 4, verified: 4 };
  const position = completed ? steps.length : stopped ? null : positions[job.stage] ?? null;
  const known = position !== null && position < steps.length;
  const count = completed ? steps.length : known ? position : null;
  const note = message || (stale ? t("进度更新中断，以下保留上次结果；恢复连接后自动更新。") : uncertain ? t("请核实设备上的实际结果后再操作。") : job.status === "failed" ? systemErrorText(job.error_code) || t("请检查设备后重试部署。") : job.status === "cancelled" ? t("任务在设备领取前已取消。") : completed ? job.activate ? t("设备已确认模型运行正常。") : t("模型已加入设备模型库，当前推理模型未切换。") : job.status === "queued" ? t("设备连接后会自动领取任务，可关闭窗口后从设备记录继续查看。") : t("根据设备实际回报自动更新。可关闭窗口，部署会继续。"));
  return `<div class="edge-model-progress ${completed ? "success" : stopped || stale ? "warning" : ""}"><div class="edge-model-progress-heading"><h3>${esc(systemDeploymentLabel(job))}</h3>${count !== null ? `<span>${t("已完成 {0} / {1} 个阶段", [count, steps.length])}</span>` : ""}</div><p class="edge-model-progress-file">${esc(job.filename || t("当前模型"))} · ${job.activate ? t("上传并启用") : t("仅加入模型库")}</p>${count !== null ? `<progress max="${steps.length}" value="${count}" aria-label="${ta("模型部署阶段进度")}" aria-valuetext="${ta("{0}，已完成 {1} / {2} 个阶段", [esc(systemDeploymentLabel(job)), count, steps.length])}"></progress>` : ""}<ol class="edge-model-stages">${steps.map((label, index) => `<li class="${count !== null && index < count ? "done" : known && index === position && !stale ? "current" : ""}"${known && index === position ? ' aria-current="step"' : ""}><span aria-hidden="true">${count !== null && index < count ? "✓" : index + 1}</span>${esc(label)}</li>`).join("")}</ol><p role="status">${esc(note)}</p>${job.updated_at ? `<small>${t("最近更新：{0}", [esc(dateText(job.updated_at))])}</small>` : ""}</div>`;
}

function systemModelNameError(value) {
  const name = value.trim();
  if (!name) return "";
  if (name.length > 64) return t("模型名称最多 64 个字符。");
  return /^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(name) ? "" : t("请以英文字母或数字开头，只使用英文字母、数字、短横线和下划线；无需填写 .rknn。");
}

function systemModelFilename(name, releaseId) {
  return name && Number.isSafeInteger(Number(releaseId)) && Number(releaseId) > 0 ? `${name}-r${Number(releaseId)}.rknn` : "";
}

async function openDeviceDeployment(device, { current = pageContext(), project = null, run = null, conversion = null, historyOnly = false } = {}) {
  const lockedConversionId = conversion ? Number(conversion.id) : null;
  let data = null, node, modal, monitor = null, request = null, requestSerial = 0, submitting = false, preparing = false, receivedAt = 0, readFailed = true, connectedDevice = null;
  let statusSignature = "", jobsSignature = "", progressSignature = "", submittedJob = null, preparationMessage = "", initialModelName = "";
  const alive = () => current() && Boolean(node?.isConnected) && Boolean(modal?.isConnected);
  const connectionBlock = () => {
    if (readFailed || !receivedAt || Date.now() - receivedAt > DEVICE_REFRESH_STALE_MS) return t("设备状态尚未确认或已过期，请刷新后再创建部署。");
    if (!project) return "";
    if (!connectedDevice) return t("该设备已不属于当前项目，请重新选择设备。");
    const status = projectDeviceAvailability(connectedDevice, project.id, { observedAt: receivedAt });
    return status.selectable ? "" : `${status.label}${t("：")}${status.reason}`;
  };
  const source = () => (data?.conversions || []).find(item => Number(item.id) === lockedConversionId && Number(item.training_run_id) === Number(run?.id));
  const releaseCompatible = release => Number(release.conversion_id) === lockedConversionId && Number(release.project_id) === Number(project?.id)
    && release.status === "ready" && data.capabilities?.model_api === true && data.capabilities.algorithms?.includes(release.contract?.algorithm || release.model_info?.algorithm);
  const readyRelease = () => (data?.releases || []).find(releaseCompatible);
  const pending = () => (data?.deployments || []).some(job => !systemTerminalStates.has(job.status) || job.status === "unknown");
  openModal({ title: `${device.name} · ${historyOnly ? t("系统模型状态与记录") : t("部署模型")}`, subtitle: project ? t("{0} · {1} · 转换 #{2}", [project.name || t("当前项目"), run.name || t("训练 {0}", [run.id]), lockedConversionId]) : t("查看设备上的模型与部署进度。"), wide: true,
    body: `<div id="edge-system-content"><p class="hint">${t("正在检查设备与模型…")}</p></div>`, submitText: t("开始部署"),
    onReady: () => { const button = document.querySelector("#modal-submit"); button.disabled = true; button.hidden = historyOnly; },
    onClose: () => { clearInterval(monitor); requestSerial++; }, onSubmit: async () => {
      if (!alive() || historyOnly) return;
      if (!data || submitting) throw new Error(t("请等待设备检查完成。"));
      const nameInput = node.querySelector("#edge-system-model-name"), modelNameSnapshot = nameInput.value, model_name = modelNameSnapshot.trim();
      const nameError = systemModelNameError(modelNameSnapshot);
      if (nameError) { syncSubmit(); setTimeout(syncSubmit, 0); nameInput.focus(); throw new Error(nameError); }
      const activate = node.querySelector("#edge-system-mode").value === "activate";
      const fps = activate ? Number(node.querySelector("#edge-system-fps").value) : 30;
      const previousJobs = new Set((data.deployments || []).map(job => Number(job.id)));
      let attemptedRelease = null, attemptedFilename = "";
      const validate = () => {
        if (nameInput.value !== modelNameSnapshot) throw new Error(t("模型名称已变化，请重新确认后部署。"));
        if (systemModelNameError(nameInput.value)) throw new Error(systemModelNameError(nameInput.value));
        if (connectionBlock() || !data.can_deploy) throw new Error(connectionBlock() || data.blocked_reason || t("设备暂不可部署。"));
        if (pending()) throw new Error(t("设备仍有等待、执行或结果待核实的任务，请先处理后再部署。"));
        if (!Number.isInteger(fps) || fps < 1 || fps > 60) throw new Error(t("目标 FPS 应为 1–60 的整数。"));
        if (activate && !node.querySelector("#edge-system-confirm").checked) throw new Error(t("请确认允许替换当前系统推理模型。"));
        if ((node.querySelector("#edge-system-mode").value === "activate") !== activate || (activate && Number(node.querySelector("#edge-system-fps").value) !== fps)) throw new Error(t("部署选项已变化，请重新确认后提交。"));
      };
      submitting = true; preparationMessage = ""; syncSubmit(); renderProgress();
      try {
        await refresh(true);
        if (!alive()) return;
        validate();
        let release = readyRelease();
        if (!release) {
          if (!source()?.compatible) throw new Error(source()?.reason || t("当前转换已不可部署，请返回项目检查转换记录。"));
          preparing = true; renderProgress();
          const published = await api("/api/edge/model-releases", jsonOptions("POST", { conversion_id: lockedConversionId }));
          if (!alive()) return;
          if (!releaseCompatible(published) || !Number.isSafeInteger(Number(published.id)) || Number(published.id) < 1) throw new Error(t("模型准备结果与当前转换不一致，请刷新后重试。"));
          await refresh(true);
          if (!alive()) return;
          validate();
          release = (data.releases || []).find(item => Number(item.id) === Number(published.id) && releaseCompatible(item));
          if (!release) throw new Error(t("模型已准备，但状态尚未确认，请稍后重试。"));
        }
        attemptedRelease = Number(release.id);
        attemptedFilename = systemModelFilename(model_name || release.default_model_name || initialModelName, attemptedRelease);
        const job = await api(`${pathFor(device)}/model-deployments`, jsonOptions("POST", { release_id: attemptedRelease, activate, fps, model_name }));
        if (!alive()) return;
        preparing = false;
        // Keep the accepted task visible even if a subsequent read is delayed or fails.
        submittedJob = { ...job, release_id: Number(release.id), filename: job.filename || attemptedFilename || release.filename, activate, fps };
        data.deployments = [submittedJob, ...(data.deployments || []).filter(item => Number(item.id) !== Number(job.id))];
        node.querySelector("#edge-system-confirm").checked = false;
        update();
        node.querySelector("#edge-system-progress").scrollIntoView?.({ block: "nearest", behavior: "instant" });
        notify(t("部署任务已创建，可在此查看进度。"));
        await refresh(true);
      } catch (error) {
        if (alive()) {
          preparationMessage = error.message;
          // Reconcile a request whose response was lost before allowing another attempt.
          await refresh(true);
          if (alive() && !readFailed && attemptedRelease && attemptedFilename && (data.deployments || []).some(job => !previousJobs.has(Number(job.id)) && Number(job.release_id) === attemptedRelease && job.filename === attemptedFilename && job.activate === activate && (!activate || Number(job.fps) === fps))) {
            preparationMessage = "";
            node.querySelector("#edge-system-confirm").checked = false;
            notify(t("部署任务已确认创建，进度已恢复。"));
            return;
          }
        }
        throw error;
      } finally {
        preparing = false; submitting = false;
        if (alive()) { renderProgress(); syncSubmit(); setTimeout(syncSubmit, 0); }
      }
    }});
  node = document.querySelector("#edge-system-content"); modal = document.querySelector(".modal");
  modal.addEventListener("click", handlePackageDownload);
  await refresh();
  if (!alive()) return;
  monitor = setInterval(() => { if (!alive()) { clearInterval(monitor); return; } if (!document.hidden) { if (data) { renderStatus(); renderProgress(); syncSubmit(); } void refresh(); } }, DEVICE_REFRESH_MS);

  function mount() {
    initialModelName = source()?.default_model_name || readyRelease()?.default_model_name || "";
    node.innerHTML = `${project ? `<div class="edge-project-deployment-source"><strong>${esc(run.name || t("训练 {0}", [run.id]))}</strong><span>${t("转换 #{0} · {1}", [lockedConversionId, esc(device.name)])}</span><button type="button" class="btn ghost" id="edge-system-change-device">${t("更换设备")}</button></div>` : ""}<div id="edge-system-feedback" class="inline-error" role="status" hidden></div><section id="edge-system-progress" aria-label="${ta("模型部署进度")}" aria-live="polite" hidden></section>
      <section class="edge-deploy-step" id="edge-system-deploy-section"${historyOnly ? " hidden" : ""}>${historyOnly ? "" : `<div class="field edge-model-name-field"><label for="edge-system-model-name">${t("模型名称")}</label><input id="edge-system-model-name" type="text" value="${esc(initialModelName)}" placeholder="${ta("留空使用默认名称")}" autocomplete="off" autocapitalize="off" spellcheck="false" aria-describedby="edge-system-model-name-help edge-system-model-name-error edge-system-model-name-preview"/><p id="edge-system-model-name-help" class="hint">${t("最多 64 个英文字母、数字、短横线或下划线，以字母或数字开头。留空使用默认名称；系统自动追加 -r发布ID.rknn，避免不同模型版本互相覆盖。")}</p><p id="edge-system-model-name-error" class="edge-model-name-error" role="status" hidden></p><p id="edge-system-model-name-preview" class="edge-model-name-preview" role="status"></p></div>`}<div class="field"><label for="edge-system-mode">${t("部署方式")}</label><select id="edge-system-mode"><option value="upload">${t("仅加入模型库")}</option><option value="activate">${t("部署并启用")}</option></select></div><p id="edge-system-mode-note" class="hint"></p><label id="edge-system-confirm-label" class="check-line edge-uncertain-confirm" hidden><input type="checkbox" id="edge-system-confirm"/>${t("允许替换当前系统推理模型，切换时推理可能短暂中断")}</label><details id="edge-system-fps-field" class="edge-advanced" hidden><summary>${t("高级设置")}</summary><div class="field"><label for="edge-system-fps">${t("目标 FPS")}</label><input id="edge-system-fps" type="number" min="1" max="60" step="1" value="${Number.isInteger(data.current?.iFPS) && data.current.iFPS >= 1 && data.current.iFPS <= 60 ? data.current.iFPS : 30}"/></div></details><p id="edge-system-blocked" class="edge-deploy-notice" hidden></p></section>
      <details class="edge-advanced" id="edge-system-details"><summary>${t("设备状态与模型详情")}</summary><div id="edge-system-state"></div><p id="edge-system-release-detail" class="hint"></p><p id="edge-system-compatibility" class="hint"></p><div class="edge-capture-package">${downloadLink("/api/edge/apps/capture/download", t("下载 / 升级联动应用"))}</div></details>
      <details class="edge-advanced" id="edge-system-history"${historyOnly ? " open" : ""}><summary>${t("部署记录")}</summary><div id="edge-system-jobs"></div></details>
      <section id="edge-system-resolve" class="edge-deploy-notice" hidden><h3>${t("核实并解除占用")}</h3><p>${t("请先到设备核实模型状态，并确认原部署应用已经停止操作。此操作只把平台任务标记为失败并解除占用，不会回滚模型，也不会停止设备正在执行的操作。")}</p><div class="field"><label for="edge-system-resolve-name">${t("输入设备名称「{0}」", [esc(device.name)])}</label><input id="edge-system-resolve-name" autocomplete="off"/></div><label class="check-line"><input id="edge-system-resolve-ack" type="checkbox"/>${t("我已在设备核实状态，并确认原部署操作已停止")}</label><p id="edge-system-resolve-feedback" role="status"></p><div class="edge-build-row"><button type="button" class="btn ghost" id="edge-system-resolve-cancel">${t("返回")}</button><button type="button" class="btn primary" id="edge-system-resolve-submit" disabled>${t("标记失败并解除占用")}</button></div></section>
      <details class="edge-advanced"${project ? " hidden" : ""}><summary>${t("旧版推理应用部署")}</summary><button type="button" class="btn ghost" id="edge-system-legacy">${t("查看旧版应用部署与记录")}</button></details>`;
    const changeDevice = node.querySelector("#edge-system-change-device");
    if (changeDevice) changeDevice.onclick = () => { if (alive() && !submitting) void openProjectDeployment({ project, run, conversion }); };
    node.querySelector("#edge-system-legacy").onclick = () => openLegacyDeviceDeployment(device);
    const modelNameInput = node.querySelector("#edge-system-model-name");
    if (modelNameInput) modelNameInput.oninput = () => { node.querySelector("#edge-system-confirm").checked = false; syncSubmit(); };
    node.querySelector("#edge-system-mode").onchange = () => { node.querySelector("#edge-system-confirm").checked = false; syncSubmit(); };
    node.querySelector("#edge-system-confirm").onchange = syncSubmit;
    node.querySelector("#edge-system-fps").oninput = syncSubmit;
  }
  function renderStatus() {
    const markup = systemModelStatusMarkup(data, Date.now(), receivedAt);
    if (markup !== statusSignature) { node.querySelector("#edge-system-state").innerHTML = markup; statusSignature = markup; }
  }
  function renderProgress() {
    const progress = node.querySelector("#edge-system-progress");
    if (!progress) return;
    const jobs = data?.deployments || [];
    const job = jobs.find(item => !systemTerminalStates.has(item.status) || item.status === "unknown") || (preparationMessage ? null : jobs[0]);
    const markup = systemDeploymentProgressMarkup(job, { preparing, stale: Boolean(connectionBlock()), message: preparationMessage });
    progress.hidden = !markup;
    if (markup !== progressSignature) { progress.innerHTML = markup; progressSignature = markup; }
  }
  function update() {
    renderStatus(); renderProgress();
    const release = readyRelease();
    node.querySelector("#edge-system-release-detail").textContent = release ? t("发布版本 #{0} · {1} MiB · SHA-256 {2}", [Number(release.id), (Number(release.size_bytes || 0) / 1048576).toFixed(1), release.sha256 || "—"]) : historyOnly ? "" : t("点击部署后自动校验并准备模型，无需单独发布版本。");
    node.querySelector("#edge-system-compatibility").textContent = historyOnly ? "" : source()?.compatible ? t("当前转换与设备兼容。") : source()?.reason || t("当前转换不可用，请返回项目检查转换记录。");
    const jobs = JSON.stringify(data.deployments || []);
    if (jobs !== jobsSignature) { node.querySelector("#edge-system-jobs").innerHTML = systemDeploymentHistoryMarkup(data.deployments); jobsSignature = jobs; bindCancel(); }
    syncSubmit();
  }
  function syncSubmit() {
    if (!alive() || !node.querySelector("#edge-system-mode")) return;
    const modelNameInput = node.querySelector("#edge-system-model-name");
    const modelNameError = modelNameInput ? systemModelNameError(modelNameInput.value) : "";
    if (modelNameInput) {
      modelNameInput.setAttribute("aria-invalid", modelNameError ? "true" : "false");
      const error = node.querySelector("#edge-system-model-name-error"), preview = node.querySelector("#edge-system-model-name-preview");
      error.textContent = modelNameError; error.hidden = !modelNameError;
      const name = modelNameInput.value.trim() || initialModelName || readyRelease()?.default_model_name, filename = systemModelFilename(name, readyRelease()?.id);
      preview.textContent = modelNameError ? "" : filename ? t("设备上的文件名：{0}", [filename]) : name ? t("设备上的文件名：{0}-r〈发布ID〉.rknn（准备模型后确定版本编号）", [name]) : t("设备上的文件名由服务器自动生成。");
      preview.hidden = Boolean(modelNameError);
    }
    const activate = node.querySelector("#edge-system-mode").value === "activate", fps = Number(node.querySelector("#edge-system-fps").value);
    node.querySelector("#edge-system-fps-field").hidden = !activate;
    node.querySelector("#edge-system-confirm-label").hidden = !activate;
    node.querySelector("#edge-system-mode-note").textContent = activate ? t("将替换当前模型“{0}”，并自动验证运行结果。", [data.current?.sModel || t("未选择")]) : t("保存到设备模型库，保留当前运行的模型。");
    const available = readyRelease() || source()?.compatible === true;
    const blocked = node.querySelector("#edge-system-blocked");
    blocked.textContent = connectionBlock() || data.blocked_reason || (pending() ? t("设备正在部署或结果待核实，请查看上方进度。") : !available ? source()?.reason || t("当前转换不可部署，请返回项目检查模型。") : ""); blocked.hidden = !blocked.textContent;
    const button = modal.querySelector("#modal-submit");
    button.hidden = historyOnly || pending();
    node.querySelector("#edge-system-deploy-section").hidden = historyOnly || pending();
    button.disabled = historyOnly || submitting || Boolean(modelNameError) || Boolean(connectionBlock()) || !data.can_deploy || pending() || !available || (activate && (!node.querySelector("#edge-system-confirm").checked || !Number.isInteger(fps) || fps < 1 || fps > 60));
    if (!submitting && !button.innerHTML.includes("spinner")) button.textContent = activate ? t("部署并启用") : t("加入设备模型库");
    const changeDevice = node.querySelector("#edge-system-change-device"); if (changeDevice) changeDevice.disabled = submitting;
    const cancel = modal.querySelector("#modal-cancel"); if (cancel) cancel.textContent = (data.deployments || []).length ? t("关闭") : t("取消");
  }
  async function refresh(force = false) {
    if (!alive()) return;
    if (request) { await request; if (force && alive()) return refresh(); return; }
    const serial = ++requestSerial;
    request = (async () => {
      try {
        const results = await Promise.allSettled([api(`${pathFor(device)}/system-models${lockedConversionId ? `?conversion_id=${lockedConversionId}` : ""}`), project ? api(`/api/edge/devices?project_id=${Number(project.id)}`) : Promise.resolve(null)]);
        if (!alive() || serial !== requestSerial) return;
        for (const result of results) if (result.status === "rejected") throw result.reason;
        const [result, deviceList] = results.map(item => item.value);
        if (String(result.device_id) !== String(device.id) || Number(result.project_id) !== Number(device.project_id)) throw new Error(t("设备所属项目已变化，请重新选择设备。"));
        if (project && !Array.isArray(deviceList?.devices)) throw new Error(t("服务器未返回有效的设备列表。"));
        if (data && data.current?.sModel !== result.current?.sModel) {
          const confirmation = node.querySelector("#edge-system-confirm"); if (confirmation) confirmation.checked = false;
        }
        if (submittedJob) {
          if ((result.deployments || []).some(item => Number(item.id) === Number(submittedJob.id))) submittedJob = null;
          else result.deployments = [submittedJob, ...(result.deployments || [])];
        }
        data = result; receivedAt = Date.now(); readFailed = false;
        connectedDevice = project ? deviceList.devices.find(item => String(item.id) === String(device.id) && Number(item.project_id) === Number(project.id)) : null;
        if (!node.querySelector("#edge-system-state")) mount();
        node.querySelector("#edge-system-feedback").hidden = true; update();
      } catch (error) {
        if (!alive() || serial !== requestSerial) return;
        readFailed = true;
        if (!data) { node.innerHTML = `<p class="inline-error">${esc(error.message)}</p><button class="btn ghost" id="edge-system-retry">${t("重新读取")}</button>`; node.querySelector("#edge-system-retry").onclick = () => refresh(); }
        else { data = { ...data, can_deploy: false, blocked_reason: t("系统状态更新失败，请恢复连接后再创建部署。"), system_status: data.system_status ? { ...data.system_status, fresh: false } : null }; const feedback = node.querySelector("#edge-system-feedback"); feedback.textContent = t("系统状态更新失败：{0}", [error.message]); feedback.hidden = false; update(); }
      }
    })();
    try { await request; } finally { request = null; }
  }
  function bindCancel() {
    node.querySelectorAll("[data-edge-cancel-model]").forEach(button => { button.onclick = async () => {
      if (!alive() || button.disabled) return; button.disabled = true;
      try { await api(`${pathFor(device)}/model-deployments/${encodeURIComponent(button.dataset.edgeCancelModel)}/cancel`, jsonOptions("POST", {})); if (alive()) await refresh(true); }
      catch (error) { if (alive() && error.status !== 401) notify(error.message, "error"); }
      finally { if (button.isConnected) button.disabled = false; }
    }; });
    node.querySelectorAll("[data-edge-resolve-model]").forEach(button => { button.onclick = () => {
      if (!alive()) return;
      const job = data.deployments.find(item => String(item.id) === button.dataset.edgeResolveModel);
      if (!job?.lease_expired || !["running", "unknown"].includes(job.status)) return;
      const panel = node.querySelector("#edge-system-resolve"), name = node.querySelector("#edge-system-resolve-name"), ack = node.querySelector("#edge-system-resolve-ack"), submit = node.querySelector("#edge-system-resolve-submit"), feedback = node.querySelector("#edge-system-resolve-feedback");
      panel.hidden = false; name.value = ""; ack.checked = false; feedback.textContent = ""; submit.disabled = true;
      const sync = () => { submit.disabled = name.value !== device.name || !ack.checked; };
      name.oninput = sync; ack.onchange = sync; name.focus();
      node.querySelector("#edge-system-resolve-cancel").onclick = () => { panel.hidden = true; if (button.isConnected) button.focus(); };
      submit.onclick = async () => {
        if (!alive() || submit.disabled || name.value !== device.name || !ack.checked) return;
        submit.disabled = true; name.disabled = true; ack.disabled = true;
        try {
          await api(`${pathFor(device)}/model-deployments/${encodeURIComponent(job.id)}/resolve`, jsonOptions("POST", { confirmation_name: name.value, acknowledged: true, expected_updated_at: job.updated_at }));
          if (!alive()) return;
          panel.hidden = true; notify(t("任务已标记失败并解除占用；设备模型保持当前实际状态。")); await refresh(true);
        } catch (error) { if (alive() && error.status !== 401) feedback.textContent = error.message; }
        finally { if (alive()) { name.disabled = false; ack.disabled = false; sync(); } }
      };
    }; });
  }
}

async function openLegacyDeviceDeployment(device) {
  let options = null, releases = [], jobs = [], generating = false, monitor = null, jobRequest = 0;
  openModal({ title: t("{0} · 模型部署", [device.name]), subtitle: t("将训练后的模型打包为 reCamera Pro App Center 应用。"), wide: true,
    body: `<div id="edge-model-content" aria-live="polite">${t("正在检查可用模型和部署连接…")}</div>`, submitText: t("部署到设备"), onReady: () => { document.querySelector("#modal-submit").disabled = true; },
    onClose: () => { clearInterval(monitor); }, onSubmit: async () => {
      if (!options || generating) throw new Error(t("请等待模型检查或应用打包完成。"));
      if (!options.deployment?.ssh_available || !options.deployment?.known_hosts_configured) throw new Error(t("服务器尚未完成 SSH 和可信设备主机配置，请先下载应用包手动安装。"));
      if (jobs.some(item => !terminalStates.has(item.status))) throw new Error(t("此设备仍有待确认的部署，请等待当前任务结束后再操作。"));
      if (jobs[0]?.status === "unknown" && !node.querySelector("#edge-verified-uncertain").checked) throw new Error(t("请先到设备 App Center 核实上次操作结果，再确认是否重新部署。"));
      const release_id = Number(document.querySelector("#edge-release").value);
      const host = document.querySelector("#edge-ssh-host").value.trim(), username = document.querySelector("#edge-ssh-user").value.trim();
      const port = Number(document.querySelector("#edge-ssh-port").value);
      if (!release_id) throw new Error(t("请先生成或选择一个推理应用包。"));
      if (!host || /[\s/@\\]/.test(host) || host.startsWith("-")) throw new Error(t("请填写设备 IP 或主机名，不要包含协议、路径或空格。"));
      if (!/^[a-zA-Z_][a-zA-Z0-9_.-]*$/.test(username)) throw new Error(t("请填写有效的 SSH 用户名。"));
      if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error(t("SSH 端口应为 1–65535 之间的整数。"));
      const modal = document.querySelector(".modal");
      await api(`${pathFor(device)}/deployments`, jsonOptions("POST", { release_id, host, port, username, replace: document.querySelector("#edge-replace-running").checked, start: true }));
      notify(t("部署任务已创建，安装和启动进度将在这里更新。"));
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
    if (node.isConnected && error.status !== 401) { node.innerHTML = `<p class="inline-error">${esc(error.message)}</p><button class="btn ghost" id="edge-model-retry">${t("重新检查")}</button>`; node.querySelector("#edge-model-retry").onclick = () => openLegacyDeviceDeployment(device); }
  }
  function mount() {
    const conversions = options.conversions || [], compatible = conversions.filter(item => item.compatible);
    const sshReady = options.deployment?.ssh_available && options.deployment?.known_hosts_configured;
    node.innerHTML = `<section class="edge-deploy-step"><h3><span>1</span>${t("生成推理应用包")}</h3><p class="hint">${t("在项目“训练与转换”中先完成 RKNN 转换，目标芯片选择 RV1126B。只有输入、输出约定兼容的目标检测模型可以打包。")}</p><div class="edge-build-row"><div class="field"><label for="edge-conversion">${t("已转换的模型")}</label><select id="edge-conversion">${compatible.length ? "" : `<option value="">${t("暂无兼容的转换结果")}</option>`}${conversions.map(item => `<option value="${Number(item.id)}" ${item.compatible ? "" : "disabled"}>${t("转换 #{0} · {1} · {2} px · {3}{4}", [Number(item.id), esc(item.model_arch || t("模型")), Number(item.image_size) || "—", esc(item.precision || "RKNN"), item.compatible ? "" : ` · ${esc(item.reason || t("不兼容"))}`])}</option>`).join("")}</select></div><button class="btn primary" id="edge-build-release" ${compatible.length ? "" : "disabled"}>${t("生成应用包")}</button></div><p id="edge-build-feedback" class="hint" role="status"></p>${conversions.some(item => !item.compatible) ? `<details class="edge-advanced"><summary>${t("查看未满足的模型条件")}</summary><ul>${conversions.filter(item => !item.compatible).map(item => `<li>${t("转换 #{0}：{1}", [Number(item.id), esc(item.reason || t("模型约定不兼容"))])}</li>`).join("")}</ul></details>` : ""}<a class="edge-text-link" href="#/projects/${Number(device.project_id)}/train">${t("前往训练与转换")}</a></section>
      <section class="edge-deploy-step"><h3><span>2</span>${t("选择应用版本")}</h3><div class="field"><label for="edge-release">${t("可部署的推理应用包")}</label><select id="edge-release"></select></div><div id="edge-release-detail"></div>${options.capture_app?.download_url ? `<div class="edge-capture-package">${downloadLink(options.capture_app.download_url, t("下载联动应用包"))}<span class="hint">${t("采集应用独立安装，不随模型升级替换。")}</span></div>` : ""}</section>
      <section class="edge-deploy-step"><h3><span>3</span>${t("部署到同一网络的设备")}</h3><p class="hint">${t("由 CVSprout 服务器通过 SSH 和设备官方 App Center 完成安装。设备需要允许这些连接。")}</p>${sshReady ? "" : `<p class="edge-deploy-notice">${t("{0}请管理员完成部署配置；现在可以下载应用包，打开设备 App Center 手动安装。", [!options.deployment?.ssh_available ? t("服务器尚未准备 SSH 部署工具。") : t("服务器尚未配置可信设备主机文件。")])}</p>`}<div class="form-grid"><div class="field"><label for="edge-ssh-host">${t("设备 IP / 主机名")}</label><input id="edge-ssh-host" placeholder="192.168.1.30" autocomplete="off" ${sshReady ? "" : "disabled"}/></div><div class="field"><label for="edge-ssh-port">${t("SSH 端口")}</label><input id="edge-ssh-port" type="number" min="1" max="65535" value="22" ${sshReady ? "" : "disabled"}/></div><div class="field"><label for="edge-ssh-user">${t("SSH 用户")}</label><input id="edge-ssh-user" value="root" autocomplete="off" ${sshReady ? "" : "disabled"}/></div></div><label class="check-line"><input id="edge-replace-running" type="checkbox" ${sshReady ? "" : "disabled"}/>${t("若推理应用正在运行，允许停止并替换此应用")}</label><p class="hint">${t("替换时推理会短暂中断，新的模型版本安装后自动启动。")}</p><label class="check-line edge-uncertain-confirm" id="edge-uncertain-label" ${jobs[0]?.status === "unknown" ? "" : "hidden"}><input id="edge-verified-uncertain" type="checkbox"/>${t("我已到设备 App Center 核实上次不确定的操作结果，确认现在重新部署")}</label><div id="edge-deployment-history" aria-live="polite">${deploymentHistoryMarkup(jobs)}</div></section>`;
    node.querySelector("#edge-build-release").onclick = buildRelease;
    node.querySelector("#edge-release").onchange = renderRelease;
    node.querySelector("#edge-verified-uncertain").onchange = syncSubmit;
    renderReleaseOptions(); syncSubmit();
  }
  function renderReleaseOptions(selected = null) {
    const select = node.querySelector("#edge-release");
    select.innerHTML = releases.length ? releases.map(item => `<option value="${esc(item.id)}">${t("{0} · 转换 #{1}", [esc(item.version || item.id), Number(item.conversion_id)])}</option>`).join("") : `<option value="">${t("先生成推理应用包")}</option>`;
    if (selected) select.value = selected;
    renderRelease();
  }
  function renderRelease() {
    const release = releases.find(item => String(item.id) === node.querySelector("#edge-release").value);
    node.querySelector("#edge-release-detail").innerHTML = release ? `<div class="edge-release-info">${downloadLink(release.download_url, t("下载推理应用包"))}<span>${(Number(release.size_bytes || 0) / 1048576).toFixed(1)} MiB</span></div><details class="edge-advanced"><summary>${t("版本校验信息")}</summary><p>SHA-256${t("：")}<code>${esc(release.sha256 || "—")}</code></p><p>${t("设备端实际精度与速度请在目标设备上验证。")}</p></details>` : `<p class="hint">${t("生成后可直接下载，或继续填写设备地址进行部署。")}</p>`;
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
    generating = true; button.disabled = true; button.textContent = t("正在打包…"); feedback.classList.remove("feedback-error"); feedback.textContent = t("正在校验模型并生成 App Center 安装包，请稍候。"); syncSubmit();
    try {
      const result = await api("/api/edge/releases", jsonOptions("POST", { conversion_id }));
      if (!node.isConnected) return;
      const release = result.release || result;
      releases = [release, ...releases.filter(item => item.id !== release.id)]; renderReleaseOptions(String(release.id));
      feedback.textContent = t("应用包已就绪，可以下载或部署到设备。");
    } catch (error) { if (node.isConnected && error.status !== 401) { feedback.textContent = error.message; feedback.classList.add("feedback-error"); } }
    finally { generating = false; if (node.isConnected) { button.disabled = false; button.textContent = t("生成应用包"); syncSubmit(); } }
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
    } catch (error) { if (node.isConnected && request === jobRequest && error.status !== 401) { node.querySelector("#edge-deployment-history").textContent = t("部署状态更新失败：{0}", [error.message]); } }
  }
}
