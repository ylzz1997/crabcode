/* @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
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
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.clearAllMocks();
    vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it("renders before configuration loads and lets users open settings while installation is pending", async () => {
    let resolveSettings!: (settings: DesktopSettings) => void;
    let rejectInstall!: (error: Error) => void;
    vi.mocked(loadSettings).mockImplementation(() => new Promise((resolve) => { resolveSettings = resolve; }));
    vi.mocked(ensureLocalGateway).mockImplementation(() => new Promise((_resolve, reject) => { rejectInstall = reject; }));
    await act(async () => root.render(<App />));
    expect(container.querySelector(".desktop-status-bar")?.textContent).toContain("正在读取桌面配置");

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

    await act(async () => container.querySelector<HTMLButtonElement>(".workspace-settings-button")!.click());
    expect(container.querySelector(".settings-shell")).not.toBeNull();
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
});
