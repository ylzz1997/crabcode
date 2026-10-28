/**
 * CrabCode 编辑器扩展 — 入口。
 *
 * 连接 WebSocket、聊天面板、权限/选择处理、上下文推送、文件变更与状态栏等。
 */

import { syncLanguage } from "./i18n";
import { t as tr } from "./i18n";
import * as vscode from "vscode";

import { CrabCodeConnection } from "./connection";
import { ComputerUseController } from "./computerUse";
import { ChatPanelProvider } from "./chatPanel";
import { CrabCodeSettingsPanel } from "./crabCodeSettingsPanel";
import { EXTENSION_SETTINGS_QUERY } from "./settings/catalog";
import { chooseAndInstallGatewaySuite, ensureGateway, GatewayProcess } from "./gatewayManager";
import { PendingEditManager } from "./pendingEdits";
import type { IdeContextSnapshot } from "./ideContext";

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
    const { tool_name, tool_use_id, reason, agent_id, request_kind } = payload;
    const isPeerMessage = request_kind === "peer_message";
    const peerName = String(payload.tool_input.from_name ?? "unknown");
    const peerText = String(payload.tool_input.text ?? "");

    const detail =
      (reason ? `${reason}\n\n` : "") +
      (isPeerMessage ? tr("来自：{0}\n\n{1}", { 0: peerName, 1: peerText }) : tr("工具：{0}", { 0: tool_name }));

    const allowItem = tr("允许");
    const alwaysAllowItem = tr("始终允许");
    const denyItem = tr("拒绝");
    const denyFeedbackItem = tr("拒绝并反馈");

    const choices = isPeerMessage
      ? [allowItem, alwaysAllowItem, denyItem]
      : [allowItem, alwaysAllowItem, denyItem, denyFeedbackItem];

    vscode.window
      .showInformationMessage(
        isPeerMessage
          ? tr("CrabCode：是否接收跨 Session 消息？")
          : tr("CrabCode：需要你的授权才能执行工具"),
        { modal: true, detail },
        ...choices,
      )
      .then(async (choice) => {
        if (choice === denyFeedbackItem) {
          const feedback = await vscode.window.showInputBox({
            prompt: tr("告诉 AI 应该怎么做"),
            placeHolder: tr("例如：不要修改这个文件，改用另一种方式…"),
          });
          const cmd = buildPermissionResponseCommand(tool_use_id, false, {
            agentId: agent_id ?? undefined,
            feedback: feedback || undefined,
            sessionId: payload.session_id,
          });
          this.connection.sendRaw(serializeCommand(cmd));
          return;
        }
        const allowed = choice === allowItem || choice === alwaysAllowItem;
        const alwaysAllow = choice === alwaysAllowItem;
        const cmd = buildPermissionResponseCommand(tool_use_id, allowed, {
          alwaysAllow,
          agentId: agent_id ?? undefined,
          sessionId: payload.session_id,
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
            sessionId: payload.session_id,
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
            sessionId: payload.session_id,
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

  constructor(
    private readonly connection: CrabCodeConnection,
    private readonly onContext: (context: IdeContextSnapshot | null) => void = () => {},
  ) {
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
    const sessionId = this.connection.sessionId;
    const doc = editor?.document;
    const context: IdeContextSnapshot | null = editor && doc && this.isSupportedDocument(doc)
      ? {
          active_file: this.getDocumentIdentity(doc),
          selected_text: doc.getText(editor.selection) || null,
          cursor_line: editor.selection.active.line,
          cursor_column: editor.selection.active.character,
          open_files: this.getOpenFiles(),
          language_id: doc.languageId,
        }
      : null;
    const signature = JSON.stringify({
      session_id: sessionId,
      context,
    });
    if (signature === this.lastContextSignature) {
      return;
    }
    this.lastContextSignature = signature;
    this.onContext(context);
    if (!sessionId) return;
    this.connection.pushContext(context ?? {
      active_file: null,
      selected_text: null,
      cursor_line: null,
      cursor_column: null,
      open_files: [],
      language_id: null,
    }, sessionId);
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
        action === "create" ? tr("创建") : action === "modify" ? tr("修改") : action === "delete" ? tr("删除") : action;
      const name = `CrabCode：${path.split(/[\\/]/).pop()}（${actionZh}）`;
      const content = diff;
      showDiffDocument(name, content, path);
    } else {
      const actionZh =
        action === "create" ? tr("已创建") : action === "modify" ? tr("已修改") : action === "delete" ? tr("已删除") : action;
      vscode.window.setStatusBarMessage(`CrabCode：${actionZh} ${path.split(/[\\/]/).pop()}`, 3000);
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

  item.command = "crabcode.openMenu";
  item.tooltip = tr("CrabCode：Computer Use、设置、日志");

  function update() {
    item.tooltip = tr("CrabCode：Computer Use、设置、日志");
    if (connection.connected) {
      item.text = `$(circle-filled) CrabCode${connection.modelName ? `：${connection.modelName}` : ""}`;
      item.backgroundColor = undefined;
    } else {
      item.text = `$(circle-slash) CrabCode：${tr("未连接")}`;
      item.backgroundColor = new vscode.ThemeColor(
        "statusBarItem.errorBackground",
      );
    }
  }

  update();
  push(vscode.workspace.onDidChangeConfiguration(e => { if (e.affectsConfiguration("crabcode.language")) update(); }));
  push(connection.on("connected", () => update()));
  push(connection.on("disconnected", () => update()));

  item.show();
  return item;
}

// ── Commands ────────────────────────────────────────────────────────

function registerCommands(
  chatProvider: ChatPanelProvider,
  connection: CrabCodeConnection,
  extensionVersion: string,
  extensionUri: vscode.Uri,
  computerUse: ComputerUseController,
): void {
  push(vscode.commands.registerCommand("crabcode.computerUse", () => computerUse.showMenu()));
  push(vscode.commands.registerCommand("crabcode.openMenu", async () => {
    const choice = await vscode.window.showQuickPick([
      { label: "$(device-desktop) Computer Use", command: "crabcode.computerUse" },
      { label: `$(settings-gear) ${tr("设置")}`, command: "crabcode.openSettingsMenu" },
      { label: `$(output) ${tr("日志")}`, command: "crabcode.showLogs" },
    ], { title: "CrabCode", placeHolder: tr("选择要打开的功能") });
    if (choice) await vscode.commands.executeCommand(choice.command);
  }));
  push(vscode.commands.registerCommand("crabcode.openSettingsMenu", () => chatProvider.showSettingsMenu()));
  push(vscode.commands.registerCommand("crabcode.showLogs", () => outputChannel?.show()));

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
        vscode.window.showWarningMessage(tr("CrabCode：请先在编辑器里选中一段代码。"));
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
        vscode.window.showWarningMessage(tr("CrabCode：请先在编辑器里选中一段代码。"));
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
        vscode.window.showWarningMessage(tr("CrabCode：请先在编辑器里选中一段代码。"));
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
        vscode.window.showWarningMessage(tr("CrabCode：请先在编辑器里选中一段代码。"));
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
        vscode.window.showWarningMessage(tr("CrabCode：请先在编辑器里选中一段代码。"));
      }
    }),
  );

  // Open Settings
  push(
    vscode.commands.registerCommand("crabcode.openContextSettings", () => {
      CrabCodeSettingsPanel.show(extensionUri, "context", () => chatProvider.notifyConfigurationChanged());
    }),
  );

  push(
    vscode.commands.registerCommand("crabcode.openSettings", () => {
      vscode.commands.executeCommand(
        "workbench.action.openSettings",
        EXTENSION_SETTINGS_QUERY,
      );
    }),
  );

  push(
    vscode.commands.registerCommand("crabcode.openPromptSettings", () => {
      CrabCodeSettingsPanel.show(extensionUri, "prompts", () => chatProvider.notifyConfigurationChanged());
    }),
  );

  push(vscode.commands.registerCommand("crabcode.openCrabCodeSettings", (section?: string) => {
    CrabCodeSettingsPanel.show(extensionUri, section === "models" ? "models" : "general", () => chatProvider.notifyConfigurationChanged());
  }));

  // Install optional local dependencies without changing lightweight auto-install.
  push(
    vscode.commands.registerCommand("crabcode.installGatewaySuite", async () => {
      if (!outputChannel) return;
      await chooseAndInstallGatewaySuite(
        vscode.workspace.getConfiguration("crabcode"),
        outputChannel,
        extensionVersion,
      );
    }),
  );

  // Connect
  push(
    vscode.commands.registerCommand("crabcode.connect", async () => {
      if (connection.connected || !gatewayProc || !outputChannel) return;
      const config = vscode.workspace.getConfiguration("crabcode");
      const result = await ensureGateway(
        config,
        outputChannel,
        gatewayProc,
        extensionVersion,
      );
      if (!result.gatewayReady) return;
      connection.resetReconnect();
      connection.connect();
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
      vscode.window.setStatusBarMessage(tr("CrabCode：已创建新会话"), 3000);
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
      const result = await ensureGateway(
        config,
        outputChannel,
        gatewayProc,
        extensionVersion,
      );
      if (result.gatewayReady) {
        if (!connection.connected) {
          connection.connect();
        }
        vscode.window.setStatusBarMessage(tr("CrabCode：网关已重启"), 3000);
      }
    }),
  );
}

