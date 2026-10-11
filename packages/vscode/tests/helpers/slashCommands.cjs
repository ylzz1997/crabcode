const { buildSync } = require('esbuild');
const path = require('node:path');
const vm = require('node:vm');
const source = buildSync({
  entryPoints: [path.join(__dirname, '../../../shared/slashCommands.js')],
  bundle: true, write: false, platform: 'node', format: 'cjs',
}).outputFiles[0].text;
const moduleValue = { exports: {} };
vm.runInNewContext(source, { module: moduleValue, exports: moduleValue.exports });
module.exports = moduleValue.exports;
