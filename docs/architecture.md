# 架构与开发

完整的业务模块、功能边界、角色权限及后端/前端源码索引见[模块与功能说明](modules-and-features.md)。本页重点说明进程分工与开发约定。

代码保持单仓库，先以模块和进程划分职责，兼容现有业务接口。

项目显示名和发行包名统一为 CVSprout。为兼容旧版，保留 MDP_* 配置、独立推理服务的 VISIONFORGE_* 配置、内部鉴权头、持久卷键名以及既有导出数据字段。已有部署改名时的卷迁移说明见 operations.md。

| 部分 | 入口 | 职责 |
|---|---|---|
| Web | app/main.py | 页面、账号、数据、任务提交与读取 |
| 计算服务 | app/worker_server.py | 内部鉴权 RPC、标注 worker、训练/转换 worker |
| Web 计算客户端 | app/worker_client.py | 模型目录、环境探测、GPU 资源、预览代理 |
| 本地模型 | app/providers/ | 不同模型的加载和标注推理 |
| GPU 互斥 | app/providers/execution.py | 物理 GPU 标识、推理资源锁、缓存卸载 |
| 首次设置 | app/setup.py | 带一次性口令的原子管理员初始化 |
| 模型管理 | app/model_downloads.py | 固定目录下载、续传、校验、管理员导入 |
| 独立推理服务 | app/runtime/server.py | 部署包的图片预测 API 和网页测试 |
| 部署导出 | app/deployment.py | 权重、标签、配置、运行代码与脚本打包 |
| 训练绘图字体 | app/plot_fonts.py | 准备本地字体，避免隐式联网下载阻塞训练 |

Web 安装 requirements/web.txt，不安装 torch / ultralytics / transformers。本地模型探测和预览经内部 HTTP 转给 worker，远程模型预览由 Web 直接请求已配置的服务。CPU worker 与 GPU worker 分别使用独立依赖文件；RKNN/TensorRT 保留可选环境，不作为首次启动条件。

SQLite 及媒体、数据版本、训练产物位于同一台主机的持久卷。内部凭证由共享数据卷原子创建，仅由同 UID 的服务读取。worker 的 RPC 使用固定操作白名单，预测只允许读取媒体目录中的文件。

标注推理和训练/转换使用同一组跨进程文件锁。GPU 锁优先使用物理 GPU UUID，避免容器可见索引改变导致锁失效。标注推理默认在返回结果前清除模型缓存并释放显存；批量标注遇到占用会续租等待，交互预览会返回可重试提示。训练已有的租约、代次校验和取消行为保持。

独立部署包是导出到其他应用的服务，不纳入平台内的资源调度。共享一张卡时，应停止该服务后训练，或分配另一张 GPU。

源码启动仍支持 Web 内嵌 worker，便于调试。推荐发行方式是 Compose 的两个服务。现有大型业务文件保留兼容接口，后续可按接口/业务逻辑/执行器继续拆分。

运行 `python -m unittest discover -s tests -v`；真实两进程验收见 scripts/smoke_open_source.py。该脚本支持 `--web-python /path/to/lightweight/python`，用无 PyTorch 的环境验证 Web 的依赖边界。
