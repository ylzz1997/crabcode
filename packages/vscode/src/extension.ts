/**
 * CrabCode VS Code extension — main entry point.
 *
 * Wires together the WebSocket connection, chat panel, permission/choice
 * handlers, context provider, file-change handler, status bar, and all
 * registered commands.
 */

import * as vscode from "vscode";

import { CrabCodeConnection } from "./connection";
import { ChatPanelProvider } from "./chatPanel";

import {
  buildPermissionResponseCommand,
  buildChoiceResponseCommand,
  serializeCommand,
} from "./client/protocol";

import type {
  EventPayload,
  PermissionRequestPayload,
  ChoiceRequestPayload,
  FileChangePayload,
} from "./client/types";

// ── Disposable tracker for deactivate() ─────────────────────────────

const disposables: vscode.Disposable[] = [];

function push<T extends vscode.Disposable>(d: T): T {
  disposables.push(d);
  return d;
}

// ── PermissionHandler ───────────────────────────────────────────────

class PermissionHandler implements vscode.Disposable {
  private pending = new Map<string, string>();

  constructor(private readonly connection: CrabCodeConnection) {}

  handle(payload: PermissionRequestPayload): void {
    const { tool_name, tool_use_id, reason, agent_id } = payload;

    const detail =
      (reason ? `${reason}\n\n` : "") +
      `Tool: ${tool_name}`;

    const allowItem = "Allow";
    const alwaysAllowItem = "Always Allow";
    const denyItem = "Deny";

    vscode.window
      .showInformationMessage(
        `CrabCode Permission Request`,
        { modal: true, detail },
        allowItem,
        alwaysAllowItem,
        denyItem,
      )
      .then((choice) => {
        const allowed = choice === allowItem || choice === alwaysAllowItem;
        const alwaysAllow = choice === alwaysAllowItem;
        const cmd = buildPermissionResponseCommand(tool_use_id, allowed, {
          alwaysAllow,
          agentId: agent_id ?? undefined,
        });
        this.connection.sendRaw(serializeCommand(cmd));
      });
  }

  dispose(): void {
    this.pending.clear();
  }
}

// ── ChoiceHandler ───────────────────────────────────────────────────

class ChoiceHandler implements vscode.Disposable {
  constructor(private readonly connection: CrabCodeConnection) {}

  handle(payload: ChoiceRequestPayload): void {
    const { tool_use_id, question, options, multiple, agent_id } = payload;

    if (multiple) {
      // Show quick-pick with canPickMany
      vscode.window
        .showQuickPick(
          options.map((o) => ({ label: o })),
          { canPickMany: true, placeHolder: question },
        )
        .then((picked) => {
          const selected = picked ? picked.map((p) => p.label) : [];
          const cancelled = picked === undefined;
          const cmd = buildChoiceResponseCommand(tool_use_id, selected, {
            cancelled,
            agentId: agent_id ?? undefined,
          });
          this.connection.sendRaw(serializeCommand(cmd));
        });
    } else {
      vscode.window
        .showQuickPick(
          options.map((o) => ({ label: o })),
          { placeHolder: question },
        )
        .then((picked) => {
          const selected = picked ? [picked.label] : [];
          const cancelled = picked === undefined;
          const cmd = buildChoiceResponseCommand(tool_use_id, selected, {
            cancelled,
            agentId: agent_id ?? undefined,
          });
          this.connection.sendRaw(serializeCommand(cmd));
        });
    }
  }

  dispose(): void {}
}

// ── ContextProvider ─────────────────────────────────────────────────

class ContextProvider implements vscode.Disposable {
  private activeEditor: vscode.TextEditor | undefined;

  constructor(private readonly connection: CrabCodeConnection) {
    this.activeEditor = vscode.window.activeTextEditor;
    this.pushContext();

    push(
      vscode.window.onDidChangeActiveTextEditor((editor) => {
        this.activeEditor = editor;
        this.pushContext();
      }),
    );
    push(
      vscode.workspace.onDidChangeTextDocument(() => {
        this.pushContext();
      }),
    );
  }

  private pushContext(): void {
    const editor = this.activeEditor;
    if (!editor) {
      return;
    }

    const doc = editor.document;
    const selection = editor.selection;

    this.connection.pushContext({
      active_file: doc.uri.fsPath,
      selected_text: doc.getText(selection) || null,
      cursor_line: selection.active.line,
      cursor_column: selection.active.character,
      open_files: vscode.window.visibleTextEditors.map((e) => e.document.uri.fsPath),
      language_id: doc.languageId,
    });
  }

  dispose(): void {}
}

// ── FileChangeHandler ───────────────────────────────────────────────

class FileChangeHandler implements vscode.Disposable {
  private config: vscode.WorkspaceConfiguration;

