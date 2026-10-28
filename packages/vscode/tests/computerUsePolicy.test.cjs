const assert = require('node:assert/strict');
const { test } = require('node:test');
const { buildSync } = require('esbuild');
const path = require('node:path');
const vm = require('node:vm');

const source = buildSync({
  entryPoints: [path.join(__dirname, '../src/connection.ts')], bundle: true,
  write: false, platform: 'node', format: 'cjs', external: ['vscode', 'ws'],
}).outputFiles[0].text;

function connection(preferences) {
  const module = { exports: {} };
  let currentPreferences = preferences;
  const config = values => ({
    get: (key, fallback) => values[key]?.workspaceValue ?? values[key]?.globalValue ?? fallback,
    inspect: key => values[key] ?? { defaultValue: 'allow_foreground' },
  });
  vm.runInNewContext(source, {
    module, exports: module.exports,
    require: name => name === 'vscode' ? { workspace: { getConfiguration: () => config(currentPreferences) } } : require(name),
    setTimeout, clearTimeout, console, process, Buffer,
  });
  const conn = new module.exports.CrabCodeConnection(config(preferences));
  const messages = [];
  conn.sendRaw = raw => messages.push(JSON.parse(raw));
  conn.sendCommand = cmd => messages.push(JSON.parse(JSON.stringify(cmd)));
  return { conn, messages, update: next => { currentPreferences = next; conn.refreshConfiguration(); } };
}

test('saved Computer Use overrides and inherit changes apply to the next send', () => {
  const h = connection({ computerUseTargetScope: { globalValue: 'desktop' } });
  h.conn.send('first', { sessionId: 's' });
  h.update({ computerUseTargetScope: { workspaceValue: 'app_window' } });
  h.conn.send('second', { sessionId: 's' });
  h.update({});
  h.conn.send('third', { sessionId: 's' });
  assert.equal(h.messages[0].computer_use_target_scope, 'desktop');
  assert.equal(h.messages[1].computer_use_target_scope, 'app_window');
  assert.equal(h.messages[2].computer_use_target_scope, undefined);
});

test('explicit scope and delivery permission cross create, resume and send', () => {
  const { conn, messages } = connection({
    computerUseMode: { globalValue: 'foreground_desktop' },
    computerUseTargetScope: { workspaceValue: 'app_window' },
    computerUseDeliveryPolicy: { globalValue: 'allow_foreground' },
  });
  conn.sendNewSession('/workspace');
  conn.sendResumeSession('s');
  conn.send('test', { sessionId: 's' });
  assert.equal(messages.length, 3);
  for (const message of messages) {
    assert.equal(message.computer_use_target_scope, 'app_window');
    assert.equal(message.computer_use_delivery_policy, 'allow_foreground');
    assert.equal(message.computer_use_mode, undefined);
  }
});

test('extension defaults and legacy target do not grant foreground permission', () => {
  const { conn, messages } = connection({ computerUseMode: { globalValue: 'foreground_desktop' } });
  conn.sendNewSession('/workspace');
  conn.send('test', { sessionId: 's' });
  for (const message of messages) {
    assert.equal(message.computer_use_mode, 'foreground_desktop');
    assert.equal(message.computer_use_delivery_policy, undefined);
    assert.equal(message.computer_use_target_scope, undefined);
  }
});

test('a stricter workspace permission overrides a global foreground grant', () => {
  const { conn, messages } = connection({
    computerUseDeliveryPolicy: { globalValue: 'allow_foreground', workspaceValue: 'strict_background' },
  });
  conn.sendResumeSession('s');
  assert.equal(messages[0].computer_use_delivery_policy, 'strict_background');
});

test('native host binding follows create and resume and enablement follows every send', () => {
  const { conn, messages } = connection({});
  conn.setComputerUseBinding({ hostId: 'vscode-native', enabled: true });
  conn.sendNewSession('/workspace');
  conn.sendResumeSession('s');
  conn.send('observe', { sessionId: 's' });
  conn.setComputerUseBinding({ hostId: 'vscode-native', enabled: false });
  conn.send('disabled', { sessionId: 's' });
  for (const message of messages.slice(0, 2)) {
    assert.equal(message.computer_use_host_id, 'vscode-native');
    assert.equal(message.computer_use_enabled, true);
  }
  assert.equal(messages[2].computer_use_enabled, true);
  assert.equal(messages[3].computer_use_enabled, false);
});


test('model defaults apply to new sessions but never override a resumed session', () => {
  const { conn, messages } = connection({ chatModelDefault: { globalValue: 'default-profile' } });
  conn.sendNewSession('/workspace');
  conn.sendResumeSession('saved-session');
  conn.sendNewSession('/workspace', { model_profile: 'explicit-profile' });
  assert.equal(messages[0].model_profile, 'default-profile');
  assert.equal(messages[1].model_profile, undefined);
  assert.equal(messages[2].model_profile, 'explicit-profile');
});
