const assert = require('node:assert/strict');
const { test } = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const { loadPanel } = require('./helpers/chatPanelHarness.cjs');

function loadBusyIndicator(panel) {
  const html = panel.getHtmlForWebview({});
  const script = html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
  const source = ts.createSourceFile('webview.js', script, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  const functions = [];
  let activityCase;
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && ['setBusyState', 'updateBusyLabel'].includes(node.name?.text)) {
      functions.push(node.getText(source));
    }
    if (ts.isCaseClause(node) && node.expression.getText(source) === "'activityStatus'") {
      activityCase = node.statements.filter(statement => !ts.isBreakStatement(statement)).map(statement => statement.getText(source)).join('\n');
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert.equal(functions.length, 2);
  assert.ok(activityCase);
  const noop = () => {};
  const context = vm.createContext({
    isBusy: false, retryActivityLabel: null, busyLabel: { textContent: '' },
    busyIndicator: null, composerCard: null, stopBtn: null, sendBtn: null,
    activeTurn: null, pendingSteeringQueue: [], toolCards: new Map(), thinkingCards: new Map(),
    updateForkButtonsDisabled: noop, finishActiveTurn: noop, updateTurnSummary: noop,
    renderSteeringQueue: noop, updateSendButtonTitle: noop, updateComposerPlaceholder: noop,
  });
  vm.runInContext(functions.join('\n'), context);
  return {
    context,
    apply(messages) {
      for (const msg of messages) {
        context.msg = msg;
        if (msg.type === 'busyState') vm.runInContext('setBusyState(msg.busy, msg.retryLabel)', context);
        if (msg.type === 'activityStatus') vm.runInContext(activityCase, context);
      }
      messages.length = 0;
    },
    refresh() { vm.runInContext('updateBusyLabel()', context); },
  };
}

const operation = { session_id: 'a', operation_id: 'op', operation_scope: 'foreground' };

test('retry x/max survives requesting and activity refreshes until the response arrives', () => {
  const { panel, messages } = loadPanel(async () => ({ ok: true, json: async () => ({}) }));
  const indicator = loadBusyIndicator(panel);
  for (const retry_count of [1, 2, 3]) {
    panel.handleServerEvent({ ...operation, type: 'stream_retry', message: '', retry_count, max_retries: 3 });
    panel.handleServerEvent({ ...operation, type: 'stream_mode', mode: 'requesting' });
    indicator.apply(messages);
    // Other busy updates and contextual refreshes must not erase the count.
    vm.runInContext('setBusyState(true)', indicator.context);
    indicator.refresh();
    assert.equal(indicator.context.busyLabel.textContent, `模型连接中断，正在重试 ${retry_count}/3`);
  }
  panel.handleServerEvent({ ...operation, type: 'stream_text', text: 'recovered' });
  indicator.apply(messages);
  indicator.refresh();
  assert.equal(indicator.context.retryActivityLabel, null);
  assert.doesNotMatch(indicator.context.busyLabel.textContent, /重试/);
  assert.equal(panel.getSessionState('a').retryLabel, null);
});

test('retry status clears on cancellation and new turns, and stays with its own session', () => {
  const { panel, messages } = loadPanel(async () => ({ ok: true, json: async () => ({}) }));
  const indicator = loadBusyIndicator(panel);
  panel.handleServerEvent({ ...operation, type: 'stream_retry', message: '正在重试 1/3', retry_count: 1, max_retries: 3 });
  indicator.apply(messages);
  panel.displayedSessionId = 'b';
  panel.handleServerEvent({ ...operation, type: 'stream_retry', message: '正在重试 2/3', retry_count: 2, max_retries: 3 });
  assert.equal(messages.length, 0);
  assert.equal(panel.getSessionState('a').retryLabel, '正在重试 2/3');
  assert.equal(panel.getSessionState('b').retryLabel, null);
  panel.displayedSessionId = 'a';
  panel.handleSessionHistory({ type: 'session_history', session_id: 'a', messages: [] }, 'a');
  indicator.apply(messages);
  assert.equal(indicator.context.busyLabel.textContent, '正在重试 2/3');
  panel.handleServerEvent({ ...operation, type: 'turn_complete', reason: 'interrupted' });
  indicator.apply(messages);
  assert.equal(indicator.context.isBusy, false);
  assert.equal(indicator.context.retryActivityLabel, null);
  assert.equal(panel.getSessionState('a').retryLabel, null);
  panel.handleServerEvent({ ...operation, operation_id: 'next', type: 'stream_mode', mode: 'requesting' });
  indicator.apply(messages);
  assert.doesNotMatch(indicator.context.busyLabel.textContent, /重试/);
});

test('stream retry remains busy, shows reconnect status, and starts a fresh response', () => {
  const { panel, messages } = loadPanel(async () => ({ ok: true, json: async () => ({}) }));

  panel.handleServerEvent({
    type: 'stream_text',
    session_id: 'a',
    operation_id: 'op',
    operation_scope: 'foreground',
    text: 'partial',
  });
  panel.handleServerEvent({
    type: 'stream_retry',
    session_id: 'a',
    operation_id: 'op',
    operation_scope: 'foreground',
    message: 'Reconnecting... 1/5',
    error: 'incomplete chunked read',
    retry_count: 1,
    max_retries: 5,
    delay_seconds: 0.2,
    unbounded: false,
    transport_fallback: false,
    discarded_text_chars: 7,
  });

  const state = panel.getSessionState('a');
  assert.equal(state.isBusy, true);
  assert.equal(state.messages.length, 1);
  assert.equal(state.messages[0].text, 'partial');
  assert.ok(messages.some(message => message.type === 'activityStatus' && message.label === 'Reconnecting... 1/5'));

  panel.handleServerEvent({
    type: 'stream_text',
    session_id: 'a',
    operation_id: 'op',
    operation_scope: 'foreground',
    text: 'recovered',
  });

  assert.equal(state.messages.length, 2);
  assert.equal(state.messages[1].text, 'recovered');
});
