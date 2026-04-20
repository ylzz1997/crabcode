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
  serializeCommand,
  type WsCommand,
} from "./client/protocol";

import type {
  EventPayload,
  ServerConnectedPayload,
  ServerHeartbeatPayload,
  SessionInfo,
  ContextPushRequest,
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

  constructor(private config: vscode.WorkspaceConfiguration) {}

  // ── Public state ───────────────────────────────────────────────

  get connected(): boolean {
    return this._connected;
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
    const url = this.config.get<string>("serverUrl", "ws://localhost:8765");
    const password = this.config.get<string>("password", "");

    try {
      const headers: Record<string, string> = {};
      if (password) {
        headers["Authorization"] = `Bearer ${password}`;
      }

      this.ws = new WebSocket(url, { headers });

      this.ws.on("open", () => {
        this._connected = true;
        this.fire("connected", {} as any);
        this.scheduleReconnect(0); // reset backoff on successful connect
      });

      this.ws.on("message", (data: WebSocket.Data) => {
        try {
          const payload: EventPayload = JSON.parse(data.toString());
          // Track model name from server events
          this.handleServerPayload(payload);
          this.fire("message", payload);
        } catch {
          // Ignore malformed messages
        }
      });

      this.ws.on("close", () => {
        this._connected = false;
        this.fire("disconnected", undefined);
        this.scheduleReconnect(3000);
      });

      this.ws.on("error", () => {
        this._connected = false;
        this.fire("disconnected", undefined);
      });
    } catch {
      this.scheduleReconnect(3000);
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

  send(text: string, options?: { maxTurns?: number; sessionId?: string }): void {
    const cmd = buildSendMessageCommand(text, {
      maxTurns: options?.maxTurns,
      sessionId: options?.sessionId ?? this._sessionId ?? undefined,
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

  sendInterrupt(): void {
    if (!this._sessionId) {
      return;
    }
    this.sendRaw(JSON.stringify({
      type: "interrupt",
      session_id: this._sessionId,
    }));
  }

  sendNewSession(cwd: string | null): void {
    this.sendRaw(JSON.stringify({
      type: "new_session",
      cwd,
    }));
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
    this.sendRaw(serializeCommand(cmd));
  }

  sendRaw(data: string): void {
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
          this._sessionId = payload.properties.session_id as string;
        }
        break;
      case "server.heartbeat":
        if (payload.properties?.model) {
          this._modelName = payload.properties.model as string;
        }
        if (payload.properties?.session_id) {
          this._sessionId = payload.properties.session_id as string;
        }
        break;
      case "turn_complete":
        // Model name may be included in usage
        break;
    }
  }

  private scheduleReconnect(delayMs: number): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
    }
    if (this.disposed) {
      return;
    }
    this.reconnectTimer = setTimeout(() => {
      this.connect();
    }, delayMs);
  }
}
