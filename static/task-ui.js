import { state, icon, api, jsonOptions, escapeHtml, canEditCurrentAsset, notify, renderWorkspace, refreshInspector, refreshShapes, refreshWorkflowState, refreshCanvasStatus, saveAnnotation, defaultAttributes, drawDraft, setSaveFeedback } from "./app.js?v=0.21.0";
/* Task-specific annotation controls; the shared workspace owns state and persistence. */
const TASK_LABELS = { mixed: "检测 / 分割", detect: "目标检测", segment: "实例分割", classify: "图片分类", pose: "关键点" };
const TASK_GUIDANCE = {
  detect: ["用矩形框找到图片中的物体。", "例如：框出行人、车辆或安全帽。"],
  segment: ["沿物体边缘标出完整轮廓，区分每一个物体。", "例如：标出零件边缘或叶片形状。"],
  classify: ["为整张图片选择一个类别，无需画框。", "例如：区分产品“合格”和“不合格”。"],
  pose: ["标出物体上的固定位置，用点描述姿态或结构。", "例如：人体的肩、肘、膝，或零件的四个角。"],
};
const COCO_KEYPOINTS = {
  names: ["nose", "left_eye", "right_eye", "left_ear", "right_ear", "left_shoulder", "right_shoulder", "left_elbow", "right_elbow", "left_wrist", "right_wrist", "left_hip", "right_hip", "left_knee", "right_knee", "left_ankle", "right_ankle"],
  skeleton: [[0,1],[0,2],[1,3],[2,4],[5,6],[5,7],[7,9],[6,8],[8,10],[5,11],[6,12],[11,12],[11,13],[13,15],[12,14],[14,16]],
  flip_idx: [0,2,1,4,3,6,5,8,7,10,9,12,11,14,13,16,15],
};
let pendingKeypointVisibility = 2;
function projectTask() { return state.project?.task_type || "mixed"; }
function isAdmin() { return state.authMode === "disabled" || state.session?.role === "admin"; }
function canReview() { return isAdmin() || state.session?.role === "reviewer"; }
function taskAllowsTool(tool) {
  return tool === "select" || ({ mixed: ["rectangle","polygon","positive","negative"], detect:["rectangle"], segment:["polygon","positive","negative"], classify:[], pose:["keypoints"] }[projectTask()] || []).includes(tool);
}
function modelFitsProject(model) { return projectTask() === "mixed" ? (model.tasks || ["detect","segment"]).some(task => ["detect","segment"].includes(task)) : (model.tasks || []).includes(projectTask()); }
function taskFieldsMarkup() {
  return `<div class="field"><label for="project-task">标注任务类型</label><select id="project-task" aria-describedby="project-task-guide">${["detect","segment","classify","pose"].map(task=>`<option value="${task}">${TASK_LABELS[task]}</option>`).join("")}</select><div class="task-purpose" id="project-task-guide" aria-live="polite"><strong>${TASK_GUIDANCE.detect[0]}</strong><span>${TASK_GUIDANCE.detect[1]}</span></div></div>
    <div id="project-keypoint-fields" class="keypoint-setup" hidden>
      <div class="field"><label for="project-keypoint-preset">关键点方案</label><select id="project-keypoint-preset"><option value="human">人体 17 点（已配好，可直接使用）</option><option value="custom">自定义物体的关键点</option></select><p class="hint" id="project-keypoint-preset-hint">适用于人体姿态模型，包含鼻子、眼睛、肩、肘、腕、髋、膝和脚踝。</p></div>
      <details class="setup-details" id="project-keypoint-config"><summary>查看或调整关键点</summary><div class="setup-details-body">
        <div class="field"><label for="project-keypoint-names">需要标出的点（每行一个，或用逗号分隔）</label><textarea id="project-keypoint-names" aria-describedby="project-keypoint-order-hint" placeholder="例如：左上角，右上角，右下角，左下角">${COCO_KEYPOINTS.names.join(", ")}</textarea><p class="hint" id="project-keypoint-order-hint">按标注顺序填写，每张图片都使用这一顺序。使用已有 AI 模型时，点的数量和顺序要与模型一致。</p></div>
        <ol class="keypoint-order" id="project-keypoint-order" aria-label="关键点标注顺序"></ol>
        <details class="setup-details advanced-keypoint-settings" id="project-keypoint-advanced"><summary>高级设置：骨架连线与左右翻转</summary><div class="setup-details-body">
          <p class="hint">只标出点的位置时可以留空。已有 AI 模型请使用它对应的配置。</p>
          <div class="field"><label for="project-skeleton">骨架连线（可选）</label><textarea id="project-skeleton" spellcheck="false">${JSON.stringify(COCO_KEYPOINTS.skeleton)}</textarea><p class="hint">骨架是点之间的连线。填写 JSON 数组，编号从 0 开始，例如 [[0,1]] 连接第 1 和第 2 个点。</p></div>
          <div class="field"><label for="project-flip">左右翻转对应关系（可选）</label><textarea id="project-flip" spellcheck="false">${JSON.stringify(COCO_KEYPOINTS.flip_idx)}</textarea><p class="hint">填写每个点在左右翻转后对应的编号，例如 [1,0,2] 交换前两个点。留空时，训练不使用左右翻转。</p></div>
        </div></details>
      </div></details>
    </div>`;
}
function bindTaskFields() {
  const taskSelect = document.querySelector("#project-task");
  if (!taskSelect) return;
  const names = document.querySelector("#project-keypoint-names");
  const skeleton = document.querySelector("#project-skeleton");
  const flip = document.querySelector("#project-flip");
  const preset = document.querySelector("#project-keypoint-preset");
  let previousPreset = preset.value;
  const drafts = { human:{names:names.value,skeleton:skeleton.value,flip:flip.value}, custom: { names: "", skeleton: "", flip: "" } };
  const updatePresetHint = () => {
    document.querySelector("#project-keypoint-preset-hint").textContent = preset.value === "custom" ? "每张图片都按下面的名称和顺序标注。没有特殊要求时，高级设置可保持为空。" : "适用于人体姿态模型，包含鼻子、眼睛、肩、肘、腕、髋、膝和脚踝。";
  };
  const updateOrder = () => {
    const values = names.value.split(/[,，\n]/).map(name=>name.trim()).filter(Boolean);
    document.querySelector("#project-keypoint-order").innerHTML = values.map((name,index)=>`<li><span>${index+1}</span>${escapeHtml(name)}</li>`).join("");
  };
  const updateTask = () => {
    const [purpose, example] = TASK_GUIDANCE[taskSelect.value];
    document.querySelector("#project-task-guide").innerHTML = `<strong>${purpose}</strong><span>${example}</span>`;
    document.querySelector("#project-keypoint-fields").hidden = taskSelect.value !== "pose";
  };
  taskSelect.addEventListener("change", updateTask);
  [names,skeleton,flip].forEach(field=>field.addEventListener("input",()=>{
    field.removeAttribute("aria-invalid");
    if (preset.value==="human") { preset.value="custom"; previousPreset="custom"; updatePresetHint(); }
    if (field===names) updateOrder();
  }));
  preset.addEventListener("change", () => {
    drafts[previousPreset] = {names:names.value,skeleton:skeleton.value,flip:flip.value};
    const values = drafts[preset.value] || {names:COCO_KEYPOINTS.names.join(", "),skeleton:JSON.stringify(COCO_KEYPOINTS.skeleton),flip:JSON.stringify(COCO_KEYPOINTS.flip_idx)};
    names.value = values.names; skeleton.value = values.skeleton; flip.value = values.flip;
    document.querySelector("#project-keypoint-config").open = preset.value === "custom";
    previousPreset = preset.value; updateOrder(); updatePresetHint();
    if (preset.value === "custom") names.focus();
  });
  updateTask(); updateOrder();
}
function taskFieldError(id, message) {
  const field = document.querySelector(id);
  for (let parent=field.parentElement; parent; parent=parent.parentElement) {
    if (parent.tagName === "DETAILS") parent.open=true;
  }
  field.setAttribute("aria-invalid","true");
  field.focus();
  throw new Error(message);
}
function readTaskFields() {
  const task_type = document.querySelector("#project-task").value;
  if (task_type !== "pose") return {task_type};
  const names=document.querySelector("#project-keypoint-names").value.split(/[,，\n]/).map(name=>name.trim()).filter(Boolean);
  if (!names.length) taskFieldError("#project-keypoint-names", "请至少填写一个关键点名称，例如“左上角”。");
  if (names.length>200) taskFieldError("#project-keypoint-names", "关键点最多可设置 200 个，请减少数量。");
  if (new Set(names).size!==names.length) taskFieldError("#project-keypoint-names", "关键点名称不能重复，请为每个位置使用不同的名称。");
  if (names.some(name=>name.length>80)) taskFieldError("#project-keypoint-names", "每个关键点名称最多 80 个字符，请缩短名称。");
  let skeleton, flip_idx;
  try { skeleton=JSON.parse(document.querySelector("#project-skeleton").value.trim() || "[]"); }
  catch (_) { taskFieldError("#project-skeleton", "骨架连线格式不正确。请按 [[0,1],[1,2]] 填写，不需要连线时可留空。"); }
  if (!Array.isArray(skeleton) || skeleton.length>1000 || skeleton.some(pair=>!Array.isArray(pair) || pair.length!==2 || pair.some(index=>!Number.isInteger(index) || index<0 || index>=names.length) || pair[0]===pair[1])) {
    taskFieldError("#project-skeleton", `骨架连线需连接两个不同的点，编号范围为 0 到 ${names.length-1}。更改点的数量后，请同时更新连线，或将连线留空。`);
  }
  try { flip_idx=JSON.parse(document.querySelector("#project-flip").value.trim() || "null"); }
  catch (_) { taskFieldError("#project-flip", "左右翻转格式不正确。请填写编号数组，例如 [1,0,2]，不使用时可留空。"); }
  if (flip_idx!==null && (!Array.isArray(flip_idx) || flip_idx.length!==names.length || new Set(flip_idx).size!==names.length || flip_idx.some((index,i)=>!Number.isInteger(index) || index<0 || index>=names.length || flip_idx[index]!==i))) {
    taskFieldError("#project-flip", "左右翻转对应关系与关键点不匹配。每个点需有一个对应编号，左右点需互相对应；不使用时可留空。");
  }
  return {task_type, keypoint_schema:{names,skeleton,flip_idx}};
}
function annotationToolsMarkup(editable) {
  if (projectTask() === "classify") return `<span class="manual-classification-note"><strong>选类别即保存</strong><span>无需画框或先运行 AI</span></span>`;
  const enabled = editable && !state.savingAnnotation && !state.workflowBusy && !state.pendingAnnotation && !hasTaskSaveDraft() && !state.pendingObjectEdit && !state.shapeEdit && !state.advancedAnnotationDirty;
  const draftTool = state.poseDraft ? "keypoints" : state.polygon?.length ? "polygon" : state.drawing ? "rectangle" : null;
  const tools = [["rectangle", "box", "矩形框", "R", "按住鼠标拖出矩形，松开后自动保存"], ["polygon", "polygon", "多边形", "P", "沿轮廓逐点点击，至少三个点后按 Enter 完成"], ["keypoints", "target", "关键点", "K", "先拖动框选对象，再按名称顺序点击关键点"]];
  const manual = tools.filter(([tool]) => taskAllowsTool(tool)).map(([tool, glyph, name, key, hint]) => `<button class="btn ghost manual-annotation-tool ${state.tool === tool ? "active" : ""}" data-tool="${tool}" title="${name} (${key})：${hint}，无需 AI" aria-label="${name} (${key})" aria-pressed="${state.tool === tool}" ${enabled && (!draftTool || draftTool === tool) ? "" : "disabled"}>${icon(glyph)}<span>${name}</span><kbd>${key}</kbd></button>`).join("");
  const ai = [["positive", "+", "保留点"], ["negative", "−", "排除点"]].filter(([tool]) => taskAllowsTool(tool)).map(([tool, sign, name]) => `<button class="btn ghost manual-annotation-tool ai-point-tool ${state.tool === tool ? "active" : ""}" data-tool="${tool}" title="给 AI 的分割提示：${name}，还需运行 AI 辅助才会产生标注" aria-label="AI 提示：${name}" aria-pressed="${state.tool === tool}" ${enabled && (!draftTool || draftTool === tool) ? "" : "disabled"}><span aria-hidden="true">${sign}</span>${name}</button>`).join("");
  return `<span class="manual-tool-group" role="group" aria-label="手动绘制工具">${manual}</span>${ai ? `<span class="annotation-ai-points" role="group" aria-label="AI 分割提示"><small>AI 提示</small>${ai}</span>` : ""}`;
}
function taskInspectorMarkup() {
  if (state.poseDraft) {
    const draft = state.poseDraft, names = state.project.keypoint_schema.names;
    const visible = draft.keypoints.filter(point => point.visibility).length;
    const completed = draft.nextIndex >= names.length;
    const editable = canEditCurrentAsset() && !state.workflowBusy;
    const label = state.project.labels.find(item => item.id === draft.labelId) || state.activeLabel;
    return `<section class="inspector-section pose-progress manual-task-guide"><h3>手动标注关键点</h3><p class="manual-draft-class">对象类别：${escapeHtml(label?.name || "未选择")}</p>${state.savingAnnotation ? `<p role="status">正在保存关键点，位置会保留到保存成功。</p>` : completed ? `<p><strong>已完成这一轮定位</strong></p>` : `<p>第 ${draft.nextIndex + 1} / ${names.length} 个点：在图片上点击 <strong>${escapeHtml(names[draft.nextIndex])}</strong>。</p>`}<p class="hint">已定位 ${visible} 个点。无法判断位置的点可跳过，保存为“未标注”；至少需要定位一个点。</p>${draft.saveError ? `<p class="manual-draft-error" role="status">这份草稿尚未保存，框和关键点都已保留。请重试保存，也可返回上一点修改。</p>` : ""}<div class="manual-pose-actions"><button class="btn ghost" id="skip-keypoint" ${editable && !completed ? "" : "disabled"}>跳过此点（未标注）</button><button class="btn ghost" id="undo-keypoint" ${editable && draft.nextIndex > 0 ? "" : "disabled"}>上一个点 <kbd>⌫</kbd></button><button class="btn primary" id="finish-keypoints" ${editable && visible ? "" : "disabled"}>${draft.saveError ? "重试保存关键点" : completed ? "保存关键点" : "保存已定位点"} <kbd>Enter</kbd></button></div><p class="hint">最后一点完成后自动保存。提前保存时，其余点保留为未标注；<kbd>Esc</kbd> 取消整份草稿。</p></section>`;
  }
  if (projectTask() === "classify") {
    const pending = currentTaskEdit();
    const unsaved = pending?.kind === "classification" || state.pendingAnnotation;
    const nextLabel = pending?.labelName || (state.pendingAnnotation ? state.activeLabel?.name : "");
    return `<section class="inspector-section classification-summary manual-task-guide"><h3>这张图片的类别</h3><strong>${escapeHtml(state.annotations[0]?.label_name || "尚未分类")}</strong>${unsaved ? `<p class="manual-draft-error" role="status">待保存的选择：${escapeHtml(nextLabel || "所选类别")}。上方显示的仍是已保存结果。</p>${pending?.kind === "classification" ? `<button class="btn primary" data-retry-task-save ${canEditCurrentAsset() && !state.workflowBusy ? "" : "disabled"}>重试保存类别</button>` : `<p class="hint">请使用画布上方的“重试保存”，或取消未保存的选择。</p>`}` : ""}<p class="hint">直接点击类别、使用顶部类别选择框，或按类别旁的数字键即可分类并自动保存，无需先运行 AI。每张图片只选一个类别。</p>${state.readOnly || !state.lockOwned || ["submitted", "approved"].includes(state.asset?.review_status) ? `<p class="hint">当前图片只读，请按页面提示获取编辑权限或重新打开修改。</p>` : ""}</section>`;
  }
  const purpose = { detect: ["手动画矩形框", "先选择类别，再拖动框住物体，松开鼠标自动保存。", "R"], segment: ["手动画物体轮廓", "先选择类别，沿边缘逐点点击；至少三个点后按 Enter 完成。", "P"], pose: ["手动标注关键点", "先选择类别，按 K 后拖动框住对象，再按点名依次定位。", "K"], mixed: ["手动绘制标注", "先选择类别，用矩形框定位物体，或用多边形沿边缘描出轮廓。", "R / P"] }[projectTask()];
  return purpose ? `<section class="inspector-section manual-task-guide"><h3>${purpose[0]} <kbd>${purpose[2]}</kbd></h3><p>${purpose[1]}</p><p class="hint">手动标注不依赖 AI。AI 标注与提示点是另一种可选辅助方式。</p></section>` : "";
}
function keypointInspectorMarkup(annotation) {
  if (annotation.shape_type!=="keypoints") return "";
  const pending = currentTaskEdit();
  const ownPending = pending?.kind === "keypoints" && pending.annotationId === annotation.id;
  const keypoints = ownPending ? pending.keypoints : annotation.keypoints;
  const editable = canEditCurrentAsset() && !state.workflowBusy && !state.pendingAnnotation && !hasTaskSaveDraft() && !state.pendingObjectEdit && !state.advancedAnnotationDirty;
  return `<div class="keypoint-editor"><h4>关键点位置</h4>${ownPending ? `<p class="manual-draft-error" role="status">关键点修改尚未保存，当前显示待保存的位置。可重试；取消修改会恢复上次结果。</p><button class="btn primary" data-retry-task-save ${canEditCurrentAsset() && !state.workflowBusy ? "" : "disabled"}>重试保存关键点</button>` : ""}${keypoints.map((point,index)=>`<div class="keypoint-row ${state.keypointPlacement === index ? "placing" : ""}"><button class="mini-action" data-place-keypoint="${index}" ${editable?"":"disabled"} title="点击后在图片上重新定位 ${escapeHtml(state.project.keypoint_schema.names[index])}">${escapeHtml(state.project.keypoint_schema.names[index])}${state.keypointPlacement === index ? "（请在图上定位）" : ""}</button><select data-keypoint-visibility="${index}" aria-label="${escapeHtml(state.project.keypoint_schema.names[index])} 的可见状态" ${editable?"":"disabled"}>${[[2,"可见"],[1,"被遮挡"],[0,"未标注"]].map(([value,name])=>`<option value="${value}" ${point.visibility===value?"selected":""}>${name}</option>`).join("")}</select></div>`).join("")}<p class="hint">拖动图片上的点，或点击点名后重新定位。被遮挡但能判断位置时选“被遮挡”；无法判断位置时选“未标注”。</p></div>`;
}
function keypointShapesMarkup(item, selected) {
  const pending = currentTaskEdit();
  const scale=1/Math.max(0.005,state.zoom), points=pending?.kind === "keypoints" && pending.annotationId === item.id ? pending.keypoints : item.keypoints || [];
  let markup=(state.project.keypoint_schema?.skeleton || []).map(([a,b])=>points[a]?.visibility && points[b]?.visibility ? `<line x1="${points[a].x}" y1="${points[a].y}" x2="${points[b].x}" y2="${points[b].y}" stroke="${item.label_color}" stroke-width="2" vector-effect="non-scaling-stroke" pointer-events="none"/>` : "").join("");
  markup+=points.map((point,index)=>point.visibility?`<circle class="${selected && canEditCurrentAsset()?"shape-handle":"shape"}" data-annotation-id="${item.id}" data-handle-index="${1000+index}" cx="${point.x}" cy="${point.y}" r="${5*scale}" stroke="${item.label_color}" stroke-width="2" fill="${point.visibility===1?"none":"white"}" vector-effect="non-scaling-stroke"><title>${escapeHtml(state.project.keypoint_schema.names[index])}</title></circle>`:"").join("");
  return markup;
}
function taskContext() {
  return { projectId: state.project?.id, assetId: state.asset?.id, epoch: state.selectionEpoch, session: state.session };
}
function taskContextCurrent(context) {
  return state.currentSection === "annotate" && state.authReady !== false && state.project?.id === context.projectId
    && state.asset?.id === context.assetId && state.selectionEpoch === context.epoch && state.session === context.session;
}
function currentTaskEdit() {
  return state.pendingTaskEdit && taskContextCurrent(state.pendingTaskEdit) ? state.pendingTaskEdit : null;
}
function hasTaskSaveDraft() { return Boolean(currentTaskEdit()); }
function syncTaskControls() {
  if (projectTask() === "classify" && !state.pendingAnnotation && !hasTaskSaveDraft() && !state.savingAnnotation) {
    const saved = state.annotations.find(item => item.shape_type === "classification");
    if (saved) state.activeLabel = state.project?.labels.find(item => item.id === saved.label_id) || state.activeLabel;
  }
  const label = document.querySelector("#manual-label");
  const editable = canEditCurrentAsset() && !state.workflowBusy && !state.pendingAnnotation && !hasTaskSaveDraft() && !state.pendingObjectEdit && !state.poseDraft && !state.drawing && !state.polygon?.length && !state.shapeEdit && !state.advancedAnnotationDirty;
  const selected = projectTask() === "classify" ? (currentTaskEdit()?.labelId || state.pendingAnnotation?.labelId || (state.savingAnnotation ? state.activeLabel?.id : state.annotations.find(item => item.shape_type === "classification")?.label_id)) : state.activeLabel?.id;
  if (label) { label.value = selected ? String(selected) : ""; label.disabled = !editable; }
  const draftTool = state.poseDraft ? "keypoints" : state.polygon?.length ? "polygon" : state.drawing ? "rectangle" : null;
  const toolsEnabled = canEditCurrentAsset() && !state.pendingAnnotation && !hasTaskSaveDraft() && !state.pendingObjectEdit && !state.shapeEdit && !state.advancedAnnotationDirty;
  document.querySelectorAll(".manual-annotation-tool").forEach(button => { button.disabled = !toolsEnabled || Boolean(draftTool && button.dataset.tool !== draftTool); });
}
function refreshTaskState() {
  refreshShapes(); refreshInspector(); refreshWorkflowState(); refreshCanvasStatus(); syncTaskControls();
}
function discardTaskSaveDraft() {
  const pending = currentTaskEdit();
  state.pendingTaskEdit = null;
  if (pending?.kind === "classification") {
    const saved = state.annotations.find(item => item.shape_type === "classification");
    state.activeLabel = state.project?.labels.find(label => label.id === saved?.label_id) || state.activeLabel;
  }
}
function applyTaskUpdate(updated, kind) {
  const index = state.annotations.findIndex(item => item.id === updated.id);
  if (index >= 0) state.annotations[index] = updated;
  if (kind === "classification" || state.selectedAnnotation?.id === updated.id) state.selectedAnnotation = updated;
  state.asset.revision = updated.asset_revision; state.asset.review_status = "draft"; state.asset.negative_confirmed = false;
  const asset = state.assets.find(item => item.id === state.asset.id);
  if (asset) { asset.revision = updated.asset_revision; asset.review_status = "draft"; asset.negative_confirmed = false; }
}
async function persistTaskEdit(pending) {
  if (!taskContextCurrent(pending) || !canEditCurrentAsset() || state.workflowBusy || state.pendingObjectEdit || state.advancedAnnotationDirty) return null;
  state.pendingTaskEdit = pending;
  state.savingAnnotation = true;
  const classification = pending.kind === "classification";
  setSaveFeedback("saving", classification ? "正在保存分类…" : "正在保存关键点…");
  refreshTaskState();
  let saved = null;
  try {
    const payload = classification ? { expected_version: pending.version, label_id: pending.labelId, attributes: pending.attributes } : { expected_version: pending.version, keypoints: pending.keypoints };
    const updated = await api(`/api/annotations/${pending.annotationId}`, jsonOptions("PATCH", payload));
    if (!taskContextCurrent(pending) || state.pendingTaskEdit !== pending) return null;
    applyTaskUpdate(updated, pending.kind); state.pendingTaskEdit = null;
    setSaveFeedback("saved", classification ? "分类已保存" : "关键点已保存");
    saved = updated;
    return updated;
  } catch (error) {
    if (!taskContextCurrent(pending) || state.pendingTaskEdit !== pending) return null;
    pending.error = error.message;
    setSaveFeedback("error", `${classification ? "分类选择" : "关键点修改"}未确认保存，修改内容已保留。请重试保存，或取消修改以恢复已保存结果。${error.message}`);
    return null;
  } finally {
    if (taskContextCurrent(pending)) {
      state.savingAnnotation = false;
      if (saved && classification) renderWorkspace(); else refreshTaskState();
      syncTaskControls();
    }
  }
}
async function retryTaskSave() {
  const pending = currentTaskEdit();
  if (!pending || state.savingAnnotation || state.workflowBusy) return null;
  return persistTaskEdit(pending);
}
async function chooseTaskLabel(labelId) {
  const label = state.project?.labels.find(item => item.id === labelId);
  if (!label) { syncTaskControls(); return; }
  if (state.savingAnnotation || state.workflowBusy) { syncTaskControls(); return; }
  if (state.pendingAnnotation || hasTaskSaveDraft() || state.pendingObjectEdit || state.advancedAnnotationDirty || state.poseDraft || state.keypointPlacement != null) {
    syncTaskControls(); return notify("请先保存或取消当前草稿，再切换类别。", "warning");
  }
  if (projectTask() === "classify" && !canEditCurrentAsset()) { syncTaskControls(); return notify("当前图片不可编辑，请先按页面提示开启编辑。", "warning"); }
  state.activeLabel = label;
  if (projectTask() !== "classify") { refreshInspector(); return; }
  const existing = state.annotations.find(item => item.shape_type === "classification");
  if (!existing) return saveAnnotation("classification", [], [], labelId);
  if (existing.label_id === labelId) { refreshInspector(); return; }
  return persistTaskEdit({ ...taskContext(), kind: "classification", annotationId: existing.id, version: existing.version, labelId, labelName: label.name, attributes: defaultAttributes(label) });
}
function poseCanvasClick(point) {
  if (!canEditCurrentAsset() || state.workflowBusy) return false;
  if ((state.pendingAnnotation && !state.poseDraft) || hasTaskSaveDraft()) return true;
  if (state.keypointPlacement!=null && state.selectedAnnotation) {
    const item=state.selectedAnnotation, keypoints=item.keypoints.map(value=>({...value}));
    if (!keypoints[state.keypointPlacement]) { state.keypointPlacement = null; return true; }
    keypoints[state.keypointPlacement]={x:Number(point.x.toFixed(2)),y:Number(point.y.toFixed(2)),visibility:pendingKeypointVisibility};
    state.keypointPlacement=null; void saveKeypoints(item,keypoints); return true;
  }
  if (!state.poseDraft) return false;
  const draft=state.poseDraft;
  if (draft.nextIndex >= draft.keypoints.length) return true;
  draft.keypoints[draft.nextIndex]={x:Number(point.x.toFixed(2)),y:Number(point.y.toFixed(2)),visibility:2};
  draft.nextIndex++;
  if(draft.nextIndex>=draft.keypoints.length) void finishKeypoints();
  else { drawDraft(); refreshInspector(); }
  return true;
}
async function finishKeypoints() {
  const draft=state.poseDraft;
  if (!draft || !canEditCurrentAsset() || state.workflowBusy || hasTaskSaveDraft()) return null;
  if (!draft.keypoints.some(point=>point.visibility)) {
    draft.nextIndex=0; refreshInspector();
    return notify("请至少定位一个关键点，已返回第一个点","error");
  }
  const context = taskContext();
  const saved = await saveAnnotation("keypoints", draft.points, draft.keypoints, draft.labelId || state.activeLabel?.id);
  if (!taskContextCurrent(context) || state.poseDraft !== draft) return saved;
  if (saved) state.poseDraft = null;
  else draft.saveError = true;
  drawDraft(); refreshTaskState();
  return saved;
}
async function saveKeypoints(item,keypoints) {
  if (!canEditCurrentAsset() || state.workflowBusy || state.pendingAnnotation || hasTaskSaveDraft()) return null;
  return persistTaskEdit({ ...taskContext(), kind: "keypoints", annotationId: item.id, version: item.version, keypoints: keypoints.map(point => ({ ...point })) });
}
function undoKeypointDraft() {
  const draft = state.poseDraft;
  if (!draft || !canEditCurrentAsset() || state.workflowBusy || draft.nextIndex <= 0) return false;
  draft.nextIndex = Math.min(draft.nextIndex, draft.keypoints.length) - 1;
  draft.keypoints[draft.nextIndex] = { x: 0, y: 0, visibility: 0 };
  draft.saveError = false;
  setSaveFeedback("idle"); drawDraft(); refreshTaskState();
  return true;
}
function bindTaskInspector() {
  syncTaskControls();
  document.querySelector("#skip-keypoint")?.addEventListener("click",()=>{
    const draft = state.poseDraft;
    if (!draft || !canEditCurrentAsset() || state.workflowBusy || draft.nextIndex >= draft.keypoints.length) return;
    draft.keypoints[draft.nextIndex] = { x: 0, y: 0, visibility: 0 }; draft.nextIndex++;
    if(draft.nextIndex>=draft.keypoints.length) void finishKeypoints(); else { drawDraft(); refreshInspector(); }
  });
  document.querySelector("#undo-keypoint")?.addEventListener("click", undoKeypointDraft);
  document.querySelector("#finish-keypoints")?.addEventListener("click",finishKeypoints);
  document.querySelectorAll("[data-retry-task-save]").forEach(button => { button.onclick = retryTaskSave; });
  document.querySelectorAll("[data-place-keypoint]").forEach(button=>button.onclick=()=>{
    if (!canEditCurrentAsset() || state.workflowBusy || state.pendingAnnotation || hasTaskSaveDraft() || !state.selectedAnnotation) return;
    const index = Number(button.dataset.placeKeypoint), point = state.selectedAnnotation.keypoints?.[index];
    if (!point) return;
    state.keypointPlacement=index; pendingKeypointVisibility=point.visibility || 2; state.tool="select"; refreshInspector(); refreshCanvasStatus(); notify("请在图片上点击关键点的新位置，定位后自动保存");
  });
  document.querySelectorAll("[data-keypoint-visibility]").forEach(select=>select.onchange=()=>{
    if (!canEditCurrentAsset() || state.workflowBusy || state.pendingAnnotation || hasTaskSaveDraft() || !state.selectedAnnotation) { refreshInspector(); return; }
    const item=state.selectedAnnotation, keypoints=item.keypoints.map(point=>({...point})), index=Number(select.dataset.keypointVisibility), visibility=Number(select.value);
    if (!keypoints[index]) return;
    if (!keypoints[index].visibility && visibility) { state.keypointPlacement=index; pendingKeypointVisibility=visibility; state.tool="select"; refreshInspector(); refreshCanvasStatus(); notify("请在图片上点击这个点的位置，定位后才会保存"); return; }
    state.keypointPlacement = null;
    keypoints[index]=visibility?{...keypoints[index],visibility}:{x:0,y:0,visibility:0}; void saveKeypoints(item,keypoints);
  });
}
function exportFormatMarkup(name,task) {
  const formats=task==="classify"?[["yolo","按类别分文件夹","适合分类训练，下载为 ZIP"],["json","完整标注 JSON","保留标注信息，方便备份"]]:[["yolo","YOLO 格式","适合使用 YOLO 训练"],["coco","COCO 格式","适合支持 COCO 的训练工具"],["json","完整标注 JSON","保留标注信息，方便备份"]];
  return `<div class="scope-row export-format-options">${formats.map(([value,label,description],index)=>`<label class="scope-option"><input type="radio" name="${name}" value="${value}" ${index===0?"checked":""}/><span>${label}<small>${description}</small></span></label>`).join("")}</div>`;
}

export { TASK_LABELS, projectTask, isAdmin, canReview, taskAllowsTool, modelFitsProject, taskFieldsMarkup, bindTaskFields, readTaskFields, annotationToolsMarkup, taskInspectorMarkup, keypointInspectorMarkup, keypointShapesMarkup, chooseTaskLabel, poseCanvasClick, finishKeypoints, undoKeypointDraft, hasTaskSaveDraft, retryTaskSave, discardTaskSaveDraft, bindTaskInspector, exportFormatMarkup };
