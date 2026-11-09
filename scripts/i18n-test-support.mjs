// Existing UI regression fixtures exercise the original Chinese behavior. Import
// the production formatter instead of mocking translations or browser locales.
import * as i18n from '../static/i18n.js?v=0.29.3';
import * as apiI18n from '../static/api-i18n.js?v=0.29.3';
i18n.setLocale('zh-CN');
export const withI18n = sandbox => Object.assign(sandbox, i18n, apiI18n);
export const stripI18nImports = source => source.replace(/^import .*from ["']\.\/(?:api-)?i18n\.js[^\n]*\n/gm, '');
