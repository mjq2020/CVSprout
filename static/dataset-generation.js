import { datasetSplitControlsMarkup, bindDatasetSplitControls, estimatedSplitCounts } from "./dataset-ui.js?v=0.24.0";

const METHODS = [
  { type: "horizontal_flip", name: "水平翻转", group: "方向变化", description: "把图片左右镜像，模拟目标朝向不同的情况。", usage: "适合左右朝向都可能出现的物体。", risk: "文字、左右手或有方向含义的类别可能改变语义；关键点需配置左右对应关系。" },
  { type: "vertical_flip", name: "垂直翻转", group: "方向变化", description: "把图片上下镜像，增加倒置方向的样本。", usage: "适合俯视零件、显微图像等没有固定上下方向的场景。", risk: "道路、行人等通常有固定上下方向，请谨慎开启。" },
  { type: "rotate90", name: "直角旋转", group: "方向变化", description: "按选定角度旋转图片，标注随图片一起旋转。", usage: "适合相机或物体可能转动的场景。", risk: "不会产生任意角度；90° 和 270° 会交换图片宽高。", defaults: { degrees: [90, 180, 270] } },
  { type: "brightness", name: "亮度变化", group: "拍摄条件", description: "随机调亮或调暗图片，模拟不同光照。", usage: "适合白天、夜间或曝光不同的图片。", risk: "变化过大可能淹没细节；建议从小幅度开始。", defaults: { min_factor: 0.8, max_factor: 1.2 } },
  { type: "contrast", name: "对比度变化", group: "拍摄条件", description: "改变明暗差异，让模型接触更柔和或更鲜明的画面。", usage: "适合阴影、雾气或相机成像差异。", risk: "过强对比度可能丢失暗部和亮部的细节。", defaults: { min_factor: 0.8, max_factor: 1.2 } },
  { type: "saturation", name: "饱和度变化", group: "拍摄条件", description: "改变颜色的鲜艳程度，模拟相机色彩差异。", usage: "适合目标主要依靠形状而非精确颜色识别的场景。", risk: "颜色决定类别时，例如成熟度或信号灯，建议关闭。", defaults: { min_factor: 0.8, max_factor: 1.2 } },
  { type: "grayscale", name: "转为灰度", group: "拍摄条件", description: "去掉颜色信息，保留明暗和轮廓。", usage: "适合灰度相机或需要减少颜色依赖的任务。", risk: "无法保留颜色差异，不适合依靠颜色区分类别。" },
  { type: "gamma", name: "伽马校正", group: "拍摄条件", description: "调整中间亮度，模拟相机曝光与暗部差异。", usage: "适合相机亮度曲线不同、暗部细节有变化的场景。", risk: "系数大于 1 会变暗，小于 1 会变亮；过大变化会丢失细节。" },
  { type: "hue", name: "色相偏移", group: "拍摄条件", description: "轻微改变整体色调，模拟光源或白平衡差异。", usage: "适合目标以形状为主、实际拍摄有偏色情况的场景。", risk: "数值表示色相环一圈的比例，0.05 约为 18°；颜色决定类别时请勿使用。" },
  { type: "autocontrast", name: "自动对比度", group: "拍摄条件", description: "按图片明暗分布自动拉开对比度。", usage: "适合画面发灰或相机自动增强程度不同的图片。", risk: "截断比例越高，越容易丢失最亮、最暗的细节；请从较小数值开始。" },
  { type: "equalize", name: "直方图均衡化", group: "拍摄条件", description: "重新分配明暗层次，使局部差异更明显。", usage: "适合亮度分布集中的图片或需要检查对明暗变化的适应能力。", risk: "可能显著改变颜色和细节，不适合依赖真实颜色或纹理的任务。" },
  { type: "blur", name: "轻微模糊", group: "画面质量", description: "模拟轻微失焦，使训练图片带有一定模糊。", usage: "适合实际拍摄偶尔不清晰的场景。", risk: "小目标和细关键点容易消失，请先查看预览。", defaults: { radius: 1.5 } },
  { type: "noise", name: "图像噪声", group: "画面质量", description: "加入随机颗粒，模拟低照度或传感器噪声。", usage: "适合实际图片有颗粒感的拍摄条件。", risk: "强噪声会覆盖细节，不能替代真实的困难样本。", defaults: { std: 10 } },
  { type: "sharpness", name: "清晰度变化", group: "画面质量", description: "减弱或增强边缘，模拟不同相机的锐化效果。", usage: "适合图片清晰度与边缘增强程度不同的场景。", risk: "0 表示平滑，1 保持原样，大于 1 增强边缘；过强锐化会产生不自然的轮廓。" },
  { type: "jpeg_compression", name: "JPEG 压缩", group: "画面质量", description: "降低压缩质量，模拟传输或存储中的画质损失。", usage: "适合监控、网络上传或经常被压缩的图片。", risk: "质量越低失真越明显，小目标和文字可能变得难以辨认。" },
  { type: "downscale", name: "低分辨率模拟", group: "画面质量", description: "先缩小再还原到原尺寸，模拟低分辨率画面。", usage: "适合不同分辨率设备或远距离拍摄的图片。", risk: "倍率越小，丢失的细节越多；标注位置不变，但小目标可能难以辨认。" },
];
const STEPS = ["选择数据", "划分比例", "配置增强", "预览确认"];
const number = value => Number(value || 0).toLocaleString("zh-CN");
const methodName = type => METHODS.find(item => item.type === type)?.name || type;

const RANGE_PARAMETERS = {
  brightness: { keys:['min_factor','max_factor'], labels:['最小系数','最大系数'], min:.1,max:2,step:.1,values:[.8,1.2], hint:'1 表示不变，小于 1 减弱，大于 1 增强。' },
  contrast: { keys:['min_factor','max_factor'], labels:['最小系数','最大系数'], min:.1,max:2,step:.1,values:[.8,1.2], hint:'1 表示不变，小于 1 减弱，大于 1 增强。' },
  saturation: { keys:['min_factor','max_factor'], labels:['最小系数','最大系数'], min:.1,max:2,step:.1,values:[.8,1.2], hint:'1 表示不变，小于 1 减弱，大于 1 增强。' },
  gamma: { keys:['min_gamma','max_gamma'], labels:['最小伽马值','最大伽马值'], min:.2,max:3,step:.1,values:[.8,1.2], hint:'1 保持原样，大于 1 变暗，小于 1 变亮。' },
  hue: { keys:['min_shift','max_shift'], labels:['最小偏移','最大偏移'], min:-.5,max:.5,step:.01,values:[-.05,.05], hint:'以色相环一圈为 1；0 保持原样，正负数表示不同偏移方向。' },
  sharpness: { keys:['min_factor','max_factor'], labels:['最小系数','最大系数'], min:0,max:3,step:.1,values:[.5,1.5], hint:'0 平滑，1 保持原样，大于 1 增强边缘。' },
  jpeg_compression: { keys:['min_quality','max_quality'], labels:['最低质量','最高质量'], min:10,max:100,step:1,integer:true,values:[60,95], hint:'质量越高，越接近原图；请使用整数。' },
  downscale: { keys:['min_scale','max_scale'], labels:['最小缩放倍率','最大缩放倍率'], min:.1,max:1,step:.1,values:[.5,.9], hint:'先缩小再恢复原尺寸；1 保持原样，0.5 表示先缩至宽高的一半。' },
};
const SINGLE_PARAMETERS = {
  blur: { key:'radius',label:'模糊半径',min:.1,max:5,step:.1,value:1.5 },
  noise: { key:'std',label:'噪声强度',min:1,max:30,step:1,value:10 },
  autocontrast: { key:'cutoff',label:'两端截断比例（%）',min:0,max:10,step:.5,value:1 },
};

