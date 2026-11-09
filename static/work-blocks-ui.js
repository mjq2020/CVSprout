import { t, accountDisplayName, ta } from "./i18n.js?v=0.29.3";
export const BLOCK_STATUS = {
  preparing: t("AI 处理中"), available: t("待领取"), in_progress: t("标注中"),
  submitted: t("待审核"), rejected: t("需修改"), completed: t("已完成"),
};

export function annotationBlockURL(projectId, blockId, assetId = null) {
  const params = new URLSearchParams();
  if (blockId) params.set("block", String(blockId));
  if (assetId) params.set("asset", String(assetId));
  return `#/projects/${projectId}/annotate${params.size ? `?${params}` : ""}`;
}

// Every asset refresh, including the shared review dialog, uses the same scope.
// An explicit block URL survives reloads and browser back/forward navigation.
export function scopeWorkBlockRequest(path, state, method = "GET") {
  if (method.toUpperCase() !== "GET" || state.currentSection !== "annotate" || !state.activeWorkBlockId) return path;
  const [pathname, query = ""] = path.split("?");
  const match = pathname.match(/^\/api\/projects\/(\d+)\/assets$/);
  if (!match || (state.activeWorkBlock?.project_id && Number(match[1]) !== state.activeWorkBlock.project_id)) return path;
  const params = new URLSearchParams(query);
  params.set("work_block_id", String(state.activeWorkBlockId));
  return `${pathname}?${params}`;
}

