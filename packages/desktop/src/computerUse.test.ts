import { DEFAULT_VM_CONFIG } from "./virtualMachine";
/* @vitest-environment jsdom */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { COMPUTER_USE_RELEASE_RETENTION_MS, ComputerUseChannel, computerUseHostId } from "./computerUse";
import type { ComputerUseState } from "./computerUse";
import type { GatewayApi } from "./gateway";

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }));

vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));
vi.mock("./native", () => ({ isDesktopShell: () => true }));

class FakeWebSocket {
  static OPEN = 1;
  static instances: FakeWebSocket[] = [];
  readyState = 0;
  private listeners = new Map<string, Array<(event: { data?: string }) => void>>();
  constructor(public url: string) { FakeWebSocket.instances.push(this); }
  addEventListener(type: string, listener: (event: { data?: string }) => void) {
    const entries = this.listeners.get(type) ?? [];
    entries.push(listener);
    this.listeners.set(type, entries);
  }
  emit(type: string, event: { data?: string } = {}) {
    if (type === "open") this.readyState = FakeWebSocket.OPEN;
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
  send() {}
  close() {}
}

describe("ComputerUseChannel", () => {
  beforeEach(() => {
    invokeMock.mockReset();
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
    window.sessionStorage.clear();
  });

  it("automatically publishes revoked and restored permissions without reconnecting", async () => {
    vi.useFakeTimers();
    const caps = { gui_available: true, input_available: true, capture_available: true,
      ax_available: true, instance_id: "same-executor", platform: "macos", displays: [],
      supported_modes: ["background_app"], reason: null as string | null };
    invokeMock.mockImplementation(async (command: string) => command === "computer_use_capabilities" ? { ...caps } : null);
    const states: ComputerUseState[] = [];
    const channel = new ComputerUseChannel({ authenticate: async () => {}, computerUseWebSocketUrl: () => "ws://localhost/test" } as unknown as GatewayApi,
      "host", true, state => states.push(state));
    try {
      await channel.connect();
      const socket = FakeWebSocket.instances[0];
      const send = vi.spyOn(socket, "send");
      socket.emit("open");
      socket.emit("message", { data: JSON.stringify({ type: "computer_use_host_registered", available: true }) });
      states.length = 0;
      caps.capture_available = false;
      caps.reason = "Screen Recording permission is required";
      await vi.advanceTimersByTimeAsync(30_000);
      expect(states.at(-1)).toMatchObject({ status: "ready", capabilities: { capture_available: false, ax_available: true }, error: caps.reason });
      expect(send).toHaveBeenLastCalledWith(expect.stringContaining('"capture_available":false'));
      expect(states.every(state => state.status !== "connecting")).toBe(true);
      caps.gui_available = caps.input_available = caps.ax_available = false;
      await vi.advanceTimersByTimeAsync(30_000);
      expect(states.at(-1)).toMatchObject({ status: "unavailable", capabilities: { input_available: false, capture_available: false } });
      expect(send).toHaveBeenLastCalledWith(expect.stringContaining('"gui_available":false'));
      caps.gui_available = caps.input_available = caps.ax_available = caps.capture_available = true;
      caps.reason = null;
      await vi.advanceTimersByTimeAsync(30_000);
      expect(states.at(-1)).toMatchObject({ status: "ready", error: null, capabilities: { capture_available: true } });
      const publications = states.length;
      await vi.advanceTimersByTimeAsync(30_000);
      expect(states).toHaveLength(publications);
      channel.setEnabled(false);
      const checks = invokeMock.mock.calls.filter(([command]) => command === "computer_use_capabilities").length;
      await vi.advanceTimersByTimeAsync(60_000);
      expect(invokeMock.mock.calls.filter(([command]) => command === "computer_use_capabilities")).toHaveLength(checks);
    } finally {
      channel.dispose();
      vi.useRealTimers();
    }
  });

  it("retries permission detection after a probe fails without an executor instance", async () => {
    vi.useFakeTimers();
    invokeMock.mockRejectedValueOnce(new Error("permission check timed out"))
      .mockResolvedValue({ gui_available: true, input_available: true, capture_available: true,
        platform: "macos", displays: [], supported_modes: ["background_app"] });
    const publish = vi.fn();
    const channel = new ComputerUseChannel({ authenticate: async () => {}, computerUseWebSocketUrl: () => "ws://localhost/test" } as unknown as GatewayApi,
      "host", true, publish);
    try {
      await channel.connect();
      FakeWebSocket.instances[0].emit("open");
      expect(publish.mock.lastCall?.[0]).toMatchObject({ status: "unavailable", error: "permission check timed out" });
      await vi.advanceTimersByTimeAsync(30_000);
      expect(publish.mock.lastCall?.[0]).toMatchObject({ status: "ready", error: null });
    } finally {
      channel.dispose();
      vi.useRealTimers();
    }
  });

  it("keeps a running action busy when background permission checks and acknowledgements arrive", async () => {
    vi.useFakeTimers();
    let finishAction!: (result: object) => void;
    const caps = { gui_available: true, input_available: true, capture_available: true,
      platform: "macos", displays: [], supported_modes: ["background_app"] };
    invokeMock.mockImplementation((command: string) => command === "computer_use_execute"
      ? new Promise(resolve => { finishAction = resolve; }) : Promise.resolve({ ...caps }));
    const publish = vi.fn();
    const channel = new ComputerUseChannel({ authenticate: async () => {}, computerUseWebSocketUrl: () => "ws://localhost/test" } as unknown as GatewayApi,
      "host", true, publish);
    try {
      await channel.connect();
      const socket = FakeWebSocket.instances[0]; socket.emit("open");
      socket.emit("message", { data: JSON.stringify({ type: "computer_use_request", request_id: "pending",
        session_id: "session", action: { action: "observe" } }) });
      await vi.advanceTimersByTimeAsync(0);
      caps.capture_available = false;
      await vi.advanceTimersByTimeAsync(30_000);
      socket.emit("message", { data: JSON.stringify({ type: "computer_use_host_state_ack", available: true }) });
      expect(publish.mock.lastCall?.[0]).toMatchObject({ status: "busy", capabilities: { capture_available: false } });
      finishAction({ ok: true, action: "observe" });
      await vi.advanceTimersByTimeAsync(0);
      expect(publish.mock.lastCall?.[0].status).toBe("ready");
    } finally {
      channel.dispose();
      vi.useRealTimers();
    }
  });

  it("rotates AX ownership and clears previews when the shared executor restarts", async () => {
    let instance = "before";
    invokeMock.mockImplementation(async (command: string) => command === "computer_use_capabilities"
      ? { gui_available: true, input_available: true, ax_available: true, ax_protocol_version: 1,
        delivery_policy_version: 1, instance_id: instance, platform: "macos", displays: [], supported_modes: ["background_app"] }
      : { ok: true, observation_kind: "ax", accessibility: { elements: [] } });
    const publish = vi.fn();
    const channel = new ComputerUseChannel({ authenticate: async () => {}, computerUseWebSocketUrl: () => "ws://localhost/test" } as unknown as GatewayApi,
      "host", true, publish);
    await channel.connect();
    const socket = FakeWebSocket.instances[0]; socket.emit("open");
    const observe = (id: string) => socket.emit("message", { data: JSON.stringify({ type: "computer_use_request", request_id: id,
      session_id: "session", target_scope: "app_window", delivery_policy: "strict_background", action: { action: "observe", window_id: "7" } }) });
    observe("first");
    await vi.waitFor(() => expect(publish.mock.lastCall?.[0].status).toBe("ready"));
    const before = invokeMock.mock.calls.find(([command]) => command === "computer_use_execute")![1];
    expect(before.instanceId).toBe("before");
    expect(before.lease).toEqual(before.request.owner);
    instance = "after";
    channel.refresh();
    await vi.waitFor(() => expect(publish.mock.lastCall?.[0].capabilities.instance_id).toBe("after"));
    expect(publish.mock.lastCall?.[0].previews).toEqual([]);
    observe("second");
    await vi.waitFor(() => expect(invokeMock.mock.calls.filter(([command]) => command === "computer_use_execute")).toHaveLength(2));
    const after = invokeMock.mock.calls.filter(([command]) => command === "computer_use_execute")[1][1];
    expect(after.instanceId).toBe("after");
    expect(after.request.owner.connection_id).not.toBe(before.request.owner.connection_id);
    channel.dispose();
  });

  it("releases shared host leases on disable even on a non-AX platform", async () => {
    invokeMock.mockResolvedValue({ gui_available: true, input_available: true, instance_id: "windows-host",
      platform: "windows", displays: [], supported_modes: ["background_app"], delivery_policy_version: 1 });
    const channel = new ComputerUseChannel({ authenticate: async () => {}, computerUseWebSocketUrl: () => "ws://localhost/test" } as unknown as GatewayApi,
      "host", true, vi.fn());
    await channel.connect();
    channel.setEnabled(false);
    await vi.waitFor(() => expect(invokeMock).toHaveBeenCalledWith("computer_use_release_ax", {
      hostId: "host", connectionId: expect.any(String), sessionId: null, agentId: null, allAgents: true,
    }));
    channel.dispose();
  });

  it("binds AX references to a host connection and releases them before preview retention expires", async () => {
    const caps = { gui_available: true, input_available: true, capture_available: false,
      ax_available: true, ax_protocol_version: 1, platform: "macos", displays: [],
      supported_modes: ["background_app"], delivery_policy_version: 1 };
    invokeMock.mockResolvedValueOnce(caps).mockResolvedValue({ ok: true, observation_kind: "ax",
      accessibility: { snapshot_id: "s1", elements: [{ element_id: "e1" }] } });
    const publish = vi.fn();
    const channel = new ComputerUseChannel({ authenticate: vi.fn().mockResolvedValue(undefined),
      computerUseWebSocketUrl: () => "ws://localhost/test" } as unknown as GatewayApi, "ax-host", true, publish);
    await channel.connect();
    const socket = FakeWebSocket.instances[0]; socket.emit("open");
    const action = { action: "observe", window_id: "7" };
    socket.emit("message", { data: JSON.stringify({ type: "computer_use_request", request_id: "ax-1",
      session_id: "session", agent_id: "child", target_scope: "app_window", delivery_policy: "strict_background", action }) });
    await vi.waitFor(() => expect(invokeMock).toHaveBeenCalledWith("computer_use_execute", { request: {
      mode: "background_app", target_scope: "app_window", delivery_policy: "strict_background", action,
      owner: { host_id: "ax-host", connection_id: expect.any(String), session_id: "session", agent_id: "child" },
    } }));
    await vi.waitFor(() => expect(publish).toHaveBeenLastCalledWith(expect.objectContaining({ status: "ready",
      previews: [expect.objectContaining({ observationKind: "ax", axElementCount: 1, frame: null })] })));
    const connectionId = invokeMock.mock.calls.find(([name]) => name === "computer_use_execute")![1].request.owner.connection_id;
    socket.emit("message", { data: JSON.stringify({ type: "computer_use_release", session_id: "session", agent_id: "child" }) });
    await vi.waitFor(() => expect(invokeMock).toHaveBeenCalledWith("computer_use_release_ax", {
      hostId: "ax-host", connectionId, sessionId: "session", agentId: "child", allAgents: false,
    }));
    expect((publish.mock.lastCall![0] as ComputerUseState).previews).toHaveLength(1);
    socket.emit("close");
    await vi.waitFor(() => expect(invokeMock).toHaveBeenCalledWith("computer_use_release_ax", {
      hostId: "ax-host", connectionId, sessionId: null, agentId: null, allAgents: true,
    }));
    channel.dispose();
  });

  it("keeps the screenshot timestamp when a later observation contains only AX text", async () => {
    const frame = { data: "AAAA", media_type: "image/png", width: 10, height: 10, origin_x: 0, origin_y: 0, frame_id: "f" };
    invokeMock.mockResolvedValueOnce({ gui_available: true, input_available: true, platform: "macos", displays: [],
      ax_available: true, ax_protocol_version: 1, supported_modes: ["background_app"], delivery_policy_version: 1 })
      .mockResolvedValueOnce({ ok: true, screenshot: frame, observation_kind: "screenshot" })
      .mockResolvedValue({ ok: true, observation_kind: "ax", accessibility: { elements: [] } });
    const publish = vi.fn();
    const channel = new ComputerUseChannel({ authenticate: vi.fn().mockResolvedValue(undefined),
      computerUseWebSocketUrl: () => "ws://localhost/test" } as unknown as GatewayApi, "ax-host", true, publish);
    await channel.connect();
    const socket = FakeWebSocket.instances[0]; socket.emit("open");
    const send = (id: string, observation: string) => socket.emit("message", { data: JSON.stringify({
      type: "computer_use_request", request_id: id, session_id: "session", target_scope: "app_window",
      delivery_policy: "allow_foreground", action: { action: "observe", window_id: "7", observation },
    }) });
    send("image", "screenshot");
    await vi.waitFor(() => expect((publish.mock.lastCall![0] as ComputerUseState).previews[0].frame).toEqual(frame));
    const capturedAt = (publish.mock.lastCall![0] as ComputerUseState).previews[0].frameUpdatedAt;
    send("ax", "ax");
    await vi.waitFor(() => expect((publish.mock.lastCall![0] as ComputerUseState).previews[0].observationKind).toBe("ax"));
    const preview = (publish.mock.lastCall![0] as ComputerUseState).previews[0];
    expect(preview.frame).toEqual(frame);
    expect(preview.frameUpdatedAt).toBe(capturedAt);
    const monitorFrame = { ...frame, frame_id: "monitor-only" };
    invokeMock.mockResolvedValueOnce({ ok: true, observation_kind: "ax", preview_screenshot: monitorFrame,
      accessibility: { elements: [{ element_id: "e1" }] } });
    const sendSpy = vi.spyOn(socket, "send");
    send("ax-with-preview", "ax");
    await vi.waitFor(() => expect((publish.mock.lastCall![0] as ComputerUseState).previews[0].frame).toEqual(monitorFrame));
    expect((publish.mock.lastCall![0] as ComputerUseState).previews[0]).toMatchObject({ observationKind: "ax", axElementCount: 1 });
    // Even an older Gateway sees no monitor data in the forwarded tool result.
    const sent = JSON.parse((sendSpy.mock.lastCall as unknown as [string])[0]);
    expect(sent.preview_screenshot).toEqual(monitorFrame);
    expect(sent.result.preview_screenshot).toBeUndefined();
    expect(sent.result.screenshot).toBeUndefined();
    const monitorCapturedAt = (publish.mock.lastCall![0] as ComputerUseState).previews[0].frameUpdatedAt;
    send("ax-without-capture", "ax");
    await vi.waitFor(() => expect((publish.mock.lastCall![0] as ComputerUseState).previews[0].status).toBe("ready"));
    expect((publish.mock.lastCall![0] as ComputerUseState).previews[0].frame).toEqual(monitorFrame);
    expect((publish.mock.lastCall![0] as ComputerUseState).previews[0].frameUpdatedAt).toBe(monitorCapturedAt);
    channel.dispose();
  });

  it("routes VM input only to the pinned guest and refuses stale instances", async () => {
    const caps = { gui_available: true, input_available: true, platform: "macos", displays: [],
      supported_modes: ["foreground_desktop"], delivery_policy_version: 1, instance_id: "boot-1" };
    invokeMock.mockResolvedValueOnce(caps).mockResolvedValue({ ok: true });
    const publish = vi.fn();
    const channel = new ComputerUseChannel({ authenticate: vi.fn().mockResolvedValue(undefined),
      computerUseWebSocketUrl: () => "ws://localhost/test" } as unknown as GatewayApi,
      "desktop-vm", true, publish, DEFAULT_VM_CONFIG);
    await channel.connect();
    expect(invokeMock).toHaveBeenCalledWith("computer_use_vm_capabilities", { config: DEFAULT_VM_CONFIG });
    const socket = FakeWebSocket.instances[0]; socket.emit("open");
    const action = { action: "click", x: 10, y: 20 };
    const request = { type: "computer_use_request", request_id: "vm-r", session_id: "s",
      target_scope: "desktop", delivery_policy: "allow_foreground", environment_id: "lume:default:crabcode", instance_id: "boot-1", action };
    socket.emit("message", { data: JSON.stringify(request) });
    await vi.waitFor(() => expect(invokeMock).toHaveBeenCalledWith("computer_use_vm_execute", {
      config: DEFAULT_VM_CONFIG, instanceId: "boot-1", owner: "desktop-vm:s:main",
      request: { mode: "foreground_desktop", target_scope: "desktop", delivery_policy: "allow_foreground", action },
    }));
    socket.emit("message", { data: JSON.stringify({ ...request, request_id: "stale", instance_id: "old" }) });
    await vi.waitFor(() => expect(publish).toHaveBeenLastCalledWith(expect.objectContaining({ status: "error" })));
    expect(invokeMock.mock.calls.filter(([name]) => name === "computer_use_vm_execute")).toHaveLength(1);
    expect(invokeMock.mock.calls.some(([name]) => name === "computer_use_execute" || name === "computer_use_capabilities")).toBe(false);
    socket.emit("message", { data: JSON.stringify({ type: "computer_use_release", session_id: "s", all_agents: true }) });
    await vi.waitFor(() => expect(invokeMock).toHaveBeenCalledWith("computer_use_vm_release", {
      config: DEFAULT_VM_CONFIG, instanceId: "boot-1", owner: "desktop-vm:s", allAgents: true,
    }));
    channel.dispose();
  });

  it("forwards independent popup images while keeping the root monitor frame separate", async () => {
    const frame = { data: "cG5n", media_type: "image/png", width: 100, height: 80, origin_x: 20, origin_y: 30, frame_id: "root" };
    const observations = [{ window_id: "9", owner_window_id: null, relationship_uncertain: true,
      screenshot: { ...frame, frame_id: "popup", target: "window:9" } }];
    const caps = { gui_available: true, input_available: true, platform: "macos", displays: [],
      ax_available: true, ax_protocol_version: 1, window_observation_version: 1,
      supported_modes: ["background_app"], delivery_policy_version: 1 };
    invokeMock.mockResolvedValueOnce(caps).mockResolvedValue({ ok: true, observation_kind: "ax_and_screenshot",
      accessibility: { elements: [{ element_id: "e1" }] }, preview_screenshot: frame, window_observations: observations });
    const publish = vi.fn();
    const channel = new ComputerUseChannel({ authenticate: vi.fn().mockResolvedValue(undefined),
      computerUseWebSocketUrl: () => "ws://localhost/test" } as unknown as GatewayApi, "popup-host", true, publish);
    await channel.connect();
    const socket = FakeWebSocket.instances[0];
    const sendSpy = vi.spyOn(socket, "send");
    socket.emit("open");
    expect(JSON.parse((sendSpy.mock.lastCall as unknown as [string])[0]).capabilities.window_observation_version).toBe(1);
    const action = { action: "observe", window_id: "7", include_window_observations: true };
    socket.emit("message", { data: JSON.stringify({ type: "computer_use_request", request_id: "popup",
      session_id: "s", target_scope: "app_window", delivery_policy: "allow_foreground", action }) });
    await vi.waitFor(() => expect((publish.mock.lastCall![0] as ComputerUseState).previews[0].status).toBe("ready"));
    const sent = JSON.parse((sendSpy.mock.lastCall as unknown as [string])[0]);
    expect(sent.result.window_observations).toEqual(observations);
    expect(sent.result.screenshot).toBeUndefined();
    expect(sent.result.preview_screenshot).toBeUndefined();
    expect(sent.preview_screenshot).toEqual(frame);
    expect((publish.mock.lastCall![0] as ComputerUseState).previews[0].frame).toEqual(frame);
    expect(invokeMock.mock.calls.find(([name]) => name === "computer_use_execute")![1].request.action).toEqual(action);
    channel.dispose();
  });

  it("does not probe or fall back to host input when a VM is disconnected", async () => {
    invokeMock.mockRejectedValue(new Error("VM stopped"));
    const publish = vi.fn();
    const channel = new ComputerUseChannel({ authenticate: vi.fn().mockResolvedValue(undefined),
      computerUseWebSocketUrl: () => "ws://localhost/test" } as unknown as GatewayApi, "vm", true, publish, DEFAULT_VM_CONFIG);
    await channel.connect();
    const socket = FakeWebSocket.instances[0]; socket.emit("open");
    socket.emit("message", { data: JSON.stringify({ type: "computer_use_request", request_id: "r",
      target_scope: "desktop", delivery_policy: "allow_foreground", action: { action: "click", x: 10, y: 20 } }) });
    await vi.waitFor(() => expect(publish).toHaveBeenLastCalledWith(expect.objectContaining({ status: "error" })));
    expect(invokeMock.mock.calls.map(([name]) => name)).toEqual(["computer_use_vm_capabilities"]);
    channel.dispose();
  });

  it.each(["strict_background", "allow_foreground"] as const)("forwards session permission outside the action (%s)", async (policy) => {
    invokeMock.mockResolvedValueOnce({
      gui_available: true, input_available: true, platform: "macos", displays: [],
      supported_modes: ["background_app", "foreground_desktop"], delivery_policy_version: 1,
    }).mockResolvedValueOnce({ ok: false, action_dispatched: false, retry_safe: true });
    const channel = new ComputerUseChannel({
      authenticate: vi.fn().mockResolvedValue(undefined), computerUseWebSocketUrl: () => "ws://localhost/test",
    } as unknown as GatewayApi, "h", true, vi.fn());
    await channel.connect();
    const socket = FakeWebSocket.instances[0];
    socket.emit("open");
    const action = { action: "click", window_id: "42", x: 1, y: 2 };
    socket.emit("message", { data: JSON.stringify({
      type: "computer_use_request", request_id: "r", target_scope: "app_window", delivery_policy: policy, action,
    }) });
    await vi.waitFor(() => expect(invokeMock).toHaveBeenCalledWith("computer_use_execute", {
      request: { mode: "background_app", target_scope: "app_window", delivery_policy: policy, action },
    }));
    expect(invokeMock.mock.calls.filter(([name]) => name === "computer_use_execute")).toHaveLength(1);
    channel.dispose();
  });

  it.each([
    { policy: undefined, scope: "app_window" },
    { policy: "automatic", scope: "app_window" },
    { policy: "strict_background", scope: "desktop" },
  ])("rejects legacy input or invalid policy combinations ($scope, $policy)", async ({ policy, scope }) => {
    invokeMock.mockResolvedValueOnce({
      gui_available: true, input_available: true, platform: "macos", displays: [],
      supported_modes: ["background_app", "foreground_desktop"],
      ...(policy ? { delivery_policy_version: 1 } : {}),
    });
    const publish = vi.fn();
    const channel = new ComputerUseChannel({
      authenticate: vi.fn().mockResolvedValue(undefined), computerUseWebSocketUrl: () => "ws://localhost/test",
    } as unknown as GatewayApi, "h", true, publish);
    await channel.connect();
    const socket = FakeWebSocket.instances[0];
    socket.emit("open");
    socket.emit("message", { data: JSON.stringify({ type: "computer_use_request", request_id: "r",
      target_scope: scope, delivery_policy: policy, action: { action: "click", window_id: "42", x: 1, y: 2 },
    }) });
    await vi.waitFor(() => expect(publish).toHaveBeenLastCalledWith(expect.objectContaining({ status: "error" })));
    expect(invokeMock.mock.calls.filter(([name]) => name === "computer_use_execute")).toHaveLength(0);
    channel.dispose();
  });

  it("keeps one host id in the current webview across reloads", () => {
    const first = computerUseHostId();
    const second = computerUseHostId();
    expect(second).toBe(first);
    expect(first).toMatch(/^desktop-/);
  });

  it("does not inspect the GUI while disabled and detects it when enabled", async () => {
    invokeMock.mockResolvedValue({
      gui_available: true,
      input_available: true,
      platform: "macos",
      displays: [],
      supported_modes: ["background_app", "foreground_desktop"],
        delivery_policy_version: 1,
    });
    const api = {
      authenticate: vi.fn().mockResolvedValue(undefined),
      computerUseWebSocketUrl: () => "ws://localhost/computer-use/ws",
    } as unknown as GatewayApi;
    const channel = new ComputerUseChannel(api, "desktop-test", false, vi.fn());

    await channel.connect();
    expect(invokeMock).not.toHaveBeenCalled();

    channel.setEnabled(true);
    await vi.waitFor(() => expect(invokeMock).toHaveBeenCalledWith("computer_use_capabilities"));
    channel.dispose();
  });

  it("forwards the requested mode to the native executor without fallback", async () => {
    invokeMock
      .mockResolvedValueOnce({
        gui_available: true,
        input_available: true,
        platform: "macos",
        displays: [],
        supported_modes: ["background_app", "foreground_desktop"],
        delivery_policy_version: 1,
      })
      .mockResolvedValueOnce({ ok: true, action: "list_windows", windows: [] });
    const api = {
      authenticate: vi.fn().mockResolvedValue(undefined),
      computerUseWebSocketUrl: () => "ws://localhost/computer-use/ws",
    } as unknown as GatewayApi;
    const channel = new ComputerUseChannel(api, "desktop-test", true, vi.fn());

    await channel.connect();
    const socket = FakeWebSocket.instances[0];
    socket.emit("open");
    socket.emit("message", {
      data: JSON.stringify({
        type: "computer_use_request",
        request_id: "request-1",
        mode: "background_app",
        action: { action: "list_windows" },
      }),
    });

    await vi.waitFor(() => expect(invokeMock).toHaveBeenCalledWith("computer_use_execute", {
      request: { mode: "background_app", target_scope: "app_window", delivery_policy: "allow_foreground", action: { action: "list_windows" } },
    }));
    channel.dispose();
  });

  it.each([true, false])("uses dispatch status for an unverified click (dispatched: %s)", async (dispatched) => {
    invokeMock
      .mockResolvedValueOnce({
        gui_available: true,
        input_available: true,
        platform: "macos",
        displays: [],
        supported_modes: ["background_app", "foreground_desktop"],
        delivery_policy_version: 1,
      })
      .mockResolvedValueOnce({
        ok: dispatched,
        action: "click",
        summary: dispatched ? "Click dispatched" : "Click dispatch was not acknowledged",
        dispatch_succeeded: dispatched,
        effect_verified: false,
        verification_warning: "Click effect is unverified; observe again",
        ...(!dispatched ? { error: "Dispatch was not acknowledged" } : {}),
      });
    const states: ComputerUseState[] = [];
    const api = {
      authenticate: vi.fn().mockResolvedValue(undefined),
      computerUseWebSocketUrl: () => "ws://localhost/computer-use/ws",
    } as unknown as GatewayApi;
    const channel = new ComputerUseChannel(api, "desktop-test", true, (state) => states.push(state));
    try {
      await channel.connect();
      const socket = FakeWebSocket.instances[0];
      socket.emit("open");
      socket.emit("message", {
        data: JSON.stringify({
          type: "computer_use_request",
          request_id: "click-unverified",
          session_id: "session-click",
          mode: "background_app",
          action: { action: "click", window_id: "42", x: 100, y: 100 },
        }),
      });
      const summary = dispatched ? "Click dispatched" : "Click dispatch was not acknowledged";
      await vi.waitFor(() => expect(states.at(-1)?.logs.at(-1)?.summary).toBe(summary));
      expect(states.at(-1)).toMatchObject({
        status: dispatched ? "ready" : "error",
        error: dispatched ? null : "Dispatch was not acknowledged",
      });
      expect(states.at(-1)?.previews[0]).toMatchObject({
        status: dispatched ? "ready" : "error",
        summary,
      });
      expect(states.at(-1)?.logs.at(-1)?.ok).toBe(dispatched);
    } finally {
      channel.dispose();
    }
  });

  it("retains the preview while idle and releases it 15 seconds after the session releases Computer Use", async () => {
    vi.useFakeTimers();
    try {
      invokeMock
        .mockResolvedValue({ gui_available: true, input_available: true, platform: "macos", displays: [],
          supported_modes: ["background_app", "foreground_desktop"], delivery_policy_version: 1 })
        .mockResolvedValueOnce({
          gui_available: true,
          input_available: true,
          platform: "macos",
          displays: [],
          supported_modes: ["background_app", "foreground_desktop"],
        delivery_policy_version: 1,
        })
        .mockResolvedValueOnce({
          ok: true,
          action: "observe",
          screenshot: {
            data: "cG5n",
            media_type: "image/png",
            width: 100,
            height: 50,
            origin_x: 0,
            origin_y: 0,
            frame_id: "frame-1",
          },
          cursor: { x: 10, y: 10 },
        });
      const states: ComputerUseState[] = [];
      const api = {
        authenticate: vi.fn().mockResolvedValue(undefined),
        computerUseWebSocketUrl: () => "ws://localhost/computer-use/ws",
      } as unknown as GatewayApi;
      const channel = new ComputerUseChannel(api, "desktop-test", true, (state) => states.push(state));

      await channel.connect();
      const socket = FakeWebSocket.instances[0];
      socket.emit("open");
      socket.emit("message", {
        data: JSON.stringify({
          type: "computer_use_request",
          request_id: "request-idle",
          session_id: "session-idle",
          mode: "background_app",
          action: { action: "observe", window_id: "42" },
        }),
      });
      await vi.advanceTimersByTimeAsync(0);

      expect(states.at(-1)).toMatchObject({ active: true, status: "ready" });
      expect(states.at(-1)?.previews[0]?.frame?.frame_id).toBe("frame-1");
      await vi.advanceTimersByTimeAsync(COMPUTER_USE_RELEASE_RETENTION_MS * 2);
      expect(states.at(-1)).toMatchObject({ active: true });
      socket.emit("message", {
        data: JSON.stringify({
          type: "computer_use_release",
          session_id: "session-idle",
        }),
      });
      await vi.advanceTimersByTimeAsync(COMPUTER_USE_RELEASE_RETENTION_MS);
      expect(states.at(-1)).toMatchObject({ active: false, previews: [] });
      expect(states.at(-1)?.logs).toHaveLength(1);
      channel.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps simultaneous sessions in independent preview slots and releases them independently", async () => {
    vi.useFakeTimers();
    try {
      invokeMock
        .mockResolvedValue({ gui_available: true, input_available: true, platform: "macos", displays: [],
          supported_modes: ["background_app", "foreground_desktop"], delivery_policy_version: 1 })
        .mockResolvedValueOnce({
          gui_available: true,
          input_available: true,
          platform: "macos",
          displays: [],
          supported_modes: ["background_app", "foreground_desktop"],
        delivery_policy_version: 1,
        })
        .mockResolvedValueOnce({
          ok: true,
          action: "observe",
          screenshot: { data: "MQ==", media_type: "image/png", width: 10, height: 10, origin_x: 0, origin_y: 0, frame_id: "frame-one" },
        })
        .mockResolvedValueOnce({
          ok: true,
          action: "observe",
          screenshot: { data: "Mg==", media_type: "image/png", width: 10, height: 10, origin_x: 0, origin_y: 0, frame_id: "frame-two" },
        });
      const states: ComputerUseState[] = [];
      const api = {
        authenticate: vi.fn().mockResolvedValue(undefined),
        computerUseWebSocketUrl: () => "ws://localhost/computer-use/ws",
      } as unknown as GatewayApi;
      const channel = new ComputerUseChannel(api, "desktop-test", true, (state) => states.push(state));

      await channel.connect();
      const socket = FakeWebSocket.instances[0];
      socket.emit("open");
      socket.emit("message", {
        data: JSON.stringify({ type: "computer_use_request", request_id: "one", session_id: "session-one", action: { action: "observe" } }),
      });
      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(5_000);
      socket.emit("message", {
        data: JSON.stringify({ type: "computer_use_request", request_id: "two", session_id: "session-two", action: { action: "observe" } }),
      });
      await vi.advanceTimersByTimeAsync(0);

      expect(states.at(-1)?.previews.map((preview) => preview.frame?.frame_id)).toEqual(["frame-one", "frame-two"]);
      await vi.advanceTimersByTimeAsync(COMPUTER_USE_RELEASE_RETENTION_MS * 2);
      expect(states.at(-1)?.previews.map((preview) => preview.frame?.frame_id)).toEqual(["frame-one", "frame-two"]);
      socket.emit("message", {
        data: JSON.stringify({ type: "computer_use_release", session_id: "session-one" }),
      });
      await vi.advanceTimersByTimeAsync(5_000);
      socket.emit("message", {
        data: JSON.stringify({ type: "computer_use_release", session_id: "session-two" }),
      });
      await vi.advanceTimersByTimeAsync(10_000);
      expect(states.at(-1)?.previews.map((preview) => preview.frame?.frame_id)).toEqual(["frame-two"]);
      await vi.advanceTimersByTimeAsync(5_000);
      expect(states.at(-1)).toMatchObject({ active: false, previews: [] });
      channel.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it("restores an active preview after reconnect and preserves its release deadline", async () => {
    vi.useFakeTimers();
    try {
      invokeMock.mockResolvedValueOnce({
        gui_available: true,
        input_available: true,
        platform: "macos",
        displays: [],
        supported_modes: ["background_app", "foreground_desktop"],
        delivery_policy_version: 1,
      });
      const states: ComputerUseState[] = [];
      const api = {
        authenticate: vi.fn().mockResolvedValue(undefined),
        computerUseWebSocketUrl: () => "ws://localhost/computer-use/ws",
      } as unknown as GatewayApi;
      const channel = new ComputerUseChannel(api, "desktop-test", true, (state) => states.push(state));

      await channel.connect();
      const socket = FakeWebSocket.instances[0];
      socket.emit("open");
      socket.emit("message", {
        data: JSON.stringify({
          type: "computer_use_host_registered",
          available: true,
          previews: [{
            session_id: "session-restored",
            mode: "background_app",
            delivery_policy: "allow_foreground",
            status: "ready",
            action: "observe",
            summary: "Observed window",
            frame: { data: "MQ==", media_type: "image/png", width: 10, height: 10, origin_x: 0, origin_y: 0, frame_id: "restored-frame" },
            observation_kind: "ax", ax_element_count: 1,
            release_deadline_ms: Date.now() + COMPUTER_USE_RELEASE_RETENTION_MS,
          }],
        }),
      });

      expect(states.at(-1)).toMatchObject({ active: true, deliveryPolicy: "allow_foreground" });
      expect(states.at(-1)?.previews[0]?.deliveryPolicy).toBe("allow_foreground");
      expect(states.at(-1)?.previews[0]?.frame?.frame_id).toBe("restored-frame");
      expect(states.at(-1)?.previews[0]?.observationKind).toBe("ax");
      await vi.advanceTimersByTimeAsync(COMPUTER_USE_RELEASE_RETENTION_MS - 1);
      expect(states.at(-1)).toMatchObject({ active: true });
      await vi.advanceTimersByTimeAsync(1);
      expect(states.at(-1)).toMatchObject({ active: false, previews: [] });
      channel.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it("expires a released busy preview after 15 seconds and ignores its late native result", async () => {
    vi.useFakeTimers();
    try {
      let finishAction!: (result: object) => void;
      invokeMock
        .mockResolvedValueOnce({
          gui_available: true,
          input_available: true,
          platform: "macos",
          displays: [],
          supported_modes: ["background_app", "foreground_desktop"],
        delivery_policy_version: 1,
        })
        .mockReturnValueOnce(new Promise((resolve) => { finishAction = resolve; }));
      const states: ComputerUseState[] = [];
      const api = {
        authenticate: vi.fn().mockResolvedValue(undefined),
        computerUseWebSocketUrl: () => "ws://localhost/computer-use/ws",
      } as unknown as GatewayApi;
      const channel = new ComputerUseChannel(api, "desktop-test", true, (state) => states.push(state));

      await channel.connect();
      const socket = FakeWebSocket.instances[0];
      socket.emit("open");
      socket.emit("message", {
        data: JSON.stringify({
          type: "computer_use_request",
          request_id: "request-slow",
          session_id: "session-slow",
          action: { action: "observe" },
        }),
      });
      await vi.advanceTimersByTimeAsync(0);
      expect(states.at(-1)?.previews[0]?.status).toBe("busy");

      socket.emit("message", {
        data: JSON.stringify({ type: "computer_use_release", session_id: "session-slow" }),
      });
      await vi.advanceTimersByTimeAsync(COMPUTER_USE_RELEASE_RETENTION_MS);
      expect(states.at(-1)).toMatchObject({ active: false, previews: [] });

      finishAction({ ok: true, action: "observe" });
      await vi.advanceTimersByTimeAsync(0);
      expect(states.at(-1)).toMatchObject({ active: false, previews: [] });
      channel.dispose();
    } finally {
      vi.useRealTimers();
    }
  });
});
