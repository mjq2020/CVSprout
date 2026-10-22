// Navigation feedback is explicit: polling and list updates never animate the page.
const pendingLoads = new WeakMap();
const entryTimers = new WeakMap();

export function enterWorkspaceContent(node, className = "workspace-page-enter") {
  if (!node || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  clearTimeout(entryTimers.get(node));
  node.classList.add(className);
  entryTimers.set(node, setTimeout(() => {
    node.classList.remove(className);
    entryTimers.delete(node);
  }, 180));
}

export function cancelWorkspaceLoad(app) {
  pendingLoads.get(app)?.cancel();
}

export function beginWorkspaceLoad(app, { message = "正在加载页面…", fallback } = {}) {
  cancelWorkspaceLoad(app);
  let main = app.querySelector(".app-shell > main");
  const initial = !main;
  if (initial) {
    app.innerHTML = fallback();
    main = app.querySelector(".app-shell > main");
  }
  const shell = main.parentElement;
  const wasBusy = main.getAttribute("aria-busy");
  const wasInert = main.inert;
  main.dataset.workspaceLoading = "true";
  main.inert = true;
  main.setAttribute("aria-busy", "true");
  let indicator = null, timer = null;
  const clear = () => {
    clearTimeout(timer);
    indicator?.remove();
    main.classList.remove("workspace-content-pending");
    delete main.dataset.workspaceLoading;
    main.inert = wasInert || !!app.querySelector('.workspace-navigation.is-open[aria-modal="true"]');
    if (wasBusy == null) main.removeAttribute("aria-busy");
    else main.setAttribute("aria-busy", wasBusy);
  };
  const token = { cancel() { clear(); pendingLoads.delete(app); } };
  pendingLoads.set(app, token);
  const show = () => {
    if (pendingLoads.get(app) !== token || !main.isConnected) return;
    indicator = document.createElement("div");
    indicator.className = "workspace-feedback-status";
    indicator.setAttribute("role", "status");
    indicator.setAttribute("aria-live", "polite");
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    spinner.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.textContent = message;
    indicator.append(spinner, label);
    // Keep the live region outside the inert main and the sidebar.
    shell.append(indicator);
    main.classList.add("workspace-content-pending");
  };
  if (initial) show();
  else timer = setTimeout(show, 180);
  return () => {
    if (pendingLoads.get(app) !== token) return;
    token.cancel();
    const next = app.querySelector(".app-shell > main");
    if (next !== main && next?.matches(".dashboard, .project-page")) enterWorkspaceContent(next);
  };
}