export function workBlocksMarkup({ state, isAdmin, escapeHtml: esc, icon }) {
  const data = state.workBlocks || { enabled: false, blocks: [], unassigned_count: 0 };
  if (data.unavailable) return `<div class="page-head compact-head"><h1>${t("标注任务块")}</h1></div><div class="empty-state"><strong>${t("任务块功能需要更新后端")}</strong><p>${t("请管理员更新并重启后端服务，再刷新页面。现有项目数据仍可查看。")}</p><a class="btn ghost link-btn" href="#/projects/${state.project.id}/data">${t("查看项目图片")}</a></div>`;
  const all = data.blocks || [], filter = state.workBlockFilter || "mine";
  const mine = all.filter(block => block.is_mine && block.status !== "completed");
  const blocks = all.filter(block => filter === "all" || (filter === "mine" ? block.is_mine && block.status !== "completed" : block.status === filter));
  const canClaim = state.project?.permissions?.edit !== false && !mine.some(block => ["in_progress", "rejected"].includes(block.status));
  const available = all.filter(block => block.status === "available").length;
  const row = block => {
    const closed = block.status === "completed", editable = block.is_mine && !closed;
    return `<article class="work-block-row" data-block-row="${block.id}">
      <div class="work-block-identity"><a href="${annotationBlockURL(state.project.id, block.id)}"><strong>${esc(block.name)}</strong></a><span>${t("{0} 张图片 · {1}{2}", [block.total, esc(accountDisplayName(block.owner_name) || t("尚未领取")), block.is_mine ? t("（我）") : ""])}</span></div>
      <div class="work-block-progress"><span class="work-block-status ${block.status}">${BLOCK_STATUS[block.status] || esc(block.status)}</span><span>${t("已通过 {0} / {1}{2}{3}", [block.approved_count, block.total, block.submitted_count ? t(" · 待审核 {0}", [block.submitted_count]) : "", block.rejected_count ? t(" · 退回 {0}", [block.rejected_count]) : ""])}</span><progress value="${block.approved_count}" max="${block.total || 1}" aria-label="${ta("{0}审核进度", [esc(block.name)])}"></progress></div>
      <div class="work-block-actions"><a class="btn ghost link-btn" href="${annotationBlockURL(state.project.id, block.id)}">${editable ? t("继续标注") : t("查看图片")}</a>${block.status === "available" ? `<button class="btn primary" data-block-claim="${block.id}" ${canClaim ? "" : "disabled"}>${t("领取")}</button>` : ""}${editable && ["in_progress", "rejected"].includes(block.status) ? `<button class="btn primary" data-block-submit="${block.id}">${t("提交审核")}</button>` : ""}${(editable || isAdmin()) && !closed && block.owner_key ? `<button class="btn ghost" data-block-release="${block.id}">${t("交回")}</button>` : ""}${isAdmin() && !closed && block.status !== "preparing" ? `<button class="btn ghost" data-block-assign="${block.id}">${t("分配 / 转交")}</button>` : ""}${isAdmin() && closed ? `<button class="btn ghost" data-block-reopen="${block.id}">${t("重新打开")}</button>` : ""}</div>
    </article>`;
  };
  return `<div class="page-head compact-head"><div><h1>${t("标注任务块")}</h1><p class="page-subtitle">${t("领取一块图片，完成标注后提交审核。全部审核通过后自动完成，保留标注与负责人记录。")}</p></div><div class="page-head-actions"><button class="btn ghost" data-block-refresh>${t("{0}刷新", [icon("refresh")])}</button>${isAdmin() ? `<button class="btn ghost" data-block-create ${data.unassigned_count ? "" : "disabled"}>${t("{0}创建任务块", [icon("layers")])}</button>` : ""}<button class="btn primary" data-block-claim="" ${available && canClaim ? "" : "disabled"}>${t("领取下一块")}</button></div></div>
    <section class="work-block-guide"><p>${data.enabled ? canClaim ? t("整个块提交后可以领取下一块。退回的图片仍由原负责人修改；断网或关闭页面不会丢失任务。") : t("你有正在标注或退回修改的任务，请先完成并提交，或交回后再领取。") : t("此项目尚未启用任务块。管理员创建任务块后，成员先领取，再进入自己的图片队列。")}</p>${data.unassigned_count ? `<p>${t("还有")} <strong>${data.unassigned_count}</strong> ${t("张图片未分块。{0}", [isAdmin() ? t("新建任务块只处理这些图片，已有任务不变。") : t("请联系管理员创建任务块。")])}</p>` : ""}<p class="hint">${t("可以先到")}<a href="#/projects/${state.project.id}/ai">${t("AI 标注")}</a>${t("预标注，再领取任务块；也可以领取后，在自己的任务块内使用 AI 处理当前图片或本块未完成图片。人工标注始终保留，待审核、已通过及他人负责的图片会跳过；AI 处理中不能交回或转交任务块。")}</p></section>
    <div class="work-block-filters" role="group" aria-label="${ta("任务块筛选")}">${[["mine", t("我的任务（{0}）", [mine.length])], ["available", t("待领取（{0}）", [available])], ["submitted", t("待审核")], ["rejected", t("需修改")], ["completed", t("已完成")], ["all", t("全部（{0}）", [all.length])]].map(([value, label]) => `<button class="btn ghost ${filter === value ? "active" : ""}" data-block-filter="${value}" aria-pressed="${filter === value}">${label}</button>`).join("")}</div>
    <section class="work-block-list" aria-label="${ta("任务块列表")}">${blocks.length ? blocks.map(row).join("") : `<div class="empty-state"><strong>${filter === "mine" ? t("当前没有待处理的个人任务") : t("当前筛选没有任务块")}</strong><p>${available ? t("点击“领取下一块”，或切换到“待领取”选择任务。") : data.unassigned_count ? t("等待管理员将新图片分块后即可领取。") : t("可以切换筛选查看其他任务，或点击刷新获取最新进度。")}</p></div>`}</section>`;
}

