const assert = require('node:assert/strict');
const { test } = require('node:test');
const { JSDOM, VirtualConsole } = require('jsdom');
const { loadPanel } = require('./helpers/chatPanelHarness.cjs');
const { createTurnProcessPolicy } = require('./helpers/turnProcess.cjs');
const policy = createTurnProcessPolicy();

const message = (id, role, text, extra = {}) => ({ kind: 'message', message: { id, role, text, timestamp: 1000, ...extra } });
const tool = (extra = {}) => ({ kind: 'tool', card: { id: 't', toolName: 'Bash', input: { command: 'pwd' }, result: '/work', isError: false, collapsed: true, ...extra } });
const history = () => [message('u', 'user', 'task'), message('pre', 'assistant', 'checking'), tool(), message('f', 'assistant', 'finished', { messageUuid: 'final-uuid' }),
  { kind: 'turnEnd', reason: 'end_turn', finalMessageId: 'final-uuid', timestamp: 4000 }];

function webview() {
  const h = loadPanel(async () => ({ ok: true, json: async () => ({}) }));
  const errors = [];
  let saved = {};
  const dom = new JSDOM(h.panel.getHtmlForWebview({}), {
    runScripts: 'dangerously', pretendToBeVisual: true,
    virtualConsole: new VirtualConsole().on('jsdomError', error => errors.push(error)),
    beforeParse(window) {
      window.acquireVsCodeApi = () => ({ postMessage() {}, getState: () => saved, setState: value => { saved = value; } });
      window.HTMLElement.prototype.scrollIntoView = function () {};
    },
  });
  const send = data => dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data }));
  const doc = dom.window.document;
  const hidden = id => doc.getElementById(id)?.classList.contains('turn-detail-hidden');
  const replay = (items = history(), extra = {}) => send({ type: 'history', items, processSessionKey: 'gateway:a', busy: false, ...extra });
  return { h, dom, doc, send, hidden, replay, errors };
}

test('shared policy preserves terminal blocks, steering, interactions, artifacts and abnormal turns', () => {
  const items = [
    { id: 'u', kind: 'user' }, { id: 'pre', kind: 'assistant', text: 'checking' },
    { id: 't', kind: 'tool', status: 'complete', isError: true },
    { id: 'steer', kind: 'user', origin: 'user-steering' },
    { id: 'f', kind: 'assistant', text: 'answer', messageUuid: 'final' },
    { id: 'f:part-1', kind: 'assistant', text: 'appendix', messageUuid: 'final' },
    { id: 'end', kind: 'turn_duration', turnEndReason: 'end_turn', finalMessageId: 'final' },
  ];
  const turns = policy.groupTurns(items, false);
  assert.equal(turns.length, 1);
  assert.deepEqual(turns[0].processIds, ['pre', 't']);
  assert.equal(turns[0].canAutoCollapse, true);
  assert.equal(turns[0].errorCount, 1);
  assert.equal(policy.isCollapsed(turns[0], true), true);
  assert.equal(policy.isCollapsed(turns[0], false), false);
  assert.equal(policy.isCollapsed(turns[0], true, false), false);
  for (const reason of ['interrupted', 'empty_response', 'max_turns_reached', 'history_restore']) {
    assert.equal(policy.describeTurn(items, { completed: true, reason }).canAutoCollapse, false);
  }
  assert.equal(policy.describeTurn(items.slice(0, -3), { completed: true }).canAutoCollapse, false);
  assert.equal(policy.describeTurn([...items, { id: 'approval', kind: 'permission', status: 'pending' }], { completed: true }).canAutoCollapse, false);
  assert.equal(policy.describeTurn([{ id: 'img', kind: 'tool', images: [{}] }]).processIds.length, 0);
  assert.equal(policy.groupTurns([...items, { id: 'next', kind: 'user' }], true).length, 2);
});

test('webview defaults to folding the completed process while final text and actions stay visible', () => {
  const w = webview();
  try {
    w.replay();
    assert.equal(w.hidden('msg-pre'), true);
    assert.equal(w.hidden('tool-t'), true);
    assert.equal(w.hidden('msg-u'), false);
    assert.equal(w.hidden('msg-f'), false);
    assert.equal(w.doc.querySelector('.turn-summary').getAttribute('aria-expanded'), 'false');
    assert.equal(w.doc.getElementById('msg-u').nextElementSibling, w.doc.querySelector('.turn-summary'));
    assert.ok(w.doc.querySelector('#msg-f [data-copy-label="复制回复"]'));
    assert.equal(w.doc.querySelector('.turn-summary-status').textContent, '已完成');
    assert.doesNotMatch(w.doc.querySelector('.turn-summary').textContent, /次工具调用/);
    w.send({ type: 'processDisplayOptions', showToolCallCount: true });
    assert.match(w.doc.querySelector('.turn-summary').textContent, /1次工具调用/);
    assert.equal(w.hidden('tool-t'), true);
    w.send({ type: 'options', showToolCallCount: false });
    assert.doesNotMatch(w.doc.querySelector('.turn-summary').textContent, /次工具调用/);
    w.replay(history(), { showToolCallCount: true });
    assert.match(w.doc.querySelector('.turn-summary').textContent, /1次工具调用/);
    w.send({ type: 'options', autoCollapseProcess: false });
    assert.equal(w.doc.querySelector('.turn-summary-status').textContent, '已完成');
    assert.deepEqual(w.errors, []);
  } finally { w.dom.window.close(); }
});

