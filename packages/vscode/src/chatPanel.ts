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
import {
  buildChoiceResponseCommand,
  buildPermissionResponseCommand,
  serializeCommand,
} from "./client/protocol";
import type {
  AgentOutputPayload,
  ChoiceRequestPayload,
  EventPayload,
  FileChangePayload,
  ImageAttachment,
  PermissionRequestPayload,
  StreamModePayload,
  ToolResultPayload,
  ToolUsePayload,
  TurnCompletePayload,
} from "./client/types";

interface GatewayModelInfo {
  name: string;
  description?: string;
}

function uniqNonEmpty(values: Array<string | null | undefined>): string[] {
  const result: string[] = [];
  const seen = new Set<string>();
  for (const value of values) {
    if (typeof value !== "string") continue;
    const trimmed = value.trim();
    if (!trimmed || seen.has(trimmed)) continue;
    seen.add(trimmed);
    result.push(trimmed);
  }
  return result;
}

function formatTokenCount(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${Math.floor(tokens / 1_000)}k`;
  return `${tokens}`;
}

function formatPercent(percent: number): string {
  const rounded = Math.round(percent);
  return Math.abs(percent - rounded) < 0.05 ? `${rounded}%` : `${percent.toFixed(1)}%`;
}

function normalizePendingEditsVisibleFiles(value: unknown): number {
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed)) return 5;
  return Math.min(50, Math.max(1, Math.floor(parsed)));
}

interface ContextUsageStatus {
  usedTokens: number;
  windowTokens: number;
  remainingTokens: number;
  usedPercent: number;
  remainingPercent: number;
  details: string[];
}

function buildContextUsageStatus(payload: TurnCompletePayload): ContextUsageStatus | null {
  const used = Math.max(0, Math.trunc(payload.context_used_tokens ?? 0));
  const window = Math.max(0, Math.trunc(payload.context_window_tokens ?? 0));
  if (!used && !window) return null;
  if (!window) {
    return {
      usedTokens: used,
      windowTokens: 0,
      remainingTokens: 0,
      usedPercent: 0,
      remainingPercent: 0,
      details: [
        "背景信息窗口：",
        `已用 ${formatTokenCount(used)} 标记`,
        "总量未知",
      ],
    };
  }

  const usedPercent = Math.min(
    100,
    Math.max(0, payload.context_used_percent ?? (used / window) * 100),
  );
  const remainingPercent = Math.max(0, 100 - usedPercent);
  const remainingTokens = Math.max(
    0,
    Math.trunc(payload.context_remaining_tokens ?? window - used),
  );
  return {
    usedTokens: used,
    windowTokens: window,
    remainingTokens,
    usedPercent,
    remainingPercent,
    details: [
      "背景信息窗口：",
      `${formatPercent(usedPercent)} 已用（剩余 ${formatPercent(remainingPercent)}）`,
      `已用 ${formatTokenCount(used)} 标记，共 ${formatTokenCount(window)}`,
    ],
  };
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

export interface ThinkingCard {
  id: string;
  text: string;        // accumulated thinking text
  collapsed: boolean;
}

export interface ChoiceCard {
  id: string;
  question: string;
  options: string[];
  multiple: boolean;
  selected: string[];
  pendingSelected?: string[];
  completed: boolean;
  cancelled: boolean;
}

export interface PermissionCard {
  id: string;
  toolName: string;
  input: Record<string, unknown>;
  reason: string | null;
  allowed: boolean | null;
}

export interface PendingEditFileSummary {
  id: string;
  path: string;
  shortPath: string;
  action: "create" | "modify";
  added: number;
  removed: number;
  hunkCount: number;
}

export interface PendingEditReviewSummary {
  totalFiles: number;
  totalHunks: number;
  files: PendingEditFileSummary[];
}

export interface PendingEditActionMessage {
  action:
    | "undoAll"
    | "keepAll"
    | "reviewAll"
    | "undoFile"
    | "keepFile"
    | "reviewFile"
    | "undoHunk"
    | "keepHunk";
  changeId?: string;
  hunkId?: string;
}

type HistoryItem =
  | { kind: "message"; message: ChatMessage }
  | { kind: "tool"; card: ToolCard }
  | { kind: "thinking"; card: ThinkingCard }
  | { kind: "choice"; card: ChoiceCard }
  | { kind: "permission"; card: PermissionCard }
  | { kind: "fileChange"; payload: FileChangePayload };

// ── Provider ──────────────────────────────────────────────────────

export class ChatPanelProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "crabcode.chatPanel";

  private view: vscode.WebviewView | undefined;
  private messages: ChatMessage[] = [];
  private history: HistoryItem[] = [];
  private toolCards = new Map<string, ToolCard>();
  private thinkingCards = new Map<string, ThinkingCard>();
  private choiceCards = new Map<string, ChoiceCard>();
  private permissionCards = new Map<string, PermissionCard>();
  private activeThinkingId: string | null = null;
  private isBusy = false;
  private interruptRetryTimer: ReturnType<typeof setTimeout> | null = null;
  private latestModelRequestId = 0;
  private lastNonEmptyModels: string[] = [];
  private latestContextUsage: ContextUsageStatus | null = null;
  private webviewReady = false;
  private pendingWebviewMessages: any[] = [];
  private pendingEditReview: PendingEditReviewSummary | null = null;
  private readonly pendingEditActionEmitter = new vscode.EventEmitter<PendingEditActionMessage>();
  public readonly onPendingEditAction = this.pendingEditActionEmitter.event;

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
    this.webviewReady = false;
    this.pendingWebviewMessages = [];

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this.extensionUri],
    };

    // Handle messages from the webview
    webviewView.webview.onDidReceiveMessage((msg: any) => {
      if (!this.webviewReady) {
        this.webviewReady = true;
        this.flushPendingWebviewMessages();
      }
      switch (msg.type) {
        case "sendMessage":
          this.handleUserMessage(msg.text, msg.images);
          break;
        case "requestHistory":
          this.postMessage({ type: "history", items: this.history });
          break;
        case "requestOptions":
          void this.pushChatOptions();
          break;
        case "webviewReady":
          this.postMessage({ type: "history", items: this.history });
          void this.pushChatOptions();
          this.postMessage({ type: "busyState", busy: this.isBusy });
          if (this.latestContextUsage) {
            this.postMessage({ type: "contextUsage", usage: this.latestContextUsage });
          }
          this.postMessage({ type: "pendingEditReview", summary: this.pendingEditReview });
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
        case "toggleThinkingCard":
          this.toggleThinkingCard(msg.id);
          break;
        case "openFile":
          this.openFile(msg.path, msg.line);
          break;
        case "respondToChoice":
          this.respondToChoice(msg.id, msg.selected, msg.cancelled);
          break;
        case "respondToPermission":
          this.respondToPermission(msg.id, msg.allowed, msg.alwaysAllow);
          break;
        case "interrupt": {
          const result = this.connection.sendInterrupt();
          this.postMessage({ type: "interruptResult", result });
          if (result === "sent") {
            this.scheduleInterruptRetry();
          } else if (result === "disconnected") {
            this.setBusy(false);
          }
          break;
        }
        case "pendingEditAction":
          this.pendingEditActionEmitter.fire({
            action: msg.action,
            changeId: typeof msg.changeId === "string" ? msg.changeId : undefined,
            hunkId: typeof msg.hunkId === "string" ? msg.hunkId : undefined,
          });
          break;
        case "expandAllCards":
          this.expandAllCards();
          break;
      }
    });

    webviewView.webview.html = this.getHtmlForWebview(webviewView.webview);
    this.ensureSessionIfNeeded();

    webviewView.onDidChangeVisibility(() => {
      if (webviewView.visible) {
        this.ensureSessionIfNeeded();
        void this.pushChatOptions();
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
    const requestId = ++this.latestModelRequestId;
    const cfg = vscode.workspace.getConfiguration("crabcode");
    let fetchedModels: string[];
    try {
      fetchedModels = await this.resolveModelsFromSettingsOrGateway();
    } catch {
      fetchedModels = [];
    }
    if (requestId !== this.latestModelRequestId) {
      return;
    }
    if (fetchedModels.length > 0) {
      this.lastNonEmptyModels = fetchedModels;
    }
    const defaultModel = cfg.get<string>("chatModelDefault", "") ?? "";
    const fallbackModels = uniqNonEmpty([
      this.connection.modelName,
      defaultModel,
    ]);
    const models = fetchedModels.length > 0
      ? fetchedModels
      : (this.lastNonEmptyModels.length > 0 ? this.lastNonEmptyModels : fallbackModels);
    const permissionMode = cfg.get<string>("permissionMode", "default");
    const mode: "default" | "run_everything" =
      permissionMode === "run_everything" ? "run_everything" : "default";
    const pendingEditsVisibleFiles = normalizePendingEditsVisibleFiles(
      cfg.get<number>("pendingEditsVisibleFiles", 5),
    );
    const selectedModel =
      this.connection.modelName && models.includes(this.connection.modelName)
        ? this.connection.modelName
        : defaultModel;
    this.postMessage({
      type: "options",
      models,
      defaultModel,
      selectedModel,
      permissionMode: mode,
      pendingEditsVisibleFiles,
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

  /** Whether the embedded chat panel is currently visible to the user. */
  public isVisible(): boolean {
    return this.view?.visible ?? false;
  }

  /** Send a pre-composed prompt (e.g. from context-menu commands). */
  public sendPrompt(text: string): void {
    this.addMessage("user", text);
    this.ensureSessionIfNeeded();
    this.setBusy(true);
    this.connection.send(text);
    this.reveal();
  }

  /** Pre-fill the input box without sending. */
  public prefillInput(text: string): void {
    this.postMessage({ type: "prefill", text });
    this.reveal();
  }

  public setPendingEditReview(summary: PendingEditReviewSummary | null): void {
    this.pendingEditReview = summary;
    this.postMessage({ type: "pendingEditReview", summary });
  }

  // ── Internals ──────────────────────────────────────────────────

  private handleUserMessage(text: string, images?: ImageAttachment[]): void {
    this.addMessage("user", text, images);
    this.ensureSessionIfNeeded();
    this.setBusy(true);
    this.connection.send(text, { images });
  }

  private ensureSessionIfNeeded(): void {
    const cwd = vscode.workspace.workspaceFolders?.[0]?.uri?.fsPath ?? null;
    this.connection.ensureSession(cwd);
  }

  private handleServerEvent(payload: EventPayload): void {
    switch (payload.type) {
      case "stream_text":
        this.finalizeThinking();
        this.appendAssistantText(payload.text);
        break;
      case "thinking":
        this.handleThinking(payload.text);
        break;
      case "stream_mode":
        this.handleStreamMode(payload as StreamModePayload);
        break;
      case "agent_output":
        this.handleAgentOutput(payload as AgentOutputPayload);
        break;
      case "tool_use":
        this.finalizeThinking();
        this.handleToolUse(payload as ToolUsePayload);
        break;
      case "tool_result":
        this.handleToolResult(payload as ToolResultPayload);
        break;
      case "permission_request":
        this.handlePermissionRequest(payload as PermissionRequestPayload);
        break;
      case "choice_request":
        this.handleChoiceRequest(payload as ChoiceRequestPayload);
        break;
      case "file_change":
        this.handleFileChange(payload as FileChangePayload);
        break;
      case "error":
        this.setBusy(false);
        this.addMessage("system", `CrabCode：${payload.message}`);
        break;
      case "turn_complete": {
        this.finalizeThinking();
        const usage = buildContextUsageStatus(payload as TurnCompletePayload);
        if (usage) {
          this.latestContextUsage = usage;
          this.postMessage({ type: "contextUsage", usage });
        }
        this.setBusy(false);
        break;
      }
      case "server.connected":
      case "server.heartbeat":
        this.notifyConfigurationChanged();
        break;
    }
  }

  private handleThinking(chunk: string): void {
    this.setBusy(true);
    if (this.activeThinkingId) {
      // Append to existing thinking card
      const card = this.thinkingCards.get(this.activeThinkingId);
      if (card) {
        card.text += chunk;
        this.postMessage({ type: "appendThinking", id: card.id, chunk });
      }
    } else {
      // Create a new thinking card
      const id = `thinking-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const card: ThinkingCard = { id, text: chunk, collapsed: false };
      this.activeThinkingId = id;
      this.thinkingCards.set(id, card);
      this.history.push({ kind: "thinking", card });
      this.postMessage({ type: "thinkingStart", card });
    }
  }

  /** Finalize the current thinking block (collapse it). */
  private finalizeThinking(): void {
    if (!this.activeThinkingId) return;
    const card = this.thinkingCards.get(this.activeThinkingId);
    if (card) {
      card.collapsed = true;
      this.postMessage({ type: "thinkingEnd", id: card.id, collapsed: true });
    }
    this.activeThinkingId = null;
  }

  private setBusy(busy: boolean): void {
    if (this.isBusy === busy) return;
    this.isBusy = busy;
    if (!busy) {
      this.clearInterruptRetry();
    }
    this.postMessage({ type: "busyState", busy });
  }

  private scheduleInterruptRetry(): void {
    this.clearInterruptRetry();
    this.interruptRetryTimer = setTimeout(() => {
      if (!this.isBusy) return;
      const result = this.connection.sendInterrupt();
      if (result === "sent") {
        this.interruptRetryTimer = setTimeout(() => {
          if (this.isBusy) {
            this.setBusy(false);
            this.postMessage({ type: "interruptResult", result: "timeout" });
          }
        }, 5000);
      } else {
        this.setBusy(false);
      }
    }, 3000);
  }

  private clearInterruptRetry(): void {
    if (this.interruptRetryTimer) {
      clearTimeout(this.interruptRetryTimer);
      this.interruptRetryTimer = null;
    }
  }

  private handleStreamMode(payload: StreamModePayload): void {
    switch (payload.mode) {
      case "requesting":
      case "thinking":
      case "responding":
      case "tool-input":
      case "tool-running":
        this.setBusy(true);
        break;
      default:
        break;
    }
  }

  private handleAgentOutput(payload: AgentOutputPayload): void {
    switch (payload.stream) {
      case "text":
        this.finalizeThinking();
        this.appendAssistantText(payload.text);
        this.setBusy(true);
        break;
      case "thinking":
        this.setBusy(true);
        break;
      default:
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
    this.history.push({ kind: "tool", card });
    this.postMessage({ type: "toolUse", card });
  }

  private handleToolResult(payload: ToolResultPayload): void {
    const card = this.toolCards.get(payload.tool_use_id);
    if (card) {
      card.result = payload.result_for_display ?? payload.result;
      card.isError = payload.is_error ?? false;
      card.collapsed = !card.isError; // Keep errors expanded so they stay visible
      this.postMessage({ type: "toolResult", card });
    }
  }

  private handleChoiceRequest(payload: ChoiceRequestPayload): void {
    const card: ChoiceCard = {
      id: payload.tool_use_id,
      question: payload.question,
      options: payload.options,
      multiple: payload.multiple ?? false,
      selected: [],
      pendingSelected: [],
      completed: false,
      cancelled: false,
    };
    this.choiceCards.set(payload.tool_use_id, card);
    this.history.push({ kind: "choice", card });
    this.postMessage({ type: "choiceRequest", card });
  }

  private handlePermissionRequest(payload: PermissionRequestPayload): void {
    const card: PermissionCard = {
      id: payload.tool_use_id,
      toolName: payload.tool_name,
      input: payload.tool_input,
      reason: payload.reason ?? null,
      allowed: null,
    };
    this.permissionCards.set(payload.tool_use_id, card);
    this.history.push({ kind: "permission", card });
    this.postMessage({ type: "permissionRequest", card });
  }

  private respondToChoice(id: string, selected: string[] = [], cancelled = false): void {
    const card = this.choiceCards.get(id);
    if (!card || card.completed) {
      return;
    }
    card.selected = selected;
    card.pendingSelected = selected;
    card.cancelled = cancelled;
    card.completed = true;
    const cmd = buildChoiceResponseCommand(id, selected, { cancelled });
    this.connection.sendRaw(serializeCommand(cmd));
    this.postMessage({ type: "choiceResolved", card });
  }

  private respondToPermission(id: string, allowed: boolean, alwaysAllow = false): void {
    const card = this.permissionCards.get(id);
    if (!card || card.allowed !== null) {
      return;
    }
    card.allowed = allowed;
    const cmd = buildPermissionResponseCommand(id, allowed, { alwaysAllow });
    this.connection.sendRaw(serializeCommand(cmd));
    this.postMessage({ type: "permissionResolved", card });
  }

  private handleFileChange(payload: FileChangePayload): void {
    this.history.push({ kind: "fileChange", payload });
    this.postMessage({ type: "fileChange", payload });
  }

  private toggleToolCard(id: string): void {
    const card = this.toolCards.get(id);
    if (card) {
      card.collapsed = !card.collapsed;
      this.postMessage({ type: "toggleToolCard", id, collapsed: card.collapsed });
    }
  }

  private toggleThinkingCard(id: string): void {
    const card = this.thinkingCards.get(id);
    if (card) {
      card.collapsed = !card.collapsed;
      this.postMessage({ type: "toggleThinkingCard", id, collapsed: card.collapsed });
    }
  }

  private expandAllCards(): void {
    for (const [id, card] of this.toolCards) {
      if (!card.collapsed) continue;
      card.collapsed = false;
      this.postMessage({ type: "toggleToolCard", id, collapsed: false });
    }
    for (const [id, card] of this.thinkingCards) {
      if (!card.collapsed) continue;
      card.collapsed = false;
      this.postMessage({ type: "toggleThinkingCard", id, collapsed: false });
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
    this.history.push({ kind: "message", message: msg });
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
    if (!this.view) {
      return;
    }
    if (!this.webviewReady) {
      this.pendingWebviewMessages.push(msg);
      return;
    }
    void this.view.webview.postMessage(msg);
  }

  private flushPendingWebviewMessages(): void {
    if (!this.view || !this.webviewReady || this.pendingWebviewMessages.length === 0) {
      return;
    }
    const queue = this.pendingWebviewMessages;
    this.pendingWebviewMessages = [];
    for (const msg of queue) {
      void this.view.webview.postMessage(msg);
    }
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
    html {
      height: 100%;
      width: 100%;
      min-width: 0;
    }
    body {
      font-family: var(--font);
      font-size: var(--vscode-font-size);
      color: var(--vscode-foreground);
      background: var(--vscode-sideBar-background);
      display: flex;
      flex-direction: column;
      height: 100%;
      width: 100%;
      min-height: 0;
      min-width: 0;
      overflow: hidden;
    }
    button, select, textarea { font: inherit; }
    button:focus-visible, select:focus-visible, textarea:focus-visible {
      outline: 1.5px solid var(--accent);
      outline-offset: 1px;
    }

    /* ── Messages ──────────────────────────────────────────────── */
    #messages {
      flex: 1 1 0;
      width: 100%;
      min-height: 0;
      min-width: 0;
      overflow-y: auto;
      overflow-x: hidden;
      overscroll-behavior: contain;
      scrollbar-gutter: stable;
      padding: 12px 10px 16px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    #messages > * {
      max-width: 100%;
      min-width: 0;
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
    .turn-block {
      display: flex;
      flex-direction: column;
      gap: 8px;
      width: 100%;
      min-width: 0;
    }
    .turn-content {
      display: flex;
      flex-direction: column;
      gap: 8px;
      width: 100%;
      min-width: 0;
    }
    .turn-summary {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      align-self: flex-start;
      padding: 4px 0;
      border: none;
      background: transparent;
      color: var(--text-muted);
      cursor: pointer;
      font-size: 11.5px;
      font-weight: 600;
      letter-spacing: 0.01em;
    }
    .turn-summary:hover {
      color: var(--vscode-foreground);
    }
    .turn-summary.is-running .turn-summary-status {
      color: var(--vscode-foreground);
    }
    .turn-summary-status {
      color: var(--text-muted);
    }
    .turn-summary-time {
      color: var(--vscode-foreground);
      opacity: 0.88;
      font-variant-numeric: tabular-nums;
    }
    .turn-summary-chevron {
      opacity: 0.52;
      font-size: 12px;
      transition: transform 0.15s ease;
    }
    .turn-summary.is-expanded .turn-summary-chevron {
      transform: rotate(90deg);
    }
    .turn-detail-hidden {
      display: none !important;
    }

    /* ── Message bubbles ───────────────────────────────────────── */
    .msg {
      position: relative;
      width: 100%;
      max-width: 100%;
      min-width: 0;
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
      width: 100%;
      min-width: 0;
      align-self: stretch;
      border-radius: var(--radius);
      border: 1px solid var(--border);
      background: var(--surface-soft);
      overflow: hidden;
      font-size: 12px;
      box-shadow: var(--shadow-sm);
      position: relative;
    }
    .tool-card::before, .request-card::before, .thinking-card::before {
      content: '';
      position: absolute;
      left: 8px;
      top: 10px;
      bottom: 10px;
      width: 2px;
      border-radius: 999px;
      background: color-mix(in srgb, var(--accent) 28%, transparent);
      opacity: 0.7;
    }
    .tool-card-header {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px;
      min-width: 0;
      padding: 8px 10px 8px 20px;
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
    .tool-card-header .tool-name {
      flex: 1 1 auto;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: 600;
      font-size: 11.5px;
    }
    .tool-card-header .chevron { flex-shrink: 0; opacity: 0.45; transition: transform 0.15s; font-size: 11px; }
    .tool-card-header .chevron.collapsed { transform: rotate(-90deg); }
    .tool-card-header .status {
      flex-shrink: 0;
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
      padding: 8px 10px 8px 20px;
      max-height: 240px;
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

    .request-card {
      margin: 4px 0 0;
      width: 100%;
      min-width: 0;
      align-self: stretch;
      border-radius: var(--radius);
      border: 1px solid var(--border);
      background: var(--surface-soft);
      overflow: hidden;
      box-shadow: var(--shadow-sm);
      position: relative;
    }
    .request-card.permission {
      border-color: color-mix(in srgb, var(--accent) 25%, var(--border));
    }
    .request-card-header {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px;
      min-width: 0;
      padding: 8px 10px 8px 20px;
      border-bottom: 1px solid var(--border);
      background: color-mix(in srgb, var(--surface-soft) 75%, var(--vscode-editor-background));
    }
    .request-card-header .icon {
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
    .request-title {
      flex: 1 1 auto;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 12px;
      font-weight: 600;
    }
    .request-status {
      flex-shrink: 0;
      font-size: 10.5px;
      padding: 2px 7px;
      border-radius: 999px;
      background: color-mix(in srgb, var(--vscode-badge-background, #444) 45%, transparent);
    }
    .request-status.done { color: var(--vscode-terminal-ansiGreen, #89d185); }
    .request-status.denied, .request-status.cancelled { color: var(--vscode-errorForeground, #f48771); }
    .request-card-body { padding: 10px 10px 10px 20px; display: flex; flex-direction: column; gap: 8px; }
    .request-question, .request-reason { white-space: pre-wrap; line-height: 1.45; font-size: 12px; }
    .request-options { display: flex; flex-wrap: wrap; gap: 8px; }
    .request-option, .request-action {
      border: 1px solid var(--border);
      background: var(--surface-elevated);
      color: var(--vscode-foreground);
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 11.5px;
      cursor: pointer;
    }
    .request-option.selected, .request-action.primary {
      border-color: color-mix(in srgb, var(--accent) 50%, var(--border));
      background: color-mix(in srgb, var(--accent) 16%, var(--surface-elevated));
    }
    .request-option:disabled, .request-action:disabled { cursor: default; opacity: 0.6; }
    .request-action.subtle { background: transparent; }
    .request-summary { font-size: 11px; color: var(--text-muted); }
    .timeline-meta {
      font-size: 10.5px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .checklist-stack {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .checklist-meta-row {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
    }
    .checklist-badge {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 0 8px;
      border-radius: 999px;
      border: 1px solid color-mix(in srgb, var(--accent) 24%, var(--border));
      background: color-mix(in srgb, var(--accent) 10%, var(--surface-elevated));
      color: var(--vscode-foreground);
      font-size: 10.5px;
      font-weight: 600;
      letter-spacing: 0.02em;
    }
    .checklist-badge.subtle {
      border-color: var(--border);
      background: color-mix(in srgb, var(--vscode-badge-background, #444) 32%, transparent);
      color: var(--text-muted);
    }
    .checklist-badge.good {
      border-color: color-mix(in srgb, var(--vscode-terminal-ansiGreen, #89d185) 35%, var(--border));
      background: color-mix(in srgb, var(--vscode-terminal-ansiGreen, #89d185) 12%, transparent);
      color: var(--vscode-terminal-ansiGreen, #89d185);
    }
    .checklist-caption {
      font-size: 11px;
      color: var(--text-muted);
      line-height: 1.45;
    }
    .checklist-panel {
      display: flex;
      flex-direction: column;
      gap: 10px;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid color-mix(in srgb, var(--accent) 18%, var(--border));
      background:
        linear-gradient(180deg,
          color-mix(in srgb, var(--accent) 4%, var(--surface-elevated)),
          color-mix(in srgb, var(--vscode-editor-background) 92%, var(--surface-soft)));
      box-shadow: inset 0 1px 0 color-mix(in srgb, white 3%, transparent);
    }
    .checklist-panel-header {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
    }
    .checklist-panel-title {
      min-width: 0;
      font-size: 12px;
      font-weight: 700;
      line-height: 1.35;
      color: var(--vscode-foreground);
    }
    .checklist-panel-subtitle {
      margin-top: 2px;
      font-size: 10.5px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .checklist-progress {
      min-width: 58px;
      text-align: right;
      font-size: 11px;
      font-weight: 700;
      color: var(--vscode-foreground);
      font-variant-numeric: tabular-nums;
    }
    .checklist-progress-track {
      position: relative;
      height: 6px;
      border-radius: 999px;
      overflow: hidden;
      background: color-mix(in srgb, var(--vscode-badge-background, #444) 40%, transparent);
    }
    .checklist-progress-fill {
      position: absolute;
      inset: 0 auto 0 0;
      border-radius: inherit;
      background: linear-gradient(90deg,
        color-mix(in srgb, var(--accent) 70%, white),
        color-mix(in srgb, var(--vscode-terminal-ansiGreen, #89d185) 82%, var(--accent)));
    }
    .checklist-list {
      margin: 0;
      padding: 0;
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .checklist-item {
      display: flex;
      gap: 10px;
      align-items: flex-start;
      padding: 7px 8px;
      border-radius: 10px;
      background: color-mix(in srgb, var(--vscode-editor-background) 50%, transparent);
      border: 1px solid color-mix(in srgb, var(--border) 80%, transparent);
    }
    .checklist-item.is-checked {
      background: color-mix(in srgb, var(--vscode-terminal-ansiGreen, #89d185) 8%, transparent);
      border-color: color-mix(in srgb, var(--vscode-terminal-ansiGreen, #89d185) 24%, var(--border));
    }
    .checklist-item-marker {
      width: 18px;
      height: 18px;
      flex: 0 0 18px;
      border-radius: 999px;
      border: 1px solid color-mix(in srgb, var(--border) 90%, transparent);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 10px;
      background: var(--surface-elevated);
      color: transparent;
      margin-top: 1px;
    }
    .checklist-item.is-checked .checklist-item-marker {
      border-color: color-mix(in srgb, var(--vscode-terminal-ansiGreen, #89d185) 45%, var(--border));
      background: color-mix(in srgb, var(--vscode-terminal-ansiGreen, #89d185) 18%, transparent);
      color: var(--vscode-terminal-ansiGreen, #89d185);
    }
    .checklist-item-index {
      flex: 0 0 22px;
      font-size: 10.5px;
      font-weight: 700;
      color: var(--text-muted);
      text-align: right;
      line-height: 1.5;
      font-variant-numeric: tabular-nums;
    }
    .checklist-item-text {
      min-width: 0;
      flex: 1;
      line-height: 1.45;
      font-size: 11.5px;
      color: var(--vscode-foreground);
    }
    .checklist-item.is-checked .checklist-item-text {
      color: var(--text-muted);
      text-decoration: line-through;
      text-decoration-thickness: 1.25px;
    }
    .checklist-empty {
      font-size: 11px;
      color: var(--text-muted);
      padding: 2px 0;
    }
    .request-pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: var(--vscode-editor-font-family, monospace);
      font-size: 11px;
      line-height: 1.4;
      padding: 8px;
      border-radius: 8px;
      background: color-mix(in srgb, var(--vscode-editor-background) 90%, var(--surface-soft));
      border: 1px solid var(--border);
    }
    .request-actions { display: flex; gap: 8px; }

    /* ── Thinking cards ───────────────────────────────────────── */
    .thinking-card {
      margin: 4px 0 0;
      width: 100%;
      min-width: 0;
      align-self: stretch;
      border-radius: var(--radius);
      border: 1px solid color-mix(in srgb, var(--accent) 25%, var(--border));
      background: color-mix(in srgb, var(--accent) 4%, var(--surface-soft));
      overflow: hidden;
      font-size: 12px;
      box-shadow: var(--shadow-sm);
      position: relative;
    }
    .thinking-card-header {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px;
      min-width: 0;
      padding: 8px 10px 8px 20px;
      cursor: pointer;
      background: transparent;
      transition: background 0.12s;
    }
    .thinking-card-header:hover {
      background: var(--accent-muted);
    }
    .thinking-card-header .icon {
      width: 18px;
      height: 18px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 6px;
      background: color-mix(in srgb, var(--accent) 12%, transparent);
      font-size: 11px;
      opacity: 0.85;
    }
    .thinking-card-header .label {
      flex: 1 1 auto;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: 600;
      font-size: 11.5px;
      color: var(--text-muted);
    }
    .thinking-card-header .chevron { flex-shrink: 0; opacity: 0.45; transition: transform 0.15s; font-size: 11px; }
    .thinking-card-header .chevron.collapsed { transform: rotate(-90deg); }
    .card-preview {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      flex: 0 1 34%;
      opacity: 0.56;
    }
    .tool-card-header .card-preview {
      margin-left: auto;
    }
    .thinking-card-header .card-preview {
      flex-basis: 50%;
      opacity: 0.5;
    }
    .thinking-card-body {
      padding: 8px 10px 8px 20px;
      max-height: 240px;
      overflow-y: auto;
      background: color-mix(in srgb, var(--vscode-editor-background) 85%, var(--surface-soft));
      border-top: 1px solid var(--border);
    }
    .thinking-card-body.hidden { display: none; }
    .thinking-card-body pre {
      white-space: pre-wrap;
      word-break: break-word;
      font-family: var(--vscode-editor-font-family, monospace);
      font-size: 11px;
      line-height: 1.4;
      color: var(--text-muted);
    }

    /* ── Busy indicator ──────────────────────────────────────── */
    #busy-indicator {
      display: none;
      flex-shrink: 0;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      margin: 4px 0 0;
      border-radius: var(--radius);
      border: 1px solid var(--border);
      background: var(--surface-soft);
      font-size: 12px;
      color: var(--text-muted);
    }
    #busy-indicator.visible { display: flex; }
    .busy-dots {
      display: inline-flex;
      gap: 3px;
    }
    .busy-dots span {
      width: 5px;
      height: 5px;
      border-radius: 50%;
      background: var(--accent);
      opacity: 0.5;
      animation: busy-bounce 1.2s infinite ease-in-out;
    }
    .busy-dots span:nth-child(2) { animation-delay: 0.15s; }
    .busy-dots span:nth-child(3) { animation-delay: 0.3s; }
    @keyframes busy-bounce {
      0%, 80%, 100% { transform: scale(0.6); opacity: 0.3; }
      40% { transform: scale(1); opacity: 0.9; }
    }
    .busy-label {
      font-size: 11.5px;
      font-weight: 500;
    }

    /* ── Tool card running pulse ─────────────────────────────── */
    .tool-card-header .status.running {
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .tool-card-header .status.running::before {
      content: '';
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--accent);
      animation: tool-pulse 1s infinite ease-in-out;
    }
    @keyframes tool-pulse {
      0%, 100% { opacity: 0.4; }
      50% { opacity: 1; }
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

    /* ── Pending edits review bar ──────────────────────────────── */
    .pending-edits {
      flex-shrink: 0;
      width: 100%;
      min-width: 0;
      padding: 7px 9px;
      border-top: 1px solid var(--border);
      background: color-mix(in srgb, var(--vscode-editor-background) 72%, var(--vscode-sideBar-background));
      display: flex;
      flex-direction: column;
      gap: 6px;
      box-shadow: 0 -4px 14px rgba(0,0,0,0.14);
    }
    .pending-edits.hidden { display: none; }
    .pending-edits-header,
    .pending-edit-row {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }
    .pending-edits-title {
      flex: 1 1 auto;
      min-width: 0;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 11.5px;
      font-weight: 650;
      font-family: inherit;
      line-height: 1.25;
      color: var(--vscode-foreground);
      border: none;
      background: transparent;
      text-align: left;
      padding: 0;
      cursor: pointer;
    }
    .pending-edits-title .chevron {
      display: inline-block;
      color: var(--text-muted);
      font-size: 12px;
      transition: transform 0.15s ease;
    }
    .pending-edits.is-collapsed .pending-edits-title .chevron {
      transform: rotate(-90deg);
    }
    .pending-edits-list {
      display: flex;
      flex-direction: column;
      gap: 4px;
      max-height: var(--pending-edit-list-max-height, 140px);
      overflow-y: auto;
      padding-right: 2px;
    }
    .pending-edits.is-collapsed .pending-edits-list {
      display: none;
    }
    .pending-edits-actions,
    .pending-edit-actions {
      flex: 0 0 auto;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .pending-edit-row {
      padding-top: 2px;
      color: var(--text-muted);
      font-size: 11.5px;
      min-height: 24px;
    }
    .pending-edit-icon {
      flex: 0 0 auto;
      opacity: 0.8;
      font-size: 13px;
    }
    .pending-edit-name {
      flex: 1 1 auto;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      border: none;
      background: transparent;
      text-align: left;
      padding: 0;
      font: inherit;
      color: var(--vscode-foreground);
      cursor: pointer;
    }
    .pending-edit-name:hover {
      color: var(--vscode-textLink-foreground, var(--vscode-foreground));
      text-decoration: underline;
    }
    .pending-edit-stats {
      flex: 0 0 auto;
      color: var(--vscode-terminal-ansiGreen, #89d185);
      font-family: var(--vscode-editor-font-family, monospace);
      font-size: 11px;
    }
    .pending-edit-stats .removed {
      color: var(--vscode-terminal-ansiRed, #f48771);
    }
    .pending-edit-btn {
      border: none;
      background: transparent;
      color: var(--vscode-foreground);
      cursor: pointer;
      border-radius: 6px;
      padding: 3px 6px;
      font-size: 11.5px;
      line-height: 1.25;
      opacity: 0.82;
    }
    .pending-edit-btn:hover {
      background: var(--accent-muted);
      opacity: 1;
    }
    .pending-edit-btn.primary {
      background: color-mix(in srgb, var(--vscode-button-background, var(--accent)) 82%, transparent);
      color: var(--vscode-button-foreground, #fff);
      opacity: 1;
    }
    .pending-edit-btn.primary:hover {
      background: var(--vscode-button-hoverBackground, var(--vscode-button-background, var(--accent)));
    }

    /* ── Composer ──────────────────────────────────────────────── */
    #composer-wrap {
      flex-shrink: 0;
      width: 100%;
      min-width: 0;
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
      width: 100%;
      min-width: 0;
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
    .context-meter {
      --ctx-progress: 0%;
      --ctx-ring-active: color-mix(in srgb, var(--vscode-foreground) 70%, transparent);
      --ctx-ring-track: color-mix(in srgb, var(--vscode-foreground) 17%, transparent);
      position: relative;
      flex: 0 0 auto;
      width: 28px;
      height: 28px;
      border-radius: 8px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--vscode-foreground);
      opacity: 0.78;
      cursor: default;
    }
    .context-meter[hidden] { display: none; }
    .context-meter:hover,
    .context-meter:focus {
      background: var(--accent-muted);
      opacity: 1;
    }
    .context-meter:focus-visible {
      outline: 1.5px solid var(--accent);
      outline-offset: 1px;
    }
    .context-meter.is-warn {
      --ctx-ring-active: var(--vscode-editorWarning-foreground, #ffcc66);
    }
    .context-meter.is-danger {
      --ctx-ring-active: var(--vscode-errorForeground, #f48771);
    }
    .ctx-ring {
      position: relative;
      width: 16px;
      height: 16px;
      border-radius: 50%;
      background: conic-gradient(from -90deg, var(--ctx-ring-active) var(--ctx-progress), var(--ctx-ring-track) 0);
      box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--vscode-foreground) 8%, transparent);
    }
    .ctx-ring::after {
      content: '';
      position: absolute;
      inset: 3px;
      border-radius: 50%;
      background: var(--surface-elevated);
    }
    .context-tooltip {
      position: absolute;
      right: -42px;
      bottom: calc(100% + 8px);
      min-width: 210px;
      padding: 8px 10px;
      border-radius: 8px;
      border: 1px solid var(--vscode-editorWidget-border, var(--border));
      background: var(--vscode-editorWidget-background, var(--vscode-menu-background));
      color: var(--vscode-editorWidget-foreground, var(--vscode-foreground));
      box-shadow: 0 8px 22px rgba(0,0,0,0.34);
      font-size: 12px;
      line-height: 1.55;
      text-align: center;
      opacity: 0;
      visibility: hidden;
      pointer-events: none;
      transform: translateY(2px);
      transition: opacity 0.12s ease, transform 0.12s ease, visibility 0.12s;
      z-index: 45;
    }
    .context-tooltip::after {
      content: '';
      position: absolute;
      right: 49px;
      bottom: -5px;
      width: 9px;
      height: 9px;
      background: inherit;
      border-right: 1px solid var(--vscode-editorWidget-border, var(--border));
      border-bottom: 1px solid var(--vscode-editorWidget-border, var(--border));
      transform: rotate(45deg);
    }
    .context-meter:hover .context-tooltip,
    .context-meter:focus .context-tooltip {
      opacity: 1;
      visibility: visible;
      transform: translateY(0);
    }
    .context-tooltip-title {
      color: var(--text-muted);
      font-weight: 600;
    }
    .context-tooltip-usage {
      font-weight: 700;
      color: var(--vscode-foreground);
    }
    .context-tooltip-detail {
      color: color-mix(in srgb, var(--vscode-foreground) 86%, transparent);
    }
    .plus-menu {
      position: fixed;
      top: 0;
      left: 0;
      min-width: min(160px, calc(100vw - 16px));
      max-width: calc(100vw - 16px);
      background: var(--vscode-menu-background);
      color: var(--vscode-menu-foreground);
      border: 1px solid var(--vscode-menu-border, #444);
      border-radius: 10px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.35);
      z-index: 100;
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
    .tb-model-wrap {
      position: relative;
      width: 100%;
      min-width: 0;
    }
    .tb-model-wrap::after {
      content: '▾';
      position: absolute;
      right: 10px;
      top: 50%;
      transform: translateY(-50%);
      color: var(--text-muted);
      font-size: 11px;
      pointer-events: none;
    }
    .tb-model-label {
      position: absolute;
      left: 10px;
      right: 26px;
      top: 50%;
      transform: translateY(-50%);
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      pointer-events: none;
      color: var(--vscode-foreground);
      font-size: 11.5px;
    }
    .tb-model-wrap.is-empty .tb-model-label {
      color: var(--text-muted);
    }
    .tb-model {
      width: 100%;
      max-width: 100%;
      padding: 4px 28px 4px 10px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: color-mix(in srgb, var(--vscode-input-background) 50%, transparent);
      color: transparent;
      font-size: 11.5px;
      cursor: pointer;
      appearance: none;
      -webkit-appearance: none;
    }
    .tb-model:hover {
      border-color: color-mix(in srgb, var(--accent) 40%, var(--border));
      background: color-mix(in srgb, var(--vscode-input-background) 70%, transparent);
    }
    .tb-model:disabled { opacity: 0.5; cursor: not-allowed; }
    .tb-model option {
      color: var(--vscode-foreground);
    }
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

    :root[data-panel-width="narrow"] #messages {
      padding-left: 8px;
      padding-right: 8px;
    }
    :root[data-panel-width="narrow"] .msg.user {
      width: 100%;
    }
    :root[data-panel-width="narrow"] .tool-card-header,
    :root[data-panel-width="narrow"] .request-card-header,
    :root[data-panel-width="narrow"] .thinking-card-header {
      gap: 4px;
      padding-right: 8px;
    }
    :root[data-panel-width="narrow"] .card-preview {
      display: none;
    }
    :root[data-panel-width="narrow"] .tool-card-header .status,
    :root[data-panel-width="narrow"] .request-status {
      order: 3;
      margin-left: 24px;
    }
    :root[data-panel-width="narrow"] .tool-card-header .chevron,
    :root[data-panel-width="narrow"] .thinking-card-header .chevron {
      margin-left: auto;
    }
    :root[data-panel-width="compact"] .request-actions {
      flex-wrap: wrap;
    }

    /* ── Scrollbar (subtle) ────────────────────────────────────── */
    ::-webkit-scrollbar { width: 5px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: color-mix(in srgb, var(--vscode-foreground) 15%, transparent); border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: color-mix(in srgb, var(--vscode-foreground) 28%, transparent); }
  </style>
</head>
<body>
  <div id="messages"></div>
  <div id="busy-indicator">
    <span class="busy-dots"><span></span><span></span><span></span></span>
    <span class="busy-label">CrabCode 正在处理</span>
  </div>
  <div id="pending-edits-bar" class="pending-edits hidden"></div>
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
            <button type="button" class="tb-icon-btn" id="plus-btn" title="添加文件或图片" aria-haspopup="menu" aria-expanded="false">+</button>
          </div>
          <div class="model-pill-wrap">
            <div id="model-select-wrap" class="tb-model-wrap is-empty">
              <span id="model-select-label" class="tb-model-label">（正在连接网关…）</span>
              <select id="model-select" class="tb-model" title="模型"></select>
            </div>
          </div>
          <div id="context-meter" class="context-meter" hidden tabindex="0" role="img" aria-label="背景信息窗口用量" aria-describedby="context-tooltip">
            <span class="ctx-ring" aria-hidden="true"></span>
            <div id="context-tooltip" class="context-tooltip" role="tooltip"></div>
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
  <div id="plus-menu" class="plus-menu hidden" role="menu">
    <button type="button" role="menuitem" data-action="image">添加图片…</button>
    <button type="button" role="menuitem" data-action="file">添加文件…</button>
    <button type="button" role="menuitem" data-action="screenshot">屏幕截图说明</button>
  </div>
  <script nonce="${nonce}">
    (function() {
      try {
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
        const modelSelectWrap = document.getElementById('model-select-wrap');
        const modelSelectLabel = document.getElementById('model-select-label');
        const contextMeter = document.getElementById('context-meter');
        const contextTooltip = document.getElementById('context-tooltip');
        const permissionSelect = document.getElementById('permission-select');
        const pendingEditsBar = document.getElementById('pending-edits-bar');

    // ── Tool card state ──────────────────────────────────────────
    const toolCards = new Map();
    const thinkingCards = new Map();
    const choiceCards = new Map();
    const permissionCards = new Map();
    const toolCardTurns = new Map();
    const thinkingCardTurns = new Map();
    const busyIndicator = document.getElementById('busy-indicator');
    const busyLabel = busyIndicator ? busyIndicator.querySelector('.busy-label') : null;
    const rootEl = document.documentElement;
    let isBusy = false;
    let stickToBottom = true;
    let hasReceivedOptions = false;
    let pendingEditsCollapsed = false;
    let pendingEditsVisibleFiles = 5;
    let currentPendingEditSummary = null;
    let turnCounter = 0;
    let activeTurn = null;
    const turns = [];
    const SEND_ICON_HTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>';
    const STOP_ICON_HTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';

    // pendingImages: { media_type, data, dataUrl }; pendingTextFiles: { name, text }
    const pendingImages = [];
    const pendingTextFiles = [];
    const MAX_IMAGE_SIZE = 20 * 1024 * 1024;
    const MAX_TEXT_FILE = 20 * 1024 * 1024;

    function isNearBottom() {
      return msgContainer.scrollHeight - msgContainer.scrollTop - msgContainer.clientHeight <= 24;
    }

    function scrollMessagesToBottom(force = false) {
      if (!force && !stickToBottom) return;
      requestAnimationFrame(() => {
        msgContainer.scrollTop = msgContainer.scrollHeight;
      });
    }

    function captureScrollAnchor() {
      return isNearBottom();
    }

    function restoreScrollAnchor(shouldStick) {
      stickToBottom = shouldStick;
      if (shouldStick) {
        scrollMessagesToBottom(true);
      }
    }

    function normalizePendingEditsVisibleFiles(value) {
      const parsed = Number(value);
      if (!Number.isFinite(parsed)) return 5;
      return Math.min(50, Math.max(1, Math.floor(parsed)));
    }

    function formatTurnDuration(ms) {
      const totalSeconds = Math.max(0, Math.floor(ms / 1000));
      const minutes = Math.floor(totalSeconds / 60);
      const seconds = totalSeconds % 60;
      const hours = Math.floor(minutes / 60);
      if (hours > 0) {
        const restMinutes = minutes % 60;
        return hours + 'h ' + String(restMinutes).padStart(2, '0') + 'm';
      }
      if (minutes > 0) {
        return minutes + 'm ' + String(seconds).padStart(2, '0') + 's';
      }
      return totalSeconds + 's';
    }

    function getTurnDurationMs(turn) {
      const end = turn.status === 'done' ? (turn.endTime || turn.startTime) : Date.now();
      return Math.max(0, end - turn.startTime);
    }

    function getTurnSummaryLabel(turn) {
      return turn.status === 'running' ? '处理中' : '已处理';
    }

    function updateTurnSummary(turn) {
      const hasDetails = turn.detailCount > 0;
      turn.summaryEl.style.display = hasDetails ? 'inline-flex' : 'none';
      turn.summaryEl.classList.toggle('is-running', turn.status === 'running');
      turn.summaryEl.classList.toggle('is-expanded', !!turn.expanded);
      turn.summaryStatusEl.textContent = getTurnSummaryLabel(turn);
      turn.summaryTimeEl.textContent = formatTurnDuration(getTurnDurationMs(turn));
      syncTurnDetailVisibility(turn);
    }

    function updateAllTurnSummaries() {
      turns.forEach(updateTurnSummary);
    }

    function finishActiveTurn() {
      if (!activeTurn) return;
      activeTurn.status = 'done';
      activeTurn.endTime = Date.now();
      updateTurnSummary(activeTurn);
      activeTurn = null;
    }

    function toggleTurnDetails(turn) {
      if (!turn || turn.detailCount === 0) return;
      turn.expanded = !turn.expanded;
      updateTurnSummary(turn);
    }

    function createTurn(startTime) {
      const shouldStick = captureScrollAnchor();
      const turn = {
        id: 'turn-' + (++turnCounter),
        startTime: startTime || Date.now(),
        endTime: null,
        status: 'running',
        expanded: false,
        detailCount: 0,
        toolIds: [],
        thinkingIds: [],
        rootEl: document.createElement('div'),
        contentEl: document.createElement('div'),
        summaryEl: document.createElement('button'),
        summaryStatusEl: document.createElement('span'),
        summaryTimeEl: document.createElement('span'),
      };

      turn.rootEl.className = 'turn-block';
      turn.rootEl.id = turn.id;
      turn.contentEl.className = 'turn-content';
      turn.summaryEl.type = 'button';
      turn.summaryEl.className = 'turn-summary';
      turn.summaryStatusEl.className = 'turn-summary-status';
      turn.summaryTimeEl.className = 'turn-summary-time';
      turn.summaryEl.innerHTML = '<span class="turn-summary-status"></span><span class="turn-summary-time"></span><span class="turn-summary-chevron">›</span>';
      turn.summaryStatusEl = turn.summaryEl.querySelector('.turn-summary-status');
      turn.summaryTimeEl = turn.summaryEl.querySelector('.turn-summary-time');
      turn.summaryEl.addEventListener('click', () => {
        toggleTurnDetails(turn);
      });

      turn.rootEl.appendChild(turn.summaryEl);
      turn.rootEl.appendChild(turn.contentEl);
      msgContainer.appendChild(turn.rootEl);
      turns.push(turn);
      activeTurn = turn;
      updateTurnSummary(turn);
      restoreScrollAnchor(shouldStick);
      return turn;
    }

    function getCurrentTurn() {
      if (activeTurn) return activeTurn;
      return turns.length > 0 ? turns[turns.length - 1] : null;
    }

    function getTurnById(turnId) {
      return turns.find(turn => turn.id === turnId) || null;
    }

    function appendToTurnContent(turn, el) {
      if (turn) {
        turn.contentEl.appendChild(el);
      } else {
        msgContainer.appendChild(el);
      }
    }

    function appendToTurnDetails(turn, el, options = {}) {
      if (turn) {
        turn.contentEl.appendChild(el);
        turn.detailCount += 1;
        if (options.toolId) turn.toolIds.push(options.toolId);
        if (options.thinkingId) turn.thinkingIds.push(options.thinkingId);
        if (turn.status === 'running' || options.forceExpanded) {
          turn.expanded = true;
        }
        el.classList.add('turn-detail-item');
        updateTurnSummary(turn);
      } else {
        msgContainer.appendChild(el);
      }
    }

    function setDetailCardVisibility(turn, el) {
      if (!turn || !el) return;
      el.classList.toggle('turn-detail-hidden', !turn.expanded);
    }

    function syncTurnDetailVisibility(turn) {
      if (!turn) return;
      for (const id of turn.toolIds) {
        const card = toolCards.get(id);
        const el = document.getElementById('tool-' + id);
        if (card && el) {
          setDetailCardVisibility(turn, el);
        }
      }
      for (const id of turn.thinkingIds) {
        const card = thinkingCards.get(id);
        const el = document.getElementById('thinking-' + id);
        if (card && el) {
          setDetailCardVisibility(turn, el);
        }
      }
    }

    setInterval(() => {
      updateAllTurnSummaries();
    }, 1000);

    function updatePanelWidthMode() {
      const width = (document.body ? document.body.clientWidth : 0) || window.innerWidth || 0;
      let mode = 'wide';
      if (width <= 360) mode = 'narrow';
      else if (width <= 420) mode = 'compact';
      rootEl.dataset.panelWidth = mode;
    }

    msgContainer.addEventListener('scroll', () => {
      stickToBottom = isNearBottom();
    });

    window.addEventListener('resize', () => {
      updatePanelWidthMode();
      if (!plusMenu.classList.contains('hidden')) {
        positionPlusMenu();
      }
      if (stickToBottom) {
        scrollMessagesToBottom(true);
      }
    });

    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(() => {
        updatePanelWidthMode();
        if (!plusMenu.classList.contains('hidden')) {
          positionPlusMenu();
        }
        if (stickToBottom) {
          scrollMessagesToBottom(true);
        }
      }).observe(document.body);
    }

    function addMessageEl(msg) {
      const shouldStick = captureScrollAnchor();
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
      if (msg.role === 'user') {
        finishActiveTurn();
        const turn = createTurn(msg.timestamp);
        appendToTurnContent(turn, div);
      } else {
        appendToTurnContent(getCurrentTurn(), div);
      }
      restoreScrollAnchor(shouldStick);
    }

    function renderToolCard(card) {
      const existing = document.getElementById('tool-' + card.id);
      if (existing) { updateToolCard(existing, card); return existing; }

      const shouldStick = captureScrollAnchor();
      const el = document.createElement('div');
      el.className = 'tool-card';
      el.id = 'tool-' + card.id;
      el.innerHTML = buildToolCardHtml(card);
      const turn = getCurrentTurn();
      if (turn) {
        toolCardTurns.set(card.id, turn.id);
      }
      appendToTurnDetails(turn, el, { toolId: card.id });
      restoreScrollAnchor(shouldStick);

      el.querySelector('.tool-card-header').addEventListener('click', () => {
        vscode.postMessage({ type: 'toggleToolCard', id: card.id });
      });

      const pathEl = el.querySelector('.path');
      if (pathEl) {
        pathEl.addEventListener('click', (e) => {
          const path = e.target.dataset.path;
          if (path) vscode.postMessage({ type: 'openFile', path });
        });
      }

      toolCards.set(card.id, card);
      return el;
    }

    function updateToolCard(el, card) {
      el.innerHTML = buildToolCardHtml(card);
      el.querySelector('.tool-card-header').addEventListener('click', () => {
        vscode.postMessage({ type: 'toggleToolCard', id: card.id });
      });
      const pathEl = el.querySelector('.path');
      if (pathEl) {
        pathEl.addEventListener('click', (e) => {
          const path = e.target.dataset.path;
          if (path) vscode.postMessage({ type: 'openFile', path });
        });
      }
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
        statusHtml = '<span class="status running">运行中</span>';
      }

      const inputHtml = renderToolInput(card.toolName, card.input);
      let bodyHtml = '';
      if (!card.collapsed) {
        if (card.result !== null) {
          bodyHtml = '<div class="tool-card-body">' + renderResult(card.result, card.toolName) + '</div>';
        } else {
          bodyHtml = '<div class="tool-card-body">' + inputHtml + '</div>';
        }
      } else if (card.result !== null) {
        const preview = card.result.split('\\n')[0].substring(0, 90);
        statusHtml += '<span class="card-preview">' + escapeHtml(preview) + (card.result.length > 90 ? '…' : '') + '</span>';
      }

      return '<div class="tool-card-header">' +
        '<span class="icon">&#9881;</span>' +
        '<span class="tool-name">' + escapeHtml(card.toolName) + '</span>' +
        statusHtml +
        '<span class="' + chevron + '">▾</span>' +
        '</div>' + bodyHtml;
    }

    function formatToolInput(toolName, input) {
      if ((toolName || '').toLowerCase() === 'checklist') {
        return formatChecklistInput(input);
      }
      if (input.file_path || input.path) {
        const p = input.file_path || input.path;
        const rest = Object.entries(input).filter(([k]) => k !== 'file_path' && k !== 'path')
          .map(([k,v]) => k + ': ' + (typeof v === 'string' ? v.substring(0,80) : JSON.stringify(v)))
          .join(', ');
        return p + (rest ? '\\n' + rest : '');
      }
      return JSON.stringify(input, null, 2);
    }

    function renderToolInput(toolName, input) {
      if ((toolName || '').toLowerCase() === 'checklist') {
        return renderChecklistInput(input);
      }
      return '<div class="timeline-meta">input</div><pre>' + escapeHtml(formatToolInput(toolName, input)) + '</pre>';
    }

    function formatChecklistInput(input) {
      const action = input.action || 'unknown';
      const title = input.title ? ('title: ' + input.title + '\\n') : '';
      const items = Array.isArray(input.items) ? input.items.map((item, index) => (index + 1) + '. ' + item).join('\\n') : '';
      return 'action: ' + action + '\\n' + title + items;
    }

    function getChecklistActionLabel(action) {
      const map = {
        create: '新建清单',
        update: '更新清单',
        check: '标记完成',
        uncheck: '取消完成',
        list: '查看清单',
        clear: '清空清单',
      };
      return map[action] || String(action || 'checklist');
    }

    function renderChecklistItems(items) {
      if (!Array.isArray(items) || items.length === 0) {
        return '<div class="checklist-empty">暂无任务项</div>';
      }
      return '<ul class="checklist-list">' + items.map((item, index) => {
        const checked = !!item.checked;
        return '<li class="checklist-item' + (checked ? ' is-checked' : '') + '">' +
          '<span class="checklist-item-marker">' + (checked ? '✓' : '') + '</span>' +
          '<span class="checklist-item-index">' + (index + 1) + '.</span>' +
          '<span class="checklist-item-text">' + escapeHtml(item.text || '') + '</span>' +
          '</li>';
      }).join('') + '</ul>';
    }

    function renderChecklistPanel(data) {
      const title = escapeHtml(data.title || 'Checklist');
      const subtitle = data.subtitle ? '<div class="checklist-panel-subtitle">' + escapeHtml(data.subtitle) + '</div>' : '';
      const done = Number.isFinite(data.done) ? data.done : (Array.isArray(data.items) ? data.items.filter(item => item.checked).length : 0);
      const total = Number.isFinite(data.total) ? data.total : (Array.isArray(data.items) ? data.items.length : 0);
      const progressText = total > 0 ? (done + '/' + total) : '';
      const progressPct = total > 0 ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0;
      const progressHtml = total > 0
        ? '<div class="checklist-progress">' + progressText + '</div><div class="checklist-progress-track"><div class="checklist-progress-fill" style="width:' + progressPct + '%"></div></div>'
        : '';
      return '<div class="checklist-panel">' +
        '<div class="checklist-panel-header">' +
        '<div><div class="checklist-panel-title">' + title + '</div>' + subtitle + '</div>' +
        (progressHtml ? '<div style="min-width:64px">' + progressHtml + '</div>' : '') +
        '</div>' +
        renderChecklistItems(data.items || []) +
        '</div>';
    }

    function parseChecklistResultBlocks(text) {
      const lines = String(text).split('\\n');
      const blocks = [];
      let current = null;

      for (const rawLine of lines) {
        const line = rawLine.trim();
        if (!line) continue;
        const titleMatch = line.match(/^📋\\s+(.+)$/);
        if (titleMatch) {
          if (current) blocks.push(current);
          current = { title: titleMatch[1], items: [], done: 0, total: 0 };
          continue;
        }
        const itemMatch = line.match(/^(✅|◻)\\s+(\\d+)\\.\\s+(.+)$/);
        if (itemMatch && current) {
          current.items.push({
            text: itemMatch[3],
            checked: itemMatch[1] === '✅',
          });
          continue;
        }
        const progressMatch = line.match(/^\\((\\d+)\\/(\\d+)\\s+completed\\)$/i);
        if (progressMatch && current) {
          current.done = parseInt(progressMatch[1], 10);
          current.total = parseInt(progressMatch[2], 10);
        }
      }

      if (current) blocks.push(current);
      return blocks;
    }

    function renderChecklistInput(input) {
      const action = String(input.action || 'unknown');
      const items = Array.isArray(input.items) ? input.items.map(item => ({ text: String(item), checked: false })) : [];
      const metaParts = [
        '<span class="checklist-badge">' + escapeHtml(getChecklistActionLabel(action)) + '</span>',
      ];
      if (input.checklist_id) {
        metaParts.push('<span class="checklist-badge subtle">ID ' + escapeHtml(String(input.checklist_id)) + '</span>');
      }
      if (Number.isInteger(input.item)) {
        metaParts.push('<span class="checklist-badge subtle">第 ' + escapeHtml(String(input.item)) + ' 项</span>');
      }
      if (Array.isArray(input.remove_items) && input.remove_items.length > 0) {
        metaParts.push('<span class="checklist-badge subtle">移除 ' + escapeHtml(input.remove_items.join(', ')) + '</span>');
      }

      const panelNeeded = !!input.title || items.length > 0;
      const panel = panelNeeded
        ? renderChecklistPanel({
            title: input.title || 'Checklist',
            subtitle: action === 'create' ? '即将创建' : getChecklistActionLabel(action),
            items,
            done: 0,
            total: items.length,
          })
        : '';

      return '<div class="checklist-stack">' +
        '<div class="checklist-meta-row">' + metaParts.join('') + '</div>' +
        panel +
        '</div>';
    }

    function renderResult(text, toolName) {
      if ((toolName || '').toLowerCase() === 'checklist') {
        return renderChecklistResult(text);
      }
      if (text.startsWith('---') || text.startsWith('diff --git') || text.includes('\\n+++')) {
        return '<div class="timeline-meta">result</div><pre>' + text.split('\\n').map(line => {
          if (line.startsWith('+++') || line.startsWith('+')) return '<span class="diff-line-add">' + escapeHtml(line) + '</span>';
          if (line.startsWith('---') || line.startsWith('-')) return '<span class="diff-line-del">' + escapeHtml(line) + '</span>';
          if (line.startsWith('@@')) return '<span class="diff-line-ctx">' + escapeHtml(line) + '</span>';
          return escapeHtml(line);
        }).join('\\n') + '</pre>';
      }
      return '<div class="timeline-meta">result</div><pre>' + escapeHtml(text) + '</pre>';
    }

    function renderChecklistResult(text) {
      const blocks = parseChecklistResultBlocks(text);
      if (blocks.length === 0) {
        return '<div class="timeline-meta">result</div><pre>' + escapeHtml(text) + '</pre>';
      }
      const firstBlockIndex = String(text).split('\\n').findIndex(line => /^(\\s*)📋\\s+/.test(line));
      const prefix = firstBlockIndex > 0
        ? String(text).split('\\n').slice(0, firstBlockIndex).map(line => line.trim()).filter(Boolean).join(' ')
        : '';
      const panels = blocks.map(block => renderChecklistPanel({
        title: block.title,
        subtitle: '当前进度',
        items: block.items,
        done: block.done,
        total: block.total || block.items.length,
      })).join('');
      return '<div class="checklist-stack">' +
        '<div class="checklist-meta-row">' +
        '<span class="checklist-badge good">Checklist</span>' +
        (prefix ? '<span class="checklist-caption">' + escapeHtml(prefix) + '</span>' : '') +
        '</div>' +
        panels +
        '</div>';
    }

    function addFileChangePill(payload) {
      const shouldStick = captureScrollAnchor();
      const div = document.createElement('div');
      div.className = 'file-change';
      const actionClass = payload.action; // create | modify | delete
      const shortPath = payload.path.split('/').pop() || payload.path;
      div.innerHTML =
        '<span class="action ' + actionClass + '">' + escapeHtml(payload.action) + '</span>' +
        '<span class="path" data-path="' + escapeHtml(payload.path) + '" title="' + escapeHtml(payload.path) + '">' + escapeHtml(shortPath) + '</span>';
      appendToTurnContent(getCurrentTurn(), div);
      restoreScrollAnchor(shouldStick);

      div.querySelector('.path').addEventListener('click', () => {
        vscode.postMessage({ type: 'openFile', path: payload.path });
      });
    }

    function renderHistoryItem(item) {
      if (item.kind === 'message') {
        addMessageEl(item.message);
      } else if (item.kind === 'tool') {
        renderToolCard(item.card);
      } else if (item.kind === 'thinking') {
        renderThinkingCard(item.card);
      } else if (item.kind === 'choice') {
        renderChoiceCard(item.card);
      } else if (item.kind === 'permission') {
        renderPermissionCard(item.card);
      } else if (item.kind === 'fileChange') {
        addFileChangePill(item.payload);
      }
    }

    function renderChoiceCard(card) {
      const existing = document.getElementById('choice-' + card.id);
      if (existing) { updateChoiceCard(existing, card); return existing; }

      const shouldStick = captureScrollAnchor();
      const el = document.createElement('div');
      el.className = 'request-card';
      el.id = 'choice-' + card.id;
      el.innerHTML = buildChoiceCardHtml(card);
      appendToTurnContent(getCurrentTurn(), el);
      restoreScrollAnchor(shouldStick);
      bindChoiceCardEvents(el, card);
      choiceCards.set(card.id, card);
      return el;
    }

    function updateChoiceCard(el, card) {
      el.innerHTML = buildChoiceCardHtml(card);
      bindChoiceCardEvents(el, card);
      choiceCards.set(card.id, card);
    }

    function bindChoiceCardEvents(el, card) {
      el.querySelectorAll('[data-choice-option]').forEach(btn => {
        btn.addEventListener('click', () => {
          if (card.completed) return;
          const selected = btn.dataset.choiceOption;
          if (!selected) return;
          if (card.multiple) {
            const set = new Set(card.pendingSelected || []);
            if (set.has(selected)) set.delete(selected); else set.add(selected);
            card.pendingSelected = Array.from(set);
            updateChoiceCard(el, card);
            return;
          }
          vscode.postMessage({ type: 'respondToChoice', id: card.id, selected: [selected], cancelled: false });
        });
      });
      const submitBtn = el.querySelector('[data-choice-submit]');
      if (submitBtn) {
        submitBtn.addEventListener('click', () => {
          if (card.completed) return;
          vscode.postMessage({ type: 'respondToChoice', id: card.id, selected: card.pendingSelected || [], cancelled: false });
        });
      }
      const cancelBtn = el.querySelector('[data-choice-cancel]');
      if (cancelBtn) {
        cancelBtn.addEventListener('click', () => {
          if (card.completed) return;
          vscode.postMessage({ type: 'respondToChoice', id: card.id, selected: [], cancelled: true });
        });
      }
      const clearBtn = el.querySelector('[data-choice-clear]');
      if (clearBtn) {
        clearBtn.addEventListener('click', () => {
          if (card.completed) return;
          card.pendingSelected = [];
          updateChoiceCard(el, card);
        });
      }
    }

    function buildChoiceCardHtml(card) {
      const selectedValues = card.completed ? card.selected : (card.pendingSelected || []);
      const state = card.completed
        ? (card.cancelled ? '<span class="request-status cancelled">已取消</span>' : '<span class="request-status done">已选择</span>')
        : '<span class="request-status pending">等待选择</span>';
      const options = card.options.map(option => {
        const selected = selectedValues.includes(option) ? ' selected' : '';
        const disabled = card.completed ? ' disabled' : '';
        return '<button class="request-option' + selected + '" data-choice-option="' + escapeAttr(option) + '"' + disabled + '>' + escapeHtml(option) + '</button>';
      }).join('');
      const summary = (card.completed ? card.selected.length > 0 : selectedValues.length > 0)
        ? '<div class="request-summary">已选：' + escapeHtml((card.completed ? card.selected : selectedValues).join(', ')) + '</div>'
        : '';
      const actions = card.completed
        ? ''
        : (card.multiple
          ? '<button class="request-action primary" data-choice-submit' + (selectedValues.length === 0 ? ' disabled' : '') + '>确认选择</button><button class="request-action subtle" data-choice-clear>清空</button><button class="request-action subtle" data-choice-cancel>取消</button>'
          : '<button class="request-action subtle" data-choice-cancel>取消</button>');
      const meta = card.multiple ? '<div class="timeline-meta">可多选，选择后点“确认选择”</div>' : '';
      return '<div class="request-card-header">' +
        '<span class="icon">?</span>' +
        '<span class="request-title">选择</span>' +
        state +
        '</div>' +
        '<div class="request-card-body">' +
        meta +
        '<div class="request-question">' + escapeHtml(card.question) + '</div>' +
        '<div class="request-options">' + options + '</div>' +
        summary +
        '<div class="request-actions">' + actions + '</div>' +
        '</div>';
    }

    function renderPermissionCard(card) {
      const existing = document.getElementById('permission-' + card.id);
      if (existing) { updatePermissionCard(existing, card); return existing; }

      const shouldStick = captureScrollAnchor();
      const el = document.createElement('div');
      el.className = 'request-card permission';
      el.id = 'permission-' + card.id;
      el.innerHTML = buildPermissionCardHtml(card);
      appendToTurnContent(getCurrentTurn(), el);
      restoreScrollAnchor(shouldStick);
      bindPermissionCardEvents(el, card);
      permissionCards.set(card.id, card);
      return el;
    }

    function updatePermissionCard(el, card) {
      el.innerHTML = buildPermissionCardHtml(card);
      bindPermissionCardEvents(el, card);
      permissionCards.set(card.id, card);
    }

    function bindPermissionCardEvents(el, card) {
      const allowBtn = el.querySelector('[data-permission-allow]');
      if (allowBtn) {
        allowBtn.addEventListener('click', () => {
          if (card.allowed !== null) return;
          vscode.postMessage({ type: 'respondToPermission', id: card.id, allowed: true, alwaysAllow: false });
        });
      }
      const denyBtn = el.querySelector('[data-permission-deny]');
      if (denyBtn) {
        denyBtn.addEventListener('click', () => {
          if (card.allowed !== null) return;
          vscode.postMessage({ type: 'respondToPermission', id: card.id, allowed: false, alwaysAllow: false });
        });
      }
    }

    function buildPermissionCardHtml(card) {
      const state = card.allowed === null
        ? '<span class="request-status pending">等待确认</span>'
        : (card.allowed ? '<span class="request-status done">已允许</span>' : '<span class="request-status denied">已拒绝</span>');
      const actions = card.allowed === null
        ? '<button class="request-action primary" data-permission-allow>允许</button><button class="request-action subtle" data-permission-deny>拒绝</button>'
        : '';
      const reason = card.reason ? '<div class="request-reason">' + escapeHtml(card.reason) + '</div>' : '';
      return '<div class="request-card-header">' +
        '<span class="icon">⚡</span>' +
        '<span class="request-title">工具权限 · ' + escapeHtml(card.toolName) + '</span>' +
        state +
        '</div>' +
        '<div class="request-card-body">' +
        '<div class="timeline-meta">permission request</div>' +
        reason +
        '<pre class="request-pre">' + escapeHtml(formatToolInput(card.toolName, card.input)) + '</pre>' +
        '<div class="request-actions">' + actions + '</div>' +
        '</div>';
    }

    // ── Thinking cards ────────────────────────────────────────────

    function renderThinkingCard(card) {
      const existing = document.getElementById('thinking-' + card.id);
      if (existing) { updateThinkingCard(existing, card); return; }

      const shouldStick = captureScrollAnchor();
      const el = document.createElement('div');
      el.className = 'thinking-card';
      el.id = 'thinking-' + card.id;
      el.innerHTML = buildThinkingCardHtml(card);
      const turn = getCurrentTurn();
      if (turn) {
        thinkingCardTurns.set(card.id, turn.id);
      }
      appendToTurnDetails(turn, el, { thinkingId: card.id, forceExpanded: !card.collapsed });
      restoreScrollAnchor(shouldStick);

      el.querySelector('.thinking-card-header').addEventListener('click', () => {
        vscode.postMessage({ type: 'toggleThinkingCard', id: card.id });
      });

      thinkingCards.set(card.id, card);
    }

    function updateThinkingCard(el, card) {
      el.innerHTML = buildThinkingCardHtml(card);
      el.querySelector('.thinking-card-header').addEventListener('click', () => {
        vscode.postMessage({ type: 'toggleThinkingCard', id: card.id });
      });
      thinkingCards.set(card.id, card);
    }

    function buildThinkingCardHtml(card) {
      const chevron = card.collapsed ? 'chevron collapsed' : 'chevron';
      const bodyHidden = card.collapsed ? ' hidden' : '';
      const preview = card.collapsed ? card.text.split('\\n')[0].substring(0, 60) : '';
      const previewHtml = card.collapsed
        ? '<span class="card-preview">' + escapeHtml(preview) + (card.text.length > 60 ? '…' : '') + '</span>'
        : '';

      return '<div class="thinking-card-header">' +
        '<span class="icon">💭</span>' +
        '<span class="label">思考</span>' +
        previewHtml +
        '<span class="' + chevron + '">▾</span>' +
        '</div>' +
        '<div class="thinking-card-body' + bodyHidden + '"><pre>' + escapeHtml(card.text) + '</pre></div>';
    }

    // ── Busy indicator ────────────────────────────────────────────

    function setBusyState(busy) {
      isBusy = busy;
      if (busyIndicator) {
        busyIndicator.classList.toggle('visible', busy);
      }
      if (!busy) {
        finishActiveTurn();
      } else if (activeTurn) {
        activeTurn.status = 'running';
        activeTurn.endTime = null;
        updateTurnSummary(activeTurn);
      }
      if (busy && busyLabel) {
        busyLabel.textContent = 'CrabCode 正在处理';
      }
      if (sendBtn) {
        sendBtn.innerHTML = busy ? STOP_ICON_HTML : SEND_ICON_HTML;
        sendBtn.title = busy ? '中断当前会话' : '发送 (⌘↵ / Ctrl+Enter)';
        sendBtn.setAttribute('aria-label', busy ? '中断当前会话' : '发送');
      }
    }

    function updateBusyLabel() {
      if (!busyLabel || !isBusy) return;
      // Show contextual label based on current activity
      const activeTool = [...toolCards.values()].find(c => c.result === null);
      if (activeTool) {
        busyLabel.textContent = '正在运行 ' + activeTool.toolName + '…';
      } else if ([...thinkingCards.values()].some(c => !c.collapsed)) {
        busyLabel.textContent = 'CrabCode 正在思考…';
      } else {
        busyLabel.textContent = 'CrabCode 正在处理…';
      }
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

    function renderContextUsage(usage) {
      if (!contextMeter) return;
      if (!usage) {
        contextMeter.hidden = true;
        return;
      }

      const percent = Math.max(0, Math.min(100, Number(usage.usedPercent) || 0));
      const details = Array.isArray(usage.details) ? usage.details : [];
      const title = details[0] || '背景信息窗口：';
      const usageLine = details[1] || '用量未知';
      const detailLine = details[2] || '';

      contextMeter.hidden = false;
      contextMeter.style.setProperty('--ctx-progress', percent.toFixed(2) + '%');
      contextMeter.classList.toggle('is-warn', percent >= 70 && percent < 90);
      contextMeter.classList.toggle('is-danger', percent >= 90);
      contextMeter.setAttribute('aria-label', [title, usageLine, detailLine].filter(Boolean).join(' '));

      if (contextTooltip) {
        contextTooltip.innerHTML =
          '<div class="context-tooltip-title">' + escapeHtml(title) + '</div>' +
          '<div class="context-tooltip-usage">' + escapeHtml(usageLine) + '</div>' +
          (detailLine ? '<div class="context-tooltip-detail">' + escapeHtml(detailLine) + '</div>' : '');
      }
    }

    function renderPendingEdits(summary) {
      if (!pendingEditsBar) return;
      const files = summary && Array.isArray(summary.files) ? summary.files : [];
      if (!summary || files.length === 0) {
        currentPendingEditSummary = null;
        pendingEditsCollapsed = false;
        pendingEditsBar.classList.add('hidden');
        pendingEditsBar.innerHTML = '';
        return;
      }

      currentPendingEditSummary = summary;
      const title = summary.totalFiles + (summary.totalFiles === 1 ? ' File' : ' Files');
      const toggleTitle = pendingEditsCollapsed ? '展开待审核文件' : '折叠待审核文件';
      const rows = files.map(function(file) {
        const stats = '<span>+' + escapeHtml(String(file.added || 0)) + '</span> ' +
          '<span class="removed">-' + escapeHtml(String(file.removed || 0)) + '</span>';
        return '<div class="pending-edit-row">' +
          '<span class="pending-edit-icon">' + (file.action === 'create' ? '+' : '↔') + '</span>' +
          '<button type="button" class="pending-edit-name" data-pending-action="reviewFile" data-change-id="' + escapeAttr(file.id) + '" title="' + escapeAttr(file.path) + '">' + escapeHtml(file.shortPath || file.path) + '</button>' +
          '<span class="pending-edit-stats">' + stats + '</span>' +
          '<span class="pending-edit-actions">' +
            '<button type="button" class="pending-edit-btn" data-pending-action="undoFile" data-change-id="' + escapeAttr(file.id) + '">Undo</button>' +
            '<button type="button" class="pending-edit-btn primary" data-pending-action="keepFile" data-change-id="' + escapeAttr(file.id) + '">Keep</button>' +
          '</span>' +
        '</div>';
      }).join('');

      pendingEditsBar.classList.remove('hidden');
      pendingEditsBar.classList.toggle('is-collapsed', pendingEditsCollapsed);
      pendingEditsBar.style.setProperty('--pending-edit-list-max-height', (pendingEditsVisibleFiles * 28) + 'px');
      pendingEditsBar.innerHTML =
        '<div class="pending-edits-header">' +
          '<button type="button" class="pending-edits-title" data-pending-toggle aria-expanded="' + (pendingEditsCollapsed ? 'false' : 'true') + '" title="' + toggleTitle + '">' +
            '<span class="chevron">⌄</span><span>' + title + '</span>' +
          '</button>' +
          '<div class="pending-edits-actions">' +
            '<button type="button" class="pending-edit-btn" data-pending-action="undoAll">Undo</button>' +
            '<button type="button" class="pending-edit-btn primary" data-pending-action="keepAll">Keep</button>' +
            '<button type="button" class="pending-edit-btn" data-pending-action="reviewAll">Review</button>' +
          '</div>' +
        '</div>' +
        '<div class="pending-edits-list">' + rows + '</div>';

      const toggle = pendingEditsBar.querySelector('[data-pending-toggle]');
      if (toggle) {
        toggle.addEventListener('click', function() {
          pendingEditsCollapsed = !pendingEditsCollapsed;
          renderPendingEdits(currentPendingEditSummary);
        });
      }

      pendingEditsBar.querySelectorAll('[data-pending-action]').forEach(function(btn) {
        btn.addEventListener('click', function() {
          vscode.postMessage({
            type: 'pendingEditAction',
            action: btn.getAttribute('data-pending-action'),
            changeId: btn.getAttribute('data-change-id') || undefined,
          });
        });
      });
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
      hasReceivedOptions = true;
      const models = msg.models || [];
      const previousValue = modelSelect.value;
      modelSelect.innerHTML = '';
      if (models.length === 0) {
        const o = document.createElement('option');
        o.value = '';
        o.textContent = msg.connected ? '（网关未返回可用模型）' : '（正在连接网关…）';
        o.disabled = true;
        modelSelect.appendChild(o);
        modelSelect.disabled = true;
        if (modelSelectLabel) {
          modelSelectLabel.textContent = o.textContent;
          modelSelectLabel.title = o.textContent;
        }
        if (modelSelectWrap) modelSelectWrap.classList.add('is-empty');
      } else {
        models.forEach(function(m) {
          const o = document.createElement('option');
          o.value = m;
          o.textContent = m;
          modelSelect.appendChild(o);
        });
        const preferred = [
          msg.selectedModel,
          msg.defaultModel,
          previousValue,
          models[0],
        ].find(function(v) {
          return !!v && models.indexOf(v) >= 0;
        }) || models[0];
        modelSelect.disabled = false;
        modelSelect.value = preferred;
        if (modelSelect.selectedIndex < 0 && modelSelect.options.length > 0) {
          modelSelect.selectedIndex = 0;
        }
        const selectedOption = modelSelect.selectedIndex >= 0 ? modelSelect.options[modelSelect.selectedIndex] : null;
        const selectedText = (selectedOption ? selectedOption.textContent : '') || preferred;
        if (modelSelectLabel) {
          modelSelectLabel.textContent = selectedText;
          modelSelectLabel.title = selectedText;
        }
        if (modelSelectWrap) modelSelectWrap.classList.remove('is-empty');
      }
      permissionSelect.value = msg.permissionMode === 'run_everything' ? 'run_everything' : 'default';
      const nextPendingEditsVisibleFiles = normalizePendingEditsVisibleFiles(msg.pendingEditsVisibleFiles);
      if (nextPendingEditsVisibleFiles !== pendingEditsVisibleFiles) {
        pendingEditsVisibleFiles = nextPendingEditsVisibleFiles;
        if (currentPendingEditSummary) renderPendingEdits(currentPendingEditSummary);
      }
    }

    // ── Send ─────────────────────────────────────────────────────

    function send() {
      if (isBusy) {
        vscode.postMessage({ type: 'interrupt' });
        return;
      }
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

    function positionPlusMenu() {
      plusMenu.classList.remove('hidden');
      plusMenu.style.visibility = 'hidden';
      plusMenu.style.left = '0px';
      plusMenu.style.top = '0px';
      const btnRect = plusBtn.getBoundingClientRect();
      const cardRect = composerCard.getBoundingClientRect();
      const menuRect = plusMenu.getBoundingClientRect();
      const margin = 8;
      const gap = 6;
      const maxLeft = Math.max(margin, window.innerWidth - menuRect.width - margin);
      const maxTop = Math.max(margin, window.innerHeight - menuRect.height - margin);
      const left = Math.min(Math.max(btnRect.left, margin), maxLeft);
      const top = Math.min(Math.max(cardRect.top - menuRect.height - gap, margin), maxTop);
      plusMenu.style.left = left + 'px';
      plusMenu.style.top = top + 'px';
      plusMenu.style.visibility = '';
    }

    function openPlusMenu() {
      positionPlusMenu();
      plusBtn.setAttribute('aria-expanded', 'true');
    }

    function closePlusMenu() {
      plusMenu.classList.add('hidden');
      plusMenu.style.visibility = '';
      plusBtn.setAttribute('aria-expanded', 'false');
    }

    plusBtn.addEventListener('click', function(e) {
      e.stopPropagation();
      if (plusMenu.classList.contains('hidden')) openPlusMenu();
      else closePlusMenu();
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
      const selectedOption = modelSelect.selectedIndex >= 0 ? modelSelect.options[modelSelect.selectedIndex] : null;
      const selectedText = (selectedOption ? selectedOption.textContent : '') || modelSelect.value || '';
      if (modelSelectLabel) {
        modelSelectLabel.textContent = selectedText;
        modelSelectLabel.title = selectedText;
      }
      if (modelSelectWrap) modelSelectWrap.classList.toggle('is-empty', !selectedText);
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
      try {
      switch (msg.type) {
        case 'newMessage':
          addMessageEl(msg.message);
          break;
        case 'appendText': {
          const el = document.getElementById('msg-' + msg.id);
          if (el) {
            const textEl = el.querySelector('.text');
            textEl.textContent += msg.chunk;
            scrollMessagesToBottom();
          }
          break;
        }
        case 'history':
          msgContainer.innerHTML = '';
          toolCards.clear();
          toolCardTurns.clear();
          thinkingCards.clear();
          thinkingCardTurns.clear();
          choiceCards.clear();
          permissionCards.clear();
          turns.length = 0;
          activeTurn = null;
          turnCounter = 0;
          (msg.items || []).forEach(renderHistoryItem);
          finishActiveTurn();
          updateBusyLabel();
          updateAllTurnSummaries();
          scrollMessagesToBottom(true);
          break;
        case 'prefill':
          input.value = msg.text;
          input.focus();
          break;
        case 'toolUse':
          renderToolCard(msg.card);
          updateBusyLabel();
          updateAllTurnSummaries();
          break;
        case 'choiceRequest':
          renderChoiceCard(msg.card);
          updateBusyLabel();
          updateAllTurnSummaries();
          break;
        case 'choiceResolved': {
          const card = msg.card;
          choiceCards.set(card.id, card);
          const el = document.getElementById('choice-' + card.id);
          if (el) updateChoiceCard(el, card);
          else renderChoiceCard(card);
          updateBusyLabel();
          updateAllTurnSummaries();
          break;
        }
        case 'permissionRequest':
          renderPermissionCard(msg.card);
          updateBusyLabel();
          updateAllTurnSummaries();
          break;
        case 'permissionResolved': {
          const card = msg.card;
          permissionCards.set(card.id, card);
          const el = document.getElementById('permission-' + card.id);
          if (el) updatePermissionCard(el, card);
          else renderPermissionCard(card);
          updateBusyLabel();
          updateAllTurnSummaries();
          break;
        }
        case 'toolResult': {
          const el = renderToolCard(msg.card);
          if (msg.card && msg.card.isError && el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
          }
          const turn = getTurnById(toolCardTurns.get(msg.card ? msg.card.id : undefined));
          if (turn) {
            updateTurnSummary(turn);
          }
          updateBusyLabel();
          updateAllTurnSummaries();
          break;
        }
        case 'toggleToolCard': {
          const card = toolCards.get(msg.id);
          if (card) {
            card.collapsed = msg.collapsed;
            const el = document.getElementById('tool-' + msg.id);
            if (el) updateToolCard(el, card);
            const turn = getTurnById(toolCardTurns.get(msg.id));
            if (turn) {
              updateTurnSummary(turn);
            }
          }
          break;
        }
        case 'thinkingStart':
          renderThinkingCard(msg.card);
          updateBusyLabel();
          updateAllTurnSummaries();
          break;
        case 'appendThinking': {
          const tc = thinkingCards.get(msg.id);
          if (tc) {
            tc.text += msg.chunk;
            const el = document.getElementById('thinking-' + msg.id);
            if (el) {
              const body = el.querySelector('.thinking-card-body pre');
              if (body) body.textContent += msg.chunk;
            }
            scrollMessagesToBottom();
          }
          updateBusyLabel();
          updateAllTurnSummaries();
          break;
        }
        case 'thinkingEnd': {
          const tc2 = thinkingCards.get(msg.id);
          if (tc2) {
            tc2.collapsed = msg.collapsed;
            const el = document.getElementById('thinking-' + msg.id);
            if (el) updateThinkingCard(el, tc2);
            const turn = getTurnById(thinkingCardTurns.get(msg.id));
            if (turn) {
              updateTurnSummary(turn);
            }
          }
          updateBusyLabel();
          updateAllTurnSummaries();
          break;
        }
        case 'toggleThinkingCard': {
          const tc3 = thinkingCards.get(msg.id);
          if (tc3) {
            tc3.collapsed = msg.collapsed;
            const el = document.getElementById('thinking-' + msg.id);
            if (el) updateThinkingCard(el, tc3);
            const turn = getTurnById(thinkingCardTurns.get(msg.id));
            if (turn) {
              updateTurnSummary(turn);
            }
          }
          break;
        }
        case 'busyState':
          setBusyState(msg.busy);
          break;
        case 'contextUsage':
          renderContextUsage(msg.usage);
          break;
        case 'pendingEditReview':
          renderPendingEdits(msg.summary);
          break;
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
      } catch (error) {
        console.error(error);
      }
    });

    syncComposerChrome();
    updatePanelWidthMode();

    vscode.postMessage({ type: 'webviewReady' });
    vscode.postMessage({ type: 'requestHistory' });
    vscode.postMessage({ type: 'requestOptions' });
    let optionsRetryCount = 0;
    const optionsRetryTimer = setInterval(() => {
      if (hasReceivedOptions && modelSelect.options.length > 0 && !modelSelect.options[0].disabled) {
        clearInterval(optionsRetryTimer);
        return;
      }
      if (optionsRetryCount >= 20) {
        clearInterval(optionsRetryTimer);
        return;
      }
      optionsRetryCount += 1;
      vscode.postMessage({ type: 'requestOptions' });
    }, 500);
    } catch (error) {
      console.error(error);
    }
    })();
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
