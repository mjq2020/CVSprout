import { t, accountDisplayName, accountNameFromInput, ta, getLocale } from "./i18n.js?v=0.29.3";
import { state, api, app, topbar, escapeHtml, notify, openModal, jsonOptions, closeModal } from "cvsprout/app";
import { isAdmin } from "./task-ui.js?v=0.29.3";
import { openMemberSessions, openMemberEvents, signedOut } from "./security-ui.js?v=0.29.3";

const MEMBER_ROLES = {
  user: {name:t("普通用户"),description:t("拥有个人工作空间，可创建私有项目，并按项目邀请协作者或只读成员。")},
  admin: {name:t("系统管理员"),description:t("管理账号和系统配置，能够查看和管理所有用户的项目。")},
};
let members = [], adminCount = 0, requestId = 0, registrationRequestId = 0;
let registrations = [], activeTab = 'members';
const filters = {q:"",role:"",enabled:"",page:1,page_size:20};
const registrationFilters = {q:'',status:'pending',page:1,page_size:25};
const REGISTRATION_STATES = {pending:t("待审批"),approved:t("已批准"),rejected:t("已拒绝")};
const dateText = value => value ? new Date(value).toLocaleString(getLocale()) : t("暂无记录");

function usersContext(node = document.querySelector('#member-results')) {
  const epoch = state.selectionEpoch, session = state.session;
  return () => state.currentSection === 'users' && state.selectionEpoch === epoch
    && state.session === session && Boolean(node?.isConnected)
    && document.getElementById(node?.id) === node;
}

