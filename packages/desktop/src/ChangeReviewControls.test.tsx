/* @vitest-environment jsdom */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import { ChangeReview, type ChangeReviewTurn } from "./ChangeReview";
import { ChangeReviewControls } from "./ChangeReviewControls";
import { ALL_EDITS, allEditsSummary, editHistoryIndex } from "./reviewHistory";
import type { GitReviewInfo } from "./types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const turns: ChangeReviewTurn[] = [1, 2, 3, 4].map((number) => ({ id: `edit-${number}`, summary: {
  added: 1, removed: 0, files: [{ path: "same.txt", action: "modify", added: 1, removed: 0,
    diff: `@@ -1 +1,2 @@\n context\n+edit ${number}` }],
} }));
const gitInfo: GitReviewInfo = { available: true, reason: null, root: "/repo", head: "abc123", branch: "feature",
  default_base: "main", branches: ["main", "feature"], commits: [{ id: "abc123", subject: "latest commit" }] };

function setup(info: GitReviewInfo | null = gitInfo) {
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  const onSelect = vi.fn();
  act(() => root.render(<ChangeReviewControls turns={turns} selection={{ summaryId: "edit-4", path: null, showAll: true }}
    gitInfo={info} gitLoading={false} unavailable="当前目录不在 Git 仓库中" onSelect={onSelect} onRefresh={vi.fn()} />));
  const click = (name: string) => act(() => {
    Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.getAttribute("aria-label") === name || button.textContent === name)!.click();
  });
  return { root, container, onSelect, click, cleanup: () => { act(() => root.unmount()); container.remove(); } };
}

describe("edit history addressing", () => {
  it.each([["1", 0], ["4", 3], ["-1", 2], ["-2", 1], ["-3", 0], ["-4", null], ["5", null],
    ["0", null], ["", null], ["1.5", null], ["1e2", null]] as const)("resolves %s among four edits", (value, expected) => {
    expect(editHistoryIndex(value, 4)).toBe(expected);
  });

  it("keeps all edits grouped rather than treating cumulative diffs as a net patch", () => {
    const summary = allEditsSummary(turns);
    expect(summary).toMatchObject({ added: 4, files: [{ path: "same.txt", added: 4, diff: null }] });
    const container = document.createElement("div"); const root = createRoot(container);
    act(() => root.render(<ChangeReview summary={summary} history={turns} path={null} showAll toolbar={null} onShowAll={vi.fn()} />));
    expect(container.querySelectorAll(".change-review-history-group")).toHaveLength(4);
    expect(container.textContent).toContain("第 1 次编辑");
    expect(container.textContent).toContain("第 4 次编辑");
    expect(container.textContent).toContain("edit 1");
    expect(container.textContent).toContain("edit 4");
    act(() => root.unmount());
  });
});

describe("independent Git scopes and edit history", () => {
  it("selects a Git scope and supports keyboard dismissal", () => {
    const view = setup();
    view.click("变更范围");
    expect(document.querySelectorAll('[role="menuitemradio"]')).toHaveLength(6);
    view.click("已暂存");
    expect(view.onSelect).toHaveBeenLastCalledWith({ source: "git", scope: "staged", path: null, showAll: true });
    expect(document.querySelector('[role="menu"]')).toBeNull();
    view.click("变更范围");
    act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(document.querySelector('[role="menu"]')).toBeNull();
    expect(document.activeElement).toBe(view.container.querySelector('[aria-label="变更范围"]'));
    view.cleanup();
  });

  it("disables Git scopes without disabling edit history for non-repositories", () => {
    const view = setup({ ...gitInfo, available: false, head: null, branches: [], commits: [] });
    view.click("变更范围");
    const options = document.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]');
    expect(options[0].disabled).toBe(false);
    expect(Array.from(options).slice(1).every((option) => option.disabled)).toBe(true);
    view.click("编辑记录");
    view.click("全部");
    expect(view.onSelect).toHaveBeenLastCalledWith({ summaryId: ALL_EDITS, path: null, showAll: true });
    view.cleanup();
  });

  it("offers all, previous edits and signed numeric addressing, rejecting out-of-range input", () => {
    const view = setup();
    for (const [label, id] of [["全部", ALL_EDITS], ["前一次", "edit-3"], ["前二次", "edit-2"], ["前三次", "edit-1"]]) {
      view.click("编辑记录"); view.click(label);
      expect(view.onSelect).toHaveBeenLastCalledWith({ summaryId: id, path: null, showAll: true });
    }
    const fill = (value: string) => act(() => {
      const input = document.querySelector<HTMLInputElement>('[aria-label="编辑序号"]')!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    view.click("编辑记录"); fill("-4"); view.click("查看");
    expect(document.querySelector('[role="alert"]')?.textContent).toContain("序号超出范围");
    fill("-1"); view.click("查看");
    expect(view.onSelect).toHaveBeenLastCalledWith({ summaryId: "edit-3", path: null, showAll: true });
    view.click("编辑记录"); fill("1"); view.click("查看");
    expect(view.onSelect).toHaveBeenLastCalledWith({ summaryId: "edit-1", path: null, showAll: true });
    view.cleanup();
  });
});
