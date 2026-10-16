const SVG_NS = "http://www.w3.org/2000/svg";
const mounted = new WeakMap();
let nextGuideId = 0;

// Work in original image coordinates: zoom and scrolling only change the SVG matrix.
export function canvasGuideGeometry({ clientX, clientY, matrix, width, height, drawing = null }) {
  if (!matrix || ![clientX, clientY, width, height, matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f].every(Number.isFinite) || width <= 0 || height <= 0) return null;
  const determinant = matrix.a * matrix.d - matrix.b * matrix.c;
  if (!Number.isFinite(determinant) || Math.abs(determinant) < 1e-12) return null;
  const inverse = { a: matrix.d / determinant, b: -matrix.b / determinant, c: -matrix.c / determinant, d: matrix.a / determinant };
  const x = inverse.a * (clientX - matrix.e) + inverse.c * (clientY - matrix.f);
  const y = inverse.b * (clientX - matrix.e) + inverse.d * (clientY - matrix.f);
  const clamp = (value, maximum) => Math.max(0, Math.min(maximum, value));
  const measuring = drawing && !drawing.complete && [drawing.start?.x, drawing.start?.y, drawing.current?.x, drawing.current?.y].every(Number.isFinite);
  return {
    x, y, inverse, inside: x >= 0 && x <= width && y >= 0 && y <= height,
    size: measuring ? { width: Math.abs(clamp(drawing.current.x, width) - clamp(drawing.start.x, width)), height: Math.abs(clamp(drawing.current.y, height) - clamp(drawing.start.y, height)) } : null,
  };
}

export function canvasGuidesAllowed(state, { editable = false, blocked = false, pointerType = "mouse", buttons = 0 } = {}) {
  return Boolean(state.currentSection === "annotate" && state.asset && editable && !blocked
    && ["mouse", "pen"].includes(pointerType)
    && (state.tool === "rectangle" || state.tool === "keypoints" && !state.poseDraft)
    && !state.pan && !state.spacePressed && !state.readOnly && !state.savingAnnotation && !state.workflowBusy
    && !state.shapeEdit && !state.pendingAnnotation && !state.pendingTaskEdit && !state.pendingObjectEdit && !state.advancedAnnotationDirty
    && state.keypointPlacement == null && !state.polygon?.length && !state.drawing?.complete
    && (buttons === 0 || buttons === 1 && state.drawing));
}

