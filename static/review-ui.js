import { t, ta, getLocale } from "./i18n.js?v=0.29.3";
// Dependencies are provided by app.js; this module never imports the application.
let context = null;
let bound = false;
const ACTIONS = { submit: t("批量提交审核"), approve: t("批量通过"), reject: t("批量退回修改") };
const STATUS_NAMES = { eligible: t("可处理"), succeeded: t("已完成"), skipped: t("已跳过"), conflict: t("需重新检查"), forbidden: t("权限不足"), not_found: t("不可用") };
const REVIEW_STATES = { draft: t("草稿"), in_progress: t("标注中"), submitted: t("待审核"), approved: t("已通过"), rejected: t("已退回") };

export function shouldContinueReview() {
  return true;
}

export function reviewContinuationMarkup(reviewer) {
  return reviewer ? `<span class="review-next-toggle" title="${ta("审核通过或退回成功后，自动进入当前范围内下一张待审核图片")}">${t("Y 通过 · X 退回 · 审核后自动下一张")}</span>` : "";
}

export function reviewQueueMarkup(state, reviewer) {
  const count = status => state.assets.filter(asset => asset.review_status === status).length;
  return `<div class="review-queue-shortcuts" aria-label="${ta("审核队列")}"><span>${reviewer ? t("审核队列") : t("协作进度")}</span><button class="btn ghost ${state.dataStatus === "submitted" ? "active" : ""}" data-review-filter="submitted" aria-pressed="${state.dataStatus === "submitted"}">${t("待审核")} <strong>${count("submitted")}</strong></button><button class="btn ghost ${state.dataStatus === "rejected" ? "active" : ""}" data-review-filter="rejected" aria-pressed="${state.dataStatus === "rejected"}">${t("需修改")} <strong>${count("rejected")}</strong></button><button class="btn ghost" data-review-filter="all">${t("查看全部")}</button><small>${reviewer ? t("先检查图片与标注；批量通过仅处理已提交的图片。") : t("检查 AI 结果后提交审核；已退回的图片需要修改后再次提交。")}</small></div>`;
}

export function batchReviewSelectionMarkup(state, reviewer) {
  if (!state.selectedAssetIds.size) return "";
  const total = state.selectedAssetIds.size;
  const tooMany = total > 500;
  return `<div class="selection-banner batch-review-selection"><div><strong>${t("已选择 {0} 张图片", [total.toLocaleString(getLocale())])}</strong><span>${tooMany ? t("批量审核每次最多处理 500 张，请减少选择。") : t("提交前会列出可处理和跳过的图片；AI 初标需要检查并提交后才能通过审核。")}</span></div><div class="batch-review-actions"><button class="btn ghost" data-batch-review="submit" ${tooMany ? "disabled" : ""}>${t("提交审核")}</button>${reviewer ? `<button class="btn primary" data-batch-review="approve" ${tooMany ? "disabled" : ""}>${t("审核通过")}</button><button class="btn danger" data-batch-review="reject" ${tooMany ? "disabled" : ""}>${t("退回修改")}</button>` : `<small>${t("批量通过和退回由审核员处理")}</small>`}</div></div>`;
}

export function bindReviewPage(dependencies) {
  context = dependencies;
  if (bound) return;
  bound = true;
  document.addEventListener("click", event => {
    const filter = event.target.closest("[data-review-filter]");
    if (filter && context.state.currentSection === "data") {
      context.state.dataStatus = filter.dataset.reviewFilter;
      context.state.assetRenderLimit = 200;
      context.renderDataPage();
      return;
    }
    const action = event.target.closest("[data-batch-review]");
    if (action && !action.disabled && context.state.currentSection === "data") openBatchReview(action.dataset.batchReview);
    const picker = event.target.closest("[data-review-picker]");
    if (picker && !picker.disabled && ["ai", "annotate"].includes(context.state.currentSection)) openReviewPicker();
    const job = event.target.closest("[data-job-review]");
    if (job && !job.disabled && context.state.project) openJobReview(Number(job.dataset.jobReview));
  });
}