  constructor(
    private readonly connection: CrabCodeConnection,
    _context: vscode.ExtensionContext,
  ) {
    this.config = vscode.workspace.getConfiguration("crabcode");

    // Listen for file_change events from the server (tool-made changes)
    push(
      connection.on("message", (payload: EventPayload) => {
        if (payload.type === "file_change") {
          this.handleServerFileChange(payload as FileChangePayload);
        }
      }),
    );
  }

  private handleServerFileChange(payload: FileChangePayload): void {
    const { action, path, diff } = payload;

    // Auto-reload the document if it's open in the editor
    for (const doc of vscode.workspace.textDocuments) {
      if (doc.uri.fsPath === path) {
        // VSCode auto-detects external changes, but we can force a revert
        // if the file watcher hasn't picked it up yet
        break;
      }
    }

    const showDiff = this.config.get<boolean>("showDiffOnFileChange", false);

    if (showDiff && diff && (action === "modify" || action === "create")) {
      // Show a virtual diff document
      const name = `CrabCode: ${path.split("/").pop()} (${action})`;
      const content = diff;
      showDiffDocument(name, content, path);
    } else {
      vscode.window.setStatusBarMessage(
        `CrabCode: ${action} ${path.split("/").pop()}`,
        3000,
      );
    }
  }

  dispose(): void {}
}

async function showDiffDocument(
  name: string,
  diffContent: string,
  filePath: string,
): Promise<void> {
  const uri = vscode.Uri.parse(`untitled:${name}`);
  try {
    const doc = await vscode.workspace.openTextDocument(uri);
    const editor = await vscode.window.showTextDocument(doc, {
      preview: true,
      preserveFocus: true,
      viewColumn: vscode.ViewColumn.Beside,
    });
    await editor.edit((edit) => {
      edit.insert(new vscode.Position(0, 0), diffContent);
    });
    // Set language to diff for syntax highlighting
    await vscode.languages.setTextDocumentLanguage(doc, "diff");
  } catch {
    // Fallback: just show status message
    vscode.window.setStatusBarMessage(`CrabCode: ${name}`, 4000);
  }
}

// ── Status bar ──────────────────────────────────────────────────────

function createStatusBar(
  connection: CrabCodeConnection,
): vscode.StatusBarItem {
  const item = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right,
    100,
  );

  item.command = "crabcode.openSettings";
  item.tooltip = "CrabCode — click to open settings";

  function update() {
    if (connection.connected) {
      item.text = `$(circle-filled) CrabCode${connection.modelName ? ` — ${connection.modelName}` : ""}`;
      item.backgroundColor = undefined;
    } else {
      item.text = "$(circle-slash) CrabCode — Disconnected";
      item.backgroundColor = new vscode.ThemeColor(
        "statusBarItem.errorBackground",
      );
    }
  }

  update();
  push(connection.on("connected", () => update()));
  push(connection.on("disconnected", () => update()));

  item.show();
  return item;
}

// ── Commands ────────────────────────────────────────────────────────

function registerCommands(
  chatProvider: ChatPanelProvider,
  connection: CrabCodeConnection,
): void {
  // Open Chat
  push(
    vscode.commands.registerCommand("crabcode.openChat", () => {
      chatProvider.reveal();
    }),
  );

  // Explain Code
  push(
    vscode.commands.registerCommand("crabcode.explainCode", () => {
      const editor = vscode.window.activeTextEditor;
      const selection = editor?.selection;
      const text = editor?.document.getText(selection);
      if (text) {
        chatProvider.sendPrompt(`Explain this code:\n\n${text}`);
      } else {
        vscode.window.showWarningMessage("Select some code first.");
      }
    }),
  );

  // Fix Code
  push(
    vscode.commands.registerCommand("crabcode.fixCode", () => {
      const editor = vscode.window.activeTextEditor;
      const selection = editor?.selection;
      const text = editor?.document.getText(selection);
      if (text) {
        chatProvider.sendPrompt(`Fix the issues in this code:\n\n${text}`);
      } else {
        vscode.window.showWarningMessage("Select some code first.");
      }
    }),
  );

  // Refactor Code
  push(
    vscode.commands.registerCommand("crabcode.refactorCode", () => {
      const editor = vscode.window.activeTextEditor;
      const selection = editor?.selection;
      const text = editor?.document.getText(selection);
      if (text) {
        chatProvider.sendPrompt(`Refactor this code for clarity and efficiency:\n\n${text}`);
      } else {
        vscode.window.showWarningMessage("Select some code first.");
      }
    }),
  );

  // Add Tests
  push(
    vscode.commands.registerCommand("crabcode.addTests", () => {
      const editor = vscode.window.activeTextEditor;
      const selection = editor?.selection;
      const text = editor?.document.getText(selection);
      if (text) {
        chatProvider.sendPrompt(`Write tests for this code:\n\n${text}`);
      } else {
        vscode.window.showWarningMessage("Select some code first.");
      }
    }),
  );

  // Send to Chat
  push(
    vscode.commands.registerCommand("crabcode.sendToChat", () => {
      const editor = vscode.window.activeTextEditor;
      const selection = editor?.selection;
      const text = editor?.document.getText(selection);
      if (text) {
        chatProvider.prefillInput(text);
      } else {
        vscode.window.showWarningMessage("Select some code first.");
      }
    }),
  );

  // Open Settings
  push(
    vscode.commands.registerCommand("crabcode.openSettings", () => {
      vscode.commands.executeCommand(
        "workbench.action.openSettings",
        "crabcode",
      );
    }),
  );

  // Connect
  push(
    vscode.commands.registerCommand("crabcode.connect", () => {
      if (!connection.connected) {
        connection.connect();
      }
    }),
  );

  // Disconnect
  push(
    vscode.commands.registerCommand("crabcode.disconnect", () => {
      connection.dispose();
    }),
  );

  // Interrupt
  push(
    vscode.commands.registerCommand("crabcode.interrupt", () => {
      connection.sendInterrupt();
    }),
  );

  // New Session
  push(
    vscode.commands.registerCommand("crabcode.newSession", () => {
      const cwd =
        vscode.workspace.workspaceFolders?.[0]?.uri?.fsPath ?? null;
      connection.sendNewSession(cwd);
      vscode.window.setStatusBarMessage("CrabCode: New session created", 3000);
    }),
  );
}

