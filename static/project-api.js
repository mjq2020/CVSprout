export const WORK_BLOCKS_COMPATIBILITY_MESSAGE = "后端仍为旧版本，暂不支持任务块功能。请更新并重新构建、重启前后端服务，再刷新页面。现有项目可继续使用原来的标注与审核流程。";

// Unknown/development versions do not establish that task ownership is absent.
function predatesWorkBlocks(version) {
  if (typeof version !== "string") return false;
  const match = version.match(/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/);
  if (!match) return false;
  const parts = match.slice(1).map(Number);
  if (!parts.every(Number.isSafeInteger)) return false;
  return parts[0] === 0 && parts[1] < 21;
}

export async function loadProjectWorkBlocks(api, projectId, projectPromiseOrObject, health) {
  // Attach both handlers immediately: the project can fail while the optional
  // endpoint is still pending. A real missing/inaccessible project never falls back.
  const projectResult = Promise.resolve(projectPromiseOrObject).then(
    project => ({ project }), error => ({ failed: true, error }),
  );
  let blocks, requestError, failed = false;
  try {
    blocks = await api(`/api/projects/${projectId}/work-blocks`);
  } catch (error) {
    failed = true;
    requestError = error;
  }
  const result = await projectResult;
  if (result.failed) throw result.error;
  if (!failed) return blocks;
  if (requestError?.status !== 404) throw requestError;

  const project = result.project;
  const confirmedProject = project && typeof project === "object"
    && Number.isSafeInteger(project.id) && project.id > 0 && project.id === projectId;
  if (confirmedProject && !project.work_blocks_enabled && predatesWorkBlocks(health?.version)) {
    return { enabled: false, blocks: [], unassigned_count: 0, unavailable: true,
      compatibility_message: WORK_BLOCKS_COMPATIBILITY_MESSAGE };
  }
  const error = new Error("后端任务块接口不可用，可能尚未更新或前后端版本不一致。请管理员更新并重新构建、重启前后端服务，再刷新页面。", { cause: requestError });
  error.status = 404;
  error.code = "work_blocks_api_unavailable";
  throw error;
}
