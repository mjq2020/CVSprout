// Exercise actual device rendering and polling logic with a small DOM adapter.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../static/edge-devices-ui.js", import.meta.url), "utf8")
  .replace(/^import .*;\n/gm, "").replaceAll("export async function ", "async function ").replaceAll("export function ", "function ");
const escapeHtml = value => String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const tick = () => new Promise(resolve => setImmediate(resolve));
const state = { currentSection: "devices", selectionEpoch: 1, session: { role: "admin" } };
const feedback = { hidden: true, textContent: "" }, updated = { textContent: "" };
const listeners = {}, calls = [];

class Card {
  constructor(markup, grid) {
    this.grid = grid; this.dataset = { edgeDevice: markup.match(/data-edge-device="([^"]+)"/)[1] };
    this.values = [...markup.matchAll(/data-edge-value="([^"]+)"[^>]*>([^<]*)</g)].map(match => ({ dataset: { edgeValue: match[1] }, textContent: match[2] }));
    this.badge = { className: markup.match(/class="(edge-connection [^"]+)"/)[1], textContent: markup.match(/class="edge-connection [^"]+" role="status">([^<]+)/)[1] };
    this.diagnosis = { className: "" }; this.error = { hidden: false, open: false };
    this.more = { hidden: false, open: false }; this.settings = { hidden: false }; this.model = { hidden: false };
  }
  querySelector(selector) {
    return selector === ".edge-connection" ? this.badge : selector === ".edge-device-diagnosis" ? this.diagnosis : selector === ".edge-diagnostic-details" ? this.error
      : this.values.find(node => selector === `[data-edge-value="${node.dataset.edgeValue}"]`) || null;
  }
  querySelectorAll(selector) {
    return selector === "[data-edge-value]" ? this.values : selector.includes("[data-edge-edit-control]") ? [this.settings, this.model, this.more] : [];
  }
  remove() { this.grid.cards = this.grid.cards.filter(card => card !== this); }
}
class Grid {
  constructor(markup) { this.cards = []; this.insertAdjacentHTML("beforeend", markup); }
  querySelectorAll(selector) { return selector === "[data-edge-device]" ? this.cards : []; }
  insertAdjacentHTML(_, markup) { for (const [article] of markup.matchAll(/<article\b[\s\S]*?<\/article>/g)) this.cards.push(new Card(article, this)); }
}
const result = { id: "edge-device-results", isConnected: true, grid: null, _html: "", contains: () => false,
  setAttribute() {}, removeAttribute() {},
  set innerHTML(value) { this._html = value; this.grid = value.includes('class="edge-device-grid"') ? new Grid(value) : null; },
  get innerHTML() { return this._html; },
  querySelector(selector) { return selector === ".edge-device-grid" ? this.grid : null; },
  querySelectorAll(selector) { return selector === "[data-edge-device]" ? this.grid?.cards || [] : []; } };
const document = { activeElement: null, hidden: false,
  addEventListener: (event, callback) => { listeners[event] = callback; },
  querySelector: selector => ({ "#edge-device-results": result, "#edge-page-feedback": feedback, "#edge-refresh-time": updated })[selector] || null,
  getElementById: id => id === result.id ? result : null };
const context = vm.createContext({ URL, Date, Number, Set, Map, JSON, document, state, esc: escapeHtml,
  api: path => { const pending = deferred(); calls.push({ path, ...pending }); return pending.promise; } });
vm.runInContext(source, context);

for (const value of ["", "not-a-url", "javascript:alert(1)", "ftp://host", "http://localhost:8000", "http://camera.localhost", "http://127.2.3.4", "http://0.0.0.0", "http://[::1]", "http://[::]", "https://user:secret@host", "https://host?q=token", "https://host/#token"]) {
  assert.equal(context.remoteServerURL(value), "", `Reject device-unreachable or credential-bearing URL: ${value}`);
}
assert.equal(context.remoteServerURL("http://192.168.1.20:8080/"), "http://192.168.1.20:8080");
assert.equal(context.remoteServerURL("https://vision.example/cvsprout/"), "https://vision.example/cvsprout");
assert.equal(context.remoteServerURL("http://[fd00::20]:8080"), "http://[fd00::20]:8080");

