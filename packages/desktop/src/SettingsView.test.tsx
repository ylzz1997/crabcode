/* @vitest-environment jsdom */

import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  filterSettingsSections,
  SettingsView,
  type SettingsSectionId,
} from "./SettingsView";
import type { DocumentEngineInstallProgress } from "./native";
import type { DesktopSettings, DocumentCapabilities, GatewayViewState } from "./types";

const settings: DesktopSettings = {
  schema_version: 3,
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
  document_agent_width: 400,
  document_agent_collapsed: false,
  document_show_original_text: false,
  document_translation_concurrency: 3,
  document_translation_batch_size: 200,
  theme_mode: "system",
  light_theme: {
    accent_color: "#e75f4b",
    background_color: "#f5f7f6",
    foreground_color: "#172421",
    ui_font_family: "system",
    code_font_family: "system-mono",
    translucent_sidebar: false,
    contrast: 50,
  },
  dark_theme: {
    accent_color: "#ff765f",
    background_color: "#0d1517",
    foreground_color: "#edf4ef",
    ui_font_family: "system",
    code_font_family: "system-mono",
    translucent_sidebar: false,
    contrast: 50,
  },
  pointer_cursor: true,
  ui_font_size: 14,
  code_font_size: 12,
  diff_marker_style: "color",
  font_smoothing: true,
  show_turn_duration: true,
  turn_duration_format: "hms",
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
    expect(filterSettingsSections("凭据").map((section) => section.id)).toEqual(["connections"]);
    expect(filterSettingsSections("工作目录").map((section) => section.id)).toEqual(["projects"]);
    expect(filterSettingsSections("Dock 图标").map((section) => section.id)).toEqual(["appearance"]);
    expect(filterSettingsSections("字体平滑").map((section) => section.id)).toEqual(["appearance"]);
    expect(filterSettingsSections("对比度").map((section) => section.id)).toEqual(["appearance"]);
    expect(filterSettingsSections("处理用时").map((section) => section.id)).toEqual(["general"]);
    expect(filterSettingsSections("并行请求").map((section) => section.id)).toEqual(["document"]);
    expect(filterSettingsSections("显示原文").map((section) => section.id)).toEqual(["document"]);
    expect(filterSettingsSections("Yuri Head").map((section) => section.id)).toEqual(["about"]);
    expect(filterSettingsSections("不存在")).toEqual([]);
  });
});

describe("SettingsView", () => {
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
    Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
  });

  const callbacks = () => ({
    onBack: vi.fn(),
    onSavePythonPath: vi.fn(),
    onConversationChange: vi.fn(),
    onDocumentChange: vi.fn(),
    onThemeModeChange: vi.fn(),
    onThemeProfileChange: vi.fn(),
    onAppearanceChange: vi.fn(),
    onDockIconChange: vi.fn().mockResolvedValue(undefined),
    onActivateConnection: vi.fn(),
    onNewConnection: vi.fn(),
    onEditConnection: vi.fn(),
    onDeleteConnection: vi.fn(),
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

  it("saves a normalized Python path when the field loses focus", () => {
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

    const input = container.querySelector<HTMLInputElement>('input[aria-label="Python 路径"]')!;
    act(() => changeInput(input, "  /opt/python3  "));
    act(() => input.dispatchEvent(new FocusEvent("focusout", { bubbles: true })));

    expect(handlers.onSavePythonPath).toHaveBeenCalledWith("/opt/python3");
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
        documentCapabilities={capabilities}
        canManageDocumentEngine
        documentEngineBusy={documentEngineBusy}
        documentEngineProgress={documentEngineProgress}
        onInstallDocumentEngine={install}
      />,
    );
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
