import { route, state, api, app, topbar, escapeHtml, notify, openModal, closeModal, jsonOptions, renderLoginScreen } from "./app.js?v=0.25.1";

const roles = {admin:"管理员", reviewer:"审核员", annotator:"标注员"};
const actions = {login_success:"登录成功", login_failure:"登录失败", login_failed:"登录失败", logout:"退出登录", create:"创建账号", update:"更新账号", password_change:"修改密码", password_changed:"修改密码", password_reset:"重置密码", unlock:"解除登录限制", revoke_sessions:"撤销登录", session_revoked:"撤销登录", revoke_session:"撤销登录", profile_update:"修改个人资料"};
Object.assign(actions, {account_created:"创建账号", account_updated:"更新账号", login_throttled:"登录验证受到限制", password_change_failed:"修改密码验证失败", login_unlocked:"解除登录限制", session_logout:"退出登录", sessions_revoked:"撤销设备登录"});
let accountEpoch = 0, sessionsRequest = 0, memberDialogRequest = 0;
const timeText = value => value ? new Date(value).toLocaleString("zh-CN") : "尚无记录";

function pageContext(section, node = null) {
  const epoch = state.selectionEpoch, session = state.session;
  return () => state.selectionEpoch === epoch && state.session === session
    && state.currentSection === section && (!node || node.isConnected);
}

function memberDialogContext() {
  const request = ++memberDialogRequest, current = pageContext('users', document.querySelector('#member-results'));
  const modal = document.querySelector('#modal-root')?.firstElementChild;
  return () => request === memberDialogRequest && current()
    && document.querySelector('#modal-root')?.firstElementChild === modal;
}

function signedOut(message) {
  state.session = null; state.authReady = false;
  closeModal(); renderLoginScreen(message, false, true);
}

function passwordFields(force = false) {
  return `<div class="field"><label for="current-password">${force ? "当前初始密码" : "当前密码"}</label><input id="current-password" type="password" autocomplete="current-password" required maxlength="256"/></div><div class="field"><label for="new-password">新密码</label><input id="new-password" type="password" autocomplete="new-password" required minlength="12" maxlength="256" aria-describedby="password-rules"/><p class="hint" id="password-rules">12–256 个字符，可以使用较长的短语。请不要重复使用初始密码或其他网站的密码。</p></div><div class="field"><label for="confirm-password">再次输入新密码</label><input id="confirm-password" type="password" autocomplete="new-password" required minlength="12" maxlength="256"/></div><label class="check-line"><input id="show-own-password" type="checkbox"/>显示密码</label>`;
}

function bindForm(id, handler) {
  const form = document.querySelector(id);
  if (!form) return;
  form.onsubmit = async event => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]');
    if (submit.disabled) return;
    const old = submit.textContent, feedback = form.querySelector('[role="alert"]');
    feedback.hidden = true; submit.disabled = true; submit.textContent = "正在保存…";
    try { await handler(form); }
    catch (error) { if (form.isConnected) { feedback.textContent = error.message; feedback.hidden = false; feedback.focus(); } }
    finally { if (form.isConnected) { submit.disabled = false; submit.textContent = old; } }
  };
}

function sessionMarkup(sessions, controls = false) {
  if (!sessions.length) return '<p class="hint">没有有效的登录会话。</p>';
  return `<div class="session-list">${sessions.map(item => `<article class="session-item"><div><strong>${item.current ? "当前浏览器" : "其他登录"}</strong>${item.current ? '<span class="member-self">正在使用</span>' : ""}<p>最近活动：${escapeHtml(timeText(item.last_seen_at || item.created_at))}</p><p>登录地址：${escapeHtml(item.ip_address || "未记录")} · 到期：${escapeHtml(timeText(item.expires_at))}</p><details class="technical-details"><summary>设备信息与登录时间</summary><p>${escapeHtml(item.user_agent || "未记录设备信息")}</p><p>登录于 ${escapeHtml(timeText(item.created_at))}</p></details></div>${controls ? `<button class="btn ghost" data-revoke-session="${escapeHtml(item.id)}" data-current="${Boolean(item.current)}">${item.current ? "退出当前登录" : "撤销此登录"}</button>` : ""}</article>`).join("")}</div>`;
}