const now = Date.now();
const device = { id: "device-1", name: '<img src=x onerror="bad()">', project_id: 2, project_name: "A & B", enabled: true,
  capture_enabled: true, capture_interval_seconds: 10, capture_count: 3, asset_count: 2, last_seen_at: new Date(now - 5000).toISOString(),
  connection: { state: "online", age_seconds: 5, timeout_seconds: 90, last_contact_at: new Date(now - 5000).toISOString(), last_heartbeat_at: new Date(now - 5000).toISOString() },
  heartbeat: { app_version: "1.0.2", pending_images: 0 }, ingestion: { state: "receiving", heartbeat_fresh: true, pending_images: 0 } };
assert.equal(context.deviceConnection(device, now).kind, "online");
assert.equal(context.deviceConnection({ ...device, capture_enabled: false }, now).label, "在线 · 已连接", "Capture setting is not connection evidence");
assert.equal(context.deviceConnection({ ...device, project_archived: true }, now).kind, "online", "Archival is distinct from connectivity");
assert.equal(context.deviceConnection({ ...device, connection: { state: "never_connected" } }, now).kind, "waiting");
assert.equal(context.deviceConnection({ ...device, connection: { state: "reconnect_required" } }, now).kind, "reconnect");
assert.equal(context.deviceConnection({ ...device, connection: { state: "offline" } }, now).kind, "offline");
assert.equal(context.deviceConnection({ ...device, connection: null, last_seen_at: "bad date" }, now).kind, "offline");
assert.equal(context.deviceConnection(device, now + 90000, { observedAt: now }).kind, "offline");
assert.equal(context.deviceConnection(device, now, { stale: true }).kind, "unknown");
assert.equal(context.deviceConnection({ ...device, enabled: false }, now, { stale: true }).kind, "revoked");
assert.equal(context.deviceConnection({ ...device, connection: null, last_seen_at: null }, now).kind, "waiting");
for (const [stateName, phrase] of [["no_recent_images", "近期没有收到"], ["waiting_heartbeat", "心跳"], ["waiting_camera", "相机画面"], ["paused", "已暂停"], ["archived", "归档"], ["session_complete", "已达上限"], ["upload_pending", "等待上传"], ["queue_full", "队列已满"]]) {
  assert(context.deviceDiagnosis({ ...device, ingestion: { state: stateName } }).title.includes(phrase), stateName);
}
assert(context.deviceDiagnosis({ ...device, ingestion: { state: "error", last_error: "http_401" } }).text.includes("最新 device_token"));
assert(context.deviceDiagnosis({ ...device, connection: { state: "never_connected" } }).text.includes("仅创建接入或打开采集开关不会连接设备"));
const html = context.deviceCardMarkup({ ...device, device_token: "never-render-this-token" });
assert(html.includes("&lt;img") && html.includes("A &amp; B"));
assert(!html.includes("<img") && !html.includes("never-render-this-token"));
assert(html.includes("data-edge-model") && html.includes("data-edge-captures") && html.includes("最近联系") && html.includes("最近心跳") && html.includes("采集应用版本：1.0.2"));
const revoked = context.deviceCardMarkup({ ...device, revoked_at: new Date().toISOString() });
assert(revoked.includes('data-edge-model="device-1" hidden') && revoked.includes("设备接入已撤销"));
const archived = context.deviceCardMarkup({ ...device, project_archived: true, ingestion: { state: "archived" } });
assert(archived.includes("项目已归档，已暂停新采集") && !archived.includes("秒一张") && !archived.includes("队列继续上传"));
for (const asset_id of [null, undefined, 0, -1, "", "invalid"]) {
  const record = context.captureRecordMarkup({ asset_id, filename: '<scene>.jpg', capture_id: "capture-tombstone" }, device);
  assert(record.includes("原图已删除") && record.includes("采集记录保留") && record.includes("&lt;scene&gt;.jpg"));
  assert(!record.includes("<a ") && !record.includes("annotate?asset="), "Deleted-image history must never navigate to another image");
  assert(!record.includes("<img ") && !record.includes("data-edge-preview"), "Deleted images must not render broken thumbnails or preview buttons");
}
const imageRecord = context.captureRecordMarkup({ asset_id: 42, filename: '<scene "x">.jpg', capture_id: 'capture-"<bad>' }, device);
assert(imageRecord.includes("annotate?asset=42"));
assert(imageRecord.includes('src="/api/assets/42/thumbnail?size=320"') && imageRecord.includes('data-edge-preview="42"'));
assert(imageRecord.includes("&lt;scene &quot;x&quot;&gt;.jpg") && imageRecord.includes('data-edge-capture="capture-&quot;&lt;bad&gt;"'));
assert(!imageRecord.includes('<scene "x">') && !imageRecord.includes('data-edge-capture="capture-"<bad>'));
for (const [complete, naturalWidth, event, expected] of [[true, 320, null, "loaded"], [true, 0, null, "failed"], [false, 0, "load", "loaded"], [false, 0, "error", "failed"]]) {
  const status = { hidden: false, textContent: "正在加载…" }, events = {}, classes = new Set();
  const image = { complete, naturalWidth, hidden: false, parentElement: { querySelector: () => status }, classList: { add: value => classes.add(value) },
    addEventListener: (name, callback) => { events[name] = callback; } };
  context.bindCaptureImages({ querySelectorAll: () => [image] });
  if (event) events[event]();
  assert.equal(image.hidden, expected === "failed"); assert.equal(status.hidden, expected === "loaded");
  if (expected === "loaded") assert(classes.has("loaded")); else assert.equal(status.textContent, "图片暂不可用");
}
const deployed = context.deploymentHistoryMarkup([{ status: "succeeded", stage: "running" }]);
assert(deployed.includes("部署完成") && deployed.includes("应用已启动") && !deployed.includes("正在部署"));
assert(context.deploymentHistoryMarkup([{ status: "running", stage: "connecting" }]).includes("<strong>正在部署</strong>"));
for (const value of ["javascript:alert(1)", "//attacker.invalid/api/edge/release", "https://attacker.invalid/api/edge/release", "/api/edge/\\attacker"]) assert.equal(context.downloadLink(value, "download"), "");
assert(context.downloadLink("/api/edge/releases/1/download", "下载").includes("data-edge-package"));