async function renderUsersPage() {
  if (!isAdmin()) { location.hash="#/"; return; }
  state.currentSection="users"; state.project=null; state.asset=null; state.annotations=[];
  state.selectedAnnotation=null; state.poseDraft=null; state.keypointPlacement=null; state.lockOwned=false;
  app.innerHTML=`<div class="app-shell">${topbar("users")}<main class="dashboard member-management-page"><div class="dashboard-inner" id="member-management-root"><div class="page-head"><div><h1>${t("用户管理")}</h1><p class="page-subtitle">${t("审批注册申请，管理成员角色与登录权限。普通用户的项目相互隔离，可通过项目成员功能分享。")}</p></div><div class="member-page-actions"><button class="btn ghost" id="refresh-members">${t("刷新列表")}</button><button class="btn primary" id="add-user">${t("添加成员")}</button></div></div>${state.authMode==="disabled"?`<p class="modal-warning">${t("当前处于开发模式，角色限制未启用。独立账号请使用 accounts 登录模式。")}</p>`:""}<details class="member-role-guide"><summary>${t("不同角色能做什么？")}</summary><dl>${Object.values(MEMBER_ROLES).map(role=>`<div><dt>${role.name}</dt><dd>${role.description}</dd></div>`).join("")}</dl><p>${t("管理员创建的账号首次登录及重置密码后，需要设置自己的密码。自行注册的成员审批通过后，使用注册时设置的密码登录。")}</p></details>
  <div class="member-management-tabs" role="tablist" aria-label="${ta("成员与注册审批")}"><button type="button" id="members-tab" role="tab" data-member-tab="members" aria-controls="members-panel">${t("成员")}</button><button type="button" id="registrations-tab" role="tab" data-member-tab="registrations" aria-controls="registration-panel">${t("注册申请")}<span class="registration-pending-count" id="registration-pending-count" hidden></span></button></div>
  <section id="members-panel" role="tabpanel" aria-labelledby="members-tab"><div class="members-toolbar"><div class="field"><label for="member-search">${t("搜索成员")}</label><input id="member-search" type="search" maxlength="100" placeholder="${ta("账号或显示名称")}" value="${escapeHtml(filters.q)}"/></div><div class="field"><label for="member-role-filter">${t("角色")}</label><select id="member-role-filter"><option value="">${t("全部角色")}</option>${Object.entries(MEMBER_ROLES).map(([value,role])=>`<option value="${value}" ${filters.role===value?"selected":""}>${role.name}</option>`).join("")}</select></div><div class="field"><label for="member-status-filter">${t("账号状态")}</label><select id="member-status-filter"><option value="">${t("全部状态")}</option><option value="true" ${filters.enabled==='true'?'selected':''}>${t("启用")}</option><option value="false" ${filters.enabled==='false'?'selected':''}>${t("停用")}</option></select></div></div><div id="member-results" aria-live="polite"></div></section>
  <section id="registration-panel" role="tabpanel" aria-labelledby="registrations-tab"><p class="registration-intro">${t("批准前核对申请人身份并分配角色。拒绝时填写的原因会显示给申请人。")}</p><div class="members-toolbar registration-toolbar"><div class="field"><label for="registration-search">${t("搜索申请")}</label><input id="registration-search" type="search" maxlength="100" placeholder="${ta("账号或显示名称")}" value="${escapeHtml(registrationFilters.q)}"/></div><div class="field"><label for="registration-status-filter">${t("审批状态")}</label><select id="registration-status-filter">${[['pending',t("待审批")],['approved',t("已批准")],['rejected',t("已拒绝")],['all',t("全部申请")]].map(([value,label])=>`<option value="${value}" ${registrationFilters.status===value?'selected':''}>${label}</option>`).join('')}</select></div></div><p class="registration-review-feedback" id="registration-review-feedback" role="status" hidden></p><div id="registration-results" aria-live="polite"></div></section></div></main></div>`;
  document.querySelector('#add-user').onclick=()=>openUserModal();
  const current = usersContext(document.querySelector('#member-management-root'));
  document.querySelectorAll('[data-member-tab]').forEach(button=>{
    button.onclick=()=>showUsersTab(button.dataset.memberTab);
    button.onkeydown=event=>{
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
      event.preventDefault();const next=event.key==='Home'?'members':event.key==='End'?'registrations':activeTab==='members'?'registrations':'members';
      showUsersTab(next);document.querySelector(`[data-member-tab="${next}"]`).focus();
    };
  });
  showUsersTab(activeTab);
  document.querySelector('#refresh-members').onclick=async event=>{const button=event.currentTarget;button.disabled=true;try{await Promise.allSettled([loadMembers(),loadRegistrations()]);}finally{if(current())button.disabled=false;}};
  let timer, registrationTimer;
  document.querySelector('#member-search').oninput=event=>{filters.q=event.target.value;filters.page=1;clearTimeout(timer);timer=setTimeout(()=>{if(current())void loadMembers();},250);};
  document.querySelector('#member-role-filter').onchange=event=>{filters.role=event.target.value;filters.page=1;void loadMembers();};
  document.querySelector('#member-status-filter').onchange=event=>{filters.enabled=event.target.value;filters.page=1;void loadMembers();};
  document.querySelector('#registration-search').oninput=event=>{registrationFilters.q=event.target.value;registrationFilters.page=1;document.querySelector('#registration-review-feedback').hidden=true;clearTimeout(registrationTimer);registrationTimer=setTimeout(()=>{if(current())void loadRegistrations();},250);};
  document.querySelector('#registration-status-filter').onchange=event=>{registrationFilters.status=event.target.value;registrationFilters.page=1;document.querySelector('#registration-review-feedback').hidden=true;void loadRegistrations();};
  await Promise.allSettled([loadMembers(),loadRegistrations()]);
}

function showUsersTab(tab) {
  activeTab=tab==='registrations'?'registrations':'members';
  document.querySelectorAll('[data-member-tab]').forEach(button=>{const selected=button.dataset.memberTab===activeTab;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;});
  const membersPanel=document.querySelector('#members-panel'), registrationPanel=document.querySelector('#registration-panel');
  if(membersPanel)membersPanel.hidden=activeTab!=='members';if(registrationPanel)registrationPanel.hidden=activeTab!=='registrations';
}

