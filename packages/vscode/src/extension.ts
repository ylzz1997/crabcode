/**
 * CrabCode 编辑器扩展 — 入口。
 *
 * 连接 WebSocket、聊天面板、权限/选择处理、上下文推送、文件变更与状态栏等。
 */

import * as vscode from "vscode";

import { CrabCodeConnection } from "./connection";
import { ChatPanelProvider } from "./chatPanel";
import { ensureGateway, GatewayProcess } from "./gatewayManager";
import { PendingEditManager } from "./pendingEdits";

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
      `工具：${tool_name}`;

    const allowItem = "允许";
    const alwaysAllowItem = "始终允许";
    const denyItem = "拒绝";
    const denyFeedbackItem = "拒绝并反馈";

    vscode.window
      .showInformationMessage(
        `CrabCode：需要你的授权才能执行工具`,
        { modal: true, detail },
        allowItem,
        alwaysAllowItem,
        denyItem,
        denyFeedbackItem,
      )
      .then(async (choice) => {
        if (choice === denyFeedbackItem) {
          const feedback = await vscode.window.showInputBox({
            prompt: "告诉 AI 应该怎么做",
            placeHolder: "例如：不要修改这个文件，改用另一种方式…",
          });
          const cmd = buildPermissionResponseCommand(tool_use_id, false, {
            agentId: agent_id ?? undefined,
            feedback: feedback || undefined,
          });
          this.connection.sendRaw(serializeCommand(cmd));
          return;
        }
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
          { title: "CrabCode", canPickMany: true, placeHolder: question },
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
          { title: "CrabCode", placeHolder: question },
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
  private static readonly PUSH_DEBOUNCE_MS = 250;
  private activeEditor: vscode.TextEditor | undefined;
  private pushTimer: ReturnType<typeof setTimeout> | null = null;
  private lastContextSignature: string | null = null;

  public syncNow(): void {
    this.schedulePush(0);
  }

  constructor(private readonly connection: CrabCodeConnection) {
    this.activeEditor = vscode.window.activeTextEditor;
    this.schedulePush(0);

    push(
      vscode.window.onDidChangeActiveTextEditor((editor) => {
        this.activeEditor = editor;
        this.schedulePush(0);
      }),
    );
    push(
      vscode.window.onDidChangeTextEditorSelection((event) => {
        if (event.textEditor === this.activeEditor) {
          this.schedulePush();
        }
      }),
    );
    push(
      vscode.window.onDidChangeVisibleTextEditors(() => {
        this.schedulePush();
      }),
    );
    push(
      vscode.workspace.onDidChangeTextDocument((event) => {
        if (this.activeEditor && event.document === this.activeEditor.document) {
          this.schedulePush();
        }
      }),
    );
  }

  private schedulePush(delay = ContextProvider.PUSH_DEBOUNCE_MS): void {
    if (this.pushTimer) {
      clearTimeout(this.pushTimer);
    }
    this.pushTimer = setTimeout(() => {
      this.pushTimer = null;
      this.pushContext();
    }, delay);
  }

  private isSupportedDocument(document: vscode.TextDocument): boolean {
    return document.uri.scheme === "file"
      || document.uri.scheme === "untitled"
      || document.uri.scheme === "vscode-remote";
  }

  private getDocumentIdentity(document: vscode.TextDocument): string {
    return document.uri.fsPath || document.uri.toString();
  }

  private getOpenFiles(): string[] {
    const seen = new Set<string>();
    const openFiles: string[] = [];

    for (const editor of vscode.window.visibleTextEditors) {
      if (!this.isSupportedDocument(editor.document)) {
        continue;
      }
      const id = this.getDocumentIdentity(editor.document);
      if (!id || seen.has(id)) {
        continue;
      }
      seen.add(id);
      openFiles.push(id);
    }

    return openFiles;
  }

  private pushContext(): void {
    const editor = this.activeEditor;
    if (!editor || !this.connection.sessionId) {
      return;
    }

    const doc = editor.document;
    if (!this.isSupportedDocument(doc)) {
      return;
    }
    const selection = editor.selection;
    const context = {
      active_file: this.getDocumentIdentity(doc),
      selected_text: doc.getText(selection) || null,
      cursor_line: selection.active.line,
      cursor_column: selection.active.character,
      open_files: this.getOpenFiles(),
      language_id: doc.languageId,
    };
    const signature = JSON.stringify({
      session_id: this.connection.sessionId,
      ...context,
    });
    if (signature === this.lastContextSignature) {
      return;
    }
    this.lastContextSignature = signature;

    this.connection.pushContext(context);
  }

  dispose(): void {
    if (this.pushTimer) {
      clearTimeout(this.pushTimer);
      this.pushTimer = null;
    }
  }
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
        // 编辑器可能稍后检测到外部变更；此处仅作占位遍历
        break;
      }
    }

    const showDiff = this.config.get<boolean>("showDiffOnFileChange", false);

    if (showDiff && diff && (action === "modify" || action === "create")) {
      // Show a virtual diff document
      const actionZh =
        action === "create" ? "创建" : action === "modify" ? "修改" : action === "delete" ? "删除" : action;
      const name = `CrabCode：${path.split("/").pop()}（${actionZh}）`;
      const content = diff;
      showDiffDocument(name, content, path);
    } else {
      const actionZh =
        action === "create" ? "已创建" : action === "modify" ? "已修改" : action === "delete" ? "已删除" : action;
      vscode.window.setStatusBarMessage(`CrabCode：${actionZh} ${path.split("/").pop()}`, 3000);
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
    vscode.window.setStatusBarMessage(`CrabCode：${name}`, 4000);
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
  item.tooltip = "CrabCode：打开本扩展的设置";

  function update() {
    if (connection.connected) {
      item.text = `$(circle-filled) CrabCode${connection.modelName ? `：${connection.modelName}` : ""}`;
      item.backgroundColor = undefined;
    } else {
      item.text = "$(circle-slash) CrabCode：未连接";
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
        vscode.window.showWarningMessage("CrabCode：请先在编辑器里选中一段代码。");
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
        vscode.window.showWarningMessage("CrabCode：请先在编辑器里选中一段代码。");
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
        vscode.window.showWarningMessage("CrabCode：请先在编辑器里选中一段代码。");
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
        vscode.window.showWarningMessage("CrabCode：请先在编辑器里选中一段代码。");
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
        vscode.window.showWarningMessage("CrabCode：请先在编辑器里选中一段代码。");
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
        connection.resetReconnect();
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
      vscode.window.setStatusBarMessage("CrabCode：已创建新会话", 3000);
    }),
  );

  // Restart Gateway
  push(
    vscode.commands.registerCommand("crabcode.restartGateway", async () => {
      if (!gatewayProc || !outputChannel) return;

      gatewayProc.dispose();
      gatewayProc = new GatewayProcess();
      push(gatewayProc);

      const config = vscode.workspace.getConfiguration("crabcode");
      const result = await ensureGateway(config, outputChannel, gatewayProc);
      if (result.gatewayReady) {
        if (!connection.connected) {
          connection.connect();
        }
        vscode.window.setStatusBarMessage("CrabCode：网关已重启", 3000);
      }
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

  // Ensure the gateway is ready before attempting WebSocket connection
  if (gatewayProc && outputChannel) {
    const result = await ensureGateway(config, outputChannel, gatewayProc);
    if (!result.gatewayReady) {
      vscode.window.showWarningMessage(
        "CrabCode：网关未就绪，将自动重试连接。请检查输出面板。",
      );
    }
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
      "CrabCode：无法连接到网关，将自动重试。请检查配置项 crabcode.serverUrl。",
    );
  }
}

// ── activate / deactivate ──────────────────────────────────────────

let connection: CrabCodeConnection | undefined;
let chatProvider: ChatPanelProvider | undefined;
let gatewayProc: GatewayProcess | undefined;
let outputChannel: vscode.OutputChannel | undefined;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  // 1. Read configuration
  const config = vscode.workspace.getConfiguration("crabcode");

  // 2. Create output channel & gateway process manager
  outputChannel = vscode.window.createOutputChannel("CrabCode");
  push(outputChannel);

  gatewayProc = new GatewayProcess();
  push(gatewayProc);

  // 3. Create connection
  const activeConnection = new CrabCodeConnection(config, outputChannel);
  connection = activeConnection;
  push(activeConnection);

  // 4. Register ChatProvider as WebviewViewProvider
  const activeChatProvider = new ChatPanelProvider(context.extensionUri, activeConnection, outputChannel);
  chatProvider = activeChatProvider;
  push(
    vscode.window.registerWebviewViewProvider(
      ChatPanelProvider.viewType,
      activeChatProvider,
    ),
  );

  push(
    activeConnection.on("connected", () => {
      const cwd = vscode.workspace.workspaceFolders?.[0]?.uri?.fsPath ?? null;
      activeConnection.ensureSession(cwd);
      activeChatProvider.notifyConfigurationChanged();
    }),
  );

  push(
    activeConnection.on("disconnected", () => {
      activeChatProvider.notifyConfigurationChanged();
    }),
  );

  push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("crabcode")) {
        activeChatProvider.notifyConfigurationChanged();
      }
    }),
  );

  // 5. Register PermissionHandler and ChoiceHandler
  const permissionHandler = new PermissionHandler(activeConnection);
  push(permissionHandler);

  const choiceHandler = new ChoiceHandler(activeConnection);
  push(choiceHandler);

  // 6. Register ContextProvider and FileChangeHandler
  const contextProvider = new ContextProvider(activeConnection);
  push(contextProvider);
  push(new FileChangeHandler(activeConnection, context));
  push(new PendingEditManager(activeConnection, activeChatProvider));

  // Wire server events to handlers
  push(
    activeConnection.on("message", (payload: EventPayload) => {
      switch (payload.type) {
        case "permission_request":
          // Prefer in-panel interaction when the chat panel is visible.
          // Fallback to modal quick actions when the panel is hidden.
          if (!activeChatProvider.isVisible()) {
            permissionHandler.handle(payload as PermissionRequestPayload);
          }
          break;
        case "choice_request":
          // Avoid double responses: when panel UI is visible it owns choice handling.
          if (!activeChatProvider.isVisible()) {
            choiceHandler.handle(payload as ChoiceRequestPayload);
          }
          break;
        case "server.connected":
          if (activeConnection.sessionId) {
            void activeChatProvider.syncSessionPreferencesFromSettings();
            contextProvider.syncNow();
          }
          break;
      }
    }),
  );

  // 7. Register all commands
  registerCommands(activeChatProvider, activeConnection);

  // 8. Status bar item
  push(createStatusBar(activeConnection));

  // 9. Auto-connect
  await autoConnect(activeConnection, config);

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
  gatewayProc = undefined;
  outputChannel = undefined;
}
