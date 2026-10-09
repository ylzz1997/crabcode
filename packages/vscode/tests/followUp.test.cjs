const assert = require('node:assert/strict');
const { test } = require('node:test');
const vm = require('node:vm');
const { loadPanel } = require('./helpers/chatPanelHarness.cjs');

function harness(mode) {
  const h = loadPanel(async () => ({ ok: true, json: async () => ({}) }));
  h.panel.reveal = () => {};
  if (mode) h.config.followUpMode = mode;
  h.calls = [];
  h.connection.queue = (text, options) => h.calls.push({ mode: 'queue', text, options });
  h.connection.steer = (text, options) => h.calls.push({ mode: 'steer', text, options });
  h.actions = [];
  h.connection.queuedMessageAction = (id, action, sessionId, operationId) => h.actions.push({ id, action, sessionId, operationId });
  h.state = h.panel.getSessionState('a');
  h.state.isBusy = true;
  h.state.activeOperationId = 'op';
  return h;
}

test('follow-ups default to queue; a one-message override does not change the saved mode', async () => {
  const h = harness();
  const images = [{ media_type: 'image/png', data: 'base64' }];
  await h.panel.handleUserMessage('next task', images);
  await h.panel.handleUserMessage('change direction', images, true);
  await h.panel.handleUserMessage('another task');
  assert.deepEqual(h.calls.map(call => call.mode), ['queue', 'steer', 'queue']);
  assert.equal(h.calls[0].options.sessionId, 'a');
  assert.deepEqual(h.calls[0].options.images, images);
  assert.equal(h.state.messages.length, 0);
  assert.equal(h.state.pendingSteeringMessages.length, 3);
  assert.equal(h.calls[0].options.requestId, h.state.pendingSteeringMessages[0].id);
});

test('steering default can be reversed for one message; idle input starts a normal turn', async () => {
  const h = harness('steer');
  await h.panel.handleUserMessage('guidance');
  await h.panel.handleUserMessage('after this', undefined, true);
  assert.deepEqual(h.calls.map(call => call.mode), ['steer', 'queue']);
  h.state.isBusy = false;
  await h.panel.handleUserMessage('new task', undefined, true);
  assert.equal(h.sent[0].text, 'new task');
  assert.equal(h.calls.length, 2);
});

test('queue and steering are applied independently, including sessions no longer displayed', async () => {
  const h = harness();
  await h.panel.handleUserMessage('next task');
  await h.panel.handleUserMessage('guidance', undefined, true);
  const queueId = h.state.pendingSteeringMessages[0].id;
  h.panel.displayedSessionId = 'b';
  h.panel.handleServerEvent({ type: 'steering_applied', session_id: 'a', operation_id: 'op', count: 1 });
  assert.deepEqual(Array.from(h.state.messages, message => message.text), ['guidance']);
  assert.deepEqual(Array.from(h.state.pendingSteeringMessages, message => message.text), ['next task']);
  h.panel.handleServerEvent({ type: 'queued_message_started', session_id: 'a', operation_id: 'op', request_id: queueId, text: 'next task' });
  assert.deepEqual(Array.from(h.state.messages, message => message.text), ['guidance', 'next task']);
  assert.equal(h.state.pendingSteeringMessages.length, 0);
  assert.equal(h.panel.getSessionState('b').messages.length, 0);
});

test('queue rejection preserves exactly the rejected message and its full context for recovery', async () => {
  const h = harness();
  const images = [{ media_type: 'image/png', data: 'base64' }];
  await h.panel.handleUserMessage('accepted');
  await h.panel.handleUserMessage('rejected with context', images);
  const pending = h.state.pendingSteeringMessages[1];
  h.panel.handleServerEvent({ type: 'error', session_id: 'a', operation_id: 'op', command_error: true, command: 'queue_message', request_id: pending.id, message: 'Queue full' });
  assert.equal(h.state.pendingSteeringMessages[0].followUpFailed, undefined);
  assert.equal(pending.followUpFailed, true);
  assert.equal(pending.followUpPrompt, 'rejected with context');
  assert.deepEqual(pending.images, images);
  assert.equal(h.state.isBusy, true);
  // No webview timer needed for a background session terminal.
  h.panel.displayedSessionId = 'b';
  h.panel.handleServerEvent({ type: 'turn_complete', session_id: 'a', operation_id: 'op', reason: 'interrupted' });
  assert.ok(h.state.pendingSteeringMessages.every(message => message.followUpFailed));
  assert.equal(h.state.isBusy, false);
});

test('context-menu prompts and expanded skills also respect the follow-up default', () => {
  const h = harness();
  h.panel.sendPrompt('explain selected code');
  h.panel.sendExpandedPrompt('/review', 'expanded skill instructions');
  assert.deepEqual(h.calls.map(call => call.mode), ['queue', 'queue']);
  assert.equal(h.calls[1].text, 'expanded skill instructions');
  assert.equal(h.state.pendingSteeringMessages[1].text, '/review');
  assert.equal(h.state.pendingSteeringMessages[1].followUpPrompt, 'expanded skill instructions');
});

