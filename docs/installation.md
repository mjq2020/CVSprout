# 安装与 GPU 检查

## 推荐路径

Linux：安装 Docker Engine 与 Compose；NVIDIA 用户安装匹配显卡的驱动，以及 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)。
Windows：安装 Docker Desktop，启用 WSL2 后端；参见 [Docker GPU 支持](https://docs.docker.com/desktop/features/gpu/)。容器带有模型运行环境，不需要用户自行组合 Python、PyTorch 和 CUDA Toolkit。

第一版镜像面向 Linux x86-64，GPU 环境固定 PyTorch 2.6.0 / CUDA 12.4。不能据此保证所有新旧显卡都支持；启动脚本会在容器内执行真实 CUDA 张量运算。已验证的具体设备见 verification.md。

下载发行包后执行 `./start.sh`（Linux/WSL）或 `powershell -ExecutionPolicy Bypass -File .\start.ps1`（Windows）。源码目录首次启动会构建镜像；有网络的发行包首次启动拉取镜像。

## 环境诊断

### 端口被占用

`address already in use` 表示所选宿主机端口正在使用。可执行 `./start.sh --port 8080`（PowerShell：`.\start.ps1 -Port 8080`）指定端口，也可以修改 `.env` 中的 `MDP_PORT`。优先级为命令行参数、当前终端的 MDP_PORT 环境变量、.env 配置、默认 8000。

默认启动遇到端口占用时，会让 Docker 在绑定时分配一个可用端口；端口号不一定是 8001。`--port 0` / `-Port 0` 可直接请求自动分配。原有占用端口的程序不会被关闭，绑定地址仍遵守 MDP_BIND_ADDRESS（默认只允许本机访问）。

成功后，实际端口写回 `.env`，其余配置保留。使用终端输出的 URL 访问，或运行 `./start.sh --status` / `.\start.ps1 -Status` 查看。失败的启动不会改写端口配置。自动切换只针对端口占用，镜像、GPU、网络或健康检查的其他错误仍会直接报告。

如果反向代理、SSH 转发或防火墙依赖固定端口，使用 `--strict-port` / `-StrictPort` 禁止自动切换，或同步更新相关转发配置。直接运行 `docker compose up` 时不会执行启动脚本的自动切换逻辑。

### 运行环境检查

`./start.sh --gpu --check` / `.\start.ps1 -Gpu -Check` 检查 Docker、可写数据目录、剩余磁盘、PyTorch、Ultralytics 和真实 GPU 运算。检查模式可能下载或构建镜像，但不会启动业务服务。

- Docker 不可用：启动 Docker 服务；Windows 检查 Docker Desktop 和 WSL2。
- `could not select device driver`：Linux 检查 NVIDIA Container Toolkit 的 Docker runtime 配置。
- `CUDA unavailable`：确认驱动和容器透传；不要把“nvidia-smi 能运行”视为训练环境已正常。
- GPU 架构不支持当前 PyTorch：需维护者发布匹配该架构的镜像，不应静默切换到 CPU。
- 显存不足：关闭其他 GPU 程序，减小 batch 或输入尺寸，或换用更小模型。
- 模型下载失败：检查 GitHub / Hugging Face 连通性，再点击下载续传；也可管理员导入已有权重。
- 权限错误：使用默认命名卷。自行改用宿主机目录时，容器 UID/GID 为 10001。

GPU 模式默认使用物理 GPU 0。修改 `.env` 的 `MDP_GPU_DEVICES=1`，或填写逗号分隔的多个索引/UUID。容器中重新从 `cuda:0` 编号。

管理员创建后，去“模型服务”下载基础模型。无需下载全部模型；SAM3 为可选项，需要自行准备其权重和源码。

如需下载代理，在 .env 中设置 MDP_DOWNLOAD_PROXY=http://可从容器访问的代理地址:端口。下载不继承宿主机进程的代理变量；支持 HTTP(S) 代理。

源码镜像构建的网络代理由 Docker 配置，和模型下载代理分别设置。若构建依赖时网络受限，可先执行 `docker compose build --build-arg HTTP_PROXY --build-arg HTTPS_PROXY`，使用当前终端中的代理变量；GPU 模式加上 `-f compose.yaml -f compose.gpu.yaml`。

训练绘图默认使用 Matplotlib 自带字体，避免临时联网下载字体阻塞训练。需要完整中文图表时，在计算环境安装 Noto CJK，或通过 `MDP_PLOT_FONT` 指定本地字体；没有中文字体时，模型标签和预测 JSON 仍保留中文，训练图片中的中文可能显示为缺字。