// Repeated poll ticks share one request. A manual refresh queues one fresh GET.
const first = context.loadDevices(), duplicate = context.loadDevices({ background: true });
assert.equal(calls.length, 1);
const manual = context.loadDevices();
assert.equal(calls.length, 1);
calls[0].resolve({ devices: [{ ...device, name: "Current device" }] }); await tick();
assert.equal(calls.length, 2, "Manual refresh runs after the pending response");
calls[1].resolve({ devices: [{ ...device, name: "Saved settings" }] }); await Promise.all([first, duplicate, manual]);
const card = result.grid.cards[0];
assert.equal(card.querySelector('[data-edge-value="name"]').textContent, "Saved settings");

// Updating counters never replaces cards, keyboard focus, or expanded menus.
card.more.open = true; card.error.open = true; document.activeElement = card.settings;
const next = context.loadDevices({ background: true }); calls.at(-1).resolve({ devices: [{ ...device, name: "Updated heartbeat", capture_count: 4 }] }); await next;
assert.equal(result.grid.cards[0], card); assert.equal(document.activeElement, card.settings);
assert(card.more.open && card.error.open); assert.equal(card.querySelector('[data-edge-value="captures"]').textContent, "4");

// A failed refresh immediately removes stale online indicators, but keeps data.
const failed = context.loadDevices({ background: true }); calls.at(-1).reject(new Error("network unavailable")); await failed;
assert.equal(card.badge.className, "edge-connection unknown"); assert.equal(feedback.hidden, false);
assert.equal(card.querySelector('[data-edge-value="captures"]').textContent, "4");
const recovered = context.loadDevices({ background: true }); calls.at(-1).resolve({ devices: [device] }); await recovered;
assert.equal(card.badge.className, "edge-connection online"); assert(feedback.hidden);

// Hidden pages do not fetch. Returning to the visible page resumes immediately.
const beforeHidden = calls.length; document.hidden = true; await context.loadDevices({ background: true }); listeners.visibilitychange();
assert.equal(calls.length, beforeHidden);
document.hidden = false; listeners.visibilitychange(); assert.equal(calls.length, beforeHidden + 1);
calls.at(-1).resolve({ devices: [device] }); await tick();

// A previous page/session request may finish later, but cannot repaint this one.
const old = context.loadDevices({ background: true }); const oldCall = calls.at(-1); state.selectionEpoch++;
const current = context.loadDevices({ background: true }); calls.at(-1).resolve({ devices: [{ ...device, name: "New page" }] }); await current;
oldCall.resolve({ devices: [{ ...device, name: "Old page" }] }); await old;
assert.equal(card.querySelector('[data-edge-value="name"]').textContent, "New page");
const staleFailure = context.loadDevices(); state.session = { role: "reviewer" }; calls.at(-1).reject(new Error("old session failure")); await staleFailure;
assert(feedback.hidden);

