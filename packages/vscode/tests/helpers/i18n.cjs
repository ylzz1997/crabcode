const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const filename = path.join(__dirname, '../../../shared/i18n/runtime.js');
const source = fs.readFileSync(filename, 'utf8').replace('export function', 'function');
const sandbox = {};
vm.runInNewContext(source, sandbox);
const runtime = sandbox.createI18n('zh-CN');
module.exports = { ...runtime, tr: runtime.t, trHtml: runtime.html,
  createI18n: sandbox.createI18n,
  webviewLocaleData: () => JSON.stringify({ locale: 'zh-CN', dictionaries: {} }),
};
