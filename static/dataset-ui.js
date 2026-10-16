const SPLITS = ["train", "valid", "test"];
const NAMES = { train: "训练", valid: "验证", test: "测试" };
const PURPOSES = { train: "让模型学习图片和标注", valid: "训练时检查效果、选择模型", test: "训练完成后评估最终效果" };

// Match the server's largest-remainder allocation, including its one-image
// training minimum. These are estimates until the version is created.
export function estimatedSplitCounts(total, percentages) {
  const raw = SPLITS.map(name => Math.max(0, total) * (percentages[name] / 100));
  const counts = raw.map(Math.floor);
  const order = [0, 1, 2].sort((a, b) => (raw[b] - counts[b]) - (raw[a] - counts[a]) || a - b);
  for (const index of order.slice(0, total - counts.reduce((sum, count) => sum + count, 0))) counts[index]++;
  if (total > 0 && counts[0] === 0) {
    const donor = counts[1] >= counts[2] ? 1 : 2;
    if (counts[donor]) { counts[donor]--; counts[0] = 1; }
  }
  return Object.fromEntries(SPLITS.map((name, index) => [name, counts[index]]));
}

export function adjustSplitPercentages(current, changed, requested) {
  if (!SPLITS.includes(changed) || !Number.isFinite(requested)) return { ...current };
  const value = Math.max(changed === "train" ? 1 : 0, Math.min(changed === "train" ? 100 : 99, Math.round(requested)));
  const others = SPLITS.filter(name => name !== changed), rest = 100 - value;
  const weight = current[others[0]] + current[others[1]];
  let first = weight ? Math.round(rest * current[others[0]] / weight) : rest;
  const minimum = others[0] === "train" ? 1 : 0;
  const maximum = rest - (others[1] === "train" ? 1 : 0);
  first = Math.max(minimum, Math.min(maximum, first));
  return { ...current, [changed]: value, [others[0]]: first, [others[1]]: rest - first };
}

// The handles move cumulative boundaries. Moving one never changes the group
// on the far side of the other boundary, unlike editing an individual number.
export function adjustSplitBoundaryPercentages(current, boundary, requested) {
  if (!Number.isFinite(requested)) return { ...current };
  const end = current.train + current.valid;
  if (boundary === "train") {
    const train = Math.max(1, Math.min(end, Math.round(requested)));
    return { train, valid: end - train, test: current.test };
  }
  if (boundary === "test") {
    const nextEnd = Math.max(current.train, Math.min(100, Math.round(requested)));
    return { train: current.train, valid: nextEnd - current.train, test: 100 - nextEnd };
  }
  return { ...current };
}

export function datasetSplitControlsMarkup() {
  return `<fieldset class="dataset-split-controls"><legend>分配训练、验证和测试数据</legend><p id="dataset-split-help" class="hint">拖动上方手柄调整训练与验证，下方手柄调整验证与测试，合计始终为 100%。训练至少占 1%，验证和测试可设为 0%。</p><div class="dataset-split-legend" aria-label="各组比例">${SPLITS.map(name => `<span class="${name}"><i aria-hidden="true"></i>${NAMES[name]} <strong data-split-percent="${name}"></strong></span>`).join("")}</div><div class="dataset-split-bar" role="group" aria-label="数据集分配比例"><div class="dataset-split-track" aria-hidden="true">${SPLITS.map(name => `<span class="${name}" data-split-segment="${name}"></span>`).join("")}</div><button type="button" class="dataset-split-handle train" data-split-handle="train" role="slider" aria-label="训练与验证分界（上方手柄）" aria-orientation="horizontal" aria-describedby="dataset-split-keyboard" title="拖动调整训练与验证比例"><span aria-hidden="true"></span></button><button type="button" class="dataset-split-handle test" data-split-handle="test" role="slider" aria-label="验证与测试分界（下方手柄）" aria-orientation="horizontal" aria-describedby="dataset-split-keyboard" title="拖动调整验证与测试比例"><span aria-hidden="true"></span></button></div><p id="dataset-split-keyboard" class="dataset-split-keyboard hint">手柄可用方向键调 1%，Page Up / Down 调 10%，Home / End 到边界。也可在下方输入精确比例，其他两组会自动分配剩余比例。</p>${SPLITS.map(name => `<div class="dataset-split-row ${name}"><div class="dataset-split-label"><label for="split-${name}">${NAMES[name]}</label><small>${PURPOSES[name]}</small></div><div class="dataset-split-number"><input id="split-${name}" data-split-number="${name}" type="number" min="${name === "train" ? 1 : 0}" max="${name === "train" ? 100 : 99}" step="1" inputmode="numeric" aria-label="${NAMES[name]}比例（百分比）"/><span aria-hidden="true">%</span></div><output data-split-count="${name}" for="split-${name}"></output></div>`).join("")}<div class="dataset-split-footer"><span>合计 <strong id="dataset-split-total">100%</strong></span><button type="button" class="btn ghost" data-split-default>恢复 70 / 20 / 10</button></div><p id="dataset-split-note" class="hint" role="status"></p></fieldset>`;
}

