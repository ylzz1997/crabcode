/* @vitest-environment jsdom */

import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import { deriveTrajectory, TrajectoryView } from "./TrajectoryView";
import type { ChatItem } from "./types";

const items: ChatItem[] = [
  { id: "user-1", kind: "user", text: "检查构建", status: "complete", startedAt: 1_000, completedAt: 1_000 },
  { id: "thinking-1", kind: "thinking", text: "先读取配置", status: "complete", startedAt: 1_100, completedAt: 1_300 },
  {
    id: "tool-1",
    kind: "tool",
    title: "Bash",
    input: { command: "npm test" },
    result: "48 passed",
    status: "complete",
    startedAt: 1_300,
    completedAt: 3_300,
  },
  { id: "assistant-1", kind: "assistant", text: "构建通过", status: "complete", startedAt: 3_400, completedAt: 3_500 },
  { id: "duration-1", kind: "turn_duration", status: "complete", durationMs: 2_500 },
];

describe("trajectory projection", () => {
  it("derives turn, lane, duration, and searchable tool details from chat items", () => {
    const turns = deriveTrajectory(items, 4_000);
    expect(turns).toHaveLength(1);
    expect(turns[0].durationMs).toBe(2_500);
    expect(turns[0].records.map((record) => record.lane)).toEqual(["input", "model", "tools", "model"]);
    expect(turns[0].records[2]).toMatchObject({
      label: "TOOL",
      title: "Bash",
      durationMs: 2_000,
    });
    expect(turns[0].records[2].searchable).toContain("npm test");
    expect(turns[0].records[2].searchable).toContain("48 passed");
  });

  it("filters calls and expands a record inspector", () => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    vi.spyOn(Date, "now").mockReturnValue(4_000);

    act(() => root.render(<TrajectoryView items={items} now={4_000} />));
    expect(container.querySelectorAll(".trajectory-record")).toHaveLength(4);

    const calls = Array.from(container.querySelectorAll<HTMLButtonElement>(".trajectory-toolbar-actions button"))
      .find((button) => button.textContent?.includes("Calls"))!;
    act(() => calls.click());
    expect(container.querySelectorAll(".trajectory-record")).toHaveLength(3);

    const first = container.querySelector<HTMLButtonElement>(".trajectory-record-summary")!;
    act(() => first.click());
    expect(first.getAttribute("aria-expanded")).toBe("true");
    expect(container.querySelector(".trajectory-record-details")?.textContent).toContain("Turn / Step");

    act(() => calls.click());
    const toolSummary = Array.from(container.querySelectorAll<HTMLButtonElement>(".trajectory-record-summary"))
      .find((button) => button.textContent?.includes("Bash"))!;
    act(() => toolSummary.click());
    expect(container.querySelector('[aria-label="复制调用参数"]')).not.toBeNull();
    expect(container.querySelector('[aria-label="复制执行结果"]')).not.toBeNull();

    act(() => root.unmount());
    container.remove();
  });
});
