/**
 * Gateway manager — auto-detect Python, check/install crabcode-gateway,
 * and start/stop the gateway process.
 */

import { execFile, spawn, type ChildProcess } from "child_process";
import * as vscode from "vscode";
import * as net from "net";
import { existsSync } from "fs";
import * as path from "path";

const GATEWAY_PKG = "crabcode[gateway]";
const GATEWAY_CLI_MODULE = "crabcode_cli";
const MIN_PYTHON_MAJOR = 3;
const MIN_PYTHON_MINOR = 10;

// ── Helpers ──────────────────────────────────────────────────────────

function execAsync(
  cmd: string,
  args: string[],
  opts?: { cwd?: string; env?: NodeJS.ProcessEnv },
): Promise<{ stdout: string; stderr: string; code: number }> {
  return new Promise((resolve) => {
    execFile(cmd, args, { ...opts, timeout: 15_000 }, (err, stdout, stderr) => {
      resolve({
        stdout: (stdout ?? "").trim(),
        stderr: (stderr ?? "").trim(),
        code: err && "code" in err ? (err.code as number) : err ? 1 : 0,
      });
    });
  });
}

function parsePythonVersion(raw: string): { major: number; minor: number } | null {
  // "Python 3.12.1" or "3.12.1"
  const m = raw.match(/(\d+)\.(\d+)/);
  if (!m) return null;
  return { major: parseInt(m[1], 10), minor: parseInt(m[2], 10) };
}

/** Try to connect to a TCP port; resolves true on success. */
function probePort(host: string, port: number, timeoutMs = 2000): Promise<boolean> {
  return new Promise((resolve) => {
    const sock = net.createConnection({ host, port }, () => {
      sock.destroy();
      resolve(true);
    });
    sock.setTimeout(timeoutMs);
    sock.on("timeout", () => { sock.destroy(); resolve(false); });
    sock.on("error", () => { sock.destroy(); resolve(false); });
  });
}

// ── Python detection ─────────────────────────────────────────────────

/**
 * Find a usable Python interpreter.
 *
 * Priority:
 *  1. `crabcode.pythonPath` setting
 *  2. Python from the ms-python extension (if installed)
 *  3. Conda environments (base + named envs)
 *  4. `python3` / `python` on PATH
 */
export async function detectPython(
  config: vscode.WorkspaceConfiguration,
): Promise<string | null> {
  // 1. User-configured path
  const userPath = config.get<string>("pythonPath", "").trim();
  if (userPath) {
    const v = await checkPython(userPath);
    if (v) return userPath;
    vscode.window.showWarningMessage(
      `CrabCode：配置的 pythonPath「${userPath}」无效或版本低于 3.10，将尝试自动检测。`,
    );
  }

  // 2. Try ms-python extension API
  try {
    const pyExt = vscode.extensions.getExtension("ms-python.python");
    if (pyExt?.isActive) {
      const settings = pyExt.exports?.settings;
      const pythonPath = settings?.getExecutionDetails?.()?.execCommand?.[0];
      if (pythonPath) {
        const v = await checkPython(pythonPath);
        if (v) return pythonPath;
      }
    }
  } catch {
    // Extension not available, ignore
  }

  // 3. Conda environments
  const condaPython = await detectCondaPython();
  if (condaPython) return condaPython;

  // 4. Probe common names on PATH
  for (const candidate of ["python3", "python"]) {
    const v = await checkPython(candidate);
    if (v) return candidate;
  }

  return null;
}

/** Verify a Python binary exists and meets the minimum version. */
async function checkPython(bin: string): Promise<boolean> {
  try {
    const { stdout, code } = await execAsync(bin, ["--version"]);
    if (code !== 0) return false;
    const v = parsePythonVersion(stdout);
    if (!v) return false;
    return v.major > MIN_PYTHON_MAJOR || (v.major === MIN_PYTHON_MAJOR && v.minor >= MIN_PYTHON_MINOR);
  } catch {
    return false;
  }
}

/**
 * Detect Python from conda environments.
 *
 * Strategy:
 *  1. Run `conda info --json` to find conda prefix and env list
 *  2. Check base environment's python
 *  3. Check each named environment's python (sorted by recency)
 *
 * Returns the python path if a valid one is found, null otherwise.
 */
async function detectCondaPython(): Promise<string | null> {
  // Try to locate conda itself
  const condaBin = await findCondaBin();
  if (!condaBin) return null;

  try {
    const { stdout, code } = await execAsync(condaBin, ["info", "--json"]);
    if (code !== 0 || !stdout) return null;

    const info = JSON.parse(stdout);
    const envs: string[] = info.envs ?? [];

    // Sort: base first, then others
    const basePrefix: string | undefined = info.conda_prefix ?? info.root_prefix;
    if (basePrefix && !envs.includes(basePrefix)) {
      envs.unshift(basePrefix);
    } else if (basePrefix) {
      // Move base to front
      const idx = envs.indexOf(basePrefix);
      if (idx > 0) { envs.splice(idx, 1); envs.unshift(basePrefix); }
    }

    for (const envPath of envs) {
      const pythonPath = buildCondaPythonPath(envPath);
      if (await checkPython(pythonPath)) {
        return pythonPath;
      }
    }
  } catch {
    // conda info failed or returned invalid JSON
  }

  return null;
}