function openJobReview(jobId) {
  const deps = context;
  const { state, api, openModal, closeModal, notify, escapeHtml } = deps;
  const issue = workspaceReviewProblem(state);
  if (issue) return notify(issue, "warning");
  const projectId = state.project?.id, section = state.currentSection, epoch = state.selectionEpoch, session = state.session;
  const current = () => state.project?.id === projectId && state.currentSection === section && state.selectionEpoch === epoch && state.session === session;
  let content, phase = "loading";
  const loadScope = async () => {
    phase = "loading";
    const scope = await api(`/api/projects/${projectId}/review/jobs/${jobId}`);
    if (!current() || !content?.isConnected) return;
    const terminal = ["completed", "failed", "cancelled"].includes(scope.job_status);
    if (!scope.scope_available || !terminal || !scope.items?.length) {
      phase = "unavailable";
      const message = !terminal ? t("请等待此任务结束后再提交审核。已保存的标注会保留。") : scope.scope_reason || t("此历史任务未记录图片范围，请使用批量选择。");
      content.innerHTML = `<p class="job-review-scope-message">${escapeHtml(message)}</p>${terminal && !scope.scope_available ? `<p class="hint">${t("无法确定哪些图片由此任务处理。可以手动选择需要提交的图片。")}</p><button class="btn ghost" data-job-review-manual>${t("打开批量选择")}</button>` : ""}`;
      content.querySelector("[data-job-review-manual]")?.addEventListener("click", () => { if (current()) openReviewPicker(); });
      const button = document.querySelector("#modal-submit"); button.disabled = false; button.textContent = t("关闭");
      return;
    }
    const entries = [...new Map(scope.items.map(item => [item.asset_id, item])).values()];
    const run = { scope, results: new Map(), submitted: new Set() };
    const openPart = index => {
      if (!current()) return;
      const part = entries.slice(index, index + 500);
      openBatchReview("submit", {
        assetIds: part.map(item => item.asset_id),
        targets: part.map(item => ({ asset_id: item.asset_id, expected_revision: item.expected_revision })),
        jobRun: run, jobStart: index, jobTotal: entries.length,
        nextBatch: index + 500 < entries.length ? () => openPart(index + 500) : null,
        nextBatchSize: Math.min(500, entries.length - index - 500),
        restart: () => { if (current()) openJobReview(jobId); },
      });
    };
    openPart(0);
  };
  openModal({
    title: t("提交本次任务的图片"), wide: true,
    subtitle: t("提交此任务已处理图片的当前保存标注，包含后续人工校正。"),
    body: `<div id="job-review-scope"><p class="hint" role="status">${t("正在读取本次任务处理过的图片…")}</p></div>`, submitText: t("正在读取…"),
    onReady: () => {
      content = document.querySelector("#job-review-scope");
      document.querySelector("#modal-submit").disabled = true;
      void loadScope().catch(error => {
        if (!current() || !content.isConnected) return;
        phase = "error";
        content.innerHTML = `<p class="inline-error">${t("{0} 尚未提交任何图片。", [escapeHtml(error.message)])}</p>`;
        const button = document.querySelector("#modal-submit"); button.disabled = false; button.textContent = t("重新读取任务范围");
      });
    },
    onSubmit: async () => {
      if (!current()) throw new Error(t("页面已切换，请从当前项目的任务记录重新打开。"));
      if (phase === "unavailable") { closeModal(); return; }
      await loadScope();
    },
  });
}

function workspaceReviewProblem(state) {
  if (state.currentSection !== "annotate") return "";
  if (state.savingAnnotation || state.workflowBusy) return t("正在保存或更新审核状态，请完成后再批量处理。");
  if (state.saveFeedback?.status === "error") return t("当前图片有尚未确认保存的修改，请先重试保存或重新加载已保存结果，再批量处理。");
  if (state.pendingAnnotation || state.pendingTaskEdit || state.pendingObjectEdit || state.advancedAnnotationDirty || state.shapeEdit || state.drawing || state.polygon?.length || state.poseDraft || state.keypointPlacement || state.assistPoints?.length) return t("当前图片还有未完成的标注，请先完成并保存，或按 Esc 取消草稿，再批量处理。");
  // Advanced fields are applied explicitly; opening a review must not hide them.
  for (const selector of ["#annotation-points", "#annotation-attributes"]) {
    const field = document.querySelector(selector);
    if (field && field.value !== field.defaultValue) return t("高级坐标或属性有未应用的修改，请先点击“应用高级修改”，再批量处理。");
  }
  return "";
}

