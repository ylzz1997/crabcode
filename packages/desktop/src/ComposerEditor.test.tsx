/* @vitest-environment jsdom */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ComposerEditor, extractComposerText, type ComposerReferenceOption } from "./ComposerEditor";

const references: ComposerReferenceOption[] = [
  { key: "file:1", kind: "file", label: "notes.md", detail: "文件 · 2 KB" },
  { key: "folder:1", kind: "folder", label: "src", detail: "/work/src" },
];

function placeCaretAtEnd(node: Node) {
  const range = document.createRange();
  range.selectNodeContents(node);
  range.collapse(false);
  const selection = window.getSelection()!;
  selection.removeAllRanges();
  selection.addRange(range);
}

describe("ComposerEditor mentions", () => {
  let container: HTMLDivElement;
  let root: Root;
  let onChange: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    onChange = vi.fn();
    act(() => root.render(
      <ComposerEditor
        value=""
        references={references}
        placeholder="输入任务"
        onChange={onChange}
        onSubmit={vi.fn()}
      />,
    ));
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  function openFileMention() {
    const editor = container.querySelector<HTMLElement>(".composer-editor-input")!;
    editor.textContent = "请看 @not";
    placeCaretAtEnd(editor.firstChild!);
    act(() => editor.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: "t" })));
    return editor;
  }

  it("opens filtered completion after @ and inserts a capsule", () => {
    const editor = openFileMention();
    const options = container.querySelectorAll<HTMLButtonElement>('.composer-mention-menu [role="option"]');
    expect(options).toHaveLength(1);
    expect(options[0].textContent).toContain("notes.md");

    act(() => options[0].dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));

    expect(editor.querySelector(".composer-inline-mention")?.textContent).toContain("notes.md");
    expect(extractComposerText(editor)).toBe("请看 @notes.md");
    expect(onChange).toHaveBeenLastCalledWith("请看 @notes.md");
  });

  it("removes an inserted capsule with its x button", () => {
    const editor = openFileMention();
    const option = container.querySelector<HTMLButtonElement>('.composer-mention-menu [role="option"]')!;
    act(() => option.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
    const remove = editor.querySelector<HTMLButtonElement>("[data-remove-mention]")!;

    act(() => remove.click());

    expect(editor.querySelector(".composer-inline-mention")).toBeNull();
    expect(extractComposerText(editor)).toBe("请看 ");
  });

  it("removes an inserted capsule with Backspace", () => {
    const editor = openFileMention();
    const option = container.querySelector<HTMLButtonElement>('.composer-mention-menu [role="option"]')!;
    act(() => option.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));

    act(() => editor.dispatchEvent(new KeyboardEvent("keydown", { key: "Backspace", bubbles: true, cancelable: true })));

    expect(editor.querySelector(".composer-inline-mention")).toBeNull();
    expect(extractComposerText(editor)).toBe("请看 ");
  });
});
