import { describe, expect, it } from "vitest";
import { getToolPresentation, parseChecklistResult } from "./toolPresentation";

describe("tool card presentations", () => {
  it("builds a readable file edit summary and structured fields", () => {
    const card = getToolPresentation("Edit", {
      file_path: "src/App.tsx",
      old_string: "before",
      new_string: "after",
      replace_all: false,
    });
    expect(card).toMatchObject({ kind: "file", label: "编辑文件", summary: "src/App.tsx" });
    expect(card.fields.map((field) => field.label)).toEqual(["文件", "全部替换", "替换前", "替换后"]);
  });

  it("keeps unknown plugin tools on the generic fallback", () => {
    const card = getToolPresentation("acme.custom_tool", { target: "demo" });
    expect(card).toMatchObject({ known: false, kind: "generic", label: "acme.custom_tool" });
    expect(card.fields[0]).toMatchObject({ label: "target", value: "demo" });
  });

  it("parses checklist output into progress cards", () => {
    expect(parseChecklistResult("Checklist created:\n  📋 Release\n  ✅ 1. Build\n  ◻ 2. Ship\n  (1/2 completed)"))
      .toEqual([{ title: "Release", items: [{ text: "Build", checked: true }, { text: "Ship", checked: false }], done: 1, total: 2 }]);
  });
});
