/* Geometry follows SVG paint order: the last annotation is the topmost object. */
function nearSegment(point, ax, ay, bx, by, tolerance) {
  const dx = bx - ax, dy = by - ay;
  const length = dx * dx + dy * dy;
  const position = length ? Math.max(0, Math.min(1, ((point.x - ax) * dx + (point.y - ay) * dy) / length)) : 0;
  return Math.hypot(point.x - ax - position * dx, point.y - ay - position * dy) <= tolerance;
}

export function findAnnotationsAtPoint(annotations, point, tolerance = 0) {
  if (!Array.isArray(annotations) || !Number.isFinite(point?.x) || !Number.isFinite(point?.y)) return [];
  const margin = Number.isFinite(tolerance) ? Math.max(0, tolerance) : 0;
  return [...annotations].reverse().filter(annotation => {
    const points = annotation?.points;
    if (!Array.isArray(points) || !points.every(Number.isFinite)) return false;
    if (["rectangle", "keypoints"].includes(annotation.shape_type)) {
      if (points.length !== 4) return false;
      const left = Math.min(points[0], points[2]), right = Math.max(points[0], points[2]);
      const top = Math.min(points[1], points[3]), bottom = Math.max(points[1], points[3]);
      return point.x >= left - margin && point.x <= right + margin && point.y >= top - margin && point.y <= bottom + margin;
    }
    if (annotation.shape_type !== "polygon" || points.length < 6 || points.length % 2) return false;
    let inside = false;
    for (let current = 0, previous = points.length - 2; current < points.length; previous = current, current += 2) {
      const ax = points[previous], ay = points[previous + 1], bx = points[current], by = points[current + 1];
      if (nearSegment(point, ax, ay, bx, by, margin)) return true;
      if ((ay > point.y) !== (by > point.y) && point.x < (bx - ax) * (point.y - ay) / (by - ay) + ax) inside = !inside;
    }
    return inside;
  });
}

let menuSequence = 0;

