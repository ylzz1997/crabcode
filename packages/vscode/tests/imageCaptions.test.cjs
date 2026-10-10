const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");

const filename = path.join(__dirname, "../src/chatPanel.ts");
const source = ts.createSourceFile(filename, fs.readFileSync(filename, "utf8"), ts.ScriptTarget.Latest, true);

// Exercise the real history and webview methods without starting the VS Code host.
function loadMethod(name) {
  let method;
  function visit(node) {
    if (ts.isMethodDeclaration(node) && node.name?.getText(source) === name) method = node;
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert.ok(method, name);
  const script = ts.transpileModule(`class Harness { ${method.getText(source)} }; globalThis.Harness = Harness;`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const sandbox = { getNonce: () => "test-nonce" };
  vm.runInNewContext(script, sandbox);
  return sandbox.Harness.prototype[name];
}

const html = loadMethod("getHtmlForWebview").call({}, {});
const script = html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
const scriptSource = ts.createSourceFile("webview.js", script, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);

function webviewFunction(name) {
  let result;
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) result = node.getText(scriptSource);
    ts.forEachChild(node, visit);
  }
  visit(scriptSource);
  assert.ok(result, name);
  return result;
}

test("single and multiple image captions render below their images and escape HTML", () => {
  const sandbox = {
    getToolPresentation: () => ({ kind: "image", glyph: "▧", label: "发送图片", summary: "" }),
    renderToolInput: () => "",
    renderResult: () => "",
  };
  vm.runInNewContext(["escapeHtml", "escapeAttr", "buildToolCardHtml"].map(webviewFunction).join("\n"), sandbox);
  const images = [
    { media_type: "image/png", data: "YQ==", description: "单图 <script>alert(1)</script>\n下一行" },
    { media_type: "image/jpeg", data: "Yg==", description: "第二张" },
  ];
  for (const count of [1, 2]) {
    const rendered = sandbox.buildToolCardHtml({ toolName: "Image", input: {}, result: "attached", collapsed: false, images: images.slice(0, count) });
    assert.equal((rendered.match(/<figure /g) || []).length, count);
    assert.equal((rendered.match(/<figcaption>/g) || []).length, count);
    assert.equal((rendered.match(/data-image-preview/g) || []).length, count);
    assert.match(rendered, /<img[^>]*\/><figcaption>单图 &lt;script&gt;alert\(1\)&lt;\/script&gt;\n下一行<\/figcaption>/);
    assert.ok(!rendered.includes("<script>"));
  }
  const legacy = sandbox.buildToolCardHtml({ toolName: "Image", input: {}, result: "attached", collapsed: false, images: [{ media_type: "image/png", data: "YQ==" }] });
  assert.ok(!legacy.includes("<figcaption>"));
});

test("webview includes an accessible image preview dialog for message, tool, and composer images", () => {
  assert.match(html, /id="image-preview"[^>]*role="dialog"[^>]*aria-modal="true"/);
  assert.match(html, /id="image-preview-close"[^>]*aria-label="关闭图片预览"/);
  assert.match(script, /function openImagePreview\(image\)/);
  assert.match(script, /function closeImagePreview\(\)/);
  assert.match(script, /thumb\.innerHTML = '<img data-image-preview/);
});

test("restored tool captions remain ordered without duplicate user image messages", () => {
  const state = { toolCards: new Map(), thinkingCards: new Map(), choiceCards: new Map(), permissionCards: new Map(), planCards: new Map() };
  const harness = {
    getSessionState: () => state,
    displayedSessionId: "different-session",
    busySessions: new Set(),
    restoreClientTurnDetails: loadMethod("restoreClientTurnDetails"),
    fetchAndApplyContextUsage: async () => {},
  };
  loadMethod("handleSessionHistory").call(harness, {
    session_id: "images",
    messages: [
      { role: "assistant", content: [{ type: "tool_use", id: "batch", name: "Image", input: { path: ["a.png", "b.png"] } }] },
      { role: "user", content: [
        { type: "tool_result", tool_use_id: "batch", content: "attached" },
        { type: "image", source: { media_type: "image/png", data: "YQ==" }, description: "前" },
        { type: "image", source: { media_type: "image/png", data: "Yg==" }, description: "后" },
      ] },
    ],
  });
  assert.equal(state.history.length, 1);
  assert.equal(state.messages.length, 0);
  const card = state.toolCards.get("batch");
  assert.equal(card.collapsed, false);
  assert.deepEqual(Array.from(card.images, (image) => image.description), ["前", "后"]);
});
