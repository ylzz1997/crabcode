import { setLocale } from "./i18n";
/* @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { ComputerUseChannel } from "./computerUse";
import { GatewayApi, SessionChannel } from "./gateway";
import { ensureLocalGateway, loadSettings, normalizeSettings } from "./native";
import type { DesktopSettings } from "./types";

vi.mock("./native", async (importOriginal) => {
  const original = await importOriginal<typeof import("./native")>();
  return {
    ...original,
    isDesktopShell: () => true,
    loadSettings: vi.fn(),
    saveSettings: vi.fn().mockResolvedValue(undefined),
    setDockIcon: vi.fn().mockResolvedValue(undefined),
    ensureLocalGateway: vi.fn(),
  };
});

describe("application startup feedback", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    setLocale("zh-CN");
    vi.spyOn(navigator, "languages", "get").mockReturnValue(["zh-CN"]);
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.clearAllMocks();
    vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
    HTMLElement.prototype.scrollIntoView = vi.fn();
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
  });

  it("renders before configuration loads and lets users open settings while installation is pending", async () => {
    let resolveSettings!: (settings: DesktopSettings) => void;
    let rejectInstall!: (error: Error) => void;
    vi.mocked(loadSettings).mockImplementation(() => new Promise((resolve) => { resolveSettings = resolve; }));
    vi.mocked(ensureLocalGateway).mockImplementation(() => new Promise((_resolve, reject) => { rejectInstall = reject; }));
    await act(async () => root.render(<App />));
    expect(container.querySelector(".desktop-status-bar")?.textContent).toContain("正在读取桌面配置");
    expect(container.querySelectorAll(".window-titlebar")).toHaveLength(1);

    await act(async () => resolveSettings(normalizeSettings({
      schema_version: 4,
      active_connection_id: "local",
      connection_order: ["local"],
      sidebar_width: 280,
      python_path: null,
      connections: [{ id: "local", name: "Local", base_url: "http://127.0.0.1:4096", projects: [] }],
    } as unknown as DesktopSettings)));
    const onProgress = vi.mocked(ensureLocalGateway).mock.calls[0][4]!;
    act(() => onProgress({ connectionId: "local", operationId: "test", stage: "installing", detail: "正在安装 CrabCode、Browser 和 Chromium，首次启动可能需要几分钟" }));
    expect(container.querySelector(".desktop-status-bar")?.textContent).toContain("正在安装 CrabCode、Browser 和 Chromium");
    expect(container.querySelector<HTMLButtonElement>('button[title="重新连接"]')?.disabled).toBe(true);
    expect(container.querySelector(".workspace-brand .sidebar-toggle")).not.toBeNull();
    expect(container.querySelectorAll(".workspace-brand-copy strong")).toHaveLength(1);
    expect(container.querySelector(".window-titlebar .workspace-connections .gateway-tab")?.textContent).toBe("Local");
    expect(container.querySelector(".sidebar .workspace-connections")).toBeNull();
    const secondaryNav = container.querySelector<HTMLElement>("#workspace-secondary-nav")!;
    expect(container.querySelector(".workbench")?.getAttribute("data-sidebar-mode")).toBe("full");

    expect(secondaryNav.hidden).toBe(false);
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="收起导航"]')!.click());
    expect(container.querySelector(".workbench")?.getAttribute("data-sidebar-mode")).toBe("compact");
    expect(secondaryNav.hidden).toBe(true);
    expect(container.querySelector(".workspace-nav-primary .workspace-nav-item")?.textContent).toBe("新会话");
    expect(container.querySelector(".sidebar .projects-section")).not.toBeNull();
    expect(container.querySelector(".sidebar .sessions-section")).not.toBeNull();
    await act(async () => container.querySelector<HTMLButtonElement>('button[title="隐藏侧栏"]')!.click());
    expect(container.querySelector(".sidebar.closed")).not.toBeNull();
    expect(container.querySelector(".workbench")?.getAttribute("data-sidebar-mode")).toBe("hidden");
    expect(container.querySelector(".sidebar.closed")?.hasAttribute("inert")).toBe(true);
    expect(container.querySelector(".window-titlebar .gateway-tab")?.textContent).toBe("Local");
    await act(async () => container.querySelector<HTMLButtonElement>('button[title="显示侧栏"]')!.click());
    expect(container.querySelector(".sidebar.open")).not.toBeNull();
    expect(secondaryNav.hidden).toBe(true);
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="展开导航"]')!.click());
    expect(secondaryNav.hidden).toBe(false);
    expect(container.querySelector(".workbench")?.getAttribute("data-sidebar-mode")).toBe("full");

    await act(async () => secondaryNav.querySelector<HTMLButtonElement>("button")!.click());
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="隐藏侧栏"]')!.click());
    expect(container.querySelector(".main-panel .sidebar-restore-floating .sidebar-expand-button")).not.toBeNull();
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="显示侧栏"]')!.click());

    const prompt = vi.spyOn(window, "prompt");
    act(() => container.querySelector(".gateway-tab")!.dispatchEvent(new MouseEvent("dblclick", { bubbles: true })));
    expect(prompt).not.toHaveBeenCalled();
    await act(async () => container.querySelector<HTMLButtonElement>(".status-connection")!.click());
    expect(container.querySelector(".modal-divider")?.textContent).toBe("新建远程连接");
    expect(container.querySelector('.saved-connection-row [title="编辑连接"]')).toBeNull();
    await act(async () => container.querySelector<HTMLButtonElement>('.modal button[title="关闭"]')!.click());

    await act(async () => container.querySelector<HTMLButtonElement>(".workspace-settings-button")!.click());
    expect(container.querySelector(".settings-shell")).not.toBeNull();
    expect(container.querySelectorAll(".window-titlebar")).toHaveLength(1);
    expect(container.querySelector(".window-titlebar")?.textContent).toBe("");
    expect(container.querySelector(".settings-sidebar .settings-back")).not.toBeNull();
    expect(container.querySelector(".desktop-status-bar")?.textContent).toContain("正在安装 CrabCode、Browser 和 Chromium");

    await act(async () => rejectInstall(new Error("安装失败：无法下载依赖")));
    expect(container.querySelector(".desktop-status-bar.error")?.textContent).toContain("无法下载依赖");
    await act(async () => container.querySelector<HTMLButtonElement>(".status-retry")!.click());
    expect(ensureLocalGateway).toHaveBeenCalledTimes(2);
    expect(container.querySelector(".desktop-status-bar.busy")).not.toBeNull();
    expect(container.querySelector(".desktop-status-bar")?.textContent).not.toContain("无法下载依赖");
  });

  it("shows a configuration error instead of an endless loading screen", async () => {
    vi.mocked(loadSettings).mockRejectedValue(new Error("配置文件不可读"));
    await act(async () => root.render(<App />));
    expect(container.querySelector(".boot")?.textContent).toBe("无法加载桌面配置");
    expect(container.querySelector(".desktop-status-bar.error")?.textContent).toContain("配置文件不可读");
    expect(container.querySelector(".spin")).toBeNull();
    expect(ensureLocalGateway).not.toHaveBeenCalled();
  });

  it("restores the sidebar from the left of the conversation title without changing the session", async () => {
    vi.mocked(loadSettings).mockResolvedValue(normalizeSettings({
      active_connection_id: "local", connection_order: ["local"], sidebar_width: 280,
      connections: [{ id: "local", name: "Local", base_url: "http://127.0.0.1:4096", projects: [{
        id: "project", kind: "project", name: "Project", path: "/work/project", directories: ["/work/project"],
        last_session_id: null,
      }] }],
    } as unknown as DesktopSettings));
    vi.mocked(ensureLocalGateway).mockResolvedValue({
      ready: true, started_by_desktop: false, python: null, version: null, message: "ready",
    });
    vi.spyOn(GatewayApi.prototype, "authenticate").mockResolvedValue(undefined);
    vi.spyOn(GatewayApi.prototype, "workspaceInfo").mockResolvedValue({
      startup_cwd: "/work/project", home: "/work", browse_roots: ["/work"],
    });
    vi.spyOn(GatewayApi.prototype, "sessions").mockResolvedValue([]);
    vi.spyOn(GatewayApi.prototype, "models").mockResolvedValue([]);
    vi.spyOn(GatewayApi.prototype, "schedules").mockResolvedValue([]);
    vi.spyOn(GatewayApi.prototype, "skills").mockResolvedValue([]);
    vi.spyOn(GatewayApi.prototype, "tools").mockResolvedValue([]);
    vi.spyOn(SessionChannel.prototype, "connect").mockResolvedValue(undefined);
    vi.spyOn(ComputerUseChannel.prototype, "connect").mockResolvedValue(undefined);
    await act(async () => root.render(<App />));
    await act(async () => container.querySelector<HTMLButtonElement>(".workspace-nav-primary .workspace-nav-item")!.click());
    expect(container.querySelector(".conversation-title h1")?.textContent).toBe("新会话");
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="隐藏侧栏"]')!.click());
    const heading = container.querySelector(".conversation-heading")!;
    expect(heading.firstElementChild?.getAttribute("aria-label")).toBe("显示侧栏");
    expect(heading.lastElementChild?.classList.contains("conversation-title")).toBe(true);
    expect(container.querySelector(".sidebar-restore-floating")).toBeNull();
    await act(async () => heading.querySelector<HTMLButtonElement>("button")!.click());
    expect(container.querySelector(".sidebar.open")).not.toBeNull();
    expect(container.querySelector(".conversation-heading .sidebar-expand-button")).toBeNull();
    expect(container.querySelector(".conversation-title span")?.textContent).toBe("/work/project");
  });
});
