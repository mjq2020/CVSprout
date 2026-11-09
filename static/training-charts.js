import { t, ta } from "./i18n.js?v=0.29.3";
// Plot only recorded epochs. Missing values remain missing, including legacy runs.
const finite = value => (typeof value === "number" || typeof value === "string" && value.trim() !== "") && Number.isFinite(Number(value)) ? Number(value) : null;
const nonnegative = value => { const number = finite(value); return number !== null && number >= 0 ? number : null; };
const proportion = value => { const number = nonnegative(value); return number !== null && number <= 1 ? number : null; };
const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
const componentNames = { box: t("目标框"), cls: t("类别"), dfl: t("框位置分布"), seg: t("实例轮廓"), sem: t("语义分割"), pose: t("关键点位置"), kobj: t("关键点可见性"), rle: t("关键点定位"), loss: t("分类") };

export function metricNumber(value) {
  const number = finite(value);
  return number === null ? "—" : number !== 0 && (Math.abs(number) < .0001 || Math.abs(number) >= 1e21) ? number.toExponential(2) : number.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
}

export function evaluationMetric(task, metrics = {}) {
  metrics = metrics || {};
  const suffix = task === "segment" ? "M" : task === "pose" ? "P" : "B";
  const names = { detect: t("目标检测 mAP"), segment: t("实例分割 mAP"), pose: t("关键点 mAP"), classify: t("分类准确率") };
  const candidates = task === "classify" ? ["metrics/accuracy_top1", "top1", "accuracy"] : [`metrics/mAP50-95(${suffix})`, "map50_95"];
  const key = candidates.find(candidate => proportion(metrics[candidate]) !== null);
  const invalid = candidates.some(candidate => {
    const value = metrics[candidate];
    return value != null && !(typeof value === "string" && value.trim() === "") && proportion(value) === null;
  });
  return { key, label: names[task] || t("检测 mAP"), value: key ? proportion(metrics[key]) : null, invalid, description: task === "classify" ? t("预测的第一类别正确的比例，越高越好。") : t("mAP50–95：综合不同匹配严格程度的验证表现，越高越好。") };
}

export function recordedLosses(item = {}) {
  if (item.losses) return { train: nonnegative(item.losses.train), val: nonnegative(item.losses.val), train_components: item.losses.train_components || {}, val_components: item.losses.val_components || {} };
  const metrics = item.metrics || {};
  const result = { train: null, val: null, train_components: {}, val_components: {} };
  for (const split of ["train", "val"]) {
    for (const [key, value] of Object.entries(metrics)) {
      if (key.startsWith(`${split}/`) && /loss$/.test(key)) result[`${split}_components`][key.slice(split.length + 1)] = nonnegative(value);
    }
    const components = Object.values(result[`${split}_components`]);
    if (!components.length) {
      const legacy = metrics[`${split}_loss`];
      if (Array.isArray(legacy)) legacy.forEach((value, index) => { result[`${split}_components`][`component_${index + 1}`] = nonnegative(value); });
      else if (finite(legacy) !== null) result[`${split}_components`].loss = nonnegative(legacy);
    }
    const values = Object.values(result[`${split}_components`]);
    result[split] = values.length && values.every(value => value !== null) ? nonnegative(values.reduce((sum, value) => sum + value, 0)) : null;
  }
  return result;
}

function componentLabel(key) {
  if (/^component_\d+$/.test(key)) return t("未命名分量 {0}", [key.split("_").at(-1)]);
  return componentNames[key.replace(/_loss$/, "")] || t("分量 {0}", [key.replace(/_loss$/, "")]);
}

