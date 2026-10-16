const FORMAT_META = {
  onnx: { label: "ONNX", target: "通用电脑与服务器", description: "交给 ONNX Runtime 等工具，在 CPU 或 GPU 上运行。", suffix: ".onnx" },
  torchscript: { label: "TorchScript", target: "PyTorch / LibTorch 应用", description: "适合已有 PyTorch 部署程序的设备。", suffix: ".torchscript" },
  rknn: { label: "RKNN", target: "瑞芯微 NPU 设备", description: "为指定瑞芯微芯片编译，需要匹配设备端运行时。", suffix: ".rknn" },
  engine: { label: "TensorRT", target: "NVIDIA 显卡", description: "为 NVIDIA GPU 编译；应匹配部署端 GPU 与 TensorRT 环境。", suffix: ".engine" },
};
const RKNN_TARGETS = ["rk3588", "rk3576", "rk3566", "rk3568", "rk3562", "rv1103", "rv1106", "rv1126b"];
const finite = value => value !== "" && value != null && Number.isFinite(Number(value));
export const conversionFormatName = format => FORMAT_META[format]?.label || String(format || "模型").toUpperCase();
export const conversionFormatDescription = format => FORMAT_META[format]?.description || "供相应设备运行的模型文件。";

export function conversionFormats(raw = {}) {
  return Object.entries(FORMAT_META).map(([format, meta]) => {
    const supplied = typeof raw[format] === "boolean" ? { available: raw[format] } : raw[format] || {};
    return { format, ...meta, ...supplied, available: Boolean(supplied.available), reason: supplied.reason || (supplied.available ? "" : "服务器尚未配置此格式的转换环境，请联系管理员。") };
  });
}

export function conversionFormatCards(raw, escapeHtml, selected = "onnx") {
  return `<div class="conversion-format-grid" role="radiogroup" aria-label="模型的部署格式">${conversionFormats(raw).map(config => `<label class="conversion-format-option ${config.available ? "" : "is-unavailable"}"><input type="radio" name="convert-format" value="${config.format}" ${config.format === selected ? "checked" : ""}/><span><strong>${escapeHtml(config.label)} <small>${escapeHtml(config.suffix)}</small></strong><b>${escapeHtml(config.target)}</b><span>${escapeHtml(config.description)}</span><em>${config.available ? "可以转换" : escapeHtml(config.reason)}</em></span></label>`).join("")}</div>`;
}

export function conversionSettingsEntries(item) {
  const settings = item.settings || {}, rknn = settings.rknn || {};
  const entries = [];
  if (item.format === "rknn") {
    entries.push(["目标芯片", rknn.target_platform?.toUpperCase() || "未记录"], ["模型精度", rknn.quantized ? "INT8 量化" : "FP16（不量化）"], ["输入通道", "RGB"]);
    if (rknn.quantized) entries.push(["校准图片", `${rknn.calibration_count ?? "—"} 张 · 冻结版本训练集`], ["量化算法", rknn.quantized_algorithm || "normal"]);
    if (rknn.mean_values) entries.push(["均值", rknn.mean_values.join(", ")]);
    if (rknn.std_values) entries.push(["标准差", rknn.std_values.join(", ")]);
    if (rknn.optimization_level != null) entries.push(["优化级别", String(rknn.optimization_level)]);
  } else {
    entries.push(["模型精度", settings.half ? "FP16" : "FP32"], ["输入尺寸", settings.dynamic ? "动态尺寸" : "固定尺寸"]);
    if (item.format === "engine") entries.push(["GPU", settings.device || "自动选择"], ["工作空间上限", settings.workspace == null ? "自动" : `${settings.workspace} GiB`]);
  }
  if (settings.image_size) entries.push(["图片尺寸", `${settings.image_size} × ${settings.image_size}`]);
  if (settings.batch != null) entries.push(["批量大小", String(settings.batch)]);
  return entries;
}

export function conversionSettingsSummary(item) {
  const settings = item.settings || {}, rknn = settings.rknn || {};
  const parts = item.format === "rknn" ? [rknn.target_platform?.toUpperCase(), rknn.quantized ? "INT8" : "FP16 · 不量化"] : [settings.half ? "FP16" : "FP32", settings.dynamic ? "动态输入" : "固定输入"];
  if (settings.image_size) parts.push(`${settings.image_size} × ${settings.image_size}`);
  if (item.format === "rknn" && rknn.quantized) parts.push(`${rknn.calibration_count ?? "—"} 张校准图`);
  return parts.filter(Boolean).join(" · ");
}