export function augmentationTargetCount(mode, rawValue, trainCount, totalOriginal = trainCount) {
  const train = Number(trainCount), value = Number(rawValue);
  if (!Number.isInteger(train) || train <= 0) throw new Error('当前没有训练图片，请先调整数据范围或划分比例。');
  if (String(rawValue ?? '').trim() === '' || !Number.isFinite(value)) throw new Error('请输入有效的增强数量或倍数。');
  if (mode !== 'multiplier' && mode !== 'count') throw new Error('请选择按倍数或按新增张数设置增强量。');
  if (mode === 'multiplier' && (value <= 1 || value > 21)) throw new Error('扩增倍数需大于 1 且不超过 21，包含训练原图。');
  if (mode === 'count' && (!Number.isInteger(value) || value < 1)) throw new Error('新增张数需为大于 0 的整数。');
  const target = mode === 'multiplier' ? Math.round(train * (value - 1)) : value;
  if (target < 1) throw new Error('按当前训练图片数与倍数，四舍五入后新增 0 张。请提高倍数，或按新增张数至少设置 1 张。');
  const limit = Math.max(0, Math.min(5000, train * 20, 20000 - Number(totalOriginal)));
  if (target > limit) throw new Error(`本次最多新增 ${number(limit)} 张，请减少倍数或新增张数。`);
  return target;
}

// Keep only the newest requested configuration and never overlap CPU-heavy
// previews. Changing a setting invalidates the response without restarting the
// server work; closing the dialog or exceeding the timeout ends the client wait.
export function createGenerationPreviewQueue({ request, isCurrent, onPending, onResult, onError, delay = 400, timeout = 45000 }) {
  let serial = 0, timer = null, pending = null, active = null, ready = false, disposed = false;
  const run = () => {
    if (disposed || active || !ready || !pending || !isCurrent()) return;
    const job = pending; pending = null; ready = false;
    const controller = new AbortController(); active = { job, controller };
    let deadline;
    const timedOut = new Promise((resolve, reject) => {
      deadline = setTimeout(() => {
        reject(new Error('预览请求超时，请稍后重试或减少增强副本数。'));
        controller.abort();
      }, timeout);
    });
    Promise.race([Promise.resolve().then(() => request(job.value, controller.signal)), timedOut]).then(result => {
      if (!disposed && isCurrent() && job.serial === serial) onResult(result, job.value);
    }).catch(error => {
      if (!disposed && isCurrent() && job.serial === serial) onError(error, job.value);
    }).finally(() => { clearTimeout(deadline); active = null; run(); });
  };
  const cancel = () => { serial += 1; clearTimeout(timer); timer = null; pending = null; ready = false; };
  return {
    schedule(value, { immediate = false } = {}) {
      if (disposed || !isCurrent()) return;
      clearTimeout(timer); ready = false;
      pending = { value, serial: ++serial }; onPending(value);
      if (immediate) { ready = true; run(); }
      else timer = setTimeout(() => { timer = null; ready = true; run(); }, delay);
    },
    cancel,
    dispose() { if (disposed) return; cancel(); disposed = true; active?.controller.abort(); },
  };
}

export function versionAugmentationMarkup(version, escapeHtml, compact = false) {
  const configuration = version.augmentation || version.stats?.augmentation?.config || version.stats?.augmentation;
  if (!configuration?.operations?.length) return "";
  const names = configuration.operations.map(operation => methodName(operation.type));
  const counts = version.stats?.augmentation?.counts || version.stats?.augmentation || {};
  const generated = counts.generated ?? counts.generated_count;
  const copies = configuration.copies_per_image;
  const amount = configuration.target_count != null ? `计划最多新增 ${number(configuration.target_count)} 张` : copies ? `每张最多新增 ${number(copies)} 份` : '';
  if (compact) return `<small class="version-augmentation-compact">训练集增强${amount ? ` · ${amount}` : ""} · ${escapeHtml(names.join("、"))}</small>`;
  return `<details class="version-augmentation"><summary>训练集增强 · ${escapeHtml(names.join("、"))}${generated != null ? ` · 实际新增 ${number(generated)} 张` : ""}</summary><p>保留训练原图${amount ? `，${amount}` : ""}；验证集和测试集不增强。未发生变化或重复的副本会跳过。</p><dl>${configuration.operations.map(operation => `<div><dt>${escapeHtml(methodName(operation.type))}</dt><dd>${escapeHtml(operationSummary(operation))}</dd></div>`).join("")}</dl>${configuration.seed != null ? `<p class="hint">增强随机种子：${escapeHtml(String(configuration.seed))}</p>` : ""}</details>`;
}

function operationSummary(operation) {
  const parts = [];
  if (operation.degrees) parts.push(`角度 ${operation.degrees.map(value => `${value}°`).join(" / ")}`);
  if (operation.min_factor != null) parts.push(`系数 ${operation.min_factor}–${operation.max_factor}`);
  if (operation.min_gamma != null) parts.push(`伽马值 ${operation.min_gamma}–${operation.max_gamma}`);
  if (operation.min_shift != null) parts.push(`色相偏移 ${operation.min_shift}–${operation.max_shift}`);
  if (operation.min_quality != null) parts.push(`质量 ${operation.min_quality}–${operation.max_quality}`);
  if (operation.min_scale != null) parts.push(`缩放倍率 ${operation.min_scale}–${operation.max_scale}`);
  if (operation.cutoff != null) parts.push(`截断比例 ${operation.cutoff}%`);
  if (operation.radius != null) parts.push(`模糊半径 ${operation.radius}`);
  if (operation.std != null) parts.push(`噪声强度 ${operation.std}`);
  parts.push(`应用概率 ${Math.round((operation.probability ?? 1) * 100)}%`);
  return parts.join("；");
}

