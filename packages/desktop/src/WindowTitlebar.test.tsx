/* @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "./i18n";
import { WindowTitlebar } from "./WindowTitlebar";

const shell = vi.hoisted(() => ({ desktop: true, maximized: false, fullscreen: false, focused: true }));
const appWindow = vi.hoisted(() => ({
  minimize: vi.fn().mockResolvedValue(undefined),
  toggleMaximize: vi.fn().mockResolvedValue(undefined),
  close: vi.fn().mockResolvedValue(undefined),
  isMaximized: vi.fn(async () => shell.maximized),
  isFullscreen: vi.fn(async () => shell.fullscreen),
  isFocused: vi.fn(async () => shell.focused),
  onResized: vi.fn(),
  onFocusChanged: vi.fn(),
}));
vi.mock("@tauri-apps/api/window", () => ({ getCurrentWindow: () => appWindow }));
vi.mock("./native", () => ({ isDesktopShell: () => shell.desktop }));

describe("integrated window titlebar", () => {
  let container: HTMLDivElement;
  let root: Root;
  const resizeUnlisten = vi.fn();
  const focusUnlisten = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    Object.assign(shell, { desktop: true, maximized: false, fullscreen: false, focused: true });
    vi.spyOn(navigator, "platform", "get").mockReturnValue("Win32");
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue("Windows");
    appWindow.onResized.mockResolvedValue(resizeUnlisten);
    appWindow.onFocusChanged.mockResolvedValue(focusUnlisten);
    setLocale("zh-CN");
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  });

  it("connects the Windows controls to the shell and restores a maximized window", async () => {
    await act(async () => root.render(<WindowTitlebar />));
    const titlebar = container.querySelector("header")!;
    expect(titlebar.dataset.platform).toBe("windows");
    expect(titlebar.hasAttribute("data-tauri-drag-region")).toBe(true);
    expect(container.querySelector("button")?.hasAttribute("data-tauri-drag-region")).toBe(false);
    expect(container.querySelectorAll(".window-controls button")).toHaveLength(3);

    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="最小化"]')!.click());
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="最大化"]')!.click());
    expect(appWindow.minimize).toHaveBeenCalledOnce();
    expect(appWindow.toggleMaximize).toHaveBeenCalledOnce();

    shell.maximized = true;
    await act(async () => appWindow.onResized.mock.calls[0][0]());
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="还原窗口"]')!.click());
    expect(appWindow.toggleMaximize).toHaveBeenCalledTimes(2);
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="关闭窗口"]')!.click());
    expect(appWindow.close).toHaveBeenCalledOnce();

    shell.focused = false;
    await act(async () => appWindow.onFocusChanged.mock.calls[0][0]());
    expect(titlebar.dataset.focused).toBe("false");
  });

  it("leaves macOS controls native and updates the inset when entering fullscreen", async () => {
    vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue("Macintosh");
    await act(async () => root.render(<WindowTitlebar />));
    const titlebar = container.querySelector("header")!;
    expect(titlebar.dataset.platform).toBe("macos");
    expect(titlebar.dataset.fullscreen).toBe("false");
    expect(document.documentElement.dataset.windowPlatform).toBe("macos");
    expect(titlebar.textContent).toBe("");
    expect(container.querySelector(".window-controls")).toBeNull();
    shell.fullscreen = true;
    await act(async () => appWindow.onResized.mock.calls[0][0]());
    expect(titlebar.dataset.fullscreen).toBe("true");
    expect(document.documentElement.dataset.windowFullscreen).toBe("true");
  });

  it("does not expose native controls or call window APIs in the browser preview", async () => {
    shell.desktop = false;
    await act(async () => root.render(<WindowTitlebar />));
    expect(container.querySelector("header")?.dataset.platform).toBe("browser");
    expect(container.querySelector("[data-tauri-drag-region]")).toBeNull();
    expect(container.querySelector(".window-controls")).toBeNull();
    expect(appWindow.isMaximized).not.toHaveBeenCalled();
  });

  it("removes listeners even if they finish registering after unmount", async () => {
    let finishResize!: (unlisten: () => void) => void;
    appWindow.onResized.mockImplementationOnce(() => new Promise((resolve) => { finishResize = resolve; }));
    await act(async () => root.render(<WindowTitlebar />));
    act(() => root.unmount());
    expect(focusUnlisten).toHaveBeenCalledOnce();
    await act(async () => finishResize(resizeUnlisten));
    expect(resizeUnlisten).toHaveBeenCalledOnce();
    root = createRoot(container);
  });
});