function openReviewPicker() {
  const deps = context;
  const { state, openModal, notify, escapeHtml } = deps;
  const problem = workspaceReviewProblem(state);
  if (problem) return notify(problem, "warning");
  const projectId = state.project?.id, section = state.currentSection, epoch = state.selectionEpoch, session = state.session;
  const current = () => state.project?.id === projectId && state.currentSection === section && state.selectionEpoch === epoch && state.session === session;
  const ids = new Set(state.assets.map(asset => asset.id));
  state.selectedAssetIds.forEach(id => { if (!ids.has(id)) state.selectedAssetIds.delete(id); });
  let filter = section === "annotate" ? state.assetFilter || "all" : "all", search = "", limit = 120;
  let content;
  const filtered = () => state.assets.filter(asset => (!search || asset.filename.toLocaleLowerCase().includes(search.toLocaleLowerCase())) && (filter === "all" || (["labeled", "unlabeled"].includes(filter) ? asset.status === filter : asset.review_status === filter)));
  const renderList = () => {
    if (!current() || !content?.isConnected) return;
    const assets = filtered();
    const visible = assets.slice(0, limit);
    const list = content.querySelector("[data-review-picker-list]"), scroll = list.scrollTop;
    const focusedId = document.activeElement?.dataset?.reviewPick;
    list.innerHTML = visible.map(asset => `<label class="review-picker-item ${state.selectedAssetIds.has(asset.id) ? "selected" : ""}"><input type="checkbox" data-review-pick="${asset.id}" ${state.selectedAssetIds.has(asset.id) ? "checked" : ""}/><img src="/api/assets/${asset.id}/thumbnail?size=160" alt="" loading="lazy"/><span><strong>${escapeHtml(asset.filename)}</strong><small>${t("{0} · {1} 个标注{2}", [REVIEW_STATES[asset.review_status] || t("草稿"), asset.annotation_count || 0, asset.id === state.asset?.id && section === "annotate" ? t(" · 当前图片") : ""])}</small></span></label>`).join("") || `<p class="hint">${t("当前筛选没有图片，请调整状态或文件名。")}</p>`;
    list.scrollTop = scroll;
    if (focusedId) list.querySelector(`[data-review-pick="${Number(focusedId)}"]`)?.focus({ preventScroll: true });
    const hidden = [...state.selectedAssetIds].filter(id => !assets.some(asset => asset.id === id)).length;
    content.querySelector("[data-review-picker-count]").textContent = t("筛选结果 {0} 张 · 已选 {1} 张{2}", [assets.length, state.selectedAssetIds.size, hidden ? t("（含筛选外 {0} 张）", [hidden]) : ""]);
    const load = content.querySelector("[data-review-pick-more]");
    load.hidden = visible.length >= assets.length;
    load.textContent = t("再显示 {0} 张", [Math.min(120, assets.length - visible.length)]);
    content.querySelector("[data-review-pick-all]").disabled = !assets.length;
    content.querySelector("[data-review-pick-clear]").disabled = !state.selectedAssetIds.size;
    document.querySelector("#modal-submit").disabled = !state.selectedAssetIds.size || state.selectedAssetIds.size > 500;
  };
  openModal({
    title: t("选择图片，批量提交与审核"), wide: true,
    subtitle: t("选择图片后先预览可处理范围。这里只更新审核状态，不运行 AI 标注；当前页面和画布保持不变。每批最多 500 张。"),
    submitText: t("预览所选图片"),
    body: `<div id="review-picker"><div class="review-picker-toolbar"><label>${t("文件名")}<input data-review-pick-search type="search" placeholder="${ta("搜索图片文件名")}"/></label><label>${t("图片状态")}<select data-review-pick-filter>${[["all", t("全部图片")], ["unlabeled", t("未标注")], ["labeled", t("已标注")], ["draft", t("草稿")], ["in_progress", t("标注中")], ["submitted", t("待审核")], ["approved", t("已通过")], ["rejected", t("已退回")]].map(([value, label]) => `<option value="${value}" ${filter === value ? "selected" : ""}>${label}</option>`).join("")}</select></label><label>${t("批量操作")}<select data-review-pick-action><option value="submit">${t("提交审核")}</option>${deps.canReview() ? `<option value="approve">${t("审核通过")}</option><option value="reject">${t("退回修改")}</option>` : ""}</select></label></div><div class="review-picker-summary"><span data-review-picker-count role="status"></span><button class="btn ghost" data-review-pick-all>${t("选择全部筛选结果")}</button><button class="btn ghost" data-review-pick-clear>${t("清空选择")}</button></div><div class="review-picker-list" data-review-picker-list></div><button class="btn ghost" data-review-pick-more hidden></button><p class="hint">${t("AI 初标请先检查再提交。审核通过只处理“待审核”的图片；其他状态会在预览中说明原因。")}</p></div>`,
    onReady: () => {
      content = document.querySelector("#review-picker");
      content.querySelector("[data-review-pick-search]").oninput = event => { search = event.target.value.trim(); limit = 120; renderList(); };
      content.querySelector("[data-review-pick-filter]").onchange = event => { filter = event.target.value; limit = 120; renderList(); };
      content.querySelector("[data-review-pick-all]").onclick = () => {
        if (!current()) return;
        const all = new Set([...state.selectedAssetIds, ...filtered().map(asset => asset.id)]);
        if (all.size > 500) return notify(t("筛选结果超过每批 500 张的上限，请缩小筛选范围或分批勾选。"), "warning");
        state.selectedAssetIds = all; renderList();
      };
      content.querySelector("[data-review-pick-clear]").onclick = () => { if (current()) { state.selectedAssetIds.clear(); renderList(); } };
      content.querySelector("[data-review-pick-more]").onclick = () => { limit += 120; renderList(); };
      content.addEventListener("change", event => {
        const input = event.target.closest("[data-review-pick]");
        if (!input || !current()) return;
        const id = Number(input.dataset.reviewPick);
        if (input.checked && !state.selectedAssetIds.has(id) && state.selectedAssetIds.size >= 500) { input.checked = false; return notify(t("每批最多选择 500 张，请先处理当前选择。"), "warning"); }
        input.checked ? state.selectedAssetIds.add(id) : state.selectedAssetIds.delete(id); renderList();
      });
      renderList();
    },
    onSubmit: () => {
      if (!current()) throw new Error(t("页面已切换，请在当前项目重新选择图片。"));
      const issue = workspaceReviewProblem(state);
      if (issue) throw new Error(issue);
      openBatchReview(content.querySelector("[data-review-pick-action]").value);
    },
  });
}

