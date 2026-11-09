import { t, getLocale } from "./i18n.js?v=0.29.3";
const summaries = new Map();

// Overview reads only a small summary; opening a record loads its full details.
export function mountOverviewTraining({ projectId, userId, api, escapeHtml, statusLabel, isCurrent }) {
  const host = document.querySelector("#overview-training-content");
  if (!host) return;
  const key = `${userId}:${projectId}`;
  const base = `#/projects/${projectId}/train`;
  const current = () => host.isConnected && isCurrent();
  const number = value => Number(value || 0).toLocaleString(getLocale());
  const render = summary => {
    const total = Number(summary.total || 0), counts = summary.counts || {};
    // The navigation already provides an entry to training. An empty overview
    // should focus on preparing data, without four zero-valued counters.
    host.closest('.overview-training').hidden = !total;
    if (!total) { host.replaceChildren(); return; }
    host.innerHTML = `<div class="overview-training-counts"><div><strong>${number(total)}</strong><span>${t("累计训练")}</span></div><div><strong>${number(counts.active)}</strong><span>${t("进行中")}</span></div><div><strong>${number(counts.completed)}</strong><span>${t("已完成")}</span></div><div><strong>${number(counts.failed)}</strong><span>${t("失败")}</span></div></div>${total ? `<div class="overview-training-recent">${(summary.recent || []).map(run => {
      const date = new Date(run.created_at);
      const time = Number.isNaN(date.getTime()) ? "" : date.toLocaleString(getLocale(), { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
      const status = ["queued", "running", "completed", "failed", "cancelled"].includes(run.status) ? run.status : "unknown";
      return `<a class="overview-training-link" href="${base}?run=${Number(run.id)}"><span><strong>${escapeHtml(run.name || t("训练 {0}", [run.id]))}</strong><small>${t("{0} · {1} / {2} 轮", [escapeHtml(time), number(run.current_epoch), number(run.total_epochs)])}</small></span><span class="overview-training-status ${status}">${escapeHtml(statusLabel(run.status))}</span></a>`;
    }).join("")}</div><a class="overview-training-all" href="${base}">${t("查看全部 {0} 次训练 →", [number(total)])}</a>` : `<p class="overview-training-empty">${t("还没有训练记录。生成数据版本后，即可开始第一次训练。")}</p><a class="overview-training-all" href="${base}">${t("前往训练页面 →")}</a>`}`;
  };
  const refresh = async (force = false) => {
    if (!current()) return;
    const cached = summaries.get(key);
    if (!force && cached?.data && Date.now() - cached.at < 10000) {
      render(cached.data);
      if (cached.data.counts?.active) setTimeout(() => refresh(true), 10000);
      return;
    }
    if (document.hidden) { setTimeout(() => refresh(force), 10000); return; }
    const entry = cached || {};
    if (!entry.pending) {
      entry.pending = api(`/api/projects/${projectId}/training-summary`);
      summaries.set(key, entry);
    }
    const pending = entry.pending;
    try {
      const summary = await pending;
      entry.data = summary; entry.at = Date.now();
      if (!current()) return;
      render(summary);
      if (summary.counts?.active) setTimeout(() => refresh(true), 10000);
    } catch (_) {
      if (!current()) return;
      host.innerHTML = `<div class="empty-compact" role="status">${t("训练记录暂时无法加载。")}<button type="button" class="mini-action" id="retry-overview-training">${t("重新加载")}</button></div>`;
      host.querySelector("#retry-overview-training")?.addEventListener("click", () => refresh(true));
    } finally {
      if (entry.pending === pending) entry.pending = null;
    }
  };
  refresh();
}