export function bindDatasetSplitControls(root, getTotal) {
  let values = { train: 70, valid: 20, test: 10 };
  let dragging = null;
  const bar = root.querySelector(".dataset-split-bar");
  const track = root.querySelector(".dataset-split-track");
  const handles = Object.fromEntries(["train", "test"].map(name => [name, root.querySelector(`[data-split-handle="${name}"]`)]));
  const bounds = name => name === "train" ? { value: values.train, min: 1, max: values.train + values.valid } : { value: values.train + values.valid, min: values.train, max: 100 };
  const emit = (element, type) => element.dispatchEvent(new Event(type, { bubbles: true }));
  const update = () => {
    const total = Number(getTotal()) || 0;
    const counts = estimatedSplitCounts(total, values);
    SPLITS.forEach(name => {
      root.querySelector(`[data-split-number="${name}"]`).value = values[name];
      root.querySelector(`[data-split-count="${name}"]`).textContent = `预计 ${counts[name].toLocaleString()} 张`;
      root.querySelector(`[data-split-percent="${name}"]`).textContent = `${values[name]}%`;
      root.querySelector(`[data-split-segment="${name}"]`).style.width = `${values[name]}%`;
    });
    Object.entries(handles).forEach(([name, handle]) => {
      const { value, min, max } = bounds(name);
      handle.style.left = `${value}%`;
      handle.setAttribute("aria-valuemin", String(min));
      handle.setAttribute("aria-valuemax", String(max));
      handle.setAttribute("aria-valuenow", String(value));
      handle.setAttribute("aria-valuetext", `训练 ${values.train}%，验证 ${values.valid}%，测试 ${values.test}%；预计分别 ${counts.train}、${counts.valid}、${counts.test} 张`);
    });
    const note = root.querySelector("#dataset-split-note");
    const empty = SPLITS.filter(name => values[name] > 0 && counts[name] === 0);
    note.classList.toggle("dataset-split-warning", total > 0 && (empty.length > 0 || !counts.valid));
    note.textContent = !total ? "选择要保存的图片后，会显示每组预计张数。" : !counts.valid ? `按 ${total} 张图片估算，当前分配没有验证图片，暂时不能用于训练。可增加验证比例或补充图片；仍可保存版本用于下载。` : empty.length ? `图片较少，按整数分配后${empty.map(name => NAMES[name]).join("、")}组为 0 张。创建后可查看实际分组；训练组至少保留 1 张。` : `按当前 ${total.toLocaleString()} 张图片估算，实际分组以生成后的版本为准。每张图片只进入其中一组。`;
  };
  const setBoundary = (name, requested) => {
    const next = adjustSplitBoundaryPercentages(values, name, requested);
    if (SPLITS.every(split => values[split] === next[split])) return false;
    values = next; update(); emit(handles[name], "input"); return true;
  };
  const pointerValue = event => {
    const rect = track.getBoundingClientRect();
    return rect.width > 0 ? (event.clientX - rect.left) / rect.width * 100 : NaN;
  };
  const startDrag = (event, name, jump) => {
    if (dragging || event.isPrimary === false || (event.pointerType !== "touch" && event.button !== 0)) return;
    event.preventDefault();
    const handle = handles[name]; handle.focus({ preventScroll: true });
    // Keep the original grab offset so a tap near a handle edge does not jump.
    const offset = jump ? 0 : pointerValue(event) - bounds(name).value;
    dragging = { name, pointerId: event.pointerId, offset, changed: false };
    handle.setPointerCapture(event.pointerId);
    bar.classList.add("dragging");
    if (jump) dragging.changed = setBoundary(name, pointerValue(event));
  };
  Object.entries(handles).forEach(([name, handle]) => {
    handle.addEventListener("pointerdown", event => startDrag(event, name, false));
    handle.addEventListener("keydown", event => {
      const { value, min, max } = bounds(name);
      const requested = { ArrowLeft: value - 1, ArrowDown: value - 1, ArrowRight: value + 1, ArrowUp: value + 1, PageDown: value - 10, PageUp: value + 10, Home: min, End: max }[event.key];
      if (requested == null || event.altKey || event.ctrlKey || event.metaKey) return;
      event.preventDefault(); event.stopPropagation();
      if (setBoundary(name, requested)) emit(handle, "change");
    });
    handle.addEventListener("lostpointercapture", event => {
      if (dragging?.pointerId === event.pointerId) finishDrag(event);
    });
  });
  track.addEventListener("pointerdown", event => {
    const value = pointerValue(event);
    const name = Math.abs(value - values.train) <= Math.abs(value - values.train - values.valid) ? "train" : "test";
    startDrag(event, name, true);
  });
  bar.addEventListener("pointermove", event => {
    if (!dragging || event.pointerId !== dragging.pointerId) return;
    event.preventDefault();
    dragging.changed = setBoundary(dragging.name, pointerValue(event) - dragging.offset) || dragging.changed;
  });
  const finishDrag = event => {
    if (!dragging || event.pointerId !== dragging.pointerId) return;
    const { name, pointerId, changed } = dragging; dragging = null;
    bar.classList.remove("dragging");
    if (handles[name].hasPointerCapture(pointerId)) handles[name].releasePointerCapture(pointerId);
    if (changed) emit(handles[name], "change");
  };
  bar.addEventListener("pointerup", finishDrag);
  bar.addEventListener("pointercancel", finishDrag);
  root.querySelectorAll("[data-split-number]").forEach(input => {
    input.addEventListener("input", () => {
      if (input.value.trim() === "" || !Number.isFinite(Number(input.value))) return;
      values = adjustSplitPercentages(values, input.dataset.splitNumber, Number(input.value));
      update();
    });
    input.addEventListener("blur", update);
  });
  root.querySelector("[data-split-default]").onclick = event => { values = { train: 70, valid: 20, test: 10 }; update(); emit(event.currentTarget, "input"); };
  update();
  return { update, read: () => ({ ...values }) };
}

