const SAMPLE_PAGE_SIZE = 8;
const MAX_SAMPLES = 3;

export function aiPreviewMarkup() {
  return `<section class="ai-preview-panel" data-ai-preview aria-label="AI 标注效果预览">
    <div class="ai-preview-heading"><h3>先看标注效果</h3><span class="ai-preview-only">预览不保存标注</span></div>
    <details class="ai-preview-picker" data-preview-picker><summary><span data-preview-sample-summary>选择试标图片</span><small>更换图片</small></summary>
      <div class="ai-preview-picker-body"><label class="ai-preview-search"><input type="search" data-preview-search aria-label="搜索试标图片" placeholder="按文件名搜索" autocomplete="off"/></label><div class="ai-preview-samples" data-preview-samples></div><div class="ai-preview-sample-pages" data-preview-sample-pages></div><p class="hint" data-preview-picker-hint>建议选择有代表性的图片，例如目标较小或背景复杂的图片。</p></div>
    </details>
    <div class="ai-preview-toolbar"><div class="ai-preview-result-tabs" data-preview-result-tabs role="group" aria-label="切换预览图片"></div><button type="button" class="btn primary" data-preview-run>预览效果</button></div>
    <div class="ai-preview-feedback" data-preview-feedback role="status" aria-live="polite"></div>
    <div class="ai-preview-viewer" data-preview-viewer><div class="ai-preview-canvas" data-preview-canvas><div class="ai-preview-empty"><strong>查看 AI 会如何标注</strong><span>点击“预览效果”，检查类别、位置和漏标情况。</span></div></div>
      <div class="ai-preview-view-controls"><label class="ai-preview-overlay-toggle"><input type="checkbox" data-preview-overlay checked disabled/><span>显示预测</span></label><span class="ai-preview-image-name" data-preview-image-name></span><div class="ai-preview-zoom"><button type="button" class="mini-action" data-preview-zoom="out" aria-label="缩小预览" disabled>−</button><button type="button" class="mini-action" data-preview-zoom="fit" disabled>适应</button><button type="button" class="mini-action" data-preview-zoom="in" aria-label="放大预览" disabled>+</button></div></div>
    </div>
    <div class="ai-preview-result-summary" data-preview-result-summary></div>
  </section>`;
}

// Batch-only switches do not affect inference. A broader batch scope can reuse
// the preview as long as the sampled images still belong to that scope.
function inferenceSignature(payload, ids) {
  const { asset_ids, replace_existing_auto, include_reviewed, ...inference } = payload;
  return JSON.stringify({ ...inference, asset_ids: [...ids].sort((a, b) => a - b) });
}

