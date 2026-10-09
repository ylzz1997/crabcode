/* @vitest-environment jsdom */

import { act, useState, type ComponentProps } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import type { GatewayApi } from "./gateway";
import { type ChangeReviewSelection, type ChangeReviewTurn } from "./ChangeReview";
import {
  activateProjectFileTab,
  classifyProjectFile,
  closeProjectFileTab,
  ProjectFilesWorkspace,
  projectFileDisplayPath,
  projectPathKey,
  type ProjectFileTabsState,
} from "./ProjectFilesWorkspace";

function ControlledWorkspace(props: Omit<ComponentProps<typeof ProjectFilesWorkspace>, "openFiles" | "selectedFile" | "onSelectFile" | "onCloseFile" | "treeOpen" | "onToggleTree">) {
  const [tabs, setTabs] = useState<ProjectFileTabsState>({ files: [], activePath: null });
  const [treeOpen, setTreeOpen] = useState(true);
  const selectedFile = tabs.files.find((file) => file.path === tabs.activePath) ?? null;
  return (
    <ProjectFilesWorkspace
      {...props}
      openFiles={tabs.files}
      selectedFile={selectedFile}
      treeOpen={treeOpen}
      onSelectFile={(file) => setTabs((current) => activateProjectFileTab(current, file))}
      onCloseFile={(path) => setTabs((current) => closeProjectFileTab(current, path))}
      onToggleTree={() => setTreeOpen((value) => !value)}
    />
  );
}

