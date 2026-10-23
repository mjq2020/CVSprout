# CVSprout 模块与功能说明

本文描述 **0.25.2** 已实现的模块、主要功能、数据流和源码入口，供首次使用者了解产品，也供开发者定位代码。具体操作步骤见[用户指南](user-guide.md)，安装命令见 [README](../README.md)。功能、配置或权限发生变化时，应同步维护本文。

## 阅读导航

- [工作流与基本概念](#工作流与基本概念)
- [业务模块与功能](#业务模块与功能)
- [角色与权限](#角色与权限)
- [运行架构与数据保存](#运行架构与数据保存)
- [后端源码索引](#后端源码索引)
- [前端源码索引](#前端源码索引)
- [启动、运维与开发工具](#启动运维与开发工具)
- [主要接口入口](#主要接口入口)
- [当前支持范围](#当前支持范围)

## 工作流与基本概念

典型流程：**创建项目 → 导入图片 → 可选 AI 初标 → 可选分块并领取 → 人工标注 → 提交审核 → 审核通过 → 生成数据版本 → 训练 → 下载权重、转换模型或导出推理部署包**。

个人使用可以直接标注；多人协作可以启用任务块。AI 初标、任务块和模型转换都是按需要使用的环节。

| 概念 | 含义 | 与后续步骤的关系 |
|---|---|---|
| 项目 | 一组图片、类别定义和任务类型的工作空间 | 组织标注、审核、数据版本和训练记录 |
| 类别 | 要识别的目标或图片类别，可配置颜色、快捷键和属性 | 标注、模型预测、导出和训练使用一致的类别含义 |
| 图片 | 导入后的原始媒体和尺寸、哈希、审核状态等记录 | 同一图片可以有多个对象标注；分类任务表示整张图的类别 |
| 标注 | 矩形框、多边形、整图分类或关键点对象 | 记录来源、类别、属性及人工修改情况 |
| 任务块 | 一批互不重叠的图片及其负责人、进度和历史 | 控制谁能修改这些图片，不复制原图 |
| AI 标注任务 | 使用某个模型和配置处理一组图片的后台任务 | 产生待人工检查的标注，不等同于审核通过 |
| 数据版本 | 冻结图片、标签、标注、划分和增强配置的快照 | 训练从明确版本读取数据，后续工作区修改不会改写该版本 |
| 训练记录 | 数据版本、基础模型、参数、设备、日志及产物的关联记录 | 成功后可下载权重、创建转换任务或导出部署包 |
| 转换记录 | 将已训练权重转换为指定格式的后台任务 | 输出模型文件及部署说明，仍需匹配目标运行环境 |
| 推理部署包 | 包含训练权重和独立预测服务的 ZIP | 解压后在目标电脑运行，与标注平台分别管理 |

### 任务类型

| 类型 | 使用场景 | 标注内容 |
|---|---|---|
| 目标检测 `detect` | 找出图中的物体及位置 | 类别和矩形框 |
| 实例分割 `segment` | 描述每个物体的轮廓 | 类别和多边形 |
| 图片分类 `classify` | 判断整张图属于哪个类别 | 单张图片的类别 |
| 关键点 `pose` | 描述姿态或结构点的位置 | 对象框、固定顺序的关键点、可见性及骨架定义 |
| 兼容混合模式 `mixed` | 保留旧项目中的检测框和多边形 | 兼容已有数据；训练时仍需检查具体任务和版本是否匹配 |

## 业务模块与功能

### 1. 首次设置与登录

首次安装时，通过启动输出或 `python -m app.setup` 获取一次性初始化口令，在网页创建首位管理员。初始化采用数据库事务，避免多人同时操作创建重复的首位管理员。

常规部署使用独立账号登录。用户可提交注册申请，管理员审核后分配角色；申请尚未通过时不能进入业务页面。兼容的 Token 模式供已有安装使用，`disabled` 模式供本地开发使用。

输入是初始化口令或账号信息，输出是账号与登录会话。相关实现：[setup.py](../app/setup.py)、[accounts.py](../app/accounts.py)、[auth.py](../app/auth.py)。

### 2. 成员管理与账号安全

管理员可以搜索成员、创建账号、调整角色、启用或停用账号、重置密码、解除账号登录限制、审核注册申请，以及查看和撤销会话。成员可以维护自己的显示名称、修改密码、检查登录会话和安全记录。被拒注册申请可凭原申请密码查询状态、修改资料后重新提交。

管理员创建账号或重置密码后，用户必须先修改初始密码才能使用业务接口。角色变更、停用或密码重置会使相关会话和编辑租约失效；修改显示名称不会强制退出。系统保留至少一名启用的管理员。

注册申请审核与图片审核是两个功能：**拒绝注册申请仍需填写原因，退回图片修改的原因可以留空**。详见[用户指南](user-guide.md)。

### 3. 项目工作台与项目总览

项目列表支持按名称搜索、任务类型筛选和排序，显示图片预览、标注进度及待审核数量。管理员可创建项目、定义任务类型与类别，并将暂时不用的项目归档或恢复。普通及归档列表均可永久删除项目，须完整输入项目名称确认；后端再次核对名称与权限，并检查正在执行或排队的任务、图片编辑占用及计算资源租约。

项目总览汇总图片、标注、类别和审核进度，显示正在执行的 AI 任务、训练统计与最近训练记录，并提供下一步操作入口。项目内部按数据、任务块、标注与审核、AI 标注、数据版本、训练等页面组织流程。

归档用于整理项目列表，不删除数据，也不代表自动停止任务或把所有接口改成只读。具体任务块操作还会检查项目是否已归档。

永久删除通过事务移除项目关联记录，随后清理其图片、冻结版本与训练/转换产物；保留审计记录、共享基础模型及其他项目。清理失败会记录待处理文件并明确反馈，不会把已提交的删除误报成失败。操作说明见 [删除项目](user-guide.md#删除项目)，后端实现为 [project_deletion.py](../app/project_deletion.py)。

### 4. 类别与任务定义

管理员在创建项目时确定任务类型和类别；关键点项目还需定义点名称、顺序及骨架。类别支持颜色、数字快捷键和属性定义，画布只展示与项目任务匹配的工具。

服务端校验类别归属、坐标范围、几何形状和关键点可见性。修改对象类别时仍保留对象本身的几何信息；删除类别时检查标注历史引用，避免产生无效对象。

当前类别定义接口提供新增和删除，不提供任意修改已有类别定义的完整管理接口；至少保留一个类别，有标注历史的类别不能直接删除。

主要实现：[task_types.py](../app/task_types.py)、[schemas.py](../app/schemas.py)、[task-ui.js](../static/task-ui.js)。

### 5. 图片导入与数据管理

支持上传图片、浏览器选择的文件夹及 ZIP，识别 JPEG、PNG、WebP 和 BMP。系统检查真实图像格式、尺寸、像素数、文件体积和解压总量，按内容哈希在项目内去重，并生成缩略图。

数据页面提供文件名搜索、标注与审核状态筛选、选图、批量操作和进入单图工作区的入口。搜索与加载更多局部更新网格，复用已有图片节点，保留输入过程、选择和可保留的滚动位置；空结果可直接清除筛选。管理员删除图片时先查看预览结果，再执行删除；图片版本、编辑占用和任务状态会影响是否允许删除，结果按图片分别反馈。预览也会列出关联的历史版本：清理工作区图片不会删除已冻结的数据版本及训练记录，但当前没有图片回收站。标注对象的删除另采用软删除机制。

**ZIP 导入当前只提取图片，不解析 ZIP 中的 YOLO 或 COCO 标注文件。** 已有外部标注迁移不能直接视为已支持的导入功能。

主要实现：[storage.py](../app/storage.py)、[asset_management.py](../app/asset_management.py)、[asset-management-ui.js](../static/asset-management-ui.js)。

### 6. 人工标注工作区

工作区由图片队列、画布、工具栏和对象详情组成，支持从空白图片开始标注，也支持校正 AI 初标。

| 功能 | 说明 |
|---|---|
| 创建对象 | 画矩形框、多边形，选择整图类别，或按定义顺序放置关键点 |
| 校正对象 | 选择已有对象，拖动边界或控制点，修改类别、属性及关键点 |
| 查看图片 | 缩放、适配窗口、平移、隐藏或显示标注、专注模式、收起侧栏 |
| 选择重叠对象 | 使用右键菜单或类别标签定位对象，避免选错重叠区域 |
| 复用对象 | 在同一项目内复制和粘贴对象，再调整位置与类别 |
| 保存反馈 | 完成的标注写入服务端；失败时保留可重试的草稿，未完成草稿会阻止切图 |
| 图片浏览 | 按状态筛选，上一张、下一张或寻找下一张待校正图片；块内浏览限定当前块 |
| 无目标确认 | 没有现存标注对象的非分类图片可确认无目标，再提交审核；添加对象会清除确认，AI 未检出目标不会代替人工确认 |

常用快捷键包括 `V` 选择、`R` 矩形框、`P` 多边形、`K` 关键点、`H` 平移、`F` 适配、`A/D` 切图、`Enter` 完成适用草稿、`Esc` 取消草稿、`?` 查看帮助。工具是否可用取决于项目类型和当前编辑权限；输入框、弹窗、未保存草稿及请求处理中会限制快捷键行为。

主要实现：[app.js](../static/app.js)、[task-ui.js](../static/task-ui.js)、[annotation-shortcuts.js](../static/annotation-shortcuts.js)、[annotation-context-menu.js](../static/annotation-context-menu.js)。

### 7. 任务块协作

任务块将同一项目的图片分成互不重叠的小批次，让每位成员领取自己的标注范围。管理员创建任务块，默认每块 50 张，可设置 1–500 张；后续新导入的图片可另行分块。

分块只纳入尚未分块且未提交审核、未审核通过的图片。启用任务块后，新导入的图片需先分块并领取，才能人工编辑；任务块也不等同于训练/验证/测试集的划分。

成员可以领取指定块或下一块，也可以交回未完成的块；管理员可以分配、转交，或明确重新打开已完成的块。并发领取通过事务保证只有一人成功。每个成员在同一项目最多主动领取一个尚未提交或已退回的块，整个块提交后可领取下一块。

| 状态 | 含义 | 后续处理 |
|---|---|---|
| AI 处理中 `preparing` | 块内有排队或运行中的 AI 任务 | 等待结束后开放领取 |
| 待领取 `available` | 无人负责且满足领取条件 | 成员领取或管理员分配 |
| 标注中 `in_progress` | 负责人正在处理图片 | 修改、确认无目标并提交审核 |
| 待审核 `submitted` | 所有图片已提交或已通过，仍有图片待审核 | 审核员通过或退回 |
| 需修改 `rejected` | 有图片被退回 | 原负责人修改后再次提交 |
| 已完成 `completed` | 块内图片全部通过审核 | 保留记录，不再次进入领取池；返工由管理员明确开启 |

块的归属随账号保持，关闭页面、退出登录或暂时断网不会自动把整块交给别人。当前没有定时自动回收未完成块的功能，需要主动交回或由管理员处理。

管理员重新打开已完成的任务块时必须填写返工原因，块内图片重新进入草稿状态。这个原因要求独立于单张图片的退回原因。

任务块可以在 AI 初标后创建，也可以在 AI 处理中创建并等待，或直接分块人工标注。负责人可对自己未完成块中的草稿、退回图片运行单图或批量 AI，包括 SAM3；人工绘制和人工校正过的标注保留。其他成员的块、已交回但尚未重新分配的人工任务块、已完成的块及块内待审核、已通过图片受到写保护，管理员同样遵守归属限制。页面显示可处理范围和跳过原因；服务端在创建、重试、推理前及写入时再次校验。预览只读，不代表具备正式任务的写权限。

主要实现：[work_blocks.py](../app/work_blocks.py)、[work-blocks-ui.js](../static/work-blocks-ui.js)。操作说明见[任务块协作](user-guide.md#任务块协作)。

### 8. 编辑占用与冲突保护

任务块规定长期负责人；图片编辑租约规定当前由哪个页面编辑。即使同一账号打开两个标签页，也使用不同的页面令牌，第二个页面不能覆盖或释放第一个页面的有效租约。

图片和标注带有版本号，保存、审核、删除或转交时检查预期版本。过期页面会收到冲突提示，而不会直接覆盖新结果。转交任务块会使旧页面的编辑状态失效；已提交和已通过图片也会限制人工改动及 AI 改写。

这些检查在后端执行，页面上的只读提示是同一规则的展示。任务负责人之外的管理员也需要先领取或转交，才能修改块内标注；审核员可以审核已提交图片。

主要实现：[collaboration.py](../app/collaboration.py)、[work_blocks.py](../app/work_blocks.py) 和 [main.py](../app/main.py) 的图片锁、标注及工作流接口。

### 9. 提交与审核

图片审核状态独立于“有没有标注”。基本状态为草稿 `draft`、处理中 `in_progress`、待审核 `submitted`、已通过 `approved` 和已退回 `rejected`。

标注完成后，由成员单图或批量提交。审核员或管理员检查已提交图片，选择通过或退回。退回后由负责人修改并重新提交；已通过图片需要按权限重新打开才能继续修改，已完成任务块的返工还需管理员重新打开整个块。

单图审核支持 **`Y` 通过、`X` 打开退回窗口**。通过或退回成功后，自动刷新当前范围的队列并进入下一张待审核图片，跳过已经处理或被其他页面占用的图片。当前队列没有可继续审核的图片时，返回任务块列表或项目的待审核数据列表。切图和加载标注期间不能连续误触审核。

单图和批量退回都允许不填写原因，填写的文字保存在审核记录中。批量操作先预览哪些图片可处理，批量通过还需确认已经检查结果；每次最多 500 张，执行时逐张复核版本和占用，成功、跳过、冲突和失败分别反馈。

从 AI 任务历史发起“提交审核”时，范围来自该次任务确实写入过结果的图片，采用当前保存的标注，也包含之后的人工作业。任务结束不等同于自动提交或自动通过。

主要实现：[review.py](../app/review.py)、[review-ui.js](../static/review-ui.js)、[main.py](../app/main.py)。

### 10. 模型服务与基础权重

模型服务页显示模型能力、适用任务、配置状态及健康情况。管理员可接入、配置或启停自定义远程服务，并通过环境变量引用服务密钥。

| 模型来源 | 当前用途 | 准备方式 |
|---|---|---|
| Qwen3.5 | 本地 0.8B / 35B-A3B 图像语义标注，保留远程接口 | 标注时选择后后台下载，或从模型服务提前下载；远程模式配置接口地址 |
| Grounding DINO Tiny | 本地文本引导目标检测 | 下载目录中的固定权重与配置 |
| SAM 3 | 本地文本概念检测与分割 | 检测项目按完整实例掩码生成外接框，分割项目保留多边形；获得官方访问权限后自行放置 sam3.pt，页面重新检查，无自动下载 |
| SAM2.1 Tiny | 本地交互式分割 | 自行放置 Tiny 权重，兼容原完整 HF 目录，结合点或框提示 |
| Ultralytics 权重 | 支持检测、分割、分类或关键点的本地训练与推理 | 支持 8 个系列、85 个内置训练选项，默认按需下载；可提前下载或由管理员导入兼容 `.pt` 文件 |
| 自定义远程模型 | 检测、分割、分类、关键点或交互分割协议适配 | 在模型服务中配置对应协议 |

基础模型下载使用固定来源、大小和 SHA-256 校验，并支持中断后续传。导入权重不会覆盖同名文件。模型文件与第三方组件有独立的许可信息，见[第三方说明](../THIRD_PARTY_NOTICES.md)。

网页准备目录共 90 项：85 个训练模型、Grounding DINO、两个 Qwen3.5 型号，以及两个只提供本地文件说明的 SAM 条目。可自动下载的共 88 项；SAM 下载接口明确拒绝请求。页面区分文件已存在、下载中和推理就绪，支持刷新文件检查。标注员、审核员和管理员可准备指定的官方标注模型；自定义下载及权重导入仍由管理员管理。

主要实现：[model_downloads.py](../app/model_downloads.py)、[annotation_models.py](../app/annotation_models.py)、[training_models.py](../app/training_models.py)、[providers](../app/providers)、[model-preparation-ui.js](../static/model-preparation-ui.js)、[setup-ui.js](../static/setup-ui.js)。

### 11. AI 预览与后台标注

AI 标注页面允许选择模型、提示词、目标类别、置信度和处理范围。先选 1–3 张代表图片试标，查看预测与过滤结果，再决定是否提交正式任务；预览不会写入正式标注。

正式任务支持自动批量标注和适用模型的单图辅助。后台队列记录逐图结果、进度、错误、取消和重试信息；模型预测会经过类别、置信度、坐标和任务类型校验后才写入。人工改过的对象及受保护的审核、任务块状态会限制 AI 覆盖。

GPU 忙时，后台任务等待资源并维持租约，交互预览给出可重试提示。模型未配置或运行失败会显示错误，不会自动以演示结果冒充模型预测。

主要实现：[annotation_preview.py](../app/annotation_preview.py)、[annotation_inference.py](../app/annotation_inference.py)、[jobs.py](../app/jobs.py)、[ai-preview-ui.js](../static/ai-preview-ui.js)。

### 12. 数据统计、质量检查与审计

项目统计包括图片与对象数量、标注覆盖率、审核状态分布、类别分布、模型或人工来源和形状类型。质量接口还汇总缺失媒体、空标注、重复哈希、低置信度及过小对象等线索，帮助定位需要复查的数据。

质量统计是检查线索，不代替人工审核，也不代表模型的业务精度。明确确认的无目标图片应结合其业务含义判断，不能只凭对象数为零认定错误。

审核记录保留单图状态变化、操作者和原因；任务块保留领取、转交及完成历史；操作审计和账号安全记录分别帮助追踪业务操作及登录管理行为。

主要实现：[analytics.py](../app/analytics.py)、[database.py](../app/database.py) 及各业务模块的审计写入。

### 13. 数据版本、划分与增强

数据版本向导依次选择数据、设置训练/验证/测试划分、配置增强、预览并确认。创建版本默认只纳入审核通过的图片，也可以明确选择全部图片；训练使用所选版本，不会在训练时再次按审核状态过滤。冻结时检查来源及文件哈希一致性，预览后来源发生变化时需要重新检查。

划分默认训练/验证/测试为 70%/20%/10%，比例合计为 100%，随机种子用于复现分组。当前按图片确定划分，不提供按类别分层或按人员、视频来源分组的划分策略，也不自动保证近重复图片不会跨组。

增强仅作用于已划分的训练集，保留原图，不扩增验证和测试集。预览显示原图与增强图对照，生成受任务类型、操作参数和数量上限约束。

| 增强类别 | 当前操作 |
|---|---|
| 几何变换 | 水平/垂直翻转、90/180/270 度旋转；同步调整框、多边形和关键点 |
| 光照与颜色 | 亮度、对比度、饱和度、灰度、伽马、色相、自动对比度、直方图均衡 |
| 清晰度与图像质量 | 模糊、噪声、锐度、JPEG 压缩、降低清晰度 |

关键点水平翻转需要完整的左右点映射。增强可按每图副本数或预计新增总量配置；无有效变化或重复内容可能被跳过，预计数量不保证全部生成。增强不会修改工作区原始图片和标注，当前离线增强不提供裁剪、任意角度旋转、Mosaic 或 MixUp。

生成后保存图片、类别、标注、来源关系和配置快照。版本详情可以按划分查看图片与缩略图，导出 YOLO、COCO 或 JSON；分类使用分类目录或 JSON，不能导出为 COCO。工作区快速导出与版本导出用途不同，正式训练应选择固定数据版本。

版本创建目前由同步接口完成，不是可取消的后台生成任务；当前没有版本修改或删除接口。

主要实现：[dataset_versions.py](../app/dataset_versions.py)、[dataset_augmentation.py](../app/dataset_augmentation.py)、[dataset-generation.js](../static/dataset-generation.js)、[dataset-ui.js](../static/dataset-ui.js)。

### 14. 训练任务与监控

训练页面从数据版本、内置模型和本地权重目录创建任务，先检查任务、类别和模型兼容性，再选择训练参数及 CPU 或可用 GPU。内置 YOLOv5u、YOLOv8、YOLOv9、YOLOv10、YOLO11、YOLO12、YOLO26 和 RT-DETR，检测 38 个、分割 17 个、分类与关键点各 15 个选项；可搜索名称或按系列筛选，筛选隐藏已选模型时明确保留当前选择。无需预先安装权重。创建时记录数据版本哈希、初始化方式、基础权重哈希或架构配置、后端版本和参数，便于追溯结果。

训练后端使用 Ultralytics，支持上述检测、实例分割、分类和关键点任务，不是任意 PyTorch 脚本或大语言模型的训练平台，也不负责训练 Grounding DINO/SAM。训练和验证划分都需具备适用的有效标注，创建训练前会检查。

| 功能 | 说明 |
|---|---|
| 预训练与从零训练 | 默认使用预训练权重；关闭后从模型结构生成随机参数，历史配置与复用参数保留该选择 |
| 按需下载 | worker 在分配 GPU 前下载缺少的官方权重，固定来源、大小与 SHA-256，显示进度，支持取消和失败重试续传 |
| 上传自己的权重 | 管理员在训练弹窗上传兼容 `.pt` 文件，检查真实任务后选择模型；保留已填参数，同名不覆盖 |
| 参数配置 | 按任务和模型能力设置轮次、图片尺寸、batch、优化器、学习率、种子、早停、AMP 等参数，服务端检查范围和兼容性 |
| 训练列表 | 按状态、任务或名称/模型搜索，分页查看，保留当前选中记录 |
| 实时监控 | 展示运行状态、进度、逐轮 loss、评估指标、日志及错误信息 |
| 取消与重试 | 请求取消任务，或从失败/取消记录创建可追踪的重试记录 |
| 失联恢复 | 使用 worker 租约与执行代次识别过期任务；恢复时检查可用检查点并记录恢复情况 |
| 产物下载 | 成功后提供校验过的训练权重；再进入模型转换或部署包导出 |

恢复是否能继续原训练状态取决于检查点是否保留相应内容，不保证任意中断都能无损续训。当前单次训练选择一个设备，不提供 DDP 多卡分布式训练。

资源选择提供均衡、性能优先、显存友好等策略，用于选择设备；显存友好不会自动减小 batch 或改写训练参数。训练器自身的在线增强与版本生成时的离线增强是两套配置。

主要实现：[training.py](../app/training.py)、[training_models.py](../app/training_models.py)、[模型清单](../model-catalog/models.json)、[training-navigation.js](../static/training-navigation.js)、[training-charts.js](../static/training-charts.js)、[overview-training.js](../static/overview-training.js)。

### 15. 模型转换

训练完成后可创建转换任务，页面按实际环境提供可用格式和参数。转换与训练分别记录进度、日志、取消、重试、文件大小和哈希，并提供产物下载及部署说明。

| 格式 | 主要用途 | 运行条件与边界 |
|---|---|---|
| TorchScript | 在相应 PyTorch 运行环境加载模型 | 仍需匹配输入和后处理 |
| ONNX | 交给支持 ONNX 的推理工具使用 | 导出参数和目标运行时需兼容 |
| TensorRT `engine` | NVIDIA GPU 推理 | 需要专用依赖与 GPU；引擎需匹配目标硬件和软件环境 |
| RKNN | Rockchip 设备端推理 | 需要匹配的 SDK、目标芯片和部署处理；可使用隔离 Python 环境 |

可用选项以页面环境探测结果为准。生成某个格式的文件，不等于完成了目标设备安装或业务精度验收。

RKNN 支持按目标芯片配置浮点或 INT8、均值/标准差、量化算法及优化级别；RV1103/RV1106 要求 INT8。INT8 校准从该训练版本的训练集取图并校验内容，不使用验证或测试集。当前 TensorRT 转换没有对应的 INT8 校准流程。转换入口针对平台内已完成训练的产物，不是任意上传模型的通用转换服务。

主要实现：[conversion_support.py](../app/conversion_support.py)、[conversion_runner.py](../app/conversion_runner.py)、[training.py](../app/training.py)、[conversion-ui.js](../static/conversion-ui.js)。

### 16. 独立推理部署

“下载推理部署包”将已完成训练的 `.pt` 权重、任务和标签清单、SHA-256、运行代码、固定依赖、Docker 配置、启动脚本及调用示例打包为 ZIP。

目标电脑解压后可按 CPU 或 NVIDIA GPU 模式启动，网页支持上传图片查看预测结果，业务程序通过带 Bearer 密钥的 `POST /predict` 调用。服务启动时校验模型文件，使用相应任务的预处理和后处理。

预测为单张图片同步请求，限制 20 MiB、2,500 万像素，同时处理一个请求，忙时返回 HTTP 429。网页展示 JSON 和适用结果中的矩形框，不是完整的分割遮罩、关键点骨架可视化器；附带的 `predict.py` 是基于 `httpx` 的调用示例。部署包启动脚本与主项目启动器不同，端口通过部署包配置设置，不具备主启动器的自动换端口功能。

当前部署包运行训练出的 `.pt` 模型，不是 ONNX、TensorRT 或 RKNN 的统一运行器。独立服务不加入平台的 GPU 调度，若与平台共享显卡，需要单独安排资源。

主要实现：[deployment.py](../app/deployment.py)、[runtime/server.py](../app/runtime/server.py)、[deploy/runtime](../deploy/runtime)。启动与调用步骤见[模型推理部署](deployment.md)。

## 角色与权限

正常协作采用 `accounts` 模式。角色决定可执行的操作，任务块归属、编辑租约、审核状态和版本号还会继续限制具体图片的写入。

| 操作 | 当前权限规则 |
|---|---|
| 查看共享项目、图片、模型目录、任务和产物 | 已登录成员；接口仍检查相应记录是否存在和可访问 |
| 上传图片、人工标注、领取与提交任务块 | 已登录成员；修改块内图片需为负责人，且通过编辑和状态检查 |
| 创建/归档/永久删除项目、维护类别、删除图片、配置模型和管理用户 | 管理员 |
| 创建任务块、分配/转交任务块、重新打开已完成块 | 管理员 |
| 主动交回未完成任务块 | 当前负责人或管理员，需符合块状态和版本条件 |
| 审核图片、预览/创建数据版本、创建/重试训练和转换 | 审核员或管理员 |
| 查看和下载训练、转换产物或部署包 | 已登录成员，产物需存在并通过相应校验 |
| 取消训练或转换 | 当前后端接口允许已登录成员；界面上的操作入口可能更严格 |
| 调整自己的资料、密码和会话 | 当前账号本人；管理员另有成员管理入口 |

当前采用实例内共享项目与角色控制，尚无按项目成员划分的多租户隔离。不要把“任务块只能由负责人编辑”理解成“其他成员看不到这个项目”。

当前也不强制标注者和审核者必须是不同人员；有审核权限的成员可以审核自己的提交。

## 运行架构与数据保存

### 进程分工

| 部分 | 负责什么 | 计算与数据关系 |
|---|---|---|
| 浏览器前端 | 表单、画布、列表、快捷键、结果展示 | 不训练模型，电脑无需安装 CUDA |
| Web 服务 | 页面、认证、业务接口、任务提交、结果读取与下载 | 轻量环境不安装 PyTorch，通过内部调用请求计算能力 |
| worker 计算服务 | AI 推理、训练、转换、模型与设备探测 | 与 Web 共享同机数据及模型卷，在 CPU/GPU 上计算 |
| 独立推理服务 | 使用导出的模型提供预测 API | 单独部署和鉴权，不依赖平台的项目数据库 |

后台任务由 Web 写入数据库，再由 worker 领取执行；本地模型探测和本地模型交互预览通过内部接口请求 worker，远程模型预览由 Web 直接请求已配置的远端服务。运行状态、租约和执行代次用于避免过期 worker 覆盖新执行结果。源码调试也支持 Web 内嵌 worker，标准 Compose 部署使用分开的 Web 和 worker 进程。

本地标注、预览、训练和转换共享跨进程 GPU 文件锁，优先按物理 GPU UUID 识别资源，避免容器中设备编号变化造成冲突。远端模型服务和不使用这套锁的外部程序不受其调度。每个 PipelineWorker 顺序处理训练或转换，当前不是弹性计算集群。

### 数据保存

| 数据 | 默认位置或配置 | 用途 |
|---|---|---|
| SQLite 数据库 | `MDP_DATA_DIR/mdp.sqlite3` | 账号、项目、标注、任务块、审核、版本索引、训练与转换记录 |
| 原始图片 | `MDP_DATA_DIR/media/` | 标注工作区的媒体文件 |
| 缩略图 | `MDP_DATA_DIR/thumbnails/` | 列表与版本预览的缩略图缓存；画布相邻图片预加载读取原图 |
| 数据版本文件 | `MDP_DATA_DIR/versions/` | 版本快照、划分及增强结果 |
| 训练与转换文件 | `MDP_DATA_DIR/training/` | 执行目录、检查点及模型产物 |
| 基础模型 | `MDP_MODELS_DIR` | 下载或管理员准备的模型权重及配置 |
| 部署配置 | 项目目录的 `.env` | 端口、访问地址、认证及模型服务等设置，不提交到仓库 |

Compose 使用 `visionforge-data`、`visionforge-models` 持久卷；这些旧内部名称为兼容已有部署而保留。数据库启动时执行结构迁移。停止容器保留卷，备份与恢复需同时考虑数据库、图片、版本、训练产物和模型，详见[迁移与备份](operations.md)。

### 安全与一致性

认证层验证账号或 Token，浏览器写请求检查来源，上传在解析大文件前进行鉴权，并对请求体、像素数和 ZIP 解压量设限。worker 内部接口使用共享随机凭证和操作白名单，不在默认 Compose 中暴露到宿主机。

数据写入使用权限、归属、租约与版本检查；模型下载、训练产物和部署包使用哈希等元数据校验。代码有这些保护不代表所有部署已完成安全或容量验收，实际上线检查见[生产部署说明](production.md)。

## 后端源码索引

以下索引覆盖业务和基础设施模块。`__init__.py` 用于 Python 包组织，未单独作为业务功能列出。

| 文件 | 职责 |
|---|---|
| [app/main.py](../app/main.py) | FastAPI 启动、静态页面、项目与类别、图片与标注、图片锁、单图审核、模型服务、AI 任务入口及工作区导出 |
| [app/config.py](../app/config.py) | 读取和校验环境配置，提供数据、模型及各业务目录 |
| [app/database.py](../app/database.py) | SQLite 表结构、迁移、连接和事务辅助、审计基础能力 |
| [app/schemas.py](../app/schemas.py) | 项目、标注、AI 任务、版本、增强、训练和转换请求结构及参数校验 |
| [app/serializers.py](../app/serializers.py) | 将数据库中的标注和任务记录整理成接口响应 |
| [app/auth.py](../app/auth.py) | 解析登录身份、角色、会话和页面编辑令牌，提供权限检查 |
| [app/accounts.py](../app/accounts.py) | 登录、注册申请、用户管理、密码、会话撤销和账号安全事件 |
| [app/setup.py](../app/setup.py) | 初始化口令、首次管理员创建及 CLI 入口 |
| [app/security.py](../app/security.py) | 浏览器写请求来源检查、安全响应头、按接口制定请求体预算 |
| [app/http_limits.py](../app/http_limits.py) | 对实际接收的请求体实施大小限制，供平台和独立推理服务复用 |
| [app/storage.py](../app/storage.py) | 图片解码校验、ZIP 图片读取、内容哈希及媒体落盘 |
| [app/asset_management.py](../app/asset_management.py) | 批量删除预览、逐图校验、删除及媒体清理结果 |
| [app/project_deletion.py](../app/project_deletion.py) | 项目名称确认、管理员权限与任务占用检查、关联数据删除及文件清理审计 |
| [app/file_responses.py](../app/file_responses.py) | 固定模型下载文件，保留分段下载并避免项目删除中断已开始的下载 |
| [app/task_types.py](../app/task_types.py) | 任务允许的形状、关键点顺序/数量/可见性及坐标规则 |
| [app/collaboration.py](../app/collaboration.py) | 图片写入、审核、删除、编辑占用和 AI 冲突的公共检查 |
| [app/work_blocks.py](../app/work_blocks.py) | 分块、领取、归属、转交、交回、完成、返工和 AI 范围保护 |
| [app/review.py](../app/review.py) | 批量审核预览与执行、逐图结果，以及按 AI 任务确定提交范围 |
| [app/analytics.py](../app/analytics.py) | 项目概览统计、类别与来源分布、数据质量线索 |
| [app/annotation_preview.py](../app/annotation_preview.py) | 少量样本试标、预览响应及不写入正式标注的结果展示 |
| [app/annotation_inference.py](../app/annotation_inference.py) | 构建推理上下文、提示信息与统一预测过滤逻辑 |
| [app/jobs.py](../app/jobs.py) | AI 标注任务领取、租约、逐图执行、取消、恢复和结果写入 |
| [app/model_downloads.py](../app/model_downloads.py) | 固定下载目录、进度、续传、完整性校验和管理员权重导入 |
| [app/worker_client.py](../app/worker_client.py) | Web 调用计算服务、共享凭证、探测/预览代理 |
| [app/worker_server.py](../app/worker_server.py) | 内部计算接口白名单、鉴权、健康检查及后台 worker 生命周期 |
| [app/dataset_versions.py](../app/dataset_versions.py) | 数据选择、版本预览与快照、划分、版本图片读取及导出 |
| [app/dataset_augmentation.py](../app/dataset_augmentation.py) | 增强操作、确定性生成、几何与标注同步变换 |
| [app/training.py](../app/training.py) | 模型目录与资源、训练/转换接口、执行队列、租约恢复、日志指标和产物 |
| [app/training_models.py](../app/training_models.py) | 官方训练模型清单、按需下载、共享下载锁及取消与续传 |
| [app/plot_fonts.py](../app/plot_fonts.py) | 为训练绘图准备本地字体，减少隐式联网依赖 |
| [app/conversion_support.py](../app/conversion_support.py) | 转换环境与能力探测、格式参数、校准及部署说明辅助 |
| [app/conversion_runner.py](../app/conversion_runner.py) | 执行专用转换流程，特别是独立环境中的 RKNN 转换 |
| [app/deployment.py](../app/deployment.py) | 将校验后的权重、运行文件和配置生成可下载部署 ZIP |
| [app/runtime/server.py](../app/runtime/server.py) | 独立预测服务、模型加载校验、鉴权、图片上传预测及网页演示 |
| [app/diagnostics.py](../app/diagnostics.py) | 检查存储、基础依赖及 GPU 条件，供启动器和人工排查使用 |

### 模型适配层

| 文件 | 职责 |
|---|---|
| [providers/__init__.py](../app/providers/__init__.py) | 对外汇总适配层入口，供业务代码调用 |
| [providers/base.py](../app/providers/base.py) | 统一模型预测结果和适配器接口 |
| [providers/registry.py](../app/providers/registry.py) | 内置模型登记、能力描述和适配器选择 |
| [providers/local.py](../app/providers/local.py) | 本地 Grounding DINO、SAM 系列的加载、可用性检查和推理 |
| [providers/qwen.py](../app/providers/qwen.py) | Qwen/OpenAI 兼容视觉接口的请求与结果解析 |
| [providers/remote.py](../app/providers/remote.py) | 项目约定的远程检测、分割等协议适配 |
| [providers/yolo.py](../app/providers/yolo.py) | Ultralytics 权重推理及统一结果转换 |
| [providers/execution.py](../app/providers/execution.py) | 物理 GPU 识别、跨进程资源互斥及推理后缓存释放 |
| [providers/demo.py](../app/providers/demo.py) | 显式开启的演示适配器，默认不作为真实模型结果使用 |

## 前端源码索引

前端使用原生 JavaScript 模块和 CSS，由 Web 直接提供静态资源。`app.js` 仍承担主状态与多项页面编排，下面的独立模块负责更具体的交互。

| 文件 | 职责 |
|---|---|
| [static/index.html](../static/index.html) | 页面根节点、样式与主脚本入口、静态资源版本 |
| [static/app.js](../static/app.js) | 路由、会话、API 请求、项目页面、画布、保存、单图审核、轮询与通用弹窗 |
| [static/project-api.js](../static/project-api.js) | 任务块接口的版本兼容判断，区分旧服务缺接口与真实业务错误 |
| [static/workspace-navigation.js](../static/workspace-navigation.js) | 项目上下文与工作流导航、底部服务状态/主题/账号入口、手机抽屉、键盘焦点与项目内标签页状态保存 |
| [static/workspace-feedback.js](../static/workspace-feedback.js) | 保留加载前内容、延迟加载提示、过期请求隔离及显式页面/标签页入场；遵循减少动态效果偏好 |
| [static/workspace-feedback.css](../static/workspace-feedback.css) | 内容区层级与响应式布局、加载反馈、按钮/弹窗/提示的短过渡；标注画布不参与入场动画 |
| [static/theme.js](../static/theme.js) | 首屏前应用浅色/深色/跟随系统偏好，处理本地保存、系统变化和跨标签页同步；存储受限时仍可切换 |
| [static/themes.css](../static/themes.css) | 双主题语义色、表面、文字、表单、状态与图表配色；保留图片及标注类别原色 |
| [static/history-navigation.js](../static/history-navigation.js) | 历史记录分页与导航状态辅助 |
| [static/setup-ui.js](../static/setup-ui.js) | 首次管理员设置和基础模型下载面板 |
| [static/auth-ui.js](../static/auth-ui.js) | 登录、注册申请及相关表单状态 |
| [static/accounts-ui.js](../static/accounts-ui.js) | 用户管理、注册申请审核及管理员操作 |
| [static/security-ui.js](../static/security-ui.js) | 个人资料、密码、会话及安全事件界面 |
| [static/task-ui.js](../static/task-ui.js) | 任务类型表单、类别选择、分类与关键点交互、属性和导出选项 |
| [static/annotation-shortcuts.js](../static/annotation-shortcuts.js) | 标注与审核快捷键、帮助、输入焦点及忙碌状态保护 |
| [static/annotation-context-menu.js](../static/annotation-context-menu.js) | 命中测试、重叠对象选择、右键对象操作 |
| [static/canvas-guides.js](../static/canvas-guides.js) | 画布辅助线和绘制过程中的定位反馈 |
| [static/asset-management-ui.js](../static/asset-management-ui.js) | 批量删除图片的预览、确认和执行结果 |
| [static/work-blocks-ui.js](../static/work-blocks-ui.js) | 任务块列表、筛选、领取/转交/返工以及块内请求范围 |
| [static/review-ui.js](../static/review-ui.js) | 审核队列、批量选图、预览与执行、任务块提交和 AI 任务提交范围 |
| [static/ai-preview-ui.js](../static/ai-preview-ui.js) | AI 试标样本选择、预测展示和参数变化后的预览状态管理；参数表单和正式任务提交由主模块编排 |
| [static/dataset-generation.js](../static/dataset-generation.js) | 数据版本向导、划分比例、增强配置与预览请求管理 |
| [static/dataset-ui.js](../static/dataset-ui.js) | 版本样本展示、版本图片分组浏览 |
| [static/training-navigation.js](../static/training-navigation.js) | 训练列表查询参数、筛选分页和选中记录导航 |
| [static/training-charts.js](../static/training-charts.js) | 训练曲线、任务评估指标及历史数据展示 |
| [static/overview-training.js](../static/overview-training.js) | 项目总览中的训练摘要和最近记录 |
| [static/conversion-ui.js](../static/conversion-ui.js) | 转换格式说明、选项表单、参数校验与设置摘要 |

`static/` 中的 CSS 按账号、导航、画布、审核、任务块、数据版本、训练等界面拆分；`styles.css` 提供基础样式，其他样式由 `index.html` 统一加载，`themes.css` 定义语义色，随后加载 `workspace-feedback.css` 调整内容区交互。`theme.js` 在样式前同步执行，首次绘制就应用已保存的偏好；切换只更新根元素属性，不重建业务页面或画布。更新交互模块时需保持导入和资源版本一致，避免浏览器混用旧文件。

## 启动、运维与开发工具

| 文件或目录 | 功能 |
|---|---|
| [start.sh](../start.sh)、[start.ps1](../start.ps1) | 一键启动、CPU/GPU 模式、环境检查、指定端口/自动换端口、状态查询、停止与重新构建 |
| [compose.yaml](../compose.yaml)、[compose.gpu.yaml](../compose.gpu.yaml) | Web/worker 服务、共享卷、健康检查和 NVIDIA GPU 覆盖配置 |
| [Dockerfile](../Dockerfile) | 构建轻量 Web、CPU worker 和 GPU worker 镜像 |
| [.env.example](../.env.example) | 可复制的配置模板；真实凭证保存在本地 `.env` |
| [model-catalog/models.json](../model-catalog/models.json) | 可下载模型的固定版本、来源、文件清单、校验和与许可信息 |
| [requirements](../requirements) | 按 Web、CPU/GPU worker、独立 runtime、可选转换 SDK 拆分依赖 |
| 根目录 `requirements*.txt` | 兼容已有安装的依赖入口；`requirements.txt` 和 `requirements-models.txt` 引用 GPU worker 依赖，CPU 使用独立文件 |
| [scripts/download_models.sh](../scripts/download_models.sh) | 从命令行下载固定版本的 Grounding DINO Tiny 和 SAM2.1 Tiny |
| [scripts/package_release.py](../scripts/package_release.py) | 制作安装 ZIP/TAR，写入镜像前缀和版本，排除本地数据与秘密配置 |
| [scripts/smoke_pipeline.py](../scripts/smoke_pipeline.py) | 使用临时数据验证训练及转换等基础流程 |
| [scripts/smoke_open_source.py](../scripts/smoke_open_source.py) | 验证初始化、Web/worker 分离、训练、部署包及独立推理的实际流程 |
| [scripts/smoke_conversions.py](../scripts/smoke_conversions.py) | 在具备相应环境时执行真实模型转换冒烟 |
| [scripts](../scripts) 中的 `check_*.mjs` | 检查画布、快捷键、认证、任务块、审核、版本向导和训练等前端行为 |
| [tests](../tests) | Python 接口、权限、并发、任务执行、导出、账号、上传及启动脚本回归 |
| [.github/workflows/ci.yml](../.github/workflows/ci.yml) | Web 依赖审计、回归、CPU 冒烟、前端/Compose 检查和 Web 镜像构建 |
| [.github/workflows/release.yml](../.github/workflows/release.yml) | `v*` 标签触发验证、镜像构建发布、安装包制作及 GitHub Release 附件上传 |
| [deploy/runtime](../deploy/runtime) | 导出包使用的启动器、Compose、Dockerfile、说明和 Python 预测示例 |

安装与启动参数见 [README](../README.md)，排查依赖和设备问题见[安装说明](installation.md)，远程访问见[服务器使用方式](remote-server.md)，升级备份见[运维说明](operations.md)，发布见[发行说明](releases.md)。

更新源码后需重新构建或重启后端，再刷新浏览器。仅修改磁盘文件不会替换已运行 Python 进程中的接口。旧版后端缺少任务块接口时，前端会在确认安全的旧项目上提供兼容提示；已启用任务块的项目不能通过降级绕过归属规则。

## 主要接口入口

下表用于定位模块，不替代具体请求参数。运行中的 FastAPI 服务通过 `/docs` 和 `/openapi.json` 提供当前接口定义，业务接口按身份和角色鉴权。

| 领域 | 代表性入口 |
|---|---|
| 服务状态 | `/api/health`、`/api/ready` |
| 首次设置、身份与会话 | `/api/setup`、`/api/login`、`/api/session` |
| 注册、成员与个人安全 | `/api/register`、`/api/registration-requests`、`/api/users`、`/api/account` |
| 项目、类别与图片 | `/api/projects`、`/api/projects/{id}/labels`、`/api/projects/{id}/assets` |
| 永久删除项目 | `DELETE /api/projects/{id}`，JSON 请求体为 `{"confirmation_name":"项目完整名称"}`，仅管理员 |
| 图片编辑与审核 | `/api/assets/{id}/lock`、`/annotations`、`/workflow`、`/review-events`；标注对象更新使用 `/api/annotations/{id}` |
| 任务块 | `/api/projects/{id}/work-blocks` 及领取、交回、分配、返工子接口 |
| 批量审核 | `/api/projects/{id}/review/preview`、`/api/projects/{id}/review/batch` |
| 模型与 AI | `/api/models`、`/api/model-downloads`、`/api/projects/{id}/annotation-preview`、`/api/projects/{id}/jobs` |
| 数据质量与审计 | `/api/projects/{id}/analytics`、`/quality`、`/audit-logs` |
| 数据版本与增强 | `/api/projects/{id}/versions`、`/versions/preview`、`/augmentation-options`；版本详情使用 `/api/versions/{id}` |
| 训练与设备 | `/api/training/catalog`、`/api/training/resources`、`/api/projects/{id}/training-runs` |
| 训练监控与权重 | `/api/training-runs/{id}/logs`、`/metrics`、`/artifact` |
| 转换与部署包 | `/api/training-runs/{id}/conversions`、`/api/conversions/{id}`、`/api/training-runs/{id}/deployment` |
| 转换产物与说明 | `/api/conversions/{id}/artifact`、`/manifest` |
| 独立推理服务 | `/health`、`/predict`，使用部署包自己的服务地址和密钥 |

同一行省略前缀的子路径沿用该行前面的资源路径；请求方法和字段应以接口定义为准。

## 当前支持范围

| 主题 | 当前范围 |
|---|---|
| 数据类型 | 以静态图片为工作对象；模型元数据中出现视频能力不代表已实现视频导入、逐帧标注或跟踪 |
| 外部标注导入 | 支持图片/ZIP 导入及多格式导出，尚无通用 YOLO/COCO 标注导入流程 |
| 本机与远程 | 本机启动，或将整套 Web、worker 和数据部署在一台 GPU 服务器后远程访问 |
| 独立异地 worker | 尚无节点注册、跨主机数据同步和产物回传；配置内部 worker URL 不等于具备该能力 |
| 多 GPU | 可以限制可见 GPU 并选择计算设备；单次训练不提供 DDP 多卡训练 |
| 运行环境 | 面向 CPU 与 NVIDIA 环境；Windows/WSL2、macOS/AMD 和专用转换环境的具体实测范围以验证记录为准 |
| 团队隔离 | 共享项目、角色权限、任务块负责人和页面锁；尚无多租户或按项目成员隔离 |
| 备份与监控 | 提供健康检查、日志和运维步骤；没有内置定时备份、外部告警或自动故障切换平台 |
| 完成与回收 | 全部通过的块保留负责人和历史，不自动重领；未完成块通过交回/转交管理，没有定时自动回收 |
| 模型部署 | 自动生成 `.pt` 独立服务；其他转换格式需结合目标运行环境使用 |
| 发行状态 | 仓库具备构建和发布工作流；是否已有公开镜像或发行包，应以实际发布记录为准 |

现有验收范围和未实测条件见[验证记录](verification.md)。原创代码许可见 [LICENSE](../LICENSE)，第三方依赖和模型许可见[第三方说明](../THIRD_PARTY_NOTICES.md)。