for (const source of ['history', 'live']) {
  test(`computer-use screenshots fold with the process and survive expansion (${source})`, () => {
    const w = webview();
    const images = [{ data: 'YQ==', media_type: 'image/png' }];
    const cards = ['observe', 'click', 'scroll'].map(action => tool({
      id: action, toolName: 'ComputerUse', input: { action }, images, collapsed: false,
    }));
    const picture = tool({ id: 'picture', toolName: 'Image', images, collapsed: false });
    try {
      if (source === 'history') {
        w.replay([message('u', 'user', 'task'), ...cards, picture,
          message('f', 'assistant', 'finished'), { kind: 'turnEnd', reason: 'end_turn' }]);
      } else {
        w.replay([], { busy: true });
        w.send({ type: 'newMessage', message: message('u', 'user', 'task').message });
        for (const { card } of [...cards, picture]) {
          w.send({ type: 'toolUse', card: { ...card, result: null, images: [] } });
          w.send({ type: 'toolResult', card });
          assert.equal(w.hidden('tool-' + card.id), false);
        }
        w.send({ type: 'newMessage', message: message('f', 'assistant', 'finished').message });
        w.send({ type: 'processTurnEnd', reason: 'end_turn' });
        w.send({ type: 'busyState', busy: false });
      }
      const screenshot = w.doc.querySelector('#tool-scroll .tool-result-image');
      assert.ok(screenshot);
      for (const { card } of cards) assert.equal(w.hidden('tool-' + card.id), true, card.id);
      assert.equal(w.hidden('tool-picture'), false);
      assert.equal(w.hidden('msg-f'), false);
      w.doc.querySelector('.turn-summary').click();
      for (const { card } of cards) assert.equal(w.hidden('tool-' + card.id), false, card.id);
      w.doc.querySelector('.turn-summary').click();
      for (const { card } of cards) assert.equal(w.hidden('tool-' + card.id), true, card.id);
      w.send({ type: 'processDisplayOptions', autoCollapseProcess: false });
      assert.equal(w.hidden('tool-scroll'), false);
      assert.equal(w.doc.querySelector('#tool-scroll .tool-result-image'), screenshot);
      assert.deepEqual(w.errors, []);
    } finally { w.dom.window.close(); }
  });
}

test('manual expansion survives new history IDs, session switches and setting changes', () => {
  const w = webview();
  try {
    w.replay();
    w.doc.querySelector('.turn-summary').click();
    assert.equal(w.hidden('msg-pre'), false);
    w.send({ type: 'history', items: [], processSessionKey: 'gateway:b' });
    const restored = history();
    restored[0].message.id = 'durable-user';
    w.replay(restored);
    assert.equal(w.hidden('tool-t'), false);
    w.send({ type: 'options', autoCollapseProcess: false });
    assert.equal(w.hidden('msg-pre'), false);
    w.send({ type: 'options', autoCollapseProcess: true });
    assert.equal(w.hidden('tool-t'), false);
    w.replay(history(), { processSessionKey: 'other-gateway:a' });
    assert.equal(w.hidden('tool-t'), true);
    assert.deepEqual(w.errors, []);
  } finally { w.dom.window.close(); }
});

test('selecting final text or the whole turn keeps hidden process entries folded', () => {
  const w = webview();
  try {
    w.replay();
    const selection = w.dom.window.getSelection();
    for (const selector of ['#msg-f', '.turn-content', '#msg-pre']) {
      const range = w.doc.createRange();
      range.selectNodeContents(w.doc.querySelector(selector));
      selection.removeAllRanges();
      selection.addRange(range);
      w.doc.dispatchEvent(new w.dom.window.Event('selectionchange'));
      assert.equal(w.hidden('msg-pre'), true, selector);
      assert.equal(w.hidden('tool-t'), true, selector);
      assert.equal(w.doc.querySelector('.turn-summary').getAttribute('aria-expanded'), 'false');
      assert.equal(selection.getRangeAt(0), range);
    }
    assert.deepEqual(w.errors, []);
  } finally { w.dom.window.close(); }
});

test('selection intersecting visible process content postpones folding until cleared', () => {
  const w = webview();
  try {
    w.replay(history().slice(0, -1), { busy: true });
    const selection = w.dom.window.getSelection();
    const range = w.doc.createRange();
    range.setStart(w.doc.getElementById('msg-u'), 0);
    range.setEndAfter(w.doc.getElementById('msg-pre'));
    selection.addRange(range);
    w.doc.dispatchEvent(new w.dom.window.Event('selectionchange'));
    w.send({ type: 'processTurnEnd', reason: 'end_turn', finalMessageId: 'final-uuid' });
    assert.equal(w.hidden('msg-pre'), false);
    assert.equal(w.hidden('tool-t'), false);
    selection.removeAllRanges();
    w.doc.dispatchEvent(new w.dom.window.Event('selectionchange'));
    assert.equal(w.hidden('msg-pre'), true);
    assert.equal(w.hidden('tool-t'), true);
    assert.deepEqual(w.errors, []);
  } finally { w.dom.window.close(); }
});

