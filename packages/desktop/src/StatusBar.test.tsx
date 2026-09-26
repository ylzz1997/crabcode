/* @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StatusBar } from "./StatusBar";
import { updateGatewayStartup } from "./gatewayStartup";
import type { ComputerUseState } from "./computerUse";
import { initialComputerUseState } from "./computerUse";
import type { ConnectionPreset, GatewayViewState } from "./types";

const connection = { id: "local", name: "Local", base_url: "http://127.0.0.1:4096" } as ConnectionPreset;
const connecting = { status: "connecting", error: null } as GatewayViewState;

describe("desktop status bar", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-16T12:00:00Z"));
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
  });

  it("keeps showing elapsed time while waiting for installer output, then stops on success", () => {
    let startup = updateGatewayStartup(undefined, "checking_python", "正在检测 Python 环境");
    act(() => root.render(<StatusBar connection={connection} gateway={connecting} startup={startup} />));
    act(() => vi.advanceTimersByTime(65_000));
    expect(container.querySelector(".status-elapsed")?.textContent).toBe("1 分 5 秒");
    startup = updateGatewayStartup(startup, "installing", "Downloading crabcode");
    act(() => root.render(<StatusBar connection={connection} gateway={connecting} startup={startup} />));
    act(() => container.querySelector<HTMLButtonElement>(".status-current")!.click());
    expect(container.querySelector(".startup-log")?.textContent).toContain("正在检测 Python 环境");
    expect(container.querySelector(".startup-log")?.textContent).toContain("Downloading crabcode");
    startup = updateGatewayStartup(startup, "online", "Gateway 已连接，工作区就绪");
    act(() => root.render(<StatusBar connection={connection} gateway={{ ...connecting, status: "online" }} startup={startup} />));
    expect(container.querySelector('[role="status"]')?.textContent).toBe("就绪");
    expect(container.querySelector(".spin")).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
    act(() => vi.advanceTimersByTime(10_000));
    expect(container.querySelector(".startup-details header")?.textContent).toContain("耗时 1 分 5 秒");
  });

  it("retains the failure and log, exposes retry, and supports Escape", () => {
    const onRetry = vi.fn();
    const startup = updateGatewayStartup(undefined, "error", "Python 3.10 or newer was not found");
    act(() => root.render(<StatusBar connection={connection} gateway={{ ...connecting, status: "error", error: startup.detail }} startup={startup} onRetry={onRetry} />));
    expect(container.querySelector('[role="status"]')?.textContent).toContain("Python 3.10");
    act(() => container.querySelector<HTMLButtonElement>(".status-retry")!.click());
    expect(onRetry).toHaveBeenCalledOnce();
    act(() => container.querySelector<HTMLButtonElement>(".status-current")!.click());
    expect(container.querySelector(".startup-log")?.textContent).toContain(startup.detail);
    act(() => container.querySelector("footer")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(container.querySelector(".startup-details")).toBeNull();
    expect(document.activeElement).toBe(container.querySelector(".status-current"));
  });

  it("shows configuration loading and a readable configuration error", () => {
    act(() => root.render(<StatusBar loading />));
    expect(container.querySelector('[role="status"]')?.textContent).toBe("正在读取桌面配置…");
    act(() => root.render(<StatusBar error="配置文件不可读" />));
    expect(container.querySelector('[role="status"]')?.textContent).toBe("配置文件不可读");
    expect(container.querySelector(".spin")).toBeNull();
  });

  it("bounds log history and starts a clean history on retry", () => {
    let startup = updateGatewayStartup(undefined, "installing", "first", 0);
    for (let i = 1; i <= 150; i++) startup = updateGatewayStartup(startup, "installing", `line ${i}`, i);
    expect(startup.history).toHaveLength(100);
    expect(startup.history[0].detail).toBe("line 51");
    expect(updateGatewayStartup(startup, "installing", "line 150").history).toHaveLength(100);
    expect(updateGatewayStartup(undefined, "connecting", "retry", 200).startedAt).toBe(200);
  });

  it("shows limited input capability, the cursor, and lets the user disable Computer Use", () => {
    const onEnabledChange = vi.fn();
    const onOpenInputSettings = vi.fn();
    const onRefresh = vi.fn();
    const computerUse: ComputerUseState = {
      hostId: "desktop-test",
      enabled: true,
      active: true,
      mode: "background_app",
      status: "ready",
      capabilities: {
        gui_available: true,
        input_available: false,
        platform: "macos",
        displays: [],
        supported_modes: ["background_app", "foreground_desktop"],
        reason: "Desktop input permission is unavailable",
      },
      previews: [{
        key: "session:session-one:agent:main",
        sessionId: "session-one",
        mode: "background_app",
        status: "ready",
        action: "click",
        summary: "Clicked",
        frame: {
          data: "cG5n",
          media_type: "image/png",
          width: 100,
          height: 50,
          origin_x: 10,
          origin_y: 20,
          frame_id: "frame-1",
        },
        cursor: { x: 60, y: 45 },
        updatedAt: Date.now(),
      }],
      logs: [{ id: "action-1", time: Date.now(), action: "click", summary: "Clicked", ok: true }],
      error: null,
    };
    act(() => root.render(
      <StatusBar
        computerUse={computerUse}
        onComputerUseEnabledChange={onEnabledChange}
        onComputerUseOpenInputSettings={onOpenInputSettings}
        onComputerUseRefresh={onRefresh}
      />,
    ));
    expect(container.querySelector(".status-computer-use > svg")).toBeNull();
    act(() => container.querySelector<HTMLButtonElement>(".status-computer-use")!.click());
    expect(container.querySelector(".computer-use-console")?.textContent).toContain("有限可用");
    expect(container.querySelector(".computer-use-permission")?.textContent).toContain("需要开启辅助功能权限");
    act(() => container.querySelectorAll<HTMLButtonElement>(".computer-use-permission-actions button")[0].click());
    expect(onOpenInputSettings).toHaveBeenCalledOnce();
    act(() => container.querySelectorAll<HTMLButtonElement>(".computer-use-permission-actions button")[1].click());
    expect(onRefresh).toHaveBeenCalledOnce();
    expect(container.querySelector(".computer-use-console")?.textContent).toContain("Clicked");
    const cursor = container.querySelector<SVGElement>(".computer-use-cursor")!;
    expect(cursor.style.left).toBe("60%");
    expect(cursor.style.top).toBe("90%");
    act(() => container.querySelector<HTMLButtonElement>(".computer-use-power")!.click());
    expect(onEnabledChange).toHaveBeenCalledWith(false);
    act(() => root.render(
      <StatusBar
        computerUse={{ ...computerUse, active: false, previews: [] }}
        onComputerUseEnabledChange={onEnabledChange}
        onComputerUseOpenInputSettings={onOpenInputSettings}
        onComputerUseRefresh={onRefresh}
      />,
    ));
    expect(container.querySelector(".computer-use-console")).toBeNull();
  });

  it("distinguishes strict background input unavailability from macOS permissions", () => {
    const computerUse: ComputerUseState = {
      ...initialComputerUseState("desktop-test", true),
      active: true,
      status: "ready",
      deliveryPolicy: "strict_background",
      capabilities: {
        gui_available: true, input_available: true, platform: "macos", displays: [],
        supported_modes: ["background_app", "foreground_desktop"],
        delivery_policy_version: 1, strict_background_input_available: false,
      },
    };
    act(() => root.render(<StatusBar computerUse={computerUse} />));
    act(() => container.querySelector<HTMLButtonElement>(".status-computer-use")!.click());
    expect(container.querySelector(".computer-use-console")?.textContent).toContain("有限可用");
    expect(container.querySelector(".computer-use-mode")?.textContent).toContain("严格后台");
    expect(container.querySelector(".computer-use-warning")?.textContent).toContain("尚不支持严格后台输入");
    expect(container.querySelector(".computer-use-permission")).toBeNull();
    act(() => root.render(<StatusBar computerUse={{ ...computerUse, deliveryPolicy: "allow_foreground" }} />));
    expect(container.querySelector(".computer-use-warning")).toBeNull();
    expect(container.querySelector(".computer-use-mode")?.textContent).toContain("允许前台");
  });

  it("labels AX observations and a retained screenshot without calling it the current view", () => {
    const computerUse: ComputerUseState = {
      ...initialComputerUseState("desktop-test", true), status: "ready", active: true,
      capabilities: { gui_available: true, input_available: true, platform: "macos", displays: [],
        supported_modes: ["background_app"], capture_available: false, ax_available: true },
      previews: [{ key: "s", sessionId: "s", mode: "background_app", status: "ready", action: "observe",
        summary: "Observed accessibility tree", observationKind: "ax", axElementCount: 12,
        frame: { data: "AAAA", media_type: "image/png", width: 10, height: 10, origin_x: 0, origin_y: 0, frame_id: "f" },
        frameUpdatedAt: Date.now() - 60000, cursor: null, updatedAt: Date.now() }],
    };
    act(() => root.render(<StatusBar computerUse={computerUse} />));
    act(() => container.querySelector<HTMLButtonElement>(".status-computer-use")!.click());
    expect(container.textContent).toContain("辅助功能 · 12 个元素");
    expect(container.textContent).toContain("本次模型读取 AX Tree");
    expect(container.textContent).toContain("录屏不可用时仍可读取和操作界面元素");
    expect(container.querySelector(".computer-use-frame img")?.getAttribute("alt")).toContain("最近一次");
  });

  it("shows the current configured policy while keeping preview policy as action history", () => {
    const computerUse: ComputerUseState = {
      ...initialComputerUseState("desktop-test", true),
      active: true,
      status: "ready",
      previews: [{
        key: "session:s:agent:main", sessionId: "s", mode: "background_app",
        deliveryPolicy: "allow_foreground", status: "ready", action: "click",
        summary: "Clicked", frame: null, cursor: null, updatedAt: Date.now(),
      }],
    };
    const config = {
      cwd: "/workspace", snapshot_enabled: true, snapshot_max_size_mb: 1024,
      computer_use_target_scope: "app_window" as const,
      computer_use_delivery_policy: "strict_background" as const,
      extra_tools: [], extra_tools_by_source: {}, sources: [], warnings: [],
    };
    act(() => root.render(<StatusBar computerUse={computerUse} computerUseConfig={config} />));
    act(() => container.querySelector<HTMLButtonElement>(".status-computer-use")!.click());
    expect(container.querySelector(".computer-use-mode")?.textContent).toBe("指定窗口 · 严格后台");
    expect(container.querySelector(".computer-use-preview-card header")?.textContent).toContain("操作时：指定窗口 · 允许前台");

    act(() => root.render(<StatusBar computerUse={computerUse} computerUseConfig={{
      ...config, computer_use_target_scope: "desktop", computer_use_delivery_policy: "allow_foreground",
    }} />));
    expect(container.querySelector(".computer-use-mode")?.textContent).toBe("整个桌面 · 允许前台");
  });

  it("keeps the monitor and its detail screenshot-only during AX observations", () => {
    const preview = { key: "ax", sessionId: "session", mode: "background_app" as const, status: "ready" as const,
      action: "observe", summary: "Observed accessibility tree", observationKind: "ax" as const,
      axElementCount: 3, frame: null, cursor: null, updatedAt: Date.now() };
    let computerUse: ComputerUseState = { ...initialComputerUseState("desktop-test", true),
      status: "ready", active: true, previews: [preview] };
    act(() => root.render(<StatusBar computerUse={computerUse} />));
    act(() => container.querySelector<HTMLButtonElement>(".status-computer-use")!.click());
    expect(container.textContent).toContain("暂无截图");
    expect(container.querySelector(".computer-use-preview pre")).toBeNull();
    const frame = { data: "AAAA", media_type: "image/png", width: 10, height: 10, origin_x: 0, origin_y: 0, frame_id: "f" };
    computerUse = { ...computerUse, previews: [{ ...preview, frame }] };
    act(() => root.render(<StatusBar computerUse={computerUse} />));
    expect(container.querySelector(".computer-use-preview img")?.getAttribute("src")).toBe("data:image/png;base64,AAAA");
    act(() => container.querySelector<HTMLButtonElement>(".computer-use-preview-content")!.click());
    expect(document.body.querySelector(".computer-use-detail pre")).toBeNull();
    expect(document.body.querySelector(".computer-use-detail img")).not.toBeNull();
    const updated = { ...computerUse.previews[0], frame: { ...frame, data: "BBBB" } };
    computerUse = { ...computerUse, previews: [updated] };
    act(() => root.render(<StatusBar computerUse={computerUse} />));
    expect(document.body.querySelector(".computer-use-detail img")?.getAttribute("src")).toBe("data:image/png;base64,BBBB");
    computerUse = { ...computerUse, previews: [{ ...updated, observationKind: "ax_and_screenshot" }] };
    act(() => root.render(<StatusBar computerUse={computerUse} />));
    expect(container.querySelector(".computer-use-preview img")).not.toBeNull();
    expect(document.body.querySelector(".computer-use-detail img")).not.toBeNull();
    expect(document.body.querySelector(".computer-use-detail pre")).toBeNull();
  });

  it.each([
    { label: "background window at a positive screen origin", mode: "background_app", origin: [320, 180], cursor: { x: 25, y: 15 }, expected: ["25%", "30%"] },
    { label: "background window on a display with a negative origin", mode: "background_app", origin: [-1600, -900], cursor: { x: 25, y: 15 }, expected: ["25%", "30%"] },
    { label: "foreground desktop at a positive screen origin", mode: "foreground_desktop", origin: [320, 180], cursor: { x: 345, y: 195 }, expected: ["25%", "30%"] },
    { label: "foreground desktop on a display with a negative origin", mode: "foreground_desktop", origin: [-1600, -900], cursor: { x: -1575, y: -885 }, expected: ["25%", "30%"] },
  ] as const)("positions the cursor in both preview sizes for a $label", ({ mode, origin, cursor, expected }) => {
    const computerUse: ComputerUseState = {
      hostId: "desktop-test",
      enabled: true,
      active: true,
      mode: "background_app",
      status: "ready",
      capabilities: null,
      previews: [{
        key: "session:cursor-test:agent:main",
        sessionId: "cursor-test",
        mode,
        status: "ready",
        action: "click",
        summary: "Clicked",
        frame: {
          data: "cG5n", media_type: "image/png", width: 100, height: 50,
          origin_x: origin[0], origin_y: origin[1], frame_id: "cursor-frame",
        },
        cursor,
        updatedAt: Date.now(),
      }],
      logs: [],
      error: null,
    };

    act(() => root.render(<StatusBar computerUse={computerUse} />));
    act(() => container.querySelector<HTMLButtonElement>(".status-computer-use")!.click());
    const thumbnailCursor = container.querySelector<SVGElement>(".computer-use-frame .computer-use-cursor");
    expect(thumbnailCursor?.style.left).toBe(expected[0]);
    expect(thumbnailCursor?.style.top).toBe(expected[1]);

    act(() => container.querySelector<HTMLButtonElement>(".computer-use-preview-content")!.click());
    const detailCursor = document.body.querySelector<SVGElement>(".computer-use-detail-frame .computer-use-cursor");
    expect(detailCursor?.style.left).toBe(expected[0]);
    expect(detailCursor?.style.top).toBe(expected[1]);
  });

  it("renders simultaneous Computer Use sessions as separate previews", () => {
    const preview = (sessionId: string, frameId: string) => ({
      key: `session:${sessionId}:agent:main`,
      sessionId,
      mode: "background_app" as const,
      status: "ready" as const,
      action: "observe",
      summary: "Observed application window",
      frame: { data: "cG5n", media_type: "image/png", width: 100, height: 50, origin_x: 0, origin_y: 0, frame_id: frameId },
      cursor: null,
      updatedAt: Date.now(),
    });
    const computerUse: ComputerUseState = {
      hostId: "desktop-test",
      enabled: true,
      active: true,
      mode: "background_app",
      status: "ready",
      capabilities: null,
      previews: [preview("session-one", "frame-one"), preview("session-two", "frame-two")],
      logs: [
        { id: "one", time: Date.now(), action: "observe", summary: "first session", ok: true, sessionId: "session-one" },
        { id: "two", time: Date.now(), action: "click", summary: "second session", ok: true, sessionId: "session-two" },
      ],
      error: null,
    };

    act(() => root.render(<StatusBar computerUse={computerUse} />));
    act(() => container.querySelector<HTMLButtonElement>(".status-computer-use")!.click());
    expect(container.querySelectorAll(".computer-use-preview-card")).toHaveLength(2);
    expect(container.querySelector(".computer-use-console")?.textContent).toContain("2 个活动预览");
    expect(container.querySelectorAll<HTMLImageElement>(".computer-use-frame img")[0].alt).toContain("session-o");
    expect(container.querySelectorAll<HTMLImageElement>(".computer-use-frame img")[1].alt).toContain("session-t");

    const firstPreview = container.querySelectorAll<HTMLButtonElement>(".computer-use-preview-content")[0];
    act(() => firstPreview.click());
    const detail = document.body.querySelector<HTMLElement>('[role="dialog"][aria-label*="Computer Use 详情"]')!;
    expect(detail.textContent).toContain("session-one");
    expect(detail.textContent).toContain("first session");
    expect(detail.textContent).not.toContain("second session");
    expect(detail.querySelector<HTMLImageElement>(".computer-use-detail-frame img")?.alt).toContain("session-one");
    expect(document.activeElement).toBe(detail.querySelector('[aria-label="关闭 Computer Use 详情"]'));
    act(() => detail.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(document.body.querySelector(".computer-use-detail-backdrop")).toBeNull();
    expect(container.querySelector(".computer-use-console")).not.toBeNull();
    expect(document.activeElement).toBe(firstPreview);
  });
});