test('the webview sends the opposite only for modified Enter while working and preserves IME/newlines', () => {
  const h = harness();
  const html = h.panel.getHtmlForWebview({});
  const handler = html.match(/input\.addEventListener\('keydown', (function\(e\) \{[\s\S]*?\n    \})\);/)[1];
  for (const composerSendKey of ['enter', 'mod_enter']) {
    const sends = [];
    let newlines = 0;
    const context = {
      composerSendKey, isBusy: true, composerIsComposing: false,
      mentionPopup: { classList: { contains: () => true } },
      slashPopup: { classList: { contains: () => true } },
      isComposerModifierSubmit: event => event.key === 'Enter' && Boolean(event.ctrlKey),
      closeMentionPopup() {}, closeSlashPopup() {},
      send: opposite => sends.push(opposite), insertComposerLineBreak: () => newlines++,
      input: { dispatchEvent() {} }, Event,
    };
    const keydown = vm.runInNewContext('(' + handler + ')', context);
    const key = changes => ({ key: 'Enter', preventDefault() {}, ...changes });
    keydown(key({}));
    keydown(key({ ctrlKey: true }));
    assert.deepEqual(sends, [false, true]);
    keydown(key({ shiftKey: true }));
    keydown(key({ shiftKey: true, ctrlKey: true }));
    assert.equal(newlines, 2);
    keydown(key({ ctrlKey: true, isComposing: true }));
    keydown(key({ keyCode: 229 }));
    assert.equal(sends.length, 2);
  }
});

test('editing waits for server recall, keeps raw context and images, and deletes do not add chat messages', async () => {
  const h = harness();
  const images = [{ media_type: 'image/png', data: 'base64' }];
  await h.panel.handleUserMessage('full original prompt', images);
  const entry = h.state.pendingSteeringMessages[0];
  h.panel.handleQueuedMessageAction(entry.id, 'edit');
  h.panel.handleQueuedMessageAction(entry.id, 'edit');
  assert.equal(h.actions.length, 1);
  assert.deepEqual(h.actions[0], { id: entry.id, action: 'edit', sessionId: 'a', operationId: 'op' });
  assert.equal(h.messages.filter(message => message.type === 'restoreFollowUp').length, 0);
  h.panel.handleServerEvent({ type: 'queued_message_updated', session_id: 'a', operation_id: 'op', request_id: entry.id, action: 'edit' });
  const restored = h.messages.find(message => message.type === 'restoreFollowUp');
  assert.equal(restored.text, 'full original prompt');
  assert.deepEqual(restored.images, images);
  assert.equal(h.state.pendingSteeringMessages.length, 0);
  await h.panel.handleUserMessage('delete this');
  const removed = h.state.pendingSteeringMessages[0];
  h.panel.handleQueuedMessageAction(removed.id, 'remove');
  h.panel.handleServerEvent({ type: 'queued_message_updated', session_id: 'a', operation_id: 'op', request_id: removed.id, action: 'remove' });
  assert.equal(h.state.messages.length, 0);
  assert.equal(h.state.pendingSteeringMessages.length, 0);
});

test('promoted queue entries join the end of pending guidance and are applied only once', async () => {
  const h = harness();
  await h.panel.handleUserMessage('queued first');
  await h.panel.handleUserMessage('guide before promotion', undefined, true);
  const entry = h.state.pendingSteeringMessages[0];
  h.panel.handleQueuedMessageAction(entry.id, 'steer');
  h.panel.handleServerEvent({ type: 'queued_message_updated', session_id: 'a', operation_id: 'op', request_id: entry.id, action: 'steer' });
  assert.deepEqual(Array.from(h.state.pendingSteeringMessages, message => message.text), ['guide before promotion', 'queued first']);
  h.panel.handleServerEvent({ type: 'steering_applied', session_id: 'a', operation_id: 'op', count: 2 });
  assert.equal(h.state.pendingSteeringMessages.length, 0);
  assert.deepEqual(Array.from(h.state.messages, message => message.text), ['guide before promotion', 'queued first']);
});

test('rejected actions preserve queued input and recalls after switching sessions do not overwrite another composer', async () => {
  const h = harness();
  await h.panel.handleUserMessage('keep me');
  const entry = h.state.pendingSteeringMessages[0];
  h.panel.handleQueuedMessageAction(entry.id, 'steer');
  h.panel.handleServerEvent({ type: 'error', session_id: 'a', operation_id: 'op', request_id: entry.id,
    command_error: true, command: 'queued_message_action', error_type: 'follow_up_rejected', message: 'Full' });
  assert.equal(entry.followUpFailed, undefined);
  assert.equal(entry.followUpAction, undefined);
  h.panel.handleQueuedMessageAction(entry.id, 'edit');
  h.panel.displayedSessionId = 'b';
  h.panel.handleServerEvent({ type: 'queued_message_updated', session_id: 'a', operation_id: 'op', request_id: entry.id, action: 'edit' });
  assert.equal(h.messages.filter(message => message.type === 'restoreFollowUp').length, 0);
  assert.equal(entry.followUpFailed, true);
  h.panel.displayedSessionId = 'a';
  h.panel.handleQueuedMessageAction(entry.id, 'edit');
  assert.equal(h.actions.length, 2); // The final recall is local; Core already removed it.
  assert.equal(h.messages.find(message => message.type === 'restoreFollowUp').text, 'keep me');
});

test('disable queue takes effect before settings finish saving and preserves already queued input', async () => {
  const h = harness();
  await h.panel.handleUserMessage('already queued');
  let saved;
  h.config.update = () => new Promise(resolve => { saved = resolve; });
  const changingMode = h.panel.setFollowUpMode('steer');
  await h.panel.handleUserMessage('immediate guidance');
  assert.deepEqual(h.calls.map(call => call.mode), ['queue', 'steer']);
  assert.equal(h.state.pendingSteeringMessages[0].followUpMode, 'queue');
  saved();
  await changingMode;
  assert.equal(h.config.followUpMode, 'steer');
  h.config.update = async () => { throw new Error('read only'); };
  await h.panel.setFollowUpMode('queue');
  assert.equal(h.panel.followUpMode, 'steer');
  assert.ok(h.messages.some(message => message.message?.text?.includes('保存跟进处理方式失败')));
});
