// Browsing preferences belong to a project and survive a live list refresh.
const histories = new Map();
let historySession;

export function resetHistoryNavigation(projectId, kind) {
  histories.delete(`${projectId}:${kind}`);
}

export function historyPageNumbers(page, pages) {
  if (pages <= 7) return Array.from({ length: pages }, (_, index) => index + 1);
  const visible = new Set([1, pages, page - 1, page, page + 1]);
  const numbers = [...visible].filter(value => value >= 1 && value <= pages).sort((a, b) => a - b);
  return numbers.flatMap((value, index) => index && value - numbers[index - 1] > 1 ? [null, value] : [value]);
}

export function createHistoryNavigation({ projectId, kind, session, records, pageSize, searchText, statuses = [], escapeHtml }) {
  if (historySession !== session) { histories.clear(); historySession = session; }
  const key = `${projectId}:${kind}`;
  const previous = document.querySelector(`[data-history-key="${key}"]`);
  // Replacing a composing input interrupts Chinese input methods. Its compositionend
  // handler renders the latest records, including any polling updates deferred here.
  if (previous?.querySelector('[data-history-search][data-composing="true"]')) return null;
  if (!histories.has(key)) histories.set(key, { query: "", status: "all", page: 1 });
  const preferences = histories.get(key);
  const query = preferences.query.trim().toLocaleLowerCase();
  const terms = query.split(/\s+/).filter(Boolean);
  const matching = records.filter(record => (preferences.status === "all" || record.status === preferences.status)
    && terms.every(term => searchText(record).toLocaleLowerCase().includes(term)));
  const pages = Math.max(1, Math.ceil(matching.length / pageSize));
  preferences.page = Math.max(1, Math.min(preferences.page, pages));
  const start = (preferences.page - 1) * pageSize;
  const filtered = Boolean(query || preferences.status !== "all");
  const active = previous?.contains(document.activeElement) ? document.activeElement : null;
  const focus = active?.dataset.historyFocus ? { key: active.dataset.historyFocus, start: active.selectionStart, end: active.selectionEnd, direction: active.selectionDirection } : null;
  const activeRecord = active?.closest('[data-history-record]');
  const recordFocus = activeRecord ? { id: activeRecord.dataset.historyRecord, tag: active.tagName,
    attributes: [...active.attributes].filter(attribute => attribute.name.startsWith('data-') || attribute.name === 'href').map(attribute => [attribute.name, attribute.value]),
    index: [...activeRecord.querySelectorAll(active.tagName)].indexOf(active) } : null;
  const scroll = previous ? { top: previous.closest('.project-page')?.scrollTop || 0, nav: document.querySelector('.project-subnav')?.scrollLeft || 0 } : null;
  const expanded = new Map([...previous?.querySelectorAll('[data-history-record]') || []].map(record => [record.dataset.historyRecord, [...record.querySelectorAll('details')].map(detail => detail.open)]));
  const noun = kind === "ai" ? "条任务" : "个版本";
  const label = kind === "ai" ? "任务历史" : "数据版本";
  const count = filtered ? `找到 ${matching.length} ${noun}，共 ${records.length} ${noun}` : `共 ${records.length} ${noun}`;
  const range = matching.length ? `当前显示 ${start + 1}–${Math.min(start + pageSize, matching.length)}` : "暂无匹配结果";
  const navigation = (position) => matching.length > pageSize ? `<nav class="history-pagination" aria-label="${label}分页"><span class="history-page-label">第 ${preferences.page} / ${pages} 页</span><div class="history-page-buttons"><button type="button" class="btn ghost" data-history-page="${preferences.page - 1}" data-history-focus="${position}-previous" aria-label="${label}上一页" ${preferences.page === 1 ? "disabled" : ""}>上一页</button>${historyPageNumbers(preferences.page, pages).map(page => page === null ? '<span class="history-ellipsis" aria-hidden="true">…</span>' : `<button type="button" class="btn ghost history-page-number" data-history-page="${page}" data-history-focus="${position}-page-${page}" aria-label="${label}第 ${page} 页" ${page === preferences.page ? 'aria-current="page"' : ""}>${page}</button>`).join("")}<button type="button" class="btn ghost" data-history-page="${preferences.page + 1}" data-history-focus="${position}-next" aria-label="${label}下一页" ${preferences.page === pages ? "disabled" : ""}>下一页</button></div></nav>` : "";
  return {
    items: matching.slice(start, start + pageSize),
    attributes: `data-history-key="${key}" data-history-kind="${kind}"`,
    toolbar: `<div class="history-toolbar"><label class="history-search"><span>${kind === "ai" ? "搜索任务" : "搜索版本"}</span><input type="search" data-history-search data-history-focus="search" aria-label="${kind === "ai" ? "搜索 AI 任务" : "搜索数据版本"}" placeholder="${kind === "ai" ? "模型名称或任务编号" : "版本名称、编号或说明"}" value="${escapeHtml(preferences.query)}" autocomplete="off" /></label>${statuses.length ? `<label class="history-filter"><span>任务状态</span><select data-history-status data-history-focus="status" aria-label="筛选任务状态"><option value="all" ${preferences.status === "all" ? "selected" : ""}>全部状态</option>${statuses.map(([value, title]) => `<option value="${value}" ${preferences.status === value ? "selected" : ""}>${title}</option>`).join("")}</select></label>` : ""}<button type="button" class="btn ghost history-clear" data-history-clear data-history-focus="clear" ${preferences.query || preferences.status !== "all" ? "" : "disabled"}>清除筛选</button></div><div class="history-list-summary"><p role="status" aria-live="polite"><strong>${count}</strong><span>${range} · 每页 ${pageSize} ${noun}</span></p>${navigation("top")}</div>`,
    footer: pages > 1 ? `<div class="history-footer">${navigation("bottom")}</div>` : "",
    empty: `<div class="empty-state history-no-results"><strong>没有匹配的${kind === "ai" ? "任务" : "数据版本"}</strong><p>试试其他关键词${statuses.length ? "或任务状态" : ""}，也可以清除筛选查看全部。</p><button type="button" class="btn ghost" data-history-clear data-history-focus="empty-clear">清除筛选</button></div>`,
    bind(root, render) {
      const container = root.querySelector(`[data-history-key="${key}"]`);
      if (!container) return;
      const input = container.querySelector('[data-history-search]');
      input.addEventListener('compositionstart', () => { input.dataset.composing = "true"; });
      const search = () => { preferences.query = input.value; preferences.page = 1; render(); };
      input.addEventListener('compositionend', () => { delete input.dataset.composing; search(); });
      input.addEventListener('input', event => { if (!event.isComposing && input.dataset.composing !== "true") search(); });
      input.addEventListener('blur', () => {
        if (input.dataset.composing !== "true") return;
        delete input.dataset.composing;
        preferences.query = input.value; preferences.page = 1;
        // Clicking a route must finish before considering a deferred render.
        queueMicrotask(() => { if (container.isConnected) render(); });
      });
      container.querySelector('[data-history-status]')?.addEventListener('change', event => { preferences.status = event.target.value; preferences.page = 1; render(); });
      container.querySelectorAll('[data-history-clear]').forEach(button => button.onclick = () => {
        preferences.query = ""; preferences.status = "all"; preferences.page = 1; render();
        root.querySelector(`[data-history-key="${key}"] [data-history-search]`)?.focus({ preventScroll: true });
      });
      container.querySelectorAll('[data-history-page]').forEach(button => button.onclick = () => {
        preferences.page = Math.max(1, Math.min(pages, Number(button.dataset.historyPage))); render();
        const updated = root.querySelector(`[data-history-key="${key}"]`);
        updated?.scrollIntoView({ block: "start", behavior: "auto" });
        updated?.querySelector('[aria-current="page"]')?.focus({ preventScroll: true });
      });
      container.querySelectorAll('[data-history-record]').forEach(record => {
        const details = expanded.get(record.dataset.historyRecord);
        if (details) record.querySelectorAll('details').forEach((detail, index) => { detail.open = Boolean(details[index]); });
      });
      if (focus) {
        const target = [...container.querySelectorAll('[data-history-focus]')].find(element => element.dataset.historyFocus === focus.key);
        if (target && !target.disabled) {
          target.focus({ preventScroll: true });
          if (typeof target.setSelectionRange === "function" && focus.start !== null) target.setSelectionRange(focus.start, focus.end, focus.direction || "none");
        }
      } else if (recordFocus?.index >= 0) {
        const record = [...container.querySelectorAll('[data-history-record]')].find(element => element.dataset.historyRecord === recordFocus.id);
        const candidates = [...record?.querySelectorAll(recordFocus.tag) || []];
        const target = recordFocus.attributes.length ? candidates.find(element => recordFocus.attributes.every(([name, value]) => element.getAttribute(name) === value)) : candidates[recordFocus.index];
        if (target && !target.disabled) target.focus({ preventScroll: true });
      }
      if (scroll) {
        const page = container.closest('.project-page'), nav = root.querySelector('.project-subnav');
        if (page) page.scrollTop = scroll.top;
        if (nav) nav.scrollLeft = scroll.nav;
      }
    },
  };
}
