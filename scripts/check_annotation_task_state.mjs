// Run with: node scripts/check_annotation_task_state.mjs
// Exercise the real task controls with deferred requests; no server or database is used.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

const source = readFileSync(new URL("../static/task-ui.js", import.meta.url), "utf8")
  .replace(/^import[^\n]*\n/, "")
  .replace(/^export \{[^\n]*\};?$/m, "");

function setup(task = "classify") {
  const state = {
    project: { id: 1, task_type: task, labels: [{ id: 1, name: "甲" }, { id: 2, name: "乙" }], keypoint_schema: { names: ["左", "右"], skeleton: [[0, 1]] } },
    asset: { id: 10, revision: 1, review_status: "draft" }, assets: [],
    annotations: [{ id: 7, version: 1, label_id: 1, label_name: "甲", shape_type: task === "classify" ? "classification" : "keypoints", keypoints: [{ x: 5, y: 6, visibility: 2 }, { x: 7, y: 8, visibility: 2 }] }],
    selectionEpoch: 4, session: { user_id: 1 }, authReady: true, currentSection: "annotate",
    lockOwned: true, readOnly: false, savingAnnotation: false, workflowBusy: false,
    pendingTaskEdit: null, pendingAnnotation: null, tool: "select", zoom: 1,
  };
  state.assets = [state.asset]; state.activeLabel = state.project.labels[0]; state.selectedAnnotation = state.annotations[0];
  let resolve, reject;
  const response = new Promise((success, failure) => { resolve = success; reject = failure; });
  const events = [];
  const context = {
    state, document: { querySelector: () => null, querySelectorAll: () => [] },
    api: () => response, jsonOptions: (method, body) => ({ method, body }), escapeHtml: String, icon: () => "",
    canEditCurrentAsset: () => Boolean(state.asset && state.lockOwned && !state.savingAnnotation && !state.readOnly),
    notify: (...args) => events.push(["notify", ...args]), renderWorkspace: () => events.push(["render"]),
    refreshInspector: () => events.push(["inspector"]), refreshShapes: () => events.push(["shapes"]),
    refreshWorkflowState: () => events.push(["workflow"]), refreshCanvasStatus: () => events.push(["canvas"]),
    saveAnnotation: () => response, defaultAttributes: () => ({}), drawDraft: () => events.push(["draw"]),
    setSaveFeedback: (...args) => { state.saveFeedback = args; events.push(["feedback", ...args]); },
  };
  vm.createContext(context); vm.runInContext(source, context);
  return { state, context, events, resolve, reject };
}

for (const task of ["classify", "pose"]) {
  for (const fails of [false, true]) {
    const fixture = setup(task);
    const operation = vm.runInContext(task === "classify" ? "chooseTaskLabel(2)" : "saveKeypoints(state.selectedAnnotation,[{x:30,y:40,visibility:2},{x:7,y:8,visibility:2}])", fixture.context);
    assert.equal(fixture.state.savingAnnotation, true);
    const newAsset = { id: 20, revision: 99, review_status: "draft" }, newAnnotations = [{ id: 80, label_id: 1 }], newPending = { marker: "new operation" };
    fixture.state.asset = newAsset; fixture.state.assets = [newAsset]; fixture.state.annotations = newAnnotations;
    fixture.state.selectedAnnotation = newAnnotations[0]; fixture.state.selectionEpoch++;
    fixture.state.pendingTaskEdit = newPending; fixture.state.saveFeedback = ["saving", "new image"];
    const eventCount = fixture.events.length;
    if (fails) fixture.reject(new Error("old request failed"));
    else fixture.resolve({ id: 7, label_id: 2, asset_revision: 2 });
    await operation;
    assert.equal(fixture.state.asset, newAsset); assert.equal(fixture.state.annotations, newAnnotations);
    assert.equal(fixture.state.pendingTaskEdit, newPending); assert.equal(fixture.state.savingAnnotation, true);
    assert.deepEqual(fixture.state.saveFeedback, ["saving", "new image"]); assert.equal(fixture.events.length, eventCount);
  }
}

const classification = setup();
const save = vm.runInContext("chooseTaskLabel(2)", classification.context);
await vm.runInContext("chooseTaskLabel(1)", classification.context);
assert.equal(classification.state.activeLabel.id, 2, "A second category click must not change an in-flight save");
classification.resolve({ id: 7, label_id: 2, asset_revision: 2 }); await save;

const pose = setup("pose");
pose.state.poseDraft = { points: [1, 1, 9, 9], keypoints: [{ x: 3, y: 4, visibility: 2 }, { x: 0, y: 0, visibility: 0 }], nextIndex: 1, labelId: 1 };
const finish = vm.runInContext("finishKeypoints()", pose.context), newDraft = { marker: "new draft" };
pose.state.poseDraft = newDraft; pose.state.selectionEpoch++;
pose.resolve({ id: 90 }); await finish;
assert.equal(pose.state.poseDraft, newDraft, "A late creation response must not clear another image's pose draft");

const undo = setup("pose");
undo.state.poseDraft = { points: [1, 1, 9, 9], keypoints: [{ x: 3, y: 4, visibility: 2 }, { x: 7, y: 8, visibility: 2 }], nextIndex: 2, labelId: 1, saveError: true };
const frozen = { expectedRevision: 5 };
undo.state.pendingAnnotation = frozen;
assert.equal(vm.runInContext("undoKeypointDraft()", undo.context), true);
assert.equal(undo.state.poseDraft.nextIndex, 1);
assert.equal(undo.state.poseDraft.keypoints[1].visibility, 0);
assert.equal(undo.state.pendingAnnotation, frozen, "Undo must retain the failed save's original revision for conflict detection");

console.log("PASS: 4 late classification/pose PATCH responses preserve the new image; concurrent category clicks are ignored; late pose creation preserves the new draft; pose undo retains revision protection.");
