# Client localization

Desktop, the VS Code chat/settings webviews, extension-host messages, and the local Computer Use companion share these offline catalogs:

| Preference | Language |
| --- | --- |
| `auto` | System language (Desktop) or VS Code display language |
| `zh-CN` | 简体中文 |
| `zh-TW` | 繁體中文 |
| `en` | English |
| `ja` | 日本語 |
| `ko` | 한국어 |
| `de` | Deutsch |
| `fr` | Français |
| `es` | Español |
| `pt-BR` | Português (Brasil) |
| `ru` | Русский |

Desktop stores `language` in its existing settings file. VS Code uses `crabcode.language`, also available in **CrabCode Settings → General → Display language**. Missing or invalid preferences become `auto`; unsupported system languages fall back to English. Chinese script subtags take precedence over regions, and Traditional Chinese includes Taiwan, Hong Kong and Macau. Other supported regional variants resolve to their base language; Portuguese resolves to Brazilian Portuguese.

Changing the preference updates the UI without restarting the Gateway. Desktop rerenders without remounting editors. The chat webview transfers its composer text and attachments before replacing the localized shell, then replays the extension host's session state. Settings editors with unsaved changes remain mounted and refresh their labels when saved or revisited. User messages, model responses, code, paths, template names and prompt bodies are not translated. Backend/native diagnostics remain in the language supplied by their source; known configuration-scope and prompt-section labels are mapped by stable IDs.

VS Code command-palette titles and native configuration descriptions use `package.nls*.json`. These follow **VS Code's display language**, independently of `crabcode.language`, as required by [VS Code's localization model](https://github.com/microsoft/vscode-l10n#package-nls-json).

## Adding or changing messages

Use the existing Simplified Chinese message as a source key:

```ts
tr("已导入 {0}", { 0: filename })
```

Pass interpolated values separately, use complete sentences where possible, and preserve protocol IDs. Do not translate input data or document-reference envelope markers. Only application-owned UI metadata should use `localized(...)`; it provides lazy getters so static catalogs follow subsequent language changes. `trHtml(...)` is restricted to application-owned markup literals and escapes translated text/attributes. Never pass user HTML or a rendered transcript to it.

`runtime.js` is dependency-free and its factory must remain self-contained: the chat panel embeds it under its existing CSP nonce. Catalog JSON is escaped before embedding; user values are substituted once, never recursively interpreted.

From the repository root:

```sh
node scripts/i18n.mjs --extract
# Edit all nine translated catalogs in locales/.
node scripts/i18n.mjs
npm --prefix packages/shared test
npm --prefix packages/desktop test
npm --prefix packages/vscode test
```

The audit checks source coverage, interpolation placeholders, technical tokens and unexpected line breaks in every language, and synchronizes both extensions' native language packs. Both client builds run it; no translation service or network request is used at runtime or during a normal build. When drafting translations, send each message as a separate translation unit and validate response boundaries; joining messages into one paragraph can move words between neighboring entries.

The initial catalogs were drafted with machine assistance. Core navigation, language names, approval controls and permission terminology were reviewed explicitly. Further native-speaker review is recommended for long help text and less common workflows.