export function validateConversionPayload(payload, { config = {}, calibration = {}, devices = [] } = {}) {
  const issues = [];
  const add = (field, message) => issues.push({ field, message });
  const integer = (value, low, high) => Number.isInteger(value) && value >= low && value <= high;
  if (!config.available) add("", config.reason || "此转换格式当前不可用，请联系管理员检查依赖。");
  if (!integer(payload.image_size, 32, 4096) || payload.image_size % 32) add("convert-imgsz", "输入图片尺寸需为 32–4096 之间的 32 的倍数，例如 640。");
  if (!integer(payload.max_retries, 0, 10)) add("convert-max-retries", "失败重试次数需为 0–10 的整数。");
  if (!integer(payload.batch, 1, 64)) add("convert-batch", "批量大小需为 1–64 的整数。");
  const availableGpu = devices.filter(device => device.available !== false && (["cuda", "gpu"].includes(device.type) || /^cuda(?::|$)|^\d+$/.test(String(device.id))));
  const gpuChosen = payload.device === "auto" ? availableGpu.length > 0 : availableGpu.some(device => String(device.id) === payload.device);
  if ((payload.format === "engine" || payload.half) && !gpuChosen) add(payload.format === "engine" ? "convert-engine-device" : "convert-device", "这个设置需要可用的 NVIDIA 显卡。请选择 GPU，或请管理员配置转换环境。");
  if (payload.half && config.supports_half === false) add("convert-precision", "此格式不支持 FP16，请选择 FP32。");
  if (payload.format === "engine" && payload.workspace != null && (!finite(payload.workspace) || payload.workspace < 0.25 || payload.workspace > 64)) add("convert-workspace", "工作空间上限需为 0.25–64 GiB；留空则由转换工具自动选择。");
  if (payload.format === "rknn") {
    const rknn = payload.rknn || {}, targets = config.target_platforms || RKNN_TARGETS;
    if (!targets.includes(rknn.target_platform)) add("convert-rknn-target", "请选择当前工具支持的目标芯片，芯片型号必须与实际设备一致。");
    if ((config.int8_only_platforms || ["rv1103", "rv1106"]).includes(rknn.target_platform) && !rknn.quantized) add("convert-rknn-quantized", `${String(rknn.target_platform).toUpperCase()} 只支持 INT8，请启用量化并提供校准图片。`);
    if (!["normal", "mmse", "kl_divergence"].includes(rknn.quantized_algorithm)) add("convert-rknn-algorithm", "请选择支持的量化算法。");
    if (!integer(rknn.optimization_level, 0, 3)) add("convert-rknn-optimization", "优化级别需为 0–3。");
    if (!Array.isArray(rknn.mean_values) || rknn.mean_values.length !== 3 || rknn.mean_values.some(value => !finite(value) || Math.abs(Number(value)) > 1000000)) add("convert-rknn-mean", "均值需要 3 个绝对值不超过 1000000 的有限数值，按 R、G、B 顺序用逗号分隔。");
    if (!Array.isArray(rknn.std_values) || rknn.std_values.length !== 3 || rknn.std_values.some(value => !finite(value) || Number(value) <= 0 || Number(value) > 1000000)) add("convert-rknn-std", "标准差需要 3 个大于 0 且不超过 1000000 的数值，按 R、G、B 顺序填写。");
    if (!integer(rknn.calibration_count, 1, 1000)) add("convert-rknn-count", "校准图片数量需为 1–1000 的整数。");
    if (rknn.quantized) {
      if (!Number(calibration.available_count)) add("convert-rknn-count", calibration.reason || "这个训练版本的训练集没有可用校准图片，暂时无法量化。可先导出不量化模型。");
      else if (rknn.calibration_count > Number(calibration.available_count)) add("convert-rknn-count", `当前冻结版本只有 ${calibration.available_count} 张可用校准图片，请减少数量。`);
    }
  }
  return issues;
}

