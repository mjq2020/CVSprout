# 第三方软件与模型

本项目原创代码采用 [Apache License 2.0](LICENSE)，允许在遵守该协议的前提下免费商用、修改和分发。软件依赖、模型权重和用户数据分别遵守各自条款，Apache-2.0 不会改变第三方许可证。

**Ultralytics 的许可需单独评估。** 当前训练和 YOLO 推理依赖 Ultralytics，其官方提供 AGPL-3.0 与 Enterprise 两种授权路径。免费使用 AGPL-3.0 路径需要履行相应的源码公开等义务；需要闭源商用的用户应核对官方 Enterprise 条款，或替换相关组件。项目原创代码采用 Apache-2.0，不表示整个依赖栈或导出的 YOLO 模型都可无条件闭源商用。

本文件记录主要依赖和模型的来源，不替代各项目的完整许可条款。

| 组件 | 来源与许可入口 |
|---|---|
| Ultralytics（训练、推理、转换及 YOLO 权重） | https://www.ultralytics.com/license |
| PyTorch / torchvision | https://github.com/pytorch/pytorch/blob/main/LICENSE |
| Transformers | https://github.com/huggingface/transformers/blob/main/LICENSE |
| Matplotlib / 随包 DejaVu 字体（训练图表） | https://matplotlib.org/stable/project/license.html；字体许可随 Matplotlib 的 fonts/ttf 目录分发 |
| Grounding DINO Tiny | https://huggingface.co/IDEA-Research/grounding-dino-tiny |
| SAM 2.1 Hiera Tiny | https://huggingface.co/facebook/sam2.1-hiera-tiny |
| SAM3（可选） | 使用者提供的源码和权重所附许可证 |
| TensorRT（可选） | https://developer.nvidia.com/tensorrt |
| RKNN Toolkit2（可选） | https://github.com/airockchip/rknn-toolkit2 |

model-catalog/models.json 固定受支持下载条目的来源、版本、文件大小和 SHA-256。默认镜像不打包业务模型权重。管理员导入自己的 checkpoint 时应确认来源及分发权限；导出的部署包会包含该训练权重。