/** Locate the conda executable. */
async function findCondaBin(): Promise<string | null> {
  for (const candidate of ["conda", "conda.exe"]) {
    const { code } = await execAsync(candidate, ["--version"]);
    if (code === 0) return candidate;
  }
  return null;
}

/** Build the python path for a conda environment directory. */
function buildCondaPythonPath(envPath: string): string {
  // conda env dirs on Windows: envs/myenv/python.exe
  // on Unix: envs/myenv/bin/python3
  if (process.platform === "win32") {
    return `${envPath}\\python.exe`;
  }
  return `${envPath}/bin/python3`;
}

type PythonLaunchOptions = {
  cwd?: string;
  env?: NodeJS.ProcessEnv;
  usingWorkspaceSources: boolean;
};

function getPythonLaunchOptions(): PythonLaunchOptions {
  const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri?.fsPath;
  if (!workspaceRoot) {
    return { usingWorkspaceSources: false };
  }

  const packageRoots = [
    path.join(workspaceRoot, "packages", "cli"),
    path.join(workspaceRoot, "packages", "core"),
    path.join(workspaceRoot, "packages", "gateway"),
    path.join(workspaceRoot, "packages", "search"),
  ];

  const required = [
    path.join(workspaceRoot, "packages", "cli", "crabcode_cli"),
    path.join(workspaceRoot, "packages", "core", "crabcode_core"),
    path.join(workspaceRoot, "packages", "gateway", "crabcode_gateway"),
  ];

  if (!required.every((p) => existsSync(p))) {
    return { usingWorkspaceSources: false };
  }

  const delimiter = process.platform === "win32" ? ";" : ":";
  const existing = process.env.PYTHONPATH?.trim();
  const env = { ...process.env };
  env.PYTHONPATH = existing
    ? `${packageRoots.join(delimiter)}${delimiter}${existing}`
    : packageRoots.join(delimiter);

  return {
    cwd: workspaceRoot,
    env,
    usingWorkspaceSources: true,
  };
}

// ── Gateway check / install ──────────────────────────────────────────

/** Check if crabcode-cli (with gateway support) is importable in the given Python. */
async function isGatewayInstalled(
  python: string,
  launchOptions?: { cwd?: string; env?: NodeJS.ProcessEnv },
): Promise<boolean> {
  const { code } = await execAsync(
    python,
    ["-c", `import crabcode_cli; import crabcode_gateway; print(1)`],
    launchOptions,
  );
  return code === 0;
}

/** Install crabcode-gateway via pip. Returns true on success. */
async function installGateway(
  python: string,
  outputChannel: vscode.OutputChannel,
): Promise<boolean> {
  outputChannel.show(true);
  outputChannel.appendLine(`[CrabCode] 正在安装 ${GATEWAY_PKG} ...`);

  return new Promise((resolve) => {
    const proc = spawn(python, ["-m", "pip", "install", GATEWAY_PKG], {
      stdio: ["ignore", "pipe", "pipe"],
    });

    proc.stdout?.on("data", (d: Buffer) => outputChannel.append(d.toString()));
    proc.stderr?.on("data", (d: Buffer) => outputChannel.append(d.toString()));

    proc.on("close", (code) => {
      const ok = code === 0;
      outputChannel.appendLine(
        ok
          ? `[CrabCode] ${GATEWAY_PKG} 安装完成`
          : `[CrabCode] ${GATEWAY_PKG} 安装失败（退出码 ${code}）`,
      );
      resolve(ok);
    });

    proc.on("error", (err) => {
      outputChannel.appendLine(`[CrabCode] 安装进程出错: ${err.message}`);
      resolve(false);
    });
  });
}

// ── Gateway start / stop ─────────────────────────────────────────────

export class GatewayProcess implements vscode.Disposable {
  private proc: ChildProcess | null = null;
  private _running = false;

  get running(): boolean {
    return this._running;
  }

