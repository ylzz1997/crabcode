const assert = require('node:assert/strict');
const { test } = require('node:test');
const { JSDOM, VirtualConsole } = require('jsdom');
const { loadPanel } = require('./helpers/chatPanelHarness.cjs');

const row = (id, cwd) => ({ session_id: id, cwd, title: id, message_count: 2, created_at: '2026-10-29T00:00:00Z' });
const latest = h => h.messages.filter(m => m.type === 'sessionList' && !m.loading).at(-1);
const tick = () => new Promise(resolve => setImmediate(resolve));
function bind(h) {
  let receive;
  h.panel.pushChatOptions = async () => {};
  h.panel.resolveWebviewView({
    webview: { asWebviewUri: uri => uri.fsPath, onDidReceiveMessage: fn => { receive = fn; } },
    onDidChangeVisibility() {},
  });
  return receive;
}

test('session lists explicitly query the workspace, including spaces and non-ASCII paths', async () => {
  const requests = [];
  const cwd = '/projects/魔法使 & notes';
  const h = loadPanel(async (url, init) => {
    requests.push({ url: new URL(url), init });
    return { ok: true, json: async () => [row('local', cwd)] };
  });
  h.vscode.workspace.workspaceFolders = [{ uri: { fsPath: cwd } }];
  h.config.password = 'test-password';
  await h.panel.fetchAndSendSessions();
  assert.equal(requests[0].url.pathname, '/session/list');
  assert.equal(requests[0].url.searchParams.get('cwd'), cwd);
  assert.equal(requests[0].init.headers.Authorization, 'Bearer test-password');
  assert.equal(latest(h).scope, 'workspace');
  assert.equal(latest(h).sessions[0].cwd, cwd);
});

test('multi-root workspaces merge and de-duplicate only their own project lists', async () => {
  const requests = [];
  const h = loadPanel(async url => {
    const cwd = new URL(url).searchParams.get('cwd');
    requests.push(cwd);
    return { ok: true, json: async () => [row(cwd, cwd), row('duplicate', cwd)] };
  });
  h.vscode.workspace.workspaceFolders = ['/one', '/two', '/one'].map(fsPath => ({ uri: { fsPath } }));
  await h.panel.fetchAndSendSessions();
  assert.deepEqual(requests, ['/one', '/two']);
  assert.equal(latest(h).sessions.length, 3);
});

test('an empty workspace never falls back to the Gateway default project', async () => {
  let calls = 0;
  const h = loadPanel(async () => { calls++; throw Error('unexpected network request'); });
  h.vscode.workspace.workspaceFolders = undefined;
  await h.panel.fetchAndSendSessions();
  assert.equal(calls, 0);
  assert.equal(latest(h).hasWorkspace, false);
  assert.equal(latest(h).sessions.length, 0);
});

test('scope changes cross the bridge, persist per workspace, and restore on reopening', async () => {
  const requests = [];
  const h = loadPanel(async url => {
    requests.push(new URL(url));
    return { ok: true, json: async () => [row('foreign', '/other')] };
  });
  const receive = bind(h);
  receive({ type: 'setSessionListScope', scope: 'all' });
  await tick();
  assert.equal(requests.at(-1).pathname, '/session/recent');
  assert.equal(requests.at(-1).searchParams.get('limit'), '200');
  assert.equal(h.saved.get('crabcode.sessionListScope'), 'all');
  assert.equal(latest(h).scope, 'all');
  const restored = loadPanel(async () => ({ ok: true, json: async () => [] }), [...h.saved]);
  assert.equal(restored.panel.sessionListScope, 'all');
  receive({ type: 'setSessionListScope', scope: 'invalid' });
  await tick();
  assert.equal(requests.length, 1);
  receive({ type: 'setSessionListScope', scope: 'workspace' });
  await tick();
  assert.equal(requests.at(-1).searchParams.get('cwd'), h.workspaceRoot);
  assert.equal(h.saved.get('crabcode.sessionListScope'), 'workspace');
});