// The setup panel updates success in place; the one-time token is not re-rendered.
const tokenInput = { value: "one-time-secret", type: "password" }, setupTitle = { textContent: "" }, setupText = { textContent: "" };
context.setupNode = { isConnected: true, className: "", querySelector: tag => tag === "strong" ? setupTitle : setupText };
context.setupDevice = device;
vm.runInContext("tokenSetup = { device: setupDevice, node: setupNode };", context);
context.refreshTokenStatus(); assert.equal(setupTitle.textContent, "已确认设备连接成功"); assert.equal(tokenInput.value, "one-time-secret");
vm.runInContext("listReadFailed = true;", context); context.refreshTokenStatus(); assert.equal(setupTitle.textContent, "连接状态待确认");
vm.runInContext("listReadFailed = false; listReceivedAt = Date.now() - 20000;", context); context.refreshConnectionDisplay();
assert.equal(card.badge.className, "edge-connection unknown", "Stalled requests must not leave an online indicator indefinitely");
state.currentSection = "overview"; const beforeLeaving = calls.length; listeners.visibilitychange(); await context.loadDevices({ background: true });
assert.equal(calls.length, beforeLeaving);

console.log("PASS edge devices: connection/ingestion states, server-age expiry, escaped cards, stable focused/expanded DOM, single-flight polling, queued manual refresh, hidden/navigation/session guards, failure recovery, setup status and token preservation");

// LAN HTTP has no Clipboard API. Copy must happen in the original click turn,
// never expose the password input, and remove the temporary token afterwards.
function clipboardHarness({ writeText, copyResult = true, copyThrows = false, legacyAvailable = true } = {}) {
  const entries = [], removed = [], copyCalls = [], range = { cloneRange: () => range };
  const selection = { rangeCount: 1, getRangeAt: () => range, removeAllRanges() { this.rangeCount = 0; }, addRange(value) { assert.equal(value, range); this.rangeCount++; } };
  const copyDocument = { activeElement: null, addEventListener() {}, getSelection: () => selection };
  const input = { isConnected: true, type: "password", value: "cvsprout_device_test-secret", selectionStart: 3, selectionEnd: 8, selectionDirection: "forward",
    focus() { copyDocument.activeElement = this; }, setSelectionRange(start, end, direction) { this.selectionStart = start; this.selectionEnd = end; this.selectionDirection = direction; } };
  input.focus();
  const container = { isConnected: true, appendChild(node) { entries.push(node); node.isConnected = true; } };
  copyDocument.createElement = tag => {
    assert.equal(tag, "textarea");
    const node = { value: "", style: {}, isConnected: false, setAttribute() {},
      focus() { copyDocument.activeElement = this; }, select() {}, setSelectionRange() {},
      remove() { removed.push({ value: this.value, node: this }); entries.splice(entries.indexOf(this), 1); this.isConnected = false; } };
    return node;
  };
  if (legacyAvailable) copyDocument.execCommand = command => {
    copyCalls.push({ command, value: copyDocument.activeElement.value });
    if (copyThrows) throw new Error("Browser refused copy");
    return copyResult;
  };
  const clipboardContext = vm.createContext({ document: copyDocument, navigator: writeText ? { clipboard: { writeText } } : {}, Date, state: { currentSection: "overview" } });
  vm.runInContext(source, clipboardContext);
  return { context: clipboardContext, copy: (isCurrent = () => true) => clipboardContext.copyDeviceToken(input.value, container, isCurrent), entries, removed, copyCalls, input, container, document: copyDocument, selection };
}
const lan = clipboardHarness();
const lanCopy = lan.copy();
assert.equal(lan.copyCalls.length, 1, "HTTP fallback must run synchronously before the click yields");
assert.deepEqual(lan.copyCalls[0], { command: "copy", value: lan.input.value });
assert.equal(await lanCopy, true);
assert.equal(lan.entries.length, 0); assert.equal(lan.removed[0].value, "", "Temporary token must be cleared before removal");
assert.equal(lan.document.activeElement, lan.input); assert.equal(lan.input.type, "password");
assert.equal(lan.input.selectionStart, 3); assert.equal(lan.input.selectionEnd, 8); assert.equal(lan.input.selectionDirection, "forward");
assert.equal(lan.selection.rangeCount, 1);

