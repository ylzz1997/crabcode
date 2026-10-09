/* @vitest-environment jsdom */

import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  filterSettingsSections,
  SettingsView,
  type SettingsSectionId,
} from "./SettingsView";
import { composerModifierLabel } from "./ComposerEditor";
import { RuntimeSettingsPanel } from "./RuntimeSettingsPanel";
import { BUILTIN_THEMES } from "./theme";
import type { DocumentEngineInstallProgress, GatewayInstallFeature } from "./native";
import { installedGatewayFeatures } from "./native";
import type {
  DesktopSettings,
  DocumentCapabilities,
  GatewayViewState,
  ModelSettingsResponse,
  RuntimeSettingsResponse,
} from "./types";

vi.mock("./native", async (importOriginal) => {
  const original = await importOriginal<typeof import("./native")>();
  return {
    ...original,
    installedGatewayFeatures: vi.fn(() => Promise.resolve([])),
  };
});

const settings: DesktopSettings = {
  schema_version: 4,
  computer_use_enabled: true,
  active_connection_id: "local",
  connection_order: ["local", "remote"],
  connections: [
    {
      id: "local",
      name: "Local",
      base_url: "http://127.0.0.1:4096",
      credential_ref: null,
      allow_insecure_remote: false,
      document_workspace_root: null,
      projects: [{
        id: "crab",
        kind: "project",
        path: "/work/crabcode",
        name: "CrabCode",
        directories: ["/work/crabcode", "/work/shared"],
        last_session_id: null,
      }],
      last_project_path: "/work/crabcode",
      last_project_id: "crab",
    },
    {
      id: "remote",
      name: "Build Server",
      base_url: "https://build.example.com:4096",
      credential_ref: "gateway-remote",
      allow_insecure_remote: false,
      document_workspace_root: null,
      projects: [],
      last_project_path: null,
      last_project_id: null,
    },
  ],
  python_path: null,
  sidebar_width: 280,
  project_files_width: 640,
  project_files_max_tabs: 5,
  document_agent_width: 400,
  document_agent_collapsed: false,
  document_show_original_text: false,
  document_translation_concurrency: 3,
  document_translation_batch_size: 200,
  theme_mode: "system",
  active_theme_id: "builtin.crab",
  custom_theme_presets: [],
  pointer_cursor: true,
  ui_font_size: 14,
  code_font_size: 12,
  diff_marker_style: "color",
  font_smoothing: true,
  show_turn_duration: true,
  show_file_edit_summary: true,
  turn_duration_format: "hms",
  session_notify_on_start: true,
  session_notify_on_complete: true,
  session_notify_on_interaction: true,
  composer_send_key: "enter",
  approval_shortcuts: { enabled: true, approve: "Ctrl+Alt+Shift+F9", deny: "Ctrl+Alt+Shift+F10", always_allow: "Ctrl+Alt+Shift+F11" },
  file_upload_mode: "content",
  file_upload_max_size_mb: 5,
  dock_icon: "dark",
};

const onlineGateway: GatewayViewState = {
  status: "online",
  error: null,
  token: null,
  tokenExpiresAt: 0,
  workspace: {
    startup_cwd: "/work/crabcode",
    home: "/Users/test",
    browse_roots: ["/Users/test", "/work"],
  },
  sessionsByProject: {},
  models: [],
  runningCount: 0,
  pendingCount: 0,
};

const offlineGateway: GatewayViewState = {
  ...onlineGateway,
  status: "offline",
  workspace: null,
};