// ── Auto-connect ────────────────────────────────────────────────────

async function autoConnect(
  connection: CrabCodeConnection,
  config: vscode.WorkspaceConfiguration,
  extensionVersion: string,
): Promise<void> {
  const autoConnectEnabled = config.get<boolean>("autoConnect", true);
  if (!autoConnectEnabled) {
    return;
  }

  // Ensure the gateway is ready before attempting WebSocket connection
  if (gatewayProc && outputChannel) {
    const result = await ensureGateway(
      config,
      outputChannel,
      gatewayProc,
      extensionVersion,
    );
    if (!result.gatewayReady) {
      return;
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
      tr("CrabCode：无法连接到网关，将自动重试。请检查配置项 crabcode.serverUrl。"),
    );
  }
}

// ── activate / deactivate ──────────────────────────────────────────

let connection: CrabCodeConnection | undefined;
let chatProvider: ChatPanelProvider | undefined;
let gatewayProc: GatewayProcess | undefined;
let outputChannel: vscode.OutputChannel | undefined;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  syncLanguage();
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
  const computerUse = push(new ComputerUseController(context, activeConnection, outputChannel));
  await computerUse.configure();

  // 4. Register ChatProvider as WebviewViewProvider
  const activeChatProvider = new ChatPanelProvider(context.extensionUri, activeConnection, outputChannel, context.workspaceState);
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
        syncLanguage();
        if (e.affectsConfiguration("crabcode.language")) activeChatProvider.refreshLanguage();
        activeConnection.refreshConfiguration();
        if (e.affectsConfiguration("crabcode.serverUrl") || e.affectsConfiguration("crabcode.password")) {
          void activeChatProvider.fetchAndSendSessions();
        }
        if (e.affectsConfiguration("crabcode.language") || e.affectsConfiguration("crabcode.computerUseEnabled") || e.affectsConfiguration("crabcode.serverUrl") || e.affectsConfiguration("crabcode.password")) {
          void computerUse.configure();
        }
        activeChatProvider.notifyConfigurationChanged();
        if (activeConnection.connected && activeConnection.sessionId) {
          void activeChatProvider.syncSessionPreferencesFromSettings();
        }
      }
    }),
  );

  push(vscode.workspace.onDidChangeWorkspaceFolders(() => {
    void activeChatProvider.fetchAndSendSessions();
  }));

  // 5. Register PermissionHandler and ChoiceHandler
  const permissionHandler = new PermissionHandler(activeConnection);
  push(permissionHandler);

  const choiceHandler = new ChoiceHandler(activeConnection);
  push(choiceHandler);

  // 6. Register ContextProvider and FileChangeHandler
  const contextProvider = new ContextProvider(
    activeConnection,
    (ideContext) => activeChatProvider.updateIdeContext(ideContext),
  );
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
  const extensionVersion = String(context.extension.packageJSON.version ?? "");
  registerCommands(activeChatProvider, activeConnection, extensionVersion, context.extensionUri, computerUse);

  // 8. Status bar item
  push(createStatusBar(activeConnection));

  // 9. Auto-connect
  await autoConnect(activeConnection, config, extensionVersion);

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
