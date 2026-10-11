const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const { createSlashCommands } = require('./helpers/slashCommands.cjs');
const { loadPanel } = require('./helpers/chatPanelHarness.cjs');

function parse(name, args = '') {
  const actions = [], notices = [];
  createSlashCommands({ postMessage: m => actions.push(JSON.parse(JSON.stringify(m))), showMessage: text => notices.push(text) }).handlers[name](args);
  return { actions, notices };
}

test('both GUI catalogs cover every CLI slash command except terminal exit aliases', () => {
  const cli = fs.readFileSync(path.join(__dirname, '../../cli/crabcode_cli/repl.py'), 'utf8').split('_SLASH_COMMANDS: dict')[1].split('\n}\n')[0];
  const names = [...cli.matchAll(/"(\/[^\"]+)":/g)].map(match => match[1]).filter(name => name !== '/exit' && name !== '/quit');
  const { commands, handlers } = createSlashCommands({ postMessage() {}, showMessage() {} });
  assert.ok(names.length > 30);
  for (const name of names) {
    assert.ok(commands.some(command => command.name === name), name);
    assert.equal(typeof handlers[name], 'function', name);
  }
  const factory = vm.runInNewContext('(' + createSlashCommands.toString() + ')');
  assert.equal(factory({ postMessage() {}, showMessage() {} }).commands.length, commands.length);
});

test('goal lifecycle, budgets and quoted objectives survive parsing with their original casing', () => {
  assert.deepEqual(parse('/goal', 'Ship My API').actions[0], { type: 'manageGoal', action: 'set', objective: 'Ship My API', tokenBudget: null, budgetWasSet: false });
  assert.deepEqual(parse('/goal', 'edit --budget 25000 "Ship My API"').actions[0], { type: 'manageGoal', action: 'edit', objective: 'Ship My API', tokenBudget: 25000, budgetWasSet: true });
  for (const action of ['pause', 'resume', 'complete', 'blocked', 'clear']) {
    assert.deepEqual(parse('/goal', action).actions[0], { type: 'manageGoal', action, objective: null, budgetWasSet: false });
  }
  for (const args of ['set --budget', 'set --budget 12oops Ship', 'edit --budget=0 Ship']) assert.equal(parse('/goal', args).actions.length, 0, args);
});

test('schedule and team requests preserve quoted prompts, options and failure reasons', () => {
  assert.deepEqual(parse('/schedule', 'create --tag nightly --max-runs 2 "Night Job" cron "0 2 * * *" "Check API"').actions[0], {
    type: 'createSchedule', request: { tags: ['nightly'], enabled: true, extra: {}, max_runs: 2, name: 'Night Job', schedule_type: 'cron', schedule: '0 2 * * *', prompt: 'Check API' },
  });
  assert.deepEqual(parse('/team', 'task-fail team-a task-b --agent worker-c "Build failed"').actions[0], {
    type: 'failTeamTask', teamId: 'team-a', taskId: 'task-b', agentId: 'worker-c', reason: 'Build failed',
  });
  assert.deepEqual(parse('/wait', 'agent-a,agent-b --timeout 5000').actions[0], { type: 'waitAgent', agentIds: ['agent-a', 'agent-b'], timeoutMs: 5000 });
});

test('invalid command arguments never emit mutations or silently discard unknown options', () => {
  for (const [command, args] of [['/follow-up', 'later'], ['/queue', 'steer 0'], ['/add', 'whatever'], ['/clear', 'extra'], ['/logs', '--tail 12oops search'], ['/logs', '--clear'], ['/wait', 'agent --timeout x'], ['/schedule', 'create --timeout 1oops Job interval 5s prompt']]) {
    const result = parse(command, args);
    assert.equal(result.actions.length, 0, command + ' ' + args);
    assert.ok(result.notices.length, command);
  }
});

test('the actual webview composer dispatches new slash commands without sending them to the model', () => {
  const h = loadPanel(async () => ({ ok: true, json: async () => ({}) }));
  const sent = [];
  const dom = new JSDOM(h.panel.getHtmlForWebview({}), {
    runScripts: 'dangerously', pretendToBeVisual: true,
    beforeParse(window) {
      window.acquireVsCodeApi = () => ({ postMessage: m => sent.push(JSON.parse(JSON.stringify(m))), getState: () => ({}), setState() {} });
      window.HTMLElement.prototype.scrollIntoView = function() {};
    },
  });
  try {
    const input = dom.window.document.getElementById('input');
    for (const [command, type] of [['/FOLLOW-UP\tsteer', 'setFollowUpMode'], ['/queue steer 2', 'steerQueue'], ['/add group', 'openModelSettings'], ['/del model', 'openModelSettings'], ['/fork', 'forkSession'], ['/goal pause', 'manageGoal']]) {
      input.textContent = command;
      input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
      dom.window.document.getElementById('send-btn').click();
      assert.equal(sent.at(-1).type, type, command);
      assert.equal(input.textContent, '');
    }
    assert.ok(!sent.some(message => message.type === 'sendMessage'));
    assert.deepEqual(sent.find(message => message.type === 'forkSession'), { type: 'forkSession', messageUuid: null });
  } finally { dom.window.close(); }
});
