#!/usr/bin/env node
// Run: node scripts/check_dataset_split.mjs
// Offline regression of the real control handlers; no server or data writes.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../static/dataset-ui.js", import.meta.url), "utf8");
const { adjustSplitPercentages, adjustSplitBoundaryPercentages, estimatedSplitCounts, datasetSplitControlsMarkup, bindDatasetSplitControls } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const splits = ["train", "valid", "test"];
function valid(value) {
  assert.equal(value.train + value.valid + value.test, 100);
  for (const name of splits) assert.ok(Number.isInteger(value[name]) && value[name] >= (name === "train" ? 1 : 0) && value[name] <= 100);
}
for (let train = 1; train <= 100; train++) {
  for (let test = 0; test <= 100 - train; test++) {
    const current = Object.freeze({ train, valid: 100 - train - test, test });
    for (const requested of [-10, 0, 1, 49.6, 99, 100, 110]) {
      for (const name of splits) valid(adjustSplitPercentages(current, name, requested));
      const first = adjustSplitBoundaryPercentages(current, "train", requested);
      const second = adjustSplitBoundaryPercentages(current, "test", requested);
      valid(first); valid(second);
      assert.equal(first.test, test, "Moving the first boundary must preserve the test group");
      assert.equal(second.train, train, "Moving the second boundary must preserve the training group");
    }
    for (const total of [0, 1, 2, 7, 1001]) {
      const counts = estimatedSplitCounts(total, current);
      assert.equal(counts.train + counts.valid + counts.test, total);
      assert.ok(counts.train >= (total > 0 ? 1 : 0));
      assert.ok(splits.every(name => Number.isInteger(counts[name]) && counts[name] >= 0));
      if (!current.valid) assert.equal(counts.valid, 0);
      if (!current.test) assert.equal(counts.test, 0);
    }
  }
}
const initial = { train: 70, valid: 20, test: 10 };
for (const invalid of [NaN, Infinity, -Infinity]) {
  assert.deepEqual(adjustSplitPercentages(initial, "train", invalid), initial);
  assert.deepEqual(adjustSplitBoundaryPercentages(initial, "test", invalid), initial);
}
assert.deepEqual(estimatedSplitCounts(3, initial), { train: 2, valid: 1, test: 0 });
assert.deepEqual(estimatedSplitCounts(1, { train: 1, valid: 0, test: 99 }), { train: 1, valid: 0, test: 0 });

// Minimal event/element fixture: exercise the production binding, not a second
// implementation of its interactions. Geometry and visual hit targets are also
// checked separately in Chromium at desktop and mobile sizes.
class Element extends EventTarget {
  constructor(parent = null) {
    super(); this.parent = parent; this.attrs = {}; this.style = {}; this.dataset = {}; this.textContent = ""; this.captures = new Set();
    const classes = new Set();
    this.classList = { add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name), toggle: (name, on) => on ? classes.add(name) : classes.delete(name) };
  }
  set value(value) { this._value = String(value); }
  get value() { return this._value || ""; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return this.attrs[name]; }
  focus() { root.focused = this; }
  setPointerCapture(id) { this.captures.add(id); }
  hasPointerCapture(id) { return this.captures.has(id); }
  releasePointerCapture(id) { this.captures.delete(id); }
  getBoundingClientRect() { return { left: 20, width: 400 }; }
  dispatchEvent(event) {
    const result = super.dispatchEvent(event);
    if (event.bubbles && !event.cancelBubble) this.parent?.dispatchEvent(event);
    return result;
  }
}
const root = new Element();
const bar = new Element(root), track = new Element(bar);
const elements = new Map([[".dataset-split-bar", bar], [".dataset-split-track", track]]);
for (const name of splits) {
  for (const kind of ["number", "count", "percent", "segment"]) {
    const element = new Element(root); element.dataset.splitNumber = name;
    elements.set(`[data-split-${kind}="${name}"]`, element);
  }
}
for (const name of ["train", "test"]) elements.set(`[data-split-handle="${name}"]`, new Element(bar));
elements.set("#dataset-split-note", new Element(root));
elements.set("[data-split-default]", new Element(root));
root.querySelector = selector => elements.get(selector);
root.querySelectorAll = selector => selector === "[data-split-number]" ? splits.map(name => elements.get(`[data-split-number="${name}"]`)) : [];
let total = 100, inputs = 0, changes = 0;
root.addEventListener("input", () => inputs++); root.addEventListener("change", () => changes++);
const controls = bindDatasetSplitControls(root, () => total);
const first = elements.get('[data-split-handle="train"]'), second = elements.get('[data-split-handle="test"]');
function event(type, properties = {}) {
  const result = new Event(type, { bubbles: true, cancelable: true });
  for (const [key, value] of Object.entries(properties)) Object.defineProperty(result, key, { value, configurable: true });
  return result;
}
function key(handle, keyName) { const value = event("keydown", { key: keyName }); handle.dispatchEvent(value); return value; }
function numeric(name, value) { const element = elements.get(`[data-split-number="${name}"]`); element.value = value; element.dispatchEvent(event("input")); }
function reset() { const target = elements.get("[data-split-default]"); target.onclick({ currentTarget: target }); }
assert.deepEqual(controls.read(), initial);
const copy = controls.read(); copy.train = 1; assert.deepEqual(controls.read(), initial, "Readers must not mutate internal state");
assert.equal(key(first, "ArrowRight").defaultPrevented, true);
assert.deepEqual(controls.read(), { train: 71, valid: 19, test: 10 });
key(second, "PageDown"); assert.deepEqual(controls.read(), { train: 71, valid: 9, test: 20 });
key(first, "End"); assert.deepEqual(controls.read(), { train: 80, valid: 0, test: 20 });
assert.equal(first.getAttribute("aria-valuemax"), "80"); assert.equal(second.getAttribute("aria-valuemin"), "80");
assert.equal(first.getAttribute("aria-valuenow"), "80");
assert.match(first.getAttribute("aria-valuetext"), /训练 80%，验证 0%，测试 20%/);
key(second, "ArrowRight"); assert.deepEqual(controls.read(), { train: 80, valid: 1, test: 19 }, "The second handle remains usable after boundaries coincide");
key(first, "Home"); assert.equal(controls.read().train, 1);
key(second, "End"); assert.equal(controls.read().test, 0);
key(second, "Home"); assert.deepEqual(controls.read(), { train: 1, valid: 0, test: 99 });
key(first, "ArrowLeft"); assert.equal(controls.read().train, 1);
assert.equal(key(first, "Tab").defaultPrevented, false, "Tab must preserve the focus order");
assert.equal(key(first, "Escape").defaultPrevented, false, "Escape remains available to the enclosing dialog");
assert.ok(inputs > 0 && changes > 0, "Keyboard changes must reach the version preview listeners");

