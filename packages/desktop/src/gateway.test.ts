/* @vitest-environment jsdom */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { SessionChannel, type GatewayApi } from "./gateway";
import { applyGatewayEvent } from "./events";
import type { GatewayEvent, SessionViewState } from "./types";

class FakeWebSocket {
  static OPEN = 1;
  static instances: FakeWebSocket[] = [];
  readyState = 0;
  sent: string[] = [];
  private listeners = new Map<string, Array<(event: Event) => void>>();

  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }

  addEventListener(type: string, listener: (event: Event) => void) {
    const entries = this.listeners.get(type) ?? [];
    entries.push(listener);
    this.listeners.set(type, entries);
  }

  emit(type: string) {
    if (type === "open") this.readyState = FakeWebSocket.OPEN;
    for (const listener of this.listeners.get(type) ?? []) listener(new Event(type));
  }

  receive(event: GatewayEvent) {
    const message = new MessageEvent("message", { data: JSON.stringify(event) });
    for (const listener of this.listeners.get("message") ?? []) listener(message);
  }

  send(value: string) {
    this.sent.push(value);
  }

  close() {}
}

describe("SessionChannel new-session controls", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });

  it("routes queue and steering commands to the same active operation with attachments", async () => {
    const api = {
      authenticate: vi.fn().mockResolvedValue(undefined), webSocketUrl: () => "ws://localhost/ws",
    } as unknown as GatewayApi;
    const channel = new SessionChannel(api, { cwd: "/work", onEvent: vi.fn(), onReady: vi.fn(), onState: vi.fn() });
    await channel.connect();
    const socket = FakeWebSocket.instances[0];
    socket.emit("open");
    socket.receive({ type: "server.connected", properties: { session_id: "session-a" } });
    const images = [{ media_type: "image/png", data: "image-data" }];
    channel.queueMessage("next task", "op", images, "q1");
    channel.steer("change direction", "op", images);
    expect(socket.sent.map((value) => JSON.parse(value)).slice(-2)).toEqual([
      { type: "queue_message", text: "next task", operation_id: "op", session_id: "session-a", request_id: "q1", images },
      { type: "steer_message", text: "change direction", operation_id: "op", session_id: "session-a", images },
    ]);
    for (const action of ["edit", "remove", "steer"] as const) {
      channel.queuedMessageAction("q1", action, "op");
      expect(JSON.parse(socket.sent.at(-1)!)).toEqual({
        type: "queued_message_action", request_id: "q1", action, operation_id: "op", session_id: "session-a",
      });
    }
    channel.dispose();
  });

  it("sends the remembered composer controls with the initial request", async () => {
    const api = {
      authenticate: vi.fn().mockResolvedValue(undefined),
      webSocketUrl: () => "ws://localhost/ws",
    } as unknown as GatewayApi;
    const channel = new SessionChannel(api, {
      cwd: "/work/crab",
      modelProfile: "smart",
      reasoningEffort: "xhigh",
      ultraMode: true,
      mode: "plan",
      permissionMode: "ai_review",
      onEvent: vi.fn(),
      onReady: vi.fn(),
      onState: vi.fn(),
    });

    await channel.connect();
    const socket = FakeWebSocket.instances[0];
    socket.emit("open");

    expect(JSON.parse(socket.sent[0])).toMatchObject({
      type: "new_session",
      cwd: "/work/crab",
      model_profile: "smart",
      reasoning_effort: "xhigh",
      ultra_mode: true,
      mode: "plan",
      permission_mode: "ai_review",
    });
    channel.dispose();
  });

  it.each([undefined, "session-b"])("isolates a thinking session from a new or resumed channel (%s)", async (sessionId) => {
    const api = {
      authenticate: vi.fn().mockResolvedValue(undefined),
      webSocketUrl: () => "ws://localhost/ws",
    } as unknown as GatewayApi;
    function view(id: string): SessionViewState {
      return {
        id, cwd: "/work/crab", title: id, items: [], loading: false,
        busy: false, connected: false, operationId: null, status: null,
        error: null, runStartedAt: null, currentStep: null, lastTurnUsage: null,
      };
    }
    let first = view("session-a");
    let second = view(sessionId ?? "new-pending");
    const onReady = vi.fn((id: string) => { second = { ...second, id }; });
    const firstChannel = new SessionChannel(api, {
      sessionId: first.id, cwd: first.cwd,
      onEvent: (event) => { first = applyGatewayEvent(first, event); },
      onReady: vi.fn(), onState: vi.fn(),
    });
    const secondChannel = new SessionChannel(api, {
      sessionId, cwd: second.cwd,
      onEvent: (event) => { second = applyGatewayEvent(second, event); },
      onReady, onState: vi.fn(),
    });
    await firstChannel.connect();
    await secondChannel.connect();
    const [firstSocket, secondSocket] = FakeWebSocket.instances;
    firstSocket.emit("open");
    secondSocket.emit("open");
    firstSocket.receive({ type: "server.connected", properties: { session_id: "session-a" } });

    // A new socket can receive the Gateway default session's stream while
    // new_session/resume_session is still in flight.
    const thinking = { type: "thinking", session_id: "session-a", text: "Only A's thought" };
    firstSocket.receive(thinking);
    secondSocket.receive(thinking);
    expect(first.items[0]).toMatchObject({ kind: "thinking", text: thinking.text });
    expect(second.items).toEqual([]);
    expect(second.busy).toBe(false);
    expect(second.currentStep).toBeNull();

    secondSocket.receive({ type: "server.connected", properties: { session_id: "session-b" } });
    secondSocket.receive(thinking);
    secondSocket.receive({ type: "stream_text", session_id: "session-a", text: "A's reply" });
    secondSocket.receive({ type: "server.connected", properties: { session_id: "session-a" } });
    secondSocket.receive({ type: "thinking", session_id: "session-b", text: "Only B's thought" });
    secondSocket.receive({ type: "turn_complete", session_id: "session-a" });
    expect(secondChannel.sessionId).toBe("session-b");
    expect(onReady).toHaveBeenCalledTimes(1);
    expect(second.items).toHaveLength(1);
    expect(second.items[0]).toMatchObject({ kind: "thinking", text: "Only B's thought" });
    expect(second.busy).toBe(true);
    expect(first.items[0].text).toBe(thinking.text);
    firstChannel.dispose();
    secondChannel.dispose();
  });

  it.each([undefined, "missing-session"])("keeps initial command failures visible (%s)", async (sessionId) => {
    const api = {
      authenticate: vi.fn().mockResolvedValue(undefined),
      webSocketUrl: () => "ws://localhost/ws",
    } as unknown as GatewayApi;
    const onEvent = vi.fn();
    const channel = new SessionChannel(api, {
      sessionId, cwd: "/work/crab", onEvent, onReady: vi.fn(), onState: vi.fn(),
    });
    await channel.connect();
    const socket = FakeWebSocket.instances[0];
    socket.emit("open");
    // The Gateway can label a failed initial command with its default session.
    const error = {
      type: "error", command_error: true,
      command: sessionId ? "resume_session" : "new_session",
      session_id: "gateway-default", message: "Cannot open session",
    };
    socket.receive(error);
    expect(onEvent).toHaveBeenCalledWith(error);
    channel.dispose();
  });
});
