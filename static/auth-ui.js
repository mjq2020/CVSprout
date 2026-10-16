const USERNAME = /^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,63}$/;

export function mountAuthScreen(container, { authMode, useToken = false, message = '', notice = false, icon, escapeHtml, isCurrent, onLogin }) {
  let disposed = false, serial = 0, controller = null, busy = false, mode = useToken ? 'token' : 'login';
  const current = form => !disposed && isCurrent() && (!form || form.isConnected && container.contains(form));
  const eye = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>';
  const field = (id, label, type, options = '') => `<div class="auth-field"><label for="${id}">${label}</label>${type === 'password' ? `<div class="auth-password"><input id="${id}" name="${id}" type="password" ${options}/><button type="button" class="auth-password-toggle" data-password-toggle="${id}" aria-label="显示${label}" aria-controls="${id}" aria-pressed="false">${eye}<span>显示</span></button></div>` : `<input id="${id}" name="${id}" type="${type}" ${options}/>`}<p class="auth-field-error" id="${id}-error" hidden></p></div>`;
  const illustration = `<div class="auth-preview" aria-hidden="true"><div class="auth-preview-head"><span>协作标注工作区</span><span><i></i>AI + 人工校正</span></div><svg viewBox="0 0 420 236" fill="none"><defs><pattern id="auth-grid" width="26" height="26" patternUnits="userSpaceOnUse"><path d="M26 0H0V26" stroke="#d7e2f2" stroke-width=".6"/></pattern></defs><rect width="420" height="236" fill="#edf3fb"/><rect width="420" height="236" fill="url(#auth-grid)"/><path d="M0 194 80 163 175 179 278 139 420 171V236H0Z" fill="#d9e5f2"/><rect x="59" y="76" width="98" height="116" rx="8" fill="#8eacd0"/><rect x="52" y="66" width="112" height="135" stroke="#365cf5" stroke-width="2"/><rect x="52" y="45" width="72" height="21" rx="3" fill="#365cf5"/><text x="60" y="60" fill="white" font-size="11">目标检测</text><path d="m204 97 45-19 39 25 8 62-42 29-51-21Z" fill="#a6c9bf"/><path d="m199 94 50-22 44 28 9 68-46 32-59-24Z" fill="#159474" fill-opacity=".14" stroke="#158367" stroke-width="2"/><rect x="197" y="51" width="72" height="21" rx="3" fill="#158367"/><text x="205" y="66" fill="white" font-size="11">实例分割</text><path d="m352 84-1 48m-28-12 28 12 26-13m-26 13-16 48m16-48 19 47" stroke="#8970c3" stroke-width="4" stroke-linecap="round"/><g fill="#fff" stroke="#7757b4" stroke-width="2"><circle cx="352" cy="84" r="7"/><circle cx="351" cy="132" r="5"/><circle cx="323" cy="120" r="5"/><circle cx="377" cy="119" r="5"/><circle cx="335" cy="180" r="5"/><circle cx="370" cy="179" r="5"/></g><rect x="320" y="48" width="66" height="21" rx="3" fill="#7757b4"/><text x="328" y="63" fill="white" font-size="11">关键点</text></svg><div class="auth-preview-footer"><span>检测 · 分割 · 分类 · 关键点</span><span>团队共同完成</span></div></div>`;
  function shell(content) {
    return `<main class="auth-shell"><div class="auth-layout"><section class="auth-intro"><a class="auth-brand" href="#/" aria-label="CVSprout 首页"><span class="brand-mark">${icon('logo')}</span><span>CVSprout<small>视觉数据协作平台</small></span></a><div class="auth-intro-copy"><h1>让数据准备，<br/>更有条理。</h1><p>从第一张图片到可部署的模型，<br class="auth-desktop-break"/>在同一个工作区完成。</p></div>${illustration}<ol class="auth-workflow" aria-label="数据工作流程">${['导入','标注','训练','转换','导出'].map((label, i) => `<li><span>${i + 1}</span>${label}</li>`).join('')}</ol><p class="auth-team-note">AI 生成初稿，成员协作校正，审核后用于训练。</p></section><section class="auth-access" aria-label="账号登录与注册">${content}</section></div></main>`;
  }
  function tabs() {
    if (authMode !== 'accounts' || mode === 'token') return '';
    return `<div class="auth-tabs" role="tablist" aria-label="账号入口"><button type="button" role="tab" id="auth-login-tab" data-auth-mode="login" aria-controls="auth-form-panel" aria-selected="${mode === 'login'}" tabindex="${mode === 'login' ? 0 : -1}">登录</button><button type="button" role="tab" id="auth-register-tab" data-auth-mode="register" aria-controls="auth-form-panel" aria-selected="${mode === 'register'}" tabindex="${mode === 'register' ? 0 : -1}">申请注册</button></div>`;
  }
  function render(nextMode, defaults = {}, initialMessage = '', initialNotice = false) {
    if (!current()) return;
    controller?.abort(); serial++; busy = false; mode = nextMode;
    const register = mode === 'register', token = mode === 'token', resubmit = register && defaults.resubmit;
    const heading = register ? resubmit ? '重新提交注册申请' : '申请加入工作区' : token ? '管理员令牌登录' : '登录工作区';
    const explanation = register ? resubmit ? '根据管理员反馈修改申请，并使用原申请密码验证身份。' : '填写账号信息，管理员审批并分配角色后即可登录。' : token ? '使用管理员提供的访问令牌。' : '使用已开通的账号，继续你的数据工作。';
    const formId = register ? 'register-form' : 'login-form';
    const content = token ? field('login-token', '访问令牌', 'password', 'autocomplete="off" required aria-describedby="login-token-error"') : `${field(register ? 'register-username' : 'login-username', '账号', 'text', `value="${escapeHtml(defaults.username || '')}" autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false" maxlength="64" required ${resubmit ? 'readonly' : ''} aria-describedby="${register ? 'register-username-hint register-username-error' : 'login-username-error'}"`)}${register ? `<p class="auth-field-hint" id="register-username-hint">2–64 位字母、数字或 . _ -，以字母或数字开头。</p>${field('register-display', '显示名称', 'text', `value="${escapeHtml(defaults.display_name || '')}" autocomplete="nickname" maxlength="80" required aria-describedby="register-display-error"`)}` : ''}${field(register ? 'register-password' : 'login-password', resubmit ? '原申请密码' : '密码', 'password', `autocomplete="${register && !resubmit ? 'new-password' : 'current-password'}" maxlength="256" required aria-describedby="${register ? 'register-password-hint register-password-error' : 'login-password-error'}"`)}${register ? `<p class="auth-field-hint" id="register-password-hint">${resubmit ? '输入第一次申请时设置的密码。' : '至少 12 个字符，建议使用便于记忆的长密码。'}</p>${field('register-confirm', '确认密码', 'password', 'autocomplete="new-password" maxlength="256" required aria-describedby="register-confirm-error"')}<details class="auth-note-details" ${defaults.note ? 'open' : ''}><summary>申请说明（可选）</summary><label class="sr-only" for="register-note">申请说明</label><textarea id="register-note" name="note" maxlength="1000" rows="3" placeholder="例如所属团队，以及需要参与的工作。">${escapeHtml(defaults.note || '')}</textarea><p class="auth-field-hint">权限由管理员审批时分配。</p></details>` : ''}`;
    container.innerHTML = shell(`<div class="auth-card">${tabs()}<div id="auth-form-panel" ${authMode === 'accounts' && !token ? `role="tabpanel" aria-labelledby="${register ? 'auth-register-tab' : 'auth-login-tab'}"` : ''}><div class="auth-form-heading"><h2>${heading}</h2><p>${explanation}</p></div><div id="login-feedback" class="auth-feedback ${initialNotice ? 'notice' : 'error'}" role="${initialNotice ? 'status' : 'alert'}" tabindex="-1" ${initialMessage ? '' : 'hidden'}>${escapeHtml(initialMessage)}</div><form id="${formId}" class="auth-form" novalidate><fieldset>${content}<p class="auth-caps-warning" data-caps-warning hidden>大写锁定已开启</p><button class="btn primary auth-submit" type="submit">${register ? '提交注册申请' : '进入工作台'}</button></fieldset></form>${!register ? `<p class="auth-help">${authMode === 'accounts' && !token ? '已提交申请？输入账号密码可查看审批状态。<br/>' : ''}忘记密码请联系管理员。</p>` : '<p class="auth-help">申请通过前无法访问项目和数据。</p>'}${authMode === 'accounts' && !register ? `<details class="auth-admin-entry" ${token ? 'open' : ''}><summary>管理员登录方式</summary><button type="button" class="auth-text-button" id="login-mode" data-auth-mode="${token ? 'login' : 'token'}">${token ? '使用账号密码登录' : '使用管理员令牌登录'}</button></details>` : ''}</div></div><p class="auth-access-caption">协作有分工，数据有记录。</p>`);
    const form = container.querySelector(`#${formId}`);
    bindSwitches(form, defaults);
    form.addEventListener('input', event => {
      const input = event.target; input.removeAttribute('aria-invalid');
      const error = container.querySelector(`#${input.id}-error`); if (error) { error.hidden = true; error.textContent = ''; }
    });
    form.addEventListener('keydown', caps); form.addEventListener('keyup', caps);
    form.addEventListener('focusout', () => { const node = container.querySelector('[data-caps-warning]'); if (node) node.hidden = true; });
    form.onsubmit = event => { event.preventDefault(); void submit(form, defaults); };
  }
  function caps(event) {
    const node = container.querySelector('[data-caps-warning]');
    if (node) node.hidden = !event.target.closest('.auth-password') || !event.getModifierState?.('CapsLock');
  }
  function bindSwitches(form, defaults) {
    container.querySelectorAll('[data-auth-mode]').forEach(button => button.onclick = () => {
      if (busy || !current(form)) return;
      const username = container.querySelector('#login-username, #register-username')?.value.trim() || defaults.username || '';
      render(button.dataset.authMode, { username });
      container.querySelector('input')?.focus();
    });
    container.querySelector('.auth-tabs')?.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || busy) return;
      event.preventDefault();
      const target = event.key === 'Home' ? 'login' : event.key === 'End' ? 'register' : mode === 'login' ? 'register' : 'login';
      container.querySelector(`[data-auth-mode="${target}"]`)?.click();
      container.querySelector(`[data-auth-mode="${target}"]`)?.focus();
    });
    container.querySelectorAll('[data-password-toggle]').forEach(button => button.onclick = () => {
      const input = container.querySelector(`#${button.dataset.passwordToggle}`), showing = input.type === 'password';
      const start = input.selectionStart, end = input.selectionEnd;
      input.type = showing ? 'text' : 'password'; button.setAttribute('aria-pressed', String(showing));
      button.setAttribute('aria-label', `${showing ? '隐藏' : '显示'}${input.id === 'login-token' ? '访问令牌' : '密码'}`);
      button.querySelector('span').textContent = showing ? '隐藏' : '显示';
      input.focus({ preventScroll: true }); if (start != null) input.setSelectionRange(start, end);
    });
  }
  function setBusy(form, value) {
    busy = value; form.querySelector('fieldset').disabled = value; form.setAttribute('aria-busy', String(value));
    container.querySelectorAll('[data-auth-mode]').forEach(button => { button.disabled = value; });
    form.querySelector('[type="submit"]').textContent = value ? mode === 'register' ? '正在提交…' : '正在登录…' : mode === 'register' ? '提交注册申请' : '进入工作台';
  }
  function validation(form) {
    form.querySelectorAll('[aria-invalid]').forEach(input => input.removeAttribute('aria-invalid'));
    form.querySelectorAll('.auth-field-error').forEach(error => { error.hidden = true; error.textContent = ''; });
    const values = {}, problems = [];
    const read = id => form.querySelector(`#${id}`)?.value || '';
    if (mode === 'token') { values.token = read('login-token').trim(); if (!values.token) problems.push(['login-token', '请输入访问令牌。']); }
    else {
      const registering = mode === 'register', prefix = registering ? 'register' : 'login';
      values.username = read(`${prefix}-username`).trim(); values.password = read(`${prefix}-password`);
      if (!values.username) problems.push([`${prefix}-username`, '请输入账号。']);
      else if (registering && !USERNAME.test(values.username)) problems.push(['register-username', '账号需为 2–64 位字母、数字或 . _ -，并以字母或数字开头。']);
      if (!values.password) problems.push([`${prefix}-password`, '请输入密码。']);
      else if (registering && values.password.length < 12) problems.push(['register-password', '密码至少需要 12 个字符。']);
      if (registering) {
        values.display_name = read('register-display').trim(); values.note = read('register-note').trim();
        if (!values.display_name) problems.push(['register-display', '请输入团队中使用的显示名称。']);
        if (read('register-confirm') !== values.password || !read('register-confirm')) problems.push(['register-confirm', '两次输入的密码不一致，请重新确认。']);
      }
    }
    for (const [id, text] of problems) {
      const input = form.querySelector(`#${id}`), error = form.querySelector(`#${id}-error`);
      input?.setAttribute('aria-invalid', 'true'); if (error) { error.hidden = false; error.textContent = text; }
    }
    if (problems.length) { form.querySelector(`#${problems[0][0]}`)?.focus(); return null; }
    return values;
  }
  async function submit(form, defaults) {
    if (busy || !current(form)) return;
    const payload = validation(form); if (!payload) return;
    const feedback = container.querySelector('#login-feedback'); feedback.hidden = true; feedback.replaceChildren();
    const own = ++serial, actionMode = mode; controller = new AbortController(); setBusy(form, true);
    try {
      const headers = { 'X-MDP-Client': 'browser', 'Content-Type': 'application/json' };
      if (actionMode === 'token') headers.Authorization = `Bearer ${payload.token}`;
      const response = await fetch(actionMode === 'register' ? '/api/register' : actionMode === 'token' ? '/api/session' : '/api/login', {
        method: 'POST', headers, credentials: 'same-origin', signal: controller.signal,
        ...(actionMode === 'token' ? {} : { body: JSON.stringify(payload) }),
      });
      let data;
      try { data = await response.json(); } catch (_) { throw new Error('服务暂时无法处理请求，请稍后重试。'); }
      if (!current(form) || own !== serial) return;
      if (!response.ok) {
        const detail = data.detail;
        if (['registration_pending', 'registration_rejected'].includes(detail?.code)) {
          const registration = detail.registration || {}, rejected = detail.code === 'registration_rejected';
          feedback.className = `auth-feedback ${rejected ? 'warning' : 'notice'}`; feedback.setAttribute('role', 'status'); feedback.hidden = false;
          feedback.innerHTML = `<strong>${rejected ? '申请未通过' : '申请正在等待审批'}</strong><p>${escapeHtml(detail.message || (rejected ? '请根据反馈修改申请。' : '管理员审批并分配角色后，即可登录。'))}</p>${rejected && registration.reason ? `<p class="auth-review-reason">管理员反馈：${escapeHtml(registration.reason)}</p>` : ''}${registration.can_resubmit ? '<button type="button" class="auth-text-button" data-resubmit>修改并重新提交申请</button>' : ''}`;
          feedback.querySelector('[data-resubmit]')?.addEventListener('click', () => render('register', { ...registration, username: payload.username, resubmit: true }));
          feedback.focus(); return;
        }
        throw new Error(typeof detail === 'string' ? detail : detail?.message || (response.status === 422 ? '填写的信息不符合要求，请检查账号、密码长度和申请说明。' : '登录或申请未完成，请稍后重试。'));
      }
      if (actionMode === 'register') { renderReceipt(data, payload.username); return; }
      await onLogin(data);
    } catch (error) {
      if (!current(form) || own !== serial || error.name === 'AbortError') return;
      feedback.hidden = false; feedback.className = 'auth-feedback error'; feedback.setAttribute('role', 'alert');
      feedback.textContent = error instanceof TypeError ? '无法连接服务，请检查网络后重试。' : error.message; feedback.focus();
    } finally {
      if (current(form) && own === serial) { controller = null; setBusy(form, false); }
    }
  }
  function renderReceipt(data, username) {
    serial++; busy = false; controller = null;
    container.innerHTML = shell(`<div class="auth-card auth-receipt"><span class="auth-receipt-icon">${icon('check')}</span><h2>注册申请已提交</h2><p>账号 <strong>${escapeHtml(data.username || username)}</strong> 正在等待管理员审批。</p><ol><li>管理员核对申请并分配角色。</li><li>审批通过后，使用刚设置的账号密码登录。</li></ol><p class="auth-receipt-note">可以返回登录页，输入账号密码查看审批状态。</p><button type="button" class="btn primary auth-submit" data-return-login>返回登录</button></div><p class="auth-access-caption">你的注册申请不会自动获得项目访问权限。</p>`);
    container.querySelector('[data-return-login]').onclick = () => { render('login', { username: data.username || username }); container.querySelector('#login-password')?.focus(); };
    container.querySelector('[data-return-login]').focus();
  }
  render(mode, {}, message, notice);
  return () => { disposed = true; serial++; controller?.abort(); };
}