async function loadMembers() {
  const request=++requestId, node=document.querySelector('#member-results'); if (!node || state.currentSection!=='users') return;
  const context=usersContext(node), signature=JSON.stringify(filters), current=()=>request===requestId && context() && JSON.stringify(filters)===signature;
  node.setAttribute('aria-busy','true');
  if(!node.hasChildNodes())node.innerHTML=`<p class="member-count">${t("正在读取成员列表…")}</p>`;
  const query=new URLSearchParams({page:filters.page,page_size:filters.page_size});
  if(filters.q.trim())query.set('q',filters.q.trim()); if(filters.role)query.set('role',filters.role); if(filters.enabled)query.set('enabled',filters.enabled);
  try {
    const result=await api(`/api/users?${query}`);
    if(!current())return;
    members=result.items; adminCount=result.enabled_admin_count;
    const pages=Math.max(1,Math.ceil(result.total/filters.page_size));
    if(filters.page>pages){filters.page=pages;await loadMembers();return;}
    node.innerHTML=`<p class="member-count">${t("共 {0} 位{1}成员", [result.total, filters.q||filters.role||filters.enabled?t("符合条件的"):''])}</p><div class="member-table"><table><thead><tr><th scope="col">${t("成员")}</th><th scope="col">${t("角色")}</th><th scope="col">${t("账号状态")}</th><th scope="col">${t("最近登录")}</th><th scope="col">${t("操作")}</th></tr></thead><tbody>${members.map(user=>`<tr><td data-label="${ta("成员")}"><strong>${escapeHtml(accountDisplayName(user.display_name))}</strong>${user.id===state.session?.user_id?`<span class="member-self">${t("当前账号")}</span>`:''}<small class="member-username">${escapeHtml(user.username)}</small></td><td data-label="${ta("角色")}"><span class="member-role ${escapeHtml(user.role)}">${escapeHtml(MEMBER_ROLES[user.role]?.name || user.role)}</span></td><td data-label="${ta("账号状态")}"><span class="member-status ${user.enabled?'enabled':'disabled'}">${user.enabled?t("已启用"):t("已停用")}</span>${user.must_change_password?`<small class="member-note">${t("下次登录需改密")}</small>`:''}${user.locked_until && new Date(user.locked_until)>new Date()?`<small class="member-note feedback-error">${t("登录暂时受限")}</small>`:''}</td><td data-label="${ta("最近登录")}">${escapeHtml(dateText(user.last_login_at))}</td><td data-label="${ta("操作")}"><div class="member-actions"><button class="btn ghost" data-edit-user="${user.id}">${t("账号设置")}</button><details class="member-more"><summary>${t("更多")}</summary><div>${user.id===state.session?.user_id?`<a href="#/account">${t("修改我的密码")}</a>`:`<button data-reset-user="${user.id}">${t("重置密码")}</button>`}<button data-sessions-user="${user.id}">${t("登录设备")}</button><button data-unlock-user="${user.id}">${t("解除登录限制")}</button><button data-events-user="${user.id}">${t("安全记录")}</button></div></details></div></td></tr>`).join('')}</tbody></table>${members.length?'':`<div class="empty-compact">${t("没有符合条件的成员，请调整筛选条件或添加成员。")}</div>`}</div><div class="member-pagination"><span>${t("第 {0} / {1} 页 · 每页 {2} 位", [filters.page, pages, filters.page_size])}</span><button class="btn ghost" id="members-prev" ${filters.page<=1?'disabled':''}>${t("上一页")}</button><button class="btn ghost" id="members-next" ${filters.page>=pages?'disabled':''}>${t("下一页")}</button></div>`;
    const bind=(attribute,handler)=>node.querySelectorAll(`[${attribute}]`).forEach(button=>button.onclick=()=>{button.closest('details')?.removeAttribute('open');handler(members.find(user=>user.id===Number(button.getAttribute(attribute))));});
    bind('data-edit-user',openUserModal);bind('data-reset-user',openResetModal);bind('data-sessions-user',openMemberSessions);bind('data-events-user',openMemberEvents);bind('data-unlock-user',openUnlockModal);
    node.querySelector('#members-prev').onclick=()=>{filters.page--;void loadMembers();};node.querySelector('#members-next').onclick=()=>{filters.page++;void loadMembers();};
  } catch(error) {
    if(error.status===401 || !current())return;
    node.innerHTML=`<div class="member-load-error" role="alert"><strong>${t("成员列表未能加载")}</strong><p>${escapeHtml(error.message)}</p><button class="btn ghost" id="retry-users">${t("重新加载")}</button></div>`;
    node.querySelector('#retry-users').onclick=loadMembers;
  } finally {if(current())node.removeAttribute('aria-busy');}
}

function registrationNoteMarkup(note) {
  const value=String(note || '').trim();
  if(!value)return `<p class="registration-note empty">${t("未填写申请说明")}</p>`;
  if(value.length<=180)return `<p class="registration-note">${escapeHtml(value)}</p>`;
  return `<p class="registration-note">${escapeHtml(value.slice(0,180))}…</p><details class="registration-full-note"><summary>${t("查看完整申请说明")}</summary><p>${escapeHtml(value)}</p></details>`;
}

