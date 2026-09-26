import { describe, expect, it } from "vitest";
import { sessionExecutionTask } from "./sessionExecution";
import type { SessionViewState } from "./types";

function session(partial: Partial<SessionViewState> & Pick<SessionViewState, "id">): SessionViewState {
  return {
    cwd: "/tmp",
    title: "未命名会话",
    items: [],
    loading: false,
    busy: false,
    connected: true,
    operationId: null,
    status: null,
    error: null,
    runStartedAt: null,
    currentStep: null,
    ...partial,
  };
}

describe("session execution task", () => {
  it("describes the active session step and keeps its start time", () => {
    const active = session({
      id: "a",
      busy: true,
      title: "整理文档",
      runStartedAt: 1_000,
      currentStep: { kind: "tool", label: "Bash", startedAt: 1_200 },
    });
    expect(sessionExecutionTask({ a: active, b: session({ id: "b" }) }, active)).toEqual({
      label: "正在执行 · Bash",
      startedAt: 1_000,
    });
  });

  it("counts background sessions when the open one is idle", () => {
    const idle = session({ id: "idle", title: "当前" });
    const older = session({
      id: "older",
      busy: true,
      runStartedAt: 1_000,
      currentStep: { kind: "response", label: "生成回复", startedAt: 1_000 },
    });
    const newer = session({
      id: "newer",
      busy: true,
      runStartedAt: 5_000,
      currentStep: { kind: "tool", label: "FileEdit", startedAt: 5_000 },
    });
    expect(sessionExecutionTask({ idle, older, newer }, idle)).toEqual({
      label: "2 个会话正在执行 · FileEdit",
      startedAt: 5_000,
    });
  });

  it("returns nothing when every session is idle", () => {
    expect(sessionExecutionTask({ a: session({ id: "a" }) }, null)).toBeNull();
  });
});
