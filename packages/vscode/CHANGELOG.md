# Changelog

All notable changes to the CrabCode Visual Studio Code extension are documented
in this file.

## 0.1.9

- Build client packages when a GitHub Release is published and attach all VSIXs
  and macOS/Windows Desktop installers with SHA256 checksums.
- Publish the prebuilt main and Computer Use extensions to Visual Studio
  Marketplace and Open VSX through trusted publishing on stable releases.
- Give the Computer Use companion its own native Computer Use icon and verify
  that packaging preserves each extension's icon.

## 0.1.6

- Add `crabcode.computerUseMode` with background-application and foreground-desktop modes; explicit VS Code values override the Gateway mode without automatic fallback.
- Add Desktop-style composer capsules for reasoning effort, models, and modes.
- Add the seven reasoning effort levels and an Ultra toggle in the **+** menu,
  with a removable gradient capsule and bounded spectrum animation.
- Restore confirmed effort and Ultra settings per workspace, Gateway, and session.
- Support narrow sidebars, theme colors, keyboard effort selection, and reduced motion.
- Keep model and reasoning capsules on one adaptive row in narrow sidebars, and
  remove the redundant footer Agent/Plan selector now that Plan lives in the
  composer menu and active-mode capsule.
- Show active IDE context as a removable composer capsule, add a responsive
  **+ → IDE context** submenu for current-file and workspace file/folder
  references, and expose those references through composer `@` completion.

## 0.1.5

### Marketplace preparation

- Add extension-specific setup, authentication, usage, and troubleshooting
  documentation, plus this changelog.
- Exclude local logs, environment files, tests, and source maps from the VSIX.
- Record Node.js 24.16.0 as the build environment in `.nvmrc`.
- Correct a context-tooltip test to match the existing English estimate label.

### Included capabilities

- Streaming Gateway chat, code actions, session controls, permission and choice
  prompts, attachments, file-edit review, and checkpoint recovery.
- Inline single and multiple image results with ordered captions, including
  restored session history.
- Local Gateway detection, installation, startup, reconnection, and restart
  controls.
- Configurable chat models, permission mode, file upload limits, diff preview,
  and send-key behavior.
