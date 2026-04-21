/**
 * Webview-based chat panel that renders in the CrabCode sidebar.
 *
 * The panel is a `WebviewViewProvider` so it lives inside the
 * activity-bar sidebar defined in package.json (`crabcode.chatPanel`).
 * It communicates with the extension host via `postMessage` and
 * forwards chat traffic to / from the WebSocket connection.
 *
 * P2 features: tool use/result cards with collapsible bodies, diff
 * rendering, file-change notifications.
 */

import * as vscode from "vscode";
import type { CrabCodeConnection } from "./connection";
import type {
  EventPayload,
  ToolUsePayload,
  ToolResultPayload,
  FileChangePayload,
  ImageAttachment,
} from "./client/types";

interface GatewayModelInfo {
  name: string;
  description?: string;
}

// ── Chat message stored locally for rendering ─────────────────────

export type ChatMessageRole = "user" | "assistant" | "system";

export interface ChatMessage {
  id: string;
  role: ChatMessageRole;
  text: string;
  timestamp: number;
  images?: ImageAttachment[];
}

export interface ToolCard {
  id: string;          // tool_use_id
  toolName: string;
  input: Record<string, unknown>;
  result: string | null;
  isError: boolean;
  collapsed: boolean;
}

// ── Provider ──────────────────────────────────────────────────────

