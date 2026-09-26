const assert = require('node:assert/strict');
const { test } = require('node:test');
const vm = require('node:vm');
const { loadPanel } = require('./helpers/chatPanelHarness.cjs');

function harness() {
  const states = { a: { reasoning_effort: 'medium', ultra_mode: false }, b: { reasoning_effort: 'low', ultra_mode: false } };
  const requests = [];
  let failure = false;
  let pause;
  const host = loadPanel(async (url, init) => {
    const body = init.body ? JSON.parse(init.body) : null;
    const session = body?.session_id || new URL(url).searchParams.get('session_id');
    const state = states[session];
    requests.push({ url, body, session });
    if (pause) { const wait = pause; pause = null; await wait; }
    if (failure && body) return { ok: false, json: async () => ({ detail: 'provider rejected' }) };
    if (body?.effort === 'auto') delete state.reasoning_effort;
    else if (body?.effort) state.reasoning_effort = body.effort;
    if (typeof body?.enabled === 'boolean') state.ultra_mode = body.enabled;
    const result = body?.effort ? (state.reasoning_effort ? { reasoning_effort: state.reasoning_effort } : {})
      : typeof body?.enabled === 'boolean' ? { ultra_mode: state.ultra_mode } : { ...state };
    return { ok: true, json: async () => result };
  });
  return { ...host, states, requests, fail: () => { failure = true; }, pause: promise => { pause = promise; } };
}

test('effort and Ultra cross the host bridge, use session IDs, and persist acknowledged values', async () => {
  const h = harness();
  await h.panel.runtimeControls.refresh('a');
  await h.panel.showOrSetReasoningEffort('max');
  await h.panel.setUltraMode(true);
  assert.deepEqual(h.states.a, { reasoning_effort: 'max', ultra_mode: true });
  assert.ok(h.requests.some(r => r.url.endsWith('/config/reasoning-effort') && r.body.effort === 'max' && r.body.session_id === 'a'));
  assert.ok(h.requests.some(r => r.url.endsWith('/config/ultra-mode') && r.body.enabled === true));
  const update = h.messages.filter(m => m.type === 'runtimeControls').at(-1);
  assert.equal(update.pending, false);
  assert.equal(update.ultra_mode, true);
  assert.equal(update.reasoning_effort, 'max');
  assert.equal([...h.saved.values()].at(-1).ultra_mode, true);
  // Disabling is a persisted preference, not a missing value.
  await h.panel.setUltraMode(false);
  h.states.a = { reasoning_effort: 'low', ultra_mode: true };
  await h.panel.runtimeControls.refresh('a', true);
  assert.deepEqual(h.states.a, { reasoning_effort: 'max', ultra_mode: false });
});

test('auto is selectable and clears a previously persisted effort', async () => {
  const h = harness();
  await h.panel.showOrSetReasoningEffort('max');
  await h.panel.showOrSetReasoningEffort('auto');
  assert.equal(h.states.a.reasoning_effort, undefined);
  assert.equal(h.panel.runtimeControls.get('a').reasoning_effort, undefined);
  assert.ok(h.requests.some(r => r.url.endsWith('/config/reasoning-effort') && r.body.effort === 'auto' && r.body.session_id === 'a'));
  assert.equal([...h.saved.values()].at(-1)?.reasoning_effort, undefined);
  h.states.a = { ultra_mode: false };
  await h.panel.runtimeControls.refresh('a', true);
  assert.equal(h.states.a.reasoning_effort, undefined);
});

test('automatic effort stays automatic until explicitly selected; invalid effort never reaches Gateway', async () => {
  const h = harness();
  h.states.a = { ultra_mode: false };
  await h.panel.runtimeControls.refresh('a', true);
  assert.equal(h.panel.runtimeControls.get('a').reasoning_effort, undefined);
  assert.equal(h.saved.size, 0);
  await h.panel.showOrSetReasoningEffort('ultra');
  assert.equal(h.requests.length, 1);
  assert.equal(h.saved.size, 0);
});

