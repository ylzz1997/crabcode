/**
 * WebSocket connection manager for the CrabCode Gateway.
 *
 * Wraps a single WebSocket connection with reconnect logic, event
 * emission, and typed command sending. Consumes the protocol helpers
 * from the shared `client` package.
 */

import WebSocket from "ws";
import * as vscode from "vscode";

import {
  buildSendMessageCommand,
  buildPushContextCommand,
  buildSwitchModelCommand,
  buildSetPermissionModeCommand,
  buildPlanActionCommand,
  serializeCommand,
  type PermissionMode,
  type WsCommand,
} from "./client/protocol";

import type {
  EventPayload,
  ServerConnectedPayload,
  ServerHeartbeatPayload,
  SessionInfo,
  ContextPushRequest,
  ImageAttachment,
} from "./client/types";

// ── Events emitted by the connection ──────────────────────────────

export interface ConnectionEvents {
  connected: ServerConnectedPayload["properties"];
  disconnected: void;
  message: EventPayload;
}

export type ConnectionEventName = keyof ConnectionEvents;

type Listener<E extends ConnectionEventName> = E extends "disconnected"
  ? () => void
  : (payload: ConnectionEvents[E]) => void;

// ── Connection class ──────────────────────────────────────────────

export class CrabCodeConnection implements vscode.Disposable {
  private ws: WebSocket | null = null;
  private listeners = new Map<string, Set<Listener<any>>>();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private _sessionId: string | null = null;
  private _modelName: string | null = null;
  private _connected = false;
  private disposed = false;
  private reconnectAttempts = 0;
  private pendingSessionCommands: string[] = [];
  private sessionRequestPending = false;
  private static readonly MAX_RECONNECT_ATTEMPTS = 10;
  private static readonly BASE_RECONNECT_DELAY = 1000; // 1s
  private static readonly MAX_RECONNECT_DELAY = 30000; // 30s
  private static readonly RAW_LOG_MAX_CHARS = 8000;

  constructor(
    private config: vscode.WorkspaceConfiguration,
    private outputChannel?: vscode.OutputChannel,
  ) {}

  // ── Public state ───────────────────────────────────────────────

  get connected(): boolean {
    return this._connected || this.ws?.readyState === WebSocket.OPEN;
  }

  get sessionId(): string | null {
    return this._sessionId;
  }

  get modelName(): string | null {
    return this._modelName;
  }

  // ── Lifecycle ──────────────────────────────────────────────────

  connect(): void {
    if (this.disposed) {
      return;
    }
    // Close any existing connection before creating a new one
    if (this.ws) {
      this.ws.removeAllListeners();
      this.ws.close();
      this.ws = null;
    }

    const url = this.config.get<string>("serverUrl", "ws://localhost:4096/ws");
    const password = this.config.get<string>("password", "");

    try {
      const headers: Record<string, string> = {};
      if (password) {
        headers["Authorization"] = `Bearer ${password}`;
      }

      this.ws = new WebSocket(url, { headers });

      this.ws.on("open", () => {
        if (this.reconnectTimer) {
          clearTimeout(this.reconnectTimer);
          this.reconnectTimer = null;
        }
        this._connected = true;
        this._sessionId = null;
        this.sessionRequestPending = false;
        this.reconnectAttempts = 0; // reset backoff on successful connect
        this.log(`ws open url=${url}`);
        this.fire("connected", {} as any);
      });

      this.ws.on("message", (data: WebSocket.Data) => {
        const rawText = this.coerceWebSocketDataToText(data);
        this.logRawPayload("recv", rawText);
        try {
          const payload: EventPayload = JSON.parse(rawText);
          this.log(`ws recv type=${payload.type ?? "unknown"}`);
          // Track model name from server events
          this.handleServerPayload(payload);
          this.fire("message", payload);
        } catch {
          this.log("ws recv invalid JSON");
          // Ignore malformed messages
        }
      });

      this.ws.on("close", (code: number, reason: Buffer) => {
        this._connected = false;
        this._sessionId = null;
        this.sessionRequestPending = false;
        this.log(`ws close code=${code} reason=${reason.toString() || "(empty)"}`);
        this.fire("disconnected", undefined);
        // Code 4001 = server rejected (no session), don't reconnect
        if (code === 4001) {
          return;
        }
        this.scheduleReconnectWithBackoff();
      });

      this.ws.on("error", (err: Error) => {
        const state = this.ws?.readyState;
        this.log(`ws error state=${state ?? "unknown"} message=${err.message}`);
        // Let the close event drive the actual disconnected transition.
        // Some transient errors can be emitted while the socket is still open.
        if (state === WebSocket.CONNECTING) {
          this._connected = false;
        }
      });
    } catch {
      this.log("ws connect threw before open");
      this.scheduleReconnectWithBackoff();
    }
  }

