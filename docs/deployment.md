# 部署训练好的模型

训练完成后，在训练详情点击“下载推理部署包”。ZIP 包包含已校验的 model.pt、标签/任务/尺寸清单、固定版本依赖、Dockerfile、Compose、Linux/Windows 启动脚本和 Python 调用示例。

在目标电脑或 GPU 服务器解压，执行：

```bash
bash start.sh          # CPU
bash start.sh --gpu    # NVIDIA GPU
```

Windows 使用 `powershell -ExecutionPolicy Bypass -File .\start.ps1`，GPU 添加 `-Gpu`。

首次会构建推理环境并生成 `.env` 中的部署密钥。访问 http://127.0.0.1:8080，填写部署密钥、上传图片，查看预测坐标与 JSON。调用接口为 `POST /predict`，上传 multipart `file` 并提供 `Authorization: Bearer <key>`；运行示例见包内 predict.py。

模型加载前校验 SHA-256，运行时用与训练相同的 Ultralytics 版本处理预处理和后处理。检测、分割、分类和关键点使用同一个结果协议。不同任务的精度和耗时需要用自己的数据评估。

原有 ONNX、TorchScript、RKNN、TensorRT 转换继续保留。TensorRT 引擎的硬件与版本兼容范围有限，建议在实际目标环境构建与测试：[官方说明](https://docs.nvidia.com/deeplearning/tensorrt/latest/getting-started/support-matrix.html)。本次自动生成的可运行服务使用 .pt；设备端 RKNN 仍需匹配芯片和后处理。

部署包是独立服务，会占用自己的显存。和平台在同一张 GPU 上运行时，需要自行停止推理服务后再训练；本版本没有自动把独立服务纳入平台任务队列。