test('a rejected setting preserves the confirmed UI and saved preference and clears pending state', async () => {
  const h = harness();
  await h.panel.showOrSetReasoningEffort('high');
  h.fail();
  await h.panel.setUltraMode(true);
  assert.equal(h.panel.runtimeControls.get('a').ultra_mode, false);
  assert.equal(h.panel.runtimeControls.get('a').pending, false);
  assert.equal([...h.saved.values()].at(-1).ultra_mode, undefined);
  assert.ok(h.messages.some(m => m.type === 'newMessage' && m.message.text.includes('provider rejected')));
});

test('pending settings finish before sending; switching sessions cannot redirect the message or late UI reply', async () => {
  const h = harness();
  let release;
  h.pause(new Promise(resolve => { release = resolve; }));
  const setting = h.panel.showOrSetReasoningEffort('xhigh');
  const send = h.panel.handleUserMessage('use xhigh');
  h.panel.displayedSessionId = 'b';
  const count = h.messages.length;
  await Promise.resolve();
  assert.equal(h.sent.length, 0);
  release();
  await Promise.all([setting, send]);
  assert.equal(h.sent[0].sessionId, 'a');
  assert.equal(h.states.a.reasoning_effort, 'xhigh');
  assert.equal(h.states.b.reasoning_effort, 'low');
  assert.ok(!h.messages.slice(count).some(m => m.type === 'runtimeControls'));
});

test('restoring another session or another gateway does not reuse the previous preference', async () => {
  const h = harness();
  await h.panel.showOrSetReasoningEffort('high');
  await h.panel.setUltraMode(true);
  await h.panel.runtimeControls.refresh('b', true);
  assert.deepEqual(h.states.b, { reasoning_effort: 'low', ultra_mode: false });
  h.config.serverUrl = 'ws://other-gateway:4096/ws';
  h.states.a = { reasoning_effort: 'low', ultra_mode: false };
  await h.panel.runtimeControls.refresh('a', true);
  assert.deepEqual(h.states.a, { reasoning_effort: 'low', ultra_mode: false });
});

test('slash Ultra toggles the server state and generated webview JavaScript parses', async () => {
  const h = harness();
  h.states.a.ultra_mode = true;
  await h.panel.setUltraMode(null);
  assert.equal(h.states.a.ultra_mode, false);
  const html = h.panel.getHtmlForWebview({});
  new vm.Script(html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1]);
});

test('composer keeps model and effort in one adaptive group and has no footer mode selector', () => {
  const h = harness();
  const html = h.panel.getHtmlForWebview({});
  assert.match(
    html,
    /class="composer-primary-controls"[\s\S]*id="model-btn"[\s\S]*id="effort-btn"[\s\S]*?<\/div>/,
  );
  assert.ok(!html.includes('id="mode-btn"'));
  assert.ok(!html.includes('id="mode-menu"'));
  assert.ok(html.includes('id="plan-chip"'));
  assert.ok(html.includes('data-action="plan"'));
});

test('a fresh extension host restores persisted preferences after Gateway restart', async () => {
  const first = harness();
  await first.panel.showOrSetReasoningEffort('max');
  await first.panel.setUltraMode(true);
  const reopened = harness();
  for (const [key, value] of first.saved) reopened.saved.set(key, value);
  await reopened.panel.runtimeControls.refresh('a', true);
  assert.deepEqual(reopened.states.a, { reasoning_effort: 'max', ultra_mode: true });
});

test('a late Plan toggle does not change the displayed session capsule', async () => {
  const h = harness();
  let release;
  h.pause(new Promise(resolve => { release = resolve; }));
  const toggle = h.panel.switchMode('plan');
  h.panel.displayedSessionId = 'b';
  release();
  await toggle;
  assert.equal(h.panel.getSessionState('a').mode, 'plan');
  assert.equal(h.panel.getSessionState('b').mode, 'agent');
  assert.ok(!h.messages.some(m => m.type === 'modeChange'));
});
