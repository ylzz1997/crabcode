const assert = require('node:assert/strict');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const { loadPanel } = require('./helpers/chatPanelHarness.cjs');

const policy = {
  configured_mode: 'bypassPermissions', effective_mode: 'plan',
  allow: [], ask: [], deny: [{ tool: 'Bash', command: '<script>rm *</script>' }], runtime_allow_count: 1,
};

test('loaded permissions reach the chat menu, respect plan mode, and clear on session switch', async () => {
  const h = loadPanel(async () => ({ ok: true, json: async () => ({ permission_mode: 'default', permission_policy: policy }) }));
  await h.panel.runtimeControls.refresh('a');
  const update = h.messages.filter(m => m.type === 'runtimeControls').at(-1);
  assert.match(update.permissionSummary.inheritedDescription, /继承后：完全访问/);
  assert.match(update.permissionSummary.current, /计划模式（只读）/);
  const sent = [];
  const dom = new JSDOM(h.panel.getHtmlForWebview({}), {
    runScripts: 'dangerously', pretendToBeVisual: true,
    beforeParse(window) {
      window.acquireVsCodeApi = () => ({ postMessage: m => sent.push(m), getState: () => ({}), setState() {} });
      window.HTMLElement.prototype.scrollIntoView = function() {};
    },
  });
  try {
    const send = data => dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data }));
    const doc = dom.window.document;
    send({ type: 'sessionInfo', sessionId: 'a' });
    send(update);
    assert.equal(doc.getElementById('perm-label').textContent, '默认 · 完全访问');
    assert.match(doc.getElementById('perm-effective').textContent, /计划模式（只读）/);
    assert.match(doc.getElementById('perm-rule-text').textContent, /<script>rm \*<\/script>/);
    assert.equal(doc.querySelector('#perm-rule-text script'), null);
    doc.getElementById('perm-btn').click();
    assert.ok(sent.some(m => m.type === 'requestPermissionPolicy'));
    send({ type: 'sessionInfo', sessionId: 'b' });
    assert.equal(doc.getElementById('perm-label').textContent, '默认 · 暂无法读取');
    assert.equal(doc.getElementById('perm-rule-text').textContent, '');
    send(update); // late response for a cannot populate b
    assert.equal(doc.getElementById('perm-rule-text').textContent, '');
  } finally { dom.window.close(); }
});

test('permission changes refresh status and old Gateways remain explicitly unknown', async () => {
  const h = loadPanel(async () => ({ ok: true, json: async () => ({ permission_mode: 'default' }) }));
  h.panel.handleServerEvent({ type: 'permission_mode_change', session_id: 'a', permission_mode: 'default' });
  await h.panel.runtimeControls.whenSettled('a');
  const update = h.messages.filter(m => m.type === 'runtimeControls').at(-1);
  assert.equal(update.permissionSummary.inheritedLabel, '暂无法读取');
  assert.equal(update.permissionSummary.effectiveDanger, false);
});
