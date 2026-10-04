import { describe, expect, it } from "vitest";
import {
  diffSessionNotifications,
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
    expect(initial.next.get("local:one")).toBe(true);

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
});
