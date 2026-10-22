import assert from "node:assert/strict";
import { loadProjectWorkBlocks, WORK_BLOCKS_COMPATIBILITY_MESSAGE } from "../static/project-api.js";

const project = { id: 12, name: "Existing project" };
const health = { version: "0.20.2" };
const failure = status => Object.assign(new Error(status === 404 ? "未找到这项数据，可能已被移除。" : `HTTP ${status}`), { status });
const missingInterface = failure(404);
const absent = async path => { assert.equal(path, "/api/projects/12/work-blocks"); throw missingInterface; };

for (const version of ["0.0.0", "0.19.99", "0.20.2", "0.20.999"]) {
  for (const record of [project, { ...project, work_blocks_enabled: 0 }, { ...project, work_blocks_enabled: false }]) {
    const result = await loadProjectWorkBlocks(absent, 12, Promise.resolve(record), { version });
    assert.deepEqual(result, { enabled: false, blocks: [], unassigned_count: 0, unavailable: true,
      compatibility_message: WORK_BLOCKS_COMPATIBILITY_MESSAGE });
    assert.match(result.compatibility_message, /旧版本/);
    assert.match(result.compatibility_message, /重启前后端/);
  }
}

function actionable(error) {
  assert.equal(error.status, 404);
  assert.equal(error.code, "work_blocks_api_unavailable");
  assert.equal(error.cause, missingInterface);
  assert.match(error.message, /后端任务块接口不可用/);
  assert.match(error.message, /重启前后端/);
  assert.doesNotMatch(error.message, /数据.*移除/);
  return true;
}

for (const version of ["0.21.0", "0.21.1", "0.22.0", "1.0.0", undefined, null, "", "unknown", "0.20", "0.20.2-dev", "00.20.2", "0.20.-1", 0.2]) {
  await assert.rejects(loadProjectWorkBlocks(absent, 12, project, { version }), actionable);
}
await assert.rejects(loadProjectWorkBlocks(absent, 12, project, undefined), actionable);
for (const enabled of [true, 1, "1", "false"]) {
  await assert.rejects(loadProjectWorkBlocks(absent, 12, { ...project, work_blocks_enabled: enabled }, health), actionable);
}
for (const unconfirmed of [undefined, null, {}, { id: 13 }, { id: "12" }]) {
  await assert.rejects(loadProjectWorkBlocks(absent, 12, unconfirmed, health), actionable);
}

for (const status of [401, 403, 409, 500, 503]) {
  const error = failure(status);
  await assert.rejects(loadProjectWorkBlocks(async () => { throw error; }, 12, project, health), actual => actual === error);
}

// A confirmed missing project takes precedence over a missing optional endpoint.
for (const status of [404, 401, 403, 500]) {
  const error = failure(status);
  await assert.rejects(loadProjectWorkBlocks(absent, 12, Promise.reject(error), health), actual => actual === error);
}

// The project may reject well before the task-block request finishes. Its failure
// must be observed immediately, while the caller still receives that exact error.
let finishRequest;
const delayed = new Promise(resolve => { finishRequest = resolve; });
const projectError = failure(404);
const pending = loadProjectWorkBlocks(async () => { await delayed; throw missingInterface; }, 12, Promise.reject(projectError), health);
const checked = assert.rejects(pending, error => error === projectError);
await new Promise(resolve => setImmediate(resolve));
finishRequest();
await checked;

const live = { enabled: true, blocks: [{ id: 3, owner_user_id: 7 }], unassigned_count: 5 };
for (const version of ["0.20.2", "0.21.1", undefined]) {
  const loaded = await loadProjectWorkBlocks(async path => {
    assert.equal(path, "/api/projects/12/work-blocks"); return live;
  }, 12, Promise.resolve(project), { version });
  assert.equal(loaded, live, "Successful ownership data is returned unchanged regardless of backend version");
}
await assert.rejects(loadProjectWorkBlocks(async () => live, 12, Promise.reject(projectError), health), error => error === projectError);

console.log("PASS legacy project compatibility; current/unknown versions and ownership fail closed; actual project/auth/server errors preserved; concurrent project rejection handled; current API data unchanged");
