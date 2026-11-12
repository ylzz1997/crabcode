const assert = require('node:assert/strict');
const { test } = require('node:test');
const { JSDOM, VirtualConsole } = require('jsdom');
const { loadPanel } = require('./helpers/chatPanelHarness.cjs');
const { harness } = require('./helpers/settingsHarness.cjs');

const languages = ['zh-CN', 'zh-TW', 'en', 'ja', 'ko', 'de', 'fr', 'es', 'pt-BR', 'ru'];
const allow = ['允许', '允許', 'Allow', '許可', '허용', 'Zulassen', 'Autoriser', 'Permitir', 'Permitir', 'Разрешить'];
const welcome = [
  '开始和 CrabCode 聊天吧', '開始和 CrabCode 聊天吧', 'Start chatting with CrabCode',
  'CrabCode とチャットを始めましょう', 'CrabCode와 대화를 시작하세요', 'Starte einen Chat mit CrabCode',
  'Commencez à discuter avec CrabCode', 'Empieza a chatear con CrabCode',
  'Comece a conversar com o CrabCode', 'Начните общение с CrabCode',
];
function openChat(h, state = {}) {
  const errors = [], sent = [];
  let saved = state;
  const dom = new JSDOM(h.panel.getHtmlForWebview({}), {
    runScripts: 'dangerously', pretendToBeVisual: true,
    virtualConsole: new VirtualConsole().on('jsdomError', error => errors.push(error)),
    beforeParse(window) {
      window.acquireVsCodeApi = () => ({ postMessage: msg => sent.push(msg), getState: () => saved, setState: value => { saved = value; } });
      window.HTMLElement.prototype.scrollIntoView = function () {};
    },
  });
  return { dom, doc: dom.window.document, errors, sent, get saved() { return saved; },
    send: data => dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data })) };
}

test('all ten chat webviews execute localized scripts without changing user content or permission IDs', () => {
  const h = loadPanel(async () => ({ ok: true, json: async () => ({}) }));
  for (const [index, language] of languages.entries()) {
    h.config.language = language;
    const view = openChat(h);
    try {
      assert.equal(view.doc.documentElement.lang, language);
      assert.equal(view.errors.length, 0);
      assert.ok(view.sent.some(msg => msg.type === 'webviewReady'));
      const emptyRule = [...view.doc.styleSheets[0].cssRules].find(rule => rule.selectorText === '#message-content:empty::before');
      assert.equal(emptyRule.style.content, 'attr(data-placeholder)');
      const content = view.doc.querySelector('#message-content');
      assert.equal(content.dataset.placeholder, welcome[index]);
      assert.ok(content.matches(':empty'));
      view.send({ type: 'history', items: [{ kind: 'message', message: { id: 'user', role: 'user', text: '保存 /path/中文.txt {0}', timestamp: 1 } }] });
      assert.ok(!content.matches(':empty'));
      assert.ok(view.doc.querySelector('#msg-user').textContent.includes('保存 /path/中文.txt {0}'));
      assert.equal(view.doc.querySelector('[data-perm="run_everything"]').dataset.perm, 'run_everything');
      // Core approval words must come from the selected locale, not a fallback catalog.
      const data = view.doc.documentElement.outerHTML;
      if (index > 0) assert.ok(!data.includes('placeholder="搜索模型…"'));
      view.send({ type: 'permissionRequest', card: { id: 'permission-test', toolName: 'Bash', input: { command: 'echo 中文' }, allowed: null } });
      const button = view.doc.querySelector('[data-permission-allow]');
      assert.equal(button.textContent, allow[index]);
      button.click();
      const action = view.sent.find(message => message.type === 'respondToPermission');
      assert.equal(action.id, 'permission-test');
      assert.equal(action.allowed, true);
      assert.equal(action.alwaysAllow, false);
      view.send({ type: 'history', items: [] });
      assert.ok(content.matches(':empty'));
      assert.equal(content.dataset.placeholder, welcome[index]);
    } finally { view.dom.window.close(); }
  }
});

test('chat localizes file review and tool labels while retaining the file path and action IDs', () => {
  const h = loadPanel(async () => ({ ok: true, json: async () => ({}) }));
  h.config.language = 'zh-CN';
  const view = openChat(h);
  try {
    const file = { id: 'edit-1', path: '/workspace/a&b.ts', shortPath: 'a&b.ts', action: 'modify', added: 2, removed: 1, hunkCount: 1 };
    view.send({ type: 'pendingEditReview', summary: { totalFiles: 1, totalHunks: 1, files: [file] } });
    assert.equal(view.doc.querySelector('.pending-edits-title span:last-child').textContent, '待审核文件：1');
    assert.equal(view.doc.querySelector('[data-pending-action="reviewFile"]').title, file.path);
    assert.equal(view.doc.querySelector('[data-pending-action="undoFile"]').textContent, '撤销更改');
    assert.equal(view.doc.querySelector('[data-pending-action="reviewAll"]').textContent, '查看变更');
    const keep = view.doc.querySelector('[data-pending-action="keepFile"]');
    assert.equal(keep.textContent, '保留');
    keep.click();
    const action = view.sent.find(msg => msg.type === 'pendingEditAction');
    assert.equal(action.action, 'keepFile');
    assert.equal(action.changeId, file.id);

    view.send({ type: 'toolResult', card: { id: 'tool-1', toolName: 'Bash', input: { command: 'pwd' }, result: file.path, collapsed: false } });
    assert.deepEqual([...view.doc.querySelectorAll('#tool-tool-1 .timeline-meta')].map(el => el.textContent), ['工具输入', '执行结果']);
    assert.ok(view.doc.querySelector('#tool-tool-1').textContent.includes(file.path));
    assert.deepEqual(view.errors, []);
  } finally { view.dom.window.close(); }
});

