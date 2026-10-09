import { afterEach, describe, expect, it, vi } from "vitest";
import { applyGatewayEvent } from "./events";
import type { SessionViewState } from "./types";

function state(): SessionViewState {
  return {
    id: "session-1",
    cwd: "/work/project",
    title: "Test",
    items: [],
    loading: false,
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
  it("recalls, deletes and promotes only acknowledged queue entries", () => {
    const current: SessionViewState = {
      ...state(), busy: true,
      pendingFollowUps: ["edit", "remove", "steer"].map((id) => ({
        item: { id, kind: "user", text: id }, text: `${id} with context`,
        images: [{ media_type: "image/png", data: "image-data" }], status: "pending",
      })),
    };
    const edited = applyGatewayEvent(current, { type: "queued_message_updated", request_id: "edit", action: "edit" });
    expect(edited.pendingFollowUps?.[0]).toMatchObject({ status: "editing", text: "edit with context", images: current.pendingFollowUps![0].images });
    const removed = applyGatewayEvent(edited, { type: "queued_message_updated", request_id: "remove", action: "remove" });
    expect(removed.items).toHaveLength(0);
    expect(removed.pendingFollowUps?.map((entry) => entry.item.id)).toEqual(["edit", "steer"]);
    const steered = applyGatewayEvent(removed, { type: "queued_message_updated", request_id: "steer", action: "steer" });
    expect(steered.items.map((item) => item.text)).toEqual(["steer"]);
    expect(steered.pendingFollowUps).toHaveLength(1);
    expect(applyGatewayEvent(steered, { type: "turn_complete" }).pendingFollowUps?.[0].status).toBe("editing");
    expect(applyGatewayEvent(steered, { type: "queued_message_updated", request_id: "steer", action: "steer" })).toBe(steered);
  });

  it("a rejected queue action unlocks its card without falsely removing or cancelling it", () => {
    const current: SessionViewState = {
      ...state(), busy: true,
      pendingFollowUps: [{ item: { id: "q", kind: "user", text: "queued" }, text: "queued", images: [], status: "pending", action: "steer" }],
    };
    const updated = applyGatewayEvent(current, {
      type: "error", command_error: true, command: "queued_message_action", request_id: "q", error_type: "follow_up_rejected", message: "full",
    });
    expect(updated.pendingFollowUps?.[0]).toMatchObject({ status: "pending", action: undefined });
    expect(updated.busy).toBe(true);
    expect(applyGatewayEvent(current, { type: "queued_message_updated", operation_id: "old", request_id: "q", action: "remove" })).toBe(current);
  });

  it("starts queued messages after the previous reply and leaves other pending messages alone", () => {
    const current: SessionViewState = {
      ...state(), busy: true, runStartedAt: Date.now() - 1000,
      items: [{ id: "reply", kind: "assistant", text: "first result", status: "running" }],
      pendingFollowUps: ["q1", "q2"].map((id) => ({
        item: { id, kind: "user", text: id }, text: id, images: [], status: "pending",
      })),
    };
    const updated = applyGatewayEvent(current, {
      type: "queued_message_started", operation_id: "operation-1", request_id: "q1", text: "q1",
    });
    expect(updated.items.filter((item) => item.kind !== "turn_duration").map((item) => item.text)).toEqual(["first result", "q1"]);
    expect(updated.items[0].status).toBe("complete");
    expect(updated.busy).toBe(true);
    expect(updated.pendingFollowUps?.map((entry) => entry.item.id)).toEqual(["q2"]);
    expect(applyGatewayEvent(updated, { type: "turn_complete", reason: "interrupted" }).pendingFollowUps?.[0].status).toBe("cancelled");
  });

  it("keeps rejected queue content recoverable and correlates failures by request id", () => {
    const current: SessionViewState = {
      ...state(), busy: true,
      pendingFollowUps: ["q1", "q2"].map((id) => ({
        item: { id, kind: "user", text: id }, text: id,
        images: [{ media_type: "image/png", data: "base64" }], status: "pending",
      })),
    };
    const updated = applyGatewayEvent(current, {
      type: "error", command_error: true, command: "queue_message", request_id: "q2", message: "Queue full",
    });
    expect(updated.pendingFollowUps?.map((entry) => entry.status)).toEqual(["pending", "cancelled"]);
    expect(updated.pendingFollowUps?.[1].images[0].data).toBe("base64");
    expect(updated.busy).toBe(true);
    expect(applyGatewayEvent(updated, {
      type: "queued_message_started", operation_id: "unrelated", request_id: "q1", text: "q1",
    })).toBe(updated);
  });

  it("keeps prompt estimates separate from the server context count", () => {
    const current = state();
    current.status = {
      session_id: current.id, cwd: current.cwd, model: "test", provider: "test", mode: "agent",
      permission_mode: "default", context_used_tokens: 0, context_window_tokens: 32000, context_used_percent: 0,
    };
    const budget = {
      source: "estimated" as const, mode: "discovery" as const,
      system_tokens: 1000, tool_tokens: 3000, directory_tokens: 160,
      loaded_tools: 12, available_tools: 47, loaded_names: ["ToolSearch"],
    };
    const updated = applyGatewayEvent(current, {
      type: "turn_complete", context_used_tokens: 5000, context_token_source: "server", prompt_budget: budget,
    });
    expect(updated.status?.prompt_budget).toEqual(budget);
    expect(updated.status?.context_token_source).toBe("server");
  });
  it("merges streaming assistant chunks", () => {
    let current = applyGatewayEvent(state(), { type: "stream_text", text: "Hello" });
    current = applyGatewayEvent(current, { type: "stream_text", text: " world" });
    expect(current.items).toHaveLength(1);
    expect(current.items[0].text).toBe("Hello world");
    expect(current.busy).toBe(true);
    expect(current.currentStep?.label).toBe("生成回复");
    expect(current.runStartedAt).not.toBeNull();
  });

  it("keeps an output-limit error visible after the turn ends and heartbeats arrive", () => {
    let current = applyGatewayEvent(state(), { type: "stream_text", text: "unfinished reply" });
    current = applyGatewayEvent(current, {
      type: "error", error_type: "output_limit", recoverable: true,
      message: "Output token limit reached; the reply is incomplete.",
    });
    current = applyGatewayEvent(current, { type: "turn_complete", reason: "length" });
    current = applyGatewayEvent(current, { type: "server.heartbeat" });
    expect(current.busy).toBe(false);
    expect(current.currentStep).toBeNull();
    expect(current.items).toEqual(expect.arrayContaining([
      expect.objectContaining({ kind: "assistant", text: "unfinished reply", status: "complete" }),
      expect.objectContaining({ kind: "error", text: "Output token limit reached; the reply is incomplete." }),
    ]));
  });

  it("keeps the turn live and starts a fresh response item while reconnecting", () => {
    let current = applyGatewayEvent(state(), { type: "stream_text", text: "partial" });
    current = applyGatewayEvent(current, {
      type: "stream_retry",
      message: "Reconnecting... 1/5",
      retry_count: 1,
      max_retries: 5,
      delay_seconds: 0.2,
      discarded_text_chars: 7,
    });
    expect(current.busy).toBe(true);
    expect(current.currentStep?.label).toBe("Reconnecting... 1/5");
    expect(current.items[0]).toMatchObject({ text: "partial", status: "complete" });

    current = applyGatewayEvent(current, { type: "stream_text", text: "recovered" });
    expect(current.items).toHaveLength(2);
    expect(current.items[1]).toMatchObject({ text: "recovered", status: "running" });
    expect(current.currentStep?.label).toBe("生成回复");
  });

  it.each([
    ["stream_text", "response", "生成回复"],
    ["thinking", "thinking", "思考中"],
  ] as const)("clears retry status when %s resumes", (type, kind, label) => {
    const clock = vi.spyOn(Date, "now").mockReturnValue(1_000);
    let current = applyGatewayEvent(state(), { type: "stream_text", text: "partial" });
    clock.mockReturnValue(2_000);
    current = applyGatewayEvent(current, {
      type: "stream_retry", message: "模型连接中断，正在重试 1/3",
    });
    expect(current.currentStep).toEqual({
      kind: "retry", label: "模型连接中断，正在重试 1/3", startedAt: 2_000,
    });

    clock.mockReturnValue(3_000);
    current = applyGatewayEvent(current, { type, text: "recovered" });
    expect(current.currentStep).toEqual({ kind, label, startedAt: 3_000 });
    expect(current.busy).toBe(true);
    expect(current.runStartedAt).toBe(1_000);

    clock.mockReturnValue(4_000);
    current = applyGatewayEvent(current, { type, text: " more" });
    expect(current.currentStep).toEqual({ kind, label, startedAt: 3_000 });
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

  it("uses normalized tool input returned with the result", () => {
    let current = applyGatewayEvent(state(), {
      type: "tool_use",
      tool_name: "apply_patch",
      tool_use_id: "patch-1",
      tool_input: { patch: "*** Begin Patch" },
    });
    current = applyGatewayEvent(current, {
      type: "tool_result",
      tool_name: "apply_patch",
      tool_use_id: "patch-1",
      tool_input: { patch: "*** Begin Patch", affected_paths: ["src/a.ts", "src/b.ts"] },
      result: "Applied patch",
    });
    expect(current.items[0].input).toEqual({
      patch: "*** Begin Patch",
      affected_paths: ["src/a.ts", "src/b.ts"],
    });
  });

  it("shows tool result image attachments immediately", () => {
    const current = applyGatewayEvent(state(), {
      type: "tool_result",
      tool_use_id: "tool-shot",
      tool_name: "Browser",
      result: "screenshot saved",
      images: [{ media_type: "image/png", data: "aGVsbG8=" }],
    });
    expect(current.items[0]).toMatchObject({
      kind: "tool",
      images: [{ media_type: "image/png", data: "aGVsbG8=" }],
      collapsed: false,
    });
  });

  it.each([false, true])("shows the complete AX tree in live and restored tool cards (with screenshot: %s)", (withImage) => {
    const tree = 'e1 window "Demo"\n\te2 text "<img src=x>"\n\te3 button (press) "Send"';
    const result = JSON.stringify({ ok: true, summary: "Observed accessibility tree",
      accessibility: { tree, element_count: 3, truncated: true, tree_format: "indexed_text_v1" } });
    const images = withImage ? [{ media_type: "image/png", data: "cG5n" }] : [];
    const started = applyGatewayEvent(state(), { type: "tool_use", tool_name: "ComputerUse", tool_use_id: "ax",
      tool_input: { action: "observe", window_id: "7" } });
    const live = applyGatewayEvent(started, { type: "tool_result", tool_use_id: "ax",
      result, result_for_display: "Observed accessibility tree", images });
    const restored = applyGatewayEvent(state(), { type: "session_history", messages: [
      { role: "assistant", content: [{ type: "tool_use", name: "ComputerUse", id: "ax", input: { action: "observe", window_id: "7" } }] },
      { role: "user", content: [{ type: "tool_result", tool_use_id: "ax", content: result },
        ...images.map((image) => ({ type: "image", source: { type: "base64", ...image } }))] },
    ] });
    for (const item of [live.items[0], restored.items[0]]) {
      expect(item.result).toContain("AX Tree · 紧凑文本树 · 3 个元素 · 内容已截断");
      expect(item.result).toContain(tree);
      expect(item.result).not.toContain("tree_format");
      expect(item.images).toEqual(images);
    }
  });

  it("preserves captioned image batches in live results and restored history", () => {
    const images = [
      { media_type: "image/png", data: "YQ==", description: "修改前" },
      { media_type: "image/jpeg", data: "Yg==", description: "修改后\n细节" },
    ];
    const live = applyGatewayEvent(state(), {
      type: "tool_result", tool_use_id: "image-batch", tool_name: "Image", result: "attached", images,
    });
    const restored = applyGatewayEvent(state(), {
      type: "session_history",
      messages: [
        { role: "assistant", content: [{ type: "tool_use", id: "image-batch", name: "Image", input: { path: ["a.png", "b.jpg"] } }] },
        { role: "user", content: [
          { type: "tool_result", tool_use_id: "image-batch", content: "attached" },
          ...images.map(({ description, ...source }) => ({ type: "image", source: { type: "base64", ...source }, description })),
        ] },
      ],
    });
    expect(live.items[0].images).toEqual(images);
    expect(restored.items).toHaveLength(1);
    expect(restored.items[0]).toMatchObject({ images, collapsed: false });
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
        loading: true,
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
    expect(current.loading).toBe(false);
    expect(current.busy).toBe(false);
    expect(current.operationId).toBeNull();
    expect(current.error).toBeNull();
    expect(current.items[0].text).toBe("恢复成功");
    expect(current.runStartedAt).toBeNull();
  });

  it("clears stale execution state when session resume is rejected", () => {
    const current = applyGatewayEvent(
      {
        ...state(),
        loading: true,
        busy: true,
        connected: false,
        error: "连接中断",
        runStartedAt: 1_000,
        currentStep: { kind: "response", label: "生成回复", startedAt: 1_000 },
      },
      {
        type: "error",
        command: "resume_session",
        command_error: true,
        error_type: "session_not_found",
        message: "session not found",
      },
    );
    expect(current.busy).toBe(false);
    expect(current.loading).toBe(false);
    expect(current.connected).toBe(false);
    expect(current.operationId).toBeNull();
    expect(current.runStartedAt).toBeNull();
    expect(current.currentStep).toBeNull();
    expect(current.error).toBe("session not found");
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

  it("clears busy state when a stale steering operation is rejected", () => {
    const current = applyGatewayEvent(
      {
        ...state(),
        busy: true,
        runStartedAt: 1_000,
        currentStep: { kind: "response" as const, label: "生成回复", startedAt: 1_000 },
      },
      {
        type: "error",
        command: "steer_message",
        command_error: true,
        error_type: "operation_not_found",
        operation_id: "operation-1",
        message: "operation not found or not foreground",
      },
    );

    expect(current.busy).toBe(false);
    expect(current.operationId).toBeNull();
    expect(current.runStartedAt).toBeNull();
    expect(current.currentStep).toBeNull();
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

  it("updates the local-estimate hint when server usage becomes available", () => {
    const initial: SessionViewState = {
      ...state(),
      status: { session_id: "session-1", cwd: "/work", model: "example", provider: "codex",
        mode: "agent", permission_mode: "default", context_used_tokens: 8000,
        context_window_tokens: 32000, context_used_percent: 25, context_token_source: "estimated" },
    };
    const updated = applyGatewayEvent(initial, { type: "turn_complete", context_used_tokens: 2000,
      context_used_percent: 6.25, context_token_source: "calibrated" });
    expect(updated.status?.context_used_tokens).toBe(2000);
    expect(updated.status?.context_token_source).toBe("calibrated");
    const missing = applyGatewayEvent(updated, { type: "turn_complete", context_used_tokens: 2500,
      context_token_source: "estimated" });
    expect(missing.status?.context_token_source).toBe("estimated");
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

  it("restores user file references as attachment chips above the prompt text", () => {
    const current = applyGatewayEvent(state(), {
      type: "session_history",
      messages: [{
        uuid: "user-files",
        role: "user",
        content: '<file name="notes.md">\nsecret body\n</file>\n\n<folder>\n/work/src\n</folder>\n\n继续看',
      }],
    });
    expect(current.items[0]).toMatchObject({
      kind: "user",
      text: "继续看",
      attachments: [
        { kind: "file", label: "notes.md", title: "notes.md" },
        { kind: "folder", label: "src", title: "/work/src" },
      ],
    });
    expect(current.items[0].text).not.toContain("secret body");
  });
});
