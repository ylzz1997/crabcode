/** Extract/check application-owned messages and generate VS Code native language packs. No network access. */
import { readFileSync, writeFileSync, readdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const ts = require(require.resolve('typescript', { paths: [path.join(root, 'packages/desktop'), path.join(root, 'packages/vscode')] }));
const directory = path.join(root, 'packages/shared/i18n/locales');
const locales = ['zh-CN', 'zh-TW', 'en', 'ja', 'ko', 'de', 'fr', 'es', 'pt-BR', 'ru'];
const nativeNames = new Set(['简体中文', '繁體中文', 'English', '日本語', '한국어', 'Deutsch', 'Français', 'Español', 'Português (Brasil)', 'Русский']);
const read = file => JSON.parse(readFileSync(file, 'utf8'));
const save = (file, data) => writeFileSync(file, JSON.stringify(Object.fromEntries(Object.entries(data).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)), null, 2) + '\n');
const messages = new Set();
function htmlKeys(value) {
  const attributeFragment = /^[\s"']*[a-z][\w-]*\s*=/i.test(value);
  value.replace(/(^|>)([^<>]*)(?=<|$)/g, (_, prefix, text) => { if ((prefix || !attributeFragment) && /[\u3400-\u9fff]/.test(text)) messages.add(text.trim()); });
  value.replace(/((?:title|placeholder|aria-label|alt)\s*=\s*)(["'])([\s\S]*?)(\2|$)/g, (_, prefix, quote, text) => { if (/[\u3400-\u9fff]/.test(text)) messages.add(text.trim()); });
}
function collect(source, file) {
  const parsed = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  function literals(node, callback) {
    if (ts.isStringLiteralLike(node)) callback(node.text);
    else if (ts.isTemplateExpression(node)) { callback(node.head.text); for (const span of node.templateSpans) callback(span.literal.text); }
    else if (ts.isPropertyAssignment(node)) literals(node.initializer, callback);
    else ts.forEachChild(node, child => literals(child, callback));
  }
  function visit(node) {
    if (ts.isCallExpression(node)) {
      const name = node.expression.getText(parsed);
      if (['tr', 't'].includes(name) && ts.isStringLiteralLike(node.arguments[0])) messages.add(node.arguments[0].text);
      if (name === 'trHtml') literals(node.arguments[0], htmlKeys);
      if (name === 'localized') literals(node.arguments[0], text => { if (/[\u3400-\u9fff]/.test(text) && !nativeNames.has(text)) messages.add(text); });
    }
    // The chat browser script is intentionally embedded under a nonce.
    if (ts.isTemplateExpression(node) && node.templateSpans.some(span => span.literal.text.includes('acquireVsCodeApi()'))) {
      const cooked = node.head.text + node.templateSpans.map((span, index) => `__HOST_${index}__` + span.literal.text).join('');
      for (const match of cooked.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)) {
        if (match[1].trim()) collect(match[1], 'chat-browser.js');
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(parsed);
}
function walk(directory) {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) walk(file);
    else if (/\.tsx?$/.test(file) && !/(\.test\.|\.d\.ts$)/.test(file)) collect(readFileSync(file, 'utf8'), file);
  }
}
walk(path.join(root, 'packages/desktop/src'));
walk(path.join(root, 'packages/vscode/src'));
for (const relative of ['packages/shared/slashCommands.js', 'packages/shared/i18n/metadata.js', 'packages/computer-use/client/vscodeHost.js']) collect(readFileSync(path.join(root, relative), 'utf8'), relative);
// Native capability hint is application-owned; the rest of native diagnostics stay untouched.
const channel = readFileSync(path.join(root, 'packages/computer-use/client/src/channel.ts'), 'utf8');
for (const match of channel.matchAll(/return "([^"\n]*[\u3400-\u9fff][^"\n]*)";/g)) messages.add(match[1]);
const manifests = ['packages/vscode', 'packages/vscode/computer-use'].map(relative => ({ directory: path.join(root, relative), source: read(path.join(root, relative, 'package.nls.zh-cn.json')) }));
for (const manifest of manifests) for (const value of Object.values(manifest.source)) messages.add(value);
const source = Object.fromEntries([...messages].map(message => [message, message]));
if (process.argv.includes('--extract')) {
  save(path.join(directory, 'zh-CN.json'), source);
  for (const locale of locales.filter(locale => locale !== 'zh-CN')) {
    const file = path.join(directory, locale + '.json');
    save(file, Object.fromEntries(Object.entries(read(file)).filter(([key]) => messages.has(key))));
  }
  console.log(`Extracted ${messages.size} application messages.`);
} else {
  const errors = [];
  for (const manifest of manifests) {
    const inspect = value => {
      if (typeof value === 'string') {
        const key = /^%([^%]+)%$/.exec(value)?.[1];
        if (key && typeof manifest.source[key] !== 'string') errors.push(`${path.relative(root, manifest.directory)}: missing native message ${key}`);
      } else if (value && typeof value === 'object') Object.values(value).forEach(inspect);
    };
    inspect(read(path.join(manifest.directory, 'package.json')));
  }
  const expected = read(path.join(directory, 'zh-CN.json'));
  if (Object.keys(expected).length !== messages.size || [...messages].some(key => !(key in expected))) errors.push('Source catalog is stale. Run node scripts/i18n.mjs --extract.');
  const parameters = value => [...value.matchAll(/\{\d+\}/g)].map(match => match[0]).sort().join(',');
  for (const locale of locales) {
    const catalog = read(path.join(directory, locale + '.json'));
    for (const key of messages) {
      const value = catalog[key];
      if (typeof value !== 'string' || !value.trim()) errors.push(`${locale}: missing ${JSON.stringify(key)}`);
      else if (parameters(value) !== parameters(key)) errors.push(`${locale}: placeholder mismatch ${JSON.stringify(key)}`);
      else {
        if (!key.includes('\n') && value.includes('\n')) errors.push(`${locale}: unexpected line break ${JSON.stringify(key)}`);
        if (/ZXQ\d|QXZ|CC\d{5}|ZNLZ/.test(value)) errors.push(`${locale}: translation marker leaked ${JSON.stringify(key)}`);
        for (const token of key.match(/`[^`\n]+`|\$\([\w-]+\)|https?:\/\/[^\s`]+|<[A-Za-z][A-Za-z0-9| /-]*>|--[a-z][a-z-]*|(?<!\w)\/[a-z][a-z-]*\b|\b[a-z]+(?:_[a-z0-9]+)+\b|\/Users\/[A-Za-z/]+/g) ?? []) {
          if (!/[\u3400-\u9fff]/.test(token) && !value.includes(token)) errors.push(`${locale}: changed technical token ${JSON.stringify(token)} in ${JSON.stringify(key)}`);
        }
      }
    }
  }
  if (errors.length) { console.error(errors.slice(0, 30).join('\n') + `\n${errors.length} localization errors.`); process.exitCode = 1; }
  else {
    for (const manifest of manifests) for (const locale of locales) {
      const catalog = read(path.join(directory, locale + '.json'));
      const values = Object.fromEntries(Object.entries(manifest.source).map(([key, text]) => [key, nativeNames.has(text) ? text : catalog[text]]));
      const suffix = locale === 'en' ? '' : '.' + locale.toLowerCase();
      save(path.join(manifest.directory, 'package.nls' + suffix + '.json'), values);
    }
    console.log(`i18n verified: ${messages.size} messages × ${locales.length} languages; VS Code language packs synchronized.`);
  }
}
