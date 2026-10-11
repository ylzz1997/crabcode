import { describe, expect, it, vi } from "vitest";
import { createSlashCommands, type SlashCommandAction } from "../../shared/slashCommands.js";
import { executeAdditionalDesktopAction, followDesktopLog } from "./desktopSlashCommands";
import type { GatewayApi } from "./gateway";

function harness() {
  const request = vi.fn().mockResolvedValue({ status: "ok" });
  const context = { api: { request } as unknown as GatewayApi, sessionId: "displayed-session", show: vi.fn(), card: vi.fn(), local: vi.fn().mockResolvedValue(false) };
  async function run(name: string, args: string) {
    let action: SlashCommandAction | undefined;
    createSlashCommands({ postMessage: message => { action = message; }, showMessage: context.show }).handlers[name](args);
    return action ? executeAdditionalDesktopAction(action, context) : true;
  }
  return { request, context, run };
}

describe("Desktop slash-command execution", () => {
  it("sets the displayed session on Agent reads and mutations, including multi-agent waits", async () => {
    const h = harness();
    await h.run("/agent", "a/b");
    expect(h.request).toHaveBeenLastCalledWith("/agent/a%2Fb?session_id=displayed-session");
    await h.run("/agent-send", "agent-a --interrupt \"Continue My Task\"");
    expect(JSON.parse(h.request.mock.calls.at(-1)![1].body)).toEqual({ session_id: "displayed-session", prompt: "Continue My Task", interrupt: true });
    await h.run("/wait", "agent-a,agent-b --timeout 1000");
    expect(JSON.parse(h.request.mock.calls.at(-1)![1].body)).toEqual({ session_id: "displayed-session", agent_id: ["agent-a", "agent-b"], timeout_ms: 1000 });
  });

  it("preserves an omitted Goal budget and sends explicit budget changes, including removal", async () => {
    const h = harness();
    await h.run("/goal", "pause");
    expect(JSON.parse(h.request.mock.calls.at(-1)![1].body)).toEqual({ session_id: "displayed-session", action: "pause", objective: null });
    await h.run("/goal", "edit --budget 20000 Ship My API");
    expect(JSON.parse(h.request.mock.calls.at(-1)![1].body)).toMatchObject({ objective: "Ship My API", token_budget: 20000 });
    await h.run("/goal", "edit --no-budget Ship My API");
    expect(JSON.parse(h.request.mock.calls.at(-1)![1].body)).toMatchObject({ token_budget: null });
  });

  it("executes schedule creation and filtered history instead of opening a placeholder page", async () => {
    const h = harness();
    await h.run("/schedule", "create \"Night Job\" cron \"0 2 * * *\" \"Check My API\"");
    expect(h.request.mock.calls.at(-1)![0]).toBe("/schedule/create");
    expect(JSON.parse(h.request.mock.calls.at(-1)![1].body)).toMatchObject({ session_id: "displayed-session", name: "Night Job", schedule: "0 2 * * *", prompt: "Check My API" });
    await h.run("/schedule", "runs short-id --status failed --limit 10");
    expect(h.request).toHaveBeenLastCalledWith("/schedule/short-id/runs?session_id=displayed-session&status=failed&limit=10");
  });

  it("keeps team failure reasons and unread-message filters", async () => {
    const h = harness();
    await h.run("/team", "task-fail team-a task-b --agent worker-c \"Build failed\"");
    expect(JSON.parse(h.request.mock.calls.at(-1)![1].body)).toMatchObject({ session_id: "displayed-session", reason: "Build failed", agent_id: "worker-c" });
    await h.run("/team", "messages team-a --agent worker-c --unread");
    expect(h.request).toHaveBeenLastCalledWith("/team/team-a/messages?session_id=displayed-session&agent_id=worker-c&unread=true");
  });

  it("keeps local actions local and surfaces Gateway errors", async () => {
    const h = harness();
    h.context.local.mockResolvedValue(true);
    await h.run("/follow-up", "steer");
    expect(h.request).not.toHaveBeenCalled();
    h.context.local.mockResolvedValue(false);
    h.request.mockRejectedValue(new Error("Session not found"));
    await expect(h.run("/agents", "")).rejects.toThrow("Session not found");
    expect(h.context.card).not.toHaveBeenCalled();
  });

  it("decodes log SSE frames split across chunks and releases the stream", async () => {
    const bytes = new TextEncoder().encode('data: "第一行"\n\ndata: "second"\n\n');
    const stream = new ReadableStream({ start(controller) { controller.enqueue(bytes.slice(0, 12)); controller.enqueue(bytes.slice(12)); controller.close(); } });
    const response = vi.fn().mockResolvedValue(new Response(stream));
    const onLines = vi.fn();
    await followDesktopLog({ response } as unknown as GatewayApi, "displayed-session", "search", new AbortController().signal, onLines);
    expect(onLines.mock.calls.map(call => call[0]).join("\n")).toBe("第一行\nsecond");
    expect(stream.locked).toBe(false);
  });
});
