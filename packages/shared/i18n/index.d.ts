export type Locale = "zh-CN" | "zh-TW" | "en" | "ja" | "ko" | "de" | "fr" | "es" | "pt-BR" | "ru";
export type LanguagePreference = "auto" | Locale;
export type Values = Record<string | number, unknown>;
export type Translator = (source: string, values?: Values) => string;
export interface I18n {
  languages: [Locale, string][];
  resolveLocale(preference?: string, systemLanguages?: readonly string[]): Locale;
  getLocale(): Locale;
  setLocale(preference: string, systemLanguages?: readonly string[]): Locale;
  subscribe(listener: () => void): () => void;
  t: Translator;
  html(source: string): string;
  localized<T>(value: T, translate?: Translator): T;
  escapeHtml(value: unknown): string;
}
export function createI18n(locale?: string, dictionaries?: Partial<Record<Locale, Record<string, string>>>): I18n;
export const languages: I18n["languages"];
export const resolveLocale: I18n["resolveLocale"];
export const getLocale: I18n["getLocale"];
export const setLocale: I18n["setLocale"];
export const subscribe: I18n["subscribe"];
export const t: Translator;
export const html: I18n["html"];
export const localized: I18n["localized"];
export const escapeHtml: I18n["escapeHtml"];
export const dictionaries: Partial<Record<Locale, Record<string, string>>>;
export function normalizeLanguage(value: unknown): LanguagePreference;
export function webviewLocaleData(locale?: Locale): string;
