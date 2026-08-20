import { authenticateConnection, normalizeBaseUrl } from "./native";
import type {
  CheckpointInfo,
  BackgroundTaskInfo,
  ConnectionPreset,
  GatewayEvent,
  GatewayModel,
  GoalState,
  ReasoningEffort,
  ScheduleJobInfo,
  SessionInfo,
  SessionStatus,
  SkillInfo,
  ToolInfo,
  WorkspaceDirectoryListing,
  WorkspaceInfo,
} from "./types";

export class GatewayApi {
  private token: string | null = null;
  private expiresAt = 0;
  private authenticated = false;
  private authPromise: Promise<void> | null = null;
  readonly baseUrl: string;

  constructor(public connection: ConnectionPreset) {
    this.baseUrl = normalizeBaseUrl(connection.base_url);
  }

  get accessToken(): string | null {
    return this.token;
  }

  get tokenExpiresAt(): number {
    return this.expiresAt;
  }

  refreshAuthentication(): Promise<void> {
    return this.authenticate(true);
  }

  async authenticate(force = false): Promise<void> {
    if (
      !force
      && this.authenticated
      && (this.expiresAt === 0 || this.expiresAt - Date.now() > 60_000)
    ) return;
    if (this.authPromise) return this.authPromise;
    this.authPromise = (async () => {
      const result = await authenticateConnection(
        this.connection.base_url,
        this.connection.credential_ref,
      );
      this.token = result.access_token;
      this.authenticated = true;
      this.expiresAt = result.expires_in > 0
        ? Date.now() + result.expires_in * 1000
        : 0;
    })().finally(() => {
      this.authPromise = null;
    });
    return this.authPromise;
  }

  async request<T>(path: string, init: RequestInit = {}, retry = true): Promise<T> {
    await this.authenticate();
    const headers = new Headers(init.headers);
    if (this.token) headers.set("Authorization", `Bearer ${this.token}`);
    if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    const response = await fetch(new URL(path.replace(/^\//, ""), this.baseUrl), {
      ...init,
      headers,
    });
    if (response.status === 401 && retry) {
      await this.authenticate(true);
      return this.request<T>(path, init, false);
    }
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const payload = await response.json() as { detail?: string };
        detail = payload.detail || detail;
      } catch {
        // Keep the status text when the response is not JSON.
      }
      throw new Error(detail);
    }
    if (response.status === 204) return undefined as T;
    return response.json() as Promise<T>;
  }

  workspaceInfo(): Promise<WorkspaceInfo> {
    return this.request("/workspace/info");
  }

  directories(path: string, includeHidden = false): Promise<WorkspaceDirectoryListing> {
    const query = new URLSearchParams({ path, include_hidden: String(includeHidden) });
    return this.request(`/workspace/directories?${query}`);
  }

  sessions(cwd: string): Promise<SessionInfo[]> {
    return this.request(`/session/list?${new URLSearchParams({ cwd })}`);
  }

  recentSessions(): Promise<SessionInfo[]> {
    return this.request("/session/recent?limit=100");
  }

  models(sessionId?: string): Promise<GatewayModel[]> {
    const query = sessionId ? `?${new URLSearchParams({ session_id: sessionId })}` : "";
    return this.request(`/config/models${query}`);
  }

  goal(sessionId: string): Promise<GoalState> {
    return this.request(`/config/goal?${new URLSearchParams({ session_id: sessionId })}`);
  }

  manageGoal(
    sessionId: string,
    action: "set" | "edit" | "clear",
    objective?: string,
  ): Promise<GoalState> {
    return this.request("/config/goal", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, action, objective }),
    });
  }

  skills(sessionId?: string, cwd?: string): Promise<SkillInfo[]> {
    const query = new URLSearchParams();
    if (sessionId) query.set("session_id", sessionId);
    if (cwd) query.set("cwd", cwd);
    const suffix = query.size ? `?${query}` : "";
    return this.request(`/skills${suffix}`);
  }

  tools(sessionId?: string, cwd?: string): Promise<ToolInfo[]> {
    const query = new URLSearchParams();
    if (sessionId) query.set("session_id", sessionId);
    if (cwd) query.set("cwd", cwd);
    const suffix = query.size ? `?${query}` : "";
    return this.request(`/tools${suffix}`);
  }

  schedules(globalScope = false): Promise<ScheduleJobInfo[]> {
    return this.request(globalScope ? "/schedule/list?scope=global" : "/schedule/list");
  }

  backgroundTasks(
    globalScope = false,
    status?: string,
    sessionId?: string,
  ): Promise<BackgroundTaskInfo[]> {
    const query = new URLSearchParams();
    if (globalScope) query.set("scope", "global");
    if (status) query.set("status", status);
    if (sessionId) query.set("session_id", sessionId);
    const suffix = query.size ? `?${query}` : "";
    return this.request(`/tasks${suffix}`);
  }

  pauseSchedule(jobId: string): Promise<ScheduleJobInfo> {
    return this.scheduleAction<ScheduleJobInfo>("pause", jobId);
  }

  resumeSchedule(jobId: string): Promise<ScheduleJobInfo> {
    return this.scheduleAction<ScheduleJobInfo>("resume", jobId);
  }

  triggerSchedule(jobId: string): Promise<{ job_id: string; started: boolean }> {
    return this.scheduleAction("trigger", jobId);
  }

  cancelSchedule(jobId: string): Promise<{ job_id: string; cancelled: boolean }> {
    return this.scheduleAction("cancel", jobId);
  }

  private scheduleAction<T>(action: string, jobId: string): Promise<T> {
    return this.request(`/schedule/${action}`, {
      method: "POST",
      body: JSON.stringify({ job_id: jobId, scope: "global" }),
    });
  }

  sessionStatus(sessionId: string): Promise<SessionStatus> {
    return this.request(`/session/status?${new URLSearchParams({ session_id: sessionId })}`);
  }

  checkpoints(sessionId: string): Promise<CheckpointInfo[]> {
    return this.request(`/snapshot/list?${new URLSearchParams({ session_id: sessionId })}`);
  }

  revert(sessionId: string, checkpointId: string): Promise<unknown> {
    return this.request("/snapshot/revert", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, checkpoint_id: checkpointId }),
    });
  }

  undo(sessionId: string): Promise<unknown> {
    return this.request("/snapshot/undo", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId }),
    });
  }

  archive(sessionId: string): Promise<unknown> {
    return this.request("/session/archive", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId }),
    });
  }

  webSocketUrl(): string {
    const url = new URL("ws", this.baseUrl);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    if (this.token) url.searchParams.set("auth_token", this.token);
    return url.toString();
  }
}