function registrationRow(item) {
  const pending=item.status==='pending', status=REGISTRATION_STATES[item.status] || t("未知状态");
  const reviewer=String(item.reviewed_by || '').replace(/^user:/,'');
  return `<article class="registration-request" data-registration-id="${item.id}"><div class="registration-person"><div class="registration-person-heading"><strong>${escapeHtml(item.display_name)}</strong><span class="registration-username">${escapeHtml(item.username)}</span></div>${registrationNoteMarkup(item.note)}<div class="registration-dates"><span>${t("申请于 {0}", [escapeHtml(dateText(item.created_at))])}</span>${item.revision>1&&item.updated_at!==item.created_at?`<span>${t("最近更新 {0}", [escapeHtml(dateText(item.updated_at))])}</span>`:''}</div></div><div class="registration-decision"><span class="registration-status ${escapeHtml(item.status)}">${status}</span>${pending?`<div class="registration-actions"><button type="button" class="btn primary" data-registration-approve="${item.id}">${t("批准申请")}</button><button type="button" class="btn ghost danger" data-registration-reject="${item.id}">${t("拒绝")}</button></div>`:`<p>${item.status==='approved'?`${t("分配角色：")}<strong>${escapeHtml(MEMBER_ROLES[item.role]?.name || item.role || t("未记录"))}</strong>`:t("申请未通过")}${reviewer?`<span>${t("由 {0} 审批", [escapeHtml(reviewer)])}</span>`:''}<span>${escapeHtml(dateText(item.reviewed_at))}</span></p>${item.reason?`<div class="registration-review-reason"><strong>${t("审批原因")}</strong><p>${escapeHtml(item.reason)}</p></div>`:''}`}</div></article>`;
}

async function loadRegistrations() {
  const request=++registrationRequestId, node=document.querySelector('#registration-results');
  if(!node||state.currentSection!=='users')return;
  const context=usersContext(node), signature=JSON.stringify(registrationFilters), current=()=>request===registrationRequestId&&context()&&JSON.stringify(registrationFilters)===signature;
  node.setAttribute('aria-busy','true');
  if(!node.hasChildNodes())node.innerHTML=`<p class="member-count">${t("正在读取注册申请…")}</p>`;
  const query=new URLSearchParams({status:registrationFilters.status,page:registrationFilters.page,page_size:registrationFilters.page_size});
  if(registrationFilters.q.trim())query.set('q',registrationFilters.q.trim());
  try {
    const result=await api(`/api/registration-requests?${query}`);
    if(!current())return;
    registrations=result.items;
    const pending=Number(result.counts?.pending || 0), badge=document.querySelector('#registration-pending-count');
    badge.textContent=String(pending);badge.hidden=false;badge.classList.toggle('empty',pending===0);badge.setAttribute('aria-label',t("{0} 份待审批申请", [pending]));
    const pages=Math.max(1,Math.ceil(result.total/registrationFilters.page_size));
    if(registrationFilters.page>pages){registrationFilters.page=pages;await loadRegistrations();return;}
    const filtered=Boolean(registrationFilters.q.trim()), statusName=registrationFilters.status==='all'?'':REGISTRATION_STATES[registrationFilters.status];
    node.innerHTML=`<div class="registration-count-line"><p class="member-count">${t("共 {0} 份{1}{2}申请", [result.total, filtered?t("符合搜索条件的"):'', statusName || ''])}</p><span>${t("待审批 {0} · 已批准 {1} · 已拒绝 {2}", [pending, Number(result.counts?.approved || 0), Number(result.counts?.rejected || 0)])}</span></div><div class="registration-list">${registrations.length?registrations.map(registrationRow).join(''):`<div class="registration-empty"><strong>${filtered?t("没有找到匹配的申请"):registrationFilters.status==='pending'?t("没有待审批的申请"):t("当前没有这类申请")}</strong><p>${filtered?t("请尝试其他账号或显示名称。"):registrationFilters.status==='pending'?t("新成员可在登录页提交注册申请。你也可以直接添加成员。"):t("切换审批状态可查看其他申请。")}</p></div>`}</div><div class="member-pagination"><span>${t("第 {0} / {1} 页 · 每页 {2} 份", [registrationFilters.page, pages, registrationFilters.page_size])}</span><button type="button" class="btn ghost" id="registrations-prev" ${registrationFilters.page<=1?'disabled':''}>${t("上一页")}</button><button type="button" class="btn ghost" id="registrations-next" ${registrationFilters.page>=pages?'disabled':''}>${t("下一页")}</button></div>`;
    node.querySelectorAll('[data-registration-approve]').forEach(button=>button.onclick=()=>openRegistrationReview(registrations.find(item=>item.id===Number(button.dataset.registrationApprove)),'approve'));
    node.querySelectorAll('[data-registration-reject]').forEach(button=>button.onclick=()=>openRegistrationReview(registrations.find(item=>item.id===Number(button.dataset.registrationReject)),'reject'));
    node.querySelector('#registrations-prev').onclick=()=>{registrationFilters.page--;void loadRegistrations();};
    node.querySelector('#registrations-next').onclick=()=>{registrationFilters.page++;void loadRegistrations();};
  } catch(error) {
    if(error.status===401||!current())return;
    node.innerHTML=`<div class="member-load-error" role="alert"><strong>${t("注册申请未能加载")}</strong><p>${escapeHtml(error.message)}</p><button type="button" class="btn ghost" id="retry-registrations">${t("重新加载")}</button></div>`;
    node.querySelector('#retry-registrations').onclick=loadRegistrations;
  } finally {if(current())node.removeAttribute('aria-busy');}
}

