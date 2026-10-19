const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const { execFile } = require('node:child_process');

const METHODS = {
  computer_use_capabilities: 'capabilities',
  computer_use_execute: 'execute',
  computer_use_release_ax: 'release',
  computer_use_open_input_settings: 'permissions',
  computer_use_heartbeat: 'heartbeat',
};

function stateDirectory() {
  const base = process.platform === 'darwin' ? path.join(os.homedir(), 'Library', 'Application Support')
    : process.platform === 'win32' ? process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local')
      : process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share');
  return path.join(base, 'CrabCode', 'ComputerUse');
}

function bundledExecutable(directory) {
  return path.join(directory, process.platform === 'darwin'
    ? 'Crab Computer Use.app/Contents/MacOS/crabcode-computer-use-host'
    : `crabcode-computer-use-host${process.platform === 'win32' ? '.exe' : ''}`);
}

class NativeComputerUse {
  constructor(bundleDirectory, stateDir = stateDirectory()) {
    this.bundleDirectory = bundleDirectory;
    this.stateDir = stateDir;
    this.starting = null;
  }

  async endpoint() {
    const endpoint = JSON.parse(await fs.readFile(path.join(this.stateDir, 'endpoint.json'), 'utf8'));
    if (endpoint.protocol_version !== 1 || !Number.isInteger(endpoint.port) || endpoint.port < 1 || endpoint.port > 65535
      || typeof endpoint.token !== 'string' || endpoint.token.length !== 64 || typeof endpoint.instance_id !== 'string') {
      throw new Error('Computer Use 组件版本不兼容，请更新组件。');
    }
    return endpoint;
  }

  async start() {
    if (this.starting) return this.starting;
    this.starting = (async () => {
      // Platform VSIXs and the all-platform VSIX use the same directory layout.
      // Keep the flat layout for Desktop, Python and existing source builds.
      const platformBundle = path.join(this.bundleDirectory, `${process.platform}-${process.arch}`);
      let executable = bundledExecutable(platformBundle);
      let platformPayload = true;
      try { await fs.access(executable); }
      catch { platformPayload = false; executable = bundledExecutable(this.bundleDirectory); }
      if (platformPayload) {
        const manifest = JSON.parse(await fs.readFile(path.join(platformBundle, 'runtime.json'), 'utf8'));
        if (manifest.platform !== process.platform || manifest.arch !== process.arch || manifest.protocol_version !== 1) {
          throw new Error('Computer Use 原生组件的平台或协议不匹配，请重新安装当前平台的扩展。');
        }
      }
      try { await fs.access(executable); }
      catch {
        executable = bundledExecutable(this.stateDir);
        try { await fs.access(executable); }
        catch { throw new Error('Computer Use 原生组件未安装，请安装当前平台的完整 CrabCode 扩展或共享组件。'); }
      }
      await new Promise((resolve, reject) => {
        execFile(executable, ['--install-and-start', '--state-dir', this.stateDir], { timeout: 25_000, windowsHide: true }, (error, _stdout, stderr) => {
          if (error) reject(new Error(stderr.trim() || 'Computer Use 组件启动失败，请重新安装组件后重试。'));
          else resolve();
        });
      });
    })().finally(() => { this.starting = null; });
    return this.starting;
  }

  async invoke(command, args = {}) {
    const method = METHODS[command];
    if (!method) throw new Error(`Unknown native Computer Use command: ${command}`);
    if (method === 'capabilities' || method === 'permissions') await this.start();
    const endpoint = await this.endpoint();
    if (args.instanceId && endpoint.instance_id !== args.instanceId) {
      throw new Error('Computer Use 执行器已重启，请重新检测并观察窗口。');
    }
    return request(endpoint, method, args);
  }
}

function request(endpoint, method, params, timeout = 95_000) {
  // Once a request is written it is never retried: an interrupted reply does
  // not establish whether native input was delivered.
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host: '127.0.0.1', port: endpoint.port });
    const chunks = [];
    let size = 0;
    let settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      socket.destroy();
      if (error) reject(error); else resolve(value);
    };
    const timer = setTimeout(() => finish(new Error('Computer Use 执行器响应超时；请先观察结果，不要直接重复输入。')), timeout);
    socket.on('connect', () => {
      socket.write(JSON.stringify({ token: endpoint.token, instance_id: endpoint.instance_id, method, params, deadline_ms: Date.now() + 85_000 }) + '\n');
    });
    socket.on('data', (chunk) => {
      size += chunk.length;
      if (size > 64 * 1024 * 1024) return finish(new Error('Computer Use 响应超过大小限制'));
      chunks.push(chunk);
      if (!chunk.includes(10)) return;
      try {
        const response = JSON.parse(Buffer.concat(chunks).toString('utf8').trim());
        if (response.ok !== true) finish(new Error(response.error || 'Computer Use 执行失败'));
        else finish(null, response.result);
      } catch (error) { finish(error); }
    });
    socket.on('error', () => finish(new Error('Computer Use 执行器连接中断；请先重新检测并观察结果。')));
    socket.on('end', () => finish(new Error('Computer Use 响应不完整；请先观察结果，不要直接重复输入。')));
  });
}

module.exports = { NativeComputerUse, bundledExecutable, stateDirectory, request };
