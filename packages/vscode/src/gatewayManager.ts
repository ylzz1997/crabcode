/**
 * Gateway manager — auto-detect Python, check/install crabcode-gateway,
 * and start/stop the gateway process.
 */

import { t as tr, localized } from "./i18n";
import { existsSync } from "fs";
import * as http from "http";
import * as https from "https";
import * as net from "net";
import * as path from "path";
import { execFile, spawn, spawnSync, type ChildProcess } from "child_process";
import * as vscode from "vscode";
import { windowsPythonCandidates } from "./windowsPython";

const GATEWAY_PKG = "crabcode[gateway]";
const GATEWAY_CLI_MODULE = "crabcode_cli";
const MIN_PYTHON_MAJOR = 3;
const MIN_PYTHON_MINOR = 10;
const MIN_GATEWAY_PROTOCOL = 1;
const MAX_GATEWAY_PROTOCOL = 1;
const HEALTH_TIMEOUT_MS = 2_000;
const HEALTH_BODY_LIMIT_BYTES = 64 * 1024;

interface GatewayTarget {
  healthUrl: URL;
  host: string;
  port: number;
  isLocal: boolean;
  authToken: string;
}

interface ProtocolSupport {
  protocolVersion: number | null;
  minProtocolVersion: number | null;
  maxProtocolVersion: number | null;
}

interface GatewayHealth extends ProtocolSupport {
  status: string;
  version: string;
}

interface GatewayHealthProbe {
  reachable: boolean;
  health?: GatewayHealth;
  error?: string;
}

interface InstalledGatewayInfo extends ProtocolSupport {
  version: string;
}

export type GatewayInstallFeature = "search" | "debugger";
export type GatewayInstallSuite = "gateway" | "search" | "debugger" | "search-debugger";
type GatewayInstallSelection = GatewayInstallSuite | readonly GatewayInstallFeature[];

export const GATEWAY_INSTALL_FEATURES: ReadonlyArray<{
  id: GatewayInstallFeature;
  label: string;
  description: string;
  detail: string;
}> = localized([
  {
    id: "search",
    label: "Search",
    description: "语义代码搜索",
    detail: "增加 Search 索引与本地嵌入依赖，下载体积较大。",
  },
  {
    id: "debugger",
    label: "Debugger",
    description: "DAP 与进程调试",
    detail: "增加 Debugger 套件；实际能力取决于本机调试器、权限与平台。",
  },
]);

function normalizeGatewayInstallFeatures(selection: GatewayInstallSelection): GatewayInstallFeature[] {
  const legacyFeatures: Record<GatewayInstallSuite, readonly GatewayInstallFeature[]> = {
    gateway: [],
    search: ["search"],
    debugger: ["debugger"],
    "search-debugger": ["search", "debugger"],
  };
  const requested = typeof selection === "string" ? legacyFeatures[selection] : selection;
  if (!requested) throw new Error("Unknown CrabCode suite");
  for (const feature of requested) {
    if (!GATEWAY_INSTALL_FEATURES.some((candidate) => candidate.id === feature)) {
      throw new Error(`Unknown CrabCode feature: ${feature}`);
    }
  }
  return GATEWAY_INSTALL_FEATURES
    .map((feature) => feature.id)
    .filter((feature) => requested.includes(feature));
}

export function gatewayPackageSpec(
  version: string,
  selection: GatewayInstallSelection = [],
): string {
  const features = normalizeGatewayInstallFeatures(selection);
  return `crabcode[gateway${features.map((feature) => `,${feature}`).join("")}]==${version}`;
}

// ── Helpers ──────────────────────────────────────────────────────────

