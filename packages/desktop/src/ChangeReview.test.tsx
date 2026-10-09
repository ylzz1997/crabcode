/* @vitest-environment jsdom */

import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import { ChangeReview, ChangeReviewFiles } from "./ChangeReview";
import { diffRows } from "./diffRows";
import type { FileEditSummaryData } from "./fileEditSummary";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const summary: FileEditSummaryData = {
  added: 2,
  removed: 1,
  files: [
    {
      path: "packages/desktop/src/styles.css",
      action: "modify",
      added: 1,
      removed: 1,
      diff: [
        "--- a/packages/desktop/src/styles.css",
        "+++ b/packages/desktop/src/styles.css",
        "@@ -10701,3 +10701,3 @@",
        " .settings-loading-status {",
        "-  display: block;",
        "+  display: flex;",
        " }",
      ].join("\n"),
    },
    {
      path: "packages/desktop/src-tauri/src/gateway.rs",
      action: "modify",
      added: 1,
      removed: 0,
      diff: "@@ -1,1 +1,2 @@\n fn main() {\n+    ready();\n }",
    },
  ],
};

describe("change review diff", () => {
  it("does not invent a trailing context line or discard content resembling file headers", () => {
    const rows = diffRows("--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n--- old\n+++ new\n");
    expect(rows).toEqual([
      { kind: "remove", text: "-- old", oldNumber: 1, newNumber: null, gap: 0 },
      { kind: "add", text: "++ new", oldNumber: null, newNumber: 1, gap: 0 },
    ]);
    expect(diffRows("@@ -0,0 +1 @@\n+new file\n")).toHaveLength(1);
  });
  it("collapses the untouched lines before a hunk", () => {
    const rows = diffRows(summary.files[0].diff);
    expect(rows[0]).toMatchObject({ kind: "gap", gap: 10700 });
    expect(rows.filter((row) => row.kind === "add")).toHaveLength(1);
    expect(rows.find((row) => row.kind === "remove")?.oldNumber).toBe(10702);
  });
});

describe("change review panel", () => {
  it("opens one file and can switch to another from the tree", () => {
    const onSelectFile = vi.fn();
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    act(() => {
      root.render(
        <>
          <ChangeReview
            summary={summary}
            toolbar={null}
            path="packages/desktop/src/styles.css"
            showAll={false}
            onShowAll={vi.fn()}
          />
          <ChangeReviewFiles
            files={summary.files}
            selectedPath={summary.files[0].path}
            onSelectFile={onSelectFile}
            onShowAll={vi.fn()}
          />
        </>,
      );
    });

    expect(container.querySelector(".change-review-diff")).not.toBeNull();
    expect(container.textContent).toContain("10700 行未修改");
    expect(container.textContent).toContain("display: flex;");
    expect(container.textContent).not.toContain("fn main()");

    const gateway = Array.from(container.querySelectorAll<HTMLButtonElement>(".change-review-tree-file"))
      .find((button) => button.textContent?.includes("gateway.rs"));
    act(() => gateway!.click());
    expect(onSelectFile).toHaveBeenCalledWith("packages/desktop/src-tauri/src/gateway.rs");

    act(() => root.unmount());
    container.remove();
  });

  it("lists every file when showing all changes", () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    act(() => {
      root.render(
        <ChangeReview
          summary={summary}
          toolbar={null}
          path={null}
          showAll
          onShowAll={vi.fn()}
        />,
      );
    });
    expect(container.textContent).toContain("styles.css");
    expect(container.textContent).toContain("fn main()");
    act(() => root.unmount());
    container.remove();
  });
});
