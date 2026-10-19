# Shared Computer Use

Desktop, VS Code and CLI use the same native executor. Desktop and VS Code
also share the JavaScript host channel. Neither VS Code nor CLI requires
Crab Desktop.

```
Gateway ComputerUseBroker
  -> computer-use/client/src/channel.ts (Desktop / VS Code / local companion)
  -> authenticated loopback RPC
  -> computer-use/host (one per OS user, one native execution queue)
  -> computer-use/core (original Desktop engine)
```

The CLI binds Core's `ComputerUse` tool directly to this executor through a
small Python IPC adapter in `packages/cli/crabcode_cli/computer_use.py`. It
does not need Gateway, WebSocket, Node or either GUI client. Native capture,
input, AX ownership and delivery policies remain implemented only in `core`.

Source layout:

```
packages/computer-use/{core,host,client,scripts}
packages/vscode/computer-use/  # local UI extension for remote workspaces
packages/shared/              # unrelated cross-client chat utilities
```

`core` contains the original native implementation, background profiles and
regression fixtures. Only the Tauri entry points were replaced by ordinary Rust
exports. The Desktop VM guest also links this crate. Do not maintain a separate
VS Code implementation of capture, input, AX ownership or delivery policies.

`packages/computer-use/client/src/channel.ts` retains the original Desktop
Gateway protocol, model/monitor screenshot separation, preview retention and
release behavior. `channel.js` and `channel.d.ts` are checked-in generated files;
regenerate with `node packages/computer-use/client/build.mjs`.

## Installation and permissions

Each platform distribution bundles `host`. First enablement installs it in the
per-user application-data directory, under `CrabCode/ComputerUse`. macOS uses
`~/Library/Application Support/CrabCode/ComputerUse/Crab Computer Use.app`.
Grant that app Accessibility and Screen Recording access. Desktop and VS Code
recheck local component permissions every 30 seconds while enabled; select
refresh in the client to check immediately. macOS checks run in a fresh process
so a long-running executor cannot keep reporting a revoked grant. The shared
executor's permissions action opens the
relevant settings pane and a floating card that drags the installed app bundle
into the Screen Recording or Accessibility list. The card appears from Desktop,
VS Code or `crabcode computer-use permissions`; CLI can select either pane
directly with `--screen-recording` or `--accessibility`. Existing Desktop
permissions do not transfer to the new app.
Local ad-hoc builds can require renewed authorization after recompilation.
Local client build commands copy the same signed bundle into Desktop, VS Code,
and the local companion. The shared installer keeps a newer installed build
when an older client starts, so switching clients does not repeatedly replace
the authorized helper. A genuinely new ad-hoc build can still require one
authorization; stable signing remains necessary to preserve TCC across changes.
If the switches are on but TCC logs report a code-requirement mismatch, reset
only this development component with `tccutil reset Accessibility
io.crabcode.computer-use` and `tccutil reset ScreenCapture
io.crabcode.computer-use`, then manually authorize the installed app again.
The application never resets or grants system permissions automatically.
Release builds must use the same `CRABCODE_SIGNING_IDENTITY` for both clients;
the native component must be included in the distribution's signing and
notarization process.
The Python release workflow imports the optional
`CRABCODE_SIGNING_CERTIFICATE_P12_BASE64` and
`CRABCODE_SIGNING_CERTIFICATE_PASSWORD` repository secrets and passes
`CRABCODE_SIGNING_IDENTITY` to the component build. Without signing credentials,
macOS builds are ad hoc and may require renewed authorization on upgrade.

An exclusive OS lock prevents multiple executors. The endpoint descriptor is
private to the user and contains a random authentication token; do not log or
publish it. Only loopback connections are accepted. Every process start has a
new instance ID, so actions referring to a previous process are rejected.
Interrupted input requests are never replayed automatically.

Clients heartbeat their connection every 30 seconds. Native references for a
crashed connection expire after 120 seconds. Explicit release is scoped to the
client connection, session and agent. Upgrade waits for active leases/actions
to finish; it never terminates an active task. A different component build can
be installed when idle, followed by capability refresh.

For Remote SSH, WSL and dev containers, install `crabcode-computer-use` on the
local UI side. The main extension forwards the Gateway URL using
`vscode.env.asExternalUri`; the local companion owns native execution and the
monitor. A remote machine's desktop is not selected implicitly.

## CLI

