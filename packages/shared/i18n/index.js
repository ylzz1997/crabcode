import { createI18n } from "./runtime.js";
import en from "./locales/en.json" with { type: "json" };
import zhTW from "./locales/zh-TW.json" with { type: "json" };
import ja from "./locales/ja.json" with { type: "json" };
import ko from "./locales/ko.json" with { type: "json" };
import de from "./locales/de.json" with { type: "json" };
import fr from "./locales/fr.json" with { type: "json" };
import es from "./locales/es.json" with { type: "json" };
import ptBR from "./locales/pt-BR.json" with { type: "json" };
import ru from "./locales/ru.json" with { type: "json" };
export { createI18n };
export const dictionaries = { en, "zh-TW": zhTW, ja, ko, de, fr, es, "pt-BR": ptBR, ru };
// Clients resolve their system language explicitly before mounting UI. Chinese preserves source fixtures.
const runtime = createI18n("zh-CN", dictionaries);
export const { languages, resolveLocale, getLocale, setLocale, subscribe, t, html, localized, escapeHtml } = runtime;
export function normalizeLanguage(value) {
  return value === "auto" || languages.some(([code]) => code === value) ? value : "auto";
}
/** Safe even when a catalog contains script-looking text. */
export function webviewLocaleData(locale = getLocale()) {
  return JSON.stringify({ locale, dictionaries: { en, [locale]: dictionaries[locale] } }).replace(/</g, "\\u003c").replace(/\u2028/g, "\\u2028").replace(/\u2029/g, "\\u2029");
}