interface SessionChannelOptions {
  sessionId?: string;
  cwd: string;
  additionalDirectories?: string[];
  modelProfile?: string;
  onEvent: (event: GatewayEvent) => void;
  onReady: (sessionId: string) => void;
  onState: (connected: boolean, error?: string) => void;
}

export class SessionChannel {
  private socket: WebSocket | null = null;
  private reconnectTimer: number | null = null;
  private attempts = 0;
  private disposed = false;
  private initialCommandSent = false;
  sessionId: string | null;

  constructor(private api: GatewayApi, private options: SessionChannelOptions) {
    this.sessionId = options.sessionId ?? null;
  }

  get isDisposed(): boolean {
    return this.disposed;
  }

  async connect(): Promise<void> {
    if (this.disposed) return;
    try {
      await this.api.authenticate();
      // The channel may have been disposed while authentication was in flight
      // (for example when the component is refreshed or the session is deleted).
      if (this.disposed) return;
      const socket = new WebSocket(this.api.webSocketUrl());
      this.socket = socket;
      socket.addEventListener("open", () => {
        if (this.disposed || this.socket !== socket) {
          socket.close();
          return;
        }
        this.attempts = 0;
        this.options.onState(true);
        this.sendInitialCommand();
      });
      socket.addEventListener("message", (message) => {
        if (this.disposed || this.socket !== socket) return;
        try {
          const event = JSON.parse(String(message.data)) as GatewayEvent;
          const announced = event.type === "server.connected"
            ? event.properties?.session_id
            : undefined;
          if (typeof announced === "string") {
            this.sessionId = announced;
            this.options.onReady(announced);
          }
          if (event.session_id && this.sessionId && event.session_id !== this.sessionId) return;
          this.options.onEvent(event);
        } catch {
          this.options.onState(false, "Gateway 返回了无效事件");
        }
      });
      socket.addEventListener("close", (event) => {
        if (this.socket !== socket) return;
        this.socket = null;
        if (this.disposed) return;
        this.options.onState(
          false,
          event.code === 1008 ? "Gateway 认证已失效，正在重新认证" : undefined,
        );
        if (event.code === 1008) {
          void this.api.refreshAuthentication()
            .catch((error) => {
              if (this.disposed) return;
              this.options.onState(false, error instanceof Error ? error.message : String(error));
            })
            .finally(() => this.scheduleReconnect());
          return;
        }
        this.scheduleReconnect();
      });
      socket.addEventListener("error", () => {
        if (this.disposed || this.socket !== socket) return;
        this.options.onState(false, "WebSocket 连接失败");
      });
    } catch (error) {
      if (this.disposed) return;
      this.options.onState(false, error instanceof Error ? error.message : String(error));
      this.scheduleReconnect();
    }
  }