// ── Auto-connect ────────────────────────────────────────────────────

async function autoConnect(
  connection: CrabCodeConnection,
  config: vscode.WorkspaceConfiguration,
): Promise<void> {
  const autoConnectEnabled = config.get<boolean>("autoConnect", true);
  if (!autoConnectEnabled) {
    return;
  }

  connection.connect();

  // Wait briefly to see if the connection succeeds
  await new Promise<void>((resolve) => {
    let settled = false;

    const onConnected = connection.on("connected", () => {
      if (settled) return;
      settled = true;
      sub.dispose();
      resolve();
    });

    const onDisconnected = connection.on("disconnected", () => {
      if (settled) return;
      settled = true;
      sub.dispose();
      onConnected.dispose();
      resolve();
    });

    const sub = onDisconnected;

    // Give it 5 seconds
    setTimeout(() => {
      if (settled) return;
      settled = true;
      onConnected.dispose();
      sub.dispose();
      resolve();
    }, 5000);
  });

  // Show warning if not connected after initial attempt
  if (!connection.connected) {
    vscode.window.showWarningMessage(
      "CrabCode: Could not connect to server. Will retry automatically. Check your serverUrl setting.",
    );
  } else {
    // Create a session with the workspace root as cwd
    const cwd = vscode.workspace.workspaceFolders?.[0]?.uri?.fsPath ?? null;
    connection.sendNewSession(cwd);
  }
}

// ── activate / deactivate ──────────────────────────────────────────

let connection: CrabCodeConnection | undefined;
let chatProvider: ChatPanelProvider | undefined;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  // 1. Read configuration
  const config = vscode.workspace.getConfiguration("crabcode");

  // 2. Create connection
  connection = new CrabCodeConnection(config);
  push(connection);

  // 3. Register ChatProvider as WebviewViewProvider
  chatProvider = new ChatPanelProvider(context.extensionUri, connection);
  push(
    vscode.window.registerWebviewViewProvider(
      ChatPanelProvider.viewType,
      chatProvider,
    ),
  );

  // 4. Register PermissionHandler and ChoiceHandler
  const permissionHandler = new PermissionHandler(connection);
  push(permissionHandler);

  const choiceHandler = new ChoiceHandler(connection);
  push(choiceHandler);

  // Wire server events to handlers
  push(
    connection.on("message", (payload: EventPayload) => {
      switch (payload.type) {
        case "permission_request":
          permissionHandler.handle(payload as PermissionRequestPayload);
          break;
        case "choice_request":
          choiceHandler.handle(payload as ChoiceRequestPayload);
          break;
      }
    }),
  );

  // 5. Register ContextProvider and FileChangeHandler
  push(new ContextProvider(connection));
  push(new FileChangeHandler(connection, context));

  // 6. Register all commands
  registerCommands(chatProvider, connection);

  // 7. Status bar item
  push(createStatusBar(connection));

  // 8. Auto-connect
  await autoConnect(connection, config);

  // Push remaining disposables into the extension context
  context.subscriptions.push(...disposables);
}

export function deactivate(): void {
  for (const d of disposables) {
    try {
      d.dispose();
    } catch {
      // Swallow errors during cleanup
    }
  }
  disposables.length = 0;
  connection = undefined;
  chatProvider = undefined;
}
