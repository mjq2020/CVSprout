// Loaded before styles so a saved dark theme also applies to the first paint.
(() => {
  'use strict';
  if (window.CVSproutTheme) return;

  const storageKey = 'cvsprout-theme';
  const normalize = value => ['light', 'dark', 'system'].includes(value) ? value : 'system';
  const media = window.matchMedia?.('(prefers-color-scheme: dark)');
  let storage;
  let preference = 'system';
  let theme;
  try {
    storage = window.localStorage;
    preference = normalize(storage.getItem(storageKey));
  } catch { /* Private browsing may disable storage; the current tab still works. */ }

  function apply(nextPreference) {
    const previousPreference = preference;
    const previousTheme = theme;
    preference = normalize(nextPreference);
    theme = preference === 'system' ? (media?.matches ? 'dark' : 'light') : preference;
    const root = document.documentElement;
    root.dataset.theme = theme;
    root.dataset.themePreference = preference;
    root.style.colorScheme = theme;
    if (previousPreference !== preference || previousTheme !== theme) {
      window.dispatchEvent(new CustomEvent('cvsprout-theme-change', { detail: { preference, theme } }));
    }
  }

  window.CVSproutTheme = Object.freeze({
    getPreference: () => preference,
    getTheme: () => theme,
    setPreference(value) {
      apply(value);
      try { storage?.setItem(storageKey, preference); } catch { /* Keep the in-memory choice. */ }
    },
  });

  const systemChanged = () => { if (preference === 'system') apply(preference); };
  if (media?.addEventListener) media.addEventListener('change', systemChanged);
  else media?.addListener?.(systemChanged);
  window.addEventListener('storage', event => {
    if (storage && event.storageArea === storage && (event.key === storageKey || event.key === null)) {
      apply(event.key === null ? 'system' : event.newValue);
    }
  });
  apply(preference);
})();
