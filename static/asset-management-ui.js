// Keep destructive data actions separate from annotation and review controls.
let context;
let bound = false;
const LIMIT = 500;
const STATUS = { eligible: "可删除", succeeded: "已删除", conflict: "暂不可删除", forbidden: "权限不足", not_found: "已不存在", skipped: "已跳过" };

export function bindAssetManagement(dependencies) {
  context = dependencies;
  if (bound) return;
  bound = true;
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-delete-asset], #delete-selected-assets");
    if (!button || button.disabled || context.state.currentSection !== "data") return;
    const ids = button.dataset.deleteAsset ? [Number(button.dataset.deleteAsset)] : [...context.state.selectedAssetIds];
    void openDeleteAssets(ids);
  });
}

async function openDeleteAssets(ids) {
  const { state, api, jsonOptions, isAdmin, escapeHtml, openModal, closeModal, notify, renderDataPage } = context;
  if (!isAdmin()) return notify("删除导入图片需要管理员权限。", "warning");
  ids = [...new Set(ids)];
  if (!ids.length || ids.length > LIMIT) return notify(`请选择 1 至 ${LIMIT} 张图片后删除。`, "warning");
  const projectId = state.project.id, epoch = state.selectionEpoch;
  const current = () => state.project?.id === projectId && state.currentSection === "data" && state.selectionEpoch === epoch;
  let content, phase = "loading", preview, remaining = [...ids];
  let targets = ids.map(id => ({ asset_id: id, expected_revision: state.assets.find(asset => asset.id === id)?.revision ?? 0 }));
  const modalCurrent = () => current() && content?.isConnected;
  const submit = () => content?.closest(".modal")?.querySelector("#modal-submit");
  const labelButton = (label, disabled = false, destructive = false) => {
    const button = submit();
    if (!button) return;
    button.textContent = label; button.disabled = disabled;
    button.classList.toggle("delete-confirm-button", destructive);
  };
  const itemsMarkup = items => `<div class="asset-delete-items" role="list">${items.map(item => `<div class="asset-delete-item ${item.status}" role="listitem"><div><strong>${escapeHtml(item.filename || `图片 #${item.asset_id}`)}</strong><span>${escapeHtml(item.message || "")}${Number.isInteger(item.annotation_count) ? ` · ${item.annotation_count} 个标注对象` : ""}</span></div><span>${STATUS[item.status] || "未完成"}</span></div>`).join("")}</div>`;
  const refreshAssets = async () => {
    const [assets, project] = await Promise.all([api(`/api/projects/${projectId}/assets`), api(`/api/projects/${projectId}`)]);
    if (!current()) return false;
    state.assets = assets; state.project = project;
    const available = new Set(assets.map(asset => asset.id));
    state.selectedAssetIds = new Set([...state.selectedAssetIds].filter(id => available.has(id)));
    renderDataPage();
    return true;
  };
  const loadPreview = async (refresh = false) => {
    if (!modalCurrent()) return;
    phase = "loading";
    labelButton("正在检查…", true);
    content.innerHTML = '<p role="status">正在检查图片、标注和编辑占用…</p>';
    try {
      if (refresh) {
        if (!(await refreshAssets()) || !modalCurrent()) return;
        targets = remaining.map(id => ({ asset_id: id, expected_revision: state.assets.find(asset => asset.id === id)?.revision ?? 0 }));
      }
      preview = await api(`/api/projects/${projectId}/assets/delete-preview`, jsonOptions("POST", { assets: targets }));
      if (!modalCurrent()) return;
      const eligible = preview.items.filter(item => item.status === "eligible");
      const count = eligible.length, annotations = eligible.reduce((sum, item) => sum + (item.annotation_count || 0), 0);
      content.innerHTML = `<div class="asset-delete-summary" role="status"><strong>${count ? `将删除 ${count} 张图片及 ${annotations} 个标注对象` : "当前没有可删除的图片"}</strong><p>${count ? "删除后，图片及其当前标注和审核记录将从本项目移除，无法在此恢复。" : "请查看下方原因；可以关闭窗口调整选择，或稍后重新检查。"}</p><p>已有数据版本及使用这些版本的训练记录会保留。</p>${count < preview.items.length ? `<p>${preview.items.length - count} 张图片暂不可删除，不会影响其余图片的处理。</p>` : ""}</div>${itemsMarkup(preview.items)}`;
      phase = "preview";
      labelButton(count ? `确认删除 ${count} 张` : "重新检查", false, Boolean(count));
    } catch (error) {
      if (!modalCurrent()) return;
      phase = "retry"; labelButton("重新检查");
      content.innerHTML = `<div class="inline-error" role="alert">${escapeHtml(error.message)}</div>`;
    }
  };
  openModal({
    title: ids.length === 1 ? "删除导入图片" : `删除所选 ${ids.length} 张图片`,
    subtitle: "仅管理员可以删除。确认前请核对图片范围和标注数量。",
    body: '<div id="asset-delete-content"></div>', wide: true, submitText: "正在检查…",
    onReady: () => { content = document.querySelector("#asset-delete-content"); void loadPreview(); },
    onSubmit: async () => {
      if (!modalCurrent()) throw new Error("页面已切换，请关闭窗口并在当前项目重新选择图片。");
      if (phase === "done") return closeModal();
      if (["retry", "result"].includes(phase) || (phase === "preview" && !preview.items.some(item => item.status === "eligible"))) return loadPreview(true);
      if (phase !== "preview") return;
      const eligible = preview.items.filter(item => item.status === "eligible");
      const requests = eligible.map(item => targets.find(target => target.asset_id === item.asset_id));
      phase = "deleting";
      try {
        const result = await api(`/api/projects/${projectId}/assets/delete-batch`, jsonOptions("POST", { assets: requests }));
        if (!modalCurrent()) return;
        const succeeded = new Set(result.items.filter(item => item.status === "succeeded").map(item => item.asset_id));
        const attempted = new Set(result.items.map(item => item.asset_id));
        const items = [...result.items, ...preview.items.filter(item => !attempted.has(item.asset_id))];
        remaining = remaining.filter(id => !succeeded.has(id));
        succeeded.forEach(id => state.selectedAssetIds.delete(id));
        state.assets = state.assets.filter(asset => !succeeded.has(asset.id));
        renderDataPage();
        content.innerHTML = `<div class="asset-delete-summary" role="status"><strong>${succeeded.size ? `已删除 ${succeeded.size} 张图片` : "本次没有删除图片"}</strong><p>${remaining.length ? `还有 ${remaining.length} 张未删除。查看原因后，可重新检查剩余图片；再次删除前会重新预览。` : "图片列表已更新，可以继续导入或标注。"}</p></div>${itemsMarkup(items)}`;
        phase = remaining.length ? "result" : "done";
        labelButton(remaining.length ? "重新检查剩余图片" : "完成");
        content.closest(".modal").querySelector("#modal-cancel").textContent = "返回图片列表";
        if (succeeded.size) notify(`已删除 ${succeeded.size} 张图片及其当前标注。`);
        try { await refreshAssets(); }
        catch (error) {
          if (modalCurrent()) content.insertAdjacentHTML("beforeend", `<p class="inline-error">删除结果已返回，但列表刷新失败：${escapeHtml(error.message)} 请关闭窗口后刷新页面。</p>`);
        }
      } catch (error) {
        if (!modalCurrent()) return;
        phase = "retry"; labelButton("刷新并重新检查");
        throw error;
      }
    },
  });
}