function eventsMarkup(events) {
  return events.length ? `<ol class="security-events">${events.map(event => {
    const detail = event.details || {}, changes = [];
    if (detail.role) changes.push(`角色：${roles[detail.role] || detail.role}`);
    if (typeof detail.enabled === "boolean") changes.push(detail.enabled ? "账号已启用" : "账号已停用");
    if (detail.changed_fields?.includes("display_name")) changes.push("显示名称已修改");
    if (Number.isInteger(detail.revoked_sessions)) changes.push(`撤销 ${detail.revoked_sessions} 个登录`);
    const actor = event.actor === "anonymous" ? "未登录用户" : event.actor === "bootstrap-admin" ? "管理员令牌" : (event.actor || "").replace(/^user:/, "");
    return `<li><div><strong>${escapeHtml(actions[event.action] || event.action)}</strong><span>${escapeHtml(timeText(event.created_at))}</span></div><p>${escapeHtml([event.ip_address, actor, ...changes].filter(Boolean).join(" · "))}</p></li>`;
  }).join("")}</ol>` : '<p class="hint">暂无安全记录。</p>';
}

async function renderAccountPage() {
  const epoch = ++accountEpoch;
  state.currentSection = "account"; state.project = null; state.asset = null;
  const context = pageContext('account'), current = () => epoch === accountEpoch && context();
  const force = Boolean(state.session?.must_change_password);
  const shell = content => `<div class="app-shell">${topbar()}<main class="dashboard"><div class="dashboard-inner account-page"><div class="page-head"><div><h1>${force ? "先设置自己的密码" : "账号与安全"}</h1><p class="page-subtitle">${force ? "初始密码只用于首次登录。修改后即可进入工作台。" : "管理个人资料、密码和已登录的设备。"}</p></div>${!force ? '<a class="btn ghost link-btn" href="#/">返回项目</a>' : ""}</div>${content}</div></main></div>`;
  if (!state.session?.user_id) { app.innerHTML = shell('<div class="inline-notice">当前使用管理员令牌或开发模式。个人改密和设备管理适用于独立账号。</div>'); return; }
  app.innerHTML = shell('<p role="status">正在读取账号信息…</p>');
  try {
    const user = await api("/api/account");
    if (!current()) return;
    app.innerHTML = shell(`${force ? '<div class="inline-notice">为保护账号，设置新密码前暂时无法操作项目。已有项目和标注不会改变。</div>' : ""}<div class="account-grid ${force ? "password-required" : ""}">${!force ? `<section class="surface account-section"><h2>个人资料</h2><p class="hint">登录账号 <strong>${escapeHtml(user.username)}</strong> · ${roles[user.role]}</p><form id="profile-form"><div class="field"><label for="profile-name">显示名称</label><input id="profile-name" value="${escapeHtml(user.display_name)}" required maxlength="80"/></div><p class="inline-error" role="alert" tabindex="-1" hidden></p><button type="submit" class="btn primary">保存名称</button></form></section>` : ""}<section class="surface account-section"><h2>${force ? "设置新密码" : "修改密码"}</h2><form id="password-form">${passwordFields(force)}<p class="hint">保存后会退出其他设备上的登录，当前浏览器继续使用新会话。</p><p class="inline-error" role="alert" tabindex="-1" hidden></p><button type="submit" class="btn primary">${force ? "保存密码并进入工作台" : "更新密码"}</button></form></section></div>${!force ? '<section class="surface account-section"><div class="section-head"><div><h2>已登录设备</h2><p class="hint">发现不认识的登录时，请撤销该登录并修改密码。</p></div><button id="revoke-other-sessions" class="btn ghost">退出其他设备</button></div><div id="account-sessions" role="status">正在读取…</div></section><section class="surface account-section"><h2>最近安全记录</h2><div id="account-events">正在读取…</div></section>' : ""}`);
    document.querySelector('#show-own-password').onchange = event => document.querySelectorAll('#password-form input[autocomplete]').forEach(input => input.type = event.target.checked ? "text" : "password");
    bindForm('#profile-form', async form => {
      const display_name = form.querySelector('#profile-name').value.trim();
      if (!display_name) throw new Error("请输入显示名称。");
      const result = await api('/api/account', jsonOptions('PATCH', {display_name}));
      state.session.display_name = result.display_name; notify("显示名称已更新。");
    });
    bindForm('#password-form', async form => {
      const current_password = form.querySelector('#current-password').value, new_password = form.querySelector('#new-password').value;
      if (new_password !== form.querySelector('#confirm-password').value) throw new Error("两次输入的新密码不一致，请重新确认。");
      if (new_password === current_password) throw new Error("新密码不能与当前密码相同。");
      const session = await api('/api/account/password', jsonOptions('POST', {current_password, new_password}));
      const stillOnAccountPage = current();
      state.session = session; state.authReady = true;
      notify("密码已更新，其他设备已退出登录。");
      if (stillOnAccountPage) {
        if (force) location.hash = '#/'; else await renderAccountPage();
      } else { await route(); }
    });
    if (!force) {
      document.querySelector('#revoke-other-sessions').onclick = () => openModal({title:"退出其他设备", subtitle:"当前浏览器保持登录。其他设备需要重新输入账号密码，未保存的修改可能丢失。", body:'<p>确认退出此账号在其他设备上的登录？</p>', submitText:"退出其他设备", onSubmit: async () => { const modal=document.querySelector('.modal'); await api('/api/account/sessions?keep_current=true', {method:'DELETE'}); if(modal?.isConnected)closeModal(); notify("其他设备的登录已撤销。"); if(current())await loadAccountSessions(); }});
      void loadAccountSessions();
      const eventsNode = document.querySelector('#account-events');
      void api('/api/account/events?limit=20').then(result => { if (current() && eventsNode?.isConnected) eventsNode.innerHTML = eventsMarkup(result.events || result); }).catch(error => { if (error.status!==401 && current() && eventsNode?.isConnected) eventsNode.textContent = error.message; });
    }
  } catch (error) {
    if (error.status === 401 || !current()) return;
    app.innerHTML = shell(`<div class="inline-error"><p>${escapeHtml(error.message)}</p><button class="btn ghost" id="retry-account">重新加载</button></div>`);
    document.querySelector('#retry-account').onclick = renderAccountPage;
  }
}

