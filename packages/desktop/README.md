# Crab Desktop

Crab Desktop is the shared React client for local and remote CrabCode Gateways.
It runs either in a browser or inside the Tauri desktop shell.

Choose **Queue** (default) or **Steer** under **Settings → General → Conversation →
Follow-up behavior**. Enter uses the
default; Cmd+Enter (macOS) / Ctrl+Enter (Windows/Linux) sends the opposite for just
one message. Shift+Enter inserts a newline. Queue executes follow-ups in order
after the current turn; Steer adds guidance at a safe boundary. After a stop or
failure, unexecuted queued messages can be restored into the composer.
Queued cards above the composer offer Steer and Delete. Their menu offers Edit
message, which recalls the message and its images for editing, and Disable queue,
which changes the default to Steer while keeping existing queued messages.

## Local macOS virtual machine

Computer Use supports an isolated local macOS VM managed by Lume, with guest
screenshots/input, shared folders and localhost service forwarding. See
[setup, runtime boundaries and troubleshooting](LOCAL_VM.md).

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

The bottom status bar stays visible during startup and in Settings. It shows
environment checks, live pip output, Gateway startup, connection progress, and
elapsed time. Click the status message to inspect the latest 100 log entries;
failed connections keep their error details and offer a retry action. Local
installation and authentication run on background workers so the desktop
window remains responsive while they are in progress.
The log also records the Gateway address and startup mode, plus the running
Gateway's version, package path, Python version and executable, environment
directory/type, platform, and startup directory. Runtime details are returned
by the authenticated workspace endpoint, so remote connections describe the
server's environment. Older Gateways can still connect without this metadata.
For local connections, packaged Desktop first checks the configured Python and
other detected Python environments for an existing CrabCode installation. It
reuses an installation only when its version matches Desktop, its Gateway
protocol and CLI/server dependencies pass checks, and the actual Gateway process
starts and passes its health check. Unusable candidates are skipped with a
diagnostic in the startup log. If none is usable, Desktop creates or reuses
`~/.crabcode/desktop/gateway-venv` and installs CrabCode there as needed; it does
not install into or upgrade external system, Homebrew, or Conda environments.
This rule applies to automatic provisioning. **Settings → General → CrabCode
Suite** provides a component checklist and install button. Gateway is always
selected, while Search, Debugger, and future optional capabilities can be
checked independently for the Python environment Desktop resolves for the
local Gateway. Search has substantially larger dependencies. Installing Search
or Debugger does not enable those tools automatically; add their import paths
under **Runtime & Tools** when they should be available to new sessions.

**Settings → General → System Tools** manages host command-line utilities
separately from the CrabCode suite. Ripgrep is checked before installation: an
existing `rg` is reused, otherwise Desktop downloads a checksum-pinned official
release binary into the selected Python environment and exposes its scripts
directory to the local Gateway.
`npm run tauri dev` instead uses the configured Python or
the terminal's active Python environment directly so Gateway source and local
editable installs can be debugged.

For a remote Gateway, prefer HTTPS/WSS. An HTTP remote connection requires
explicit acknowledgement in the connection dialog. A browser UI hosted away
from localhost must also be allowed by the Gateway's `--cors` setting.

New conversations inherit the connection's most recently used model, reasoning
effort, Agent/Plan mode, Ultra setting, and permission mode. Unsent composer
text and attachments remain in the composer while the new conversation opens.

Tauri writes non-secret UI state to `~/.crabcode/settings_desktop.json`.

Set `CRABCODE_HOME` to an absolute directory (or `~/...`) before launching Desktop
to relocate its settings, managed Gateway environment and other global data.
The default is `~/.crabcode`. The app must inherit this variable; launching it
from the system launcher may not inherit terminal exports. Existing files are
not moved automatically. See [global configuration](../../README.md#custom-global-directory).

Gateway model and tool settings continue to use the normal `settings.json`.
The Models settings section queries the active Gateway for raw named-model
fields, group inheritance, and effective configuration, and can create, edit,
delete, or set the default model in the selected user, project, or local
settings layer.
The Runtime & Tools settings section edits Computer Use's independent
`target_scope` (`app_window` / `desktop`) and `delivery_policy`
(`strict_background` / `allow_foreground`), file snapshots, and extra tools.
Defaults are `app_window + allow_foreground`. Desktop scope requires
`allow_foreground`. These are user/session settings, not model action arguments.


Legacy `computer_use.mode` values migrate only the target scope, never the
foreground permission. Old Desktop hosts must be upgraded before input can be
sent. Policy is preserved across session binding and preview reconnection.
Disabling file snapshots does not disable conversation checkpoints; changes
apply to new or reconnected sessions.

The native action receipt separates `action_dispatched` (true/false/null),
`effect_verified`, `focus_isolation`, and `retry_safe`. Null dispatch means
input may have arrived; a transport failure is not permission to click again.

On macOS, background pointer events carry a window ID and window-local position.
The position uses the private `CGEventSetWindowLocation` symbol, resolved at runtime;
if it is unavailable, window-targeted pointer input returns an error without
falling back to full-desktop input. Under `allow_foreground`, the target might
already have been activated during preparation. Compatibility can change with macOS or the target app.
Scroll results confirm dispatch only (`effect_verified: false`); the agent must
check the target area in the observation. Both macOS modes now use pixels with
positive deltas down/right, rather than the old mixed units and signs.

macOS window lists use one WindowServer snapshot. `application_frontmost`
describes the process; `focused` describes the AX focused window and is `null`
when focus cannot be established. Save sheets and their internal content
windows are resolved through AX ownership, never by matching titles or position.
If a target disappears after dispatched input, `target_disappeared_after_action: true` requests observation
of the parent without treating the closure as a failed dispatch. This does not
confirm that the intended save or other operation succeeded.

Native receipts include `timings_ms` for worker queueing, window enumeration,
AX lookup/input preparation, dispatch, capture, encoding, and `native_total`.
Nested stage totals overlap and should not be added together. AX messages use
a one-second per-element timeout; this is not an overall action deadline.
Model requests retain the five most recent Computer Use screenshot messages;
older receipts and the complete original images remain in the session history.

## Build and test

```bash
npm test
npm run build
npm run tauri build
```

The Gateway WebSocket protocol remains version 1.

The opt-in macOS scroll integration test launches two overlapping, isolated
AppKit windows in one process, checks the intended scroll offset and verifies
that the other window, frontmost app and real pointer stay unchanged. It requires
Accessibility permission for the test runner and an idle pointer/focus during
the input check:

```bash
cd src-tauri
cargo test --lib computer_use::tests::macos_background_scroll_targets_one_of_two_windows_without_focus -- --ignored --nocapture
```

The real save-panel regression creates an isolated `NSSavePanel`, clicks its
Save button through AX, and checks the saved file in a temporary directory:

```bash
cargo test --lib computer_use::regression_tests::macos_real_save_sheet_receives_one_ax_click_and_saves_file -- --ignored --nocapture --test-threads=1
```