  /**
   * Start the gateway server as a background process.
   * Returns true if the gateway appears to be listening.
   */
  async start(
    python: string,
    host: string,
    port: number,
    outputChannel: vscode.OutputChannel,
    launchOptions?: { cwd?: string; env?: NodeJS.ProcessEnv; usingWorkspaceSources?: boolean },
  ): Promise<boolean> {
    // Already running?
    if (await probePort(host, port)) {
      outputChannel.appendLine(`[CrabCode] 网关已在 ${host}:${port} 上运行`);
      this._running = true;
      return true;
    }

    outputChannel.appendLine(`[CrabCode] 正在启动网关 (${host}:${port}) ...`);
    if (launchOptions?.usingWorkspaceSources) {
      outputChannel.appendLine(`[CrabCode] 使用当前工作区源码启动网关`);
    }

    this.proc = spawn(
      python,
      ["-m", GATEWAY_CLI_MODULE, "gateway", "--host", host, "--port", String(port)],
      {
        stdio: ["ignore", "pipe", "pipe"],
        detached: false,
        cwd: launchOptions?.cwd,
        env: launchOptions?.env,
      },
    );

    this.proc.stdout?.on("data", (d: Buffer) => outputChannel.append(d.toString()));
    this.proc.stderr?.on("data", (d: Buffer) => outputChannel.append(d.toString()));

    this.proc.on("close", (code) => {
      outputChannel.appendLine(`[CrabCode] 网关进程已退出（码 ${code}）`);
      this._running = false;
      this.proc = null;
    });

    this.proc.on("error", (err) => {
      outputChannel.appendLine(`[CrabCode] 网关进程出错: ${err.message}`);
      this._running = false;
      this.proc = null;
    });

    // Wait up to 10s for the port to become reachable
    for (let i = 0; i < 20; i++) {
      await new Promise((r) => setTimeout(r, 500));
      if (await probePort(host, port)) {
        this._running = true;
        outputChannel.appendLine(`[CrabCode] 网关已就绪 (${host}:${port})`);
        return true;
      }
    }

    outputChannel.appendLine(`[CrabCode] 网关启动超时`);
    return false;
  }

  dispose(): void {
    if (this.proc) {
      this.proc.kill();
      this.proc = null;
    }
    this._running = false;
  }
}

// ── Top-level orchestration ──────────────────────────────────────────

export interface GatewayEnsureResult {
  python: string;
  gatewayReady: boolean;
  startedByUs: boolean; // true if we started the gateway process
}

/**
 * Ensure the gateway is ready:
 *  1. Detect Python
 *  2. Check if gateway is installed → install if needed
 *  3. Check if gateway is running → start if needed
 *
 * Returns the result including whether the gateway is reachable.
 */
export async function ensureGateway(
  config: vscode.WorkspaceConfiguration,
  outputChannel: vscode.OutputChannel,
  gatewayProc: GatewayProcess,
): Promise<GatewayEnsureResult> {
  const serverUrl = config.get<string>("serverUrl", "ws://localhost:4096/ws");
  const { host, port } = parseWsUrl(serverUrl);
  const launchOptions = getPythonLaunchOptions();

  // 1. Detect Python
  const python = await detectPython(config);
  if (!python) {
    vscode.window.showErrorMessage(
      "CrabCode：未找到 Python >= 3.10，请安装 Python 或在设置中指定 crabcode.pythonPath。",
    );
    return { python: "", gatewayReady: false, startedByUs: false };
  }

  // 2. Already running?
  if (await probePort(host, port)) {
    outputChannel.appendLine(`[CrabCode] 网关已在运行 (${host}:${port})`);
    return { python, gatewayReady: true, startedByUs: false };
  }

  // 3. Check if gateway package is installed
  if (launchOptions.usingWorkspaceSources) {
    outputChannel.appendLine(`[CrabCode] 检测到当前工作区源码，跳过已安装包检查`);
  }
  const installed = launchOptions.usingWorkspaceSources
    ? true
    : await isGatewayInstalled(python, launchOptions);
  const autoInstall = config.get<boolean>("gatewayAutoInstall", true);

  if (!installed) {
    if (!autoInstall) {
      const action = await vscode.window.showErrorMessage(
        `CrabCode：未安装 ${GATEWAY_PKG}，是否现在安装？`,
        "安装",
        "取消",
      );
      if (action !== "安装") {
        return { python, gatewayReady: false, startedByUs: false };
      }
    }

    const ok = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: `CrabCode：正在安装 ${GATEWAY_PKG}`,
        cancellable: false,
      },
      () => installGateway(python, outputChannel),
    );

    if (!ok) {
      vscode.window.showErrorMessage(
        `CrabCode：安装 ${GATEWAY_PKG} 失败，请手动执行 pip install ${GATEWAY_PKG}`,
      );
      return { python, gatewayReady: false, startedByUs: false };
    }
  }

  // 4. Start gateway
  const started = await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: "CrabCode：正在启动网关",
      cancellable: false,
    },
    () => gatewayProc.start(python, host, port, outputChannel, launchOptions),
  );

  if (!started) {
    vscode.window.showErrorMessage(
      `CrabCode：网关启动失败，请检查输出面板或手动运行 python -m crabcode_cli gateway`,
    );
  }

  return { python, gatewayReady: started, startedByUs: started };
}

// ── URL parsing ──────────────────────────────────────────────────────

function parseWsUrl(url: string): { host: string; port: number } {
  try {
    const u = new URL(url);
    return {
      host: u.hostname || "127.0.0.1",
      port: parseInt(u.port, 10) || 4096,
    };
  } catch {
    return { host: "127.0.0.1", port: 4096 };
  }
}
