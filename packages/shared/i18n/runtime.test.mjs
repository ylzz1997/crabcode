import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createI18n, dictionaries, languages, normalizeLanguage, webviewLocaleData } from './index.js';

test('system language negotiation respects scripts, regional variants and supported fallbacks', () => {
  const { resolveLocale } = createI18n();
  for (const [input, expected] of [['zh-Hans-HK', 'zh-CN'], ['zh-Hant-CN', 'zh-TW'], ['zh-HK', 'zh-TW'], ['zh_SG', 'zh-CN'], ['pt-PT', 'pt-BR'], ['en-GB', 'en'], ['fr-CA', 'fr'], ['ru-RU', 'ru']]) assert.equal(resolveLocale(input), expected);
  assert.equal(resolveLocale('auto', ['nl-NL', 'ko-KR']), 'ko');
  assert.equal(resolveLocale('auto', ['nl-NL']), 'en');
  assert.equal(resolveLocale('ja', ['en-US']), 'ja');
  assert.equal(normalizeLanguage('bad'), 'auto');
  assert.equal(normalizeLanguage(undefined), 'auto');
});

test('all ten languages use reviewed core labels and preserve native language names', () => {
  const expected = { 'zh-CN': '允许', 'zh-TW': '允許', en: 'Allow', ja: '許可', ko: '허용', de: 'Zulassen', fr: 'Autoriser', es: 'Permitir', 'pt-BR': 'Permitir', ru: 'Разрешить' };
  assert.equal(languages.length, 10);
  for (const [locale] of languages) {
    const i18n = createI18n(locale, dictionaries);
    assert.equal(i18n.t('允许'), expected[locale]);
    assert.equal(i18n.localized(languages)[0][1], '简体中文');
  }
});

test('fallback and interpolation do not translate or recursively expand user content', () => {
  const i18n = createI18n('ja', { en: { '路径 {0}：{1}': 'Path {0}: {1}' } });
  const userText = '保存 <script>\" {1} /tmp/中文.md';
  assert.equal(i18n.t('路径 {0}：{1}', { 0: userText, 1: '$&' }), `Path ${userText}: $&`);
  assert.equal(i18n.t('Unregistered diagnostic'), 'Unregistered diagnostic');
});

test('language changes update static labels without changing data IDs or cloning behavior', () => {
  const i18n = createI18n('zh-CN', dictionaries);
  const value = i18n.localized([{ id: 'save', label: '保存', options: [['auto', '跟随系统']] }]);
  let updates = 0;
  const unsubscribe = i18n.subscribe(() => updates++);
  i18n.setLocale('de');
  assert.equal(value[0].label, 'Speichern');
  assert.equal(value[0].id, 'save');
  assert.equal(structuredClone(value)[0].label, 'Speichern');
  i18n.setLocale('de');
  assert.equal(updates, 1);
  unsubscribe();
  i18n.setLocale('ja');
  assert.equal(value[0].label, '保存');
  assert.equal(updates, 1);
});

test('HTML localization escapes catalog text and webview payloads cannot terminate a script', () => {
  const i18n = createI18n('en', { en: { '保存': '<img src=x onerror=alert(1)>"' } });
  assert.equal(i18n.html('<button title="保存">保存</button>'), '<button title="&lt;img src=x onerror=alert(1)&gt;&quot;">&lt;img src=x onerror=alert(1)&gt;&quot;</button>');
  dictionaries.en['security-test'] = '</script><script>alert(1)</script>\u2028';
  try {
    const serialized = webviewLocaleData('en');
    assert.ok(!serialized.includes('</script>'));
    assert.equal(JSON.parse(serialized).dictionaries.en['security-test'], dictionaries.en['security-test']);
  } finally { delete dictionaries.en['security-test']; }
});

test('partial HTML attribute fragments preserve structural quotes in every locale', () => {
  for (const [locale] of languages) {
    const i18n = createI18n(locale, dictionaries);
    const fragment = i18n.html('" role="button" tabindex="0" aria-label="放大查看：');
    assert.ok(fragment.startsWith('" role="button" tabindex="0" aria-label="'));
    assert.ok(!fragment.includes('&quot; role'));
  }
});
