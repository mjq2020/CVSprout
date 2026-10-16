# CVSprout

**Annotate, train, and deploy computer vision models on your own GPU.**

用自己的 GPU 完成视觉模型工作流：导入图片 → AI 标注与人工审核 → 数据版本 → 训练 → 模型转换与推理部署。

支持目标检测、实例分割、图片分类和关键点。适合个人电脑、单台 GPU 服务器和小团队协作。

原创代码采用 [Apache-2.0](LICENSE)，允许免费商用。Ultralytics 等依赖和模型有各自的许可要求，涉及闭源商用时请先阅读 [第三方许可说明](THIRD_PARTY_NOTICES.md)。

## 快速启动

准备 Docker。NVIDIA GPU 用户还需可用的驱动和容器 GPU 支持：

| 环境 | 安装要求 |
|---|---|
| Linux x86-64 | Docker Engine、Compose；GPU 模式另需 NVIDIA Container Toolkit |
| Windows x86-64 | Docker Desktop，启用 WSL2；GPU 模式使用支持 WSL2 的 NVIDIA 驱动 |
| CPU | 可人工标注和小规模训练；大型模型建议使用 GPU |
| macOS / AMD GPU | 本版本未验证本机加速；可用浏览器访问 GPU 服务器 |

当前可以克隆源码启动；正式发行版发布后，也可以下载安装包：

```bash
git clone https://github.com/mjq2020/CVSprout.git
cd CVSprout
```

在项目目录执行：

```bash
# Linux / WSL：自动检测 NVIDIA；没有 GPU 时使用 CPU
./start.sh

# 显式选择模式或检查环境
./start.sh --gpu
./start.sh --cpu
./start.sh --gpu --check
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
# 强制 GPU / CPU：添加 -Gpu / -Cpu；仅检查环境：添加 -Check
```

打开启动输出中的地址，默认 http://127.0.0.1:8000。复制终端显示的初始化口令，在网页创建管理员。

正式发行包拉取预构建镜像。源码目录的 `MDP_VERSION=local` 会自动构建镜像，第一次启动需要下载依赖；**本地构建完成不代表镜像已经发布到 GHCR**。

## 第一次使用

1. 登录后进入“模型服务”，点击“下载基础模型”。选择与你项目类型对应的 YOLO26 Nano。
2. 创建项目、定义类别，上传图片。可使用 AI 预标注，再人工修正；也可直接手工标注。
3. 审核数据，创建数据版本并划分训练集、验证集。
4. 创建训练，选择模型和计算设备。第一次建议少量图片、1–3 轮，先验证流程。
5. 训练完成后下载模型，或点击“下载推理部署包”，在目标机器启动预测接口。

模型从固定来源按需下载，校验 SHA-256，支持中断后续传。已有自己的 Ultralytics 权重，可由管理员点击“导入训练权重”导入 `.pt` 文件；不会覆盖同名模型。

单卡默认串行使用 GPU，标注等待正在执行的训练；预览在 GPU 忙碌时给出提示。GPU 标注完成后释放缓存模型，避免空闲模型占用训练显存。多卡通过 `.env` 的 `MDP_GPU_DEVICES` 指定可见设备。

## 数据与日常管理

数据和模型分别保存在 Compose 持久卷 `visionforge-data`、`visionforge-models` 中，普通重启和镜像升级会保留。**`docker compose down -v` 会删除数据卷。**

```bash
./start.sh --status      # 服务状态、未完成初始化时的口令
./start.sh --stop        # 停止并保留数据
# Windows 使用 .\start.ps1 -Status / -Stop
```

升级前备份数据卷和模型卷。首次使用新版的已有安装请先阅读 [迁移与备份](docs/operations.md)，特别是旧版机器专属的模型路径。

## 文档

- [安装、GPU 检查与故障排查](docs/installation.md)
- [远程服务器使用方式](docs/remote-server.md)
- [模型推理部署](docs/deployment.md)
- [详细业务使用手册](docs/user-guide.md)
- [架构与开发](docs/architecture.md)
- [参与开发](CONTRIBUTING.md)
- [迁移、备份和升级](docs/operations.md)
- [发布发行包](docs/releases.md)
- [第三方组件与模型许可](THIRD_PARTY_NOTICES.md)

## 开发与测试

以下命令在源码目录执行；发行安装包仅包含启动配置和文档。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements/worker-cpu.txt
# NVIDIA 开发环境改用 requirements/worker-gpu.txt
cp .env.example .env
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
# 另一个终端查看首次设置口令
.venv/bin/python -m app.setup
```

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/smoke_pipeline.py
.venv/bin/python scripts/smoke_open_source.py --device cpu
# 单卡 GPU 实测，可先设置 CUDA_VISIBLE_DEVICES
.venv/bin/python scripts/smoke_open_source.py --device cuda:0
```

真实冒烟使用临时目录、合成图片和随机初始权重，验证执行链路，不能代表模型精度。详细验证范围见 [测试记录](docs/verification.md)。
