import { t, accountDisplayName, ta } from "./i18n.js?v=0.29.3";
import { state, api, jsonOptions, openModal, escapeHtml, notify } from "cvsprout/app";
import { isAdmin, canManageProject } from "./task-ui.js?v=0.29.3";

export function openProjectMembers() {
  if (!canManageProject()) return;
  const projectId = state.project.id, session = state.session, epoch = state.selectionEpoch;
  let modal, busy = false, loadId = 0;
  const current = () => modal?.isConnected && state.session === session && state.project?.id === projectId && state.selectionEpoch === epoch;
  const base = `/api/projects/${projectId}`;
  const roleOptions = role => `<option value="editor" ${role === "editor" ? "selected" : ""}>${t("协作者")}</option><option value="viewer" ${role === "viewer" ? "selected" : ""}>${t("只读成员")}</option>`;
  async function load() {
    const request = ++loadId;
    const box = modal.querySelector("#project-member-list");
    try {
      const result = await api(`${base}/members`);
      if (!current() || request !== loadId) return;
      box.innerHTML = `<p>${t("所有者：")}<strong>${escapeHtml(accountDisplayName(result.owner?.display_name) || t("待系统管理员分配"))}</strong>${result.owner ? `${t("（")}${escapeHtml(result.owner.username)}${t("）")}` : ""}</p>${result.members.length ? result.members.map(member => `<div class="project-member-row" data-member="${member.id}"><div><strong>${escapeHtml(accountDisplayName(member.display_name))}</strong><small>${escapeHtml(member.username)}${member.enabled ? "" : t(" · 账号已停用")}</small></div><label>${t("项目权限")}<select data-member-role>${roleOptions(member.role)}</select></label><label class="check-line"><input type="checkbox" data-member-export ${member.role === "editor" || member.can_export ? "checked" : ""} ${member.role === "editor" ? "disabled" : ""}/>${t("允许下载与导出")}</label><button type="button" class="btn ghost" data-member-save>${t("保存")}</button><button type="button" class="btn ghost danger" data-member-remove>${t("移除")}</button></div>`).join("") : `<p class="hint">${t("尚未分享给其他用户。只有所有者和系统管理员能访问。")}</p>`}`;
      box.querySelectorAll("[data-member]").forEach(row => {
        const member = result.members.find(item => item.id === Number(row.dataset.member));
        row.querySelector("[data-member-role]").onchange = event => { const input = row.querySelector("[data-member-export]"); input.disabled = event.target.value === "editor"; input.checked = event.target.value === "editor"; };
        row.querySelector("[data-member-save]").onclick = () => action(async () => {
          await api(`${base}/members`, jsonOptions("PUT", { username: member.username, role: row.querySelector("[data-member-role]").value, can_export: row.querySelector("[data-member-role]").value === "viewer" && row.querySelector("[data-member-export]").checked }));
        }, t("项目权限已更新"));
        row.querySelector("[data-member-remove]").onclick = () => action(() => api(`${base}/members/${member.id}`, { method: "DELETE" }), t("已移除成员，已有标注记录保留"));
      });
    } catch (error) {
      if (!current() || request !== loadId) return;
      box.innerHTML = `<p role="alert">${escapeHtml(error.message)}</p><button type="button" class="btn ghost" data-members-retry>${t("重新加载")}</button>`;
      box.querySelector("[data-members-retry]").onclick = load;
    }
  }
  async function action(operation, message) {
    if (!current() || busy) return;
    busy = true;
    modal.querySelectorAll("button").forEach(button => { button.disabled = true; });
    try { await operation(); if (current()) { notify(message); await load(); } }
    catch (error) { if (current()) notify(error.message, "error"); }
    finally { busy = false; if (current()) modal.querySelectorAll("button").forEach(button => { button.disabled = false; }); }
  }
  openModal({ title: t("项目成员"), subtitle: t("只分享当前项目。协作者可以上传、标注、审核和训练；仅所有者可以管理成员或删除项目。"), wide: true,
    body: `<section id="project-member-list" aria-live="polite">${t("正在加载成员…")}</section><hr/><div class="field"><label for="project-share-username">${t("添加用户的完整登录账号")}</label><input id="project-share-username" autocomplete="off" maxlength="64" placeholder="${ta("例如 zhangsan")}"/></div><div class="field"><label for="project-share-role">${t("项目权限")}</label><select id="project-share-role">${roleOptions("editor")}</select></div><label class="check-line"><input id="project-share-export" type="checkbox" disabled/>${t("允许只读成员下载与导出")}</label><p class="hint">${t("协作者可下载与导出。移除或降为只读后，其编辑占用会释放，未完成的 AI 标注会取消；已有成果保留。")}</p>${isAdmin() ? `<details><summary>${t("系统管理员：分配项目所有者")}</summary><p class="hint">${t("转交后，原所有者不再自动拥有访问权，其他分享成员保持原权限。")}</p><div class="field"><label for="project-owner-username">${t("新所有者的完整登录账号")}</label><input id="project-owner-username" autocomplete="off" maxlength="64"/></div><button type="button" class="btn ghost" id="assign-project-owner">${t("分配所有者")}</button></details>` : ""}`,
    submitText: t("添加 / 更新成员"),
    onReady: () => {
      modal = document.querySelector(".modal");
      modal.querySelector("#project-share-role").onchange = event => { modal.querySelector("#project-share-export").disabled = event.target.value === "editor"; };
      modal.querySelector("#assign-project-owner")?.addEventListener("click", () => {
        const username = modal.querySelector("#project-owner-username").value.trim();
        if (!username) return notify(t("请填写新所有者的登录账号"), "error");
        void action(async () => { await api(`${base}/owner`, jsonOptions("PUT", { username })); }, t("项目所有者已更新"));
      });
      void load();
    },
    onSubmit: async () => {
      if (!current() || busy) return;
      const username = modal.querySelector("#project-share-username").value.trim();
      if (!username) throw new Error(t("请填写要添加的完整登录账号。"));
      await action(async () => {
        await api(`${base}/members`, jsonOptions("PUT", { username, role: modal.querySelector("#project-share-role").value, can_export: modal.querySelector("#project-share-export").checked }));
        if (current()) modal.querySelector("#project-share-username").value = "";
      }, t("项目已分享"));
    },
  });
}
