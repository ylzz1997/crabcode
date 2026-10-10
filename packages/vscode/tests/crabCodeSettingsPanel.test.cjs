const assert = require("node:assert/strict");
const { test } = require("node:test");
const { harness } = require("./helpers/settingsHarness.cjs");

test("local preferences preserve user/workspace precedence and validate in the host", async () => {
  const h = harness({
    initial: { followUpMode: "queue" },
    workspace: { followUpMode: "steer" },
  });
  await h.settle();
  assert.equal(
    h.doc.querySelector('[aria-label="跟进处理方式"]').value,
    "steer",
  );
  assert.match(h.doc.body.textContent, /当前由工作区覆盖/);
  h.change("聊天偏好保存范围", "workspace");
  h.change("跟进处理方式", "queue");
  await h.settle();
  assert.equal(h.writes.at(-1).target, 2);
  assert.equal(h.workspace.followUpMode, "queue");
  const rejected = await h.rpc({
    action: "saveLocal",
    key: "fileUploadMaxSizeMb",
    value: -1,
    scope: "user",
  });
  assert.match(rejected.error, /整数/);
  assert.equal(h.writes.length, 1);
  assert.match(
    (
      await h.rpc({
        action: "saveLocal",
        key: "password",
        value: "x",
        scope: "user",
      })
    ).error,
    /未知/,
  );
  assert.ok(!h.panel.webview.html.includes("test-only-secret"));
  h.dom.window.close();
});

test("compaction uses authenticated Gateway writes with cwd, selected source, zero and null", async () => {
  const h = harness({ section: "context" });
  await h.settle();
  h.change("压缩预留 token", "0");
  await h.settle();
  const save = h.requests.at(-1);
  assert.equal(save.headers.Authorization, "Bearer test-only-secret");
  assert.equal(save.body.compact_buffer_tokens, 0);
  assert.equal(save.body.source, "projectSettings");
  assert.ok(save.body.cwd.endsWith("settings-project"));
  h.change("提前触发阈值", "");
  await h.settle();
  assert.equal(h.requests.at(-1).body.max_context_length, null);
  h.change("压缩预留 token", "-1");
  await h.settle();
  assert.match(h.doc.querySelector(".status").textContent, /有效整数/);
  assert.equal(h.requests.filter((item) => item.body).length, 2);
  h.dom.window.close();
});

test("failed saves retain drafts across navigation and retry successfully", async () => {
  const h = harness({ section: "context" });
  await h.settle();
  h.fail({ message: "write failed" });
  h.change("压缩预留 token", "30000");
  await h.settle();
  assert.match(h.doc.querySelector(".status").textContent, /write failed/);
  h.click("常规");
  await h.settle();
  h.click("上下文压缩");
  await h.settle();
  assert.equal(
    h.doc.querySelector('[aria-label="压缩预留 token"]').value,
    "30000",
  );
  h.fail(null);
  h.change("压缩预留 token", "30000");
  await h.settle();
  assert.equal(h.state.runtime.compact_buffer_tokens, 30000);
  h.dom.window.close();
});

test("model editor saves only changes and never writes redacted credentials back", async () => {
  const h = harness({ section: "models" });
  await h.settle();
  h.click("编辑");
  await h.settle();
  h.change(
    "HTTP Headers",
    '{"Authorization":"[redacted]","X-Client":"new"}',
    "input",
  );
  h.change("最大输出 Token", "2048", "input");
  h.click("保存");
  await h.settle();
  const mutation = h.requests.filter((item) => item.body).at(-1).body;
  assert.equal(mutation.action, "upsert_model");
  assert.equal(mutation.config.max_tokens, 2048);
  assert.deepEqual(mutation.config.http_headers, { "X-Client": "new" });
  assert.equal(mutation.config.provider, undefined);
  assert.equal(h.changedModels, 1);
  h.dom.window.close();
});

test("model deletion targets a layer that actually contains the entry", async () => {
  const h = harness({ section: "models" });
  await h.settle();
  h.change("保存到配置层", "userSettings");
  h.click("删除");
  await h.settle();
  assert.match(h.doc.querySelector(".status").textContent, /不在当前保存层/);
  assert.equal(h.requests.filter((item) => item.body).length, 0);
  h.change("保存到配置层", "projectSettings");
  h.click("删除");
  await h.settle();
  assert.equal(h.requests.at(-1).body.source, "projectSettings");
  assert.equal(h.confirmations.length, 1);
  h.dom.window.close();
});

test("prompt drafts survive section navigation and changes to user-append prompts", async () => {
  const h = harness({ section: "prompts" });
  await h.settle();
  h.change("模板名称", "My draft", "input");
  h.change("上下文压缩提示", "Keep decisions", "input");
  h.click("常规");
  await h.settle();
  h.click("提示词");
  await h.settle();
  assert.equal(
    h.doc.querySelector('[aria-label="模板名称"]').value,
    "My draft",
  );
  h.change("新增用户提示", "remember tests", "input");
  h.click("添加提示");
  await h.settle();
  assert.equal(
    h.doc.querySelector('[aria-label="上下文压缩提示"]').value,
    "Keep decisions",
  );
  h.click("保存并启用");
  await h.settle();
  const mutation = h.requests.filter((item) => item.body).at(-1).body;
  assert.equal(mutation.template_name, "My draft");
  assert.equal(mutation.sections.compact_prompt, "Keep decisions");
  h.dom.window.close();
});

