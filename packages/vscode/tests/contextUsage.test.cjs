const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");

const filename = path.join(__dirname, "../src/chatPanel.ts");
const source = ts.createSourceFile(filename, fs.readFileSync(filename, "utf8"), ts.ScriptTarget.Latest, true);
const names = new Set(["contextEstimateNote", "buildContextUsageStatus", "buildCacheUsageDetail", "formatPercent", "formatTokenCount"]);
const functions = source.statements.filter(node => ts.isFunctionDeclaration(node) && names.has(node.name?.text));
const sandbox = { ...require("./helpers/i18n.cjs") };
vm.runInNewContext(ts.transpileModule(functions.map(node => node.getText(source)).join("\n"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText, sandbox);

for (const source of ["server", "calibrated", "estimated", undefined]) {
  test(`context tooltip only warns for local estimates (${source})`, () => {
    const result = sandbox.buildContextUsageStatus({
      type: "turn_complete", context_token_source: source,
      context_used_tokens: 25000, context_window_tokens: 100000,
    });
    assert.equal(result.usedTokens, 25000);
    assert.equal(result.usedPercent, 25);
    assert.equal(result.details.join(" ").includes("Local estimate"), !source || source === "estimated");
    assert.ok(!result.details.join(" ").includes("计数来源"));
  });
}