export function bindWorkBlocks(deps) {
  const { state, api, jsonOptions, openModal, closeModal, notify, escapeHtml: esc, refresh, render, submitBlock } = deps;
  if (state.workBlocks?.unavailable) return;
  const projectId = state.project.id, epoch = state.selectionEpoch, session = state.session;
  const current = () => state.project?.id === projectId && state.currentSection === "blocks" && state.selectionEpoch === epoch && state.session === session;
  const base = `/api/projects/${projectId}/work-blocks`;
  const blockById = id => state.workBlocks.blocks.find(block => block.id === Number(id));
  const handle = action => async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try { await action(button); } catch (error) {
      if (current()) {
        notify(error.message, "error");
        if (error.status === 409) await refresh().catch(() => {});
      }
    }
    finally { if (button.isConnected) button.disabled = false; }
  };
  document.querySelector("[data-block-refresh]").onclick = handle(() => refresh());
  document.querySelectorAll("[data-block-filter]").forEach(button => button.onclick = () => { state.workBlockFilter = button.dataset.blockFilter; render(); });
  document.querySelectorAll("[data-block-claim]").forEach(button => button.onclick = handle(async button => {
    const block = await api(`${base}/claim`, jsonOptions("POST", { ...(button.dataset.blockClaim ? { block_id: Number(button.dataset.blockClaim) } : {}) }));
    if (!current()) return;
    notify(t("已领取{0}，这块图片由你负责。", [block.name]));
    location.hash = annotationBlockURL(projectId, block.id);
  }));
  document.querySelector("[data-block-create]")?.addEventListener("click", () => {
    openModal({ title: t("将未分块的图片创建为任务块"), subtitle: t("图片和标注保留在原项目；每张图片只属于一个任务块。"), submitText: t("创建任务块"), body: `<div class="field"><label for="block-size">${t("每块图片数")}</label><input type="number" id="block-size" min="1" max="500" step="1" value="50" autofocus/><p class="hint">${t("默认 50 张，最多 500 张。最后一块可以少于指定数量。")}</p></div><p>${t("本次处理 {0} 张未分块图片，已有任务块保持不变。", [state.workBlocks.unassigned_count])}</p><p class="hint">${t("如需 AI 预标注，请先启动 AI 任务。运行中的图片会等待 AI 结束后再开放领取。")}</p>`, onSubmit: async () => {
      if (!current()) throw new Error(t("页面已切换，请重新打开创建窗口。"));
      const size = Number(document.querySelector("#block-size").value);
      if (!Number.isInteger(size) || size < 1 || size > 500) throw new Error(t("每块图片数需为 1 至 500 的整数。"));
      const result = await api(base, jsonOptions("POST", { block_size: size }));
      if (!current()) return;
      state.workBlocksRequest += 1;
      state.workBlocks = result; state.workBlockFilter = "all"; closeModal(); render();
      notify(t("已创建 {0} 个任务块。", [result.created_count]));
    } });
  });
  document.querySelectorAll("[data-block-submit]").forEach(button => button.onclick = handle(button => submitBlock(blockById(button.dataset.blockSubmit))));
  for (const action of ["release", "assign", "reopen"]) {
    document.querySelectorAll(`[data-block-${action}]`).forEach(button => button.onclick = handle(async button => {
      const block = blockById(button.dataset[`block${action[0].toUpperCase()}${action.slice(1)}`]);
      if (!block) return;
      const users = action === "assign" ? await api(`${base}/assignees`) : [];
      if (!current()) return;
      const title = { release: t("交回任务块"), assign: t("分配或转交任务块"), reopen: t("重新打开已完成任务块") }[action];
      openModal({ title, subtitle: block.name, submitText: title, body: `${action === "assign" ? `<div class="field"><label for="block-assignee">${t("接收成员")}</label><select id="block-assignee">${users.map(user => `<option value="${user.id}" ${user.id === block.owner_user_id ? "selected" : ""}>${esc(accountDisplayName(user.display_name) || user.username)}${t("（")}${esc(user.username)}${t("）")}</option>`).join("")}</select></div>` : ""}<p>${{ release: t("已保存的标注和审核结果会保留。交回后其他成员可以领取尚未完成的工作，你将失去修改权限。"), assign: t("标注和审核记录会保留。原负责人的编辑权限将被收回，新负责人继续完成这块图片。"), reopen: t("本块所有已通过图片将重新进入标注流程，需要重新提交审核；现有标注仍然保留。") }[action]}</p><div class="field"><label for="block-reason">${action === "reopen" ? t("重新打开的原因（必填）") : t("交接说明（可选）")}</label><textarea id="block-reason" maxlength="2000" placeholder="${ta("说明接下来需要处理的内容")}"></textarea></div>`, onSubmit: async () => {
        if (!current()) throw new Error(t("页面已切换，请重新打开任务块。"));
        const reason = document.querySelector("#block-reason").value.trim();
        if (action === "reopen" && !reason) throw new Error(t("请填写重新打开的原因。"));
        const userId = action === "assign" ? Number(document.querySelector("#block-assignee").value) : null;
        if (action === "assign" && !userId) throw new Error(t("没有可分配的成员，请先在用户管理中创建独立账号。"));
        await api(`${base}/${block.id}/${action}`, jsonOptions("POST", { expected_revision: block.revision, reason, ...(action === "assign" ? { user_id: userId } : {}) }));
        if (!current()) return;
        closeModal(); await refresh(); notify(t("{0}已完成。", [title]));
      } });
    }));
  }
}
