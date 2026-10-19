const { test } = require('node:test');
const assert = require('node:assert/strict');
const net = require('node:net');
const { once } = require('node:events');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { request, bundledExecutable } = require('../../computer-use/client/nativeClient.cjs');

async function startupHarness(t, platform = process.platform, arch = process.arch) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'crabcode-native-start-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const bundle = path.join(root, 'bundle'), state = path.join(root, 'state');
  const calls = [];
  let startupError = null;
  const module = { exports: {} };
  const source = await fs.readFile(require.resolve('../../computer-use/client/nativeClient.cjs'), 'utf8');
  vm.runInNewContext(source, {
    module, process: { ...process, platform, arch },
    require: name => name === 'node:child_process' ? {
      execFile: (executable, args, options, callback) => {
        calls.push({ executable, args, options });
        callback(startupError, '', startupError ? 'executor failed' : '');
      },
    } : require(name),
  });
  const payload = async directory => {
    const executable = module.exports.bundledExecutable(directory);
    await fs.mkdir(path.dirname(executable), { recursive: true });
    await fs.writeFile(executable, 'test payload');
    return executable;
  };
  return { bundle, state, calls, payload,
    client: new module.exports.NativeComputerUse(bundle, state),
    failStartup: () => { startupError = new Error('startup failed'); },
  };
}

test('a source extension without a bundle starts the already installed shared component', async t => {
  const h = await startupHarness(t);
  const executable = await h.payload(h.state);
  await h.client.start();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].executable, executable);
  assert.deepEqual(Array.from(h.calls[0].args), ['--install-and-start', '--state-dir', h.state]);
});

test('the extension bundle takes precedence over an installed shared component', async t => {
  const h = await startupHarness(t);
  const executable = await h.payload(h.bundle);
  await h.payload(h.state);
  await h.client.start();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].executable, executable);
});

for (const platform of ['darwin-arm64', 'darwin-x64', 'win32-x64']) {
  test(`a universal VSIX selects only its ${platform} payload`, async t => {
    const [targetOS, arch] = platform.split('-');
    const h = await startupHarness(t, targetOS, arch);
    const directory = path.join(h.bundle, platform);
    const executable = await h.payload(directory);
    await fs.writeFile(path.join(directory, 'runtime.json'), JSON.stringify({ platform: targetOS, arch, protocol_version: 1 }));
    await h.payload(h.bundle);
    await h.payload(h.state);
    await h.client.start();
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].executable, executable);
  });
}

test('a mismatched platform manifest is rejected without launching or falling back', async t => {
  const h = await startupHarness(t, 'darwin', 'arm64');
  const directory = path.join(h.bundle, 'darwin-arm64');
  await h.payload(directory);
  await fs.writeFile(path.join(directory, 'runtime.json'), JSON.stringify({ platform: 'darwin', arch: 'x64', protocol_version: 1 }));
  await h.payload(h.state);
  await assert.rejects(h.client.start(), /平台或协议不匹配/);
  assert.equal(h.calls.length, 0);
});

test('missing bundled and shared components report an installation error without launching', async t => {
  const h = await startupHarness(t);
  await assert.rejects(h.client.start(), /原生组件未安装/);
  assert.equal(h.calls.length, 0);
});

test('a failing bundled executor reports its error without starting another component', async t => {
  const h = await startupHarness(t);
  const executable = await h.payload(h.bundle);
  await h.payload(h.state);
  h.failStartup();
  await assert.rejects(h.client.start(), /executor failed/);
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].executable, executable);
});

async function server(handler, run) {
  const sockets = new Set();
  const listener = net.createServer(socket => {
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
    let input = '';
    socket.on('data', chunk => {
      input += chunk.toString();
      if (input.endsWith('\n')) handler(socket, JSON.parse(input));
    });
  });
  listener.listen(0, '127.0.0.1');
  await once(listener, 'listening');
  try { await run({ port: listener.address().port, token: 'a'.repeat(64), instance_id: 'i' }); }
  finally { for (const socket of sockets) socket.destroy(); listener.close(); await once(listener, 'close'); }
}

test('native IPC preserves large screenshots, AX metadata and receipt fields', async () => {
  const result = { ok: true, screenshot: { data: 'a'.repeat(256 * 1024), media_type: 'image/png' },
    accessibility: { snapshot_id: 's', elements: [{ element_id: 'e' }] },
    action_dispatched: true, effect_verified: false, focus_isolation: 'preserved' };
  await server((socket, message) => {
    assert.equal(message.method, 'execute');
    assert.equal(message.params.instanceId, 'i');
    const bytes = Buffer.from(JSON.stringify({ ok: true, result }) + '\n');
    for (let i = 0; i < bytes.length; i += 1703) socket.write(bytes.subarray(i, i + 1703));
  }, async endpoint => assert.deepEqual(await request(endpoint, 'execute', { instanceId: 'i' }), result));
});

test('an interrupted native reply never resends input', async () => {
  let count = 0;
  await server(socket => { count++; socket.end('{"ok":true,'); }, async endpoint => {
    await assert.rejects(request(endpoint, 'execute', {}), /不完整/);
    assert.equal(count, 1);
  });
});

test('native IPC times out a nonresponsive executor without retrying', async () => {
  let count = 0;
  await server(() => { count++; }, async endpoint => {
    await assert.rejects(request(endpoint, 'execute', {}, 30), /超时/);
    assert.equal(count, 1);
  });
});

test('native executor errors propagate without converting them into successful receipts', async () => {
  await server(socket => socket.end(JSON.stringify({ ok: false, error: 'stale instance' }) + '\n'), async endpoint => {
    await assert.rejects(request(endpoint, 'execute', {}), /stale instance/);
  });
});
