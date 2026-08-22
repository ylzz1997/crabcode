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
      type: "tool_use", tool_name: "Bash", tool_use_id: "tool-1", tool_input: { command: "sleep 30" },
    });
    current = applyGatewayEvent(current, { type: "permission_request", tool_name: "Bash", tool_use_id: "tool-1" });
    current = applyGatewayEvent(current, { type: "permission_response", tool_use_id: "tool-1", allowed: true });
    expect(current.items[0].status).toBe("running");
    expect(current.items[1].status).toBe("allowed");
    current = applyGatewayEvent(current, { type: "tool_result", tool_use_id: "tool-1", result: "done" });
    expect(current.items[0].status).toBe("complete");
    expect(current.items[0].input).toEqual({ command: "sleep 30" });
    expect(current.items[0].result).toBe("done");
    expect(current.items[1].status).toBe("allowed");
  });

  it("creates a completed tool card when a result arrives without its use event", () => {
    const current = applyGatewayEvent(state(), {
      type: "tool_result",
      tool_use_id: "tool-late",
      tool_name: "Grep",
      tool_input: { pattern: "needle", path: "src" },
      result: "src/a.ts:1:needle",
      is_error: false,
    });
    expect(current.items[0]).toMatchObject({
      kind: "tool",
      title: "Grep",
      input: { pattern: "needle", path: "src" },
      result: "src/a.ts:1:needle",
      status: "complete",
    });
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

  it("rebuilds structured history cards and hides internal task notifications", () => {
    const current = applyGatewayEvent(state(), {
      type: "session_history",
      messages: [
        { uuid: "user-1", role: "user", content: "测试后台任务" },
        {
          uuid: "assistant-1",
          role: "assistant",
          content: [
            { type: "thinking", thinking: "Planning execution" },
            { type: "tool_use", id: "tool-1", name: "Monitor", input: { command: "sleep 30" } },
          ],
        },
        {
          uuid: "result-1",
          role: "user",
          content: [{ type: "tool_result", tool_use_id: "tool-1", content: "taskId: task-1" }],
        },
        {
          uuid: "notification-1",
          role: "user",
          origin: "task-notification",
          content: "<monitor-event>internal</monitor-event>",
        },
        {
          uuid: "document-action-1",
          role: "user",
          origin: "document-action",
          content: "[文档操作：内部提示词]",
        },
        { uuid: "assistant-2", role: "assistant", content: [{ type: "text", text: "后台任务已完成" }] },
      ],
    });

    expect(current.items.map((item) => item.kind)).toEqual([
      "user",
      "thinking",
      "tool",
      "assistant",
    ]);
    expect(current.items[2]).toMatchObject({
      title: "Monitor",
      detail: "taskId: task-1",
      tool_use_id: "tool-1",
      status: "complete",
    });
    expect(current.items.some((item) => item.text?.includes("monitor-event"))).toBe(false);
    expect(current.items.some((item) => item.text?.includes("内部提示词"))).toBe(false);
  });

  it("updates one document operation card through retry and completion", () => {
    let current = applyGatewayEvent(state(), {
      type: "document_job",
      operation_id: "operation-1",
      action: "translate",
      status: "running",
      locale: "zh-CN",
      current: 0,
      total: 12,
      message: "正在准备",
      engine: "precise",
    });
    current = applyGatewayEvent(current, {
      type: "document_job",
      operation_id: "operation-1",
      action: "translate",
      status: "retrying",
      current: 0,
      total: 12,
      message: "校验后重试",
    });
    current = applyGatewayEvent(current, {
      type: "document_job",
      operation_id: "operation-1",
      action: "translate",
      status: "completed",
      current: 12,
      total: 12,
      message: "已保存",
    });
    expect(current.items).toHaveLength(1);
    expect(current.items[0]).toMatchObject({
      kind: "document_job",
      status: "complete",
      current: 12,
      total: 12,
      locale: "zh-CN",
      engine: "precise",
    });
  });

  it("keeps the requested Blog language on its operation card", () => {
    const current = applyGatewayEvent(state(), {
      type: "document_job",
      operation_id: "blog-operation-1",
      action: "generate_blog",
      status: "running",
      locale: "en",
      language: "ja",
      source: "translation",
      message: "正在生成",
    });
    expect(current.items[0]).toMatchObject({
      kind: "document_job",
      action: "generate_blog",
      locale: "en",
      language: "ja",
      source: "translation",
    });
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

  it("settles an optimistic document card when document action is rejected", () => {
    const running = {
      ...state(),
      busy: true,
      operationId: "operation-1",
      runStartedAt: 1_000,
      currentStep: { kind: "document" as const, label: "翻译文档", startedAt: 1_000 },
      items: [{
        id: "operation-1:document-job",
        kind: "document_job" as const,
        title: "翻译文档",
        status: "running" as const,
        action: "translate" as const,
        startedAt: 1_000,
      }],
    };
    const current = applyGatewayEvent(running, {
      type: "error",
      message: "invalid translation batch size",
      command: "document_action",
      command_error: true,
      operation_id: "operation-1",
    });

    expect(current.busy).toBe(false);
    expect(current.operationId).toBeNull();
    expect(current.items[0]).toMatchObject({ status: "failed", text: "invalid translation batch size" });
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
    expect(current.items.at(-1)).toMatchObject({
      kind: "turn_duration",
      durationMs: expect.any(Number),
    });
  });

  it("restores completed turn durations from message timestamps", () => {
    const current = applyGatewayEvent(state(), {
      type: "session_history",
      messages: [
        { uuid: "user-1", role: "user", timestamp: "2026-08-20T00:00:00.000Z", content: "开始" },
        { uuid: "assistant-1", role: "assistant", timestamp: "2026-08-20T01:02:03.000Z", content: "完成" },
      ],
    });
    expect(current.items.at(-1)).toMatchObject({
      kind: "turn_duration",
      durationMs: 3_723_000,
    });
  });

  it("does not count a later background callback as part of the foreground turn", () => {
    const current = applyGatewayEvent(state(), {
      type: "session_history",
      messages: [
        { uuid: "user-1", role: "user", timestamp: "2026-08-20T00:00:00.000Z", content: "启动后台任务" },
        { uuid: "assistant-1", role: "assistant", timestamp: "2026-08-20T00:00:10.000Z", content: "任务已启动" },
        {
          uuid: "notification-1",
          role: "user",
          origin: "task-notification",
          timestamp: "2026-08-20T00:10:00.000Z",
          content: "<monitor-event>internal</monitor-event>",
        },
        { uuid: "assistant-2", role: "assistant", timestamp: "2026-08-20T00:10:05.000Z", content: "后台任务已完成" },
      ],
    });
    expect(current.items.map((item) => item.kind)).toEqual([
      "user",
      "assistant",
      "turn_duration",
      "assistant",
    ]);
    expect(current.items[2].durationMs).toBe(10_000);
  });
});
