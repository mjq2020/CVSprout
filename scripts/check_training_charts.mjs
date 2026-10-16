#!/usr/bin/env node
// Exercise the public chart renderer: axis labels, plotted geometry, accessible
// hover text, missing epochs and the displayed values must agree.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const source = readFileSync(new URL('../static/training-charts.js', import.meta.url), 'utf8');
const { trainingChartMarkup, trainingSeries, evaluationMetric, recordedLosses, metricNumber } =
  await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

const keys = { detect: 'metrics/mAP50-95(B)', segment: 'metrics/mAP50-95(M)', pose: 'metrics/mAP50-95(P)', classify: 'metrics/accuracy_top1' };
const run = task => ({ id: 1, task, status: 'running', current_epoch: 6, total_epochs: 20 });
const scoreHistory = (task, values) => ({ fetchedAt: 1, history: values.map((value, index) => ({ epoch: index + 1, metrics: { [keys[task]]: value } })) });
const svgOf = markup => markup.match(/<svg class="training-loss-chart"[\s\S]*?<\/svg>/)?.[0] || '';
const ticksOf = svg => [...svg.matchAll(/<line[^>]*class="loss-grid"[^>]*\/><text[^>]*>([^<]+)<\/text>/g)].map(match => Number(match[1]));
const hoverOf = svg => [...svg.matchAll(/<circle[^>]*><title>([^<]+)<\/title><\/circle>/g)].map(match => match[1]);
const pointsOf = svg => [...svg.matchAll(/<polyline[^>]*points="([^"]*)"/g)].flatMap(match => match[1].split(' ').map(point => point.split(',').map(Number)));
const assertInsidePlot = svg => {
  const gridYs = [...svg.matchAll(/<line[^>]*y1="([^"]+)"[^>]*class="loss-grid"/g)].map(match => Number(match[1]));
  assert.equal(gridYs.length, 5);
  const low = Math.min(...gridYs), high = Math.max(...gridYs);
  for (const [x, y] of pointsOf(svg)) {
    assert.ok(Number.isFinite(x) && Number.isFinite(y), `Invalid plotted coordinate: ${x}, ${y}`);
    assert.ok(y >= low - 1e-8 && y <= high + 1e-8, `Point escaped chart range: ${y}`);
  }
};

for (const task of Object.keys(keys)) {
  for (const values of [[0, .25, .75, 1], [.45, .5], [0], [1], [.5, .5, .5], [0, 0], [1, 1], [1e-7, 1 - 1e-7]]) {
    const markup = trainingChartMarkup(run(task), scoreHistory(task, values), { metric: 'score' });
    const svg = svgOf(markup);
    assert.ok(svg, `${task} must render recorded zero/constant/boundary scores`);
    assert.deepEqual(ticksOf(svg), [0, .25, .5, .75, 1], `${task}: probability axes cannot be padded above 1 or rescaled between runs`);
    assertInsidePlot(svg);
    const hovers = hoverOf(svg);
    assert.equal(hovers.length, values.length);
    values.forEach((value, index) => assert.ok(hovers[index].endsWith(`：${metricNumber(value)}`), hovers[index]));
    assert.ok(svg.includes('纵轴固定为 0 到 1'));
  }
  const empty = trainingChartMarkup(run(task), scoreHistory(task, []), { metric: 'score' });
  assert.equal(svgOf(empty), '', 'An unrecorded run must not invent a zero curve');
  const missing = trainingChartMarkup(run(task), scoreHistory(task, [null, undefined, '', ' ']), { metric: 'score' });
  assert.equal(svgOf(missing), '', 'Null and empty strings remain missing, never zero');
  const invalidState = scoreHistory(task, [-.01, 1.08, NaN, Infinity, false]);
  const invalid = trainingChartMarkup(run(task), invalidState, { metric: 'score' });
  assert.equal(svgOf(invalid), '', 'Out-of-range scores must not be clamped into perfect/zero results');
  assert.ok(invalid.includes('有效范围为 0–1'));
  assert.ok(trainingSeries(run(task), invalidState).rows.every(row => row.score === null));
}

// Legacy aliases and numeric strings retain their semantics at both boundaries.
assert.equal(evaluationMetric('detect', { map50_95: '1' }).value, 1);
assert.equal(evaluationMetric('classify', { top1: '0' }).value, 0);
assert.equal(evaluationMetric('classify', { accuracy: '.875' }).value, .875);
assert.equal(evaluationMetric('classify', null).value, null);
assert.equal(evaluationMetric('detect', { map50_95: 1.08 }).value, null);

const gaps = scoreHistory('detect', [.2, null, .4, 1.1, .6]);
const gapMarkup = trainingChartMarkup(run('detect'), gaps, { metric: 'score', allRows: true });
const gapSvg = svgOf(gapMarkup);
assert.equal((gapSvg.match(/<polyline /g) || []).length, 3, 'Missing or invalid epochs must break lines');
assert.equal(hoverOf(gapSvg).length, 3, 'No hover point is invented for a missing epoch');
assert.match(gapMarkup, /<th scope="row">4<\/th><td>—<\/td>/);
const skipped = { fetchedAt: 1, history: [gaps.history[0], { ...gaps.history[2], epoch: 7 }] };
assert.equal((svgOf(trainingChartMarkup(run('detect'), skipped, { metric: 'score' })).match(/<polyline /g) || []).length, 2, 'Nonconsecutive recorded epochs must not connect');

for (const values of [[2, 5], [0], [3, 3], [1e-7, 2e-7], [1e30, 2e30], [Number.MAX_VALUE]]) {
  const state = { fetchedAt: 1, history: values.map((value, index) => ({ epoch: index + 1, losses: { train: value, val: value / 2, train_components: { box_loss: value }, val_components: { box_loss: value / 2 } } })) };
  for (const metric of ['loss', 'component:box_loss']) {
    const svg = svgOf(trainingChartMarkup(run('detect'), state, { metric }));
    assert.ok(svg, `Loss values must render: ${values}`);
    const ticks = ticksOf(svg);
    assert.equal(ticks[0], 0, 'Loss axes must start at zero');
    assert.ok(ticks.every(value => value >= 0), 'Loss ticks cannot be negative');
    if (Math.max(...values) > 1 && Math.max(...values) < 1e21) assert.ok(ticks.at(-1) > Math.max(...values), 'Loss axes still adapt above 1 with headroom');
    assertInsidePlot(svg);
    assert.ok(hoverOf(svg).some(text => text.endsWith(`：${metricNumber(values[0])}`)), 'Loss hover must keep the recorded magnitude');
  }
}
assert.equal(metricNumber(1e30), '1.00e+30', 'Scientific-notation exponent zeros cannot be trimmed as decimal zeros');
assert.equal(recordedLosses({ losses: { train: -1, val: 0 } }).train, null);
assert.equal(recordedLosses({ metrics: { 'train/box_loss': -1, 'train/cls_loss': 2 } }).train, null);
assert.equal(recordedLosses({ metrics: { 'train/box_loss': Number.MAX_VALUE, 'train/cls_loss': Number.MAX_VALUE } }).train, null);
assert.equal(svgOf(trainingChartMarkup(run('detect'), { fetchedAt: 1, history: [{ epoch: 1, losses: { train: -1, val: null } }] })), '');
console.log('PASS training charts: fixed 0–1 mAP/accuracy axes for all four tasks; zero/constant/missing/invalid values; consistent hover/table values; loss headroom, nonnegative range and extreme-value geometry.');
