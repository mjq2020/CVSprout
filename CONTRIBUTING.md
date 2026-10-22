# 参与开发

先阅读 [架构说明](docs/architecture.md)。Web、计算服务和独立推理服务保留在一个仓库中；模型权重按需下载，不随源码提交。

## 本地环境

使用 Python 3.12。CPU 开发环境不需要显卡：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements/worker-cpu.txt
cp .env.example .env
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

在另一个终端运行 `.venv/bin/python -m app.setup`，取得首次管理员初始化口令。已有 `.env` 时保留自己的配置。NVIDIA 环境可使用 `requirements/worker-gpu.txt`，容器验收使用 `./start.sh --gpu`。

## 验证改动

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/smoke_pipeline.py
.venv/bin/python scripts/smoke_training_initialization.py
.venv/bin/python scripts/check_training_catalog.py
.venv/bin/python scripts/smoke_training_models.py
.venv/bin/python scripts/smoke_open_source.py --device cpu
for file in static/*.js; do node --check "$file"; done
for file in scripts/check_*.mjs; do node "$file"; done
```

冒烟脚本会在临时目录中生成合成数据、训练小模型并验证部署。GPU 相关改动还需选择空闲显卡运行 `CUDA_VISIBLE_DEVICES=0 .venv/bin/python scripts/smoke_open_source.py --device cuda:0`。没有相应硬件时，在提交说明中列出未验证的部分。

初始化冒烟默认检查从零训练及上传权重；加 `--download-official` 可在隔离目录实际下载固定官方权重并训练，需要可用网络，可配置 `MDP_DOWNLOAD_PROXY`。默认 CI 不联网下载模型。

模型目录检查解析全部内置架构并核对任务及规模；可加 `--release-metadata /path/to/release.json` 对照官方 GitHub 发布 API 的响应文件。多模型冒烟默认验证四个任务的新增 s/m 型号；用 `--models rtdetr-l.pt --exports onnx torchscript` 可验证指定模型的训练、转换、下载及 RT-DETR 的运行时数值一致性。加 `--pretrained` 会实际下载指定模型并从其权重训练。

保持 Web 无 PyTorch 的依赖边界；修改 GPU 调度时验证训练、标注和取消任务的互斥与恢复。数据库结构变动应提供迁移，并验证已有记录可以读取。新增模型下载条目应固定版本、文件大小、SHA-256 和许可来源。

提交说明写清问题、修改后的行为和验证结果。测试使用临时存储，提交中不包含 `.env`、账号密钥、用户图片、数据库或模型权重。问题报告可提供操作步骤、系统/GPU 信息和去除凭证后的错误日志。

原创代码贡献按项目的 Apache-2.0 许可证提供。引入第三方代码、字体或模型时同时保留其许可，并更新 THIRD_PARTY_NOTICES.md。