function execAsync(
  cmd: string,
  args: string[],
  opts?: { cwd?: string; env?: NodeJS.ProcessEnv },
): Promise<{ stdout: string; stderr: string; code: number }> {
  return new Promise((resolve) => {
    const env = {
      ...process.env,
      ...opts?.env,
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8",
    };
    execFile(cmd, args, { ...opts, env, timeout: 15_000, windowsHide: true }, (err, stdout, stderr) => {
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

function isLoopbackHost(host: string): boolean {
  const normalized = host.toLowerCase().replace(/^\[|\]$/g, "");
  if (
    normalized === "localhost"
    || normalized === "::"
    || normalized === "::1"
    || normalized === "0.0.0.0"
  ) {
    return true;
  }
  return net.isIP(normalized) === 4 && normalized.startsWith("127.");
}

function displayHealthUrl(target: GatewayTarget): string {
  const displayUrl = new URL(target.healthUrl.toString());
  displayUrl.username = "";
  displayUrl.password = "";
  return displayUrl.toString();
}

function parseGatewayTarget(serverUrl: string): GatewayTarget | null {
  try {
    const wsUrl = new URL(serverUrl);
    if (wsUrl.protocol !== "ws:" && wsUrl.protocol !== "wss:") {
      return null;
    }

    const healthUrl = new URL(wsUrl.toString());
    healthUrl.protocol = wsUrl.protocol === "wss:" ? "https:" : "http:";
    healthUrl.hash = "";
    const authToken = wsUrl.searchParams.get("auth_token") ?? "";
    healthUrl.search = "";

    const trimmedPath = wsUrl.pathname.replace(/\/+$/, "");
    const basePath = trimmedPath.endsWith("/ws")
      ? trimmedPath.slice(0, -3)
      : trimmedPath.slice(0, Math.max(0, trimmedPath.lastIndexOf("/") + 1));
    healthUrl.pathname = `${basePath}/health`.replace(/\/{2,}/g, "/");

    const port = wsUrl.port
      ? parseInt(wsUrl.port, 10)
      : wsUrl.protocol === "wss:"
        ? 443
        : 80;
    const host = wsUrl.hostname.replace(/^\[|\]$/g, "");
    return {
      healthUrl,
      host,
      port,
      isLocal: isLoopbackHost(host),
      authToken,
    };
  } catch {
    return null;
  }
}

function probeGatewayHealth(
  target: GatewayTarget,
  password: string,
  timeoutMs = HEALTH_TIMEOUT_MS,
): Promise<GatewayHealthProbe> {
  return new Promise((resolve) => {
    let settled = false;
    let socketConnected = false;
    const finish = (result: GatewayHealthProbe): void => {
      if (settled) return;
      settled = true;
      resolve(result);
    };
    const client = target.healthUrl.protocol === "https:" ? https : http;
    const requestUrl = new URL(target.healthUrl.toString());
    if (target.authToken) {
      requestUrl.searchParams.set("auth_token", target.authToken);
    }
    const headers: Record<string, string> = { Accept: "application/json" };
    if (password) {
      headers.Authorization = `Bearer ${password}`;
    }

    const req = client.get(requestUrl, { headers }, (res) => {
      const chunks: Buffer[] = [];
      let bodyBytes = 0;

      res.on("data", (chunk: Buffer) => {
        bodyBytes += chunk.length;
        if (bodyBytes > HEALTH_BODY_LIMIT_BYTES) {
          req.destroy();
          finish({ reachable: true, error: tr("健康响应过大") });
          return;
        }
        chunks.push(chunk);
      });

      res.on("end", () => {
        if (settled) return;
        const statusCode = res.statusCode ?? 0;
        if (statusCode < 200 || statusCode >= 300) {
          finish({ reachable: true, error: tr("/health 返回 HTTP {0}", { 0: statusCode }) });
          return;
        }

        try {
          const payload = JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, unknown>;
          if (payload.status !== "ok" || typeof payload.version !== "string") {
            finish({ reachable: true, error: tr("/health 响应不是有效的 CrabCode gateway") });
            return;
          }
          finish({
            reachable: true,
            health: {
              status: payload.status,
              version: payload.version,
              protocolVersion: Number.isInteger(payload.protocol_version)
                ? payload.protocol_version as number
                : null,
              minProtocolVersion: Number.isInteger(payload.min_protocol_version)
                ? payload.min_protocol_version as number
                : null,
              maxProtocolVersion: Number.isInteger(payload.max_protocol_version)
                ? payload.max_protocol_version as number
                : null,
            },
          });
        } catch {
          finish({ reachable: true, error: tr("/health 返回了无效 JSON") });
        }
      });
    });

    req.on("socket", (socket) => {
      socket.once("connect", () => {
        socketConnected = true;
      });
    });
    req.setTimeout(timeoutMs, () => {
      req.destroy();
      finish({ reachable: socketConnected, error: tr("/health 请求超时") });
    });
    req.on("error", (err) => {
      finish({ reachable: socketConnected, error: err.message });
    });
  });
}

function protocolCompatibility(support: ProtocolSupport): { compatible: boolean; reason?: string } {
  if (support.protocolVersion === null) {
    return { compatible: false, reason: tr("gateway 未声明协议版本") };
  }

  const gatewayMin = support.minProtocolVersion ?? support.protocolVersion;
  const gatewayMax = support.maxProtocolVersion ?? support.protocolVersion;
  if (gatewayMin > gatewayMax) {
    return { compatible: false, reason: tr("gateway 声明了无效协议范围 {0}-{1}", { 0: gatewayMin, 1: gatewayMax }) };
  }
  if (gatewayMax < MIN_GATEWAY_PROTOCOL || gatewayMin > MAX_GATEWAY_PROTOCOL) {
    return {
      compatible: false,
      reason:
        tr("gateway 协议范围 {0}-{1} 与插件支持范围 ", { 0: gatewayMin, 1: gatewayMax }) +
        tr("{0}-{1} 不相交", { 0: MIN_GATEWAY_PROTOCOL, 1: MAX_GATEWAY_PROTOCOL }),
    };
  }
  return { compatible: true };
}

function gatewayCompatibility(
  info: InstalledGatewayInfo,
  expectedVersion: string,
  usingWorkspaceSources = false,
): { compatible: boolean; reason?: string } {
  const protocol = protocolCompatibility(info);
  if (!protocol.compatible || usingWorkspaceSources) return protocol;

  // Protocol compatibility alone does not include runtime bug fixes. Keep newer
  // releases, but require at least the extension's release for packaged gateways.
  const current = /^(\d+)\.(\d+)\.(\d+)(-[^+]+)?(?:\+.*)?$/.exec(info.version);
  const expected = /^(\d+)\.(\d+)\.(\d+)(-[^+]+)?(?:\+.*)?$/.exec(expectedVersion);
  if (current && expected) {
    const difference = [1, 2, 3].map((i) => Number(current[i]) - Number(expected[i])).find((n) => n !== 0) ?? 0;
    if (difference > 0 || (difference === 0 && (!current[4] || current[4] === expected[4]))) {
      return protocol;
    }
  }
  return {
    compatible: false,
    reason: tr("gateway 版本 {0} 不满足扩展要求的 {1}", { 0: info.version, 1: expectedVersion }),
  };
}

// ── Python detection ─────────────────────────────────────────────────

/**
 * Find a usable Python interpreter.
 *
 * Priority:
 *  1. `crabcode.pythonPath` setting
 *  2. Python from the ms-python extension (if installed)
 *  3. Workspace .venv
 *  4. Conda environments (base + named envs)
 *  5. `python3` / `python` on PATH
 *  6. Windows launcher, registry, uv, and common installation directories
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
      tr("CrabCode：配置的 pythonPath「{0}」无效或版本低于 3.10，将尝试自动检测。", { 0: userPath }),
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

  // 3. Prefer project dependencies to an unrelated Conda or system Python.
  for (const folder of vscode.workspace?.workspaceFolders ?? []) {
    const python = process.platform === "win32"
      ? path.join(folder.uri.fsPath, ".venv", "Scripts", "python.exe")
      : path.join(folder.uri.fsPath, ".venv", "bin", "python");
    if (existsSync(python) && await checkPython(python)) return python;
  }

  // 4. Conda environments
  const condaPython = await detectCondaPython();
  if (condaPython) return condaPython;

  // 5. Probe common names on PATH
  for (const candidate of ["python3", "python"]) {
    const v = await checkPython(candidate);
    if (v) return candidate;
  }

  if (process.platform === "win32") {
    const { stdout, code } = await execAsync("py", ["-3", "-c", "import sys; print(sys.executable)"]);
    if (code === 0 && stdout && await checkPython(stdout)) return stdout;
    for (const candidate of await windowsPythonCandidates()) {
      if (await checkPython(candidate)) return candidate;
    }
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
  for (const candidate of [process.env.CONDA_EXE, "conda", "conda.exe"].filter((value): value is string => Boolean(value))) {
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

/** Read the installed gateway release and wire protocol from the selected Python. */
async function getInstalledGatewayInfo(
  python: string,
  launchOptions?: { cwd?: string; env?: NodeJS.ProcessEnv },
): Promise<InstalledGatewayInfo | null> {
  const script = [
    "import json",
    "import crabcode_cli.app",
    "import crabcode_gateway",
    "import crabcode_gateway.server",
    "try:",
    "    from crabcode_gateway.protocol import GATEWAY_MAX_PROTOCOL_VERSION, GATEWAY_MIN_PROTOCOL_VERSION, GATEWAY_PROTOCOL_VERSION",
    "except ImportError:",
    "    GATEWAY_PROTOCOL_VERSION = None",
    "    GATEWAY_MIN_PROTOCOL_VERSION = None",
    "    GATEWAY_MAX_PROTOCOL_VERSION = None",
    "print(json.dumps({'version': getattr(crabcode_gateway, '__version__', ''), 'protocol_version': GATEWAY_PROTOCOL_VERSION, 'min_protocol_version': GATEWAY_MIN_PROTOCOL_VERSION, 'max_protocol_version': GATEWAY_MAX_PROTOCOL_VERSION}))",
  ].join("\n");
  const { stdout, code } = await execAsync(
    python,
    ["-c", script],
    launchOptions,
  );
  if (code !== 0 || !stdout) {
    return null;
  }
  try {
    const payload = JSON.parse(stdout) as Record<string, unknown>;
    return {
      version: typeof payload.version === "string" && payload.version
        ? payload.version
        : tr("未知"),
      protocolVersion: Number.isInteger(payload.protocol_version)
        ? payload.protocol_version as number
        : null,
      minProtocolVersion: Number.isInteger(payload.min_protocol_version)
        ? payload.min_protocol_version as number
        : null,
      maxProtocolVersion: Number.isInteger(payload.max_protocol_version)
        ? payload.max_protocol_version as number
        : null,
    };
  } catch {
    return null;
  }
}

/** Install the CrabCode release paired with this extension. */
async function installGateway(
  python: string,
  version: string,
  outputChannel: vscode.OutputChannel,
  selection: GatewayInstallSelection = [],
): Promise<boolean> {
  const packageSpec = gatewayPackageSpec(version, selection);
  outputChannel.show(true);
  outputChannel.appendLine(tr("[CrabCode] 正在安装 {0} ...", { 0: packageSpec }));

  return new Promise((resolve) => {
    const proc = spawn(python, ["-m", "pip", "install", "--upgrade", packageSpec], {
      stdio: ["ignore", "pipe", "pipe"],
      env: {
        ...process.env,
        PYTHONUTF8: "1",
        PYTHONIOENCODING: "utf-8",
      },
      windowsHide: true,
    });

    proc.stdout?.on("data", (d: Buffer) => outputChannel.append(d.toString()));
    proc.stderr?.on("data", (d: Buffer) => outputChannel.append(d.toString()));

    proc.on("close", (code) => {
      const ok = code === 0;
      outputChannel.appendLine(
        ok
          ? tr("[CrabCode] {0} 安装完成", { 0: packageSpec })
          : tr("[CrabCode] {0} 安装失败（退出码 {1}）", { 0: packageSpec, 1: code }),
      );
      resolve(ok);
    });

    proc.on("error", (err) => {
      outputChannel.appendLine(tr("[CrabCode] 安装进程出错: {0}", { 0: err.message }));
      resolve(false);
    });
  });
}

/** Let the user install an optional CrabCode suite into the selected local Python. */
export async function chooseAndInstallGatewaySuite(
  config: vscode.WorkspaceConfiguration,
  outputChannel: vscode.OutputChannel,
  expectedVersion: string,
): Promise<boolean> {
  const serverUrl = config.get<string>("serverUrl", "ws://localhost:4096/ws");
  const target = parseGatewayTarget(serverUrl);
  if (!target) {
    vscode.window.showErrorMessage(tr("CrabCode：serverUrl 必须是有效的 ws:// 或 wss:// 地址。"));
    return false;
  }
  if (!target.isLocal) {
    vscode.window.showWarningMessage(
      tr("CrabCode：当前配置的是远程 Gateway。请在远程主机上安装可选套件，本机不会代为修改。"),
    );
    return false;
  }
  if (!/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(expectedVersion)) {
    vscode.window.showErrorMessage(
      tr("CrabCode：扩展版本“{0}”无效，无法确定匹配的套件版本。", { 0: expectedVersion }),
    );
    return false;
  }

  const selected = await vscode.window.showQuickPick(
    GATEWAY_INSTALL_FEATURES.map((feature) => ({
      label: feature.label,
      description: feature.description,
      detail: feature.detail,
      feature: feature.id,
    })),
    {
      title: tr("安装 CrabCode 套件"),
      placeHolder: tr("勾选要安装的可选能力；基础 Gateway 始终安装"),
      ignoreFocusOut: true,
      canPickMany: true,
    },
  );
  if (!selected) return false;
  const features = selected.map((item) => item.feature);
  const selectedLabel = selected.length > 0
    ? `Gateway + ${selected.map((item) => item.label).join(" + ")}`
    : tr("基础 Gateway");

  const python = await detectPython(config);
  if (!python) {
    vscode.window.showErrorMessage(
      tr("CrabCode：未找到 Python >= 3.10，请安装 Python 或在设置中指定 crabcode.pythonPath。"),
    );
    return false;
  }

  const packageSpec = gatewayPackageSpec(expectedVersion, features);
  const ok = await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: tr("CrabCode：正在安装 {0}", { 0: selectedLabel }),
      cancellable: false,
    },
    () => installGateway(python, expectedVersion, outputChannel, features),
  );
  if (!ok) {
    vscode.window.showErrorMessage(
      tr("CrabCode：安装 {0} 失败，请检查输出面板或手动执行 ", { 0: packageSpec }) +
      `python -m pip install --upgrade "${packageSpec}"`,
    );
    return false;
  }

  vscode.window.showInformationMessage(
    tr("CrabCode：{0} 已安装。新会话可使用新增依赖；如 Gateway 已运行，可按需重启。", { 0: selectedLabel }),
  );
  return true;
}

// ── Gateway start / stop ─────────────────────────────────────────────

export class GatewayProcess implements vscode.Disposable {
  private proc: ChildProcess | null = null;
  private _running = false;

  get running(): boolean {
    return this._running;
  }

  get ownsProcess(): boolean {
    return this.proc !== null;
  }

  /**
   * Start the gateway server as a background process.
   * Returns true if the gateway appears to be listening.
   */
  async start(
    python: string,
    target: GatewayTarget,
    password: string,
    outputChannel: vscode.OutputChannel,
    launchOptions?: { cwd?: string; env?: NodeJS.ProcessEnv; usingWorkspaceSources?: boolean },
  ): Promise<boolean> {
    const existing = await probeGatewayHealth(target, password);
    if (existing.health) {
      const compatibility = protocolCompatibility(existing.health);
      if (compatibility.compatible) {
        outputChannel.appendLine(
          tr("[CrabCode] 网关已在 {0}:{1} 上运行 ", { 0: target.host, 1: target.port }) +
          tr("(CrabCode {0}, 协议 {1})", { 0: existing.health.version, 1: existing.health.protocolVersion }),
        );
        this._running = true;
        return true;
      }
      outputChannel.appendLine(tr("[CrabCode] 已运行的网关不兼容：{0}", { 0: compatibility.reason }));
      return false;
    }
    if (existing.reachable) {
      outputChannel.appendLine(`[CrabCode] ${existing.error ?? tr("健康检查失败")}`);
      return false;
    }

    outputChannel.appendLine(tr("[CrabCode] 正在启动网关 ({0}:{1}) ...", { 0: target.host, 1: target.port }));
    if (launchOptions?.usingWorkspaceSources) {
      outputChannel.appendLine(tr("[CrabCode] 使用当前工作区源码启动网关"));
    }

    const child = spawn(
      python,
      ["-m", GATEWAY_CLI_MODULE, "gateway", "--host", target.host, "--port", String(target.port)],
      {
        stdio: ["ignore", "pipe", "pipe"],
        detached: false,
        cwd: launchOptions?.cwd,
        env: {
          ...process.env,
          ...launchOptions?.env,
          PYTHONUTF8: "1",
          PYTHONIOENCODING: "utf-8",
        },
        windowsHide: true,
      },
    );
    this.proc = child;
    let exited = false;

    child.stdout?.on("data", (d: Buffer) => outputChannel.append(d.toString()));
    child.stderr?.on("data", (d: Buffer) => outputChannel.append(d.toString()));

    child.on("close", (code) => {
      exited = true;
      outputChannel.appendLine(tr("[CrabCode] 网关进程已退出（码 {0}）", { 0: code }));
      if (this.proc === child) {
        this._running = false;
        this.proc = null;
      }
    });

    child.on("error", (err) => {
      exited = true;
      outputChannel.appendLine(tr("[CrabCode] 网关进程出错: {0}", { 0: err.message }));
      if (this.proc === child) {
        this._running = false;
        this.proc = null;
      }
    });

    // Wait up to 10s for a compatible health response.
    for (let i = 0; i < 20; i++) {
      await new Promise((r) => setTimeout(r, 500));
      if (exited || this.proc !== child) return false;
      const probe = await probeGatewayHealth(target, password, 500);
      if (exited || this.proc !== child) return false;
      if (probe.health) {
        const compatibility = protocolCompatibility(probe.health);
        if (!compatibility.compatible) {
          outputChannel.appendLine(tr("[CrabCode] 启动后的网关不兼容：{0}", { 0: compatibility.reason }));
          this.stop();
          return false;
        }
        this._running = true;
        outputChannel.appendLine(
          tr("[CrabCode] 网关已就绪 ({0}:{1}, ", { 0: target.host, 1: target.port }) +
          tr("CrabCode {0}, 协议 {1})", { 0: probe.health.version, 1: probe.health.protocolVersion }),
        );
        return true;
      }
    }

    outputChannel.appendLine(tr("[CrabCode] 网关启动超时"));
    this.stop();
    return false;
  }

  stop(): void {
    if (this.proc) {
      const child = this.proc;
      this.proc = null;
      if (process.platform === "win32" && child.pid) {
        const result = spawnSync(
          "taskkill",
          ["/PID", String(child.pid), "/T", "/F"],
          { stdio: "ignore", windowsHide: true },
        );
        if (result.status !== 0 && child.exitCode === null) {
          child.kill();
        }
      } else {
        child.kill();
      }
    }
    this._running = false;
  }

  dispose(): void {
    this.stop();
  }
}

// ── Top-level orchestration ──────────────────────────────────────────

export interface GatewayEnsureResult {
  python: string;
  gatewayReady: boolean;
  startedByUs: boolean; // true if we started the gateway process
}

async function installExpectedGateway(
  python: string,
  expectedVersion: string,
  outputChannel: vscode.OutputChannel,
): Promise<boolean> {
  const packageSpec = gatewayPackageSpec(expectedVersion);
  const ok = await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: tr("CrabCode：正在安装 {0}", { 0: packageSpec }),
      cancellable: false,
    },
    () => installGateway(python, expectedVersion, outputChannel),
  );

  if (!ok) {
    vscode.window.showErrorMessage(
      tr("CrabCode：安装 {0} 失败，请检查输出面板或手动执行 ", { 0: packageSpec }) +
      `python -m pip install --upgrade "${packageSpec}"`,
    );
  }
  return ok;
}

async function confirmGatewayUpgrade(
  currentVersion: string,
  protocolVersion: number | null,
  expectedVersion: string,
): Promise<boolean> {
  const protocolLabel = protocolVersion === null ? tr("未声明") : String(protocolVersion);
  const action = await vscode.window.showWarningMessage(
    tr("CrabCode：本地 gateway 与当前 VS Code 扩展不兼容。"),
    {
      modal: true,
      detail:
        tr("当前 gateway：{0}（协议 {1}）\n", { 0: currentVersion, 1: protocolLabel }) +
        tr("插件支持协议：{0}-{1}\n", { 0: MIN_GATEWAY_PROTOCOL, 1: MAX_GATEWAY_PROTOCOL }) +
        tr("是否安装匹配的 CrabCode {0}？", { 0: expectedVersion }),
    },
    tr("升级到 {0}", { 0: expectedVersion }),
    tr("取消"),
  );
  return action === tr("升级到 {0}", { 0: expectedVersion });
}

async function handleRunningIncompatibleGateway(
  target: GatewayTarget,
  health: GatewayHealth,
  password: string,
  expectedVersion: string,
  config: vscode.WorkspaceConfiguration,
  outputChannel: vscode.OutputChannel,
  gatewayProc: GatewayProcess,
  launchOptions: PythonLaunchOptions,
): Promise<GatewayEnsureResult> {
  const compatibility = gatewayCompatibility(health, expectedVersion, launchOptions.usingWorkspaceSources);
  const detail =
    tr("CrabCode {0}（协议 {1}）：", { 0: health.version, 1: health.protocolVersion ?? tr("未声明") }) +
    `${compatibility.reason ?? tr("不兼容")}`;
  outputChannel.appendLine(`[CrabCode] ${detail}`);

  if (!target.isLocal) {
    vscode.window.showWarningMessage(
      tr("CrabCode：远程 gateway 不兼容。{0}。请在远程服务器升级到兼容版本。", { 0: detail }),
    );
    return { python: "", gatewayReady: false, startedByUs: false };
  }

  if (!gatewayProc.ownsProcess) {
    // Another window or a manually started process may use a different Python.
    // Do not modify its environment or terminate a process we do not own.
    vscode.window.showWarningMessage(
      tr("CrabCode：当前 gateway 不是由扩展启动。请先停止旧进程，再执行“CrabCode：连接网关”。"),
    );
    return { python: "", gatewayReady: false, startedByUs: false };
  }

  if (!await confirmGatewayUpgrade(
    health.version,
    health.protocolVersion,
    expectedVersion,
  )) {
    return { python: "", gatewayReady: false, startedByUs: false };
  }

  const python = await detectPython(config);
  if (!python) {
    vscode.window.showErrorMessage(
      tr("CrabCode：未找到 Python >= 3.10，无法安装匹配的本地 gateway。"),
    );
    return { python: "", gatewayReady: false, startedByUs: false };
  }

  // Stop before pip replaces modules/DLLs, which may be locked on Windows.
  gatewayProc.stop();
  if (!launchOptions.usingWorkspaceSources) {
    const installed = await installExpectedGateway(python, expectedVersion, outputChannel);
    if (!installed) {
      return { python, gatewayReady: false, startedByUs: false };
    }
  }

  const started = await gatewayProc.start(
    python,
    target,
    password,
    outputChannel,
    launchOptions,
  );
  return { python, gatewayReady: started, startedByUs: started && gatewayProc.ownsProcess };
}

/**
 * Ensure the gateway is ready:
 *  1. Probe /health and validate protocol compatibility and minimum release
 *  2. For a missing local gateway, detect Python and install if needed
 *  3. Start a local gateway, or warn without installing for remote targets
 *
 * Returns the result including whether the gateway is reachable.
 */
export async function ensureGateway(
  config: vscode.WorkspaceConfiguration,
  outputChannel: vscode.OutputChannel,
  gatewayProc: GatewayProcess,
  expectedVersion: string,
): Promise<GatewayEnsureResult> {
  const serverUrl = config.get<string>("serverUrl", "ws://localhost:4096/ws");
  const target = parseGatewayTarget(serverUrl);
  if (!target) {
    vscode.window.showErrorMessage(
      tr("CrabCode：serverUrl 必须是有效的 ws:// 或 wss:// 地址。"),
    );
    return { python: "", gatewayReady: false, startedByUs: false };
  }
  if (!/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(expectedVersion)) {
    vscode.window.showErrorMessage(
      tr("CrabCode：扩展版本“{0}”无效，无法确定匹配的 gateway 版本。", { 0: expectedVersion }),
    );
    return { python: "", gatewayReady: false, startedByUs: false };
  }

  const password = config.get<string>("password", "");
  const launchOptions = getPythonLaunchOptions();

  // Check the actual gateway identity and protocol before touching local Python.
  const existing = await probeGatewayHealth(target, password);
  if (existing.health) {
    const compatibility = gatewayCompatibility(existing.health, expectedVersion, launchOptions.usingWorkspaceSources);
    if (compatibility.compatible) {
      outputChannel.appendLine(
        tr("[CrabCode] 网关已就绪 ({0}, ", { 0: displayHealthUrl(target) }) +
        tr("CrabCode {0}, 协议 {1})", { 0: existing.health.version, 1: existing.health.protocolVersion }),
      );
      return { python: "", gatewayReady: true, startedByUs: gatewayProc.ownsProcess };
    }
    return handleRunningIncompatibleGateway(
      target,
      existing.health,
      password,
      expectedVersion,
      config,
      outputChannel,
      gatewayProc,
      launchOptions,
    );
  }

  if (existing.reachable) {
    const message =
      tr("CrabCode：{0} 可访问，但不是有效的兼容 gateway", { 0: displayHealthUrl(target) }) +
      `（${existing.error ?? tr("健康检查失败")}）。`;
    outputChannel.appendLine(`[CrabCode] ${message}`);
    vscode.window.showWarningMessage(message);
    return { python: "", gatewayReady: false, startedByUs: false };
  }

  if (!target.isLocal) {
    const reason = existing.error ? `：${existing.error}` : "";
    vscode.window.showWarningMessage(
      tr("CrabCode：无法访问远程 gateway {0}{1}。", { 0: displayHealthUrl(target), 1: reason }) +
      tr("扩展不会在本机自动安装远程 gateway。"),
    );
    return { python: "", gatewayReady: false, startedByUs: false };
  }

  // No local gateway is running. Inspect the selected Python environment.
  const python = await detectPython(config);
  if (!python) {
    vscode.window.showErrorMessage(
      tr("CrabCode：未找到 Python >= 3.10，请安装 Python 或在设置中指定 crabcode.pythonPath。"),
    );
    return { python: "", gatewayReady: false, startedByUs: false };
  }

  outputChannel.appendLine(tr("[CrabCode] 使用 Python：{0}", { 0: python }));
  if (launchOptions.usingWorkspaceSources) {
    outputChannel.appendLine(tr("[CrabCode] 检测到当前工作区源码，正在检查 CLI 和网关依赖"));
    const runtime = await execAsync(
      python,
      ["-c", "import crabcode_cli.app; import crabcode_gateway.server"],
      launchOptions,
    );
    if (runtime.code !== 0) {
      outputChannel.appendLine(runtime.stderr || runtime.stdout || tr("Python 运行依赖检查失败"));
      const command = `${process.platform === "win32" ? "& " : ""}"${python}" ` +
        `-m pip install -e "${launchOptions.cwd}[gateway]"`;
      outputChannel.appendLine(tr("[CrabCode] 请为该 Python 安装工作区依赖：{0}", { 0: command }));
      vscode.window.showErrorMessage(
        tr("CrabCode：Python「{0}」无法加载工作区 CLI 或网关。请检查输出面板中的依赖错误和安装命令，或通过 crabcode.pythonPath 选择已安装依赖的 Python。", { 0: python }),
      );
      return { python, gatewayReady: false, startedByUs: false };
    }
  } else {
    const installed = await getInstalledGatewayInfo(python, launchOptions);
    if (!installed) {
      const autoInstall = config.get<boolean>("gatewayAutoInstall", true);
      if (!autoInstall) {
        const action = await vscode.window.showErrorMessage(
          tr("CrabCode：未安装 {0}，是否安装匹配版本 {1}？", { 0: GATEWAY_PKG, 1: expectedVersion }),
          tr("安装 {0}", { 0: expectedVersion }),
          tr("取消"),
        );
        if (action !== tr("安装 {0}", { 0: expectedVersion })) {
          return { python, gatewayReady: false, startedByUs: false };
        }
      }
      if (!await installExpectedGateway(python, expectedVersion, outputChannel)) {
        return { python, gatewayReady: false, startedByUs: false };
      }
    } else {
      const compatibility = gatewayCompatibility(installed, expectedVersion);
      if (!compatibility.compatible) {
        outputChannel.appendLine(
          tr("[CrabCode] 已安装 gateway {0} 不兼容：{1}", { 0: installed.version, 1: compatibility.reason }),
        );
        if (!await confirmGatewayUpgrade(
          installed.version,
          installed.protocolVersion,
          expectedVersion,
        )) {
          return { python, gatewayReady: false, startedByUs: false };
        }
        if (!await installExpectedGateway(python, expectedVersion, outputChannel)) {
          return { python, gatewayReady: false, startedByUs: false };
        }
      }
    }
  }

  const started = await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: tr("CrabCode：正在启动网关"),
      cancellable: false,
    },
    () => gatewayProc.start(python, target, password, outputChannel, launchOptions),
  );

  if (!started) {
    vscode.window.showErrorMessage(
      tr("CrabCode：网关启动失败，请检查输出面板或手动运行 python -m crabcode_cli gateway"),
    );
  }

  return { python, gatewayReady: started, startedByUs: started && gatewayProc.ownsProcess };
}
