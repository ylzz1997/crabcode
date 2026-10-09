# CrabCode for Visual Studio Code

Use [CrabCode](https://github.com/ylzz1997/crabcode) in Visual Studio Code. The
extension connects to a CrabCode Gateway, providing streaming chat, code-aware
actions, session management, permission prompts, file-change review, and
checkpoint recovery without leaving the editor.

## Requirements

- Visual Studio Code 1.85.0 or later.
- Python 3.10 or later when the extension starts a local Gateway.
- A running CrabCode Gateway for a remote connection.
- A model provider configured on the Gateway, with its required credentials.

## Quick start

1. Install this extension from the Visual Studio Marketplace.
2. For a local Gateway, install CrabCode with Gateway support:

   ```bash
   python -m pip install "crabcode[gateway]==0.1.5"
   crabcode gateway
   ```

   The default Gateway WebSocket address is `ws://localhost:4096/ws`.
   The extension can also detect, install, and start a local Gateway when
   `crabcode.gatewayAutoInstall` is enabled. Automatic installation intentionally
   stays on the lightweight `gateway` extra. From CrabCode settings, select
   **选择并安装其他套件**, or run **CrabCode：选择并安装本地套件**, to add
   optional capabilities through a multi-select checklist. Gateway is always
   included; Search and Debugger can be checked independently.
3. Configure the model provider, model, and credentials on the Gateway machine
   using `~/.crabcode/settings.json`. See the
   [model configuration examples](https://github.com/ylzz1997/crabcode#multi-api-support-1).
   Provider API keys belong to the Gateway configuration, not the extension's
   `crabcode.password` setting. Restart the Gateway after changing its environment.
4. Open a project folder in VS Code, select **CrabCode** in the Activity Bar,
   and send a message. Use `CrabCode：连接网关` if a connection is not established.

For a Gateway on another machine, start it there and configure
`crabcode.serverUrl` with its `ws://` or `wss://` address. Use `wss://` when
the Gateway is accessed beyond the local machine. Remote Gateways must already
be installed and running; the extension does not install them remotely.

## Features

- Follow-up behavior: choose Queue (default) or Steer with `crabcode.followUpMode`.
  Queued cards above the composer offer Steer and Delete. Their menu offers Edit
  message (recall with images) and Disable queue (use Steer for future messages;
  existing queued messages remain). Enter uses the default; Cmd+Enter on macOS
  or Ctrl+Enter elsewhere sends the opposite for one message. Shift+Enter inserts
  a newline. Queue runs messages in order after the current turn; Steer supplies
  guidance at the next safe boundary. Unsent input remains recoverable after a stop.
- Streaming chat with Markdown, code blocks, diffs, tool results, attachments,
  permissions, choices, and plans.
- Explain, fix, refactor, test, or send the selected editor code to chat.
- Create, resume, interrupt, and fork conversation sessions.
- Choose reasoning effort from the composer capsule (none through max). Enable
  Ultra mode from **+**, and click its gradient capsule to turn it off. These
  settings are saved per session in the workspace and restored on reconnect.
  Ultra uses the Desktop spectrum animation and respects reduced motion.
- Review and keep or undo pending file edits.
- Start, reconnect to, or restart a local Gateway from the editor.
- See the active editor as a removable context capsule above the composer. Use
  **+ → IDE context** to toggle it or reference workspace files and folders with
  the native VS Code picker; current and added references are also available
  from `@` in the composer and are injected into that message by path.

## Configuration

Open **Settings** and search for `CrabCode`, or add settings such as:

```json
{
  "crabcode.serverUrl": "ws://localhost:4096/ws",
  "crabcode.autoConnect": true,
  "crabcode.gatewayAutoInstall": true,
  "crabcode.pythonPath": "",
  "crabcode.showDiffOnFileChange": false,
  "crabcode.computerUseTargetScope": "app_window",
  "crabcode.computerUseDeliveryPolicy": "allow_foreground"
}
```

Explicit VS Code values override the Gateway's `computer_use.target_scope`
and `computer_use.delivery_policy` when creating, resuming or sending to a
session. Merely inheriting an extension default does not override Gateway settings.
Targets are `app_window` and `desktop`; policies are `strict_background`
and `allow_foreground`. Desktop scope requires foreground permission.

The Computer Use delivery policy is independent of tool approval modes,
including Full Access (`run_everything` / `bypassPermissions`). Strict
background uses process/window-targeted input and never exposes `focus_window`.
Some applications can ignore input while inactive; select `allow_foreground`
in settings to permit activation-assisted fallback. The model cannot set this
permission inside a ComputerUse action.
Legacy `crabcode.computerUseMode` remains a target alias only and grants no
foreground permission. The new target setting takes precedence over that alias.

Installing a suite provides its Python package and dependencies. Search and
Debugger remain disabled until their import paths are added to the Gateway's
`extra_tools` configuration. Remote Gateway suites must be installed on the
remote host.

### Gateway authentication

The Gateway binds to `127.0.0.1:4096` and disables authentication by default,
unless you have configured another security mode. To enable password
authentication for a manually started Gateway, for example in a POSIX shell:

```bash
export CRABCODE_GATEWAY_PASSWORD="replace-with-a-strong-password"
crabcode gateway --security-mode password
```

Set the same value in **VS Code User Settings**:

```json
{
  "crabcode.serverUrl": "ws://localhost:4096/ws",
  "crabcode.password": "replace-with-a-strong-password"
}
```

The extension sends this value as a Bearer credential using the Gateway's
compatible password authentication. This setting is stored in VS Code's
settings JSON; do not add a real password to shared workspace settings or source
control. The extension does not perform the Gateway's public-key challenge and
signature flow; use `password` mode, or the password method in `mixed` mode.
For a remote Gateway, configure HTTPS/WSS and use its `wss://.../ws` address.

`crabcode.chatModels` lists the Gateway model profiles shown in the chat UI;
`crabcode.chatModelDefault` selects the initial one. `crabcode.permissionMode`
controls how the current Gateway session handles tool permissions.

By default, Enter sends a message and Ctrl+Enter (Windows/Linux) or Cmd+Enter
(macOS) inserts a newline. Set `crabcode.composerSendKey` to `mod_enter` to
reverse these actions.

## Commands

Open the Command Palette and run one of the following:

- `CrabCode：打开聊天`
- `CrabCode：解释选中代码`
- `CrabCode：修复选中代码`
- `CrabCode：重构选中代码`
- `CrabCode：为选中代码添加测试`
- `CrabCode：发送到聊天`
- `CrabCode：连接网关` / `CrabCode：断开网关`
- `CrabCode：新建会话` / `CrabCode：中断当前任务`
- `CrabCode：重启网关` / `CrabCode：打开扩展设置`

## Troubleshooting

- **Cannot connect:** check `crabcode.serverUrl`, the Gateway process, and the
  password. The local health endpoint is `http://localhost:4096/health`.
- **Python not found:** set `crabcode.pythonPath` to the Python 3.10+ executable
  in the environment where you installed CrabCode.
- **Incompatible Gateway:** follow the local upgrade prompt, or upgrade the
  Gateway on its host if using a remote connection.
- **Connected but model requests fail:** check the Gateway's model configuration
  and provider credentials.

Open **View → Output → CrabCode** for connection and startup diagnostics.
`crabcode.debugLogRawWsPayload` is off by default; enabling it can log message
content, so review logs before sharing them.

## Building from source

From `packages/vscode`, use Node.js 24 (the tested version is in `.nvmrc`):

```bash
nvm use
npm ci
npm run lint
npm test
npm run package
```

The package command builds the extension and creates `crabcode-0.1.5.vsix`.
Install it with **Extensions → … → Install from VSIX…**, or run
`code --install-extension crabcode-0.1.5.vsix`. Node.js and npm are only required
for building the extension, not for installing the VSIX in VS Code.

## Support and source

Report issues or contribute at
[ylzz1997/crabcode](https://github.com/ylzz1997/crabcode). The full Gateway
configuration and security documentation is in the repository's
[README](https://github.com/ylzz1997/crabcode#gateway-server).

## License

[MIT](https://github.com/ylzz1997/crabcode/blob/main/packages/vscode/LICENSE)
