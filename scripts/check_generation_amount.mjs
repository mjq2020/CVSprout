import assert from 'node:assert/strict';
import { augmentationTargetCount, versionAugmentationMarkup } from '../static/dataset-generation.js';

assert.equal(augmentationTargetCount('multiplier', 2, 3), 3, '2× includes original training images');
assert.equal(augmentationTargetCount('multiplier', 1.5, 3), 2, 'additional images round to an integer');
assert.equal(augmentationTargetCount('multiplier', 21, 250), 5000, 'both limits are inclusive');
assert.equal(augmentationTargetCount('count', 1, 300), 1, 'count mode allows small additions');
assert.equal(augmentationTargetCount('count', 20, 1), 20);
assert.throws(() => augmentationTargetCount('multiplier', 1.1, 1), /新增 0 张/);
assert.throws(() => augmentationTargetCount('multiplier', 21, 251), /最多新增 5,000 张/);
assert.throws(() => augmentationTargetCount('count', 21, 1), /最多新增 20 张/);
assert.throws(() => augmentationTargetCount('count', 5001, 1000), /最多新增 5,000 张/);
assert.equal(augmentationTargetCount('count', 1000, 13300, 19000), 1000);
assert.throws(() => augmentationTargetCount('count', 1001, 13300, 19000), /最多新增 1,000 张/);
for (const invalid of ['', ' ', null, undefined, NaN, Infinity, -1, 0, 1.5]) {
  assert.throws(() => augmentationTargetCount('count', invalid, 3));
}
assert.equal(augmentationTargetCount('multiplier', 1.003333, 300), 1, 'count to multiplier preserves small additions');
for (const invalid of [1, .9, 21.01]) assert.throws(() => augmentationTargetCount('multiplier', invalid, 10));
assert.throws(() => augmentationTargetCount('count', 1, 0), /没有训练图片/);
assert.throws(() => augmentationTargetCount('unsupported', 2, 3), /请选择/);

const escape = value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;');
const legacy = versionAugmentationMarkup({augmentation:{copies_per_image:2,operations:[{type:'brightness',min_factor:.8,max_factor:1.2}]}}, escape);
assert.match(legacy, /每张最多新增 2 份/);
const modern = versionAugmentationMarkup({augmentation:{target_count:7,copies_per_image:20,operations:[{type:'gamma',min_gamma:.8,max_gamma:1.2},{type:'hue',min_shift:-.05,max_shift:.05},{type:'jpeg_compression',min_quality:60,max_quality:95},{type:'downscale',min_scale:.5,max_scale:.9},{type:'autocontrast',cutoff:1}]},stats:{augmentation:{counts:{generated:5}}}}, escape);
assert.match(modern, /计划最多新增 7 张/); assert.match(modern, /实际新增 5 张/); assert.doesNotMatch(modern, /每张最多新增 20/);
for (const label of ['伽马值','色相偏移','质量 60','缩放倍率','截断比例']) assert.ok(modern.includes(label), label);
console.log('PASS generation amount rounding, limits, invalid input, legacy versions and new parameter summaries');
