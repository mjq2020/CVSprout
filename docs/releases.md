# 发布开源发行版

项目原创代码采用 Apache-2.0。发行时保留 LICENSE、NOTICE 和 THIRD_PARTY_NOTICES.md，并核对各组件和模型的来源及再分发条件。实际数据、.env、模型权重、会话和下载缓存不进入源码仓库或安装包；用户主动导出的推理部署包包含自己的训练权重。

GitHub Actions：

- ci.yml：单元/接口回归、CPU 真实训练与双进程部署验收、JavaScript 检查、Compose 检查、轻量 Web 镜像构建。
- release.yml：推送 v 开头的版本标签后，构建 web、worker-cpu、worker-gpu 镜像，推送到 `ghcr.io/<owner>/<repository>/...`，生成安装 ZIP/TAR 与 SHA256SUMS，挂到 GitHub Release。

首次发布后确认 GHCR 包可被未登录用户拉取。工作流存在不代表已发布镜像。

仅打包本地安装器：

```bash
python scripts/package_release.py --image-prefix ghcr.io/mjq2020/cvsprout --version v0.20.0
```

安装包会注入正确镜像前缀和版本。源码开发使用 local 标签并构建；安装包使用发行标签并拉取镜像。镜像与安装包应一起发布，避免版本不匹配。

发布前在干净 Linux CPU、Linux NVIDIA、Windows WSL2 环境执行首次安装、启动、下载模型、训练、部署、重启和升级验证。当前测试覆盖范围见 verification.md，不把未实测平台标为验证通过。
