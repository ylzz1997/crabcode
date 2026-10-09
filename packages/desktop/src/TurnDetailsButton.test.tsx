/* @vitest-environment jsdom */

import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import { ChatItemView } from "./App";
import { applyGatewayEvent } from "./events";
import { withClientTurnDetails } from "./turnDetails";
import type { SessionViewState } from "./types";

const details = {
  session_id: "round-session", started_at: "2026-10-10T01:00:00Z", ended_at: "2026-10-10T01:00:03Z",
  duration_ms: 3000, tool_call_count: 2, thinking_count: 1, request_count: 3,
  retry_count: 0, compact_count: 0, reason: "end_turn", usage: { input_tokens: 100, output_tokens: 30 },
};
const state: SessionViewState = {
  id: details.session_id, cwd: "/work", title: "test", items: [], loading: false, busy: true,
  connected: true, operationId: "op", status: null, error: null, runStartedAt: 1000,
};

describe("per-round details", () => {
  it("shows details for an old Gateway history without requiring turn_details", () => {
    const restored = applyGatewayEvent(state, { type: "session_history", messages: [
      { uuid: "u", role: "user", timestamp: details.started_at, content: "question" },
      { uuid: "a", role: "assistant", timestamp: details.ended_at, content: [{ type: "thinking", thinking: "reason" }, { type: "text", text: "done" }] },
    ] });
    const assistant = restored.items.find((item) => item.kind === "assistant")!;
    expect(assistant.turnDetails).toMatchObject({ session_id: state.id, source: "history", duration_ms: 3000, tool_call_count: 0, thinking_count: 1 });
    expect(assistant.turnDetails?.retry_count).toBeUndefined();
    const cached = restored.items.map((item) => ({ ...item, turnDetails: undefined }));
    expect(withClientTurnDetails(cached, state.id).find((item) => item.kind === "assistant")?.turnDetails).toEqual(assistant.turnDetails);
  });

  it("adds a live fallback on old terminal events and preserves running and previous rounds", () => {
    const current = { ...state, items: [
      { id: "u", kind: "user" as const, startedAt: 1000 },
      { id: "t", kind: "tool" as const, tool_use_id: "t", status: "complete" as const },
      { id: "child", kind: "tool" as const, agent_id: "child", tool_use_id: "child" },
      { id: "a", kind: "assistant" as const, text: "done", status: "running" as const },
    ] };
    expect(withClientTurnDetails(current.items, state.id, true)[3].turnDetails).toBeUndefined();
    const finished = applyGatewayEvent(current, { type: "turn_complete" });
    expect(finished.items[3].turnDetails).toMatchObject({ session_id: state.id, tool_call_count: 1 });
  });

  it("keeps server statistics on live and restored replies without overwriting a prior round", () => {
    const live = applyGatewayEvent({ ...state, items: [{ id: "live", kind: "assistant", text: "done", status: "running" }] }, {
      type: "turn_complete", assistant_message_uuid: "durable", turn_details: details,
    });
    expect(live.items[0]).toMatchObject({ id: "durable", turnDetails: details });
    const restored = applyGatewayEvent(state, { type: "session_history", messages: [{
      uuid: "durable", role: "assistant", timestamp: details.ended_at, content: [{ type: "text", text: "done" }], turn_details: details,
    }] });
    expect(restored.items[0].turnDetails).toEqual(details);
    const interrupted = applyGatewayEvent({ ...state, items: [live.items[0], { id: "new", kind: "user", text: "next" }] }, {
      type: "turn_complete", reason: "interrupted", turn_details: { ...details, tool_call_count: 0 },
    });
    expect(interrupted.items[0].turnDetails).toEqual(details);
  });

  it("opens before copy, traps focus, copies the ID and closes with focus restored", async () => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const previousClipboard = navigator.clipboard;
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    try {
      act(() => root.render(<ChatItemView item={{ id: "reply", kind: "assistant", text: "done", status: "complete", turnDetails: details }}
        now={0} showTurnDuration turnDurationFormat="hms" onPermission={vi.fn()} onToggleChoice={vi.fn()} onSubmitChoice={vi.fn()} onPlan={vi.fn()} />));
      const trigger = container.querySelector<HTMLButtonElement>('[aria-label="本轮详情"]')!;
      expect(container.querySelector(".message-actions")?.firstElementChild).toBe(trigger);
      act(() => trigger.click());
      const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!;
      expect(dialog.textContent).toContain("round-session");
      expect(dialog.textContent).toContain("工具调用次数2 次");
      expect(dialog.textContent).toContain("思考次数1 次");
      expect(document.activeElement).toBe(dialog);
      act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, cancelable: true })));
      const copy = dialog.querySelector<HTMLButtonElement>('[aria-label="复制 Session ID"]')!;
      expect(document.activeElement).toBe(copy);
      await act(async () => { copy.click(); await Promise.resolve(); });
      expect(writeText).toHaveBeenCalledWith("round-session");
      act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })));
      expect(document.querySelector('[role="dialog"]')).toBeNull();
      expect(document.activeElement).toBe(trigger);
      act(() => trigger.click());
      act(() => document.querySelector(".modal-backdrop")!.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
      expect(document.querySelector('[role="dialog"]')).toBeNull();
    } finally {
      act(() => root.unmount());
      container.remove();
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: previousClipboard });
    }
  });
});
