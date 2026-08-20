import { afterEach, describe, expect, it, vi } from "vitest";
import { applyGatewayEvent } from "./events";
import type { SessionViewState } from "./types";

function state(): SessionViewState {
  return {
    id: "session-1",
    cwd: "/work/project",
    title: "Test",
    items: [],
    busy: false,
    connected: true,
    operationId: "operation-1",
    status: null,
    error: null,
    runStartedAt: null,
    currentStep: null,
    lastTurnUsage: null,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Gateway event reducer", () => {
  it("merges streaming assistant chunks", () => {
    let current = applyGatewayEvent(state(), { type: "stream_text", text: "Hello" });
    current = applyGatewayEvent(current, { type: "stream_text", text: " world" });
    expect(current.items).toHaveLength(1);
    expect(current.items[0].text).toBe("Hello world");
    expect(current.busy).toBe(true);
    expect(current.currentStep?.label).toBe("生成回复");
    expect(current.runStartedAt).not.toBeNull();
  });

  it("resolves tool and permission cards by tool id", () => {
    let current = applyGatewayEvent(state(), {
      type: "tool_use", tool_name: "FileEdit", tool_use_id: "tool-1", tool_input: { path: "app.ts" },
    });
    current = applyGatewayEvent(current, { type: "tool_result", tool_use_id: "tool-1", result: "done" });
    current = applyGatewayEvent(current, { type: "permission_request", tool_name: "Bash", tool_use_id: "tool-2" });
    current = applyGatewayEvent(current, { type: "permission_response", tool_use_id: "tool-2", allowed: true });
    expect(current.items[0].status).toBe("complete");
    expect(current.items[1].status).toBe("allowed");
  });

  it("records live and completed step durations", () => {
    const now = vi.spyOn(Date, "now");
    now.mockReturnValueOnce(1_000);
    let current = applyGatewayEvent(state(), {
      type: "tool_use",
      tool_name: "FileEdit",
      tool_use_id: "tool-1",
    });
    expect(current.runStartedAt).toBe(1_000);
    expect(current.currentStep).toEqual({ kind: "tool", label: "FileEdit", startedAt: 1_000 });
    expect(current.items[0].startedAt).toBe(1_000);

    now.mockReturnValueOnce(2_600);
    current = applyGatewayEvent(current, {
      type: "tool_result",
      tool_use_id: "tool-1",
      result: "done",
    });
    expect(current.items[0].durationMs).toBe(1_600);
    expect(current.currentStep).toEqual({ kind: "response", label: "整理结果", startedAt: 2_600 });
  });

  it("restores history into an idle connected state", () => {
    const current = applyGatewayEvent(
      {
        ...state(),
        busy: true,
        connected: false,
        error: "连接中断",
        runStartedAt: 1_000,
        currentStep: { kind: "response", label: "生成回复", startedAt: 1_000 },
      },
      {
        type: "session_history",
        messages: [{ uuid: "message-1", role: "user", content: "恢复成功" }],
      },
    );
    expect(current.connected).toBe(true);
    expect(current.busy).toBe(false);
    expect(current.operationId).toBeNull();
    expect(current.error).toBeNull();
    expect(current.items[0].text).toBe("恢复成功");
    expect(current.runStartedAt).toBeNull();
  });

  it("keeps an active turn running when a command fails", () => {
    const running = {
      ...state(),
      busy: true,
      runStartedAt: 1_000,
      currentStep: { kind: "tool" as const, label: "Bash", startedAt: 1_200 },
      items: [{ id: "tool-1", kind: "tool" as const, status: "running" as const, startedAt: 1_200 }],
    };
    const current = applyGatewayEvent(running, {
      type: "error",
      message: "invalid permission mode",
      command: "set_permission_mode",
      command_error: true,
    });
    expect(current.busy).toBe(true);
    expect(current.runStartedAt).toBe(1_000);
    expect(current.currentStep).toEqual(running.currentStep);
    expect(current.items).toEqual(running.items);
  });

  it("finishes the active session state", () => {
    const current = applyGatewayEvent(
      { ...state(), busy: true, runStartedAt: 1_000 },
      {
        type: "turn_complete",
        context_used_percent: 25,
        usage: { input_tokens: 100, cache_read_tokens: 75 },
      },
    );
    expect(current.busy).toBe(false);
    expect(current.operationId).toBeNull();
    expect(current.runStartedAt).toBeNull();
    expect(current.currentStep).toBeNull();
    expect(current.lastTurnUsage).toEqual({ input_tokens: 100, cache_read_tokens: 75 });
  });
});
