/* @vitest-environment jsdom */

import { act, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useDocumentSelectionListeners } from "./DocumentWorkspace";

function SelectionListenerHarness({
  active,
  onCapture,
}: {
  active: boolean;
  onCapture: () => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  useDocumentSelectionListeners(rootRef, onCapture, active);
  return active ? <div ref={rootRef} data-testid="document-scroll" /> : null;
}

describe("document selection listeners", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      callback(0);
      return 1;
    });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  });

  it("binds after the document scroll container appears", () => {
    const onCapture = vi.fn();

    act(() => root.render(<SelectionListenerHarness active={false} onCapture={onCapture} />));
    expect(container.querySelector('[data-testid="document-scroll"]')).toBeNull();

    act(() => root.render(<SelectionListenerHarness active onCapture={onCapture} />));
    const documentScroll = container.querySelector<HTMLElement>('[data-testid="document-scroll"]')!;
    act(() => documentScroll.dispatchEvent(new MouseEvent("mouseup", { bubbles: true })));

    expect(onCapture).toHaveBeenCalledOnce();
  });

  it("removes listeners when leaving document view", () => {
    const onCapture = vi.fn();

    act(() => root.render(<SelectionListenerHarness active onCapture={onCapture} />));
    const documentScroll = container.querySelector<HTMLElement>('[data-testid="document-scroll"]')!;
    act(() => root.render(<SelectionListenerHarness active={false} onCapture={onCapture} />));
    act(() => documentScroll.dispatchEvent(new MouseEvent("mouseup", { bubbles: true })));

    expect(onCapture).not.toHaveBeenCalled();
  });
});