export class ChatPanelProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "crabcode.chatPanel";

  private view: vscode.WebviewView | undefined;
  private messages: ChatMessage[] = [];
  private toolCards = new Map<string, ToolCard>();

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly connection: CrabCodeConnection,
  ) {
    // Forward server events to the webview
    connection.on("message", (payload: EventPayload) => {
      this.handleServerEvent(payload);
    });
  }

  // ── WebviewViewProvider ────────────────────────────────────────

  public resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken,
  ): void {
    this.view = webviewView;

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this.extensionUri],
    };

    webviewView.webview.html = this.getHtmlForWebview(webviewView.webview);
    this.ensureSessionIfNeeded();

    webviewView.onDidChangeVisibility(() => {
      if (webviewView.visible) {
        this.ensureSessionIfNeeded();
      }
    });

    // Handle messages from the webview
    webviewView.webview.onDidReceiveMessage((msg: any) => {
      switch (msg.type) {
        case "sendMessage":
          this.handleUserMessage(msg.text, msg.images);
          break;
        case "requestHistory":
          this.postMessage({ type: "history", messages: this.messages });
          break;
        case "requestOptions":
          void this.pushChatOptions();
          break;
        case "setModel":
          if (typeof msg.name === "string" && msg.name.length > 0) {
            this.connection.sendSwitchModel(msg.name);
            void vscode.workspace
              .getConfiguration("crabcode")
              .update("chatModelDefault", msg.name, vscode.ConfigurationTarget.Global);
          }
          break;
        case "setPermissionMode":
          if (msg.mode === "default" || msg.mode === "run_everything") {
            void vscode.workspace
              .getConfiguration("crabcode")
              .update("permissionMode", msg.mode, vscode.ConfigurationTarget.Global);
            this.connection.sendSetPermissionMode(msg.mode);
          }
          break;
        case "pickFiles":
          void this.pickFilesForChat();
          break;
        case "screenshotHint":
          void vscode.window.showInformationMessage(
            "CrabCode：请用系统截图（如 macOS ⌘⇧4 / ⌘⇧5，Windows Win+Shift+S），再在本聊天输入框中粘贴即可。",
          );
          break;
        case "toggleToolCard":
          this.toggleToolCard(msg.id);
          break;
        case "openFile":
          this.openFile(msg.path, msg.line);
          break;
      }
    });

    this.pushChatOptions();
  }

  /** 将 CrabCode 扩展配置（模型列表、权限模式）推送到 Webview。 */
  public notifyConfigurationChanged(): void {
    void this.pushChatOptions();
  }

  private async resolveModelsFromSettingsOrGateway(): Promise<string[]> {
    const cfg = vscode.workspace.getConfiguration("crabcode");
    const configuredModels = cfg.get<string[]>("chatModels", []) ?? [];
    if (configuredModels.length > 0) {
      return configuredModels;
    }

    // Try fetching from gateway HTTP API when connected (sessionId or just connected)
    if (!this.connection.connected) {
      return [];
    }

    const wsUrl = cfg.get<string>("serverUrl", "ws://localhost:4096/ws");
    const password = cfg.get<string>("password", "");

    try {
      const url = new URL(wsUrl);
      url.protocol = url.protocol === "wss:" ? "https:" : "http:";
      url.pathname = "/config/models";
      url.search = "";

      const headers: Record<string, string> = {};
      if (password) {
        headers.Authorization = `Bearer ${password}`;
      }

      const response = await fetch(url.toString(), { headers });
      if (!response.ok) {
        return [];
      }

      const models = (await response.json()) as GatewayModelInfo[];
      return models.map((model) => model.name).filter((name) => name.length > 0);
    } catch {
      return [];
    }
  }

  /** After WebSocket connects, align server session with workspace settings. */
  public async syncSessionPreferencesFromSettings(): Promise<void> {
    if (!this.connection.connected) {
      return;
    }

    const cfg = vscode.workspace.getConfiguration("crabcode");
    const models = await this.resolveModelsFromSettingsOrGateway();
    const defaultModel = cfg.get<string>("chatModelDefault", "") ?? "";
    const permissionMode = cfg.get<string>("permissionMode", "default");
    const mode: "default" | "run_everything" =
      permissionMode === "run_everything" ? "run_everything" : "default";

    if (models.length > 0) {
      const pick =
        defaultModel && models.includes(defaultModel) ? defaultModel : models[0];
      this.connection.sendSwitchModel(pick);
    }
    this.connection.sendSetPermissionMode(mode);
    void this.pushChatOptions();
  }

  private async pushChatOptions(): Promise<void> {
    const cfg = vscode.workspace.getConfiguration("crabcode");
    const models = await this.resolveModelsFromSettingsOrGateway();
    const defaultModel = cfg.get<string>("chatModelDefault", "") ?? "";
    const permissionMode = cfg.get<string>("permissionMode", "default");
    const mode: "default" | "run_everything" =
      permissionMode === "run_everything" ? "run_everything" : "default";
    this.postMessage({
      type: "options",
      models,
      defaultModel,
      permissionMode: mode,
      connected: this.connection.connected,
    });
  }

  private async pickFilesForChat(): Promise<void> {
    const picked = await vscode.window.showOpenDialog({
      title: "CrabCode：选择要附加的文件",
      canSelectMany: true,
      openLabel: "添加",
      filters: {
        Images: ["png", "jpg", "jpeg", "gif", "webp"],
        "Text / code": [
          "txt",
          "md",
          "json",
          "py",
          "ts",
          "tsx",
          "js",
          "jsx",
          "mjs",
          "cjs",
          "css",
          "html",
          "yml",
          "yaml",
          "toml",
          "rs",
          "go",
          "java",
          "kt",
          "swift",
          "c",
          "h",
          "cpp",
          "hpp",
          "cs",
          "rb",
          "php",
          "sh",
          "vue",
          "svelte",
        ],
        "All files": ["*"],
      },
    });
    if (!picked?.length) {
      return;
    }

    const images: ImageAttachment[] = [];
    const textSnippets: { name: string; text: string }[] = [];
    const maxBytes = 20 * 1024 * 1024;
    const maxTextChars = 200_000;

    for (const uri of picked) {
      try {
        const stat = await vscode.workspace.fs.stat(uri);
        if (stat.size > maxBytes) {
          void vscode.window.showWarningMessage(`CrabCode：已跳过过大文件（>20MB）\n${uri.fsPath}`);
          continue;
        }
        const buf = await vscode.workspace.fs.readFile(uri);
        const base = uri.fsPath.split(/[/\\]/).pop() || "file";
        const ext = base.includes(".") ? base.split(".").pop()!.toLowerCase() : "";
        const imageExts: Record<string, string> = {
          png: "image/png",
          jpg: "image/jpeg",
          jpeg: "image/jpeg",
          gif: "image/gif",
          webp: "image/webp",
        };
        if (ext && imageExts[ext]) {
          images.push({
            media_type: imageExts[ext],
            data: Buffer.from(buf).toString("base64"),
          });
        } else {
          const decoder = new TextDecoder("utf-8", { fatal: false });
          let text = decoder.decode(buf);
          if (text.length > maxTextChars) {
            text = text.slice(0, maxTextChars) + "\n…(已截断)";
          }
          textSnippets.push({ name: base, text });
        }
      } catch {
        void vscode.window.showWarningMessage(`CrabCode：无法读取文件\n${uri.fsPath}`);
      }
    }

    if (images.length > 0 || textSnippets.length > 0) {
      this.postMessage({ type: "addAttachments", images, textSnippets });
    }
  }

  // ── Public API used by commands ────────────────────────────────

  /** Reveal the chat panel in the sidebar. */
  public reveal(): void {
    this.ensureSessionIfNeeded();
    if (this.view) {
      this.view.show?.(true);
    } else {
      vscode.commands.executeCommand("crabcode.chatPanel.focus");
    }
  }

  /** Send a pre-composed prompt (e.g. from context-menu commands). */
  public sendPrompt(text: string): void {
    this.addMessage("user", text);
    this.ensureSessionIfNeeded();
    this.connection.send(text);
    this.reveal();
  }

  /** Pre-fill the input box without sending. */
  public prefillInput(text: string): void {
    this.postMessage({ type: "prefill", text });
    this.reveal();
  }

  // ── Internals ──────────────────────────────────────────────────

  private handleUserMessage(text: string, images?: ImageAttachment[]): void {
    this.addMessage("user", text, images);
    this.ensureSessionIfNeeded();
    this.connection.send(text, { images });
  }

  private ensureSessionIfNeeded(): void {
    const cwd = vscode.workspace.workspaceFolders?.[0]?.uri?.fsPath ?? null;
    this.connection.ensureSession(cwd);
  }

  private handleServerEvent(payload: EventPayload): void {
    switch (payload.type) {
      case "stream_text":
        this.appendAssistantText(payload.text);
        break;
      case "thinking":
        this.appendAssistantText(`[CrabCode 思考] ${payload.text}`);
        break;
      case "tool_use":
        this.handleToolUse(payload as ToolUsePayload);
        break;
      case "tool_result":
        this.handleToolResult(payload as ToolResultPayload);
        break;
      case "file_change":
        this.handleFileChange(payload as FileChangePayload);
        break;
      case "error":
        this.addMessage("system", `CrabCode：${payload.message}`);
        break;
      case "turn_complete":
        break;
    }
  }

  private handleToolUse(payload: ToolUsePayload): void {
    const card: ToolCard = {
      id: payload.tool_use_id,
      toolName: payload.tool_name,
      input: payload.tool_input,
      result: null,
      isError: false,
      collapsed: false,
    };
    this.toolCards.set(payload.tool_use_id, card);
    this.postMessage({ type: "toolUse", card });
  }

  private handleToolResult(payload: ToolResultPayload): void {
    const card = this.toolCards.get(payload.tool_use_id);
    if (card) {
      card.result = payload.result_for_display ?? payload.result;
      card.isError = payload.is_error ?? false;
      card.collapsed = true; // Auto-collapse once result arrives
      this.postMessage({ type: "toolResult", card });
    }
  }

  private handleFileChange(payload: FileChangePayload): void {
    this.postMessage({ type: "fileChange", payload });
  }

  private toggleToolCard(id: string): void {
    const card = this.toolCards.get(id);
    if (card) {
      card.collapsed = !card.collapsed;
      this.postMessage({ type: "toggleToolCard", id, collapsed: card.collapsed });
    }
  }

  private async openFile(path: string, line?: number): Promise<void> {
    const uri = vscode.Uri.file(path);
    try {
      const doc = await vscode.workspace.openTextDocument(uri);
      const editor = await vscode.window.showTextDocument(doc, {
        preview: true,
        preserveFocus: true,
      });
      if (line !== undefined && line >= 0) {
        const pos = new vscode.Position(line, 0);
        editor.selection = new vscode.Selection(pos, pos);
        editor.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenter);
      }
    } catch {
      // File may not exist or be inaccessible
    }
  }

  private addMessage(role: ChatMessageRole, text: string, images?: ImageAttachment[]): void {
    const msg: ChatMessage = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      role,
      text,
      timestamp: Date.now(),
      images,
    };
    this.messages.push(msg);
    this.postMessage({ type: "newMessage", message: msg });
  }

  /** Append text to the last assistant message (streaming). */
  private appendAssistantText(chunk: string): void {
    const last = this.messages[this.messages.length - 1];
    if (last && last.role === "assistant") {
      last.text += chunk;
      this.postMessage({ type: "appendText", id: last.id, chunk });
    } else {
      this.addMessage("assistant", chunk);
    }
  }

  private postMessage(msg: any): void {
    this.view?.webview.postMessage(msg);
  }

  // ── HTML ───────────────────────────────────────────────────────

  private getHtmlForWebview(webview: vscode.Webview): string {
    const nonce = getNonce();

    return /*html*/ `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}'; img-src data:;" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>CrabCode Chat</title>
  <style nonce="${nonce}">
    :root {
      --font: var(--vscode-font-family);
      --radius: 10px;
      --radius-lg: 14px;
      --border: color-mix(in srgb, var(--vscode-widget-border, #444) 55%, transparent);
      --border-strong: color-mix(in srgb, var(--vscode-widget-border, #444) 80%, transparent);
      --surface: color-mix(in srgb, var(--vscode-editor-background) 60%, var(--vscode-sideBar-background));
      --surface-elevated: color-mix(in srgb, var(--vscode-input-background) 85%, var(--vscode-sideBar-background));
      --surface-soft: color-mix(in srgb, var(--vscode-sideBar-background) 65%, var(--vscode-editor-background));
      --accent: var(--vscode-focusBorder, #3794ff);
      --accent-muted: color-mix(in srgb, var(--accent) 14%, transparent);
      --text-muted: var(--vscode-descriptionForeground);
      --shadow-sm: 0 1px 3px rgba(0,0,0,0.08);
      --shadow-md: 0 4px 12px rgba(0,0,0,0.12);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: var(--font);
      font-size: var(--vscode-font-size);
      color: var(--vscode-foreground);
      background: var(--vscode-sideBar-background);
      display: flex;
      flex-direction: column;
      height: 100vh;
    }
    button, select, textarea { font: inherit; }
    button:focus-visible, select:focus-visible, textarea:focus-visible {
      outline: 1.5px solid var(--accent);
      outline-offset: 1px;
    }

    /* ── Messages ──────────────────────────────────────────────── */
    #messages {
      flex: 1;
      overflow-y: auto;
      padding: 12px 10px 16px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    #messages:empty::before {
      content: '开始和 CrabCode 聊天吧';
      display: block;
      margin: auto;
      padding: 16px 14px;
      border-radius: var(--radius-lg);
      border: 1px dashed var(--border);
      color: var(--text-muted);
      background: var(--surface);
      text-align: center;
      font-size: 12px;
    }

    /* ── Message bubbles ───────────────────────────────────────── */
    .msg {
      position: relative;
      padding: 8px 10px;
      border-radius: var(--radius-lg);
      border: 1px solid var(--border);
      background: var(--surface);
      box-shadow: var(--shadow-sm);
    }
    .msg.user {
      align-self: flex-end;
      width: min(94%, 520px);
      background: color-mix(in srgb, var(--accent) 10%, var(--surface-elevated));
      border-color: color-mix(in srgb, var(--accent) 22%, var(--border));
      border-top-right-radius: 4px;
    }
    .msg.assistant {
      align-self: stretch;
      background: color-mix(in srgb, var(--surface) 80%, var(--vscode-editor-background));
      border-top-left-radius: 4px;
    }
    .msg.system {
      align-self: stretch;
      background: color-mix(in srgb, var(--vscode-editorWarning-background, #553300) 60%, var(--surface));
      border-color: color-mix(in srgb, var(--vscode-editorWarning-foreground, #ffcc66) 24%, var(--border));
      opacity: 0.96;
    }
    .msg .role {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      margin-bottom: 4px;
      font-weight: 600;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.03em;
      color: var(--text-muted);
    }
    .msg .text {
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.55;
      font-size: 13px;
    }

    /* ── Tool cards ────────────────────────────────────────────── */
    .tool-card {
      margin: 4px 0 0;
      border-radius: var(--radius);
      border: 1px solid var(--border);
      background: var(--surface-soft);
      overflow: hidden;
      font-size: 12px;
      box-shadow: var(--shadow-sm);
    }
    .tool-card-header {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      cursor: pointer;
      background: transparent;
      transition: background 0.12s;
    }
    .tool-card-header:hover {
      background: var(--accent-muted);
    }
    .tool-card-header .icon {
      width: 18px;
      height: 18px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 6px;
      background: var(--accent-muted);
      font-size: 10px;
      opacity: 0.85;
    }
    .tool-card-header .tool-name { font-weight: 600; flex: 1; font-size: 11.5px; }
    .tool-card-header .chevron { opacity: 0.45; transition: transform 0.15s; font-size: 11px; }
    .tool-card-header .chevron.collapsed { transform: rotate(-90deg); }
    .tool-card-header .status {
      font-size: 10.5px;
      opacity: 0.85;
      padding: 1px 7px;
      border-radius: 6px;
      background: color-mix(in srgb, var(--vscode-badge-background, #444) 45%, transparent);
    }
    .tool-card-header .status.error {
      color: var(--vscode-errorForeground, #f48771);
      background: color-mix(in srgb, var(--vscode-errorForeground, #f48771) 10%, transparent);
    }
    .tool-card-header .status.ok {
      color: var(--vscode-terminal-ansiGreen, #89d185);
      background: color-mix(in srgb, var(--vscode-terminal-ansiGreen, #89d185) 10%, transparent);
    }
    .tool-card-body {
      padding: 8px 10px;
      max-height: 200px;
      overflow-y: auto;
      background: color-mix(in srgb, var(--vscode-editor-background) 85%, var(--surface-soft));
      border-top: 1px solid var(--border);
    }
    .tool-card-body.hidden { display: none; }
    .tool-card-body pre {
      white-space: pre-wrap;
      word-break: break-word;
      font-family: var(--vscode-editor-font-family, monospace);
      font-size: 11px;
      line-height: 1.4;
    }

    /* ── Diff colours ──────────────────────────────────────────── */
    .diff-line-add { color: var(--vscode-terminal-ansiGreen, #89d185); }
    .diff-line-del { color: var(--vscode-terminal-ansiRed, #f48771); }
    .diff-line-ctx { color: var(--vscode-descriptionForeground, #888); }

    /* ── File change pills ─────────────────────────────────────── */
    .file-change {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 8px;
      margin: 2px 0;
      border-radius: 6px;
      font-size: 11px;
      border: 1px solid var(--border);
      background: var(--surface-elevated);
      align-self: flex-start;
    }
    .file-change .action {
      font-weight: 600;
      text-transform: uppercase;
      font-size: 9.5px;
      letter-spacing: 0.04em;
    }
    .file-change .action.create { color: var(--vscode-terminal-ansiGreen, #89d185); }
    .file-change .action.modify { color: var(--vscode-terminal-ansiYellow, #cca700); }
    .file-change .action.delete { color: var(--vscode-terminal-ansiRed, #f48771); }
    .file-change .path {
      cursor: pointer;
      color: var(--vscode-textLink-foreground, var(--vscode-foreground));
      text-decoration: none;
    }
    .file-change .path:hover { text-decoration: underline; }

    /* ── Composer ──────────────────────────────────────────────── */
    #composer-wrap {
      flex-shrink: 0;
      padding: 6px 8px 8px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      background: var(--vscode-sideBar-background);
      border-top: 1px solid var(--border);
    }
    #composer-wrap.drag-hover #composer-card {
      outline: 1.5px dashed var(--accent);
      outline-offset: 2px;
    }
    #composer-card {
      border-radius: var(--radius-lg);
      border: 1px solid var(--border-strong);
      background: var(--surface-elevated);
      box-shadow: var(--shadow-md);
      overflow: hidden;
      display: flex;
      flex-direction: column;
      transition: border-color 0.15s, box-shadow 0.15s;
    }
    #composer-card:focus-within {
      border-color: color-mix(in srgb, var(--accent) 40%, var(--border-strong));
      box-shadow: var(--shadow-md), 0 0 0 2px color-mix(in srgb, var(--accent) 12%, transparent);
    }

    /* ── Composer meta bar ─────────────────────────────────────── */
    .composer-meta {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      border-bottom: 1px solid color-mix(in srgb, var(--border) 65%, transparent);
      background: color-mix(in srgb, var(--vscode-editor-background) 20%, transparent);
      min-height: 32px;
    }
    .ctx-toggle {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      border: none;
      background: transparent;
      color: var(--vscode-foreground);
      font-size: 11.5px;
      cursor: pointer;
      padding: 3px 6px;
      border-radius: 6px;
      opacity: 0.9;
    }
    .ctx-toggle:hover { background: var(--accent-muted); }
    .ctx-chevron {
      display: inline-block;
      font-size: 12px;
      width: 12px;
      text-align: center;
      color: var(--text-muted);
      transition: transform 0.18s ease;
      transform: rotate(0deg);
    }
    #composer-card.ctx-open .ctx-chevron { transform: rotate(90deg); }
    #ctx-summary { color: var(--text-muted); font-weight: 500; font-size: 11.5px; }
    .meta-spacer { flex: 1; }
    .meta-link {
      border: none;
      background: transparent;
      color: var(--vscode-textLink-foreground, var(--vscode-foreground));
      font-size: 11.5px;
      cursor: pointer;
      padding: 3px 7px;
      border-radius: 6px;
      opacity: 0.8;
    }
    .meta-link:hover { background: var(--accent-muted); }
    #ctx-attachments {
      padding: 6px 8px 3px;
      max-height: 100px;
      overflow-y: auto;
      border-bottom: 1px solid color-mix(in srgb, var(--border) 60%, transparent);
    }
    #composer-card:not(.ctx-open) #ctx-attachments { display: none; }
    #composer-card:not(.has-attachments) #ctx-attachments { display: none; border-bottom: none; }
    #composer-card:not(.has-attachments) .ctx-chevron { display: none; }
    #composer-card:not(.has-attachments) .composer-meta { display: none; }
    #composer-tip {
      font-size: 10.5px;
      line-height: 1.4;
      color: var(--text-muted);
      padding: 4px 10px 1px;
      max-height: 0;
      overflow: hidden;
      opacity: 0;
      transition: max-height 0.2s ease, opacity 0.2s ease, padding 0.2s ease;
    }
    #composer-tip kbd {
      font-family: var(--vscode-editor-font-family, monospace);
      font-size: 10px;
      padding: 0px 4px;
      border-radius: 4px;
      border: 1px solid var(--border);
      background: color-mix(in srgb, var(--vscode-input-background) 75%, transparent);
    }
    #composer-card.tip-visible #composer-tip {
      max-height: 48px;
      opacity: 1;
      padding-top: 6px;
    }
    #attachment-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: flex-start;
    }
    .attachment-thumb {
      position: relative;
      width: 48px;
      height: 48px;
      border-radius: 8px;
      border: 1px solid var(--border);
      overflow: hidden;
    }
    .attachment-thumb img { width: 100%; height: 100%; object-fit: cover; }
    .attachment-thumb .remove-btn {
      position: absolute;
      top: 2px;
      right: 2px;
      width: 16px;
      height: 16px;
      background: rgba(0,0,0,0.55);
      color: #fff;
      border: none;
      border-radius: 50%;
      font-size: 10px;
      line-height: 16px;
      text-align: center;
      cursor: pointer;
      padding: 0;
    }
    .attachment-thumb .remove-btn:hover { background: rgba(180,40,40,0.9); }
    .text-file-chip {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      max-width: 200px;
      padding: 3px 8px;
      border-radius: 6px;
      font-size: 10.5px;
      background: color-mix(in srgb, var(--vscode-input-background) 60%, var(--vscode-badge-background, #333));
      border: 1px solid var(--border);
    }
    .text-file-chip .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .text-file-chip .remove-btn {
      background: transparent;
      border: none;
      color: var(--vscode-foreground);
      cursor: pointer;
      padding: 0 1px;
      opacity: 0.65;
      font-size: 11px;
    }
    .text-file-chip .remove-btn:hover { opacity: 1; }

    /* ── Textarea ──────────────────────────────────────────────── */
    #input {
      display: block;
      width: 100%;
      min-height: 72px;
      max-height: 200px;
      resize: vertical;
      border: none;
      outline: none;
      background: transparent;
      color: var(--vscode-input-foreground);
      padding: 10px 12px 8px;
      font-size: 13px;
      line-height: 1.5;
    }
    #input::placeholder { color: color-mix(in srgb, var(--vscode-input-foreground) 35%, transparent); }

    /* ── Toolbar ───────────────────────────────────────────────── */
    .composer-toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
      padding: 6px 8px;
      border-top: 1px solid color-mix(in srgb, var(--border) 55%, transparent);
    }
    .toolbar-left {
      display: flex;
      align-items: center;
      gap: 6px;
      flex: 1;
      min-width: 0;
    }
    .tb-left-wrap { position: relative; flex-shrink: 0; }
    .tb-icon-btn {
      width: 28px;
      height: 28px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: transparent;
      color: var(--vscode-foreground);
      cursor: pointer;
      font-size: 15px;
      line-height: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: background 0.12s, border-color 0.12s;
      opacity: 0.75;
    }
    .tb-icon-btn:hover {
      background: var(--accent-muted);
      border-color: color-mix(in srgb, var(--accent) 30%, var(--border));
      opacity: 1;
    }
    .plus-menu {
      position: absolute;
      bottom: calc(100% + 6px);
      left: 0;
      min-width: 160px;
      background: var(--vscode-menu-background);
      color: var(--vscode-menu-foreground);
      border: 1px solid var(--vscode-menu-border, #444);
      border-radius: 10px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.35);
      z-index: 30;
      padding: 3px 0;
    }
    .plus-menu.hidden { display: none; }
    .plus-menu button {
      display: block;
      width: 100%;
      text-align: left;
      padding: 7px 12px;
      border: none;
      background: transparent;
      color: inherit;
      cursor: pointer;
      font-size: 12px;
    }
    .plus-menu button:hover { background: var(--vscode-menu-selectionBackground, rgba(127,127,127,0.18)); }
    .model-pill-wrap { flex: 1; min-width: 0; max-width: 100%; }
    .tb-model {
      width: 100%;
      max-width: 100%;
      padding: 4px 10px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: color-mix(in srgb, var(--vscode-input-background) 50%, transparent);
      color: var(--vscode-foreground);
      font-size: 11.5px;
      cursor: pointer;
    }
    .tb-model:hover {
      border-color: color-mix(in srgb, var(--accent) 40%, var(--border));
      background: color-mix(in srgb, var(--vscode-input-background) 70%, transparent);
    }
    .tb-model:disabled { opacity: 0.5; cursor: not-allowed; }
    .tb-send-circle {
      flex-shrink: 0;
      width: 30px;
      height: 30px;
      border-radius: 8px;
      border: none;
      background: var(--accent);
      color: white;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: filter 0.12s, transform 0.12s;
    }
    .tb-send-circle:hover {
      filter: brightness(1.1);
      transform: translateY(-0.5px);
    }
    .tb-send-circle:active { transform: scale(0.95); }
    .tb-send-circle svg { display: block; }

    /* ── Footer ────────────────────────────────────────────────── */
    #footer-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
      padding: 0 3px 1px;
      font-size: 10.5px;
    }
    .footer-left.muted {
      color: var(--text-muted);
      letter-spacing: 0.02em;
      font-weight: 500;
    }
    .footer-select {
      flex: 0 1 58%;
      max-width: 180px;
      padding: 3px 8px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: color-mix(in srgb, var(--vscode-input-background) 45%, transparent);
      color: var(--vscode-foreground);
      font-size: 10.5px;
    }

    /* ── Images in messages ────────────────────────────────────── */
    .msg-images {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      margin-top: 6px;
    }
    .msg-images img {
      max-width: 180px;
      max-height: 130px;
      border-radius: 8px;
      border: 1px solid var(--border);
      cursor: pointer;
    }
    .msg-images img:hover { opacity: 0.88; }

    /* ── Scrollbar (subtle) ────────────────────────────────────── */
    ::-webkit-scrollbar { width: 5px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: color-mix(in srgb, var(--vscode-foreground) 15%, transparent); border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: color-mix(in srgb, var(--vscode-foreground) 28%, transparent); }
  </style>
</head>
<body>
  <div id="messages"></div>
  <div id="composer-wrap">
    <div id="composer-card">
      <div class="composer-meta">
        <button type="button" class="ctx-toggle" id="ctx-toggle" aria-expanded="false" title="展开或折叠附件">
          <span class="ctx-chevron">▸</span>
          <span id="ctx-summary"></span>
        </button>
        <span class="meta-spacer"></span>
      </div>
      <div id="ctx-attachments">
        <div id="attachment-bar"></div>
      </div>
      <textarea id="input" rows="3" placeholder="输入问题或命令（如 /help）…"></textarea>
      <div id="input-toolbar" class="composer-toolbar">
        <div class="toolbar-left">
          <div class="tb-left-wrap">
            <button type="button" class="tb-icon-btn" id="plus-btn" title="添加文件或图片">+</button>
            <div id="plus-menu" class="plus-menu hidden">
              <button type="button" data-action="image">添加图片…</button>
              <button type="button" data-action="file">添加文件…</button>
              <button type="button" data-action="screenshot">屏幕截图说明</button>
            </div>
          </div>
          <div class="model-pill-wrap">
            <select id="model-select" class="tb-model" title="模型"></select>
          </div>
        </div>
        <button type="button" class="tb-send-circle" id="send-btn" title="发送 (⌘↵ / Ctrl+Enter)" aria-label="发送">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>
        </button>
      </div>
    </div>
    <div id="footer-bar">
      <span class="footer-left muted">CrabCode</span>
      <select id="permission-select" class="footer-select" title="权限">
        <option value="default">默认</option>
        <option value="run_everything">run_everything</option>
      </select>
    </div>
    <input type="file" id="file-input-image" accept="image/*" multiple hidden />
  </div>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    const msgContainer = document.getElementById('messages');
    const input = document.getElementById('input');
    const sendBtn = document.getElementById('send-btn');
    const attachmentBar = document.getElementById('attachment-bar');
    const composerWrap = document.getElementById('composer-wrap');
    const composerCard = document.getElementById('composer-card');
    const ctxToggle = document.getElementById('ctx-toggle');
    const plusBtn = document.getElementById('plus-btn');
    const plusMenu = document.getElementById('plus-menu');
    const fileInputImage = document.getElementById('file-input-image');
    const modelSelect = document.getElementById('model-select');
    const permissionSelect = document.getElementById('permission-select');

    // ── Tool card state ──────────────────────────────────────────
    const toolCards = new Map();

    // pendingImages: { media_type, data, dataUrl }; pendingTextFiles: { name, text }
    const pendingImages = [];
    const pendingTextFiles = [];
    const MAX_IMAGE_SIZE = 20 * 1024 * 1024;
    const MAX_TEXT_FILE = 20 * 1024 * 1024;

    function addMessageEl(msg) {
      const div = document.createElement('div');
      div.className = 'msg ' + msg.role;
      div.id = 'msg-' + msg.id;
      let html = '<div class="role">' + roleLabel(msg.role) + '</div><div class="text">' + escapeHtml(msg.text) + '</div>';
      // Render images in user messages
      if (msg.images && msg.images.length > 0) {
        html += '<div class="msg-images">';
        for (const img of msg.images) {
          const src = 'data:' + escapeAttr(img.media_type) + ';base64,' + img.data;
          html += '<img src="' + src + '" alt="attachment" loading="lazy" />';
        }
        html += '</div>';
      }
      div.innerHTML = html;
      msgContainer.appendChild(div);
      msgContainer.scrollTop = msgContainer.scrollHeight;
    }

    function renderToolCard(card) {
      const existing = document.getElementById('tool-' + card.id);
      if (existing) { updateToolCard(existing, card); return; }

      const el = document.createElement('div');
      el.className = 'tool-card';
      el.id = 'tool-' + card.id;
      el.innerHTML = buildToolCardHtml(card);
      msgContainer.appendChild(el);
      msgContainer.scrollTop = msgContainer.scrollHeight;

      el.querySelector('.tool-card-header').addEventListener('click', () => {
        vscode.postMessage({ type: 'toggleToolCard', id: card.id });
      });

      el.querySelector('.path')?.addEventListener('click', (e) => {
        const path = e.target.dataset.path;
        if (path) vscode.postMessage({ type: 'openFile', path });
      });

      toolCards.set(card.id, card);
    }

    function updateToolCard(el, card) {
      el.innerHTML = buildToolCardHtml(card);
      el.querySelector('.tool-card-header').addEventListener('click', () => {
        vscode.postMessage({ type: 'toggleToolCard', id: card.id });
      });
      el.querySelector('.path')?.addEventListener('click', (e) => {
        const path = e.target.dataset.path;
        if (path) vscode.postMessage({ type: 'openFile', path });
      });
      toolCards.set(card.id, card);
    }

    function buildToolCardHtml(card) {
      const chevron = card.collapsed ? 'chevron collapsed' : 'chevron';
      let statusHtml = '';
      if (card.result !== null) {
        statusHtml = card.isError
          ? '<span class="status error">失败</span>'
          : '<span class="status ok">完成</span>';
      } else {
        statusHtml = '<span class="status">运行中</span>';
      }

      const inputStr = formatToolInput(card.toolName, card.input);
      let bodyHtml = '';
      if (!card.collapsed) {
        if (card.result !== null) {
          bodyHtml = '<div class="tool-card-body"><pre>' + renderResult(card.result, card.toolName) + '</pre></div>';
        } else {
          bodyHtml = '<div class="tool-card-body"><pre>' + escapeHtml(inputStr) + '</pre></div>';
        }
      } else if (card.result !== null) {
        const preview = card.result.split('\\n')[0].substring(0, 90);
        statusHtml += '<span style="opacity:0.56; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:34%;">' + escapeHtml(preview) + (card.result.length > 90 ? '…' : '') + '</span>';
      }

      return '<div class="tool-card-header">' +
        '<span class="icon">&#9881;</span>' +
        '<span class="tool-name">' + escapeHtml(card.toolName) + '</span>' +
        statusHtml +
        '<span class="' + chevron + '">▾</span>' +
        '</div>' + bodyHtml;
    }

    function formatToolInput(toolName, input) {
      // Show concise input for common tools
      if (input.file_path || input.path) {
        const p = input.file_path || input.path;
        const rest = Object.entries(input).filter(([k]) => k !== 'file_path' && k !== 'path')
          .map(([k,v]) => k + ': ' + (typeof v === 'string' ? v.substring(0,80) : JSON.stringify(v)))
          .join(', ');
        return p + (rest ? '\\n' + rest : '');
      }
      return JSON.stringify(input, null, 2);
    }

    function renderResult(text, toolName) {
      // If it looks like a diff, colorize lines
      if (text.startsWith('---') || text.startsWith('diff --git') || text.includes('\\n+++')) {
        return text.split('\\n').map(line => {
          if (line.startsWith('+++') || line.startsWith('+')) return '<span class="diff-line-add">' + escapeHtml(line) + '</span>';
          if (line.startsWith('---') || line.startsWith('-')) return '<span class="diff-line-del">' + escapeHtml(line) + '</span>';
          if (line.startsWith('@@')) return '<span class="diff-line-ctx">' + escapeHtml(line) + '</span>';
          return escapeHtml(line);
        }).join('\\n');
      }
      return escapeHtml(text);
    }

    function addFileChangePill(payload) {
      const div = document.createElement('div');
      div.className = 'file-change';
      const actionClass = payload.action; // create | modify | delete
      const shortPath = payload.path.split('/').pop() || payload.path;
      div.innerHTML =
        '<span class="action ' + actionClass + '">' + escapeHtml(payload.action) + '</span>' +
        '<span class="path" data-path="' + escapeHtml(payload.path) + '" title="' + escapeHtml(payload.path) + '">' + escapeHtml(shortPath) + '</span>';
      msgContainer.appendChild(div);
      msgContainer.scrollTop = msgContainer.scrollHeight;

      div.querySelector('.path').addEventListener('click', () => {
        vscode.postMessage({ type: 'openFile', path: payload.path });
      });
    }

    function roleLabel(role) {
      if (role === 'user') return '你';
      if (role === 'assistant') return 'CrabCode';
      if (role === 'system') return '系统';
      return role;
    }
    function escapeHtml(t) {
      if (t == null) return '';
      return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }
    function escapeAttr(t) {
      if (t == null) return '';
      return String(t).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    // ── Attachments (images + text files) ───────────────────────

    function guessImageMime(name, mime) {
      if (mime && mime.startsWith('image/')) return mime;
      const ext = (name.split('.').pop() || '').toLowerCase();
      const map = { png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp' };
      return map[ext] || '';
    }

    function addImageFile(file) {
      const mime = file.type || guessImageMime(file.name, '');
      if (!mime.startsWith('image/')) return;
      if (file.size > MAX_IMAGE_SIZE) {
        alert('CrabCode：图片过大（最大 20MB）\\n' + file.name);
        return;
      }
      const reader = new FileReader();
      reader.onload = function(e) {
        const dataUrl = e.target.result;
        const base64 = dataUrl.split(',')[1];
        pendingImages.push({ media_type: mime, data: base64, dataUrl: dataUrl });
        renderAttachmentBar();
      };
      reader.readAsDataURL(file);
    }

    function addTextFile(file) {
      if (file.size > MAX_TEXT_FILE) {
        alert('CrabCode：文件过大（最大 20MB）\\n' + file.name);
        return;
      }
      const reader = new FileReader();
      reader.onload = function() {
        let t = reader.result || '';
        if (t.length > 200000) t = t.slice(0, 200000) + '\\n…(已截断)';
        pendingTextFiles.push({ name: file.name, text: t });
        renderAttachmentBar();
      };
      reader.readAsText(file);
    }

    function addDroppedOrPickedFile(file) {
      const mime = file.type || guessImageMime(file.name, '');
      if (mime.startsWith('image/')) addImageFile(file);
      else addTextFile(file);
    }

    function removeImage(index) {
      pendingImages.splice(index, 1);
      renderAttachmentBar();
    }

    function removeTextFile(index) {
      pendingTextFiles.splice(index, 1);
      renderAttachmentBar();
    }

    function renderAttachmentBar() {
      attachmentBar.innerHTML = '';
      pendingImages.forEach(function(img, idx) {
        const thumb = document.createElement('div');
        thumb.className = 'attachment-thumb';
        thumb.innerHTML = '<img src="' + escapeAttr(img.dataUrl) + '" alt="" />' +
          '<button type="button" class="remove-btn" data-kind="img" data-idx="' + idx + '" title="移除">✕</button>';
        attachmentBar.appendChild(thumb);
      });
      pendingTextFiles.forEach(function(f, idx) {
        const chip = document.createElement('div');
        chip.className = 'text-file-chip';
        chip.innerHTML = '<span class="name" title="' + escapeAttr(f.name) + '">' + escapeHtml(f.name) + '</span>' +
          '<button type="button" class="remove-btn" data-kind="txt" data-idx="' + idx + '" title="移除">✕</button>';
        attachmentBar.appendChild(chip);
      });
      attachmentBar.querySelectorAll('.remove-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
          const k = btn.getAttribute('data-kind');
          const i = parseInt(btn.getAttribute('data-idx'), 10);
          if (k === 'img') removeImage(i);
          else removeTextFile(i);
        });
      });
      syncComposerChrome();
    }

    let prevAttachCount = 0;
    function syncComposerChrome() {
      const n = pendingImages.length + pendingTextFiles.length;
      composerCard.classList.toggle('has-attachments', n > 0);
      const sum = document.getElementById('ctx-summary');
      if (sum) sum.textContent = n ? (n + ' 个附件') : '';
      if (n > 0 && prevAttachCount === 0) composerCard.classList.add('ctx-open');
      ctxToggle.setAttribute('aria-expanded', composerCard.classList.contains('ctx-open') ? 'true' : 'false');
      prevAttachCount = n;
    }

    function mergeHostAttachments(msg) {
      (msg.images || []).forEach(function(img) {
        const url = 'data:' + img.media_type + ';base64,' + img.data;
        pendingImages.push({ media_type: img.media_type, data: img.data, dataUrl: url });
      });
      (msg.textSnippets || []).forEach(function(s) {
        pendingTextFiles.push({ name: s.name, text: s.text });
      });
      renderAttachmentBar();
    }

    function applyOptions(msg) {
      const models = msg.models || [];
      modelSelect.innerHTML = '';
      if (models.length === 0) {
        const o = document.createElement('option');
        o.value = '';
        o.textContent = msg.connected ? '（网关未返回可用模型）' : '（正在连接网关…）';
        o.disabled = true;
        modelSelect.appendChild(o);
      } else {
        models.forEach(function(m) {
          const o = document.createElement('option');
          o.value = m;
          o.textContent = m;
          modelSelect.appendChild(o);
        });
        const pick = msg.defaultModel && models.indexOf(msg.defaultModel) >= 0 ? msg.defaultModel : models[0];
        modelSelect.value = pick;
      }
      permissionSelect.value = msg.permissionMode === 'run_everything' ? 'run_everything' : 'default';
    }

    // ── Send ─────────────────────────────────────────────────────

    function send() {
      let text = input.value.trim();
      let extra = '';
      const bt = String.fromCharCode(96);
      pendingTextFiles.forEach(function(f) {
        extra += '\\n\\n[附加文件: ' + f.name + ']\\n' + bt + bt + bt + '\\n' + f.text + '\\n' + bt + bt + bt + '\\n';
      });
      text = (text + extra).trim();
      if (!text && pendingImages.length === 0 && pendingTextFiles.length === 0) return;
      const images = pendingImages.map(function(img) {
        return { media_type: img.media_type, data: img.data };
      });
      vscode.postMessage({ type: 'sendMessage', text: text, images: images.length > 0 ? images : undefined });
      input.value = '';
      pendingImages.length = 0;
      pendingTextFiles.length = 0;
      renderAttachmentBar();
    }

    sendBtn.addEventListener('click', send);
    input.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
    });

    // ── Plus menu & file inputs ─────────────────────────────────

    function closePlusMenu() { plusMenu.classList.add('hidden'); }
    plusBtn.addEventListener('click', function(e) {
      e.stopPropagation();
      plusMenu.classList.toggle('hidden');
    });
    document.addEventListener('click', function() { closePlusMenu(); });
    plusMenu.addEventListener('click', function(e) { e.stopPropagation(); });
    plusMenu.querySelectorAll('button[data-action]').forEach(function(btn) {
      btn.addEventListener('click', function() {
        const act = btn.getAttribute('data-action');
        closePlusMenu();
        if (act === 'image') fileInputImage.click();
        else if (act === 'file') vscode.postMessage({ type: 'pickFiles' });
        else if (act === 'screenshot') vscode.postMessage({ type: 'screenshotHint' });
      });
    });
    fileInputImage.addEventListener('change', function() {
      if (fileInputImage.files) Array.from(fileInputImage.files).forEach(addImageFile);
      fileInputImage.value = '';
    });

    modelSelect.addEventListener('change', function() {
      if (modelSelect.value) vscode.postMessage({ type: 'setModel', name: modelSelect.value });
    });
    permissionSelect.addEventListener('change', function() {
      const m = permissionSelect.value === 'run_everything' ? 'run_everything' : 'default';
      vscode.postMessage({ type: 'setPermissionMode', mode: m });
    });

    ctxToggle.addEventListener('click', function() {
      const n = pendingImages.length + pendingTextFiles.length;
      if (n === 0) {
        return;
      }
      composerCard.classList.toggle('ctx-open');
      ctxToggle.setAttribute('aria-expanded', composerCard.classList.contains('ctx-open') ? 'true' : 'false');
    });

    // ── Drag & drop on composer ─────────────────────────────────

    ;['dragenter', 'dragover'].forEach(function(ev) {
      composerWrap.addEventListener(ev, function(e) {
        e.preventDefault();
        e.stopPropagation();
        composerWrap.classList.add('drag-hover');
      });
    });
    composerWrap.addEventListener('dragleave', function(e) {
      e.preventDefault();
      const rel = e.relatedTarget;
      if (!rel || !composerWrap.contains(rel)) composerWrap.classList.remove('drag-hover');
    });
    composerWrap.addEventListener('drop', function(e) {
      e.preventDefault();
      e.stopPropagation();
      composerWrap.classList.remove('drag-hover');
      const files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) Array.from(files).forEach(addDroppedOrPickedFile);
    });

    // ── Paste: images + files ───────────────────────────────────

    input.addEventListener('paste', function(e) {
      const items = e.clipboardData && e.clipboardData.items;
      if (!items) return;
      let handled = false;
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (item.kind === 'file') {
          const f = item.getAsFile();
          if (f) {
            handled = true;
            addDroppedOrPickedFile(f);
          }
        } else if (item.type && item.type.indexOf('image/') === 0) {
          const f = item.getAsFile();
          if (f) { handled = true; addImageFile(f); }
        }
      }
      if (handled) e.preventDefault();
    });

    window.addEventListener('message', event => {
      const msg = event.data;
      if (!msg) return;
      switch (msg.type) {
        case 'newMessage':
          addMessageEl(msg.message);
          break;
        case 'appendText': {
          const el = document.getElementById('msg-' + msg.id);
          if (el) {
            const textEl = el.querySelector('.text');
            textEl.textContent += msg.chunk;
            msgContainer.scrollTop = msgContainer.scrollHeight;
          }
          break;
        }
        case 'history':
          msgContainer.innerHTML = '';
          msg.messages.forEach(addMessageEl);
          break;
        case 'prefill':
          input.value = msg.text;
          input.focus();
          break;
        case 'toolUse':
          renderToolCard(msg.card);
          break;
        case 'toolResult':
          renderToolCard(msg.card);
          break;
        case 'toggleToolCard': {
          const card = toolCards.get(msg.id);
          if (card) {
            card.collapsed = msg.collapsed;
            const el = document.getElementById('tool-' + msg.id);
            if (el) updateToolCard(el, card);
          }
          break;
        }
        case 'fileChange':
          addFileChangePill(msg.payload);
          break;
        case 'options':
          applyOptions(msg);
          break;
        case 'addAttachments':
          mergeHostAttachments(msg);
          break;
      }
    });

    syncComposerChrome();

    vscode.postMessage({ type: 'requestHistory' });
    vscode.postMessage({ type: 'requestOptions' });
  </script>
</body>
</html>`;
  }
}

// ── Helpers ───────────────────────────────────────────────────────

function getNonce(): string {
  let text = "";
  const possible = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  for (let i = 0; i < 32; i++) {
    text += possible.charAt(Math.floor(Math.random() * possible.length));
  }
  return text;
}
