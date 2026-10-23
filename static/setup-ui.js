import { modelDownloadSize, modelPreparationDetails } from "./model-preparation-ui.js?v=0.25.2";

export function mountSetup(container, { escapeHtml, onComplete }) {
  container.innerHTML = `<main class="login-shell setup-shell"><section class="login-card"><h1>欢迎使用 CVSprout</h1><p>先创建管理员，再导入数据开始标注与训练。官方训练权重会在任务执行时按需下载。</p><form id="setup-form">
    <label>初始化口令<input name="code" autocomplete="off" required minlength="16" placeholder="复制启动终端显示的口令"></label>
    <label>管理员账号<input name="username" autocomplete="username" required minlength="2" maxlength="64" pattern="[a-zA-Z0-9][a-zA-Z0-9_.-]+"></label>
    <label>显示名称<input name="display_name" autocomplete="name" value="管理员" required maxlength="80"></label>
    <label>密码<input name="password" type="password" autocomplete="new-password" required minlength="12" maxlength="256"></label>
    <label>确认密码<input name="confirm" type="password" autocomplete="new-password" required minlength="12" maxlength="256"></label>
    <p class="setup-feedback" role="status" aria-live="polite"></p><button class="btn primary" type="submit">创建管理员</button>
    <p>忘记初始化口令？在安装目录运行 <code>./start.sh --status</code>，Windows 使用 <code>.\\start.ps1 -Status</code>。</p>
  </form></section></main>`;
  const form = container.querySelector('form');
  const feedback = form.querySelector('[role="status"]');
  form.onsubmit = async event => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form));
    if (values.password !== values.confirm) { feedback.textContent = '两次密码不一致。'; return; }
    delete values.confirm;
    const button = form.querySelector('button');
    button.disabled = true; feedback.textContent = '正在创建管理员…';
    try {
      const response = await fetch('/api/setup', { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-MDP-Client': 'browser' }, body: JSON.stringify(values) });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '请检查账号格式和密码长度。');
      if (form.isConnected) onComplete();
    } catch (error) { if (form.isConnected) feedback.textContent = error.message; }
    finally { if (form.isConnected) button.disabled = false; }
  };
  form.querySelector('input').focus();
}

export function mountModelDownloads(container, { api, escapeHtml, isCurrent = () => true }) {
  const statusNames = { downloading: '下载中', complete: '已校验', failed: '下载失败', interrupted: '下载中断', missing: '文件缺失' };
  let disposed = false, timer = null, loading = false, actionPending = false, signature = '', models = [], controller = null;
  const fileMarkup = new WeakMap();
  const live = () => !disposed && container.isConnected && isCurrent();
  const manual = model => model.download_policy === 'manual' || model.preparation?.mode === 'manual';
  container.innerHTML = '<p>官方训练模型可在新建训练时按需下载；Qwen 和检测模型也可在这里提前准备。SAM 系列请按指定目录手动放置文件。下载保存在持久化存储中，关闭窗口后仍会继续。</p><p role="alert" data-download-error hidden></p><div class="model-download-list"></div>';
  const errorNode = container.querySelector('[data-download-error]'), list = container.querySelector('.model-download-list');
  const schedule = () => {
    clearTimeout(timer); timer = null;
    if (live() && models.some(model => !manual(model) && (model.state || model.preparation?.state)?.status === 'downloading')) timer = setTimeout(() => { void refresh(); }, 1500);
  };
  const render = () => {
    if (!live()) return;
    const nextSignature = JSON.stringify(models.map(model => [model.id, manual(model)]));
    if (nextSignature !== signature) {
      list.innerHTML = models.map(model => `<article data-download-row="${escapeHtml(model.id)}" ${manual(model) ? 'class="model-download-manual"' : ''}><div><strong>${escapeHtml(model.name)}</strong><p data-download-size></p><p role="status" data-download-status></p>${manual(model) ? '<details class="model-preparation" open><summary>手动准备目录与文件</summary><div data-download-files></div></details>' : ''}</div><button type="button" class="btn" data-download-action="${escapeHtml(model.id)}">${manual(model) ? '重新检查文件' : '下载模型'}</button></article>`).join('');
      signature = nextSignature;
    }
    list.querySelectorAll('[data-download-row]').forEach(row => {
      const model = models.find(item => item.id === row.dataset.downloadRow), prep = model.preparation, state = model.state || prep?.state || {};
      const total = Number(state.total || model.size_bytes || prep?.size_bytes), busy = !manual(model) && state.status === 'downloading';
      row.querySelector('[data-download-size]').textContent = `${manual(model) ? '仅手动准备权重' : modelDownloadSize(model.size_bytes || prep?.size_bytes)}${model.license ? ` · ${model.license}` : ''}`;
      row.querySelector('[data-download-status]').textContent = manual(model) ? prep?.missing_files?.length ? `缺少 ${prep.missing_files.length} 项文件，请按下方说明放置。` : '文件检查完成；是否可以推理请在模型服务卡片重新检查。' : (state.error || statusNames[state.status] || (model.installed ? '文件已存在' : '尚未下载')) + (busy && total > 0 ? ` ${Math.min(100, Math.floor(100 * (Number(state.downloaded) || 0) / total))}%` : '');
      if (manual(model)) {
        const files = row.querySelector('[data-download-files]'), markup = modelPreparationDetails({ preparation: prep }, escapeHtml);
        if (fileMarkup.get(files) !== markup) { files.innerHTML = markup; fileMarkup.set(files, markup); }
      }
      const button = row.querySelector('[data-download-action]');
      button.disabled = busy || actionPending;
      button.textContent = manual(model) ? '重新检查文件' : busy ? '后台下载中…' : model.installed ? '校验模型' : ['failed', 'interrupted'].includes(state.status) ? '重试下载' : '下载模型';
      button.onclick = async () => {
        if (!live() || actionPending) return;
        if (manual(model)) { await refresh(); return; }
        actionPending = true; render();
        try { await api(`/api/model-downloads/${encodeURIComponent(model.id)}`, { method: 'POST' }); if (live()) { if (loading) await loading; await refresh(); } }
        catch (error) { if (live()) { errorNode.textContent = error.message; errorNode.hidden = false; } }
        finally { if (live()) { actionPending = false; render(); } }
      };
    });
  };
  async function refresh() {
    if (!live() || loading) return loading;
    clearTimeout(timer); controller = new AbortController();
    loading = (async () => {
      try { const updated = await api('/api/model-downloads', { signal: controller.signal }); if (!live()) return; models = updated; errorNode.hidden = true; render(); schedule(); }
      catch (error) { if (live() && !controller.signal.aborted) { errorNode.textContent = error.message; errorNode.hidden = false; errorNode.innerHTML += '<br><button type="button" class="mini-action" data-download-retry>重新读取目录</button>'; errorNode.querySelector('[data-download-retry]').onclick = () => { void refresh(); }; } }
      finally { loading = false; }
    })();
    return loading;
  }
  void refresh();
  return { refresh, dispose() { disposed = true; clearTimeout(timer); controller?.abort(); } };
}
