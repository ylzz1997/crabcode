# CrabCode Computer Use

Local companion for the CrabCode extension in local, Remote SSH, WSL and
dev-container windows. Install this extension on the **local** machine and
CrabCode in the workspace extension host. The universal main extension uses
this companion in local windows too.

`crabcode.computerUseEnabled` is enabled by default in CrabCode settings;
set it to `false` to disable Computer Use. The CrabCode status-bar menu's
Computer Use entry provides enable/disable, permission refresh, monitoring
screenshots and operation history. Operations target the local desktop.

On macOS, grant **Crab Computer Use** Accessibility and Screen Recording
permissions. Crab Desktop is not required. Delivery policy remains controlled
by the existing CrabCode session settings.

For source development, run `npm ci`, `npm run computer-use:build`, then
`npm run build`. The JavaScript prepublish hook does not rebuild native payloads.
**Actions → Client Packages → Run workflow** creates the three platform VSIXs
and an all-platform VSIX, together with the main extension and Desktop installers.
Publishing a GitHub Release also uploads these packages as release assets;
stable releases publish all VSIXs to Visual Studio Marketplace and Open VSX
using trusted publishing. The companion uses the native Computer Use icon.
See [client packaging](../../../CLIENT_PACKAGING.md) for downloads and signing,
and `../../computer-use/README.md` for native development and regression validation.