export function trainingSeries(run, metricState = {}) {
  const history = [...new Map((metricState.history || []).filter(item => finite(item.epoch) !== null && Number(item.epoch) > 0).map(item => [Number(item.epoch), item])).values()].sort((a, b) => Number(a.epoch) - Number(b.epoch));
  const rows = history.map(item => {
    const score = evaluationMetric(run.task, item.metrics);
    return { epoch: Number(item.epoch), ...recordedLosses(item), score: score.value, invalidScore: score.invalid };
  });
  const keys = [...new Set(rows.flatMap(row => [...Object.keys(row.train_components), ...Object.keys(row.val_components)]))];
  const options = [{ key: "loss", label: t("总损失"), description: t("训练损失表示模型在学习图片上的误差；验证损失用于观察未参与学习的图片。通常越低越好，短时波动不等于训练失败。总损失是已记录分量之和。") }, ...keys.map(key => ({ key: `component:${key}`, label: t("{0}损失", [componentLabel(key)]), description: t("比较同一分量的训练与验证损失，通常越低越好。不同分量和不同任务的数值不宜直接比较。") })), { key: "score", label: evaluationMetric(run.task).label, description: t("{0} 仅反映当前验证集，部署前仍需检查真实场景。", [evaluationMetric(run.task).description]) }];
  return { rows, options };
}

function valuesFor(row, choice) {
  if (choice === "score") return [null, row.score];
  if (choice.startsWith("component:")) { const key = choice.slice(10); return [nonnegative(row.train_components[key]), nonnegative(row.val_components[key])]; }
  return [row.train, row.val];
}

function plot(rows, choice, label) {
  const points = rows.map(row => ({ epoch: row.epoch, values: valuesFor(row, choice) }));
  const valid = points.flatMap(point => point.values).filter(value => value !== null);
  if (!valid.length) return "";
  const width = 960, height = 270, left = 65, right = 25, top = 22, bottom = 42;
  const first = points[0].epoch, last = points.at(-1).epoch;
  // Scores are proportions; padding their upper bound made a perfect mAP
  // display a misleading 1.08 tick. Only nonnegative loss values need padding.
  const min = 0, max = valid.reduce((highest, value) => Math.max(highest, value), 0);
  const high = choice === "score" ? 1 : max > 0 ? Math.min(Number.MAX_VALUE, max + max * .08) : 1;
  const x = epoch => left + (last === first ? .5 : (epoch - first) / (last - first)) * (width - left - right);
  const y = value => height - bottom - (value - min) / (high - min) * (height - top - bottom);
  const grids = Array.from({ length: 5 }, (_, index) => {
    const value = min + (high - min) * (index / 4), yy = y(value);
    return `<line x1="${left}" y1="${yy}" x2="${width - right}" y2="${yy}" class="loss-grid"/><text x="${left - 10}" y="${yy + 4}" text-anchor="end">${escape(metricNumber(value))}</text>`;
  }).join("");
  const tickCount = Math.min(6, last - first + 1);
  const ticks = [...new Set(Array.from({ length: tickCount }, (_, i) => Math.round(first + (last - first) * i / Math.max(1, tickCount - 1))))];
  const lines = [0, 1].map(index => {
    if (choice === "score" && index === 0) return "";
    const segments = []; let segment = [], previous = null;
    for (const point of points) {
      if (point.values[index] === null || previous !== null && point.epoch !== previous + 1) { if (segment.length) segments.push(segment); segment = []; }
      if (point.values[index] !== null) segment.push(point);
      previous = point.epoch;
    }
    if (segment.length) segments.push(segment);
    return segments.map(group => {
      const step = Math.max(1, Math.ceil(group.length / 500));
      const sampled = group.filter((_, i) => i % step === 0 || i === group.length - 1);
      const dots = sampled.length <= 40 ? sampled : [sampled[0], sampled.at(-1)];
      return `<polyline class="loss-line ${index ? "validation" : "training"}" points="${sampled.map(point => `${x(point.epoch)},${y(point.values[index])}`).join(" ")}"/>${dots.map(point => `<circle class="loss-point ${index ? "validation" : "training"}" cx="${x(point.epoch)}" cy="${y(point.values[index])}" r="3"><title>${t("第 {0} 轮，{1}：{2}", [point.epoch, index ? choice === "score" ? label : t("验证") : t("训练"), metricNumber(point.values[index])])}</title></circle>`).join("")}`;
    }).join("");
  }).join("");
  return `<svg class="training-loss-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${ta("{0}随训练轮次变化；下方可查看每轮数值", [escape(label)])}"><title>${t("{0}随训练轮次变化", [escape(label)])}</title><desc>${t("横轴为训练轮次，纵轴为记录值。{0}缺失数据不补零，缺失轮次不连接。详细数据见下方数值表。", [choice === "score" ? t("显示验证表现，纵轴固定为 0 到 1。") : t("实线为训练，虚线为验证；纵轴从 0 开始并按损失大小调整。")])}</desc>${grids}${lines}${ticks.map(epoch => `<text x="${x(epoch)}" y="${height - 19}" text-anchor="middle">${epoch}</text>`).join("")}<text x="${width - right}" y="${height - 2}" text-anchor="end">${t("训练轮次")}</text></svg>`;
}

