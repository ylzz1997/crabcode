const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const { EventEmitter } = require("node:events");
const ts = require("typescript");

function loadDetection(probe, platform = "win32", workspaceRoots = [], pythonExtension) {
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
        extensions: { getExtension: () => pythonExtension },
        workspace: { workspaceFolders: workspaceRoots.map((fsPath) => ({ uri: { fsPath } })) },
        window: { showWarningMessage() {} },
      };
      if (name === "fs") return { existsSync: () => workspaceRoots.length > 0 };
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

function loadGatewayManager(options = {}) {
  const filename = path.join(__dirname, "../src/gatewayManager.ts");
  const source = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  vm.runInNewContext(source, {
    exports,
    process: { platform: "linux", env: {} },
    URL,
    Buffer,
    setTimeout: options.setTimeout ?? setTimeout,
    require(name) {
      if (name === "vscode") return options.vscode ?? {
        extensions: { getExtension: () => undefined },
        window: { showWarningMessage() {} },
      };
      if (name === "child_process") return options.childProcess ?? {};
      if (name === "fs") return options.fs ?? fs;
      if (name === "http") return options.http ?? require(name);
      return require(name);
    },
  }, { filename });
  return exports;
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

for (const platform of ["win32", "linux"]) {
  test(`${platform} workspace .venv takes priority over Conda and PATH`, async () => {
    const root = path.join("workspace with spaces", "project");
    const python = platform === "win32"
      ? path.join(root, ".venv", "Scripts", "python.exe")
      : path.join(root, ".venv", "bin", "python");
    const calls = [];
    const detect = loadDetection((command) => {
      calls.push(command);
      if (command === python) return "Python 3.13.1";
      throw new Error(`Unexpected interpreter: ${command}`);
    }, platform, [root]);
    assert.equal(await detect(config), python);
    assert.deepEqual(calls, [python]);
  });
}

test("explicit Python and active Python extension retain priority over workspace .venv", async () => {
  const root = path.join("workspace", "project");
  const extension = {
    isActive: true,
    exports: { settings: { getExecutionDetails: () => ({ execCommand: ["active-python"] }) } },
  };
  const detect = loadDetection(() => "Python 3.12.1", "win32", [root], extension);
  assert.equal(await detect({ get: () => "configured-python" }), "configured-python");
  assert.equal(await detect(config), "active-python");
});

test("unusable workspace .venv falls back to a working PATH interpreter", async () => {
  const detect = loadDetection((command) => command === "python3" ? "Python 3.12.1" : undefined,
    "linux", ["workspace"]);
  assert.equal(await detect(config), "python3");
});

test("POSIX discovery does not probe the Windows launcher", async () => {
  const detect = loadDetection((command) => { assert.notEqual(command, "py"); }, "linux");
  assert.equal(await detect(config), null);
});

function unreachableHttp() {
  return {
    get() {
      const request = new EventEmitter();
      request.setTimeout = () => {};
      queueMicrotask(() => request.emit("error", new Error("ECONNREFUSED")));
      return request;
    },
  };
}

function outputLog() {
  const lines = [];
  return { lines, appendLine: (line) => lines.push(line), append: (text) => lines.push(text) };
}

for (const missingDependencies of [true, false]) {
  test(`workspace sources ${missingDependencies ? "report missing dependencies before launch" : "start with project Python after validating dependencies"}`, async () => {
    const root = path.resolve("workspace with spaces");
    const python = path.join(root, ".venv", "bin", "python");
    const output = outputLog();
    const errors = [];
    const calls = [];
    const { ensureGateway } = loadGatewayManager({
      fs: { existsSync: () => true },
      http: unreachableHttp(),
      vscode: {
        extensions: { getExtension: () => undefined },
        workspace: { workspaceFolders: [{ uri: { fsPath: root } }] },
        ProgressLocation: { Notification: 15 },
        window: {
          withProgress: (_options, task) => task(),
          showErrorMessage: (message) => errors.push(message),
        },
      },
      childProcess: {
        execFile(command, args, options, callback) {
          calls.push({ command, args: Array.from(args), options });
          if (args[0] === "--version") {
            callback(null, "Python 3.13.1", "");
          } else {
            callback(missingDependencies ? { code: 1 } : null, "",
              missingDependencies ? "ModuleNotFoundError: No module named 'typer'" : "");
          }
        },
      },
    });
    let starts = 0;
    const gateway = {
      ownsProcess: true,
      async start(selected, _target, _password, _output, launchOptions) {
        starts++;
        assert.equal(selected, python);
        assert.equal(launchOptions.cwd, root);
        assert.equal(launchOptions.usingWorkspaceSources, true);
        return true;
      },
    };
    const result = await ensureGateway(config, output, gateway, "0.1.6");
    assert.equal(result.python, python);
    assert.equal(result.gatewayReady, !missingDependencies);
    assert.equal(result.startedByUs, !missingDependencies);
    assert.equal(starts, missingDependencies ? 0 : 1);
    assert.equal(calls.length, 2);
    assert.equal(calls[1].args[1], "import crabcode_cli.app; import crabcode_gateway.server");
    assert.ok(calls[1].options.env.PYTHONPATH.includes(path.join(root, "packages", "cli")));
    if (missingDependencies) {
      assert.equal(errors.length, 1);
      assert.ok(output.lines.some((line) => line.includes("No module named 'typer'")));
      assert.ok(output.lines.some((line) => line.includes(`-m pip install -e "${root}[gateway]"`)));
    } else {
      assert.deepEqual(errors, []);
    }
  });
}

for (const failure of ["close", "error"]) {
  test(`gateway process ${failure} ends startup immediately without a timeout`, async () => {
    const output = outputLog();
    const delays = [];
    const { GatewayProcess } = loadGatewayManager({
      http: unreachableHttp(),
      setTimeout(callback, delay) {
        delays.push(delay);
        queueMicrotask(callback);
      },
      childProcess: {
        spawn() {
          const child = new EventEmitter();
          queueMicrotask(() => child.emit(failure, failure === "close" ? 1 : new Error("ENOENT")));
          return child;
        },
      },
    });
    const gateway = new GatewayProcess();
    const result = await gateway.start("python", {
      healthUrl: new URL("http://127.0.0.1:4096/health"),
      host: "127.0.0.1", port: 4096, isLocal: true, authToken: "",
    }, "", output);
    assert.equal(result, false);
    assert.equal(gateway.running, false);
    assert.equal(gateway.ownsProcess, false);
    assert.deepEqual(delays, [500]);
    assert.ok(output.lines.some((line) => line.includes(failure === "close" ? "已退出" : "进程出错")));
    assert.ok(output.lines.every((line) => !line.includes("网关启动超时")));
  });
}

test("independent feature package specs always keep Gateway, stable order, and paired version", () => {
  const { gatewayPackageSpec } = loadGatewayManager();
  assert.equal(gatewayPackageSpec("0.1.5", []), "crabcode[gateway]==0.1.5");
  assert.equal(gatewayPackageSpec("0.1.5", ["search"]), "crabcode[gateway,search]==0.1.5");
  assert.equal(gatewayPackageSpec("0.1.5", ["debugger"]), "crabcode[gateway,debugger]==0.1.5");
  assert.equal(
    gatewayPackageSpec("0.1.5", ["debugger", "search", "debugger"]),
    "crabcode[gateway,search,debugger]==0.1.5",
  );
  assert.throws(() => gatewayPackageSpec("0.1.5", ["unknown"]), /Unknown CrabCode feature/);

  // Legacy scalar suite IDs stay supported for existing callers.
  assert.equal(gatewayPackageSpec("0.1.5", "gateway"), "crabcode[gateway]==0.1.5");
  assert.equal(gatewayPackageSpec("0.1.5", "search"), "crabcode[gateway,search]==0.1.5");
  assert.equal(gatewayPackageSpec("0.1.5", "debugger"), "crabcode[gateway,debugger]==0.1.5");
  assert.equal(
    gatewayPackageSpec("0.1.5", "search-debugger"),
    "crabcode[gateway,search,debugger]==0.1.5",
  );
});
