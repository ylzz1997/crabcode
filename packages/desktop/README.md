# Crab Desktop

Crab Desktop is the shared React client for local and remote CrabCode Gateways.
It runs either in a browser or inside the Tauri desktop shell.

## Development

```bash
cd packages/desktop
npm install

# Browser mode
npm run dev

# Tauri mode
npm run tauri dev
```

Browser mode opens at `http://127.0.0.1:1420`, stores connection and project UI
state in `localStorage`, and keeps passwords only in the current tab's
`sessionStorage`. It connects to an already-running Gateway. Tauri mode adds
system credential storage and automatic local Gateway installation/startup.

For a remote Gateway, prefer HTTPS/WSS. An HTTP remote connection requires
explicit acknowledgement in the connection dialog. A browser UI hosted away
from localhost must also be allowed by the Gateway's `--cors` setting.

Tauri writes non-secret UI state to `~/.crabcode/settings_desktop.json`.
Gateway model and tool settings continue to use the normal `settings.json`.
The Models settings section is read-only: it queries the active Gateway for
the raw named-model fields, group inheritance, and effective configuration.

## Build and test

```bash
npm test
npm run build
npm run tauri build
```

The Gateway WebSocket protocol remains version 1.
