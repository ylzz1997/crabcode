const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");

function loadDiscovery({ files = [], registry = [], registryError = null, malformed = false } = {}) {
  const filename = path.join(__dirname, "../src/windowsPython.ts");
  const source = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const paths = files.map((file) => path.win32.normalize(file));
  const exports = {};
  const calls = [];
  vm.runInNewContext(source, {
    exports, process: { env: {} },
    require(name) {
      if (name === "os") return { homedir: () => "C:\\Users\\test" };
      if (name === "fs") return {
        statSync(value) { return { isFile: () => paths.some((file) => file.toLowerCase() === value.toLowerCase()) }; },
        readdirSync(root) {
          const normalized = path.win32.normalize(root).toLowerCase().replace(/\\+$/, "") + "\\";
          return [...new Set(paths.filter((file) => file.toLowerCase().startsWith(normalized))
            .map((file) => file.slice(normalized.length).split("\\")[0]))];
        },
      };
      if (name === "child_process") return {
        execFile(command, args, options, callback) {
          calls.push({ command, args: Array.from(args), options });
          callback(registryError, malformed ? "invalid json" : JSON.stringify(registry), "");
        },
      };
      return require(name);
    },
  }, { filename });
  return { discover: exports.windowsPythonCandidates, calls };
}

test("finds uv runtimes without PATH and prefers newer patch versions", async () => {
  const root = "C:\\Users\\测试 用户\\AppData\\Roaming";
  const old = `${root}\\uv\\python\\cpython-3.13.6-windows-x86_64-none\\python.exe`;
  const latest = `${root}\\uv\\python\\cpython-3.13.16-windows-x86_64-none\\python.exe`;
  const { discover } = loadDiscovery({ files: [old, latest] });
  assert.deepEqual(Array.from(await discover({ APPDATA: root })), [latest, old]);
});

test("registry paths preserve Unicode and spaces and deduplicate filesystem results", async () => {
  const root = "C:\\用户 目录\\uv\\python";
  const python = `${root}\\cpython-3.13.16\\python.exe`;
  const { discover, calls } = loadDiscovery({ files: [python], registry: [python, python.toUpperCase(), "C:\\missing.exe", 5, "python.exe"] });
  assert.deepEqual(Array.from(await discover({ UV_PYTHON_INSTALL_DIR: root, SystemRoot: "C:\\Windows" })), [python]);
  assert.equal(calls[0].options.windowsHide, true);
  assert.equal(calls[0].options.timeout, 5000);
  assert.equal(calls[0].options.encoding, "utf8");
});

for (const failure of ["unavailable", "malformed"]) {
  test(`registry ${failure} still allows custom uv discovery`, async () => {
    const root = "D:\\Python runtimes";
    const python = `${root}\\cpython-3.12.8\\python.exe`;
    const { discover } = loadDiscovery({ files: [python], registryError: failure === "unavailable" ? new Error("blocked") : null, malformed: failure === "malformed" });
    assert.deepEqual(Array.from(await discover({ UV_PYTHON_INSTALL_DIR: root })), [python]);
  });
}

test("discovers standard Windows and Conda locations without scanning arbitrary projects", async () => {
  const files = [
    "C:\\Local\\Programs\\Python\\Python313\\python.exe",
    "C:\\Local\\Python\\pythoncore-3.14-64\\python.exe",
    "C:\\Program Files\\Python312\\python.exe",
    "C:\\Python311\\python.exe",
    "C:\\Users\\test\\miniconda3\\python.exe",
    "C:\\ProgramData\\miniforge3\\python.exe",
  ];
  const { discover } = loadDiscovery({ files: [...files, "C:\\project\\.venv\\python.exe", "C:\\Program Files\\Unrelated\\python.exe"] });
  assert.deepEqual(Array.from(await discover({ LOCALAPPDATA: "C:\\Local", ProgramFiles: "C:\\Program Files", SystemDrive: "C:", PROGRAMDATA: "C:\\ProgramData" })), files);
});
