const assert = require('node:assert/strict');
const { test } = require('node:test');
const { loadPanel } = require('./helpers/chatPanelHarness.cjs');
const tick = () => new Promise(resolve => setImmediate(resolve));
const options = h => h.messages.filter(m => m.type === 'options').at(-1);

function harness() {
  const requests = [], switches = [];
  const h = loadPanel(async url => {
    const request = new URL(url);
    requests.push(request);
    const sessionId = request.searchParams.get('session_id');
    return { ok: true, json: async () => request.pathname === '/session/status'
      ? { session_id: sessionId, model_profile: sessionId === 'a' ? 'saved-profile' : 'other-profile', model: 'provider-model', mode: 'agent' }
      : [{ name: 'default-profile' }, { name: 'saved-profile' }, { name: 'other-profile' }] };
  });
  h.config.chatModelDefault = 'default-profile';
  h.connection.sendSwitchModel = (...args) => switches.push(args);
  h.connection.sendSetPermissionMode = () => {};
  return { ...h, requests, switches };
}

test('resume and configuration synchronization preserve the server model instead of sending the default', async () => {
  const h = harness();
  h.connection.modelName = null; // Resume acknowledgements contain only the session ID.
  await h.panel.runtimeControls.refresh('a', true);
  await h.panel.syncSessionPreferencesFromSettings();
  await tick();
  assert.deepEqual(h.switches, []);
  assert.equal(options(h).selectedModel, 'saved-profile');
  assert.equal(options(h).modelPending, false);
  assert.equal(h.panel.getSessionState('a').modelName, 'saved-profile');
});

test('a saved profile remains visible even when the configured menu only includes the default', async () => {
  const h = harness();
  h.config.chatModels = ['default-profile'];
  await h.panel.runtimeControls.refresh('a', true);
  await h.panel.pushChatOptions();
  assert.equal(options(h).selectedModel, 'saved-profile');
  assert.ok(options(h).models.includes('saved-profile'));
});

test('late model catalogs and runtime status cannot repaint the newly selected session', async () => {
  let release;
  const h = loadPanel(async url => {
    const session = new URL(url).searchParams.get('session_id');
    if (session === 'a') await new Promise(resolve => { release = resolve; });
    return { ok: true, json: async () => [{ name: session + '-profile', group: session }] };
  });
  h.panel.getSessionState('a').modelName = 'a-profile';
  const old = h.panel.pushChatOptions();
  h.panel.displayedSessionId = 'b';
  h.connection.sessionId = 'b';
  h.panel.getSessionState('b').modelName = 'b-profile';
  await h.panel.pushChatOptions();
  release();
  await old;
  assert.equal(options(h).sessionId, 'b');
  assert.equal(options(h).selectedModel, 'b-profile');
  assert.deepEqual(Array.from(options(h).models), ['b-profile']);
  assert.equal(h.panel.lastNonEmptyModelGroups['b-profile'], 'b');
});

test('background model changes stay in their session and active changes refresh the selector', async () => {
  const h = harness();
  await h.panel.runtimeControls.refresh('a');
  await tick();
  const count = h.messages.length;
  h.listeners.message({ type: 'model_change', session_id: 'b', model_profile: 'background-profile' });
  await tick();
  assert.equal(h.messages.length, count);
  assert.equal(h.panel.getSessionState('b').modelName, 'background-profile');
  h.listeners.message({ type: 'model_change', session_id: 'a', model_profile: 'active-profile' });
  await tick();
  assert.equal(options(h).selectedModel, 'active-profile');
});

test('an older status response cannot undo a newer acknowledged model change', async () => {
  let release;
  const h = loadPanel(async url => {
    if (new URL(url).pathname === '/session/status') {
      await new Promise(resolve => { release = resolve; });
      return { ok: true, json: async () => ({ model_profile: 'old-profile' }) };
    }
    return { ok: true, json: async () => [{ name: 'new-profile' }] };
  });
  const read = h.panel.runtimeControls.refresh('a');
  await tick();
  h.listeners.message({ type: 'model_change', session_id: 'a', model_profile: 'new-profile' });
  release();
  await read;
  await tick();
  assert.equal(h.panel.getSessionState('a').modelName, 'new-profile');
  assert.equal(options(h).selectedModel, 'new-profile');
});
