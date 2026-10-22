const TASKS = { detect: "目标检测", segment: "实例分割", pose: "关键点", classify: "图片分类", mixed: "检测 / 分割" };
const TOOLS = { detect: [["R", "画矩形框"]], segment: [["P", "逐点画轮廓"]], pose: [["K", "框选对象，再依次定位关键点"]], classify: [], mixed: [["R", "画矩形框"], ["P", "逐点画轮廓"]] };
const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);

export function annotationShortcutHint(task) {
  const draw = { detect: "R 画框", segment: "P 画轮廓", pose: "K 标关键点", classify: "数字键选类别", mixed: "R 画框 / P 画轮廓" }[task] || "V 选择";
  return `${draw} · A / D 切图 · ? 查看快捷键`;
}

export function annotationShortcutsHelp(task, escapeHtml = escape, labels = []) {
  const esc = escapeHtml;
  const editing = [...(TOOLS[task] || TOOLS.mixed), ["0–9", task === "classify" ? "按类别旁的数字分类，选择后立即保存" : "选中对象时修改其类别；未选中时选择新对象类别"]];
  if (["segment", "mixed", "pose"].includes(task)) editing.push(["Enter", task === "pose" ? "完成关键点并保存；未定位的点保留为未标注" : "至少三个顶点后，完成轮廓并保存"], ["Backspace / Ctrl（⌘）+ Z", "撤销草稿最后一个点，不删除已保存对象"]);
  editing.push(["Enter", "保存失败后，重试当前未保存的标注"], ["Esc", "取消当前绘制、定位或拖动；没有草稿时取消选中"], ["Delete / Backspace（⌫）", task === "classify" ? "清除这张图片已保存的类别（图片须可编辑）" : "无绘制草稿时，删除选中的已保存对象（图片须可编辑）"]);
  const groups = [
    ["查看图片", [["右键 / 点击类别标签", "选择已有对象；右键可改类别、调整边界或选择重叠对象"], ["V", "选择对象，校正已有标注"], ["H", "切换到移动画布工具"], ["按住空格 + 拖动", "临时移动图片，松开空格恢复原工具"], ["滚轮 / + / −", "缩放图片，不改变标注坐标"], ["F", "适配窗口，显示整张图片"]]],
    ["人工标注与校正", editing],
    ["审核已提交图片", [["Y", "审核通过，成功后自动进入下一张待审核图片"], ["X", "打开退回修改对话框；原因可不填，确认退回后继续下一张"], ["审核权限", "仅管理员和审核员可用；有未保存修改时请先完成或取消"]]],
    ["切换与复制", [["A / ←", "上一张图片"], ["D / →", "下一张图片"], ["N", "当前筛选内下一张待校正图片"], ["Ctrl / ⌘ + C", "复制选中对象（选中文本时仍复制文本）"], ["Ctrl / ⌘ + V", "粘贴到当前项目的可编辑图片"], ["?", "打开本页快捷键帮助"]]],
  ];
  const assigned = labels.filter(label => /^[0-9]$/.test(String(label.shortcut || "")));
  return `<div class="annotation-shortcuts-help"><p class="shortcuts-intro">当前任务：<strong>${esc(TASKS[task] || TASKS.mixed)}</strong>。快捷键只在标注页面生效，输入文字、操作表单或打开对话框时不会修改标注。</p>${groups.map(([title, rows]) => `<section class="shortcuts-group"><h3>${esc(title)}</h3><dl>${rows.map(([keys, text]) => `<div><dt><kbd>${esc(keys)}</kbd></dt><dd>${esc(text)}</dd></div>`).join("")}</dl></section>`).join("")}${assigned.length ? `<section class="shortcuts-group shortcuts-labels"><h3>本项目的类别数字键</h3><ul>${assigned.map(label => `<li><kbd>${esc(label.shortcut)}</kbd><span>${esc(label.name)}</span></li>`).join("")}</ul></section>` : '<p class="hint">本项目暂未配置类别数字键，可直接点击类别选择。</p>'}<p class="shortcuts-note">只读图片仍可查看、缩放和切图。草稿未完成时仍可平移和缩放；切换图片或开始其他标注前，请先保存或按 Esc 取消。按住按键不会连续保存、删除或切图。Ctrl / ⌘ + R、P、F、S 等浏览器快捷键保持原行为。</p></div>`;
}

