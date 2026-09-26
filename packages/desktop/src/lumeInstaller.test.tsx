/* @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { VirtualMachineSettings } from "./VirtualMachineSettings";
import { useLumeInstaller, type LumeInstallerState } from "./lumeInstaller";
import { getLumeInstallStatus, installLume, type LumeInstallProgress, type LumeInstallStatus } from "./native";
import { DEFAULT_VM_CONFIG } from "./virtualMachine";
import type { DesktopSettings } from "./types";

vi.mock("./native", () => ({ isDesktopShell: () => true, getLumeInstallStatus: vi.fn(), installLume: vi.fn() }));
const missing: LumeInstallStatus = { supported: true, available: false, version: null, path: null, reason: "尚未安装 Lume" };
const installed: LumeInstallStatus = { supported: true, available: true, version: "0.5.3", path: "/Users/test/.local/bin/lume", reason: null };
const settings = { computer_use_environment: "local_vm", computer_use_vm: DEFAULT_VM_CONFIG } as DesktopSettings;
let container: HTMLDivElement;
let root: Root;
let controller: LumeInstallerState;
const onInstalled = vi.fn();
function Harness({ show = true, taskBusy = false }: { show?: boolean; taskBusy?: boolean }) {
  controller = useLumeInstaller(onInstalled);
  return show ? <VirtualMachineSettings settings={settings} lumeInstaller={controller} taskBusy={taskBusy} onChange={vi.fn()} /> : <span>其他页面</span>;
}
function button(text: string) { return [...container.querySelectorAll("button")].find(b => b.textContent === text)!; }
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.resetAllMocks();
  vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)");
  vi.mocked(getLumeInstallStatus).mockResolvedValue(missing);
  container = document.createElement("div"); document.body.appendChild(container); root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("Lume install settings", () => {
  it("shows progress across page changes and prevents concurrent attempts", async () => {
    let finish!: (result: LumeInstallStatus) => void;
    let progress!: (next: LumeInstallProgress) => void;
    vi.mocked(installLume).mockImplementation(onProgress => {
      progress = onProgress!;
      return new Promise(resolve => { finish = resolve; });
    });
    await act(async () => root.render(<Harness />));
    expect(button("一键安装 Lume").disabled).toBe(false);
    await act(async () => { button("一键安装 Lume").click(); });
    await act(async () => { void controller.install(); });
    expect(installLume).toHaveBeenCalledOnce();
    expect(button("正在安装…").disabled).toBe(true);
    expect(button("后台启动").disabled).toBe(true);
    act(() => progress({ operationId: "one", stage: "installing", detail: "正在下载并安装 Lume", percent: 35 }));
    expect(container.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("35");
    act(() => root.render(<Harness show={false} />));
    expect(controller.busy).toBe(true);
    act(() => root.render(<Harness />));
    expect(container.textContent).toContain("正在下载并安装 Lume");
    await act(async () => finish(installed));
    expect(container.querySelector(".vm-engine-badge")?.textContent).toContain("已安装");
    expect(button("一键安装 Lume")).toBeUndefined();
    expect(container.textContent).toContain("已就绪 · 0.5.3");
    expect(onInstalled).toHaveBeenCalledOnce();
  });

  it("retains failure feedback across page changes and allows retry", async () => {
    vi.mocked(installLume).mockRejectedValueOnce(new Error("下载失败，请检查网络")).mockResolvedValueOnce(installed);
    await act(async () => root.render(<Harness />));
    await act(async () => button("一键安装 Lume").click());
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("下载失败");
    act(() => root.render(<Harness show={false} />));
    await act(async () => root.render(<Harness />));
    expect(button("重试安装 Lume").disabled).toBe(false);
    await act(async () => button("重试安装 Lume").click());
    expect(installLume).toHaveBeenCalledTimes(2);
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(container.querySelector(".vm-engine-badge")?.textContent).toContain("已安装");
    expect(button("一键安装 Lume")).toBeUndefined();
  });

  it("does not report success for an unusable install", async () => {
    vi.mocked(installLume).mockResolvedValue({ ...missing, reason: "安装后版本验证失败" });
    await act(async () => root.render(<Harness />));
    await act(async () => button("一键安装 Lume").click());
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("版本验证失败");
    expect(onInstalled).not.toHaveBeenCalled();
  });

  it("disables installation on unsupported hosts and during tasks", async () => {
    await act(async () => root.render(<Harness taskBusy />));
    expect(button("一键安装 Lume").disabled).toBe(true);
    vi.mocked(getLumeInstallStatus).mockResolvedValue({ ...missing, supported: false, reason: "需要 Apple Silicon Mac" });
    await act(async () => controller.refresh());
    act(() => root.render(<Harness />));
    expect(button("一键安装 Lume").disabled).toBe(true);
    expect(container.textContent).toContain("需要 Apple Silicon Mac");
    expect(installLume).not.toHaveBeenCalled();
  });
});