function openRegistrationReview(item, action) {
  if(!item||item.status!=='pending')return;
  const pageCurrent=usersContext(document.querySelector('#member-management-root')), approving=action==='approve';
  let modal;
  const modalCurrent=()=>pageCurrent()&&modal?.isConnected&&document.querySelector('.modal')===modal;
  openModal({title:approving?t("批准注册申请"):t("拒绝注册申请"),subtitle:approving?t("确认身份并分配角色，通过后即可使用注册时设置的密码登录。"):t("原因会显示给申请人，请说明需要补充或调整的内容。"),body:`<div class="registration-review-person"><strong>${escapeHtml(item.display_name)}</strong><span>${escapeHtml(item.username)}</span>${registrationNoteMarkup(item.note)}</div>${approving?`<div class="field"><label for="registration-role">${t("批准后分配的角色")}</label><select id="registration-role">${Object.entries(MEMBER_ROLES).map(([value,role])=>`<option value="${value}" ${value==='user'?'selected':''}>${role.name}</option>`).join('')}</select><p class="hint" id="registration-role-help">${MEMBER_ROLES.user.description}</p></div><details class="registration-role-guide"><summary>${t("查看全部角色权限")}</summary><dl>${Object.values(MEMBER_ROLES).map(role=>`<div><dt>${role.name}</dt><dd>${role.description}</dd></div>`).join('')}</dl></details>`:`<div class="field"><label for="registration-reason">${t("拒绝原因")} <span class="registration-required">${t("必填")}</span></label><textarea id="registration-reason" rows="4" maxlength="1000" placeholder="${ta("例如：请补充所属团队与使用目的后重新申请。")}" autofocus></textarea><p class="hint">${t("最多 1000 个字符。申请人补充信息后可重新提交。")}</p></div>`}`,submitText:approving?t("批准并创建成员"):t("拒绝申请"),onReady:()=>{
    modal=document.querySelector('.modal');modal.classList.add('registration-review-modal');
    if(approving)modal.querySelector('#registration-role').onchange=event=>{modal.querySelector('#registration-role-help').textContent=MEMBER_ROLES[event.target.value].description;};
  },onSubmit:async()=>{
    if(!modalCurrent())return;
    const payload={action,expected_revision:item.revision};
    if(approving){payload.role=modal.querySelector('#registration-role').value;if(!MEMBER_ROLES[payload.role])throw new Error(t("请选择有效的成员角色。"));}
    else {payload.reason=modal.querySelector('#registration-reason').value.trim();if(!payload.reason)throw new Error(t("请填写拒绝原因，方便申请人了解并补充信息。"));if(payload.reason.length>1000)throw new Error(t("拒绝原因最多 1000 个字符。"));}
    try {
      await api(`/api/registration-requests/${item.id}/review`,jsonOptions('POST',payload));
    } catch(error) {
      if(!pageCurrent())return;
      if(error.status!==409)throw error;
      if(modalCurrent())closeModal();
      await Promise.allSettled([loadRegistrations(),loadMembers()]);
      if(!pageCurrent())return;
      const message=error.status === 409?t("这份申请已被审批或重新提交，列表已刷新。请查看最新状态。"):t("{0} 列表已刷新，请核对最新状态。", [error.message]);
      showRegistrationFeedback(message,true);notify(message,'warning');return;
    }
    if(!pageCurrent())return;
    if(modalCurrent())closeModal();
    const message=approving?t("已批准 {0} 成为{1}，可使用注册时设置的密码登录。", [item.display_name, MEMBER_ROLES[payload.role].name]):t("已拒绝 {0} 的申请，原因会显示给申请人。", [item.display_name]);
    showRegistrationFeedback(message);notify(message);
    await Promise.allSettled([loadRegistrations(),loadMembers()]);
  }});
}

