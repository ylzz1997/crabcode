/** Dependency-free runtime. Keep the factory self-contained: VS Code embeds it under a CSP nonce. */
export function createI18n(initialLocale = "en", dictionaries = {}) {
  const languages = [
    ["zh-CN", "简体中文"], ["zh-TW", "繁體中文"], ["en", "English"],
    ["ja", "日本語"], ["ko", "한국어"], ["de", "Deutsch"], ["fr", "Français"],
    ["es", "Español"], ["pt-BR", "Português (Brasil)"], ["ru", "Русский"],
  ];
  function resolveLocale(preference, systemLanguages = []) {
    const candidates = preference && preference !== "auto" ? [preference] : systemLanguages;
    for (const candidate of candidates) {
      const tag = String(candidate).replace(/_/g, "-").toLowerCase();
      if (tag === "zh" || tag.startsWith("zh-")) {
        if (/(?:^|-)hans(?:-|$)/.test(tag)) return "zh-CN";
        return /(?:^|-)(?:hant|tw|hk|mo)(?:-|$)/.test(tag) ? "zh-TW" : "zh-CN";
      }
      const language = tag.split("-")[0];
      if (language === "pt") return "pt-BR";
      if (languages.some(([code]) => code === language)) return language;
    }
    return "en";
  }
  let locale = resolveLocale(initialLocale);
  const listeners = new Set();
  const getLocale = () => locale;
  function setLocale(preference, systemLanguages = []) {
    const next = resolveLocale(preference, systemLanguages);
    if (next !== locale) {
      locale = next;
      listeners.forEach(listener => listener());
    }
    return locale;
  }
  function subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); }
  function t(source, values = {}) {
    const message = locale === "zh-CN" ? source : (dictionaries[locale]?.[source] ?? dictionaries.en?.[source] ?? source);
    // Replace in one pass: interpolated user content is never interpreted as another placeholder.
    return message.replace(/\{(\d+)\}/g, (token, key) => Object.prototype.hasOwnProperty.call(values, key) ? String(values[key]) : token);
  }
  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  }
  /** Only call with application-owned HTML literals, never a rendered message or user content. */
  function html(source) {
    const text = value => value.replace(/^(\s*)([\s\S]*?)(\s*)$/, (_, start, body, end) => start + (locale === "zh-CN" ? body : escapeHtml(t(body))) + end);
    // Concatenated markup can begin/end inside a tag or an attribute. Structural quotes must stay raw.
    const attributeFragment = /^[\s"']*[a-z][\w-]*\s*=/i.test(source);
    return source.replace(/(^|>)([^<>]*)(?=<|$)/g, (_, prefix, value) => prefix + ((!prefix && attributeFragment) || !/[\u3400-\u9fff]/.test(value) ? value : text(value)))
      .replace(/((?:title|placeholder|aria-label|alt)\s*=\s*)(["'])([\s\S]*?)(\2|$)/g,
        (_, prefix, quote, value, closing) => prefix + quote + (/[\u3400-\u9fff]/.test(value) ? text(value) : value) + closing);
  }
  /** Lazy getters keep static UI catalogs reactive without translating user data or using proxies. */
  function localized(value, translate = t) {
    if (!value || typeof value !== "object") return value;
    const result = Array.isArray(value) ? [] : {};
    for (const key of Object.keys(value)) {
      const item = value[key];
      if (typeof item === "string" && /[\u3400-\u9fff]/.test(item) && !languages.some(([, name]) => name === item)) {
        Object.defineProperty(result, key, { enumerable: true, configurable: true, get: () => translate(item) });
      } else result[key] = item && typeof item === "object" ? localized(item, translate) : item;
    }
    return result;
  }
  return { languages, resolveLocale, getLocale, setLocale, subscribe, t, html, localized, escapeHtml };
}
