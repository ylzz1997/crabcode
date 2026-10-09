/* @vitest-environment jsdom */

import { act, useState, type ComponentProps } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import DocumentWorkspace from "./DocumentWorkspace";
import { ProjectFilesWorkspace } from "./ProjectFilesWorkspace";
import type { GatewayApi } from "./gateway";
import type { ChangeReviewSelection } from "./ChangeReview";
import type { DocumentManifest, ProjectPreset, WorkspaceFileEntry } from "./types";

const pdf = vi.hoisted(() => ({
  numPages: 1,
  fingerprints: ["test"],
  getPage: vi.fn(async () => ({
    rotate: 0,
    getViewport: ({ scale }: { scale: number }) => ({ width: 600 * scale, height: 800 * scale }),
    getTextContent: async () => ({ items: [] }),
    streamTextContent: () => ({}),
  })),
  destroy: vi.fn(),
}));

vi.mock("pdfjs-dist", () => ({
  GlobalWorkerOptions: {},
  getDocument: () => ({ promise: Promise.resolve(pdf) }),
  TextLayer: class {
    render() { return Promise.resolve(); }
    cancel() {}
  },
}));

describe("document file workspace", () => {
  let container: HTMLDivElement;
  let root: Root;
  let api: GatewayApi;
  const onDocumentViewState = vi.fn();
  const file: WorkspaceFileEntry = { path: "/paper/Blog.md", name: "Blog.md", size: 10, hidden: false, is_symlink: false };
  const project: ProjectPreset = { id: "paper", name: "paper", kind: "document", path: "/paper", directories: ["/paper"], last_session_id: null };
  const turns = [{ id: "edit", summary: { added: 1, removed: 1, files: [
    { path: "Blog.md", action: "modify" as const, added: 1, removed: 1, diff: "@@ -1 +1 @@\n-old content\n+new content" },
  ] } }];

  beforeEach(() => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.clearAllMocks();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
    api = {
      documentManifest: vi.fn(async () => ({
        source: { name: "paper.pdf" }, pdf: { page_count: 1 },
        layout: { fingerprint: "test:paragraph-v1" }, translations: {}, blog: null, jobs: {},
      } as DocumentManifest)),
      documentAsset: vi.fn(async () => new ArrayBuffer(0)),
      documentBlog: vi.fn(async () => ({ markdown: "# Original Blog", revision: "1", language: "en" })),
      documentCapabilities: vi.fn(async () => ({})),
      documentAnnotations: vi.fn(async () => []),
      documentTranslation: vi.fn(async () => { throw new Error("404 Not Found"); }),
      directories: vi.fn(async () => ({ path: "/paper", parent: "/", directories: [], files: [file] })),
      workspaceFile: vi.fn(async () => ({ text: async () => "# File preview" } as Blob)),
      gitReviewInfo: vi.fn(async () => ({ available: false, branches: [], commits: [] })),
    } as unknown as GatewayApi;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  });

  function Harness() {
    const [view, setView] = useState<"files" | "changes" | null>(null);
    const [selectedFile, setSelectedFile] = useState<WorkspaceFileEntry | null>(null);
    const [selection, setSelection] = useState<ChangeReviewSelection>({ summaryId: "edit", path: null, showAll: true });
    const filesWorkspace: ComponentProps<typeof DocumentWorkspace>["filesWorkspace"] = {
      activeView: view, changeCount: 1, onViewChange: setView,
      content: <ProjectFilesWorkspace
        api={api} projectName="paper" projectPath="/paper" directories={["/paper"]} embedded
        treeOpen width={640} openFiles={selectedFile ? [selectedFile] : []} selectedFile={selectedFile} referencedPaths={new Set()}
        onClose={() => setView(null)} onToggleTree={vi.fn()} onCloseFile={() => setSelectedFile(null)} onReference={vi.fn()}
        onSelectFile={(file) => { setSelectedFile(file); setView("files"); }} onWidthChange={vi.fn()} onWidthCommit={vi.fn()}
        changes={{ turns, selection, active: view === "changes",
          onOpen: (next) => { setSelection(next); setView("changes"); },
          onActivate: () => setView("changes"), onClose: () => setView("files"),
        }}
      />,
    };
    return <DocumentWorkspace
      api={api} connectionId="local" project={project} agentWidth={400} agentCollapsed={false} showOriginalText={false}
      translationConcurrency={1} translationBatchSize={1} sessionBusy={false} sessionError={null} selectionTranslationEvent={null}
      onAgentWidth={vi.fn()} onAgentCollapsed={vi.fn()} onDocumentViewState={onDocumentViewState}
      onDocumentAction={vi.fn()} onDocumentReference={vi.fn()} onTranslateSelection={vi.fn()} filesWorkspace={filesWorkspace}
    />;
  }

  async function render() {
    await act(async () => root.render(<Harness />));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    expect(container.querySelector(".document-loading")).toBeNull();
  }

  function clickTab(label: string) {
    act(() => Array.from(container.querySelectorAll<HTMLButtonElement>(".document-view-tabs button"))
      .find((button) => button.textContent?.startsWith(label))!.click());
  }

  it("embeds files and changes in the document workspace and preserves the reader while hidden", async () => {
    await render();
    const scroll = container.querySelector<HTMLDivElement>(".document-pdf-scroll")!;
    act(() => container.querySelector<HTMLButtonElement>('[aria-label="放大"]')!.click());
    scroll.scrollTop = 420;
    act(() => scroll.dispatchEvent(new Event("scroll", { bubbles: true })));
    clickTab("文件");
    expect(scroll.hidden).toBe(true);
    expect(container.querySelector(".document-file-workspace .project-files-workspace.embedded")).not.toBeNull();
    expect(container.querySelector(".project-files-resizer")).toBeNull();
    expect(container.querySelector(".project-files-workspace.drawer")).toBeNull();

    // A hidden reader has no layout box; its reported offset must not overwrite saved reading state.
    scroll.scrollTop = 0;
    act(() => scroll.dispatchEvent(new Event("scroll", { bubbles: true })));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 280)); });
    expect(onDocumentViewState).toHaveBeenLastCalledWith("local", "paper", { zoom: 1.3, scroll_top: 420, scroll_left: 0 });
    clickTab("变更");
    expect(container.querySelector(".change-review-scroll")?.textContent).toContain("new content");
    clickTab("文档");
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    expect(container.querySelector(".document-pdf-scroll")).toBe(scroll);
    expect(scroll.hidden).toBe(false);
    expect(scroll.scrollTop).toBe(420);
    expect(container.querySelector(".document-zoom")?.textContent).toBe("130%");
    expect(api.documentAsset).toHaveBeenCalledOnce();
    expect(pdf.destroy).not.toHaveBeenCalled();
  });

  it("keeps Blog edits and file selection when switching workspace views", async () => {
    await render();
    clickTab("Blog");
    act(() => Array.from(container.querySelectorAll<HTMLButtonElement>(".document-blog-view-tabs button"))
      .find((button) => button.textContent === "Raw")!.click());
    const editor = container.querySelector<HTMLTextAreaElement>('[aria-label="Markdown Blog 编辑器"]')!;
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(editor, "# Unsaved edit");
      editor.dispatchEvent(new Event("input", { bubbles: true }));
    });
    clickTab("文件");
    await act(async () => container.querySelector<HTMLButtonElement>(".project-tree-row.file")!.click());
    expect(container.querySelector(".project-file-preview")?.textContent).toContain("File preview");
    clickTab("变更");
    expect(container.querySelector(".change-review-scroll")?.textContent).toContain("new content");
    clickTab("Blog");
    await act(async () => { await Promise.resolve(); });
    expect(container.querySelector('[aria-label="Markdown Blog 编辑器"]')).toBe(editor);
    expect(editor.value).toBe("# Unsaved edit");
    clickTab("文件");
    await act(async () => { await Promise.resolve(); });
    expect(container.querySelector(".project-file-preview")?.textContent).toContain("File preview");
  });
});
