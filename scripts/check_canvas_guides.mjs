#!/usr/bin/env node
// Offline geometry / visibility regression; no server, dependencies, or data writes.
import assert from "node:assert/strict";
import { canvasGuideGeometry, canvasGuidesAllowed } from "../static/canvas-guides.js";

const state = { currentSection: "annotate", asset: { id: 1, width: 640, height: 420 }, tool: "rectangle", polygon: [] };
const options = { editable: true };
assert.equal(canvasGuidesAllowed(state, options), true);
for (const field of ["pan", "spacePressed", "readOnly", "savingAnnotation", "workflowBusy", "shapeEdit", "pendingAnnotation", "pendingTaskEdit", "pendingObjectEdit", "advancedAnnotationDirty"]) {
  assert.equal(canvasGuidesAllowed({ ...state, [field]: true }, options), false, field);
}
for (const tool of ["select", "pan", "polygon", "positive", "negative"]) assert.equal(canvasGuidesAllowed({ ...state, tool }, options), false, tool);
assert.equal(canvasGuidesAllowed({ ...state, tool: "keypoints" }, options), true);
assert.equal(canvasGuidesAllowed({ ...state, tool: "keypoints", poseDraft: {} }, options), false);
assert.equal(canvasGuidesAllowed({ ...state, keypointPlacement: 0 }, options), false);
assert.equal(canvasGuidesAllowed({ ...state, currentSection: "data" }, options), false);
assert.equal(canvasGuidesAllowed({ ...state, drawing: { complete: true } }, options), false);
for (const extra of [{ editable: false }, { blocked: true }, { pointerType: "touch" }, { buttons: 2 }, { buttons: 4 }, { buttons: 1 }]) assert.equal(canvasGuidesAllowed(state, { ...options, ...extra }), false);
assert.equal(canvasGuidesAllowed({ ...state, drawing: {} }, { ...options, buttons: 1 }), true);
const drawing = { start: { x: 210.25, y: 170 }, current: { x: 70, y: 70 } };
for (const zoom of [0.02, 0.5, 1, 2, 8]) {
  const matrix = { a: zoom, b: 0, c: 0, d: zoom, e: -150, f: 40 };
  const geometry = canvasGuideGeometry({ clientX: 120 * zoom - 150, clientY: 180 * zoom + 40, matrix, width: 640, height: 420, drawing });
  assert.ok(Math.abs(geometry.x - 120) < 1e-8); assert.ok(Math.abs(geometry.y - 180) < 1e-8);
  assert.equal(geometry.inside, true); assert.deepEqual(geometry.size, { width: 140.25, height: 100 });
}
const matrix = { a: 0, b: 2, c: -2, d: 0, e: 30, f: -50 };
const rotated = canvasGuideGeometry({ clientX: -330, clientY: 190, matrix, width: 640, height: 420 });
assert.equal(rotated.x, 120); assert.equal(rotated.y, 180);
const identity = { a: 1, b: 0, c: 0, d: 1, e: 0, f: 0 };
for (const [x, y] of [[-1, 10], [641, 10], [10, -1], [10, 421]]) assert.equal(canvasGuideGeometry({ clientX: x, clientY: y, matrix: identity, width: 640, height: 420 }).inside, false);
assert.equal(canvasGuideGeometry({ clientX: 0, clientY: 0, matrix: identity, width: 640, height: 420 }).inside, true);
assert.equal(canvasGuideGeometry({ clientX: 640, clientY: 420, matrix: identity, width: 640, height: 420 }).inside, true);
assert.equal(canvasGuideGeometry({ clientX: 1, clientY: 1, matrix: { ...identity, a: 0 }, width: 640, height: 420 }), null);
assert.equal(canvasGuideGeometry({ clientX: NaN, clientY: 1, matrix: identity, width: 640, height: 420 }), null);
const clamped = canvasGuideGeometry({ clientX: 20, clientY: 20, matrix: identity, width: 640, height: 420, drawing: { start: { x: -30, y: -10 }, current: { x: 700, y: 450 } } });
assert.deepEqual(clamped.size, { width: 640, height: 420 });
console.log("PASS image-coordinate geometry at 2–800% zoom, scrolling/rotation, bounds, actual pixel sizes, tool/permission/draft/pointer visibility guards");