export async function openWorkBlockSubmit(block, onResult) {
  const deps = context;
  const { state, api, notify } = deps;
  const projectId = state.project.id, epoch = state.selectionEpoch, session = state.session;
  const detail = await api(`/api/projects/${projectId}/work-blocks/${block.id}`);
  if (state.project?.id !== projectId || state.selectionEpoch !== epoch || state.session !== session || state.currentSection !== "blocks") return;
  const items = detail.items.filter(item => ["draft", "in_progress", "rejected"].includes(item.review_status));
  if (!items.length) { await onResult?.(); notify(t("本块图片均已提交或审核通过，无需重复提交。")); return; }
  openBatchReview("submit", {
    assetIds: items.map(item => item.asset_id),
    targets: items.map(item => ({ asset_id: item.asset_id, expected_revision: item.expected_revision })),
    onResult,
  });
}

function openBatchReview(action, options = {}) {
  const deps = context;
  const { state, api, jsonOptions, openModal, closeModal, notify, escapeHtml } = deps;
  const problem = workspaceReviewProblem(state);
  if (problem) return notify(problem, "warning");
  const projectId = state.project.id;
  const epoch = state.selectionEpoch;
  const section = state.currentSection, session = state.session;
  const independent = Array.isArray(options.assetIds);
  const selected = independent ? [...options.assetIds] : [...state.selectedAssetIds];
  if (!selected.length || selected.length > 500) return notify(t("请选择 1 至 500 张图片后再进行批量审核。"), "warning");
  const current = () => state.project?.id === projectId && state.currentSection === section && state.selectionEpoch === epoch && state.session === session;
  const renderPage = () => { if (current()) (section === "annotate" ? deps.renderWorkspace : section === "ai" ? deps.renderAiPage : section === "blocks" ? deps.renderWorkBlocksPage : deps.renderDataPage)(); };
  let targets = options.targets || selected.map(id => ({ asset_id: id, expected_revision: state.assets.find(asset => asset.id === id)?.revision ?? 0 }));
  const jobRun = options.jobRun;
  const source = jobRun ? { source_job_id: jobRun.scope.source_job_id, source_job_generation: jobRun.scope.source_job_generation } : {};
  const scopeMarkup = () => jobRun ? `<div class="job-review-scope-summary"><strong>${t("本次任务已处理 {0} 张图片{1}", [options.jobTotal.toLocaleString(getLocale()), options.jobTotal > 500 ? t(" · 本批第 {0}–{1} 张", [options.jobStart + 1, options.jobStart + selected.length]) : ""])}</strong><span>${t("以当前保存的标注为准，包含后续人工校正。{0}", [options.jobTotal > 500 ? t("每批最多 500 张，确认后可继续下一批。") : ""])}</span>${jobRun.submitted.size ? `<span>${t("本轮累计新提交 {0} 张", [jobRun.submitted.size.toLocaleString(getLocale())])}</span>` : ""}</div>` : "";
  let preview = null;
  let phase = "loading";
  let reason = "";
  let remaining = [...selected];
  let refreshRequired = false;
  let content;
  const modalCurrent = () => current() && content?.isConnected;
  const itemMessage = item => jobRun && item.status === "skipped" && item.review_status === "submitted" ? t("已提交，正在等待审核，无需重复提交") : jobRun && item.status === "skipped" && item.review_status === "approved" ? t("已审核通过，无需再次提交") : item.message;
  const itemsMarkup = items => `<div class="batch-review-items" role="list">${items.map(item => `<div class="batch-review-item ${item.status}" role="listitem">${item.filename ? `<img src="/api/assets/${item.asset_id}/thumbnail?size=96" alt="" loading="lazy"/>` : `<span class="batch-review-no-image">—</span>`}<div><strong>${escapeHtml(item.filename || t("图片 #{0}", [item.asset_id]))}</strong><span>${escapeHtml(itemMessage(item))}${item.status !== "succeeded" && REVIEW_STATES[item.review_status] ? t(" · 当前：{0}", [REVIEW_STATES[item.review_status]]) : ""}</span></div><span class="batch-review-status">${STATUS_NAMES[item.status] || escapeHtml(item.status)}</span></div>`).join("")}</div>`;
  const priorIssuesMarkup = () => {
    if (!jobRun) return "";
    const prior = [...jobRun.results.values()].filter(item => !selected.includes(item.asset_id) && item.status !== "succeeded" && !["submitted", "approved"].includes(item.review_status));
    return prior.length ? `<details class="job-review-prior"><summary>${t("前面批次还有 {0} 张未提交，可查看原因", [prior.length])}</summary>${itemsMarkup(prior)}<p class="hint">${t("处理后，可重新打开本任务的“提交审核”检查这些图片。")}</p></details>` : "";
  };
  const countsMarkup = (items, result = false) => {
    const primary = items.filter(item => item.status === (result ? "succeeded" : "eligible")).length;
    const skipped = items.filter(item => item.status === "skipped").length;
    const unavailable = items.length - primary - skipped;
    return `<div class="batch-review-counts" role="status"><span><strong>${primary}</strong>${result ? t("已完成") : t("可处理")}</span><span><strong>${skipped}</strong>${t("跳过")}</span><span><strong>${unavailable}</strong>${t("需处理问题")}</span></div>`;
  };
  const showPreview = async (refresh = refreshRequired) => {
    if (!modalCurrent()) return;
    phase = "loading";
    document.querySelector("#modal-submit").disabled = true;
    if (refresh) {
      refreshRequired = true;
      const assets = await api(`/api/projects/${projectId}/assets`);
      if (!modalCurrent()) return;
      state.assets = assets;
      targets = remaining.map(id => ({ asset_id: id, expected_revision: assets.find(asset => asset.id === id)?.revision ?? 0 }));
      if (section === "annotate" && remaining.includes(state.asset?.id)) {
        const assetId = state.asset.id;
        const asset = assets.find(item => item.id === assetId);
        if (!asset || asset.revision !== state.asset.revision) {
          const [annotations, events] = asset ? await Promise.all([api(`/api/assets/${assetId}/annotations`), api(`/api/assets/${assetId}/review-events`)]) : [[], []];
          if (!modalCurrent() || state.asset?.id !== assetId) return;
          state.asset = asset || null; state.annotations = annotations; state.selectedAnnotation = null; state.reviewEvents = events;
          if (!asset || asset.lock_owner !== (state.session?.actor || sessionStorage.getItem("visionforge-actor-id"))) {
            clearInterval(state.lockTimer); state.lockTimer = null; state.lockOwned = false; state.readOnly = true;
            state.readOnlyReason = t("图片状态已更新；当前仅供查看，需要校正时请重新打开图片。");
          }
          renderPage();
        }
      }
      refreshRequired = false;
      if (section !== "annotate") renderPage();
    }
    preview = await api(`/api/projects/${projectId}/review/preview`, jsonOptions("POST", { action, assets: targets, ...source }));
    if (!modalCurrent()) return;
    phase = "preview";
    if (jobRun) preview.items.forEach(item => jobRun.results.set(item.asset_id, item));
    const eligible = preview.counts.eligible;
    content.innerHTML = `${scopeMarkup()}${countsMarkup(preview.items)}<p class="hint">${eligible ? t("只会处理下方标记为“可处理”的图片。执行时会再次检查版本和编辑占用，其他图片保持不变。") : options.nextBatch ? t("本批没有可提交图片，可以查看原因后继续预览下一批。已提交或已通过的图片无需重复提交。") : t("当前没有可处理的图片。请查看每张图片的原因；已提交或已通过的图片无需重复提交。")}</p>${action === "reject" ? `<div class="field"><label for="batch-review-reason">${t("修改要求（可选，填写后会显示在每张退回图片的审核记录中）")}</label><textarea id="batch-review-reason" maxlength="2000" placeholder="${ta("例如：右侧目标漏标，请补充；边缘轮廓需要贴近物体。")}">${escapeHtml(reason)}</textarea></div>` : ""}${action === "approve" && eligible ? `<label class="check-line batch-review-confirm"><input type="checkbox" id="batch-review-confirm"/>${t("我已检查这些图片的类别和标注位置，确认可以通过审核")}</label>` : ""}${itemsMarkup(preview.items)}${!eligible && options.nextBatch && preview.items.some(item => item.status === "conflict") ? `<button class="btn ghost job-review-recheck" data-job-review-retry-batch>${t("重新检查本批图片")}</button>` : ""}${priorIssuesMarkup()}`;
    const button = document.querySelector("#modal-submit");
    button.textContent = eligible ? t("{0} {1} 张", [jobRun ? t("提交审核") : ACTIONS[action], eligible]) : options.nextBatch ? t("预览下一批 {0} 张", [options.nextBatchSize]) : preview.items.some(item => item.status === "conflict") ? t("刷新并重新检查") : t("关闭");
    button.disabled = false;
  };
  const syncResult = async items => {
    const success = new Set(items.filter(item => item.status === "succeeded").map(item => item.asset_id));
    remaining = remaining.filter(id => !success.has(id));
    if (!current()) return;
    if (!independent) success.forEach(id => state.selectedAssetIds.delete(id));
    const updates = new Map(items.filter(item => item.asset).map(item => [item.asset_id, item.asset]));
    state.assets = state.assets.map(asset => updates.has(asset.id) ? { ...asset, ...updates.get(asset.id) } : asset);
    state.project.submitted_count = state.assets.filter(asset => asset.review_status === "submitted").length;
    state.project.approved_count = state.assets.filter(asset => asset.review_status === "approved").length;
    if (section === "annotate" && success.has(state.asset?.id)) {
      const assetId = state.asset.id;
      state.asset = state.assets.find(asset => asset.id === assetId);
      // The backend released this actor's lease atomically with the review.
      // Do not send another DELETE, which might disturb a later lease.
      clearInterval(state.lockTimer); state.lockTimer = null; state.lockOwned = false;
      state.readOnly = true; state.tool = "select";
      state.handedOffAssetId = action === "reject" ? assetId : null;
      state.readOnlyReason = action === "reject" ? t("已交给标注员修改，你可以继续审核其他图片。") : action === "submit" ? t("已提交审核，等待审核员确认。") : t("审核已通过，需要修改时请先重新打开。");
      state.reviewEvents = [];
      const [annotations, events] = await Promise.allSettled([api(`/api/assets/${assetId}/annotations`), api(`/api/assets/${assetId}/review-events`)]);
      if (!current() || state.asset?.id !== assetId) return;
      if (annotations.status === "fulfilled") { state.annotations = annotations.value; state.selectedAnnotation = null; }
      if (events.status === "fulfilled") state.reviewEvents = events.value;
      if (annotations.status === "rejected" || events.status === "rejected") notify(t("审核已完成，但部分画布或审核记录刷新失败。请重新打开图片查看最新结果，无需重复审核。"), "warning");
    }
    renderPage();
  };
  openModal({
    title: jobRun ? t("提交本次任务的图片") : ACTIONS[action],
    subtitle: jobRun ? t("提交此任务已处理图片的当前保存标注，包含后续人工校正。") : action === "approve" ? t("仅审核已提交的图片。AI 尚未提交的结果会跳过，请先检查标注质量。") : action === "reject" ? t("修改要求可不填；标注员修正后可以再次提交。") : t("请先检查 AI 标注和必要的人工修正。提交后将等待审核员确认。"),
    wide: true,
    body: `<div id="batch-review-content"><p class="hint" role="status">${t("正在检查所选图片的状态和编辑占用…")}</p></div>`,
    submitText: t("正在检查…"),
    onReady: () => {
      content = document.querySelector("#batch-review-content");
      content.addEventListener("click", event => {
        if (!modalCurrent()) return;
        if (event.target.closest("[data-job-review-retry-batch]")) {
          // Use the modal's own busy/error handling for an explicit recheck.
          phase = "recheck-batch";
          document.querySelector("#modal-submit").click();
        }
        if (event.target.closest("[data-job-review-restart]")) options.restart?.();
      });
      document.querySelector("#modal-submit").disabled = true;
      void showPreview().catch(error => {
        if (!modalCurrent()) return;
        phase = "preview-error";
        content.innerHTML = `<p class="inline-error" role="alert">${t("{0} 尚未执行任何审核操作。", [escapeHtml(error.message)])}</p>`;
        const button = document.querySelector("#modal-submit"); button.disabled = false; button.textContent = t("重新检查");
      });
    },
    onSubmit: async () => {
      if (!modalCurrent()) throw new Error(t("页面已切换，请关闭窗口并在当前项目重新选择图片。"));
      const issue = workspaceReviewProblem(state);
      if (issue) throw new Error(issue);
      if (phase === "recheck-batch") { await showPreview(true); return; }
      if (phase === "result") {
        if (options.nextBatch) { options.nextBatch(); return; }
        if (!remaining.length) { closeModal(); return; }
        await showPreview(true); return;
      }
      if (phase === "preview-error" || phase === "loading") { await showPreview(); return; }
      if (!preview?.counts.eligible) {
        if (options.nextBatch) { options.nextBatch(); return; }
        if (preview?.items.some(item => item.status === "conflict")) { await showPreview(true); return; }
        closeModal(); return;
      }
      if (action === "reject") {
        reason = document.querySelector("#batch-review-reason").value.trim();
      }
      if (action === "approve" && !document.querySelector("#batch-review-confirm").checked) throw new Error(t("请先确认已经检查图片与标注，再批量通过审核。"));
      const eligible = new Set(preview.items.filter(item => item.status === "eligible").map(item => item.asset_id));
      let response;
      const batchToken = {};
      if (section === "annotate") { state.reviewBatchToken = batchToken; state.workflowBusy = true; deps.refreshWorkflowState(); }
      try {
        try {
          response = await api(`/api/projects/${projectId}/review/batch`, jsonOptions("POST", { action, reason, assets: targets.filter(target => eligible.has(target.asset_id)), ...source }));
        } catch (error) {
          phase = "preview-error"; refreshRequired = true;
          if (content.isConnected) document.querySelector("#modal-submit").textContent = t("刷新并重新检查");
          throw error;
        }
        const applied = new Map(response.items.map(item => [item.asset_id, item]));
        const items = preview.items.map(item => applied.get(item.asset_id) || item);
        if (jobRun) {
          items.forEach(item => jobRun.results.set(item.asset_id, item));
          items.filter(item => item.status === "succeeded").forEach(item => jobRun.submitted.add(item.asset_id));
        }
        await syncResult(items);
        if (current()) {
          try { await options.onResult?.(); }
          catch (_) { notify(t("审核操作已完成，任务块进度暂未刷新。稍后点击刷新即可，无需重复提交。"), "warning"); }
        }
        if (!modalCurrent()) { notify(t("批量操作已完成 {0} 张；其他图片未处理，可返回原项目查看。", [response.counts.succeeded])); return; }
        phase = "result";
        const resultNote = jobRun ? t("本批新提交 {0} 张。{1}{2}", [response.counts.succeeded, remaining.length ? t("另外 {0} 张未重复提交，请查看下方原因。", [remaining.length]) : t("本批全部提交完成。"), options.nextBatch ? t("下一批仍需预览并确认后才会提交。") : t("如有需要处理的问题，可处理后重新打开本任务的提交入口。")]) : t("已完成的图片已移出选择。{0}", [remaining.length ? t("剩余 {0} 张仍保持选中；检查原因后可重新检查范围，再决定是否重试。", [remaining.length]) : t("全部处理完成，可以继续查看下一批图片。")]);
        content.innerHTML = `${scopeMarkup()}${countsMarkup(items, true)}<p class="hint">${resultNote}</p>${itemsMarkup(items)}${jobRun && remaining.length && options.nextBatch ? `<button class="btn ghost job-review-recheck" data-job-review-retry-batch>${t("重新检查本批未处理图片")}</button>` : ""}${priorIssuesMarkup()}${jobRun && !options.nextBatch && [...jobRun.results.values()].some(item => item.status !== "succeeded" && !["submitted", "approved"].includes(item.review_status)) ? `<button class="btn ghost job-review-recheck" data-job-review-restart>${t("重新检查本任务的图片")}</button>` : ""}`;
        document.querySelector("#modal-submit").textContent = options.nextBatch ? t("预览下一批 {0} 张", [options.nextBatchSize]) : remaining.length ? jobRun ? t("重新检查本批图片") : t("重新检查未处理图片") : t("完成");
        document.querySelector("#modal-cancel").textContent = section === "annotate" ? t("返回当前图片") : section === "ai" ? t("返回 AI 标注页") : t("返回图片列表");
      } finally {
        if (section === "annotate" && state.reviewBatchToken === batchToken) {
          state.reviewBatchToken = null; state.workflowBusy = false;
          if (current()) { deps.refreshWorkflowState(); deps.refreshInspector(); }
        }
      }
    },
  });
}
