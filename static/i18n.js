import messages from "./locales/en.js?v=0.29.3";

// Source-language keys keep the Chinese copy readable at the call site. Only
// explicit messages are translated: user names, labels, prompts and data are not.
export const locales = Object.freeze({ "zh-CN": "简体中文", en: "English" });
const storageKey = "cvsprout-locale";
const normalize = value => /^zh(?:-|$)/i.test(value || "") ? "zh-CN" : /^en(?:-|$)/i.test(value || "") ? "en" : null;
const browserLocale = () => {
  for (const value of globalThis.navigator?.languages || [globalThis.navigator?.language]) {
    const supported = normalize(value);
    if (supported) return supported;
  }
  return "zh-CN";
};
function initialLocale() {
  const requested = normalize(new URLSearchParams(globalThis.location?.search || "").get("lang"));
  if (requested) return requested;
  try { const saved = normalize(globalThis.localStorage?.getItem(storageKey)); if (saved) return saved; } catch { /* Private browsing. */ }
  return browserLocale();
}
let locale = initialLocale();
export const getLocale = () => locale;

const translation = source => locale === "en" && Object.hasOwn(messages, source) ? messages[source] : source;
const interpolate = (message, values) => message.replace(/\{(\d+)\}/g, (token, index) => index in values ? String(values[index] ?? "") : token);
export const t = (source, values = []) => interpolate(translation(source), values);

// Setup and the CLI historically saved this default as the account's name.
// Translate it only at account-name display sites; other names remain user data.
export const accountDisplayName = name => name === "管理员" || name === "Administrator" ? t("管理员") : name || "";

// Saving an unchanged localized default must not rename the stored account.
export const accountNameFromInput = (value, original = "") => value.trim() === accountDisplayName(original) ? original : value.trim();

// HTML attribute templates already escape their dynamic values at the call site.
// Escape the catalog copy too, since English quotes can differ from Chinese ones.
export const ta = (source, values = []) => interpolate(translation(source).replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[char]), values);

function applyDocumentLanguage() {
  if (typeof document === "undefined") return;
  document.documentElement.lang = locale;
  document.title = t("CVSprout · 智能标注平台");
}

export function setLocale(value) {
  const next = normalize(value);
  if (!next || next === locale) return false;
  locale = next;
  try { globalThis.localStorage?.setItem(storageKey, locale); } catch { /* Still usable for this page. */ }
  // The URL also makes switching work when the browser blocks localStorage.
  if (globalThis.location && globalThis.history?.replaceState) {
    const url = new URL(location.href);
    url.searchParams.set("lang", locale);
    history.replaceState(history.state, "", url);
  }
  applyDocumentLanguage();
  return true;
}

export function languageControlsMarkup(prefix) {
  return `<label for="${prefix}-language-select">${t("界面语言")}</label><select id="${prefix}-language-select" data-language-select aria-label="${t("界面语言")}">${Object.entries(locales).map(([value, name]) => `<option value="${value}" lang="${value}" ${value === locale ? "selected" : ""}>${name}</option>`).join("")}</select>`;
}

export function languageSwitcherMarkup(prefix) {
  return `<div class="language-switcher" title="${ta("界面语言")}"><svg class="language-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/></svg>${languageControlsMarkup(prefix)}<svg class="language-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg></div>`;
}

// Native file-picker captions follow the browser's UI language, which can differ
// from the chosen app language. Keep the accessible native input with our text.
export function fileInputMarkup(id, accept, required = false) {
  return `<span class="localized-file-input"><input id="${id}" type="file" accept="${accept}" ${required ? "required" : ""} data-localized-file aria-describedby="${id}-selection" title="${ta("选择文件")}"><span class="localized-file-button" aria-hidden="true">${t("选择文件")}</span><span id="${id}-selection" data-file-selection aria-live="polite">${t("尚未选择文件")}</span></span>`;
}

// A reload re-evaluates module-level labels and preserves the current URL. The
// application can cancel this event when it has an unsaved draft or active form.
export function installLanguageControls() {
  const validationMessages = new WeakSet();
  document.addEventListener("invalid", event => {
    const field = event.target, validity = field.validity;
    if (!validity || validity.customError && !validationMessages.has(field)) return;
    const message = validity.valueMissing ? t("请填写此字段。")
      : validity.typeMismatch ? t("请输入有效的{0}。", [field.type === "email" ? t("电子邮箱地址") : t("网址")])
      : validity.rangeUnderflow ? t("请输入不小于 {0} 的数值。", [field.min])
      : validity.rangeOverflow ? t("请输入不大于 {0} 的数值。", [field.max])
      : validity.tooShort ? t("请至少输入 {0} 个字符。", [field.minLength])
      : validity.tooLong ? t("请最多输入 {0} 个字符。", [field.maxLength])
      : validity.badInput || validity.stepMismatch ? t("请输入有效的数值。")
      : t("请按要求的格式填写此字段。");
    field.setCustomValidity(message);
    validationMessages.add(field);
  }, true);
  const clearValidation = event => {
    if (validationMessages.has(event.target)) {
      event.target.setCustomValidity("");
      validationMessages.delete(event.target);
    }
  };
  document.addEventListener("input", clearValidation);
  document.addEventListener("change", clearValidation);
  document.addEventListener("change", event => {
    if (!event.target.matches?.("[data-localized-file]")) return;
    const text = [...event.target.files].map(file => file.name).join(", ") || t("尚未选择文件");
    event.target.parentElement.querySelector("[data-file-selection]").textContent = text;
    event.target.title = text;
  });
  document.addEventListener("change", event => {
    if (!event.target.matches?.("[data-language-select]")) return;
    const next = normalize(event.target.value);
    if (!next || next === locale) return;
    const request = new CustomEvent("cvsprout-before-language-change", { cancelable: true });
    if (!window.dispatchEvent(request)) { event.target.value = locale; return; }
    if (setLocale(next)) window.location.reload();
  });
}

applyDocumentLanguage();
