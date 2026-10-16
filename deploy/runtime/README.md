# 运行你的模型

安装 Docker；GPU 模式另需兼容的 NVIDIA 驱动和容器 GPU 支持。

Linux：`bash start.sh`；NVIDIA GPU：`bash start.sh --gpu`。
Windows：`powershell -ExecutionPolicy Bypass -File .\start.ps1`；GPU 添加 `-Gpu`（Docker Desktop WSL2）。

首次启动构建固定版本的运行环境。访问 http://127.0.0.1:8080，复制本目录 `.env` 中的部署密钥并上传图片测试。
`GET /health` 用于健康检查；`POST /predict` 接收 multipart `file`，必须发送 `Authorization: Bearer <部署密钥>`。
Python 示例见 `predict.py`（需要 httpx）。输入预处理和输出后处理由固定版本的 Ultralytics 执行，结果坐标为原图像素。

`model/manifest.json` 包含任务、图片尺寸、标签、训练记录和模型 SHA-256，启动时验证模型内容。

在 GPU 服务器上执行相同命令，再通过 SSH 转发访问：`ssh -L 8080:127.0.0.1:8080 user@server`。
持续推理服务有自己的显存占用，建议使用另一张卡，或在训练前停止该服务。

停止：`docker compose stop`。GPU 部署管理时使用 `docker compose -f compose.yaml -f compose.gpu.yaml ...`。
同时部署多个模型时，为每个目录使用独立的 Compose 项目名（`COMPOSE_PROJECT_NAME`）和 `VISIONFORGE_PORT`。

此部署包包含用户训练的权重；Ultralytics 软件与模型的使用条件见 https://www.ultralytics.com/license 。