test('live content folds only at a normal terminal boundary, not when final streaming begins', () => {
  const w = webview();
  try {
    w.replay([], { busy: true });
    w.send({ type: 'newMessage', message: message('u', 'user', 'task').message });
    w.send({ type: 'busyState', busy: true });
    w.send({ type: 'newMessage', message: message('pre', 'assistant', 'checking').message });
    w.send({ type: 'toolUse', card: tool({ result: null, collapsed: false }).card });
    w.send({ type: 'toolResult', card: tool().card });
    w.send({ type: 'newMessage', message: message('f', 'assistant', 'answer').message });
    w.send({ type: 'appendText', id: 'f', chunk: ' continues' });
    assert.equal(w.hidden('msg-pre'), false);
    w.send({ type: 'messageForkable', id: 'f', messageUuid: 'final-uuid' });
    w.send({ type: 'processTurnEnd', reason: 'end_turn', finalMessageId: 'final-uuid' });
    w.send({ type: 'busyState', busy: false });
    assert.equal(w.hidden('msg-pre'), true);
    assert.match(w.doc.getElementById('msg-f').textContent, /answer continues/);
    assert.equal(w.hidden('msg-f'), false);
    assert.deepEqual(w.errors, []);
  } finally { w.dom.window.close(); }
});

test('interruption and active restored history remain open; manual folding keeps requests and images visible', () => {
  const w = webview();
  try {
    const stopped = history(); stopped.at(-1).reason = 'interrupted';
    w.replay(stopped);
    assert.equal(w.hidden('tool-t'), false);
    w.replay(history().slice(0, -1), { busy: true });
    assert.equal(w.hidden('tool-t'), false);
    const interactive = history().slice(0, -1);
    interactive.splice(3, 0,
      { kind: 'permission', card: { id: 'approval', toolName: 'Bash', input: {}, allowed: null } },
      { kind: 'choice', card: { id: 'choice', question: 'choose', options: ['one'], selected: [], completed: false } },
      { kind: 'plan', card: { id: 'plan', plan: {}, status: 'pending' } },
      tool({ id: 'picture', images: [{ data: 'YQ==', media_type: 'image/png' }] }));
    w.replay(interactive);
    w.doc.querySelector('.turn-summary').click();
    assert.equal(w.hidden('tool-t'), true);
    for (const id of ['permission-approval', 'choice-choice', 'plan-plan', 'tool-picture']) assert.equal(w.hidden(id), false, id);
    assert.deepEqual(w.errors, []);
  } finally { w.dom.window.close(); }
});

test('the extension sends the terminal boundary and exposes the default-on setting', async () => {
  const { panel, messages } = loadPanel(async () => ({ ok: true, json: async () => ({}) }));
  const state = panel.getSessionState('a');
  state.activeOperationId = 'op';
  panel.handleServerEvent({ type: 'stream_text', session_id: 'a', operation_id: 'op', text: 'done' });
  panel.handleServerEvent({ type: 'turn_complete', session_id: 'a', operation_id: 'op', reason: 'interrupted' });
  assert.equal(messages.find(m => m.type === 'processTurnEnd').reason, 'interrupted');
  assert.equal(state.history.at(-1).kind, 'turnEnd');
  const settings = require('../package.json').contributes.configuration.properties;
  assert.equal(settings['crabcode.autoCollapseProcess'].default, true);
  assert.equal(settings['crabcode.showToolCallCount'].default, false);
});

test('display preference updates do not wait for the model-list request', () => {
  const { panel, messages, config } = loadPanel(() => new Promise(() => {}));
  config.autoCollapseProcess = false;
  config.showToolCallCount = true;
  panel.notifyConfigurationChanged();
  assert.equal(messages.find(m => m.type === 'processDisplayOptions').autoCollapseProcess, false);
  assert.equal(messages.find(m => m.type === 'processDisplayOptions').showToolCallCount, true);
});

test('steering stays in the active turn and queued prompts start another turn', () => {
  const w = webview();
  try {
    w.replay(history().slice(0, -1), { busy: true });
    w.send({ type: 'newMessage', message: message('steer', 'user', 'correction', { followUpMode: 'steer' }).message });
    assert.equal(w.doc.querySelectorAll('.turn-block').length, 1);
    w.send({ type: 'newMessage', message: message('next', 'user', 'next task', { followUpMode: 'queue' }).message });
    assert.equal(w.doc.querySelectorAll('.turn-block').length, 2);
    w.send({ type: 'processDisplayOptions', autoCollapseProcess: false });
    assert.equal(w.hidden('msg-pre'), false);
  } finally { w.dom.window.close(); }
});