export function installAnnotationShortcuts(deps) {
  const { state } = deps;
  const win = deps.window || window, doc = deps.document || document;
  let pending = false;
  const editor = () => state.currentSection === "annotate" && Boolean(state.project);
  const busy = () => pending || state.savingAnnotation || state.workflowBusy;
  const draft = () => deps.hasDraft ? deps.hasDraft() : Boolean(state.pendingAnnotation || state.pendingTaskEdit || state.pendingObjectEdit || state.advancedAnnotationDirty || state.drawing || state.shapeEdit || state.polygon?.length || state.poseDraft || state.keypointPlacement != null);
  const blockedDraft = () => { if (!draft()) return false; deps.notify("当前仍有未保存的修改。请先完成或重试保存，或按 Esc 取消，再执行此操作。", "warning"); return true; };
  const writable = () => !busy() && deps.canEditCurrentAsset();
  const readOnlyMessage = () => deps.notify(state.readOnlyReason || "当前图片不可编辑。请先取得编辑权限；已提交或已通过的图片需要重新打开后才能修改。", "warning");
  const canvas = () => doc.querySelector("#canvas-scroll");
  const resetSpace = () => { state.spacePressed = false; canvas()?.classList.toggle("pan-ready", state.tool === "pan"); };
  const run = async callback => {
    if (busy()) return;
    pending = true;
    try { await callback(); }
    catch (error) { deps.notify(error?.message || "操作未完成，请检查当前标注后重试。", "error"); }
    finally { pending = false; }
  };
  function openHelp() {
    if (!editor() || deps.hasModal?.() || deps.navigationIsOpen?.()) return;
    resetSpace();
    deps.openModal({ title: "标注快捷键", subtitle: "也可以点击页面上的工具按钮完成相同操作。", body: annotationShortcutsHelp(deps.projectTask(), deps.escapeHtml, state.project.labels || []), submitText: "知道了", onReady: () => {
      const modal = doc.querySelector(".modal"); modal?.classList.add("annotation-shortcuts-dialog");
    }, onSubmit: () => deps.closeModal() });
  }
  const editableTarget = element => Boolean(element?.closest?.('input,textarea,select,[contenteditable=""],[contenteditable="true"],[role="textbox"],[role="combobox"],[role="spinbutton"],[role="slider"]') || element?.isContentEditable);
  const protectedTarget = element => Boolean(element?.closest?.('.workspace-navigation,.workspace-topbar,.section-tabs,[role="dialog"],[inert]'));
  const nativeControl = element => Boolean(element?.closest?.('button,a[href],summary,[role="button"],[role="tab"],[role="menuitem"]'));
  const chooseLabel = key => (state.project.labels || []).find(label => /^[0-9]$/.test(String(label.shortcut || "")) && String(label.shortcut) === key);
  const consume = event => event.preventDefault();
  async function keydown(event) {
    if (event.defaultPrevented || event.isComposing || event.keyCode === 229 || !editor() || deps.hasModal?.() || deps.navigationIsOpen?.()) return;
    const focus = doc.activeElement, target = event.target;
    if (editableTarget(focus) || editableTarget(target) || protectedTarget(focus) || protectedTarget(target)) return;
    const key = event.key, lower = key.toLowerCase(), command = event.ctrlKey || event.metaKey;
    // Browser / OS combinations are reserved, except the editor's exact copy, paste and draft-undo chords.
    if (event.altKey || command && event.shiftKey) return;
    if (command) {
      if (lower === "c" && state.selectedAnnotation && !win.getSelection?.()?.toString()) {
        consume(event); if (event.repeat || busy()) return;
        state.copiedAnnotation = { ...JSON.parse(JSON.stringify(state.selectedAnnotation)), _copiedProjectId: state.project.id };
        deps.notify("已复制选中对象，可在本项目的可编辑图片上粘贴。");
      } else if (lower === "v" && state.copiedAnnotation) {
        consume(event); if (event.repeat || busy()) return;
        if (!writable()) return readOnlyMessage();
        if (blockedDraft()) return;
        if (state.copiedAnnotation._copiedProjectId !== state.project.id) return deps.notify("请在当前项目重新复制对象，再粘贴到图片上。", "warning");
        await run(deps.pasteCopiedAnnotation);
      } else if (lower === "z" && (state.poseDraft || state.polygon?.length)) {
        consume(event); if (event.repeat || !writable()) return;
        if (state.poseDraft) { deps.undoKeypointDraft(); return; }
        if (state.pendingAnnotation || state.pendingTaskEdit) return deps.notify("这份标注尚未确认保存，请先重试或按 Esc 取消。", "warning");
        deps.undoPolygonPoint();
      }
      return;
    }
    // Space, Enter, arrows and deletion keep their native meaning on focused controls.
    if ((nativeControl(focus) || nativeControl(target)) && [" ", "Enter", "ArrowLeft", "ArrowRight", "Backspace", "Delete"].includes(key)) return;
    if (event.shiftKey && [" ", "Enter", "ArrowLeft", "ArrowRight", "Backspace", "Delete"].includes(key)) return;
    if (key === "?" && !event.repeat) { consume(event); openHelp(); return; }
    if (!state.asset) return;
    if (["y", "x"].includes(lower)) {
      if (state.asset.review_status !== "submitted" || !deps.canReview?.() || !deps.updateWorkflow) return;
      consume(event); if (event.repeat || busy() || blockedDraft()) return;
      await run(() => deps.updateWorkflow(lower === "y" ? "approve" : "reject")); return;
    }
    if (key === "Escape") {
      consume(event); resetSpace(); if (event.repeat || busy()) return;
      if (state.pan) deps.stopCanvasPan();
      else if (state.shapeEdit) deps.cancelShapeEdit();
      else { const hadDraft = draft(); deps.cancelDrawing(true); if (!hadDraft) deps.clearObjectSelection?.(); deps.refreshInspector(); }
      return;
    }
    if (event.code === "Space" || key === " ") {
      consume(event); if (busy() || state.shapeEdit || state.drawing) return;
      state.spacePressed = true; canvas()?.classList.add("pan-ready"); return;
    }
    if (["+", "=", "-"].includes(key)) {
      consume(event); if (!state.shapeEdit && !state.drawing) deps.setCanvasZoom(state.zoom * (key === "-" ? 0.8 : 1.25)); return;
    }
    if (lower === "f") { consume(event); if (!event.repeat && !state.shapeEdit && !state.drawing) deps.fitCanvas(); return; }
    const tool = { v: "select", h: "pan", r: "rectangle", p: "polygon", k: "keypoints" }[lower];
    if (tool) {
      if (tool !== "pan" && !deps.taskAllowsTool(tool)) return;
      consume(event); if (event.repeat || busy()) return;
      if (!["select", "pan"].includes(tool) && !writable()) return readOnlyMessage();
      if (state.tool === tool && !(state.selectionIntent && ["rectangle", "polygon", "keypoints"].includes(tool))) return;
      deps.setTool(tool); return;
    }
    if (["ArrowLeft", "ArrowRight"].includes(key) || ["a", "d", "n"].includes(lower)) {
      consume(event); if (event.repeat || busy() || blockedDraft()) return;
      await run(() => deps.navigateAsset(key === "ArrowLeft" || lower === "a" ? -1 : 1, lower === "n")); return;
    }
    if (key === "Enter") {
      if (!state.pendingAnnotation && !state.pendingTaskEdit && !state.pendingObjectEdit && !state.poseDraft && !state.polygon?.length) return;
      consume(event); if (event.repeat || busy()) return;
      if (!writable()) return readOnlyMessage();
      if (state.pendingAnnotation || state.pendingTaskEdit || state.pendingObjectEdit) await run(deps.retryPendingAnnotation);
      else if (state.poseDraft) await run(deps.finishKeypoints);
      else if (state.polygon.length >= 6) await run(deps.finishPolygon);
      else deps.notify("轮廓至少需要三个顶点。请继续点击图片，或按 Esc 取消草稿。", "warning");
      return;
    }
    if (key === "Backspace" && (state.poseDraft || state.polygon?.length)) {
      consume(event); if (event.repeat || !writable()) return;
      if (state.poseDraft) { deps.undoKeypointDraft(); return; }
      if (state.pendingAnnotation || state.pendingTaskEdit) return deps.notify("这份标注尚未确认保存，请先重试或按 Esc 取消。", "warning");
      deps.undoPolygonPoint();
      return;
    }
    if (["Delete", "Backspace"].includes(key) && (state.selectedAnnotation || deps.projectTask() === "classify" && state.annotations?.some(item => item.shape_type === "classification"))) {
      consume(event); if (event.repeat || busy()) return;
      if (!writable()) return readOnlyMessage();
      if (blockedDraft()) return;
      await run(deps.deleteSelectedAnnotation); return;
    }
    const label = chooseLabel(key);
    if (label) {
      consume(event); if (event.repeat || busy()) return;
      if (!writable()) return readOnlyMessage();
      if (draft()) return deps.notify("请先完成或取消当前草稿，再切换类别。", "warning");
      if (deps.projectTask() === "classify" && state.annotations?.[0]?.label_id === label.id) return;
      await run(() => deps.chooseTaskLabel(label.id));
    }
  }
  const keyup = event => { if (event.code === "Space" || event.key === " ") resetSpace(); };
  const blur = () => { resetSpace(); if (state.pan) deps.stopCanvasPan(); if (state.shapeEdit) deps.cancelShapeEdit(); if (state.drawing && !state.drawing.complete && !state.pendingAnnotation && !state.pendingTaskEdit) deps.cancelDrawing(); };
  win.addEventListener("keydown", keydown); win.addEventListener("keyup", keyup); win.addEventListener("blur", blur);
  return { openHelp, destroy() { win.removeEventListener("keydown", keydown); win.removeEventListener("keyup", keyup); win.removeEventListener("blur", blur); resetSpace(); } };
}
