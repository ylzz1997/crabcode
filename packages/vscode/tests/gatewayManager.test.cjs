const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");

function loadDetection(probe, platform = "win32") {
  const filename = path.join(__dirname, "../src/gatewayManager.ts");
  const source = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  vm.runInNewContext(source, {
    exports,
    process: { platform, env: {} },
    URL,
    require(name) {
      if (name === "vscode") return {
        extensions: { getExtension: () => undefined },
        window: { showWarningMessage() {} },
      };
      if (name === "child_process") return {
        execFile(command, args, options, callback) {
          const result = probe(command, args);
          callback(result === undefined ? { code: "ENOENT" } : null, result ?? "", "");
        },
      };
      return require(name);
    },
  }, { filename });
  return exports.detectPython;
}

const config = { get: (_key, fallback) => fallback };

test("Windows py-only installation returns its concrete interpreter with spaces", async () => {
  const python = "C:\\Program Files\\Python312\\python.exe";
  const calls = [];
  const detect = loadDetection((command, args) => {
    calls.push([command, args]);
    if (command === "py") {
      assert.deepEqual(Array.from(args), ["-3", "-c", "import sys; print(sys.executable)"]);
      return python;
    }
    if (command === python) return "Python 3.12.5";
  });
  assert.equal(await detect(config), python);
  assert.ok(calls.some(([command]) => command === python));
});

test("Reject unsupported py interpreter version", async () => {
  const detect = loadDetection((command) => {
    if (command === "py") return "C:\\Python39\\python.exe";
    if (command === "C:\\Python39\\python.exe") return "Python 3.9.8";
  });
  assert.equal(await detect(config), null);
});

test("Configured Python stays higher priority than py", async () => {
  const detect = loadDetection((command) => {
    assert.equal(command, "custom-python");
    return "Python 3.12.1";
  });
  assert.equal(await detect({ get: () => "custom-python" }), "custom-python");
});

test("POSIX discovery does not probe the Windows launcher", async () => {
  const detect = loadDetection((command) => { assert.notEqual(command, "py"); }, "linux");
  assert.equal(await detect(config), null);
});