test('a late global response cannot replace the newer current-project list', async () => {
  let release;
  const h = loadPanel(async url => {
    if (new URL(url).pathname === '/session/recent') {
      await new Promise(resolve => { release = resolve; });
      return { ok: true, json: async () => [row('foreign', '/other')] };
    }
    return { ok: true, json: async () => [row('local', '/workspace')] };
  });
  h.panel.sessionListScope = 'all';
  const slow = h.panel.fetchAndSendSessions();
  h.panel.sessionListScope = 'workspace';
  await h.panel.fetchAndSendSessions();
  release();
  await slow;
  assert.equal(latest(h).scope, 'workspace');
  assert.equal(latest(h).sessions[0].session_id, 'local');
});

test('failed refreshes clear prior results and a capped global list reports its limit', async () => {
  let fail = false;
  const h = loadPanel(async () => {
    if (fail) throw Error('offline');
    return { ok: true, json: async () => Array.from({ length: 200 }, (_, i) => row(String(i), '/other')) };
  });
  h.panel.sessionListScope = 'all';
  await h.panel.fetchAndSendSessions();
  assert.equal(latest(h).limit, 200);
  fail = true;
  await h.panel.fetchAndSendSessions();
  assert.equal(latest(h).sessions.length, 0);
  assert.match(latest(h).error, /offline/);
});

test('title lookup resolves the selected session even when it belongs to another project', async () => {
  const requests = [];
  const h = loadPanel(async url => {
    requests.push(new URL(url));
    return { ok: true, json: async () => ({ ...row('a', '/other'), title: 'Foreign title' }) };
  });
  await h.panel.fetchAndSendCurrentTitle();
  assert.equal(requests[0].pathname, '/session/resolve');
  assert.equal(requests[0].searchParams.get('selector'), 'a');
  assert.equal(h.messages.at(-1).title, 'Foreign title');
});

test('both list controls stay in sync, keep search text, and clear old sessions while switching', () => {
  const h = loadPanel(async () => ({ ok: true, json: async () => [] }));
  const sent = [], errors = [];
  const dom = new JSDOM(h.panel.getHtmlForWebview({}), {
    runScripts: 'dangerously', pretendToBeVisual: true,
    virtualConsole: new VirtualConsole().on('jsdomError', error => errors.push(error)),
    beforeParse(window) {
      window.acquireVsCodeApi = () => ({ postMessage: m => sent.push(m), getState: () => ({}), setState() {} });
      window.HTMLElement.prototype.scrollIntoView = function () {};
    },
  });
  try {
    const doc = dom.window.document;
    const send = data => dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data }));
    const main = doc.getElementById('session-list-scope'), history = doc.getElementById('history-scope');
    assert.equal(main.value, 'workspace');
    send({ type: 'sessionList', scope: 'workspace', hasWorkspace: true, sessions: [row('local', '/one')] });
    assert.equal(doc.querySelectorAll('.session-item').length, 1);
    const search = doc.getElementById('history-search');
    search.value = 'foreign';
    search.dispatchEvent(new dom.window.Event('input'));
    main.value = 'all';
    main.dispatchEvent(new dom.window.Event('change'));
    assert.equal(history.value, 'all');
    assert.equal(sent.at(-1).type, 'setSessionListScope');
    assert.equal(sent.at(-1).scope, 'all');
    assert.equal(doc.querySelectorAll('.session-item').length, 0);
    assert.match(doc.getElementById('history-list-items').textContent, /正在加载/);
    send({ type: 'sessionList', scope: 'all', hasWorkspace: true, sessions: [row('foreign', '/other'), row('local', '/one')] });
    assert.equal(search.value, 'foreign');
    assert.equal(doc.querySelectorAll('.history-item').length, 1);
    doc.querySelector('.history-item').click();
    assert.equal(sent.at(-1).sessionId, 'foreign');
    send({ type: 'sessionList', scope: 'workspace', hasWorkspace: false, sessions: [] });
    assert.equal(main.value, 'workspace');
    assert.match(doc.getElementById('session-list-items').textContent, /请先打开项目/);
    assert.deepEqual(errors, []);
  } finally { dom.window.close(); }
});
