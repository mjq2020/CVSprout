# CVSprout

**Annotate, train, and deploy computer vision models on your own GPU.**

用自己的 GPU 完成视觉模型工作流：导入图片 → AI 标注与人工审核 → 数据版本 → 训练 → 模型转换与推理部署。

支持目标检测、实例分割、图片分类和关键点。适合个人电脑、单台 GPU 服务器和小团队协作。

多人协作可启用[任务块领取](docs/user-guide.md#任务块协作)：管理员将图片按数量分块，成员独占领取，完成标注后交给审核员审核；全部通过的块保留完成记录，避免重复分配。

左侧导航集中显示当前项目、工作流程、标注服务状态和账号入口。底部“主题”可切换浅色、深色或跟随系统，浏览器会记住选择；手机点击顶部菜单打开导航。详见[界面与主题](docs/user-guide.md#界面与主题)。

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
# Linux / WSL：首次自动检测 NVIDIA；之后沿用上次成功启动的模式
./start.sh
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

首次默认使用 http://127.0.0.1:8000；端口被占用时自动分配可用端口。打开启动输出中的实际地址，复制初始化口令，在网页创建管理员。启动成功后，实际端口会保存到 `.env` 的 `MDP_PORT`，下次沿用；`./start.sh --status` 也会显示当前访问地址。

正式发行包拉取预构建镜像。源码目录的 `MDP_VERSION=local` 会自动构建镜像，第一次启动需要下载依赖；**本地构建完成不代表镜像已经发布到 GHCR**。

## 启动参数

下列命令在服务器或本机的项目目录执行。Linux / WSL 使用 `./start.sh`；PowerShell 使用 `.\start.ps1`。若 Windows 执行策略阻止脚本，将示例中的 `.\start.ps1` 替换为 `powershell -ExecutionPolicy Bypass -File .\start.ps1`，其后参数保持不变。

| Linux / WSL 参数 | PowerShell 参数 | 作用 |
|---|---|---|
| 不带参数 | 不带参数 | 启动 Web 和 worker。首次自动检测 NVIDIA，没有可用的 NVIDIA 检测结果时选择 CPU；后续沿用上次成功启动的模式 |
| `--cpu` | `-Cpu` | 显式使用 CPU；成功启动后记住此模式 |
| `--gpu` | `-Gpu` | 显式使用 NVIDIA GPU；成功启动后记住此模式。GPU 环境检查失败会报错 |
| `--port 8080` | `-Port 8080` | 指定宿主机端口，范围为 0–65535；0 表示让 Docker 分配可用端口 |
| `--strict-port` | `-StrictPort` | 端口被占用时直接报错，禁止自动换端口。需要固定端口时与非零端口一起使用 |
| `--check` | `-Check` | 仅检查运行环境，不启动 Web 和常驻 worker；可能构建或拉取镜像，并运行临时诊断容器 |
| `--status` | `-Status` | 查看容器状态；服务运行时显示访问地址和尚未完成初始化时的口令 |
| `--stop` | `-Stop` | 停止 Web 和 worker，保留容器、账号、项目、模型和数据卷 |
| `--build` | `-Build` | 强制从源码构建镜像后继续启动或检查，需要完整源码；不代表清除构建缓存 |
| `--help` | 无对应参数；使用 `Get-Help .\start.ps1` | 显示脚本用法；PowerShell 使用内置帮助命令 |

`--cpu` / `--gpu` 选择一种；`--check` / `--status` / `--stop` 每次选择一种操作。不指定操作时就是启动。端口、模式和严格模式可以组合；Bash 也支持 `--port=8080`。

常用组合，按需要选择一条执行：

| 场景 | Linux / WSL | PowerShell |
|---|---|---|
| 使用 GPU，在 8080 端口启动，占用时自动切换 | `./start.sh --gpu --port 8080` | `.\start.ps1 -Gpu -Port 8080` |
| 使用 CPU，在固定 8080 端口启动 | `./start.sh --cpu --port 8080 --strict-port` | `.\start.ps1 -Cpu -Port 8080 -StrictPort` |
| 自动分配端口 | `./start.sh --port 0` | `.\start.ps1 -Port 0` |
| 检查 GPU 是否可用 | `./start.sh --gpu --check` | `.\start.ps1 -Gpu -Check` |
| 修改源码后重新构建并启动 | `./start.sh --build` | `.\start.ps1 -Build` |

端口优先级：命令行参数 > 当前终端的 `MDP_PORT` 环境变量 > `.env` 中的 `MDP_PORT` > 默认 8000。启动成功后实际端口写回 `.env`；若终端仍设置了 `MDP_PORT`，下次启动仍优先使用终端的值。自动切换只处理端口占用，其他错误需要按输出排查。

## 停止与重新启动

启动完成后服务在后台运行，关闭终端不会停止服务。需要停止时执行：

```bash
./start.sh --stop
```

Windows PowerShell 对应命令：

```powershell
.\start.ps1 -Stop
```

停止会中断正在执行的训练或推理，建议在任务完成后操作。停止后数据仍保留，重新执行 `./start.sh` / `.\start.ps1` 即可启动；需要重启时，先执行停止命令，成功后再执行启动命令。再次启动会进行正常的镜像和环境检查，不代表自动续训被中断的任务。

查看正在运行的服务及访问地址：`./start.sh --status` / `.\start.ps1 -Status`。停止和状态查询不需要再次指定端口，脚本会读取项目配置。

## 远程访问与常用配置

首次执行启动脚本会从 `.env.example` 创建 `.env`。以后直接修改 `.env` 中对应的配置项，保留自己的其他设置；修改后再次执行启动命令生效。

| 配置项 | 默认值 | 用途 |
|---|---|---|
| `MDP_BIND_ADDRESS` | `127.0.0.1` | 默认仅本机访问；局域网 / VPN 访问可改成 `0.0.0.0` |
| `MDP_PORT` | `8000` | 宿主机端口；启动成功后保存实际使用的端口 |
| `MDP_GPU_DEVICES` | `0` | GPU 模式下可见的物理 GPU；可填写逗号分隔的索引或 UUID |

如果要从局域网或 VPN 中的其他电脑访问，在服务器的 `.env` 中设置：

```dotenv
MDP_BIND_ADDRESS=0.0.0.0
MDP_PORT=8080
```

然后执行 `./start.sh --strict-port`（PowerShell：`.\start.ps1 -StrictPort`），在自己的电脑打开 `http://服务器IP:8080`。例如 `http://192.168.1.100:8080`；远程电脑应使用服务器的真实 IP，即使启动输出显示的是 `127.0.0.1`。服务器防火墙或云安全组需允许访问所选端口。端口被占用时可修改端口，或去掉严格模式并使用实际分配的新端口，同时更新相应网络规则。

公网服务器的个人访问可以通过 SSH 转发，保持服务仅监听 `127.0.0.1`；多人跨网络访问使用 HTTPS 反向代理。具体启动、转发和域名配置见 [远程服务器使用方式](docs/remote-server.md)。训练使用部署服务器的 GPU，浏览器所在电脑不需要 GPU。

## 第一次使用

1. 登录后进入“模型服务”，点击“下载基础模型”。选择与你项目类型对应的 YOLO26 Nano。
2. 创建项目、定义类别，上传图片。可使用 AI 预标注，再人工修正；也可直接手工标注。多人协作时，由管理员创建任务块，成员领取自己的块后开始标注。
3. 审核数据：单图按 `Y` 通过、`X` 打开退回窗口，退回原因可留空；成功后自动进入下一张待审核图片。块内审核保持块范围，处理完后返回任务块列表；项目审核处理完后返回数据页的待审核列表。然后创建数据版本并划分训练集、验证集。
4. 创建训练，选择模型和计算设备。第一次建议少量图片、1–3 轮，先验证流程。按钮不可点击时，查看旁边的具体原因；缺少权重可通过“准备训练模型”下载或导入，完成后返回训练页刷新。详见 [训练故障排查](docs/installation.md#开始新训练按钮不可点击)。
5. 训练完成后下载模型，或点击“下载推理部署包”，在目标机器启动预测接口。

模型从固定来源按需下载，校验 SHA-256，支持中断后续传。已有自己的 Ultralytics 权重，可由管理员点击“导入训练权重”导入 `.pt` 文件；不会覆盖同名模型。

管理员可在项目卡片点击“删除”，完整输入项目名称后永久删除项目及其数据版本、训练产物；有任务排队、执行或图片正在编辑时会阻止删除。需要保留项目时可使用“归档”。详见 [删除项目](docs/user-guide.md#删除项目)。

单卡默认串行使用 GPU，标注等待正在执行的训练；预览在 GPU 忙碌时给出提示。GPU 标注完成后释放缓存模型，避免空闲模型占用训练显存。多卡通过 `.env` 的 `MDP_GPU_DEVICES` 指定可见设备。

## 数据与日常管理

数据和模型分别保存在 Compose 持久卷 `visionforge-data`、`visionforge-models` 中，普通重启和镜像升级会保留。**`docker compose down -v` 会删除数据卷。**

升级前备份数据卷和模型卷。首次使用新版的已有安装请先阅读 [迁移与备份](docs/operations.md)，特别是旧版机器专属的模型路径。

## 升级后页面提示接口不可用

仅执行 `git pull` 会更新源码文件，已经运行的 Python 进程仍可能使用旧接口。新版页面请求任务块接口时，旧后端会返回 404；这类版本不一致不代表项目数据被删除。新版页面会明确提示更新服务；确认是未启用任务块的旧项目时，仍可继续原来的标注与审核流程。

源码通过 Docker 部署时，先等待正在运行的任务结束，停止服务并按上文备份，再更新并重新构建、启动 Web 和 worker：

```bash
./start.sh --stop
# 完成备份后继续
git pull
./start.sh --build
```

脚本沿用已保存的 CPU / GPU 模式和 `.env` 配置；原先需要命令行指定端口或严格模式时，仍带上相同参数，例如 `./start.sh --gpu --port 8080 --strict-port --build`。PowerShell 对应 `.\start.ps1 -Stop`、`git pull`、`.\start.ps1 -Build`，同样保留原来的 `-Cpu` / `-Gpu`、`-Port` 和 `-StrictPort` 设置。成功后刷新浏览器，必要时按 `Ctrl+Shift+R` 强制刷新。

直接使用 Python 启动时，在原启动终端按 `Ctrl+C` 停止服务，备份后执行 `git pull`，同步当前 CPU / GPU 模式所用的依赖文件，再使用原有环境变量、地址和端口重新启动。例如本机开发模式使用 `.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000`；另行启动的 worker 也需按原命令重启。由服务管理器托管时，重启对应的 Web 和 worker 服务。最后刷新浏览器，确保前后端来自同一次更新。

## 文档

- [模块与功能说明](docs/modules-and-features.md)：业务功能、角色权限、数据流和源码索引
- [生产部署与上线审查](docs/production.md)
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
