import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../static/workspace-feedback.js", import.meta.url), "utf8").replaceAll("export function ", "function ");

function fixture({ initial = false, reduced = false } = {}) {
  const timers = new Map(); let sequence = 0, now = 0, fallbacks = 0;
  class Node {
    constructor(classes = []) {
      this.classes = new Set(classes); this.attrs = new Map(); this.dataset = {};
      this.children = []; this.inert = false; this.isConnected = true;
      this.classList = { add: name => this.classes.add(name), remove: name => this.classes.delete(name), contains: name => this.classes.has(name) };
    }
    setAttribute(key, value) { this.attrs.set(key, value); }
    getAttribute(key) { return this.attrs.get(key) ?? null; }
    removeAttribute(key) { this.attrs.delete(key); }
    append(...nodes) { nodes.forEach(node => { node.parentElement = this; this.children.push(node); }); }
    remove() { this.parentElement.children = this.parentElement.children.filter(node => node !== this); this.isConnected = false; }
    matches() { return this.classes.has("dashboard") || this.classes.has("project-page"); }
  }
  const main = new Node(["project-page"]), shell = new Node(); shell.append(main);
  const app = { main: initial ? null : main, drawer: false, replacements: 0,
    querySelector(selector) { return selector === ".app-shell > main" ? this.main : this.drawer ? new Node() : null; },
    set innerHTML(value) { this.replacements++; this.main = main; this.html = value; },
  };
  const document = { createElement: () => new Node(), activeElement: main };
  const context = vm.createContext({ document, window: { matchMedia: () => ({ matches: reduced }) },
    setTimeout: (callback, delay) => { const id = ++sequence; timers.set(id, { callback, due: now + delay }); return id; },
    clearTimeout: id => timers.delete(id),
  });
  vm.runInContext(source, context);
  return { app, main, shell, document, Node, timers,
    begin: message => context.beginWorkspaceLoad(app, { message, fallback: () => { fallbacks++; return "initial shell"; } }),
    cancel: () => context.cancelWorkspaceLoad(app),
    enter: context.enterWorkspaceContent,
    get fallbacks() { return fallbacks; },
    advance(ms) { now += ms; for (const [id, task] of timers) if (task.due <= now) { timers.delete(id); task.callback(); } },
    replace(classes = ["project-page"]) { main.isConnected = false; const next = new Node(classes); shell.append(next); app.main = next; return next; },
  };
}

// Existing content, focus and controls survive a quick navigation response.
{
  const f = fixture(), finish = f.begin("正在加载数据…");
  assert.equal(f.app.replacements, 0); assert.equal(f.fallbacks, 0);
  assert.equal(f.document.activeElement, f.main);
  assert.equal(f.main.inert, true); assert.equal(f.main.getAttribute("aria-busy"), "true");
  f.advance(179); assert.equal(f.shell.children.length, 1);
  const next = f.replace(); finish(); f.advance(1);
  assert.equal(f.main.inert, false); assert.equal(f.main.getAttribute("aria-busy"), null);
  assert.equal(next.classList.contains("workspace-page-enter"), true);
  assert.equal(f.shell.children.some(node => node.className === "workspace-feedback-status"), false);
  f.advance(180); assert.equal(next.classList.contains("workspace-page-enter"), false);
}
// A slow response announces a status outside the inert content. User strings are text.
{
  const f = fixture(), finish = f.begin("<项目> 正在加载…"); f.advance(180);
  const status = f.shell.children[1];
  assert.equal(status.getAttribute("role"), "status"); assert.equal(status.parentElement, f.shell);
  assert.equal(status.children[1].textContent, "<项目> 正在加载…");
  assert.equal(f.main.classList.contains("workspace-content-pending"), true);
  finish(); assert.equal(status.isConnected, false); assert.equal(f.main.inert, false);
  assert.equal(f.main.classList.contains("workspace-content-pending"), false);
  assert.equal(f.main.classList.contains("workspace-page-enter"), false, "Retained content does not replay an entrance");
}
// A superseded request must never unlock the next route or clear its indicator.
{
  const f = fixture(), oldFinish = f.begin(); f.advance(100);
  const currentFinish = f.begin(); oldFinish(); f.advance(80);
  assert.equal(f.main.inert, true); assert.equal(f.shell.children.length, 1);
  f.advance(100); assert.equal(f.shell.children.length, 2);
  oldFinish(); assert.equal(f.shell.children.length, 2);
  currentFinish(); assert.equal(f.shell.children.length, 1); assert.equal(f.main.inert, false);
}
// Route cancellation / sign-out cleans timers and restores previous accessibility state.
{
  const f = fixture(); f.main.setAttribute("aria-busy", "false");
  const finish = f.begin(); f.cancel(); f.advance(1000); finish();
  assert.equal(f.main.inert, false); assert.equal(f.main.getAttribute("aria-busy"), "false");
  assert.equal(f.main.dataset.workspaceLoading, undefined); assert.equal(f.shell.children.length, 1);
  assert.equal(f.timers.size, 0);
}
// Initial visits have a stable shell and immediate feedback, including reduced motion.
{
  const f = fixture({ initial: true, reduced: true }), finish = f.begin();
  assert.equal(f.fallbacks, 1); assert.equal(f.app.replacements, 1); assert.equal(f.shell.children.length, 2);
  const next = f.replace(); finish(); assert.equal(next.classList.contains("workspace-page-enter"), false);
  f.enter(next, "workspace-panel-enter"); assert.equal(next.classList.contains("workspace-panel-enter"), false);
}
// Annotation geometry is never animated; opening navigation retains its input lock.
{
  const f = fixture(), finish = f.begin(); const canvas = f.replace(["annotation-workspace"]); finish();
  assert.equal(canvas.classList.contains("workspace-page-enter"), false);
  f.app.main = f.main; f.main.isConnected = true;
  const done = f.begin(); f.app.drawer = true; done(); assert.equal(f.main.inert, true);
}
console.log("PASS workspace feedback: delayed/initial status, retained DOM, stale response isolation, cleanup, reduced motion, drawer lock and annotation geometry");