export function createAnnotationContextMenu(deps) {
  let layer = null, panel = null, candidates = [], selectedId = null, reason = "", previousFocus = null;
  let busy = false, generation = 0, destroyed = false, anchor = { x: 0, y: 0 };
  let removeListeners = () => {}, removeClickSuppression = () => {};
  const identifier = `annotation-context-${++menuSequence}`;
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  };
  const selected = () => candidates.find(item => item.id === selectedId);
  const editable = () => !reason && Boolean(deps.canEditCurrentAsset());
  const objectName = annotation => {
    const index = deps.state?.annotations?.findIndex(item => item.id === annotation.id) ?? -1;
    const number = index >= 0 ? index + 1 : candidates.indexOf(annotation) + 1;
    const label = deps.state?.project?.labels?.find(item => item.id === annotation.label_id);
    return `${label?.name || annotation.label_name || "未命名类别"} · 对象 ${number}`;
  };
  const focusable = () => [...(panel?.querySelectorAll("button:not(:disabled),select:not(:disabled),[tabindex='0']") || [])]
    .filter(node => node.getClientRects().length);
  const focusFirst = () => (focusable()[0] || panel)?.focus({ preventScroll: true });

  function position() {
    if (!panel) return;
    const viewport = window.visualViewport;
    const left = viewport?.offsetLeft || 0, top = viewport?.offsetTop || 0;
    const width = viewport?.width || window.innerWidth, height = viewport?.height || window.innerHeight;
    const gap = 8;
    panel.style.maxWidth = `${Math.max(0, width - gap * 2)}px`;
    panel.style.maxHeight = `${Math.max(0, height - gap * 2)}px`;
    const rect = panel.getBoundingClientRect();
    panel.style.left = `${Math.max(left + gap, Math.min(anchor.x, left + width - rect.width - gap))}px`;
    panel.style.top = `${Math.max(top + gap, Math.min(anchor.y, top + height - rect.height - gap))}px`;
  }

  function close({ restoreFocus = true, force = false } = {}) {
    if (!panel || (busy && !force)) return false;
    const focus = previousFocus;
    generation++; removeListeners(); removeListeners = () => {};
    layer.remove(); layer = null; panel = null; candidates = []; selectedId = null; busy = false; previousFocus = null;
    if (restoreFocus && focus?.isConnected) focus.focus({ preventScroll: true });
    deps.onClose?.();
    return true;
  }

  function swallowNextClick() {
    removeClickSuppression();
    let timer;
    const stop = event => { event.preventDefault(); event.stopImmediatePropagation(); removeClickSuppression(); };
    const nextGesture = () => removeClickSuppression();
    removeClickSuppression = () => {
      clearTimeout(timer);
      ["click", "auxclick", "contextmenu"].forEach(type => document.removeEventListener(type, stop, true));
      document.removeEventListener("pointerdown", nextGesture, true);
      removeClickSuppression = () => {};
    };
    ["click", "auxclick", "contextmenu"].forEach(type => document.addEventListener(type, stop, true));
    document.addEventListener("pointerdown", nextGesture, true);
    timer = setTimeout(removeClickSuppression, 1200);
  }

  function setBusy(value, message = "") {
    busy = value;
    if (!panel) return;
    panel.setAttribute("aria-busy", String(value));
    panel.querySelectorAll("button,select").forEach(node => { node.disabled = value || node.dataset.disabled === "true"; });
    const status = panel.querySelector(".annotation-context-status");
    status.textContent = message;
    status.hidden = !message;
    if (value) panel.focus({ preventScroll: true });
    position();
  }

  async function runAction(callback, message, needsEdit = false) {
    if (!panel || busy) return;
    if (needsEdit && !editable()) { render(); return; }
    const currentPanel = panel, token = generation;
    setBusy(true, message);
    try {
      const result = await callback();
      if (panel !== currentPanel || generation !== token) return;
      if (result !== false) { close({ restoreFocus: false, force: true }); return; }
      busy = false; render(); setBusy(false, "操作未完成，请查看页面提示。");
    } catch (_) {
      if (panel === currentPanel && generation === token) {
        busy = false; render(); setBusy(false, "操作未完成，请查看页面提示。");
      }
    } finally {
      if (panel === currentPanel && generation === token && !busy) focusFirst();
    }
  }

  function render() {
    if (!panel) return;
    const annotation = selected();
    if (!annotation) { close({ force: true }); return; }
    panel.replaceChildren();
    const header = element("div", "annotation-context-header");
    const heading = element("h2", "", "对象操作"); heading.id = `${identifier}-heading`;
    const dismiss = element("button", "annotation-context-close", "×"); dismiss.type = "button";
    dismiss.setAttribute("aria-label", "关闭对象操作菜单"); dismiss.title = "关闭 (Esc)";
    dismiss.addEventListener("click", () => close()); header.append(heading, dismiss); panel.append(header);
    const body = element("div", "annotation-context-body"); panel.append(body);
    if (candidates.length > 1) {
      const label = element("label", "annotation-context-field", `此处有 ${candidates.length} 个对象，选择要操作的对象`);
      const select = element("select"); select.id = `${identifier}-target`; label.htmlFor = select.id;
      candidates.forEach(item => {
        const option = element("option", "", objectName(item)); option.value = String(item.id); option.selected = item.id === selectedId; select.append(option);
      });
      select.addEventListener("change", () => {
        const next = candidates.find(item => String(item.id) === select.value);
        if (!next || busy || deps.onSelect(next.id) === false) { select.value = String(selectedId); return; }
        selectedId = next.id; render(); panel?.querySelector(`#${identifier}-target`)?.focus({ preventScroll: true });
      });
      label.append(select); body.append(label);
    }
    body.append(element("p", "annotation-context-target", objectName(annotation)));
    const writable = editable();
    if (!writable) body.append(element("p", "annotation-context-readonly", reason || "当前图片只读，可以查看或复制对象。"));
    const labels = deps.state?.project?.labels || [];
    const category = element("label", "annotation-context-field", "对象类别（选择后自动保存）");
    const categorySelect = element("select"); categorySelect.id = `${identifier}-label`; category.htmlFor = categorySelect.id;
    categorySelect.disabled = !writable || !labels.length; categorySelect.dataset.disabled = String(categorySelect.disabled);
    labels.forEach(label => {
      const option = element("option", "", label.name); option.value = String(label.id); option.selected = label.id === annotation.label_id; categorySelect.append(option);
    });
    categorySelect.addEventListener("change", () => {
      const label = labels.find(item => String(item.id) === categorySelect.value);
      if (!label || label.id === annotation.label_id) return;
      void runAction(() => deps.onChangeLabel(annotation.id, label.id), "正在保存类别…", true);
    });
    category.append(categorySelect); body.append(category);
    const actions = element("div", "annotation-context-actions");
    const addAction = (text, name, callback, disabled, needsEdit = false) => {
      const button = element("button", `annotation-context-action${name === "delete" ? " danger" : ""}`, text);
      button.type = "button"; button.dataset.contextAction = name; button.disabled = disabled; button.dataset.disabled = String(disabled);
      button.addEventListener("click", () => { void runAction(callback, name === "delete" ? "正在删除对象…" : "正在处理…", needsEdit); });
      actions.append(button);
    };
    addAction("移动 / 调整边界", "edit", () => deps.onEdit(annotation.id), !writable, true);
    addAction("复制对象", "copy", () => deps.onCopy(annotation.id), false);
    addAction("删除对象", "delete", () => deps.onDelete(annotation.id), !writable, true);
    addAction("继续绘制", "continue", () => deps.onContinue(), !writable, true);
    body.append(actions);
    const status = element("p", "annotation-context-status"); status.setAttribute("role", "status"); status.hidden = true; body.append(status);
    position();
  }

  function open({ annotations, selectedId: requestedId, clientX, clientY, readOnlyReason = "" }) {
    if (destroyed || busy) return false;
    if (panel) close({ restoreFocus: false });
    candidates = (annotations || []).filter(item => ["rectangle", "polygon", "keypoints"].includes(item?.shape_type));
    if (!candidates.length) return false;
    selectedId = candidates.find(item => item.id === requestedId)?.id ?? candidates[0].id;
    previousFocus = document.activeElement;
    if (deps.onSelect(selectedId) === false) { candidates = []; selectedId = null; previousFocus = null; return false; }
    reason = String(readOnlyReason || ""); anchor = { x: Number.isFinite(clientX) ? clientX : 8, y: Number.isFinite(clientY) ? clientY : 8 };
    generation++; layer = element("div", "annotation-context-layer"); panel = element("section", "annotation-context-menu");
    panel.setAttribute("role", "dialog"); panel.setAttribute("aria-modal", "true"); panel.setAttribute("aria-busy", "false"); panel.setAttribute("aria-labelledby", `${identifier}-heading`); panel.tabIndex = -1;
    layer.append(panel); document.body.append(layer); render();
    const stopPropagation = event => event.stopPropagation();
    const keydown = event => {
      event.stopPropagation();
      if (event.key === "Escape") { event.preventDefault(); event.stopImmediatePropagation(); close(); return; }
      if (event.key === "Tab") {
        event.preventDefault();
        const choices = focusable(), index = choices.indexOf(document.activeElement);
        const next = event.shiftKey ? (index <= 0 ? choices.length - 1 : index - 1) : (index + 1) % choices.length;
        (choices[next] || panel)?.focus({ preventScroll: true });
      }
    };
    const outside = event => {
      if (!panel || panel.contains(event.target)) return;
      event.preventDefault(); event.stopImmediatePropagation();
      if (busy) return;
      swallowNextClick(); close();
    };
    const focus = event => { if (panel && !panel.contains(event.target)) focusFirst(); };
    // Selecting an object can reposition the canvas or inspector while layout settles.
    // Only viewport scrolling dismisses here; intentional outside-wheel gestures
    // are already handled by the transparent layer below.
    const scroll = event => {
      if (event.target === document || event.target === document.scrollingElement) close({ restoreFocus: false });
    };
    const leave = () => { if (!close({ restoreFocus: false })) position(); };
    const wheel = event => {
      event.stopPropagation();
      if (panel && !panel.contains(event.target)) { event.preventDefault(); close({ restoreFocus: false }); }
    };
    const contextmenu = event => { event.preventDefault(); event.stopPropagation(); };
    panel.addEventListener("keydown", keydown); panel.addEventListener("keyup", stopPropagation);
    panel.addEventListener("pointerdown", stopPropagation); panel.addEventListener("pointerup", stopPropagation); panel.addEventListener("pointermove", stopPropagation); panel.addEventListener("click", stopPropagation);
    layer.addEventListener("contextmenu", contextmenu);
    layer.addEventListener("wheel", wheel, { passive: false });
    document.addEventListener("pointerdown", outside, true); document.addEventListener("focusin", focus, true);
    document.addEventListener("scroll", scroll, true); window.addEventListener("resize", leave); window.addEventListener("blur", leave);
    window.visualViewport?.addEventListener("resize", leave); window.visualViewport?.addEventListener("scroll", leave);
    removeListeners = () => {
      document.removeEventListener("pointerdown", outside, true); document.removeEventListener("focusin", focus, true);
      document.removeEventListener("scroll", scroll, true); window.removeEventListener("resize", leave); window.removeEventListener("blur", leave);
      window.visualViewport?.removeEventListener("resize", leave); window.visualViewport?.removeEventListener("scroll", leave);
    };
    focusFirst(); return true;
  }

  return {
    open, close, isOpen: () => Boolean(panel),
    destroy() { close({ restoreFocus: false, force: true }); removeClickSuppression(); destroyed = true; },
  };
}
