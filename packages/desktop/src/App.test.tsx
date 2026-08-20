/* @vitest-environment jsdom */

import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  collectFavoriteSessions,
  defaultProjectDirectory,
  FavoritesView,
  formatTurnDuration,
  MessageMarkdown,
  ProjectActionsMenu,
  ProjectDeleteModal,
  ProjectModal,
  resolveDefaultProjectId,
  resolveRememberedModel,
  ScheduleDeleteModal,
  ScheduledTasksView,
} from "./App";
import type { GatewayApi } from "./gateway";
import { favoriteEntries, resolveFavoriteEntries } from "./favorites";
import type { BackgroundTaskInfo, ConnectionPreset, GatewayViewState, ScheduleJobInfo } from "./types";

describe("MessageMarkdown", () => {
  it("renders inline and display math with KaTeX", () => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const container = document.createElement("div");
    const root = createRoot(container);

    act(() => root.render(
      <MessageMarkdown>{"Inline $x^2$\n\n$$\n\\int_0^1 x\\,dx\n$$"}</MessageMarkdown>,
    ));

    expect(container.querySelectorAll(".katex")).toHaveLength(2);
    expect(container.querySelector(".katex-display")).not.toBeNull();
    expect(container.textContent).toContain("x2");

    act(() => root.unmount());
  });
});

const job: ScheduleJobInfo = {
  id: "job-1",
  name: "依赖巡检",
  prompt: "检查依赖",
  schedule: "+1h",
  schedule_type: "once",
  cwd: "/work/crabcode",
  enabled: false,
  status: "error",
  last_run: null,
  next_run: null,
  run_count: 1,
  max_runs: null,
  created_at: "2026-08-20T00:00:00Z",
  session_id: null,
  description: "",
  tags: [],
  timeout: 600,
  model_profile: null,
  extra: {},
  running: false,
};

