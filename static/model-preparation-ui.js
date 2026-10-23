// Model files and inference readiness are separate: a completed download may
// still need runtime dependencies before preview or annotation can run.
export function modelDownloadSize(bytes) {
  const value = Number(bytes);
  if (!(value > 0)) return "大小待确认";
  return value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(1)} GiB` : `${(value / 1024 ** 2).toFixed(1)} MiB`;
}

export function annotationModelSelectable(model, allowPrepare = true) {
  return Boolean(model?.enabled && (model.configured || model.preparation?.mode === "manual" || (allowPrepare && model.preparation?.mode === "download" && model.preparation.downloadable)));
}

export function modelPreparationStatus(model) {
  const prep = model?.preparation, state = prep?.state || {};
  if (!model?.enabled) return "模型已停用";
  if (model.configured) return "模型已就绪";
  if (state.status === "downloading") {
    const total = Number(state.total || prep.size_bytes), downloaded = Math.max(0, Number(state.downloaded) || 0);
    return `正在后台下载${total > 0 ? ` ${Math.min(100, Math.floor(downloaded / total * 100))}% · ${downloaded ? modelDownloadSize(downloaded) : "0 MB"} / ${modelDownloadSize(total)}` : ""}，完成后检查推理环境。`;
  }
  if (["failed", "interrupted"].includes(state.status)) return state.error || (state.status === "failed" ? "下载失败，请重试。" : "下载已中断，可以继续下载。");
  if (prep?.installed) return model.availability_error ? `已检测到模型文件；${model.availability_error}` : "已检测到模型文件，推理环境尚未就绪，请重新检查。";
  if (prep?.mode === "manual") return prep.missing_files?.length ? `请手动放置模型文件，当前缺少 ${prep.missing_files.length} 项。` : model.availability_error || "请按下方目录准备模型文件和推理依赖，再重新检查。";
  if (prep?.mode === "download" && prep.downloadable) return `首次使用需要下载 ${modelDownloadSize(prep.size_bytes)} 权重。下载在后台继续，可以离开页面。`;
  return model?.availability_error || "模型尚未配置，请联系管理员。";
}

export function modelPreparationDetails(model, escapeHtml) {
  const prep = model?.preparation;
  if (!prep) return "";
  const missing = new Set(prep.missing_files || []);
  let source = "";
  try { const url = new URL(prep.source_url); if (["http:", "https:"].includes(url.protocol)) source = `<p><a href="${escapeHtml(url.href)}" target="_blank" rel="noopener noreferrer">查看官方模型来源</a></p>`; } catch (_) { /* No usable public source link. */ }
  return `${prep.instructions ? `<p>${escapeHtml(prep.instructions)}</p>` : ""}${prep.directory ? `<p>模型目录：<code>${escapeHtml(prep.directory)}</code></p>` : ""}${prep.files?.length ? `<ul>${prep.files.map(file => `<li><code>${escapeHtml(file)}</code>${missing.has(file) ? " <span>缺失</span>" : ""}</li>`).join("")}</ul>` : ""}${source}`;
}

export function mountModelPreparation(container, { model: initialModel, api, escapeHtml, isCurrent = () => true, canPrepare = () => false, onChange = () => {}, pollMs = 1500 }) {
  let model = initialModel, disposed = false, pending = false, preparing = false, queuedPrepare = false, timer = null, requestController = null, error = "", detailsMarkup = "";
  const live = () => !disposed && container.isConnected && isCurrent();
  container.innerHTML = `<div class="model-preparation"><p class="model-preparation-status" role="status" aria-live="polite" data-preparation-status></p><p class="model-preparation-error" role="alert" data-preparation-error hidden></p><details data-preparation-details><summary>模型文件与使用条件</summary><div data-preparation-files></div></details><div class="model-preparation-actions"><button type="button" class="btn ghost" data-preparation-start hidden>下载并准备</button><button type="button" class="mini-action" data-preparation-refresh>重新检查</button></div></div>`;
  const query = selector => container.querySelector(selector);
  const schedule = () => {
    clearTimeout(timer); timer = null;
    if (live() && !pending && model.preparation?.state?.status === "downloading") timer = setTimeout(() => { void refresh(); }, pollMs);
  };
  function render() {
    if (!live()) return;
    const prep = model.preparation;
    const downloading = prep?.state?.status === "downloading";
    query("[data-preparation-status]").textContent = pending && !downloading ? "正在检查模型准备状态…" : modelPreparationStatus(model);
    const failure = query("[data-preparation-error]"); failure.textContent = error; failure.hidden = !error;
    const details = modelPreparationDetails(model, escapeHtml);
    query("[data-preparation-details]").hidden = !details;
    if (details !== detailsMarkup) { query("[data-preparation-files]").innerHTML = details; detailsMarkup = details; }
    const start = query("[data-preparation-start]");
    start.hidden = !(canPrepare() && model.enabled && !model.configured && prep?.mode === "download" && prep.downloadable && !prep.installed);
    start.disabled = pending || downloading;
    start.textContent = downloading ? "后台下载中…" : ["failed", "interrupted"].includes(prep?.state?.status) ? "重试下载" : `下载并准备 · ${modelDownloadSize(prep?.size_bytes)}`;
    query("[data-preparation-refresh]").disabled = pending;
  }
  async function request(prepare) {
    if (!live()) return;
    if (prepare && (!canPrepare() || !model.enabled || model.configured || model.preparation?.mode !== "download" || !model.preparation.downloadable || model.preparation.installed || model.preparation.state?.status === "downloading")) return;
    if (pending) { if (prepare && !preparing) queuedPrepare = true; return; }
    pending = true; preparing = prepare; error = ""; clearTimeout(timer); render();
    requestController = new AbortController();
    try {
      const updated = await api(`/api/models/${encodeURIComponent(model.id)}/${prepare ? "prepare" : "preparation"}`, { method: prepare ? "POST" : "GET", signal: requestController.signal });
      if (!live()) return;
      if (updated?.id !== model.id) throw new Error("未收到当前模型的准备状态，请重新检查。");
      const previous = model; model = updated;
      onChange(model, previous);
    } catch (problem) { if (live() && !requestController.signal.aborted) error = problem.message || "模型状态检查失败，请重试。"; }
    finally { if (live()) { pending = false; preparing = false; requestController = null; render(); if (queuedPrepare) { queuedPrepare = false; void request(true); } else if (!error) schedule(); } }
  }
  const refresh = () => request(false), prepare = () => request(true);
  query("[data-preparation-start]").onclick = prepare;
  query("[data-preparation-refresh]").onclick = refresh;
  render(); schedule();
  if (model.preparation?.mode === "manual" && !model.preparation.installed) query("[data-preparation-details]").open = true;
  return { refresh, prepare, getModel: () => model, dispose() { disposed = true; clearTimeout(timer); requestController?.abort(); } };
}