export function versionSamplesMarkup(version, escapeHtml, compact = false) {
  const samples = (version.preview_assets || []).slice(0, compact ? 1 : 6);
  if (!samples.length) return "";
  return `<div class="version-samples ${compact ? "compact" : ""}" aria-label="版本中保存的图片示例">${samples.map(asset => `<div class="version-sample" title="${escapeHtml(asset.filename)} · ${NAMES[asset.split]}"><img src="${escapeHtml(asset.thumbnail_url)}?size=160" alt="${escapeHtml(asset.filename)}" loading="lazy" onerror="this.hidden=true;this.parentElement.classList.add('missing')"/><span class="version-image-fallback">图片暂不可用</span>${compact ? "" : `<small>${NAMES[asset.split]}</small>`}</div>`).join("")}</div>`;
}

export function openVersionImages(version, dependencies) {
  const { state, api, openModal, closeModal, escapeHtml } = dependencies;
  const projectId = state.project?.id, epoch = state.selectionEpoch, session = state.session;
  const current = () => state.project?.id === projectId && state.currentSection === "versions" && state.selectionEpoch === epoch && state.session === session;
  let content, split = "all", serial = 0, loading = false, shown = 0, total = 0;
  const labels = { all: "全部图片", train: "训练集", valid: "验证集", test: "测试集" };
  const load = async (append = false) => {
    const request = ++serial, requestedSplit = split;
    loading = true;
    const grid = content.querySelector("[data-version-image-grid]");
    const button = content.querySelector("[data-version-more]");
    const feedback = content.querySelector("[data-version-image-feedback]");
    feedback.hidden = true; button.disabled = true;
    if (!append) { shown = 0; grid.innerHTML = `<p class="hint" role="status">正在读取版本中的图片…</p>`; }
    try {
      const result = await api(`/api/versions/${version.id}/assets?split=${requestedSplit}&offset=${append ? shown : 0}&limit=48`);
      if (!current() || !content.isConnected || request !== serial) return;
      const markup = result.items.map(asset => `<article class="version-image-card"><a class="version-image-link" href="${escapeHtml(asset.image_url)}" target="_blank" rel="noopener" aria-label="查看 ${escapeHtml(asset.filename)} 的完整版本图片（新标签页）"><img src="${escapeHtml(asset.thumbnail_url)}?size=320" alt="${escapeHtml(asset.filename)}" loading="lazy" onerror="this.hidden=true;this.parentElement.classList.add('missing')"/><span class="version-image-fallback">图片暂不可用<br/>请联系管理员检查版本文件</span></a><strong title="${escapeHtml(asset.filename)}">${escapeHtml(asset.filename)}</strong><span>${labels[asset.split]} · ${asset.variant_index ? `增强副本 ${asset.variant_index}` : "原始图片"} · ${asset.annotation_count} 个标注</span><small>${asset.width} × ${asset.height}</small></article>`).join("");
      if (append) grid.insertAdjacentHTML("beforeend", markup); else grid.innerHTML = markup || `<p class="hint">这个版本的${labels[requestedSplit]}中没有图片。可切换其他分组查看。</p>`;
      shown += result.items.length; total = result.total;
      content.querySelector("[data-version-image-count]").textContent = `${labels[requestedSplit]}：已显示 ${shown.toLocaleString()} / ${total.toLocaleString()} 张`;
      button.hidden = !result.has_more; button.textContent = `继续显示 ${Math.min(48, total - shown)} 张`;
    } catch (error) {
      if (!current() || !content.isConnected || request !== serial) return;
      if (!append) grid.innerHTML = "";
      feedback.innerHTML = `<span>${escapeHtml(error.message)}</span><button class="btn ghost" data-version-retry>重新加载</button>`; feedback.hidden = false;
      feedback.querySelector("[data-version-retry]").onclick = () => { void load(append); };
      button.hidden = true;
    } finally {
      if (request === serial) { loading = false; if (content.isConnected) button.disabled = false; }
    }
  };
  openModal({
    title: `${version.name || `V${version.version_number}`} · 版本图片`, wide: true,
    subtitle: "查看生成版本时保存的图片与分组。之后修改或删除工作区图片，不会改变这里的内容。点击缩略图可查看完整图片。",
    body: `<div id="version-image-browser"><div class="version-image-filters" aria-label="版本图片分组">${Object.entries(labels).map(([name, label]) => `<button class="btn ghost ${name === "all" ? "active" : ""}" data-version-image-split="${name}" aria-pressed="${name === "all"}">${label} <strong>${name === "all" ? version.asset_count : version.splits?.[name]?.asset_count || 0}</strong></button>`).join("")}</div><p class="hint" data-version-split-purpose>训练用于学习，验证用于训练中检查，测试用于最终评估。</p><p class="version-image-count" data-version-image-count role="status"></p><div class="version-image-grid" data-version-image-grid></div><div class="version-image-feedback" data-version-image-feedback role="alert" hidden></div><button class="btn ghost version-more" data-version-more hidden></button></div>`,
    submitText: "关闭",
    onReady: () => {
      content = document.querySelector("#version-image-browser");
      document.querySelector("#modal-cancel").hidden = true;
      content.querySelectorAll("[data-version-image-split]").forEach(button => button.onclick = () => {
        if (!current() || split === button.dataset.versionImageSplit) return;
        split = button.dataset.versionImageSplit;
        content.querySelectorAll("[data-version-image-split]").forEach(tab => { tab.classList.toggle("active", tab === button); tab.setAttribute("aria-pressed", String(tab === button)); });
        content.querySelector("[data-version-split-purpose]").textContent = split === "all" ? "训练用于学习，验证用于训练中检查，测试用于最终评估。" : `${labels[split]}：${PURPOSES[split]}。`;
        void load();
      });
      content.querySelector("[data-version-more]").onclick = () => { if (!loading && current()) void load(true); };
      void load();
    },
    onSubmit: closeModal,
  });
}
