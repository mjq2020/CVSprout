export function mountSetup(container, { escapeHtml, onComplete }) {
  container.innerHTML = `<main class="login-shell setup-shell"><section class="login-card"><h1>欢迎使用 CVSprout</h1><p>先创建管理员，然后在“模型服务”中下载适合你的基础模型。</p><form id="setup-form">
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

export async function mountModelDownloads(container, { api, escapeHtml, notify }) {
  const statusNames = { downloading: '下载中', complete: '已校验', failed: '下载失败', interrupted: '下载中断', missing: '文件缺失' };
  async function render() {
    try {
      const models = await api('/api/model-downloads');
      if (!container.isConnected) return;
      container.innerHTML = `<p>模型按需下载到持久化存储，升级时保留。首次体验建议下载与你项目类型对应的 YOLO26 Nano。</p>
        <div class="model-download-list">${models.map(model => {
          const busy = model.state.status === 'downloading';
          const progress = Math.min(100, Math.round(100 * (model.state.downloaded || 0) / model.size_bytes));
          return `<article><div><strong>${escapeHtml(model.name)}</strong><p>${(model.size_bytes / 1048576).toFixed(1)} MB · ${escapeHtml(model.license)}</p>
            <p role="status">${escapeHtml(model.state.error || statusNames[model.state.status] || (model.installed ? '文件已存在' : '尚未下载'))}${busy ? ` ${progress}%` : ''}</p></div>
            <button class="btn" data-download-model="${escapeHtml(model.id)}" ${busy ? 'disabled' : ''}>${model.installed ? '校验模型' : '下载模型'}</button></article>`;
        }).join('')}</div>`;
      container.querySelectorAll('[data-download-model]').forEach(button => {
        button.onclick = async () => {
          button.disabled = true;
          try { await api(`/api/model-downloads/${encodeURIComponent(button.dataset.downloadModel)}`, { method: 'POST' }); await render(); }
          catch (error) { notify(error.message); if (button.isConnected) button.disabled = false; }
        };
      });
      if (models.some(model => model.state.status === 'downloading')) setTimeout(() => { if (container.isConnected) render(); }, 1500);
    } catch (error) { if (container.isConnected) container.textContent = error.message; }
  }
  await render();
}
