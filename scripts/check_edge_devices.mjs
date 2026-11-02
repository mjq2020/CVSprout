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
}
assert(context.captureRecordMarkup({ asset_id: 42, filename: "scene.jpg", capture_id: "capture-live" }, device).includes("annotate?asset=42"));
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