test("old Gateways show a retryable error while local settings remain available", async () => {
  const h = harness();
  await h.settle();
  h.fail({ status: 404, message: "Not Found" });
  h.click("模型");
  await h.settle();
  assert.match(h.doc.body.textContent, /升级 Gateway/);
  h.click("常规");
  await h.settle();
  h.change("发送快捷键", "mod_enter");
  await h.settle();
  assert.equal(h.user.composerSendKey, "mod_enter");
  h.dom.window.close();
});

test("connection switches discard stale reads and reject stale mutations", async () => {
  const h = harness();
  await h.settle();
  let release;
  h.pause(
    new Promise((resolve) => {
      release = resolve;
    }),
  );
  h.click("上下文压缩");
  await new Promise((resolve) => setImmediate(resolve));
  h.changeConnection();
  release();
  await h.settle();
  assert.ok(h.requests.at(-1).url.startsWith("http://other.example/"));
  const oldCount = h.requests.length;
  const stale = await h.rpc({
    action: "mutate",
    generation: 1,
    resource: "runtime",
    mutation: {
      action: "set_compaction",
      source: "userSettings",
      compact_buffer_tokens: 1,
    },
  });
  assert.match(stale.error, /已切换/);
  assert.equal(h.requests.length, oldCount);
  h.dom.window.close();
});

test("search jumps to a specific item and usage distinguishes missing records from zero", async () => {
  const h = harness();
  await h.settle();
  h.change("搜索设置", "预留", "input");
  const result = h.doc.querySelector(".search-result");
  assert.ok(result);
  result.click();
  await h.settle();
  assert.equal(h.doc.querySelector("h1").textContent, "上下文压缩");
  assert.equal(h.doc.activeElement.id, "compact_buffer_tokens");
  h.click("使用情况");
  await h.settle();
  assert.match(
    h.doc.querySelector('[data-section="usage"].settings-page').textContent,
    /未记录/,
  );
  assert.equal(new URL(h.requests.at(-1).url).searchParams.has("cwd"), false);
  h.change("统计范围", "project");
  h.click("查询");
  await h.settle();
  assert.ok(new URL(h.requests.at(-1).url).searchParams.has("cwd"));
  h.dom.window.close();
});

test("Computer Use inherit removes only the selected override and sources stay synchronized", async () => {
  const h = harness({
    section: "tools",
    initial: { computerUseTargetScope: "desktop" },
  });
  await h.settle();
  h.change("Computer Use 操作目标覆盖", "");
  await h.settle();
  assert.equal(h.user.computerUseTargetScope, undefined);
  h.change("保存到配置层", "localSettings");
  h.click("上下文压缩");
  await h.settle();
  const page = h.doc.querySelector('.settings-page[data-section="context"]');
  assert.equal(
    page.querySelector('[aria-label="保存到配置层"]').value,
    "localSettings",
  );
  h.dom.window.close();
});

test("refreshing General preserves an unsaved model editor in another section", async () => {
  const h = harness({ section: "models" });
  await h.settle();
  h.click("编辑");
  await h.settle();
  h.change("模型 ID", "unsaved-model", "input");
  h.click("常规");
  await h.settle();
  h.click("刷新");
  await h.settle();
  h.click("模型");
  await h.settle();
  assert.equal(
    h.doc.querySelector('.model-editor [aria-label="模型 ID"]').value,
    "unsaved-model",
  );
  h.dom.window.close();
});

test("prompt import and export use host file dialogs and portable JSON", async () => {
  const h = harness({ section: "prompts" });
  await h.settle();
  const writes = [];
  h.vscode.window.showSaveDialog = async () => ({ fsPath: "/export.json" });
  h.vscode.workspace.fs.writeFile = async (uri, bytes) =>
    writes.push({ uri, text: bytes.toString() });
  h.click("导出全部");
  await h.settle();
  assert.equal(JSON.parse(writes[0].text).name, "模板一");
  h.vscode.window.showOpenDialog = async () => [{ fsPath: "/import.json" }];
  h.vscode.workspace.fs.readFile = async () =>
    Buffer.from(
      JSON.stringify({
        templates: [
          { name: "Imported", sections: { compact_prompt: "keep decisions" } },
        ],
      }),
    );
  h.click("导入模板");
  await h.settle();
  const saved = h.requests
    .filter((item) => item.body?.action === "save_template")
    .at(-1).body;
  assert.equal(saved.source, "projectSettings");
  assert.equal(saved.template_name, "Imported");
  assert.equal(saved.sections.compact_prompt, "keep decisions");
  h.dom.window.close();
});

test("invalid connection addresses do not prevent local settings from opening", async () => {
  const h = harness({ initial: { serverUrl: "not a url" } });
  await h.settle();
  assert.match(h.doc.querySelector(".context-bar").textContent, /连接地址无效/);
  h.change("发送快捷键", "mod_enter");
  await h.settle();
  assert.equal(h.user.composerSendKey, "mod_enter");
  h.dom.window.close();
});

test("local preferences can save while a Gateway request is still pending", async () => {
  const h = harness();
  await h.settle();
  let release;
  h.pause(
    new Promise((resolve) => {
      release = resolve;
    }),
  );
  h.click("上下文压缩");
  await new Promise((resolve) => setImmediate(resolve));
  h.click("常规");
  await new Promise((resolve) => setImmediate(resolve));
  h.change("发送快捷键", "mod_enter");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(h.user.composerSendKey, "mod_enter");
  release();
  await h.settle();
  h.dom.window.close();
});
