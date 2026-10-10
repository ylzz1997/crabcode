/* @vitest-environment jsdom */

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { SlidingTabIndicator } from "./SlidingTabIndicator";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it.each(["document-mode", ""])("preserves and positions the conversation slider in %s", (mode) => {
  (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  let resize: () => void = () => {};
  const disconnect = vi.fn();
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) { resize = callback; }
    observe() {}
    disconnect = disconnect;
  });
  vi.spyOn(HTMLElement.prototype, "offsetLeft", "get").mockImplementation(function (this: HTMLElement) {
    return Number(this.dataset.left ?? 0);
  });
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockImplementation(function (this: HTMLElement) {
    return Number(this.dataset.width ?? 0);
  });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const render = (view: string) => act(() => root.render(
    <main className={`main-panel ${mode}`}>
      <div className="conversation-header">
        <div className="conversation-view-tabs" role="tablist">
          <SlidingTabIndicator activeKey={view} className="conversation-view-indicator" />
          <button role="tab" aria-selected={view === "chat"} data-left="3" data-width="45">对话</button>
          <button role="tab" aria-selected={view === "trajectory"} data-left="50" data-width="49">轨迹</button>
        </div>
      </div>
    </main>,
  ));

  try {
    render("chat");
    const indicator = container.querySelector<HTMLElement>(".conversation-view-indicator")!;
    expect(indicator.style.transform).toBe("translateX(3px)");
    expect(indicator.style.width).toBe("45px");
    render("trajectory");
    expect(container.querySelector(".conversation-view-indicator")).toBe(indicator);
    expect(indicator.style.transform).toBe("translateX(50px)");
    expect(indicator.style.width).toBe("49px");

    const trajectory = container.querySelector<HTMLElement>('[aria-selected="true"]')!;
    trajectory.dataset.left = "42";
    trajectory.dataset.width = "41";
    act(() => resize());
    expect(indicator.style.transform).toBe("translateX(42px)");
    expect(indicator.style.width).toBe("41px");
    render("chat");
    expect(indicator.style.transform).toBe("translateX(3px)");
  } finally {
    act(() => root.unmount());
    container.remove();
  }
  expect(disconnect).toHaveBeenCalled();
});
