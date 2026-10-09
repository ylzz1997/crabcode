import { describe, expect, it } from "vitest";
import { summarizeFileEdits } from "./fileEditSummary";
import type { ChatItem } from "./types";

const diff = [
  "--- /work/project/src/App.tsx",
  "+++ /work/project/src/App.tsx",
  "@@ -1,2 +1,3 @@",
  " context",
  "-old",
  "+new",
  "+extra",
].join("\n");

describe("file edit summary", () => {
  it("aggregates edit, write, and patch results for the current turn", () => {
    const items: ChatItem[] = [
      { id: "old", kind: "tool", title: "Edit", status: "complete", result: "Updated /work/project/old.ts [+1, -1]" },
      { id: "user", kind: "user", text: "继续" },
      {
        id: "edit",
        kind: "tool",
        title: "Edit",
        status: "complete",
        result: `Updated /work/project/src/App.tsx (lines 1-2) [+9, -1]\n${diff}`,
      },
      {
        id: "write",
        kind: "tool",
        title: "Write",
        status: "complete",
        result: "Created /work/project/src/new.ts (4 lines).",
      },
      {
        id: "patch",
        kind: "tool",
        title: "apply_patch",
        status: "complete",
        input: {
          patch: [
            "*** Begin Patch",
            "*** Update File: src/a.ts",
            "@@",
            "-old",
            "+new",
            "*** Add File: src/b.ts",
            "+one",
            "*** End Patch",
          ].join("\n"),
        },
        result: "Applied patch to 2 files [+2/-1]",
      },
      { id: "failed", kind: "tool", title: "Edit", status: "complete", isError: true, result: "Updated /work/project/src/nope.ts [+1, -1]" },
    ];

    expect(summarizeFileEdits(items.slice(1), "/work/project")).toEqual({
      added: 15,
      removed: 2,
      files: [
        { path: "src/App.tsx", action: "modify", added: 9, removed: 1, diff },
        { path: "src/new.ts", action: "create", added: 4, removed: 0, diff: null },
        { path: "src/a.ts", action: "modify", added: 1, removed: 1, diff: null },
        { path: "src/b.ts", action: "create", added: 1, removed: 0, diff: null },
      ],
    });
  });

  it("keeps one row when the same file is reported by the tool and a file change", () => {
    const items: ChatItem[] = [
      { id: "edit", kind: "tool", title: "Edit", status: "complete", result: "Updated /work/project/src/App.tsx [+2, -1]" },
      { id: "change", kind: "file_change", path: "/work/project/src/App.tsx", action: "modify", diff, status: "complete" },
    ];
    expect(summarizeFileEdits(items, "/work/project")).toEqual({
      added: 2,
      removed: 1,
      files: [{ path: "src/App.tsx", action: "modify", added: 2, removed: 1, diff }],
    });
  });

  it("sums repeated edits of the same file", () => {
    const items: ChatItem[] = [
      { id: "one", kind: "tool", title: "Edit", status: "complete", result: "Updated src/App.tsx [+1, -1]" },
      { id: "two", kind: "tool", title: "Edit", status: "complete", result: "Updated src/App.tsx [+4, -0]" },
    ];
    expect(summarizeFileEdits(items, "/work/project")?.files[0]).toMatchObject({
      path: "src/App.tsx",
      added: 5,
      removed: 1,
    });
  });
});
