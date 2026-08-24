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
import * as os from "os";
import * as path from "path";
import type { CrabCodeConnection, SessionLaunchOverrides } from "./connection";
import {
  buildChoiceResponseCommand,
  buildPermissionResponseCommand,
  serializeCommand,
  type PermissionMode,
} from "./client/protocol";
import type {
  AgentOutputPayload,
  AgentStatePayload,
  AgentInfo,
  AgentTranscriptResponse,
  BackgroundTaskInfo,
  ChoiceResponsePayload,
  ChoiceRequestPayload,
  CompactPayload,
  EventPayload,
  FileChangePayload,
  ImageAttachment,
  LogsResponse,
  PermissionResponsePayload,
  PermissionRequestPayload,
  PeerMessagePayload,
  PeerInfo,
  PlanReadyPayload,
  ScheduleRunPayload,
  ScheduleCreateRequest,
  ScheduleJobInfo,
  ScheduleRunInfo,
  SessionMessagePayload,
  SessionHistoryPayload,
  SessionRuntimeStatus,
  SnapshotPayload,
  StreamModePayload,
  SteeringAppliedPayload,
  TaskUpdatePayload,
  TeamMessagePayload,
  TeamMessageInfo,
  TeamBridgeInfo,
  CrossTeamMessageInfo,
  TeamStatePayload,
  TeamStatusInfo,
  TeamTaskInfo,
  ToolResultPayload,
  ToolUsePayload,
  TurnCompletePayload,
  RevertPayload,
} from "./client/types";

interface GatewayModelInfo {
  name: string;
  description?: string;
  group?: string;
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

function normalizeSessionLaunchOverrides(value: unknown): SessionLaunchOverrides | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const source = value as Record<string, unknown>;
  const result: SessionLaunchOverrides = {};
  for (const key of ["model", "provider", "base_url", "api_format", "model_profile"] as const) {
    const item = source[key];
    if (typeof item === "string" && item.length > 0) result[key] = item;
  }
  return Object.keys(result).length > 0 ? result : undefined;
}

function resolveLocalPath(value: string, baseDirectory?: string): string {
  const expanded = value === "~"
    ? os.homedir()
    : (value.startsWith("~/") || value.startsWith("~\\"))
      ? path.join(os.homedir(), value.slice(2))
      : value;
  return path.isAbsolute(expanded)
    ? path.normalize(expanded)
    : path.resolve(baseDirectory ?? process.cwd(), expanded);
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

function buildCacheUsageDetail(payload: TurnCompletePayload): string | null {
  const usage = payload.usage;
  if (!usage || (!("cache_read_tokens" in usage) && !("cache_write_tokens" in usage))) {
    return null;
  }
  const tokenValue = (key: string): number => {
    const value = Number(usage[key]);
    return Number.isFinite(value) ? Math.max(0, Math.trunc(value)) : 0;
  };
  const cacheRead = tokenValue("cache_read_tokens");
  const totalInput = tokenValue("total_input_tokens") || tokenValue("input_tokens");
  const hitRate = totalInput ? cacheRead / totalInput * 100 : 0;
  const parts = [
    `缓存命中 ${formatPercent(hitRate)}`,
    `读取 ${formatTokenCount(cacheRead)}`,
  ];
  if ("cache_write_tokens" in usage) {
    parts.push(`写入 ${formatTokenCount(tokenValue("cache_write_tokens"))}`);
  }
  return parts.join(" · ");
}

function normalizePendingEditsVisibleFiles(value: unknown): number {
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed)) return 5;
  return Math.min(50, Math.max(1, Math.floor(parsed)));
}

function normalizePermissionMode(value: unknown): PermissionMode {
  if (value === "ask" || value === "ai_review" || value === "run_everything") {
    return value;
  }
  return "default";
}

interface ContextUsageStatus {
  usedTokens: number;
  windowTokens: number;
  remainingTokens: number;
  usedPercent: number;
  remainingPercent: number;
  cacheDetail?: string;
  details: string[];
}