Platform Python wheels for macOS (arm64 / x86_64) and Windows x64 include the
native component in `crabcode_cli/_native`. `pip install crabcode` installs the
component along with the Python code. On first launch, CLI automatically deploys
it to the shared user directory; later sessions reuse the singleton executor.
Both interactive and pipe sessions connect automatically. Run `crabcode` as
usual; no separate installation command or session enablement flag is needed.
Startup stays quiet; `/status` reports capture, input and AX availability.
Capabilities determine whether Core exposes the tool. CLI follows the existing
`computer_use` settings and tool permission policy. With no installed component
or graphical session, ordinary coding remains available.

When the connected executor is Windows and no target scope was explicitly set,
Core selects desktop scope if the session already allows foreground input. This
also applies to Desktop and VS Code connected to a Gateway on another OS.
Explicit window targets and strict-background settings are preserved; clients
never rewrite these settings to enable Windows input.
Windows shortcut letters are case-insensitive when used with Ctrl, Alt or Win;
include Shift explicitly when needed. Text pastes preserve existing clipboard
formats, including images, file lists and rich text, and do not overwrite data
copied during the operation. An unsupported clipboard format is reported before
the clipboard is changed or input is sent.

```sh
crabcode computer-use status
crabcode computer-use status --json
crabcode computer-use permissions  # macOS permission settings and drag guide
crabcode computer-use permissions --screen-recording
crabcode computer-use permissions --accessibility
```

Source/editable installations and universal Python wheels do not embed a native
binary. They can reuse Desktop or VS Code's installed component; otherwise,
install a platform wheel containing the executor. For local development, build
a native bundle and package it into a wheel as described below.
After first-time installation, start a new CLI session. Permission changes are
detected during an existing session.
An SSH CLI controls the machine on which it runs; it does not forward to a
different desktop. VS Code's local companion is the supported remote UI route.

## Build and validation

The app icon lives in `icons/icon.png` (master), `icons/icon.icns` (macOS),
and `icons/icon.ico` (Windows, 16/24/32/48/64/128/256 px).
It keeps CrabCode's charcoal-and-silver crab, with a screen and pointer for
Computer Use. After editing the master, regenerate both platform formats with
`node packages/computer-use/scripts/build-icons.mjs` on macOS.
The shared build copies the macOS icon into the app before signing. The Windows
host embeds the ICO into its executable using the Windows SDK resource compiler
(MSVC) or windres (GNU). The icon files are checked in; normal builds do not need
the macOS image tools. Desktop, VS Code and CLI platform wheels carry the same
platform-specific native component and icon. Bundle resource changes
participate in the host build ID and follow the existing idle-only upgrade path.

The Python release workflow builds native wheels on macOS arm64, macOS Intel
and Windows x64, plus a universal wheel/source distribution for other environments.
Both the aggregate `crabcode` package and standalone `crabcode-cli` support the
same build hook. To include a prebuilt bundle, set
`CRABCODE_COMPUTER_USE_BUNDLE` to its directory when running `python -m build --wheel`.
The hook verifies component version, architecture and macOS signature, keeps the
complete app bundle, and derives the wheel's minimum macOS version from the binary.
No compiler or network download is needed when installing the resulting wheel.
Use `python packages/computer-use/scripts/check-wheel.py <wheel-directory>` to
verify wheel metadata, the native payload, executable permissions and app icons.
Windows wheel verification requires `python -m pip install pefile` and compares
the embedded icon images with the checked-in ICO, including every size.

From the repository root:

```sh
node packages/computer-use/scripts/build.mjs --debug
node packages/computer-use/client/build.mjs
cargo test --locked --manifest-path packages/computer-use/Cargo.toml --workspace
```

For VS Code extension-host development, `npm run watch` from `packages/vscode`
builds a debug native bundle before starting the JavaScript watcher; the F5
launch task uses this command too. After native source changes, rebuild with
`npm run computer-use:build -- --debug`. JavaScript watcher rebuilds do not
recompile native code. Extensions without a native bundle can reuse the shared
component already installed by CLI, Desktop or another CrabCode extension.
The local companion uses the same native build command from its own package
directory.

Desktop's Tauri build hook and both VS Code extensions' `vscode:prepublish`
scripts include the native component. Package platform-specific VSIX files;
build Windows binaries on Windows and macOS binaries on macOS, matching the
target architecture. `--out` selects a client resource directory. Release
builds are the default; `--debug` is intended for development only.

The core's ignored macOS tests exercise live input and need an unlocked GUI
and OS permissions. The opt-in `scripts/live-smoke.py` uses the real Gateway
broker, shared Node channel and installed executor against an isolated AppKit
fixture; it never types into an existing user document.
Use `--client cli` to exercise Core's tool with the CLI adapter and installed
executor, without starting Gateway or Node. The default `--client gateway`
tests the shared JavaScript channel.
