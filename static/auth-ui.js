import { localizePayload } from "./api-i18n.js?v=0.29.3";
import { t, ta, getLocale, languageSwitcherMarkup } from "./i18n.js?v=0.29.3";
import { appearanceControlsMarkup } from "./appearance-ui.js?v=0.29.3";
import { mountVisionScene } from "./vision-scene.js?v=0.29.3";

const USERNAME = /^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,63}$/;

export function mountAuthScreen(container, { authMode, useToken = false, message = '', notice = false, icon, escapeHtml, isCurrent, onLogin }) {
  let disposed = false, serial = 0, controller = null, busy = false, mode = useToken ? 'token' : 'login';
  let scene = null;
  const sceneCleanup = () => { scene?.destroy(); scene = null; };
  function refreshScene() {
    sceneCleanup(); scene = mountVisionScene(container.querySelector('.vision-scene'));
  }
  const current = form => !disposed && isCurrent() && (!form || form.isConnected && container.contains(form));
  const eye = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>';
  const field = (id, label, type, options = '') => `<div class="auth-field"><label for="${id}">${label}</label>${type === 'password' ? `<div class="auth-password"><input id="${id}" name="${id}" type="password" ${options}/><button type="button" class="auth-password-toggle" data-password-toggle="${id}" aria-label="${ta("显示{0}", [label])}" aria-controls="${id}" aria-pressed="false">${eye}<span>${t("显示")}</span></button></div>` : `<input id="${id}" name="${id}" type="${type}" ${options}/>`}<p class="auth-field-error" id="${id}-error" hidden></p></div>`;
  const illustration = `<div class="vision-scene" aria-hidden="true"><div class="vision-fallback"><i></i><i></i><i></i></div><canvas></canvas><span class="vision-corner top-left"></span><span class="vision-corner top-right"></span><span class="vision-corner bottom-left"></span><span class="vision-corner bottom-right"></span></div>`;
  function shell(content) {
    return `<main class="auth-shell"><div class="auth-utilities">${languageSwitcherMarkup("auth")}<details class="auth-appearance studio-disclosure" data-studio-disclosure><summary aria-label="${ta("外观设置")}">${icon("sliders")}<span>${t("外观")}</span></summary><div class="studio-popover">${appearanceControlsMarkup("auth")}</div></details></div><div class="auth-layout"><section class="auth-intro"><a class="auth-brand" href="#/" aria-label="${ta("CVSprout 首页")}"><span class="brand-mark">${icon('logo')}</span><span>CVSprout</span></a><div class="auth-intro-copy"><h1>${t("从数据，")}<br/>${t("到看见。")}</h1><p>${t("标注每一个细节，让视觉模型生长。")}</p></div>${illustration}<div class="auth-studio-footer"><span>${t("你的数据，你的模型。")}</span><span>${t("导入 / 标注 / 训练 / 部署")}</span></div></section><section class="auth-access" aria-label="${ta("账号登录与注册")}">${content}</section></div></main>`;
  }
  function tabs() {
    if (authMode !== 'accounts' || mode === 'token') return '';
    return `<div class="auth-tabs" role="tablist" aria-label="${ta("账号入口")}"><button type="button" role="tab" id="auth-login-tab" data-auth-mode="login" aria-controls="auth-form-panel" aria-selected="${mode === 'login'}" tabindex="${mode === 'login' ? 0 : -1}">${t("登录")}</button><button type="button" role="tab" id="auth-register-tab" data-auth-mode="register" aria-controls="auth-form-panel" aria-selected="${mode === 'register'}" tabindex="${mode === 'register' ? 0 : -1}">${t("申请注册")}</button></div>`;
  }
  function render(nextMode, defaults = {}, initialMessage = '', initialNotice = false) {
    if (!current()) return;
    sceneCleanup(); controller?.abort(); serial++; busy = false; mode = nextMode;
    const register = mode === 'register', token = mode === 'token', resubmit = register && defaults.resubmit;
    const heading = register ? resubmit ? t("重新提交注册申请") : t("申请加入工作区") : token ? t("管理员令牌登录") : t("欢迎回来");
    const explanation = register ? resubmit ? t("根据管理员反馈修改申请，并使用原申请密码验证身份。") : t("填写账号信息，管理员审批并分配角色后即可登录。") : token ? t("使用管理员提供的访问令牌。") : t("登录，继续你的视觉项目。");
    const formId = register ? 'register-form' : 'login-form';
    const content = token ? field('login-token', t("访问令牌"), 'password', 'autocomplete="off" required aria-describedby="login-token-error"') : `${field(register ? 'register-username' : 'login-username', t("账号"), 'text', `value="${escapeHtml(defaults.username || '')}" autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false" maxlength="64" required ${resubmit ? 'readonly' : ''} aria-describedby="${register ? 'register-username-hint register-username-error' : 'login-username-error'}"`)}${register ? `<p class="auth-field-hint" id="register-username-hint">${t("2–64 位字母、数字或 . _ -，以字母或数字开头。")}</p>${field('register-display', t("显示名称"), 'text', `value="${escapeHtml(defaults.display_name || '')}" autocomplete="nickname" maxlength="80" required aria-describedby="register-display-error"`)}` : ''}${field(register ? 'register-password' : 'login-password', resubmit ? t("原申请密码") : t("密码"), 'password', `autocomplete="${register && !resubmit ? 'new-password' : 'current-password'}" maxlength="256" required aria-describedby="${register ? 'register-password-hint register-password-error' : 'login-password-error'}"`)}${register ? `<p class="auth-field-hint" id="register-password-hint">${resubmit ? t("输入第一次申请时设置的密码。") : t("至少 12 个字符，建议使用便于记忆的长密码。")}</p>${field('register-confirm', t("确认密码"), 'password', 'autocomplete="new-password" maxlength="256" required aria-describedby="register-confirm-error"')}<details class="auth-note-details" ${defaults.note ? 'open' : ''}><summary>${t("申请说明（可选）")}</summary><label class="sr-only" for="register-note">${t("申请说明")}</label><textarea id="register-note" name="note" maxlength="1000" rows="3" placeholder="${ta("例如所属团队，以及需要参与的工作。")}">${escapeHtml(defaults.note || '')}</textarea><p class="auth-field-hint">${t("权限由管理员审批时分配。")}</p></details>` : ''}`;
    container.innerHTML = shell(`<div class="auth-card">${tabs()}<div id="auth-form-panel" ${authMode === 'accounts' && !token ? `role="tabpanel" aria-labelledby="${register ? 'auth-register-tab' : 'auth-login-tab'}"` : ''}><div class="auth-form-heading"><h2>${heading}</h2><p>${explanation}</p></div><div id="login-feedback" class="auth-feedback ${initialNotice ? 'notice' : 'error'}" role="${initialNotice ? 'status' : 'alert'}" tabindex="-1" ${initialMessage ? '' : 'hidden'}>${escapeHtml(initialMessage)}</div><form id="${formId}" class="auth-form" novalidate><fieldset>${content}<p class="auth-caps-warning" data-caps-warning hidden>${t("大写锁定已开启")}</p><button class="btn primary auth-submit" type="submit">${register ? t("提交注册申请") : t("进入工作台")}</button></fieldset></form>${!register ? `<p class="auth-help">${t("{0}忘记密码请联系管理员。", [authMode === 'accounts' && !token ? `${t("已提交申请？输入账号密码可查看审批状态。")}<br/>` : ''])}</p>` : `<p class="auth-help">${t("申请通过前无法访问项目和数据。")}</p>`}${authMode === 'accounts' && !register ? `<details class="auth-admin-entry" ${token ? 'open' : ''}><summary>${t("管理员登录方式")}</summary><button type="button" class="auth-text-button" id="login-mode" data-auth-mode="${token ? 'login' : 'token'}">${token ? t("使用账号密码登录") : t("使用管理员令牌登录")}</button></details>` : ''}</div></div>`);
    refreshScene();
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
      button.setAttribute('aria-label', showing ? t("隐藏{0}", [input.id === 'login-token' ? t("访问令牌") : t("密码")]) : t("显示{0}", [input.id === 'login-token' ? t("访问令牌") : t("密码")]));
      button.querySelector('span').textContent = showing ? t("隐藏") : t("显示");
      input.focus({ preventScroll: true }); if (start != null) input.setSelectionRange(start, end);
    });
  }
  function setBusy(form, value) {
    busy = value; form.querySelector('fieldset').disabled = value; form.setAttribute('aria-busy', String(value));
    container.querySelectorAll('[data-auth-mode]').forEach(button => { button.disabled = value; });
    form.querySelector('[type="submit"]').textContent = value ? mode === 'register' ? t("正在提交…") : t("正在登录…") : mode === 'register' ? t("提交注册申请") : t("进入工作台");
  }
  function validation(form) {
    form.querySelectorAll('[aria-invalid]').forEach(input => input.removeAttribute('aria-invalid'));
    form.querySelectorAll('.auth-field-error').forEach(error => { error.hidden = true; error.textContent = ''; });
    const values = {}, problems = [];
    const read = id => form.querySelector(`#${id}`)?.value || '';
    if (mode === 'token') { values.token = read('login-token').trim(); if (!values.token) problems.push(['login-token', t("请输入访问令牌。")]); }
    else {
      const registering = mode === 'register', prefix = registering ? 'register' : 'login';
      values.username = read(`${prefix}-username`).trim(); values.password = read(`${prefix}-password`);
      if (!values.username) problems.push([`${prefix}-username`, t("请输入账号。")]);
      else if (registering && !USERNAME.test(values.username)) problems.push(['register-username', t("账号需为 2–64 位字母、数字或 . _ -，并以字母或数字开头。")]);
      if (!values.password) problems.push([`${prefix}-password`, t("请输入密码。")]);
      else if (registering && values.password.length < 12) problems.push(['register-password', t("密码至少需要 12 个字符。")]);
      if (registering) {
        values.display_name = read('register-display').trim(); values.note = read('register-note').trim();
        if (!values.display_name) problems.push(['register-display', t("请输入团队中使用的显示名称。")]);
        if (read('register-confirm') !== values.password || !read('register-confirm')) problems.push(['register-confirm', t("两次输入的密码不一致，请重新确认。")]);
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
      const headers = { 'X-MDP-Client': 'browser', 'Accept-Language': getLocale(), 'Content-Type': 'application/json' };
      if (actionMode === 'token') headers.Authorization = `Bearer ${payload.token}`;
      const response = await fetch(actionMode === 'register' ? '/api/register' : actionMode === 'token' ? '/api/session' : '/api/login', {
        method: 'POST', headers, credentials: 'same-origin', signal: controller.signal,
        ...(actionMode === 'token' ? {} : { body: JSON.stringify(payload) }),
      });
      let data;
      try { data = localizePayload(await response.json(), "/api/login", response.status); } catch (_) { throw new Error(t("服务暂时无法处理请求，请稍后重试。")); }
      if (!current(form) || own !== serial) return;
      if (!response.ok) {
        const detail = data.detail;
        if (['registration_pending', 'registration_rejected'].includes(detail?.code)) {
          const registration = detail.registration || {}, rejected = detail.code === 'registration_rejected';
          feedback.className = `auth-feedback ${rejected ? 'warning' : 'notice'}`; feedback.setAttribute('role', 'status'); feedback.hidden = false;
          feedback.innerHTML = `<strong>${rejected ? t("申请未通过") : t("申请正在等待审批")}</strong><p>${escapeHtml(detail.message || (rejected ? t("请根据反馈修改申请。") : t("管理员审批并分配角色后，即可登录。")))}</p>${rejected && registration.reason ? `<p class="auth-review-reason">${t("管理员反馈：{0}", [escapeHtml(registration.reason)])}</p>` : ''}${registration.can_resubmit ? `<button type="button" class="auth-text-button" data-resubmit>${t("修改并重新提交申请")}</button>` : ''}`;
          feedback.querySelector('[data-resubmit]')?.addEventListener('click', () => render('register', { ...registration, username: payload.username, resubmit: true }));
          feedback.focus(); return;
        }
        throw new Error(typeof detail === 'string' ? detail : detail?.message || (response.status === 422 ? t("填写的信息不符合要求，请检查账号、密码长度和申请说明。") : t("登录或申请未完成，请稍后重试。")));
      }
      if (actionMode === 'register') { renderReceipt(data, payload.username); return; }
      await onLogin(data);
    } catch (error) {
      if (!current(form) || own !== serial || error.name === 'AbortError') return;
      feedback.hidden = false; feedback.className = 'auth-feedback error'; feedback.setAttribute('role', 'alert');
      feedback.textContent = error instanceof TypeError ? t("无法连接服务，请检查网络后重试。") : error.message; feedback.focus();
    } finally {
      if (current(form) && own === serial) { controller = null; setBusy(form, false); }
    }
  }
  function renderReceipt(data, username) {
    sceneCleanup(); serial++; busy = false; controller = null;
    container.innerHTML = shell(`<div class="auth-card auth-receipt"><span class="auth-receipt-icon">${icon('check')}</span><h2>${t("注册申请已提交")}</h2><p>${t("账号")} <strong>${escapeHtml(data.username || username)}</strong> ${t("正在等待管理员审批。")}</p><ol><li>${t("管理员核对申请并分配角色。")}</li><li>${t("审批通过后，使用刚设置的账号密码登录。")}</li></ol><p class="auth-receipt-note">${t("可以返回登录页，输入账号密码查看审批状态。")}</p><button type="button" class="btn primary auth-submit" data-return-login>${t("返回登录")}</button></div><p class="auth-access-caption">${t("你的注册申请不会自动获得项目访问权限。")}</p>`);
    refreshScene();
    container.querySelector('[data-return-login]').onclick = () => { render('login', { username: data.username || username }); container.querySelector('#login-password')?.focus(); };
    container.querySelector('[data-return-login]').focus();
  }
  render(mode, {}, message, notice);
  return () => { disposed = true; serial++; controller?.abort(); sceneCleanup(); };
}
