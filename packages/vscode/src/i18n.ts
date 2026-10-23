import * as vscode from "vscode";
import { localized as localize, resolveLocale, setLocale, t as translate, html as translateHtml, type Translator } from "../../shared/i18n/index.js";
export { createI18n, webviewLocaleData, languages, normalizeLanguage } from "../../shared/i18n/index.js";
export function getLocale() {
  return resolveLocale(vscode.workspace.getConfiguration("crabcode").get("language", "auto"), [vscode.env?.language ?? "zh-CN"]);
}
export function syncLanguage() { return setLocale(getLocale()); }
export const t: Translator = (source, values) => { syncLanguage(); return translate(source, values); };
export const html = (source: string) => { syncLanguage(); return translateHtml(source); };
export const localized = <T>(value: T): T => localize(value, t);
