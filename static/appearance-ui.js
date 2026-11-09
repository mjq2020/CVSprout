import { t } from "./i18n.js?v=0.29.3";
export const themeLabels = { light: t("浅色"), dark: t("深色"), system: t("跟随系统") };
export const styleLabels = { forest: t("松林"), ocean: t("深海"), sand: t("暖砂") };
export const themePreference = () => window.CVSproutTheme?.getPreference?.() || 'system';
export const stylePreference = () => window.CVSproutTheme?.getStyle?.() || 'forest';

// Shared by the login entrance and workspace; changes never rerender a form.
export function appearanceControlsMarkup(prefix) {
  const options = (labels, selected) => Object.entries(labels).map(([value, label]) => `<option value="${value}" ${selected === value ? 'selected' : ''}>${label}</option>`).join('');
  return `<div class="appearance-fields"><label for="${prefix}-style-select">${t("界面风格")}</label><select id="${prefix}-style-select" data-style-select>${options(styleLabels, stylePreference())}</select><label for="${prefix}-theme-select">${t("明暗模式")}</label><select id="${prefix}-theme-select" data-theme-select>${options(themeLabels, themePreference())}</select></div>`;
}