numeric("train", 100); assert.deepEqual(controls.read(), { train: 100, valid: 0, test: 0 });
key(first, "ArrowLeft"); assert.deepEqual(controls.read(), { train: 99, valid: 1, test: 0 });
numeric("train", ""); assert.equal(controls.read().train, 99);
elements.get('[data-split-number="train"]').dispatchEvent(event("blur"));
assert.equal(elements.get('[data-split-number="train"]').value, "99");
numeric("test", 999); assert.deepEqual(controls.read(), { train: 1, valid: 0, test: 99 });
reset(); assert.deepEqual(controls.read(), initial);

for (const pointerType of ["mouse", "touch"]) {
  reset();
  // Grab slightly off-center; holding still must not change the split.
  const props = { pointerId: 7, pointerType, button: 0, isPrimary: true, clientX: 305 };
  first.dispatchEvent(event("pointerdown", props));
  assert.equal(root.focused, first); assert.equal(first.hasPointerCapture(7), true);
  bar.dispatchEvent(event("pointermove", { ...props, clientX: 305 })); assert.deepEqual(controls.read(), initial);
  bar.dispatchEvent(event("pointermove", { ...props, clientX: 145 })); assert.deepEqual(controls.read(), { train: 30, valid: 60, test: 10 });
  bar.dispatchEvent(event("pointermove", { ...props, pointerId: 8, clientX: 420 })); assert.equal(controls.read().train, 30, "A second pointer must not take over");
  bar.dispatchEvent(event("pointermove", { ...props, clientX: -100 })); assert.equal(controls.read().train, 1);
  bar.dispatchEvent(event("pointermove", { ...props, clientX: 900 })); assert.deepEqual(controls.read(), { train: 90, valid: 0, test: 10 });
  bar.dispatchEvent(event("pointercancel", props)); assert.equal(first.hasPointerCapture(7), false); assert.equal(bar.classList.contains("dragging"), false);
  bar.dispatchEvent(event("pointermove", { ...props, clientX: 25 })); assert.equal(controls.read().train, 90, "Cancelled drags must stop");
}
reset();
track.dispatchEvent(event("pointerdown", { pointerId: 9, pointerType: "mouse", button: 0, clientX: 420 }));
assert.deepEqual(controls.read(), { train: 70, valid: 30, test: 0 }, "Clicking the track moves the nearest boundary");
bar.dispatchEvent(event("pointerup", { pointerId: 9 }));
total = 1; controls.update(); assert.match(elements.get("#dataset-split-note").textContent, /没有验证图片/);
total = 0; controls.update(); assert.match(elements.get("#dataset-split-note").textContent, /选择要保存的图片/);
const markup = datasetSplitControlsMarkup();
assert.equal((markup.match(/role="slider"/g) || []).length, 2);
assert.equal((markup.match(/type="number"/g) || []).length, 3);
assert.doesNotMatch(markup, /type="range"/);
assert.match(markup, /aria-orientation="horizontal"/);
assert.match(markup, /aria-describedby="dataset-split-keyboard"/);
console.log("PASS: all legal percentage combinations; allocation totals; two independent boundaries; zero-width groups; keyboard/ARIA; numeric inputs/reset; mouse/touch capture/cancel; bubbling preview updates.");
