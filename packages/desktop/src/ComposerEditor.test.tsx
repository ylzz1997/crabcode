/* @vitest-environment jsdom */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ComposerEditor,
  createComposerCommandOptions,
  extractComposerText,
  resolveComposerCommandCompletion,
  type ComposerReferenceOption,
} from "./ComposerEditor";

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
  let onImages: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    onChange = vi.fn();
    onImages = vi.fn();
    act(() => root.render(
      <ComposerEditor
        value=""
        references={references}
        placeholder="输入任务"
        onChange={onChange}
        onImages={onImages}
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

  it("attaches an image pasted from the clipboard", () => {
    const editor = container.querySelector<HTMLElement>(".composer-editor-input")!;
    const image = new File(["image"], "clipboard.png", { type: "image/png" });
    const paste = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(paste, "clipboardData", {
      value: {
        items: [{ kind: "file", type: "image/png", getAsFile: () => image }],
        files: [image],
        getData: () => "",
      },
    });

    act(() => editor.dispatchEvent(paste));

    expect(paste.defaultPrevented).toBe(true);
    expect(onImages).toHaveBeenCalledWith([image]);
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("ComposerEditor slash commands", () => {
  let container: HTMLDivElement;
  let root: Root;
  let onChange: ReturnType<typeof vi.fn>;
  const commands = createComposerCommandOptions(
    [{ name: "sonnet", description: "Fast model" }],
    [{ name: "release", description: "Prepare a release" }],
  );

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
        commands={commands}
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

  function typeCommand(text: string) {
    const editor = container.querySelector<HTMLElement>(".composer-editor-input")!;
    editor.textContent = text;
    placeCaretAtEnd(editor);
    act(() => editor.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText" })));
    return editor;
  }

  it("includes dynamic models and skills in completion sources", () => {
    expect(resolveComposerCommandCompletion("/rel", commands)?.items.map((item) => item.name)).toEqual(["/release"]);
    expect(resolveComposerCommandCompletion("/model so", commands)?.items.map((item) => item.name)).toEqual(["sonnet"]);
  });

  it("can limit built-ins to commands implemented by the host while retaining skills", () => {
    const desktopCommands = createComposerCommandOptions(
      [{ name: "sonnet" }],
      [{ name: "release", description: "Prepare a release" }],
      new Set(["/plan", "/model"]),
    );
    expect(desktopCommands.filter((item) => item.kind === "command").map((item) => item.name)).toEqual(["/plan", "/model"]);
    expect(desktopCommands.find((item) => item.kind === "skill")?.name).toBe("/release");
  });

  it("completes a command and then its subcommand with the keyboard", () => {
    const editor = typeCommand("/sch");
    expect(container.querySelector(".composer-command-menu")?.textContent).toContain("/schedule");

    act(() => editor.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true })));
    expect(extractComposerText(editor)).toBe("/schedule ");
    expect(container.querySelector(".composer-command-heading")?.textContent).toContain("/schedule");

    typeCommand("/schedule pa");
    act(() => editor.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })));
    expect(extractComposerText(editor)).toBe("/schedule pause ");
    expect(onChange).toHaveBeenLastCalledWith("/schedule pause ");
  });
});
