/* @vitest-environment jsdom */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import {
  ComposerEditor,
  composerModifierLabel,
  createComposerCommandOptions,
  extractComposerText,
  isMacPlatform,
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
  let onChange: Mock<(value: string) => void>;
  let onImages: Mock<(files: File[]) => void>;

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

  function pasteClipboard(
    items: Array<{ kind: string; type: string; getAsFile: () => File | null }>,
    files: File[],
    text = "",
  ) {
    const editor = container.querySelector<HTMLElement>(".composer-editor-input")!;
    const paste = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(paste, "clipboardData", {
      value: { items, files, getData: () => text },
    });
    act(() => editor.dispatchEvent(paste));
    return paste;
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

  it("attaches one image when items and files expose different File objects", () => {
    const image = new File(["image"], "clipboard.png", { type: "image/png", lastModified: 1 });
    const wrapper = new File([image], image.name, { type: image.type, lastModified: 2 });

    const paste = pasteClipboard([{ kind: "file", type: image.type, getAsFile: () => image }], [wrapper]);

    expect(paste.defaultPrevented).toBe(true);
    expect(onImages).toHaveBeenCalledExactlyOnceWith([image]);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("preserves separate images with identical metadata in clipboard order", () => {
    const options = { type: "image/png", lastModified: 1 };
    const images = [new File(["first"], "image.png", options), new File(["other"], "image.png", options)];
    const wrappers = images.map((image) => new File([image], image.name, options));

    pasteClipboard(images.map((image) => ({ kind: "file", type: image.type, getAsFile: () => image })), wrappers);

    expect(onImages).toHaveBeenCalledExactlyOnceWith(images);
    expect(onImages.mock.calls[0][0][0]).toBe(images[0]);
    expect(onImages.mock.calls[0][0][1]).toBe(images[1]);
  });

  it.each([false, true])("falls back to files when readable image items are unavailable (null item: %s)", (nullItem) => {
    const image = new File(["image"], "clipboard.png", { type: "image/png" });
    const items = nullItem ? [{ kind: "file", type: image.type, getAsFile: () => null }] : [];

    const paste = pasteClipboard(items, [image]);

    expect(paste.defaultPrevented).toBe(true);
    expect(onImages).toHaveBeenCalledExactlyOnceWith([image]);
  });

  it("allows the same image to be pasted again in a separate action", () => {
    const image = new File(["image"], "clipboard.png", { type: "image/png" });
    for (let i = 0; i < 2; i++) {
      pasteClipboard([{ kind: "file", type: image.type, getAsFile: () => image }], [image]);
    }

    expect(onImages).toHaveBeenCalledTimes(2);
    expect(onImages.mock.calls.map(([files]) => files)).toEqual([[image], [image]]);
  });

  it("preserves selected draft text when the clipboard has no supported content", () => {
    const editor = container.querySelector<HTMLElement>(".composer-editor-input")!;
    editor.textContent = "keep this draft";
    const range = document.createRange();
    range.selectNodeContents(editor);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);

    pasteClipboard([], []);

    expect(extractComposerText(editor)).toBe("keep this draft");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("replaces selected draft text with plain text", () => {
    const editor = container.querySelector<HTMLElement>(".composer-editor-input")!;
    editor.textContent = "replace this";
    const range = document.createRange();
    range.selectNodeContents(editor);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);

    const paste = pasteClipboard([], [], "<b>plain text</b>\nnext line");

    expect(paste.defaultPrevented).toBe(true);
    expect(editor.textContent).toBe("<b>plain text</b>\nnext line");
    expect(editor.querySelector("b")).toBeNull();
    expect(onChange).toHaveBeenLastCalledWith("<b>plain text</b>\nnext line");
  });

  it.each([false, true])("does not modify a selection outside the composer (crossing boundary: %s)", (crossing) => {
    const editor = container.querySelector<HTMLElement>(".composer-editor-input")!;
    editor.textContent = "keep this draft";
    const outside = document.createElement("p");
    outside.textContent = "keep this message";
    container.append(outside);
    const range = document.createRange();
    range.selectNodeContents(outside);
    if (crossing) range.setStart(editor.firstChild!, 0);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);

    pasteClipboard([], [], "pasted text");

    expect(extractComposerText(editor)).toBe("keep this draft");
    expect(outside.textContent).toBe("keep this message");
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("ComposerEditor send shortcuts", () => {
  let container: HTMLDivElement;
  let root: Root;
  let onSubmit: Mock<() => void>;

  beforeEach(() => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    onSubmit = vi.fn();
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  function render(sendKey?: "enter" | "mod_enter", busy = false) {
    act(() => root.render(
      <ComposerEditor
        value=""
        references={references}
        placeholder="输入任务"
        sendKey={sendKey}
        busy={busy}
        onChange={vi.fn()}
        onSubmit={onSubmit}
      />,
    ));
    return container.querySelector<HTMLElement>(".composer-editor-input")!;
  }

  it("sends with Enter by default and does not require a modifier", () => {
    const editor = render();
    act(() => editor.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })));
    expect(onSubmit).toHaveBeenCalledOnce();
  });

  it.each(["enter", "mod_enter"] as const)("uses Enter/default and modifier/opposite while busy regardless of %s", (sendKey) => {
    const editor = render(sendKey, true);
    act(() => editor.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })));
    expect(onSubmit).toHaveBeenLastCalledWith(false);
    const modifier = isMacPlatform() ? { metaKey: true } : { ctrlKey: true };
    act(() => editor.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", ...modifier, bubbles: true, cancelable: true })));
    expect(onSubmit).toHaveBeenLastCalledWith(true);
    onSubmit.mockClear();
    act(() => editor.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", shiftKey: true, ...modifier, bubbles: true, cancelable: true })));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(editor.querySelector("br")).not.toBeNull();
    act(() => editor.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true })));
    act(() => editor.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", ...modifier, bubbles: true, cancelable: true })));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("does not send while an IME composition is active", () => {
    const editor = render();
    act(() => editor.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true })));
    act(() => editor.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("does not send the trailing Enter immediately after compositionend", () => {
    const editor = render();
    act(() => editor.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true })));
    act(() => editor.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true })));
    act(() => editor.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("inserts a line break with the system modifier in Enter-send mode", () => {
    const editor = render("enter");
    const modifier = isMacPlatform() ? { metaKey: true } : { ctrlKey: true };
    act(() => editor.dispatchEvent(new KeyboardEvent("keydown", {
      key: "Enter",
      ...modifier,
      bubbles: true,
      cancelable: true,
    })));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(editor.querySelector("br")).not.toBeNull();
  });

  it("sends with Ctrl/Cmd+Enter when configured", () => {
    const editor = render("mod_enter");
    act(() => editor.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })));
    expect(onSubmit).not.toHaveBeenCalled();
    const modifier = isMacPlatform() ? { metaKey: true } : { ctrlKey: true };
    act(() => editor.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", ...modifier, bubbles: true, cancelable: true })));
    expect(onSubmit).toHaveBeenCalledOnce();
  });

  it("inserts a visible line break when Enter is not the send key", () => {
    const editor = render("mod_enter");
    act(() => editor.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(editor.querySelector("br")).not.toBeNull();
  });

  it("uses Command on macOS and Ctrl elsewhere", () => {
    const originalPlatform = navigator.platform;
    Object.defineProperty(navigator, "platform", { configurable: true, value: "MacIntel" });
    expect(isMacPlatform()).toBe(true);
    expect(composerModifierLabel()).toBe("Command+Enter");
    Object.defineProperty(navigator, "platform", { configurable: true, value: "Linux x86_64" });
    expect(isMacPlatform()).toBe(false);
    expect(composerModifierLabel()).toBe("Ctrl+Enter");
    Object.defineProperty(navigator, "platform", { configurable: true, value: originalPlatform });
  });
});

describe("ComposerEditor slash commands", () => {
  let container: HTMLDivElement;
  let root: Root;
  let onChange: Mock<(value: string) => void>;
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