export function mountAiPreview(container, dependencies) {
  const { api, jsonOptions, escapeHtml, projectId, getAssets, getPayload, isCurrent = () => true, onChange = () => {}, keypointSchema } = dependencies;
  const panel = container.matches?.('[data-ai-preview]') ? container : container.querySelector('[data-ai-preview]');
  if (!panel) throw new Error('未找到 AI 预览区域');
  const query = selector => panel.querySelector(selector);
  let disposed = false, busy = false, requestSerial = 0, controller = null;
  let assets = [], selectedIds = [], initialized = false, page = 0, search = '';
  let response = null, resultSignature = '', currentSignature = '', validationError = '', error = '', notice = '';
  let activeIndex = 0, showOverlay = true, zoom = 1, slowTimer = null;
  const current = () => !disposed && panel.isConnected && isCurrent();
  const getState = () => ({ busy, hasFreshResult: Boolean(response && currentSignature && resultSignature === currentSignature), stale: Boolean(response && resultSignature !== currentSignature), selectedCount: selectedIds.length, resultCount: response?.items?.length || 0 });
  const emit = () => { if (current()) onChange(getState()); };

  function readConfiguration() {
    validationError = '';
    try {
      const payload = getPayload();
      currentSignature = selectedIds.length ? inferenceSignature(payload, selectedIds) : '';
      return payload;
    } catch (problem) {
      currentSignature = '';
      validationError = problem.message || '请先完成左侧标注设置。';
      return null;
    }
  }

  function renderSamples() {
    const matches = assets.filter(asset => String(asset.filename || '').toLocaleLowerCase().includes(search.toLocaleLowerCase()));
    const pages = Math.max(1, Math.ceil(matches.length / SAMPLE_PAGE_SIZE));
    page = Math.min(page, pages - 1);
    query('[data-preview-sample-summary]').textContent = `试标图片 ${selectedIds.length} / ${MAX_SAMPLES} 张`;
    query('[data-preview-samples]').innerHTML = matches.slice(page * SAMPLE_PAGE_SIZE, (page + 1) * SAMPLE_PAGE_SIZE).map(asset => {
      const selected = selectedIds.includes(asset.id), disabled = !selected && selectedIds.length >= MAX_SAMPLES;
      return `<label class="ai-preview-sample ${selected ? 'selected' : ''} ${disabled ? 'unavailable' : ''}" title="${escapeHtml(asset.filename || '')}"><input type="checkbox" data-preview-sample="${asset.id}" ${selected ? 'checked' : ''} ${disabled ? 'disabled' : ''}/><img src="/api/assets/${asset.id}/thumbnail?size=160" alt="" loading="lazy"/><span>${escapeHtml(asset.filename || `图片 ${asset.id}`)}</span></label>`;
    }).join('') || `<p class="hint">${assets.length ? '没有匹配的图片，请修改文件名。' : '当前范围没有可试标的图片，请调整处理范围。'}</p>`;
    query('[data-preview-sample-pages]').innerHTML = `<span>${search ? `找到 ${matches.length} 张` : `当前范围 ${assets.length} 张`}${selectedIds.length ? `，已选 ${selectedIds.length} 张` : ''}</span>${pages > 1 ? `<div><button type="button" class="mini-action" data-preview-page="prev" ${page === 0 ? 'disabled' : ''} aria-label="上一页试标图片">上一页</button><span>${page + 1} / ${pages}</span><button type="button" class="mini-action" data-preview-page="next" ${page === pages - 1 ? 'disabled' : ''} aria-label="下一页试标图片">下一页</button></div>` : ''}`;
  }

  function renderFeedback() {
    const state = getState(), node = query('[data-preview-feedback]');
    let message = '', kind = '';
    if (error) { message = error; kind = 'error'; }
    else if (busy) { message = notice || `正在预览 ${selectedIds.length} 张图片，请稍候…`; kind = 'busy'; }
    else if (state.stale) { message = '参数或试标图片已改变，下方保留的是上一次结果。请重新预览，确认新的效果。'; kind = 'stale'; }
    else if (notice) { message = notice; kind = 'stale'; }
    else if (!selectedIds.length) message = '请展开“试标图片”，至少选择 1 张图片。';
    else if (validationError) message = validationError;
    // The modal footer already announces a successful, current preview. Reserve
    // the space above the image for errors, work in progress, or stale results.
    node.textContent = message; node.className = `ai-preview-feedback ${kind}`; node.hidden = !message;
    const button = query('[data-preview-run]');
    button.disabled = busy || !selectedIds.length || Boolean(validationError);
    button.textContent = busy ? '正在预览…' : response || notice || error ? '重新预览' : '预览效果';
    panel.setAttribute('aria-busy', String(busy));
    panel.classList.toggle('has-stale-preview', state.stale);
  }

  function renderTabs() {
    const items = response?.items || [];
    query('[data-preview-result-tabs]').innerHTML = items.length ? items.map((item, index) => `<button type="button" class="ai-preview-result-tab ${index === activeIndex ? 'active' : ''}" data-preview-result="${index}" aria-pressed="${index === activeIndex}" title="${escapeHtml(item.filename || '')}"><img src="/api/assets/${Number(item.asset_id)}/thumbnail?size=96" alt=""/><span>图片 ${index + 1}</span><small>${item.annotations?.length || 0} 个</small></button>`).join('') : `<span class="ai-preview-before-run">${selectedIds.length ? `先试标 ${selectedIds.length} 张` : '尚未选择图片'}</span>`;
  }

  const colorFor = annotation => /^#(?:[\da-f]{3}|[\da-f]{6})$/i.test(annotation.label_color || '') ? annotation.label_color : '#69ddac';
  const numeric = value => Number.isFinite(Number(value)) ? Number(value) : null;
  const visiblePoint = point => point && numeric(point.x) != null && numeric(point.y) != null && Number(point.visibility) > 0;
  const labelFor = annotation => `${annotation.label_name || '未命名类别'}${annotation.confidence == null ? '' : ` ${Math.round(Math.max(0, Math.min(1, Number(annotation.confidence) || 0)) * 100)}%`}`;

  function shapesMarkup(item) {
    return (item.annotations || []).map(annotation => {
      const points = Array.isArray(annotation.points) ? annotation.points.map(numeric) : [];
      if (points.some(point => point == null)) return '';
      const color = colorFor(annotation), label = labelFor(annotation);
      let shape = '';
      if (['rectangle', 'keypoints'].includes(annotation.shape_type) && points.length === 4) shape += `<rect x="${Math.min(points[0], points[2])}" y="${Math.min(points[1], points[3])}" width="${Math.abs(points[2] - points[0])}" height="${Math.abs(points[3] - points[1])}"/>`;
      if (annotation.shape_type === 'polygon' && points.length >= 6 && points.length % 2 === 0) shape += `<polygon points="${points.join(' ')}" fill="${color}" fill-opacity=".13"/>`;
      if (annotation.shape_type === 'keypoints') {
        const keypoints = annotation.keypoints || [], schema = response?.keypoint_schema || keypointSchema;
        for (const [start, end] of schema?.skeleton || []) if (visiblePoint(keypoints[start]) && visiblePoint(keypoints[end])) shape += `<line x1="${Number(keypoints[start].x)}" y1="${Number(keypoints[start].y)}" x2="${Number(keypoints[end].x)}" y2="${Number(keypoints[end].y)}"/>`;
        keypoints.forEach((point, index) => { if (visiblePoint(point)) shape += `<circle data-preview-keypoint cx="${Number(point.x)}" cy="${Number(point.y)}" r="4" fill="${Number(point.visibility) === 1 ? 'none' : '#fff'}"><title>${escapeHtml(schema?.names?.[index] || `关键点 ${index + 1}`)}</title></circle>`; });
      }
      if (!shape) return '';
      const xs = points.filter((_, index) => index % 2 === 0), ys = points.filter((_, index) => index % 2 === 1);
      const x = Math.max(0, Math.min(...xs)), y = Math.max(0, Math.min(...ys));
      return `<g class="ai-preview-shape" fill="none" stroke="${color}">${shape}<text data-preview-shape-label data-label-x="${x}" data-label-y="${y}" fill="#fff" stroke="#172231" paint-order="stroke" stroke-linejoin="round">${escapeHtml(label)}</text><title>${escapeHtml(label)}</title></g>`;
    }).join('');
  }

  function sizeCanvas() {
    if (!current()) return;
    const canvas = query('[data-preview-canvas]'), svg = canvas.querySelector('svg'), item = response?.items?.[activeIndex];
    if (!svg || !item) return;
    const width = Number(item.width), height = Number(item.height);
    if (!(width > 0 && height > 0)) return;
    const fit = Math.min(Math.max(1, canvas.clientWidth - 24) / width, Math.max(1, canvas.clientHeight - 24) / height);
    const scale = Math.max(.00001, fit * zoom), inverse = 1 / scale;
    svg.style.width = `${width * scale}px`; svg.style.height = `${height * scale}px`;
    svg.querySelectorAll('[data-preview-keypoint]').forEach(point => point.setAttribute('r', String(4 * inverse)));
    svg.querySelectorAll('[data-preview-shape-label]').forEach(label => {
      const x = Number(label.dataset.labelX), y = Number(label.dataset.labelY);
      label.setAttribute('font-size', String(12 * inverse));
      label.setAttribute('x', String(Math.max(3 * inverse, Math.min(width - 3 * inverse, x + 3 * inverse))));
      label.setAttribute('y', String(Math.max(15 * inverse, y - 5 * inverse)));
    });
    query('[data-preview-zoom="out"]').disabled = zoom <= 1;
    query('[data-preview-zoom="in"]').disabled = zoom >= 4;
    query('[data-preview-zoom="fit"]').textContent = zoom === 1 ? '适应' : `${zoom}×`;
  }

  function renderResult() {
    renderTabs();
    const item = response?.items?.[activeIndex], canvas = query('[data-preview-canvas]');
    query('[data-preview-overlay]').disabled = !item;
    panel.querySelectorAll('[data-preview-zoom]').forEach(button => { button.disabled = !item; });
    if (!item) return;
    const annotations = item.annotations || [], width = Number(item.width), height = Number(item.height);
    if (!(width > 0 && height > 0)) {
      canvas.innerHTML = '<div class="ai-preview-empty"><strong>预览图片尺寸无效</strong><span>请重新预览或换一张图片。</span></div>';
      return;
    }
    // Use the authenticated project asset endpoint instead of trusting a model URL.
    canvas.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(item.filename || '试标图片')}，${annotations.length} 个预测标注"><image href="/api/assets/${Number(item.asset_id)}/image" width="${width}" height="${height}"/><g class="ai-preview-predictions" ${showOverlay ? '' : 'visibility="hidden"'}>${shapesMarkup(item)}</g></svg>`;
    canvas.scrollLeft = 0; canvas.scrollTop = 0;
    query('[data-preview-image-name]').textContent = item.filename || `图片 ${activeIndex + 1}`;
    query('[data-preview-image-name]').title = `${item.filename || ''} · ${width} × ${height}`;
    const counts = item.counts || {}, filtered = Math.max(0, Number(counts.predicted || 0) - Number(counts.accepted ?? annotations.length));
    const countsByLabel = new Map();
    for (const annotation of annotations) {
      const name = annotation.label_name || '未命名类别';
      const entry = countsByLabel.get(name) || { name, color: colorFor(annotation), count: 0, classification: annotation.shape_type === 'classification', confidence: annotation.confidence };
      entry.count += 1; countsByLabel.set(name, entry);
    }
    const warnings = [...new Set([...(response.warnings || []), ...(item.warnings || [])])].filter(value => typeof value === 'string');
    const pending = response.pending_labels || [];
    const mismatched = Number(counts.filtered_unknown_label || 0) + Number(counts.filtered_target_label || 0);
    query('[data-preview-result-summary]').innerHTML = `<div class="ai-preview-count-line"><strong>${response.task_type === 'classify' ? annotations.length ? '预测类别' : '尚未得到图片类别' : `保留 ${annotations.length} 个预测对象`}</strong>${filtered ? `<span>另有 ${filtered} 个结果未保留</span>` : ''}</div>${annotations.length ? `<div class="ai-preview-labels" data-preview-labels ${showOverlay ? '' : 'hidden'}>${[...countsByLabel.values()].map(entry => `<span><i style="background:${entry.color}"></i>${escapeHtml(entry.name)}<strong>${entry.classification ? entry.confidence == null ? '' : `${Math.round(Number(entry.confidence) * 100)}%` : entry.count}</strong></span>`).join('')}</div>` : `<p class="ai-preview-no-detections">没有保留的预测结果。请核对模型与目标类别，再调整提示词、置信度或更换图片试标。</p>`}${counts.filtered_confidence ? `<p class="hint">${Number(counts.filtered_confidence)} 个结果低于当前置信度阈值。</p>` : ''}${mismatched ? `<p class="hint">${mismatched} 个结果的类别与本次目标不匹配，请核对模型类别和提示词。</p>` : ''}${counts.filtered_geometry ? `<p class="hint">${Number(counts.filtered_geometry)} 个结果不符合当前任务的标注要求，请检查模型的任务类型。</p>` : ''}${pending.length ? `<p class="hint">正式标注时将创建新类别：${escapeHtml(pending.join('、'))}。本次预览不会创建类别。</p>` : ''}${warnings.length ? `<details class="ai-preview-warnings"><summary>查看预览提示（${warnings.length}）</summary><ul>${warnings.map(warning => `<li>${escapeHtml(warning)}</li>`).join('')}</ul></details>` : ''}`;
    sizeCanvas();
  }

  function refresh() {
    if (!current()) return;
    assets = getAssets().filter(asset => Number.isSafeInteger(asset.id) && asset.id > 0);
    const allowed = new Set(assets.map(asset => asset.id)), previousCount = selectedIds.length;
    selectedIds = selectedIds.filter(id => allowed.has(id));
    if (!initialized || previousCount && !selectedIds.length) selectedIds = assets.length ? [assets[0].id] : [];
    initialized = true;
    readConfiguration();
    if (!busy) { error = ''; notice = ''; }
    renderSamples(); renderTabs(); renderFeedback(); emit();
  }

  async function run() {
    if (!current() || busy) return;
    const payload = readConfiguration();
    if (!payload || !selectedIds.length) { renderFeedback(); emit(); return; }
    const signature = currentSignature, sampleIds = [...selectedIds], serial = ++requestSerial;
    controller = new AbortController();
    const requestController = controller;
    busy = true; error = ''; notice = ''; renderFeedback(); emit();
    slowTimer = setTimeout(() => {
      if (current() && serial === requestSerial && busy) { notice = '模型仍在处理试标图片，请稍候。关闭窗口后不再等待结果。'; renderFeedback(); }
    }, 12000);
    try {
      const result = await api(`/api/projects/${projectId}/annotation-preview`, { ...jsonOptions('POST', { ...payload, asset_ids: sampleIds }), signal: requestController.signal });
      if (!current() || serial !== requestSerial) return;
      readConfiguration();
      if (signature !== currentSignature) {
        notice = '试标期间参数或图片已改变。请重新预览，以查看当前设置的效果。';
        return;
      }
      if (!Array.isArray(result?.items) || result.items.length !== sampleIds.length || new Set(result.items.map(item => item.asset_id)).size !== sampleIds.length || result.items.some(item => !sampleIds.includes(item.asset_id))) throw new Error('未收到完整的预览结果，请重新预览。');
      response = result; resultSignature = signature; activeIndex = 0; zoom = 1; showOverlay = true;
      query('[data-preview-overlay]').checked = true;
      notice = ''; renderResult();
    } catch (problem) {
      if (!current() || serial !== requestSerial || requestController.signal.aborted) return;
      readConfiguration();
      if (signature !== currentSignature) { notice = '试标期间参数或图片已改变。请按当前设置重新预览。'; return; }
      error = problem.message || '预览未完成，请检查参数后重试。';
    } finally {
      clearTimeout(slowTimer);
      if (current() && serial === requestSerial) { busy = false; controller = null; renderFeedback(); emit(); }
    }
  }

  const handleClick = event => {
    const button = event.target.closest('button');
    if (!button || !panel.contains(button)) return;
    if (button.hasAttribute('data-preview-run')) void run();
    if (button.hasAttribute('data-preview-page')) { page += button.dataset.previewPage === 'next' ? 1 : -1; renderSamples(); }
    if (button.hasAttribute('data-preview-result')) { activeIndex = Number(button.dataset.previewResult); zoom = 1; renderResult(); }
    if (button.hasAttribute('data-preview-zoom')) {
      const action = button.dataset.previewZoom;
      zoom = action === 'fit' ? 1 : Math.max(1, Math.min(4, zoom + (action === 'in' ? 1 : -1)));
      sizeCanvas();
    }
  };
  const handleChange = event => {
    if (event.target.hasAttribute('data-preview-sample')) {
      const id = Number(event.target.dataset.previewSample);
      if (event.target.checked && selectedIds.length < MAX_SAMPLES) selectedIds.push(id);
      else if (!event.target.checked) selectedIds = selectedIds.filter(value => value !== id);
      readConfiguration(); error = ''; notice = ''; renderSamples(); renderTabs(); renderFeedback(); emit();
      query(`[data-preview-sample="${id}"]`)?.focus({ preventScroll: true });
    }
    if (event.target.hasAttribute('data-preview-overlay')) {
      showOverlay = event.target.checked;
      query('.ai-preview-predictions')?.setAttribute('visibility', showOverlay ? 'visible' : 'hidden');
      const labels = query('[data-preview-labels]'); if (labels) labels.hidden = !showOverlay;
    }
  };
  const handleSearch = event => { search = event.target.value.trim(); page = 0; renderSamples(); };
  panel.addEventListener('click', handleClick);
  panel.addEventListener('change', handleChange);
  query('[data-preview-search]').addEventListener('input', handleSearch);
  const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(sizeCanvas) : null;
  observer?.observe(query('[data-preview-canvas]'));
  function dispose() {
    if (disposed) return;
    disposed = true; requestSerial += 1; controller?.abort(); clearTimeout(slowTimer); observer?.disconnect();
    panel.removeEventListener('click', handleClick); panel.removeEventListener('change', handleChange);
    query('[data-preview-search]').removeEventListener('input', handleSearch);
  }
  refresh();
  return { refresh, dispose, getState };
}