function annotationPreview(image, escapeHtml, schema) {
  if (!image || !/^data:image\/(png|jpeg|webp);base64,/.test(image.image_data_url || "")) return `<p class="generation-preview-empty">预览图片不可用，请重新预览。</p>`;
  const width = Number(image.width), height = Number(image.height);
  if (!(width > 0 && height > 0)) return `<p class="generation-preview-empty">预览图片尺寸无效，请重新预览。</p>`;
  const annotations = image.annotations || [];
  const shapes = annotations.map(annotation => {
    const color = /^#[0-9a-f]{3,8}$/i.test(annotation.color || "") ? annotation.color : "#365cf5";
    const points = Array.isArray(annotation.points) ? annotation.points.map(Number) : [];
    if (!points.every(Number.isFinite)) return "";
    let shape = "";
    if (["rectangle", "keypoints"].includes(annotation.shape_type) && points.length === 4) shape += `<rect x="${Math.min(points[0], points[2])}" y="${Math.min(points[1], points[3])}" width="${Math.abs(points[2] - points[0])}" height="${Math.abs(points[3] - points[1])}"/>`;
    if (annotation.shape_type === "polygon" && points.length >= 6) shape += `<polygon points="${points.join(" ")}" fill="${color}" fill-opacity=".12"/>`;
    if (annotation.shape_type === "keypoints") {
      const keypoints = annotation.keypoints || [];
      const visible = point => point && Number.isFinite(Number(point.x)) && Number.isFinite(Number(point.y)) && Number(point.visibility) > 0;
      for (const [start, end] of schema?.skeleton || []) if (visible(keypoints[start]) && visible(keypoints[end])) shape += `<line x1="${Number(keypoints[start].x)}" y1="${Number(keypoints[start].y)}" x2="${Number(keypoints[end].x)}" y2="${Number(keypoints[end].y)}"/>`;
      for (const point of keypoints) if (visible(point)) shape += `<circle cx="${Number(point.x)}" cy="${Number(point.y)}" r="${Math.max(width, height) / 100}" fill="${color}"/>`;
    }
    return `<g fill="none" stroke="${color}" stroke-width="${Math.max(width, height) / 220}">${shape}</g>`;
  }).join("");
  const labels = [...new Set(annotations.map(annotation => annotation.label_name).filter(Boolean))];
  return `<div class="generation-preview-image"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="图片与 ${annotations.length} 个标注"><image href="${escapeHtml(image.image_data_url)}" width="${width}" height="${height}"/><g class="generation-preview-annotations">${shapes}</g></svg></div><figcaption>${number(width)} × ${number(height)} · ${number(annotations.length)} 个标注${labels.length ? `<span>${escapeHtml(labels.join("、"))}</span>` : ""}</figcaption>`;
}