function changeInput(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("ScheduleDeleteModal", () => {
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
  });

  it("requires an explicit in-app confirmation before deleting", () => {
    const onClose = vi.fn();
    const onConfirm = vi.fn();
    act(() => root.render(
      <ScheduleDeleteModal
        job={job}
        busy={false}
        error={null}
        onClose={onClose}
        onConfirm={onConfirm}
      />,
    ));

    expect(container.querySelector('[role="dialog"]')?.textContent).toContain("删除“依赖巡检”？");
    const buttons = Array.from(container.querySelectorAll<HTMLButtonElement>(".modal-actions button"));
    act(() => buttons.find((button) => button.textContent?.includes("永久删除"))!.click());

    expect(onConfirm).toHaveBeenCalledOnce();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("disables both actions while deletion is in progress", () => {
    act(() => root.render(
      <ScheduleDeleteModal
        job={job}
        busy
        error={null}
        onClose={vi.fn()}
        onConfirm={vi.fn()}
      />,
    ));

    const buttons = Array.from(container.querySelectorAll<HTMLButtonElement>(".modal-actions button"));
    expect(buttons).toHaveLength(2);
    expect(buttons.every((button) => button.disabled)).toBe(true);
  });
});

describe("ProjectActionsMenu", () => {
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
  });

  it("shows the three requested project actions and invokes them", () => {
    const onNewSession = vi.fn();
    const onEdit = vi.fn();
    const onDelete = vi.fn();
    act(() => root.render(
      <ProjectActionsMenu
        projectName="CrabCode"
        onNewSession={onNewSession}
        onEdit={onEdit}
        onDelete={onDelete}
      />,
    ));

    const trigger = container.querySelector<HTMLButtonElement>('[aria-haspopup="menu"]')!;
    act(() => trigger.click());
    expect(Array.from(document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')).map((button) => button.textContent))
      .toEqual(["新建会话", "编辑项目", "删除项目"]);

    act(() => document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')[0].click());
    expect(onNewSession).toHaveBeenCalledOnce();
    expect(document.querySelector('[role="menu"]')).toBeNull();

    act(() => trigger.click());
    act(() => document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')[1].click());
    expect(onEdit).toHaveBeenCalledOnce();

    act(() => trigger.click());
    act(() => document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')[2].click());
    expect(onDelete).toHaveBeenCalledOnce();
  });

  it("closes with Escape and returns focus to the trigger", () => {
    act(() => root.render(
      <ProjectActionsMenu
        projectName="CrabCode"
        onNewSession={vi.fn()}
        onEdit={vi.fn()}
        onDelete={vi.fn()}
      />,
    ));
    const trigger = container.querySelector<HTMLButtonElement>('[aria-haspopup="menu"]')!;
    act(() => trigger.click());
    act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })));
    expect(document.querySelector('[role="menu"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("keeps the default project delete action disabled", () => {
    const onDelete = vi.fn();
    act(() => root.render(
      <ProjectActionsMenu
        projectName="Home"
        deleteDisabled
        onNewSession={vi.fn()}
        onEdit={vi.fn()}
        onDelete={onDelete}
      />,
    ));

    act(() => container.querySelector<HTMLButtonElement>('[aria-haspopup="menu"]')!.click());
    const deleteAction = document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')[2];
    expect(deleteAction.disabled).toBe(true);
    expect(deleteAction.textContent).toBe("默认项目不可删除");
    act(() => deleteAction.click());
    expect(onDelete).not.toHaveBeenCalled();
  });

  it("can favorite and unfavorite a project from the project menu", () => {
    const onToggleFavorite = vi.fn();
    act(() => root.render(
      <ProjectActionsMenu
        projectName="CrabCode"
        favorite
        onNewSession={vi.fn()}
        onEdit={vi.fn()}
        onToggleFavorite={onToggleFavorite}
        onDelete={vi.fn()}
      />,
    ));

    act(() => container.querySelector<HTMLButtonElement>('[aria-haspopup="menu"]')!.click());
    const favoriteAction = Array.from(document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'))
      .find((button) => button.textContent === "取消收藏项目")!;
    act(() => favoriteAction.click());
    expect(onToggleFavorite).toHaveBeenCalledOnce();
  });
});

describe("ProjectDeleteModal", () => {
  it("uses an in-app confirmation and removes only after confirmation", () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const onConfirm = vi.fn();
    act(() => root.render(
      <ProjectDeleteModal
        project={{
          id: "trial",
          name: "试试新项目",
          path: "/Users/hyl/试试新项目",
          directories: ["/Users/hyl/试试新项目"],
          last_session_id: null,
        }}
        onClose={vi.fn()}
        onConfirm={onConfirm}
      />,
    ));

    expect(container.querySelector('[role="dialog"]')?.textContent).toContain("从列表中删除“试试新项目”？");
    const remove = Array.from(container.querySelectorAll<HTMLButtonElement>(".modal-actions button"))
      .find((button) => button.textContent?.includes("删除项目"))!;
    act(() => remove.click());
    expect(onConfirm).toHaveBeenCalledOnce();

    act(() => root.unmount());
    container.remove();
  });
});

describe("automation deck tabs", () => {
  it("switches to running monitors and exposes their project and session", () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const project = {
      id: "project-1",
      path: "/work/crabcode",
      name: "CrabCode",
      directories: ["/work/crabcode"],
      last_session_id: "session-1",
    };
    const session = {
      session_id: "session-1",
      message_count: 4,
      model: "",
      provider: "",
      created_at: "2026-08-20T00:00:00Z",
      title: "监控发布流水线",
      cwd: project.path,
      tokens_used: 0,
      preview: "等待部署状态",
    };
    const monitor: BackgroundTaskInfo = {
      task_id: "monitor-12345678",
      agent_id: null,
      session_id: session.session_id,
      cwd: project.path,
      description: "监听部署状态",
      task_type: "local_bash",
      source: "command",
      status: "running",
      output_file: null,
      created_at: "2026-08-20T00:00:00Z",
      started_at: "2026-08-20T00:00:00Z",
      finished_at: null,
      updated_at: "2026-08-20T00:00:00Z",
      error: "",
      exit_code: null,
    };
    const onOpenSession = vi.fn();

    function Harness() {
      const [tab, setTab] = useState<"schedule" | "monitor">("schedule");
      return (
        <ScheduledTasksView
          tab={tab}
          onTabChange={setTab}
          jobs={[]}
          tasks={[monitor]}
          projects={[project]}
          sessionsByProject={{ [project.path]: [session] }}
          loading={false}
          error={null}
          actionState={null}
          connected
          onRefresh={vi.fn()}
          onAction={vi.fn()}
          onNew={vi.fn()}
          onOpenSession={onOpenSession}
        />
      );
    }

    act(() => root.render(<Harness />));
    expect(container.textContent).toContain("启动一个流程");
    const monitorTab = Array.from(container.querySelectorAll<HTMLButtonElement>('[role="tab"]'))
      .find((button) => button.textContent?.includes("Monitor"))!;
    act(() => monitorTab.click());

    expect(monitorTab.getAttribute("aria-selected")).toBe("true");
    expect(container.textContent).toContain("监听部署状态");
    expect(container.textContent).toContain("CrabCode");
    expect(container.textContent).toContain("监控发布流水线");
    act(() => container.querySelector<HTMLButtonElement>(".monitor-open")!.click());
    expect(onOpenSession).toHaveBeenCalledWith(project, session);

    act(() => root.unmount());
    container.remove();
  });
});

describe("project defaults", () => {
  it("protects only the first startup project when legacy projects share its path", () => {
    const projects = [
      {
        id: "home",
        name: "hyl",
        path: "/Users/hyl",
        directories: ["/Users/hyl"],
        last_session_id: null,
      },
      {
        id: "trial",
        name: "试试新项目",
        path: "/Users/hyl",
        directories: [],
        is_default: true,
        last_session_id: null,
      },
    ];

    expect(resolveDefaultProjectId(projects, "/Users/hyl")).toBe("home");
    expect(projects[1].id).not.toBe(resolveDefaultProjectId(projects, "/Users/hyl"));
  });

  it("derives a safe folder beneath the home directory", () => {
    expect(defaultProjectDirectory("/Users/test", "试试新项目")).toBe("/Users/test/试试新项目");
    expect(defaultProjectDirectory("/Users/test/", "a/b:c")).toBe("/Users/test/a-b-c");
  });

  it("creates and saves the suggested project directory", async () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const createDirectory = vi.fn().mockResolvedValue({
      name: "试试新项目",
      path: "/Users/test/试试新项目",
      hidden: false,
      is_symlink: false,
    });
    const onSave = vi.fn();
    act(() => root.render(
      <ProjectModal
        api={{ createDirectory } as unknown as GatewayApi}
        home="/Users/test"
        roots={["/Users/test"]}
        project={null}
        projects={[]}
        onClose={vi.fn()}
        onSave={onSave}
      />,
    ));

    const name = container.querySelector<HTMLInputElement>(".project-name-field input")!;
    act(() => changeInput(name, "试试新项目"));
    expect(container.textContent).toContain("项目文件夹（可选）");
    expect(container.textContent).toContain("不选择时，将在用户主目录下自动创建同名文件夹");
    expect(container.textContent).not.toContain("/Users/test/试试新项目");
    const create = Array.from(container.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.textContent?.includes("创建项目"))!;
    await act(async () => create.click());

    expect(createDirectory).toHaveBeenCalledWith("/Users/test/试试新项目");
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      name: "试试新项目",
      path: "/Users/test/试试新项目",
      directories: ["/Users/test/试试新项目"],
    }));
    act(() => root.unmount());
    container.remove();
  });

  it("uses a selected folder instead of creating the default directory", async () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const createDirectory = vi.fn();
    const directories = vi.fn().mockImplementation(async (path: string) => ({
      path,
      parent: path === "/Users/test" ? null : "/Users/test",
      directories: path === "/Users/test" ? [{
        name: "已有源码",
        path: "/Users/test/已有源码",
        hidden: false,
        is_symlink: false,
      }] : [],
    }));
    const onSave = vi.fn();
    act(() => root.render(
      <ProjectModal
        api={{ createDirectory, directories } as unknown as GatewayApi}
        home="/Users/test"
        roots={["/Users/test"]}
        project={null}
        projects={[]}
        onClose={vi.fn()}
        onSave={onSave}
      />,
    ));

    act(() => changeInput(
      container.querySelector<HTMLInputElement>(".project-name-field input")!,
      "源码项目",
    ));
    const choose = Array.from(container.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.textContent?.includes("选择项目文件夹"))!;
    await act(async () => choose.click());
    const source = Array.from(document.body.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.textContent?.includes("已有源码"))!;
    await act(async () => source.click());
    const use = Array.from(document.body.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.textContent?.includes("加入此目录"))!;
    act(() => use.click());
    const create = Array.from(container.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.textContent?.includes("创建项目"))!;
    await act(async () => create.click());

    expect(createDirectory).not.toHaveBeenCalled();
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      name: "源码项目",
      path: "/Users/test/已有源码",
      directories: ["/Users/test/已有源码"],
    }));
    act(() => root.unmount());
    container.remove();
  });

  it("protects only the original directory of the default project", () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    act(() => root.render(
      <ProjectModal
        api={{} as GatewayApi}
        home="/Users/test"
        roots={["/Users/test"]}
        project={{
          id: "existing",
          name: "已有项目",
          path: "/Users/test/已有项目",
          directories: ["/Users/test/已有项目", "/Users/test/附加目录"],
          last_session_id: null,
        }}
        projects={[]}
        protectPrimaryDirectory
        onClose={vi.fn()}
        onSave={vi.fn()}
      />,
    ));

    expect(container.textContent).toContain("项目目录");
    expect(container.textContent).toContain("/Users/test/已有项目");
    const removeButtons = Array.from(container.querySelectorAll<HTMLButtonElement>('button[aria-label^="移除目录"]'));
    expect(removeButtons).toHaveLength(2);
    expect(removeButtons[0].disabled).toBe(true);
    expect(removeButtons[0].title).toBe("默认项目的主目录不能移除");
    expect(removeButtons[1].disabled).toBe(false);
    act(() => root.unmount());
    container.remove();
  });

  it("rejects a main directory already used by another project", async () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const createDirectory = vi.fn();
    act(() => root.render(
      <ProjectModal
        api={{ createDirectory } as unknown as GatewayApi}
        home="/Users/test"
        roots={["/Users/test"]}
        project={null}
        projects={[{
          id: "existing",
          name: "已有项目",
          path: "/Users/test/重复",
          directories: ["/Users/test/重复"],
          last_session_id: null,
        }]}
        onClose={vi.fn()}
        onSave={vi.fn()}
      />,
    ));

    act(() => changeInput(
      container.querySelector<HTMLInputElement>(".project-name-field input")!,
      "重复",
    ));
    const create = Array.from(container.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.textContent?.includes("创建项目"))!;
    await act(async () => create.click());

    expect(container.textContent).toContain("已有项目”已经使用这个主目录");
    expect(createDirectory).not.toHaveBeenCalled();
    act(() => root.unmount());
    container.remove();
  });
});

