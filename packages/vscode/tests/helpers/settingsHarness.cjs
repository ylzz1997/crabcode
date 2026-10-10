const { buildSync } = require("esbuild");
const { JSDOM } = require("jsdom");
const path = require("node:path");
const vm = require("node:vm");
const hostCode = buildSync({
  entryPoints: [path.join(__dirname, "../../src/crabCodeSettingsPanel.ts")],
  bundle: true,
  write: false,
  platform: "node",
  format: "cjs",
  external: ["vscode"],
}).outputFiles[0].text;
const browserCode = buildSync({
  entryPoints: [path.join(__dirname, "../../src/settings/webview.ts")],
  bundle: true,
  write: false,
  platform: "browser",
  loader: { ".css": "empty" },
}).outputFiles[0].text;

function fixtures(cwd) {
  const sources = [
    {
      id: "userSettings",
      label: "用户配置",
      path: path.join(
        path.parse(cwd).root,
        "user",
        ".crabcode",
        "settings.json",
      ),
      writable: true,
    },
    {
      id: "projectSettings",
      label: "项目配置",
      path: path.join(cwd, ".crabcode", "settings.json"),
      writable: true,
    },
    {
      id: "localSettings",
      label: "项目本地配置",
      path: path.join(cwd, ".crabcode", "settings.local.json"),
      writable: true,
    },
  ];
  const common = { cwd, editable_sources: sources, warnings: [] };
  return {
    runtime: {
      ...common,
      auto_compact_enabled: true,
      compact_buffer_tokens: 20000,
      max_context_length: null,
      computer_use_target_scope: "app_window",
      computer_use_delivery_policy: "allow_foreground",
      snapshot_enabled: true,
      snapshot_max_size_mb: 10,
      extra_tools: [],
      extra_tools_by_source: {},
    },
    models: {
      ...common,
      default_model: "example",
      groups: { shared: { provider: "openai" } },
      group_sources: { shared: [sources[1].path] },
      models: [
        {
          name: "example",
          configured: {
            provider: "openai",
            model: "model-a",
            http_headers: { Authorization: "[redacted]", "X-Client": "test" },
          },
          effective: {
            provider: "openai",
            model: "model-a",
            context_window: 128000,
          },
          sources: [sources[1].path],
        },
      ],
    },
    prompts: {
      ...common,
      active_template_id: null,
      templates: [
        {
          id: "one",
          name: "模板一",
          source: "projectSettings",
          sections: { extra: "saved" },
        },
      ],
      user_prompts: [
        {
          id: "note",
          text: "使用中文",
          enabled: true,
          source: "projectSettings",
        },
      ],
      sections: [
        { key: "extra", label: "额外系统提示", default_text: "" },
        {
          key: "compact_prompt",
          label: "上下文压缩提示",
          default_text: "default compact",
        },
      ],
    },
    usage: {
      start: "2026-10-01",
      end: "2026-10-11",
      timezone: "Asia/Shanghai",
      scope: "global",
      days: [
        { date: "2026-10-10", total_tokens: 3000 },
        { date: "2026-10-11", total_tokens: null },
      ],
      models: [
        {
          model: "example",
          model_id: "model-a",
          provider: "openai",
          total_tokens: 3000,
          recorded_request_count: 1,
          points: [{ date: "2026-10-10", total_tokens: 3000 }],
        },
      ],
      summary: {
        total_tokens: 3000,
        input_tokens: 2000,
        output_tokens: 1000,
        request_count: 2,
        unknown_requests: 1,
        missing_requests: 0,
        partial_requests: 0,
      },
    },
  };
}
function harness({ initial = {}, workspace = {}, section = "general" } = {}) {
  const cwd = path.join(path.parse(process.cwd()).root, "settings-project");
  const state = fixtures(cwd);
  const user = {
    serverUrl: "wss://gateway.example/ws",
    password: "test-only-secret",
    ...initial,
  };
  const writes = [],
    requests = [],
    events = [],
    confirmations = [];
  let receive,
    onConfig,
    onFolders,
    dom,
    failure,
    pause,
    changedModels = 0;
  const uri = (fsPath) => ({ fsPath, toString: () => fsPath });
  const panel = {
    reveal() {},
    onDidDispose() {},
    webview: {
      cspSource: "vscode-resource:",
      asWebviewUri: (value) => value.fsPath,
      onDidReceiveMessage: (fn) => {
        receive = fn;
      },
      postMessage: async (event) => {
        events.push(event);
        if (dom)
          dom.window.dispatchEvent(
            new dom.window.MessageEvent("message", { data: event }),
          );
        return true;
      },
    },
  };
  const vscode = {
    ViewColumn: { Active: 1 },
    ConfigurationTarget: { Global: 1, Workspace: 2 },
    Uri: {
      file: uri,
      joinPath: (base, ...parts) => uri(path.join(base.fsPath, ...parts)),
    },
    commands: { executeCommand: async () => undefined },
    window: {
      createWebviewPanel: () => panel,
      showWarningMessage: async (message) => {
        confirmations.push(message);
        return "确认";
      },
      showOpenDialog: async () => undefined,
      showSaveDialog: async () => undefined,
    },
    workspace: {
      workspaceFolders: [{ uri: uri(cwd) }],
      onDidChangeWorkspaceFolders: (fn) => {
        onFolders = fn;
      },
      onDidChangeConfiguration: (fn) => {
        onConfig = fn;
      },
      getConfiguration: () => ({
        get: (key, fallback) => workspace[key] ?? user[key] ?? fallback,
        inspect: (key) => ({
          globalValue: user[key],
          workspaceValue: workspace[key],
        }),
        update: async (key, value, target) => {
          writes.push({ key, value, target });
          (target === 1 ? user : workspace)[key] = value;
          onConfig({ affectsConfiguration: () => true });
        },
      }),
      fs: {},
    },
  };
  const module = { exports: {} };
  const fetch = async (url, init) => {
    const resource = String(url).includes("runtime-settings")
      ? "runtime"
      : String(url).includes("model-settings")
        ? "models"
        : String(url).includes("prompt-settings")
          ? "prompts"
          : "usage";
    const body = init.body && JSON.parse(init.body);
    requests.push({ url: String(url), ...init, body });
    if (pause) {
      const wait = pause;
      pause = undefined;
      await wait;
    }
    if (failure)
      return {
        ok: false,
        status: failure.status ?? 500,
        json: async () => ({ detail: failure.message }),
      };
    if (body) {
      if (resource === "runtime") Object.assign(state.runtime, body);
      if (body.action === "save_template") {
        const id = body.template_id ?? "new-template";
        state.prompts.templates = state.prompts.templates
          .filter((item) => item.id !== id)
          .concat({
            id,
            name: body.template_name,
            sections: body.sections,
            source: body.source,
          });
        state.prompts.active_template_id = id;
      }
      if (body.action === "add_user_prompt")
        state.prompts.user_prompts.push({
          id: "new",
          text: body.prompt_text,
          enabled: true,
          source: body.source,
        });
      if (body.action === "delete_user_prompt")
        state.prompts.user_prompts = state.prompts.user_prompts.filter(
          (item) => item.id !== body.prompt_id,
        );
    }
    return {
      ok: true,
      json: async () => JSON.parse(JSON.stringify(state[resource])),
    };
  };
  vm.runInNewContext(hostCode, {
    module,
    exports: module.exports,
    require: (name) => (name === "vscode" ? vscode : require(name)),
    URL,
    AbortController,
    fetch,
    setTimeout,
    clearTimeout,
    Buffer,
  });
  module.exports.CrabCodeSettingsPanel.show(uri("/extension"), section, () => {
    changedModels++;
  });
  dom = new JSDOM('<div id="app"></div>', {
    runScripts: "outside-only",
    url: "https://settings.invalid/",
  });
  dom.window.HTMLElement.prototype.scrollIntoView = function () {};
  dom.window.acquireVsCodeApi = () => ({
    postMessage: receive,
    getState: () => undefined,
    setState() {},
  });
  dom.window.eval(browserCode);
  const instance = module.exports.CrabCodeSettingsPanel.current;
  const settle = async () => {
    for (let i = 0; i < 6; i++) {
      await new Promise((resolve) => setImmediate(resolve));
      await instance.queue;
    }
  };
  const doc = dom.window.document;
  const click = (text) => {
    const node = [...doc.querySelectorAll("button")].find(
      (node) => node.textContent === text && !node.closest("[hidden]"),
    );
    if (!node) throw new Error(`Button not found: ${text}`);
    node.click();
    return node;
  };
  const change = (label, value, event = "change") => {
    const node = doc.querySelector(`[aria-label="${label}"]`);
    if (!node) throw new Error(`Input not found: ${label}`);
    if (node.type === "checkbox") node.checked = value;
    else node.value = value;
    node.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    if (event === "change")
      node.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    return node;
  };
  return {
    dom,
    doc,
    state,
    user,
    workspace,
    writes,
    requests,
    events,
    panel,
    settle,
    click,
    change,
    confirmations,
    vscode,
    fail: (value) => {
      failure = value;
    },
    pause: (promise) => {
      pause = promise;
    },
    changeConnection: () => {
      user.serverUrl = "ws://other.example/ws";
      onConfig({ affectsConfiguration: () => true });
    },
    rpc: async (message) => {
      receive({
        id: 9999,
        type: "request",
        generation: instance.generation,
        ...message,
      });
      await settle();
      return events.filter((event) => event.id === 9999).at(-1);
    },
    get changedModels() {
      return changedModels;
    },
  };
}
module.exports = { harness, fixtures };