/** Call after binding the current canvas; refresh after viewport / tool / save-state changes. */
export function mountCanvasGuides({ state, canEditCurrentAsset, isContextMenuOpen = () => false }) {
  const svg = document.querySelector("#annotation-canvas"), scroll = document.querySelector("#canvas-scroll");
  if (!svg || !scroll) return { refresh() {}, destroy() {} };
  mounted.get(svg)?.destroy();
  const doc = svg.ownerDocument, win = doc.defaultView;
  const assetId = state.asset?.id;
  let frame = null, pointer = null, destroyed = false;
  const cleanups = [];
  const make = (tag, attributes = {}, parent = null) => {
    const node = doc.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    parent?.append(node); return node;
  };
  const layer = make("g", { class: "canvas-guides", "aria-hidden": "true", "pointer-events": "none" }, svg);
  layer.style.display = "none";
  const defs = make("defs", {}, layer), clipId = `canvas-guide-clip-${++nextGuideId}`;
  const clip = make("clipPath", { id: clipId, clipPathUnits: "userSpaceOnUse" }, defs);
  const clipRect = make("rect", { x: 0, y: 0 }, clip);
  const content = make("g", { "clip-path": `url(#${clipId})` }, layer);
  const lines = ["dark", "light"].map(tone => make("path", { class: `canvas-guide-line canvas-guide-line-${tone}`, "vector-effect": "non-scaling-stroke", fill: "none" }, content));
  const size = make("g", { class: "canvas-guide-size" }, content);
  const sizeRect = make("rect", { height: 26, rx: 5 }, size);
  const sizeText = make("text", { x: 8, y: 17 }, size);
  const blockers = () => Boolean(doc.hidden || isContextMenuOpen()
    || doc.querySelector('#modal-root > *, dialog[open], [role="dialog"][aria-modal="true"], .annotation-more-actions[open], .account-menu[open]'));
  let previousBlocked = blockers();
  const hide = () => { layer.style.display = "none"; };
  function paint() {
    frame = null;
    if (destroyed) return;
    if (!svg.isConnected || !scroll.isConnected) { destroy(); return; }
    previousBlocked = blockers();
    if (!pointer || state.asset?.id !== assetId || !canvasGuidesAllowed(state, { editable: canEditCurrentAsset(), blocked: previousBlocked, ...pointer })) { hide(); return; }
    const bounds = scroll.getBoundingClientRect();
    const viewport = { left: bounds.left + scroll.clientLeft, top: bounds.top + scroll.clientTop };
    viewport.right = viewport.left + scroll.clientWidth; viewport.bottom = viewport.top + scroll.clientHeight;
    if (pointer.clientX < viewport.left || pointer.clientX > viewport.right || pointer.clientY < viewport.top || pointer.clientY > viewport.bottom) { hide(); return; }
    const matrix = svg.getScreenCTM();
    const geometry = canvasGuideGeometry({ ...pointer, matrix, width: state.asset.width, height: state.asset.height, drawing: state.drawing });
    if (!geometry?.inside) { hide(); return; }
    const { x, y, inverse } = geometry;
    clipRect.setAttribute("width", state.asset.width); clipRect.setAttribute("height", state.asset.height);
    const path = `M 0 ${y} H ${state.asset.width} M ${x} 0 V ${state.asset.height}`;
    lines.forEach(line => line.setAttribute("d", path));
    layer.dataset.imageX = String(x); layer.dataset.imageY = String(y);
    layer.style.display = "";
    size.style.display = geometry.size ? "" : "none";
    if (!geometry.size) return;
    const pixels = value => Number(value.toFixed(1)).toLocaleString("zh-CN", { maximumFractionDigits: 1 });
    sizeText.textContent = `${pixels(geometry.size.width)} × ${pixels(geometry.size.height)} px`;
    size.dataset.width = String(geometry.size.width); size.dataset.height = String(geometry.size.height);
    const labelWidth = Math.ceil(sizeText.getComputedTextLength()) + 16;
    sizeRect.setAttribute("width", labelWidth);
    // Keep the fixed-screen-size readout visible near the pointer at image / viewport edges.
    const image = svg.getBoundingClientRect();
    const left = Math.max(viewport.left, image.left) + 4, top = Math.max(viewport.top, image.top) + 4;
    const right = Math.min(viewport.right, image.right) - 4, bottom = Math.min(viewport.bottom, image.bottom) - 4;
    if (right - left < labelWidth || bottom - top < 26) { size.style.display = "none"; return; }
    const screenX = Math.max(left, Math.min(pointer.clientX + 14, right - labelWidth));
    const screenY = Math.max(top, Math.min(pointer.clientY + 16, bottom - 26));
    const label = canvasGuideGeometry({ clientX: screenX, clientY: screenY, matrix, width: state.asset.width, height: state.asset.height });
    size.setAttribute("transform", `matrix(${inverse.a} ${inverse.b} ${inverse.c} ${inverse.d} ${label.x} ${label.y})`);
  }
  function refresh() { if (!destroyed && frame === null) frame = win.requestAnimationFrame(paint); }
  const listen = (target, name, handler, options) => {
    target.addEventListener(name, handler, options);
    cleanups.push(() => target.removeEventListener(name, handler, options));
  };
  const track = event => {
    if (event.pointerType === "touch" || event.isPrimary === false) { pointer = null; hide(); return; }
    pointer = { clientX: event.clientX, clientY: event.clientY, pointerType: event.pointerType || "mouse", buttons: event.buttons || 0 };
    refresh();
  };
  const leave = () => { pointer = null; hide(); };
  for (const event of ["pointerenter", "pointermove", "pointerdown", "pointerup"]) listen(scroll, event, track, { capture: true, passive: true });
  for (const event of ["pointerleave", "pointercancel"]) listen(scroll, event, leave, { passive: true });
  listen(scroll, "scroll", refresh, { passive: true });
  listen(scroll, "contextmenu", leave, { capture: true });
  listen(win, "blur", leave); listen(win, "resize", refresh);
  listen(win, "keydown", refresh); listen(win, "keyup", refresh);
  listen(doc, "visibilitychange", leave); listen(doc, "toggle", refresh, { capture: true });
  // No animation loop or detached global listeners: disposal also handles full workspace rerenders.
  const observer = new win.MutationObserver(() => {
    if (!svg.isConnected || !scroll.isConnected) { destroy(); return; }
    const blocked = blockers();
    if (blocked !== previousBlocked) { previousBlocked = blocked; if (blocked) hide(); refresh(); }
  });
  observer.observe(doc.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["open", "hidden", "inert", "aria-hidden"] });
  function destroy() {
    if (destroyed) return;
    destroyed = true;
    if (frame !== null) win.cancelAnimationFrame(frame);
    observer.disconnect(); cleanups.forEach(cleanup => cleanup()); layer.remove();
    if (mounted.get(svg)?.destroy === destroy) mounted.delete(svg);
  }
  const controller = { refresh, destroy };
  mounted.set(svg, controller);
  return controller;
}