async function loadAccountSessions() {
  const epoch = accountEpoch, request = ++sessionsRequest, node = document.querySelector('#account-sessions');
  if (!node || state.currentSection !== 'account') return;
  const context=pageContext('account',node), current=()=>epoch===accountEpoch && request===sessionsRequest && context();
  try {
    const result = await api('/api/account/sessions');
    if (!current()) return;
    node.innerHTML = sessionMarkup(result.sessions, true);
    node.querySelectorAll('[data-revoke-session]').forEach(button => button.onclick = () => {
      const current = button.dataset.current === 'true';
      openModal({title:current ? "退出当前登录" : "撤销此设备的登录", subtitle:"该设备需要重新输入账号密码才能继续使用。", body:`<p>${current ? "你将回到登录页面。" : "撤销后，该设备无法继续读取或修改项目。"}</p>`, submitText:current ? "退出登录" : "撤销登录", onSubmit:async () => {
        const modal=document.querySelector('.modal');
        await api(`/api/account/sessions/${encodeURIComponent(button.dataset.revokeSession)}`, {method:'DELETE'});
        if(modal?.isConnected)closeModal(); if (current) signedOut("已退出当前登录。"); else { notify("该设备的登录已撤销。"); if(context() && epoch===accountEpoch)await loadAccountSessions(); }
      }});
    });
  } catch (error) { if (error.status!==401 && current()) node.textContent = error.message; }
}

async function openMemberSessions(user) {
  const current=memberDialogContext();
  try {
    const result = await api(`/api/users/${user.id}/sessions`);
    if (!current()) return;
    openModal({title:`${user.display_name} 的登录设备`, subtitle:"撤销登录不会删除账号、项目或标注。", body:sessionMarkup(result.sessions), submitText:"撤销全部登录", onReady:() => { document.querySelector('#modal-submit').disabled = !result.sessions.length; }, onSubmit:async () => {
      const modal=document.querySelector('.modal'); await api(`/api/users/${user.id}/sessions`, {method:'DELETE'}); if(modal?.isConnected)closeModal();
      if (user.id === state.session?.user_id) signedOut("全部登录已撤销，请重新登录。"); else notify("该成员的全部登录已撤销。");
    }});
  } catch (error) { if (error.status !== 401 && current()) notify(error.message, "error"); }
}

async function openMemberEvents(user) {
  const current=memberDialogContext();
  try {
    const result = await api(`/api/users/${user.id}/events?limit=30`);
    if (!current()) return;
    openModal({title:`${user.display_name} 的安全记录`, subtitle:"最近的登录和账号管理记录。", body:eventsMarkup(result.events || result), submitText:"关闭", onSubmit:async () => closeModal()});
  } catch (error) { if (error.status !== 401 && current()) notify(error.message, "error"); }
}

export { renderAccountPage, openMemberSessions, openMemberEvents, signedOut };