export function openDatasetGeneration(dependencies) {
  const { state, api, jsonOptions, openModal, closeModal, escapeHtml, onCreated } = dependencies;
  const projectId = state.project?.id, epoch = state.selectionEpoch, section = state.currentSection, session = state.session;
  const project = state.project;
  const assets = [...state.assets];
  const approved = assets.filter(asset => asset.review_status === "approved").length;
  const total = assets.length;
  const contextCurrent = () => state.project?.id === projectId && state.selectionEpoch === epoch && state.currentSection === section && state.session === session;
  let form, controls, modal, step = 0, highestStep = 0, options = null, optionsError = "", optionsSerial = 0, preview = null, previewKey = "", previewBusy = false, selectedAsset = "";
  let disposed = false, previewQueue, resizeObserver, lastEditedControl, samplePreview = null, sampleKey = "", samplePayload = null, sampleBusy = false, sampleError = "", sampleVariant = 0;
  const current = () => !disposed && contextCurrent() && form?.isConnected;
  const includedCount = () => form.querySelector("#version-approved").checked ? approved : total;
  const trainCount = () => estimatedSplitCounts(includedCount(), controls.read()).train;
  const amountMode = () => form.querySelector('#augmentation-amount-mode').value;
  const amountField = () => form.querySelector(amountMode() === 'count' ? '#augmentation-target-count' : '#augmentation-multiplier');
  const readAmount = (focus = false) => {
    try { return augmentationTargetCount(amountMode(), amountField().value, trainCount(), includedCount()); }
    catch (error) { if (focus) amountField().focus(); throw error; }
  };
  const fieldNumber = (selector, low, high, label, integer = false, focus = true) => {
    const field = form.querySelector(selector), value = Number(field.value);
    if (!field.value.trim() || !Number.isFinite(value) || value < low || value > high || (integer && !Number.isInteger(value))) { if (focus) field.focus(); throw new Error(`${label}需为 ${low} 至 ${high} 的${integer ? "整数" : "数字"}。`); }
    return value;
  };
  const readPayload = ({ sample = false, focus = true } = {}) => {
    const readNumber = (selector, low, high, label, integer = false) => fieldNumber(selector, low, high, label, integer, focus);
    const ratios = controls.read();
    const name = form.querySelector("#version-name").value.trim();
    if (!name && !sample) { if (focus) form.querySelector("#version-name").focus(); throw new Error("请输入版本名称，方便之后选择训练数据。"); }
    const payload = { name: name || "样本预览", description: form.querySelector("#version-description").value.trim(), approved_only: form.querySelector("#version-approved").checked, train_ratio: ratios.train / 100, valid_ratio: ratios.valid / 100, test_ratio: ratios.test / 100, split_seed: readNumber("#version-seed", 0, 2147483647, "分组随机种子", true), augmentation: null };
    if (!includedCount()) throw new Error("没有符合条件的图片，请先完成标注审核，或调整数据范围。");
    if (form.querySelector("#generation-enable").checked) {
      if (!options && !sample) throw new Error("增强选项尚未加载，请重新加载后再继续。");
      const operations = METHODS.filter(method => form.querySelector(`[data-operation="${method.type}"]`).checked).map(method => {
        const operation = { type: method.type, probability: readNumber(`#augmentation-${method.type}-probability`, 1, 100, `${method.name}的应用概率`, true) / 100 };
        if (method.type === "rotate90") {
          operation.degrees = [...form.querySelectorAll('[data-rotation]:checked')].map(input => Number(input.value));
          if (!operation.degrees.length) throw new Error("请为直角旋转选择至少一个角度。");
        }
        const range = RANGE_PARAMETERS[method.type], single = SINGLE_PARAMETERS[method.type];
        if (range) {
          range.keys.forEach((key,index) => { operation[key] = readNumber(`#augmentation-${method.type}-${key}`, range.min, range.max, `${method.name}的${range.labels[index]}`, Boolean(range.integer)); });
          if (operation[range.keys[0]] > operation[range.keys[1]]) throw new Error(`${method.name}的最小值不能大于最大值。`);
        }
        if (single) operation[single.key] = readNumber(`#augmentation-${method.type}-${single.key}`, single.min, single.max, single.label);
        return operation;
      });
      if (!operations.length && !sample) throw new Error("请选择至少一种增强方式，或关闭数据增强后继续。");
      if (operations.length) payload.augmentation = { seed: readNumber("#augmentation-seed", 0, 2147483647, "增强随机种子", true), target_count: readAmount(focus), operations };
    }
    return payload;
  };
  const estimates = () => {
    const original = includedCount(), split = estimatedSplitCounts(original, controls.read());
    const enabled = form.querySelector("#generation-enable").checked && METHODS.some(method => form.querySelector(`[data-operation="${method.type}"]`).checked);
    let generated = 0;
    if (enabled) { try { generated = readAmount(); } catch {} }
    return { original, original_train: split.train, valid: split.valid, test: split.test, generated, final: original + generated, final_train: split.train + generated };
  };
  const countsMarkup = (counts, estimated = true) => `<p class="generation-training-count">训练集 <strong>${number(counts.original_train)} → ${estimated ? '最多 ' : ''}${number(counts.final_train)} 张</strong>，${estimated ? '计划' : '实际'}新增 ${number(counts.generated)} 张</p><div class="generation-counts" aria-label="${estimated ? "预计最多" : "实际"}图片数量"><div><span>全部原图</span><strong>${number(counts.original)}</strong></div><span class="generation-count-symbol" aria-hidden="true">＋</span><div><span>${estimated ? "最多新增" : "增强副本"}</span><strong>${number(counts.generated)}</strong></div><span class="generation-count-symbol" aria-hidden="true">＝</span><div class="generation-final-count"><span>${estimated ? "版本预计最多" : "版本实际总计"}</span><strong>${number(counts.final)}</strong></div></div><p class="hint">验证 ${number(counts.valid)} 张、测试 ${number(counts.test)} 张保持不变。${estimated && counts.generated ? "未发生变化或重复的副本会跳过，实际新增张数可能更少。" : "只对训练图片增强。"}</p>`;
  const invalidatePreview = () => { previewQueue?.cancel(); preview = null; previewKey = ""; previewBusy = false; sampleBusy = false; };
  const updateEstimates = () => {
    const counts = estimates();
    form.querySelector("[data-generation-estimate]").innerHTML = countsMarkup(counts, true);
    const included = includedCount(), onlyApproved = form.querySelector("#version-approved").checked;
    form.querySelector("#version-scope-summary").textContent = included ? `本次包含 ${number(included)} 张图片。${onlyApproved ? `均已审核通过${total > approved ? `，另有 ${number(total - approved)} 张尚未通过审核，本次不包含` : ""}。` : "包含尚未审核的图片，请先确认标注质量。"}` : "暂无审核通过的图片。请先完成审核，或调整上方数据范围。";
    const enabled = form.querySelector("#generation-enable").checked;
    form.querySelector("[data-augmentation-settings]").hidden = !enabled;
    form.querySelector("[data-no-augmentation]").hidden = enabled;
    form.querySelector('[data-amount-multiplier]').hidden = amountMode() !== 'multiplier';
    form.querySelector('[data-amount-count]').hidden = amountMode() !== 'count';
    const limit = Math.max(0, Math.min(5000, trainCount() * 20, 20000 - includedCount()));
    form.querySelector('#augmentation-target-count').max = String(limit);
    const feedback = form.querySelector('[data-augmentation-amount-feedback]');
    try {
      const target = readAmount(); feedback.classList.remove('generation-error');
      feedback.textContent = `训练原图 ${number(trainCount())} 张，计划新增 ${number(target)} 张，训练集最多 ${number(trainCount() + target)} 张。`;
    } catch (error) { feedback.classList.add('generation-error'); feedback.textContent = error.message; }
    form.querySelector('[data-augmentation-amount-limit]').textContent = `本次最多新增 ${number(limit)} 张。倍数包含训练原图，新增数量四舍五入为整数；验证集和测试集不变。`;
  };
  const setStep = index => {
    previewQueue?.cancel(); previewBusy = false; sampleBusy = false;
    step = index; highestStep = Math.max(highestStep, step);
    form.querySelectorAll("[data-generation-step]").forEach(panel => { panel.hidden = Number(panel.dataset.generationStep) !== step; });
    form.querySelectorAll("[data-generation-go]").forEach(button => { const n = Number(button.dataset.generationGo); button.disabled = n > highestStep; button.setAttribute("aria-current", n === step ? "step" : "false"); button.classList.toggle("active", n === step); });
    modal.querySelector("#generation-previous").hidden = step === 0;
    modal.querySelector("#modal-submit").textContent = step === 3 ? "生成数据版本" : `下一步：${STEPS[step + 1]}`;
    modal.querySelector("#modal-submit").disabled = false;
    form.querySelector("[data-generation-position]").textContent = `第 ${step + 1} / 4 步`;
    modal.querySelector("#modal-feedback").hidden = true;
    const heading = form.querySelector(`[data-generation-step="${step}"] h3`);
    setTimeout(() => { if (current() && step === index && index > 0) heading?.focus({ preventScroll: true }); }, 0);
    modal.querySelector(".modal-body").scrollTop = 0;
    modal.classList.toggle("is-augmentation-step", step === 2);
    if (step === 2) loadSamplePreview({ immediate: true });
    if (step === 3) loadPreview({ force: true });
  };
  const optionAvailability = method => {
    const entry = (options?.operations || []).find(item => item.type === method.type);
    if (!entry) return { available: false, reason: "服务端未提供此增强，请联系管理员检查版本。" };
    return { available: entry.available !== false, reason: entry.reason || "当前项目暂不支持此增强。" };
  };
  const loadOptions = async () => {
    const serial = ++optionsSerial;
    const feedback = form.querySelector("[data-generation-options-feedback]");
    feedback.innerHTML = `<span role="status">正在读取当前任务可用的增强方式…</span>`;
    form.querySelectorAll("[data-operation]").forEach(input => { input.disabled = true; });
    try {
      const result = await api(`/api/projects/${projectId}/augmentation-options`);
      if (!current() || serial !== optionsSerial) return;
      options = result; optionsError = ""; feedback.replaceChildren();
      METHODS.forEach(method => {
        const availability = optionAvailability(method), input = form.querySelector(`[data-operation="${method.type}"]`);
        input.disabled = !availability.available;
        if (!availability.available) input.checked = false;
        const reason = form.querySelector(`[data-operation-reason="${method.type}"]`);
        reason.textContent = availability.available ? "" : availability.reason; reason.hidden = availability.available;
      });
      if (step === 2) loadSamplePreview({ immediate: true });
    } catch (error) {
      if (!current() || serial !== optionsSerial) return;
      options = null; optionsError = error.message;
      feedback.innerHTML = `<span role="alert">增强选项未加载：${escapeHtml(optionsError)}</span><button type="button" class="btn ghost" data-options-retry>重新加载</button>`;
      feedback.querySelector("[data-options-retry]").onclick = () => { void loadOptions(); };
    }
  };
  const updateAssetSelectors = () => {
    const onlyApproved = form.querySelector('#version-approved').checked;
    const available = assets.filter(item => !onlyApproved || item.review_status === 'approved');
    if (selectedAsset && !available.some(item => item.id === Number(selectedAsset))) selectedAsset = '';
    const markup = `<option value="">自动选择训练图片</option>${available.map(item => `<option value="${item.id}">${escapeHtml(item.filename)}</option>`).join('')}`;
    form.querySelectorAll('[data-generation-asset]').forEach(selector => { selector.innerHTML = markup; selector.value = selectedAsset; });
  };
  const payloadWithAsset = payload => selectedAsset ? { ...payload, asset_id: Number(selectedAsset) } : payload;
  const previewExplanation = (result, payload, live = false) => {
    const asset = result.selected_asset, variants = result.variants || [];
    if (!payload.augmentation) return form.querySelector('#generation-enable').checked
      ? '尚未选择增强方式，当前展示原图。勾选一种方式后会自动更新效果。'
      : '当前未开启增强，图片和标注保持原样。';
    if (asset?.split !== 'train') return '这张图片属于验证集或测试集，不会生成增强副本。可改看有新增副本的训练图片。';
    if (asset?.planned_copies === 0) return '新增总数会分配给训练图片，这张图片本次没有分配到新增副本。原图仍会保留，可改看有新增副本的训练图片。';
    const partial = result.preview_truncated ? `这张图片计划新增 ${number(asset.planned_copies)} 份，目前仅预览前 ${number(result.preview_attempted ?? 3)} 份，其余副本尚未预览。` : '';
    if (!variants.length) return `本次预览未产生新增副本：所选方式可能未触发，或与原图、已有副本相同，已跳过。${partial}可调整参数、概率或更换图片。`;
    return `${partial || (live ? '展示当前图片的实际增强结果，最多预览 3 份副本。' : '最多展示 3 份增强副本。')}所选方式依次组合，标注同步变化；概率与随机种子会影响效果。`;
  };
  const renderSamplePreview = () => {
    if (!current()) return;
    const container = form.querySelector('[data-augmentation-preview]'), feedback = form.querySelector('[data-augmentation-preview-status]');
    const stale = Boolean(samplePreview && (sampleBusy || sampleError));
    const jump = form.querySelector('[data-live-preview-jump]');
    jump.querySelector('span').textContent = sampleError ? '预览未更新' : sampleBusy ? '正在更新预览…' : '预览已更新';
    jump.querySelector('strong').textContent = sampleError ? '查看提示 ↓' : '查看效果 ↓';
    container.classList.toggle('is-stale', stale);
    container.setAttribute('aria-busy', String(sampleBusy));
    feedback.className = `generation-live-status ${sampleError ? 'error' : sampleBusy ? 'loading' : ''}`;
    feedback.hidden = !sampleBusy && !sampleError;
    feedback.innerHTML = sampleError
      ? `<span>${escapeHtml(sampleError)}${samplePreview ? ' 下方保留的是上一次预览。' : ''}</span><button type="button" class="mini-action" data-live-preview-retry>重试</button>`
      : sampleBusy ? `<span class="spinner" aria-hidden="true"></span><span>${samplePreview ? '设置已变化，正在更新；下方为上一次预览。' : '正在生成图片与标注的真实预览…'}</span>` : '';
    feedback.querySelector('[data-live-preview-retry]')?.addEventListener('click', () => loadSamplePreview({ immediate: true, force: true }));
    form.querySelector('[data-live-preview-refresh]').disabled = sampleBusy;
    if (!samplePreview) {
      container.innerHTML = `<div class="generation-preview-empty"><strong>${sampleError ? '暂时无法显示预览' : '准备查看增强效果'}</strong><p>${sampleError ? '调整设置后会自动重试，也可以点击上方“重试”。' : '选择或调整增强方式后，这里会同步更新图片和标注。'}</p></div>`;
      return;
    }
    const result = samplePreview, variants = result.variants || [], asset = result.selected_asset;
    sampleVariant = Math.min(sampleVariant, Math.max(0, variants.length - 1));
    const variant = variants[sampleVariant], splitName = { train:'训练集', valid:'验证集', test:'测试集' }[asset?.split] || '';
    container.innerHTML = `<div class="generation-live-caption"><span title="${escapeHtml(asset?.filename || '')}">${escapeHtml(asset?.filename || '示例图片')}${splitName ? ` · ${splitName}` : ''}</span>${variants.length > 1 ? `<label>查看副本<select data-live-variant aria-label="查看哪一份增强副本">${variants.map((item, index) => `<option value="${index}" ${index === sampleVariant ? 'selected' : ''}>增强副本 ${item.variant_index || index + 1}</option>`).join('')}</select></label>` : ''}</div><div class="generation-preview-grid"><figure><h4>原图与标注</h4>${annotationPreview(result.original, escapeHtml, result.keypoint_schema || project.keypoint_schema)}</figure><figure><h4>${variant ? `增强副本 ${variant.variant_index || sampleVariant + 1}` : '当前效果 · 未新增副本'}</h4>${annotationPreview(variant || result.original, escapeHtml, result.keypoint_schema || project.keypoint_schema)}</figure></div><p class="generation-live-explanation">${escapeHtml(previewExplanation(result, samplePayload, true))}</p>${result.skipped_duplicates ? `<p class="hint">本次预览尝试中跳过了 ${number(result.skipped_duplicates)} 份未变化或重复的副本。</p>` : ''}${samplePayload.augmentation && (asset?.split !== 'train' || asset?.planned_copies === 0) ? '<button type="button" class="mini-action" data-preview-use-train>改看有新增副本的训练图片</button>' : ''}`;
    container.querySelector('[data-live-variant]')?.addEventListener('change', event => { sampleVariant = Number(event.target.value); renderSamplePreview(); });
    container.querySelector('[data-preview-use-train]')?.addEventListener('click', () => { selectedAsset = ''; updateAssetSelectors(); invalidatePreview(); loadSamplePreview({ immediate: true }); });
  };
  const loadSamplePreview = ({ immediate = false, force = false } = {}) => {
    if (!current() || step !== 2) return;
    let payload;
    try { payload = payloadWithAsset(readPayload({ sample: true, focus: false })); }
    catch (error) { previewQueue.cancel(); sampleBusy = false; sampleError = error.message; renderSamplePreview(); return; }
    const key = JSON.stringify(payload);
    if (!force && samplePreview && key === sampleKey) {
      previewQueue.cancel(); sampleBusy = false; sampleError = ''; samplePayload = payload; renderSamplePreview(); return;
    }
    previewQueue.schedule({ kind:'sample', payload, key }, { immediate });
  };
  const loadPreview = ({ force = false } = {}) => {
    if (!current() || step !== 3) return;
    const container = form.querySelector('[data-generation-preview]');
    let payload;
    try { payload = payloadWithAsset(readPayload({ focus: false })); }
    catch (error) {
      previewQueue.cancel(); previewBusy = false;
      form.querySelector('[data-generation-confirmation]').replaceChildren();
      container.innerHTML = `<p role="alert" class="generation-error">${escapeHtml(error.message)} 请返回前面步骤调整。</p>`;
      return;
    }
    const key = JSON.stringify(payload);
    if (!force && preview && key === previewKey) { previewQueue.cancel(); previewBusy = false; renderPreview(payload); return; }
    previewQueue.schedule({ kind:'confirmation', payload, key }, { immediate: true });
  };
  const renderPreview = payload => {
    modal.querySelector('#modal-feedback').hidden = true;
    form.querySelector('[data-generation-confirmation]').innerHTML = `<dl class="generation-confirmation"><div><dt>版本名称</dt><dd>${escapeHtml(payload.name)}</dd></div><div><dt>数据范围</dt><dd>${payload.approved_only ? '仅审核通过' : '包含未审核图片'}</dd></div><div><dt>增强设置</dt><dd>${payload.augmentation ? `${payload.augmentation.operations.map(operation => escapeHtml(methodName(operation.type))).join('、')}；训练集计划最多新增 ${number(payload.augmentation.target_count)} 张` : '不增强，保存原始图片和标注'}</dd></div></dl>${countsMarkup(preview.counts, true)}`;
    const asset = preview.selected_asset, variants = preview.variants || [];
    const splitName = { train:'训练集', valid:'验证集', test:'测试集' }[asset?.split] || '';
    form.querySelector('[data-generation-preview]').innerHTML = `<p class="generation-preview-caption">${escapeHtml(asset?.filename || '示例图片')}${splitName ? ` · ${splitName}` : ''}${asset?.split && asset.split !== 'train' ? '：此图片不会增强，新增副本仅来自训练集。' : ''}</p><div class="generation-preview-grid"><figure><h4>原图与标注</h4>${annotationPreview(preview.original, escapeHtml, preview.keypoint_schema || project.keypoint_schema)}</figure>${variants.map((variant,index) => `<figure><h4>增强副本 ${variant.variant_index || index + 1}</h4>${annotationPreview(variant,escapeHtml,preview.keypoint_schema || project.keypoint_schema)}</figure>`).join('')}</div><p class="hint">${escapeHtml(previewExplanation(preview,payload))}</p>${preview.skipped_duplicates ? `<p class="hint">本次预览尝试中跳过了 ${number(preview.skipped_duplicates)} 份未变化或重复的副本。</p>` : ''}`;
  };
  const parameterMarkup = method => {
    const field = (key, label, min, max, stepValue, value) => `<label>${label}<input id="augmentation-${method.type}-${key}" type="number" min="${min}" max="${max}" step="${stepValue}" value="${value}" inputmode="decimal"/></label>`;
    let body = "";
    if (method.type === "rotate90") body += `<fieldset class="generation-angles"><legend>逆时针旋转角度（随机选择一个）</legend>${[90, 180, 270].map(degrees => `<label><input type="checkbox" data-rotation value="${degrees}" checked/>${degrees}°</label>`).join("")}</fieldset>`;
    const range = RANGE_PARAMETERS[method.type], single = SINGLE_PARAMETERS[method.type];
    if (range) body += `<div class="generation-parameter-pair">${range.keys.map((key,index) => field(key, range.labels[index], range.min, range.max, range.step, range.values[index])).join('')}</div><p class="hint">范围 ${range.min} 至 ${range.max}。${range.hint}每份副本在范围内随机取值。</p>`;
    if (single) body += `${field(single.key, `${single.label}（${single.min}–${single.max}）`, single.min, single.max, single.step, single.value)}`;
    return `${body}<details class="generation-probability"><summary>应用概率（默认每次应用）</summary>${field("probability", "每份副本的应用概率（%）", 1, 100, 1, 100)}<p class="hint">每份副本独立随机决定是否使用此方法；未触发时该项保持不变。</p></details>`;
  };
  openModal({ title: "生成数据版本", subtitle: "先划分原始数据，再只对训练集生成增强副本。原图始终保留。", wide: true,
    body: `<div id="dataset-generation"><nav class="generation-steps" aria-label="生成版本步骤">${STEPS.map((label, index) => `<button type="button" data-generation-go="${index}" ${index ? "disabled" : ""}><span>${index + 1}</span>${label}</button>`).join("")}</nav><p class="generation-position" data-generation-position></p>
    <section data-generation-step="0"><h3 tabindex="-1">选择要保存的数据</h3><p class="hint">生成后的版本会固定保存图片、标注和增强配置，之后修改工作区不会改变此版本。</p><div class="field"><label for="version-name">版本名称</label><input id="version-name" type="text" maxlength="100" placeholder="例如：第一批审核完成的数据" value="V${state.versions.length + 1}"/></div><div class="field"><label for="version-description">版本说明（选填）</label><textarea id="version-description" maxlength="1000" placeholder="例如：新增夜间样本，模拟亮度变化"></textarea></div><label class="check-line version-approved"><input id="version-approved" type="checkbox" checked/>仅包含审核通过的图片（训练推荐）</label><p class="generation-scope" id="version-scope-summary" role="status"></p></section>
    <section data-generation-step="1" hidden><h3 tabindex="-1">为学习和评估分配图片</h3><p class="hint">先按原图分组。同一张原图和它的增强副本始终留在训练集，避免评估时重复看到训练图片。</p>${datasetSplitControlsMarkup()}<details class="pipeline-details"><summary>高级选项：重复相同的数据划分</summary><div class="field"><label for="version-seed">分组随机种子</label><input id="version-seed" type="number" min="0" max="2147483647" step="1" value="42"/><div class="hint">图片和比例不变时，保持此数字即可重复相同的分组。</div></div></details></section>
    <section data-generation-step="2" hidden><h3 tabindex="-1">模拟真实使用时的变化</h3><p class="hint">只选择实际可能遇到的变化。增强可以丰富训练样本，不能替代采集真实图片。选择与调整后，会自动更新图片和标注预览。</p><div class="generation-augmentation-layout"><div class="generation-augmentation-controls"><button type="button" class="generation-live-jump" data-live-preview-jump><span>正在更新预览…</span><strong>查看效果 ↓</strong></button><label class="generation-enable"><input id="generation-enable" type="checkbox"/><span><strong>开启训练集数据增强</strong><small>保留原图，另外生成带标注的增强副本</small></span></label><p class="generation-scope" data-no-augmentation>当前不增强：图片与标注保持原样。可以直接进入下一步确认。</p><div data-augmentation-settings hidden><div class="generation-amount"><div class="field"><label for="augmentation-amount-mode">增强数量设置方式</label><select id="augmentation-amount-mode"><option value="multiplier">按扩增倍数（包含训练原图）</option><option value="count">按新增张数</option></select></div><div class="field" data-amount-multiplier><label for="augmentation-multiplier">训练集扩增到原来的几倍</label><input id="augmentation-multiplier" type="number" min="1" max="21" step="any" value="2" inputmode="decimal"/><p class="hint">例如 2 倍：保留训练原图，再增加与训练原图相同数量的副本。</p></div><div class="field" data-amount-count hidden><label for="augmentation-target-count">计划新增多少张训练图片</label><input id="augmentation-target-count" type="number" min="1" max="5000" step="1" value="1" inputmode="numeric"/></div><p class="generation-amount-feedback" data-augmentation-amount-feedback role="status"></p><p class="hint" data-augmentation-amount-limit></p><p class="hint">所选方式依次组合到每份副本，不按方法数量增加份数。未变化或重复的结果会跳过，实际可能更少。</p></div><div class="generation-options-feedback" data-generation-options-feedback></div>${["方向变化", "拍摄条件", "画面质量"].map(group => `<fieldset class="generation-method-group"><legend>${group}</legend><div class="generation-methods">${METHODS.filter(method => method.group === group).map(method => `<article class="generation-method"><label class="generation-method-label"><input type="checkbox" data-operation="${method.type}" disabled/><span><strong>${method.name}</strong><span>${method.description}</span></span></label><p class="generation-unavailable" data-operation-reason="${method.type}" hidden></p><div class="generation-method-settings" data-operation-settings="${method.type}" hidden><p>${method.usage}</p><p class="generation-risk">注意：${method.risk}</p>${parameterMarkup(method)}</div></article>`).join("")}</div></fieldset>`).join("")}<details class="pipeline-details"><summary>高级选项：重复相同的增强效果</summary><div class="field"><label for="augmentation-seed">增强随机种子</label><input id="augmentation-seed" type="number" min="0" max="2147483647" step="1" value="42"/><p class="hint">同一图片与相同设置会产生相同结果；调整此数字可换一组随机效果。</p></div></details></div><div data-generation-estimate aria-live="polite"></div></div><aside class="generation-live-preview" aria-label="实时数据增强效果"><div class="generation-live-heading"><h4>实时增强预览</h4><span>仅预览，不保存版本</span></div><button type="button" class="generation-return-settings mini-action" data-live-settings>返回刚才的设置</button><div class="generation-preview-toolbar"><label for="augmentation-preview-asset">预览图片<select id="augmentation-preview-asset" data-generation-asset><option value="">自动选择训练图片</option></select></label><button type="button" class="btn ghost" data-live-preview-refresh title="重新读取图片与标注，更新预览">刷新</button></div><div class="generation-live-status" data-augmentation-preview-status role="status" aria-live="polite" hidden></div><div data-augmentation-preview></div></aside></div></section>
    <section data-generation-step="3" hidden><h3 tabindex="-1">检查效果，再生成固定版本</h3><div data-generation-confirmation></div><div class="generation-preview-toolbar"><label for="generation-preview-asset">预览图片<select id="generation-preview-asset" data-generation-asset><option value="">自动选择训练图片</option></select></label><button type="button" class="btn ghost" data-preview-refresh>重新预览</button></div><div data-generation-preview></div><p class="generation-freeze-note">预览不会保存版本。点击“生成数据版本”后才会保存当前数据；生成期间请等待完成。</p></section></div>`,
    submitText: "下一步：划分比例",
    onReady: () => {
      form = document.querySelector("#dataset-generation"); modal = form.closest(".modal"); modal.classList.add("dataset-generation-modal");
      previewQueue = createGenerationPreviewQueue({
        isCurrent: current,
        request: (job, signal) => api(`/api/projects/${projectId}/versions/preview`, { ...jsonOptions('POST', job.payload), signal }),
        onPending: job => {
          sampleBusy = job.kind === 'sample'; previewBusy = job.kind === 'confirmation';
          if (job.kind === 'sample') { sampleError = ''; renderSamplePreview(); return; }
          preview = null; previewKey = '';
          form.querySelector('[data-generation-confirmation]').replaceChildren();
          form.querySelector('[data-generation-preview]').innerHTML = '<div class="generation-preview-empty" role="status"><span class="spinner"></span><strong>正在核对当前数据与预览…</strong><p>会重新读取图片与标注，确认当前设置；本次不会保存版本。</p></div>';
          form.querySelector('[data-preview-refresh]').disabled = true;
          setTimeout(() => { if (current() && step === 3 && previewBusy) modal.querySelector('#modal-submit').disabled = true; }, 0);
        },
        onResult: (result, job) => {
          if (!result?.original || !result?.selected_asset || !result?.counts) throw new Error('预览结果不完整，请重试。');
          if (job.kind === 'sample') {
            samplePreview = result; sampleKey = job.key; samplePayload = job.payload; sampleVariant = 0; sampleBusy = false; sampleError = ''; renderSamplePreview();
          } else {
            if (!/^[a-f0-9]{64}$/.test(result.source_hash || '')) throw new Error('未能核对当前数据，请重新预览后再生成版本。');
            preview = result; previewKey = job.key; previewBusy = false;
            form.querySelector('[data-preview-refresh]').disabled = false; modal.querySelector('#modal-submit').disabled = false;
            renderPreview(job.payload);
          }
        },
        onError: (error, job) => {
          if (job.kind === 'sample') { sampleBusy = false; sampleError = error.message || '预览未完成，请重试。'; renderSamplePreview(); return; }
          previewBusy = false; preview = null; previewKey = '';
          form.querySelector('[data-preview-refresh]').disabled = false; modal.querySelector('#modal-submit').disabled = false;
          const container = form.querySelector('[data-generation-preview]');
          container.innerHTML = `<div class="generation-preview-empty generation-error" role="alert"><strong>预览未完成</strong><p>${escapeHtml(error.message)}</p><button type="button" class="btn ghost" data-preview-retry>重新预览</button></div>`;
          container.querySelector('[data-preview-retry]').onclick = () => loadPreview({ force: true });
        },
      });
      const previous = document.createElement("button"); previous.type = "button"; previous.id = "generation-previous"; previous.className = "btn ghost"; previous.textContent = "上一步"; previous.hidden = true;
      modal.querySelector(".modal-actions").prepend(previous); previous.onclick = () => { if (current() && step > 0) setStep(step - 1); };
      controls = bindDatasetSplitControls(form, includedCount);
      form.querySelector('#augmentation-target-count').value = String(Math.max(1, trainCount()));
      let previousAmountMode = amountMode();
      form.querySelector('#augmentation-amount-mode').addEventListener('input', () => {
        const nextMode = amountMode();
        if (nextMode === previousAmountMode) return;
        try {
          const previousField = form.querySelector(previousAmountMode === 'count' ? '#augmentation-target-count' : '#augmentation-multiplier');
          const target = augmentationTargetCount(previousAmountMode, previousField.value, trainCount(), includedCount());
          amountField().value = nextMode === 'count' ? String(target) : String(Number((1 + target / trainCount()).toFixed(6)));
        } catch { /* Keep the new mode's value editable when the previous one was invalid. */ }
        previousAmountMode = nextMode;
      });
      form.querySelectorAll("[data-generation-go]").forEach(button => { button.onclick = () => { if (current() && Number(button.dataset.generationGo) <= highestStep) setStep(Number(button.dataset.generationGo)); }; });
      form.querySelectorAll("[data-operation]").forEach(input => input.addEventListener("change", () => { form.querySelector(`[data-operation-settings="${input.dataset.operation}"]`).hidden = !input.checked; }));
      const settingsChanged = event => {
        if (event.target.matches('[data-generation-asset], [data-live-variant]')) return;
        if (event.target.closest('.generation-augmentation-controls')) lastEditedControl = event.target;
        invalidatePreview();
        modal.querySelector('#modal-feedback').hidden = true;
        if (event.target.id === 'version-approved') { selectedAsset = ''; controls.update(); updateAssetSelectors(); }
        updateEstimates();
        if (step === 2) loadSamplePreview();
      };
      form.addEventListener('input', settingsChanged);
      // Number/text inputs already updated on input. Their later blur/change
      // must not replace a preview button while the user is clicking it.
      form.addEventListener('change', event => { if (!event.target.matches('input, textarea')) settingsChanged(event); });
      form.querySelector("[data-split-default]").addEventListener("click", () => { invalidatePreview(); updateEstimates(); });
      form.querySelectorAll('[data-generation-asset]').forEach(selector => { selector.onchange = event => { selectedAsset = event.target.value; form.querySelectorAll('[data-generation-asset]').forEach(input => { input.value = selectedAsset; }); invalidatePreview(); if (step === 2) loadSamplePreview({ immediate: true }); else if (step === 3) loadPreview({ force: true }); }; });
      form.querySelector('[data-live-preview-jump]').onclick = () => form.querySelector('.generation-live-preview').scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start' });
      form.querySelector('[data-live-settings]').onclick = () => {
        const target = lastEditedControl?.isConnected ? lastEditedControl : form.querySelector('#generation-enable');
        (target.closest('.generation-method, .generation-amount') || target).scrollIntoView({ block:'center', behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
        target.focus({ preventScroll:true });
      };
      form.querySelector('[data-live-preview-refresh]').onclick = () => loadSamplePreview({ immediate: true, force: true });
      form.querySelector('[data-preview-refresh]').onclick = () => { invalidatePreview(); loadPreview({ force: true }); };
      // Pin the preview only when its entire contents fit in the one scrollable
      // modal body. A short viewport or long feedback always remains scrollable.
      const bodyElement = modal.querySelector('.modal-body'), liveElement = form.querySelector('.generation-live-preview');
      resizeObserver = new ResizeObserver(() => {
        if (!current()) return;
        const fits = step === 2 && window.innerWidth > 850 && liveElement.offsetHeight + 24 <= bodyElement.clientHeight;
        modal.classList.toggle('is-preview-sticky', fits);
      });
      resizeObserver.observe(bodyElement); resizeObserver.observe(liveElement);
      updateAssetSelectors(); updateEstimates(); setStep(0); void loadOptions();
    },
    onClose: () => { disposed = true; optionsSerial += 1; resizeObserver?.disconnect(); previewQueue?.dispose(); },
    onSubmit: async () => {
      if (!current()) throw new Error("页面已切换，请在当前项目重新生成数据版本。");
      const payload = readPayload();
      if (step < 3) { setStep(step + 1); return; }
      if (previewBusy) throw new Error("真实预览正在生成，请等待完成后再保存版本。");
      const expected = JSON.stringify(selectedAsset ? { ...payload, asset_id: Number(selectedAsset) } : payload);
      if (!preview || previewKey !== expected) { void loadPreview(); throw new Error("请先完成当前设置的预览，再生成数据版本。"); }
      modal.querySelector("#generation-previous").disabled = true;
      modal.querySelector("#modal-submit").innerHTML = `<span class="spinner"></span>正在生成数据版本…`;
      try {
        const version = await api(`/api/projects/${projectId}/versions`, jsonOptions("POST", { ...payload, expected_source_hash: preview.source_hash }));
        if (!current()) { dependencies.notify("数据版本已在原项目生成，可返回该项目查看。"); return; }
        closeModal(); onCreated(version);
      } catch (error) {
        if (current() && error.status === 409) {
          invalidatePreview();
          form.querySelector("[data-generation-confirmation]").replaceChildren();
          form.querySelector("[data-generation-preview]").innerHTML = `<div class="generation-preview-empty generation-error" role="alert"><strong>数据已变化，需要重新预览</strong><p>图片或标注在预览后发生了变化。本次尚未生成版本，请检查新的预览再保存。</p><button type="button" class="btn ghost" data-preview-retry>重新预览</button></div>`;
          form.querySelector("[data-preview-retry]").onclick = () => { void loadPreview(); };
        }
        throw error;
      } finally { if (current()) modal.querySelector("#generation-previous").disabled = false; }
    },
  });
}
