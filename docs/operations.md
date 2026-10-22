# 迁移、备份与升级

## 从旧版升级

先停止旧服务并备份，再在新目录验证。旧版默认读取维护者机器上的绝对模型路径；新版统一到 models/ 或 Compose 的 /models。

- 直接 Python 启动：将自己的权重放到 models/training，或在本机 .env 中显式配置 MDP_TRAINING_BASE_MODEL、MDP_TRAINING_SEGMENTATION_MODEL、MDP_TRAINING_MODEL_DIRS。
- SAM3：放到 models/sam3/sam3.pt，源码放到 models/sam3/source，或者显式配置对应路径。
- Compose：新安装默认项目名为 cvsprout。由 VisionForge 旧版升级时，在 .env 中保留 `COMPOSE_PROJECT_NAME=visionforge`（若以前使用自定义项目名，则继续使用该名称），这样仍会读取原数据卷。数据卷的内部键名保持兼容；把旧权重复制进模型卷。
- 旧版 worker/pipeline 拆分改为合并计算服务。停止并移除已确认属于旧部署的 pipeline 容器，避免同时启动两个版本的执行器；保留数据卷。
- 保留已有 .env 和账号。启动脚本保留已有配置和账号，启动成功后会将实际使用的端口写入 .env 的 `MDP_PORT`。

任务记录包含模型内容哈希和路径。历史任务如果仍引用旧路径，需恢复对应可读挂载后再重试；不要为了绕过校验而修改历史哈希。

## 备份

停止 Web 和 worker 后，同时备份数据卷及模型卷。通过 `docker volume ls` 查找实际卷名（新安装默认为 cvsprout_visionforge-data / cvsprout_visionforge-models，旧版前缀取决于原项目名）。备份应包含 SQLite、媒体、冻结数据版本、训练产物和模型文件。

不要在 SQLite 正在写入时只复制主数据库文件；默认 WAL 模式可能还有未合并的日志。可使用 SQLite 备份 API，或停止服务后复制整个卷。

普通 `docker compose stop` / `down` 保留卷；`down -v` 会删除卷，不用于升级。

## 升级

1. 停止服务，备份并确认备份可读取。
2. 下载新发行包，保留现有 .env、Compose 项目名和卷。把 .env 中镜像版本改为新发行版对应标签。
3. 运行启动脚本，等待健康检查；确认原项目、账号、模型、训练记录可见。进入已有数据版本项目的训练页，点击“刷新”，确认“开始新训练”可点击，并在“训练环境与可用模型”中确认所需任务权重可用。历史训练记录可见不代表基础权重已迁移；按钮禁用时见 [训练故障排查](installation.md#开始新训练按钮不可点击)。
4. 数据库会在启动时迁移；回滚时同时恢复匹配旧版本的完整数据备份。
