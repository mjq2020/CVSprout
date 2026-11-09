import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { localizePayload, translateMessage } from '../static/api-i18n.js';

const en = (value, path = '/api/example', status = 200) => localizePayload(value, path, status, 'en');
const user = '项目不存在';
const block = { name: '任务块 001', owner_name: user, events: [{ reason: user, payload: { message: user } }] };
assert.equal(en(block, '/api/projects/1/work-blocks/1?detail=true').name, 'Work block 001');
assert.deepEqual(en(block, '/api/projects/1'), block);
assert.deepEqual(en(block, '/api/projects/1/work-blocks').events, block.events);
const model = en({ builtin: true, provider_type: 'local_yolo', name: 'yolov8n（本地 AI）', availability_error: 'checkpoint 无法加载：未安装 ultralytics' });
assert.equal(model.name, 'yolov8n (Local AI)');
assert.equal(model.availability_error, 'Checkpoint loading failed: Ultralytics not installed');
assert.equal(en({ builtin: false, provider_type: 'remote_detection', name: user, badge: '自定义' }).badge, 'Custom');
assert.equal(translateMessage('下载失败，可重试续传：ValueError: 项目不存在', 'en'), 'Download failed, retry or resume: ValueError: Project does not exist');
assert.equal(en({ detail: '项目不存在' }, '/api/login', 403).detail, 'Project does not exist');
const raw = { name: user, description: user, filename: user, reason: user, note: user,
  labels: [{ name: user }], attributes: { message: user }, config: { badge: user }, settings: { error: user },
  metadata: { message: user }, payload: { reason: user }, prompt: user };
assert.deepEqual(en(raw), raw);
assert.deepEqual(localizePayload(raw, '/api/projects/1', 200, 'zh-CN'), raw);
const options = en({ system_model: { available: false, reason: '当前训练任务为图像分类，不支持 reCamera Pro 系统模型导出。目前仅支持 YOLOv8 / YOLO11 目标检测。' }, blocked_reason: '设备已撤销或项目已归档' }, '/api/training-runs/1/conversion-options');
assert.doesNotMatch(JSON.stringify(options), /[\u3400-\u9fff]/);
assert.doesNotMatch(JSON.stringify(en({ formats: { onnx: { description: '通用交换格式，可交给 ONNX Runtime 或其他推理引擎。' } } }, '/api/training-runs/1/conversion-options')), /[\u3400-\u9fff]/);
assert.equal(en({ operations: [{ type: 'horizontal_flip', name: '水平翻转', available: true }] }, '/api/projects/1/augmentation-options').operations[0].name, 'Horizontal flip');
assert.equal(translateMessage('未收录的第三方诊断'.repeat(1000), 'en'), '未收录的第三方诊断'.repeat(1000));
const rules = JSON.parse(readFileSync(new URL('../static/locales/api.json', import.meta.url)));
for (const template of rules.templates) {
  const message = template.replace(/\{\d+\}/g, 'example');
  assert.doesNotMatch(translateMessage(message, 'en'), /[\u3400-\u9fff]/, template);
}
console.log('PASS API localization of persisted records, nested errors, deployment reasons, legacy responses and user data preservation');
