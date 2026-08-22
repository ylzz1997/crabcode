/* @vitest-environment jsdom */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DocumentToolbarNumberEditor } from "./DocumentWorkspace";

function changeInput(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("document toolbar number editor", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it("enters edit mode on double click and commits with Enter", () => {
    const onCommit = vi.fn();
    act(() => root.render(
      <DocumentToolbarNumberEditor
        ariaLabel="缩放百分比"
        className="document-zoom"
        displayValue="140%"
        editValue="140"
        min={60}
        max={250}
        step={1}
        suffix="%"
        onCommit={onCommit}
      />,
    ));

    act(() => container.querySelector<HTMLElement>("[role=button]")?.dispatchEvent(
      new MouseEvent("dblclick", { bubbles: true }),
    ));
    const input = container.querySelector<HTMLInputElement>("input")!;
    expect(input.value).toBe("140");
    expect(document.activeElement).toBe(input);

    act(() => changeInput(input, "137"));
    act(() => input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })));

    expect(onCommit).toHaveBeenCalledWith("137");
    expect(container.querySelector("input")).toBeNull();
  });

  it("cancels editing with Escape", () => {
    const onCommit = vi.fn();
    act(() => root.render(
      <DocumentToolbarNumberEditor
        ariaLabel="当前页"
        className="document-page-indicator"
        displayValue="2 / 25"
        editValue="2"
        min={1}
        max={25}
        step={1}
        suffix=" / 25"
        onCommit={onCommit}
      />,
    ));

    act(() => container.querySelector<HTMLElement>("[role=button]")?.dispatchEvent(
      new MouseEvent("dblclick", { bubbles: true }),
    ));
    const input = container.querySelector<HTMLInputElement>("input")!;
    act(() => changeInput(input, "18"));
    act(() => input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));

    expect(onCommit).not.toHaveBeenCalled();
    expect(container.querySelector("input")).toBeNull();
  });
});