function flush() {
  return act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function changeInput(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("project file classification", () => {
  it("recognizes supported previews and applies size limits", () => {
    expect(classifyProjectFile({ name: "README.md", size: 20 }).kind).toBe("markdown");
    expect(classifyProjectFile({ name: "App.tsx", size: 20 })).toMatchObject({ kind: "text", language: "tsx" });
    expect(classifyProjectFile({ name: ".gitignore", size: 20 }).kind).toBe("text");
    expect(classifyProjectFile({ name: "photo.webp", size: 20 }).kind).toBe("image");
    expect(classifyProjectFile({ name: "archive.zip", size: 20 }).kind).toBe("unsupported");
    expect(classifyProjectFile({ name: "large.txt", size: 2 * 1024 * 1024 + 1 }).reason).toContain("2 MiB");
    expect(classifyProjectFile({ name: "large.png", size: 10 * 1024 * 1024 + 1 }).reason).toContain("10 MiB");
  });
});

describe("project file tabs", () => {
  const first = { name: "README.md", path: "/work/README.md", size: 10, hidden: false, is_symlink: false };
  const second = { name: "App.tsx", path: "/work/App.tsx", size: 20, hidden: false, is_symlink: false };

  it("deduplicates by absolute path and activates an existing tab", () => {
    let state = activateProjectFileTab({ files: [], activePath: null }, first);
    state = activateProjectFileTab(state, second);
    state = activateProjectFileTab(state, { ...first, size: 30 });

    expect(state.files.map((file) => file.path)).toEqual([first.path, second.path]);
    expect(state.files[0].size).toBe(30);
    expect(state.activePath).toBe(first.path);
  });

  it("selects the adjacent tab after closing the active file", () => {
    const state = { files: [first, second], activePath: first.path };
    expect(closeProjectFileTab(state, first.path)).toEqual({ files: [second], activePath: second.path });
    expect(closeProjectFileTab({ files: [first], activePath: first.path }, first.path))
      .toEqual({ files: [], activePath: null });
  });

  it("replaces the earliest opened tab after reaching the configured limit", () => {
    const third = { name: "notes.txt", path: "/work/notes.txt", size: 5, hidden: false, is_symlink: false };
    let state = activateProjectFileTab({ files: [], activePath: null }, first, 2);
    state = activateProjectFileTab(state, second, 2);
    state = activateProjectFileTab(state, first, 2);
    expect(state.files.map((file) => file.path)).toEqual([first.path, second.path]);

    state = activateProjectFileTab(state, third, 2);
    expect(state.files.map((file) => file.path)).toEqual([second.path, third.path]);
    expect(state.activePath).toBe(third.path);
  });

  it("shows a root-relative location and falls back to the absolute path", () => {
    expect(projectFileDisplayPath("/work/crab/src/App.tsx", ["/work/crab", "/work"])).toBe("crab/src/App.tsx");
    expect(projectFileDisplayPath("/outside/notes.txt", ["/work/crab"])).toBe("/outside/notes.txt");
  });

  it("treats Windows drive paths as case-insensitive", () => {
    const original = { ...first, path: "C:\\Work\\Crab\\README.md" };
    const refreshed = { ...original, path: "c:\\work\\crab\\readme.md", size: 42 };
    let state = activateProjectFileTab({ files: [], activePath: null }, original);
    state = activateProjectFileTab(state, refreshed);

    expect(state.files).toHaveLength(1);
    expect(state.files[0].size).toBe(42);
    expect(projectPathKey(original.path)).toBe(projectPathKey(refreshed.path));
    expect(projectFileDisplayPath(
      "c:\\work\\crab\\src\\App.tsx",
      ["C:\\Work\\Crab"],
    )).toBe("Crab/src/App.tsx");
  });
});

describe("ProjectFilesWorkspace", () => {
  it("keeps changes and file previews in one workspace across navigation, turn selection, and closing tabs", async () => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const file = { name: "README.md", path: "/work/README.md", size: 10, hidden: false, is_symlink: false };
    const turns: ChangeReviewTurn[] = [
      { id: "older", summary: { added: 2, removed: 1, files: [
        { path: "src/app.ts", action: "modify", added: 1, removed: 1, diff: "@@ -1 +1 @@\n-before\n+after" },
        { path: "removed.ts", action: "delete", added: 1, removed: 0, diff: null },
      ] } },
      { id: "latest", summary: { added: 1, removed: 0, files: [
        { path: "new.ts", action: "create", added: 1, removed: 0, diff: "@@ -0,0 +1 @@\n+latest change" },
      ] } },
    ];
    const api = {
      directories: vi.fn(async () => ({ path: "/work", parent: "/", directories: [], files: [file] })),
      workspaceFile: vi.fn(async () => ({ text: async () => "# File preview" } as Blob)),
    } as unknown as GatewayApi;
    function Harness() {
      const [selection, setSelection] = useState<ChangeReviewSelection | null>({ summaryId: "older", path: "src/app.ts", showAll: false });
      const [active, setActive] = useState(true);
      const [treeOpen, setTreeOpen] = useState(true);
      return <ProjectFilesWorkspace
        api={api} projectName="test" directories={["/work"]} width={640}
        openFiles={[file]} selectedFile={file} referencedPaths={new Set()}
        treeOpen={treeOpen} onToggleTree={() => setTreeOpen((value) => !value)}
        onClose={vi.fn()} onCloseFile={vi.fn()} onReference={vi.fn()}
        onWidthChange={vi.fn()} onWidthCommit={vi.fn()}
        onSelectFile={() => setActive(false)}
        changes={{ turns, selection, active,
          onOpen: (next) => { setSelection(next); setActive(true); },
          onActivate: () => setActive(true),
          onClose: () => { setSelection(null); setActive(false); },
        }}
      />;
    }
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<Harness />));
    await flush();
    const click = (selector: string) => act(() => container.querySelector<HTMLButtonElement>(selector)!.click());

    expect(container.querySelectorAll(".project-files-workspace")).toHaveLength(1);
    expect(container.querySelectorAll('[role="tab"]')).toHaveLength(2);
    expect(container.querySelector(".change-review-scroll")?.textContent).toContain("after");
    expect(container.querySelector(".project-change-tab [role=tab]")?.getAttribute("aria-selected")).toBe("true");

    click('.project-file-tab:not(.project-change-tab) [role="tab"]');
    expect(container.querySelector(".project-file-preview")?.textContent).toContain("File preview");
    expect(container.querySelector(".project-change-tab [role=tab]")?.getAttribute("aria-selected")).toBe("false");
    click('.project-change-tab [role="tab"]');
    expect(container.querySelector(".change-review-scroll")?.textContent).toContain("after");

    click('.change-review-tree-file[title="removed.ts"]');
    expect(container.querySelector(".change-review-scroll")?.textContent).toContain("没有保存完整 diff");
    expect(container.querySelector(".change-review-scroll")?.textContent).not.toContain("after");
    click(".change-review-all");
    expect(container.querySelectorAll(".change-review-scroll article")).toHaveLength(2);
    click('[aria-label="编辑记录"]');
    act(() => Array.from(document.querySelectorAll<HTMLButtonElement>('.change-review-turn-menu button')).find((button) => button.textContent === '最近一次')!.click());
    expect(container.querySelector(".change-review-scroll")?.textContent).toContain("latest change");
    expect(container.querySelectorAll(".change-review-tree-file")).toHaveLength(1);

    click('[aria-label="收起文件树"]');
    expect(container.querySelector(".project-files-workspace.tree-collapsed")).not.toBeNull();
    expect(container.querySelector(".change-review-scroll")?.textContent).toContain("latest change");
    click('[aria-label="关闭变更"]');
    expect(container.querySelector(".project-change-tab")).toBeNull();
    expect(container.querySelector(".project-file-preview")?.textContent).toContain("File preview");
    click('[aria-label="查看变更"]');
    expect(container.querySelector(".change-review-scroll")?.textContent).toContain("latest change");
    expect(api.workspaceFile).toHaveBeenCalledTimes(1);
    act(() => root.unmount());
    container.remove();
  });

  it("loads multiple roots lazily, filters loaded files, previews Markdown, and references paths", async () => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const directories = vi.fn(async (path: string, showHidden: boolean) => {
      if (path === "/work/crab") {
        return {
          path,
          parent: "/work",
          directories: [
            { name: "src", path: "/work/crab/src", hidden: false, is_symlink: false },
            ...(showHidden ? [{ name: ".git", path: "/work/crab/.git", hidden: true, is_symlink: false }] : []),
          ],
          files: [{ name: "README.md", path: "/work/crab/README.md", size: 18, hidden: false, is_symlink: false }],
        };
      }
      if (path === "/work/shared") {
        return {
          path,
          parent: "/work",
          directories: [],
          files: [{ name: "shared.txt", path: "/work/shared/shared.txt", size: 6, hidden: false, is_symlink: false }],
        };
      }
      return {
        path,
        parent: "/work/crab",
        directories: [],
        files: [{ name: "App.tsx", path: "/work/crab/src/App.tsx", size: 12, hidden: false, is_symlink: false }],
      };
    });
    const workspaceFile = vi.fn(async (path: string) => ({
      type: "text/plain",
      text: async () => path.endsWith("README.md") ? "# Project\n\nHello" : "export default 1",
    } as Blob));
    const onReference = vi.fn();
    const onClose = vi.fn();
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);

    await act(async () => root.render(
      <ControlledWorkspace
        api={{ directories, workspaceFile } as unknown as GatewayApi}
        projectName="Crab"
        directories={["/work/crab", "/work/shared"]}
        width={640}
        referencedPaths={new Set()}
        onClose={onClose}
        onReference={onReference}
        onWidthChange={vi.fn()}
        onWidthCommit={vi.fn()}
      />,
    ));
    await flush();

    expect(container.querySelector(".project-files-workspace")?.classList.contains("tree-open")).toBe(true);
    expect(container.querySelector(".project-file-preview")?.textContent).toContain("浏览文件");
    expect(directories).toHaveBeenCalledWith("/work/crab", false, true);
    expect(directories).toHaveBeenCalledWith("/work/shared", false, true);
    expect(container.textContent).toContain("README.md");
    expect(container.textContent).toContain("shared.txt");

    const src = Array.from(container.querySelectorAll<HTMLButtonElement>(".project-tree-row.directory"))
      .find((button) => button.textContent?.includes("src"))!;
    await act(async () => src.click());
    await flush();
    expect(directories).toHaveBeenCalledWith("/work/crab/src", false, true);

    const filter = container.querySelector<HTMLInputElement>(".project-file-filter input")!;
    act(() => changeInput(filter, "App"));
    expect(container.textContent).toContain("App.tsx");
    expect(container.textContent).not.toContain("shared.txt");

    act(() => changeInput(filter, ""));
    const readme = Array.from(container.querySelectorAll<HTMLButtonElement>(".project-tree-row.file"))
      .find((button) => button.textContent?.includes("README.md"))!;
    await act(async () => readme.click());
    await flush();
    expect(container.querySelector(".project-files-workspace")?.classList.contains("has-file")).toBe(true);
    expect(container.querySelectorAll<HTMLButtonElement>('.project-file-tabs [role="tab"]')).toHaveLength(1);
    expect(container.querySelector<HTMLButtonElement>('.project-file-tabs [role="tab"]')?.getAttribute("aria-selected")).toBe("true");
    const tab = container.querySelector<HTMLElement>(".project-file-tab.active")!;
    act(() => tab.dispatchEvent(new MouseEvent("mouseover", { bubbles: true })));
    expect(document.querySelector('[role="tooltip"]')?.textContent).toBe("crab/README.md");
    act(() => tab.dispatchEvent(new MouseEvent("mouseout", { bubbles: true })));
    expect(document.querySelector('[role="tooltip"]')).toBeNull();
    expect(workspaceFile).toHaveBeenCalledWith("/work/crab/README.md");
    expect(container.querySelector(".project-file-markdown h1")?.textContent).toBe("Project");

    const source = Array.from(container.querySelectorAll<HTMLButtonElement>(".project-file-view-toggle button"))
      .find((button) => button.textContent === "源码")!;
    act(() => source.click());
    expect(container.querySelector(".project-file-code")?.textContent).toContain("# Project");

    const reference = container.querySelector<HTMLButtonElement>(".project-file-reference")!;
    act(() => reference.click());
    expect(onReference).toHaveBeenCalledWith(expect.objectContaining({ path: "/work/crab/README.md" }));

    const shared = Array.from(container.querySelectorAll<HTMLButtonElement>(".project-tree-row.file"))
      .find((button) => button.textContent?.includes("shared.txt"))!;
    await act(async () => shared.click());
    await flush();
    expect(container.querySelectorAll<HTMLButtonElement>('.project-file-tabs [role="tab"]')).toHaveLength(2);
    expect(container.querySelector(".project-file-preview-title")?.textContent).toContain("shared.txt");

    const readmeTab = Array.from(container.querySelectorAll<HTMLButtonElement>('.project-file-tabs [role="tab"]'))
      .find((button) => button.textContent?.includes("README.md"))!;
    await act(async () => readmeTab.click());
    await flush();
    expect(container.querySelector(".project-file-markdown h1")?.textContent).toBe("Project");

    act(() => container.querySelector<HTMLButtonElement>('button[title="关闭 shared.txt"]')!.click());
    expect(container.querySelectorAll<HTMLButtonElement>('.project-file-tabs [role="tab"]')).toHaveLength(1);

    const hidden = container.querySelector<HTMLButtonElement>('button[title="显示点文件"]')!;
    await act(async () => hidden.click());
    await flush();
    expect(directories).toHaveBeenCalledWith("/work/crab", true, true);
    expect(container.textContent).toContain(".git");

    const toggleTree = container.querySelector<HTMLButtonElement>('.project-file-tabs-bar button[title="收起文件树"]')!;
    act(() => toggleTree.click());
    expect(container.querySelector(".project-files-workspace")?.classList.contains("tree-collapsed")).toBe(true);
    expect(container.querySelector(".project-file-preview")?.textContent).toContain("README.md");
    expect(container.textContent).toContain("README.md");
    expect(container.querySelector(".project-file-explorer")?.getAttribute("aria-hidden")).toBe("true");

    const reopenTree = container.querySelector<HTMLButtonElement>('.project-file-tabs-bar button[title="展开文件树"]')!;
    act(() => reopenTree.click());
    expect(container.querySelector(".project-files-workspace")?.classList.contains("tree-open")).toBe(true);

    act(() => container.querySelector<HTMLButtonElement>('button[title="收起文件查看"]')!.click());
    expect(onClose).toHaveBeenCalledOnce();

    await act(async () => root.unmount());
    container.remove();
  });

  it("creates and releases image preview URLs", async () => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const createObjectURL = vi.fn(() => "blob:project-preview");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });
    const directories = vi.fn(async () => ({
      path: "/work/crab",
      parent: "/work",
      directories: [],
      files: [{ name: "preview.png", path: "/work/crab/preview.png", size: 8, hidden: false, is_symlink: false }],
    }));
    const workspaceFile = vi.fn(async () => new Blob(["image"], { type: "image/png" }));
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);

    await act(async () => root.render(
      <ControlledWorkspace
        api={{ directories, workspaceFile } as unknown as GatewayApi}
        projectName="Crab"
        directories={["/work/crab"]}
        width={640}
        referencedPaths={new Set()}
        onClose={vi.fn()}
        onReference={vi.fn()}
        onWidthChange={vi.fn()}
        onWidthCommit={vi.fn()}
      />,
    ));
    await flush();
    const imageFile = container.querySelector<HTMLButtonElement>(".project-tree-row.file")!;
    await act(async () => imageFile.click());
    await flush();

    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(container.querySelector<HTMLImageElement>(".project-file-image img")?.src).toBe("blob:project-preview");

    await act(async () => root.unmount());
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:project-preview");
    container.remove();
  });
});
