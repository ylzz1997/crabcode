import { describe, expect, it } from "vitest";
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
  };
}

describe("Gateway event reducer", () => {
  it("merges streaming assistant chunks", () => {
    let current = applyGatewayEvent(state(), { type: "stream_text", text: "Hello" });
    current = applyGatewayEvent(current, { type: "stream_text", text: " world" });
    expect(current.items).toHaveLength(1);
    expect(current.items[0].text).toBe("Hello world");
    expect(current.busy).toBe(true);
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

  it("finishes the active session state", () => {
    const current = applyGatewayEvent(
      { ...state(), busy: true },
      { type: "turn_complete", context_used_percent: 25 },
    );
    expect(current.busy).toBe(false);
    expect(current.operationId).toBeNull();
  });
});
