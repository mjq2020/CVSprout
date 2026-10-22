// Exercise the actual conversion renderer/controller and its payload validation.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const source = readFileSync(new URL('../static/conversion-ui.js', import.meta.url), 'utf8');
const { mountConversionForm, validateConversionPayload } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const escapeHtml = value => String(value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const devices = [{ id: 'cpu', type: 'cpu', available: true }, { id: 'cuda:0', type: 'cuda', available: true }];
const basePayload = { format: 'onnx', image_size: 640, dynamic: false, half: false, device: 'cpu', max_retries: 2, batch: 1 };
const constraints = { min_image_size: 128, dynamic: false };
const validContext = { config: { available: true }, devices, constraints };
assert.deepEqual(validateConversionPayload(basePayload, validContext), []);
for (const image_size of [32, 64, 96, 127, 129, 4097]) {
  assert(validateConversionPayload({ ...basePayload, image_size }, validContext).some(issue => issue.field === 'convert-imgsz'));
}
for (const image_size of [128, 160, 640, 4096]) assert.deepEqual(validateConversionPayload({ ...basePayload, image_size }, validContext), []);
assert(validateConversionPayload({ ...basePayload, dynamic: true }, validContext).some(issue => issue.field === 'convert-dynamic'));
assert.deepEqual(validateConversionPayload({ ...basePayload, image_size: 32, dynamic: true }, { config: { available: true } }), []);

// This small DOM fixture retains the rendered attributes and form event bindings.
function fixture({ task = 'detect', backend = 'rtdetr', imageSize = 640 } = {}) {
  let container, active = null;
  const matches = (node, selector) => {
    if (selector === '*') return true;
    if (selector.startsWith('#')) return node.attrs.id === selector.slice(1);
    const tag = selector.match(/^[a-z]+/)?.[0];
    if (tag && node.tag !== tag) return false;
    for (const [, name, value] of selector.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)) {
      if (!(name in node.attrs) || (value !== undefined && node.attrs[name] !== value)) return false;
    }
    return !selector.includes(':checked') || node.checked;
  };
  class Element {
    constructor(tag, attrs = {}) {
      this.tag = tag; this.attrs = attrs; this.value = attrs.value || ''; this.children = []; this.listeners = {};
      this.disabled = 'disabled' in attrs; this.hidden = 'hidden' in attrs; this._checked = 'checked' in attrs;
      this.dataset = Object.fromEntries(Object.entries(attrs).filter(([name]) => name.startsWith('data-')).map(([name, value]) => [name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase()), value]));
    }
    get checked() { return this._checked; }
    set checked(value) { this._checked = value; if (value && this.attrs.type === 'radio') container.querySelectorAll(`input[name="${this.attrs.name}"]`).forEach(node => { if (node !== this) node._checked = false; }); }
    set innerHTML(html) {
      this.html = html; this.children = [];
      const stack = [this];
      for (const [, closing, tag, raw] of html.matchAll(/<(\/?)([a-z][\w-]*)([^>]*)>/g)) {
        if (closing) { for (let i = stack.length - 1; i > 0; i--) if (stack[i].tag === tag) { stack.length = i; break; } continue; }
        const attrs = {};
        for (const [, name, value] of raw.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) attrs[name] = value ?? '';
        const node = new Element(tag, attrs); stack.at(-1).children.push(node);
        if (!['input', 'br', 'hr', 'img'].includes(tag) && !raw.trimEnd().endsWith('/')) stack.push(node);
      }
      this.querySelectorAll('select').forEach(node => { const options = node.querySelectorAll('option'); node.value = (options.find(item => 'selected' in item.attrs) || options[0])?.value || ''; });
    }
    get innerHTML() { return this.html; }
    querySelectorAll(selector) { return this.children.flatMap(node => [node, ...node.querySelectorAll('*')]).filter(node => matches(node, selector)); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    setAttribute(name, value) { this.attrs[name] = value; }
    removeAttribute(name) { delete this.attrs[name]; }
    addEventListener(event, callback) { this.listeners[event] = callback; }
    focus() { active = this; }
  }
  container = new Element('div');
  let valid = false;
  const options = {
    model_backend: backend, constraints: backend === 'rtdetr' ? constraints : {},
    formats: { onnx: { available: true }, torchscript: { available: true }, engine: { available: true }, rknn: { available: backend !== 'rtdetr', reason: backend === 'rtdetr' ? 'RT-DETR 暂不支持 RKNN 转换。' : '' } },
    defaults: { image_size: imageSize }, calibration: { available_count: 12, default_count: 12 },
  };
  const form = mountConversionForm(container, { run: { id: 1, task }, options, resources: { devices }, escapeHtml, onValidityChange: value => { valid = value; } });
  const q = id => container.querySelector(`#${id}`);
  return { form, options, q, container, get valid() { return valid; }, get active() { return active; }, change(id, value) { q(id).value = String(value); container.listeners.input(); }, format(value) { container.querySelector(`input[name="convert-format"][value="${value}"]`).checked = true; container.listeners.change(); } };
}

