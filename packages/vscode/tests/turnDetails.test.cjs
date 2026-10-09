const assert = require('node:assert/strict');
const { test } = require('node:test');
const { loadPanel } = require('./helpers/chatPanelHarness.cjs');

const details = { session_id: 'a', started_at: '2026-10-10T01:00:00Z', ended_at: '2026-10-10T01:00:03Z', tool_call_count: 2, thinking_count: 1, source: 'recorded' };

test('old Gateway history and terminal events still expose per-round details', () => {
  const { panel, messages } = loadPanel(async () => ({ ok: true, json: async () => ({}) }));
  panel.handleSessionHistory({ type: 'session_history', session_id: 'a', messages: [
    { uuid: 'u', role: 'user', content: 'question', timestamp: details.started_at },
    { uuid: 'a', role: 'assistant', timestamp: details.ended_at, content: [{ type: 'thinking', thinking: 'reason' }, { type: 'text', text: 'done' }] },
  ] });
  const state = panel.getSessionState('a');
  const restored = state.messages.find(message => message.role === 'assistant').turnDetails;
  assert.equal(restored.session_id, 'a');
  assert.equal(restored.source, 'history');
  assert.equal(restored.duration_ms, 3000);
  assert.equal(restored.thinking_count, 1);
  assert.equal(restored.retry_count, undefined);
  panel.addMessageOnState(state, 'user', 'next', false);
  state.activeOperationId = 'op';
  panel.handleServerEvent({ type: 'stream_text', session_id: 'a', operation_id: 'op', text: 'done again' });
  panel.handleServerEvent({ type: 'turn_complete', session_id: 'a', operation_id: 'op' });
  assert.equal(messages.findLast(message => message.type === 'messageForkable').turnDetails.session_id, 'a');
});

test('round details stay on their own reply across session switches and restoration', () => {
  const { panel, messages } = loadPanel(async () => ({ ok: true, json: async () => ({}) }));
  const state = panel.getSessionState('a');
  state.activeOperationId = 'op';
  panel.handleServerEvent({ type: 'stream_text', session_id: 'a', operation_id: 'op', text: 'done' });
  panel.displayedSessionId = 'b';
  panel.handleServerEvent({ type: 'turn_complete', session_id: 'a', operation_id: 'op', assistant_message_uuid: 'reply', turn_details: details });
  assert.equal(state.messages[0].turnDetails, details);
  assert.equal(panel.getSessionState('b').messages.length, 0);
  assert.ok(!messages.some(message => message.type === 'messageForkable'));
  panel.handleSessionHistory({ type: 'session_history', session_id: 'a', messages: [{ uuid: 'reply', role: 'assistant', content: 'done', timestamp: details.ended_at, turn_details: details }] });
  assert.equal(state.messages[0].turnDetails, details);
});

test('terminal details reach the webview before copy and a replyless interruption preserves the prior round', () => {
  const { panel, messages } = loadPanel(async () => ({ ok: true, json: async () => ({}) }));
  const state = panel.getSessionState('a');
  state.activeOperationId = 'op';
  panel.handleServerEvent({ type: 'stream_text', session_id: 'a', operation_id: 'op', text: 'done' });
  panel.handleServerEvent({ type: 'turn_complete', session_id: 'a', operation_id: 'op', assistant_message_uuid: 'reply', turn_details: details });
  assert.equal(messages.find(message => message.type === 'messageForkable').turnDetails, details);
  const previous = state.messages[0];
  panel.addMessageOnState(state, 'user', 'next', false);
  state.activeOperationId = 'next-op';
  panel.displayedSessionId = 'b';
  panel.handleServerEvent({ type: 'turn_complete', session_id: 'a', operation_id: 'next-op', reason: 'interrupted', turn_details: { ...details, tool_call_count: 0 } });
  assert.equal(previous.turnDetails, details);
});