export function mountConversionForm(container, { run, options, resources = {}, escapeHtml: esc, onValidityChange }) {
  const resolvedFormats = Object.fromEntries(conversionFormats(options.formats).map(config => [config.format, Array.isArray(config.tasks) && !config.tasks.includes(run.task) ? { ...config, available: false, reason: "此格式暂不支持当前训练任务，请选择其他格式。" } : config]));
  const formats = conversionFormats(resolvedFormats), calibration = options.calibration || {}, defaults = options.defaults || {};
  const rknnDefaults = defaults.rknn || {}, rknnConfig = formats.find(item => item.format === "rknn");
  const selectedFormat = formats.find(item => item.available)?.format || "onnx";
  const targets = rknnConfig.target_platforms || RKNN_TARGETS;
  const algorithms = rknnConfig.quantized_algorithms || ["normal", "mmse", "kl_divergence"];
  const devices = [...new Map([{ id: "auto", name: "自动选择" }, ...(resources.devices || []).filter(item => item.available !== false)].map(item => [String(item.id), item])).values()];
  const count = Math.max(1, Math.min(Number(calibration.default_count || rknnDefaults.calibration_count || 100), Number(calibration.max_count || 1000), Number(calibration.available_count || 1)));
  const optionList = (items, selected, label = value => value) => items.map(item => `<option value="${esc(String(item))}" ${item === selected ? "selected" : ""}>${esc(label(item))}</option>`).join("");
  container.innerHTML = `<div class="conversion-form"><section class="conversion-form-step"><h3><span>1</span>选择模型要运行的设备</h3><p class="hint">四种格式对应不同部署环境。选择暂不可用的格式，也可以查看配置与所缺依赖。</p>${conversionFormatCards(resolvedFormats, esc, selectedFormat)}</section><section class="conversion-form-step"><h3><span>2</span>确认转换设置</h3><div id="conversion-format-notice" class="conversion-format-notice" role="status"></div><div class="form-grid"><div class="field"><label for="convert-imgsz">输入图片尺寸</label><input id="convert-imgsz" type="number" min="32" max="4096" step="32" value="${Number(defaults.image_size || run.settings?.image_size || 640)}"/><p class="hint">正方形边长，单位为像素；首次转换可保持训练时的尺寸。</p></div><div class="field" data-conversion-show="onnx torchscript engine"><label for="convert-precision">模型精度</label><select id="convert-precision"><option value="fp32">FP32 · 兼容优先</option><option value="fp16">FP16 · 需要 NVIDIA GPU</option></select><p class="hint">精度降低可能改变预测结果，转换后需要验证。</p></div><div class="field" data-conversion-show="rknn"><label for="convert-rknn-target">目标芯片</label><select id="convert-rknn-target">${optionList(targets, rknnDefaults.target_platform || "rk3588", value => value.toUpperCase())}</select><p class="hint">必须与实际设备一致；RV1126B 与 RV1126 是不同芯片。</p></div><div class="field" data-conversion-show="rknn"><label for="convert-rknn-quantized">模型精度</label><select id="convert-rknn-quantized"><option value="false" ${rknnDefaults.quantized ? "" : "selected"}>FP16 · 不量化，无需校准</option><option value="true" ${rknnDefaults.quantized ? "selected" : ""}>INT8 · 使用图片校准</option></select><p class="hint">RKNN 的 FP16 编译不需要 NVIDIA GPU。INT8 可减小模型，但需检查准确率。</p></div><div class="field" data-conversion-show="engine"><label for="convert-engine-device">用于编译的 GPU</label><select id="convert-engine-device">${devices.filter(item => item.id === "auto" || ["cuda", "gpu"].includes(item.type)).map(item => `<option value="${esc(String(item.id))}">${esc(item.name || String(item.id))}</option>`).join("")}</select><p class="hint">请使用与目标部署环境兼容的 GPU 和 TensorRT 版本。</p></div></div><div class="conversion-calibration" id="conversion-calibration" hidden><strong>用这次训练的冻结数据校准</strong><p>来源：数据版本 ${esc(String(calibration.version_id || run.version_id || "—"))} 的训练集。可用 <b>${Number(calibration.available_count || 0)}</b> 张；不会读取之后新导入的图片。</p><div class="field"><label for="convert-rknn-count">使用多少张校准图片</label><input id="convert-rknn-count" type="number" min="1" max="${Math.max(1, Math.min(1000, Number(calibration.available_count || 1)))}" step="1" value="${count}"/><p class="hint">选择能代表实际场景的数据。数量越多，转换通常越慢。</p></div>${calibration.reason ? `<p class="hint">${esc(calibration.reason)}</p>` : ""}</div><details class="pipeline-details conversion-advanced"><summary>高级设置（通常可保持默认）</summary><div class="form-grid"><div class="field" data-conversion-show="onnx torchscript"><label for="convert-device">执行转换的设备</label><select id="convert-device">${devices.map(item => `<option value="${esc(String(item.id))}" ${String(item.id) === (defaults.device || "auto") ? "selected" : ""}>${esc(item.name || String(item.id))}</option>`).join("")}</select></div><div class="field" data-conversion-show="onnx torchscript engine"><label for="convert-batch">批量大小</label><input id="convert-batch" type="number" min="1" max="64" step="1" value="${Number(defaults.batch || 1)}"/><p class="hint">一次输入的图片数。RKNN 固定为 1。</p></div><div class="field" data-conversion-show="engine"><label for="convert-workspace">编译工作空间上限（GiB）</label><input id="convert-workspace" type="number" min="0.25" max="64" step="0.25" placeholder="自动" value="${defaults.workspace == null ? "" : Number(defaults.workspace)}"/><p class="hint">留空自动选择。限制过小可能导致编译失败；这不是模型文件大小。</p></div><div class="field" data-conversion-show="rknn"><label for="convert-rknn-algorithm">量化算法</label><select id="convert-rknn-algorithm">${optionList(algorithms, rknnDefaults.quantized_algorithm || "normal", value => ({ normal: "Normal · 默认", mmse: "MMSE · 最小均方误差", kl_divergence: "KL divergence · 分布差异" })[value] || value)}</select><p class="hint">只在 INT8 量化时使用。</p></div><div class="field" data-conversion-show="rknn"><label for="convert-rknn-optimization">编译优化级别</label><select id="convert-rknn-optimization">${optionList(rknnConfig.optimization_levels || [0, 1, 2, 3], rknnDefaults.optimization_level ?? 3, value => `${value}${value === 3 ? " · 默认" : ""}`)}</select></div><div class="field" data-conversion-show="rknn"><label for="convert-rknn-mean">RGB 均值</label><input id="convert-rknn-mean" type="text" inputmode="decimal" value="${esc((rknnDefaults.mean_values || [0, 0, 0]).join(", "))}"/><p class="hint">三个数值，按 R、G、B 顺序，用逗号分隔。</p></div><div class="field" data-conversion-show="rknn"><label for="convert-rknn-std">RGB 标准差</label><input id="convert-rknn-std" type="text" inputmode="decimal" value="${esc((rknnDefaults.std_values || [255, 255, 255]).join(", "))}"/><p class="hint">三个大于 0 的数值，默认把 0–255 归一化到 0–1。</p></div><div class="field"><label for="convert-max-retries">最多允许重试次数</label><input id="convert-max-retries" type="number" min="0" max="10" step="1" value="2"/><p class="hint">失败后由你点击“按原设置重试”。</p></div></div><div class="check-stack"><label class="check-line" data-conversion-show="onnx engine"><input id="convert-dynamic" type="checkbox"/>动态批量与图片尺寸（部署程序需支持）</label><label class="check-line" data-conversion-show="onnx engine rknn"><input id="convert-simplify" type="checkbox" checked/>简化中间 ONNX 模型并检查导出结果</label></div><p class="hint" data-conversion-show="rknn">输入通道固定为 RGB；输入尺寸固定，批量固定为 1。归一化方式为（像素 − 均值）÷ 标准差；修改均值或标准差会改变模型的输入约定，请与部署端同步。</p></details></section><div id="conversion-validation" class="conversion-validation" role="status" aria-live="polite" hidden></div><section class="conversion-submit-summary" aria-labelledby="conversion-summary-heading"><h3 id="conversion-summary-heading">本次转换</h3><div id="conversion-submit-summary"></div></section></div>`;
  const node = id => container.querySelector(`#${id}`);
  const values = id => node(id).value.split(/[,，]/).map(value => value.trim()).map(value => value === "" ? NaN : Number(value));
  const number = id => node(id).value.trim() === "" ? NaN : Number(node(id).value);
  const read = () => {
    const format = container.querySelector('input[name="convert-format"]:checked').value;
    const rknn = format === "rknn";
    const payload = { format, image_size: number("convert-imgsz"), half: !rknn && node("convert-precision").value === "fp16", dynamic: ["onnx", "engine"].includes(format) && node("convert-dynamic").checked, simplify: format !== "torchscript" && node("convert-simplify").checked, device: rknn ? "cpu" : format === "engine" ? node("convert-engine-device").value : node("convert-device").value, max_retries: number("convert-max-retries"), batch: rknn ? 1 : number("convert-batch") };
    if (format === "engine") payload.workspace = node("convert-workspace").value === "" ? null : Number(node("convert-workspace").value);
    if (rknn) payload.rknn = { target_platform: node("convert-rknn-target").value, quantized: node("convert-rknn-quantized").value === "true", quantized_algorithm: node("convert-rknn-algorithm").value, optimization_level: Number(node("convert-rknn-optimization").value), mean_values: values("convert-rknn-mean"), std_values: values("convert-rknn-std"), input_color: "rgb", calibration_source: "version_train", calibration_count: Number(node("convert-rknn-count").value) };
    return payload;
  };
  const issuesFor = payload => validateConversionPayload(payload, { config: formats.find(item => item.format === payload.format), calibration, devices: resources.devices || [] });
  const refresh = () => {
    let payload = read();
    const config = formats.find(item => item.format === payload.format);
    const int8Only = payload.rknn && (config.int8_only_platforms || ["rv1103", "rv1106"]).includes(payload.rknn.target_platform);
    node("convert-rknn-quantized").querySelector('option[value="false"]').disabled = Boolean(int8Only);
    if (int8Only && !payload.rknn.quantized) { node("convert-rknn-quantized").value = "true"; payload = read(); }
    const issues = issuesFor(payload);
    container.querySelectorAll("[data-conversion-show]").forEach(element => { element.hidden = !element.dataset.conversionShow.split(" ").includes(payload.format); });
    node("conversion-calibration").hidden = !payload.rknn?.quantized;
    node("convert-rknn-algorithm").disabled = !payload.rknn?.quantized;
    const warnings = (config.warnings || []).filter(Boolean);
    if (payload.dynamic && run.task !== "classify") warnings.push("动态输入会导出常规 YOLO 输出，以支持较小尺寸。部署程序需自行执行置信度筛选和 NMS（去除重叠预测）；具体输出结构见部署说明。");
    if (int8Only) warnings.push(`${payload.rknn.target_platform.toUpperCase()} 仅支持 INT8，已启用量化。请在下方确认冻结训练集的校准图片数量。`);
    if (payload.rknn && (payload.rknn.mean_values.some(value => value !== 0) || payload.rknn.std_values.some(value => value !== 255))) warnings.push("你已修改 RGB 归一化参数，这会改变模型输入约定。请确认与训练及设备端预处理一致，否则预测结果可能不正确。");
    const notices = [...(!config.available ? [config.reason] : []), ...warnings];
    node("conversion-format-notice").hidden = !notices.length;
    node("conversion-format-notice").innerHTML = notices.map(message => `<p>${esc(String(message))}</p>`).join("");
    container.querySelectorAll('[aria-invalid="true"]').forEach(element => element.removeAttribute("aria-invalid"));
    issues.forEach(issue => { if (issue.field) node(issue.field)?.setAttribute("aria-invalid", "true"); });
    const fieldIssues = issues.filter(issue => issue.field);
    node("conversion-validation").hidden = !fieldIssues.length;
    node("conversion-validation").innerHTML = fieldIssues.map(issue => `<p>${esc(issue.message)}</p>`).join("");
    const quantized = payload.rknn?.quantized;
    node("conversion-submit-summary").innerHTML = `<strong>${esc(config.label)}${payload.rknn ? ` · ${esc(payload.rknn.target_platform.toUpperCase())}` : ""} · ${payload.rknn ? quantized ? "INT8" : "FP16（不量化）" : payload.half ? "FP16" : "FP32"}</strong><p>来源：${esc(run.name || `训练 ${run.id}`)}。输入 ${payload.image_size || "—"} × ${payload.image_size || "—"}，${payload.dynamic ? "允许动态尺寸" : "固定尺寸"}，批量 ${payload.batch || "—"}。</p><p>${payload.rknn ? quantized ? `使用冻结版本训练集的 ${payload.rknn.calibration_count || "—"} 张图片校准；原始训练模型会保留。` : "不使用校准图片，无需 NVIDIA GPU；原始训练模型会保留。" : `转换完成后下载 ${esc(config.suffix)} 文件；原始训练模型会保留。`}</p>`;
    onValidityChange?.(issues.length === 0, issues);
    return issues;
  };
  container.addEventListener("input", refresh); container.addEventListener("change", refresh);
  refresh();
  return { refresh, payload() { const payload = read(), issues = issuesFor(payload); if (issues.length) { if (issues[0].field) node(issues[0].field)?.focus(); throw new Error(issues[0].message); } return payload; } };
}