function showRegistrationFeedback(message,warning=false) {
  const node=document.querySelector('#registration-review-feedback');if(!node)return;
  node.textContent=message;node.hidden=false;node.classList.toggle('warning',warning);
}

function passwordInput(label=t("初始密码")) {
  return `<div class="field"><label for="user-password">${label}</label><div class="password-entry"><input id="user-password" type="password" autocomplete="new-password" minlength="12" maxlength="256"/><button type="button" id="toggle-member-password" aria-pressed="false">${t("显示")}</button></div><p class="hint">${t("12–256 个字符。请通过可信方式交给成员；首次登录后必须修改。")}</p><button type="button" class="mini-action" id="generate-user-password">${t("生成随机密码")}</button></div>`;
}
function bindPasswordInput() {
  document.querySelector('#toggle-member-password')?.addEventListener('click',event=>{const input=document.querySelector('#user-password'),visible=input.type==='password';input.type=visible?'text':'password';event.currentTarget.textContent=visible?t("隐藏"):t("显示");event.currentTarget.setAttribute('aria-pressed',String(visible));});
  document.querySelector('#generate-user-password')?.addEventListener('click',()=>{
    if(!globalThis.crypto?.getRandomValues)return notify(t("当前浏览器不支持安全随机生成，请自行设置较长的密码。"),'warning');
    const bytes=crypto.getRandomValues(new Uint8Array(20)),alphabet='ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%';
    // Rejection sampling avoids modulo bias in generated passwords.
    let password='';const limit=256-(256%alphabet.length);
    for(const value of bytes)if(value<limit)password+=alphabet[value%alphabet.length];
    while(password.length<20){const more=crypto.getRandomValues(new Uint8Array(1))[0];if(more<limit)password+=alphabet[more%alphabet.length];}
    const input=document.querySelector('#user-password');input.value=password;input.type='text';document.querySelector('#toggle-member-password').textContent=t("隐藏");document.querySelector('#toggle-member-password').setAttribute('aria-pressed','true');
  });
}
function readPassword() {const password=document.querySelector('#user-password').value;if(password.length<12 || password.length>256)throw new Error(t("密码需要 12–256 个字符。"));return password;}
function openUserModal(user=null) {
  const pageCurrent=usersContext();
  const current=user?.id===state.session?.user_id,onlyAdmin=user?.role==='admin' && user.enabled && adminCount===1;
  openModal({title:user?t("{0} 的账号设置", [accountDisplayName(user.display_name)]):t("添加协作成员"),subtitle:user?t("修改显示名称不会退出登录；更改角色或停用账号会立即撤销该成员的登录。"):t("创建后请将账号和初始密码交给成员。"),body:`${user?`<p>${t("登录账号：")}<strong>${escapeHtml(user.username)}</strong>${t("（不可修改）")}</p>`:`<div class="field"><label for="user-name">${t("登录账号")}</label><input id="user-name" autocomplete="off" autocapitalize="none" spellcheck="false" maxlength="64" placeholder="${ta("例如 zhangsan")}"/><p class="hint">${t("2–64 个字符，以字母或数字开头，可包含 . _ -。")}</p></div>`}<div class="field"><label for="user-display">${t("显示名称")}</label><input id="user-display" value="${escapeHtml(accountDisplayName(user?.display_name))}" maxlength="80" placeholder="${ta("例如 张三")}"/></div><div class="field"><label for="user-role">${t("角色")}</label><select id="user-role" ${onlyAdmin?'disabled':''}>${Object.entries(MEMBER_ROLES).map(([value,role])=>`<option value="${value}" ${(user?.role||'user')===value?'selected':''}>${role.name}</option>`).join('')}</select><p class="hint" id="user-role-help">${MEMBER_ROLES[user?.role||'user'].description}</p></div>${onlyAdmin?`<p class="inline-notice">${t("请先添加另一位启用的管理员，才能降级或停用这个账号。")}</p>`:''}${user?`<label class="check-line"><input id="user-enabled" type="checkbox" ${user.enabled?'checked':''} ${onlyAdmin?'disabled':''}/>${t("允许此成员登录")}</label><p class="hint">${t("停用后立即退出该成员的所有登录，已有标注和审核记录仍会保留。")}</p>`:passwordInput()}`,submitText:user?t("保存设置"):t("创建成员账号"),onReady:()=>{bindPasswordInput();document.querySelector('#user-role').onchange=event=>document.querySelector('#user-role-help').textContent=MEMBER_ROLES[event.target.value].description;},onSubmit:async()=>{
    const display_name=accountNameFromInput(document.querySelector('#user-display').value,user?.display_name),role=document.querySelector('#user-role').value;if(!display_name)throw new Error(t("请输入显示名称。"));let payload;
    if(user){const enabled=document.querySelector('#user-enabled').checked;payload={...(display_name!==user.display_name?{display_name}:{}),...(role!==user.role?{role}:{}),...(enabled!==Boolean(user.enabled)?{enabled}:{})};if(!Object.keys(payload).length){closeModal();notify(t("账号设置没有变更。"));return;}}
    else{const username=document.querySelector('#user-name').value.trim().toLowerCase();if(!/^[a-z0-9][a-z0-9_.-]{1,63}$/.test(username))throw new Error(t("账号需要 2–64 个字符，以字母或数字开头，只能包含字母、数字、点、下划线和短横线。"));payload={username,display_name,role,password:readPassword()};}
    const modal=document.querySelector('.modal');
    const saved=await api(user?`/api/users/${user.id}`:'/api/users',jsonOptions(user?'PATCH':'POST',payload));if(modal?.isConnected)closeModal();
    if(current && ('role' in payload || 'enabled' in payload)){signedOut(t("账号权限已更新，请重新登录。"));return;}
    if(current)state.session.display_name=saved.display_name;
    notify(user?t("账号设置已保存。"):t("已创建 {0}，首次登录需设置自己的密码。", [saved.username]));if(pageCurrent())await loadMembers();
  }});
}
function openResetModal(user) {
  const pageCurrent=usersContext();
  openModal({title:t("重置 {0} 的密码", [accountDisplayName(user.display_name)]),subtitle:t("重置会立即退出该成员的所有登录，并要求下次登录修改初始密码。"),body:passwordInput(t("新的初始密码")),submitText:t("重置密码并退出其登录"),onReady:bindPasswordInput,onSubmit:async()=>{
    const modal=document.querySelector('.modal');
    await api(`/api/users/${user.id}`,jsonOptions('PATCH',{password:readPassword()}));if(modal?.isConnected)closeModal();
    if(user.id===state.session?.user_id){signedOut(t("密码已重置，请用新的初始密码重新登录。"));return;}
    notify(t("初始密码已重置，请通过可信方式交给成员。"));if(pageCurrent())await loadMembers();
  }});
}
function openUnlockModal(user) {
  const pageCurrent=usersContext();
  openModal({title:t("解除 {0} 的登录限制", [accountDisplayName(user.display_name)]),subtitle:t("仅清除因密码输错导致的账号限制；不会启用已停用账号，也不会更改密码。"),body:`<p>${t("先确认是成员本人在尝试登录。若忘记密码，请使用“重置密码”。")}</p>`,submitText:t("解除账号限制"),onSubmit:async()=>{const modal=document.querySelector('.modal');await api(`/api/users/${user.id}/unlock`,{method:'POST'});if(modal?.isConnected)closeModal();notify(t("账号登录限制已解除。若同一网络仍受限制，请稍后重试。"));if(pageCurrent())await loadMembers();}});
}
export { renderUsersPage };