function changeInput(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("settings search", () => {
  it("matches section names, item labels, and descriptions", () => {
    expect(filterSettingsSections("Python").map((section) => section.id)).toEqual(["general"]);
    expect(filterSettingsSections("Debugger").map((section) => section.id)).toEqual(["general"]);
    expect(filterSettingsSections("Playwright").map((section) => section.id)).toEqual(["general"]);
    expect(filterSettingsSections("Ripgrep").map((section) => section.id)).toEqual(["general"]);
    expect(filterSettingsSections("凭据").map((section) => section.id)).toEqual(["connections"]);
    expect(filterSettingsSections("工作目录").map((section) => section.id)).toEqual(["projects"]);
    expect(filterSettingsSections("Dock 图标").map((section) => section.id)).toEqual(["appearance"]);
    expect(filterSettingsSections("字体平滑").map((section) => section.id)).toEqual(["appearance"]);
    expect(filterSettingsSections("对比度").map((section) => section.id)).toEqual(["appearance"]);
    expect(filterSettingsSections("处理用时").map((section) => section.id)).toEqual(["general"]);
    expect(filterSettingsSections("系统通知").map((section) => section.id)).toEqual(["general"]);
    expect(filterSettingsSections("最大标签数").map((section) => section.id)).toEqual(["general"]);
    expect(filterSettingsSections("并行请求").map((section) => section.id)).toEqual(["document"]);
    expect(filterSettingsSections("显示原文").map((section) => section.id)).toEqual(["document"]);
    expect(filterSettingsSections("快照").map((section) => section.id)).toEqual(["runtime"]);
    expect(filterSettingsSections("额外工具").map((section) => section.id)).toEqual(["runtime"]);
    expect(filterSettingsSections("提示词模版").map((section) => section.id)).toEqual(["prompts"]);
    expect(filterSettingsSections("Provider").map((section) => section.id)).toEqual(["models"]);
    expect(filterSettingsSections("配置组").map((section) => section.id)).toEqual(["models"]);
    expect(filterSettingsSections("Yuri Head").map((section) => section.id)).toEqual(["about"]);
    expect(filterSettingsSections("不存在")).toEqual([]);
  });
});

describe("SettingsView", () => {
  it("includes the usage page in settings search", () => {
    expect(filterSettingsSections("Token").some((section) => section.id === "usage")).toBe(true);
  });
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.mocked(installedGatewayFeatures).mockReset();
    vi.mocked(installedGatewayFeatures).mockResolvedValue([]);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
    vi.restoreAllMocks();
  });

  const callbacks = () => ({
    onBack: vi.fn(),
    onSavePythonPath: vi.fn(),
    onInstallGatewaySuite: vi.fn().mockResolvedValue(undefined),
    onInstallSystemTool: vi.fn().mockResolvedValue(undefined),
    onConversationChange: vi.fn(),
    onDocumentChange: vi.fn(),
    onThemeModeChange: vi.fn(),
    onThemeProfileChange: vi.fn(),
    onThemePresetChange: vi.fn(),
    onThemeDuplicate: vi.fn(),
    onThemeRename: vi.fn(),
    onThemeDelete: vi.fn(),
    onThemeRestoreDefault: vi.fn(),
    onThemeImport: vi.fn(),
    onThemeImportFailure: vi.fn(),
    onAppearanceChange: vi.fn(),
    onDockIconChange: vi.fn().mockResolvedValue(undefined),
    onActivateConnection: vi.fn(),
    onNewConnection: vi.fn(),
    onEditConnection: vi.fn(),
    onDeleteConnection: vi.fn(),
    onMutateRuntimeSettings: vi.fn().mockResolvedValue(undefined),
    onNewProject: vi.fn(),
    onEditProject: vi.fn(),
  });

  it("filters the navigation and opens the first matching section", () => {
    const handlers = callbacks();
    function Harness() {
      const [section, setSection] = useState<SettingsSectionId>("general");
      return (
        <SettingsView
          {...handlers}
          settings={settings}
          gateways={{ local: onlineGateway, remote: offlineGateway }}
          activeConnection={settings.connections[0]}
          activeProject={settings.connections[0].projects[0]}
          activeSection={section}
          onSectionChange={setSection}
        />
      );
    }

    act(() => root.render(<Harness />));
    const search = container.querySelector<HTMLInputElement>('input[aria-label="搜索设置"]')!;
    act(() => changeInput(search, "项目目录"));

    expect(container.querySelectorAll(".settings-nav button")).toHaveLength(1);
    expect(container.querySelector(".settings-nav button")?.textContent).toContain("项目");
    expect(container.querySelector(".settings-page-header h1")?.textContent).toBe("项目");
  });

  it("saves a normalized Python path when the field loses focus", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    const handlers = callbacks();
    await act(async () => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="general"
        onSectionChange={vi.fn()}
      />,
    ));

    const input = container.querySelector<HTMLInputElement>('input[aria-label="Python 路径"]')!;
    act(() => changeInput(input, "  /opt/python3  "));
    act(() => input.dispatchEvent(new FocusEvent("focusout", { bubbles: true })));

    expect(handlers.onSavePythonPath).toHaveBeenCalledWith("/opt/python3");
  });

  it("checks independent CrabCode features and installs them together", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    const handlers = callbacks();
    await act(async () => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="general"
        onSectionChange={vi.fn()}
      />,
    ));

    const search = container.querySelector<HTMLInputElement>('input[aria-label="Search"]')!;
    const debuggerOption = container.querySelector<HTMLInputElement>('input[aria-label="Debugger"]')!;
    const browser = container.querySelector<HTMLInputElement>('input[aria-label="Browser"]')!;
    expect(search.checked).toBe(true);
    expect(debuggerOption.checked).toBe(false);
    expect(browser.checked).toBe(false);
    expect(container.querySelector('[aria-label="CrabCode 安装组件"]')?.textContent).toContain("Chromium");
    expect(container.querySelector('[aria-label="CrabCode 安装组件"]')?.textContent).not.toContain("Ripgrep");
    act(() => {
      debuggerOption.click();
      browser.click();
    });
    await act(async () => {
      Array.from(container.querySelectorAll<HTMLButtonElement>("button"))
        .find((button) => button.textContent?.includes("安装套件"))!
        .click();
      await Promise.resolve();
    });

    expect(handlers.onInstallGatewaySuite).toHaveBeenCalledWith(["search", "debugger", "browser"], null);
  });

  it("blocks suite changes until detection completes and checks again after installation", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    let finishProbe!: (features: GatewayInstallFeature[]) => void;
    vi.mocked(installedGatewayFeatures).mockImplementation(() => new Promise((resolve) => { finishProbe = resolve; }));
    const handlers = callbacks();
    const renderSettings = (success: string | null = null) => root.render(
      <SettingsView {...handlers} settings={settings} gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]} activeProject={settings.connections[0].projects[0]}
        activeSection="general" onSectionChange={vi.fn()} gatewaySuiteSuccess={success} />,
    );
    act(() => renderSettings());

    const suite = container.querySelector('.gateway-suite-install')!;
    const browser = suite.querySelector<HTMLInputElement>('input[aria-label="Browser"]')!;
    const install = suite.querySelector<HTMLButtonElement>('.gateway-suite-actions button')!;
    expect(suite.querySelector('[role="status"]')?.textContent).toContain("正在检测已安装的组件");
    expect(suite.querySelector('[aria-busy="true"]')).not.toBeNull();
    expect(Array.from(suite.querySelectorAll<HTMLInputElement>('input')).every((input) => input.disabled)).toBe(true);
    expect(install.disabled).toBe(true);
    act(() => { browser.click(); install.click(); });
    expect(browser.checked).toBe(false);
    expect(handlers.onInstallGatewaySuite).not.toHaveBeenCalled();
    // Unrelated settings remain usable while this probe is pending.
    act(() => container.querySelector<HTMLButtonElement>('[aria-label="显示处理用时"]')!.click());
    expect(handlers.onConversationChange).toHaveBeenCalledWith({ show_turn_duration: false });

    await act(async () => finishProbe(["browser"]));
    expect(suite.querySelector('.settings-loading-status')).toBeNull();
    expect(browser.checked).toBe(true);
    expect(browser.disabled).toBe(true);
    expect(install.disabled).toBe(false);
    await act(async () => install.click());
    expect(handlers.onInstallGatewaySuite).toHaveBeenCalledWith(["search", "browser"], null);

    act(() => renderSettings("安装完成"));
    expect(install.disabled).toBe(true);
    expect(suite.querySelector('[aria-busy="true"]')).not.toBeNull();
    await act(async () => finishProbe(["search", "browser"]));
    expect(install.disabled).toBe(false);
    expect(suite.querySelector<HTMLInputElement>('input[aria-label="Search"]')!.disabled).toBe(true);
  });

  it("keeps failed detection locked and lets the user retry", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    vi.mocked(installedGatewayFeatures).mockRejectedValueOnce(new Error("检测超时"));
    const handlers = callbacks();
    await act(async () => root.render(
      <SettingsView {...handlers} settings={settings} gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]} activeProject={settings.connections[0].projects[0]}
        activeSection="general" onSectionChange={vi.fn()} />,
    ));

    const suite = container.querySelector('.gateway-suite-install')!;
    expect(suite.querySelector('[role="alert"]')?.textContent).toContain("组件检测失败：检测超时");
    expect(suite.querySelector('.settings-loading-status')).toBeNull();
    expect(suite.querySelector<HTMLButtonElement>('.primary')!.disabled).toBe(true);
    expect(suite.querySelector<HTMLInputElement>('input[aria-label="Browser"]')!.disabled).toBe(true);
    await act(async () => {
      Array.from(suite.querySelectorAll<HTMLButtonElement>('button'))
        .find((button) => button.textContent === "重新检测")!.click();
    });
    expect(suite.querySelector('[role="alert"]')).toBeNull();
    expect(suite.querySelector<HTMLButtonElement>('.primary')!.disabled).toBe(false);
    expect(suite.querySelector<HTMLInputElement>('input[aria-label="Browser"]')!.disabled).toBe(false);
    expect(handlers.onInstallGatewaySuite).not.toHaveBeenCalled();
  });

  it("ignores stale detection after changing Python environments", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    const probes: ((features: GatewayInstallFeature[]) => void)[] = [];
    vi.mocked(installedGatewayFeatures).mockImplementation(() => new Promise((resolve) => { probes.push(resolve); }));
    const handlers = callbacks();
    const renderSettings = (pythonPath: string | null) => root.render(
      <SettingsView {...handlers} settings={{ ...settings, python_path: pythonPath }} gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]} activeProject={settings.connections[0].projects[0]}
        activeSection="general" onSectionChange={vi.fn()} />,
    );
    act(() => renderSettings(null));
    act(() => renderSettings("/opt/new/python3"));
    expect(installedGatewayFeatures).toHaveBeenLastCalledWith("/opt/new/python3");
    await act(async () => probes[1](["debugger"]));
    await act(async () => probes[0](["browser"]));

    const browser = container.querySelector<HTMLInputElement>('input[aria-label="Browser"]')!;
    const debuggerOption = container.querySelector<HTMLInputElement>('input[aria-label="Debugger"]')!;
    expect(browser.checked).toBe(false);
    expect(browser.disabled).toBe(false);
    expect(debuggerOption.checked).toBe(true);
    expect(debuggerOption.disabled).toBe(true);

    act(() => renderSettings(null));
    expect(browser.disabled).toBe(true);
    await act(async () => probes[2]([]));
    expect(debuggerOption.checked).toBe(false);
    expect(debuggerOption.disabled).toBe(false);
  });

  it("locks installed suite features as checked and grayed out", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    vi.mocked(installedGatewayFeatures).mockResolvedValue(["browser", "search"]);
    const handlers = callbacks();
    await act(async () => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="general"
        onSectionChange={vi.fn()}
      />,
    ));

    const search = container.querySelector<HTMLInputElement>('input[aria-label="Search"]')!;
    const debuggerOption = container.querySelector<HTMLInputElement>('input[aria-label="Debugger"]')!;
    const browser = container.querySelector<HTMLInputElement>('input[aria-label="Browser"]')!;
    expect(search.checked).toBe(true);
    expect(search.disabled).toBe(true);
    expect(browser.checked).toBe(true);
    expect(browser.disabled).toBe(true);
    expect(debuggerOption.checked).toBe(false);
    expect(debuggerOption.disabled).toBe(false);
    expect(search.closest("label")?.className).toContain("is-installed");
    expect(container.textContent).toContain("已安装 · 语义代码搜索");
    expect(container.textContent).toContain("已安装 · 网页浏览");
    act(() => {
      search.click();
      debuggerOption.click();
    });
    expect(search.checked).toBe(true);
    await act(async () => {
      Array.from(container.querySelectorAll<HTMLButtonElement>("button"))
        .find((button) => button.textContent?.includes("安装套件"))!
        .click();
      await Promise.resolve();
    });

    expect(handlers.onInstallGatewaySuite).toHaveBeenCalledWith(["search", "debugger", "browser"], null);
  });

  it("installs Ripgrep from the separate system tools section", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    const handlers = callbacks();
    act(() => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="general"
        onSectionChange={vi.fn()}
      />,
    ));

    expect(container.textContent).toContain("系统工具");
    expect(container.textContent).toContain("Ripgrep (rg)");
    await act(async () => {
      container.querySelector<HTMLButtonElement>('button[aria-label="安装 Ripgrep"]')!.click();
      await Promise.resolve();
    });

    expect(handlers.onInstallSystemTool).toHaveBeenCalledWith("ripgrep", null);
    expect(handlers.onInstallGatewaySuite).not.toHaveBeenCalled();
  });

  it("controls turn duration visibility and format", () => {
    const handlers = callbacks();
    act(() => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="general"
        onSectionChange={vi.fn()}
      />,
    ));

    const seconds = Array.from(container.querySelectorAll<HTMLButtonElement>('[aria-label="处理用时格式"] button'))
      .find((button) => button.textContent?.includes("仅秒数"))!;
    act(() => seconds.click());
    act(() => container.querySelector<HTMLButtonElement>('[aria-label="显示处理用时"]')!.click());

    expect(handlers.onConversationChange).toHaveBeenNthCalledWith(1, { turn_duration_format: "seconds" });
    expect(handlers.onConversationChange).toHaveBeenNthCalledWith(2, { show_turn_duration: false });
  });

  it("toggles session start and completion notifications", () => {
    const handlers = callbacks();
    act(() => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="general"
        onSectionChange={vi.fn()}
      />,
    ));

    expect(container.textContent).toContain("会话开始执行时弹出系统通知。");
    expect(container.textContent).toContain("会话这一轮执行结束时弹出系统通知。");
    expect(container.textContent).toContain("会话请求权限、询问选择或等待确认计划时弹出系统通知。");
    act(() => container.querySelector<HTMLButtonElement>('[aria-label="执行时通知"]')!.click());
    act(() => container.querySelector<HTMLButtonElement>('[aria-label="执行完毕通知"]')!.click());
    act(() => container.querySelector<HTMLButtonElement>('[aria-label="需要操作时通知"]')!.click());

    expect(handlers.onConversationChange).toHaveBeenNthCalledWith(1, { session_notify_on_start: false });
    expect(handlers.onConversationChange).toHaveBeenNthCalledWith(2, { session_notify_on_complete: false });
    expect(handlers.onConversationChange).toHaveBeenNthCalledWith(3, { session_notify_on_interaction: false });
  });

  it("changes the composer send shortcut", () => {
    const handlers = callbacks();
    act(() => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="general"
        onSectionChange={vi.fn()}
      />,
    ));

    const modifierMode = Array.from(container.querySelectorAll<HTMLButtonElement>('[aria-label="发送快捷键"] button'))
      .find((button) => button.textContent?.includes(composerModifierLabel()))!;
    act(() => modifierMode.click());
    expect(handlers.onConversationChange).toHaveBeenCalledWith({ composer_send_key: "mod_enter" });
    const followUpButtons = container.querySelectorAll<HTMLButtonElement>('[aria-label="跟进处理方式"] button');
    expect(followUpButtons[0].getAttribute("aria-pressed")).toBe("true");
    act(() => followUpButtons[1].click());
    expect(handlers.onConversationChange).toHaveBeenCalledWith({ follow_up_mode: "steer" });
  });

  it("records approval shortcuts, rejects duplicates, restores defaults and disables them", () => {
    const handlers = callbacks();
    function Harness() {
      const [value, setValue] = useState(settings);
      return <SettingsView
        {...handlers}
        settings={value}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="general"
        onSectionChange={vi.fn()}
        onConversationChange={(changes) => {
          handlers.onConversationChange(changes);
          setValue((current) => ({ ...current, ...changes }));
        }}
      />;
    }
    act(() => root.render(<Harness />));
    expect(filterSettingsSections("权限快捷键").map((section) => section.id)).toContain("general");
    const approve = container.querySelector<HTMLButtonElement>('[aria-label="允许一次快捷键"]')!;
    const displayedKeys = () => Array.from(approve.querySelectorAll("kbd")).map((key) => key.textContent).join("+");
    act(() => approve.click());
    expect(document.activeElement).toBe(approve);
    act(() => approve.dispatchEvent(new KeyboardEvent("keydown", {
      key: "F10", code: "F10", ctrlKey: true, altKey: true, shiftKey: true, bubbles: true,
    })));
    expect(handlers.onConversationChange).not.toHaveBeenCalled();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("必须使用不同");

    act(() => approve.dispatchEvent(new KeyboardEvent("keydown", {
      key: "y", code: "KeyY", ctrlKey: true, altKey: true, bubbles: true,
    })));
    expect(handlers.onConversationChange).toHaveBeenLastCalledWith({
      approval_shortcuts: { ...settings.approval_shortcuts, approve: "Ctrl+Alt+Y" },
    });
    expect(displayedKeys()).toBe("Ctrl+Alt+Y");

    act(() => approve.click());
    const onGlobalKey = vi.fn();
    window.addEventListener("keydown", onGlobalKey);
    act(() => document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true })));
    window.removeEventListener("keydown", onGlobalKey);
    expect(onGlobalKey).not.toHaveBeenCalled();
    expect(displayedKeys()).toBe("Ctrl+Alt+Y");
    expect(handlers.onConversationChange).toHaveBeenCalledTimes(1);

    const always = container.querySelector<HTMLButtonElement>('[aria-label="始终允许快捷键"]')!;
    act(() => always.click());
    act(() => always.dispatchEvent(new KeyboardEvent("keydown", {
      key: "a", code: "KeyA", ctrlKey: true, altKey: true, bubbles: true,
    })));
    expect(handlers.onConversationChange).toHaveBeenLastCalledWith({
      approval_shortcuts: { ...settings.approval_shortcuts, approve: "Ctrl+Alt+Y", always_allow: "Ctrl+Alt+A" },
    });

    act(() => container.querySelector<HTMLButtonElement>('[aria-label="恢复默认权限快捷键"]')!.click());
    expect(displayedKeys()).toBe("Ctrl+Alt+Shift+F9");
    act(() => container.querySelector<HTMLButtonElement>('[aria-label="启用权限快捷键"]')!.click());
    expect(handlers.onConversationChange).toHaveBeenLastCalledWith({
      approval_shortcuts: { ...settings.approval_shortcuts, enabled: false },
    });
    expect(approve.disabled).toBe(true);
    expect(container.querySelector<HTMLButtonElement>('[aria-label="拒绝快捷键"]')!.disabled).toBe(true);
    expect(always.disabled).toBe(true);
  });

  it("switches file uploads between content and path mode", () => {
    const handlers = callbacks();
    act(() => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="general"
        onSectionChange={vi.fn()}
      />,
    ));

    const pathMode = Array.from(container.querySelectorAll<HTMLButtonElement>('[aria-label="文件上传方式"] button'))
      .find((button) => button.textContent?.includes("仅传路径"))!;
    act(() => pathMode.click());

    expect(handlers.onConversationChange).toHaveBeenCalledWith({ file_upload_mode: "path" });

    const maximum = container.querySelector<HTMLInputElement>('[aria-label="最大文件大小（MB）"]')!;
    act(() => changeInput(maximum, "12"));
    act(() => maximum.dispatchEvent(new FocusEvent("focusout", { bubbles: true })));
    expect(handlers.onConversationChange).toHaveBeenCalledWith({ file_upload_max_size_mb: 12 });

    const tabs = container.querySelector<HTMLInputElement>('[aria-label="文件查看最大标签数"]')!;
    act(() => changeInput(tabs, "60"));
    act(() => tabs.dispatchEvent(new FocusEvent("focusout", { bubbles: true })));
    expect(handlers.onConversationChange).toHaveBeenCalledWith({ project_files_max_tabs: 50 });
  });

  it("shows document translation controls and updates their values", () => {
    const handlers = callbacks();
    act(() => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="document"
        onSectionChange={vi.fn()}
      />,
    ));

    const navigation = Array.from(container.querySelectorAll(".settings-nav button"))
      .map((button) => button.textContent);
    expect(navigation.indexOf("文档")).toBe(navigation.indexOf("外观") + 1);
    expect(navigation[navigation.length - 1]).toBe("关于");
    expect(container.querySelector(".settings-section h2")?.textContent).toBe("翻译");
    expect(Array.from(container.querySelectorAll(".settings-section h2")).map((heading) => heading.textContent))
      .toEqual(["翻译", "选择"]);

    act(() => container.querySelector<HTMLButtonElement>('[aria-label="显示原文"]')!.click());
    const concurrency = container.querySelector<HTMLInputElement>('[aria-label="翻译并行请求数"]')!;
    const batchSize = container.querySelector<HTMLInputElement>('[aria-label="翻译单批 Block 数"]')!;
    act(() => changeInput(concurrency, "4"));
    act(() => concurrency.dispatchEvent(new FocusEvent("focusout", { bubbles: true })));
    act(() => changeInput(batchSize, "120"));
    act(() => batchSize.dispatchEvent(new FocusEvent("focusout", { bubbles: true })));

    expect(handlers.onDocumentChange).toHaveBeenCalledWith({ document_show_original_text: true });
    expect(handlers.onDocumentChange).toHaveBeenCalledWith({ document_translation_concurrency: 4 });
    expect(handlers.onDocumentChange).toHaveBeenCalledWith({ document_translation_batch_size: 120 });
  });

  it("keeps the document engine and translation controls in one group", async () => {
    const handlers = callbacks();
    const install = vi.fn().mockResolvedValue(undefined);
    const capabilities: DocumentCapabilities = {
      supported_extensions: [".pdf"],
      available_extensions: [".pdf"],
      max_bytes: 100,
      documents_dir: "/work/documents",
      libreoffice: { available: false, executable: null },
      ocr: { available: false },
      translation_engines: {
        default: "legacy",
        legacy: { available: true, status: "ready" },
        precise: {
          available: false,
          status: "not_installed",
          version: "0.6.4",
          detail: "高精度 PDF 引擎尚未安装",
          install_command: "crabcode document-engine install",
          remove_command: "crabcode document-engine remove --yes",
        },
      },
    };
    const renderSettings = (
      key: string,
      documentEngineBusy: "install" | "remove" | null = null,
      documentEngineProgress: DocumentEngineInstallProgress | null = null,
      loading = false,
    ) => root.render(
      <SettingsView
        key={key}
        {...handlers}
        settings={settings}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="document"
        onSectionChange={vi.fn()}
        documentCapabilities={loading ? undefined : capabilities}
        canManageDocumentEngine
        documentEngineBusy={documentEngineBusy}
        documentEngineProgress={documentEngineProgress}
        onInstallDocumentEngine={install}
      />,
    );
    act(() => renderSettings("loading", null, null, true));
    expect(container.querySelector('.document-engine-row [role="status"]')?.textContent).toContain("正在检测文档引擎");
    expect(container.querySelector('.document-engine-row button')).toBeNull();
    expect(container.querySelector<HTMLInputElement>('[aria-label="翻译并行请求数"]')!.disabled).toBe(false);
    act(() => renderSettings("idle"));

    const group = container.querySelector(".document-translation-group")!;
    expect(group.querySelectorAll(":scope > .settings-row")).toHaveLength(3);
    expect(group.querySelectorAll(":scope > .document-engine-row")).toHaveLength(1);
    expect(group.querySelector(".document-engine-command code")?.textContent)
      .toBe("crabcode document-engine install");
    await act(async () => {
      Array.from(group.querySelectorAll<HTMLButtonElement>("button"))
        .find((button) => button.textContent?.includes("执行安装"))!
        .click();
      await Promise.resolve();
    });
    expect(install).toHaveBeenCalledOnce();

    const installingProgress: DocumentEngineInstallProgress = {
      operationId: "install-1",
      stage: "installing",
      detail: "正在从 BabelDOC 官方源安装程序与依赖",
      percent: 36,
    };
    act(() => renderSettings("installing-first", "install", installingProgress));
    const progress = container.querySelector<HTMLElement>('[role="progressbar"]')!;
    expect(progress.getAttribute("aria-valuenow")).toBe("36");
    expect(progress.getAttribute("aria-valuetext")).toContain("安装程序与依赖");
    expect(progress.querySelector<HTMLElement>(".document-engine-progress-track i")?.style.width).toBe("36%");
    expect(progress.textContent).toContain("36%");

    act(() => renderSettings("installing-remounted", "install", installingProgress));
    expect(container.querySelector('[role="progressbar"]')?.textContent).toContain("36%");
    expect(container.textContent).toContain("正在安装…");
  });

  it("edits remote compaction, snapshot settings and extra tools by configuration layer", async () => {
    const handlers = callbacks();
    const runtimeSettings: RuntimeSettingsResponse = {
      cwd: "/work/crabcode",
      snapshot_enabled: true,
      snapshot_max_size_mb: 1024,
      auto_compact_enabled: true,
      compact_buffer_tokens: 20_000,
      max_context_length: 50_000,
      computer_use_mode: "background_app",
      computer_use_target_scope: "app_window",
      computer_use_delivery_policy: "allow_foreground",
      extra_tools: ["pkg.UserTool"],
      extra_tools_by_source: { userSettings: ["pkg.UserTool"] },
      sources: ["/Users/test/.crabcode/settings.json"],
      warnings: [],
      editable_sources: [
        { id: "userSettings", label: "用户配置", path: "/Users/test/.crabcode/settings.json", exists: true, writable: true },
        { id: "projectSettings", label: "项目配置", path: "/work/crabcode/.crabcode/settings.json", exists: false, writable: true },
      ],
    };
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    act(() => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="runtime"
        onSectionChange={vi.fn()}
        runtimeSettings={runtimeSettings}
        onRefreshRuntimeSettings={vi.fn()}
      />,
    ));

    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="自动压缩"]')!.click());
    const buffer = container.querySelector<HTMLInputElement>('[aria-label="压缩预留 token"]')!;
    act(() => changeInput(buffer, "30000"));
    await act(async () => buffer.dispatchEvent(new FocusEvent("focusout", { bubbles: true })));
    const limit = container.querySelector<HTMLInputElement>('[aria-label="提前触发阈值（tokens）"]')!;
    act(() => changeInput(limit, ""));
    await act(async () => limit.dispatchEvent(new FocusEvent("focusout", { bubbles: true })));
    const calls = handlers.onMutateRuntimeSettings.mock.calls.length;
    act(() => changeInput(buffer, "-1"));
    await act(async () => buffer.dispatchEvent(new FocusEvent("focusout", { bubbles: true })));
    expect(handlers.onMutateRuntimeSettings.mock.calls.length).toBe(calls);
    expect(container.textContent).toContain("压缩预留 token 必须是非负整数");

    await act(async () => Array.from(container.querySelectorAll<HTMLButtonElement>('[aria-label="Computer Use 操作目标"] button'))
      .find((button) => button.textContent === "整个桌面")!.click());
    act(() => container.querySelector<HTMLButtonElement>('[aria-label="启用文件快照"]')!.click());
    const size = container.querySelector<HTMLInputElement>('[aria-label="快照最大大小（MiB）"]')!;
    act(() => changeInput(size, "2048"));
    await act(async () => size.dispatchEvent(new FocusEvent("focusout", { bubbles: true })));
    const toolInput = container.querySelector<HTMLInputElement>('[aria-label="额外工具导入路径"]')!;
    act(() => changeInput(toolInput, "pkg.ProjectTool"));
    await act(async () => toolInput.form?.requestSubmit());
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="移除额外工具 pkg.UserTool"]')!.click());

    expect(handlers.onMutateRuntimeSettings).toHaveBeenCalledWith(expect.objectContaining({ action: "set_snapshot", snapshot_enabled: false, source: "projectSettings" }));
    expect(handlers.onMutateRuntimeSettings).toHaveBeenCalledWith(expect.objectContaining({ action: "set_compaction", auto_compact_enabled: false, source: "projectSettings" }));
    expect(handlers.onMutateRuntimeSettings).toHaveBeenCalledWith(expect.objectContaining({ action: "set_compaction", compact_buffer_tokens: 30000, source: "projectSettings" }));
    expect(handlers.onMutateRuntimeSettings).toHaveBeenCalledWith(expect.objectContaining({ action: "set_compaction", max_context_length: null, source: "projectSettings" }));
    expect(handlers.onMutateRuntimeSettings).toHaveBeenCalledWith(expect.objectContaining({ action: "set_computer_use_options", computer_use_target_scope: "desktop", source: "projectSettings" }));
    expect(handlers.onMutateRuntimeSettings).toHaveBeenCalledWith(expect.objectContaining({ action: "set_snapshot", snapshot_max_size_mb: 2048, source: "projectSettings" }));
    expect(handlers.onMutateRuntimeSettings).toHaveBeenCalledWith(expect.objectContaining({ action: "add_extra_tool", tool_path: "pkg.ProjectTool", source: "projectSettings" }));
    expect(handlers.onMutateRuntimeSettings).toHaveBeenCalledWith(expect.objectContaining({ action: "remove_extra_tool", tool_path: "pkg.UserTool", source: "userSettings" }));
    confirm.mockRestore();
  });

  it("requires explicit foreground permission and revokes desktop scope atomically", async () => {
    vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)");
    const onMutate = vi.fn().mockResolvedValue(undefined);
    const data: RuntimeSettingsResponse = {
      cwd: "/work/crabcode", snapshot_enabled: true, snapshot_max_size_mb: 1024,
      computer_use_target_scope: "app_window", computer_use_delivery_policy: "strict_background",
      extra_tools: [], extra_tools_by_source: {}, sources: [], warnings: [],
      editable_sources: [{ id: "projectSettings", label: "项目配置", path: "/work/crabcode/.crabcode/settings.json", exists: true, writable: true }],
    };
    const render = (value: RuntimeSettingsResponse) => root.render(
      <RuntimeSettingsPanel
        activeConnection={settings.connections[0]} activeProject={settings.connections[0].projects[0]}
        gateway={onlineGateway} data={value} loading={false} error={null}
        onRefresh={vi.fn()} onMutate={onMutate}
      />,
    );
    const button = (label: string) => Array.from(container.querySelectorAll<HTMLButtonElement>("button"))
      .find((item) => item.textContent === label)!;
    act(() => render(data));
    expect(button("整个桌面").disabled).toBe(true);
    expect(container.textContent).toContain("严格后台仅允许已验证的系统、应用版本和输入操作");
    expect(container.textContent).toContain("不会自动切换到前台");
    expect(container.textContent).toContain("并不表示所有输入都支持自动回退");
    expect(container.textContent).toContain("与会话的“完全访问”权限无关");
    await act(async () => button("允许前台操作").click());
    expect(onMutate).toHaveBeenLastCalledWith(expect.objectContaining({
      computer_use_delivery_policy: "allow_foreground",
    }));
    act(() => render({ ...data, computer_use_target_scope: "desktop", computer_use_delivery_policy: "allow_foreground" }));
    expect(button("整个桌面").disabled).toBe(false);
    await act(async () => button("严格后台").click());
    expect(onMutate).toHaveBeenLastCalledWith(expect.objectContaining({
      computer_use_delivery_policy: "strict_background", computer_use_target_scope: "app_window",
    }));
    expect(onMutate).toHaveBeenCalledTimes(2);
  });

  it("disables window scope and strict background only on Windows", async () => {
    vi.spyOn(navigator, "platform", "get").mockReturnValue("Win32");
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue("Mozilla/5.0 (Windows NT 10.0; Win64; x64)");
    const onMutate = vi.fn().mockResolvedValue(undefined);
    const data: RuntimeSettingsResponse = {
      cwd: "/work/crabcode", snapshot_enabled: true, snapshot_max_size_mb: 1024,
      computer_use_target_scope: "app_window", computer_use_delivery_policy: "allow_foreground",
      extra_tools: [], extra_tools_by_source: {}, sources: [], warnings: [],
      editable_sources: [{ id: "projectSettings", label: "项目配置", path: "/work/crabcode/.crabcode/settings.json", exists: true, writable: true }],
    };
    await act(async () => {
      root.render(
        <RuntimeSettingsPanel
          activeConnection={settings.connections[0]} activeProject={settings.connections[0].projects[0]}
          gateway={onlineGateway} data={data} loading={false} error={null}
          onRefresh={vi.fn()} onMutate={onMutate}
        />,
      );
      await Promise.resolve();
    });
    const button = (label: string) => Array.from(container.querySelectorAll<HTMLButtonElement>("button"))
      .find((item) => item.textContent === label)!;
    expect(button("指定窗口").disabled).toBe(true);
    expect(button("指定窗口").getAttribute("aria-pressed")).toBe("false");
    expect(button("严格后台").disabled).toBe(true);
    expect(button("整个桌面").disabled).toBe(false);
    expect(button("整个桌面").getAttribute("aria-pressed")).toBe("true");
    expect(button("允许前台操作").disabled).toBe(false);
    expect(onMutate).toHaveBeenCalledWith(expect.objectContaining({ computer_use_target_scope: "desktop" }));
    const saved = onMutate.mock.calls.length;
    await act(async () => button("指定窗口").click());
    await act(async () => button("严格后台").click());
    expect(onMutate).toHaveBeenCalledTimes(saved);
  });

  it("shows a host-side command instead of remote execution for remote gateways", () => {
    const handlers = callbacks();
    const capabilities: DocumentCapabilities = {
      supported_extensions: [".pdf"],
      available_extensions: [".pdf"],
      max_bytes: 100,
      documents_dir: "/work/documents",
      libreoffice: { available: false, executable: null },
      ocr: { available: false },
      translation_engines: {
        default: "legacy",
        legacy: { available: true, status: "ready" },
        precise: {
          available: false,
          status: "not_installed",
          version: "0.6.4",
          detail: "高精度 PDF 引擎尚未安装",
          install_command: "crabcode document-engine install",
          remove_command: "crabcode document-engine remove --yes",
        },
      },
    };
    act(() => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ remote: onlineGateway }}
        activeConnection={{ ...settings.connections[0], id: "remote", name: "Remote" }}
        activeProject={settings.connections[0].projects[0]}
        activeSection="document"
        onSectionChange={vi.fn()}
        documentCapabilities={capabilities}
      />,
    ));

    const command = container.querySelector(".document-engine-command.remote")!;
    expect(command.textContent).toContain("请在 Gateway 主机运行");
    expect(command.querySelector("code")?.textContent).toBe("crabcode document-engine install");
    expect(command.querySelector("button")).toBeNull();
  });

  it("exposes connection actions but never offers to delete the local connection", () => {
    const handlers = callbacks();
    act(() => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ local: onlineGateway, remote: offlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="connections"
        onSectionChange={vi.fn()}
      />,
    ));

    expect(container.querySelector('[aria-label="删除 Local"]')).toBeNull();
    const editRemote = container.querySelector<HTMLButtonElement>('[aria-label="编辑 Build Server"]')!;
    const deleteRemote = container.querySelector<HTMLButtonElement>('[aria-label="删除 Build Server"]')!;
    act(() => editRemote.click());
    act(() => deleteRemote.click());

    expect(handlers.onEditConnection).toHaveBeenCalledWith("remote");
    expect(handlers.onDeleteConnection).toHaveBeenCalledWith("remote");
  });

  it("queries grouped models and shows configured and effective values without edit actions", () => {
    const handlers = callbacks();
    const refresh = vi.fn();
    const modelSettings: ModelSettingsResponse = {
      cwd: "/work/crabcode",
      default_model: "gpt-5.6",
      sources: ["/Users/test/.crabcode/settings.json"],
      groups: {
        "test_rourter": {
          provider: "codex",
          base_url: "https://router.example.com/v1",
          reasoning_effort: "high",
        },
      },
      models: [
        {
          name: "claude",
          group: null,
          is_default: false,
          configured: { provider: "anthropic", model: "claude-opus-4.6" },
          effective: { provider: "anthropic", model: "claude-opus-4.6", max_tokens: 16384 },
          overridden_fields: ["provider", "model"],
          sources: ["/Users/test/.crabcode/settings.json"],
        },
        {
          name: "gpt-5.6",
          group: "test_rourter",
          is_default: true,
          configured: { group: "test_rourter", model: "gpt-5.6-sol" },
          effective: {
            group: "test_rourter",
            provider: "codex",
            base_url: "https://router.example.com/v1",
            model: "gpt-5.6-sol",
            reasoning_effort: "high",
          },
          overridden_fields: ["model"],
          sources: ["/Users/test/.crabcode/settings.json"],
        },
      ],
      warnings: [],
    };
    act(() => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="models"
        onSectionChange={vi.fn()}
        modelSettings={modelSettings}
        onRefreshModelSettings={refresh}
      />,
    ));

    expect(Array.from(container.querySelectorAll(".model-settings-group > header strong"))
      .map((element) => element.textContent)).toEqual(["未分组", "test_rourter"]);
    expect(container.querySelector(".model-settings-detail")?.textContent).toContain("gpt-5.6");
    expect(container.querySelector(".model-settings-detail")?.textContent).toContain("继承 test_rourter");
    expect(container.querySelector(".model-settings-detail")?.textContent).toContain("https://router.example.com/v1");
    expect(container.querySelector('[aria-label^="编辑"]')).toBeNull();
    expect(container.querySelector('[aria-label^="删除"]')).toBeNull();

    const groupToggles = container.querySelectorAll<HTMLButtonElement>(".model-settings-group-toggle");
    expect(groupToggles).toHaveLength(2);
    expect(groupToggles[1].getAttribute("aria-expanded")).toBe("true");
    act(() => groupToggles[1].click());
    expect(groupToggles[1].getAttribute("aria-expanded")).toBe("false");
    expect(groupToggles[1].closest(".model-settings-group")?.classList.contains("is-collapsed")).toBe(true);

    act(() => container.querySelector<HTMLButtonElement>(".model-refresh-command")!.click());
    expect(refresh).toHaveBeenCalledOnce();

    const search = container.querySelector<HTMLInputElement>('[aria-label="搜索模型配置"]')!;
    act(() => changeInput(search, "anthropic"));
    expect(container.querySelectorAll(".model-settings-group > button")).toHaveLength(1);
    expect(container.querySelector(".model-settings-group > button")?.textContent).toContain("claude");
    expect(container.querySelector(".model-settings-group-toggle")?.getAttribute("aria-expanded")).toBe("true");
    act(() => changeInput(search, ""));
    expect(container.querySelectorAll(".model-settings-group-toggle")[1].getAttribute("aria-expanded")).toBe("false");
  });

  it("changes the theme mode and built-in Dock icon", async () => {
    const handlers = callbacks();
    act(() => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="appearance"
        onSectionChange={vi.fn()}
      />,
    ));

    const themeChoices = Array.from(container.querySelectorAll<HTMLButtonElement>(".theme-choice"));
    const lightTheme = themeChoices.find((button) => button.textContent?.includes("始终使用浅色主题"))!;
    const darkTheme = themeChoices.find((button) => button.textContent?.includes("始终使用深色主题"))!;
    const lightDockIcon = Array.from(container.querySelectorAll<HTMLButtonElement>(".dock-icon-choice"))
      .find((button) => button.textContent?.includes("白底黑蟹"))!;
    act(() => lightTheme.click());
    act(() => darkTheme.click());
    await act(async () => {
      lightDockIcon.click();
      await Promise.resolve();
    });

    expect(themeChoices).toHaveLength(3);
    expect(handlers.onThemeModeChange).toHaveBeenNthCalledWith(1, "light");
    expect(handlers.onThemeModeChange).toHaveBeenNthCalledWith(2, "dark");
    expect(handlers.onDockIconChange).toHaveBeenCalledWith("light", undefined);
  });

  it("updates light and dark theme profiles independently", () => {
    const handlers = callbacks();
    act(() => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="appearance"
        onSectionChange={vi.fn()}
      />,
    ));

    const accent = container.querySelector<HTMLInputElement>('input[aria-label="浅色主题强调色"]')!;
    const contrast = container.querySelector<HTMLInputElement>('input[aria-label="深色主题对比度"]')!;
    const uiFont = container.querySelector<HTMLSelectElement>('select[aria-label="浅色主题UI 字体"]')!;
    act(() => changeInput(accent, "#33aa88"));
    act(() => changeInput(contrast, "72"));
    act(() => {
      uiFont.value = "serif";
      uiFont.dispatchEvent(new Event("change", { bubbles: true }));
    });
    act(() => container.querySelector<HTMLButtonElement>('[aria-label="深色主题半透明侧栏"]')!.click());

    expect(container.querySelectorAll(".theme-profile-card")).toHaveLength(2);
    expect(handlers.onThemeProfileChange).toHaveBeenCalledWith("light", { accent_color: "#33aa88" });
    expect(handlers.onThemeProfileChange).toHaveBeenCalledWith("dark", { contrast: 72 });
    expect(handlers.onThemeProfileChange).toHaveBeenCalledWith("light", { ui_font_family: "serif" });
    expect(handlers.onThemeProfileChange).toHaveBeenCalledWith("dark", { translucent_sidebar: true });
  });

  it("lists built-in skin presets and exposes preset management actions", () => {
    const handlers = callbacks();
    act(() => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="appearance"
        onSectionChange={vi.fn()}
      />,
    ));

    const presetCards = Array.from(container.querySelectorAll<HTMLElement>(".theme-preset-card"));
    const graphite = presetCards.find((card) => card.textContent?.includes("石墨"))!;
    const deepSea = presetCards.find((card) => card.textContent?.includes("深海"))!;

    act(() => graphite.querySelector<HTMLButtonElement>(".theme-preset-select")!.click());
    act(() => deepSea.querySelector<HTMLButtonElement>('button[title="复制 深海"]')!.click());
    act(() => container.querySelector<HTMLButtonElement>(".theme-transfer-toolbar button:last-of-type")!.click());

    expect(presetCards).toHaveLength(3);
    expect(handlers.onThemePresetChange).toHaveBeenCalledWith("builtin.graphite");
    expect(handlers.onThemeDuplicate).toHaveBeenCalledWith("builtin.deep-sea");
    expect(handlers.onThemeRestoreDefault).not.toHaveBeenCalled();
    expect(container.querySelector<HTMLButtonElement>(".theme-transfer-toolbar button:last-of-type")?.disabled).toBe(true);
  });

  it("renames and deletes a custom preset with inline confirmation", () => {
    const handlers = callbacks();
    const customTheme = {
      ...BUILTIN_THEMES[0],
      id: "custom.crab-copy",
      name: "Crab 默认 副本",
      author: "本地用户",
    };
    act(() => root.render(
      <SettingsView
        {...handlers}
        settings={{
          ...settings,
          active_theme_id: customTheme.id,
          custom_theme_presets: [customTheme],
        }}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="appearance"
        onSectionChange={vi.fn()}
      />,
    ));

    act(() => container.querySelector<HTMLButtonElement>('button[title="重命名 Crab 默认 副本"]')!.click());
    const renameInput = container.querySelector<HTMLInputElement>('input[aria-label="重命名 Crab 默认 副本"]')!;
    expect(renameInput).not.toBeNull();
    act(() => changeInput(renameInput, "我的螃蟹"));
    act(() => container.querySelector<HTMLButtonElement>('button[title="保存名称"]')!.click());
    expect(handlers.onThemeRename).toHaveBeenCalledWith(customTheme.id, "我的螃蟹");

    act(() => container.querySelector<HTMLButtonElement>('button[title="删除 Crab 默认 副本"]')!.click());
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("确定删除");
    expect(handlers.onThemeDelete).not.toHaveBeenCalled();
    act(() => container.querySelector<HTMLButtonElement>('button[title="确认删除 Crab 默认 副本"]')!.click());
    expect(handlers.onThemeDelete).toHaveBeenCalledWith(customTheme.id);
  });

  it("shows both profiles for system mode and only the forced profile otherwise", () => {
    const handlers = callbacks();
    const renderAppearance = (themeMode: DesktopSettings["theme_mode"]) => act(() => root.render(
      <SettingsView
        {...handlers}
        settings={{ ...settings, theme_mode: themeMode }}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="appearance"
        onSectionChange={vi.fn()}
      />,
    ));

    renderAppearance("system");
    expect(container.querySelectorAll(".theme-profile-card")).toHaveLength(2);

    renderAppearance("light");
    expect(container.querySelectorAll(".theme-profile-card")).toHaveLength(1);
    expect(container.querySelector('[aria-label="浅色主题"]')).not.toBeNull();
    expect(container.querySelector('[aria-label="深色主题"]')).toBeNull();

    renderAppearance("dark");
    expect(container.querySelectorAll(".theme-profile-card")).toHaveLength(1);
    expect(container.querySelector('[aria-label="浅色主题"]')).toBeNull();
    expect(container.querySelector('[aria-label="深色主题"]')).not.toBeNull();
  });

  it("updates shared appearance preferences", () => {
    const handlers = callbacks();
    act(() => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ local: onlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="appearance"
        onSectionChange={vi.fn()}
      />,
    ));

    act(() => container.querySelector<HTMLButtonElement>('[aria-label="指针光标"]')!.click());
    act(() => container.querySelector<HTMLButtonElement>('[aria-label="增大界面字号"]')!.click());
    act(() => container.querySelector<HTMLButtonElement>('[aria-label="增大代码字号"]')!.click());
    const symbolMarkers = Array.from(container.querySelectorAll<HTMLButtonElement>('.settings-segmented button'))
      .find((button) => button.textContent?.includes("+ / -"))!;
    act(() => symbolMarkers.click());
    act(() => container.querySelector<HTMLButtonElement>('[aria-label="字体平滑"]')!.click());

    expect(handlers.onAppearanceChange).toHaveBeenCalledWith({ pointer_cursor: false });
    expect(handlers.onAppearanceChange).toHaveBeenCalledWith({ ui_font_size: 15 });
    expect(handlers.onAppearanceChange).toHaveBeenCalledWith({ code_font_size: 13 });
    expect(handlers.onAppearanceChange).toHaveBeenCalledWith({ diff_marker_style: "symbols" });
    expect(handlers.onAppearanceChange).toHaveBeenCalledWith({ font_smoothing: false });
  });

  it("keeps project rows visible but disables directory operations while offline", () => {
    const handlers = callbacks();
    act(() => root.render(
      <SettingsView
        {...handlers}
        settings={settings}
        gateways={{ local: offlineGateway }}
        activeConnection={settings.connections[0]}
        activeProject={settings.connections[0].projects[0]}
        activeSection="projects"
        onSectionChange={vi.fn()}
      />,
    ));

    expect(container.querySelector(".settings-entity-copy strong")?.textContent).toBe("CrabCode");
    expect(container.querySelector<HTMLButtonElement>(".settings-command")?.disabled).toBe(true);
    expect(container.querySelector<HTMLButtonElement>('[aria-label="编辑 CrabCode"]')?.disabled).toBe(true);
    expect(container.querySelector(".settings-inline-note")?.textContent).toContain("连接 Gateway 后");
  });
});
