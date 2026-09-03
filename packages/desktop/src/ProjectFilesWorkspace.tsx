import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Eye,
  EyeOff,
  FileCode2,
  FileImage,
  FileText,
  Folder,
  FolderOpen,
  LoaderCircle,
  PanelRightClose,
  Plus,
  Quote,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { isValidElement, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown, { type Components } from "react-markdown";
import SyntaxHighlighter from "react-syntax-highlighter/dist/esm/prism-light";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import css from "react-syntax-highlighter/dist/esm/languages/prism/css";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import jsx from "react-syntax-highlighter/dist/esm/languages/prism/jsx";
import markdown from "react-syntax-highlighter/dist/esm/languages/prism/markdown";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import rust from "react-syntax-highlighter/dist/esm/languages/prism/rust";
import tsx from "react-syntax-highlighter/dist/esm/languages/prism/tsx";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";
import remarkGfm from "remark-gfm";
import type { GatewayApi } from "./gateway";
import { projectPathKey, sameProjectPath } from "./pathUtils";
import type { WorkspaceDirectoryEntry, WorkspaceDirectoryListing, WorkspaceFileEntry } from "./types";

export { projectPathKey } from "./pathUtils";

SyntaxHighlighter.registerLanguage("bash", bash);
SyntaxHighlighter.registerLanguage("css", css);
SyntaxHighlighter.registerLanguage("javascript", javascript);
SyntaxHighlighter.registerLanguage("json", json);
SyntaxHighlighter.registerLanguage("jsx", jsx);
SyntaxHighlighter.registerLanguage("markdown", markdown);
SyntaxHighlighter.registerLanguage("python", python);
SyntaxHighlighter.registerLanguage("rust", rust);
SyntaxHighlighter.registerLanguage("tsx", tsx);
SyntaxHighlighter.registerLanguage("typescript", typescript);

const MAX_TEXT_PREVIEW_BYTES = 2 * 1024 * 1024;
const MAX_IMAGE_PREVIEW_BYTES = 10 * 1024 * 1024;
const IMAGE_EXTENSIONS = new Set(["gif", "jpeg", "jpg", "png", "webp"]);
const MARKDOWN_EXTENSIONS = new Set(["markdown", "md", "mdx"]);
const TEXT_EXTENSIONS = new Set([
  "bash", "bat", "c", "cc", "cfg", "cjs", "cmd", "conf", "cpp", "css", "cts", "cxx",
  "env", "fish", "gitattributes", "gitignore", "go", "gql", "graphql", "h", "hpp", "htm",
  "html", "ini", "java", "js", "json", "jsonc", "jsx", "kt", "kts", "less", "lock", "mjs",
  "mts", "php", "proto", "ps1", "py", "pyi", "rb", "rs", "sass", "scss", "sh", "sql", "svg",
  "swift", "toml", "ts", "tsx", "txt", "xml", "yaml", "yml", "zsh",
]);
const TEXT_FILENAMES = new Set([
  ".dockerignore", ".editorconfig", ".eslintignore", ".eslintrc", ".gitattributes", ".gitignore",
  ".npmrc", ".prettierignore", ".prettierrc", ".rgignore", ".stylelintignore", ".stylelintrc",
  "dockerfile", "gemfile", "license", "makefile", "procfile", "readme",
]);
const LANGUAGE_BY_EXTENSION: Record<string, string> = {
  bash: "bash", cjs: "javascript", css: "css", js: "javascript", json: "json", jsonc: "json",
  jsx: "jsx", markdown: "markdown", md: "markdown", mdx: "markdown", mjs: "javascript", py: "python",
  pyi: "python", rs: "rust", sh: "bash", ts: "typescript", tsx: "tsx", yaml: "yaml", yml: "yaml",
};
const HIGHLIGHTED_LANGUAGES = new Set([
  "bash", "css", "javascript", "json", "jsx", "markdown", "python", "rust", "tsx", "typescript",
]);

type PreviewKind = "image" | "markdown" | "text" | "unsupported";
type DirectoryState = {
  listing: WorkspaceDirectoryListing | null;
  loading: boolean;
  error: string | null;
};
type PreviewState = {
  status: "empty" | "loading" | "ready" | "error";
  kind: PreviewKind | null;
  text: string;
  imageUrl: string | null;
  error: string | null;
};

export type ProjectFileClassification = {
  kind: PreviewKind;
  language: string | null;
  reason: string | null;
};

export type ProjectFileTabsState = {
  files: WorkspaceFileEntry[];
  activePath: string | null;
};

function normalizeProjectFileTabLimit(maxTabs: number): number {
  if (!Number.isFinite(maxTabs)) return 5;
  return Math.min(50, Math.max(1, Math.round(maxTabs)));
}

export function limitProjectFileTabs(
  state: ProjectFileTabsState,
  maxTabs: number,
): ProjectFileTabsState {
  const limit = normalizeProjectFileTabLimit(maxTabs);
  if (state.files.length <= limit) return state;
  const files = state.files.slice(-limit);
  return {
    files,
    activePath: files.some((file) => sameProjectPath(file.path, state.activePath))
      ? state.activePath
      : files[files.length - 1]?.path ?? null,
  };
}

export function activateProjectFileTab(
  state: ProjectFileTabsState,
  file: WorkspaceFileEntry,
  maxTabs = 5,
): ProjectFileTabsState {
  const existing = state.files.findIndex((item) => sameProjectPath(item.path, file.path));
  const limit = normalizeProjectFileTabLimit(maxTabs);
  const retained = existing < 0
    ? limit > 1 ? state.files.slice(-(limit - 1)) : []
    : state.files;
  return {
    files: existing < 0
      ? [...retained, file]
      : state.files.map((item, index) => index === existing ? file : item),
    activePath: file.path,
  };
}

export function closeProjectFileTab(
  state: ProjectFileTabsState,
  path: string,
): ProjectFileTabsState {
  const index = state.files.findIndex((file) => sameProjectPath(file.path, path));
  if (index < 0) return state;
  const files = state.files.filter((file) => !sameProjectPath(file.path, path));
  return {
    files,
    activePath: sameProjectPath(state.activePath, path)
      ? files[Math.min(index, files.length - 1)]?.path ?? null
      : state.activePath,
  };
}

function basename(path: string): string {
  return path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;
}

export function projectFileDisplayPath(path: string, roots: string[]): string {
  const normalizedPath = projectPathKey(path);
  const matchingRoot = roots
    .map((root) => root.replace(/[\\/]+$/, ""))
    .filter((root) => {
      const normalizedRoot = projectPathKey(root);
      return root && (
        normalizedPath === normalizedRoot || normalizedPath.startsWith(`${normalizedRoot}/`)
      );
    })
    .sort((left, right) => right.length - left.length)[0];
  if (!matchingRoot) return path;
  const relative = path
    .slice(matchingRoot.length)
    .replace(/^[\\/]+/, "")
    .replace(/\\/g, "/");
  const rootName = basename(matchingRoot);
  if (!relative) return rootName;
  return rootName === "/" ? `/${relative}` : `${rootName}/${relative}`;
}

function extension(name: string): string {
  const index = name.lastIndexOf(".");
  return index > 0 ? name.slice(index + 1).toLowerCase() : "";
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function classifyProjectFile(file: Pick<WorkspaceFileEntry, "name" | "size">): ProjectFileClassification {
  const suffix = extension(file.name);
  const name = file.name.toLowerCase();
  if (IMAGE_EXTENSIONS.has(suffix)) {
    return file.size > MAX_IMAGE_PREVIEW_BYTES
      ? { kind: "image", language: null, reason: "图片超过 10 MiB 预览限制" }
      : { kind: "image", language: null, reason: null };
  }
  if (MARKDOWN_EXTENSIONS.has(suffix)) {
    return file.size > MAX_TEXT_PREVIEW_BYTES
      ? { kind: "markdown", language: "markdown", reason: "文件超过 2 MiB 预览限制" }
      : { kind: "markdown", language: "markdown", reason: null };
  }
  if (TEXT_EXTENSIONS.has(suffix) || TEXT_FILENAMES.has(name) || name.startsWith(".env")) {
    return file.size > MAX_TEXT_PREVIEW_BYTES
      ? { kind: "text", language: LANGUAGE_BY_EXTENSION[suffix] ?? null, reason: "文件超过 2 MiB 预览限制" }
      : { kind: "text", language: LANGUAGE_BY_EXTENSION[suffix] ?? null, reason: null };
  }
  return { kind: "unsupported", language: null, reason: "此文件类型暂不支持预览" };
}

function fileIcon(file: WorkspaceFileEntry) {
  const kind = classifyProjectFile(file).kind;
  if (kind === "image") return <FileImage />;
  if (kind === "text") return <FileCode2 />;
  return <FileText />;
}

function markdownCode(children: ReactNode): { language: string; source: string } | null {
  if (!isValidElement<{ className?: string; children?: ReactNode }>(children)) return null;
  const language = /(?:^|\s)language-([^\s]+)/.exec(children.props.className ?? "")?.[1]?.toLowerCase();
  if (!language) return null;
  return {
    language: LANGUAGE_BY_EXTENSION[language] ?? language,
    source: String(children.props.children ?? "").replace(/\n$/, ""),
  };
}

const PROJECT_MARKDOWN_COMPONENTS: Components = {
  img: ({ alt }) => <span className="project-markdown-image-placeholder">{alt ? `[图片：${alt}]` : "[相对图片未加载]"}</span>,
  a: ({ href, children }) => (
    typeof href === "string" && /^(?:https?:|mailto:)/i.test(href)
      ? <a href={href} target="_blank" rel="noreferrer">{children}</a>
      : <span>{children}</span>
  ),
  pre: ({ children }) => {
    const code = markdownCode(children);
    if (!code || !HIGHLIGHTED_LANGUAGES.has(code.language)) return <pre>{children}</pre>;
    return (
      <SyntaxHighlighter
        className="project-file-code"
        language={code.language}
        PreTag="pre"
        CodeTag="code"
        useInlineStyles={false}
        customStyle={{}}
      >
        {code.source}
      </SyntaxHighlighter>
    );
  },
};

function SourcePreview({ text, language }: { text: string; language: string | null }) {
  if (!language || !HIGHLIGHTED_LANGUAGES.has(language)) return <pre className="project-file-plain">{text}</pre>;
  return (
    <SyntaxHighlighter
      className="project-file-code"
      language={language}
      PreTag="pre"
      CodeTag="code"
      showLineNumbers
      useInlineStyles={false}
      customStyle={{}}
    >
      {text}
    </SyntaxHighlighter>
  );
}

export function ProjectFilesWorkspace({
  api,
  projectName,
  directories,
  drawer = false,
  treeOpen,
  width,
  openFiles,
  selectedFile,
  referencedPaths,
  onToggleTree,
  onClose,
  onCloseFile,
  onReference,
  onSelectFile,
  onWidthChange,
  onWidthCommit,
}: {
  api: GatewayApi;
  projectName: string;
  directories: string[];
  drawer?: boolean;
  treeOpen: boolean;
  width: number;
  openFiles: WorkspaceFileEntry[];
  selectedFile: WorkspaceFileEntry | null;
  referencedPaths: ReadonlySet<string>;
  onToggleTree: () => void;
  onClose: () => void;
  onCloseFile: (path: string) => void;
  onReference: (file: WorkspaceFileEntry) => void;
  onSelectFile: (file: WorkspaceFileEntry) => void;
  onWidthChange: (width: number) => void;
  onWidthCommit: (width: number) => void;
}) {
  const [listings, setListings] = useState<Record<string, DirectoryState>>({});
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(directories));
  const [showHidden, setShowHidden] = useState(false);
  const [filter, setFilter] = useState("");
  const [markdownSource, setMarkdownSource] = useState(false);
  const [preview, setPreview] = useState<PreviewState>({
    status: "empty", kind: null, text: "", imageUrl: null, error: null,
  });
  const [tabTooltip, setTabTooltip] = useState<{ label: string; left: number; top: number } | null>(null);
  const generationRef = useRef(0);
  const expandedRef = useRef(expanded);
  const workspaceRef = useRef<HTMLElement | null>(null);
  const rootsKey = directories.join("\u0000");
  expandedRef.current = expanded;

  const requestDirectory = useCallback(async (path: string, generation: number) => {
    setListings((current) => ({
      ...current,
      [path]: { listing: current[path]?.listing ?? null, loading: true, error: null },
    }));
    try {
      const listing = await api.directories(path, showHidden, true);
      if (generation !== generationRef.current) return;
      setListings((current) => ({ ...current, [path]: { listing, loading: false, error: null } }));
    } catch (reason) {
      if (generation !== generationRef.current) return;
      setListings((current) => ({
        ...current,
        [path]: {
          listing: current[path]?.listing ?? null,
          loading: false,
          error: reason instanceof Error ? reason.message : String(reason),
        },
      }));
    }
  }, [api, showHidden]);

  useEffect(() => {
    setFilter("");
    setMarkdownSource(false);
    const next = new Set(directories);
    setExpanded(next);
    expandedRef.current = next;
  }, [api, rootsKey]);

  useEffect(() => {
    const generation = ++generationRef.current;
    const paths = new Set([...directories, ...expandedRef.current]);
    setListings({});
    paths.forEach((path) => void requestDirectory(path, generation));
    return () => {
      generationRef.current += 1;
    };
  }, [api, requestDirectory, rootsKey, showHidden]);

  useEffect(() => {
    setMarkdownSource(false);
    if (!selectedFile) {
      setPreview({ status: "empty", kind: null, text: "", imageUrl: null, error: null });
      return undefined;
    }
    const classification = classifyProjectFile(selectedFile);
    if (classification.reason) {
      setPreview({ status: "error", kind: classification.kind, text: "", imageUrl: null, error: classification.reason });
      return undefined;
    }
    let cancelled = false;
    let imageUrl: string | null = null;
    setPreview({ status: "loading", kind: classification.kind, text: "", imageUrl: null, error: null });
    void api.workspaceFile(selectedFile.path)
      .then(async (blob) => {
        if (cancelled) return;
        if (classification.kind === "image") {
          imageUrl = URL.createObjectURL(blob);
          setPreview({ status: "ready", kind: "image", text: "", imageUrl, error: null });
          return;
        }
        const text = await blob.text();
        if (cancelled) return;
        setPreview({ status: "ready", kind: classification.kind, text, imageUrl: null, error: null });
      })
      .catch((reason) => {
        if (cancelled) return;
        const detail = reason instanceof Error ? reason.message : String(reason);
        const legacy = /404|not found/i.test(detail)
          ? "当前 Gateway 不支持文件预览，或文件已不存在"
          : detail;
        setPreview({ status: "error", kind: classification.kind, text: "", imageUrl: null, error: legacy });
      });
    return () => {
      cancelled = true;
      if (imageUrl) URL.revokeObjectURL(imageUrl);
    };
  }, [api, selectedFile]);

  const refresh = () => {
    const generation = ++generationRef.current;
    const paths = new Set([...directories, ...expandedRef.current]);
    setListings({});
    paths.forEach((path) => void requestDirectory(path, generation));
  };

  const toggleDirectory = (path: string) => {
    const next = new Set(expandedRef.current);
    if (next.has(path)) {
      next.delete(path);
    } else {
      next.add(path);
      if (!listings[path]) void requestDirectory(path, generationRef.current);
    }
    expandedRef.current = next;
    setExpanded(next);
  };

  const normalizedFilter = filter.trim().toLowerCase();
  const directoryMatches = useCallback((path: string, name: string, visited = new Set<string>()): boolean => {
    if (!normalizedFilter || name.toLowerCase().includes(normalizedFilter)) return true;
    if (visited.has(path)) return false;
    const nextVisited = new Set(visited).add(path);
    const listing = listings[path]?.listing;
    if (!listing) return false;
    return (listing.files ?? []).some((file) => file.name.toLowerCase().includes(normalizedFilter))
      || listing.directories.some((directory) => directoryMatches(directory.path, directory.name, nextVisited));
  }, [listings, normalizedFilter]);

  const renderDirectory = (directory: WorkspaceDirectoryEntry, depth: number, ancestry: Set<string>): ReactNode => {
    if (!directoryMatches(directory.path, directory.name)) return null;
    const state = listings[directory.path];
    const open = expanded.has(directory.path) || Boolean(normalizedFilter && state?.listing);
    const cyclic = ancestry.has(directory.path);
    const nextAncestry = new Set(ancestry).add(directory.path);
    const rowStyle = { "--project-tree-depth": depth } as CSSProperties;
    return (
      <div className="project-tree-node" key={directory.path}>
        <button
          className="project-tree-row directory"
          style={rowStyle}
          type="button"
          title={directory.path}
          aria-expanded={open}
          onClick={() => !cyclic && toggleDirectory(directory.path)}
        >
          {state?.loading ? <LoaderCircle className="spin" /> : open ? <ChevronDown /> : <ChevronRight />}
          {open ? <FolderOpen /> : <Folder />}
          <span>{directory.name}</span>
          {directory.is_symlink && <small>链接</small>}
        </button>
        {open && !cyclic && (
          <div className="project-tree-children">
            {state?.error && <div className="project-tree-error" style={rowStyle}><AlertTriangle />{state.error}</div>}
            {state?.listing?.directories.map((child) => renderDirectory(child, depth + 1, nextAncestry))}
            {(state?.listing?.files ?? [])
              .filter((file) => !normalizedFilter || file.name.toLowerCase().includes(normalizedFilter))
              .map((file) => (
                <button
                  className={`project-tree-row file ${sameProjectPath(selectedFile?.path ?? null, file.path) ? "selected" : ""}`}
                  style={{ "--project-tree-depth": depth + 1 } as CSSProperties}
                  type="button"
                  title={`${file.path}\n${formatFileSize(file.size)}`}
                  key={file.path}
                  onClick={() => onSelectFile(file)}
                >
                  <span className="project-tree-file-spacer" />
                  {fileIcon(file)}
                  <span>{file.name}</span>
                  {file.is_symlink && <small>链接</small>}
                </button>
              ))}
            {state?.listing && state.listing.directories.length === 0 && (state.listing.files ?? []).length === 0 && (
              <div className="project-tree-empty" style={rowStyle}>空目录</div>
            )}
          </div>
        )}
      </div>
    );
  };

  const rootNodes = useMemo(() => directories.map((path) => ({
    name: basename(path), path, hidden: basename(path).startsWith("."), is_symlink: false,
  })), [rootsKey]);
  const selectedClassification = selectedFile ? classifyProjectFile(selectedFile) : null;
  const referenced = Boolean(selectedFile && referencedPaths.has(selectedFile.path));

  const showTabTooltip = (file: WorkspaceFileEntry, element: HTMLElement) => {
    const bounds = element.getBoundingClientRect();
    const viewportPadding = 12;
    const maximumWidth = Math.min(560, window.innerWidth - viewportPadding * 2);
    const halfWidth = maximumWidth / 2;
    setTabTooltip({
      label: projectFileDisplayPath(file.path, directories),
      left: Math.min(
        Math.max(halfWidth + viewportPadding, bounds.left + bounds.width / 2),
        Math.max(halfWidth + viewportPadding, window.innerWidth - halfWidth - viewportPadding),
      ),
      top: bounds.bottom + 8,
    });
  };

  const startResize = (event: React.PointerEvent<HTMLDivElement>) => {
    if (drawer) return;
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = width;
    const parentWidth = workspaceRef.current?.parentElement?.getBoundingClientRect().width ?? window.innerWidth;
    const maximum = Math.max(480, Math.min(1_000, parentWidth - 420));
    let latest = startWidth;
    const move = (moveEvent: PointerEvent) => {
      latest = Math.max(480, Math.min(maximum, startWidth + startX - moveEvent.clientX));
      onWidthChange(Math.round(latest));
    };
    const finish = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", finish);
      document.body.classList.remove("project-files-resizing");
      onWidthCommit(Math.round(latest));
    };
    document.body.classList.add("project-files-resizing");
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", finish, { once: true });
  };

  return (
    <>
    <aside
      ref={workspaceRef}
      className={`project-files-workspace ${selectedFile ? "has-file" : "empty-preview"} ${treeOpen ? "tree-open" : "tree-collapsed"} ${drawer ? "drawer" : ""}`}
      style={{ "--project-files-width": `${width}px` } as CSSProperties}
      aria-label={`${projectName} 文件工作区`}
    >
      {!drawer && <div className="project-files-resizer" role="separator" aria-orientation="vertical" onPointerDown={startResize} />}
      <div className="project-file-tabs-bar">
        <div className="project-file-tabs" role="tablist" aria-label="打开的文件">
          {openFiles.length === 0 && (
            <div className="project-file-tab empty active">
              <FolderOpen />
              <span>浏览文件</span>
            </div>
          )}
          {openFiles.map((file) => {
            const active = sameProjectPath(selectedFile?.path ?? null, file.path);
            return (
              <div
                className={`project-file-tab ${active ? "active" : ""}`}
                key={file.path}
                onMouseEnter={(event) => showTabTooltip(file, event.currentTarget)}
                onMouseLeave={() => setTabTooltip(null)}
                onFocusCapture={(event) => showTabTooltip(file, event.currentTarget)}
                onBlurCapture={() => setTabTooltip(null)}
              >
                <button type="button" role="tab" aria-selected={active} onClick={() => onSelectFile(file)}>
                  {fileIcon(file)}
                  <span>{file.name}</span>
                </button>
                <button
                  className="project-file-tab-close"
                  type="button"
                  title={`关闭 ${file.name}`}
                  aria-label={`关闭 ${file.name}`}
                  onClick={() => {
                    setTabTooltip(null);
                    onCloseFile(file.path);
                  }}
                >
                  <X />
                </button>
              </div>
            );
          })}
          <button
            className="project-file-new-tab"
            type="button"
            title="打开其他文件"
            aria-label="打开其他文件"
            onClick={() => {
              if (!treeOpen) onToggleTree();
            }}
          >
            <Plus />
          </button>
        </div>
        <div className="project-file-tabs-actions">
          <button
            className={`icon-button tiny ${treeOpen ? "active" : ""}`}
            type="button"
            title={treeOpen ? "收起文件树" : "展开文件树"}
            aria-label={treeOpen ? "收起文件树" : "展开文件树"}
            aria-pressed={treeOpen}
            onClick={onToggleTree}
          >
            {treeOpen ? <FolderOpen /> : <Folder />}
          </button>
          <button className="icon-button tiny" type="button" title="收起文件查看" aria-label="收起文件查看" onClick={onClose}>
            <PanelRightClose />
          </button>
        </div>
      </div>
      <section className="project-file-preview">
        <header>
          <div className="project-file-preview-title">
            {selectedFile ? fileIcon(selectedFile) : <FolderOpen />}
            <span>
              <strong>{selectedFile?.name ?? "浏览文件"}</strong>
              <small title={selectedFile?.path}>{selectedFile?.path ?? "从文件树选择要查看的文件"}</small>
            </span>
          </div>
          <div className="project-file-preview-actions">
            {selectedClassification?.kind === "markdown" && preview.status === "ready" && (
              <div className="project-file-view-toggle">
                <button className={!markdownSource ? "active" : ""} type="button" onClick={() => setMarkdownSource(false)}>预览</button>
                <button className={markdownSource ? "active" : ""} type="button" onClick={() => setMarkdownSource(true)}>源码</button>
              </div>
            )}
            {selectedFile && (
              <button
                className={`project-file-reference ${referenced ? "active" : ""}`}
                type="button"
                disabled={referenced}
                onClick={() => onReference(selectedFile)}
              >
                <Quote />{referenced ? "已引用" : "引用"}
              </button>
            )}
          </div>
        </header>
        <div className="project-file-preview-body">
          {preview.status === "empty" && (
            <div className="project-file-preview-empty"><FolderOpen /><strong>浏览文件</strong><span>{treeOpen ? "从右侧文件树选择文件" : "使用右上角文件夹按钮打开文件树"}</span></div>
          )}
          {preview.status === "loading" && (
            <div className="project-file-preview-empty"><LoaderCircle className="spin" /><strong>正在读取文件</strong></div>
          )}
          {preview.status === "error" && (
            <div className="project-file-preview-empty error"><AlertTriangle /><strong>无法预览</strong><span>{preview.error}</span></div>
          )}
          {preview.status === "ready" && preview.kind === "image" && preview.imageUrl && (
            <div className="project-file-image"><img src={preview.imageUrl} alt={selectedFile?.name ?? "文件图片"} /></div>
          )}
          {preview.status === "ready" && preview.kind === "markdown" && !markdownSource && (
            <article className="project-file-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={PROJECT_MARKDOWN_COMPONENTS}>{preview.text}</ReactMarkdown></article>
          )}
          {preview.status === "ready" && (preview.kind === "text" || (preview.kind === "markdown" && markdownSource)) && (
            <SourcePreview text={preview.text} language={selectedClassification?.language ?? null} />
          )}
        </div>
      </section>

      <section
        className="project-file-explorer"
        aria-hidden={!treeOpen}
        {...(!treeOpen ? { inert: "" } : {})}
      >
        <header>
          <strong>文件</strong>
          <div>
            <button className="icon-button tiny" type="button" title={showHidden ? "隐藏点文件" : "显示点文件"} onClick={() => setShowHidden((value) => !value)}>
              {showHidden ? <EyeOff /> : <Eye />}
            </button>
            <button className="icon-button tiny" type="button" title="刷新文件树" onClick={refresh}><RefreshCw /></button>
          </div>
        </header>
        <label className="project-file-filter">
          <Search />
          <input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="筛选已加载文件…" />
        </label>
        <div className="project-file-tree">
          {rootNodes.map((root) => renderDirectory(root, 0, new Set()))}
          {rootNodes.length === 0 && <div className="project-tree-empty">项目没有可浏览的目录</div>}
        </div>
      </section>
    </aside>
    {tabTooltip && createPortal(
      <div
        className="project-file-tab-tooltip"
        role="tooltip"
        style={{ left: tabTooltip.left, top: tabTooltip.top }}
      >
        {tabTooltip.label}
      </div>,
      document.body,
    )}
    </>
  );
}
