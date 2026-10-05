import { describe, expect, it } from "vitest";
import {
  diffSessionNotifications,
  sessionInteractions,
  sessionNotificationCopy,
  sessionNotificationName,
  type SessionNotifyState,
} from "./sessionNotifications";

function sessions(entries: Array<[string, SessionNotifyState]>): Map<string, SessionNotifyState> {
  return new Map(entries);
}

describe("session system notifications", () => {
  it("describes the start and completion banners", () => {
    expect(sessionNotificationCopy("start", "  修复登录  ")).toEqual({
      title: "会话开始执行 · 修复登录",
      body: "修复登录",
    });
    expect(sessionNotificationCopy("complete", "")).toEqual({
      title: "会话执行完毕 · 未命名会话",
      body: "未命名会话",
    });
    expect(sessionNotificationCopy("complete", "x".repeat(140)).body).toHaveLength(120);
    expect(sessionNotificationCopy("complete", "x".repeat(140)).title).toBe(`会话执行完毕 · ${"x".repeat(119)}…`);
    expect(sessionNotificationCopy("interaction", "修复登录", "允许 Bash？", "permission")).toEqual({
      title: "需要确认权限 · 修复登录",
      body: "允许 Bash？",
    });
    expect(sessionNotificationCopy("interaction", "修复登录", "选一个方案", "choice")).toEqual({
      title: "需要你的选择 · 修复登录",
      body: "选一个方案",
    });
    expect(sessionNotificationCopy("interaction", "", "", "plan")).toEqual({
      title: "需要确认计划 · 未命名会话",
      body: "未命名会话",
    });
  });

  it("prefers the sidebar title over the placeholder", () => {
    expect(sessionNotificationName("新会话", "修复登录", "请帮我改登录")).toBe("修复登录");
    expect(sessionNotificationName("新会话", "", "请帮我改登录")).toBe("请帮我改登录");
    expect(sessionNotificationName("新会话", "  ", "")).toBe("未命名会话");
  });

  it("baselines the first snapshot and then reports busy transitions", () => {
    const running = sessions([["local:one", { title: "修复登录", busy: true }]]);
    const initial = diffSessionNotifications(null, running);
    expect(initial.intents).toEqual([]);
    expect(initial.next.get("local:one")).toEqual({ busy: true, interactionIds: [] });

    const stillRunning = diffSessionNotifications(initial.next, running);
    expect(stillRunning.intents).toEqual([]);

    const finished = diffSessionNotifications(stillRunning.next, sessions([
      ["local:one", { title: "修复登录", busy: false }],
      ["local:two", { title: "写测试", busy: true }],
    ]));
    expect(finished.intents).toEqual([
      { sessionId: "local:one", phase: "complete", title: "修复登录" },
      { sessionId: "local:two", phase: "start", title: "写测试" },
    ]);
  });

  it("does not treat a removed session as finished", () => {
    const previous = diffSessionNotifications(null, sessions([
      ["local:one", { title: "修复登录", busy: true }],
    ])).next;
    const removed = diffSessionNotifications(previous, sessions([]));
    expect(removed.intents).toEqual([]);
    expect(removed.next.size).toBe(0);
  });

  it("notifies once when a new permission, choice, or plan appears", () => {
    expect(sessionInteractions([
      { id: "tool-1", kind: "permission", status: "pending", title: "允许 Bash？", tool_use_id: "tool-1" },
      { id: "tool-1", kind: "tool", status: "running", title: "Bash", tool_use_id: "tool-1" },
      { id: "ask-1", kind: "choice", status: "complete", title: "已经选过" },
      { id: "plan-1", kind: "plan", status: "pending", title: "实施计划" },
    ])).toEqual([
      { id: "permission:tool-1", kind: "permission", detail: "允许 Bash？" },
      { id: "plan:plan-1", kind: "plan", detail: "实施计划" },
    ]);

    const waiting = sessions([["local:one", {
      title: "修复登录",
      busy: true,
      interactions: [{ id: "permission:tool-1", kind: "permission", detail: "允许 Bash？" }],
    }]]);
    const initial = diffSessionNotifications(null, waiting);
    expect(initial.intents).toEqual([]);

    const stillWaiting = diffSessionNotifications(initial.next, waiting);
    expect(stillWaiting.intents).toEqual([]);

    const asked = diffSessionNotifications(stillWaiting.next, sessions([["local:one", {
      title: "修复登录",
      busy: true,
      interactions: [
        { id: "permission:tool-1", kind: "permission", detail: "允许 Bash？" },
        { id: "choice:ask-1", kind: "choice", detail: "选一个方案" },
      ],
    }]]));
    expect(asked.intents).toEqual([{
      sessionId: "local:one",
      phase: "interaction",
      title: "修复登录",
      detail: "选一个方案",
      interaction: "choice",
    }]);

    const resolved = diffSessionNotifications(asked.next, sessions([["local:one", {
      title: "修复登录",
      busy: true,
      interactions: [],
    }]]));
    expect(resolved.intents).toEqual([]);
  });
});