function buildContextUsageStatus(payload: TurnCompletePayload): ContextUsageStatus | null {
  const used = Math.max(0, Math.trunc(payload.context_used_tokens ?? 0));
  const window = Math.max(0, Math.trunc(payload.context_window_tokens ?? 0));
  if (!used && !window) return null;
  if (!window) {
    const cacheDetail = buildCacheUsageDetail(payload);
    return {
      usedTokens: used,
      windowTokens: 0,
      remainingTokens: 0,
      usedPercent: 0,
      remainingPercent: 0,
      cacheDetail: cacheDetail ?? undefined,
      details: [
        "背景信息窗口：",
        `已用 ${formatTokenCount(used)} 标记`,
        cacheDetail ? `总量未知 · ${cacheDetail}` : "总量未知",
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
  const tokenDetail = `已用 ${formatTokenCount(used)} 标记，共 ${formatTokenCount(window)}`;
  const cacheDetail = buildCacheUsageDetail(payload);
  return {
    usedTokens: used,
    windowTokens: window,
    remainingTokens,
    usedPercent,
    remainingPercent,
    cacheDetail: cacheDetail ?? undefined,
    details: [
      "背景信息窗口：",
      `${formatPercent(usedPercent)} 已用（剩余 ${formatPercent(remainingPercent)}）`,
      cacheDetail ? `${tokenDetail} · ${cacheDetail}` : tokenDetail,
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
  parentId?: string | null;
  origin?: string | null;
  usage?: Record<string, unknown> | null;
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
  agentId?: string | null;
}

export interface PermissionCard {
  id: string;
  toolName: string;
  input: Record<string, unknown>;
  reason: string | null;
  allowed: boolean | null;
  agentId?: string | null;
  requestKind?: "tool" | "peer_message";
}

export interface PlanCard {
  id: string;
  plan: Record<string, unknown>;
  status: "pending" | "executing" | "revising" | "cancelled";
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
  | { kind: "plan"; card: PlanCard }
  | { kind: "fileChange"; payload: FileChangePayload };

interface SessionState {
  messages: ChatMessage[];
  history: HistoryItem[];
  toolCards: Map<string, ToolCard>;
  thinkingCards: Map<string, ThinkingCard>;
  choiceCards: Map<string, ChoiceCard>;
  permissionCards: Map<string, PermissionCard>;
  planCards: Map<string, PlanCard>;
  activeThinkingId: string | null;
  activeOperationId: string | null;
  isBusy: boolean;
  contextUsage: ContextUsageStatus | null;
  batchDenied: boolean;
  mode: "agent" | "plan";
  pendingSteeringMessages: ChatMessage[];
}

function createEmptySessionState(): SessionState {
  return {
    messages: [],
    history: [],
    toolCards: new Map(),
    thinkingCards: new Map(),
    choiceCards: new Map(),
    permissionCards: new Map(),
    planCards: new Map(),
    activeThinkingId: null,
    activeOperationId: null,
    isBusy: false,
    contextUsage: null,
    batchDenied: false,
    mode: "agent",
    pendingSteeringMessages: [],
  };
}

// ── Provider ──────────────────────────────────────────────────────

export class ChatPanelProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "crabcode.chatPanel";

  private view: vscode.WebviewView | undefined;
  private sessionStates = new Map<string, SessionState>();
  private displayedSessionId: string | null = null;
  private busySessions = new Set<string>();
  private interruptRetryTimer: ReturnType<typeof setTimeout> | null = null;
  private latestModelRequestId = 0;
  private _lastModelsFetchTime = 0;
  private _modelsFetchInProgress = false;
  private static readonly MODELS_FETCH_COOLDOWN_MS = 30_000;
  private lastNonEmptyModels: string[] = [];
  private lastNonEmptyModelGroups: Record<string, string> = {};
  private webviewReady = false;
  private pendingWebviewMessages: any[] = [];
  private logFollowAbort: AbortController | null = null;
  private shellTerminal: vscode.Terminal | null = null;
  private shellTerminalCwd: string | null = null;
  private pendingEditReview: PendingEditReviewSummary | null = null;
  private readonly pendingEditActionEmitter = new vscode.EventEmitter<PendingEditActionMessage>();
  public readonly onPendingEditAction = this.pendingEditActionEmitter.event;

  /** Get or create session state for a given session ID. */
  private getSessionState(sessionId: string): SessionState {
    let state = this.sessionStates.get(sessionId);
    if (!state) {
      state = createEmptySessionState();
      this.sessionStates.set(sessionId, state);
    }
    return state;
  }

  /** Get the state for the currently displayed session. */
  private get currentState(): SessionState {
    if (this.displayedSessionId) {
      return this.getSessionState(this.displayedSessionId);
    }
    return createEmptySessionState();
  }

  // Convenience accessors for the displayed session state
  private get messages(): ChatMessage[] { return this.currentState.messages; }
  private set messages(v: ChatMessage[]) { if (this.displayedSessionId) this.getSessionState(this.displayedSessionId).messages = v; }
  private get history(): HistoryItem[] { return this.currentState.history; }
  private set history(v: HistoryItem[]) { if (this.displayedSessionId) this.getSessionState(this.displayedSessionId).history = v; }
  private get toolCards(): Map<string, ToolCard> { return this.currentState.toolCards; }
  private get thinkingCards(): Map<string, ThinkingCard> { return this.currentState.thinkingCards; }
  private get choiceCards(): Map<string, ChoiceCard> { return this.currentState.choiceCards; }
  private get permissionCards(): Map<string, PermissionCard> { return this.currentState.permissionCards; }
  private get planCards(): Map<string, PlanCard> { return this.currentState.planCards; }
  private get activeThinkingId(): string | null { return this.currentState.activeThinkingId; }
  private set activeThinkingId(v: string | null) { if (this.displayedSessionId) this.getSessionState(this.displayedSessionId).activeThinkingId = v; }
  private get isBusy(): boolean { return this.currentState.isBusy; }
  private set isBusy(v: boolean) { if (this.displayedSessionId) this.getSessionState(this.displayedSessionId).isBusy = v; }
  private get latestContextUsage(): ContextUsageStatus | null { return this.currentState.contextUsage; }
  private set latestContextUsage(v: ContextUsageStatus | null) { if (this.displayedSessionId) this.getSessionState(this.displayedSessionId).contextUsage = v; }

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly connection: CrabCodeConnection,
    private readonly outputChannel?: vscode.OutputChannel,
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
          this.postMessage({ type: "contextUsage", usage: this.latestContextUsage ?? null });
          this.postMessage({ type: "pendingEditReview", summary: this.pendingEditReview });
          this.postMessage({
            type: "steeringQueue",
            messages: this.currentState.pendingSteeringMessages,
          });
          break;
        case "setModel":
          if (typeof msg.name === "string" && msg.name.length > 0) {
            this.connection.sendSwitchModel(
              msg.name,
              this.displayedSessionId ?? this.connection.sessionId ?? undefined,
            );
            void vscode.workspace
              .getConfiguration("crabcode")
              .update("chatModelDefault", msg.name, vscode.ConfigurationTarget.Global);
          }
          break;
        case "setPermissionMode":
          if (msg.mode === "ask" || msg.mode === "default" || msg.mode === "run_everything" || msg.mode === "ai_review") {
            const mode = normalizePermissionMode(msg.mode);
            void vscode.workspace
              .getConfiguration("crabcode")
              .update("permissionMode", mode, vscode.ConfigurationTarget.Global);
            this.connection.sendSetPermissionMode(
              mode,
              this.displayedSessionId ?? this.connection.sessionId ?? undefined,
            );
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
          this.respondToPermission(msg.id, msg.allowed, msg.alwaysAllow, msg.feedback);
          break;
        case "respondToPlan":
          this.respondToPlan(msg.id, msg.action);
          break;
        case "interrupt": {
          const sessionId = this.displayedSessionId ?? this.connection.sessionId;
          const operationId = sessionId
            ? this.getSessionState(sessionId).activeOperationId ?? undefined
            : undefined;
          const result = this.connection.sendInterrupt(sessionId, operationId);
          this.postMessage({ type: "interruptResult", result });
          if (result === "sent") {
            this.scheduleInterruptRetry(sessionId!, operationId);
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
        case "fetchSkills":
          void this.fetchAndSendSkills();
          break;
        case "compact":
          void this.triggerCompact(msg.customInstructions);
          break;
        case "invokeSkill":
          if (typeof msg.name === "string") {
            void this.invokeSkill(msg.name, typeof msg.userInput === "string" ? msg.userInput : "");
          }
          break;
        case "newSession": {
          const cwd = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? null;
          this.displayedSessionId = null; // will be set by server.connected
          this.connection.sendNewSession(
            cwd,
            normalizeSessionLaunchOverrides(msg.options),
          );
          this.postMessage({ type: "history", items: [] });
          this.postMessage({ type: "busyState", busy: false });
          this.postMessage({ type: "contextUsage", usage: null });
          this.postMessage({ type: "steeringQueue", messages: [] });
          break;
        }
        case "fetchSessions":
          void this.fetchAndSendSessions();
          break;
        case "resumeSession":
          if (typeof msg.sessionId === "string") {
            void this.resumeSession(
              msg.sessionId,
              normalizeSessionLaunchOverrides(msg.options),
            );
          }
          break;
        case "openSettings":
          void vscode.commands.executeCommand("workbench.action.openSettings", "crabcode");
          break;
        case "clearMessages":
          void this.clearSessionHistory();
          break;
        case "webviewError":
          this.outputChannel?.appendLine(`[CrabCode][WebviewError] ${msg.message}`);
          if (msg.stack) this.outputChannel?.appendLine(msg.stack);
          break;
        case "localMessage":
          this.addMessage(msg.role as ChatMessageRole, msg.text);
          break;
        case "runShellCommand":
          if (typeof msg.command === "string") this.runShellCommand(msg.command);
          break;
        case "fetchStatus": {
          void this.fetchAndShowStatus();
          break;
        }
        case "setEffort":
          void this.showOrSetReasoningEffort(
            typeof msg.effort === "string" ? msg.effort : null,
          );
          break;
        case "setUltra":
          void this.setUltraMode(
            typeof msg.enabled === "boolean" ? msg.enabled : null,
          );
          break;
        case "switchMode":
          void this.switchMode(msg.mode as "agent" | "plan");
          break;
        case "fetchRecentSessions":
          void this.fetchRecentSessions(typeof msg.limit === "number" ? msg.limit : 10);
          break;
        case "searchSessions":
          if (typeof msg.query === "string") void this.searchSessions(msg.query);
          break;
        case "archiveSession":
          if (typeof msg.sessionId === "string") void this.archiveSession(msg.sessionId);
          break;
        case "pruneSessions":
          void this.pruneSessions(
            typeof msg.days === "number" ? msg.days : 30,
            msg.deleteFiles === true,
          );
          break;
        case "exportSession":
          void this.exportSession(
            msg.format === "json" ? "json" : "md",
            typeof msg.path === "string" ? msg.path : undefined,
            typeof msg.sessionId === "string" ? msg.sessionId : undefined,
          );
          break;
        case "fetchModel":
          void this.fetchAndShowModel();
          break;
        case "fetchStats":
          void this.fetchStats();
          break;
        case "createCheckpoint":
          void this.createCheckpoint(typeof msg.label === "string" ? msg.label : "");
          break;
        case "fetchCheckpoints":
          void this.fetchCheckpoints();
          break;
        case "rollbackCheckpoint":
          if (typeof msg.checkpointId === "string") void this.rollbackCheckpoint(msg.checkpointId);
          break;
        case "revertCheckpoint":
          if (typeof msg.checkpointId === "string") void this.revertCheckpoint(msg.checkpointId);
          break;
        case "undoCheckpoint":
          void this.undoLastCheckpoint();
          break;
        case "fetchAgents":
          void this.fetchAgents();
          break;
        case "attachImagePaths":
          if (Array.isArray(msg.paths)) {
            void this.attachImagePaths(msg.paths.filter((item: unknown): item is string => typeof item === "string"));
          }
          break;
        case "fetchAgent":
          if (typeof msg.agentId === "string") void this.fetchAgent(msg.agentId);
          break;
        case "fetchAgentLog":
          if (typeof msg.agentId === "string") void this.fetchAgentLog(msg.agentId, typeof msg.lines === "number" ? msg.lines : 200);
          break;
        case "sendAgentInput":
          if (typeof msg.agentId === "string" && typeof msg.prompt === "string") {
            void this.sendAgentInput(msg.agentId, msg.prompt, msg.interrupt === true);
          }
          break;
        case "waitAgent":
          if (
            typeof msg.agentId === "string"
            || (Array.isArray(msg.agentIds) && msg.agentIds.every((item: unknown) => typeof item === "string"))
          ) {
            void this.waitAgent(
              Array.isArray(msg.agentIds) ? msg.agentIds : msg.agentId,
              typeof msg.timeoutMs === "number" ? msg.timeoutMs : null,
            );
          }
          break;
        case "cancelAgent":
          if (typeof msg.agentId === "string") void this.cancelAgent(msg.agentId);
          break;
        case "spawnAgent":
          if (typeof msg.prompt === "string") {
            void this.spawnAgent(
              msg.prompt,
              msg.subagentType,
              msg.name,
              msg.modelProfile,
              msg.callback !== false,
            );
          }
          break;
        case "fetchGoal":
          void this.fetchGoal();
          break;
        case "manageGoal":
          if (typeof msg.action === "string") {
            void this.manageGoal(msg.action, typeof msg.objective === "string" ? msg.objective : null, typeof msg.tokenBudget === "number" ? msg.tokenBudget : null, msg.budgetWasSet === true);
          }
          break;
        case "fetchTasks":
          void this.fetchTasks();
          break;
        case "fetchTask":
          if (typeof msg.taskId === "string") void this.fetchTask(msg.taskId);
          break;
        case "fetchTaskOutput":
          if (typeof msg.taskId === "string") {
            void this.fetchTaskOutput(msg.taskId, typeof msg.lines === "number" ? msg.lines : 200);
          }
          break;
        case "stopTask":
          if (typeof msg.taskId === "string") void this.stopTask(msg.taskId);
          break;
        case "fetchSchedules":
          void this.fetchSchedules({
            status: typeof msg.status === "string" ? msg.status : undefined,
            scheduleType: typeof msg.scheduleType === "string" ? msg.scheduleType : undefined,
            enabled: typeof msg.enabled === "boolean" ? msg.enabled : undefined,
            limit: typeof msg.limit === "number" ? msg.limit : undefined,
          });
          break;
        case "fetchSchedule":
          if (typeof msg.jobId === "string") void this.fetchSchedule(msg.jobId);
          break;
        case "fetchScheduleRuns":
          if (typeof msg.jobId === "string") {
            void this.fetchScheduleRuns(
              msg.jobId,
              typeof msg.status === "string" ? msg.status : undefined,
              typeof msg.limit === "number" ? msg.limit : undefined,
            );
          }
          break;
        case "createSchedule":
          if (msg.request && typeof msg.request === "object") {
            void this.createSchedule(msg.request as ScheduleCreateRequest);
          }
          break;
        case "mutateSchedule":
          if (
            (msg.action === "pause" || msg.action === "resume" || msg.action === "run" || msg.action === "cancel")
            && typeof msg.jobId === "string"
          ) {
            void this.mutateSchedule(msg.action, msg.jobId);
          }
          break;
        case "fetchPeers":
          void this.fetchPeers();
          break;
        case "sendPeerMessage":
          if (typeof msg.to === "string" && typeof msg.text === "string") void this.sendPeerMessage(msg.to, msg.text);
          break;
        case "fetchTeams":
          void this.fetchTeams();
          break;
        case "fetchTeamStatus":
          if (typeof msg.teamId === "string") void this.fetchTeamStatus(msg.teamId);
          break;
        case "fetchTeamMessages":
          if (typeof msg.teamId === "string") {
            void this.fetchTeamMessages(
              msg.teamId,
              typeof msg.agentId === "string" ? msg.agentId : undefined,
              msg.unread === true,
            );
          }
          break;
        case "fetchTeamTasks":
          if (typeof msg.teamId === "string") void this.fetchTeamTasks(msg.teamId);
          break;
        case "createTeam":
          if (typeof msg.name === "string") void this.createTeam(msg.name, typeof msg.maxTeammates === "number" ? msg.maxTeammates : null);
          break;
        case "spawnTeamMember":
          if (typeof msg.teamId === "string" && typeof msg.prompt === "string") {
            void this.spawnTeamMember(msg.teamId, msg.prompt, msg.role, msg.name, msg.modelProfile);
          }
          break;
        case "removeTeamMember":
          if (typeof msg.teamId === "string" && typeof msg.agentId === "string") {
            void this.removeTeamMember(msg.teamId, msg.agentId);
          }
          break;
        case "sendTeamMessage":
          if (typeof msg.teamId === "string" && typeof msg.to === "string" && typeof msg.text === "string") {
            void this.sendTeamMessage(msg.teamId, msg.to, msg.text, msg.fromAgent);
          }
          break;
        case "broadcastTeamMessage":
          if (typeof msg.teamId === "string" && typeof msg.text === "string") {
            void this.broadcastTeamMessage(msg.teamId, msg.text, msg.fromAgent);
          }
          break;
        case "markTeamMessagesRead":
          if (typeof msg.teamId === "string" && typeof msg.agentId === "string") {
            void this.markTeamMessagesRead(
              msg.teamId,
              msg.agentId,
              Array.isArray(msg.messageIds) ? msg.messageIds : undefined,
            );
          }
          break;
        case "addTeamTask":
          if (typeof msg.teamId === "string" && typeof msg.description === "string") {
            void this.addTeamTask(msg.teamId, msg.description);
          }
          break;
        case "claimTeamTask":
          if (typeof msg.teamId === "string" && typeof msg.taskId === "string") {
            void this.claimTeamTask(msg.teamId, msg.taskId, msg.agentId);
          }
          break;
        case "completeTeamTask":
          if (typeof msg.teamId === "string" && typeof msg.taskId === "string") {
            void this.completeTeamTask(msg.teamId, msg.taskId, typeof msg.result === "string" ? msg.result : "", msg.agentId);
          }
          break;
        case "failTeamTask":
          if (typeof msg.teamId === "string" && typeof msg.taskId === "string") {
            void this.failTeamTask(
              msg.teamId,
              msg.taskId,
              typeof msg.reason === "string" ? msg.reason : "",
              msg.agentId,
            );
          }
          break;
        case "getTeamBridge":
          if (typeof msg.teamA === "string" && typeof msg.teamB === "string") {
            void this.getTeamBridge(msg.teamA, msg.teamB);
          }
          break;
        case "registerTeamBridge":
          if (typeof msg.teamA === "string" && typeof msg.teamB === "string") {
            void this.registerTeamBridge(msg.teamA, msg.teamB, msg.policy);
          }
          break;
        case "sendCrossTeamMessage":
          if (
            typeof msg.fromTeam === "string"
            && typeof msg.toTeam === "string"
            && typeof msg.text === "string"
          ) {
            void this.sendCrossTeamMessage(
              msg.fromTeam,
              msg.toTeam,
              msg.text,
              msg.fromAgent,
              msg.toAgent,
            );
          }
          break;
        case "shutdownTeam":
          if (typeof msg.teamId === "string") void this.shutdownTeam(msg.teamId);
          break;
        case "fetchPlanStatus":
          void this.fetchPlanStatus();
          break;
        case "fetchLogs":
          void this.fetchLogs(
            typeof msg.lines === "number" ? msg.lines : 100,
            typeof msg.name === "string" ? msg.name : null,
            typeof msg.tail === "number" ? msg.tail : null,
            msg.clear === true,
          );
          break;
        case "followLogs":
          if (typeof msg.name === "string") void this.followLogs(msg.name);
          break;
        case "stopLogFollow":
          this.stopLogFollow();
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
    // Reset cooldown so the next pushChatOptions actually fetches
    this._lastModelsFetchTime = 0;
    void this.pushChatOptions();
  }

  private sendCurrentSessionInfo(): void {
    const sid = this.displayedSessionId || this.connection.sessionId;
    if (sid) {
      const status = this.busySessions.has(sid) ? "running" : "done";
      this.postMessage({ type: "sessionInfo", sessionId: sid, title: null, status });
    }
  }

  private async fetchAndApplyContextUsage(sessionId: string): Promise<void> {
    try {
      const url = this._gatewayUrl(`/session/status`);
      const urlWithParam = `${url}?session_id=${encodeURIComponent(sessionId)}`;
      const response = await fetch(urlWithParam, { headers: this._gatewayHeaders() });
      if (!response.ok) return;
      const data = await response.json() as {
        context_used_tokens?: number;
        context_window_tokens?: number;
        context_used_percent?: number;
      };
      if (this.displayedSessionId !== sessionId) return;
      const state = this.getSessionState(sessionId);
      const used = Math.max(0, Math.trunc(data.context_used_tokens ?? 0));
      const window = Math.max(0, Math.trunc(data.context_window_tokens ?? 0));
      if (!used && !window) {
        state.contextUsage = null;
        this.postMessage({ type: "contextUsage", usage: null });
        return;
      }
      const usedPercent = Math.min(100, Math.max(0, data.context_used_percent ?? (window ? used / window * 100 : 0)));
      const remainingPercent = Math.max(0, 100 - usedPercent);
      const remainingTokens = Math.max(0, window - used);
      const usage: ContextUsageStatus = {
        usedTokens: used,
        windowTokens: window,
        remainingTokens,
        usedPercent,
        remainingPercent,
        details: [
          "背景信息窗口：",
          `已用 ${formatTokenCount(used)} / ${formatTokenCount(window)} 标记`,
          `剩余 ${remainingPercent.toFixed(1)}%`,
        ],
      };
      state.contextUsage = usage;
      this.postMessage({ type: "contextUsage", usage });
    } catch {
      // ignore
    }
  }

  private async fetchSessionRuntimeStatus(sessionId: string): Promise<SessionRuntimeStatus | null> {
    try {
      const url = new URL(this._gatewayUrl("/session/status"));
      url.searchParams.set("session_id", sessionId);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) return null;
      return await response.json() as SessionRuntimeStatus;
    } catch {
      return null;
    }
  }

  private addSessionSystemMessage(sessionId: string, text: string): void {
    this.addMessageOnState(
      this.getSessionState(sessionId),
      "system",
      text,
      this.displayedSessionId === sessionId,
    );
  }

  private async fetchAndShowStatus(): Promise<void> {
    const sessionId = this.displayedSessionId || this.connection.sessionId;
    if (!sessionId) {
      this.addMessage("system", "CrabCode：当前没有活动会话。");
      return;
    }

    const status = await this.fetchSessionRuntimeStatus(sessionId);
    if (!status) {
      this.addSessionSystemMessage(sessionId, "CrabCode：暂时无法读取会话状态。");
      return;
    }

    const model = [status.provider, status.model].filter(Boolean).join("/") || "未配置";
    const effort = status.reasoning_effort || "auto";
    const ultra = status.ultra_mode ? "开启" : "关闭";
    const permission = status.permission_mode || "default";
    const lines = [
      `**CrabCode：** v${status.version || "unknown"} · **会话 ID：** \`${status.session_id || sessionId}\``,
      `**模型：** ${model} · **模式：** ${status.mode || "agent"}`,
      `**Effort：** ${effort} · **Ultra mode：** ${ultra}`,
      `**工具权限：** ${permission}`,
    ];
    const used = Math.max(0, Math.trunc(status.context_used_tokens ?? 0));
    const window = Math.max(0, Math.trunc(status.context_window_tokens ?? 0));
    if (used || window) {
      const usedPercent = Math.min(
        100,
        Math.max(0, status.context_used_percent ?? (window ? used / window * 100 : 0)),
      );
      lines.push(
        `**背景窗口：** ${used.toLocaleString()} / ${window.toLocaleString()} tokens（${usedPercent.toFixed(1)}% 已用，剩余 ${Math.max(0, 100 - usedPercent).toFixed(1)}%）`,
      );
    } else {
      lines.push("**背景窗口：** 暂无数据");
    }
    const cachedUsage = this.getSessionState(sessionId).contextUsage;
    if (cachedUsage?.cacheDetail) {
      lines.push(`**提示缓存：** ${cachedUsage.cacheDetail}`);
    }
    const tools = status.tool_count == null ? "未加载" : status.tool_count.toLocaleString();
    lines.push(
      `**消息数：** ${status.message_count ?? 0} · **压缩次数：** ${status.compact_count ?? 0} · **自动压缩：** ${status.auto_compact_enabled === false ? "关闭" : "开启"}`,
      `**配置：** think=${status.thinking_enabled ? "on" : "off"} · max_tokens=${status.max_tokens ?? 0} · tools=${tools}`,
    );
    if ((status.agent_total ?? 0) > 0) {
      lines.push(
        `**Agents：** total=${status.agent_total} · active=${status.agent_active ?? 0} · failed=${status.agent_failed ?? 0} · callbacks=${status.agent_pending_callbacks ?? 0} · max_concurrency=${status.agent_max_concurrency ?? 0}`,
      );
    }
    if ((status.monitor_total ?? 0) > 0) {
      lines.push(
        `**Monitors：** total=${status.monitor_total} · active=${status.monitor_active ?? 0} · failed=${status.monitor_failed ?? 0}`,
      );
    }
    if (status.search_index) {
      const search = status.search_index;
      const details: string[] = [];
      if (search.chunks != null) details.push(`${search.chunks} chunks`);
      if (search.files != null) details.push(`${search.files} files`);
      if (search.done != null && search.total != null) {
        details.push(`${search.done}/${search.total}`);
      }
      lines.push(`**语义搜索：** ${search.state}${details.length > 0 ? `（${details.join("，")}）` : ""}`);
    }
    if (status.cwd) lines.push(`**工作目录：** \`${status.cwd}\``);
    this.addSessionSystemMessage(sessionId, lines.join("\n"));
  }

  private async showOrSetReasoningEffort(effort: string | null): Promise<void> {
    const sessionId = this.displayedSessionId || this.connection.sessionId;
    if (!sessionId) {
      this.addMessage("system", "CrabCode：当前没有活动会话。");
      return;
    }

    if (!effort) {
      const status = await this.fetchSessionRuntimeStatus(sessionId);
      if (!status) {
        this.addSessionSystemMessage(sessionId, "CrabCode：暂时无法读取 reasoning effort。");
        return;
      }
      const current = status.reasoning_effort || "auto";
      this.addSessionSystemMessage(
        sessionId,
        `当前 reasoning effort：**${current}**`,
      );
      return;
    }

    try {
      const response = await fetch(this._gatewayUrl("/config/reasoning-effort"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, effort }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({})) as { detail?: string };
        this.addSessionSystemMessage(
          sessionId,
          `CrabCode：设置 effort 失败：${payload.detail || response.statusText}`,
        );
        return;
      }
      const payload = await response.json() as { reasoning_effort?: string };
      this.addSessionSystemMessage(
        sessionId,
        `Reasoning effort 已设为 **${payload.reasoning_effort || effort}**，下一次请求生效。`,
      );
    } catch {
      this.addSessionSystemMessage(sessionId, "CrabCode：设置 effort 失败，无法连接网关。");
    }
  }

  private async setUltraMode(enabled: boolean | null): Promise<void> {
    const sessionId = this.displayedSessionId || this.connection.sessionId;
    if (!sessionId) {
      this.addMessage("system", "CrabCode：当前没有活动会话。");
      return;
    }

    try {
      const response = await fetch(this._gatewayUrl("/config/ultra-mode"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, enabled }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({})) as { detail?: string };
        this.addSessionSystemMessage(
          sessionId,
          `CrabCode：设置 ultra mode 失败：${payload.detail || response.statusText}`,
        );
        return;
      }
      const payload = await response.json() as { ultra_mode?: boolean };
      this.addSessionSystemMessage(
        sessionId,
        `Ultra mode 已**${payload.ultra_mode ? "开启" : "关闭"}**，下一次请求生效。`,
      );
    } catch {
      this.addSessionSystemMessage(sessionId, "CrabCode：设置 ultra mode 失败，无法连接网关。");
    }
  }

  private async fetchAndSendCurrentTitle(): Promise<void> {
    const sid = this.connection.sessionId;
    if (!sid) return;
    const cfg = vscode.workspace.getConfiguration("crabcode");
    const wsUrl = cfg.get<string>("serverUrl", "ws://localhost:4096/ws");
    const password = cfg.get<string>("password", "");
    try {
      const url = new URL(wsUrl);
      url.protocol = url.protocol === "wss:" ? "https:" : "http:";
      url.pathname = "/session/list";
      url.search = "";
      const headers: Record<string, string> = {};
      if (password) headers.Authorization = `Bearer ${password}`;
      const response = await fetch(url.toString(), { headers });
      if (!response.ok) return;
      const sessions = (await response.json()) as Array<{ session_id: string; title?: string }>;
      const found = sessions.find((s) => s.session_id === sid);
      if (found?.title) {
        this.postMessage({ type: "sessionInfo", sessionId: sid, title: found.title, status: null });
      }
    } catch {
      // ignore
    }
  }

  private async switchMode(mode: "agent" | "plan"): Promise<void> {
    const sessionId = this.displayedSessionId || this.connection.sessionId;
    if (!sessionId) {
      return;
    }
    const state = this.getSessionState(sessionId);
    const cfg = vscode.workspace.getConfiguration("crabcode");
    const wsUrl = cfg.get<string>("serverUrl", "ws://localhost:4096/ws");
    const password = cfg.get<string>("password", "");
    try {
      const url = new URL(wsUrl);
      url.protocol = url.protocol === "wss:" ? "https:" : "http:";
      url.pathname = "/config/switch-mode";
      url.search = "";
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (password) headers.Authorization = `Bearer ${password}`;
      const response = await fetch(url.toString(), {
        method: "POST",
        headers,
        body: JSON.stringify({ mode, session_id: sessionId }),
      });
      if (!response.ok) {
        throw new Error(`switch mode failed: ${response.status}`);
      }
      state.mode = mode;
      this.postMessage({ type: "modeChange", mode });
    } catch {
      // The webview updates optimistically when the menu is clicked.  Roll it
      // back to the last confirmed per-session mode if the gateway rejects the
      // request or is unavailable.
      this.postMessage({ type: "modeChange", mode: state.mode });
      this.addSessionSystemMessage(sessionId, "CrabCode：切换模式失败，会话模式未改变。");
    }
  }

  private async triggerCompact(customInstructions?: string): Promise<void> {
    const cfg = vscode.workspace.getConfiguration("crabcode");
    const wsUrl = cfg.get<string>("serverUrl", "ws://localhost:4096/ws");
    const password = cfg.get<string>("password", "");
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const url = new URL(wsUrl);
      url.protocol = url.protocol === "wss:" ? "https:" : "http:";
      url.pathname = "/session/compact";
      url.search = "";
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (password) headers.Authorization = `Bearer ${password}`;
      const response = await fetch(url.toString(), {
        method: "POST",
        headers,
        body: JSON.stringify({
          session_id: sessionId,
          custom_instructions: customInstructions?.trim() || null,
        }),
      });
      if (!response.ok) {
        throw new Error(`compact failed: ${response.status}`);
      }
      const data = (await response.json()) as { status?: string };
      if (data.status === "not_compacted") {
        this.addSessionSystemMessage(sessionId, "历史不足，或压缩检查点生成失败。");
        return;
      }
      await this.fetchAndApplySessionHistory(sessionId);
    } catch {
      this.addSessionSystemMessage(sessionId, "压缩对话失败。");
    }
  }

  private async clearSessionHistory(): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/session/clear"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId }),
      });
      if (!response.ok) throw new Error(`clear failed: ${response.status}`);
      await this.fetchAndApplySessionHistory(sessionId);
      this.addSessionSystemMessage(sessionId, "对话历史已清除。");
    } catch {
      this.addSessionSystemMessage(sessionId, "清除对话历史失败。");
    }
  }

  private async invokeSkill(name: string, userInput: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/skills/expand"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ name, user_input: userInput, session_id: sessionId }),
      });
      if (!response.ok) throw new Error(`skill expansion failed: ${response.status}`);
      const data = (await response.json()) as { prompt: string };
      const displayText = `/${name}${userInput ? ` ${userInput}` : ""}`;
      this.sendExpandedPrompt(displayText, data.prompt);
    } catch {
      this.addSessionSystemMessage(sessionId, `Skill /${name} 展开失败。`);
    }
  }

  private sendExpandedPrompt(displayText: string, prompt: string): void {
    this.ensureSessionIfNeeded();
    if (this.isBusy) {
      this.queueSteeringMessageOnState(this.currentState, displayText);
      this.connection.steer(prompt, {
        sessionId: this.displayedSessionId ?? this.connection.sessionId ?? undefined,
      });
    } else {
      this.addMessage("user", displayText);
      this.setBusy(true);
      this.sendForegroundMessage(prompt);
    }
  }

  private async fetchAndSendSkills(): Promise<void> {
    const cfg = vscode.workspace.getConfiguration("crabcode");
    const wsUrl = cfg.get<string>("serverUrl", "ws://localhost:4096/ws");
    const password = cfg.get<string>("password", "");
    try {
      const url = new URL(wsUrl);
      url.protocol = url.protocol === "wss:" ? "https:" : "http:";
      url.pathname = "/skills";
      url.search = "";
      const sessionId = this.displayedSessionId ?? this.connection.sessionId;
      if (sessionId) url.searchParams.set("session_id", sessionId);
      const headers: Record<string, string> = {};
      if (password) headers.Authorization = `Bearer ${password}`;
      const response = await fetch(url.toString(), { headers });
      if (!response.ok) return;
      const skills = (await response.json()) as Array<{ name: string; description: string }>;
      this.postMessage({ type: "skills", skills });
    } catch {
      // gateway not ready yet — ignore
    }
  }

  private async fetchAndSendSessions(): Promise<void> {
    const cfg = vscode.workspace.getConfiguration("crabcode");
    const wsUrl = cfg.get<string>("serverUrl", "ws://localhost:4096/ws");
    const password = cfg.get<string>("password", "");
    try {
      const url = new URL(wsUrl);
      url.protocol = url.protocol === "wss:" ? "https:" : "http:";
      url.pathname = "/session/list";
      url.search = "";
      const headers: Record<string, string> = {};
      if (password) headers.Authorization = `Bearer ${password}`;
      const response = await fetch(url.toString(), { headers });
      if (!response.ok) return;
      const sessions = (await response.json()) as Array<{
        session_id: string;
        message_count?: number;
        model?: string;
        created_at?: string;
        title?: string;
      }>;
      const sessionList = sessions.map((s) => ({
        session_id: s.session_id,
        message_count: s.message_count,
        model: s.model,
        created_at: s.created_at,
        title: s.title ?? s.session_id.slice(0, 12),
        status: this.busySessions.has(s.session_id) ? "running" : "done",
      }));
      this.postMessage({ type: "sessionList", sessions: sessionList });
    } catch {
      // gateway not ready yet — ignore
    }
  }

  private async resolveSessionId(selector: string): Promise<string | null> {
    try {
      const url = new URL(this._gatewayUrl("/session/resolve"));
      url.searchParams.set("selector", selector);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) return null;
      const data = await response.json() as { session_id?: string };
      return typeof data.session_id === "string" && data.session_id ? data.session_id : null;
    } catch {
      return null;
    }
  }

  private async resumeSession(
    selector: string,
    overrides?: SessionLaunchOverrides,
  ): Promise<void> {
    const sessionId = await this.resolveSessionId(selector);
    if (!sessionId) {
      this.addMessage("system", `找不到会话，或短 ID 不唯一：\`${selector}\``);
      return;
    }
    // Immediately update displayed session before processing
    this.displayedSessionId = sessionId;

    // If we already have cached state for this session, render it immediately
    const cached = this.sessionStates.get(sessionId);
    if (cached) {
      this.postMessage({ type: "history", items: cached.history });
      this.postMessage({ type: "busyState", busy: cached.isBusy });
      this.postMessage({ type: "contextUsage", usage: cached.contextUsage ?? null });
      this.postMessage({ type: "modeChange", mode: cached.mode });
      this.postMessage({ type: "steeringQueue", messages: cached.pendingSteeringMessages });
    } else {
      // No cached state — clear display and request from server
      this.postMessage({ type: "history", items: [] });
      this.postMessage({ type: "busyState", busy: this.busySessions.has(sessionId) });
      this.postMessage({ type: "contextUsage", usage: null });
      this.postMessage({ type: "modeChange", mode: "agent" });
      this.postMessage({ type: "steeringQueue", messages: [] });
    }
    // Rendering cached state does not make it the WebSocket's active session.
    // Always synchronize server ownership when the selected conversation is
    // different, otherwise permission and plan commands can target the old one.
    const hasOverrides = Boolean(overrides && Object.keys(overrides).length > 0);
    if (this.connection.sessionId !== sessionId || hasOverrides) {
      this.connection.sendResumeSession(
        hasOverrides ? selector : sessionId,
        overrides,
      );
    }

    this.sendCurrentSessionInfo();
    setTimeout(() => void this.fetchAndSendCurrentTitle(), 500);
  }

  private _gatewayUrl(path: string): string {
    const cfg = vscode.workspace.getConfiguration("crabcode");
    const wsUrl = cfg.get<string>("serverUrl", "ws://localhost:4096/ws");
    const url = new URL(wsUrl);
    url.protocol = url.protocol === "wss:" ? "https:" : "http:";
    url.pathname = path;
    url.search = "";
    return url.toString();
  }

  private _gatewayHeaders(): Record<string, string> {
    const password = vscode.workspace.getConfiguration("crabcode").get<string>("password", "");
    const h: Record<string, string> = {};
    if (password) h.Authorization = `Bearer ${password}`;
    return h;
  }

  private async gatewayError(response: Response): Promise<string> {
    const fallback = response.statusText || `HTTP ${response.status}`;
    try {
      const payload = await response.json() as { detail?: unknown };
      return typeof payload.detail === "string" && payload.detail ? payload.detail : fallback;
    } catch {
      return fallback;
    }
  }

  private formatScheduleTime(value: string | null | undefined): string {
    if (!value) return "未安排";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
  }

  private scheduleSessionId(): string | null {
    return this.displayedSessionId ?? this.connection.sessionId;
  }

  private async fetchSchedules(options: {
    status?: string;
    scheduleType?: string;
    enabled?: boolean;
    limit?: number;
  } = {}): Promise<void> {
    const sessionId = this.scheduleSessionId();
    if (!sessionId) {
      this.addMessage("system", "暂无活跃会话。");
      return;
    }
    try {
      const url = new URL(this._gatewayUrl("/schedule/list"));
      url.searchParams.set("session_id", sessionId);
      if (options.status) url.searchParams.set("status", options.status);
      if (options.scheduleType) url.searchParams.set("schedule_type", options.scheduleType);
      if (options.enabled !== undefined) url.searchParams.set("enabled", String(options.enabled));
      url.searchParams.set("limit", String(Math.max(1, Math.min(1000, Math.trunc(options.limit ?? 100)))));
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) throw new Error(await this.gatewayError(response));
      const jobs = await response.json() as ScheduleJobInfo[];
      if (!jobs.length) {
        this.addSessionSystemMessage(sessionId, "暂无定时任务。");
        return;
      }
      const lines = ["## 定时任务", ""];
      for (const job of jobs) {
        const count = job.max_runs == null ? String(job.run_count ?? 0) : `${job.run_count ?? 0}/${job.max_runs}`;
        lines.push(
          `- \`${job.id.slice(0, 8)}\` **${job.name}** · ${job.status} · ` +
          `${job.schedule_type} \`${job.schedule}\` · runs ${count} · next ${this.formatScheduleTime(job.next_run)}`,
        );
      }
      this.addSessionSystemMessage(sessionId, lines.join("\n"));
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `读取定时任务失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async fetchSchedule(jobId: string): Promise<void> {
    const sessionId = this.scheduleSessionId();
    if (!sessionId) {
      this.addMessage("system", "暂无活跃会话。");
      return;
    }
    try {
      const url = new URL(this._gatewayUrl(`/schedule/${encodeURIComponent(jobId)}`));
      url.searchParams.set("session_id", sessionId);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) throw new Error(await this.gatewayError(response));
      const job = await response.json() as ScheduleJobInfo;
      const count = job.max_runs == null ? String(job.run_count ?? 0) : `${job.run_count ?? 0}/${job.max_runs}`;
      const lines = [
        `## 定时任务 \`${job.id.slice(0, 8)}\``,
        `**名称：** ${job.name}`,
        `**状态：** ${job.status}（${job.enabled === false ? "停用" : "启用"}）`,
        `**计划：** ${job.schedule_type} \`${job.schedule}\``,
        `**下次执行：** ${this.formatScheduleTime(job.next_run)}`,
        `**执行次数：** ${count}`,
        `**工作目录：** \`${job.cwd ?? ""}\``,
      ];
      if (job.description) lines.push(`**说明：** ${job.description}`);
      if (job.tags?.length) lines.push(`**标签：** ${job.tags.join(", ")}`);
      if (job.model_profile) lines.push(`**模型配置：** ${job.model_profile}`);
      if (job.session_id) lines.push(`**复用会话：** \`${job.session_id}\``);
      if (job.extra && Object.keys(job.extra).length > 0) {
        lines.push(`**扩展数据：** \`${JSON.stringify(job.extra)}\``);
      }
      lines.push("", "**提示词：**", "```", job.prompt, "```");
      this.addSessionSystemMessage(sessionId, lines.join("\n"));
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `读取定时任务失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async fetchScheduleRuns(jobId: string, status?: string, limit = 50): Promise<void> {
    const sessionId = this.scheduleSessionId();
    if (!sessionId) {
      this.addMessage("system", "暂无活跃会话。");
      return;
    }
    try {
      const url = new URL(this._gatewayUrl(`/schedule/${encodeURIComponent(jobId)}/runs`));
      url.searchParams.set("session_id", sessionId);
      url.searchParams.set("limit", String(Math.max(1, Math.min(1000, Math.trunc(limit)))));
      if (status) url.searchParams.set("status", status);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) throw new Error(await this.gatewayError(response));
      const runs = await response.json() as ScheduleRunInfo[];
      if (!runs.length) {
        this.addSessionSystemMessage(sessionId, `定时任务 \`${jobId.slice(0, 8)}\` 暂无执行历史。`);
        return;
      }
      const lines = [`## 定时任务执行历史 \`${jobId.slice(0, 8)}\``, ""];
      for (const run of runs) {
        const detail = run.error_message || run.result_summary || "";
        lines.push(
          `- \`${run.id.slice(0, 8)}\` · ${run.status} · ` +
          `${this.formatScheduleTime(run.started_at ?? run.created_at)} · ` +
          `${run.duration_seconds == null ? "" : `${run.duration_seconds.toFixed(1)}s`} ` +
          `${detail}`.trim(),
        );
      }
      this.addSessionSystemMessage(sessionId, lines.join("\n"));
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `读取执行历史失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async createSchedule(request: ScheduleCreateRequest): Promise<void> {
    const sessionId = this.scheduleSessionId();
    if (!sessionId) {
      this.addMessage("system", "暂无活跃会话。");
      return;
    }
    try {
      const response = await fetch(this._gatewayUrl("/schedule/create"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ ...request, session_id: sessionId }),
      });
      if (!response.ok) throw new Error(await this.gatewayError(response));
      const job = await response.json() as ScheduleJobInfo;
      this.addSessionSystemMessage(
        sessionId,
        `已创建定时任务 **${job.name}**（\`${job.id.slice(0, 8)}\`），下次执行：${this.formatScheduleTime(job.next_run)}。`,
      );
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `创建定时任务失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async mutateSchedule(
    action: "pause" | "resume" | "run" | "cancel",
    jobId: string,
  ): Promise<void> {
    const sessionId = this.scheduleSessionId();
    if (!sessionId) {
      this.addMessage("system", "暂无活跃会话。");
      return;
    }
    if (action === "cancel") {
      const confirmed = await vscode.window.showWarningMessage(
        `将永久删除定时任务 ${jobId} 及其执行历史。`,
        { modal: true },
        "删除",
      );
      if (confirmed !== "删除") return;
    }
    const endpoint = action === "run" ? "trigger" : action;
    try {
      const response = await fetch(this._gatewayUrl(`/schedule/${endpoint}`), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: jobId, session_id: sessionId }),
      });
      if (!response.ok) throw new Error(await this.gatewayError(response));
      const data = await response.json().catch(() => ({})) as ScheduleJobInfo & { started?: boolean; cancelled?: boolean };
      const verb = action === "pause" ? "已暂停" : action === "resume" ? "已恢复" : action === "run" ? "已触发" : "已删除";
      this.addSessionSystemMessage(sessionId, `${verb}定时任务 \`${data.id?.slice(0, 8) || jobId.slice(0, 8)}\`。`);
      if (action !== "cancel") void this.fetchSchedule(jobId);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `定时任务操作失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async fetchRecentSessions(limit: number): Promise<void> {
    try {
      const url = new URL(this._gatewayUrl("/session/recent"));
      url.searchParams.set("limit", String(limit));
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) return;
      const sessions = (await response.json()) as Array<{
        session_id: string; message_count?: number; model?: string; created_at?: string; title?: string;
        cwd?: string; tokens_used?: number; preview?: string;
      }>;
      const sessionList = sessions.map((s) => ({
        session_id: s.session_id,
        message_count: s.message_count,
        model: s.model,
        created_at: s.created_at,
        title: s.title ?? s.session_id.slice(0, 12),
        cwd: s.cwd,
        tokens_used: s.tokens_used,
        preview: s.preview,
        status: this.busySessions.has(s.session_id) ? "running" : "done",
      }));
      this.postMessage({ type: "sessionList", sessions: sessionList });
    } catch { /* ignore */ }
  }

  private async searchSessions(query: string): Promise<void> {
    try {
      const response = await fetch(this._gatewayUrl("/session/search"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ query, limit: 20 }),
      });
      if (!response.ok) return;
      const sessions = (await response.json()) as Array<{
        session_id: string; message_count?: number; model?: string; created_at?: string; title?: string;
        cwd?: string; tokens_used?: number; preview?: string;
      }>;
      const sessionList = sessions.map((s) => ({
        session_id: s.session_id,
        message_count: s.message_count,
        model: s.model,
        created_at: s.created_at,
        title: s.title ?? s.session_id.slice(0, 12),
        cwd: s.cwd,
        tokens_used: s.tokens_used,
        preview: s.preview,
        status: this.busySessions.has(s.session_id) ? "running" : "done",
      }));
      this.postMessage({ type: "sessionList", sessions: sessionList });
    } catch { /* ignore */ }
  }

  private async archiveSession(sessionId: string): Promise<void> {
    try {
      const response = await fetch(this._gatewayUrl("/session/archive"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId }),
      });
      if (!response.ok) {
        this.addMessage("system", `归档失败：${response.statusText}`);
        return;
      }
      const data = await response.json() as { session_id?: string };
      const archivedId = data.session_id || sessionId;
      const archivedCurrent = this.displayedSessionId === archivedId
        || this.connection.sessionId === archivedId;
      this.sessionStates.delete(archivedId);
      this.busySessions.delete(archivedId);
      if (archivedCurrent) {
        this.displayedSessionId = null;
        this.postMessage({ type: "history", items: [] });
        this.postMessage({ type: "busyState", busy: false });
        this.postMessage({ type: "contextUsage", usage: null });
        this.postMessage({ type: "steeringQueue", messages: [] });
        const cwd = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? null;
        this.connection.sendNewSession(cwd);
        void vscode.window.showInformationMessage(
          `CrabCode：会话 ${archivedId.slice(0, 8)} 已归档，已创建新会话。`,
        );
      } else {
        this.addMessage("system", `会话 \`${archivedId.slice(0, 8)}\` 已归档。`);
      }
      void this.fetchAndSendSessions();
    } catch (error) {
      this.addMessage("system", `归档失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async pruneSessions(days: number, deleteFiles: boolean): Promise<void> {
    const normalizedDays = Math.max(0, Math.trunc(days));
    const action = deleteFiles ? "归档并永久清理文件" : "归档";
    const confirmed = await vscode.window.showWarningMessage(
      `将${action} ${normalizedDays} 天前的非活动会话。`,
      { modal: true },
      "继续",
    );
    if (confirmed !== "继续") return;

    try {
      const response = await fetch(this._gatewayUrl("/session/prune"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({
          days: normalizedDays,
          delete_files: deleteFiles,
        }),
      });
      if (!response.ok) throw new Error(`prune failed: ${response.status}`);
      const result = await response.json() as {
        archived?: number;
        purged?: number;
        failed?: string[];
      };
      const parts = [
        `已归档 ${result.archived ?? 0} 个会话`,
        `已清理 ${result.purged ?? 0} 个会话文件`,
      ];
      if (result.failed?.length) parts.push(`${result.failed.length} 个清理失败`);
      this.addMessage("system", `${parts.join("，")}。`);
      void this.fetchAndSendSessions();
    } catch (error) {
      this.addMessage(
        "system",
        `清理会话失败：${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }

  private async exportSession(
    format: "md" | "json",
    requestedPath?: string,
    requestedSessionId?: string,
  ): Promise<void> {
    const sessionId = requestedSessionId?.trim() || this.displayedSessionId || this.connection.sessionId;
    if (!sessionId) { this.addMessage("system", "暂无活跃会话。"); return; }
    try {
      const response = await fetch(this._gatewayUrl("/session/export"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, format }),
      });
      if (!response.ok) {
        this.addMessage("system", `导出失败：${await this.gatewayError(response)}`);
        return;
      }
      const content = await response.text();
      const ext = format === "json" ? "json" : "md";
      const safeId = sessionId.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 8) || "export";
      const filename = `session-${safeId}.${ext}`;
      const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
      const target = requestedPath?.trim();
      const targetPath = target
        ? resolveLocalPath(target, workspaceRoot)
        : path.join(workspaceRoot ?? "/tmp", filename);
      const uri = vscode.Uri.file(targetPath);
      await vscode.workspace.fs.writeFile(uri, Buffer.from(content, "utf-8"));
      this.addMessage("system", `会话已导出：\`${uri.fsPath}\``);
    } catch (error) {
      this.addMessage("system", `导出失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async fetchAndShowModel(): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) {
      this.addMessage("system", "暂无活跃会话。");
      return;
    }
    try {
      const status = await this.fetchSessionRuntimeStatus(sessionId);
      if (!status) throw new Error("无法读取会话状态");
      const url = new URL(this._gatewayUrl("/config/models"));
      url.searchParams.set("session_id", sessionId);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) throw new Error(`读取模型列表失败：${response.status}`);
      const models = await response.json() as GatewayModelInfo[];
      const activeProfile = status.model_profile || this.connection.modelName || "";
      const activeModel = [status.provider, status.model].filter(Boolean).join("/") || "未配置";
      const lines = [
        `**当前配置：** ${activeProfile || "默认"}`,
        `**Provider/Model：** ${activeModel}`,
      ];
      if (models.length > 0) {
        lines.push("", "**可用模型：**");
        const groupedModels = new Map<string, GatewayModelInfo[]>();
        for (const model of models) {
          const group = model.group || "default";
          const entries = groupedModels.get(group) ?? [];
          entries.push(model);
          groupedModels.set(group, entries);
        }
        for (const [group, entries] of groupedModels) {
          lines.push("", `**${group}**`);
          for (const model of entries) {
            const marker = model.name === activeProfile ? " ← 当前" : "";
            lines.push(`- \`${model.name}\`${marker}${model.description ? ` · ${model.description}` : ""}`);
          }
        }
      } else {
        lines.push("", "（网关没有返回已命名模型）");
      }
      this.addSessionSystemMessage(sessionId, lines.join("\n"));
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `读取模型失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async fetchStats(): Promise<void> {
    try {
      const response = await fetch(this._gatewayUrl("/session/stats"), { headers: this._gatewayHeaders() });
      if (!response.ok) return;
      const data = (await response.json()) as {
        global: { total_sessions: number; total_tokens: number; total_messages: number; week_sessions: number; week_tokens: number };
        project: { total_sessions: number; total_tokens: number; cwd: string };
        by_model: Array<{ model: string; sessions: number; tokens: number }>;
      };
      const g = data.global;
      const p = data.project;
      const lines = [
        "## 使用统计",
        "",
        `**全局：** ${g.total_sessions} 个会话，${g.total_messages.toLocaleString()} 条消息，${g.total_tokens.toLocaleString()} tokens`,
        `**本周：** ${g.week_sessions} 个会话，${g.week_tokens.toLocaleString()} tokens`,
        `**当前项目 (${p.cwd})：** ${p.total_sessions} 个会话，${p.total_tokens.toLocaleString()} tokens`,
      ];
      if (data.by_model.length > 0) {
        lines.push("", "**按模型：**");
        data.by_model.forEach((m) => lines.push(`- ${m.model}：${m.sessions} 个会话，${m.tokens.toLocaleString()} tokens`));
      }
      this.addMessage("system", lines.join("\n"));
    } catch { /* ignore */ }
  }

  private async createCheckpoint(label: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) { this.addMessage("system", "暂无活跃会话。"); return; }
    try {
      const response = await fetch(this._gatewayUrl("/snapshot/checkpoint"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, label }),
      });
      if (!response.ok) { this.addMessage("system", `创建检查点失败：${response.statusText}`); return; }
      const data = (await response.json()) as { checkpoint_id: string };
      this.addMessage("system", `检查点已创建：\`${data.checkpoint_id.slice(0, 8)}\`${label ? `（${label}）` : ""}`);
    } catch { /* ignore */ }
  }

  private async fetchCheckpoints(): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) { this.addMessage("system", "暂无活跃会话。"); return; }
    try {
      const url = new URL(this._gatewayUrl("/snapshot/list"));
      url.searchParams.set("session_id", sessionId);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) return;
      const checkpoints = (await response.json()) as Array<{
        id: string; label?: string; created_at?: number; message_index?: number;
      }>;
      if (checkpoints.length === 0) { this.addMessage("system", "无检查点。"); return; }
      const lines = ["## 检查点", ""];
      checkpoints.forEach((cp) => {
        const ts = cp.created_at ? new Date(cp.created_at * 1000).toLocaleString() : "未知时间";
        lines.push(`- \`${cp.id.slice(0, 8)}\`  ${cp.label ? `**${cp.label}**  ` : ""}${ts}  消息数：${cp.message_index ?? "?"}`);
      });
      this.addMessage("system", lines.join("\n"));
    } catch { /* ignore */ }
  }

  private async rollbackCheckpoint(checkpointId: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/snapshot/rollback"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, checkpoint_id: checkpointId }),
      });
      if (!response.ok) { this.addMessage("system", `回滚失败：${response.statusText}`); return; }
      await this.fetchAndApplySessionHistory(sessionId);
      this.addSessionSystemMessage(sessionId, `对话已回滚到检查点 \`${checkpointId.slice(0, 8)}\`（文件未还原）。`);
      void this.fetchAndSendSessions();
    } catch { /* ignore */ }
  }

  private async revertCheckpoint(checkpointId: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/snapshot/revert"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, checkpoint_id: checkpointId }),
      });
      if (!response.ok) { this.addMessage("system", `还原失败：${response.statusText}`); return; }
      await this.fetchAndApplySessionHistory(sessionId);
      this.addSessionSystemMessage(sessionId, `对话和文件已还原到检查点 \`${checkpointId.slice(0, 8)}\`。`);
    } catch { /* ignore */ }
  }

  private async undoLastCheckpoint(): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/snapshot/undo"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId }),
      });
      if (!response.ok) { this.addMessage("system", `撤销失败：${response.statusText}`); return; }
      const data = (await response.json()) as { checkpoint_id: string; files_restored?: string[]; warning?: string | null };
      await this.fetchAndApplySessionHistory(sessionId);
      const fileText = data.files_restored?.length ? `，恢复 ${data.files_restored.length} 个文件` : "";
      const warning = data.warning ? `\n\n${data.warning}` : "";
      this.addMessage("system", `已撤销到检查点 \`${data.checkpoint_id.slice(0, 8)}\`${fileText}。${warning}`);
    } catch { /* ignore */ }
  }

  private async fetchAgents(): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    try {
      const url = new URL(this._gatewayUrl("/agent/list"));
      if (sessionId) url.searchParams.set("session_id", sessionId);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) return;
      const agents = (await response.json()) as Array<{
        agent_id: string; title: string; status: string; subagent_type: string;
      }>;
      if (agents.length === 0) { this.addSessionSystemMessage(sessionId ?? "", "无托管 Agent。"); return; }
      const lines = ["## Agents", ""];
      agents.forEach((a) => lines.push(`- \`${a.agent_id.slice(0, 8)}\`  **${a.title}**  [${a.subagent_type}]  ${a.status}`));
      this.addSessionSystemMessage(sessionId ?? "", lines.join("\n"));
    } catch (error) {
      this.addSessionSystemMessage(sessionId ?? "", `读取 Agents 失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async fetchAgent(agentId: string): Promise<void> {
    try {
      const url = new URL(this._gatewayUrl(`/agent/${encodeURIComponent(agentId)}`));
      const sessionId = this.displayedSessionId ?? this.connection.sessionId;
      if (sessionId) url.searchParams.set("session_id", sessionId);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) throw new Error(`agent lookup failed: ${response.status}`);
      const agent = await response.json() as AgentInfo;
      const lines = [
        `## Agent \`${agent.agent_id.slice(0, 8)}\``,
        "",
        `**标题：** ${agent.title}`,
        `**状态：** ${agent.status} · **类型：** ${agent.subagent_type}`,
        `**模型：** ${agent.model_profile || agent.model || "默认"} · **深度：** ${agent.depth ?? 0}`,
        `**回调：** ${agent.callback_enabled ? (agent.callback_state || "enabled") : "disabled"}`,
      ];
      if (agent.usage && Object.keys(agent.usage).length > 0) {
        lines.push(`**Usage：** \`${JSON.stringify(agent.usage)}\``);
      }
      if (agent.final_result) lines.push("", "**结果：**", agent.final_result);
      if (agent.error) lines.push("", `**错误：** ${agent.error}`);
      if (agent.transcript_path) lines.push("", `**Transcript：** \`${agent.transcript_path}\``);
      this.addSessionSystemMessage(sessionId ?? this.connection.sessionId ?? "", lines.join("\n"));
    } catch (error) {
      this.addMessage("system", `读取 Agent 失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async fetchAgentLog(agentId: string, lines: number): Promise<void> {
    try {
      const url = new URL(this._gatewayUrl(`/agent/${encodeURIComponent(agentId)}/transcript`));
      const sessionId = this.displayedSessionId ?? this.connection.sessionId;
      if (sessionId) url.searchParams.set("session_id", sessionId);
      url.searchParams.set("lines", String(Math.max(1, Math.min(10_000, Math.trunc(lines)))));
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) throw new Error(`agent transcript failed: ${response.status}`);
      const data = await response.json() as AgentTranscriptResponse;
      const body = data.lines?.length ? data.lines.join("\n") : "（无 transcript）";
      this.addSessionSystemMessage(
        sessionId ?? "",
        `## Agent Log \`${data.agent_id.slice(0, 8)}\`${data.truncated ? "（已截断）" : ""}\n\n\`\`\`\n${body}\n\`\`\``,
      );
    } catch (error) {
      this.addMessage("system", `读取 Agent transcript 失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async sendAgentInput(agentId: string, prompt: string, interrupt = false): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl(`/agent/${encodeURIComponent(agentId)}/input`), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, interrupt, session_id: sessionId }),
      });
      if (!response.ok) throw new Error(`send input failed: ${response.status}`);
      this.addSessionSystemMessage(sessionId, `已向 Agent \`${agentId.slice(0, 8)}\` 发送输入。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `发送 Agent 输入失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async waitAgent(agentIds: string | string[], timeoutMs: number | null): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    const selectors = Array.isArray(agentIds) ? agentIds : [agentIds];
    try {
      const response = await fetch(this._gatewayUrl("/agent/wait"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_id: Array.isArray(agentIds) ? selectors : selectors[0],
          timeout_ms: timeoutMs,
          session_id: sessionId,
        }),
      });
      if (!response.ok) throw new Error(`wait failed: ${response.status}`);
      const agent = await response.json() as AgentInfo;
      this.addSessionSystemMessage(
        sessionId,
        `Agent \`${agent.agent_id.slice(0, 8)}\` 已结束：**${agent.status}**${agent.final_result ? `\n\n${agent.final_result}` : ""}${agent.error ? `\n\n错误：${agent.error}` : ""}`,
      );
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `等待 Agent 失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async cancelAgent(agentId: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const url = new URL(this._gatewayUrl(`/agent/${encodeURIComponent(agentId)}/cancel`));
      url.searchParams.set("session_id", sessionId);
      const response = await fetch(url.toString(), {
        method: "POST",
        headers: this._gatewayHeaders(),
      });
      if (!response.ok) throw new Error(`cancel failed: ${response.status}`);
      this.addSessionSystemMessage(sessionId, `已请求取消 Agent \`${agentId.slice(0, 8)}\`。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `取消 Agent 失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async spawnAgent(
    prompt: string,
    subagentType?: string,
    name?: string,
    modelProfile?: string,
    callback = true,
  ): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/agent/spawn"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt,
          subagent_type: subagentType || "generalPurpose",
          name: name || null,
          model_profile: modelProfile || null,
          callback,
          session_id: sessionId,
        }),
      });
      if (!response.ok) throw new Error(`spawn failed: ${response.status}`);
      const agent = await response.json() as AgentInfo;
      this.addSessionSystemMessage(sessionId, `已启动 Agent \`${agent.agent_id.slice(0, 8)}\`：${agent.title}`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `启动 Agent 失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async attachImagePaths(rawPaths: string[]): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!rawPaths.length) return;
    const images: ImageAttachment[] = [];
    const maxBytes = 20 * 1024 * 1024;
    const imageExts: Record<string, string> = {
      png: "image/png",
      jpg: "image/jpeg",
      jpeg: "image/jpeg",
      gif: "image/gif",
      webp: "image/webp",
    };
    const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    const errors: string[] = [];
    for (const rawPath of rawPaths) {
      const trimmed = rawPath.trim();
      if (!trimmed) continue;
      const filePath = resolveLocalPath(trimmed, workspaceRoot);
      const uri = vscode.Uri.file(filePath);
      try {
        const stat = await vscode.workspace.fs.stat(uri);
        if ((stat.type & vscode.FileType.File) === 0) {
          errors.push(`${trimmed} 不是文件`);
          continue;
        }
        if (stat.size > maxBytes) {
          errors.push(`${trimmed} 超过 20MB`);
          continue;
        }
        const base = path.basename(filePath);
        const ext = path.extname(base).slice(1).toLowerCase();
        const mediaType = imageExts[ext];
        if (!mediaType) {
          errors.push(`${trimmed} 不是支持的图片格式`);
          continue;
        }
        const data = await vscode.workspace.fs.readFile(uri);
        images.push({ media_type: mediaType, data: Buffer.from(data).toString("base64") });
      } catch (error) {
        errors.push(`${trimmed}：${error instanceof Error ? error.message : "无法读取"}`);
      }
    }
    if (images.length > 0) {
      this.postMessage({ type: "addAttachments", images, textSnippets: [] });
    }
    if (errors.length > 0) {
      this.addSessionSystemMessage(sessionId ?? "", `添加图片时跳过：\n${errors.map((item) => `- ${item}`).join("\n")}`);
    }
    if (images.length > 0) {
      this.addSessionSystemMessage(sessionId ?? "", `已添加 ${images.length} 张图片到下一条消息。`);
    }
  }

  private runShellCommand(rawCommand: string): void {
    const command = rawCommand.trim();
    if (!command) {
      this.addMessage("system", "用法：! <cmd>");
      return;
    }

    const activeUri = vscode.window.activeTextEditor?.document.uri;
    const activeFolder = activeUri
      ? vscode.workspace.getWorkspaceFolder(activeUri)
      : undefined;
    const workspaceFolder = activeFolder ?? vscode.workspace.workspaceFolders?.[0];
    const cwd = workspaceFolder?.uri.fsPath ?? process.cwd();
    const terminalIsLive = this.shellTerminal !== null
      && vscode.window.terminals.includes(this.shellTerminal);

    let terminal = this.shellTerminal;
    if (!terminalIsLive || this.shellTerminalCwd !== cwd || terminal === null) {
      terminal = vscode.window.createTerminal({
        name: "CrabCode Shell",
        cwd,
      });
      this.shellTerminal = terminal;
      this.shellTerminalCwd = cwd;
    }

    terminal.show(false);
    terminal.sendText(command, true);
    this.outputChannel?.appendLine(`[CrabCode][Shell][${cwd}] ${command}`);
    this.addMessage("system", `已在终端运行：${command}`);
  }

  private async fetchGoal(): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const url = new URL(this._gatewayUrl("/config/goal"));
      url.searchParams.set("session_id", sessionId);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) throw new Error(`goal lookup failed: ${response.status}`);
      const data = await response.json() as { goal?: {
        objective: string; status: string; token_budget?: number | null; tokens_used?: number;
      } | null };
      if (!data.goal) {
        this.addSessionSystemMessage(sessionId, "当前没有设置 Goal。");
        return;
      }
      const goal = data.goal;
      const budget = goal.token_budget == null
        ? "无预算"
        : `${(goal.tokens_used ?? 0).toLocaleString()} / ${goal.token_budget.toLocaleString()}`;
      this.addSessionSystemMessage(sessionId, `## Goal\n\n${goal.objective}\n\n**状态：** ${goal.status} · **Tokens：** ${budget}`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `读取 Goal 失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async manageGoal(
    action: string,
    objective: string | null,
    tokenBudget: number | null,
    budgetWasSet: boolean,
  ): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const body: Record<string, unknown> = { action, session_id: sessionId };
      if (objective !== null) body.objective = objective;
      if (budgetWasSet) body.token_budget = tokenBudget;
      const response = await fetch(this._gatewayUrl("/config/goal"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(`goal update failed: ${response.status}`);
      await this.fetchGoal();
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `更新 Goal 失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async fetchTasks(): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const url = new URL(this._gatewayUrl("/tasks"));
      url.searchParams.set("session_id", sessionId);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) throw new Error(`task list failed: ${response.status}`);
      const tasks = await response.json() as BackgroundTaskInfo[];
      if (tasks.length === 0) {
        this.addSessionSystemMessage(sessionId, "没有后台任务。");
        return;
      }
      const lines = ["## 后台任务", ""];
      tasks.forEach((task) => {
        lines.push(`- \`${task.task_id.slice(0, 8)}\` · **${task.status || "unknown"}** · ${task.task_type || "task"} · ${task.description || ""}`);
      });
      this.addSessionSystemMessage(sessionId, lines.join("\n"));
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `读取后台任务失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async fetchTask(taskId: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const url = new URL(this._gatewayUrl(`/tasks/${encodeURIComponent(taskId)}`));
      url.searchParams.set("session_id", sessionId);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) throw new Error(`task lookup failed: ${response.status}`);
      const task = await response.json() as BackgroundTaskInfo;
      const lines = [
        `## Task \`${task.task_id}\``,
        "",
        `**状态：** ${task.status || "unknown"}`,
        `**类型：** ${task.task_type || "task"} · **来源：** ${task.source || "unknown"}`,
        `**描述：** ${task.description || "（无）"}`,
      ];
      if (task.agent_id) lines.push(`**Agent：** \`${task.agent_id}\``);
      if (task.output_file) lines.push(`**输出：** \`${task.output_file}\``);
      if (task.error) lines.push(`**错误：** ${task.error}`);
      this.addSessionSystemMessage(sessionId, lines.join("\n"));
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `读取任务失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async fetchTaskOutput(taskId: string, lines = 200): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const url = new URL(this._gatewayUrl(`/tasks/${encodeURIComponent(taskId)}/output`));
      url.searchParams.set("session_id", sessionId);
      url.searchParams.set("lines", String(Math.max(1, Math.min(10_000, Math.trunc(lines)))));
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) throw new Error(`task output failed: ${response.status}`);
      const data = await response.json() as { task_id?: string; path?: string | null; lines?: string[]; truncated?: boolean };
      const body = data.lines?.length ? data.lines.join("\n") : "（无输出）";
      this.addSessionSystemMessage(
        sessionId,
        `## Task Output \`${data.task_id || taskId}\`${data.truncated ? "（已截断）" : ""}\n\n\`\`\`\n${body}\n\`\`\`${data.path ? `\n\n路径：\`${data.path}\`` : ""}`,
      );
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `读取任务输出失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async stopTask(taskId: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/tasks/stop"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ task_id: taskId, session_id: sessionId }),
      });
      if (!response.ok) throw new Error(`task stop failed: ${response.status}`);
      this.addSessionSystemMessage(sessionId, `已请求停止后台任务 \`${taskId.slice(0, 8)}\`。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `停止后台任务失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async fetchPeers(): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const url = new URL(this._gatewayUrl("/peer/list"));
      url.searchParams.set("session_id", sessionId);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) throw new Error(`peer list failed: ${response.status}`);
      const peers = await response.json() as PeerInfo[];
      if (peers.length === 0) {
        this.addSessionSystemMessage(sessionId, "没有可消息联系的其他会话。");
        return;
      }
      const lines = ["## CrabCode 会话 Peers", ""];
      peers.forEach((peer) => lines.push(`- **${peer.name}** · \`${peer.session_id.slice(0, 8)}\` · ${peer.permission_class || "prompting"} · ${peer.cwd}`));
      this.addSessionSystemMessage(sessionId, lines.join("\n"));
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `读取 Peers 失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async sendPeerMessage(to: string, text: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/peer/send"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ to, text, session_id: sessionId }),
      });
      if (!response.ok) throw new Error(`peer send failed: ${response.status}`);
      const delivery = await response.json() as { status: string; detail?: string };
      this.addSessionSystemMessage(sessionId, `Peer 消息 ${delivery.status}${delivery.detail ? `：${delivery.detail}` : ""}。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `发送 Peer 消息失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async fetchTeams(): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const url = new URL(this._gatewayUrl("/team/list"));
      url.searchParams.set("session_id", sessionId);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) throw new Error(`team list failed: ${response.status}`);
      const teams = await response.json() as string[];
      this.addSessionSystemMessage(sessionId, teams.length ? `## Teams\n\n${teams.map((id) => `- \`${id}\``).join("\n")}` : "没有活动 Team。");
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `读取 Team 失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async fetchTeamStatus(teamId: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const url = new URL(this._gatewayUrl(`/team/status/${encodeURIComponent(teamId)}`));
      url.searchParams.set("session_id", sessionId);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) throw new Error(`team status failed: ${response.status}`);
      const status = await response.json() as TeamStatusInfo;
      const lines = [`## Team: ${status.team_id}`, "", `**状态：** ${status.state}`, `**Teammates：** ${status.teammate_count ?? 0}/${status.max_teammates ?? 0}`];
      (status.teammates ?? []).forEach((member) => lines.push(`- ${member.name || member.agent_id.slice(0, 8)} · ${member.role} · ${member.state}`));
      const tasks = status.tasks;
      if (tasks) lines.push("", `**Tasks：** ${tasks.total ?? 0}（pending ${tasks.pending ?? 0} · claimed ${tasks.claimed ?? 0} · done ${tasks.completed ?? 0} · failed ${tasks.failed ?? 0}）`);
      this.addSessionSystemMessage(sessionId, lines.join("\n"));
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `读取 Team 状态失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async fetchTeamMessages(
    teamId: string,
    agentId?: string,
    unread = false,
  ): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const url = new URL(this._gatewayUrl(`/team/${encodeURIComponent(teamId)}/messages`));
      url.searchParams.set("session_id", sessionId);
      if (agentId) url.searchParams.set("agent_id", agentId);
      if (unread) url.searchParams.set("unread", "true");
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) throw new Error(`team messages failed: ${response.status}`);
      const messages = await response.json() as TeamMessageInfo[];
      const body = messages.length
        ? messages.slice(-50).map((message) => `- \`${(message.from_agent || "").slice(0, 8)} → ${(message.to_agent || "").slice(0, 8)}\`: ${message.text || ""}`).join("\n")
        : "（无消息）";
      this.addSessionSystemMessage(
        sessionId,
        `## Team Messages: ${teamId}${agentId ? ` · ${agentId.slice(0, 8)}` : ""}${unread ? " · unread" : ""}\n\n${body}`,
      );
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `读取 Team 消息失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async fetchTeamTasks(teamId: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const url = new URL(this._gatewayUrl(`/team/${encodeURIComponent(teamId)}/tasks`));
      url.searchParams.set("session_id", sessionId);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) throw new Error(`team tasks failed: ${response.status}`);
      const tasks = await response.json() as TeamTaskInfo[];
      const body = tasks.length
        ? tasks.map((task) => `- \`${task.id.slice(0, 8)}\` · **${task.status}** · ${task.description || ""}${task.assignee ? ` · ${task.assignee.slice(0, 8)}` : ""}`).join("\n")
        : "（无任务）";
      this.addSessionSystemMessage(sessionId, `## Team Tasks: ${teamId}\n\n${body}`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `读取 Team 任务失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async createTeam(name: string, maxTeammates: number | null): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/team/create"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ name, max_teammates: maxTeammates, session_id: sessionId }),
      });
      if (!response.ok) throw new Error(`team create failed: ${response.status}`);
      const data = await response.json() as { team_id: string };
      this.addSessionSystemMessage(sessionId, `Team \`${data.team_id}\` 已创建。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `创建 Team 失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async spawnTeamMember(
    teamId: string,
    prompt: string,
    role?: string,
    name?: string,
    modelProfile?: string,
  ): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/team/spawn"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({
          team_id: teamId,
          prompt,
          role: role || "worker",
          name: name || null,
          model_profile: modelProfile || null,
          session_id: sessionId,
        }),
      });
      if (!response.ok) throw new Error(`team spawn failed: ${response.status}`);
      const data = await response.json() as { team_id: string; agent_id: string };
      this.addSessionSystemMessage(sessionId, `Team \`${data.team_id}\` 已添加 teammate \`${data.agent_id.slice(0, 8)}\`。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `添加 Team teammate 失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async removeTeamMember(teamId: string, agentId: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/team/remove"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ team_id: teamId, agent_id: agentId, session_id: sessionId }),
      });
      if (!response.ok) throw new Error(await this.gatewayError(response));
      this.addSessionSystemMessage(sessionId, `Team ${teamId} 已移除 teammate ${agentId.slice(0, 8)}。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `移除 Team teammate 失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async sendTeamMessage(teamId: string, to: string, text: string, fromAgent?: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/team/message"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ team_id: teamId, to, text, from_agent: fromAgent || "", session_id: sessionId }),
      });
      if (!response.ok) throw new Error(`team message failed: ${response.status}`);
      const data = await response.json() as { message_id?: string };
      this.addSessionSystemMessage(sessionId, `已向 Team \`${teamId}\` 的 \`${to.slice(0, 8)}\` 发送消息${data.message_id ? `（${data.message_id.slice(0, 8)}）` : ""}。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `发送 Team 消息失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async broadcastTeamMessage(teamId: string, text: string, fromAgent?: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/team/broadcast"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ team_id: teamId, text, from_agent: fromAgent || "", session_id: sessionId }),
      });
      if (!response.ok) throw new Error(`team broadcast failed: ${response.status}`);
      const data = await response.json() as { recipient_count?: number };
      this.addSessionSystemMessage(sessionId, `Team \`${teamId}\` 广播已发送给 ${data.recipient_count ?? 0} 个成员。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `Team 广播失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async markTeamMessagesRead(
    teamId: string,
    agentId: string,
    messageIds?: string[],
  ): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/team/messages/read"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({
          team_id: teamId,
          agent_id: agentId,
          message_ids: messageIds,
          session_id: sessionId,
        }),
      });
      if (!response.ok) throw new Error(await this.gatewayError(response));
      const data = await response.json() as { marked_read?: number };
      this.addSessionSystemMessage(sessionId, `已将 Team ${teamId} 的 ${data.marked_read ?? 0} 条消息标记为已读。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `标记 Team 消息失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async addTeamTask(teamId: string, description: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/team/task/add"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ team_id: teamId, description, session_id: sessionId }),
      });
      if (!response.ok) throw new Error(`team task add failed: ${response.status}`);
      const data = await response.json() as { task_id: string };
      this.addSessionSystemMessage(sessionId, `Team 任务已添加：\`${data.task_id.slice(0, 8)}\`。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `添加 Team 任务失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async claimTeamTask(teamId: string, taskId: string, agentId?: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/team/task/claim"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ team_id: teamId, task_id: taskId, agent_id: agentId || "", session_id: sessionId }),
      });
      if (!response.ok) throw new Error(`team task claim failed: ${response.status}`);
      this.addSessionSystemMessage(sessionId, `Team 任务 \`${taskId.slice(0, 8)}\` 已认领。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `认领 Team 任务失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async completeTeamTask(teamId: string, taskId: string, result: string, agentId?: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/team/task/complete"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ team_id: teamId, task_id: taskId, result, agent_id: agentId || "", session_id: sessionId }),
      });
      if (!response.ok) throw new Error(`team task complete failed: ${response.status}`);
      this.addSessionSystemMessage(sessionId, `Team 任务 \`${taskId.slice(0, 8)}\` 已完成。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `完成 Team 任务失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async failTeamTask(teamId: string, taskId: string, reason: string, agentId?: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/team/task/fail"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ team_id: teamId, task_id: taskId, reason, agent_id: agentId || "", session_id: sessionId }),
      });
      if (!response.ok) throw new Error(await this.gatewayError(response));
      this.addSessionSystemMessage(sessionId, `Team 任务 ${taskId.slice(0, 8)} 已标记失败。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `标记 Team 任务失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async getTeamBridge(teamA: string, teamB: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const url = new URL(this._gatewayUrl(`/team/${encodeURIComponent(teamA)}/bridge/${encodeURIComponent(teamB)}`));
      url.searchParams.set("session_id", sessionId);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) throw new Error(await this.gatewayError(response));
      const bridge = await response.json() as TeamBridgeInfo;
      this.addSessionSystemMessage(sessionId, `Team Bridge \`${bridge.team_a}\` ↔ \`${bridge.team_b}\`：**${bridge.policy}**。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `读取 Team Bridge 失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async registerTeamBridge(
    teamA: string,
    teamB: string,
    policy: string = "allow_tagged",
  ): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    if (policy !== "allow_all" && policy !== "allow_tagged" && policy !== "deny") {
      this.addSessionSystemMessage(sessionId, "Bridge policy 必须是 allow_all、allow_tagged 或 deny。");
      return;
    }
    try {
      const response = await fetch(this._gatewayUrl("/team/bridge"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ team_a: teamA, team_b: teamB, policy, session_id: sessionId }),
      });
      if (!response.ok) throw new Error(await this.gatewayError(response));
      const bridge = await response.json() as TeamBridgeInfo;
      this.addSessionSystemMessage(sessionId, `Team Bridge 已设置：\`${bridge.team_a}\` ↔ \`${bridge.team_b}\` = **${bridge.policy}**。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `设置 Team Bridge 失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async sendCrossTeamMessage(
    fromTeam: string,
    toTeam: string,
    text: string,
    fromAgent?: string,
    toAgent?: string,
  ): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/team/cross-message"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({
          from_team: fromTeam,
          to_team: toTeam,
          from_agent: fromAgent || "",
          to_agent: toAgent || "",
          text,
          session_id: sessionId,
        }),
      });
      if (!response.ok) throw new Error(await this.gatewayError(response));
      const message = await response.json() as CrossTeamMessageInfo;
      this.addSessionSystemMessage(sessionId, `跨 Team 消息已发送：\`${message.id.slice(0, 8)}\`。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `发送跨 Team 消息失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async shutdownTeam(teamId: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    try {
      const response = await fetch(this._gatewayUrl("/team/shutdown"), {
        method: "POST",
        headers: { ...this._gatewayHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ team_id: teamId, session_id: sessionId }),
      });
      if (!response.ok) throw new Error(`team shutdown failed: ${response.status}`);
      this.addSessionSystemMessage(sessionId, `Team \`${teamId}\` 已关闭。`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId, `关闭 Team 失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async fetchPlanStatus(): Promise<void> {
    const sessionId = this.displayedSessionId || this.connection.sessionId;
    if (!sessionId) return;
    try {
      const url = new URL(this._gatewayUrl("/config/plan-status"));
      url.searchParams.set("session_id", sessionId);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) return;
      const data = (await response.json()) as { mode: string; in_plan_mode: boolean; plan: unknown };
      const mode = data.mode === "plan" ? "plan" : "agent";
      this.getSessionState(sessionId).mode = mode;
      this.postMessage({ type: "modeChange", mode });
      const lines = [`**模式：** ${data.mode}`, `**计划模式：** ${data.in_plan_mode ? "是" : "否"}`];
      if (data.plan) lines.push("", "**当前计划：**", "```json", JSON.stringify(data.plan, null, 2), "```");
      this.addSessionSystemMessage(sessionId, lines.join("\n"));
    } catch { /* ignore */ }
  }

  private async fetchLogs(lines: number, name: string | null = null, tail: number | null = null, clear = false): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    try {
      const url = new URL(this._gatewayUrl("/logs"));
      if (sessionId) url.searchParams.set("session_id", sessionId);
      if (name) url.searchParams.set("name", name);
      url.searchParams.set("lines", String(tail ?? lines));
      if (clear) url.searchParams.set("clear", "true");
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
      if (!response.ok) throw new Error(`logs request failed: ${response.status}`);
      const data = await response.json() as LogsResponse;
      if (!name) {
        const entries = data.logs ?? [];
        const body = entries.length
          ? entries.map((entry) => `- **${entry.name}** · \`${entry.path}\`${entry.state ? ` · ${entry.state}` : ""}`).join("\n")
          : "（无日志）";
        this.addSessionSystemMessage(sessionId ?? "", "## 后台日志\n\n" + body);
        return;
      }
      const content = data.lines?.length ? "```\n" + data.lines.join("\n") + "\n```" : "（无日志）";
      this.addSessionSystemMessage(sessionId ?? "", `## Log: ${name}${data.truncated ? "（已截断）" : ""}\n\n${content}${clear ? "\n\n已清空。" : ""}`);
    } catch (error) {
      this.addSessionSystemMessage(sessionId ?? "", `读取日志失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private stopLogFollow(): void {
    this.logFollowAbort?.abort();
    this.logFollowAbort = null;
  }

  private async followLogs(name: string): Promise<void> {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId;
    if (!sessionId) return;
    this.stopLogFollow();
    const controller = new AbortController();
    this.logFollowAbort = controller;
    try {
      const url = new URL(this._gatewayUrl("/logs/follow"));
      url.searchParams.set("name", name);
      url.searchParams.set("session_id", sessionId);
      const response = await fetch(url.toString(), { headers: this._gatewayHeaders(), signal: controller.signal });
      if (!response.ok || !response.body) throw new Error(`log follow failed: ${response.status}`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      this.addSessionSystemMessage(sessionId, `开始跟踪日志 \`${name}\`。再次执行 \`/logs --stop\` 停止。`);
      while (!controller.signal.aborted) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          const line = frame.split("\n").find((item) => item.startsWith("data:"));
          if (!line) continue;
          let text = line.slice(5).trim();
          try { text = JSON.parse(text) as string; } catch { /* raw SSE data */ }
          if (text) this.addSessionSystemMessage(sessionId, `\`\`\`\n${text}\n\`\`\``);
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) this.addSessionSystemMessage(sessionId, `日志跟踪失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      if (this.logFollowAbort === controller) this.logFollowAbort = null;
    }
  }

  private async resolveModelsFromSettingsOrGateway(): Promise<string[]> {
    const cfg = vscode.workspace.getConfiguration("crabcode");
    const configuredModels = cfg.get<string[]>("chatModels", []) ?? [];
    if (configuredModels.length > 0) {
      this.lastNonEmptyModelGroups = Object.fromEntries(
        configuredModels.map((name) => [name, "default"]),
      );
      return configuredModels;
    }

    const wsUrl = cfg.get<string>("serverUrl", "ws://localhost:4096/ws");
    const password = cfg.get<string>("password", "");

    try {
      const url = new URL(wsUrl);
      url.protocol = url.protocol === "wss:" ? "https:" : "http:";
      url.pathname = "/config/models";
      url.search = "";
      const sessionId = this.displayedSessionId ?? this.connection.sessionId;
      if (sessionId) url.searchParams.set("session_id", sessionId);

      const headers: Record<string, string> = {};
      if (password) {
        headers.Authorization = `Bearer ${password}`;
      }

      const response = await fetch(url.toString(), { headers });
      if (!response.ok) {
        return [];
      }

      const models = (await response.json()) as GatewayModelInfo[];
      const modelGroups = Object.fromEntries(
        models
          .filter((model) => typeof model.name === "string" && model.name.length > 0)
          .map((model) => [model.name, model.group || "default"]),
      );
      if (models.length > 0) this.lastNonEmptyModelGroups = modelGroups;
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
    const mode = normalizePermissionMode(
      cfg.get<string>("permissionMode", "default"),
    );

    if (models.length > 0) {
      const pick =
        defaultModel && models.includes(defaultModel) ? defaultModel : models[0];
      this.connection.sendSwitchModel(
        pick,
        this.connection.sessionId ?? undefined,
      );
    }
    this.connection.sendSetPermissionMode(
      mode,
      this.connection.sessionId ?? undefined,
    );
    void this.pushChatOptions();
  }

  private async pushChatOptions(): Promise<void> {
    const requestId = ++this.latestModelRequestId;
    const cfg = vscode.workspace.getConfiguration("crabcode");
    let fetchedModels: string[];
    let modelGroups: Record<string, string> = {};

    // Throttle: skip the HTTP fetch if we recently fetched and a fetch is
    // already in-flight.  Heartbeats every 10 s were causing a flood of
    // /config/models requests that could starve the event loop.
    const now = Date.now();
    const skipFetch =
      this._modelsFetchInProgress ||
      (now - this._lastModelsFetchTime < ChatPanelProvider.MODELS_FETCH_COOLDOWN_MS &&
        this.lastNonEmptyModels.length > 0);

    if (skipFetch) {
      fetchedModels = this.lastNonEmptyModels;
      modelGroups = this.lastNonEmptyModelGroups;
    } else {
      try {
        this._modelsFetchInProgress = true;
        fetchedModels = await this.resolveModelsFromSettingsOrGateway();
        modelGroups = this.lastNonEmptyModelGroups;
        this._lastModelsFetchTime = Date.now();
      } catch {
        fetchedModels = [];
      } finally {
        this._modelsFetchInProgress = false;
      }
    }
    if (!modelGroups) modelGroups = {};
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
    if (Object.keys(modelGroups).length === 0) {
      modelGroups = Object.fromEntries(models.map((name) => [name, "default"]));
    }
    const mode = normalizePermissionMode(
      cfg.get<string>("permissionMode", "default"),
    );
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
      modelGroups,
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
    this.ensureSessionIfNeeded();
    if (this.isBusy) {
      this.queueSteeringMessageOnState(this.currentState, text);
      this.connection.steer(text, {
        sessionId: this.displayedSessionId ?? this.connection.sessionId ?? undefined,
      });
    } else {
      this.addMessage("user", text);
      this.setBusy(true);
      this.sendForegroundMessage(text);
    }
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
    this.ensureSessionIfNeeded();
    if (this.isBusy) {
      this.queueSteeringMessageOnState(this.currentState, text, images);
      this.connection.steer(text, {
        sessionId: this.displayedSessionId ?? this.connection.sessionId ?? undefined,
        images,
      });
    } else {
      this.addMessage("user", text, images);
      this.setBusy(true);
      this.sendForegroundMessage(text, images);
    }
  }

  private sendForegroundMessage(text: string, images?: ImageAttachment[]): void {
    const sessionId = this.displayedSessionId ?? this.connection.sessionId ?? undefined;
    const operationId = this.connection.send(text, { sessionId, images });
    if (sessionId) {
      const state = this.getSessionState(sessionId);
      state.activeOperationId = operationId;
      this.busySessions.add(sessionId);
    }
  }

  private ensureSessionIfNeeded(): void {
    const cwd = vscode.workspace.workspaceFolders?.[0]?.uri?.fsPath ?? null;
    this.connection.ensureSession(cwd);
  }

  /** Whether an event can own the displayed foreground busy state. */
  private isForegroundOperationEvent(payload: EventPayload): boolean {
    if (payload.command_error) return false;
    if (payload.operation_scope === "background" || payload.operation_scope === "plan") {
      return false;
    }
    return payload.operation_scope === "foreground" || !!payload.operation_id;
  }

  /** Reject a terminal event that belongs to an older operation. */
  private terminalBelongsToState(payload: EventPayload, state: SessionState): boolean {
    if (payload.type !== "turn_complete") return false;
    if (payload.operation_scope === "background") return false;
    if (payload.reason === "history_restore") return false;
    return !(
      state.activeOperationId
      && payload.operation_id
      && state.activeOperationId !== payload.operation_id
    );
  }

  private handleServerEvent(payload: EventPayload): void {
    // History carries a session_id, but it is a replay snapshot rather than a
    // live turn event. Handle it before the generic session-event routing so a
    // reconnect/resume cannot accidentally treat it as a stream event.
    if (payload.type === "session_history") {
      const history = payload as SessionHistoryPayload;
      const sessionId = history.session_id || this.displayedSessionId || this.connection.sessionId;
      if (sessionId) this.handleSessionHistory(history, sessionId);
      return;
    }
    const eventSessionId = payload.session_id;
    const isSessionEvent = !!eventSessionId;

    if (isSessionEvent) {
      // Ensure we have a state object for this session
      const targetState = this.getSessionState(eventSessionId!);
      if (
        this.isForegroundOperationEvent(payload)
        && payload.operation_id
        && payload.type !== "turn_complete"
      ) {
        targetState.activeOperationId = payload.operation_id;
      }
      // Track busy state per-session for session list status dots.  A terminal
      // from an older operation must not clear a newer operation.
      if (this.terminalBelongsToState(payload, targetState)) {
        this.busySessions.delete(eventSessionId!);
      } else if (
        this.isForegroundOperationEvent(payload)
        && (payload.type === "stream_text" || payload.type === "thinking" || payload.type === "tool_use")
      ) {
        this.busySessions.add(eventSessionId!);
      }
      // Only update webview if this is the displayed session
      // Once displayedSessionId is set, reject events from other sessions
      const isDisplayed = this.displayedSessionId ? (eventSessionId === this.displayedSessionId) : false;

      // Route event to the target session state (even if not displayed)
      this.routeEventToState(payload, targetState, isDisplayed);
      return;
    }

    // Session-agnostic events (server.connected, server.heartbeat)
    switch (payload.type) {
      case "server.connected": {
        const connSid = this.connection.sessionId;
        if (connSid) {
          this.displayedSessionId = connSid;
          // Ensure state exists for this session
          const state = this.getSessionState(connSid);
          // Preserve busy state if this session was previously busy
          if (!state.isBusy && this.busySessions.has(connSid)) {
            state.isBusy = true;
          }
        }
        this.sendCurrentSessionInfo();
        this.notifyConfigurationChanged();
        break;
      }
      case "server.heartbeat": {
        // Only update session info on heartbeat — skip the expensive
        // notifyConfigurationChanged / pushChatOptions /config/models fetch.
        const connSid = this.connection.sessionId;
        if (connSid) {
          this.displayedSessionId = connSid;
          this.getSessionState(connSid);
        }
        this.sendCurrentSessionInfo();
        break;
      }
      case "error": {
        // WebSocket command validation errors are session-agnostic when the
        // command could not be resolved to a target.  Keep them visible in
        // the current panel, but do not clear a foreground turn: recoverable
        // Core errors and command failures are not turn boundaries.
        const sessionId = payload.session_id ?? this.displayedSessionId ?? this.connection.sessionId;
        if (sessionId) {
          this.routeEventToState(payload, this.getSessionState(sessionId), sessionId === this.displayedSessionId);
        } else {
          this.addMessage("system", `CrabCode：${payload.message}`);
        }
        break;
      }
      case "mode_change":
        this.currentState.mode = (payload as { mode: string }).mode === "plan" ? "plan" : "agent";
        this.postMessage({ type: "modeChange", mode: this.currentState.mode });
        break;
      case "plan_ready":
        this.handlePlanReadyOnState(this.currentState, (payload as PlanReadyPayload).plan, true);
        break;
    }
  }

  private routeEventToState(payload: EventPayload, state: SessionState, updateWebview: boolean): void {
    switch (payload.type) {
      case "stream_text":
        this.finalizeThinkingOnState(state, updateWebview);
        this.appendAssistantTextOnState(state, payload.text, updateWebview);
        break;
      case "thinking":
        this.handleThinkingOnState(state, payload.text, updateWebview);
        break;
      case "stream_mode":
        this.handleStreamModeOnState(state, payload as StreamModePayload, updateWebview);
        break;
      case "steering_applied":
        this.flushSteeringMessagesOnState(
          state,
          updateWebview,
          (payload as SteeringAppliedPayload).count ?? 1,
        );
        break;
      case "agent_state":
        this.handleAgentStateOnState(state, payload as AgentStatePayload, updateWebview);
        break;
      case "agent_output":
        this.handleAgentOutputOnState(state, payload as AgentOutputPayload, updateWebview);
        break;
      case "tool_use":
        this.finalizeThinkingOnState(state, updateWebview);
        this.handleToolUseOnState(state, payload as ToolUsePayload, updateWebview);
        break;
      case "tool_result":
        this.handleToolResultOnState(state, payload as ToolResultPayload, updateWebview);
        break;
      case "permission_response": {
        const response = payload as PermissionResponsePayload;
        const card = state.permissionCards.get(response.tool_use_id);
        if (card) {
          card.allowed = response.allowed;
          if (updateWebview) this.postMessage({ type: "permissionResolved", card });
        }
        break;
      }
      case "choice_response": {
        const response = payload as ChoiceResponsePayload;
        const card = state.choiceCards.get(response.tool_use_id);
        if (card) {
          card.selected = response.selected;
          card.pendingSelected = response.selected;
          card.cancelled = response.cancelled ?? false;
          card.completed = true;
          if (updateWebview) this.postMessage({ type: "choiceResolved", card });
        }
        break;
      }
      case "permission_request":
        this.handlePermissionRequestOnState(state, payload as PermissionRequestPayload, updateWebview);
        break;
      case "choice_request":
        this.handleChoiceRequestOnState(state, payload as ChoiceRequestPayload, updateWebview);
        break;
      case "plan_ready":
        this.handlePlanReadyOnState(state, (payload as PlanReadyPayload).plan, updateWebview);
        break;
      case "mode_change":
        state.mode = (payload as { mode: string }).mode === "plan" ? "plan" : "agent";
        if (updateWebview) this.postMessage({ type: "modeChange", mode: state.mode });
        break;
      case "compact": {
        const compact = payload as CompactPayload;
        const counts = compact.messages_before || compact.messages_after
          ? `（${compact.messages_before} → ${compact.messages_after} 条消息）`
          : "";
        this.addMessageOnState(
          state,
          "system",
          `对话已压缩${counts}${compact.summary ? `\n\n${compact.summary}` : ""}`,
          updateWebview,
        );
        break;
      }
      case "peer_message": {
        const peer = payload as PeerMessagePayload;
        this.addMessageOnState(
          state,
          "system",
          `[Peer ${peer.from_name} · ${peer.from_session_id.slice(0, 8)}] ${peer.text}`,
          updateWebview,
        );
        break;
      }
      case "team_message": {
        const team = payload as TeamMessagePayload;
        this.addMessageOnState(
          state,
          "system",
          `  [Team ${team.team_id.slice(0, 8)}] ${team.from_agent.slice(0, 8)} → ${team.to_agent.slice(0, 8)}: ${team.text}`,
          updateWebview,
        );
        break;
      }
      case "team_state": {
        const team = payload as TeamStatePayload;
        this.addMessageOnState(
          state,
          "system",
          `  [Team ${team.team_id.slice(0, 8)}] ${team.agent_id.slice(0, 8)} ${team.old_state} → ${team.new_state}`,
          updateWebview,
        );
        break;
      }
      case "task_update": {
        const task = payload as TaskUpdatePayload;
        const detail = task.description ? `：${task.description}` : "";
        this.addMessageOnState(
          state,
          "system",
          `  [Team ${task.team_id.slice(0, 8)}] task ${task.task_id.slice(0, 8)} ${task.status}${detail}`,
          updateWebview,
        );
        break;
      }
      case "schedule_run": {
        const schedule = payload as ScheduleRunPayload;
        const detail = schedule.error_message || schedule.result_summary || schedule.status;
        this.addMessageOnState(
          state,
          "system",
          `定时任务 \`${schedule.job_id.slice(0, 8)}\`：${schedule.status}，${detail}`,
          updateWebview,
        );
        break;
      }
      case "snapshot": {
        const snapshot = payload as SnapshotPayload;
        const files = snapshot.files?.length ? `：${snapshot.files.join(", ")}` : "";
        this.addMessageOnState(
          state,
          "system",
          `已创建文件快照 \`${snapshot.snapshot_id.slice(0, 8)}\`${files}`,
          updateWebview,
        );
        break;
      }
      case "revert": {
        const revert = payload as RevertPayload;
        const files = revert.files_restored?.length ? `：${revert.files_restored.join(", ")}` : "";
        this.addMessageOnState(
          state,
          "system",
          `已恢复文件快照 \`${revert.snapshot_id.slice(0, 8)}\`${files}`,
          updateWebview,
        );
        break;
      }
      case "file_change":
        this.handleFileChangeOnState(state, payload as FileChangePayload, updateWebview);
        break;
      case "error":
        this.addMessageOnState(state, "system", `CrabCode：${payload.message}`, updateWebview);
        if (payload.command_error && payload.command === "steer_message") {
          state.pendingSteeringMessages.shift();
          if (updateWebview) {
            this.postMessage({
              type: "steeringQueue",
              messages: state.pendingSteeringMessages,
            });
          }
        }
        if (
          payload.command_error
          && payload.command === "send_message"
          && payload.error_type !== "operation_conflict"
          && payload.operation_id
          && state.activeOperationId === payload.operation_id
        ) {
          state.activeOperationId = null;
          state.isBusy = false;
          if (payload.session_id) this.busySessions.delete(payload.session_id);
          if (updateWebview) this.postMessage({ type: "busyState", busy: false });
        }
        break;
      case "turn_complete": {
        const usage = buildContextUsageStatus(payload as TurnCompletePayload);
        if (usage) {
          state.contextUsage = usage;
          if (updateWebview) this.postMessage({ type: "contextUsage", usage });
        }
        if (!this.terminalBelongsToState(payload, state)) break;
        this.finalizeThinkingOnState(state, updateWebview);
        state.activeOperationId = null;
        state.isBusy = false;
        state.batchDenied = false;
        if (updateWebview) {
          this.postMessage({ type: "busyState", busy: false });
          setTimeout(() => void this.fetchAndSendCurrentTitle(), 2000);
        }
        break;
      }
    }
  }

  private async fetchAndApplySessionHistory(sessionId: string): Promise<void> {
    const url = new URL(this._gatewayUrl("/session/messages"));
    url.searchParams.set("session_id", sessionId);
    const response = await fetch(url.toString(), { headers: this._gatewayHeaders() });
    if (!response.ok) {
      throw new Error(`session history failed: ${response.status}`);
    }
    const data: unknown = await response.json();
    if (!Array.isArray(data)) {
      throw new Error("session history response was not an array");
    }
    this.handleSessionHistory(
      {
        type: "session_history",
        session_id: sessionId,
        messages: data as SessionMessagePayload[],
      },
      sessionId,
    );
  }

  /** Rebuild the renderable history from Core's structured message projection. */
  private handleSessionHistory(payload: SessionHistoryPayload, sessionId = payload.session_id): void {
    if (!sessionId) return;
    const state = this.getSessionState(sessionId);
    state.history = [];
    state.messages = [];
    state.toolCards.clear();
    state.thinkingCards.clear();
    state.choiceCards.clear();
    state.permissionCards.clear();
    state.planCards.clear();
    state.activeThinkingId = null;
    state.contextUsage = null;

    const updateWebview = this.displayedSessionId === sessionId;
    const fallbackId = () => `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const timestampFor = (message: SessionMessagePayload): number => {
      const parsed = typeof message.timestamp === "string" ? Date.parse(message.timestamp) : NaN;
      return Number.isFinite(parsed) ? parsed : Date.now();
    };
    const roleFor = (value: unknown): ChatMessageRole => (
      value === "user" || value === "assistant" || value === "system"
        ? value
        : "system"
    );
    const asRecord = (value: unknown): Record<string, unknown> => (
      value && typeof value === "object" ? value as Record<string, unknown> : {}
    );
    const asInput = (value: unknown): Record<string, unknown> => asRecord(value);

    for (const message of payload.messages ?? []) {
      const msg = message as SessionMessagePayload;
      const role = roleFor(msg.role);
      const baseId = typeof msg.uuid === "string" && msg.uuid ? msg.uuid : fallbackId();
      const timestamp = timestampFor(msg);
      const parentId = msg.parent_uuid ?? null;
      const origin = msg.origin ?? null;
      const usage = msg.usage ?? null;
      const content = msg.content;
      const blocks: Record<string, unknown>[] = typeof content === "string"
        ? [{ type: "text", text: content }]
        : Array.isArray(content) ? content.map(asRecord) : [];
      let pendingText = "";
      let pendingImages: ImageAttachment[] = [];
      let segment = 0;

      const flushText = (): void => {
        if (!pendingText && pendingImages.length === 0) return;
        const chatMsg: ChatMessage = {
          id: segment === 0 ? baseId : `${baseId}:part-${segment}`,
          role,
          text: pendingText,
          timestamp,
          images: pendingImages.length > 0 ? pendingImages : undefined,
          parentId,
          origin,
          usage,
        };
        state.messages.push(chatMsg);
        state.history.push({ kind: "message", message: chatMsg });
        pendingText = "";
        pendingImages = [];
        segment += 1;
      };

      for (let index = 0; index < blocks.length; index += 1) {
        const block = blocks[index];
        const type = typeof block.type === "string" ? block.type : "";
        if (type === "text") {
          if (typeof block.text === "string") pendingText += block.text;
          continue;
        }
        if (type === "image") {
          const source = asRecord(block.source);
          if (typeof source.data === "string" && source.data) {
            pendingImages.push({
              media_type: typeof source.media_type === "string" ? source.media_type : "image/png",
              data: source.data,
            });
          }
          continue;
        }
        if (type === "thinking") {
          flushText();
          const text = typeof block.thinking === "string" ? block.thinking : "";
          if (!text) continue;
          const card: ThinkingCard = {
            id: `${baseId}:thinking-${index}`,
            text,
            collapsed: true,
          };
          state.thinkingCards.set(card.id, card);
          state.history.push({ kind: "thinking", card });
          continue;
        }
        if (type === "tool_use") {
          flushText();
          const toolId = typeof block.id === "string" && block.id ? block.id : `${baseId}:tool-${index}`;
          const existing = state.toolCards.get(toolId);
          if (existing) continue;
          const card: ToolCard = {
            id: toolId,
            toolName: typeof block.name === "string" ? block.name : "tool",
            input: asInput(block.input),
            result: null,
            isError: false,
            collapsed: false,
          };
          state.toolCards.set(toolId, card);
          state.history.push({ kind: "tool", card });
          continue;
        }
        if (type === "tool_result") {
          flushText();
          const toolId = typeof block.tool_use_id === "string" ? block.tool_use_id : "";
          if (!toolId) continue;
          const result = typeof block.content === "string" ? block.content : String(block.content ?? "");
          const isError = block.is_error === true;
          let card = state.toolCards.get(toolId);
          if (!card) {
            card = {
              id: toolId,
              toolName: "tool",
              input: {},
              result: null,
              isError: false,
              collapsed: false,
            };
            state.toolCards.set(toolId, card);
            state.history.push({ kind: "tool", card });
          }
          card.result = result;
          card.isError = isError;
          card.collapsed = !isError;
          continue;
        }
        // Signatures and future block types carry no standalone UI surface.
      }
      flushText();
    }

    state.isBusy = this.busySessions.has(sessionId);
    if (updateWebview) {
      this.postMessage({ type: "history", items: state.history });
      this.postMessage({ type: "busyState", busy: state.isBusy });
      this.postMessage({ type: "contextUsage", usage: state.contextUsage });
    }
    // The server may not emit turn_complete when the projection came from
    // disk, so retrieve the persisted context counters as a second step.
    void this.fetchAndApplyContextUsage(sessionId);
  }

  private handleThinking(chunk: string): void {
    this.handleThinkingOnState(this.currentState, chunk, true);
  }

  private handleThinkingOnState(state: SessionState, chunk: string, updateWebview: boolean): void {
    state.isBusy = true;
    if (updateWebview) this.postMessage({ type: "busyState", busy: true });
    if (state.activeThinkingId) {
      const card = state.thinkingCards.get(state.activeThinkingId);
      if (card) {
        card.text += chunk;
        if (updateWebview) this.postMessage({ type: "appendThinking", id: card.id, chunk });
      }
    } else {
      const id = `thinking-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const card: ThinkingCard = { id, text: chunk, collapsed: false };
      state.activeThinkingId = id;
      state.thinkingCards.set(id, card);
      state.history.push({ kind: "thinking", card });
      if (updateWebview) this.postMessage({ type: "thinkingStart", card });
    }
  }

  /** Finalize the current thinking block (collapse it). */
  private finalizeThinking(): void {
    this.finalizeThinkingOnState(this.currentState, true);
  }

  private finalizeThinkingOnState(state: SessionState, updateWebview: boolean): void {
    if (!state.activeThinkingId) return;
    const card = state.thinkingCards.get(state.activeThinkingId);
    if (card) {
      card.collapsed = true;
      if (updateWebview) this.postMessage({ type: "thinkingEnd", id: card.id, collapsed: true });
    }
    state.activeThinkingId = null;
  }

  private setBusy(busy: boolean): void {
    if (this.isBusy === busy) return;
    this.isBusy = busy;
    if (!busy) {
      this.clearInterruptRetry();
    }
    this.postMessage({ type: "busyState", busy });
  }

  private scheduleInterruptRetry(sessionId: string, operationId?: string): void {
    this.clearInterruptRetry();
    this.interruptRetryTimer = setTimeout(() => {
      const state = this.getSessionState(sessionId);
      const stillOwnsOperation = (): boolean => (
        operationId === undefined || state.activeOperationId === operationId
      );
      if (!state.isBusy || !stillOwnsOperation()) {
        return;
      }
      const result = this.connection.sendInterrupt(sessionId, operationId);
      if (result === "sent") {
        this.interruptRetryTimer = setTimeout(() => {
          if (state.isBusy && stillOwnsOperation()) {
            state.isBusy = false;
            if (operationId === undefined || state.activeOperationId === operationId) {
              state.activeOperationId = null;
            }
            this.busySessions.delete(sessionId);
            if (this.displayedSessionId === sessionId) {
              this.postMessage({ type: "busyState", busy: false });
              this.postMessage({ type: "interruptResult", result: "timeout" });
            }
          }
        }, 5000);
      } else {
        state.isBusy = false;
        if (operationId === undefined || state.activeOperationId === operationId) {
          state.activeOperationId = null;
        }
        this.busySessions.delete(sessionId);
        if (this.displayedSessionId === sessionId) {
          this.postMessage({ type: "busyState", busy: false });
        }
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
    this.handleStreamModeOnState(this.currentState, payload, true);
  }

  private handleStreamModeOnState(state: SessionState, payload: StreamModePayload, updateWebview: boolean): void {
    switch (payload.mode) {
      case "requesting":
      case "thinking":
      case "responding":
        state.isBusy = true;
        if (updateWebview) this.postMessage({ type: "busyState", busy: true });
        break;
      case "tool-input":
        state.isBusy = true;
        state.batchDenied = false;
        if (updateWebview) this.postMessage({ type: "busyState", busy: true });
        break;
      case "tool-running":
        state.isBusy = true;
        if (updateWebview) this.postMessage({ type: "busyState", busy: true });
        break;
      default:
        break;
    }
  }

  private handleAgentStateOnState(state: SessionState, payload: AgentStatePayload, updateWebview: boolean): void {
    const shortId = payload.agent_id.slice(0, 8);
    const text = `  agent ${shortId} · ${payload.status} · ${payload.title}\n`;
    this.finalizeThinkingOnState(state, updateWebview);
    this.appendAssistantTextOnState(state, text, updateWebview);
    const terminal = payload.status === "completed"
      || payload.status === "failed"
      || payload.status === "cancelled";
    // A managed-agent terminal is not the parent turn boundary, and detached
    // background agents must never lock or unlock the foreground composer.
    if (!terminal && payload.operation_scope !== "background") {
      state.isBusy = true;
      if (updateWebview) this.postMessage({ type: "busyState", busy: true });
    }
  }

  private handleAgentOutput(payload: AgentOutputPayload): void {
    this.handleAgentOutputOnState(this.currentState, payload, true);
  }

  private handleAgentOutputOnState(state: SessionState, payload: AgentOutputPayload, updateWebview: boolean): void {
    switch (payload.stream) {
      case "text":
        this.finalizeThinkingOnState(state, updateWebview);
        this.appendAssistantTextOnState(state, payload.text, updateWebview);
        if (payload.operation_scope !== "background") {
          state.isBusy = true;
          if (updateWebview) this.postMessage({ type: "busyState", busy: true });
        }
        break;
      case "thinking":
        if (payload.operation_scope !== "background") {
          state.isBusy = true;
          if (updateWebview) this.postMessage({ type: "busyState", busy: true });
        }
        break;
      default:
        break;
    }
  }

  private handleToolUse(payload: ToolUsePayload): void {
    this.handleToolUseOnState(this.currentState, payload, true);
  }

  private handleToolUseOnState(state: SessionState, payload: ToolUsePayload, updateWebview: boolean): void {
    const card: ToolCard = {
      id: payload.tool_use_id,
      toolName: payload.tool_name,
      input: payload.tool_input,
      result: null,
      isError: false,
      collapsed: false,
    };
    state.toolCards.set(payload.tool_use_id, card);
    state.history.push({ kind: "tool", card });
    if (updateWebview) this.postMessage({ type: "toolUse", card });
  }

  private handleToolResult(payload: ToolResultPayload): void {
    this.handleToolResultOnState(this.currentState, payload, true);
  }

  private handleToolResultOnState(state: SessionState, payload: ToolResultPayload, updateWebview: boolean): void {
    let card = state.toolCards.get(payload.tool_use_id);
    if (!card) {
      card = {
        id: payload.tool_use_id,
        toolName: payload.tool_name || "tool",
        input: payload.tool_input ?? {},
        result: null,
        isError: false,
        collapsed: false,
      };
      state.toolCards.set(payload.tool_use_id, card);
      state.history.push({ kind: "tool", card });
      if (updateWebview) this.postMessage({ type: "toolUse", card });
    }
    card.result = payload.result_for_display ?? payload.result;
    card.isError = payload.is_error ?? false;
    card.collapsed = !card.isError;
    if (updateWebview) this.postMessage({ type: "toolResult", card });
  }

  private handleChoiceRequest(payload: ChoiceRequestPayload): void {
    this.handleChoiceRequestOnState(this.currentState, payload, true);
  }

  private handleChoiceRequestOnState(state: SessionState, payload: ChoiceRequestPayload, updateWebview: boolean): void {
    const card: ChoiceCard = {
      id: payload.tool_use_id,
      question: payload.question,
      options: payload.options,
      multiple: payload.multiple ?? false,
      selected: [],
      pendingSelected: [],
      completed: false,
      cancelled: false,
      agentId: payload.agent_id ?? null,
    };
    state.choiceCards.set(payload.tool_use_id, card);
    state.history.push({ kind: "choice", card });
    if (updateWebview) this.postMessage({ type: "choiceRequest", card });
  }

  private handlePermissionRequest(payload: PermissionRequestPayload): void {
    this.handlePermissionRequestOnState(this.currentState, payload, true);
  }

  private handlePermissionRequestOnState(state: SessionState, payload: PermissionRequestPayload, updateWebview: boolean): void {
    const card: PermissionCard = {
      id: payload.tool_use_id,
      toolName: payload.tool_name,
      input: payload.tool_input,
      reason: payload.reason ?? null,
      allowed: null,
      agentId: payload.agent_id ?? null,
      requestKind: payload.request_kind ?? "tool",
    };
    state.permissionCards.set(payload.tool_use_id, card);
    state.history.push({ kind: "permission", card });

    // If this batch already had a denial, auto-deny immediately
    if (state.batchDenied && card.requestKind !== "peer_message") {
      card.allowed = false;
      const cmd = buildPermissionResponseCommand(card.id, false, {
        agentId: card.agentId ?? undefined,
        sessionId: this.displayedSessionId ?? this.connection.sessionId ?? undefined,
      });
      this.connection.sendRaw(serializeCommand(cmd));
      if (updateWebview) this.postMessage({ type: "permissionResolved", card });
      return;
    }

    if (updateWebview) this.postMessage({ type: "permissionRequest", card });
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
    const cmd = buildChoiceResponseCommand(id, selected, {
      cancelled,
      agentId: card.agentId ?? undefined,
      sessionId: this.displayedSessionId ?? this.connection.sessionId ?? undefined,
    });
    this.connection.sendRaw(serializeCommand(cmd));
    this.postMessage({ type: "choiceResolved", card });
  }

  private handlePlanReadyOnState(state: SessionState, plan: Record<string, unknown>, updateWebview: boolean): void {
    this.finalizeThinkingOnState(state, updateWebview);
    state.mode = "plan";
    if (updateWebview) this.postMessage({ type: "modeChange", mode: "plan" });
    const card: PlanCard = {
      id: `plan-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      plan,
      status: "pending",
    };
    state.planCards.set(card.id, card);
    state.history.push({ kind: "plan", card });
    if (updateWebview) this.postMessage({ type: "planReady", card });
  }

  private respondToPlan(id: string, action: "execute" | "revise" | "cancel"): void {
    const card = this.planCards.get(id);
    if (!card || card.status !== "pending") {
      return;
    }
    card.status = action === "execute" ? "executing" : action === "revise" ? "revising" : "cancelled";
    this.connection.sendPlanAction(
      action,
      card.plan,
      this.displayedSessionId || this.connection.sessionId || undefined,
    );
    this.postMessage({ type: "planResolved", card });
  }

  private respondToPermission(id: string, allowed: boolean, alwaysAllow = false, feedback?: string): void {
    const card = this.permissionCards.get(id);
    if (!card || card.allowed !== null) {
      return;
    }
    card.allowed = allowed;
    const cmd = buildPermissionResponseCommand(id, allowed, {
      alwaysAllow,
      feedback,
      agentId: card.agentId ?? undefined,
      sessionId: this.displayedSessionId ?? this.connection.sessionId ?? undefined,
    });
    this.connection.sendRaw(serializeCommand(cmd));
    this.postMessage({ type: "permissionResolved", card });

    // When denying, mark the batch and auto-deny all other pending cards in this round
    if (!allowed && card.requestKind !== "peer_message") {
      this.currentState.batchDenied = true;
      for (const [otherId, otherCard] of this.permissionCards) {
        if (
          otherId !== id &&
          otherCard.allowed === null &&
          otherCard.requestKind !== "peer_message"
        ) {
          otherCard.allowed = false;
          const otherCmd = buildPermissionResponseCommand(otherId, false, {
            agentId: otherCard.agentId ?? undefined,
            sessionId: this.displayedSessionId ?? this.connection.sessionId ?? undefined,
          });
          this.connection.sendRaw(serializeCommand(otherCmd));
          this.postMessage({ type: "permissionResolved", card: otherCard });
        }
      }
    }
  }

  private handleFileChange(payload: FileChangePayload): void {
    this.handleFileChangeOnState(this.currentState, payload, true);
  }

  private handleFileChangeOnState(state: SessionState, payload: FileChangePayload, updateWebview: boolean): void {
    state.history.push({ kind: "fileChange", payload });
    if (updateWebview) this.postMessage({ type: "fileChange", payload });
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
    this.addMessageOnState(this.currentState, role, text, true, images);
  }

  private queueSteeringMessageOnState(
    state: SessionState,
    text: string,
    images?: ImageAttachment[],
  ): void {
    state.pendingSteeringMessages.push({
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      role: "user",
      text,
      timestamp: Date.now(),
      images,
    });
    if (state === this.currentState) {
      this.postMessage({
        type: "steeringQueue",
        messages: state.pendingSteeringMessages,
      });
    }
  }

  private flushSteeringMessagesOnState(
    state: SessionState,
    updateWebview: boolean,
    count: number,
  ): void {
    if (state.pendingSteeringMessages.length === 0) return;
    const appliedCount = Math.max(0, Math.trunc(count));
    const queued = state.pendingSteeringMessages.splice(0, appliedCount);
    for (const message of queued) {
      state.messages.push(message);
      state.history.push({ kind: "message", message });
      if (updateWebview) this.postMessage({ type: "newMessage", message });
    }
    if (updateWebview) {
      this.postMessage({
        type: "steeringQueue",
        messages: state.pendingSteeringMessages,
      });
    }
  }

  private addMessageOnState(state: SessionState, role: ChatMessageRole, text: string, updateWebview: boolean, images?: ImageAttachment[]): void {
    const msg: ChatMessage = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      role,
      text,
      timestamp: Date.now(),
      images,
    };
    state.messages.push(msg);
    state.history.push({ kind: "message", message: msg });
    if (updateWebview) this.postMessage({ type: "newMessage", message: msg });
  }

  /** Append text to the last assistant message (streaming). */
  private appendAssistantText(chunk: string): void {
    this.appendAssistantTextOnState(this.currentState, chunk, true);
  }

  private appendAssistantTextOnState(state: SessionState, chunk: string, updateWebview: boolean): void {
    const lastHistory = state.history[state.history.length - 1];
    const lastMessage = state.messages[state.messages.length - 1];
    // If the most recent history item is a tool/thinking/permission/choice card,
    // a new tool result just finished — start a fresh assistant message so it
    // appears after the tool cards in the DOM instead of updating the pre-tool message.
    const lastHistoryIsCard = lastHistory && lastHistory.kind !== "message";
    if (!lastHistoryIsCard && lastMessage && lastMessage.role === "assistant") {
      lastMessage.text += chunk;
      if (updateWebview) this.postMessage({ type: "appendText", id: lastMessage.id, chunk });
    } else {
      this.addMessageOnState(state, "assistant", chunk, updateWebview);
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
      position: relative;
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
    .tool-card { --tool-tone: var(--accent); }
    .tool-card::before { background: color-mix(in srgb, var(--tool-tone) 64%, transparent); }
    .tool-kind-file { --tool-tone: #59a8f5; }
    .tool-kind-terminal { --tool-tone: #8bd17c; }
    .tool-kind-search { --tool-tone: #b69cff; }
    .tool-kind-web { --tool-tone: #4dc9c0; }
    .tool-kind-debug { --tool-tone: #f19b62; }
    .tool-kind-memory, .tool-kind-goal { --tool-tone: #e8bd55; }
    .tool-kind-agent, .tool-kind-team, .tool-kind-message { --tool-tone: #d48fe8; }
    .tool-kind-task, .tool-kind-schedule { --tool-tone: #64b9cb; }
    .tool-kind-checkpoint { --tool-tone: #ef7f88; }
    .tool-kind-checklist { --tool-tone: #79c56e; }
    .tool-kind-mode, .tool-kind-skill { --tool-tone: #a7a1ff; }
    .tool-card.is-error { --tool-tone: var(--vscode-errorForeground, #f48771); }
    .tool-card-header .icon {
      flex: 0 0 20px;
      width: 20px;
      height: 20px;
      color: var(--tool-tone);
      background: color-mix(in srgb, var(--tool-tone) 13%, transparent);
      font-family: var(--vscode-editor-font-family, monospace);
      font-weight: 700;
    }
    .tool-card-header .tool-name { flex: 0 1 auto; }
    .tool-technical-name {
      flex: 0 1 auto;
      max-width: 110px;
      overflow: hidden;
      padding: 1px 5px;
      border: 1px solid var(--border);
      border-radius: 4px;
      color: var(--text-muted);
      font: 9.5px/1.35 var(--vscode-editor-font-family, monospace);
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .tool-summary {
      flex: 1 1 120px;
      min-width: 60px;
      overflow: hidden;
      color: var(--text-muted);
      font: 10.5px/1.35 var(--vscode-editor-font-family, monospace);
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .tool-card-body {
      display: grid;
      gap: 10px;
      max-height: 420px;
    }
    .tool-card-section { min-width: 0; }
    .tool-card-section .timeline-meta { margin-bottom: 5px; }
    .tool-action-badge {
      display: inline-flex;
      margin: 0 0 6px;
      padding: 1px 7px;
      border: 1px solid color-mix(in srgb, var(--tool-tone) 30%, var(--border));
      border-radius: 999px;
      background: color-mix(in srgb, var(--tool-tone) 9%, transparent);
      color: var(--tool-tone);
      font-size: 10px;
      font-weight: 600;
    }
    .tool-detail-list {
      display: grid;
      gap: 1px;
      overflow: hidden;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--border);
    }
    .tool-detail-row {
      min-width: 0;
      display: grid;
      grid-template-columns: minmax(66px, 96px) minmax(0, 1fr);
      align-items: baseline;
      gap: 8px;
      padding: 5px 7px;
      background: color-mix(in srgb, var(--surface-soft) 82%, var(--vscode-editor-background));
    }
    .tool-detail-row > span {
      color: var(--text-muted);
      font-size: 10px;
    }
    .tool-detail-row > code {
      min-width: 0;
      overflow: hidden;
      color: var(--text);
      font-family: var(--vscode-editor-font-family, monospace);
      font-size: 10.5px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .tool-detail-row > code.path { cursor: pointer; color: var(--accent); }
    .tool-detail-row.stacked { grid-template-columns: 1fr; gap: 4px; }
    .tool-detail-row.stacked pre {
      max-height: 220px;
      overflow: auto;
      margin: 0;
      padding: 7px;
      border: 1px solid var(--border);
      border-radius: 5px;
      background: var(--vscode-editor-background);
    }
    .tool-chip-list { display: flex; flex-wrap: wrap; gap: 4px; }
    .tool-chip-list code {
      padding: 1px 5px;
      border-radius: 4px;
      background: var(--vscode-editor-background);
      color: var(--text-muted);
      font-size: 10px;
    }
    .tool-empty { color: var(--text-muted); font-size: 10.5px; }

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
    .request-card.plan {
      border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
      background: color-mix(in srgb, var(--accent) 5%, var(--surface-soft));
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
    .request-actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .permission-feedback-row {
      display: flex;
      gap: 6px;
      margin-top: 8px;
      align-items: center;
    }
    .permission-feedback-row.hidden { display: none; }
    .permission-feedback-input {
      flex: 1;
      padding: 5px 8px;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--bg);
      color: var(--fg);
      font-size: 12px;
      font-family: inherit;
      outline: none;
    }
    .permission-feedback-input:focus {
      border-color: var(--accent);
    }
    .plan-summary {
      white-space: pre-wrap;
      line-height: 1.45;
      font-size: 12px;
      color: var(--text-muted);
    }
    .plan-step-list {
      display: flex;
      flex-direction: column;
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .plan-step {
      display: grid;
      grid-template-columns: 24px 1fr;
      gap: 8px;
      padding: 8px;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: color-mix(in srgb, var(--vscode-editor-background) 55%, transparent);
    }
    .plan-step-index {
      color: var(--text-muted);
      font-size: 11px;
      font-weight: 700;
      text-align: right;
    }
    .plan-step-title {
      font-size: 12px;
      font-weight: 650;
      line-height: 1.35;
    }
    .plan-step-desc {
      margin-top: 3px;
      white-space: pre-wrap;
      color: var(--text-muted);
      font-size: 11.5px;
      line-height: 1.4;
    }
    .plan-step-meta {
      margin-top: 5px;
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
    }
    .plan-chip {
      border: 1px solid color-mix(in srgb, var(--accent) 24%, var(--border));
      border-radius: 999px;
      padding: 1px 6px;
      font-size: 10.5px;
      color: var(--text-muted);
      background: color-mix(in srgb, var(--accent) 8%, transparent);
    }

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
      position: relative;
      z-index: 20;
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
      position: relative;
      width: 100%;
      min-width: 0;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      transition: border-color 0.15s, box-shadow 0.15s;
    }
    #composer-card.is-steering {
      border-color: color-mix(in srgb, var(--accent) 58%, var(--border-strong));
      box-shadow: var(--shadow-md), 0 0 0 1px color-mix(in srgb, var(--accent) 12%, transparent);
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
    #steering-hint {
      display: none;
      align-items: flex-start;
      gap: 7px;
      padding: 0 12px 7px;
      color: var(--text-muted);
      font-size: 10.5px;
      line-height: 1.35;
    }
    #steering-hint.visible { display: flex; }
    .steering-dot {
      width: 6px;
      height: 6px;
      flex: 0 0 auto;
      border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent);
      margin-top: 4px;
    }
    .steering-copy {
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    #steering-queue-preview {
      display: none;
      color: color-mix(in srgb, var(--vscode-foreground) 78%, transparent);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      max-width: 100%;
    }
    #steering-queue-preview.visible { display: block; }

    /* ── Slash command popup ───────────────────────────────────── */
    #slash-popup {
      position: fixed;
      top: 0;
      left: 0;
      background: var(--vscode-menu-background, var(--vscode-input-background));
      border: 1px solid var(--vscode-menu-border, var(--border));
      border-radius: 10px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.35);
      z-index: 200;
      overflow: hidden;
      max-height: 320px;
      min-width: 0;
      display: flex;
      flex-direction: column;
    }
    #slash-popup.hidden { display: none; }
    .slash-popup-header {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px 6px;
      border-bottom: 1px solid color-mix(in srgb, var(--border) 50%, transparent);
    }
    .slash-popup-section {
      font-size: 10px;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: color-mix(in srgb, var(--vscode-foreground) 45%, transparent);
      padding: 6px 12px 2px;
      flex-shrink: 0;
    }
    .slash-popup-list {
      overflow-y: auto;
      flex: 1;
    }
    .slash-item {
      display: flex;
      align-items: baseline;
      gap: 10px;
      padding: 6px 12px;
      cursor: pointer;
      border-radius: 0;
      transition: background 0.1s;
    }
    .slash-item:hover,
    .slash-item.active {
      background: color-mix(in srgb, var(--accent, var(--vscode-focusBorder)) 14%, var(--vscode-input-background));
    }
    .slash-item .slash-name {
      font-size: 12.5px;
      font-weight: 500;
      color: var(--vscode-foreground);
      white-space: nowrap;
      flex-shrink: 0;
    }
    .slash-item .slash-name mark {
      background: transparent;
      color: var(--accent, var(--vscode-focusBorder));
      font-weight: 700;
    }
    .slash-item .slash-desc {
      font-size: 11.5px;
      color: color-mix(in srgb, var(--vscode-foreground) 50%, transparent);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .slash-item .slash-badge {
      margin-left: auto;
      flex-shrink: 0;
      font-size: 10px;
      padding: 1px 5px;
      border-radius: 4px;
      background: color-mix(in srgb, var(--accent, var(--vscode-focusBorder)) 18%, transparent);
      color: color-mix(in srgb, var(--accent, var(--vscode-focusBorder)) 80%, var(--vscode-foreground));
    }
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
      --ctx-arrow-left: 50%;
      position: fixed;
      top: 0;
      left: 0;
      width: max-content;
      min-width: min(210px, calc(100vw - 16px));
      max-width: calc(100vw - 16px);
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
      left: var(--ctx-arrow-left);
      bottom: -5px;
      width: 9px;
      height: 9px;
      background: inherit;
      border-right: 1px solid var(--vscode-editorWidget-border, var(--border));
      border-bottom: 1px solid var(--vscode-editorWidget-border, var(--border));
      transform: rotate(45deg);
    }
    .context-tooltip.visible {
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
    .tb-model-btn {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      width: 100%;
      padding: 4px 8px 4px 10px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: color-mix(in srgb, var(--vscode-input-background) 50%, transparent);
      color: var(--vscode-foreground);
      font-size: 11.5px;
      cursor: pointer;
      text-align: left;
      white-space: nowrap;
      overflow: hidden;
    }
    .tb-model-btn:hover {
      border-color: color-mix(in srgb, var(--accent) 40%, var(--border));
      background: color-mix(in srgb, var(--vscode-input-background) 70%, transparent);
    }
    .tb-model-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .tb-model-label {
      flex: 1;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 11.5px;
    }
    .tb-model-wrap.is-empty .tb-model-label { color: var(--text-muted); }
    .tb-model-btn .model-chevron { font-size: 9px; opacity: 0.6; flex-shrink: 0; }
    .model-menu {
      position: fixed;
      top: 0; left: 0;
      background: var(--vscode-menu-background);
      color: var(--vscode-menu-foreground);
      border: 1px solid var(--vscode-menu-border, #444);
      border-radius: 10px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.35);
      z-index: 200;
      padding: 6px 0 4px;
      min-width: 180px;
      max-width: 280px;
    }
    .model-menu.hidden { display: none; }
    .model-search-wrap { padding: 0 8px 6px; }
    .model-search {
      width: 100%;
      box-sizing: border-box;
      padding: 5px 8px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: color-mix(in srgb, var(--vscode-input-background) 60%, transparent);
      color: var(--vscode-foreground);
      font-size: 11.5px;
      outline: none;
    }
    .model-search:focus { border-color: color-mix(in srgb, var(--accent) 60%, var(--border)); }
    .model-menu-list { max-height: 240px; overflow-y: auto; }
    .model-menu-group {
      padding: 6px 12px 3px 32px;
      color: var(--text-muted);
      font-size: 10px;
      font-weight: 600;
      text-transform: none;
      border-top: 1px solid color-mix(in srgb, var(--border) 55%, transparent);
    }
    .model-menu-group:first-child { border-top: none; }
    .model-menu-item {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      font-size: 12px;
      cursor: pointer;
      white-space: nowrap;
      overflow: hidden;
    }
    .model-menu-item:hover { background: var(--vscode-menu-selectionBackground, rgba(127,127,127,0.18)); }
    .model-menu-item.active { font-weight: 600; }
    .model-menu-item .model-check { width: 14px; text-align: center; opacity: 0; font-size: 11px; flex-shrink: 0; }
    .model-menu-item.active .model-check { opacity: 1; }
    .model-menu-item .model-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
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
    .composer-actions {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
    }
    .tb-stop-circle {
      width: 30px;
      height: 30px;
      border-radius: 8px;
      border: 1px solid var(--border-strong);
      background: transparent;
      color: var(--vscode-foreground);
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      opacity: 0.8;
    }
    .tb-stop-circle:hover {
      opacity: 1;
      border-color: var(--vscode-errorForeground, #f48771);
      color: var(--vscode-errorForeground, #f48771);
      background: color-mix(in srgb, var(--vscode-errorForeground, #f48771) 9%, transparent);
    }
    .tb-stop-circle[hidden] { display: none; }

    /* ── Mode selector ─────────────────────────────────────────── */
    .tb-mode-wrap { position: relative; flex-shrink: 0; }
    .tb-mode-btn {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      height: 26px;
      padding: 0 8px 0 9px;
      border-radius: 7px;
      border: 1px solid var(--border);
      background: transparent;
      color: var(--vscode-foreground);
      font-size: 11.5px;
      font-weight: 500;
      cursor: pointer;
      white-space: nowrap;
    }
    .tb-mode-btn:hover { background: color-mix(in srgb, var(--vscode-input-background) 70%, transparent); }
    .tb-mode-btn .mode-chevron { font-size: 9px; opacity: 0.6; }
    .mode-menu {
      position: fixed;
      top: 0; left: 0;
      background: var(--vscode-menu-background);
      color: var(--vscode-menu-foreground);
      border: 1px solid var(--vscode-menu-border, #444);
      border-radius: 10px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.35);
      z-index: 200;
      padding: 3px 0;
      min-width: 130px;
    }
    .mode-menu.hidden { display: none; }
    .mode-item {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 7px 12px;
      font-size: 12.5px;
      cursor: pointer;
    }
    .mode-item:hover { background: var(--vscode-menu-selectionBackground, rgba(127,127,127,0.18)); }
    .mode-item .mode-check { width: 14px; text-align: center; opacity: 0; font-size: 11px; }
    .mode-item.active .mode-check { opacity: 1; }

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
    .tb-perm-wrap { position: relative; flex-shrink: 0; }
    .tb-perm-btn {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      height: 22px;
      padding: 0 7px 0 7px;
      border-radius: 7px;
      border: 1px solid var(--border);
      background: color-mix(in srgb, var(--vscode-badge-background, #555) 18%, transparent);
      color: var(--vscode-foreground);
      font-size: 10.5px;
      font-weight: 500;
      cursor: pointer;
      white-space: nowrap;
    }
    .tb-perm-btn:hover { background: color-mix(in srgb, var(--vscode-input-background) 70%, transparent); }
    .tb-perm-btn .perm-chevron { font-size: 9px; opacity: 0.6; }
    .tb-perm-btn .perm-icon { font-size: 11px; }
    .tb-perm-btn.perm-danger { color: #e8b84b; }
    .perm-menu {
      position: fixed;
      top: 0; left: 0;
      background: var(--vscode-menu-background);
      color: var(--vscode-menu-foreground);
      border: 1px solid var(--vscode-menu-border, #444);
      border-radius: 10px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.35);
      z-index: 200;
      padding: 3px 0;
      min-width: 245px;
    }
    .perm-menu.hidden { display: none; }
    .perm-item {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 7px 12px;
      font-size: 12.5px;
      cursor: pointer;
    }
    .perm-item:hover { background: var(--vscode-menu-selectionBackground, rgba(127,127,127,0.18)); }
    .perm-item-icon { font-size: 13px; width: 16px; text-align: center; }
    .perm-item-text { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
    .perm-item-text strong { font-size: 12px; font-weight: 600; }
    .perm-item-text small { color: var(--text-muted); font-size: 10.5px; line-height: 1.25; }
    .perm-item .perm-check { width: 14px; text-align: center; opacity: 0; font-size: 11px; }
    .perm-item.active .perm-check { opacity: 1; }
    .perm-item[data-perm="run_everything"] .perm-item-text { color: #e5c300; }

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
    :root[data-panel-width="narrow"] .tool-technical-name {
      display: none;
    }
    :root[data-panel-width="narrow"] .tool-summary {
      order: 4;
      flex-basis: calc(100% - 24px);
      margin-left: 24px;
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

    /* ── Session header bar ────────────────────────────────────────── */
    #session-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 5px 8px;
      border-bottom: 1px solid var(--border);
      background: var(--vscode-sideBar-background);
      flex-shrink: 0;
      min-height: 34px;
      gap: 4px;
    }
    .session-header-left {
      display: flex;
      align-items: center;
      gap: 4px;
      min-width: 0;
      flex: 1;
    }
    .session-back-btn {
      flex-shrink: 0;
      width: 24px;
      height: 24px;
      border: none;
      background: transparent;
      color: var(--vscode-foreground);
      cursor: pointer;
      border-radius: 6px;
      display: flex;
      align-items: center;
      justify-content: center;
      opacity: 0.7;
      font-size: 14px;
      padding: 0;
      transition: opacity 0.15s, background 0.15s;
    }
    .session-back-btn:hover { opacity: 1; background: color-mix(in srgb, var(--vscode-foreground) 10%, transparent); }
    .session-title {
      font-size: 12px;
      font-weight: 600;
      color: var(--vscode-foreground);
      opacity: 0.85;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      flex: 1;
      min-width: 0;
    }
    .session-header-right {
      display: flex;
      align-items: center;
      gap: 2px;
      flex-shrink: 0;
    }
    .session-hdr-btn {
      width: 26px;
      height: 26px;
      border: none;
      background: transparent;
      color: var(--vscode-foreground);
      cursor: pointer;
      border-radius: 6px;
      display: flex;
      align-items: center;
      justify-content: center;
      opacity: 0.6;
      padding: 0;
      transition: opacity 0.15s, background 0.15s;
      flex-shrink: 0;
    }
    .session-hdr-btn:hover { opacity: 1; background: color-mix(in srgb, var(--vscode-foreground) 10%, transparent); }
    .session-hdr-btn svg { display: block; }

    /* ── Session list panel ────────────────────────────────────────── */
    #session-list-panel {
      position: absolute;
      inset: 0;
      background: var(--vscode-sideBar-background);
      z-index: 100;
      display: flex;
      flex-direction: column;
      transform: translateX(-100%);
      transition: transform 0.2s ease;
      overflow: hidden;
    }
    #session-list-panel.visible { transform: translateX(0); }
    .session-list-header {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 8px 10px;
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }
    .session-list-header-title {
      font-size: 12px;
      font-weight: 600;
      flex: 1;
      color: var(--vscode-foreground);
      opacity: 0.85;
    }
    .session-list-new-btn {
      width: 26px;
      height: 26px;
      border: none;
      background: transparent;
      color: var(--vscode-foreground);
      cursor: pointer;
      border-radius: 6px;
      display: flex;
      align-items: center;
      justify-content: center;
      opacity: 0.65;
      font-size: 16px;
      padding: 0;
      transition: opacity 0.15s, background 0.15s;
    }
    .session-list-new-btn:hover { opacity: 1; background: color-mix(in srgb, var(--vscode-foreground) 10%, transparent); }
    #session-list-items {
      flex: 1;
      overflow-y: auto;
      padding: 6px 0;
    }
    .session-item {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 8px 12px;
      cursor: pointer;
      border-radius: 0;
      transition: background 0.12s;
    }
    .session-item:hover { background: color-mix(in srgb, var(--vscode-foreground) 6%, transparent); }
    .session-item.active { background: color-mix(in srgb, var(--accent) 12%, transparent); }
    .session-item-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      flex-shrink: 0;
    }
    .session-item-dot.done { background: #3fb950; }
    .session-item-dot.running {
      background: #58a6ff;
      animation: dot-breathe 1.6s ease-in-out infinite;
    }
    .session-item-dot.error { background: #f85149; }
    @keyframes dot-breathe {
      0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(88, 166, 255, 0.6); }
      50% { opacity: 0.7; box-shadow: 0 0 0 4px rgba(88, 166, 255, 0); }
    }
    .session-item-info {
      flex: 1;
      min-width: 0;
    }
    .session-item-title {
      font-size: 12px;
      font-weight: 500;
      color: var(--vscode-foreground);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .session-item-meta {
      font-size: 11px;
      color: var(--text-muted);
      margin-top: 2px;
    }
    .session-list-empty {
      padding: 20px 14px;
      text-align: center;
      font-size: 12px;
      color: var(--text-muted);
    }

    /* ── History overlay panel ─────────────────────────────────────── */
    #history-panel {
      position: absolute;
      inset: 0;
      background: var(--vscode-sideBar-background);
      z-index: 100;
      display: flex;
      flex-direction: column;
      transform: translateX(100%);
      transition: transform 0.2s ease;
      overflow: hidden;
    }
    #history-panel.visible { transform: translateX(0); }
    .history-panel-header {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 8px 10px;
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }
    .history-panel-title {
      font-size: 12px;
      font-weight: 600;
      flex: 1;
      color: var(--vscode-foreground);
      opacity: 0.85;
    }
    .history-close-btn {
      width: 26px;
      height: 26px;
      border: none;
      background: transparent;
      color: var(--vscode-foreground);
      cursor: pointer;
      border-radius: 6px;
      display: flex;
      align-items: center;
      justify-content: center;
      opacity: 0.65;
      font-size: 14px;
      padding: 0;
      transition: opacity 0.15s, background 0.15s;
    }
    .history-close-btn:hover { opacity: 1; background: color-mix(in srgb, var(--vscode-foreground) 10%, transparent); }
    .history-search-wrap {
      padding: 8px 10px;
      border-bottom: 1px solid color-mix(in srgb, var(--border) 60%, transparent);
      flex-shrink: 0;
    }
    .history-search {
      width: 100%;
      padding: 5px 9px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: var(--surface-elevated);
      color: var(--vscode-foreground);
      font-size: 12px;
      outline: none;
    }
    .history-search:focus { border-color: var(--accent); }
    #history-list-items {
      flex: 1;
      overflow-y: auto;
      padding: 6px 0;
    }
    .history-item {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 8px 12px;
      cursor: pointer;
      transition: background 0.12s;
    }
    .history-item:hover { background: color-mix(in srgb, var(--vscode-foreground) 6%, transparent); }
    .history-item-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: #3fb950;
      flex-shrink: 0;
    }
    .history-item-info {
      flex: 1;
      min-width: 0;
    }
    .history-item-title {
      font-size: 12px;
      font-weight: 500;
      color: var(--vscode-foreground);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .history-item-meta {
      font-size: 11px;
      color: var(--text-muted);
      margin-top: 2px;
    }
    .history-list-empty {
      padding: 20px 14px;
      text-align: center;
      font-size: 12px;
      color: var(--text-muted);
    }
  </style>
</head>
<body>
  <div id="session-list-panel">
    <div class="session-list-header">
      <button type="button" class="session-back-btn" id="session-list-close-btn" title="返回">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
      </button>
      <span class="session-list-header-title">会话列表</span>
      <button type="button" class="session-list-new-btn" id="session-list-new-btn" title="新建会话">+</button>
    </div>
    <div id="session-list-items">
      <div class="session-list-empty">暂无会话</div>
    </div>
  </div>
  <div id="history-panel">
    <div class="history-panel-header">
      <button type="button" class="history-close-btn" id="history-close-btn" title="关闭">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
      </button>
      <span class="history-panel-title">历史会话</span>
    </div>
    <div class="history-search-wrap">
      <input type="text" id="history-search" class="history-search" placeholder="搜索历史会话…" autocomplete="off" spellcheck="false" />
    </div>
    <div id="history-list-items">
      <div class="history-list-empty">暂无历史记录</div>
    </div>
  </div>
  <div id="session-header">
    <div class="session-header-left">
      <button type="button" class="session-back-btn" id="session-back-btn" title="所有会话">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
      </button>
      <span class="session-title" id="session-title">新会话</span>
    </div>
    <div class="session-header-right">
      <button type="button" class="session-hdr-btn" id="hdr-new-btn" title="新建会话">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
      </button>
      <button type="button" class="session-hdr-btn" id="hdr-settings-btn" title="设置">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      </button>
      <button type="button" class="session-hdr-btn" id="hdr-history-btn" title="历史会话">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
      </button>
    </div>
  </div>
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
      <div id="steering-hint" aria-live="polite">
        <span class="steering-dot" aria-hidden="true"></span>
        <span class="steering-copy">
          <span id="steering-hint-label">Agent 正在运行；发送的新消息会在下一次工具调用后生效</span>
          <span id="steering-queue-preview"></span>
        </span>
      </div>
      <div id="input-toolbar" class="composer-toolbar">
        <div class="toolbar-left">
          <div class="tb-left-wrap">
            <button type="button" class="tb-icon-btn" id="plus-btn" title="添加文件或图片" aria-haspopup="menu" aria-expanded="false">+</button>
          </div>
          <div class="model-pill-wrap">
            <div id="model-select-wrap" class="tb-model-wrap is-empty">
              <button type="button" class="tb-model-btn" id="model-btn" title="选择模型" aria-haspopup="menu" aria-expanded="false" disabled>
                <span id="model-select-label" class="tb-model-label">（正在连接网关…）</span>
                <span class="model-chevron">▾</span>
              </button>
            </div>
          </div>
          <div id="context-meter" class="context-meter" hidden tabindex="0" role="img" aria-label="背景信息窗口用量" aria-describedby="context-tooltip">
            <span class="ctx-ring" aria-hidden="true"></span>
          </div>
        </div>
        <div class="composer-actions">
          <button type="button" class="tb-stop-circle" id="stop-btn" title="中断当前任务" aria-label="中断当前任务" hidden>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
          </button>
          <button type="button" class="tb-send-circle" id="send-btn" title="发送 (⌘↵ / Ctrl+Enter)" aria-label="发送">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>
          </button>
        </div>
      </div>
    </div>
    <div id="footer-bar">
      <span class="footer-left muted">CrabCode</span>
      <div class="tb-mode-wrap">
        <button type="button" class="tb-mode-btn" id="mode-btn" title="切换模式" aria-haspopup="menu" aria-expanded="false">
          <span id="mode-label">Agent</span>
          <span class="mode-chevron">▾</span>
        </button>
      </div>
      <div class="tb-perm-wrap">
        <button type="button" class="tb-perm-btn" id="perm-btn" title="切换权限模式" aria-haspopup="menu" aria-expanded="false">
          <span id="perm-icon" class="perm-icon">⚙</span>
          <span id="perm-label">工作区默认规则</span>
          <span class="perm-chevron">▾</span>
        </button>
      </div>
    </div>
    <input type="file" id="file-input-image" accept="image/*" multiple hidden />
  </div>
  <div id="context-tooltip" class="context-tooltip" role="tooltip"></div>
  <div id="slash-popup" class="hidden" role="listbox" aria-label="命令列表">
    <div id="slash-popup-list" class="slash-popup-list"></div>
  </div>
  <div id="plus-menu" class="plus-menu hidden" role="menu">
    <button type="button" role="menuitem" data-action="image">添加图片…</button>
    <button type="button" role="menuitem" data-action="file">添加文件…</button>
  </div>
  <div id="mode-menu" class="mode-menu hidden" role="menu">
    <div class="mode-item active" data-mode="agent" role="menuitem">
      <span class="mode-check">✓</span>Agent
    </div>
    <div class="mode-item" data-mode="plan" role="menuitem">
      <span class="mode-check">✓</span>Plan
    </div>
  </div>
  <div id="perm-menu" class="perm-menu hidden" role="menu">
    <div class="perm-item active" data-perm="default" role="menuitem">
      <span class="perm-item-icon">⚙</span>
      <span class="perm-item-text"><strong>工作区默认规则</strong><small>使用当前项目加载的 CrabCode 权限配置</small></span>
      <span class="perm-check">✓</span>
    </div>
    <div class="perm-item" data-perm="ask" role="menuitem">
      <span class="perm-item-icon">🛡</span>
      <span class="perm-item-text"><strong>每次确认</strong><small>高风险操作前先向你确认</small></span>
      <span class="perm-check">✓</span>
    </div>
    <div class="perm-item" data-perm="ai_review" role="menuitem">
      <span class="perm-item-icon">🤖</span>
      <span class="perm-item-text"><strong>AI 审查</strong><small>由审查器判断是否放行</small></span>
      <span class="perm-check">✓</span>
    </div>
    <div class="perm-item" data-perm="run_everything" role="menuitem">
      <span class="perm-item-icon">⚡</span>
      <span class="perm-item-text"><strong>完全访问</strong><small>不再逐项弹出权限确认</small></span>
      <span class="perm-check">✓</span>
    </div>
  </div>
  <div id="model-menu" class="model-menu hidden" role="listbox" aria-label="选择模型">
    <div class="model-search-wrap">
      <input type="text" id="model-search" class="model-search" placeholder="搜索模型…" autocomplete="off" spellcheck="false" />
    </div>
    <div id="model-menu-list" class="model-menu-list"></div>
  </div>
  <script nonce="${nonce}">
    (function() {
      try {
        const vscode = acquireVsCodeApi();
        const msgContainer = document.getElementById('messages');
        const input = document.getElementById('input');
        const sendBtn = document.getElementById('send-btn');
        const stopBtn = document.getElementById('stop-btn');
        const steeringHint = document.getElementById('steering-hint');
        const steeringHintLabel = document.getElementById('steering-hint-label');
        const steeringQueuePreview = document.getElementById('steering-queue-preview');
        const attachmentBar = document.getElementById('attachment-bar');
        const composerWrap = document.getElementById('composer-wrap');
        const composerCard = document.getElementById('composer-card');
        const ctxToggle = document.getElementById('ctx-toggle');
        const plusBtn = document.getElementById('plus-btn');
        const plusMenu = document.getElementById('plus-menu');
        const slashPopup = document.getElementById('slash-popup');
        const slashPopupList = document.getElementById('slash-popup-list');
        const fileInputImage = document.getElementById('file-input-image');
        const modelSelectWrap = document.getElementById('model-select-wrap');
        const modelSelectLabel = document.getElementById('model-select-label');
        const modelBtn = document.getElementById('model-btn');
        const modelMenu = document.getElementById('model-menu');
        const modelMenuList = document.getElementById('model-menu-list');
        const modelSearch = document.getElementById('model-search');
        const contextMeter = document.getElementById('context-meter');
        const contextTooltip = document.getElementById('context-tooltip');
        const pendingEditsBar = document.getElementById('pending-edits-bar');
        const modeBtn = document.getElementById('mode-btn');
        const modeLabel = document.getElementById('mode-label');
        const modeMenu = document.getElementById('mode-menu');
        const permBtn = document.getElementById('perm-btn');
        const permLabel = document.getElementById('perm-label');
        const permIcon = document.getElementById('perm-icon');
        const permMenu = document.getElementById('perm-menu');
        const sessionListPanel = document.getElementById('session-list-panel');
        const sessionListItems = document.getElementById('session-list-items');
        const sessionListCloseBtn = document.getElementById('session-list-close-btn');
        const sessionListNewBtn = document.getElementById('session-list-new-btn');
        const sessionBackBtn = document.getElementById('session-back-btn');
        const sessionTitleEl = document.getElementById('session-title');
        const hdrNewBtn = document.getElementById('hdr-new-btn');
        const hdrSettingsBtn = document.getElementById('hdr-settings-btn');
        const hdrHistoryBtn = document.getElementById('hdr-history-btn');
        const historyPanel = document.getElementById('history-panel');
        const historyCloseBtn = document.getElementById('history-close-btn');
        const historySearchInput = document.getElementById('history-search');
        const historyListItems = document.getElementById('history-list-items');

    // ── Tool card state ──────────────────────────────────────────
    const toolCards = new Map();
    const thinkingCards = new Map();
    const choiceCards = new Map();
    const permissionCards = new Map();
    const planCards = new Map();
    const toolCardTurns = new Map();
    const thinkingCardTurns = new Map();
    const busyIndicator = document.getElementById('busy-indicator');
    const busyLabel = busyIndicator ? busyIndicator.querySelector('.busy-label') : null;
    const rootEl = document.documentElement;
    let isBusy = false;
    let stickToBottom = true;
    let hasReceivedOptions = false;
    let currentModelValue = '';
    let currentModelList = [];
    let currentModelGroups = {};
    let pendingEditsCollapsed = false;
    let pendingEditsVisibleFiles = 5;
    let currentPendingEditSummary = null;
    let pendingSteeringQueue = [];
    let turnCounter = 0;
    let activeTurn = null;
    const turns = [];
    const SEND_ICON_HTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>';

    // pendingImages: { media_type, data, dataUrl }; pendingTextFiles: { name, text }
    const pendingImages = [];
    const pendingTextFiles = [];
    const MAX_IMAGE_SIZE = 20 * 1024 * 1024;
    const MAX_TEXT_FILE = 20 * 1024 * 1024;

    // ── Session management state ────────────────────────────────────
    let allSessions = [];          // { session_id, message_count, status, title }
    let currentSessionId = null;
    let currentSessionTitle = '新会话';
    let sessionListVisible = false;
    let historyPanelVisible = false;
    let historySearchQuery = '';

    function setSessionTitle(title) {
      currentSessionTitle = title || '新会话';
      if (sessionTitleEl) sessionTitleEl.textContent = currentSessionTitle;
    }

    function showSessionList() {
      sessionListVisible = true;
      if (sessionListPanel) sessionListPanel.classList.add('visible');
      vscode.postMessage({ type: 'fetchSessions' });
    }

    function hideSessionList() {
      sessionListVisible = false;
      if (sessionListPanel) sessionListPanel.classList.remove('visible');
    }

    function showHistoryPanel() {
      historyPanelVisible = true;
      if (historyPanel) historyPanel.classList.add('visible');
      vscode.postMessage({ type: 'fetchSessions' });
    }

    function hideHistoryPanel() {
      historyPanelVisible = false;
      if (historyPanel) historyPanel.classList.remove('visible');
    }

    function getSessionDotClass(session) {
      if (session.status === 'running') return 'running';
      if (session.status === 'error') return 'error';
      return 'done';
    }

    function formatSessionMeta(session) {
      const parts = [];
      if (session.message_count != null) parts.push(session.message_count + ' 条消息');
      if (session.tokens_used != null && session.tokens_used > 0) parts.push(session.tokens_used.toLocaleString() + ' tokens');
      if (session.model) parts.push(session.model);
      if (session.cwd) {
        const cwdParts = String(session.cwd).split(/[\\/]/).filter(Boolean);
        if (cwdParts.length > 0) parts.push(cwdParts[cwdParts.length - 1]);
      }
      if (session.created_at) {
        try {
          const d = new Date(session.created_at);
          const now = new Date();
          const diffMs = now - d;
          const diffMin = Math.floor(diffMs / 60000);
          const diffHr = Math.floor(diffMin / 60);
          const diffDay = Math.floor(diffHr / 24);
          if (diffMin < 1) parts.push('刚刚');
          else if (diffMin < 60) parts.push(diffMin + ' 分钟前');
          else if (diffHr < 24) parts.push(diffHr + ' 小时前');
          else parts.push(diffDay + ' 天前');
        } catch { /* ignore */ }
      }
      return parts.join(' · ');
    }

    function renderSessionList(sessions) {
      allSessions = sessions || [];
      if (!sessionListItems) return;
      const visibleSessions = allSessions.filter(function(s) {
        return s.message_count > 0 || s.session_id === currentSessionId;
      });
      if (visibleSessions.length === 0) {
        sessionListItems.innerHTML = '<div class="session-list-empty">暂无会话</div>';
        return;
      }
      sessionListItems.innerHTML = visibleSessions.map(function(s) {
        const dotClass = getSessionDotClass(s);
        const isActive = s.session_id === currentSessionId;
        const title = s.title || s.session_id.slice(0, 12) + '…';
        const meta = formatSessionMeta(s);
        return '<div class="session-item' + (isActive ? ' active' : '') + '" data-session-id="' + escapeHtml(s.session_id) + '">' +
          '<div class="session-item-dot ' + dotClass + '"></div>' +
          '<div class="session-item-info">' +
            '<div class="session-item-title">' + escapeHtml(title) + '</div>' +
            (meta ? '<div class="session-item-meta">' + escapeHtml(meta) + '</div>' : '') +
          '</div>' +
        '</div>';
      }).join('');
      sessionListItems.querySelectorAll('.session-item').forEach(function(el) {
        el.addEventListener('click', function() {
          const sid = el.getAttribute('data-session-id');
          if (sid && sid !== currentSessionId) {
            vscode.postMessage({ type: 'resumeSession', sessionId: sid });
          }
          hideSessionList();
        });
      });
    }

    function renderHistoryList(sessions, query) {
      if (!historyListItems) return;
      let items = sessions || allSessions;
      if (query) {
        const q = query.toLowerCase();
        items = items.filter(function(s) {
          const title = (s.title || s.session_id || '').toLowerCase();
          return title.includes(q);
        });
      }
      if (items.length === 0) {
        historyListItems.innerHTML = '<div class="history-list-empty">' + (query ? '无匹配结果' : '暂无历史记录') + '</div>';
        return;
      }
      historyListItems.innerHTML = items.map(function(s) {
        const title = s.title || s.session_id.slice(0, 12) + '…';
        const meta = formatSessionMeta(s);
        return '<div class="history-item" data-session-id="' + escapeHtml(s.session_id) + '">' +
          '<div class="history-item-dot"></div>' +
          '<div class="history-item-info">' +
            '<div class="history-item-title">' + escapeHtml(title) + '</div>' +
            (meta ? '<div class="history-item-meta">' + escapeHtml(meta) + '</div>' : '') +
          '</div>' +
        '</div>';
      }).join('');
      historyListItems.querySelectorAll('.history-item').forEach(function(el) {
        el.addEventListener('click', function() {
          const sid = el.getAttribute('data-session-id');
          if (sid) {
            vscode.postMessage({ type: 'resumeSession', sessionId: sid });
          }
          hideHistoryPanel();
        });
      });
    }

    // ── Session header button events ────────────────────────────────
    if (sessionBackBtn) {
      sessionBackBtn.addEventListener('click', function() { showSessionList(); });
    }
    if (hdrNewBtn) {
      hdrNewBtn.addEventListener('click', function() {
        vscode.postMessage({ type: 'newSession' });
        setSessionTitle('新会话');
      });
    }
    if (hdrSettingsBtn) {
      hdrSettingsBtn.addEventListener('click', function() {
        vscode.postMessage({ type: 'openSettings' });
      });
    }
    if (hdrHistoryBtn) {
      hdrHistoryBtn.addEventListener('click', function() { showHistoryPanel(); });
    }
    if (sessionListCloseBtn) {
      sessionListCloseBtn.addEventListener('click', function() { hideSessionList(); });
    }
    if (sessionListNewBtn) {
      sessionListNewBtn.addEventListener('click', function() {
        hideSessionList();
        vscode.postMessage({ type: 'newSession' });
        setSessionTitle('新会话');
      });
    }
    if (historyCloseBtn) {
      historyCloseBtn.addEventListener('click', function() { hideHistoryPanel(); });
    }
    if (historySearchInput) {
      historySearchInput.addEventListener('input', function() {
        historySearchQuery = historySearchInput.value;
        renderHistoryList(null, historySearchQuery);
      });
    }

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
      const presentation = getToolPresentation(card.toolName, card.input || {});
      el.className = 'tool-card tool-kind-' + presentation.kind + (card.isError ? ' is-error' : '');
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

      el.querySelectorAll('.path').forEach(pathEl => {
        pathEl.addEventListener('click', (e) => {
          const path = e.target.dataset.path;
          if (path) vscode.postMessage({ type: 'openFile', path });
        });
      });

      toolCards.set(card.id, card);
      return el;
    }

    function updateToolCard(el, card) {
      const presentation = getToolPresentation(card.toolName, card.input || {});
      el.className = 'tool-card tool-kind-' + presentation.kind + (card.isError ? ' is-error' : '');
      el.innerHTML = buildToolCardHtml(card);
      el.querySelector('.tool-card-header').addEventListener('click', () => {
        vscode.postMessage({ type: 'toggleToolCard', id: card.id });
      });
      el.querySelectorAll('.path').forEach(pathEl => {
        pathEl.addEventListener('click', (e) => {
          const path = e.target.dataset.path;
          if (path) vscode.postMessage({ type: 'openFile', path });
        });
      });
      toolCards.set(card.id, card);
    }

    function buildToolCardHtml(card) {
      const presentation = getToolPresentation(card.toolName, card.input || {});
      const chevron = card.collapsed ? 'chevron collapsed' : 'chevron';
      let statusHtml = '';
      if (card.result !== null) {
        statusHtml = card.isError
          ? '<span class="status error">失败</span>'
          : '<span class="status ok">完成</span>';
      } else {
        statusHtml = '<span class="status running">运行中</span>';
      }

      const inputHtml = renderToolInput(card.toolName, card.input, presentation);
      let bodyHtml = '';
      if (!card.collapsed) {
        if (card.result !== null) {
          bodyHtml = '<div class="tool-card-body">' + inputHtml +
            '<section class="tool-card-section">' + renderResult(card.result, card.toolName) + '</section></div>';
        } else {
          bodyHtml = '<div class="tool-card-body">' + inputHtml + '</div>';
        }
      } else if (card.result !== null) {
        const preview = card.result.split('\\n')[0].substring(0, 90);
        statusHtml += '<span class="card-preview">' + escapeHtml(preview) + (card.result.length > 90 ? '…' : '') + '</span>';
      }

      return '<div class="tool-card-header">' +
        '<span class="icon">' + escapeHtml(presentation.glyph) + '</span>' +
        '<span class="tool-name">' + escapeHtml(presentation.label) + '</span>' +
        '<span class="tool-technical-name">' + escapeHtml(card.toolName) + '</span>' +
        (presentation.summary ? '<span class="tool-summary">' + escapeHtml(presentation.summary) + '</span>' : '') +
        statusHtml +
        '<span class="' + chevron + '">▾</span>' +
        '</div>' + bodyHtml;
    }

    const TOOL_PRESENTATIONS = {
      read: ['file', '读取文件', 'R'], fileread: ['file', '读取文件', 'R'],
      edit: ['file', '编辑文件', 'E'], fileedit: ['file', '编辑文件', 'E'],
      write: ['file', '写入文件', 'W'], filewrite: ['file', '写入文件', 'W'],
      bash: ['terminal', '运行命令', '>_'], lint: ['terminal', '检查诊断', '!'],
      grep: ['search', '搜索内容', 'G'], glob: ['search', '查找文件', '*'],
      codebasesearch: ['search', '语义搜索', 'S'], websearch: ['web', '搜索网页', '↗'],
      browser: ['web', '浏览器操作', '◎'], debugger: ['debug', '调试程序', 'D'],
      processdebugger: ['debug', '进程调试', 'P'], memory: ['memory', '管理记忆', 'M'],
      monitor: ['task', '启动监控', '◉'], tasklist: ['task', '查看后台任务', '≡'], taskstop: ['task', '停止后台任务', '■'],
      agent: ['agent', '启动 Agent', 'A'], agentstatus: ['agent', '查看 Agent', 'A'],
      agentwait: ['agent', '等待 Agent', 'A'], agentcancel: ['agent', '取消 Agent', 'A'], agentsendinput: ['agent', '向 Agent 发送输入', 'A'],
      listagents: ['message', '查看会话', '↔'], sendmessage: ['message', '发送会话消息', '↗'], askuser: ['message', '请求用户选择', '?'],
      checkpoint: ['checkpoint', '创建检查点', '◆'], revert: ['checkpoint', '回退检查点', '↶'], checklist: ['checklist', '任务清单', '✓'],
      create_goal: ['goal', '创建 Goal', '◎'], get_goal: ['goal', '查看 Goal', '◎'], update_goal: ['goal', '更新 Goal', '◎'],
      switchmode: ['mode', '切换工作模式', '⇄'],
      teamcreate: ['team', '创建 Team', 'T'], teamspawn: ['team', '添加 Team 成员', 'T'], teammessage: ['team', '发送 Team 消息', 'T'],
      teambroadcast: ['team', '广播 Team 消息', 'T'], teamstatus: ['team', '查看 Team', 'T'], teamtaskadd: ['team', '添加 Team 任务', 'T'],
      teamtaskclaim: ['team', '认领 Team 任务', 'T'], teamtaskcomplete: ['team', '完成 Team 任务', 'T'], teamshutdown: ['team', '关闭 Team', 'T'],
      schedulecreate: ['schedule', '创建定时任务', '◷'], schedulelist: ['schedule', '查看定时任务', '◷'],
      schedulecancel: ['schedule', '删除定时任务', '◷'], schedulestatus: ['schedule', '查看任务状态', '◷'],
      schedulepause: ['schedule', '暂停定时任务', '◷'], scheduleresume: ['schedule', '恢复定时任务', '◷'], schedulerun: ['schedule', '立即运行任务', '◷'],
      skill: ['skill', '加载 Skill', '◇'],
    };

    const TOOL_FIELD_LABELS = {
      action: '操作', file_path: '文件', path: '路径', target_file: '文件', target_directory: '目录', cwd: '工作目录', program: '程序',
      command: '命令', timeout: '超时', timeout_seconds: '超时', offset: '起始行', limit: '行数', old_string: '替换前', new_string: '替换后',
      content: '内容', replace_all: '全部替换', pattern: '匹配模式', glob: '文件过滤', query: '查询', num_results: '结果数',
      case_insensitive: '忽略大小写', url: '网址', selector: '选择器', script: '脚本', text: '消息', prompt: '任务', description: '说明',
      objective: '目标', token_budget: 'Token 预算', status: '状态', target_mode: '目标模式', explanation: '原因', plan: '执行计划',
      session_id: 'Session', agent_id: 'Agent', agent_ids: 'Agents', task_id: '任务 ID', team_id: 'Team', to: '接收方', role: '角色',
      name: '名称', model_profile: '模型', run_in_background: '后台运行', checklist_id: '清单 ID', item: '清单项', items: '清单项',
      remove_items: '移除项', checkpoint_id: '检查点', label: '标签', schedule: '时间规则', schedule_type: '调度类型', enabled: '启用',
      max_runs: '最大次数', next_run: '下次运行', tags: '标签', language: '语言', adapter_id: '调试适配器', pid: '进程 ID',
      lines: '行号', thread_id: '线程', frame_id: '栈帧', expression: '表达式', title: '标题', skill_name: 'Skill', user_input: '用户要求',
      ws: 'WebSocket', timeout_ms: '超时', persistent: '持续监控', scope: '范围', memory_id: '记忆 ID', max_teammates: '成员上限',
      headless: '无界面模式', tab_id: '标签页', wait_until: '等待条件', return_format: '返回格式', wait_any: '任一完成即返回', interrupt: '中断当前任务',
      job_id: '任务 ID', run_limit: '运行记录数', extra: '附加配置', paths: '检查路径', linter: '检查器', args: '参数', levels: '栈深度',
      variables_reference: '变量引用', start: '起始位置', count: '数量', context: '上下文', terminate_debuggee: '终止被调试程序',
      launch_config: '启动配置', attach_config: '附加配置', duration_seconds: '持续时间', interval_seconds: '采样间隔', output_path: '输出文件',
      module_filter: '模块过滤', target_address: '目标地址', address: '地址', base_address: '基址', module_path: '模块路径', module_offset: '模块偏移',
      max_depth: '最大深度', max_offset: '最大偏移', offsets: '偏移链', pointer_size: '指针大小', align: '对齐', size: '大小',
      value_type: '值类型', value: '值', value_hex: '十六进制值', patch_hex: '补丁字节', expected_hex: '预期字节', patch_id: '补丁 ID',
      endian: '字节序', readable: '可读', writable: '可写', executable: '可执行', writable_only: '仅可写内存', executable_only: '仅可执行内存',
      max_results: '结果数', max_scan_bytes: '最大扫描字节', search_id: '搜索 ID', freeze_id: '冻结 ID', comparison: '比较方式', all: '全部',
    };

    const TOOL_FIELD_ORDER = {
      file: ['file_path', 'path', 'target_file', 'offset', 'limit', 'replace_all', 'old_string', 'new_string', 'content'],
      terminal: ['command', 'paths', 'linter', 'file_path', 'path', 'language', 'timeout'],
      search: ['query', 'pattern', 'path', 'target_directory', 'glob', 'num_results', 'case_insensitive'],
      web: ['action', 'url', 'selector', 'text', 'script', 'path', 'session_id', 'tab_id', 'headless', 'wait_until', 'return_format', 'timeout_seconds', 'options'],
      debug: ['action', 'session_id', 'program', 'pid', 'language', 'path', 'address', 'base_address', 'lines', 'thread_id', 'frame_id', 'expression', 'query', 'pattern', 'value', 'value_hex', 'patch_hex', 'args', 'cwd'],
      memory: ['action', 'title', 'query', 'content', 'memory_id', 'id'],
      task: ['action', 'task_id', 'description', 'command', 'ws', 'persistent', 'interval', 'timeout_ms', 'timeout'],
      agent: ['agent_id', 'agent_ids', 'name', 'description', 'prompt', 'subagent_type', 'model_profile', 'run_in_background', 'wait_any', 'interrupt', 'timeout_seconds', 'timeout_ms'],
      message: ['to', 'from_name', 'question', 'options', 'multiple', 'text'], checkpoint: ['checkpoint_id', 'label'],
      checklist: ['action', 'title', 'checklist_id', 'item', 'items', 'remove_items'], goal: ['objective', 'status', 'token_budget'],
      mode: ['target_mode', 'explanation', 'plan'], team: ['team_id', 'task_id', 'name', 'role', 'to', 'description', 'prompt', 'text', 'result', 'model_profile', 'max_teammates'],
      schedule: ['job_id', 'name', 'status', 'schedule_type', 'schedule', 'prompt', 'description', 'cwd', 'enabled', 'max_runs', 'next_run', 'run_limit', 'tags', 'timeout', 'model_profile', 'session_id', 'extra'],
      skill: ['skill_name', 'skill', 'name', 'user_input', 'path', 'args'],
    };

    const TOOL_ACTION_LABELS = {
      create: '创建', update: '更新', delete: '删除', list: '查看列表', search: '搜索', read: '读取', check: '标记完成',
      uncheck: '取消完成', clear: '清空', launch: '启动', attach: '附加', navigate: '打开网页', click: '点击', type: '输入文本',
      extract: '提取内容', screenshot: '截图', evaluate: '执行脚本', pause: '暂停', resume: '继续', stop: '停止', status: '查看状态',
      create_session: '创建浏览器会话', goto: '打开网页', fill: '填写内容', press: '按键', wait_for: '等待元素', list_tabs: '查看标签页',
      new_tab: '新建标签页', switch_tab: '切换标签页', close_tab: '关闭标签页', close_session: '关闭浏览器会话', start: '启动调试',
      set_breakpoints: '设置断点', continue: '继续执行', step_over: '单步跳过', step_in: '单步进入', step_out: '单步跳出', threads: '查看线程',
      stack: '查看调用栈', scopes: '查看作用域', variables: '查看变量', events: '查看事件', list_processes: '查看进程', inspect_process: '检查进程',
      attach_debugger: '附加调试器', sample_stack: '采样调用栈', dump_core: '导出 Core Dump', memory_maps: '查看内存映射', memory_regions: '查看内存区域',
      memory_read: '读取内存', memory_search: '搜索内存', memory_refine: '筛选内存搜索', memory_write: '写入内存', memory_freeze: '冻结内存值',
      memory_unfreeze: '取消冻结', memory_freezes: '查看冻结项', aob_scan: '扫描字节特征', pointer_scan: '扫描指针', pointer_resolve: '解析指针',
      code_read: '读取机器码', code_patch: '修改机器码', code_restore: '恢复机器码', code_patches: '查看机器码补丁', trace_syscalls: '跟踪系统调用',
      detach: '断开调试器', terminate: '终止进程', kill: '强制结束进程',
    };

    function toolValue(value) {
      if (typeof value === 'string') return value;
      if (typeof value === 'boolean') return value ? '是' : '否';
      if (value === null) return 'null';
      if (Array.isArray(value) && value.every(item => ['string', 'number', 'boolean'].includes(typeof item))) return value.map(String).join(' · ');
      try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
    }

    function getToolPresentation(toolName, input) {
      const normalized = String(toolName || '').trim().toLowerCase().replace(/[\\s.-]/g, '');
      const definition = TOOL_PRESENTATIONS[normalized] || ['generic', toolName || '工具', '⚙'];
      const kind = definition[0];
      const rawAction = typeof input.action === 'string' ? input.action : '';
      const action = rawAction ? (TOOL_ACTION_LABELS[rawAction.toLowerCase()] || rawAction) : '';
      const summaryKeys = {
        file: ['file_path', 'path', 'target_file'], terminal: ['command', 'paths', 'linter', 'file_path', 'path'], search: ['query', 'pattern', 'glob'],
        web: ['url', 'selector', 'text', 'path'], debug: ['program', 'pid', 'path', 'expression', 'session_id'], memory: ['title', 'query', 'content'],
        task: ['description', 'command', 'task_id'], agent: ['description', 'name', 'prompt', 'agent_id', 'agent_ids'], message: ['to', 'text', 'question'],
        checkpoint: ['label', 'checkpoint_id'], checklist: ['title', 'checklist_id', 'item'], goal: ['objective', 'status'], mode: ['target_mode', 'explanation'],
        team: ['team_id', 'name', 'description', 'to', 'prompt'], schedule: ['name', 'job_id', 'schedule'], skill: ['skill_name', 'skill', 'name', 'path'],
      };
      const summaryKey = (summaryKeys[kind] || []).find(key => input[key] !== undefined && input[key] !== '');
      let summaryValue = summaryKey ? toolValue(input[summaryKey]).replace(/\\s+/g, ' ').trim() : '';
      if (summaryValue.length > 120) summaryValue = summaryValue.substring(0, 117) + '…';
      const order = TOOL_FIELD_ORDER[kind] || [];
      const fields = Object.entries(input).filter(([, value]) => value !== undefined && value !== '' && value !== null).sort(([left], [right]) => {
        const leftIndex = order.indexOf(left); const rightIndex = order.indexOf(right);
        return (leftIndex < 0 ? 10000 : leftIndex) - (rightIndex < 0 ? 10000 : rightIndex);
      }).map(([key, value]) => {
        let variant = 'text';
        if (Array.isArray(value) && value.every(item => ['string', 'number', 'boolean'].includes(typeof item))) variant = 'list';
        else if (value !== null && typeof value === 'object') variant = 'json';
        else if (['file_path', 'path', 'target_file', 'target_directory', 'cwd', 'program', 'output_path', 'module_path'].includes(key)) variant = 'path';
        else if (['command', 'script', 'old_string', 'new_string', 'content', 'pattern', 'expression'].includes(key)) variant = 'code';
        return { key, label: TOOL_FIELD_LABELS[key] || key.replaceAll('_', ' '), value: toolValue(value), variant };
      });
      return { kind, label: definition[1], glyph: definition[2], action, summary: [action, summaryValue].filter(Boolean).join(' · '), fields };
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

    function renderToolInput(toolName, input, suppliedPresentation) {
      if ((toolName || '').toLowerCase() === 'checklist') {
        return renderChecklistInput(input);
      }
      const presentation = suppliedPresentation || getToolPresentation(toolName, input || {});
      if (presentation.fields.length === 0) return '<div class="tool-empty">无需参数</div>';
      const fields = presentation.fields.map(field => {
        if (field.variant === 'list') {
          return '<div class="tool-detail-row"><span>' + escapeHtml(field.label) + '</span><div class="tool-chip-list">' +
            field.value.split(' · ').map(value => '<code>' + escapeHtml(value) + '</code>').join('') + '</div></div>';
        }
        if (field.variant === 'code' || field.variant === 'json') {
          return '<div class="tool-detail-row stacked"><span>' + escapeHtml(field.label) + '</span><pre>' + escapeHtml(field.value) + '</pre></div>';
        }
        return '<div class="tool-detail-row"><span>' + escapeHtml(field.label) + '</span><code' +
          (field.variant === 'path' ? ' class="path" data-path="' + escapeAttr(field.value) + '"' : '') + '>' + escapeHtml(field.value) + '</code></div>';
      }).join('');
      return '<section class="tool-card-section">' +
        '<div class="timeline-meta">input</div>' +
        (presentation.action ? '<span class="tool-action-badge">' + escapeHtml(presentation.action) + '</span>' : '') +
        '<div class="tool-detail-list">' + fields + '</div></section>';
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
      } else if (item.kind === 'plan') {
        renderPlanCard(item.card);
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

    function renderPlanCard(card) {
      const existing = document.getElementById('plan-' + card.id);
      if (existing) { updatePlanCard(existing, card); return existing; }

      const shouldStick = captureScrollAnchor();
      const el = document.createElement('div');
      el.className = 'request-card plan';
      el.id = 'plan-' + card.id;
      el.innerHTML = buildPlanCardHtml(card);
      appendToTurnContent(getCurrentTurn(), el);
      restoreScrollAnchor(shouldStick);
      bindPlanCardEvents(el, card);
      planCards.set(card.id, card);
      return el;
    }

    function updatePlanCard(el, card) {
      el.innerHTML = buildPlanCardHtml(card);
      bindPlanCardEvents(el, card);
      planCards.set(card.id, card);
    }

    function bindPlanCardEvents(el, card) {
      el.querySelectorAll('[data-plan-action]').forEach(btn => {
        btn.addEventListener('click', () => {
          if (card.status !== 'pending') return;
          const action = btn.getAttribute('data-plan-action');
          if (action) vscode.postMessage({ type: 'respondToPlan', id: card.id, action: action });
        });
      });
    }

    function buildPlanCardHtml(card) {
      const plan = card.plan || {};
      const title = plan.title || 'Execution Plan';
      const summary = plan.summary || '';
      const steps = Array.isArray(plan.steps) ? plan.steps : [];
      const statusHtml = card.status === 'pending'
        ? '<span class="request-status pending">等待确认</span>'
        : card.status === 'executing'
          ? '<span class="request-status done">执行中</span>'
          : card.status === 'revising'
            ? '<span class="request-status done">继续规划</span>'
            : '<span class="request-status cancelled">已取消</span>';
      const stepHtml = steps.length > 0
        ? '<ol class="plan-step-list">' + steps.map((step, index) => buildPlanStepHtml(step, index)).join('') + '</ol>'
        : '<pre class="request-pre">' + escapeHtml(JSON.stringify(plan, null, 2)) + '</pre>';
      const actions = card.status === 'pending'
        ? '<button class="request-action primary" data-plan-action="execute">执行计划</button>' +
          '<button class="request-action subtle" data-plan-action="revise">修改计划</button>' +
          '<button class="request-action subtle" data-plan-action="cancel">取消</button>'
        : '';
      return '<div class="request-card-header">' +
        '<span class="icon">✓</span>' +
        '<span class="request-title">计划 · ' + escapeHtml(title) + '</span>' +
        statusHtml +
        '</div>' +
        '<div class="request-card-body">' +
        '<div class="timeline-meta">execution plan</div>' +
        (summary ? '<div class="plan-summary">' + escapeHtml(summary) + '</div>' : '') +
        stepHtml +
        '<div class="request-actions">' + actions + '</div>' +
        '</div>';
    }

    function buildPlanStepHtml(step, index) {
      const files = Array.isArray(step.files) ? step.files : [];
      const deps = Array.isArray(step.depends_on) ? step.depends_on : [];
      const chips = [];
      if (files.length > 0) chips.push('<span class="plan-chip">files: ' + escapeHtml(files.join(', ')) + '</span>');
      if (deps.length > 0) chips.push('<span class="plan-chip">after: ' + escapeHtml(deps.join(', ')) + '</span>');
      if (step.subagent_type) chips.push('<span class="plan-chip">' + escapeHtml(String(step.subagent_type)) + '</span>');
      return '<li class="plan-step">' +
        '<span class="plan-step-index">' + (index + 1) + '.</span>' +
        '<div>' +
        '<div class="plan-step-title">' + escapeHtml(step.title || step.id || ('Step ' + (index + 1))) + '</div>' +
        (step.description ? '<div class="plan-step-desc">' + escapeHtml(String(step.description)) + '</div>' : '') +
        (chips.length > 0 ? '<div class="plan-step-meta">' + chips.join('') + '</div>' : '') +
        '</div>' +
        '</li>';
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
      const alwaysAllowBtn = el.querySelector('[data-permission-always-allow]');
      if (alwaysAllowBtn) {
        alwaysAllowBtn.addEventListener('click', () => {
          if (card.allowed !== null) return;
          vscode.postMessage({ type: 'respondToPermission', id: card.id, allowed: true, alwaysAllow: true });
        });
      }
      const denyBtn = el.querySelector('[data-permission-deny]');
      if (denyBtn) {
        denyBtn.addEventListener('click', () => {
          if (card.allowed !== null) return;
          vscode.postMessage({ type: 'respondToPermission', id: card.id, allowed: false, alwaysAllow: false });
        });
      }
      const denyFeedbackBtn = el.querySelector('[data-permission-deny-feedback]');
      if (denyFeedbackBtn) {
        denyFeedbackBtn.addEventListener('click', () => {
          if (card.allowed !== null) return;
          const feedbackRow = el.querySelector('.permission-feedback-row');
          if (feedbackRow) {
            feedbackRow.classList.toggle('hidden');
            const input = feedbackRow.querySelector('.permission-feedback-input');
            if (input && !feedbackRow.classList.contains('hidden')) input.focus();
          }
        });
      }
      const feedbackSendBtn = el.querySelector('[data-permission-feedback-send]');
      if (feedbackSendBtn) {
        feedbackSendBtn.addEventListener('click', () => {
          if (card.allowed !== null) return;
          const input = el.querySelector('.permission-feedback-input');
          const feedback = input ? input.value.trim() : '';
          vscode.postMessage({ type: 'respondToPermission', id: card.id, allowed: false, alwaysAllow: false, feedback: feedback || undefined });
        });
      }
      const feedbackInput = el.querySelector('.permission-feedback-input');
      if (feedbackInput) {
        feedbackInput.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            if (card.allowed !== null) return;
            const feedback = feedbackInput.value.trim();
            vscode.postMessage({ type: 'respondToPermission', id: card.id, allowed: false, alwaysAllow: false, feedback: feedback || undefined });
          }
        });
      }
    }

    function buildPermissionCardHtml(card) {
      const isPeerMessage = card.requestKind === 'peer_message';
      const state = card.allowed === null
        ? '<span class="request-status pending">等待确认</span>'
        : (card.allowed ? '<span class="request-status done">已允许</span>' : '<span class="request-status denied">已拒绝</span>');
      const actions = card.allowed === null
        ? '<button class="request-action primary" data-permission-allow>' + (isPeerMessage ? '接收' : '允许') + '</button>' +
          '<button class="request-action subtle" data-permission-always-allow>' + (isPeerMessage ? '始终接收此 Session' : '始终允许') + '</button>' +
          '<button class="request-action subtle" data-permission-deny>拒绝</button>' +
          (isPeerMessage ? '' : '<button class="request-action subtle" data-permission-deny-feedback>拒绝并反馈</button>')
        : '';
      const feedbackRow = card.allowed === null && !isPeerMessage
        ? '<div class="permission-feedback-row hidden">' +
          '<input class="permission-feedback-input" type="text" placeholder="告诉 AI 应该怎么做…" />' +
          '<button class="request-action primary" data-permission-feedback-send>发送</button>' +
          '</div>'
        : '';
      const reason = card.reason ? '<div class="request-reason">' + escapeHtml(card.reason) + '</div>' : '';
      return '<div class="request-card-header">' +
        '<span class="icon">' + (isPeerMessage ? '✉' : '⚡') + '</span>' +
        '<span class="request-title">' + (isPeerMessage ? '跨 Session 消息' : '工具权限 · ' + escapeHtml(card.toolName)) + '</span>' +
        state +
        '</div>' +
        '<div class="request-card-body">' +
        '<div class="timeline-meta">' + (isPeerMessage ? 'peer message approval' : 'permission request') + '</div>' +
        reason +
        '<pre class="request-pre">' + escapeHtml(formatToolInput(card.toolName, card.input)) + '</pre>' +
        '<div class="request-actions">' + actions + '</div>' +
        feedbackRow +
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
      if (composerCard) composerCard.classList.toggle('is-steering', busy);
      if (steeringHint) steeringHint.classList.toggle('visible', busy);
      if (stopBtn) stopBtn.hidden = !busy;
      if (sendBtn) {
        sendBtn.innerHTML = SEND_ICON_HTML;
        sendBtn.title = busy ? '追加指令 (⌘↵ / Ctrl+Enter)' : '发送 (⌘↵ / Ctrl+Enter)';
        sendBtn.setAttribute('aria-label', busy ? '追加指令' : '发送');
      }
      updateComposerPlaceholder();
    }

    function renderSteeringQueue(messages) {
      pendingSteeringQueue = Array.isArray(messages) ? messages : [];
      const count = pendingSteeringQueue.length;
      if (steeringHintLabel) {
        steeringHintLabel.textContent = count > 0
          ? count + ' 条消息待注入；将在下一次工具调用后生效'
          : 'Agent 正在运行；发送的新消息会在下一次工具调用后生效';
      }
      if (steeringQueuePreview) {
        const latest = count > 0 ? String(pendingSteeringQueue[count - 1].text || '').trim() : '';
        steeringQueuePreview.textContent = latest ? '↳ ' + latest : '';
        steeringQueuePreview.classList.toggle('visible', !!latest);
        steeringQueuePreview.title = latest;
      }
    }

    function updateComposerPlaceholder() {
      if (!input) return;
      if (isBusy) {
        input.placeholder = '补充或纠正 Agent 的下一步行为…';
      } else {
        input.placeholder = currentMode === 'plan'
          ? '描述目标，CrabCode 会先只读分析并生成计划…'
          : '输入问题或命令（如 /help）…';
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
        if (contextTooltip) contextTooltip.classList.remove('visible');
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

    function positionContextTooltip() {
      if (!contextMeter || !contextTooltip || contextMeter.hidden) return;
      const margin = 8;
      const gap = 8;
      const meterRect = contextMeter.getBoundingClientRect();
      const cardRect = composerCard.getBoundingClientRect();
      const tooltipRect = contextTooltip.getBoundingClientRect();
      const maxLeft = Math.max(margin, window.innerWidth - tooltipRect.width - margin);
      const preferredLeft = meterRect.left + meterRect.width / 2 - tooltipRect.width / 2;
      const left = Math.min(Math.max(preferredLeft, margin), maxLeft);
      const top = Math.max(margin, cardRect.top - tooltipRect.height - gap);
      const arrowLeft = Math.min(
        Math.max(meterRect.left + meterRect.width / 2 - left - 5, 10),
        Math.max(10, tooltipRect.width - 19),
      );
      contextTooltip.style.left = left + 'px';
      contextTooltip.style.top = top + 'px';
      contextTooltip.style.setProperty('--ctx-arrow-left', arrowLeft + 'px');
    }

    function showContextTooltip() {
      if (!contextTooltip || !contextMeter || contextMeter.hidden) return;
      positionContextTooltip();
      contextTooltip.classList.add('visible');
    }

    function hideContextTooltip() {
      if (contextTooltip) contextTooltip.classList.remove('visible');
    }

    if (contextMeter) {
      contextMeter.addEventListener('mouseenter', showContextTooltip);
      contextMeter.addEventListener('mouseleave', hideContextTooltip);
      contextMeter.addEventListener('focus', showContextTooltip);
      contextMeter.addEventListener('blur', hideContextTooltip);
      window.addEventListener('resize', function() {
        if (contextTooltip && contextTooltip.classList.contains('visible')) {
          positionContextTooltip();
        }
      });
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
      const previousValue = currentModelValue;
      if (models.length === 0) {
        currentModelList = [];
        currentModelGroups = {};
        currentModelValue = '';
        if (modelBtn) modelBtn.disabled = true;
        if (modelSelectLabel) {
          modelSelectLabel.textContent = msg.connected ? '（网关未返回可用模型）' : '（正在连接网关…）';
          modelSelectLabel.title = modelSelectLabel.textContent;
        }
        if (modelSelectWrap) modelSelectWrap.classList.add('is-empty');
      } else {
        currentModelList = models;
        currentModelGroups = msg.modelGroups || {};
        const preferred = [
          msg.selectedModel,
          msg.defaultModel,
          previousValue,
          models[0],
        ].find(function(v) { return !!v && models.indexOf(v) >= 0; }) || models[0];
        currentModelValue = preferred;
        if (modelBtn) modelBtn.disabled = false;
        if (modelSelectLabel) {
          modelSelectLabel.textContent = preferred;
          modelSelectLabel.title = preferred;
        }
        if (modelSelectWrap) modelSelectWrap.classList.remove('is-empty');
        renderModelMenuItems(models, preferred, '');
      }
      const newPerm = msg.permissionMode === 'run_everything' ? 'run_everything' : (msg.permissionMode === 'ai_review' ? 'ai_review' : (msg.permissionMode === 'ask' ? 'ask' : 'default'));
      if (permBtn && permLabel && permIcon && permMenu) {
        permLabel.textContent = newPerm === 'run_everything' ? '完全访问' : (newPerm === 'ai_review' ? 'AI 审查' : (newPerm === 'ask' ? '每次确认' : '工作区默认规则'));
        permIcon.textContent = newPerm === 'run_everything' ? '⚡' : (newPerm === 'ai_review' ? '🤖' : (newPerm === 'ask' ? '🛡' : '⚙'));
        permBtn.classList.toggle('perm-danger', newPerm === 'run_everything');
        permMenu.querySelectorAll('.perm-item').forEach(function(el) {
          el.classList.toggle('active', el.getAttribute('data-perm') === newPerm);
        });
      }
      const nextPendingEditsVisibleFiles = normalizePendingEditsVisibleFiles(msg.pendingEditsVisibleFiles);
      if (nextPendingEditsVisibleFiles !== pendingEditsVisibleFiles) {
        pendingEditsVisibleFiles = nextPendingEditsVisibleFiles;
        if (currentPendingEditSummary) renderPendingEdits(currentPendingEditSummary);
      }
    }

    // ── Slash command popup ───────────────────────────────────────

    const BUILTIN_COMMANDS = [
      { name: '/help',          desc: '显示帮助',                    badge: '' },
      { name: '/plan',          desc: '切换到计划模式（只读分析）',    badge: '' },
      { name: '/agent',         desc: '切换到 Agent 模式 / 查看 Agent', badge: '' },
      { name: '/plan-status',   desc: '显示当前计划状态',             badge: '' },
      { name: '/agents',        desc: '列出托管的 Agent',             badge: '' },
      { name: '/agent-log',     desc: '查看 Agent transcript',         badge: '' },
      { name: '/agent-send',    desc: '向 Agent 追加输入',              badge: '' },
      { name: '/wait',          desc: '等待 Agent 完成',                badge: '' },
      { name: '/cancel-agent',  desc: '取消 Agent',                    badge: '' },
      { name: '/spawn-agent',   desc: '启动后台 Agent（支持类型/名称/模型）', badge: '' },
      { name: '/goal',          desc: '设置或管理持久 Goal',             badge: '' },
      { name: '/tasks',         desc: '列出、查看、停止后台任务',          badge: '' },
      { name: '/peers',         desc: '列出其他 CrabCode 会话',          badge: '' },
      { name: '/peer-send',     desc: '向其他会话发送消息',              badge: '' },
      { name: '/status',        desc: '显示会话状态',                 badge: '' },
      { name: '/effort',        desc: '查看/设置推理强度',             badge: '' },
      { name: '/ultra',         desc: '切换/设置 Ultra mode',         badge: '' },
      { name: '/model',         desc: '显示/切换模型',                badge: '' },
      { name: '/new',           desc: '开始新会话',                   badge: '' },
      { name: '/compact',       desc: '压缩对话上下文',               badge: '' },
      { name: '/clear',         desc: '清除历史记录',                 badge: '' },
      { name: '/sessions',      desc: '列出所有会话',                 badge: '' },
      { name: '/recent',        desc: '列出最近的会话',               badge: '' },
      { name: '/search',        desc: '搜索会话',                     badge: '' },
      { name: '/archive',       desc: '归档会话',                     badge: '' },
      { name: '/prune',         desc: '归档/清理过期会话',             badge: '' },
      { name: '/export',        desc: '导出会话 (md/json，可指定路径)',     badge: '' },
      { name: '/stats',         desc: '使用统计',                     badge: '' },
      { name: '/checkpoint',    desc: '创建检查点（含文件快照）',      badge: '' },
      { name: '/checkpoints',   desc: '列出检查点',                   badge: '' },
      { name: '/rollback',      desc: '回滚对话到检查点',             badge: '' },
      { name: '/revert',        desc: '还原文件和对话到检查点',       badge: '' },
      { name: '/undo',          desc: '撤销最后一个检查点',           badge: '' },
      { name: '/resume',        desc: '恢复会话',                     badge: '' },
      { name: '/logs',          desc: '显示后台日志',                 badge: '' },
      { name: '/team',          desc: '团队管理（创建/协作/任务板）',       badge: '' },
      { name: '/schedule',      desc: '定时任务管理（创建/执行/历史）',       badge: '' },
      { name: '/image',         desc: '附加图片到下一条消息',             badge: '' },
    ];

    const EFFORT_LEVELS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];

    let slashSkills = [];
    let slashActiveIndex = -1;
    let slashItems = [];

    function fetchSkills() {
      vscode.postMessage({ type: 'fetchSkills' });
    }

    function highlight(text, query) {
      if (!query) return escapeHtml(text);
      const idx = text.toLowerCase().indexOf(query.toLowerCase());
      if (idx < 0) return escapeHtml(text);
      return escapeHtml(text.slice(0, idx)) +
        '<mark>' + escapeHtml(text.slice(idx, idx + query.length)) + '</mark>' +
        escapeHtml(text.slice(idx + query.length));
    }

    // Commands that have sub-items (populated dynamically)
    const SUBCOMMAND_SOURCES = {
      '/model': function() {
        return currentModelList.map(function(m) {
          return { name: m, desc: m };
        });
      },
      '/tasks': function() {
        return [
          { name: 'list', desc: '列出后台任务' },
          { name: 'show', desc: '查看任务详情' },
          { name: 'output', desc: '查看任务输出' },
          { name: 'stop', desc: '停止后台任务' },
        ];
      },
      '/team': function() {
        return [
          { name: 'list', desc: '列出 Team' },
          { name: 'create', desc: '创建 Team' },
          { name: 'status', desc: '查看 Team 状态' },
          { name: 'messages', desc: '查看 Team 消息' },
          { name: 'tasks', desc: '查看 Team 任务板' },
          { name: 'spawn', desc: '添加 teammate' },
          { name: 'remove', desc: '移除 teammate' },
          { name: 'message', desc: '发送 Team 消息' },
          { name: 'broadcast', desc: '广播 Team 消息' },
          { name: 'mark-read', desc: '标记 Team 消息已读' },
          { name: 'task-add', desc: '添加 Team 任务' },
          { name: 'task-claim', desc: '认领 Team 任务' },
          { name: 'task-complete', desc: '完成 Team 任务' },
          { name: 'task-fail', desc: '将 Team 任务标记失败' },
          { name: 'bridge', desc: '设置跨 Team Bridge' },
          { name: 'bridge-status', desc: '查看跨 Team Bridge' },
          { name: 'cross-message', desc: '发送跨 Team 消息' },
          { name: 'shutdown', desc: '关闭 Team' },
        ];
      },
      '/schedule': function() {
        return [
          { name: 'list', desc: '列出定时任务' },
          { name: 'show', desc: '查看定时任务详情' },
          { name: 'runs', desc: '查看定时任务执行历史' },
          { name: 'create', desc: '创建定时任务' },
          { name: 'pause', desc: '暂停定时任务' },
          { name: 'resume', desc: '恢复定时任务' },
          { name: 'run', desc: '立即执行定时任务' },
          { name: 'cancel', desc: '删除定时任务' },
        ];
      },
    };

    function buildSlashItems(query, subCmd) {
      if (subCmd) {
        // Sub-completion mode: show sub-items for the given command
        const src = SUBCOMMAND_SOURCES[subCmd];
        if (!src) return [];
        const q = query.toLowerCase();
        return src().filter(function(s) {
          return !q || s.name.toLowerCase().startsWith(q);
        }).map(function(s) {
          return { name: s.name, desc: s.desc, type: 'sub', cmd: subCmd };
        });
      }
      const q = query.toLowerCase();
      const matchBuiltin = BUILTIN_COMMANDS.filter(c => c.name.slice(1).startsWith(q) || c.desc.toLowerCase().includes(q));
      const matchSkills = slashSkills.filter(s => s.name.toLowerCase().startsWith(q) || (s.description || '').toLowerCase().includes(q));
      return [
        ...matchBuiltin.map(c => ({ ...c, type: 'builtin' })),
        ...matchSkills.map(s => ({ name: '/' + s.name, desc: s.description || '', badge: 'skill', type: 'skill' })),
      ];
    }

    function renderSlashPopup(query, subCmd) {
      slashItems = buildSlashItems(query, subCmd);
      if (slashItems.length === 0) { closeSlashPopup(); return; }

      const builtins = slashItems.filter(i => i.type === 'builtin');
      const skills = slashItems.filter(i => i.type === 'skill');
      const subs = slashItems.filter(i => i.type === 'sub');

      let html = '';
      if (subs.length > 0) {
        html += '<div class="slash-popup-section">' + escapeHtml(subCmd) + '</div>';
        subs.forEach(function(item, idx) {
          const activeClass = idx === slashActiveIndex ? ' active' : '';
          html += '<div class="slash-item' + activeClass + '" data-index="' + idx + '" role="option">' +
            '<span class="slash-name">' + highlight(item.name, query) + '</span>' +
            '<span class="slash-desc">' + escapeHtml(item.desc) + '</span>' +
            '</div>';
        });
      }
      if (builtins.length > 0) {
        html += '<div class="slash-popup-section">命令</div>';
        builtins.forEach(function(item, idx) {
          const activeClass = idx === slashActiveIndex ? ' active' : '';
          html += '<div class="slash-item' + activeClass + '" data-index="' + idx + '" role="option">' +
            '<span class="slash-name">' + highlight(item.name, '/' + query) + '</span>' +
            '<span class="slash-desc">' + escapeHtml(item.desc) + '</span>' +
            '</div>';
        });
      }
      if (skills.length > 0) {
        html += '<div class="slash-popup-section">Skills</div>';
        skills.forEach(function(item, idx) {
          const globalIdx = builtins.length + idx;
          const activeClass = globalIdx === slashActiveIndex ? ' active' : '';
          html += '<div class="slash-item' + activeClass + '" data-index="' + globalIdx + '" role="option">' +
            '<span class="slash-name">' + highlight(item.name, '/' + query) + '</span>' +
            '<span class="slash-desc">' + escapeHtml(item.desc) + '</span>' +
            '<span class="slash-badge">skill</span>' +
            '</div>';
        });
      }

      slashPopupList.innerHTML = html;
      slashPopup.classList.remove('hidden');
      positionSlashPopup();

      slashPopupList.querySelectorAll('.slash-item').forEach(function(el) {
        el.addEventListener('mousedown', function(e) {
          e.preventDefault();
          const idx = parseInt(el.getAttribute('data-index'), 10);
          selectSlashItem(idx);
        });
        el.addEventListener('mouseover', function() {
          const idx = parseInt(el.getAttribute('data-index'), 10);
          setSlashActive(idx);
        });
      });
    }

    function positionSlashPopup() {
      const cardRect = composerCard.getBoundingClientRect();
      const popupRect = slashPopup.getBoundingClientRect();
      const margin = 8;
      const gap = 6;
      const left = Math.max(margin, cardRect.left);
      const width = cardRect.width;
      const top = Math.max(margin, cardRect.top - popupRect.height - gap);
      slashPopup.style.left = left + 'px';
      slashPopup.style.top = top + 'px';
      slashPopup.style.width = width + 'px';
    }

    function setSlashActive(idx) {
      slashActiveIndex = idx;
      slashPopupList.querySelectorAll('.slash-item').forEach(function(el, i) {
        el.classList.toggle('active', i === idx);
      });
      const activeEl = slashPopupList.querySelector('.slash-item.active');
      if (activeEl) activeEl.scrollIntoView({ block: 'nearest' });
    }

    function selectSlashItem(idx) {
      const item = slashItems[idx];
      if (!item) return;
      if (item.type === 'sub') {
        // Fill in full command + sub-value, ready to send
        input.value = item.cmd + ' ' + item.name;
        input.focus();
        closeSlashPopup();
      } else if (SUBCOMMAND_SOURCES[item.name]) {
        // Has sub-items — fill with trailing space to trigger sub-completion
        input.value = item.name + ' ';
        input.focus();
        slashActiveIndex = -1;
        renderSlashPopup('', item.name);
      } else {
        input.value = item.name + ' ';
        input.focus();
        closeSlashPopup();
      }
    }

    function closeSlashPopup() {
      slashPopup.classList.add('hidden');
      slashActiveIndex = -1;
      slashItems = [];
    }

    function getSlashQuery() {
      const val = input.value;
      if (!val.startsWith('/')) return null;
      const spaceIdx = val.indexOf(' ');
      if (spaceIdx < 0) {
        // Still typing the command name
        return { query: val.slice(1), subCmd: null };
      }
      // Has a space — check if the command has sub-items
      const cmd = val.slice(0, spaceIdx);
      if (SUBCOMMAND_SOURCES[cmd]) {
        return { query: val.slice(spaceIdx + 1), subCmd: cmd };
      }
      return null; // Unknown sub-command, close popup
    }

    input.addEventListener('input', function() {
      const result = getSlashQuery();
      if (result !== null) {
        slashActiveIndex = -1;
        renderSlashPopup(result.query, result.subCmd);
      } else {
        closeSlashPopup();
      }
    });

    input.addEventListener('keydown', function(e) {
      if (slashPopup.classList.contains('hidden')) return;
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSlashActive(Math.min(slashActiveIndex + 1, slashItems.length - 1));
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSlashActive(Math.max(slashActiveIndex - 1, 0));
      } else if (e.key === 'Enter' && !e.ctrlKey && !e.metaKey) {
        if (slashActiveIndex >= 0) {
          e.preventDefault();
          selectSlashItem(slashActiveIndex);
        }
      } else if (e.key === 'Escape') {
        closeSlashPopup();
      } else if (e.key === 'Tab') {
        e.preventDefault();
        const nextIdx = slashActiveIndex < 0 ? 0 : (slashActiveIndex + 1) % slashItems.length;
        setSlashActive(nextIdx);
      }
    });

    document.addEventListener('click', function(e) {
      if (!slashPopup.contains(e.target) && e.target !== input) closeSlashPopup();
    });

    // ── Send ─────────────────────────────────────────────────────

    // Commands handled directly without going to the model
    function addLocalSystemMessage(text) {
      vscode.postMessage({ type: 'localMessage', role: 'system', text: text });
    }

    function shellTokens(raw) {
      const tokens = [];
      const pattern = /"([^"\\]*(?:\\.[^"\\]*)*)"|'([^']*)'|(\S+)/g;
      let match;
      while ((match = pattern.exec(raw || '')) !== null) {
        tokens.push(match[1] !== undefined ? match[1].replace(/\\"/g, '"') : (match[2] !== undefined ? match[2] : match[3]));
      }
      return tokens;
    }

    function parseSessionLaunchArgs(raw, requireSelector) {
      const tokens = shellTokens(raw);
      const options = {};
      const positionals = [];
      const optionNames = {
        '--model': 'model', '-m': 'model',
        '--provider': 'provider',
        '--base-url': 'base_url',
        '--api-format': 'api_format',
        '--model-profile': 'model_profile', '-M': 'model_profile',
      };
      for (let i = 0; i < tokens.length; i += 1) {
        const token = tokens[i];
        let key = token;
        let value = null;
        const equal = token.indexOf('=');
        if (equal > 0) {
          key = token.slice(0, equal);
          value = token.slice(equal + 1);
        }
        const field = optionNames[key];
        if (field) {
          if (value === null) value = tokens[++i];
          if (!value) return { error: '选项 ' + key + ' 需要一个值。' };
          options[field] = value;
        } else if (token.charAt(0) === '-') {
          return { error: '未知选项：' + token };
        } else {
          positionals.push(token);
        }
      }
      if (requireSelector && positionals.length !== 1) {
        return { error: '用法：/resume <session-id> [--model MODEL] [--provider PROVIDER] [--base-url URL] [--api-format FORMAT] [--model-profile PROFILE]' };
      }
      if (!requireSelector && positionals.length > 0) {
        return { error: '用法：/new [--model MODEL] [--provider PROVIDER] [--base-url URL] [--api-format FORMAT] [--model-profile PROFILE]' };
      }
      return { selector: positionals[0] || null, options: options };
    }

    const DIRECT_COMMANDS = {
      '/help': function() {
        const bt = String.fromCharCode(96);
        const lines = BUILTIN_COMMANDS.map(function(c) {
          return '- ' + bt + c.name + bt + ' — ' + c.desc;
        });
        lines.push('- ' + bt + '! <cmd>' + bt + ' — 在当前工作区终端运行命令');
        if (slashSkills.length > 0) {
          lines.push('');
          lines.push('**Skills**');
          slashSkills.forEach(function(s) {
            lines.push('- ' + bt + '/' + s.name + bt + ' — ' + (s.description || ''));
          });
        }
        addLocalSystemMessage('## CrabCode 命令\\n\\n' + lines.join('\\n'));
        return true;
      },
      '/model': function(args) {
        if (args) {
          vscode.postMessage({ type: 'setModel', name: args });
        } else {
          vscode.postMessage({ type: 'fetchModel' });
        }
        return true;
      },
      '/compact': function(args) {
        vscode.postMessage({ type: 'compact', customInstructions: args || '' });
        return true;
      },
      '/new': function(args) {
        const parsed = parseSessionLaunchArgs(args, false);
        if (parsed.error) { addLocalSystemMessage(parsed.error); return true; }
        vscode.postMessage({ type: 'newSession', options: parsed.options });
        return true;
      },
      '/clear': function() {
        vscode.postMessage({ type: 'clearMessages' });
        return true;
      },
      '/status': function() {
        vscode.postMessage({ type: 'fetchStatus' });
        return true;
      },
      '/effort': function(args) {
        const effort = (args || '').toLowerCase();
        if (effort && EFFORT_LEVELS.indexOf(effort) < 0) {
          addLocalSystemMessage('用法：/effort <none|minimal|low|medium|high|xhigh|max>');
          return true;
        }
        vscode.postMessage({ type: 'setEffort', effort: effort || null });
        return true;
      },
      '/ultra': function(args) {
        const value = (args || '').toLowerCase();
        if (value && value !== 'true' && value !== 'false') {
          addLocalSystemMessage('用法：/ultra [true|false]；不带参数时切换');
          return true;
        }
        vscode.postMessage({
          type: 'setUltra',
          enabled: value ? value === 'true' : null,
        });
        return true;
      },
      '/plan': function() {
        vscode.postMessage({ type: 'switchMode', mode: 'plan' });
        updateModeButton('plan');
        return true;
      },
      '/agent': function(args) {
        if (args) {
          vscode.postMessage({ type: 'fetchAgent', agentId: args.split(/\s+/)[0] });
          return true;
        }
        vscode.postMessage({ type: 'switchMode', mode: 'agent' });
        updateModeButton('agent');
        return true;
      },
      '/sessions': function() {
        vscode.postMessage({ type: 'fetchSessions' });
        return true;
      },
      '/recent': function(args) {
        const limit = parseInt(args, 10) || 10;
        vscode.postMessage({ type: 'fetchRecentSessions', limit: limit });
        return true;
      },
      '/search': function(args) {
        if (!args) { addLocalSystemMessage('用法：/search <关键词>'); return true; }
        vscode.postMessage({ type: 'searchSessions', query: args });
        return true;
      },
      '/archive': function(args) {
        if (!args) { addLocalSystemMessage('用法：/archive <session_id>'); return true; }
        vscode.postMessage({ type: 'archiveSession', sessionId: args });
        return true;
      },
      '/prune': function(args) {
        const tokens = shellTokens(args);
        let days = 30;
        let deleteFiles = false;
        for (let i = 0; i < tokens.length; i += 1) {
          const token = tokens[i];
          if (token === '--delete-files' || token === '--purge') {
            deleteFiles = true;
          } else if (token === '--days' && tokens[i + 1]) {
            days = parseInt(tokens[++i], 10);
          } else if (token.indexOf('--days=') === 0) {
            days = parseInt(token.slice(7), 10);
          } else if (/^\d+$/.test(token)) {
            days = parseInt(token, 10);
          } else {
            addLocalSystemMessage('用法：/prune [days] [--delete-files]');
            return true;
          }
        }
        if (!Number.isFinite(days) || days < 0) {
          addLocalSystemMessage('days 必须是非负整数');
          return true;
        }
        vscode.postMessage({ type: 'pruneSessions', days: days, deleteFiles: deleteFiles });
        return true;
      },
      '/export': function(args) {
        const tokens = shellTokens(args);
        let fmt = 'md';
        let sessionId = null;
        const positional = [];
        for (let i = 0; i < tokens.length; i += 1) {
          const token = tokens[i];
          if (token === '--session' || token === '--id') {
            sessionId = tokens[++i] || null;
            if (!sessionId) { addLocalSystemMessage('用法：/export [md|json] [path] [--session ID]'); return true; }
          } else if (token.indexOf('--session=') === 0 || token.indexOf('--id=') === 0) {
            sessionId = token.slice(token.indexOf('=') + 1) || null;
            if (!sessionId) { addLocalSystemMessage('用法：/export [md|json] [path] [--session ID]'); return true; }
          } else if (token === 'json') fmt = 'json';
          else if (token === 'md' || token === 'markdown') fmt = 'md';
          else positional.push(token);
        }
        if (positional.length > 1) {
          addLocalSystemMessage('用法：/export [md|json] [path] [--session ID]');
          return true;
        }
        vscode.postMessage({ type: 'exportSession', format: fmt, path: positional[0] || undefined, sessionId: sessionId || undefined });
        return true;
      },
      '/stats': function() {
        vscode.postMessage({ type: 'fetchStats' });
        return true;
      },
      '/checkpoint': function(args) {
        vscode.postMessage({ type: 'createCheckpoint', label: args || '' });
        return true;
      },
      '/checkpoints': function() {
        vscode.postMessage({ type: 'fetchCheckpoints' });
        return true;
      },
      '/rollback': function(args) {
        if (!args) { addLocalSystemMessage('用法：/rollback <checkpoint_id>'); return true; }
        vscode.postMessage({ type: 'rollbackCheckpoint', checkpointId: args });
        return true;
      },
      '/revert': function(args) {
        if (!args) { addLocalSystemMessage('用法：/revert <checkpoint_id>'); return true; }
        vscode.postMessage({ type: 'revertCheckpoint', checkpointId: args });
        return true;
      },
      '/undo': function() {
        vscode.postMessage({ type: 'undoCheckpoint' });
        return true;
      },
      '/resume': function(args) {
        if (!args) { vscode.postMessage({ type: 'fetchSessions' }); return true; }
        const parsed = parseSessionLaunchArgs(args, true);
        if (parsed.error) { addLocalSystemMessage(parsed.error); return true; }
        vscode.postMessage({ type: 'resumeSession', sessionId: parsed.selector, options: parsed.options });
        return true;
      },
      '/agents': function() {
        vscode.postMessage({ type: 'fetchAgents' });
        return true;
      },
      '/agent-log': function(args) {
        const tokens = shellTokens(args);
        if (!tokens[0]) { addLocalSystemMessage('用法：/agent-log <agent-id> [lines]'); return true; }
        vscode.postMessage({ type: 'fetchAgentLog', agentId: tokens[0], lines: parseInt(tokens[1] || '200', 10) || 200 });
        return true;
      },
      '/agent-send': function(args) {
        const tokens = shellTokens(args);
        let interrupt = false;
        for (let i = tokens.length - 1; i >= 0; i -= 1) {
          if (tokens[i] === '--interrupt') { interrupt = true; tokens.splice(i, 1); }
        }
        if (tokens.length < 2) { addLocalSystemMessage('用法：/agent-send <agent-id> [--interrupt] <prompt>'); return true; }
        vscode.postMessage({ type: 'sendAgentInput', agentId: tokens[0], prompt: tokens.slice(1).join(' '), interrupt: interrupt });
        return true;
      },
      '/wait': function(args) {
        const tokens = shellTokens(args);
        if (!tokens[0]) { addLocalSystemMessage('用法：/wait <agent-id> [agent-id ...] [--timeout MS]'); return true; }
        let timeout = null;
        const ids = [];
        for (let i = 0; i < tokens.length; i += 1) {
          const token = tokens[i];
          if (token === '--timeout') {
            timeout = parseInt(tokens[++i] || '', 10);
          } else if (token.indexOf('--timeout=') === 0) {
            timeout = parseInt(token.slice(10), 10);
          } else {
            ids.push.apply(ids, token.split(',').filter(Boolean));
          }
        }
        // Preserve the historical /wait <id> <timeout-ms> form.
        if (ids.length === 2 && timeout === null && /^[0-9]+$/.test(ids[1])) {
          timeout = parseInt(ids.pop(), 10);
        }
        if (!ids.length || (timeout !== null && (!Number.isFinite(timeout) || timeout < 0))) {
          addLocalSystemMessage('用法：/wait <agent-id> [agent-id ...] [--timeout MS]');
          return true;
        }
        vscode.postMessage({ type: 'waitAgent', agentIds: ids, agentId: ids.length === 1 ? ids[0] : undefined, timeoutMs: timeout });
        return true;
      },
      '/cancel-agent': function(args) {
        if (!args) { addLocalSystemMessage('用法：/cancel-agent <agent-id>'); return true; }
        vscode.postMessage({ type: 'cancelAgent', agentId: args.split(/\s+/)[0] });
        return true;
      },
      '/spawn-agent': function(args) {
        const tokens = shellTokens(args);
        let subagentType = 'generalPurpose';
        let name = null;
        let modelProfile = null;
        let callback = true;
        const prompt = [];
        for (let i = 0; i < tokens.length; i += 1) {
          const token = tokens[i];
          let key = token;
          let value = null;
          const equal = token.indexOf('=');
          if (equal > 0) {
            key = token.slice(0, equal);
            value = token.slice(equal + 1);
          }
          if (key === '--type' || key === '--subagent-type') {
            value = value === null ? tokens[++i] : value;
            if (!value) { addLocalSystemMessage('用法：/spawn-agent [--type TYPE] [--name NAME] [--model PROFILE] <prompt>'); return true; }
            subagentType = value;
          } else if (key === '--name') {
            value = value === null ? tokens[++i] : value;
            if (!value) { addLocalSystemMessage('用法：/spawn-agent [--type TYPE] [--name NAME] [--model PROFILE] <prompt>'); return true; }
            name = value;
          } else if (key === '--model' || key === '--model-profile') {
            value = value === null ? tokens[++i] : value;
            if (!value) { addLocalSystemMessage('用法：/spawn-agent [--type TYPE] [--name NAME] [--model PROFILE] <prompt>'); return true; }
            modelProfile = value;
          } else if (key === '--callback') {
            value = value === null ? tokens[++i] : value;
            if (value !== 'true' && value !== 'false') { addLocalSystemMessage('callback 必须是 true 或 false'); return true; }
            callback = value === 'true';
          } else if (token === '--no-callback') {
            callback = false;
          } else if (token.charAt(0) === '-') {
            addLocalSystemMessage('未知选项：' + token + '。用法：/spawn-agent [--type TYPE] [--name NAME] [--model PROFILE] <prompt>');
            return true;
          } else {
            prompt.push(token);
          }
        }
        if (!prompt.length) { addLocalSystemMessage('用法：/spawn-agent [--type TYPE] [--name NAME] [--model PROFILE] [--callback true|false] <prompt>'); return true; }
        vscode.postMessage({ type: 'spawnAgent', prompt: prompt.join(' '), subagentType: subagentType, name: name, modelProfile: modelProfile, callback: callback });
        return true;
      },
      '/goal': function(args) {
        const tokens = shellTokens(args);
        if (!tokens.length || ['show', 'status', 'view'].indexOf(tokens[0].toLowerCase()) >= 0) {
          vscode.postMessage({ type: 'fetchGoal' });
          return true;
        }
        let action = tokens[0].toLowerCase();
        if (['pause', 'resume', 'complete', 'blocked', 'clear'].indexOf(action) >= 0) {
          if (tokens.length !== 1) addLocalSystemMessage('用法：/goal ' + action);
          else vscode.postMessage({ type: 'manageGoal', action: action === 'pause' ? 'pause' : action, objective: null, budgetWasSet: false });
          return true;
        }
        if (action !== 'set' && action !== 'edit') {
          tokens.unshift(action);
          action = 'set';
        } else {
          tokens.shift();
        }
        let budgetWasSet = false;
        let tokenBudget = null;
        const objective = [];
        for (let i = 0; i < tokens.length; i += 1) {
          const token = tokens[i];
          if (token === '--no-budget') { budgetWasSet = true; tokenBudget = null; continue; }
          if (token === '--budget' && tokens[i + 1]) {
            budgetWasSet = true;
            tokenBudget = parseInt(tokens[++i], 10);
            continue;
          }
          if (token.indexOf('--budget=') === 0) {
            budgetWasSet = true;
            tokenBudget = parseInt(token.slice(9), 10);
            continue;
          }
          objective.push(token);
        }
        if (!objective.length || (budgetWasSet && (!Number.isFinite(tokenBudget) || tokenBudget <= 0) && tokenBudget !== null)) {
          addLocalSystemMessage('用法：/goal [set|edit] [--budget N|--no-budget] <objective>');
          return true;
        }
        vscode.postMessage({ type: 'manageGoal', action: action, objective: objective.join(' '), tokenBudget: tokenBudget, budgetWasSet: budgetWasSet });
        return true;
      },
      '/tasks': function(args) {
        const tokens = shellTokens(args);
        const sub = (tokens.shift() || 'list').toLowerCase();
        if (sub === 'list') {
          vscode.postMessage({ type: 'fetchTasks' });
        } else if (sub === 'show' || sub === 'get') {
          if (!tokens[0]) addLocalSystemMessage('用法：/tasks show <task-id>');
          else vscode.postMessage({ type: 'fetchTask', taskId: tokens[0] });
        } else if (sub === 'output' || sub === 'log') {
          if (!tokens[0]) addLocalSystemMessage('用法：/tasks output <task-id> [lines]');
          else {
            const outputLines = tokens[1] ? parseInt(tokens[1], 10) : 200;
            if (!Number.isFinite(outputLines) || outputLines < 1) addLocalSystemMessage('lines 必须是正整数');
            else vscode.postMessage({ type: 'fetchTaskOutput', taskId: tokens[0], lines: outputLines });
          }
        } else if (sub === 'stop') {
          if (!tokens[0]) addLocalSystemMessage('用法：/tasks stop <task-id>');
          else vscode.postMessage({ type: 'stopTask', taskId: tokens[0] });
        } else {
          addLocalSystemMessage('用法：/tasks [list|show|output|stop] <task-id>');
        }
        return true;
      },
      '/peers': function() {
        vscode.postMessage({ type: 'fetchPeers' });
        return true;
      },
      '/peer-send': function(args) {
        const tokens = shellTokens(args);
        if (tokens.length < 2) { addLocalSystemMessage('用法：/peer-send <session|name> <text>'); return true; }
        vscode.postMessage({ type: 'sendPeerMessage', to: tokens[0], text: tokens.slice(1).join(' ') });
        return true;
      },
      '/team': function(args) {
        const tokens = shellTokens(args);
        const sub = (tokens.shift() || 'list').toLowerCase();
        if (sub === 'list') { vscode.postMessage({ type: 'fetchTeams' }); return true; }
        if (sub === 'create') {
          const name = tokens.shift();
          if (!name) { addLocalSystemMessage('用法：/team create <name> [max-teammates]'); return true; }
          const max = tokens.shift();
          const maxTeammates = max ? parseInt(max, 10) : null;
          if (max && (!Number.isFinite(maxTeammates) || maxTeammates < 1)) { addLocalSystemMessage('max-teammates 必须是正整数'); return true; }
          vscode.postMessage({ type: 'createTeam', name: name, maxTeammates: maxTeammates });
          return true;
        }
        const teamId = tokens.shift();
        if (!teamId) { addLocalSystemMessage('用法：/team [list|create|status|messages|tasks|spawn|remove|message|broadcast|mark-read|task-add|task-claim|task-complete|task-fail|bridge|bridge-status|cross-message|shutdown] <team-id>'); return true; }
        if (sub === 'status') {
          vscode.postMessage({ type: 'fetchTeamStatus', teamId: teamId });
        } else if (sub === 'messages') {
          let agentId = null;
          let unread = false;
          for (let i = 0; i < tokens.length; i += 1) {
            if (tokens[i] === '--unread') unread = true;
            else if (tokens[i] === '--agent' || tokens[i] === '--agent-id') agentId = tokens[++i] || null;
            else if (tokens[i].indexOf('--agent=') === 0) agentId = tokens[i].slice(8);
            else { addLocalSystemMessage('用法：/team messages <team-id> [--agent ID] [--unread]'); return true; }
          }
          vscode.postMessage({ type: 'fetchTeamMessages', teamId: teamId, agentId: agentId, unread: unread });
        } else if (sub === 'tasks') {
          vscode.postMessage({ type: 'fetchTeamTasks', teamId: teamId });
        } else if (sub === 'remove') {
          const agentId = tokens.shift();
          if (!agentId || tokens.length) addLocalSystemMessage('用法：/team remove <team-id> <agent-id>');
          else vscode.postMessage({ type: 'removeTeamMember', teamId: teamId, agentId: agentId });
        } else if (sub === 'shutdown') {
          vscode.postMessage({ type: 'shutdownTeam', teamId: teamId });
        } else if (sub === 'spawn') {
          let role = 'worker';
          let name = null;
          let modelProfile = null;
          const prompt = [];
          for (let i = 0; i < tokens.length; i += 1) {
            const token = tokens[i];
            let key = token;
            let value = null;
            const equal = token.indexOf('=');
            if (equal > 0) { key = token.slice(0, equal); value = token.slice(equal + 1); }
            if (key === '--role') {
              value = value === null ? tokens[++i] : value;
              if (!value) { addLocalSystemMessage('用法：/team spawn <team-id> [--role ROLE] [--name NAME] [--model PROFILE] <prompt>'); return true; }
              role = value;
            } else if (key === '--name') {
              value = value === null ? tokens[++i] : value;
              if (!value) { addLocalSystemMessage('用法：/team spawn <team-id> [--role ROLE] [--name NAME] [--model PROFILE] <prompt>'); return true; }
              name = value;
            } else if (key === '--model' || key === '--model-profile') {
              value = value === null ? tokens[++i] : value;
              if (!value) { addLocalSystemMessage('用法：/team spawn <team-id> [--role ROLE] [--name NAME] [--model PROFILE] <prompt>'); return true; }
              modelProfile = value;
            } else if (token.charAt(0) === '-') {
              addLocalSystemMessage('未知选项：' + token); return true;
            } else {
              prompt.push(token);
            }
          }
          if (!prompt.length) { addLocalSystemMessage('用法：/team spawn <team-id> [--role ROLE] [--name NAME] [--model PROFILE] <prompt>'); return true; }
          if (['lead', 'worker', 'researcher', 'reviewer'].indexOf(role) < 0) { addLocalSystemMessage('role 必须是 lead、worker、researcher 或 reviewer'); return true; }
          vscode.postMessage({ type: 'spawnTeamMember', teamId: teamId, prompt: prompt.join(' '), role: role, name: name, modelProfile: modelProfile });
        } else if (sub === 'message') {
          const to = tokens.shift();
          const text = tokens.join(' ');
          if (!to || !text) addLocalSystemMessage('用法：/team message <team-id> <agent-id> <text>');
          else vscode.postMessage({ type: 'sendTeamMessage', teamId: teamId, to: to, text: text });
        } else if (sub === 'broadcast') {
          const text = tokens.join(' ');
          if (!text) addLocalSystemMessage('用法：/team broadcast <team-id> <text>');
          else vscode.postMessage({ type: 'broadcastTeamMessage', teamId: teamId, text: text });
        } else if (sub === 'mark-read') {
          const agentId = tokens.shift();
          if (!agentId) addLocalSystemMessage('用法：/team mark-read <team-id> <agent-id> [message-id ...]');
          else vscode.postMessage({ type: 'markTeamMessagesRead', teamId: teamId, agentId: agentId, messageIds: tokens.length ? tokens : undefined });
        } else if (sub === 'task-add') {
          const description = tokens.join(' ');
          if (!description) addLocalSystemMessage('用法：/team task-add <team-id> <description>');
          else vscode.postMessage({ type: 'addTeamTask', teamId: teamId, description: description });
        } else if (sub === 'task-claim') {
          const taskId = tokens.shift();
          if (!taskId || tokens.length > 1) addLocalSystemMessage('用法：/team task-claim <team-id> <task-id> [agent-id]');
          else vscode.postMessage({ type: 'claimTeamTask', teamId: teamId, taskId: taskId, agentId: tokens[0] || '' });
        } else if (sub === 'task-complete') {
          const taskId = tokens.shift();
          let agentId = '';
          if (tokens[0] === '--agent' || tokens[0] === '--agent-id') {
            tokens.shift();
            agentId = tokens.shift() || '';
          }
          const result = tokens.join(' ');
          if (!taskId) addLocalSystemMessage('用法：/team task-complete <team-id> <task-id> [--agent ID] [result]');
          else vscode.postMessage({ type: 'completeTeamTask', teamId: teamId, taskId: taskId, result: result, agentId: agentId });
        } else if (sub === 'task-fail') {
          const taskId = tokens.shift();
          let agentId = '';
          if (tokens[0] === '--agent' || tokens[0] === '--agent-id') {
            tokens.shift();
            agentId = tokens.shift() || '';
          }
          const reason = tokens.join(' ');
          if (!taskId) addLocalSystemMessage('用法：/team task-fail <team-id> <task-id> [--agent ID] [reason]');
          else vscode.postMessage({ type: 'failTeamTask', teamId: teamId, taskId: taskId, reason: reason, agentId: agentId });
        } else if (sub === 'bridge' || sub === 'bridge-status') {
          const otherTeam = tokens.shift();
          if (!otherTeam || (sub === 'bridge' && tokens.length > 1)) {
            addLocalSystemMessage('用法：/team bridge <team-a> <team-b> [allow_all|allow_tagged|deny]');
          } else if (sub === 'bridge-status' && tokens.length) {
            addLocalSystemMessage('用法：/team bridge-status <team-a> <team-b>');
          } else if (sub === 'bridge') {
            const policy = tokens[0] || 'allow_tagged';
            vscode.postMessage({ type: 'registerTeamBridge', teamA: teamId, teamB: otherTeam, policy: policy });
          } else {
            vscode.postMessage({ type: 'getTeamBridge', teamA: teamId, teamB: otherTeam });
          }
        } else if (sub === 'cross-message') {
          const toTeam = tokens.shift();
          let fromAgent = '';
          let toAgent = '';
          const textParts = [];
          for (let i = 0; i < tokens.length; i += 1) {
            const token = tokens[i];
            if (token === '--from-agent' || token === '--from') fromAgent = tokens[++i] || '';
            else if (token === '--to-agent' || token === '--to') toAgent = tokens[++i] || '';
            else textParts.push(token);
          }
          if (!toTeam || !textParts.length) addLocalSystemMessage('用法：/team cross-message <from-team> <to-team> [--from-agent ID] [--to-agent ID] <text>');
          else vscode.postMessage({ type: 'sendCrossTeamMessage', fromTeam: teamId, toTeam: toTeam, fromAgent: fromAgent, toAgent: toAgent, text: textParts.join(' ') });
        } else {
          addLocalSystemMessage('用法：/team [list|create|status|messages|tasks|spawn|remove|message|broadcast|mark-read|task-add|task-claim|task-complete|task-fail|bridge|bridge-status|cross-message|shutdown] <team-id>');
        }
        return true;
      },
      '/schedule': function(args) {
        const tokens = shellTokens(args);
        const sub = (tokens.shift() || 'list').toLowerCase();
        if (sub === 'list') {
          let status = null;
          let scheduleType = null;
          let enabled = null;
          let limit = 100;
          for (let i = 0; i < tokens.length; i += 1) {
            const token = tokens[i];
            let key = token;
            let value = null;
            const equal = token.indexOf('=');
            if (equal > 0) { key = token.slice(0, equal); value = token.slice(equal + 1); }
            if (key === '--status') { value = value === null ? tokens[++i] : value; status = value || null; }
            else if (key === '--type' || key === '--schedule-type') { value = value === null ? tokens[++i] : value; scheduleType = value || null; }
            else if (key === '--enabled') {
              value = value === null ? tokens[++i] : value;
              if (value !== 'true' && value !== 'false') { addLocalSystemMessage('enabled 必须是 true 或 false'); return true; }
              enabled = value === 'true';
            } else if (key === '--limit') {
              value = value === null ? tokens[++i] : value;
              limit = parseInt(value || '', 10);
              if (!Number.isFinite(limit) || limit < 1) { addLocalSystemMessage('limit 必须是正整数'); return true; }
            } else {
              addLocalSystemMessage('用法：/schedule list [--status STATUS] [--type cron|interval|once] [--enabled true|false] [--limit N]');
              return true;
            }
            if (value === null || value === '') { addLocalSystemMessage('选项 ' + key + ' 需要一个值'); return true; }
          }
          vscode.postMessage({ type: 'fetchSchedules', status: status || undefined, scheduleType: scheduleType || undefined, enabled: enabled === null ? undefined : enabled, limit: limit });
          return true;
        }
        if (sub === 'show' || sub === 'status') {
          if (tokens.length !== 1) { addLocalSystemMessage('用法：/schedule show <job-id>'); return true; }
          vscode.postMessage({ type: 'fetchSchedule', jobId: tokens[0] });
          return true;
        }
        if (sub === 'runs' || sub === 'history') {
          const jobId = tokens.shift();
          if (!jobId) { addLocalSystemMessage('用法：/schedule runs <job-id> [--status STATUS] [--limit N]'); return true; }
          let status = null;
          let limit = 50;
          for (let i = 0; i < tokens.length; i += 1) {
            const token = tokens[i];
            let key = token;
            let value = null;
            const equal = token.indexOf('=');
            if (equal > 0) { key = token.slice(0, equal); value = token.slice(equal + 1); }
            if (key === '--status') { value = value === null ? tokens[++i] : value; status = value || null; }
            else if (key === '--limit') {
              value = value === null ? tokens[++i] : value;
              limit = parseInt(value || '', 10);
              if (!Number.isFinite(limit) || limit < 1) { addLocalSystemMessage('limit 必须是正整数'); return true; }
            } else { addLocalSystemMessage('用法：/schedule runs <job-id> [--status STATUS] [--limit N]'); return true; }
            if (value === null || value === '') { addLocalSystemMessage('选项 ' + key + ' 需要一个值'); return true; }
          }
          vscode.postMessage({ type: 'fetchScheduleRuns', jobId: jobId, status: status || undefined, limit: limit });
          return true;
        }
        if (sub === 'pause' || sub === 'resume' || sub === 'run' || sub === 'cancel') {
          if (tokens.length !== 1) { addLocalSystemMessage('用法：/schedule ' + sub + ' <job-id>'); return true; }
          vscode.postMessage({ type: 'mutateSchedule', action: sub, jobId: tokens[0] });
          return true;
        }
        if (sub === 'create') {
          const positionals = [];
          const request = { tags: [], enabled: true, extra: {} };
          let promptMode = false;
          for (let i = 0; i < tokens.length; i += 1) {
            const token = tokens[i];
            if (token === '--') { promptMode = true; continue; }
            if (promptMode) { positionals.push(token); continue; }
            let key = token;
            let value = null;
            const equal = token.indexOf('=');
            if (equal > 0) { key = token.slice(0, equal); value = token.slice(equal + 1); }
            if (key === '--disabled') {
              if (value !== null) { addLocalSystemMessage('--disabled 不接受值'); return true; }
              request.enabled = false;
            } else if (key === '--max-runs' || key === '--timeout' || key === '--model' || key === '--model-profile' || key === '--cwd' || key === '--description' || key === '--tag' || key === '--enabled' || key === '--next-run' || key === '--job-session' || key === '--run-session' || key === '--extra') {
              value = value === null ? tokens[++i] : value;
              if (!value) { addLocalSystemMessage('选项 ' + key + ' 需要一个值'); return true; }
              if (key === '--max-runs' || key === '--timeout') {
                const numeric = parseInt(value, 10);
                if (!Number.isFinite(numeric) || numeric < 1) { addLocalSystemMessage(key + ' 必须是正整数'); return true; }
                request[key === '--max-runs' ? 'max_runs' : 'timeout'] = numeric;
              } else if (key === '--enabled') {
                if (value !== 'true' && value !== 'false') { addLocalSystemMessage('enabled 必须是 true 或 false'); return true; }
                request.enabled = value === 'true';
              } else if (key === '--next-run') request.next_run = value;
              else if (key === '--job-session' || key === '--run-session') request.job_session_id = value;
              else if (key === '--extra') {
                try {
                  const parsed = JSON.parse(value);
                  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('object required');
                  Object.assign(request.extra, parsed);
                } catch (_) {
                  addLocalSystemMessage('--extra 必须是 JSON 对象');
                  return true;
                }
              } else if (key === '--model' || key === '--model-profile') request.model_profile = value;
              else if (key === '--cwd') request.cwd = value;
              else if (key === '--description') request.description = value;
              else request.tags.push(value);
            } else if (token.charAt(0) === '-' && token !== '-') {
              addLocalSystemMessage('未知选项：' + token); return true;
            } else positionals.push(token);
          }
          if (positionals.length < 4) {
            addLocalSystemMessage('用法：/schedule create [选项] <name> <cron|interval|once> <schedule> <prompt>');
            return true;
          }
          const kind = positionals[1];
          if (kind !== 'cron' && kind !== 'interval' && kind !== 'once') {
            addLocalSystemMessage('schedule_type 必须是 cron、interval 或 once'); return true;
          }
          request.name = positionals[0];
          request.schedule_type = kind;
          request.schedule = positionals[2];
          request.prompt = positionals.slice(3).join(' ');
          vscode.postMessage({ type: 'createSchedule', request: request });
          return true;
        }
        addLocalSystemMessage('用法：/schedule [list|show|runs|create|pause|resume|run|cancel] ...');
        return true;
      },
      '/plan-status': function() {
        vscode.postMessage({ type: 'fetchPlanStatus' });
        return true;
      },
      '/logs': function(args) {
        const tokens = shellTokens(args);
        let tail = 100;
        let name = null;
        let clear = false;
        let follow = false;
        for (let i = 0; i < tokens.length; i += 1) {
          const token = tokens[i];
          if (token === '-f' || token === '--follow') { follow = true; continue; }
          if (token === '--stop') { vscode.postMessage({ type: 'stopLogFollow' }); return true; }
          if (token === '--clear') { clear = true; continue; }
          if (token === '--tail' && tokens[i + 1]) { tail = parseInt(tokens[++i], 10) || 100; continue; }
          if (token.indexOf('--tail=') === 0) { tail = parseInt(token.slice(7), 10) || 100; continue; }
          if (token.charAt(0) !== '-' && !name) name = token;
        }
        if (follow) {
          if (!name) addLocalSystemMessage('用法：/logs -f <name>');
          else vscode.postMessage({ type: 'followLogs', name: name });
        } else {
          if (clear && !name) { addLocalSystemMessage('用法：/logs --clear <name>'); return true; }
          vscode.postMessage({ type: 'fetchLogs', lines: tail, tail: tail, name: name, clear: clear });
        }
        return true;
      },
      '/image': function(args) {
        const paths = shellTokens(args);
        if (!paths.length) { addLocalSystemMessage('用法：/image <path> [path2 ...]'); return true; }
        vscode.postMessage({ type: 'attachImagePaths', paths: paths });
        return true;
      },
    };

    function send() {
      let text = input.value.trim();
      let extra = '';
      const bt = String.fromCharCode(96);
      pendingTextFiles.forEach(function(f) {
        extra += '\\n\\n[附加文件: ' + f.name + ']\\n' + bt + bt + bt + '\\n' + f.text + '\\n' + bt + bt + bt + '\\n';
      });
      text = (text + extra).trim();
      if (!text && pendingImages.length === 0 && pendingTextFiles.length === 0) return;

      if ((text === '!' || text.startsWith('! ')) && pendingImages.length === 0 && pendingTextFiles.length === 0) {
        const command = text.slice(1).trim();
        if (!command) addLocalSystemMessage('用法：! <cmd>');
        else vscode.postMessage({ type: 'runShellCommand', command: command });
        input.value = '';
        pendingImages.length = 0;
        pendingTextFiles.length = 0;
        renderAttachmentBar();
        closeSlashPopup();
        return;
      }

      // Intercept slash commands before sending to model
      if (text.startsWith('/') && pendingImages.length === 0 && pendingTextFiles.length === 0) {
        const spaceIdx = text.indexOf(' ');
        const cmd = spaceIdx < 0 ? text : text.slice(0, spaceIdx);
        const args = spaceIdx < 0 ? '' : text.slice(spaceIdx + 1).trim();
        const handler = DIRECT_COMMANDS[cmd];
        if (handler && handler(args)) {
          input.value = '';
          pendingImages.length = 0;
          pendingTextFiles.length = 0;
          renderAttachmentBar();
          closeSlashPopup();
          return;
        }
        const skillName = cmd.startsWith('/') ? cmd.slice(1) : '';
        if (skillName && slashSkills.some(function(skill) { return skill.name === skillName; })) {
          vscode.postMessage({ type: 'invokeSkill', name: skillName, userInput: args });
          input.value = '';
          pendingImages.length = 0;
          pendingTextFiles.length = 0;
          renderAttachmentBar();
          closeSlashPopup();
          return;
        }
      }

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
    if (stopBtn) {
      stopBtn.addEventListener('click', function() {
        if (isBusy) vscode.postMessage({ type: 'interrupt' });
      });
    }
    input.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
    });

    // ── Mode menu ────────────────────────────────────────────────

    let currentMode = 'agent';

    function updateModeButton(mode) {
      currentMode = mode;
      if (modeLabel) modeLabel.textContent = mode === 'plan' ? 'Plan' : 'Agent';
      updateComposerPlaceholder();
      if (modeMenu) modeMenu.querySelectorAll('.mode-item').forEach(function(el) {
        el.classList.toggle('active', el.getAttribute('data-mode') === mode);
      });
    }

    function positionModeMenu() {
      if (!modeMenu || !modeBtn) return;
      modeMenu.classList.remove('hidden');
      modeMenu.style.visibility = 'hidden';
      modeMenu.style.left = '0px';
      modeMenu.style.top = '0px';
      const btnRect = modeBtn.getBoundingClientRect();
      const menuRect = modeMenu.getBoundingClientRect();
      const margin = 8;
      const gap = 4;
      const left = Math.min(Math.max(btnRect.left, margin), window.innerWidth - menuRect.width - margin);
      const top = Math.max(margin, btnRect.top - menuRect.height - gap);
      modeMenu.style.left = left + 'px';
      modeMenu.style.top = top + 'px';
      modeMenu.style.visibility = '';
    }

    function openModeMenu() {
      positionModeMenu();
      if (modeBtn) modeBtn.setAttribute('aria-expanded', 'true');
    }

    function closeModeMenu() {
      if (modeMenu) modeMenu.classList.add('hidden');
      if (modeBtn) modeBtn.setAttribute('aria-expanded', 'false');
    }

    modeBtn && modeBtn.addEventListener('click', function(e) {
      e.stopPropagation();
      if (modeMenu.classList.contains('hidden')) openModeMenu();
      else closeModeMenu();
    });

    modeMenu && modeMenu.querySelectorAll('.mode-item').forEach(function(el) {
      el.addEventListener('click', function() {
        const mode = el.getAttribute('data-mode');
        updateModeButton(mode);
        vscode.postMessage({ type: 'switchMode', mode: mode });
        closeModeMenu();
      });
    });

    document.addEventListener('click', function() { closeModeMenu(); });
    modeMenu && modeMenu.addEventListener('click', function(e) { e.stopPropagation(); });

    // ── Permission menu ──────────────────────────────────────────

    function positionPermMenu() {
      if (!permMenu || !permBtn) return;
      permMenu.classList.remove('hidden');
      permMenu.style.visibility = 'hidden';
      permMenu.style.left = '0px';
      permMenu.style.top = '0px';
      const btnRect = permBtn.getBoundingClientRect();
      const menuRect = permMenu.getBoundingClientRect();
      const margin = 8;
      const gap = 4;
      const left = Math.min(Math.max(btnRect.right - menuRect.width, margin), window.innerWidth - menuRect.width - margin);
      const top = Math.max(margin, btnRect.top - menuRect.height - gap);
      permMenu.style.left = left + 'px';
      permMenu.style.top = top + 'px';
      permMenu.style.visibility = '';
    }

    function openPermMenu() {
      positionPermMenu();
      closeModeMenu();
      if (permBtn) permBtn.setAttribute('aria-expanded', 'true');
    }

    function closePermMenu() {
      if (permMenu) permMenu.classList.add('hidden');
      if (permBtn) permBtn.setAttribute('aria-expanded', 'false');
    }

    permBtn && permBtn.addEventListener('click', function(e) {
      e.stopPropagation();
      if (permMenu.classList.contains('hidden')) openPermMenu();
      else closePermMenu();
    });

    permMenu && permMenu.querySelectorAll('.perm-item').forEach(function(el) {
      el.addEventListener('click', function() {
        const perm = el.getAttribute('data-perm');
        if (permLabel) permLabel.textContent = perm === 'run_everything' ? '完全访问' : (perm === 'ai_review' ? 'AI 审查' : (perm === 'ask' ? '每次确认' : '工作区默认规则'));
        if (permIcon) permIcon.textContent = perm === 'run_everything' ? '⚡' : (perm === 'ai_review' ? '🤖' : (perm === 'ask' ? '🛡' : '⚙'));
        if (permBtn) permBtn.classList.toggle('perm-danger', perm === 'run_everything');
        permMenu.querySelectorAll('.perm-item').forEach(function(item) {
          item.classList.toggle('active', item.getAttribute('data-perm') === perm);
        });
        vscode.postMessage({ type: 'setPermissionMode', mode: perm === 'run_everything' ? 'run_everything' : (perm === 'ai_review' ? 'ai_review' : (perm === 'ask' ? 'ask' : 'default')) });
        closePermMenu();
      });
    });

    document.addEventListener('click', function() { closePermMenu(); });
    permMenu && permMenu.addEventListener('click', function(e) { e.stopPropagation(); });

    // ── Model menu ────────────────────────────────────────────────

    function renderModelMenuItems(models, selected, filter) {
      if (!modelMenuList) return;
      const q = filter.toLowerCase();
      modelMenuList.innerHTML = '';
      const groupedModels = {};
      const groupOrder = [];
      models.forEach(function(m) {
        if (q && m.toLowerCase().indexOf(q) < 0) return;
        const group = currentModelGroups[m] || 'default';
        if (!groupedModels[group]) {
          groupedModels[group] = [];
          groupOrder.push(group);
        }
        groupedModels[group].push(m);
      });
      groupOrder.forEach(function(group) {
        const heading = document.createElement('div');
        heading.className = 'model-menu-group';
        heading.textContent = group;
        modelMenuList.appendChild(heading);
        groupedModels[group].forEach(function(m) {
          const el = document.createElement('div');
          el.className = 'model-menu-item' + (m === selected ? ' active' : '');
          el.setAttribute('role', 'option');
          el.setAttribute('data-model', m);
          el.innerHTML =
            '<span class="model-check">✓</span>' +
            '<span class="model-name">' + m.replace(/&/g,'&amp;').replace(/</g,'&lt;') + '</span>';
          el.addEventListener('click', function() {
            selectModel(m);
            closeModelMenu();
          });
          modelMenuList.appendChild(el);
        });
      });
    }

    function selectModel(m) {
      currentModelValue = m;
      if (modelSelectLabel) { modelSelectLabel.textContent = m; modelSelectLabel.title = m; }
      if (modelSelectWrap) modelSelectWrap.classList.toggle('is-empty', !m);
      renderModelMenuItems(currentModelList, m, modelSearch ? modelSearch.value : '');
      vscode.postMessage({ type: 'setModel', name: m });
    }

    function positionModelMenu() {
      if (!modelMenu || !modelBtn) return;
      modelMenu.classList.remove('hidden');
      modelMenu.style.visibility = 'hidden';
      modelMenu.style.left = '0px';
      modelMenu.style.top = '0px';
      const btnRect = modelBtn.getBoundingClientRect();
      const menuRect = modelMenu.getBoundingClientRect();
      const margin = 8;
      const gap = 4;
      const left = Math.min(Math.max(btnRect.left, margin), window.innerWidth - menuRect.width - margin);
      const top = Math.max(margin, btnRect.top - menuRect.height - gap);
      modelMenu.style.left = left + 'px';
      modelMenu.style.top = top + 'px';
      modelMenu.style.visibility = '';
    }

    function openModelMenu() {
      renderModelMenuItems(currentModelList, currentModelValue, '');
      if (modelSearch) modelSearch.value = '';
      positionModelMenu();
      closePermMenu();
      closeModeMenu();
      if (modelBtn) modelBtn.setAttribute('aria-expanded', 'true');
      if (modelSearch) setTimeout(function() { modelSearch.focus(); }, 0);
    }

    function closeModelMenu() {
      if (modelMenu) modelMenu.classList.add('hidden');
      if (modelBtn) modelBtn.setAttribute('aria-expanded', 'false');
    }

    modelBtn && modelBtn.addEventListener('click', function(e) {
      e.stopPropagation();
      if (modelMenu.classList.contains('hidden')) openModelMenu();
      else closeModelMenu();
    });

    modelSearch && modelSearch.addEventListener('input', function() {
      renderModelMenuItems(currentModelList, currentModelValue, modelSearch.value);
    });

    modelSearch && modelSearch.addEventListener('keydown', function(e) {
      if (e.key === 'Escape') { closeModelMenu(); modelBtn && modelBtn.focus(); }
    });

    document.addEventListener('click', function() { closeModelMenu(); });
    modelMenu && modelMenu.addEventListener('click', function(e) { e.stopPropagation(); });

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
          planCards.clear();
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
        case 'skills':
          if (Array.isArray(msg.skills)) {
            slashSkills = msg.skills;
          }
          break;
        case 'modeChange':
          updateModeButton(msg.mode);
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
        case 'planReady':
          renderPlanCard(msg.card);
          updateBusyLabel();
          updateAllTurnSummaries();
          break;
        case 'planResolved': {
          const card = msg.card;
          planCards.set(card.id, card);
          const el = document.getElementById('plan-' + card.id);
          if (el) updatePlanCard(el, card);
          else renderPlanCard(card);
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
        case 'steeringQueue':
          renderSteeringQueue(msg.messages);
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
        case 'sessionList':
          renderSessionList(msg.sessions);
          renderHistoryList(msg.sessions, historySearchQuery);
          break;
        case 'sessionInfo':
          if (msg.sessionId) currentSessionId = msg.sessionId;
          if (msg.title) setSessionTitle(msg.title);
          if (msg.status && currentSessionId) {
            const s = allSessions.find(function(x) { return x.session_id === currentSessionId; });
            if (s) { s.status = msg.status; renderSessionList(allSessions); renderHistoryList(allSessions, historySearchQuery); }
          }
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
    fetchSkills();
    let optionsRetryCount = 0;
    const optionsRetryTimer = setInterval(() => {
      if (hasReceivedOptions && currentModelList.length > 0) {
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
    } catch (outerInitError) {
      console.error('[CrabCode webview init error]', outerInitError);
      try {
        vscode.postMessage({
          type: 'webviewError',
          message: outerInitError instanceof Error ? outerInitError.message : String(outerInitError),
          stack: outerInitError instanceof Error ? outerInitError.stack : undefined,
        });
      } catch (_) { /* vscode may not be available if acquireVsCodeApi failed */ }
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