// Backend model restrictions reach native controls, inline feedback, and the final payload path.
{
  const h = fixture({ imageSize: 64 });
  assert.equal(h.q('convert-imgsz').attrs.min, '128');
  assert.equal(h.q('convert-imgsz').value, '64', 'Do not silently replace an inherited setting');
  assert.equal(h.valid, false); assert(h.q('conversion-validation').innerHTML.includes('128–4096'));
  assert.equal(h.q('convert-dynamic').disabled, true); assert(h.q('convert-dynamic-help')); assert(h.container.innerHTML.includes('RT-DETR 目前仅支持固定输入尺寸'));
  assert.throws(() => h.form.payload(), /128/); assert.equal(h.active, h.q('convert-imgsz'));
  h.change('convert-imgsz', 128); assert.equal(h.valid, true); assert.equal(h.form.payload().image_size, 128); assert.equal(h.form.payload().dynamic, false);
  h.q('convert-dynamic').checked = true; h.container.listeners.change();
  assert.equal(h.valid, false); assert.throws(() => h.form.payload(), /固定输入/);
  assert(!h.q('conversion-format-notice').innerHTML.includes('NMS'), 'RT-DETR must never get the YOLO decoder warning');
  h.q('convert-dynamic').checked = false; h.container.listeners.change();
  for (const format of ['onnx', 'torchscript', 'engine']) { h.format(format); assert.equal(h.valid, true); assert.equal(h.form.payload().dynamic, false); }
  h.format('rknn'); assert.equal(h.valid, false); assert(h.q('conversion-format-notice').innerHTML.includes('RT-DETR 暂不支持 RKNN')); assert.throws(() => h.form.payload(), /RKNN/);
}

// Existing YOLO dynamic warnings stay task-specific; format switches retain values without leaking irrelevant settings.
for (const task of ['detect', 'segment', 'pose', 'classify']) {
  const h = fixture({ task, backend: 'yolo' });
  assert.equal(h.q('convert-imgsz').attrs.min, '32'); assert.equal(h.q('convert-dynamic').disabled, false);
  h.q('convert-dynamic').checked = true; h.container.listeners.change();
  assert.equal(h.form.payload().dynamic, true); assert.equal(h.q('conversion-format-notice').innerHTML.includes('NMS'), task !== 'classify');
  h.format('torchscript'); assert.equal(h.form.payload().dynamic, false); assert.equal(h.form.payload().simplify, false); assert(!h.q('conversion-format-notice').innerHTML.includes('NMS'));
  h.format('engine'); h.change('convert-workspace', 0.1); assert.equal(h.valid, false); h.change('convert-workspace', 4); assert.equal(h.form.payload().workspace, 4);
  h.format('onnx'); assert(!('workspace' in h.form.payload())); assert.equal(h.form.payload().dynamic, true);
}
{
  const h = fixture({ backend: 'yolo' }); h.format('rknn');
  assert.equal(h.valid, true); const payload = h.form.payload(); assert.equal(payload.batch, 1); assert.equal(payload.device, 'cpu'); assert.equal(payload.half, false); assert.equal(payload.dynamic, false);
  h.change('convert-rknn-quantized', 'true'); h.change('convert-rknn-count', 13); assert.equal(h.valid, false); assert.throws(() => h.form.payload(), /只有 12/);
  h.change('convert-rknn-count', 12); assert.equal(h.valid, true);
}
console.log('PASS conversion form: model-aware min size and dynamic limits, RT-DETR RKNN reason and no YOLO warning, final payload validation, YOLO task warnings, format switching, GPU workspace and calibration limits');
