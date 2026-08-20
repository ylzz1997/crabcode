/* @vitest-environment jsdom */

import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  filterSettingsSections,
  SettingsView,
  type SettingsSectionId,
} from "./SettingsView";
import type { DesktopSettings, GatewayViewState } from "./types";

const settings: DesktopSettings = {
  schema_version: 2,
  active_connection_id: "local",
  connection_order: ["local", "remote"],
  connections: [
    {
      id: "local",
      name: "Local",
      base_url: "http://127.0.0.1:4096",
      credential_ref: null,
      allow_insecure_remote: false,
      projects: [{
        id: "crab",
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
      projects: [],
      last_project_path: null,
      last_project_id: null,
    },
  ],
  python_path: null,
  sidebar_width: 280,
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