describe("turn duration formatting", () => {
  it("supports seconds and omits zero hours/minutes in hms mode", () => {
    expect(formatTurnDuration(3_723_000, "seconds")).toBe("3723秒");
    expect(formatTurnDuration(3_723_000, "hms")).toBe("1时2分3秒");
    expect(formatTurnDuration(63_000, "hms")).toBe("1分3秒");
    expect(formatTurnDuration(3_000, "hms")).toBe("3秒");
  });
});

describe("favorite sessions", () => {
  const project = {
    id: "project-1",
    path: "/work/crab",
    name: "CrabCode",
    directories: ["/work/crab"],
    last_session_id: null,
    favorite_session_ids: ["session-1"],
  };
  const connection = {
    id: "local",
    name: "Local",
    base_url: "http://127.0.0.1:4096",
    credential_ref: null,
    allow_insecure_remote: false,
    projects: [project],
    last_project_path: project.path,
    last_project_id: project.id,
  } as ConnectionPreset;
  const gateway = {
    status: "online",
    error: null,
    token: null,
    tokenExpiresAt: 0,
    workspace: null,
    models: [],
    sessionsByProject: {
      [project.path]: [{
        session_id: "session-1",
        message_count: 2,
        model: "",
        provider: "",
        created_at: "2026-08-20T00:00:00Z",
        title: "重要会话",
        cwd: project.path,
        tokens_used: 0,
        preview: "检查发布状态",
      }],
    },
    runningCount: 0,
    pendingCount: 0,
  } as GatewayViewState;

  it("collects favorite sessions with their project", () => {
    expect(collectFavoriteSessions(connection, gateway)).toMatchObject([{
      project: { name: "CrabCode" },
      session: { title: "重要会话" },
    }]);
  });

  it("shows the project name and opens a favorite session", () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const item = collectFavoriteSessions(connection, gateway)[0];
    const items = resolveFavoriteEntries(connection, gateway);
    const onOpen = vi.fn();
    act(() => root.render(
      <FavoritesView
        items={items}
        entries={favoriteEntries(connection)}
        connected
        onOpenProject={vi.fn()}
        onOpenSession={onOpen}
        onCreateFolder={vi.fn()}
        onRenameFolder={vi.fn()}
        onMove={vi.fn()}
        onRemove={vi.fn()}
        onDeleteFolder={vi.fn()}
      />,
    ));
    expect(container.textContent).toContain("CrabCode");
    act(() => container.querySelector<HTMLButtonElement>(".favorite-session-main")!.click());
    expect(onOpen).toHaveBeenCalledWith(item.project, item.session);
    act(() => root.unmount());
    container.remove();
  });

  it("renders nested folders and creates a child folder at the selected level", () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const nestedConnection: ConnectionPreset = {
      ...connection,
      favorite_items: [{
        id: "folder-1",
        type: "folder",
        name: "客户",
        children: [{
          id: "favorite-1",
          type: "session",
          project_id: project.id,
          session_id: "session-1",
        }],
      }],
    };
    const onCreateFolder = vi.fn();
    const onDeleteFolder = vi.fn();
    act(() => root.render(
      <FavoritesView
        items={resolveFavoriteEntries(nestedConnection, gateway)}
        entries={favoriteEntries(nestedConnection)}
        connected
        onOpenProject={vi.fn()}
        onOpenSession={vi.fn()}
        onCreateFolder={onCreateFolder}
        onRenameFolder={vi.fn()}
        onMove={vi.fn()}
        onRemove={vi.fn()}
        onDeleteFolder={onDeleteFolder}
      />,
    ));

    expect(container.textContent).toContain("客户");
    expect(container.textContent).toContain("重要会话");
    act(() => container.querySelector<HTMLButtonElement>('[aria-label="在 客户 中新建文件夹"]')!.click());
    const input = container.querySelector<HTMLInputElement>('.favorite-folder-form input')!;
    act(() => changeInput(input, "发布"));
    act(() => container.querySelector<HTMLFormElement>('.favorite-folder-form')!
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(onCreateFolder).toHaveBeenCalledWith("folder-1", "发布");

    act(() => container.querySelector<HTMLButtonElement>('[aria-label="删除文件夹 客户"]')!.click());
    expect(container.querySelector('[role="dialog"]')?.textContent).toContain("请选择如何处理");
    const keepButton = Array.from(container.querySelectorAll<HTMLButtonElement>('.favorite-folder-delete-options button'))
      .find((button) => button.textContent?.includes("移到收藏根目录"))!;
    act(() => keepButton.click());
    expect(onDeleteFolder).toHaveBeenCalledWith("folder-1", "promote");

    act(() => root.unmount());
    container.remove();
  });
});

describe("remembered model selection", () => {
  it("uses the remembered profile while it is still advertised by the Gateway", () => {
    expect(resolveRememberedModel(
      { last_model_profile: "fast" },
      [{ name: "fast" }, { name: "smart" }],
    )).toBe("fast");
  });

  it("omits a removed profile so the Gateway default is used", () => {
    expect(resolveRememberedModel(
      { last_model_profile: "removed" },
      [{ name: "fast" }, { name: "smart" }],
    )).toBeUndefined();
  });
});
