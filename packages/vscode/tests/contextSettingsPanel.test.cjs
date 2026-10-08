const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const ts = require('typescript');

function harness() {
  let receiveHost, receivePage, rejectSave = false;
  const requests = [];
  const config = { serverUrl: 'wss://gateway.example/ws', password: 'test-password' };
  const data = {
    cwd: '/work/project', auto_compact_enabled: true, compact_buffer_tokens: 20000,
    max_context_length: 50000, editable_sources: [
      { id: 'userSettings', label: '用户配置', writable: true },
      { id: 'projectSettings', label: '项目配置', writable: true },
    ],
  };
  const panel = {
    reveal() {}, onDidDispose() {},
    webview: {
      html: '', onDidReceiveMessage(fn) { receiveHost = fn; },
      async postMessage(message) { receivePage({ data: message }); },
    },
  };
  const vscode = {
    ViewColumn: { Active: 1 }, window: { createWebviewPanel: () => panel },
    workspace: { workspaceFolders: [{ uri: { fsPath: '/work/project' } }],
      getConfiguration: () => ({ get: (name, fallback) => config[name] ?? fallback }) },
  };
  const exports = {};
  const filename = path.join(__dirname, '../src/contextSettingsPanel.ts');
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, {
    exports, URL, require: name => name === 'vscode' ? vscode : require(name),
    fetch: async (url, init) => {
      const body = init.body && JSON.parse(init.body);
      requests.push({ url: String(url), ...init, body });
      if (body && rejectSave) return { ok: false, json: async () => ({ detail: 'write failed' }) };
      if (body) Object.assign(data, body);
      return { ok: true, json: async () => data };
    },
  });
  exports.ContextSettingsPanel.show();
  const elements = {};
  function element() {
    return { value: '', disabled: false, checked: false, textContent: '', listeners: {},
      addEventListener(type, callback) { this.listeners[type] = callback; },
      replaceChildren() {}, appendChild() {} };
  }
  vm.runInNewContext(panel.webview.html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1], {
    document: {
      getElementById: id => elements[id] ??= element(), createElement: element,
    },
    window: { addEventListener: (_, callback) => { receivePage = callback; } },
    acquireVsCodeApi: () => ({ postMessage: message => receiveHost(message) }),
  });
  return {
    elements, requests, data, reject: value => { rejectSave = value; },
    settle: () => new Promise(resolve => setImmediate(resolve)),
    submit: () => elements.form.listeners.submit({ preventDefault() {} }),
  };
}

test('context settings load and persist through the gateway with workspace, layer and auth', async () => {
  const h = harness();
  await h.settle();
  assert.equal(new URL(h.requests[0].url).searchParams.get('cwd'), '/work/project');
  assert.equal(h.requests[0].headers.Authorization, 'Bearer test-password');
  assert.equal(h.elements.buffer.value, '20000');
  h.elements.buffer.value = '0';
  h.elements.limit.value = '';
  h.elements.enabled.checked = false;
  h.submit();
  await h.settle();
  assert.deepEqual(h.requests[1].body, {
    action: 'set_compaction', source: 'projectSettings', cwd: '/work/project',
    auto_compact_enabled: false, compact_buffer_tokens: 0, max_context_length: null,
  });
  assert.match(h.elements.message.textContent, /已保存/);
});

test('invalid values stay local; failed saves keep the draft and support retry', async () => {
  const h = harness();
  await h.settle();
  for (const value of ['', '-1', '1.5']) {
    h.elements.buffer.value = value;
    h.submit();
    assert.equal(h.requests.length, 1);
  }
  h.elements.buffer.value = '30000';
  h.reject(true);
  h.submit();
  await h.settle();
  assert.equal(h.elements.buffer.value, '30000');
  assert.equal(h.elements.fields.disabled, false);
  assert.equal(h.elements.message.textContent, 'write failed');
  h.reject(false);
  h.submit();
  await h.settle();
  assert.equal(h.data.compact_buffer_tokens, 30000);
});
