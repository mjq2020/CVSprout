"""Localize presentation fields at the HTTP boundary, never persisted user data.

The same source-key catalog is used by the browser. Workers and database records
keep their canonical messages, so concurrent clients can choose different locales.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parents[1] / "static" / "locales" / "en.json"
HAN = re.compile(r"[\u3400-\u9fff]")
SLOT = re.compile(r"\{(\d+)\}")

# Only these slots contain other application messages. All other slots (names,
# labels, paths, user feedback) remain verbatim, even if they match a message key.
MESSAGE_SLOTS = {
    "checkpoint 无法加载：{0}": [0],
    "图片“{0}”无法增强：{1}": [1],
    "本地模型不可用，请检查文件权限和依赖：{0}": [0],
    "SAM2 本地模型不可用，请检查文件权限和依赖：{0}": [0],
    "SAM3 本地模型不可用，请检查文件权限和依赖：{0}": [0],
    "Qwen3.5 本地模型不可用：{0}": [0],
    "{0}；旧 SAM3 SDK 还缺少依赖：{1}": [0],
    "{0}；旧 SAM3 SDK 缺少内置 BPE 分词词表": [0],
    "下载失败，可重试续传：{0}: {1}": [1],
    "准备训练权重失败，可重试续传：{0}: {1}": [1],
    "TensorRT 无法重新读取导出引擎，产物未发布：{0}": [0],
    "{0}产物尚未生成": [0],
    "{0}产物文件不存在": [0],
    "{0}产物 SHA-256 校验失败，文件可能已损坏或被替换": [0],
    "{0}产物文件已被删除，请刷新后重试": [0],
    "当前训练任务为{0}，不支持 reCamera Pro 系统模型导出。{1}": [0, 1],
    "训练产物未记录模型架构，无法确认系统模型兼容性；不能仅凭训练名称或权重文件名判断。{0}请重新训练兼容模型后转换。": [0],
    "当前训练模型 {0} 尚未适配 reCamera Pro 系统模型导出。{1}请使用兼容模型重新训练，或选择通用 RKNN 并自行提供推理程序。": [1],
}


@lru_cache(maxsize=1)
def presentation_rules():
    return json.loads(CATALOG_PATH.with_name("api.json").read_text(encoding="utf-8"))


def negotiate_language(header: str) -> str:
    preferences = []
    for index, item in enumerate(header.split(",")):
        language, *parameters = item.strip().split(";")
        quality = 1.0
        for parameter in parameters:
            if parameter.strip().startswith("q="):
                try:
                    quality = float(parameter.strip()[2:])
                except ValueError:
                    quality = 0
        if not 0 < quality <= 1:
            continue
        base = language.lower().split("-")[0]
        if base in {"zh", "en"}:
            preferences.append((-quality, index, "zh-CN" if base == "zh" else "en"))
    return min(preferences)[2] if preferences else "zh-CN"


@lru_cache(maxsize=1)
def catalog():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def literal_messages():
    return {source.strip(): target.strip() for source, target in catalog().items() if not SLOT.search(source)}


@lru_cache(maxsize=1)
def sentence_prefixes():
    return sorted(((source, target) for source, target in literal_messages().items()
                   if len(source) >= 6 and source.endswith(("。", "；"))), key=lambda item: len(item[0]), reverse=True)


@lru_cache(maxsize=1)
def patterns():
    result = []
    for source in presentation_rules()["templates"]:
        target = catalog()[source]
        slots = list(SLOT.finditer(source))
        if not slots:
            continue
        cursor, chunks = 0, []
        for slot in slots:
            chunks.extend((re.escape(source[cursor:slot.start()]), "([\\s\\S]*?)"))
            cursor = slot.end()
        chunks.append(re.escape(source[cursor:]))
        literals = [part for part in SLOT.split(source)[::2] if part]
        result.append((len(SLOT.sub("", source)), re.compile("".join(chunks)), slots, target, source, literals))
    # Prefer specific messages over broad fragments such as "{0} failed".
    return sorted(result, key=lambda item: item[0], reverse=True)


def translate(message: str, locale: str = "en") -> str:
    if locale != "en" or not HAN.search(message):
        return message
    if message in catalog():
        return catalog()[message]
    stripped = message.strip()
    if stripped in literal_messages():
        return literal_messages()[stripped]
    for source, target in sentence_prefixes():
        if stripped.startswith(source) and len(stripped) > len(source):
            return target + " " + translate(stripped[len(source):].strip(), locale)
    for _, pattern, slots, target, source, literals in patterns():
        # Avoid backtracking through unrelated long messages or log output.
        if not all(part in message for part in literals):
            continue
        match = pattern.fullmatch(message)
        if match:
            values = {slot.group(1): translate(value, locale) if int(slot.group(1)) in MESSAGE_SLOTS.get(source, []) and value != message else value
                      for slot, value in zip(slots, match.groups())}
            return SLOT.sub(lambda slot: values.get(slot.group(1), slot.group()), target)
    # Plans can concatenate several independently authored sentences. Translate
    # only complete known sentences, leaving technical details intact.
    diagnostic = re.fullmatch(r"((?:[A-Za-z_][\w.]*?(?:Error|Exception)|Error|Exception):\s*)([\s\S]+)", message)
    if diagnostic:
        return diagnostic[1] + translate(diagnostic[2], locale)
    pieces = [piece.strip() for piece in re.split(r"(?<=[。；\n])", message) if piece.strip()]
    if len(pieces) > 1:
        return " ".join(translate(piece, locale) for piece in pieces)
    return message


def validation_message(error: dict, locale: str) -> str:
    """Keep validation inputs private and localize standard Pydantic messages."""
    message = error["msg"].removeprefix("Value error, ")
    if HAN.search(message):
        return translate(message, locale)
    if locale == "en":
        return message
    kind, context = error["type"], error.get("ctx", {})
    simple = {
        "missing": "此字段为必填项。", "string_type": "请输入文本。",
        "int_type": "请输入整数。", "int_parsing": "请输入整数。",
        "float_type": "请输入有效的数值。", "float_parsing": "请输入有效的数值。",
        "finite_number": "数值必须是有限值。", "bool_type": "请选择是或否。",
        "bool_parsing": "请选择是或否。", "list_type": "请提供列表。",
        "dict_type": "请提供对象。", "string_pattern_mismatch": "请按要求的格式填写此字段。",
        "extra_forbidden": "包含不支持的字段。", "literal_error": "请选择允许的选项。",
    }
    bounds = {
        "string_too_short": ("请至少输入 {0} 个字符。", "min_length"),
        "string_too_long": ("请最多输入 {0} 个字符。", "max_length"),
        "greater_than_equal": ("请输入不小于 {0} 的数值。", "ge"),
        "less_than_equal": ("请输入不大于 {0} 的数值。", "le"),
        "greater_than": ("请输入大于 {0} 的数值。", "gt"),
        "less_than": ("请输入小于 {0} 的数值。", "lt"),
        "too_short": ("请至少提供 {0} 项。", "min_length"),
        "too_long": ("请最多提供 {0} 项。", "max_length"),
    }
    if kind in bounds:
        source, key = bounds[kind]
        return source.replace("{0}", str(context.get(key, "")))
    return simple.get(kind, "字段值无效，请检查后重试。")


# These fields contain application messages. User-authored content is excluded
# even when a label's attribute happens to be named "error" or "message".
MESSAGE_FIELDS = {
    "error", "message", "progress_message", "availability_error", "last_health_error",
    "instructions", "compatibility_message", "error_message", "blocked_reason",
}
USER_FIELDS = {
    "attributes", "attributes_json", "settings", "config", "keypoint_schema",
    "keypoint_schema_json", "labels", "annotations", "note", "review_note",
    "prompt", "description", "display_name", "filename", "points", "keypoints",
    "manifest", "metadata", "payload", "request", "response", "input",
}
SYSTEM_CONTAINERS = {
    "availability", "preparation", "compatibility", "licensing", "conversion_formats", "formats",
    "resource_profiles", "backend", "calibration", "system_model",
}


def localize_payload(payload, path: str, status: int, locale: str):
    path = path.split("?", 1)[0]
    metadata_route = path in {"/api/training/catalog", "/api/training/resources", "/api/model-downloads"}
    compatibility_route = path.endswith(("/training-compatibility", "/conversion-options", "/deployment-options", "/system-models"))

    def walk(value, parents=(), system=False):
        if isinstance(value, list):
            return [walk(item, parents, system) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        builtin = value.get("builtin") is True or value.get("source") == "builtin"
        for key, item in value.items():
            owned = system or key in SYSTEM_CONTAINERS
            if key == "name" and "/work-blocks" in path and isinstance(item, str) and re.fullmatch(SLOT.sub(lambda _: r"\d+", "任务块 {0}"), item):
                result[key] = translate(item, locale)
            elif key == "badge" and isinstance(item, str) and item in {"自定义", "Custom"} and "provider_type" in value:
                result[key] = "自定义" if locale == "zh-CN" else "Custom"
            elif (key == "warnings" or key == "capabilities" and builtin) and isinstance(item, list):
                result[key] = [translate(capability, locale) if isinstance(capability, str) else capability for capability in item]
            elif status >= 400 and key == "detail" and isinstance(item, str):
                result[key] = translate(item, locale)
            elif isinstance(item, str) and (
                key in MESSAGE_FIELDS
                or key == "msg" and "detail" in parents
                or key == "reason" and (system or metadata_route or compatibility_route or "available" in value or "compatible" in value)
                or key in {"name", "family", "description", "badge", "label"} and (
                    builtin or system and key != "name" or "resource_profiles" in parents
                    or "tasks" in parents or path == "/api/model-downloads"
                    or "operations" in parents and path.endswith("/augmentation-options"))
                or "licensing" in parents
            ):
                result[key] = translate(item, locale)
            elif key in USER_FIELDS:
                result[key] = item
            else:
                result[key] = walk(item, (*parents, key), owned)
        return result

    return walk(payload)


class LocaleMiddleware:
    """Translate JSON only. Binary downloads and streamed model files pass through."""

    def __init__(self, app, prefix="/api/"):
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(self.prefix):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        locale = negotiate_language(headers.get(b"accept-language", b"").decode("latin1"))
        start = None
        body = bytearray()

        async def localized_send(message):
            nonlocal start
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_meta = dict(response_headers)
                content_type = response_meta.get(b"content-type", b"")
                if (b"application/json" not in content_type
                        or b"attachment" in response_meta.get(b"content-disposition", b"")
                        or response_meta.get(b"content-encoding")):
                    await send(message)
                    return
                response_headers.append((b"content-language", locale.encode()))
                response_headers.append((b"vary", b"Accept-Language"))
                message = {**message, "headers": response_headers}
                if locale == "en":
                    start = message
                else:
                    await send(message)
            elif message["type"] == "http.response.body" and start is not None:
                body.extend(message.get("body", b""))
                if message.get("more_body", False):
                    return
                try:
                    data = localize_payload(json.loads(body), scope["path"], start["status"], locale)
                    translated = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                except (ValueError, UnicodeError):
                    translated = bytes(body)
                start["headers"] = [(key, value) for key, value in start["headers"] if key.lower() != b"content-length"]
                start["headers"].append((b"content-length", str(len(translated)).encode()))
                await send(start)
                await send({**message, "body": translated})
            else:
                await send(message)

        await self.app(scope, receive, localized_send)
