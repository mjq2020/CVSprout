// Exercise actual training readiness decisions and the rendered recovery links.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../static/app.js", import.meta.url), "utf8");
const start = source.indexOf("function trainingReadiness("), end = source.indexOf("\nfunction captureTrainingViewState(", start);
assert(start >= 0 && end > start);
const code = source.slice(start, end);
function fixture() {
  const state = { project: { id: 9 }, versions: [{ id: 2 }], training: { availability: { available: true, reason: "" }, total: 0 },
    trainingCatalog: { models: [{ id: "yolo.pt", available: true }], tasks: {} }, trainingResources: { worker: { online_count: 1 } }, trainingMetricsHistory: {} };
  const role = { reviewer: true, admin: true }, app = { innerHTML: "" };
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const context = vm.createContext({ state, app, canReview: () => role.reviewer, isAdmin: () => role.admin,
    document: { querySelector: () => null }, deferRenderForNavigation: () => false, captureTrainingViewState: () => null, monitoredTrainingRun: () => null,
    escapeHtml: esc, icon: () => "", projectPageShell: (_section, body) => body, trainingSyncMarkup: () => "",
    trainingListMarkup: () => "", trainingStatusLabel() {}, trainingTaskInfo() {}, formatDateTime() {}, trainingTaskCapabilityCard: () => "", trainingCatalogModelCard: () => "", trainingLicenseMarkup: () => "",
    bindCommonProjectEvents() {}, bindTrainingPageEvents() {}, restoreTrainingViewState() {},
  });
  vm.runInContext(code, context);
  const render = () => { context.renderTrainingPage(); return app.innerHTML; };
  return { state, role, context, render };
}
const buttonDisabled = html => /id="new-training"[^>]*disabled/.test(html);

{
  const h = fixture();
  h.state.training.availability = { available: false, reason: "没有与当前数据结构兼容且可加载的本地训练权重" };
  h.state.trainingCatalog.models = [];
  const result = h.context.trainingReadiness(), html = h.render();
  assert.equal(result.status, "model"); assert.equal(result.ready, false);
  assert(result.reason.includes(h.state.training.availability.reason), "The actual backend reason is shown beside the disabled button");
  assert(buttonDisabled(html));
  assert(/training-start-notice[^>]*>[\s\S]*?href="#\/models"/.test(html), "The visible readiness notice provides a model preparation link");
  assert(html.includes("下载或导入训练权重"));
  h.role.admin = false;
  assert(h.context.trainingReadiness().reason.includes("请联系管理员"));
  assert(!h.render().includes('href="#/models"'), "Reviewers are told who can prepare missing weights");
}
{
  const h = fixture(); h.state.trainingResources.worker = { online_count: 0, embedded_enabled: false };
  assert.equal(h.context.trainingReadiness().status, "worker");
  assert(h.context.trainingReadiness().reason.includes("没有在线的训练执行服务"));
  assert(buttonDisabled(h.render())); assert(!h.render().includes('href="#/models"'));
  h.state.trainingResources.worker.embedded_enabled = true;
  assert.equal(h.context.trainingReadiness().ready, true, "Embedded pipeline mode remains supported");
}
{
  const h = fixture(); h.state.versions = [];
  assert.equal(h.context.trainingReadiness().status, "version");
  assert(buttonDisabled(h.render())); assert(h.render().includes('href="#/projects/9/versions"'));
}
{
  const h = fixture(); h.role.reviewer = false; h.role.admin = false; h.state.versions = [];
  assert.equal(h.context.trainingReadiness().status, "permission");
  assert(buttonDisabled(h.render())); assert(!h.render().includes('href="#/projects/9/versions"'), "A member without training permission is not sent to another restricted action");
}
{
  const h = fixture(); h.state.trainingEnvironmentLoading = true; h.state.trainingCatalog.models = [];
  assert.equal(h.context.trainingReadiness().status, "loading");
  assert(h.context.trainingReadiness().reason.includes("正在检查")); assert(buttonDisabled(h.render()));
  assert(!h.render().includes('href="#/models"'), "An incomplete probe is not diagnosed as missing weights");
  h.state.trainingCatalog.models = [{ available: true }];
  assert.equal(h.context.trainingReadiness().ready, true, "Refreshing an already healthy environment does not disable creation");
}
for (const reason of ["训练功能已被 MDP_TRAINING_ENABLED 禁用", "未安装 ultralytics；请安装 requirements-models.txt", "训练环境暂时无法检查。请刷新重试。", '<script>alert("not safe")</script>']) {
  const h = fixture(); h.state.trainingCatalog.models = []; h.state.training.availability = { available: false, reason };
  assert.equal(h.context.trainingReadiness().status, "environment");
  assert.equal(h.context.trainingReadiness().reason, reason);
  assert(buttonDisabled(h.render())); assert(!h.render().includes('href="#/models"'));
  assert(!h.render().includes("<script>"), "Backend diagnostics are HTML escaped");
}
{
  const h = fixture(); assert.equal(h.context.trainingReadiness().ready, true);
  assert(!buttonDisabled(h.render())); assert(!h.render().includes("training-start-notice"));
  assert(h.render().includes('id="empty-new-training"'));
}

console.log("PASS training readiness: missing weights and actionable admin link, offline worker, embedded mode, missing version, role restrictions, loading vs healthy refresh, backend errors, safe rendered messages");
