/* @vitest-environment jsdom */

import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import { FileEditSummaryCard } from "./FileEditSummaryCard";
import type { FileEditSummaryData } from "./fileEditSummary";
import type { ChatItem } from "./types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const item: ChatItem = {
  id: "summary",
  kind: "file_edit_summary",
  title: "已编辑 7 个文件",
  status: "complete",
  detail: {
    added: 81,
    removed: 28,
    files: [
      { path: "packages/desktop/src-tauri/src/gateway.rs", action: "modify", added: 1, removed: 1, diff: null },
      { path: "packages/desktop/src/SettingsView.tsx", action: "modify", added: 51, removed: 24, diff: null },
      { path: "packages/desktop/src/styles.css", action: "modify", added: 22, removed: 2, diff: null },
      { path: "packages/desktop/src/events.ts", action: "modify", added: 1, removed: 1, diff: null },
      { path: "packages/desktop/src/App.tsx", action: "modify", added: 2, removed: 0, diff: null },
      { path: "packages/desktop/src/ChangeReview.tsx", action: "modify", added: 2, removed: 0, diff: null },
      { path: "packages/desktop/src/types.ts", action: "modify", added: 2, removed: 0, diff: null },
    ],
  },
};

function render(onOpenReview = vi.fn(), summaryItem = item) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  act(() => {
    root.render(<FileEditSummaryCard item={summaryItem} onOpenReview={onOpenReview} />);
  });
  return {
    container,
    unmount() {
      act(() => root.unmount());
      container.remove();
    },
  };
}

describe("file edit summary card", () => {
  it("expands all remaining files in place while the header opens the full review", () => {
    const onOpenReview = vi.fn();
    const view = render(onOpenReview);
    expect(view.container.querySelector(".file-edit-summary-review")?.hasAttribute("aria-pressed")).toBe(false);
    expect(view.container.querySelectorAll(".file-edit-summary-path")).toHaveLength(3);
    expect(view.container.textContent).toContain("再显示 4 个文件");
    expect(view.container.textContent).not.toContain("显示全部变更");

    act(() => view.container.querySelector<HTMLButtonElement>(".file-edit-summary-review")!.click());
    act(() => view.container.querySelector<HTMLButtonElement>(".file-edit-summary-file")!.click());
    act(() => view.container.querySelector<HTMLButtonElement>(".file-edit-summary-more")!.click());

    expect(onOpenReview).toHaveBeenNthCalledWith(1, { path: null, all: true });
    expect(onOpenReview).toHaveBeenNthCalledWith(2, { path: "packages/desktop/src-tauri/src/gateway.rs", all: false });
    expect(onOpenReview).toHaveBeenCalledTimes(2);
    expect(view.container.querySelectorAll(".file-edit-summary-path")).toHaveLength(7);
    expect(view.container.querySelector(".file-edit-summary-more")).toBeNull();
    act(() => view.container.querySelectorAll<HTMLButtonElement>(".file-edit-summary-file")[6].click());
    expect(onOpenReview).toHaveBeenLastCalledWith({ path: "packages/desktop/src/types.ts", all: false });
    expect(view.container.querySelector(".file-edit-summary-review")?.hasAttribute("aria-pressed")).toBe(false);
    view.unmount();
  });

  it.each([1, 3])("omits the footer when all %i files are already visible", (count) => {
    const detail = item.detail as FileEditSummaryData;
    const view = render(vi.fn(), { ...item, detail: { ...detail, files: detail.files.slice(0, count) } });
    expect(view.container.querySelectorAll(".file-edit-summary-file")).toHaveLength(count);
    expect(view.container.querySelector(".file-edit-summary-more")).toBeNull();
    expect(view.container.textContent).not.toContain("显示全部变更");
    view.unmount();
  });
});
