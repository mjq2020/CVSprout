import messages from "./locales/en.js?v=0.29.3";
import rules from "./locales/api.js?v=0.29.3";
import { getLocale } from "./i18n.js?v=0.29.3";

// Presentation only: never translate arbitrary DOM text or persisted user data.
// These rules/templates are generated from the backend, not maintained twice.
const han = /[\u3400-\u9fff]/;
const slot = /\{(\d+)\}/g;
const literalMessages = new Map(Object.entries(messages).filter(([key]) => !/\{\d+\}/.test(key)).map(([key, value]) => [key.trim(), value.trim()]));
const prefixes = [...literalMessages].filter(([key]) => key.length >= 6 && /[。；]$/.test(key)).sort((a, b) => b[0].length - a[0].length);
const escapePattern = text => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const patterns = rules.templates.map(source => {
  const slots = [...source.matchAll(slot)].map(match => match[1]);
  const literals = source.split(/\{\d+\}/);
  const pattern = new RegExp("^" + literals.map(escapePattern).join("([\\s\\S]*?)") + "$(?![\\s\\S])");
  return { source, slots, literals: literals.filter(Boolean), pattern };
});

export function translateMessage(message, locale = getLocale()) {
  if (typeof message !== "string" || locale !== "en" || !han.test(message)) return message;
  if (Object.hasOwn(messages, message)) return messages[message];
  const stripped = message.trim();
  if (literalMessages.has(stripped)) return literalMessages.get(stripped);
  for (const [source, target] of prefixes) {
    if (stripped.startsWith(source) && stripped.length > source.length) return target + " " + translateMessage(stripped.slice(source.length).trim(), locale);
  }
  for (const { source, slots, literals, pattern } of patterns) {
    if (!literals.every(part => message.includes(part))) continue;
    const match = message.match(pattern);
    if (!match) continue;
    const values = Object.fromEntries(slots.map((index, offset) => {
      const value = match[offset + 1];
      return [index, rules.message_slots[source]?.includes(Number(index)) && value !== message ? translateMessage(value, locale) : value];
    }));
    return messages[source].replace(slot, (token, index) => values[index] ?? token);
  }
  const diagnostic = message.match(/^((?:[A-Za-z_][\w.]*?(?:Error|Exception)|Error|Exception):\s*)([\s\S]+)$/);
  if (diagnostic) return diagnostic[1] + translateMessage(diagnostic[2], locale);
  const pieces = message.split(/(?<=[。；\n])/).map(part => part.trim()).filter(Boolean);
  return pieces.length > 1 ? pieces.map(part => translateMessage(part, locale)).join(" ") : message;
}

export function localizePayload(payload, path, status = 200, locale = getLocale()) {
  path = path.split("?", 1)[0];
  const metadataRoute = ["/api/training/catalog", "/api/training/resources", "/api/model-downloads"].includes(path);
  const compatibilityRoute = ["/training-compatibility", "/conversion-options", "/deployment-options", "/system-models"].some(suffix => path.endsWith(suffix));
  const translate = value => translateMessage(value, locale);
  function walk(value, parents = [], system = false) {
    if (Array.isArray(value)) return value.map(item => walk(item, parents, system));
    if (!value || typeof value !== "object") return value;
    const builtin = value.builtin === true || value.source === "builtin";
    return Object.fromEntries(Object.entries(value).map(([key, item]) => {
      const owned = system || rules.system_containers.includes(key);
      if (key === "name" && path.includes("/work-blocks") && typeof item === "string" && /^任务块 \d+$/.test(item)) return [key, translate(item)];
      if (key === "badge" && ["自定义", "Custom"].includes(item) && "provider_type" in value) return [key, locale === "zh-CN" ? "自定义" : "Custom"];
      if ((key === "warnings" || key === "capabilities" && builtin) && Array.isArray(item)) return [key, item.map(translate)];
      if (status >= 400 && key === "detail" && typeof item === "string") return [key, translate(item)];
      if (typeof item === "string" && (
        rules.message_fields.includes(key)
        || key === "msg" && parents.includes("detail")
        || key === "reason" && (system || metadataRoute || compatibilityRoute || "available" in value || "compatible" in value)
        || ["name", "family", "description", "badge", "label"].includes(key) && (
          builtin || system && key !== "name" || parents.includes("resource_profiles") || parents.includes("tasks") || path === "/api/model-downloads"
          || parents.includes("operations") && path.endsWith("/augmentation-options"))
        || parents.includes("licensing")
      )) return [key, translate(item)];
      return [key, rules.user_fields.includes(key) ? item : walk(item, [...parents, key], owned)];
    }));
  }
  return walk(payload);
}