for (const options of [{ copyResult: false }, { copyThrows: true }, { legacyAvailable: false }]) {
  const refused = clipboardHarness(options);
  assert.equal(await refused.copy(), false, "Refused copy must never report success");
  assert.equal(refused.entries.length, 0); assert.equal(refused.input.type, "password"); assert.equal(refused.document.activeElement, refused.input);
  assert(refused.removed.every(item => item.value === ""));
}
const pendingCopy = deferred(), modernValues = [];
const modern = clipboardHarness({ writeText: value => { modernValues.push(value); return pendingCopy.promise; } });
const modernResult = modern.copy(); assert.deepEqual(modernValues, [modern.input.value]); assert.equal(modern.copyCalls.length, 0);
pendingCopy.resolve(); assert.equal(await modernResult, true); assert.equal(modern.entries.length, 0);
const denied = clipboardHarness({ writeText: async () => { throw new Error("Permission denied"); } });
assert.equal(await denied.copy(), true); assert.equal(denied.copyCalls.length, 1); assert.equal(denied.input.type, "password");
const closedPending = deferred(), closed = clipboardHarness({ writeText: () => closedPending.promise });
const closedCopy = closed.copy(); closed.container.isConnected = false; closedPending.reject(new Error("Permission denied"));
assert.equal(await closedCopy, false); assert.equal(closed.copyCalls.length, 0); assert.equal(closed.entries.length, 0);
const staleDialog = clipboardHarness(); assert.equal(await staleDialog.copy(() => false), false); assert.equal(staleDialog.copyCalls.length, 0);
console.log("PASS device token clipboard: synchronous HTTP fallback, exact token, Clipboard API success/rejection, denied/throwing copy, mask/focus/selection retention, stale-modal guard and temporary secret cleanup");

// Exercise the actual modal button: feedback follows copy results, the button
// remains focusable while busy, repeated clicks coalesce, and closing clears it.
function tokenButtonHarness(options = {}) {
  const harness = clipboardHarness(options), notifications = [];
  const button = { isConnected: true, disabled: false, attrs: {},
    focus() { harness.document.activeElement = this; },
    setAttribute(name, value) { this.attrs[name] = value; }, removeAttribute(name) { delete this.attrs[name]; } };
  const showToken = { checked: false }, setupTitle = { textContent: "" }, setupText = { textContent: "" };
  const setup = { isConnected: true, querySelector: tag => tag === "strong" ? setupTitle : setupText };
  const modal = harness.container; modal.querySelector = () => setup; modal.addEventListener = () => {};
  const elements = { "#edge-copy-token": harness.input, ".modal": modal, "#edge-copy-token-button": button, "#edge-show-token": showToken };
  harness.document.querySelector = selector => elements[selector] || null;
  harness.context.esc = escapeHtml; harness.context.api = async () => ({});
  harness.context.notify = (...args) => notifications.push(args);
  let modalOptions;
  harness.context.openModal = options => { modalOptions = options; options.onReady(); };
  harness.context.showDeviceToken({ id: "test", enabled: true, connection: { state: "never_connected" } }, harness.input.value);
  button.focus();
  return { ...harness, button, showToken, notifications, close: () => { modalOptions.onClose(); modal.isConnected = false; } };
}
for (const copied of [true, false]) {
  const actual = tokenButtonHarness({ copyResult: copied });
  const operation = actual.button.onclick();
  assert.equal(actual.copyCalls.length, 1); assert.equal(actual.button.disabled, false);
  assert.equal(actual.document.activeElement, actual.button, "Copy button keeps keyboard focus during the operation");
  assert.equal(actual.button.attrs["aria-busy"], "true");
  await actual.button.onclick(); assert.equal(actual.copyCalls.length, 1, "Ignore a second click while the first copy is pending");
  await operation;
  assert.equal(actual.button.attrs["aria-busy"], undefined);
  assert.equal(actual.notifications.length, 1); assert.equal(actual.notifications[0][1], copied ? "success" : "warning");
  assert.equal(actual.notifications[0][0].includes("设备令牌已复制"), copied);
  assert.equal(actual.input.type, "password"); assert.equal(actual.showToken.checked, false);
  assert.equal(actual.document.activeElement, actual.button); actual.close(); assert.equal(actual.input.value, "");
}
const closingPermission = deferred(), closingButton = tokenButtonHarness({ writeText: () => closingPermission.promise });
const closingOperation = closingButton.button.onclick(); closingButton.close(); closingPermission.reject(new Error("Denied after close"));
await closingOperation; assert.equal(closingButton.notifications.length, 0); assert.equal(closingButton.copyCalls.length, 0);
console.log("PASS token button: accurate success/failure feedback, focusable busy guard, no duplicate copies, masked token and closed-modal cleanup; capture thumbnails: same-origin URLs, escaping, tombstones and cached/async loading states");
