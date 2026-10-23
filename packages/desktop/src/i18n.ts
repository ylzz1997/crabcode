import { useSyncExternalStore } from "react";
import { getLocale, setLocale, subscribe } from "../../shared/i18n/index.js";
export * from "../../shared/i18n/index.js";
export function useLocale() { return useSyncExternalStore(subscribe, getLocale, getLocale); }
export function applyLanguage(preference = "auto") {
  const locale = setLocale(preference, typeof navigator === "undefined" ? [] : navigator.languages);
  if (typeof document !== "undefined") document.documentElement.lang = locale;
  return locale;
}

export { configurationSourceLabel, promptSectionLabel } from "../../shared/i18n/metadata.js";