  private sendInitialCommand(): void {
    if (this.initialCommandSent) return;
    this.initialCommandSent = true;
    if (this.sessionId) {
      this.sendRaw({
        type: "resume_session",
        session_id: this.sessionId,
        additional_directories: this.options.additionalDirectories ?? [],
      });
    } else {
      this.sendRaw({
        type: "new_session",
        cwd: this.options.cwd,
        additional_directories: this.options.additionalDirectories ?? [],
        model_profile: this.options.modelProfile,
      });
    }
  }

  private scheduleReconnect(): void {
    if (this.disposed || this.reconnectTimer !== null) return;
    this.initialCommandSent = false;
    const delay = Math.min(30_000, 1000 * 2 ** Math.min(this.attempts++, 5));
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      void this.connect();
    }, delay);
  }

  sendMessage(text: string, images: Array<{ media_type: string; data: string }> = []): string {
    const operationId = crypto.randomUUID();
    this.sendRaw({
      type: "send_message",
      text,
      images,
      max_turns: 0,
      session_id: this.sessionId,
      operation_id: operationId,
    });
    return operationId;
  }

  steer(
    text: string,
    operationId: string,
    images: Array<{ media_type: string; data: string }> = [],
  ): void {
    this.sendRaw({
      type: "steer_message",
      text,
      images,
      session_id: this.sessionId,
      operation_id: operationId,
    });
  }

  interrupt(operationId?: string | null): void {
    this.sendRaw({
      type: "interrupt",
      session_id: this.sessionId,
      operation_id: operationId ?? null,
    });
  }

  permission(
    toolUseId: string,
    allowed: boolean,
    alwaysAllow = false,
    feedback?: string,
    agentId: string | null = null,
  ): void {
    this.sendRaw({
      type: "permission_response",
      tool_use_id: toolUseId,
      allowed,
      always_allow: alwaysAllow,
      feedback: feedback ?? null,
      agent_id: agentId,
      session_id: this.sessionId,
    });
  }

  choice(
    toolUseId: string,
    selected: string[],
    cancelled = false,
    agentId: string | null = null,
  ): void {
    this.sendRaw({
      type: "choice_response",
      tool_use_id: toolUseId,
      selected,
      cancelled,
      agent_id: agentId,
      session_id: this.sessionId,
    });
  }

  switchModel(name: string): void {
    this.sendRaw({ type: "switch_model", name, session_id: this.sessionId });
  }

  switchMode(mode: "agent" | "plan"): void {
    this.sendRaw({ type: "switch_mode", mode, session_id: this.sessionId });
  }

  setReasoningEffort(effort: ReasoningEffort): void {
    this.sendRaw({ type: "set_reasoning_effort", effort, session_id: this.sessionId });
  }

  setUltraMode(enabled: boolean): void {
    this.sendRaw({ type: "set_ultra_mode", enabled, session_id: this.sessionId });
  }

  setPermissionMode(mode: string): void {
    this.sendRaw({ type: "set_permission_mode", mode, session_id: this.sessionId });
  }

  planAction(action: "execute" | "revise" | "cancel", plan?: Record<string, unknown>): void {
    this.sendRaw({
      type: "plan_action",
      action,
      plan,
      session_id: this.sessionId,
      operation_id: action === "execute" ? crypto.randomUUID() : undefined,
    });
  }

  private sendRaw(value: Record<string, unknown>): void {
    if (this.socket?.readyState !== WebSocket.OPEN) {
      throw new Error("会话连接尚未就绪");
    }
    this.socket.send(JSON.stringify(value));
  }

  dispose(): void {
    this.disposed = true;
    if (this.reconnectTimer !== null) window.clearTimeout(this.reconnectTimer);
    this.socket?.close();
    this.socket = null;
  }
}