test('cached context usage follows the display language when sent to a reloaded chat', () => {
  const h = loadPanel(async () => ({ ok: true, json: async () => ({}) }));
  h.config.language = 'zh-CN';
  h.listeners.message({
    type: 'turn_complete', session_id: 'a', context_token_source: 'estimated',
    context_used_tokens: 25000, context_window_tokens: 100000, usage: { cache_read_tokens: 500 },
  });
  const usage = h.messages.find(msg => msg.type === 'contextUsage');
  assert.match(usage.usage.details[0], /本地估算/);
  assert.match(usage.usage.details[2], /缓存命中/);
  h.config.language = 'en';
  const view = openChat(h);
  try {
    view.send(JSON.parse(JSON.stringify(usage)));
    assert.match(view.doc.querySelector('#context-tooltip').textContent, /Context window \(local estimate, awaiting a server count\)/);
    assert.match(view.doc.querySelector('#context-tooltip').textContent, /Cache hit/);
    assert.deepEqual(view.errors, []);
  } finally { view.dom.window.close(); }
});

test('chat language refresh keeps the composer and attachments through the webview handshake', () => {
  const h = loadPanel(async () => ({ ok: true, json: async () => ({}) }));
  h.config.language = 'en';
  const first = openChat(h);
  first.doc.querySelector('#input').textContent = '未发送的草稿 <b>literal</b>';
  first.send({ type: 'addAttachments', images: [{ name: '中文.png', media_type: 'image/png', data: 'AQID' }] });
  first.send({ type: 'prepareLanguageChange' });
  assert.ok(first.sent.some(msg => msg.type === 'reloadForLanguage'));
  const state = first.saved;
  first.dom.window.close();
  h.config.language = 'de';
  const second = openChat(h, state);
  try {
    assert.equal(second.doc.querySelector('#input').textContent, '未发送的草稿 <b>literal</b>');
    assert.equal(second.doc.querySelector('#input b'), null);
    assert.equal(second.doc.querySelector('#attachment-bar img').alt, '中文.png');
    assert.equal(second.saved.languageDraft, undefined);
    assert.equal(second.errors.length, 0);
  } finally { second.dom.window.close(); }
});

test('settings language changes update navigation and preserve unsaved prompt fields', async () => {
  const h = harness({ initial: { language: 'en' } });
  try {
    await h.settle();
    assert.equal(h.doc.documentElement.lang, 'en');
    assert.equal(h.doc.querySelector('[data-local-key="language"]').options.length, 11);
    h.change('Display language', 'de');
    await h.settle();
    assert.equal(h.user.language, 'de');
    assert.equal(h.doc.querySelector('h1').textContent, 'Allgemein');
    assert.equal(h.doc.querySelector('[data-local-key="language"]').value, 'de');
    assert.equal(h.doc.querySelector('[data-local-key="language"]').options[1].textContent, '简体中文');
    h.doc.querySelector('nav [data-section="prompts"]').click();
    await h.settle();
    const textarea = h.doc.querySelector('[data-section="prompts"] textarea');
    textarea.value = '保持这段中文和代码 {0}';
    textarea.dispatchEvent(new h.dom.window.Event('input', { bubbles: true }));
    await h.rpc({ action: 'saveLocal', key: 'language', value: 'ja', scope: 'user' });
    assert.equal(h.doc.documentElement.lang, 'ja');
    assert.ok(textarea.isConnected);
    assert.equal(textarea.value, '保持这段中文和代码 {0}');
  } finally { h.dom.window.close(); }
});

test('refreshing context labels does not remount a dirty tool-settings editor', async () => {
  const h = harness();
  try {
    await h.settle();
    h.doc.querySelector('nav [data-section="tools"]').click();
    await h.settle();
    const control = h.doc.querySelector('[data-section="tools"] [data-runtime-key]');
    assert.ok(control);
    control.dispatchEvent(new h.dom.window.Event('input', { bubbles: true }));
    h.doc.querySelector('nav [data-section="context"]').click();
    await h.settle();
    await h.rpc({ action: 'saveLocal', key: 'language', value: 'en', scope: 'user' });
    assert.ok(control.isConnected);
    assert.equal(control.dataset.dirty, 'true');
  } finally { h.dom.window.close(); }
});
