# 生产部署与上线审查

审查日期：2026-10-21。部署范围按一台 GPU 服务器、可信团队成员、一个 Web 和一个 worker 评估。本次没有修改现有业务数据、启动配置或生产服务。

## 上线结论与边界

本轮修复后已验证单机训练、标注和推理链路，可用于受控团队的上线验收。仍需完成目标环境的 HTTPS、备份恢复和监控验证；本地测试不能替代这些验收。

当前所有成员共享工作区项目，角色用于区分操作权限，不提供按租户隔离的数据空间。不要把同一部署作为互不信任客户的共享平台。

PyTorch 仍固定为 2.6.0 / CUDA 12.4，存在公开安全公告，参见 [2.6.0 的 PyPI 公告数据](https://pypi.org/pypi/torch/2.6.0/json)与 [PyTorch 安全公告](https://github.com/pytorch/pytorch/security/advisories)。此次保留已验证的 GPU 组合，并明确记录为待处理项，不能将 Web 依赖扫描通过解释为整个计算环境没有漏洞。后续需选择兼容驱动的新版 PyTorch / torchvision / CUDA 组合，重新验证训练、导出和本地模型。可选 RKNN 环境也有独立的旧版依赖，本次未升级或验收。

模型属于可执行输入：管理员仅导入来源可信的 `.pt`，校验和只能验证文件一致性，不能证明模型安全。普通成员不能上传 checkpoint；不要给不可信用户管理员账号。处理不可信模型需要额外隔离的计算环境和独立凭证，当前工作进程不提供此隔离。

## 本轮修复

| 问题 | 修复及验证 |
|---|---|
| HTTP 框架、multipart、图片解析等依赖落后于安全修复 | 更新 FastAPI、Starlette、python-multipart、Pillow、certifi、urllib3；更新 Transformers、Hub、ONNX、setuptools，并在镜像中升级 pip |
| 超大上传先被解析/读入，再检查限额 | 上传解析前检查身份和权限；在 ASGI 接收阶段同时限制声明长度和实际字节数；分块超限返回 413 并关闭临时文件 |
| 推理服务忙碌时仍先解码图片 | 在读取和解码之前申请处理名额；图片解码移出事件循环；请求取消后仍保持名额直到计算结束 |
| COCO / YOLO 快速导出将整包保存在内存 | 改为磁盘临时 ZIP，传输完成或生成失败后清理；导出内容和删除冲突行为保持原有约定 |
| 后台任务退出时仍可能显示健康 | worker 检查数据库和后台任务；Web 新增 `/api/ready` 检查计算服务，Compose 使用就绪检查 |
| 特殊字符初始化口令或会话签名引发异常 | 拒绝无效凭证并返回认证错误，避免字符串摘要比较抛出 500 |
| 容器日志持续增长、子进程回收 | 设置本地日志轮转和容器 init |
| 打标签发布未等待测试结果 | 发布流程先运行可复用的完整 CI；增加 Web 依赖漏洞检查 |

依赖依据包括维护者的 [Starlette 公告](https://github.com/Kludex/starlette/security/advisories)、[python-multipart 公告](https://github.com/Kludex/python-multipart/security/advisories)、[Transformers 公告](https://github.com/huggingface/transformers/security/advisories)以及 PyPI 漏洞数据。安全状态随公告更新，应以部署当天扫描为准。

## 生产配置

保留现有 `.env`，修改对应项目；将示例域名替换为自己的域名。以下假设 HTTPS 反向代理运行在同一宿主机：

```dotenv
MDP_AUTH_MODE=accounts
MDP_BIND_ADDRESS=127.0.0.1
MDP_PORT=8080
MDP_COOKIE_SECURE=true
MDP_PUBLIC_ORIGIN=https://vision.example.com
MDP_GPU_DEVICES=0
MDP_MAX_REQUEST_MB=16
MDP_MAX_UPLOAD_MB=250
```

在项目目录运行 `./start.sh --gpu --port 8080 --strict-port`。浏览器通过 `https://vision.example.com` 访问。直接通过 HTTP 访问时，Secure Cookie 不会发送；仅在本机或可信内网 HTTP 调试时使用 `MDP_COOKIE_SECURE=false`。

反向代理负责证书、HTTPS、访问来源范围、请求速率和连接超时。上游为 `http://127.0.0.1:8080`，保留原始 `Host` 并设置 `X-Forwarded-Proto`、`X-Forwarded-For`。代理的请求大小限制可以小于应用限制；如需上传较大 checkpoint，请单独设置该路径的限制，避免普通接口获得同样大的预算。

使用 `MDP_TRUSTED_PROXIES` 指定 Web 容器实际看到的代理源 IP 或 CIDR，以便 Uvicorn 正确识别客户端 IP，保持登录限流按真实客户端生效。宿主机代理经过 Docker 网络后可能显示为网桥网关地址，需在目标环境核对。默认仅信任 `127.0.0.1`；不要在可直接访问的监听器上设置 `*`。参见 [Uvicorn 代理头配置](https://www.uvicorn.org/settings/#http)及 [Nginx 上游请求头设置](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_set_header)。

只开放反向代理需要的端口。内部 worker 的 8001 不映射到宿主机，不需要开放。

## 请求和资源限制

- 普通 API 请求体默认最多 16 MiB，由 `MDP_MAX_REQUEST_MB` 设置。
- 图片/ZIP 每个文件仍受 `MDP_MAX_UPLOAD_MB` 限制；整个上传批次默认最多 251 MiB，即 250 MiB 加 1 MiB 的 multipart 开销。大批次请拆分。
- checkpoint 导入仍仅限管理员，单文件最多 2 GiB，请求预算额外允许 1 MiB 的 multipart 开销。
- 独立推理服务的图片最多 20 MiB / 2500 万像素；整个请求最多 21 MiB。服务忙碌返回 429。
- 请求大小限制不能替代代理的连接数、速率、慢连接和总并发限制。需要按服务器内存、磁盘和预期并发配置并进行压测。
- Docker 每个服务使用 `local` 日志驱动，配置轮转为每文件 10 MB、最多 5 个文件；日志是有限留存，审计留存应另行归档。数据库任务日志、媒体、权重和临时导出仍会消耗磁盘，应监控剩余空间。

## 监控与故障处理

`GET /api/health` 用于进程存活和前端初始化；`GET /api/ready` 用于服务就绪。数据库不可用、后台任务退出或远端 worker 不健康时，就绪接口返回 503。

```bash
curl --fail http://127.0.0.1:8080/api/ready
docker compose ps
docker compose logs --tail 100 web worker
```

Compose 的 `restart: unless-stopped` 处理进程退出，不会仅因 `unhealthy` 自动重启。监控应对连续 503、容器异常、队列长时间无进展、GPU 错误和磁盘不足告警；排查后使用已确认的运行模式重新启动。不要通过频繁自动重启掩盖数据或驱动问题。

## 更新、备份和恢复

1. 等待关键任务结束，执行 `./start.sh --stop`。
2. 同时备份 `.env`、数据卷和模型卷，保存当前提交或镜像版本。备份中含账号和凭证，应限制读取权限。
3. 在独立 Compose 项目与独立卷恢复备份，验证数据库可打开、登录、项目媒体和模型读取，再执行一次小规模训练。禁止覆盖业务卷来做恢复演练。
4. 拉取已验证的代码或固定版本镜像；源码模式重新构建镜像，再使用原端口和模式启动。
5. 检查 `/api/ready`、原有账号/项目/模型和关键操作。若需要回滚，恢复匹配旧版本的完整备份，不能只回退代码而保留不兼容数据库。

`docker compose down -v` 会删除卷，不用于普通停止或升级。卷名、旧项目名兼容和备份范围见 [迁移与备份](operations.md)。本轮测试没有针对你的业务备份执行恢复演练，也没有配置你的域名、证书、防火墙或告警接收端。

## 验证范围

- 全部 256 项 Python 回归测试通过，包含 13 项新增测试；原有 11 项 Node 交互检查和全部前端语法检查通过。
- Web 依赖经 `pip-audit -r requirements/web.txt` 扫描，2026-10-21 未发现已知漏洞。计算环境另检查了带 `+cu124` 的 PyTorch 对应公共版本公告，保留上述待处理项。
- CPU 和 RTX 4090 上实际完成初始化、模型预览、队列训练、部署包导出和独立鉴权推理；测试为合成数据，不代表业务模型精度。
- 分类与关键点真实训练及 ONNX / TorchScript 转换通过；本地 Grounding DINO、SAM2 在新 Transformers 依赖下完成 CPU 推理。
- 独立 Docker CPU 项目完成镜像构建、启动、健康检查、worker 停止后 503、恢复后 200；验证容器 init、日志轮转和 pip 版本。全新 CPU worker 镜像内也完成了训练到独立部署推理的流程。GPU 运行在隔离进程中验证，未重新构建本轮 GPU 镜像。
- PowerShell 测试运行于 Linux；Windows 实机、RKNN 板端、TensorRT、新 GPU 架构、长时间压力和大规模数据容量不在本轮实测范围内。

完整历史验收记录见 [verification.md](verification.md)。
