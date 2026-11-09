# 界面语言与翻译维护

CVSprout 支持简体中文和英文。登录页顶部和工作区侧栏提供独立的语言选择器，侧栏收起时也可点击地球图标切换；首次创建管理员的页面也提供语言选择。导出的推理部署包包含同样的语言功能。

语言优先级为 URL 中的 `?lang=zh-CN` / `?lang=en`、浏览器保存的选择、浏览器首选语言；不支持的语言回退到简体中文。切换语言会重载当前页面并保留项目地址。正在编辑的表单、未完成的标注以及正在提交的操作会阻止切换，先保存或关闭后再试。语言偏好属于浏览器，不修改账号或项目数据；禁用本地存储时，URL 仍保留选择。

页面标题、导航、表单、弹窗、快捷键帮助、图表说明、状态提示、校验与接口错误都使用统一翻译。日期和数字使用所选语言的格式。用户填写的项目名称、标签、文件名、备注、审核反馈、模型提示词和标注属性保留原文。第三方返回的未知诊断、模型预测类别和原始技术数据同样保留原文。

初始化或命令行创建账号时使用的默认名称“管理员” / “Administrator”，在账号菜单、成员列表、负责人信息和账号表单中随语言显示。`accountDisplayName()` 仅用于账号名称；保存未改动的表单时通过 `accountNameFromInput()` 保留原始值，切换语言不会重命名账号。其他自定义姓名保持原文。

## 新增或修改文案

中文原文作为消息键，英文翻译保存在 `static/locales/en.json`。浏览器通过 `static/i18n.js` 使用 `t()`；HTML 属性里的消息使用 `ta()`，它会转义翻译中的引号。参数按 `{0}`、`{1}` 编号，可以在译文中调整顺序。

```js
import { t, ta, getLocale } from "./i18n.js?v=0.29.3";

button.textContent = t("重新加载");
const message = t("缺少必填属性：{0}", [attributeName]);
const markup = `<button title="${ta("打开项目 {0}", [escapeHtml(project.name)])}">${t("查看项目")}</button>`;
const timestamp = new Date(value).toLocaleString(getLocale());
```

`t()` 返回普通字符串，不自动转义参数。写入 HTML 时，用户数据必须继续经过原有的 `escapeHtml()`；写入 `textContent` 时使用原始文本。不要把用户输入当成翻译键，也不要翻译 API 枚举、CSS 类名、快捷键代码或持久化字段。

修改 JSON 后运行 `python scripts/build_locales.py`，生成浏览器直接导入的 `static/locales/en.js`，以及前后端共用的 `static/locales/api.json` / `api.js` 系统消息规则。生成文件都应提交，无需安装前端构建工具或运行翻译服务。

后端的 `app/i18n.py` 根据 `Accept-Language` 在 JSON 响应边界翻译明确的系统展示字段，并返回 `Content-Language` 和 `Vary: Accept-Language`。数据库、后台任务和审计记录保持原始值；同一条记录可被不同语言的用户同时查看。新增系统字段时更新字段规则，避免把用户填写的 `reason`、`description` 等内容纳入翻译。下载附件、模型文件和压缩响应直接透传。

前端的 `static/api-i18n.js` 在读取接口响应时执行相同的展示规则，兼容尚未重启的旧后端返回的中文消息。生成的任务块名称、内置模型名称、转换格式说明、部署限制和错误嵌套消息均需覆盖。动态消息模板只取自后端，`MESSAGE_SLOTS` 明确指定哪些参数属于可翻译的系统消息，其余参数保持原文；不要对所有参数递归翻译。文件选择控件使用应用自己的文字，避免浏览器语言与应用语言不同造成残留。

新增语言时需要扩展前端支持语言和目录加载、后端语言协商及对应目录，并增加两端测试。

## 验证

```bash
python scripts/build_locales.py
node scripts/check_i18n.mjs
node scripts/check_api_i18n.mjs
python -m unittest tests.test_i18n tests.test_i18n_catalog -v
# 需要 Playwright 和 Chromium；使用临时数据库，不启动模型任务。
python scripts/smoke_i18n_browser.py --chromium /path/to/chrome
```

目录测试检查前后端消息覆盖、英文残留中文、参数占位符和生成文件一致性。浏览器测试在中文浏览器中选择英文，使用包含任务块、模型、设备、数据版本、AI 任务、训练和转换日志的数据检查各工作页面；同时测试当前后端响应和旧后端中文响应，检查表单、弹窗、登录、注册、初始化、语言切换及未保存内容保护。中文用户内容刻意使用与翻译键相同的文本，验证不会被误译。现有前端回归使用生产翻译函数固定在中文环境运行。
