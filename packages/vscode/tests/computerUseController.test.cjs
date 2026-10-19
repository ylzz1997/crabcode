const { test } = require('node:test');
const assert = require('node:assert/strict');
const { buildSync } = require('esbuild');
const vm = require('node:vm');
const path = require('node:path');

const source = buildSync({ entryPoints: [path.join(__dirname, '../src/computerUse.ts')], bundle: true,
  write: false, platform: 'node', format: 'cjs', external: ['vscode', 'ws', '../../computer-use/client/vscodeHost.js'],
}).outputFiles[0].text;

function harness(remote = false) {
  const values = { computerUseEnabled: true, serverUrl: 'ws://localhost:4096/ws?auth_token=gateway', password: 'test-password' };
  const bindings = [], commands = [], local = [], errors = [];
  const env = { remoteName: remote ? 'ssh-remote' : undefined,
    asExternalUri: async uri => { assert.equal(uri, 'http://localhost:4096/ws'); return { toString: () => 'https://forward.example/ws?forward_token=tunnel' }; } };
  const vscode = { env, Uri: { parse: value => value }, workspace: { getConfiguration: () => ({ get: (name, fallback) => values[name] ?? fallback }) },
    commands: { executeCommand: async (command, options) => { commands.push({ command, options }); return options && { hostId: options.hostId, enabled: options.enabled }; } },
    window: { showErrorMessage: value => errors.push(value) } };
  const module = { exports: {} };
  class LocalHost {
    constructor() { local.push(this); }
    configure(options) { this.options = options; return { hostId: options.hostId, enabled: options.enabled }; }
    dispose() { this.disposed = true; }
  }
  vm.runInNewContext(source, { module, exports: module.exports, URL, URLSearchParams,
    require: name => name === 'vscode' ? vscode : name.endsWith('vscodeHost.js') ? { VSCodeComputerUseHost: LocalHost } : require(name) });
  const create = () => new module.exports.ComputerUseController({ extensionPath: '/extension' },
    { setComputerUseBinding: binding => bindings.push(binding) }, { appendLine: value => errors.push(value) });
  return { create, values, bindings, commands, local, env, vscode, errors };
}

test('local windows have distinct host identities and delegate to the UI companion', async () => {
  const h = harness();
  const a = h.create(), b = h.create();
  await a.configure(); await b.configure();
  assert.equal(h.local.length, 0);
  assert.equal(h.commands.length, 2);
  assert.ok(h.commands.every(call => call.command === 'crabcode.computerUse.configureLocal'));
  assert.notEqual(h.commands[0].options.hostId, h.commands[1].options.hostId);
  assert.equal(h.commands[0].options.enabled, true);
  assert.equal(h.commands[0].options.gatewayUrl, h.values.serverUrl);
  a.dispose(); b.dispose();
  assert.ok(h.commands.slice(2).every(call => call.command === 'crabcode.computerUse.disposeLocal'));
});

test('a disabled local window does not start its companion', async () => {
  const h = harness(), controller = h.create();
  h.values.computerUseEnabled = false;
  await controller.configure();
  assert.equal(h.bindings.at(-1).enabled, false);
  assert.ok(h.commands.every(call => call.command === 'crabcode.computerUse.disposeLocal'));
  controller.dispose();
});

test('remote workspace forwards Gateway credentials and delegates only to the local UI companion', async () => {
  const h = harness(true), controller = h.create();
  await controller.configure();
  assert.equal(h.local.length, 0);
  const call = h.commands[0];
  assert.equal(call.command, 'crabcode.computerUse.configureLocal');
  const url = new URL(call.options.gatewayUrl);
  assert.equal(url.protocol, 'wss:');
  assert.equal(url.searchParams.get('forward_token'), 'tunnel');
  assert.equal(url.searchParams.get('auth_token'), 'gateway');
  assert.equal(call.options.password, 'test-password');
  h.values.computerUseEnabled = false;
  await controller.configure();
  assert.equal(h.commands.at(-1).command, 'crabcode.computerUse.disposeLocal');
  assert.equal(h.bindings.at(-1).enabled, false);
  controller.dispose();
});

test('an obsolete forwarding result cannot re-enable a disabled remote host', async () => {
  const h = harness(true), controller = h.create();
  let resolve;
  h.env.asExternalUri = () => new Promise(done => { resolve = done; });
  const pending = controller.configure();
  h.values.computerUseEnabled = false;
  await controller.configure();
  resolve({ toString: () => 'http://127.0.0.1:1234/ws' });
  await pending;
  assert.ok(h.commands.every(call => call.command !== 'crabcode.computerUse.configureLocal'));
  assert.equal(h.bindings.at(-1).enabled, false);
  controller.dispose();
});

for (const remote of [false, true]) test(`missing companion disables Computer Use in a ${remote ? 'remote' : 'local'} window`, async () => {
  const h = harness(remote), controller = h.create();
  h.vscode.commands.executeCommand = async () => { throw new Error('command not found'); };
  await controller.configure();
  assert.equal(h.bindings.at(-1).enabled, false);
  assert.ok(h.errors.some(error => error.includes('本机安装')));
  controller.dispose();
});
