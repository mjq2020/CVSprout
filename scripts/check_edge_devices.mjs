// Exercise real device URL validation, escaped cards, downloads and stale-list guards.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../static/edge-devices-ui.js", import.meta.url), "utf8")
  .replace(/^import .*;\n/gm, "").replaceAll("export async function ", "async function ").replaceAll("export function ", "function ");
const escapeHtml = value => String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const state = { currentSection: "devices", selectionEpoch: 1, session: { role: "admin" } };
const feedback = { hidden: true, textContent: "" };
const result = { id: "edge-device-results", isConnected: true, innerHTML: "", contains: () => false,
  setAttribute() {}, removeAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
const calls = [], document = { activeElement: null, hidden: false,
  querySelector: selector => selector === "#edge-device-results" ? result : selector === "#edge-page-feedback" ? feedback : null,
  getElementById: id => id === result.id ? result : null };
const context = vm.createContext({ URL, Date, Number, Set, JSON, document, state, esc: escapeHtml,
  api: path => { const pending = deferred(); calls.push({ path, ...pending }); return pending.promise; } });
vm.runInContext(source, context);

for (const value of ["", "not-a-url", "javascript:alert(1)", "ftp://host", "http://localhost:8000", "http://camera.localhost", "http://127.2.3.4", "http://0.0.0.0", "http://[::1]", "http://[::]", "https://user:secret@host", "https://host?q=token", "https://host/#token"]) {
  assert.equal(context.remoteServerURL(value), "", `Reject device-unreachable or credential-bearing URL: ${value}`);
}
assert.equal(context.remoteServerURL("http://192.168.1.20:8080/"), "http://192.168.1.20:8080");
assert.equal(context.remoteServerURL("https://vision.example/cvsprout/"), "https://vision.example/cvsprout");
assert.equal(context.remoteServerURL("http://[fd00::20]:8080"), "http://[fd00::20]:8080");

const now = Date.parse("2026-11-01T10:00:00Z");
const device = { id: "device-1", name: '<img src=x onerror="bad()">', project_id: 2, project_name: "A & B", enabled: true,
  capture_enabled: true, capture_interval_seconds: 10, capture_count: 3, asset_count: 2, last_seen_at: "2026-11-01T09:59:55Z" };
assert.equal(context.deviceConnection(device, now).kind, "recent");
assert.equal(context.deviceConnection(device, now).label, "最近在线 · 采集已启用");
assert.equal(context.deviceConnection({ ...device, capture_enabled: false }, now).label, "最近在线 · 采集暂停");
assert.equal(context.deviceConnection({ ...device, project_archived: true }, now).label, "项目已归档 · 采集暂停");
assert.equal(context.deviceConnection({ ...device, project_archived: true, last_seen_at: null }, now).kind, "paused");
assert.equal(context.deviceConnection({ ...device, project_archived: true, enabled: false }, now).kind, "revoked");
assert.equal(context.deviceConnection({ ...device, last_seen_at: null }, now).kind, "waiting");
assert.equal(context.deviceConnection({ ...device, last_seen_at: "bad date" }, now).kind, "offline");
assert.equal(context.deviceConnection(device, now + 180000).kind, "offline");
assert.equal(context.deviceConnection({ ...device, enabled: false }, now).kind, "revoked");
const html = context.deviceCardMarkup({ ...device, device_token: "never-render-this-token" });
assert(html.includes("&lt;img") && html.includes("A &amp; B"));
assert(!html.includes("<img") && !html.includes("never-render-this-token"));
assert(html.includes("data-edge-model") && html.includes("data-edge-captures"));
const revoked = context.deviceCardMarkup({ ...device, revoked_at: "2026-11-01T10:00:00Z" });
assert(revoked.includes("data-edge-captures") && !revoked.includes("data-edge-model") && !revoked.includes("data-edge-rotate"));
const archived = context.deviceCardMarkup({ ...device, project_archived: true });
assert(archived.includes("项目已归档，已暂停新采集") && !archived.includes("秒采集一张") && !archived.includes("队列继续上传"));
for (const asset_id of [null, undefined, 0, -1, "", "invalid"]) {
  const record = context.captureRecordMarkup({ asset_id, filename: '<scene>.jpg', capture_id: "capture-tombstone" }, device);
  assert(record.includes("原图已删除") && record.includes("采集记录保留") && record.includes("&lt;scene&gt;.jpg"));
  assert(!record.includes("<a ") && !record.includes("annotate?asset="), "Deleted-image history must never navigate to another image");
}
assert(context.captureRecordMarkup({ asset_id: 42, filename: "scene.jpg", capture_id: "capture-live" }, device).includes("annotate?asset=42"));
const deployed = context.deploymentHistoryMarkup([{ status: "succeeded", stage: "running" }]);
assert(deployed.includes("部署完成") && deployed.includes("应用已启动") && !deployed.includes("正在部署"));
assert(context.deploymentHistoryMarkup([{ status: "running", stage: "connecting" }]).includes("<strong>正在部署</strong>"));
assert.equal(context.downloadLink("javascript:alert(1)", "download"), "");
assert.equal(context.downloadLink("//attacker.invalid/api/edge/release", "download"), "");
assert.equal(context.downloadLink("https://attacker.invalid/api/edge/release", "download"), "");
assert.equal(context.downloadLink("/api/edge/\\attacker", "download"), "");
assert(context.downloadLink("/api/edge/releases/1/download", "下载").includes("data-edge-package"));

// A later refresh wins; the late first response cannot put older devices back.
const first = context.loadDevices(), second = context.loadDevices();
calls[1].resolve({ devices: [{ ...device, name: "Current device" }] }); await second;
assert(result.innerHTML.includes("Current device"));
calls[0].resolve({ devices: [{ ...device, name: "Stale device" }] }); await first;
assert(!result.innerHTML.includes("Stale device"));

// Navigation/session changes prevent both successful and failed old requests repainting.
const before = result.innerHTML, third = context.loadDevices(); state.selectionEpoch++;
calls[2].resolve({ devices: [{ ...device, name: "Wrong route" }] }); await third;
assert.equal(result.innerHTML, before);
const fourth = context.loadDevices(); state.session = { role: "reviewer" };
calls[3].reject(new Error("old session failure")); await fourth;
assert.equal(feedback.hidden, true);
assert.equal(result.innerHTML, before);

// Background heartbeats retain focused controls rather than replacing their cards.
result.contains = () => true;
const fifth = context.loadDevices({ background: true });
calls[4].resolve({ devices: [{ ...device, name: "Updated heartbeat" }] }); await fifth;
assert.equal(result.innerHTML, before);
result.contains = () => false;
const sixth = context.loadDevices({ background: true });
calls[5].resolve({ devices: [{ ...device, name: "Updated heartbeat" }] }); await sixth;
assert(result.innerHTML.includes("Updated heartbeat"), "Deferred refresh appears after focus leaves, even when the next API response is unchanged");

console.log("PASS edge devices: remote URL validation, archived/connection states, escaped cards, deleted-image tombstones, deployment result labels, revoked controls, same-origin packages, reordered/session-stale requests and background focus retention");
