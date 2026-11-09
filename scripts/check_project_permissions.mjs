import { withI18n, stripI18nImports } from './i18n-test-support.mjs';
// Verify actual project controls for the new account and membership roles.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = stripI18nImports(readFileSync(new URL("../static/task-ui.js", import.meta.url), "utf8"));
const code = source.slice(source.indexOf("function isAdmin()"), source.indexOf("function taskAllowsTool("));
const state = { authMode: "accounts", session: { role: "user" }, project: null };
const context = vm.createContext(withI18n({ state }));
vm.runInContext(code, context);
const project = (role, edit, manage, canExport) => ({ project_role: role, permissions: { edit, manage, export: canExport } });
const owner = project("owner", true, true, true);
const editor = project("editor", true, false, true);
const viewer = project("viewer", false, false, false);

for (const [active, edit, manage, canExport] of [[owner, true, true, true], [editor, true, false, true], [viewer, false, false, false]]) {
  state.project = active;
  assert.equal(context.isAdmin(), false, "Project rights must not promote account administration");
  assert.equal(context.canEditProject(), edit);
  assert.equal(context.canReview(), edit);
  assert.equal(context.canManageProject(), manage);
  assert.equal(context.canExportProject(), canExport);
}
state.project = owner;
assert.equal(context.canManageProject(editor), false, "Project cards use each project's rights");
assert.equal(context.canEditProject(viewer), false);
state.project = { ...viewer, permissions: { ...viewer.permissions, export: true } };
assert.equal(context.canExportProject(), true);
assert.equal(context.canEditProject(), false, "Export permission must not allow editing");
assert.equal(context.canReview(), false);
state.project = owner;
state.session.role = "admin";
assert.equal(context.isAdmin(), true);
assert.equal(context.canManageProject(), true);
state.project = viewer;
assert.equal(context.canEditProject(), false, "Fresh project permissions override stale UI privileges");
console.log("PASS: account roles, per-project management, collaboration, read-only and export controls");