export function trainingChartMarkup(run, metricState = {}, view = {}) {
  const { rows, options } = trainingSeries(run, metricState);
  const choice = options.find(option => option.key === view.metric) || options[0];
  const latestRow = rows.at(-1);
  const current = metricState.latest_losses ? { train: nonnegative(metricState.latest_losses.train), val: nonnegative(metricState.latest_losses.val) } : latestRow || { train: null, val: null };
  const currentEpoch = Number(metricState.latest_epoch || latestRow?.epoch || 0);
  const invalidScoreCount = rows.filter(row => row.invalidScore).length;
  const scores = rows.filter(row => row.score !== null);
  const best = scores.reduce((value, row) => !value || row.score > value.score ? row : value, null);
  const metric = evaluationMetric(run.task, run.metrics);
  const total = Number(run.total_epochs || run.settings?.epochs || 0), completed = Number(run.current_epoch || 0);
  const progress = total ? Math.max(0, Math.min(100, completed / total * 100)) : 0;
  const stopped = ["completed", "failed", "cancelled"].includes(run.status);
  const chart = plot(rows, choice.key, choice.label);
  const stateMessage = run.status === "queued" ? t("正在排队。获得计算资源后开始训练，完成第一轮后会出现曲线。") : run.status === "running" && !completed ? t("正在初始化模型、准备数据或进行第一轮训练。第一轮完成后会更新曲线，无需重复提交。") : run.status === "failed" ? t("训练已停止，已记录的曲线仍然保留。请查看下方训练记录中的失败原因，修正后重试。") : run.status === "cancelled" ? t("训练已取消，已完成轮次的记录仍然保留。") : run.status === "completed" ? completed < total ? t("训练已完成，在第 {0} 轮结束，未跑满设置的 {1} 轮。可能触发早停，详情见运行日志。", [completed, total]) : t("训练已完成，可以下载模型或继续转换。") : run.cancel_requested ? t("正在停止训练，已保存的轮次记录会保留。") : t("每完成一轮自动更新。观察整体趋势，单轮波动不代表训练失败。");
  const empty = !metricState.fetchedAt && metricState.loading ? t("正在读取训练曲线…") : metricState.error && !metricState.fetchedAt ? t("尚未读取到曲线数据，请点击“刷新曲线”重试。") : rows.length ? t("这些轮次没有记录所选指标。可切换其他曲线查看；缺失值不会补为 0。") : stopped ? t("此训练记录没有逐轮指标，无法绘制历史曲线。已有的最终指标仍可在训练记录中查看。") : stateMessage;
  const tableRows = view.allRows ? rows : rows.slice(-100);
  return `<div class="training-monitor-status" role="status"><span class="job-status ${escape(run.status)}">${escape(({ queued: t("排队中"), running: t("训练中"), completed: t("已完成"), failed: t("失败"), cancelled: t("已取消") })[run.status] || t("正在更新"))}</span><span>${escape(stateMessage)}</span></div><div class="training-round-progress" role="progressbar" aria-label="${ta("已完成训练轮数")}" aria-valuemin="0" aria-valuemax="${total || 1}" aria-valuenow="${Math.min(completed, total || 1)}"><span style="width:${progress}%"></span></div><dl class="training-monitor-stats"><div><dt>${t("已完成轮数")}</dt><dd>${completed}<small> / ${total || "—"}</small></dd><span>${stopped ? t("本次实际完成轮数") : t("每轮学习一遍训练数据")}</span></div><div><dt>${t("当前训练损失")}</dt><dd>${metricNumber(current.train)}</dd><span>${currentEpoch ? t("第 {0} 轮 · 通常越低越好", [currentEpoch]) : t("等待第一轮结果")}</span></div><div><dt>${t("当前验证损失")}</dt><dd>${metricNumber(current.val)}</dd><span>${current.val === null ? currentEpoch ? t("第 {0} 轮未记录验证损失", [currentEpoch]) : t("尚无验证损失记录") : t("第 {0} 轮 · 观察泛化误差", [currentEpoch])}</span></div><div><dt>${t("最佳{0}", [escape(metric.label)])}</dt><dd>${metricNumber(best?.score)}</dd><span>${best ? t("第 {0} 轮 · 已记录轮次中的最高值", [best.epoch]) : t("尚无逐轮验证指标")}</span></div></dl><div class="training-chart-toolbar"><label for="training-curve">${t("显示曲线")}<select id="training-curve">${options.map(option => `<option value="${escape(option.key)}" ${choice.key === option.key ? "selected" : ""}>${escape(option.label)}</option>`).join("")}</select></label><div class="training-chart-legend">${choice.key === "score" ? `<span class="validation">${t("验证表现")}</span>` : `<span class="training">${t("训练损失")}</span><span class="validation">${t("验证损失")}</span>`}</div><button class="btn ghost" id="refresh-training-metrics" ${metricState.loading ? "disabled" : ""}>${metricState.loading ? t("读取中…") : t("刷新曲线")}</button></div>${metricState.error ? `<p class="training-chart-error" role="alert">${escape(metricState.error)}${rows.length ? t(" 下方保留上次读取的曲线。") : ""}</p>` : ""}${chart || `<div class="training-chart-empty" role="status">${escape(empty)}</div>`}<p class="training-chart-explanation">${escape(choice.description)}${choice.key === "score" && invalidScoreCount ? t(" 有 {0} 轮包含无效的比例指标，已忽略这些无效值；有效范围为 0–1，可查看原始训练日志核对。", [invalidScoreCount]) : ""}${choice.key !== "score" && rows.length && !rows.some(row => valuesFor(row, choice.key)[1] !== null) ? t(" 本记录没有验证损失，当前仅展示训练损失。") : ""}</p><details class="pipeline-details training-values" data-pipeline-detail="training-values-${run.id}" ${view.tableOpen ? "open" : ""}><summary>${t("查看每轮数值{0}", [rows.length ? t("（共 {0} 轮）", [rows.length]) : ""])}</summary>${rows.length ? `<div class="training-values-scroll" tabindex="0" aria-label="${ta("逐轮指标数值表")}"><table><caption>${t("{0}，缺失数值显示为 —", [escape(choice.label)])}</caption><thead><tr><th scope="col">${t("轮次")}</th>${choice.key === "score" ? `<th scope="col">${escape(choice.label)}</th>` : `<th scope="col">${t("训练")}</th><th scope="col">${t("验证")}</th>`}</tr></thead><tbody>${tableRows.map(row => { const values = valuesFor(row, choice.key); return `<tr><th scope="row">${row.epoch}</th>${(choice.key === "score" ? values.slice(1) : values).map(value => `<td>${metricNumber(value)}</td>`).join("")}</tr>`; }).join("")}</tbody></table></div>${rows.length > 100 && !view.allRows ? `<button class="btn ghost" id="all-training-values">${t("显示全部轮次（当前为最近 100 轮）")}</button>` : ""}` : `<p class="hint">${t("还没有逐轮记录。")}</p>`}</details>`;
}