  dispose(): void {
    this.disposed = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
    }
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.listeners.clear();
  }

  // ── Sending commands ───────────────────────────────────────────

  send(text: string, options?: { maxTurns?: number; sessionId?: string; images?: ImageAttachment[] }): void {
    this.log(
      `send_message requested session=${options?.sessionId ?? this._sessionId ?? "(none)"} ` +
      `images=${options?.images?.length ?? 0} chars=${text.length}`,
    );
    const cmd = buildSendMessageCommand(text, {
      maxTurns: options?.maxTurns,
      sessionId: options?.sessionId ?? this._sessionId ?? undefined,
      images: options?.images,
    });
    this.sendCommand(cmd);
  }

  pushContext(context: Omit<ContextPushRequest, "session_id">): void {
    const full: ContextPushRequest = {
      session_id: this._sessionId ?? "",
      ...context,
    };
    const cmd = buildPushContextCommand(full);
    this.sendCommand(cmd);
  }

  sendSwitchModel(name: string): void {
    this._modelName = name;
    const cmd = buildSwitchModelCommand(name);
    this.sendCommand(cmd);
  }

  sendSetPermissionMode(mode: PermissionMode): void {
    const cmd = buildSetPermissionModeCommand(mode);
    this.sendCommand(cmd);
  }

  sendPlanAction(action: "execute" | "revise" | "cancel", plan?: Record<string, unknown>): void {
    const cmd = buildPlanActionCommand(action, plan);
    this.sendCommand(cmd);
  }

  sendInterrupt(): "sent" | "no_session" | "disconnected" {
    if (!this._sessionId) {
      this.log("interrupt skipped: no session");
      return "no_session";
    }
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      this.log("interrupt skipped: ws not open");
      return "disconnected";
    }
    this.log(`interrupt session=${this._sessionId}`);
    this.sendRaw(JSON.stringify({
      type: "interrupt",
      session_id: this._sessionId,
    }));
    return "sent";
  }

  sendNewSession(cwd: string | null): void {
    this.sessionRequestPending = true;
    this.log(`new_session requested cwd=${cwd ?? "(none)"}`);
    this.sendRaw(JSON.stringify({
      type: "new_session",
      cwd,
    }));
  }

  ensureSession(cwd: string | null): void {
    if (!this._connected) {
      this.log("ensureSession skipped: ws not connected");
      return;
    }
    if (this._sessionId) {
      this.log(`ensureSession skipped: session already ready session=${this._sessionId}`);
      return;
    }
    if (this.sessionRequestPending) {
      this.log("ensureSession skipped: request already pending");
      return;
    }
    this.sendNewSession(cwd);
  }

  // ── Event subscription ─────────────────────────────────────────

  on<E extends ConnectionEventName>(event: E, listener: Listener<E>): vscode.Disposable {
    if (!this.listeners.has(event)) {
      this.listeners.set(event, new Set());
    }
    this.listeners.get(event)!.add(listener);
    return {
      dispose: () => {
        this.listeners.get(event)?.delete(listener);
      },
    };
  }

  // ── Internals ──────────────────────────────────────────────────

  private sendCommand(cmd: WsCommand): void {
    const payload = serializeCommand(cmd);
    if (this.requiresActiveSession(cmd) && !this._sessionId) {
      this.pendingSessionCommands.push(payload);
      this.log(`queued ${cmd.type} until session ready queue=${this.pendingSessionCommands.length}`);
      return;
    }
    this.log(`send ${cmd.type} session=${this._sessionId ?? "(none)"}`);
    this.sendRaw(payload);
  }

  sendRaw(data: string): void {
    this.logRawPayload("send", data);
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(data);
    }
  }

  private fire<E extends ConnectionEventName>(
    event: E,
    payload: ConnectionEvents[E] | undefined,
  ): void {
    const set = this.listeners.get(event);
    if (!set) {
      return;
    }
    for (const fn of set) {
      try {
        fn(payload);
      } catch {
        // Swallow listener errors
      }
    }
  }

  private handleServerPayload(payload: EventPayload): void {
    switch (payload.type) {
      case "server.connected":
        this._modelName = (payload.properties?.model as string) ?? null;
        if (payload.properties?.session_id) {
          this.sessionRequestPending = false;
          this._sessionId = payload.properties.session_id as string;
          this.log(`session ready session=${this._sessionId}`);
          this.flushPendingSessionCommands();
        }
        break;
      case "server.heartbeat":
        if (payload.properties?.model) {
          this._modelName = payload.properties.model as string;
        }
        if (payload.properties?.session_id) {
          this.sessionRequestPending = false;
          this._sessionId = payload.properties.session_id as string;
          this.log(`heartbeat session=${this._sessionId}`);
          this.flushPendingSessionCommands();
        }
        break;
      case "turn_complete":
        // Model name may be included in usage
        break;
    }
  }

  private requiresActiveSession(cmd: WsCommand): boolean {
    switch (cmd.type) {
      case "send_message":
      case "push_context":
      case "switch_model":
      case "set_permission_mode":
      case "plan_action":
        return true;
      default:
        return false;
    }
  }

  private flushPendingSessionCommands(): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN || !this._sessionId) {
      return;
    }
    const queued = this.pendingSessionCommands;
    this.pendingSessionCommands = [];
    this.log(`flushing queued commands count=${queued.length} session=${this._sessionId}`);
    for (const payload of queued) {
      this.sendRaw(payload);
    }
  }

  private scheduleReconnectWithBackoff(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
    }
    if (this.disposed) {
      return;
    }
    if (this.reconnectAttempts >= CrabCodeConnection.MAX_RECONNECT_ATTEMPTS) {
      return;
    }
    this.reconnectAttempts++;
    const delay = Math.min(
      CrabCodeConnection.BASE_RECONNECT_DELAY * Math.pow(2, this.reconnectAttempts - 1),
      CrabCodeConnection.MAX_RECONNECT_DELAY,
    );
    this.log(`schedule reconnect attempt=${this.reconnectAttempts} delay_ms=${delay}`);
    this.reconnectTimer = setTimeout(() => {
      this.connect();
    }, delay);
  }

  /** Reset reconnect attempts (call when user explicitly requests reconnect). */
  resetReconnect(): void {
    this.reconnectAttempts = 0;
  }

  private log(message: string): void {
    this.outputChannel?.appendLine(`[CrabCode][WS] ${message}`);
  }

  private logRawPayload(direction: "send" | "recv", raw: string): void {
    if (!this.config.get<boolean>("debugLogRawWsPayload", false)) {
      return;
    }
    const oneLine = raw.replace(/\s+/g, " ").trim();
    const clipped =
      oneLine.length > CrabCodeConnection.RAW_LOG_MAX_CHARS
        ? `${oneLine.slice(0, CrabCodeConnection.RAW_LOG_MAX_CHARS)}…(truncated)`
        : oneLine;
    this.log(`ws ${direction} raw=${clipped}`);
  }

  private coerceWebSocketDataToText(data: WebSocket.Data): string {
    if (typeof data === "string") {
      return data;
    }
    if (data instanceof Buffer) {
      return data.toString("utf-8");
    }
    if (Array.isArray(data)) {
      return Buffer.concat(data).toString("utf-8");
    }
    if (data instanceof ArrayBuffer) {
      return Buffer.from(data).toString("utf-8");
    }
    return "";
  }
}
