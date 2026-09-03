import {
  AlertTriangle,
  Activity,
  ArrowUpRight,
  Bot,
  Boxes,
  Brain,
  Check,
  Code2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock,
  GitBranch,
  Circle,
  CircleDotDashed,
  Gauge,
  FileDiff,
  FileText,
  Folder,
  FolderInput,
  FolderOpen,
  FolderPlus,
  History,
  Image as ImageIcon,
  ListTodo,
  LoaderCircle,
  Lock,
  MessageSquarePlus,
  MoreHorizontal,
  Pause,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  Pencil,
  Play,
  Plus,
  Puzzle,
  Quote,
  RefreshCw,
  RotateCcw,
  Search,
  Send,
  Server,
  ShieldCheck,
  Settings,
  ShieldAlert,
  Square,
  Sparkles,
  Star,
  Target,
  Terminal,
  Trash2,
  Timer,
  Zap,
  Wrench,
  Workflow,
  WifiOff,
  X,
  ZoomIn,
  ZoomOut,
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
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import DocumentWorkspace from "./DocumentWorkspace";
import { ComposerEditor, composerModifierLabel, createComposerCommandOptions, type ComposerReferenceOption } from "./ComposerEditor";
import { CopyButton } from "./CopyButton";
import { applyGatewayEvent } from "./events";
import { normalizeMarkdownMathDelimiters } from "./markdownMath";
import {
  addFavoriteEntry,
  countFavoriteItems,
  deleteFavoriteFolder,
  favoriteEntries,
  favoriteFolderOptions,
  favoriteParentId,
  favoriteSessionIdsForProject,
  hasFavoriteProject,
  hasFavoriteSession,
  moveFavoriteEntry,
  removeFavoriteEntries,
  renameFavoriteFolder,
  resolveFavoriteEntries,
  type FavoriteViewEntry,
  type FavoriteFolderDeleteMode,
} from "./favorites";
import { GatewayApi, SessionChannel } from "./gateway";
import { SettingsView, type SettingsSectionId } from "./SettingsView";
import {
  activateProjectFileTab,
  closeProjectFileTab,
  limitProjectFileTabs,
  projectPathKey,
  ProjectFilesWorkspace,
  type ProjectFileTabsState,
} from "./ProjectFilesWorkspace";
import { TrajectoryView } from "./TrajectoryView";
import { getToolPresentation, parseChecklistResult, type ToolField } from "./toolPresentation";
import {
  DEFAULT_THEME_ID,
  addImportedTheme,
  deleteCustomTheme,
  duplicateTheme,
  renameCustomTheme,
  resolveActiveTheme,
  resolveThemeTokens,
  updateActiveThemeProfile,
} from "./theme";
import {
  deleteCredential,
  ensureLocalGateway,
  getDocumentEngineStatus,
  installDocumentEngine,
  isDesktopShell,
  isInsecureRemoteUrl,
  isLoopbackUrl,
  loadSettings,
  normalizeBaseUrl,
  removeDocumentEngine,
  saveSettings,
  setDockIcon,
  storeCredential,
  type DocumentEngineInstallProgress,
} from "./native";
import type {
  BackgroundTaskInfo,
  ChatItem,
  CheckpointInfo,
  ConnectionPreset,
  DesktopSettings,
  DocumentCapabilities,
  DocumentReference,
  FavoriteEntry,
  FavoriteFolder,
  GatewayEvent,
  GatewayModel,
  GatewayViewState,
  ModelSettingsResponse,
  ModelSettingsMutation,
  RuntimeSettingsResponse,
  RuntimeSettingsMutation,
  ProjectPreset,
  ReasoningEffort,
  ScheduleJobInfo,
  SessionPreferences,
  SessionInfo,
  SessionViewState,
  SkillInfo,
  ToolInfo,
  ThemePreset,
  ThemeVisualSlot,
  TurnDurationFormat,
  WorkspaceDirectoryListing,
} from "./types";

type GatewayMap = Record<string, GatewayViewState>;
type SessionMap = Record<string, SessionViewState>;
type WorkspaceView = "chat" | "scheduled" | "plugins" | "favorites";
type ConversationView = "chat" | "trajectory";
type AutomationTab = "schedule" | "monitor";
type ScheduleAction = "pause" | "resume" | "trigger" | "cancel";
type ScheduleActionState = { id: string; action: ScheduleAction };
type PluginData = { skills: SkillInfo[]; tools: ToolInfo[] };
type ModelSettingsLoadState = {
  key: string;
  data: ModelSettingsResponse | null;
  loading: boolean;
  error: string | null;
};
type RuntimeSettingsLoadState = {
  key: string;
  data: RuntimeSettingsResponse | null;
  loading: boolean;
  error: string | null;
};

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

const MESSAGE_CODE_LANGUAGE_ALIASES: Record<string, string> = {
  js: "javascript",
  md: "markdown",
  py: "python",
  rs: "rust",
  sh: "bash",
  shell: "bash",
  ts: "typescript",
};
const MESSAGE_CODE_LANGUAGES = new Set([
  "bash", "css", "javascript", "json", "jsx", "markdown", "python", "rust", "tsx", "typescript",
]);

const DESKTOP_COMMAND_NAMES = new Set([
  "/help", "/plan", "/agent", "/status", "/effort", "/ultra", "/model", "/new",
  "/compact", "/clear", "/sessions", "/recent", "/search", "/archive", "/stats",
  "/checkpoint", "/checkpoints", "/rollback", "/revert", "/undo", "/resume", "/goal",
  "/tasks", "/schedule",
]);
export type FavoriteSessionItem = { project: ProjectPreset; session: SessionInfo };
type PermissionMode = "default" | "ask" | "ai_review" | "run_everything";
type SessionCleanupTarget = {
  connectionId: string;
  cwd: string;
  key: string;
  sessionId: string;
};
type FocusedSessionSnapshot = SessionCleanupTarget & {
  empty: boolean;
  busy: boolean;
  projectPath: string;
  view: WorkspaceView;
};

const DECORATION_SLOTS: readonly ThemeVisualSlot[] = [
  "app_background",
  "workspace_background",
  "sidebar_overlay",
  "welcome_character_left",
  "welcome_character_right",
  "top_trim",
  "bottom_trim",
];

function ThemeDecorations({ theme }: { theme: ThemePreset }) {
  if (!theme.visuals) return null;
  return (
    <div className="theme-visuals" aria-hidden="true">
      {DECORATION_SLOTS.map((slot) => {
        const visual = theme.visuals?.[slot];
        if (!visual) return null;
        const style: CSSProperties = {
          backgroundImage: `url("${visual.data_url}")`,
          backgroundPosition: visual.position,
          backgroundSize: visual.fit,
          opacity: visual.opacity,
        };
        return <div className={`theme-visual theme-visual-${slot.replaceAll("_", "-")}`} style={style} key={slot} />;
      })}
    </div>
  );
}

export function shouldAutoOpenDocumentSession(
  workspaceView: WorkspaceView,
  hasConnection: boolean,
  projectKind: ProjectPreset["kind"] | undefined,
  activeSessionKey: string | null,
  gatewayStatus: GatewayViewState["status"] | undefined,
): boolean {
  return workspaceView === "chat"
    && hasConnection
    && projectKind === "document"
    && !activeSessionKey
    && gatewayStatus === "online";
}

export function shouldUseWideProjectFilesLayout(width: number): boolean {
  return width >= 1280;
}

export function shouldCollapseDocumentAgent(documentMode: boolean, collapsedSetting: boolean): boolean {
  return documentMode && collapsedSetting;
}
type PendingImage = {
  id: string;
  name: string;
  media_type: string;
  data: string;
  dataUrl: string;
};
export type PendingFile = {
  id: string;
  name: string;
  mediaType: string;
  mode: "content" | "path";
  path: string | null;
  size: number | null;
  text: string;
};

export function formatDocumentReferenceLocation(reference: DocumentReference): string {
  if (reference.line_start === undefined || reference.line_end === undefined) return reference.page_label;
  return `${reference.page_label} [${reference.line_start}-${reference.line_end}]`;
}

export function documentReferencePreview(text: string, edgeLength = 72): string {
  const normalized = text.replace(/\s+/g, " ").trim();
  if (normalized.length <= edgeLength * 2 + 1) return normalized;
  return `${normalized.slice(0, edgeLength).trimEnd()}…${normalized.slice(-edgeLength).trimStart()}`;
}

function DocumentReferenceAttachment({
  reference,
  onRemove,
}: {
  reference: DocumentReference;
  onRemove: () => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [tooltipPosition, setTooltipPosition] = useState<{ left: number; top: number } | null>(null);
  const preview = documentReferencePreview(reference.text);
  const showTooltip = useCallback(() => {
    const bounds = rootRef.current?.getBoundingClientRect();
    if (!bounds) return;
    setTooltipPosition({
      left: Math.max(176, Math.min(window.innerWidth - 176, bounds.left + bounds.width / 2)),
      top: bounds.top - 8,
    });
  }, []);
  return (
    <>
      <div
        ref={rootRef}
        className="document-reference-attachment"
        onMouseEnter={showTooltip}
        onMouseLeave={() => setTooltipPosition(null)}
        onFocusCapture={showTooltip}
        onBlurCapture={() => setTooltipPosition(null)}
      >
        <Quote />
        <span><strong>文档引用</strong><small>{formatDocumentReferenceLocation(reference)}</small></span>
        <button type="button" title="移除文档引用" onClick={onRemove}><X /></button>
      </div>
      {tooltipPosition && preview && createPortal(
        <div
          className="document-reference-preview-tooltip"
          role="tooltip"
          style={tooltipPosition}
        >
          {preview}
        </div>,
        document.body,
      )}
    </>
  );
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function serializePendingFiles(files: PendingFile[]): string {
  const escapeAttribute = (value: string) => value.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
  return files.map((file) => (
    file.mode === "path" && file.path
      ? `<file path="${escapeAttribute(file.path)}"></file>`
      : `<file name="${escapeAttribute(file.name)}">\n${file.text}\n</file>`
  )).join("\n\n");
}

function FileAttachment({ file, onRemove }: { file: PendingFile; onRemove: () => void }) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [tooltipPosition, setTooltipPosition] = useState<{ left: number; top: number } | null>(null);
  const preview = file.mode === "path" ? file.path ?? "" : documentReferencePreview(file.text);
  const lineCount = file.text ? file.text.split(/\r?\n/).length : 0;
  const showTooltip = useCallback(() => {
    const bounds = rootRef.current?.getBoundingClientRect();
    if (!bounds) return;
    setTooltipPosition({
      left: Math.max(176, Math.min(window.innerWidth - 176, bounds.left + bounds.width / 2)),
      top: bounds.top - 8,
    });
  }, []);
  return (
    <>
      <div
        ref={rootRef}
        className="file-attachment"
        onMouseEnter={showTooltip}
        onMouseLeave={() => setTooltipPosition(null)}
        onFocusCapture={showTooltip}
        onBlurCapture={() => setTooltipPosition(null)}
      >
        <FileText />
        <span><strong>{file.name}</strong><small>{file.mode === "path" ? "仅路径" : formatFileSize(file.size ?? 0)}</small></span>
        <button type="button" title="移除文件" onClick={onRemove}><X /></button>
      </div>
      {tooltipPosition && createPortal(
        <div className="document-reference-preview-tooltip file-preview-tooltip" role="tooltip" style={tooltipPosition}>
          <strong>{file.name}</strong>
          <small>{file.mode === "path"
            ? "仅发送路径，不上传文件内容"
            : `${formatFileSize(file.size ?? 0)} · ${lineCount} 行${file.mediaType ? ` · ${file.mediaType}` : ""}`}</small>
          {preview && <p className={file.mode === "path" ? "file-path-preview" : ""}>{preview}</p>}
        </div>,
        document.body,
      )}
    </>
  );
}

const EMPTY_GATEWAY: GatewayViewState = {
  status: "connecting",
  error: null,
  token: null,
  tokenExpiresAt: 0,
  workspace: null,
  sessionsByProject: {},
  models: [],
  runningCount: 0,
  pendingCount: 0,
};

export function collectFavoriteSessions(
  connection: ConnectionPreset | null,
  gateway: GatewayViewState | null,
): FavoriteSessionItem[] {
  const collect = (items: FavoriteViewEntry[]): FavoriteSessionItem[] => items.flatMap((item) => {
    if (item.kind === "folder") return collect(item.children);
    return item.kind === "session" ? [{ project: item.project, session: item.session }] : [];
  });
  return collect(resolveFavoriteEntries(connection, gateway));
}

function withFavoriteItems(connection: ConnectionPreset, items: FavoriteEntry[]): ConnectionPreset {
  return {
    ...connection,
    favorite_items: items,
    projects: connection.projects.map((project) => ({
      ...project,
      favorite_session_ids: favoriteSessionIdsForProject(items, project.id),
    })),
  };
}

export function resolveRememberedModel(
  connection: Pick<ConnectionPreset, "last_model_profile">,
  models: GatewayViewState["models"],
): string | undefined {
  const remembered = connection.last_model_profile;
  return remembered && models.some((model) => model.name === remembered)
    ? remembered
    : undefined;
}

export function groupGatewayModels(
  models: GatewayModel[],
  query = "",
): Array<{ group: string; models: GatewayModel[] }> {
  const needle = query.trim().toLowerCase();
  const grouped = new Map<string, GatewayModel[]>();
  for (const model of models) {
    const group = model.group || "default";
    const matches = !needle
      || model.name.toLowerCase().includes(needle)
      || (model.description ?? "").toLowerCase().includes(needle)
      || group.toLowerCase().includes(needle);
    if (!matches) continue;
    const entries = grouped.get(group) ?? [];
    entries.push(model);
    grouped.set(group, entries);
  }
  return Array.from(grouped, ([group, entries]) => ({ group, models: entries }));
}

function sessionKey(connectionId: string, sessionId: string): string {
  return `${connectionId}:${sessionId}`;
}

function basename(path: string): string {
  const normalized = path.replace(/[\\/]+$/, "");
  return normalized.split(/[\\/]/).at(-1) || path;
}

function comparablePath(path: string): string {
  return projectPathKey(path.trim());
}

function sessionsForProject(
  sessionsByProject: Record<string, SessionInfo[]>,
  projectPath: string,
): SessionInfo[] {
  const direct = sessionsByProject[projectPath];
  if (direct) return direct;
  const key = projectPathKey(projectPath);
  return Object.entries(sessionsByProject).find(
    ([candidate]) => projectPathKey(candidate) === key,
  )?.[1] ?? [];
}

export function defaultProjectDirectory(home: string, name: string): string {
  const directoryName = name
    .trim()
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "-")
    .replace(/[. ]+$/, "")
    .trim() || "新项目";
  const separator = home.includes("\\") && !home.includes("/") ? "\\" : "/";
  return `${home.replace(/[\\/]+$/, "")}${separator}${directoryName}`;
}

export function resolveDefaultProjectId(
  projects: ProjectPreset[],
  startupCwd?: string | null,
): string | null {
  const startupProject = startupCwd
    ? projects.find((project) => comparablePath(project.path) === comparablePath(startupCwd))
    : undefined;
  return startupProject?.id
    ?? projects.find((project) => project.is_default === true)?.id
    ?? projects[0]?.id
    ?? null;
}

function projectDirectoryTitle(project: ProjectPreset): string {
  if (project.directories.length === 0) return "需要设置项目主目录";
  if (project.directories.length === 1) return project.directories[0];
  return `${project.directories[0]} 等 ${project.directories.length} 个目录`;
}

function firstUserText(items: ChatItem[]): string {
  return items.find((item) => item.kind === "user" && item.text?.trim())?.text?.trim() ?? "";
}

function formatDate(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatDateTime(value: string | null): string {
  if (!value) return "未安排";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatElapsed(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const seconds = totalSeconds % 60;
  const minutes = Math.floor(totalSeconds / 60) % 60;
  const hours = Math.floor(totalSeconds / 3600);
  if (hours > 0) return `${hours}小时${String(minutes).padStart(2, "0")}分`;
  if (minutes > 0) return `${minutes}分${String(seconds).padStart(2, "0")}秒`;
  return `${seconds}秒`;
}

export function formatTurnDuration(ms: number, format: TurnDurationFormat): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  if (format === "seconds") return `${totalSeconds}秒`;

  const seconds = totalSeconds % 60;
  const minutes = Math.floor(totalSeconds / 60) % 60;
  const hours = Math.floor(totalSeconds / 3600);
  return [
    hours > 0 ? `${hours}时` : "",
    minutes > 0 ? `${minutes}分` : "",
    `${seconds}秒`,
  ].filter(Boolean).join("");
}

function formatTokenCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${Math.floor(value / 1_000)}k`;
  return String(value);
}

function usageToken(value: Record<string, unknown> | null | undefined, key: string): number {
  const parsed = Number(value?.[key]);
  return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : 0;
}

function cacheUsage(usage: Record<string, unknown> | null | undefined): {
  hitRate: number;
  readTokens: number;
  writeTokens: number | null;
} | null {
  if (!usage || !("cache_read_tokens" in usage || "cache_write_tokens" in usage)) return null;
  const cacheRead = usageToken(usage, "cache_read_tokens");
  const totalInput = usageToken(usage, "total_input_tokens") || usageToken(usage, "input_tokens");
  const hitRate = totalInput > 0 ? Math.min(100, cacheRead / totalInput * 100) : 0;
  return {
    hitRate,
    readTokens: cacheRead,
    writeTokens: "cache_write_tokens" in usage ? usageToken(usage, "cache_write_tokens") : null,
  };
}

function normalizePermissionMode(value: string | undefined): PermissionMode {
  return value === "ask" || value === "ai_review" || value === "run_everything"
    ? value
    : "default";
}

function scheduleSummary(job: ScheduleJobInfo): string {
  if (job.running) return "正在执行";
  if (job.status === "completed") return "已完成";
  if (job.status === "error") return "运行异常";
  if (!job.enabled || job.status === "paused") return "已暂停";
  return `下次运行 ${formatDateTime(job.next_run)}`;
}

function textFromUnknown(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function diffLines(diff: string) {
  return diff.split("\n").map((line, index) => {
    const added = line.startsWith("+") && !line.startsWith("+++");
    const removed = line.startsWith("-") && !line.startsWith("---");
    const marker = added ? "+" : removed ? "-" : "";
    return (
      <span
        className={`diff-line ${added ? "added" : removed ? "removed" : "context"}`}
        data-marker={marker}
        key={`${index}-${line}`}
      >
        {marker ? line.slice(1) : line}
      </span>
    );
  });
}

function readImage(file: File): Promise<PendingImage> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("无法读取图片"));
    reader.onload = () => {
      const dataUrl = String(reader.result ?? "");
      const separator = dataUrl.indexOf(",");
      if (separator < 0) {
        reject(new Error("图片格式无效"));
        return;
      }
      resolve({
        id: crypto.randomUUID(),
        name: file.name,
        media_type: file.type || "image/png",
        data: dataUrl.slice(separator + 1),
        dataUrl,
      });
    };
    reader.readAsDataURL(file);
  });
}

function App() {
  const [settings, setSettings] = useState<DesktopSettings | null>(null);
  const [gateways, setGateways] = useState<GatewayMap>({});
  const [sessions, setSessions] = useState<SessionMap>({});
  const [activeSessions, setActiveSessions] = useState<Record<string, string | null>>({});
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [projectFilesWidth, setProjectFilesWidth] = useState(640);
  const [wideProjectFilesLayout, setWideProjectFilesLayout] = useState(() => shouldUseWideProjectFilesLayout(window.innerWidth));
  const [projectFilesOpen, setProjectFilesOpen] = useState(false);
  const [projectFileTreeOpen, setProjectFileTreeOpen] = useState(false);
  const [projectFileTabs, setProjectFileTabs] = useState<ProjectFileTabsState>({ files: [], activePath: null });
  const [documentAgentTransitioning, setDocumentAgentTransitioning] = useState(false);
  const [projectsCollapsed, setProjectsCollapsed] = useState(false);
  const [sessionsCollapsed, setSessionsCollapsed] = useState(false);
  const [workspaceView, setWorkspaceView] = useState<WorkspaceView>("chat");
  const [conversationViews, setConversationViews] = useState<Record<string, ConversationView>>({});
  const [search, setSearch] = useState("");
  const [scheduleJobs, setScheduleJobs] = useState<Record<string, ScheduleJobInfo[]>>({});
  const [monitorTasks, setMonitorTasks] = useState<Record<string, BackgroundTaskInfo[]>>({});
  const [pluginData, setPluginData] = useState<Record<string, PluginData>>({});
  const [automationTab, setAutomationTab] = useState<AutomationTab>("schedule");
  const [scheduleLoading, setScheduleLoading] = useState(false);
  const [monitorLoading, setMonitorLoading] = useState(false);
  const [pluginLoading, setPluginLoading] = useState(false);
  const [scheduleAction, setScheduleAction] = useState<ScheduleActionState | null>(null);
  const [scheduleDeleteTarget, setScheduleDeleteTarget] = useState<ScheduleJobInfo | null>(null);
  const [projectDeleteTarget, setProjectDeleteTarget] = useState<ProjectPreset | null>(null);
  const [scheduleError, setScheduleError] = useState<string | null>(null);
  const [monitorError, setMonitorError] = useState<string | null>(null);
  const [pluginError, setPluginError] = useState<string | null>(null);
  const [deletingSessionIds, setDeletingSessionIds] = useState<Set<string>>(new Set());
  const [modelSelections, setModelSelections] = useState<Record<string, string>>({});
  const [permissionSelections, setPermissionSelections] = useState<Record<string, PermissionMode>>({});
  const [runClock, setRunClock] = useState(() => Date.now());
  const [composer, setComposer] = useState("");
  const [pendingImages, setPendingImages] = useState<PendingImage[]>([]);
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([]);
  const [pendingFolders, setPendingFolders] = useState<string[]>([]);
  const [pendingDocumentReferences, setPendingDocumentReferences] = useState<DocumentReference[]>([]);
  const [selectionTranslationEvents, setSelectionTranslationEvents] = useState<Record<string, GatewayEvent | null>>({});
  const [projectModal, setProjectModal] = useState<ProjectPreset | "new" | null>(null);
  const [projectTypeModal, setProjectTypeModal] = useState(false);
  const [documentProjectModal, setDocumentProjectModal] = useState(false);
  const [documentCapabilities, setDocumentCapabilities] = useState<DocumentCapabilities | null | undefined>(undefined);
  const [documentEngineBusy, setDocumentEngineBusy] = useState<"install" | "remove" | null>(null);
  const [documentEngineProgress, setDocumentEngineProgress] = useState<DocumentEngineInstallProgress | null>(null);
  const [documentEngineError, setDocumentEngineError] = useState<string | null>(null);
  const [referencePathModal, setReferencePathModal] = useState<"all" | "file" | null>(null);
  const [goalModal, setGoalModal] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSectionId>("general");
  const [modelSettingsState, setModelSettingsState] = useState<ModelSettingsLoadState | null>(null);
  const [runtimeSettingsState, setRuntimeSettingsState] = useState<RuntimeSettingsLoadState | null>(null);
  const [connectionModal, setConnectionModal] = useState<"new" | string | null>(null);
  const [checkpointModal, setCheckpointModal] = useState(false);
  const [globalError, setGlobalError] = useState<string | null>(null);
  const apiRef = useRef(new Map<string, GatewayApi>());
  const channelRef = useRef(new Map<string, SessionChannel>());
  const connectedRef = useRef(new Set<string>());
  const deletingSessionIdsRef = useRef(new Set<string>());
  const sessionRefreshVersionRef = useRef(new Map<string, number>());
  const autoOpeningDocumentRef = useRef<string | null>(null);
  const focusedSessionRef = useRef<FocusedSessionSnapshot | null>(null);
  const messageEndRef = useRef<HTMLDivElement | null>(null);
  const settingsRef = useRef<DesktopSettings | null>(null);
  const gatewaysRef = useRef<GatewayMap>({});
  const documentAgentTransitionTimerRef = useRef<number | null>(null);
  settingsRef.current = settings;
  gatewaysRef.current = gateways;

  useEffect(() => {
    const updateLayout = () => setWideProjectFilesLayout(shouldUseWideProjectFilesLayout(window.innerWidth));
    window.addEventListener("resize", updateLayout);
    return () => window.removeEventListener("resize", updateLayout);
  }, []);

  useEffect(() => {
    if (settings) setProjectFilesWidth(settings.project_files_width);
  }, [settings?.project_files_width]);

  useEffect(() => {
    setProjectFileTabs((current) => limitProjectFileTabs(current, settings?.project_files_max_tabs ?? 5));
  }, [settings?.project_files_max_tabs]);

  const activeConnection = useMemo(() => {
    if (!settings) return null;
    return settings.connections.find((item) => item.id === settings.active_connection_id)
      ?? settings.connections[0]
      ?? null;
  }, [settings]);
  const activeGateway = activeConnection ? gateways[activeConnection.id] : null;
  const activeProject = activeConnection?.projects.find(
    (item) => item.id === activeConnection.last_project_id,
  ) ?? activeConnection?.projects.find(
    (item) => typeof activeConnection.last_project_path === "string"
      && projectPathKey(item.path) === projectPathKey(activeConnection.last_project_path),
  ) ?? activeConnection?.projects[0] ?? null;
  useEffect(() => {
    setProjectFilesOpen(false);
    setProjectFileTreeOpen(false);
    setProjectFileTabs({ files: [], activePath: null });
  }, [wideProjectFilesLayout, workspaceView, activeConnection?.id, activeProject?.id]);
  const defaultProjectId = activeConnection
    ? resolveDefaultProjectId(activeConnection.projects, activeGateway?.workspace?.startup_cwd)
    : null;
  const activeSessionKey = activeConnection ? activeSessions[activeConnection.id] : null;
  const activeSession = activeSessionKey ? sessions[activeSessionKey] : null;
  const activeChannel = activeSessionKey ? channelRef.current.get(activeSessionKey) : null;
  const activeConversationView = activeSessionKey ? conversationViews[activeSessionKey] ?? "chat" : "chat";
  const selectedProjectFile = projectFileTabs.files.find(
    (file) => projectFileTabs.activePath !== null
      && projectPathKey(file.path) === projectPathKey(projectFileTabs.activePath),
  ) ?? null;
  const activeList = activeProject?.directories.length
    ? sessionsForProject(activeGateway?.sessionsByProject ?? {}, activeProject.path)
    : [];
  // The HTTP session list is persisted metadata and can briefly lag behind
  // the live WebSocket state (especially while the first title is generated).
  // Keep the active conversation visible as soon as its first user message is
  // rendered, then let the next list refresh replace the optimistic values.
  const displayList = useMemo(() => {
    if (
      !activeSession
      || !activeProject
      || projectPathKey(activeSession.cwd) !== projectPathKey(activeProject.path)
      || activeSession.id.startsWith("new-")
      || activeSession.items.length === 0
    ) return activeList;

    const userPreview = firstUserText(activeSession.items);
    const existing = activeList.find((item) => item.session_id === activeSession.id);
    const optimistic: SessionInfo = {
      session_id: activeSession.id,
      message_count: Math.max(existing?.message_count ?? 0, activeSession.items.length),
      model: existing?.model || activeSession.status?.model || "",
      provider: existing?.provider || activeSession.status?.provider || "",
      created_at: existing?.created_at || new Date().toISOString(),
      title: existing?.title?.trim()
        || (activeSession.title !== "新会话" ? activeSession.title : userPreview.slice(0, 200))
        || "未命名会话",
      cwd: activeSession.cwd,
      tokens_used: existing?.tokens_used ?? activeSession.status?.context_used_tokens ?? 0,
      preview: existing?.preview || userPreview.slice(0, 100),
      forked_from_session_id: existing?.forked_from_session_id ?? null,
      forked_from_message_uuid: existing?.forked_from_message_uuid ?? null,
      forked_from_title: existing?.forked_from_title ?? null,
    };
    if (existing) {
      return activeList.map((item) => item.session_id === activeSession.id ? optimistic : item);
    }
    return [optimistic, ...activeList];
  }, [activeList, activeProject, activeSession]);
  const activeSessionInfo = activeSession
    ? displayList.find((item) => item.session_id === activeSession.id)
    : undefined;
  const activeForkOrigin = activeSessionInfo?.forked_from_title
    || activeSessionInfo?.forked_from_session_id?.slice(0, 8)
    || null;

  const refreshSchedules = useCallback(async (connectionId: string) => {
    const api = apiRef.current.get(connectionId);
    if (!api) return;
    setScheduleLoading(true);
    setScheduleError(null);
    try {
      const jobs = await api.schedules(true);
      setScheduleJobs((current) => ({ ...current, [connectionId]: jobs }));
    } catch (error) {
      setScheduleError(error instanceof Error ? error.message : String(error));
    } finally {
      setScheduleLoading(false);
    }
  }, []);

  const refreshMonitors = useCallback(async (connectionId: string) => {
    const api = apiRef.current.get(connectionId);
    if (!api) return;
    setMonitorLoading(true);
    setMonitorError(null);
    try {
      const globalTasks = await api.backgroundTasks(true, "running");
      const connectedSessions = Array.from(channelRef.current.entries())
        .filter(([key, channel]) => key.startsWith(`${connectionId}:`) && !channel.isDisposed)
        .flatMap(([key]) => {
          const state = sessions[key];
          return state && !state.id.startsWith("new-")
            ? [{ id: state.id, cwd: state.cwd }]
            : [];
        });

      // Gateways started before global task listing was added silently ignore
      // scope=global and only return the default session. Fall back to the
      // sessions whose channels this desktop already keeps alive, then merge
      // by the stable session/task pair. The fallback can be removed once the
      // desktop and gateway versions are negotiated explicitly.
      const needsSessionFallback = globalTasks.length === 0
        || globalTasks.some((task) => !task.cwd);
      const sessionResults = needsSessionFallback
        ? await Promise.allSettled(connectedSessions.map(async ({ id, cwd }) => (
          (await api.backgroundTasks(false, "running", id)).map((task) => ({
            ...task,
            cwd: task.cwd || cwd,
          }))
        )))
        : [];
      const merged = new Map<string, BackgroundTaskInfo>();
      globalTasks.forEach((task) => merged.set(`${task.session_id}:${task.task_id}`, task));
      sessionResults.forEach((result) => {
        if (result.status !== "fulfilled") return;
        result.value.forEach((task) => merged.set(`${task.session_id}:${task.task_id}`, task));
      });
      setMonitorTasks((current) => ({
        ...current,
        [connectionId]: Array.from(merged.values()),
      }));
    } catch (error) {
      setMonitorError(error instanceof Error ? error.message : String(error));
    } finally {
      setMonitorLoading(false);
    }
  }, [sessions]);

  const refreshPlugins = useCallback(async (connectionId: string, sessionId?: string, cwd?: string) => {
    const api = apiRef.current.get(connectionId);
    if (!api) return;
    setPluginLoading(true);
    setPluginError(null);
    try {
      const [skills, tools] = await Promise.all([
        api.skills(sessionId, cwd),
        api.tools(sessionId, cwd),
      ]);
      setPluginData((current) => ({ ...current, [connectionId]: { skills, tools } }));
    } catch (error) {
      setPluginError(error instanceof Error ? error.message : String(error));
    } finally {
      setPluginLoading(false);
    }
  }, []);

  const refreshModelSettings = useCallback(async (connectionId: string, cwd?: string) => {
    const key = `${connectionId}\u0000${cwd ?? ""}`;
    const api = apiRef.current.get(connectionId);
    if (!api) {
      setModelSettingsState({ key, data: null, loading: false, error: "Gateway 尚未连接" });
      return;
    }
    setModelSettingsState((current) => ({
      key,
      data: current?.key === key ? current.data : null,
      loading: true,
      error: null,
    }));
    try {
      const data = await api.modelSettings(cwd);
      setModelSettingsState((current) => current?.key === key
        ? { key, data, loading: false, error: null }
        : current);
    } catch (error) {
      setModelSettingsState((current) => current?.key === key
        ? {
            key,
            data: current.data,
            loading: false,
            error: error instanceof Error ? error.message : String(error),
          }
        : current);
    }
  }, []);

  const refreshRuntimeSettings = useCallback(async (connectionId: string, cwd?: string) => {
    const key = `${connectionId}\u0000${cwd ?? ""}`;
    const api = apiRef.current.get(connectionId);
    if (!api) {
      setRuntimeSettingsState({ key, data: null, loading: false, error: "Gateway 尚未连接" });
      return;
    }
    setRuntimeSettingsState((current) => ({
      key,
      data: current?.key === key ? current.data : null,
      loading: true,
      error: null,
    }));
    try {
      const data = await api.runtimeSettings(cwd);
      setRuntimeSettingsState((current) => current?.key === key
        ? { key, data, loading: false, error: null }
        : current);
    } catch (error) {
      setRuntimeSettingsState((current) => current?.key === key
        ? {
            key,
            data: current.data,
            loading: false,
            error: error instanceof Error ? error.message : String(error),
          }
        : current);
    }
  }, []);

  const mutateRuntimeSettings = useCallback(async (
    connectionId: string,
    mutation: RuntimeSettingsMutation,
  ) => {
    const api = apiRef.current.get(connectionId);
    if (!api) throw new Error("Gateway 尚未连接");
    const data = await api.mutateRuntimeSettings(mutation);
    const cwd = mutation.cwd ?? "";
    const key = `${connectionId}\u0000${cwd}`;
    setRuntimeSettingsState({ key, data, loading: false, error: null });
  }, []);

  const mutateModelSettings = useCallback(async (
    connectionId: string,
    mutation: ModelSettingsMutation,
  ) => {
    const api = apiRef.current.get(connectionId);
    if (!api) throw new Error("Gateway 尚未连接");
    const key = `${connectionId}\u0000${mutation.cwd ?? ""}`;
    setModelSettingsState((current) => ({
      key,
      data: current?.key === key ? current.data : null,
      loading: true,
      error: null,
    }));
    try {
      const data = await api.mutateModelSettings(mutation);
      const models = data.models.map((entry) => {
        const provider = typeof entry.effective.provider === "string" ? entry.effective.provider : "";
        const model = typeof entry.effective.model === "string" ? entry.effective.model : "";
        return {
          name: entry.name,
          description: [provider, model].filter(Boolean).join("/") || entry.name,
          group: entry.group ?? "default",
        };
      });
      setGateways((current) => current[connectionId]
        ? { ...current, [connectionId]: { ...current[connectionId], models } }
        : current);
      setModelSettingsState((current) => current?.key === key
        ? { key, data, loading: false, error: null }
        : current);
    } catch (error) {
      setModelSettingsState((current) => current?.key === key
        ? {
            key,
            data: current.data,
            loading: false,
            error: error instanceof Error ? error.message : String(error),
          }
        : current);
      throw error;
    }
  }, []);

  const commitSettings = useCallback((update: (current: DesktopSettings) => DesktopSettings) => {
    setSettings((current) => {
      if (!current) return current;
      const next = update(current);
      void saveSettings(next).catch((error) => setGlobalError(String(error)));
      return next;
    });
  }, []);

  const updateDocumentAgentCollapsed = useCallback((collapsed: boolean) => {
    setDocumentAgentTransitioning(true);
    commitSettings((current) => ({ ...current, document_agent_collapsed: collapsed }));
    if (documentAgentTransitionTimerRef.current !== null) {
      window.clearTimeout(documentAgentTransitionTimerRef.current);
    }
    documentAgentTransitionTimerRef.current = window.setTimeout(() => {
      documentAgentTransitionTimerRef.current = null;
      setDocumentAgentTransitioning(false);
    }, 300);
  }, [commitSettings]);

  useEffect(() => () => {
    if (documentAgentTransitionTimerRef.current !== null) {
      window.clearTimeout(documentAgentTransitionTimerRef.current);
    }
  }, []);

  const updateConnection = useCallback((
    connectionId: string,
    update: (connection: ConnectionPreset) => ConnectionPreset,
  ) => {
    commitSettings((current) => ({
      ...current,
      connections: current.connections.map((connection) => (
        connection.id === connectionId ? update(connection) : connection
      )),
    }));
  }, [commitSettings]);

  const toggleFavoriteSession = useCallback((projectId: string, sessionId: string) => {
    if (!activeConnection || sessionId.startsWith("new-")) return;
    updateConnection(activeConnection.id, (connection) => {
      const items = favoriteEntries(connection);
      const next = hasFavoriteSession(items, projectId, sessionId)
        ? removeFavoriteEntries(items, (entry) => entry.type === "session"
          && entry.project_id === projectId && entry.session_id === sessionId)
        : addFavoriteEntry(items, null, {
          id: crypto.randomUUID(),
          type: "session",
          project_id: projectId,
          session_id: sessionId,
        });
      return withFavoriteItems(connection, next);
    });
  }, [activeConnection, updateConnection]);

  const toggleFavoriteProject = useCallback((projectId: string) => {
    if (!activeConnection) return;
    updateConnection(activeConnection.id, (connection) => {
      const items = favoriteEntries(connection);
      const next = hasFavoriteProject(items, projectId)
        ? removeFavoriteEntries(items, (entry) => entry.type === "project" && entry.project_id === projectId)
        : addFavoriteEntry(items, null, {
          id: crypto.randomUUID(),
          type: "project",
          project_id: projectId,
        });
      return withFavoriteItems(connection, next);
    });
  }, [activeConnection, updateConnection]);

  const updateFavorites = useCallback((update: (items: FavoriteEntry[]) => FavoriteEntry[]) => {
    if (!activeConnection) return;
    updateConnection(activeConnection.id, (connection) => (
      withFavoriteItems(connection, update(favoriteEntries(connection)))
    ));
  }, [activeConnection, updateConnection]);

  const refreshProjectSessions = useCallback(async (connectionId: string, cwd: string) => {
    const api = apiRef.current.get(connectionId);
    if (!api) return;
    const refreshKey = `${connectionId}\u0000${projectPathKey(cwd)}`;
    const version = (sessionRefreshVersionRef.current.get(refreshKey) ?? 0) + 1;
    sessionRefreshVersionRef.current.set(refreshKey, version);
    const list = await api.sessions(cwd);
    if (sessionRefreshVersionRef.current.get(refreshKey) !== version) return;
    setGateways((current) => {
      const existing = current[connectionId]?.sessionsByProject ?? {};
      const key = projectPathKey(cwd);
      const sessionsByProject = Object.fromEntries(
        Object.entries(existing).filter(([path]) => projectPathKey(path) !== key),
      );
      sessionsByProject[cwd] = list;
      return {
        ...current,
        [connectionId]: {
          ...(current[connectionId] ?? EMPTY_GATEWAY),
          sessionsByProject,
        },
      };
    });
  }, []);

  const updateSessionStatus = useCallback(async (
    connectionId: string,
    key: string,
    id: string,
    syncControls = false,
  ) => {
    const api = apiRef.current.get(connectionId);
    if (!api) return;
    try {
      const status = await api.sessionStatus(id);
      setSessions((current) => current[key]
        ? { ...current, [key]: { ...current[key], status } }
        : current);
      setModelSelections((current) => (
        syncControls || !current[key]
          ? { ...current, [key]: status.model_profile || status.model }
          : current
      ));
      setPermissionSelections((current) => (
        syncControls || !current[key]
          ? { ...current, [key]: normalizePermissionMode(status.permission_mode) }
          : current
      ));
    } catch {
      // The history still works if the optional status request races session setup.
    }
  }, []);

  const updateSessionPreferences = useCallback((
    connectionId: string,
    projectId: string,
    sessionId: string,
    changes: Partial<SessionPreferences>,
  ) => {
    if (!sessionId || sessionId.startsWith("new-")) return;
    updateConnection(connectionId, (connection) => ({
      ...connection,
      projects: connection.projects.map((project) => {
        if (project.id !== projectId) return project;
        const previous = project.session_preferences?.[sessionId] ?? {};
        return {
          ...project,
          session_preferences: {
            ...(project.session_preferences ?? {}),
            [sessionId]: { ...previous, ...changes },
          },
        };
      }),
    }));
  }, [updateConnection]);

  const restoreSessionPreferences = useCallback(async (
    connectionId: string,
    key: string,
    channel: SessionChannel,
    sessionId: string,
    preferences: SessionPreferences | undefined,
    models: GatewayModel[],
  ) => {
    await updateSessionStatus(connectionId, key, sessionId, true);
    if (!preferences || channel.isDisposed) return;
    try {
      const model = preferences.model_profile
        && models.some((item) => item.name === preferences.model_profile)
        ? preferences.model_profile
        : undefined;
      if (model) channel.switchModel(model);
      if (preferences.reasoning_effort) channel.setReasoningEffort(preferences.reasoning_effort);
      if (typeof preferences.ultra_mode === "boolean") channel.setUltraMode(preferences.ultra_mode);
      if (preferences.mode) channel.switchMode(preferences.mode);
      if (preferences.permission_mode) channel.setPermissionMode(preferences.permission_mode);
      setModelSelections((current) => model ? { ...current, [key]: model } : current);
      if (preferences.permission_mode) {
        setPermissionSelections((current) => ({
          ...current,
          [key]: normalizePermissionMode(preferences.permission_mode),
        }));
      }
      setSessions((current) => {
        const session = current[key];
        if (!session?.status) return current;
        return {
          ...current,
          [key]: {
            ...session,
            status: {
              ...session.status,
              ...(model ? { model_profile: model } : {}),
              ...(preferences.reasoning_effort ? { reasoning_effort: preferences.reasoning_effort } : {}),
              ...(typeof preferences.ultra_mode === "boolean" ? { ultra_mode: preferences.ultra_mode } : {}),
              ...(preferences.mode ? { mode: preferences.mode } : {}),
              ...(preferences.permission_mode ? { permission_mode: preferences.permission_mode } : {}),
            },
          },
        };
      });
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  }, [updateSessionStatus]);

  const removeSessionState = useCallback((target: SessionCleanupTarget) => {
    channelRef.current.get(target.key)?.dispose();
    channelRef.current.delete(target.key);
    setSessions((current) => {
      const { [target.key]: _, ...rest } = current;
      return rest;
    });
    setConversationViews((current) => {
      const { [target.key]: _, ...rest } = current;
      return rest;
    });
    setActiveSessions((current) => (
      current[target.connectionId] === target.key
        ? { ...current, [target.connectionId]: null }
        : current
    ));
  }, []);

  const removeSessionFromGateway = useCallback((target: SessionCleanupTarget) => {
    const refreshKey = `${target.connectionId}\u0000${target.cwd}`;
    sessionRefreshVersionRef.current.set(
      refreshKey,
      (sessionRefreshVersionRef.current.get(refreshKey) ?? 0) + 1,
    );
    setGateways((current) => {
      const gateway = current[target.connectionId];
      if (!gateway) return current;
      let changed = false;
      const sessionsByProject = Object.fromEntries(
        Object.entries(gateway.sessionsByProject).map(([cwd, list]) => {
          const next = list.filter((item) => item.session_id !== target.sessionId);
          if (next.length !== list.length) changed = true;
          return [cwd, next];
        }),
      );
      if (!changed) return current;
      return {
        ...current,
        [target.connectionId]: { ...gateway, sessionsByProject },
      };
    });
  }, []);

  const archiveSession = useCallback(async (
    target: SessionCleanupTarget,
    confirm = false,
  ) => {
    if (deletingSessionIdsRef.current.has(target.key)) return;
    if (confirm && !window.confirm("删除这个会话？")) return;
    deletingSessionIdsRef.current.add(target.key);
    setDeletingSessionIds((current) => new Set(current).add(target.key));

    const archiveRequest = target.sessionId.startsWith("new-")
      ? Promise.resolve()
      : apiRef.current.get(target.connectionId)?.archive(target.sessionId)
        ?? Promise.reject(new Error("Gateway 尚未连接"));

    // Remove local state before waiting for resource teardown on the gateway.
    removeSessionState(target);
    removeSessionFromGateway(target);
    commitSettings((current) => ({
      ...current,
      connections: current.connections.map((connection) => connection.id === target.connectionId
        ? withFavoriteItems({
          ...connection,
          projects: connection.projects.map((project) => (
            projectPathKey(project.path) === projectPathKey(target.cwd)
              ? (() => {
                const { [target.sessionId]: _, ...sessionPreferences } = project.session_preferences ?? {};
                return {
                  ...project,
                  last_session_id: project.last_session_id === target.sessionId ? null : project.last_session_id,
                  session_preferences: sessionPreferences,
                };
              })()
              : project
          )),
        }, removeFavoriteEntries(favoriteEntries(connection), (entry) => entry.type === "session"
          && entry.session_id === target.sessionId
          && connection.projects.some((project) => project.id === entry.project_id
            && projectPathKey(project.path) === projectPathKey(target.cwd))))
        : connection),
    }));

    try {
      await archiveRequest;
      void refreshProjectSessions(target.connectionId, target.cwd).catch((error) => {
        setGlobalError(error instanceof Error ? error.message : String(error));
      });
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
      try {
        await refreshProjectSessions(target.connectionId, target.cwd);
      } catch (refreshError) {
        setGlobalError(refreshError instanceof Error ? refreshError.message : String(refreshError));
      }
    } finally {
      deletingSessionIdsRef.current.delete(target.key);
      setDeletingSessionIds((current) => {
        const next = new Set(current);
        next.delete(target.key);
        return next;
      });
    }
  }, [commitSettings, refreshProjectSessions, removeSessionFromGateway, removeSessionState]);

  const openSession = useCallback((
    connection: ConnectionPreset,
    project: ProjectPreset,
    info?: SessionInfo,
  ) => {
    setWorkspaceView("chat");
    const api = apiRef.current.get(connection.id);
    if (!api) {
      setGlobalError("Gateway 尚未连接");
      return;
    }
    let key = sessionKey(connection.id, info?.session_id ?? `new-${crypto.randomUUID()}`);
    const existingChannel = channelRef.current.get(key);
    if (existingChannel && !existingChannel.isDisposed) {
      setActiveSessions((current) => ({ ...current, [connection.id]: key }));
      return;
    }
    // A component refresh can dispose a channel while preserving React state.
    // Do not let that dead channel block a later resume of the same session.
    if (existingChannel) {
      existingChannel.dispose();
      channelRef.current.delete(key);
    }
    const initial: SessionViewState = {
      id: info?.session_id ?? key.split(":").slice(1).join(":"),
      cwd: project.path,
      title: info ? (info.title || "未命名会话") : "新会话",
      items: [],
      loading: Boolean(info),
      busy: false,
      connected: false,
      operationId: null,
      status: null,
      error: null,
      runStartedAt: null,
      currentStep: null,
      lastTurnUsage: null,
    };
    setSessions((current) => {
      const previous = current[key];
      return {
        ...current,
        [key]: previous
          ? {
              ...previous,
              cwd: project.path,
              title: info ? (info.title || "未命名会话") : previous.title,
              loading: Boolean(info) && previous.items.length === 0,
              connected: false,
              error: null,
            }
          : initial,
      };
    });
    setActiveSessions((current) => ({ ...current, [connection.id]: key }));

    let channel: SessionChannel;
    const isCurrentChannel = () => channelRef.current.get(key) === channel;
    channel = new SessionChannel(api, {
      sessionId: info?.session_id,
      cwd: project.path,
      additionalDirectories: project.directories.slice(1),
      modelProfile: info
        ? undefined
        : resolveRememberedModel(connection, gateways[connection.id]?.models ?? []),
      onEvent: (event: GatewayEvent) => {
        if (!isCurrentChannel()) return;
        if (event.type === "model_change" && event.model_profile) {
          setModelSelections((current) => ({ ...current, [key]: event.model_profile! }));
          updateConnection(connection.id, (current) => ({
            ...current,
            last_model_profile: event.model_profile!,
          }));
          if (channel.sessionId) {
            updateSessionPreferences(connection.id, project.id, channel.sessionId, {
              model_profile: event.model_profile,
            });
          }
        }
        if (event.type === "permission_mode_change" && event.permission_mode) {
          setPermissionSelections((current) => ({
            ...current,
            [key]: normalizePermissionMode(event.permission_mode),
          }));
          if (channel.sessionId) {
            updateSessionPreferences(connection.id, project.id, channel.sessionId, {
              permission_mode: event.permission_mode,
            });
          }
        }
        if (event.type === "mode_change" && event.mode && channel.sessionId) {
          updateSessionPreferences(connection.id, project.id, channel.sessionId, {
            mode: event.mode,
          });
        }
        if (
          event.type === "document_selection_translation"
          || (event.type === "error" && event.command === "document_selection_translate")
        ) {
          setSelectionTranslationEvents((current) => ({ ...current, [key]: event }));
        }
        setSessions((current) => {
          const state = current[key];
          return state ? { ...current, [key]: applyGatewayEvent(state, event) } : current;
        });
        if (
          channel.sessionId
          && (event.type === "turn_complete" || event.type === "compact" || event.type === "agent_state")
        ) {
          void updateSessionStatus(connection.id, key, channel.sessionId, true);
          if (event.type === "turn_complete") {
            void refreshProjectSessions(connection.id, project.path).catch((error) => {
              setGlobalError(error instanceof Error ? error.message : String(error));
            });
          }
        }
        if (
          event.type === "error"
          && event.command_error
          && (
            event.command === "switch_model"
            || event.command === "set_permission_mode"
            || event.command === "set_reasoning_effort"
            || event.command === "set_ultra_mode"
            || event.command === "switch_mode"
          )
          && channel.sessionId
        ) {
          void updateSessionStatus(connection.id, key, channel.sessionId, true);
        }
      },
      onReady: (id) => {
        if (!isCurrentChannel()) return;
        const nextKey = sessionKey(connection.id, id);
        if (nextKey !== key) {
          const previousKey = key;
          const existingChannel = channelRef.current.get(previousKey);
          channelRef.current.delete(previousKey);
          if (existingChannel) channelRef.current.set(nextKey, existingChannel);
          setSessions((current) => {
            const old = current[previousKey];
            if (!old) return current;
            const { [previousKey]: _, ...rest } = current;
            return { ...rest, [nextKey]: { ...old, id } };
          });
          setActiveSessions((current) => ({
            ...current,
            [connection.id]: current[connection.id] === previousKey ? nextKey : current[connection.id],
          }));
          setConversationViews((current) => {
            if (!current[previousKey]) return current;
            const { [previousKey]: view, ...rest } = current;
            return { ...rest, [nextKey]: view };
          });
          key = nextKey;
        }
        updateConnection(connection.id, (current) => ({
          ...current,
          last_project_path: project.path,
          last_project_id: project.id,
          projects: current.projects.map((item) => item.id === project.id
            ? { ...item, last_session_id: id }
            : item),
        }));
        void refreshProjectSessions(connection.id, project.path);
        void restoreSessionPreferences(
          connection.id,
          key,
          channel,
          id,
          settingsRef.current?.connections
            .find((item) => item.id === connection.id)
            ?.projects.find((item) => item.id === project.id)
            ?.session_preferences?.[id],
          gatewaysRef.current[connection.id]?.models ?? [],
        );
      },
      onState: (connected, error) => {
        if (!isCurrentChannel()) return;
        setSessions((current) => current[key]
          ? {
              ...current,
              [key]: {
                ...current[key],
                loading: error ? false : current[key].loading,
                connected,
                error: error ?? null,
              },
            }
          : current);
      },
    });
    channelRef.current.set(key, channel);
    void channel.connect();
  }, [
    gateways,
    refreshProjectSessions,
    restoreSessionPreferences,
    updateConnection,
    updateSessionPreferences,
    updateSessionStatus,
  ]);

  const forkSessionFromMessage = useCallback(async (item: ChatItem) => {
    if (
      !activeConnection
      || !activeProject
      || !activeSession
      || activeSession.id.startsWith("new-")
      || activeSession.busy
      || item.kind !== "assistant"
    ) return;
    const api = apiRef.current.get(activeConnection.id);
    if (!api) {
      setGlobalError("Gateway 尚未连接");
      return;
    }
    const messageUuid = item.id.replace(/:part-\d+$/, "");
    try {
      const forked = await api.forkSession(activeSession.id, messageUuid);
      openSession(activeConnection, activeProject, forked);
      void refreshProjectSessions(activeConnection.id, activeProject.path).catch((error) => {
        setGlobalError(error instanceof Error ? error.message : String(error));
      });
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  }, [activeConnection, activeProject, activeSession, openSession, refreshProjectSessions]);

  const connectGateway = useCallback(async (connection: ConnectionPreset, pythonPath: string | null) => {
    setGateways((current) => ({
      ...current,
      [connection.id]: { ...(current[connection.id] ?? EMPTY_GATEWAY), status: "connecting", error: null },
    }));
    try {
      if (isLoopbackUrl(connection.base_url)) {
        await ensureLocalGateway(
          connection.id,
          connection.base_url,
          pythonPath,
          connection.credential_ref,
        );
      }
      const api = new GatewayApi(connection);
      apiRef.current.set(connection.id, api);
      await api.authenticate();
      const [workspace, models] = await Promise.all([api.workspaceInfo(), api.models()]);
      if (connection.last_model_profile && !resolveRememberedModel(connection, models)) {
        updateConnection(connection.id, (current) => (
          current.last_model_profile === connection.last_model_profile
            ? { ...current, last_model_profile: null }
            : current
        ));
      }
      const projects = connection.projects.length > 0
        ? connection.projects
        : [{
          id: crypto.randomUUID(),
          kind: "project" as const,
          path: workspace.startup_cwd,
          name: basename(workspace.startup_cwd),
          directories: [workspace.startup_cwd],
          is_default: true,
          last_session_id: null,
            favorite_session_ids: [],
          }];
      const sessionEntries = await Promise.all(
        projects.map(async (project) => [project.path, await api.sessions(project.path)] as const),
      );
      setGateways((current) => ({
        ...current,
        [connection.id]: {
          status: "online",
          error: null,
          token: api.accessToken,
          tokenExpiresAt: api.tokenExpiresAt,
          workspace,
          models,
          sessionsByProject: Object.fromEntries(sessionEntries),
          runningCount: 0,
          pendingCount: 0,
        },
      }));
      if (connection.projects.length === 0) {
        updateConnection(connection.id, (current) => ({
          ...current,
          projects,
          last_project_path: workspace.startup_cwd,
          last_project_id: projects[0].id,
        }));
      }
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      const message = isLoopbackUrl(connection.base_url) && !isDesktopShell()
        ? `浏览器版不会自动启动本地 Gateway。请先运行 crabcode gateway。${detail}`
        : detail;
      setGateways((current) => ({
        ...current,
        [connection.id]: {
          ...(current[connection.id] ?? EMPTY_GATEWAY),
          status: "error",
          error: message,
        },
      }));
    }
  }, [updateConnection]);

  useEffect(() => {
    void loadSettings()
      .then((loaded) => {
        setSettings(loaded);
        if (isDesktopShell()) void setDockIcon(loaded.dock_icon).catch(() => undefined);
      })
      .catch((error) => setGlobalError(String(error)));
    return () => {
      channelRef.current.forEach((channel) => channel.dispose());
      channelRef.current.clear();
    };
  }, []);

  useEffect(() => {
    if (!settings) return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const root = document.documentElement;
    const applyAppearance = () => {
      const dark = settings.theme_mode === "dark"
        || (settings.theme_mode === "system" && media.matches);
      const theme = resolveActiveTheme(settings);
      const profile = dark ? theme.dark : theme.light;
      const tokens = resolveThemeTokens(profile, dark);
      root.dataset.theme = dark ? "dark" : "light";
      root.dataset.themeMode = settings.theme_mode;
      root.dataset.themeId = theme.id;
      root.dataset.themeVisuals = String(Boolean(theme.visuals && Object.keys(theme.visuals).length));
      root.dataset.customBackground = "true";
      root.dataset.customForeground = "true";
      root.dataset.translucentSidebar = String(profile.translucent_sidebar);
      root.dataset.pointerCursor = String(settings.pointer_cursor);
      root.dataset.diffMarkers = settings.diff_marker_style;
      root.dataset.fontSmoothing = String(settings.font_smoothing);
      for (const [name, value] of Object.entries(tokens)) {
        root.style.setProperty(`--${name.replaceAll("_", "-")}`, value);
      }
      root.style.setProperty("--ui-font-family", ({
        system: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
        inter: 'Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
        serif: 'ui-serif, Georgia, "Times New Roman", serif',
      })[profile.ui_font_family]);
      root.style.setProperty("--code-font-family", ({
        "system-mono": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
        menlo: 'Menlo, Monaco, "Courier New", monospace',
        monaco: 'Monaco, Menlo, "Courier New", monospace',
      })[profile.code_font_family]);
      root.style.setProperty("--ui-font-size", `${settings.ui_font_size}px`);
      root.style.setProperty("--code-font-size", `${settings.code_font_size}px`);
      root.style.setProperty("--ui-contrast", String(0.8 + profile.contrast * 0.004));
      root.style.setProperty("--theme-sidebar-width", `${settings.sidebar_width}px`);
      const composerFrame = theme.visuals?.composer_frame;
      root.style.setProperty("--theme-composer-frame", composerFrame ? `url("${composerFrame.data_url}")` : "none");
      root.style.setProperty("--theme-composer-frame-opacity", String(composerFrame?.opacity ?? 0));
    };
    applyAppearance();
    if (settings.theme_mode === "system") media.addEventListener("change", applyAppearance);
    return () => media.removeEventListener("change", applyAppearance);
  }, [
    settings?.active_theme_id,
    settings?.code_font_size,
    settings?.custom_theme_presets,
    settings?.diff_marker_style,
    settings?.font_smoothing,
    settings?.pointer_cursor,
    settings?.sidebar_width,
    settings?.theme_mode,
    settings?.ui_font_size,
  ]);

  useEffect(() => {
    if (!settings) return;
    for (const connection of settings.connections) {
      if (connectedRef.current.has(connection.id)) continue;
      connectedRef.current.add(connection.id);
      void connectGateway(connection, settings.python_path);
    }
  }, [connectGateway, settings]);

  useEffect(() => {
    if (!activeConnection || activeGateway?.status !== "online") return;
    const cwd = activeProject?.path ?? activeGateway.workspace?.startup_cwd;
    const sessionId = activeSession && cwd
      && projectPathKey(activeSession.cwd) === projectPathKey(cwd)
      ? activeSession.id
      : undefined;
    void refreshSchedules(activeConnection.id);
    void refreshPlugins(activeConnection.id, sessionId, cwd);
  }, [
    activeConnection?.id,
    activeGateway?.status,
    activeGateway?.workspace?.startup_cwd,
    activeProject?.path,
    activeSession?.id,
    refreshPlugins,
    refreshSchedules,
  ]);

  useEffect(() => {
    if (
      !activeConnection
      || !activeProject
      || !activeSessionKey
      || activeGateway?.status !== "online"
    ) return;
    const channel = channelRef.current.get(activeSessionKey);
    if (channel && !channel.isDisposed) return;

    const sessionId = activeSession?.id;
    const info = sessionId && !sessionId.startsWith("new-")
      ? activeList.find((item) => item.session_id === sessionId) ?? {
          session_id: sessionId,
          message_count: activeSession.items.length,
          model: activeSession.status?.model ?? "",
          provider: activeSession.status?.provider ?? "",
          created_at: "",
          title: activeSession.title,
          cwd: activeSession.cwd,
          tokens_used: activeSession.status?.context_used_tokens ?? 0,
          preview: "",
        }
      : undefined;
    openSession(activeConnection, activeProject, info);
  }, [
    activeConnection,
    activeGateway?.status,
    activeList,
    activeProject,
    activeSession,
    activeSessionKey,
    openSession,
  ]);

  useEffect(() => {
    if (activeSessionKey) {
      autoOpeningDocumentRef.current = null;
      return;
    }
    if (!shouldAutoOpenDocumentSession(
      workspaceView,
      Boolean(activeConnection),
      activeProject?.kind,
      activeSessionKey,
      activeGateway?.status,
    ) || !activeConnection || !activeProject) return;
    const target = `${activeConnection.id}:${activeProject.id}`;
    if (autoOpeningDocumentRef.current === target) return;
    autoOpeningDocumentRef.current = target;
    const preferred = activeList.find((item) => item.session_id === activeProject.last_session_id)
      ?? activeList[0];
    openSession(activeConnection, activeProject, preferred);
  }, [
    activeConnection,
    activeGateway?.status,
    activeList,
    activeProject,
    activeSessionKey,
    openSession,
    workspaceView,
  ]);

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ block: "end" });
  }, [activeSession?.items.length, activeSession?.items.at(-1)?.text]);

  useEffect(() => {
    if (!settingsOpen || connectionModal !== null || projectModal !== null) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSettingsOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [connectionModal, projectModal, settingsOpen]);

  const switchConnection = (connectionId: string) => {
    commitSettings((current) => ({ ...current, active_connection_id: connectionId }));
  };

  const openSettings = () => {
    setSettingsSection("general");
    setSettingsOpen(true);
  };

  const beginNewProject = () => {
    setDocumentCapabilities(undefined);
    setProjectTypeModal(true);
    if (!activeConnection) return;
    void apiRef.current.get(activeConnection.id)?.documentCapabilities()
      .then(setDocumentCapabilities)
      .catch(() => setDocumentCapabilities(null));
  };

  useEffect(() => {
    if (!settingsOpen || !activeConnection || activeGateway?.status !== "online") return;
    let cancelled = false;
    setDocumentCapabilities(undefined);
    void apiRef.current.get(activeConnection.id)?.documentCapabilities()
      .then(async (capabilities) => {
        if (
          isDesktopShell()
          && activeConnection.id === "local"
          && isLoopbackUrl(activeConnection.base_url)
        ) {
          try {
            const precise = await getDocumentEngineStatus(settings?.python_path ?? null);
            return {
              ...capabilities,
              translation_engines: {
                default: precise.available ? "precise" as const : "legacy" as const,
                legacy: { available: true as const, status: "ready" as const },
                precise,
              },
            };
          } catch {
            // A remote/older Gateway result is still useful if local CLI discovery fails.
          }
        }
        return capabilities;
      })
      .then((capabilities) => {
        if (!cancelled) setDocumentCapabilities(capabilities);
      })
      .catch(() => {
        if (!cancelled) setDocumentCapabilities(null);
      });
    return () => {
      cancelled = true;
    };
  }, [activeConnection?.id, activeConnection?.base_url, activeGateway?.status, settings?.python_path, settingsOpen]);

  useEffect(() => {
    if (
      !settingsOpen
      || settingsSection !== "models"
      || !activeConnection
      || activeGateway?.status !== "online"
    ) return;
    void refreshModelSettings(activeConnection.id, activeProject?.path);
  }, [
    activeConnection?.id,
    activeGateway?.status,
    activeProject?.path,
    refreshModelSettings,
    settingsOpen,
    settingsSection,
  ]);

  useEffect(() => {
    if (
      !settingsOpen
      || settingsSection !== "runtime"
      || !activeConnection
      || activeGateway?.status !== "online"
    ) return;
    void refreshRuntimeSettings(activeConnection.id, activeProject?.path);
  }, [
    activeConnection?.id,
    activeGateway?.status,
    activeProject?.path,
    refreshRuntimeSettings,
    settingsOpen,
    settingsSection,
  ]);

  const deleteConnection = useCallback(async (id: string) => {
    if (!settings) return;
    const connection = settings.connections.find((item) => item.id === id);
    if (!connection || id === "local") return;
    if (!window.confirm(`删除 Gateway 连接“${connection.name}”？本地项目文件不会被删除。`)) return;

    try {
      if (connection.credential_ref) await deleteCredential(connection.credential_ref);
      channelRef.current.forEach((channel, key) => {
        if (!key.startsWith(`${id}:`)) return;
        channel.dispose();
        channelRef.current.delete(key);
      });
      apiRef.current.delete(id);
      connectedRef.current.delete(id);
      setSessions((current) => Object.fromEntries(
        Object.entries(current).filter(([key]) => !key.startsWith(`${id}:`)),
      ));
      setActiveSessions((current) => {
        const { [id]: _, ...rest } = current;
        return rest;
      });
      setGateways((current) => {
        const { [id]: _, ...rest } = current;
        return rest;
      });
      commitSettings((current) => ({
        ...current,
        active_connection_id: current.active_connection_id === id ? "local" : current.active_connection_id,
        connections: current.connections.filter((item) => item.id !== id),
        connection_order: current.connection_order.filter((value) => value !== id),
      }));
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  }, [commitSettings, settings]);

  const switchProject = (project: ProjectPreset) => {
    if (!activeConnection) return;
    if (project.directories.length === 0) {
      setProjectModal(project);
      return;
    }
    setWorkspaceView("chat");
    setSearch("");
    setPendingDocumentReferences([]);
    updateConnection(activeConnection.id, (connection) => ({
      ...connection,
      last_project_path: project.path,
      last_project_id: project.id,
    }));
    void refreshProjectSessions(activeConnection.id, project.path).catch((error) => {
      setGlobalError(error instanceof Error ? error.message : String(error));
    });
    const lastId = project.last_session_id;
    if (lastId) {
      const info = sessionsForProject(activeGateway?.sessionsByProject ?? {}, project.path)
        .find((item) => item.session_id === lastId);
      if (info) {
        openSession(activeConnection, project, info);
        return;
      }
    }
    if (project.kind === "document") {
      openSession(activeConnection, project);
      return;
    }
    setActiveSessions((current) => ({ ...current, [activeConnection.id]: null }));
  };

  const removeProject = (removing: ProjectPreset): boolean => {
    if (!activeConnection) return false;
    if (removing.id === defaultProjectId) {
      setGlobalError("默认项目不能删除");
      return false;
    }
    if (removing.id === activeProject?.id) {
      setActiveSessions((current) => ({ ...current, [activeConnection.id]: null }));
    }
    updateConnection(activeConnection.id, (connection) => {
      const projects = connection.projects.filter((item) => item.id !== removing.id);
      const next = projects[0] ?? null;
      return withFavoriteItems({
        ...connection,
        projects,
        last_project_path: connection.last_project_id === removing.id ? next?.path ?? null : connection.last_project_path,
        last_project_id: connection.last_project_id === removing.id ? next?.id ?? null : connection.last_project_id,
      }, removeFavoriteEntries(favoriteEntries(connection), (entry) => (
        entry.type !== "folder" && entry.project_id === removing.id
      )));
    });
    return true;
  };

  const addImages = async (files: File[]) => {
    try {
      const images: PendingImage[] = [];
      for (const file of files) {
        if (file.size > 20 * 1024 * 1024) {
          setGlobalError(`${file.name} 超过 20MB`);
          continue;
        }
        images.push(await readImage(file));
      }
      if (images.length > 0) setPendingImages((current) => [...current, ...images]);
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  };

  const addFiles = async (files: File[]) => {
    try {
      const attachments: PendingFile[] = [];
      const maxSizeMb = settings?.file_upload_max_size_mb ?? 5;
      const maxBytes = maxSizeMb * 1024 * 1024;
      for (const file of files) {
        if (file.size > maxBytes) {
          setGlobalError(`${file.name} 超过 ${maxSizeMb}MB`);
          continue;
        }
        attachments.push({
          id: crypto.randomUUID(),
          name: file.name,
          mediaType: file.type,
          mode: "content",
          path: null,
          size: file.size,
          text: await file.text(),
        });
      }
      if (attachments.length > 0) setPendingFiles((current) => [...current, ...attachments]);
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  };

  const sendMessage = async () => {
    const text = composer.trim();
    if (
      (!text && pendingImages.length === 0 && pendingFiles.length === 0 && pendingFolders.length === 0 && pendingDocumentReferences.length === 0)
      || !activeChannel
      || !activeSessionKey
      || !activeSession
      || activeSession.loading
    ) return;
    if (
      text.startsWith("/")
      && pendingImages.length === 0
      && pendingFiles.length === 0
      && pendingFolders.length === 0
      && pendingDocumentReferences.length === 0
      && await executeDesktopSlashCommand(text)
    ) {
      setComposer("");
      return;
    }
    const fileContext = serializePendingFiles(pendingFiles);
    const folderContext = pendingFolders.map((path) => `<folder>\n${path}\n</folder>`).join("\n");
    const documentContext = pendingDocumentReferences.map((reference) => (
      `<document-reference>\n文档：${reference.document_name}\n位置：${formatDocumentReferenceLocation(reference)}\n\n${reference.text}\n</document-reference>`
    )).join("\n\n");
    const messageText = [fileContext, folderContext, documentContext, text].filter(Boolean).join("\n\n");
    const now = Date.now();
    try {
      if (activeSession.busy && activeSession.operationId) {
        activeChannel.steer(
          messageText,
          activeSession.operationId,
          pendingImages.map(({ media_type, data }) => ({ media_type, data })),
        );
        const attachmentLine = [
          ...pendingImages.map((image) => `[图片：${image.name}]`),
          ...pendingFiles.map((file) => `[文件：${file.name}]`),
          ...pendingFolders.map((path) => `[文件夹：${path}]`),
          ...pendingDocumentReferences.map((reference) => `[文档引用：${formatDocumentReferenceLocation(reference)}]`),
        ].join(" ");
        setSessions((current) => ({
          ...current,
          [activeSessionKey]: {
            ...current[activeSessionKey],
            runStartedAt: current[activeSessionKey].runStartedAt ?? now,
            currentStep: current[activeSessionKey].currentStep ?? {
              kind: "response",
              label: "接收引导",
              startedAt: now,
            },
            items: [
              ...current[activeSessionKey].items,
              { id: crypto.randomUUID(), kind: "user", text: `引导：${attachmentLine}${attachmentLine && text ? "\n\n" : ""}${text}`, status: "complete", startedAt: now, completedAt: now, durationMs: 0 },
            ],
          },
        }));
      } else {
        const operationId = activeChannel.sendMessage(
          messageText,
          pendingImages.map(({ media_type, data }) => ({ media_type, data })),
        );
        const attachmentLine = [
          ...pendingImages.map((image) => `[图片：${image.name}]`),
          ...pendingFiles.map((file) => `[文件：${file.name}]`),
          ...pendingFolders.map((path) => `[文件夹：${path}]`),
          ...pendingDocumentReferences.map((reference) => `[文档引用：${formatDocumentReferenceLocation(reference)}]`),
        ].join(" ");
        setSessions((current) => ({
          ...current,
          [activeSessionKey]: {
            ...current[activeSessionKey],
            busy: true,
            runStartedAt: current[activeSessionKey].runStartedAt ?? now,
            currentStep: current[activeSessionKey].currentStep ?? {
              kind: "response",
              label: "发送任务",
              startedAt: now,
            },
            operationId,
            items: [
              ...current[activeSessionKey].items,
              { id: crypto.randomUUID(), kind: "user", text: `${attachmentLine}${attachmentLine && text ? "\n\n" : ""}${text}`, status: "complete", startedAt: now, completedAt: now, durationMs: 0 },
            ],
          },
        }));
      }
      setComposer("");
      setPendingImages([]);
      setPendingFiles([]);
      setPendingFolders([]);
      setPendingDocumentReferences([]);
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  };

  const resolvePermission = (item: ChatItem, allowed: boolean, always = false) => {
    if (!activeChannel || !activeSessionKey || !item.tool_use_id) return;
    let feedback: string | undefined;
    if (!allowed) feedback = window.prompt("拒绝原因（可选）") ?? undefined;
    try {
      activeChannel.permission(item.tool_use_id, allowed, always, feedback, item.agent_id);
      const response: GatewayEvent = {
        type: "permission_response",
        tool_use_id: item.tool_use_id,
        allowed,
        always_allow: always,
        feedback: feedback ?? null,
        agent_id: item.agent_id,
      };
      setSessions((current) => {
        const session = current[activeSessionKey];
        return session
          ? { ...current, [activeSessionKey]: applyGatewayEvent(session, response) }
          : current;
      });
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  };

  const toggleChoice = (item: ChatItem, option: string) => {
    if (!activeSessionKey) return;
    setSessions((current) => {
      const session = current[activeSessionKey];
      if (!session) return current;
      return {
        ...current,
        [activeSessionKey]: {
          ...session,
          items: session.items.map((candidate) => {
            if (candidate.id !== item.id) return candidate;
            const selected = candidate.multiple
              ? candidate.selected?.includes(option)
                ? candidate.selected.filter((value) => value !== option)
                : [...(candidate.selected ?? []), option]
              : [option];
            return { ...candidate, selected };
          }),
        },
      };
    });
  };

  const submitChoice = (item: ChatItem) => {
    if (!activeChannel || !activeSessionKey || !item.tool_use_id) return;
    const selected = item.selected ?? [];
    try {
      activeChannel.choice(item.tool_use_id, selected, false, item.agent_id);
      const response: GatewayEvent = {
        type: "choice_response",
        tool_use_id: item.tool_use_id,
        selected,
        agent_id: item.agent_id,
      };
      setSessions((current) => {
        const session = current[activeSessionKey];
        return session
          ? { ...current, [activeSessionKey]: applyGatewayEvent(session, response) }
          : current;
      });
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  };

  const updateSchedule = async (
    action: ScheduleAction,
    job: ScheduleJobInfo,
  ): Promise<boolean> => {
    if (!activeConnection) return false;
    const api = apiRef.current.get(activeConnection.id);
    if (!api) return false;
    setScheduleAction({ id: job.id, action });
    setScheduleError(null);
    try {
      if (action === "pause") await api.pauseSchedule(job.id);
      if (action === "resume") await api.resumeSchedule(job.id);
      if (action === "trigger") await api.triggerSchedule(job.id);
      if (action === "cancel") await api.cancelSchedule(job.id);
      await refreshSchedules(activeConnection.id);
      return true;
    } catch (error) {
      setScheduleError(error instanceof Error ? error.message : String(error));
      return false;
    } finally {
      setScheduleAction(null);
    }
  };

  const confirmScheduleDelete = async () => {
    if (!scheduleDeleteTarget) return;
    const deleted = await updateSchedule("cancel", scheduleDeleteTarget);
    if (deleted) setScheduleDeleteTarget(null);
  };

  const selectReasoningEffort = (effort: ReasoningEffort) => {
    if (!activeChannel || !activeSessionKey || !activeConnection || !activeProject || !activeSession) return;
    try {
      activeChannel.setReasoningEffort(effort);
      updateSessionPreferences(activeConnection.id, activeProject.id, activeSession.id, {
        reasoning_effort: effort,
      });
      setSessions((current) => {
        const session = current[activeSessionKey];
        if (!session?.status) return current;
        return {
          ...current,
          [activeSessionKey]: {
            ...session,
            status: { ...session.status, reasoning_effort: effort },
          },
        };
      });
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  };

  const selectMode = (mode: "agent" | "plan") => {
    if (!activeChannel || !activeSessionKey || !activeConnection || !activeProject || !activeSession) return;
    try {
      activeChannel.switchMode(mode);
      updateSessionPreferences(activeConnection.id, activeProject.id, activeSession.id, { mode });
      setSessions((current) => {
        const session = current[activeSessionKey];
        if (!session?.status) return current;
        return {
          ...current,
          [activeSessionKey]: {
            ...session,
            status: { ...session.status, mode },
          },
        };
      });
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  };

  const selectUltraMode = (enabled: boolean) => {
    if (!activeChannel || !activeSessionKey || !activeConnection || !activeProject || !activeSession) return;
    try {
      activeChannel.setUltraMode(enabled);
      updateSessionPreferences(activeConnection.id, activeProject.id, activeSession.id, {
        ultra_mode: enabled,
      });
      setSessions((current) => {
        const session = current[activeSessionKey];
        if (!session?.status) return current;
        return {
          ...current,
          [activeSessionKey]: {
            ...session,
            status: { ...session.status, ultra_mode: enabled },
          },
        };
      });
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  };

  const selectModel = (name: string) => {
    if (!activeChannel || !activeSessionKey || !activeConnection || !activeProject || !activeSession || !name) return;
    try {
      activeChannel.switchModel(name);
      setModelSelections((current) => ({ ...current, [activeSessionKey]: name }));
      updateSessionPreferences(activeConnection.id, activeProject.id, activeSession.id, {
        model_profile: name,
      });
      setSessions((current) => {
        const session = current[activeSessionKey];
        if (!session?.status) return current;
        return {
          ...current,
          [activeSessionKey]: {
            ...session,
            status: { ...session.status, model_profile: name },
          },
        };
      });
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  };

  const selectPermissionMode = (mode: PermissionMode) => {
    if (!activeChannel || !activeSessionKey || !activeConnection || !activeProject || !activeSession) return;
    try {
      activeChannel.setPermissionMode(mode);
      updateSessionPreferences(activeConnection.id, activeProject.id, activeSession.id, {
        permission_mode: mode,
      });
      setPermissionSelections((current) => ({ ...current, [activeSessionKey]: mode }));
      setSessions((current) => {
        const session = current[activeSessionKey];
        if (!session?.status) return current;
        return {
          ...current,
          [activeSessionKey]: {
            ...session,
            status: { ...session.status, permission_mode: mode },
          },
        };
      });
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  };

  function appendCommandMessage(text: string, error = false, title?: string, command?: string) {
    if (!activeSessionKey) return;
    setSessions((current) => {
      const session = current[activeSessionKey];
      if (!session) return current;
      return {
        ...current,
        [activeSessionKey]: {
          ...session,
          items: [...session.items, {
            id: crypto.randomUUID(),
            kind: error ? "error" : title ? "command" : "system",
            title,
            command,
            text,
            status: error ? "failed" : "complete",
          }],
        },
      };
    });
  }

  function appendCommandCard(command: string, title: string, text: string) {
    appendCommandMessage(text, false, title, command);
  }

  function commandSessionId(): string {
    const sessionId = activeChannel?.sessionId;
    if (!sessionId) throw new Error("会话尚未就绪，请稍后重试");
    return sessionId;
  }

  function resolveVisibleSession(selector: string): { project: ProjectPreset; session: SessionInfo } | null {
    if (!activeConnection || !activeGateway) return null;
    const candidates = activeConnection.projects.flatMap((project) => (
      sessionsForProject(activeGateway.sessionsByProject, project.path)
        .map((session) => ({ project, session }))
    ));
    const exact = candidates.find(({ session }) => session.session_id === selector);
    if (exact) return exact;
    const matches = candidates.filter(({ session }) => session.session_id.startsWith(selector));
    return matches.length === 1 ? matches[0] : null;
  }

  async function executeDesktopSlashCommand(text: string): Promise<boolean> {
    const spaceIndex = text.indexOf(" ");
    const command = (spaceIndex < 0 ? text : text.slice(0, spaceIndex)).toLocaleLowerCase();
    if (!DESKTOP_COMMAND_NAMES.has(command)) return false;
    const args = spaceIndex < 0 ? "" : text.slice(spaceIndex + 1).trim();
    const api = activeConnection ? apiRef.current.get(activeConnection.id) : null;

    try {
      if (command === "/help") {
        const summary = composerCommands
          .filter((option) => option.kind === "command")
          .map((option) => `${option.name} — ${option.description}`)
          .join("\n");
        appendCommandCard(command, "快捷命令", summary || "暂无可用快捷命令");
        return true;
      }
      if (command === "/plan" || command === "/agent") {
        if (args) throw new Error(`用法：${command}`);
        selectMode(command === "/plan" ? "plan" : "agent");
        appendCommandMessage(command === "/plan" ? "已切换到 Plan 模式" : "已切换到 Agent 模式");
        return true;
      }
      if (command === "/status") {
        if (args) throw new Error("用法：/status");
        const status = activeSession?.status;
        if (!status) throw new Error("会话状态尚未就绪");
        appendCommandCard(command, "会话状态", [
          `模型 ${status.model_profile || status.model || "默认"}`,
          `模式 ${status.mode === "plan" ? "Plan" : "Agent"}`,
          `推理 ${status.reasoning_effort || "自动"}`,
          `Ultra ${status.ultra_mode ? "开启" : "关闭"}`,
          `上下文 ${status.context_used_percent.toFixed(1)}%`,
        ].join("\n"));
        return true;
      }
      if (command === "/effort") {
        if (!args) {
          appendCommandCard(command, "推理强度", `当前推理强度：${activeSession?.status?.reasoning_effort || "自动"}`);
          return true;
        }
        const allowed: ReasoningEffort[] = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];
        if (!allowed.includes(args as ReasoningEffort)) throw new Error("用法：/effort <none|minimal|low|medium|high|xhigh|max>");
        selectReasoningEffort(args as ReasoningEffort);
        appendCommandMessage(`推理强度已切换为 ${args}`);
        return true;
      }
      if (command === "/ultra") {
        if (args && args !== "true" && args !== "false") throw new Error("用法：/ultra [true|false]");
        const enabled = args ? args === "true" : !activeSession?.status?.ultra_mode;
        selectUltraMode(enabled);
        appendCommandMessage(`Ultra 模式已${enabled ? "开启" : "关闭"}`);
        return true;
      }
      if (command === "/model") {
        if (!args) {
          const grouped = groupGatewayModels(activeGateway?.models ?? []);
          const models = grouped.length > 0
            ? grouped.map(({ group, models: entries }) => `${group}: ${entries.map((model) => model.name).join("、")}`).join("\n")
            : "暂无可用模型";
          appendCommandMessage(`当前模型：${activeModel || activeSession?.status?.model || "默认"}\n可用模型：\n${models}`);
          return true;
        }
        const model = activeGateway?.models.find((item) => item.name.toLocaleLowerCase() === args.toLocaleLowerCase());
        if (!model) throw new Error(`找不到模型：${args}`);
        selectModel(model.name);
        appendCommandMessage(`模型已切换为 ${model.name}`);
        return true;
      }
      if (command === "/new") {
        if (args) throw new Error("Desktop 暂不支持 /new 参数；请先新建会话，再从工具栏选择模型");
        if (!activeConnection || !activeProject) throw new Error("当前没有可用项目");
        openSession(activeConnection, activeProject);
        return true;
      }
      if (!api) throw new Error("Gateway 尚未连接");
      if (command === "/compact") {
        const result = await api.compactSession(commandSessionId(), args);
        appendCommandMessage(result.status === "ok" ? "对话上下文已压缩" : "当前无需压缩");
        return true;
      }
      if (command === "/clear") {
        if (args) throw new Error("用法：/clear");
        const result = await api.clearSession(commandSessionId());
        if (activeSessionKey) {
          setSessions((current) => current[activeSessionKey]
            ? { ...current, [activeSessionKey]: { ...current[activeSessionKey], items: [] } }
            : current);
        }
        appendCommandMessage(`已清除 ${result.messages_cleared} 条历史消息`);
        return true;
      }
      if (command === "/sessions") {
        if (args) throw new Error("用法：/sessions");
        const list = Object.values(activeGateway?.sessionsByProject ?? {}).flat();
        appendCommandCard(command, "会话列表", list.length
          ? list.slice(0, 30).map((session) => `${session.title || "未命名会话"} (${session.session_id.slice(0, 8)})`).join("\n")
          : "暂无会话");
        return true;
      }
      if (command === "/recent") {
        const limit = args ? Number.parseInt(args, 10) : 10;
        if (!Number.isFinite(limit) || limit < 1) throw new Error("用法：/recent [数量]");
        const list = (await api.recentSessions()).slice(0, limit);
        appendCommandCard(command, "最近会话", list.length
          ? list.map((session) => `${session.title || "未命名会话"} (${session.session_id.slice(0, 8)})`).join("\n")
          : "暂无最近会话");
        return true;
      }
      if (command === "/search") {
        if (!args) throw new Error("用法：/search <关键词>");
        const list = await api.searchSessions(args);
        appendCommandCard(command, "会话搜索", list.length
          ? list.map((session) => `${session.title || "未命名会话"} (${session.session_id.slice(0, 8)})`).join("\n")
          : `没有找到与“${args}”匹配的会话`);
        return true;
      }
      if (command === "/archive") {
        if (!args) throw new Error("用法：/archive <session-id>");
        await api.archive(args);
        if (activeConnection) {
          await Promise.all(activeConnection.projects.map((project) => refreshProjectSessions(activeConnection.id, project.path)));
        }
        appendCommandMessage(`会话 ${args} 已归档`);
        return true;
      }
      if (command === "/stats") {
        if (args) throw new Error("用法：/stats");
        appendCommandCard(command, "使用统计", textFromUnknown(await api.sessionStats()));
        return true;
      }
      if (command === "/checkpoint") {
        const result = await api.createCheckpoint(commandSessionId(), args);
        const snapshotNote = result.snapshot_included === false
          ? "（仅保存对话，文件快照已跳过）"
          : "";
        appendCommandMessage(`已创建检查点 ${result.checkpoint_id.slice(0, 8)}${args ? `（${args}）` : ""}${snapshotNote}`);
        return true;
      }
      if (command === "/checkpoints") {
        if (args) throw new Error("用法：/checkpoints");
        setCheckpointModal(true);
        return true;
      }
      if (command === "/rollback" || command === "/revert") {
        if (!args) throw new Error(`用法：${command} <checkpoint-id>`);
        if (command === "/rollback") await api.rollback(commandSessionId(), args);
        else await api.revert(commandSessionId(), args);
        appendCommandMessage(`已${command === "/rollback" ? "回滚会话" : "还原文件和会话"}到检查点 ${args}`);
        return true;
      }
      if (command === "/undo") {
        if (args) throw new Error("用法：/undo");
        await api.undo(commandSessionId());
        appendCommandMessage("已撤销到最近的检查点");
        return true;
      }
      if (command === "/resume") {
        if (!args) {
          appendCommandMessage("用法：/resume <session-id>");
          return true;
        }
        const target = resolveVisibleSession(args.split(/\s+/)[0]);
        if (!target || !activeConnection) throw new Error(`找不到会话，或短 ID 不唯一：${args}`);
        updateConnection(activeConnection.id, (connection) => ({
          ...connection,
          last_project_path: target.project.path,
          last_project_id: target.project.id,
        }));
        openSession(activeConnection, target.project, target.session);
        return true;
      }
      if (command === "/goal") {
        if (!args) {
          setGoalModal(true);
          return true;
        }
        const action = args.startsWith("edit ") ? "edit" : "set";
        const objective = action === "edit" ? args.slice(5).trim() : args.replace(/^set\s+/, "").trim();
        if (!objective) throw new Error("用法：/goal [set|edit] <objective>");
        await api.manageGoal(commandSessionId(), action, objective);
        appendCommandMessage(`Goal 已${action === "edit" ? "更新" : "设置"}：${objective}`);
        return true;
      }
      if (command === "/tasks") {
        const tokens = args.split(/\s+/).filter(Boolean);
        const action = (tokens.shift() || "list").toLocaleLowerCase();
        if (action === "list") {
          setAutomationTab("monitor");
          setWorkspaceView("scheduled");
          return true;
        }
        const selector = tokens.shift();
        if (!selector) throw new Error("用法：/tasks [list|show|output|stop] <task-id>");
        if (action === "show") {
          const task = await api.backgroundTask(selector);
          appendCommandCard(command, "后台任务详情", [
            `任务：${task.description || task.task_id}`,
            `状态：${task.status}`,
            `类型：${task.task_type}`,
            `ID：${task.task_id}`,
          ].join("\n"));
        } else if (action === "output") {
          const lines = tokens[0] ? Number.parseInt(tokens[0], 10) : 200;
          if (!Number.isFinite(lines) || lines < 1) throw new Error("用法：/tasks output <task-id> [lines]");
          const output = await api.backgroundTaskOutput(selector, lines);
          appendCommandCard(command, "后台任务输出", output.lines.length ? output.lines.join("\n") : "该任务暂无输出");
        } else if (action === "stop") {
          if (tokens.length) throw new Error("用法：/tasks stop <task-id>");
          await api.stopBackgroundTask(selector);
          appendCommandMessage(`后台任务 ${selector} 已停止`);
          if (activeConnection) await refreshMonitors(activeConnection.id);
        } else {
          throw new Error("用法：/tasks [list|show|output|stop] <task-id>");
        }
        return true;
      }
      if (command === "/schedule") {
        const tokens = args.split(/\s+/).filter(Boolean);
        const action = (tokens.shift() || "list").toLocaleLowerCase();
        if (action === "list" || action === "show" || action === "runs" || action === "create") {
          setAutomationTab("schedule");
          setWorkspaceView("scheduled");
          if (action !== "list") appendCommandMessage(`已打开“已安排”页面；请在页面中继续 ${action} 操作`);
          return true;
        }
        const actionMap: Record<string, ScheduleAction> = { pause: "pause", resume: "resume", run: "trigger", cancel: "cancel" };
        const scheduleAction = actionMap[action];
        const selector = tokens[0];
        if (!scheduleAction || !selector || tokens.length !== 1) {
          throw new Error("用法：/schedule [list|show|runs|create|pause|resume|run|cancel] [job-id]");
        }
        const matches = activeJobs.filter((job) => job.id === selector || job.id.startsWith(selector));
        if (matches.length !== 1) throw new Error(`找不到已安排任务，或短 ID 不唯一：${selector}`);
        if (scheduleAction === "cancel") {
          setScheduleDeleteTarget(matches[0]);
        } else {
          await updateSchedule(scheduleAction, matches[0]);
          appendCommandMessage(`已执行 /schedule ${action} ${selector}`);
        }
        return true;
      }
      return false;
    } catch (reason) {
      appendCommandMessage(reason instanceof Error ? reason.message : String(reason), true);
      return true;
    }
  }

  const activeFavoriteEntries = favoriteEntries(activeConnection);
  const favoriteSessionIds = new Set(activeProject
    ? favoriteSessionIdsForProject(activeFavoriteEntries, activeProject.id)
    : []);
  const visibleSessions = displayList.filter((item) => (
    item.title.trim().length > 0 || item.message_count > 0 || item.preview.trim().length > 0
  ));
  const filteredSessions = visibleSessions.filter((item) => {
    const needle = search.trim().toLowerCase();
    return !needle || item.title.toLowerCase().includes(needle) || item.preview.toLowerCase().includes(needle);
  }).sort((left, right) => (
    Number(favoriteSessionIds.has(right.session_id)) - Number(favoriteSessionIds.has(left.session_id))
  ));
  const activeSessionFavorite = Boolean(activeSession && favoriteSessionIds.has(activeSession.id));
  const favoriteItems = useMemo(
    () => resolveFavoriteEntries(activeConnection, activeGateway),
    [activeConnection, activeGateway],
  );
  const favoriteItemCount = countFavoriteItems(activeFavoriteEntries);
  const activeJobs = activeConnection ? scheduleJobs[activeConnection.id] ?? [] : [];
  const activeMonitorTasks = activeConnection ? monitorTasks[activeConnection.id] ?? [] : [];
  const resourceSessionId = activeSession && activeProject
    && projectPathKey(activeSession.cwd) === projectPathKey(activeProject.path)
    ? activeSession.id
    : undefined;
  const activePluginData = activeConnection
    ? pluginData[activeConnection.id] ?? { skills: [], tools: [] }
    : { skills: [], tools: [] };
  const activeModel = activeSessionKey
    ? modelSelections[activeSessionKey] || activeSession?.status?.model_profile || activeSession?.status?.model || ""
    : "";
  const activePermissionMode = activeSessionKey
    ? permissionSelections[activeSessionKey] || normalizePermissionMode(activeSession?.status?.permission_mode)
    : "default";
  const documentMode = workspaceView === "chat" && activeProject?.kind === "document";
  const documentAgentCollapsed = shouldCollapseDocumentAgent(
    documentMode,
    settings?.document_agent_collapsed === true,
  );
  const projectFilesEligible = workspaceView === "chat"
    && activeProject?.kind === "project"
    && Boolean(activeConnection && apiRef.current.get(activeConnection.id))
    && activeGateway?.status === "online";
  const projectFilesWideLayout = projectFilesEligible && wideProjectFilesLayout;
  const projectFilesWideOpen = projectFilesEligible
    && wideProjectFilesLayout
    && projectFilesOpen;
  const projectFilesDrawerVisible = projectFilesEligible
    && !wideProjectFilesLayout
    && projectFilesOpen;
  const projectFilesVisible = projectFilesWideOpen || projectFilesDrawerVisible;
  const referencedProjectFilePaths = useMemo(() => new Set(
    pendingFiles.flatMap((file) => file.mode === "path" && file.path ? [file.path] : []),
  ), [pendingFiles]);
  const composerReferences = useMemo<ComposerReferenceOption[]>(() => [
    ...pendingImages.map((image) => ({
      key: `image:${image.id}`,
      kind: "image" as const,
      label: image.name,
      detail: "图片",
    })),
    ...pendingFiles.map((file) => ({
      key: `file:${file.id}`,
      kind: "file" as const,
      label: file.name,
      detail: file.mode === "path" ? `路径 · ${file.path}` : `文件 · ${formatFileSize(file.size ?? 0)}`,
    })),
    ...pendingFolders.map((path) => ({
      key: `folder:${path}`,
      kind: "folder" as const,
      label: basename(path),
      detail: path,
    })),
    ...pendingDocumentReferences.map((reference) => ({
      key: `document:${reference.id}`,
      kind: "document" as const,
      label: reference.document_name,
      detail: `文档引用 · ${formatDocumentReferenceLocation(reference)}`,
    })),
  ], [pendingDocumentReferences, pendingFiles, pendingFolders, pendingImages]);
  const composerCommands = useMemo(() => createComposerCommandOptions(
    activeGateway?.models ?? [],
    activePluginData.skills,
    DESKTOP_COMMAND_NAMES,
  ), [activeGateway?.models, activePluginData.skills]);

  useEffect(() => {
    if (!activeSession?.busy) return;
    setRunClock(Date.now());
    const timer = window.setInterval(() => setRunClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [activeSession?.busy, activeSessionKey]);

  useEffect(() => {
    if (
      workspaceView !== "scheduled"
      || !activeConnection
    ) return;
    if (automationTab === "schedule" && !activeJobs.some((job) => job.running)) return;
    const timer = window.setInterval(() => {
      if (automationTab === "monitor") void refreshMonitors(activeConnection.id);
      else void refreshSchedules(activeConnection.id);
    }, 1_500);
    return () => window.clearInterval(timer);
  }, [activeConnection, activeJobs, automationTab, refreshMonitors, refreshSchedules, workspaceView]);

  useEffect(() => {
    if (workspaceView !== "scheduled" || !activeConnection || activeGateway?.status !== "online") return;
    if (automationTab === "monitor") void refreshMonitors(activeConnection.id);
    else void refreshSchedules(activeConnection.id);
  }, [activeConnection, activeGateway?.status, automationTab, refreshMonitors, refreshSchedules, workspaceView]);

  useEffect(() => {
    const previous = focusedSessionRef.current;
    const nextProjectPath = activeProject?.path ?? "";
    const sameEmptyHandshake = Boolean(
      previous
      && activeSession
      && previous.sessionId.startsWith("new-")
      && !activeSession.id.startsWith("new-")
      && activeSessionKey === sessionKey(activeConnection?.id ?? "", activeSession.id)
      && projectPathKey(activeSession.cwd) === projectPathKey(previous.cwd)
      && activeSession.items.length === 0,
    );
    const changedFocus = previous && (
      previous.key !== activeSessionKey
      || previous.view !== workspaceView
      || projectPathKey(previous.projectPath) !== projectPathKey(nextProjectPath)
    );
    // Only discard a never-materialized handshake session. A real session ID
    // is durable state and must never be archived as a side effect of focus
    // changes or reconnects; the session list can lag behind the live state.
    if (
      changedFocus
      && !sameEmptyHandshake
      && previous.sessionId.startsWith("new-")
      && !previous.busy
    ) {
      void archiveSession(previous);
    }

    if (!activeSessionKey || !activeSession || workspaceView !== "chat") {
      focusedSessionRef.current = null;
      return;
    }
    const sessionInfo = activeList.find((item) => item.session_id === activeSession.id);
    focusedSessionRef.current = {
      connectionId: activeConnection?.id ?? "",
      cwd: activeSession.cwd,
      key: activeSessionKey,
      sessionId: activeSession.id,
      empty: activeSession.items.length === 0 && (!sessionInfo || sessionInfo.message_count === 0),
      busy: activeSession.busy,
      projectPath: nextProjectPath,
      view: workspaceView,
    };
  }, [
    activeConnection?.id,
    activeList,
    activeProject?.path,
    activeSession,
    activeSessionKey,
    archiveSession,
    workspaceView,
  ]);

  const startDocumentAction = (
    action: "translate" | "generate_blog",
    options: {
      locale?: string;
      language?: string;
      source?: "original" | "translation";
      translation_concurrency?: number;
      translation_batch_size?: number;
      translation_engine?: "auto" | "legacy" | "precise";
    },
  ): boolean => {
    if (!activeChannel) {
      setGlobalError("Agent 会话尚未就绪，请稍后重试");
      return false;
    }
    try {
      const operationId = activeChannel.documentAction(action, options);
      if (activeSessionKey) {
        const startedAt = Date.now();
        const title = action === "translate" ? "翻译文档" : "生成 Blog";
        setSessions((current) => current[activeSessionKey]
          ? {
              ...current,
              [activeSessionKey]: {
                ...current[activeSessionKey],
                busy: true,
                operationId,
                error: null,
                runStartedAt: current[activeSessionKey].runStartedAt ?? startedAt,
                currentStep: { kind: "document", label: title, startedAt },
                items: [
                  ...current[activeSessionKey].items,
                  {
                    id: `${operationId}:document-job`,
                    kind: "document_job" as const,
                    title,
                    text: "正在准备文档内容",
                    action,
                    locale: options.locale,
                    language: options.language,
                    source: options.source,
                    engine: options.translation_engine === "legacy" ? "legacy" : undefined,
                    current: 0,
                    total: 0,
                    status: "running" as const,
                    startedAt,
                  },
                ],
              },
            }
          : current);
      }
      return true;
    } catch (reason) {
      setGlobalError(reason instanceof Error ? reason.message : String(reason));
      return false;
    }
  };

  if (!settings) {
    return <div className="boot"><LoaderCircle className="spin" />正在加载 Crab Desktop</div>;
  }

  const activeTheme = resolveActiveTheme(settings);

  const activeModelSettingsKey = activeConnection
    ? `${activeConnection.id}\u0000${activeProject?.path ?? ""}`
    : null;
  const activeModelSettingsState = modelSettingsState?.key === activeModelSettingsKey
    ? modelSettingsState
    : null;
  const activeRuntimeSettingsKey = activeConnection
    ? `${activeConnection.id}\u0000${activeProject?.path ?? ""}`
    : null;
  const activeRuntimeSettingsState = runtimeSettingsState?.key === activeRuntimeSettingsKey
    ? runtimeSettingsState
    : null;

  return (
    <div className="app-shell">
      <ThemeDecorations theme={activeTheme} />
      {settingsOpen ? (
        <SettingsView
          settings={settings}
          gateways={gateways}
          activeConnection={activeConnection}
          activeProject={activeProject}
          activeSection={settingsSection}
          onSectionChange={setSettingsSection}
          onBack={() => setSettingsOpen(false)}
          onSavePythonPath={(pythonPath) => {
            commitSettings((current) => ({ ...current, python_path: pythonPath || null }));
          }}
          onConversationChange={(changes) => {
            commitSettings((current) => ({ ...current, ...changes }));
          }}
          onDocumentChange={(changes) => {
            commitSettings((current) => ({ ...current, ...changes }));
          }}
          onThemeModeChange={(mode) => {
            commitSettings((current) => ({ ...current, theme_mode: mode }));
          }}
          onThemeProfileChange={(scheme, changes) => {
            commitSettings((current) => updateActiveThemeProfile(current, scheme, changes));
          }}
          onThemePresetChange={(id) => {
            commitSettings((current) => ({ ...current, active_theme_id: id }));
          }}
          onThemeDuplicate={(id) => {
            commitSettings((current) => duplicateTheme(current, id));
          }}
          onThemeRename={(id, name) => {
            commitSettings((current) => renameCustomTheme(current, id, name));
          }}
          onThemeDelete={(id) => {
            commitSettings((current) => deleteCustomTheme(current, id));
          }}
          onThemeRestoreDefault={() => {
            commitSettings((current) => ({ ...current, active_theme_id: DEFAULT_THEME_ID }));
          }}
          onThemeImport={(theme) => {
            commitSettings((current) => addImportedTheme(current, theme));
          }}
          onThemeImportFailure={() => {
            commitSettings((current) => ({ ...current, active_theme_id: DEFAULT_THEME_ID }));
          }}
          onAppearanceChange={(changes) => {
            commitSettings((current) => ({ ...current, ...changes }));
          }}
          onDockIconChange={async (choice, pngBytes) => {
            await setDockIcon(choice, pngBytes);
            commitSettings((current) => ({ ...current, dock_icon: choice }));
          }}
          onActivateConnection={switchConnection}
          onNewConnection={() => setConnectionModal("new")}
          onEditConnection={setConnectionModal}
          onDeleteConnection={(id) => void deleteConnection(id)}
          modelSettings={activeModelSettingsState?.data ?? null}
          modelSettingsLoading={activeModelSettingsState?.loading ?? false}
          modelSettingsError={activeModelSettingsState?.error ?? null}
          onRefreshModelSettings={() => {
            if (activeConnection) void refreshModelSettings(activeConnection.id, activeProject?.path);
          }}
          onMutateModelSettings={(mutation) => {
            if (!activeConnection) return Promise.reject(new Error("未选择 Gateway"));
            return mutateModelSettings(activeConnection.id, mutation);
          }}
          runtimeSettings={activeRuntimeSettingsState?.data ?? null}
          runtimeSettingsLoading={activeRuntimeSettingsState?.loading ?? false}
          runtimeSettingsError={activeRuntimeSettingsState?.error ?? null}
          onRefreshRuntimeSettings={() => {
            if (activeConnection) void refreshRuntimeSettings(activeConnection.id, activeProject?.path);
          }}
          onMutateRuntimeSettings={(mutation) => {
            if (!activeConnection) return Promise.reject(new Error("未选择 Gateway"));
            return mutateRuntimeSettings(activeConnection.id, mutation);
          }}
          onNewProject={beginNewProject}
          onEditProject={setProjectModal}
          onDocumentWorkspaceRoot={(connectionId, path) => updateConnection(connectionId, (connection) => ({
            ...connection,
            document_workspace_root: path,
          }))}
          documentCapabilities={documentCapabilities}
          documentEngineBusy={documentEngineBusy}
          documentEngineProgress={documentEngineProgress}
          documentEngineError={documentEngineError}
          canManageDocumentEngine={Boolean(
            isDesktopShell()
            && activeConnection?.id === "local"
            && activeConnection
            && isLoopbackUrl(activeConnection.base_url)
          )}
          onInstallDocumentEngine={async () => {
            setDocumentEngineBusy("install");
            setDocumentEngineError(null);
            setDocumentEngineProgress({
              operationId: "pending",
              stage: "preparing",
              detail: "正在准备安装高精度 PDF 引擎",
              percent: 3,
            });
            try {
              await installDocumentEngine(settings.python_path, null, (progress) => {
                setDocumentEngineProgress({
                  ...progress,
                  percent: Math.max(0, Math.min(100, progress.percent)),
                });
              });
            } catch (reason) {
              setDocumentEngineError(reason instanceof Error ? reason.message : String(reason));
              throw reason;
            } finally {
              if (activeConnection) {
                try {
                  const capabilities = await apiRef.current.get(activeConnection.id)?.documentCapabilities();
                  if (!capabilities) {
                    setDocumentCapabilities(null);
                  } else {
                    const precise = await getDocumentEngineStatus(settings.python_path);
                    setDocumentCapabilities({
                      ...capabilities,
                      translation_engines: {
                        default: precise.available ? "precise" : "legacy",
                        legacy: { available: true, status: "ready" },
                        precise,
                      },
                    });
                  }
                } catch {
                  setDocumentCapabilities(null);
                }
              }
              setDocumentEngineBusy(null);
              setDocumentEngineProgress(null);
            }
          }}
          onRemoveDocumentEngine={async () => {
            setDocumentEngineBusy("remove");
            setDocumentEngineProgress(null);
            setDocumentEngineError(null);
            try {
              await removeDocumentEngine(settings.python_path);
            } catch (reason) {
              setDocumentEngineError(reason instanceof Error ? reason.message : String(reason));
              throw reason;
            } finally {
              if (activeConnection) {
                try {
                  const capabilities = await apiRef.current.get(activeConnection.id)?.documentCapabilities();
                  if (!capabilities) {
                    setDocumentCapabilities(null);
                  } else {
                    const precise = await getDocumentEngineStatus(settings.python_path);
                    setDocumentCapabilities({
                      ...capabilities,
                      translation_engines: {
                        default: precise.available ? "precise" : "legacy",
                        legacy: { available: true, status: "ready" },
                        precise,
                      },
                    });
                  }
                } catch {
                  setDocumentCapabilities(null);
                }
              }
              setDocumentEngineBusy(null);
            }
          }}
        />
      ) : (
      <>
        <header className="gateway-tabs">
        <button
          className="icon-button sidebar-toggle"
          title={sidebarOpen ? "隐藏侧栏" : "显示侧栏"}
          onClick={() => setSidebarOpen((value) => !value)}
        >
          {sidebarOpen ? <PanelLeftClose /> : <PanelLeftOpen />}
        </button>
        <div className="top-brand" aria-label="Crab Desktop 工作台">
          <span className={`top-brand-mark ${activeGateway?.status === "online" ? "online" : ""}`}><Code2 /></span>
          <span className="top-brand-name">Crab Desktop</span>
          <span className="top-brand-mode">WORKBENCH</span>
        </div>
        <div className="tab-strip">
          {settings.connection_order.map((id) => {
            const connection = settings.connections.find((item) => item.id === id);
            if (!connection) return null;
            const gateway = gateways[id];
            const connectionSessions = Object.entries(sessions)
              .filter(([key]) => key.startsWith(`${id}:`))
              .map(([, value]) => value);
            const running = connectionSessions.filter((item) => item.busy).length;
            const pending = connectionSessions.flatMap((item) => item.items)
              .filter((item) => item.status === "pending" && (item.kind === "permission" || item.kind === "choice"))
              .length;
            return (
              <button
                key={id}
                className={`gateway-tab ${activeConnection?.id === id ? "active" : ""}`}
                onClick={() => switchConnection(id)}
                onDoubleClick={() => {
                  const name = window.prompt("连接名称", connection.name)?.trim();
                  if (name) updateConnection(id, (current) => ({ ...current, name }));
                }}
                title={`${connection.name}\n${connection.base_url}`}
              >
                <ConnectionDot status={gateway?.status ?? "connecting"} />
                <span>{connection.name}</span>
                {running > 0 && <span className="tab-count running">{running}</span>}
                {pending > 0 && <span className="tab-count pending">{pending}</span>}
              </button>
            );
          })}
        </div>
        <button className="icon-button" title="连接 Gateway" onClick={() => setConnectionModal("new")}>
          <Plus />
        </button>
        </header>

      <div className={`workbench ${sidebarOpen ? "" : "sidebar-collapsed"}`}>
        <aside
          className={`sidebar ${sidebarOpen ? "open" : "closed"}`}
          style={{ width: sidebarOpen ? settings.sidebar_width : 0 }}
          aria-hidden={!sidebarOpen}
          {...(!sidebarOpen ? { inert: "" } : {})}
        >
          <div className="sidebar-content" style={{ width: settings.sidebar_width }}>
            <div className="workspace-brand">
              <div className="workspace-brand-lockup" title={activeProject?.path ?? activeConnection?.base_url}>
                <span className="workspace-brand-copy">
                  <strong>Crab Desktop</strong>
                  <small>{activeProject?.name ?? "工作区"}</small>
                </span>
              </div>
              <div className="workspace-brand-actions">
                <button
                  className="icon-button small"
                  title="已安排"
                  onClick={() => setWorkspaceView("scheduled")}
                >
                  <Clock />
                </button>
                <button
                  className="icon-button small"
                  title="重新连接"
                  onClick={() => {
                    if (!activeConnection) return;
                    connectedRef.current.add(activeConnection.id);
                    void connectGateway(activeConnection, settings.python_path);
                  }}
                >
                  <RefreshCw />
                </button>
              </div>
            </div>

            {activeGateway?.status === "error" && (
              <button className="connection-error" onClick={() => setConnectionModal("new")}>
                <WifiOff />
                <span>{activeGateway.error}</span>
              </button>
            )}

            <nav className="workspace-nav" aria-label="工作区">
              <button
                className={`workspace-nav-item ${workspaceView === "chat" ? "active" : ""}`}
                disabled={!activeConnection || !activeProject?.directories.length || activeGateway?.status !== "online"}
                onClick={() => activeConnection && activeProject && openSession(activeConnection, activeProject)}
              >
                <MessageSquarePlus />
                <span>新会话</span>
              </button>
              <button
                className={`workspace-nav-item ${workspaceView === "scheduled" ? "active" : ""}`}
                onClick={() => setWorkspaceView("scheduled")}
              >
                <Clock />
                <span>已安排</span>
              </button>
              <button
                className={`workspace-nav-item ${workspaceView === "plugins" ? "active" : ""}`}
                onClick={() => setWorkspaceView("plugins")}
              >
                <Puzzle />
                <span>插件</span>
              </button>
              <button
                className={`workspace-nav-item ${workspaceView === "favorites" ? "active" : ""}`}
                disabled={!activeConnection || activeGateway?.status !== "online"}
                onClick={() => setWorkspaceView("favorites")}
              >
                <Star />
                <span>收藏</span>
                {favoriteItemCount > 0 && <span className="workspace-nav-count">{favoriteItemCount}</span>}
              </button>
            </nav>

            {workspaceView === "chat" && <section className={`sidebar-section projects-section ${projectsCollapsed ? "collapsed" : ""}`}>
              <div className="section-label">
                <button
                  className="section-label-toggle"
                  aria-expanded={!projectsCollapsed}
                  onClick={() => setProjectsCollapsed((value) => !value)}
                >
                  <span>项目</span>
                </button>
                <span className="section-label-actions">
                  <button
                    className="icon-button tiny"
                    title="添加项目"
                    disabled={activeGateway?.status !== "online"}
                    onClick={beginNewProject}
                  >
                    <Plus />
                  </button>
                  <button
                    className="icon-button tiny section-collapse"
                    title={projectsCollapsed ? "展开项目" : "折叠项目"}
                    aria-expanded={!projectsCollapsed}
                    onClick={() => setProjectsCollapsed((value) => !value)}
                  >
                    <ChevronDown className={projectsCollapsed ? "collapsed" : ""} />
                  </button>
                </span>
              </div>
              <div
                className="section-content"
                aria-hidden={projectsCollapsed}
                {...(projectsCollapsed ? { inert: "" } : {})}
              >
                <div className="section-content-inner">
                  <div className="project-list">
                    {activeConnection?.projects.map((project) => {
                      const projectFavorite = hasFavoriteProject(activeFavoriteEntries, project.id);
                      return <div className="project-item-row" key={project.id}>
                        <button
                          className={`project-item ${activeProject?.id === project.id ? "active" : ""}`}
                          title={projectDirectoryTitle(project)}
                          onClick={() => switchProject(project)}
                          onDoubleClick={() => setProjectModal(project)}
                        >
                          {project.kind === "document"
                            ? <FileText />
                            : activeProject?.id === project.id ? <FolderOpen /> : <Folder />}
                          <span>{project.name}</span>
                          {projectFavorite && <Star className="project-favorite" fill="currentColor" />}
                        </button>
                        <ProjectActionsMenu
                          projectName={project.name}
                          favorite={projectFavorite}
                          newSessionDisabled={project.directories.length === 0 || activeGateway?.status !== "online"}
                          deleteDisabled={project.id === defaultProjectId}
                          onNewSession={() => {
                            if (!activeConnection) return;
                            setSearch("");
                            updateConnection(activeConnection.id, (connection) => ({
                              ...connection,
                              last_project_path: project.path,
                              last_project_id: project.id,
                            }));
                            openSession(activeConnection, project);
                          }}
                          onEdit={() => setProjectModal(project)}
                          onToggleFavorite={() => toggleFavoriteProject(project.id)}
                          onDelete={() => setProjectDeleteTarget(project)}
                        />
                      </div>
                    })}
                  </div>
                </div>
              </div>
            </section>}

            {workspaceView === "chat" && <section className={`sidebar-section sessions-section ${sessionsCollapsed ? "collapsed" : ""}`}>
              <div className="section-label">
                <button
                  className="section-label-toggle"
                  aria-expanded={!sessionsCollapsed}
                  onClick={() => setSessionsCollapsed((value) => !value)}
                >
                  <span>会话</span>
                </button>
                <span className="section-label-actions">
                  <span className="section-count">{filteredSessions.length}</span>
                  <button
                    className="icon-button tiny section-collapse"
                    title={sessionsCollapsed ? "展开会话" : "折叠会话"}
                    aria-expanded={!sessionsCollapsed}
                    onClick={() => setSessionsCollapsed((value) => !value)}
                  >
                    <ChevronDown className={sessionsCollapsed ? "collapsed" : ""} />
                  </button>
                </span>
              </div>
              <div
                className="section-content"
                aria-hidden={sessionsCollapsed}
                {...(sessionsCollapsed ? { inert: "" } : {})}
              >
                <div className="section-content-inner">
                  <label className="session-search">
                    <Search />
                    <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索会话" />
                  </label>
                  <div className="session-list">
                    {filteredSessions.map((info) => {
                      const key = sessionKey(activeConnection!.id, info.session_id);
                      const view = sessions[key];
                      const isActive = activeSessionKey === key;
                      const favorite = favoriteSessionIds.has(info.session_id);
                      const deleting = deletingSessionIds.has(key);
                      const pending = view?.items.some((item) => item.status === "pending" && (
                        item.kind === "permission" || item.kind === "choice"
                      ));
                      return (
                        <div className={`session-item-row ${isActive ? "active" : ""}`} key={info.session_id}>
                          <button
                            type="button"
                            className="session-item"
                            onClick={() => activeConnection && activeProject && openSession(activeConnection, activeProject, info)}
                          >
                            <span className="session-title-row">
                              {favorite && <Star className="session-favorite" fill="currentColor" />}
                              <span className="session-title">{info.title || "未命名会话"}</span>
                              {view?.busy && <LoaderCircle className="spin" />}
                              {pending && <ShieldAlert className="pending-icon" />}
                            </span>
                            <span className="session-preview">{info.preview || formatDate(info.created_at)}</span>
                          </button>
                          <SessionActionsMenu
                            info={info}
                            status={view?.status ?? null}
                            favorite={favorite}
                            deleting={deleting}
                            onToggleFavorite={() => {
                              if (activeProject) toggleFavoriteSession(activeProject.id, info.session_id);
                            }}
                            onDelete={() => {
                              void archiveSession({
                                connectionId: activeConnection!.id,
                                cwd: info.cwd || activeProject?.path || "",
                                key,
                                sessionId: info.session_id,
                              });
                            }}
                          />
                        </div>
                      );
                    })}
                    {activeGateway?.status === "online" && filteredSessions.length === 0 && (
                      <div className="empty-sidebar">暂无会话</div>
                    )}
                  </div>
                </div>
              </div>
            </section>}

            <div className="workspace-settings-footer">
              <button className="workspace-settings-button" type="button" onClick={openSettings}>
                <Settings />
                <span>设置</span>
              </button>
            </div>
          </div>
        </aside>

        <main
          className={`main-panel ${documentMode ? "document-mode" : ""} ${documentAgentCollapsed ? "document-agent-collapsed" : ""} ${documentMode && documentAgentTransitioning ? "document-agent-transitioning" : ""} ${projectFilesWideLayout ? "project-files-mode" : ""} ${projectFilesWideOpen ? "project-files-open" : ""}`}
          style={documentMode ? {
            gridTemplateColumns: documentAgentCollapsed
              ? "minmax(0, 1fr) 44px"
              : `minmax(180px, 1fr) min(${settings.document_agent_width ?? 400}px, calc(100% - 180px))`,
          } : projectFilesWideLayout ? {
            "--project-files-width": `${projectFilesWidth}px`,
          } as CSSProperties : undefined}
        >
          {documentMode && activeProject && activeConnection && apiRef.current.get(activeConnection.id) && (
            <DocumentWorkspace
              api={apiRef.current.get(activeConnection.id)!}
              connectionId={activeConnection.id}
              project={activeProject}
              documentView={activeProject.document_view}
              agentWidth={settings.document_agent_width ?? 400}
              agentCollapsed={documentAgentCollapsed}
              showOriginalText={settings.document_show_original_text === true}
              translationConcurrency={settings.document_translation_concurrency}
              translationBatchSize={settings.document_translation_batch_size}
              sessionBusy={Boolean(activeSession?.busy)}
              sessionError={activeSession?.error ?? null}
              selectionTranslationEvent={activeSessionKey ? selectionTranslationEvents[activeSessionKey] ?? null : null}
              onAgentWidth={(width) => commitSettings((current) => ({ ...current, document_agent_width: width }))}
              onAgentCollapsed={updateDocumentAgentCollapsed}
              onDocumentViewState={(connectionId, projectId, state) => updateConnection(connectionId, (connection) => ({
                ...connection,
                projects: connection.projects.map((project) => project.id === projectId
                  ? { ...project, document_view: state }
                  : project),
              }))}
              onDocumentAction={startDocumentAction}
              onDocumentReference={(reference) => setPendingDocumentReferences((current) => (
                current.some((item) => item.text === reference.text && item.project_id === reference.project_id)
                  ? current
                  : [...current, reference]
              ))}
              onTranslateSelection={(text, locale) => {
                if (!activeChannel || !activeSession?.connected) {
                  setGlobalError("Agent 会话尚未就绪，请稍后重试");
                  return null;
                }
                try {
                  return activeChannel.translateDocumentSelection(text, locale);
                } catch (reason) {
                  setGlobalError(reason instanceof Error ? reason.message : String(reason));
                  return null;
                }
              }}
            />
          )}
          {workspaceView === "scheduled" ? (
            <ScheduledTasksView
              tab={automationTab}
              onTabChange={setAutomationTab}
              jobs={activeJobs}
              tasks={activeMonitorTasks}
              projects={activeConnection?.projects ?? []}
              sessionsByProject={activeGateway?.sessionsByProject ?? {}}
              loading={automationTab === "schedule" ? scheduleLoading : monitorLoading}
              error={automationTab === "schedule" ? scheduleError : monitorError}
              actionState={scheduleAction}
              connected={activeGateway?.status === "online"}
              onRefresh={() => {
                if (!activeConnection) return;
                if (automationTab === "monitor") void refreshMonitors(activeConnection.id);
                else void refreshSchedules(activeConnection.id);
              }}
              onAction={(action, job) => {
                if (action === "cancel") {
                  setScheduleError(null);
                  setScheduleDeleteTarget(job);
                  return;
                }
                void updateSchedule(action, job);
              }}
              onNew={(prompt) => {
                if (!activeConnection || !activeProject) return;
                openSession(activeConnection, activeProject);
                if (prompt) setComposer(prompt);
              }}
              onOpenSession={(project, session) => {
                if (!activeConnection) return;
                updateConnection(activeConnection.id, (connection) => ({
                  ...connection,
                  last_project_path: project.path,
                  last_project_id: project.id,
                }));
                openSession(activeConnection, project, session);
              }}
            />
          ) : workspaceView === "favorites" ? (
            <FavoritesView
              items={favoriteItems}
              entries={activeFavoriteEntries}
              connected={activeGateway?.status === "online"}
              onOpenProject={(project) => switchProject(project)}
              onOpenSession={(project, session) => {
                if (!activeConnection) return;
                updateConnection(activeConnection.id, (connection) => ({
                  ...connection,
                  last_project_path: project.path,
                  last_project_id: project.id,
                }));
                openSession(activeConnection, project, session);
              }}
              onCreateFolder={(parentId, name) => {
                updateFavorites((items) => addFavoriteEntry(items, parentId, {
                  id: crypto.randomUUID(),
                  type: "folder",
                  name,
                  children: [],
                }));
              }}
              onRenameFolder={(folderId, name) => updateFavorites((items) => renameFavoriteFolder(items, folderId, name))}
              onMove={(entryId, parentId) => updateFavorites((items) => moveFavoriteEntry(items, entryId, parentId))}
              onRemove={(entryId) => updateFavorites((items) => removeFavoriteEntries(items, (entry) => entry.id === entryId))}
              onDeleteFolder={(folderId, mode) => updateFavorites((items) => deleteFavoriteFolder(items, folderId, mode))}
            />
          ) : workspaceView === "plugins" ? (
            <PluginsView
              data={activePluginData}
              loading={pluginLoading}
              error={pluginError}
              connected={activeGateway?.status === "online"}
              onRefresh={() => activeConnection && void refreshPlugins(
                activeConnection.id,
                resourceSessionId,
                activeProject?.path ?? activeGateway?.workspace?.startup_cwd,
              )}
            />
          ) : !activeSession ? (
            <EmptyWorkspace
              connection={activeConnection}
              project={activeProject}
              gateway={activeGateway}
              onNew={() => activeConnection && activeProject && openSession(activeConnection, activeProject)}
              onConnect={() => setConnectionModal("new")}
            />
          ) : (
            <>
              <div className="conversation-header">
                <div className="conversation-title">
                  <h1 className={activeSession.loading ? "session-loading-title" : undefined}>
                    {activeSession.loading && <LoaderCircle className="spin" />}
                    {activeSession.loading ? "正在加载会话" : activeSession.title}
                  </h1>
                  <span>{activeSession.cwd}</span>
                  {activeForkOrigin && (
                    <small className="conversation-origin">来自“{activeForkOrigin}” · 分叉</small>
                  )}
                </div>
                <div className="conversation-view-tabs" role="tablist" aria-label="会话视图">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={activeConversationView === "chat"}
                    className={activeConversationView === "chat" ? "active" : ""}
                    onClick={() => activeSessionKey && setConversationViews((current) => ({ ...current, [activeSessionKey]: "chat" }))}
                  >对话</button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={activeConversationView === "trajectory"}
                    className={activeConversationView === "trajectory" ? "active" : ""}
                    onClick={() => activeSessionKey && setConversationViews((current) => ({ ...current, [activeSessionKey]: "trajectory" }))}
                  >轨迹</button>
                </div>
                <div className="conversation-actions">
                  <div className="conversation-font-size" aria-label="Agent 字号">
                    <button
                      className="icon-button tiny"
                      type="button"
                      title="缩小 Agent 字号"
                      aria-label="缩小 Agent 字号"
                      disabled={settings.ui_font_size <= 11}
                      onClick={() => commitSettings((current) => ({ ...current, ui_font_size: current.ui_font_size - 1 }))}
                    ><ZoomOut /></button>
                    <output>{settings.ui_font_size}px</output>
                    <button
                      className="icon-button tiny"
                      type="button"
                      title="放大 Agent 字号"
                      aria-label="放大 Agent 字号"
                      disabled={settings.ui_font_size >= 18}
                      onClick={() => commitSettings((current) => ({ ...current, ui_font_size: current.ui_font_size + 1 }))}
                    ><ZoomIn /></button>
                  </div>
                  <ConversationActionsMenu
                    key={activeSessionKey}
                    sessionTitle={activeSession.title}
                    favorite={activeSessionFavorite}
                    favoriteDisabled={activeSession.id.startsWith("new-")}
                    onCheckpoint={() => setCheckpointModal(true)}
                    onToggleFavorite={() => activeProject && toggleFavoriteSession(activeProject.id, activeSession.id)}
                  />
                  {documentMode && (
                    <button
                      className="icon-button"
                      type="button"
                      title="收起 Agent"
                      aria-label="收起 Agent"
                      onClick={() => updateDocumentAgentCollapsed(true)}
                    >
                      <PanelRightClose />
                    </button>
                  )}
                  {projectFilesEligible && (
                    <button
                      className={`icon-button ${projectFilesVisible ? "active" : ""}`}
                      type="button"
                      title={projectFilesVisible ? "关闭文件查看" : "浏览文件"}
                      aria-label={projectFilesVisible ? "关闭文件查看" : "浏览文件"}
                      aria-pressed={projectFilesVisible}
                      onClick={() => setProjectFilesOpen((value) => !value)}
                    >
                      {projectFilesVisible ? <PanelRightClose /> : <PanelRightOpen />}
                    </button>
                  )}
                </div>
              </div>
              {activeSession.loading ? (
                <div className="messages">
                  <div className="conversation-empty session-loading-state" role="status" aria-live="polite">
                    <LoaderCircle className="spin" />
                    <h2>正在加载会话</h2>
                    <p>正在恢复历史消息和会话状态…</p>
                  </div>
                </div>
              ) : activeConversationView === "trajectory" ? (
                <TrajectoryView items={activeSession.items} now={runClock} />
              ) : <div className="messages">
                {activeSession.items.length === 0 && (
                  <div className="conversation-empty">
                    <Bot />
                    <h2>在 {activeProject?.name ?? "项目"} 中构建什么？</h2>
                  </div>
                )}
                {activeSession.items.map((item) => (
                  <ChatItemView
                    key={item.id}
                    item={item}
                    now={runClock}
                    showTurnDuration={settings.show_turn_duration}
                    turnDurationFormat={settings.turn_duration_format}
                    onPermission={resolvePermission}
                    onToggleChoice={toggleChoice}
                    onSubmitChoice={submitChoice}
                    onPlan={(action) => activeChannel?.planAction(
                      action,
                      item.detail && typeof item.detail === "object" ? item.detail as Record<string, unknown> : undefined,
                    )}
                    onFork={forkSessionFromMessage}
                    forkDisabled={activeSession.busy}
                    onCompatibilityRetry={item.kind === "document_job" && item.engine === "precise" && item.status === "failed"
                      ? () => startDocumentAction("translate", {
                          locale: item.locale,
                          translation_concurrency: settings.document_translation_concurrency,
                          translation_batch_size: settings.document_translation_batch_size,
                          translation_engine: "legacy",
                        })
                      : undefined}
                  />
                ))}
                {activeSession.busy && (
                  <ExecutionStatusBar
                    startedAt={activeSession.runStartedAt ?? runClock}
                    currentStep={activeSession.currentStep ?? null}
                    now={runClock}
                  />
                )}
                <div ref={messageEndRef} />
              </div>}
              <div className="composer-wrap">
                <div className="composer-context">
                  <span><Folder />{activeProject?.name}</span>
                  <span><Server />{activeConnection?.name}</span>
                  {!activeSession.loading && !activeSession.connected && (
                    <span
                      className="danger connection-state"
                      title={activeSession.error || "会话连接已断开，Crab Desktop 正在重连"}
                    >
                      <WifiOff />
                      {activeSession.error || "连接中断，正在重连"}
                    </span>
                  )}
                </div>
                <div className={`composer-box ${activeSession.status?.ultra_mode ? "ultra-mode" : ""}`}>
                  {(pendingImages.length > 0 || pendingFiles.length > 0 || pendingFolders.length > 0 || pendingDocumentReferences.length > 0) && (
                    <div className="attachment-strip">
                      {pendingImages.map((image) => (
                        <div className="attachment-thumb" key={image.id} title={image.name}>
                          <img src={image.dataUrl} alt={image.name} />
                          <button
                            className="icon-button tiny"
                            title="移除图片"
                            onClick={() => setPendingImages((current) => current.filter((item) => item.id !== image.id))}
                          ><X /></button>
                        </div>
                      ))}
                      {pendingFiles.map((file) => (
                        <FileAttachment
                          key={file.id}
                          file={file}
                          onRemove={() => setPendingFiles((current) => current.filter((item) => item.id !== file.id))}
                        />
                      ))}
                      {pendingFolders.map((path) => (
                        <div className="folder-attachment" key={path} title={path}>
                          <Folder />
                          <span>{basename(path)}</span>
                          <button
                            type="button"
                            title="移除文件夹引用"
                            onClick={() => setPendingFolders((current) => current.filter((item) => item !== path))}
                          ><X /></button>
                        </div>
                      ))}
                      {pendingDocumentReferences.map((reference) => (
                        <DocumentReferenceAttachment
                          key={reference.id}
                          reference={reference}
                          onRemove={() => setPendingDocumentReferences((current) => current.filter((item) => item.id !== reference.id))}
                        />
                      ))}
                    </div>
                  )}
                  <ComposerEditor
                    value={composer}
                    references={composerReferences}
                    commands={composerCommands}
                    sendKey={settings.composer_send_key}
                    onChange={setComposer}
                    onImages={(files) => void addImages(files)}
                    onSubmit={() => void sendMessage()}
                    placeholder={activeSession.loading ? "会话加载完成后即可输入" : activeSession.busy ? "输入内容以引导当前任务" : "输入任务"}
                  />
                  <div className="composer-toolbar">
                    <div className="toolbar-left">
                      <ComposerAddMenu
                        disabled={activeSession.loading || !activeSession.connected}
                        planActive={activeSession.status?.mode === "plan"}
                        ultraActive={Boolean(activeSession.status?.ultra_mode)}
                        onImages={(files) => void addImages(files)}
                        onFiles={(files) => void addFiles(files)}
                        fileUploadMode={settings.file_upload_mode}
                        onFilePaths={() => setReferencePathModal("file")}
                        onReferencePath={() => setReferencePathModal("all")}
                        onGoal={() => setGoalModal(true)}
                        onPlan={() => selectMode("plan")}
                        onUltra={() => selectUltraMode(true)}
                      />
                      <ModelPicker
                        models={activeGateway?.models ?? []}
                        value={activeModel}
                        fallback={activeSession.status?.model || "默认模型"}
                        disabled={activeSession.loading || !activeSession.connected}
                        onChange={selectModel}
                      />
                      <ReasoningEffortPicker
                        value={activeSession.status?.reasoning_effort}
                        disabled={activeSession.loading || !activeSession.connected}
                        onChange={selectReasoningEffort}
                      />
                      <PermissionPicker
                        value={activePermissionMode}
                        disabled={activeSession.loading || !activeSession.connected}
                        onChange={selectPermissionMode}
                      />
                      {activeSession.status?.mode === "plan" && (
                        <button
                          type="button"
                          className="composer-mode-chip plan"
                          title="关闭计划模式"
                          onClick={() => selectMode("agent")}
                        >
                          <ListTodo /><span>计划模式</span><X />
                        </button>
                      )}
                      {activeSession.status?.ultra_mode && (
                        <button
                          type="button"
                          className="composer-mode-chip ultra"
                          title="关闭 Ultra 模式"
                          onClick={() => selectUltraMode(false)}
                        >
                          <Sparkles /><span>Ultra 模式</span><X />
                        </button>
                      )}
                    </div>
                    <div className="toolbar-right">
                      <ContextMeter status={activeSession.status} usage={activeSession.lastTurnUsage} />
                      {activeSession.busy ? (
                        <button
                          className="round-action stop"
                          title="中断任务"
                          onClick={() => activeChannel?.interrupt(activeSession.operationId)}
                        >
                          <Square />
                        </button>
                      ) : (
                        <button
                          className="round-action send"
                          title={settings.composer_send_key === "mod_enter" ? `发送 (${composerModifierLabel()})` : "发送 (Enter)"}
                          disabled={(
                            !composer.trim()
                            && pendingImages.length === 0
                            && pendingFiles.length === 0
                            && pendingFolders.length === 0
                            && pendingDocumentReferences.length === 0
                          ) || activeSession.loading || !activeSession.connected}
                          onClick={() => void sendMessage()}
                        >
                          <Send />
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            </>
          )}
          {projectFilesEligible && !activeSession && !projectFilesVisible && (
            <button
              className="project-files-floating-toggle"
              type="button"
              title="浏览文件"
              onClick={() => setProjectFilesOpen(true)}
            ><PanelRightOpen /><span>文件</span></button>
          )}
          {projectFilesDrawerVisible && (
            <button
              className="project-files-drawer-backdrop"
              type="button"
              aria-label="关闭文件工作区"
              onClick={() => setProjectFilesOpen(false)}
            />
          )}
          {projectFilesVisible && activeConnection && activeProject && apiRef.current.get(activeConnection.id) && (
            <ProjectFilesWorkspace
              key={`${activeConnection.id}:${activeProject.id}`}
              api={apiRef.current.get(activeConnection.id)!}
              projectName={activeProject.name}
              directories={activeProject.directories}
              drawer={projectFilesDrawerVisible}
              treeOpen={projectFileTreeOpen}
              width={projectFilesWidth}
              openFiles={projectFileTabs.files}
              selectedFile={selectedProjectFile}
              referencedPaths={referencedProjectFilePaths}
              onClose={() => setProjectFilesOpen(false)}
              onToggleTree={() => setProjectFileTreeOpen((value) => !value)}
              onSelectFile={(file) => setProjectFileTabs((current) => (
                activateProjectFileTab(current, file, settings.project_files_max_tabs)
              ))}
              onCloseFile={(path) => setProjectFileTabs((current) => closeProjectFileTab(current, path))}
              onReference={(file) => setPendingFiles((current) => (
                current.some((item) => item.mode === "path" && item.path
                  && projectPathKey(item.path) === projectPathKey(file.path))
                  ? current
                  : [...current, {
                    id: crypto.randomUUID(),
                    name: file.name,
                    mediaType: "",
                    mode: "path",
                    path: file.path,
                    size: file.size,
                    text: "",
                  }]
              ))}
              onWidthChange={setProjectFilesWidth}
              onWidthCommit={(width) => commitSettings((current) => ({ ...current, project_files_width: width }))}
            />
          )}
        </main>
      </div>
      </>
      )}

      {connectionModal && (
        <ConnectionModal
          settings={settings}
          activeConnectionId={activeConnection?.id ?? ""}
          initialEditingId={connectionModal === "new" ? null : connectionModal}
          onClose={() => setConnectionModal(null)}
          onActivate={(id) => {
            switchConnection(id);
            setConnectionModal(null);
          }}
          onSave={async (connection, password) => {
            if (isInsecureRemoteUrl(connection.base_url) && !connection.allow_insecure_remote) {
              throw new Error("远程 HTTP 会明文传输密码，请确认不安全连接后再保存");
            }
            connection.base_url = normalizeBaseUrl(connection.base_url).replace(/\/$/, "");
            if (password) {
              connection.credential_ref = connection.credential_ref ?? `gateway-${connection.id}`;
              await storeCredential(connection.credential_ref, password);
            }
            commitSettings((current) => {
              const exists = current.connections.some((item) => item.id === connection.id);
              return {
                ...current,
                active_connection_id: connection.id,
                connections: exists
                  ? current.connections.map((item) => item.id === connection.id ? connection : item)
                  : [...current.connections, connection],
                connection_order: exists
                  ? current.connection_order
                  : [...current.connection_order, connection.id],
              };
            });
            connectedRef.current.add(connection.id);
            await connectGateway(connection, settings.python_path);
            setConnectionModal(null);
          }}
        />
      )}

      {projectTypeModal && (
        <ProjectTypeModal
          capabilities={documentCapabilities}
          onClose={() => setProjectTypeModal(false)}
          onSelect={(kind) => {
            setProjectTypeModal(false);
            if (kind === "document") setDocumentProjectModal(true);
            else setProjectModal("new");
          }}
        />
      )}

      {documentProjectModal && activeConnection && activeGateway?.workspace && apiRef.current.get(activeConnection.id) && (
        <DocumentProjectModal
          api={apiRef.current.get(activeConnection.id)!}
          capabilities={documentCapabilities}
          defaultRoot={activeConnection.document_workspace_root
            || activeGateway.workspace.documents_dir
            || defaultProjectDirectory(defaultProjectDirectory(activeGateway.workspace.home, "Documents"), "CrabCode")}
          onClose={() => setDocumentProjectModal(false)}
          onSave={(project) => {
            updateConnection(activeConnection.id, (connection) => ({
              ...connection,
              projects: [...connection.projects, project],
              last_project_path: project.path,
              last_project_id: project.id,
            }));
            setWorkspaceView("chat");
            setSearch("");
            void refreshProjectSessions(activeConnection.id, project.path);
            openSession(activeConnection, project);
            setDocumentProjectModal(false);
          }}
        />
      )}

      {projectModal && activeConnection && activeGateway?.workspace && (
        <ProjectModal
          api={apiRef.current.get(activeConnection.id)!}
          home={activeGateway.workspace.home}
          roots={activeGateway.workspace.browse_roots}
          project={projectModal === "new" ? null : projectModal}
          projects={activeConnection.projects}
          protectPrimaryDirectory={projectModal !== "new" && projectModal.id === defaultProjectId}
          onClose={() => setProjectModal(null)}
          onSave={(project) => {
            const previousProject = projectModal === "new" ? null : projectModal;
            if (previousProject && previousProject.id === activeProject?.id
              && projectPathKey(previousProject.path) !== projectPathKey(project.path)) {
              setActiveSessions((current) => ({ ...current, [activeConnection.id]: null }));
            }
            updateConnection(activeConnection.id, (connection) => {
              const changedPrimaryPath = Boolean(previousProject
                && projectPathKey(previousProject.path) !== projectPathKey(project.path));
              const favoriteItems = changedPrimaryPath
                ? removeFavoriteEntries(favoriteEntries(connection), (entry) => (
                  entry.type === "session" && entry.project_id === project.id
                ))
                : favoriteEntries(connection);
              return withFavoriteItems({
                ...connection,
                projects: connection.projects.some((item) => item.id === project.id)
                  ? connection.projects.map((item) => item.id === project.id ? project : item)
                  : [...connection.projects, project],
                last_project_path: project.path,
                last_project_id: project.id,
              }, favoriteItems);
            });
            void refreshProjectSessions(activeConnection.id, project.path);
            setProjectModal(null);
          }}
          onRemove={projectModal === "new" || projectModal.id === defaultProjectId ? undefined : () => {
            const removing = projectModal;
            setProjectModal(null);
            setProjectDeleteTarget(removing);
          }}
        />
      )}

      {referencePathModal && activeConnection && activeGateway?.workspace && (
        <DirectoryModal
          api={apiRef.current.get(activeConnection.id)!}
          home={activeProject?.path || activeGateway.workspace.home}
          roots={activeGateway.workspace.browse_roots}
          title={referencePathModal === "file" ? "引用文件路径" : "引用文件或文件夹"}
          selectLabel="引用此文件夹"
          allowFiles
          allowDirectorySelection={referencePathModal === "all"}
          onClose={() => setReferencePathModal(null)}
          onSelect={(path, kind) => {
            if (kind === "folder") {
              setPendingFolders((current) => current.includes(path) ? current : [...current, path]);
            } else {
              setPendingFiles((current) => current.some((file) => file.mode === "path" && file.path
                && projectPathKey(file.path) === projectPathKey(path))
                ? current
                : [...current, {
                  id: crypto.randomUUID(),
                  name: basename(path),
                  mediaType: "",
                  mode: "path",
                  path,
                  size: null,
                  text: "",
                }]);
            }
            setReferencePathModal(null);
          }}
        />
      )}

      {goalModal && activeConnection && activeChannel?.sessionId && (
        <GoalModal
          api={apiRef.current.get(activeConnection.id)!}
          sessionId={activeChannel.sessionId}
          onClose={() => setGoalModal(false)}
        />
      )}

      {checkpointModal && activeConnection && activeSession && (
        <CheckpointModal
          api={apiRef.current.get(activeConnection.id)!}
          sessionId={activeSession.id}
          onClose={() => setCheckpointModal(false)}
          onRestored={() => {
            setCheckpointModal(false);
            channelRef.current.get(activeSessionKey!)?.dispose();
            channelRef.current.delete(activeSessionKey!);
            setSessions((current) => ({
              ...current,
              [activeSessionKey!]: { ...current[activeSessionKey!], items: [], connected: false },
            }));
            const info = activeList.find((item) => item.session_id === activeSession.id);
            if (activeProject && info) openSession(activeConnection, activeProject, info);
          }}
        />
      )}

      {scheduleDeleteTarget && (
        <ScheduleDeleteModal
          job={scheduleDeleteTarget}
          busy={scheduleAction?.id === scheduleDeleteTarget.id && scheduleAction.action === "cancel"}
          error={scheduleError}
          onClose={() => {
            if (scheduleAction?.id !== scheduleDeleteTarget.id) setScheduleDeleteTarget(null);
          }}
          onConfirm={() => void confirmScheduleDelete()}
        />
      )}

      {projectDeleteTarget && (
        <ProjectDeleteModal
          project={projectDeleteTarget}
          onClose={() => setProjectDeleteTarget(null)}
          onConfirm={() => {
            if (removeProject(projectDeleteTarget)) setProjectDeleteTarget(null);
          }}
        />
      )}

      {globalError && (
        <div className="toast error-toast" role="alert">
          <AlertTriangle />
          <span>{globalError}</span>
          <button className="icon-button tiny" onClick={() => setGlobalError(null)}><X /></button>
        </div>
      )}
    </div>
  );
}

function ConnectionDot({ status }: { status: GatewayViewState["status"] }) {
  return status === "connecting"
    ? <LoaderCircle className="connection-dot spin" />
    : <Circle className={`connection-dot ${status}`} fill="currentColor" />;
}

export function ScheduledTasksView({
  tab,
  onTabChange,
  jobs,
  tasks,
  projects,
  sessionsByProject,
  loading,
  error,
  actionState,
  connected,
  onRefresh,
  onAction,
  onNew,
  onOpenSession,
}: {
  tab: AutomationTab;
  onTabChange: (tab: AutomationTab) => void;
  jobs: ScheduleJobInfo[];
  tasks: BackgroundTaskInfo[];
  projects: ProjectPreset[];
  sessionsByProject: Record<string, SessionInfo[]>;
  loading: boolean;
  error: string | null;
  actionState: ScheduleActionState | null;
  connected: boolean;
  onRefresh: () => void;
  onAction: (action: ScheduleAction, job: ScheduleJobInfo) => void;
  onNew: (prompt?: string) => void;
  onOpenSession: (project: ProjectPreset, session: SessionInfo) => void;
}) {
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const filteredJobs = jobs.filter((job) => {
    return !needle || [job.name, job.description, job.prompt, job.schedule]
      .some((value) => value.toLowerCase().includes(needle));
  });
  const monitorContexts = tasks
    .filter((task) => (
      task.status === "running"
      && task.task_type !== "local_agent"
      && task.source !== "agent"
      && !task.agent_id
    ))
    .map((task) => {
      const taskCwd = task.cwd;
      const project = (typeof taskCwd === "string" ? projects.find(
        (item) => projectPathKey(item.path) === projectPathKey(taskCwd),
      ) : undefined)
        ?? projects.find((item) => (
          sessionsForProject(sessionsByProject, item.path)
            .some((session) => session.session_id === task.session_id)
        ))
        ?? null;
      const session = (project ? sessionsForProject(sessionsByProject, project.path) : undefined)
        ?.find((item) => item.session_id === task.session_id)
        ?? Object.values(sessionsByProject).flat()
          .find((item) => item.session_id === task.session_id)
        ?? null;
      return { task, project, session };
    });
  const filteredMonitors = monitorContexts.filter(({ task, project, session }) => {
    return !needle || [
      task.description,
      task.source,
      task.task_id,
      project?.name ?? "",
      project?.path ?? task.cwd ?? "",
      session?.title ?? "",
      task.session_id,
    ]
      .some((value) => value.toLowerCase().includes(needle));
  });
  const runningCount = jobs.filter((job) => job.running).length;
  const pausedCount = jobs.filter((job) => job.status === "paused" || job.status === "disabled").length;
  const nextRun = jobs
    .filter((job) => job.enabled && job.next_run)
    .sort((left, right) => String(left.next_run).localeCompare(String(right.next_run)))[0]?.next_run ?? null;
  const monitorSessionCount = new Set(monitorContexts.map(({ task }) => task.session_id)).size;
  const monitorProjectCount = new Set(monitorContexts.map(({ task, project }) => project?.id ?? task.cwd ?? task.session_id)).size;
  const summaryCards = tab === "schedule"
    ? [
      { label: "正在执行", value: String(runningCount), icon: Activity, tone: "coral" },
      { label: "暂停待命", value: String(pausedCount), icon: Timer, tone: "amber" },
      { label: "最近触发", value: nextRun ? formatDateTime(nextRun) : "尚未安排", icon: Gauge, tone: "cyan" },
    ] as const
    : [
      { label: "运行中 Monitor", value: String(monitorContexts.length), icon: Activity, tone: "coral" },
      { label: "活跃会话", value: String(monitorSessionCount), icon: History, tone: "cyan" },
      { label: "涉及项目", value: String(monitorProjectCount), icon: Folder, tone: "amber" },
    ] as const;
  const suggestions = [
    {
      title: "晨间代码巡检",
      meta: "工作日 · 08:30",
      description: "扫描工作区变更、测试结果与待处理分支，生成一页晨间信号",
      icon: Code2,
      tone: "coral",
    },
    {
      title: "版本周报",
      meta: "周五 · 16:00",
      description: "汇总本周提交、合并请求和发布风险，输出可交付的状态摘要",
      icon: GitBranch,
      tone: "cyan",
    },
    {
      title: "依赖脉冲",
      meta: "工作日 · 09:00",
      description: "检查依赖更新与安全告警，只把需要决策的变化推到会话中",
      icon: Zap,
      tone: "amber",
    },
  ] as const;

  return (
    <section className="workspace-page scheduled-page">
      <div className="page-kicker"><Workflow /><span>CRAB DESKTOP AUTOMATION DECK</span><i /></div>
      <header className="page-header plugins-header automation-header">
        <div className="plugin-tabs automation-tabs" role="tablist" aria-label="自动化类型">
          <button type="button" role="tab" aria-selected={tab === "schedule"} className={tab === "schedule" ? "active" : ""} onClick={() => onTabChange("schedule")}><Workflow />Schedule</button>
          <button type="button" role="tab" aria-selected={tab === "monitor"} className={tab === "monitor" ? "active" : ""} onClick={() => onTabChange("monitor")}><Activity />Monitor</button>
        </div>
        <span className="capability-context automation-context"><CircleDotDashed />{connected ? "自动化已连接" : "等待 Gateway"}</span>
        <button className="icon-button page-refresh" title={tab === "schedule" ? "刷新已安排任务" : "刷新 Monitor"} onClick={onRefresh} disabled={loading || !connected}>
          <RefreshCw className={loading ? "spin" : ""} />
        </button>
      </header>
      <div className="page-intro automation-intro">
        <h1>自动化甲板</h1>
        <p>{tab === "schedule" ? "让 Crab Desktop 按计划运行检查、整理和交付流程" : "查看所有项目中正在执行的 Monitor 及其会话归属"}</p>
      </div>
      <div className="automation-summary">
        {summaryCards.map(({ label, value, icon: Icon, tone }) => (
          <div className={`automation-stat ${tone}`} key={label}>
            <span className="automation-stat-icon"><Icon /></span>
            <span className="automation-stat-copy"><small>{label}</small><strong>{value}</strong></span>
          </div>
        ))}
      </div>
      <label className="page-search">
        <Search />
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={tab === "schedule" ? "检索流程、触发器或工作区" : "检索 Monitor、会话或项目"} />
      </label>
      {!connected && <PageNotice icon={<WifiOff />} text={tab === "schedule" ? "Gateway 尚未连接，无法读取已安排任务" : "Gateway 尚未连接，无法读取 Monitor"} />}
      {error && <PageNotice icon={<AlertTriangle />} text={error} error />}
      {tab === "monitor" ? (
        <div className="page-section monitor-section">
          <div className="page-section-heading"><h2>运行中的 Monitor</h2><span>{filteredMonitors.length} 个任务</span></div>
          {filteredMonitors.length > 0 ? (
            <div className="monitor-list">
              {filteredMonitors.map(({ task, project, session }) => {
                const startedAt = task.started_at ? new Date(task.started_at).getTime() : Number.NaN;
                const elapsed = Number.isNaN(startedAt) ? "刚刚启动" : `已运行 ${formatElapsed(Date.now() - startedAt)}`;
                const source = task.source === "websocket" ? "WebSocket" : "命令";
                const projectName = project?.name || basename(task.cwd || "") || "未知项目";
                const sessionTitle = session?.title || `会话 ${task.session_id.slice(0, 8)}`;
                return (
                  <article className="monitor-item" key={`${task.session_id}:${task.task_id}`}>
                    <div className="monitor-icon"><Activity /></div>
                    <div className="monitor-copy">
                      <div className="scheduled-title-row">
                        <strong>{task.description || `${source} Monitor`}</strong>
                        <span className="schedule-type">{source} · {task.task_id.slice(0, 8)}</span>
                      </div>
                      <div className="monitor-location">
                        <span title={project?.path ?? task.cwd}><Folder />{projectName}</span>
                        <i>/</i>
                        <span title={task.session_id}><History />{sessionTitle}</span>
                      </div>
                      <div className="scheduled-meta">
                        <small className="schedule-status live"><span />执行中</small>
                        <small>{elapsed}</small>
                      </div>
                    </div>
                    {project && session && (
                      <button className="icon-button monitor-open" type="button" title="打开所属会话" aria-label={`打开会话 ${sessionTitle}`} onClick={() => onOpenSession(project, session)}><ArrowUpRight /></button>
                    )}
                  </article>
                );
              })}
            </div>
          ) : loading ? (
            <div className="page-loading"><LoaderCircle className="spin" />正在读取 Monitor</div>
          ) : (
            <div className="monitor-empty">
              <span><Activity /></span>
              <strong>{query.trim() ? "没有匹配的 Monitor" : "当前没有运行中的 Monitor"}</strong>
              <small>{query.trim() ? "试试搜索任务描述、会话标题或项目名" : "会话启动命令或 WebSocket 监控后，会自动出现在这里"}</small>
            </div>
          )}
        </div>
      ) : jobs.length > 0 ? (
        <div className="page-section">
          <div className="page-section-heading"><h2>运行队列</h2><span>{filteredJobs.length} 个流程</span></div>
          <div className="scheduled-list">
            {filteredJobs.map((job) => {
              const action = actionState?.id === job.id ? actionState.action : null;
              const busy = action !== null || job.running;
              const paused = job.status === "paused" || job.status === "disabled";
              const retryable = job.status === "error";
              const terminal = job.status === "completed";
              const statusTone = job.running ? "live" : paused ? "paused" : job.status === "error" ? "error" : job.status === "completed" ? "complete" : "live";
              const statusText = job.running ? "执行中" : paused ? "待命" : job.status === "error" ? "运行异常" : job.status === "completed" ? "已完成" : "等待触发";
              return (
                <article className="scheduled-item" key={job.id}>
                  <div className={`scheduled-icon ${statusTone}`}><Workflow /></div>
                  <div className="scheduled-copy">
                    <div className="scheduled-title-row">
                      <strong>{job.name}</strong>
                      <span className="schedule-type">{job.schedule_type} · {job.schedule}</span>
                    </div>
                    <p>{job.description || job.prompt}</p>
                    <div className="scheduled-meta">
                      <small className={`schedule-status ${statusTone}`}><span />{statusText}</small>
                      <small>{scheduleSummary(job)}{job.run_count > 0 ? ` · 已运行 ${job.run_count} 次` : ""}</small>
                      {job.cwd && <small className="schedule-scope" title={job.cwd}><Folder />{basename(job.cwd)}</small>}
                    </div>
                  </div>
                  <div className="scheduled-actions">
                    <button className="icon-button tiny" title={paused ? "恢复任务" : "暂停任务"} disabled={busy || terminal} onClick={() => onAction(paused ? "resume" : "pause", job)}>
                      {action === "pause" || action === "resume" ? <LoaderCircle className="spin" /> : paused ? <Play /> : <Pause />}
                    </button>
                    <button className="icon-button tiny" title={retryable ? "重新运行" : "立即运行"} disabled={busy || paused || terminal} onClick={() => onAction("trigger", job)}>
                      {action === "trigger" || job.running ? <LoaderCircle className="spin" /> : <Play />}
                    </button>
                    <button className="icon-button tiny danger-button" title="永久删除任务" disabled={action !== null} onClick={() => onAction("cancel", job)}>
                      {action === "cancel" ? <LoaderCircle className="spin" /> : <Trash2 />}
                    </button>
                  </div>
                </article>
              );
            })}
            {filteredJobs.length === 0 && <div className="page-empty">没有匹配的已安排任务</div>}
          </div>
        </div>
      ) : (
        <div className="page-section">
          <div className="page-section-heading"><h2>启动一个流程</h2><span>从工作区信号开始</span></div>
          <div className="suggestion-list">
            {suggestions.map(({ title, meta, description, icon: Icon, tone }) => (
              <button
                className="suggestion-item"
                key={title}
                onClick={() => onNew(`请创建“${title}”已安排任务：${description}。${meta}。`)}
                disabled={!connected}
              >
                <span className={`suggestion-icon ${tone}`}><Icon /></span>
                <span className="suggestion-copy"><strong>{title}</strong><span>{meta}</span><small>{description}</small></span>
              </button>
            ))}
          </div>
          <button className="page-command" onClick={() => onNew()} disabled={!connected}><MessageSquarePlus />新建任务</button>
        </div>
      )}
    </section>
  );
}

export function FavoritesView({
  items,
  entries,
  connected,
  onOpenProject,
  onOpenSession,
  onCreateFolder,
  onRenameFolder,
  onMove,
  onRemove,
  onDeleteFolder,
}: {
  items: FavoriteViewEntry[];
  entries: FavoriteEntry[];
  connected: boolean;
  onOpenProject: (project: ProjectPreset) => void;
  onOpenSession: (project: ProjectPreset, session: SessionInfo) => void;
  onCreateFolder: (parentId: string | null, name: string) => void;
  onRenameFolder: (folderId: string, name: string) => void;
  onMove: (entryId: string, parentId: string | null) => void;
  onRemove: (entryId: string) => void;
  onDeleteFolder: (folderId: string, mode: FavoriteFolderDeleteMode) => void;
}) {
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [folderEditor, setFolderEditor] = useState<{
    folderId?: string;
    parentId: string | null;
    name: string;
  } | null>(null);
  const [folderDeleteTarget, setFolderDeleteTarget] = useState<FavoriteFolder | null>(null);
  const needle = query.trim().toLowerCase();

  const filterItems = (source: FavoriteViewEntry[]): FavoriteViewEntry[] => source.flatMap((item) => {
    if (!needle) return [item];
    if (item.kind === "folder") {
      if (item.entry.name.toLowerCase().includes(needle)) return [item];
      const children = filterItems(item.children);
      return children.length ? [{ ...item, children }] : [];
    }
    if (item.kind === "project") {
      return [item.project.name, item.project.path].some((value) => value.toLowerCase().includes(needle))
        ? [item]
        : [];
    }
    return [item.project.name, item.project.path, item.session.title, item.session.preview]
      .some((value) => value.toLowerCase().includes(needle)) ? [item] : [];
  });
  const filtered = filterItems(items);

  const locationSelect = (entryId: string) => (
    <select
      className="favorite-location-select"
      aria-label="移动到收藏文件夹"
      title="移动到收藏文件夹"
      value={favoriteParentId(entries, entryId) ?? ""}
      onChange={(event) => onMove(entryId, event.target.value || null)}
    >
      {favoriteFolderOptions(entries, entryId).map((folder) => (
        <option key={folder.id ?? "root"} value={folder.id ?? ""}>
          {`${"　".repeat(folder.depth)}${folder.name}`}
        </option>
      ))}
    </select>
  );

  const renderItems = (source: FavoriteViewEntry[]) => source.map((item) => {
    if (item.kind === "folder") {
      const isCollapsed = !needle && collapsed.has(item.entry.id);
      return (
        <section className="favorite-folder" key={item.entry.id}>
          <div className="favorite-folder-heading">
            <button
              className="favorite-folder-toggle"
              type="button"
              aria-expanded={!isCollapsed}
              onClick={() => setCollapsed((current) => {
                const next = new Set(current);
                if (next.has(item.entry.id)) next.delete(item.entry.id);
                else next.add(item.entry.id);
                return next;
              })}
            >
              <ChevronDown className={isCollapsed ? "collapsed" : ""} />
              {isCollapsed ? <Folder /> : <FolderOpen />}
              <strong>{item.entry.name}</strong>
              <small>{countFavoriteItems(item.entry.children)} 项</small>
            </button>
            <div className="favorite-folder-actions">
              {locationSelect(item.entry.id)}
              <button
                className="icon-button tiny"
                type="button"
                title="新建子文件夹"
                aria-label={`在 ${item.entry.name} 中新建文件夹`}
                onClick={() => setFolderEditor({ parentId: item.entry.id, name: "" })}
              ><FolderPlus /></button>
              <button
                className="icon-button tiny"
                type="button"
                title="重命名文件夹"
                aria-label={`重命名文件夹 ${item.entry.name}`}
                onClick={() => setFolderEditor({ folderId: item.entry.id, parentId: null, name: item.entry.name })}
              ><Pencil /></button>
              <button
                className="icon-button tiny danger-button"
                type="button"
                title="删除文件夹"
                aria-label={`删除文件夹 ${item.entry.name}`}
                onClick={() => setFolderDeleteTarget(item.entry)}
              ><Trash2 /></button>
            </div>
          </div>
          {!isCollapsed && <div className="favorite-folder-children">{renderItems(item.children)}</div>}
        </section>
      );
    }

    if (item.kind === "project") {
      return (
        <article className="favorite-session-item favorite-project-item" key={item.entry.id}>
          <button className="favorite-session-main" type="button" onClick={() => onOpenProject(item.project)}>
            <span className="favorite-session-icon project"><FolderOpen /></span>
            <span className="favorite-session-copy">
              <strong>{item.project.name}</strong>
              <span>{projectDirectoryTitle(item.project)}</span>
              <small><Star fill="currentColor" />收藏项目</small>
            </span>
            <ChevronRight />
          </button>
          <div className="favorite-item-actions">
            {locationSelect(item.entry.id)}
            <button
              className="icon-button"
              type="button"
              title="取消收藏项目"
              aria-label={`取消收藏项目 ${item.project.name}`}
              onClick={() => onRemove(item.entry.id)}
            ><Star fill="currentColor" /></button>
          </div>
        </article>
      );
    }

    return (
      <article className="favorite-session-item" key={item.entry.id}>
        <button className="favorite-session-main" type="button" onClick={() => onOpenSession(item.project, item.session)}>
          <span className="favorite-session-icon"><Star fill="currentColor" /></span>
          <span className="favorite-session-copy">
            <strong>{item.session.title || "未命名会话"}</strong>
            <span>{item.session.preview || formatDate(item.session.created_at)}</span>
            <small title={item.project.path}><Folder />{item.project.name}<i>{item.project.path}</i></small>
          </span>
          <ChevronRight />
        </button>
        <div className="favorite-item-actions">
          {locationSelect(item.entry.id)}
          <button
            className="icon-button"
            type="button"
            title="取消收藏会话"
            aria-label={`取消收藏 ${item.session.title || "未命名会话"}`}
            onClick={() => onRemove(item.entry.id)}
          ><Star fill="currentColor" /></button>
        </div>
      </article>
    );
  });

  return (
    <section className="workspace-page favorites-page">
      <div className="page-kicker"><Star /><span>CRAB DESKTOP FAVORITES</span><i /></div>
      <header className="page-header favorites-header">
        <div>
          <h1>收藏</h1>
          <p>用分级文件夹整理重要项目和会话</p>
        </div>
        <button
          className="page-command favorites-new-folder"
          type="button"
          onClick={() => setFolderEditor({ parentId: null, name: "" })}
        ><FolderPlus />新建文件夹</button>
      </header>
      <label className="page-search">
        <Search />
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="检索收藏、项目或文件夹" />
      </label>
      {!connected && <PageNotice icon={<WifiOff />} text="Gateway 尚未连接，无法读取收藏内容" />}
      <div className="page-section favorites-section">
        <div className="page-section-heading"><h2>全部收藏</h2><span>{countFavoriteItems(entries)} 项</span></div>
        <div className="favorite-session-list favorite-tree">
          {renderItems(filtered)}
          {connected && filtered.length === 0 && (
            <div className="page-empty">{needle ? "没有匹配的收藏" : "暂无收藏项目或会话"}</div>
          )}
        </div>
      </div>
      {folderEditor && (
        <Modal title={folderEditor.folderId ? "重命名收藏文件夹" : "新建收藏文件夹"} onClose={() => setFolderEditor(null)}>
          <form
            className="form-grid favorite-folder-form"
            onSubmit={(event) => {
              event.preventDefault();
              const name = folderEditor.name.trim();
              if (!name) return;
              if (folderEditor.folderId) onRenameFolder(folderEditor.folderId, name);
              else onCreateFolder(folderEditor.parentId, name);
              setFolderEditor(null);
            }}
          >
            <label>
              <span>文件夹名称</span>
              <input
                autoFocus
                value={folderEditor.name}
                onChange={(event) => setFolderEditor({ ...folderEditor, name: event.target.value })}
                placeholder="例如：客户项目"
              />
            </label>
            <div className="modal-actions">
              <button type="button" onClick={() => setFolderEditor(null)}>取消</button>
              <button className="primary" type="submit" disabled={!folderEditor.name.trim()}>
                {folderEditor.folderId ? <Pencil /> : <FolderPlus />}{folderEditor.folderId ? "保存" : "创建"}
              </button>
            </div>
          </form>
        </Modal>
      )}
      {folderDeleteTarget && (
        <FavoriteFolderDeleteModal
          folder={folderDeleteTarget}
          hasParent={favoriteParentId(entries, folderDeleteTarget.id) !== null}
          onClose={() => setFolderDeleteTarget(null)}
          onChoose={(mode) => {
            onDeleteFolder(folderDeleteTarget.id, mode);
            setFolderDeleteTarget(null);
          }}
        />
      )}
    </section>
  );
}

export function FavoriteFolderDeleteModal({ folder, hasParent, onClose, onChoose }: {
  folder: FavoriteFolder;
  hasParent: boolean;
  onClose: () => void;
  onChoose: (mode: FavoriteFolderDeleteMode) => void;
}) {
  const itemCount = countFavoriteItems(folder.children);
  const empty = folder.children.length === 0;
  return (
    <Modal title="删除收藏文件夹" onClose={onClose}>
      <div className="confirm-dialog-copy favorite-folder-delete-copy">
        <FolderOpen />
        <div>
          <strong>删除“{folder.name}”？</strong>
          <p>{empty ? "这个文件夹是空的。" : `其中有 ${itemCount} 项收藏及可能的子文件夹，请选择如何处理。`}</p>
        </div>
      </div>
      {!empty && (
        <div className="favorite-folder-delete-options">
          <button type="button" className="recommended" onClick={() => onChoose("promote")}>
            <FolderInput />
            <span>
              <strong>{hasParent ? "移到上一级并删除文件夹" : "移到收藏根目录并删除文件夹"}</strong>
              <small>保留文件夹内的项目、会话和子文件夹</small>
            </span>
          </button>
          {hasParent && (
            <button type="button" onClick={() => onChoose("root")}>
              <FolderOpen />
              <span>
                <strong>全部移到收藏根目录</strong>
                <small>跳过上一级，直接放到收藏列表顶层</small>
              </span>
            </button>
          )}
          <button type="button" className="danger" onClick={() => onChoose("recursive")}>
            <Trash2 />
            <span>
              <strong>递归删除全部收藏</strong>
              <small>文件夹、子文件夹及其中收藏都会移除</small>
            </span>
          </button>
        </div>
      )}
      <div className="modal-actions favorite-folder-delete-actions">
        <button type="button" onClick={onClose}>取消</button>
        {empty && <button className="confirm-danger" type="button" onClick={() => onChoose("recursive")}>
          <Trash2 />删除空文件夹
        </button>}
      </div>
    </Modal>
  );
}

function PluginsView({
  data,
  loading,
  error,
  connected,
  onRefresh,
}: {
  data: PluginData;
  loading: boolean;
  error: string | null;
  connected: boolean;
  onRefresh: () => void;
}) {
  const [tab, setTab] = useState<"skills" | "tools">("skills");
  const [query, setQuery] = useState("");
  const tools = data.tools.filter((tool) => tool.is_enabled && tool.name !== "Skill");
  const source = tab === "skills" ? data.skills : tools;
  const filtered = source.filter((item) => {
    const needle = query.trim().toLowerCase();
    return !needle || item.name.toLowerCase().includes(needle) || item.description.toLowerCase().includes(needle);
  });
  const readOnlyCount = tools.filter((tool) => tool.is_read_only).length;
  const executeCount = tools.length - readOnlyCount;
  const capabilityStats = tab === "skills"
    ? [
      { label: "已加载技能", value: data.skills.length, icon: Boxes, tone: "coral" },
      { label: "工作流入口", value: data.skills.length > 0 ? "READY" : "EMPTY", icon: Workflow, tone: "cyan" },
    ] as const
    : [
      { label: "可用工具", value: tools.length, icon: Wrench, tone: "cyan" },
      { label: "只读 / 执行", value: `${readOnlyCount} / ${executeCount}`, icon: ShieldCheck, tone: "amber" },
    ] as const;
  return (
    <section className="workspace-page plugins-page">
      <div className="page-kicker"><Boxes /><span>CRAB DESKTOP CAPABILITY BAY</span><i /></div>
      <header className="page-header plugins-header">
        <div className="plugin-tabs" role="tablist" aria-label="插件类型">
          <button role="tab" aria-selected={tab === "skills"} className={tab === "skills" ? "active" : ""} onClick={() => setTab("skills")}><Puzzle />Skills</button>
          <button role="tab" aria-selected={tab === "tools"} className={tab === "tools" ? "active" : ""} onClick={() => setTab("tools")}><Wrench />Tools</button>
        </div>
        <span className="capability-context"><CircleDotDashed />{connected ? "工作区已连接" : "等待 Gateway"}</span>
        <button className="icon-button page-refresh" title="刷新能力舱" onClick={onRefresh} disabled={loading || !connected}><RefreshCw className={loading ? "spin" : ""} /></button>
      </header>
      <div className="page-intro">
        <h1>{tab === "skills" ? "Skills 能力舱" : "Tools 能力舱"}</h1>
        <p>{tab === "skills" ? "把团队工作流接入 Crab Desktop，按需装载专用能力" : "只展示当前工作区真正可用的执行接口"}</p>
      </div>
      <div className="capability-summary">
        {capabilityStats.map(({ label, value, icon: Icon, tone }) => (
          <div className={`capability-stat ${tone}`} key={label}>
            <Icon /><span><small>{label}</small><strong>{value}</strong></span>
          </div>
        ))}
        <div className="capability-note"><Terminal /><span>能力由当前 Gateway 提供</span><ArrowUpRight /></div>
      </div>
      <label className="page-search">
        <Search />
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={`检索 ${tab === "skills" ? "skill" : "tool"} 名称或描述`} />
      </label>
      {!connected && <PageNotice icon={<WifiOff />} text="Gateway 尚未连接，无法读取插件" />}
      {error && <PageNotice icon={<AlertTriangle />} text={error} error />}
      <div className="page-section plugin-section">
        <div className="page-section-heading"><h2>已挂载能力</h2><span>{filtered.length} 项可用</span></div>
        {loading && filtered.length === 0 ? <div className="page-loading"><LoaderCircle className="spin" /></div> : (
          <div className="plugin-grid">
            {filtered.map((item) => (
              <article className="plugin-item" key={item.name}>
                <div className={`plugin-icon ${tab === "skills" ? "skill" : "tool"}`}>{tab === "skills" ? <Puzzle /> : <Wrench />}</div>
                <div className="plugin-copy"><div className="plugin-title-row"><strong>{item.name}</strong><span className="plugin-availability"><span />AVAILABLE</span></div><p>{item.description || "暂无描述"}</p><small>{tab === "skills" ? "WORKFLOW SKILL" : ("is_read_only" in item && item.is_read_only ? "READ ONLY TOOL" : "EXECUTION TOOL")}</small></div>
                <Check className="plugin-check" aria-label="可用" />
              </article>
            ))}
            {filtered.length === 0 && !loading && <div className="page-empty">暂无可用{tab === "skills" ? "技能" : "工具"}</div>}
          </div>
        )}
      </div>
    </section>
  );
}

function PageNotice({ icon, text, error = false }: { icon: React.ReactNode; text: string; error?: boolean }) {
  return <div className={`page-notice ${error ? "error" : ""}`}>{icon}<span>{text}</span></div>;
}

const PERMISSION_OPTIONS: Array<{
  value: PermissionMode;
  label: string;
  description: string;
  icon: typeof ShieldCheck;
  tone: string;
}> = [
  {
    value: "default",
    label: "工作区默认规则",
    description: "使用当前项目加载的 CrabCode 权限配置",
    icon: ShieldCheck,
    tone: "neutral",
  },
  {
    value: "ask",
    label: "每次确认",
    description: "执行高风险操作前先向你确认",
    icon: ShieldAlert,
    tone: "ask",
  },
  {
    value: "ai_review",
    label: "AI 审查",
    description: "由审查器判断是否放行，必要时再询问",
    icon: Bot,
    tone: "review",
  },
  {
    value: "run_everything",
    label: "完全访问",
    description: "直接执行所有工具操作，不再弹出权限确认",
    icon: Zap,
    tone: "danger",
  },
];

const REASONING_EFFORT_OPTIONS: Array<{
  value: ReasoningEffort;
  label: string;
}> = [
  { value: "none", label: "关闭" },
  { value: "minimal", label: "最低" },
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
  { value: "xhigh", label: "极高" },
  { value: "max", label: "最大" },
];

function useDismissMenu(open: boolean, close: () => void, ref: React.RefObject<HTMLElement | null>) {
  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) close();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [close, open, ref]);
}

export function ConversationActionsMenu({
  sessionTitle,
  favorite = false,
  favoriteDisabled = false,
  onCheckpoint,
  onToggleFavorite,
}: {
  sessionTitle: string;
  favorite?: boolean;
  favoriteDisabled?: boolean;
  onCheckpoint: () => void;
  onToggleFavorite: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ top: 8, left: 8 });
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const title = sessionTitle || "未命名会话";

  const placeMenu = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const viewportPadding = 8;
    const gap = 8;
    const menuWidth = 208;
    const menuHeight = 90;
    setPosition({
      top: Math.min(
        rect.bottom + gap,
        Math.max(viewportPadding, window.innerHeight - menuHeight - viewportPadding),
      ),
      left: Math.min(
        Math.max(viewportPadding, rect.right - menuWidth),
        Math.max(viewportPadding, window.innerWidth - menuWidth - viewportPadding),
      ),
    });
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    const onViewportChange = () => placeMenu();
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", onViewportChange);
    window.addEventListener("scroll", onViewportChange, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", onViewportChange);
      window.removeEventListener("scroll", onViewportChange, true);
    };
  }, [open, placeMenu]);

  const choose = (action: () => void) => {
    setOpen(false);
    action();
  };

  return (
    <>
      <button
        ref={triggerRef}
        className="icon-button conversation-menu-trigger"
        type="button"
        title="更多会话操作"
        aria-label="更多会话操作"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => {
          if (!open) placeMenu();
          setOpen((value) => !value);
        }}
      >
        <MoreHorizontal />
      </button>
      {open && createPortal(
        <div
          ref={menuRef}
          className="session-action-menu conversation-action-menu"
          role="menu"
          aria-label={`${title} 更多操作`}
          style={position}
        >
          <div className="session-action-buttons conversation-action-buttons">
            <button type="button" role="menuitem" onClick={() => choose(onCheckpoint)}>
              <History />
              <span>检查点</span>
            </button>
            <button
              className={favorite ? "favorite-active" : ""}
              type="button"
              role="menuitem"
              aria-pressed={favorite}
              disabled={favoriteDisabled}
              onClick={() => choose(onToggleFavorite)}
            >
              <Star fill={favorite ? "currentColor" : "none"} />
              <span>{favorite ? "取消收藏会话" : "收藏会话"}</span>
            </button>
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}

export function ProjectActionsMenu({
  projectName,
  favorite = false,
  newSessionDisabled = false,
  deleteDisabled = false,
  onNewSession,
  onEdit,
  onToggleFavorite,
  onDelete,
}: {
  projectName: string;
  favorite?: boolean;
  newSessionDisabled?: boolean;
  deleteDisabled?: boolean;
  onNewSession: () => void;
  onEdit: () => void;
  onToggleFavorite?: () => void;
  onDelete: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ top: 8, left: 8 });
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  const placeMenu = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const viewportPadding = 8;
    const gap = 8;
    const menuWidth = 208;
    const menuHeight = onToggleFavorite ? 180 : 142;
    const fitsRight = rect.right + gap + menuWidth <= window.innerWidth - viewportPadding;
    setPosition({
      top: Math.min(
        Math.max(viewportPadding, rect.top - 7),
        Math.max(viewportPadding, window.innerHeight - menuHeight - viewportPadding),
      ),
      left: fitsRight
        ? rect.right + gap
        : Math.max(viewportPadding, rect.left - menuWidth - gap),
    });
  }, [onToggleFavorite]);

  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    const onViewportChange = () => placeMenu();
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", onViewportChange);
    window.addEventListener("scroll", onViewportChange, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", onViewportChange);
      window.removeEventListener("scroll", onViewportChange, true);
    };
  }, [open, placeMenu]);

  const choose = (action: () => void) => {
    setOpen(false);
    action();
  };

  return (
    <>
      <button
        ref={triggerRef}
        className="icon-button tiny project-menu-trigger"
        type="button"
        title={`项目操作 ${projectName}`}
        aria-label={`项目操作 ${projectName}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => {
          if (!open) placeMenu();
          setOpen((value) => !value);
        }}
      >
        <MoreHorizontal />
      </button>
      {open && createPortal(
        <div
          ref={menuRef}
          className="project-action-menu"
          role="menu"
          aria-label={`${projectName} 项目操作`}
          style={position}
        >
          <button type="button" role="menuitem" disabled={newSessionDisabled} onClick={() => choose(onNewSession)}>
            <MessageSquarePlus />
            <span>新建会话</span>
          </button>
          <button type="button" role="menuitem" onClick={() => choose(onEdit)}>
            <Pencil />
            <span>编辑项目</span>
          </button>
          {onToggleFavorite && <button type="button" role="menuitem" onClick={() => choose(onToggleFavorite)}>
            <Star fill={favorite ? "currentColor" : "none"} />
            <span>{favorite ? "取消收藏项目" : "收藏项目"}</span>
          </button>}
          <div className="project-action-separator" role="separator" />
          <button className="danger" type="button" role="menuitem" disabled={deleteDisabled} onClick={() => choose(onDelete)}>
            <Trash2 />
            <span>{deleteDisabled ? "默认项目不可删除" : "删除项目"}</span>
          </button>
        </div>,
        document.body,
      )}
    </>
  );
}

export function SessionActionsMenu({
  info,
  status,
  favorite = false,
  deleting = false,
  onToggleFavorite,
  onDelete,
}: {
  info: SessionInfo;
  status: SessionViewState["status"];
  favorite?: boolean;
  deleting?: boolean;
  onToggleFavorite: () => void;
  onDelete: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [position, setPosition] = useState({ top: 8, left: 8 });
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const sessionTitle = info.title || "未命名会话";

  const placeMenu = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const viewportPadding = 8;
    const gap = 8;
    const menuWidth = 208;
    const menuHeight = 148;
    const fitsRight = rect.right + gap + menuWidth <= window.innerWidth - viewportPadding;
    setPosition({
      top: Math.min(
        Math.max(viewportPadding, rect.top - 7),
        Math.max(viewportPadding, window.innerHeight - menuHeight - viewportPadding),
      ),
      left: fitsRight
        ? rect.right + gap
        : Math.max(viewportPadding, rect.left - menuWidth - gap),
    });
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    const onViewportChange = () => placeMenu();
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", onViewportChange);
    window.addEventListener("scroll", onViewportChange, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", onViewportChange);
      window.removeEventListener("scroll", onViewportChange, true);
    };
  }, [open, placeMenu]);

  const choose = (action: () => void) => {
    setOpen(false);
    action();
  };

  return (
    <>
      <button
        ref={triggerRef}
        className="icon-button tiny session-menu-trigger"
        type="button"
        title={`会话操作 ${sessionTitle}`}
        aria-label={`会话操作 ${sessionTitle}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => {
          if (!open) placeMenu();
          setOpen((value) => !value);
        }}
      >
        <MoreHorizontal />
      </button>
      {open && createPortal(
        <div
          ref={menuRef}
          className="session-action-menu"
          role="menu"
          aria-label={`${sessionTitle} 会话操作`}
          style={position}
        >
          <div className="session-action-buttons">
            <button type="button" role="menuitem" onClick={() => choose(onToggleFavorite)}>
              <Star fill={favorite ? "currentColor" : "none"} />
              <span>{favorite ? "取消收藏会话" : "收藏会话"}</span>
            </button>
            <button className="danger" type="button" role="menuitem" disabled={deleting} onClick={() => choose(onDelete)}>
              {deleting ? <LoaderCircle className="spin" /> : <Trash2 />}
              <span>{deleting ? "正在删除会话" : "删除会话"}</span>
            </button>
            <div className="session-action-separator" role="separator" />
            <button type="button" role="menuitem" onClick={() => choose(() => setDetailsOpen(true))}>
              <Activity />
              <span>会话详情</span>
            </button>
          </div>
        </div>,
        document.body,
      )}
      {detailsOpen && createPortal(
        <SessionDetailModal info={info} status={status} onClose={() => setDetailsOpen(false)} />,
        document.body,
      )}
    </>
  );
}

export function SessionDetailModal({
  info,
  status,
  onClose,
}: {
  info: SessionInfo;
  status: SessionViewState["status"];
  onClose: () => void;
}) {
  const contextUsed = status?.context_used_tokens ?? info.tokens_used;
  const contextWindow = status?.context_window_tokens ?? 0;
  const contextRemaining = status?.context_remaining_tokens
    ?? Math.max(0, contextWindow - contextUsed);
  const contextPercent = contextWindow > 0
    ? Math.min(100, Math.max(0, status?.context_used_percent ?? contextUsed / contextWindow * 100))
    : null;
  const modelLabel = status?.model_profile
    || [status?.provider || info.provider, status?.model || info.model].filter(Boolean).join("/")
    || "暂无";
  const modeLabel = status
    ? `${status.mode === "plan" ? "Plan" : "Agent"}${status.reasoning_effort ? ` · ${status.reasoning_effort}` : ""}`
    : "暂无";

  return (
    <Modal title="会话详情" onClose={onClose}>
      <div className="session-detail-dialog">
        <div className="session-detail-heading">
          <span className="session-detail-heading-icon"><Activity /></span>
          <div>
            <strong>{info.title || "未命名会话"}</strong>
            <small>{modelLabel}</small>
          </div>
        </div>

        <section className="session-context-card" aria-label="当前上下文用量">
          <div className="session-context-heading">
            <span>当前上下文</span>
            <strong>{contextUsed.toLocaleString("zh-CN")} tokens{contextPercent === null ? "" : ` · ${contextPercent.toFixed(1)}%`}</strong>
          </div>
          {contextPercent !== null && (
            <div className="session-context-progress"><span style={{ width: `${contextPercent}%` }} /></div>
          )}
          <small>{contextWindow > 0
            ? `总窗口 ${contextWindow.toLocaleString("zh-CN")} tokens · 剩余 ${contextRemaining.toLocaleString("zh-CN")} tokens`
            : "尚未加载上下文窗口上限"}</small>
        </section>

        <dl className="session-detail-list">
          <div>
            <dt>Session ID</dt>
            <dd className="session-detail-copy-value">
              <code title={info.session_id}>{info.session_id}</code>
              <CopyButton text={info.session_id} label="复制 Session ID" className="session-detail-copy" />
            </dd>
          </div>
          <div><dt>消息</dt><dd>{status?.message_count ?? info.message_count} 条</dd></div>
          <div><dt>压缩次数</dt><dd>{status?.compact_count ?? 0} 次</dd></div>
          <div><dt>模型</dt><dd title={modelLabel}>{modelLabel}</dd></div>
          <div><dt>模式</dt><dd>{modeLabel}</dd></div>
          <div><dt>创建时间</dt><dd>{info.created_at ? formatDateTime(info.created_at) : "暂无"}</dd></div>
          <div><dt>工作目录</dt><dd title={info.cwd || undefined}>{info.cwd || "暂无"}</dd></div>
          {info.forked_from_session_id && (
            <div><dt>分叉自</dt><dd title={info.forked_from_session_id}>{info.forked_from_title || info.forked_from_session_id}</dd></div>
          )}
        </dl>
      </div>
    </Modal>
  );
}

function ComposerAddMenu({
  disabled,
  planActive,
  ultraActive,
  onImages,
  onFiles,
  fileUploadMode,
  onFilePaths,
  onReferencePath,
  onGoal,
  onPlan,
  onUltra,
}: {
  disabled: boolean;
  planActive: boolean;
  ultraActive: boolean;
  onImages: (files: File[]) => void;
  onFiles: (files: File[]) => void;
  fileUploadMode: "content" | "path";
  onFilePaths: () => void;
  onReferencePath: () => void;
  onGoal: () => void;
  onPlan: () => void;
  onUltra: () => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  const imageInputRef = useRef<HTMLInputElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismissMenu(open, close, ref);
  const run = (action: () => void) => {
    action();
    setOpen(false);
  };
  return (
    <div className="composer-add" ref={ref}>
      <button
        type="button"
        className="composer-add-trigger"
        title="添加内容或切换模式"
        aria-label="添加"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
      ><Plus /></button>
      <input
        ref={imageInputRef}
        type="file"
        multiple
        hidden
        accept="image/*"
        onChange={(event) => {
          onImages(Array.from(event.target.files ?? []));
          event.target.value = "";
        }}
      />
      <input
        ref={fileInputRef}
        type="file"
        multiple
        hidden
        accept=".txt,.md,.json,.py,.ts,.tsx,.js,.jsx,.rs,.go,.java,.css,.html,.yaml,.yml,.toml"
        onChange={(event) => {
          onFiles(Array.from(event.target.files ?? []));
          event.target.value = "";
        }}
      />
      {open && (
        <div className="composer-add-menu" role="menu" aria-label="添加">
          <div className="composer-add-heading">添加</div>
          <button type="button" role="menuitem" onClick={() => run(() => imageInputRef.current?.click())}>
            <ImageIcon /><span>添加图片</span>
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => run(() => fileUploadMode === "path" ? onFilePaths() : fileInputRef.current?.click())}
          >
            <FileText /><span>添加文件</span><small>{fileUploadMode === "path" ? "仅路径" : "含内容"}</small>
          </button>
          <button type="button" role="menuitem" onClick={() => run(onReferencePath)}>
            <FolderInput /><span>引用文件或文件夹</span>
          </button>
          <div className="composer-add-separator" />
          <button type="button" role="menuitem" onClick={() => run(onGoal)}>
            <Target /><span>目标</span>
          </button>
          <button className={planActive ? "active" : ""} type="button" role="menuitem" onClick={() => run(onPlan)}>
            <ListTodo /><span>计划模式</span>{planActive && <Check />}
          </button>
          <button className={ultraActive ? "active ultra" : "ultra"} type="button" role="menuitem" onClick={() => run(onUltra)}>
            <Sparkles /><span>Ultra 模式</span>{ultraActive && <Check />}
          </button>
        </div>
      )}
    </div>
  );
}

function ModelPicker({
  models,
  value,
  fallback,
  disabled,
  onChange,
}: {
  models: GatewayViewState["models"];
  value: string;
  fallback: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const ref = useRef<HTMLDivElement | null>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismissMenu(open, close, ref);
  const grouped = groupGatewayModels(models, query);
  const filteredCount = grouped.reduce((total, entry) => total + entry.models.length, 0);
  return (
    <div className="picker model-picker" ref={ref}>
      <button
        type="button"
        className="picker-trigger model-picker-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled || models.length === 0}
        title="选择模型"
        onClick={() => setOpen((current) => !current)}
      >
        <span className="picker-trigger-icon"><Bot /></span>
        <span className="picker-trigger-label">{value || fallback}</span>
        <ChevronDown className={open ? "picker-chevron open" : "picker-chevron"} />
      </button>
      {open && (
        <div className="picker-menu model-picker-menu" role="listbox" aria-label="选择模型">
          <div className="picker-menu-heading"><span>模型</span><small>{models.length} 个可用</small></div>
          <label className="picker-search"><Search /><input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索模型" /></label>
          <div className="picker-options">
            {grouped.map(({ group, models: entries }) => (
              <div className="picker-model-group" key={group} role="group" aria-label={group}>
                <div className="picker-group-heading" aria-hidden="true">{group}</div>
                {entries.map((model) => (
                  <button
                    type="button"
                    role="option"
                    aria-selected={model.name === value}
                    className={`picker-option ${model.name === value ? "selected" : ""}`}
                    key={model.name}
                    onClick={() => { onChange(model.name); setOpen(false); setQuery(""); }}
                  >
                    <span className="picker-option-icon"><Bot /></span>
                    <span className="picker-option-copy"><strong>{model.name}</strong>{model.description && <small>{model.description}</small>}</span>
                    {model.name === value && <Check className="picker-check" />}
                  </button>
                ))}
              </div>
            ))}
            {filteredCount === 0 && <span className="picker-empty">没有匹配的模型</span>}
          </div>
        </div>
      )}
    </div>
  );
}

function ReasoningEffortPicker({
  value,
  disabled,
  onChange,
}: {
  value: string | null | undefined;
  disabled: boolean;
  onChange: (value: ReasoningEffort) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismissMenu(open, close, ref);
  const selected = REASONING_EFFORT_OPTIONS.find((option) => option.value === value);
  return (
    <div className="picker effort-picker" ref={ref}>
      <button
        type="button"
        className="picker-trigger effort-picker-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        title="选择思考强度"
        onClick={() => setOpen((current) => !current)}
      >
        <Brain />
        <span className="picker-trigger-label">思考 {selected?.label ?? "自动"}</span>
        <ChevronDown className={open ? "picker-chevron open" : "picker-chevron"} />
      </button>
      {open && (
        <div className="picker-menu effort-picker-menu" role="menu" aria-label="思考强度">
          <div className="picker-menu-heading"><span>思考强度</span><small>随会话保存</small></div>
          <div className="effort-options">
            {REASONING_EFFORT_OPTIONS.map((option) => (
              <button
                type="button"
                role="menuitemradio"
                aria-checked={option.value === value}
                className={`effort-option ${option.value === value ? "selected" : ""}`}
                key={option.value}
                onClick={() => { onChange(option.value); setOpen(false); }}
              >
                <span>{option.label}</span>
                {option.value === value && <Check className="picker-check" />}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function PermissionPicker({
  value,
  disabled,
  onChange,
}: {
  value: PermissionMode;
  disabled: boolean;
  onChange: (value: PermissionMode) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismissMenu(open, close, ref);
  const selected = PERMISSION_OPTIONS.find((item) => item.value === value) ?? PERMISSION_OPTIONS[0];
  const Icon = selected.icon;
  return (
    <div className="picker permission-picker" ref={ref}>
      <button
        type="button"
        className={`picker-trigger permission-picker-trigger ${selected.tone === "danger" ? "danger" : ""}`}
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        title="选择权限策略"
        onClick={() => setOpen((current) => !current)}
      >
        <Icon />
        <span className="picker-trigger-label">{selected.label}</span>
        <ChevronDown className={open ? "picker-chevron open" : "picker-chevron"} />
      </button>
      {open && (
        <div className="picker-menu permission-picker-menu" role="menu" aria-label="权限策略">
          <div className="picker-menu-heading"><span>工具执行权限</span><small>仅作用于当前会话</small></div>
          <div className="permission-options">
            {PERMISSION_OPTIONS.map((option) => {
              const OptionIcon = option.icon;
              return (
                <button
                  type="button"
                  role="menuitemradio"
                  aria-checked={option.value === value}
                  className={`permission-option ${option.value === value ? "selected" : ""} ${option.tone}`}
                  key={option.value}
                  onClick={() => { onChange(option.value); setOpen(false); }}
                >
                  <span className="permission-option-icon"><OptionIcon /></span>
                  <span className="permission-option-copy"><strong>{option.label}</strong><small>{option.description}</small></span>
                  {option.value === value && <Check className="picker-check" />}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function ContextMeter({
  status,
  usage,
}: {
  status: SessionViewState["status"];
  usage: Record<string, unknown> | null | undefined;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismissMenu(open, close, ref);
  if (!status?.context_window_tokens) return null;
  const percent = Math.min(100, Math.max(0, status.context_used_percent));
  const remaining = status.context_remaining_tokens ?? Math.max(0, status.context_window_tokens - status.context_used_tokens);
  const contextClass = percent >= 90 ? "danger" : percent >= 75 ? "warn" : "";
  const cache = cacheUsage(usage);
  const searchDetail = status.search_index
    ? [
        status.search_index.state,
        status.search_index.files == null ? null : `${status.search_index.files} 文件`,
        status.search_index.chunks == null ? null : `${status.search_index.chunks} 分块`,
        status.search_index.done == null || status.search_index.total == null
          ? null
          : `${status.search_index.done}/${status.search_index.total}`,
      ].filter(Boolean).join(" · ")
    : null;
  return (
    <div className="context-meter-wrap" ref={ref}>
      <button
        type="button"
        className={`context-meter ${contextClass}`}
        title="查看背景窗口"
        aria-label={`背景窗口已使用 ${Math.round(percent)}%`}
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
        style={{ "--context-progress": `${percent}%` } as React.CSSProperties}
      >
        <span className="context-meter-ring" />
        <span>{Math.round(percent)}%</span>
      </button>
      {open && (
        <div className="context-popover">
          <div className="context-popover-title"><span>背景窗口</span><strong>{Math.round(percent)}% 已用</strong></div>
          <div className="context-progress"><span style={{ width: `${percent}%` }} /></div>
          <div className="context-stat-row"><span>已用</span><strong>{formatTokenCount(status.context_used_tokens)} / {formatTokenCount(status.context_window_tokens)} tokens</strong></div>
          <div className="context-stat-row"><span>剩余</span><strong>{formatTokenCount(remaining)} tokens</strong></div>
          {cache && <div className="context-stat-row"><span>缓存命中率</span><strong>{cache.hitRate.toFixed(1)}%</strong></div>}
          {cache && <div className="context-stat-row"><span>缓存读取</span><strong>{formatTokenCount(cache.readTokens)} tokens</strong></div>}
          {cache?.writeTokens != null && <div className="context-stat-row"><span>缓存写入</span><strong>{formatTokenCount(cache.writeTokens)} tokens</strong></div>}
          <div className="context-stat-row"><span>模型</span><strong>{status.model_profile || [status.provider, status.model].filter(Boolean).join("/") || "未配置"}</strong></div>
          <div className="context-stat-row"><span>模式</span><strong>{status.mode === "plan" ? "Plan" : "Agent"}</strong></div>
          <div className="context-stat-row"><span>推理</span><strong>{status.reasoning_effort || "自动"}{status.ultra_mode ? " · Ultra" : ""}</strong></div>
          <div className="context-stat-row"><span>会话</span><strong>{status.message_count ?? 0} 条消息 · 压缩 {status.compact_count ?? 0} 次</strong></div>
          <div className="context-stat-row"><span>自动压缩</span><strong>{status.auto_compact_enabled === false ? "关闭" : "开启"}</strong></div>
          <div className="context-stat-row"><span>输出配置</span><strong>思考 {status.thinking_enabled ? "开启" : "关闭"} · {formatTokenCount(status.max_tokens ?? 0)} tokens</strong></div>
          {status.tool_count != null && <div className="context-stat-row"><span>可用工具</span><strong>{status.tool_count}</strong></div>}
          {(status.agent_total ?? 0) > 0 && <div className="context-stat-row"><span>Agents</span><strong>{status.agent_active ?? 0} 运行中 / {status.agent_total} 个 · {status.agent_failed ?? 0} 失败</strong></div>}
          {(status.monitor_total ?? 0) > 0 && <div className="context-stat-row"><span>后台任务</span><strong>{status.monitor_active ?? 0} 运行中 / {status.monitor_total} 个 · {status.monitor_failed ?? 0} 失败</strong></div>}
          {searchDetail && <div className="context-stat-row"><span>语义索引</span><strong>{searchDetail}</strong></div>}
          <div className="context-stat-row"><span>工作目录</span><strong title={status.cwd}>{status.cwd}</strong></div>
        </div>
      )}
    </div>
  );
}

function ExecutionStatusBar({
  startedAt,
  currentStep,
  now,
}: {
  startedAt: number;
  currentStep: SessionViewState["currentStep"];
  now: number;
}) {
  const total = Math.max(0, now - startedAt);
  const step = currentStep ? Math.max(0, now - currentStep.startedAt) : total;
  return (
    <div className="execution-status" role="status" aria-live="polite">
      <span className="execution-pulse"><span /></span>
      <span className="execution-copy"><strong>Agent 正在工作</strong><small>{currentStep?.label ?? "处理中"}</small></span>
      <span className="execution-time"><span>本轮 {formatElapsed(total)}</span><span>当前步骤 {formatElapsed(step)}</span></span>
    </div>
  );
}

function EmptyWorkspace({
  connection,
  project,
  gateway,
  onNew,
  onConnect,
}: {
  connection: ConnectionPreset | null;
  project: ProjectPreset | null;
  gateway: GatewayViewState | null | undefined;
  onNew: () => void;
  onConnect: () => void;
}) {
  if (!connection || gateway?.status === "error") {
    return (
      <div className="empty-workspace">
        <WifiOff />
        <h1>{connection ? "无法连接 Gateway" : "连接 CrabCode Gateway"}</h1>
        <p>{gateway?.error ?? "添加本地或远程 Gateway 连接"}</p>
        <button className="command-button" onClick={onConnect}><Server />管理连接</button>
      </div>
    );
  }
  if (gateway?.status !== "online") {
    return <div className="empty-workspace"><LoaderCircle className="spin" /><h1>正在连接 {connection.name}</h1></div>;
  }
  return (
    <div className="empty-workspace">
      <Bot />
      <h1>{project ? `在 ${project.name} 中开始` : "选择一个项目"}</h1>
      <p>{project ? projectDirectoryTitle(project) : ""}</p>
      {project && <button className="command-button" onClick={onNew}><MessageSquarePlus />新会话</button>}
    </div>
  );
}

function ToolFieldView({ field }: { field: ToolField }) {
  if (field.variant === "list") {
    return (
      <div className="tool-detail-row">
        <span>{field.label}</span>
        <div className="tool-chip-list">
          {field.value.split(" · ").map((value, index) => <code key={`${field.key}-${index}`}>{value}</code>)}
        </div>
      </div>
    );
  }
  return (
    <div className={`tool-detail-row ${field.variant === "code" || field.variant === "json" ? "stacked" : ""}`}>
      <span>{field.label}</span>
      {field.variant === "code" || field.variant === "json"
        ? <CopyablePre text={field.value} />
        : <code className={field.variant === "path" ? "tool-path" : ""}>{field.value}</code>}
    </div>
  );
}

function ChecklistResultView({ result }: { result: unknown }) {
  const blocks = parseChecklistResult(result);
  if (!blocks.length) return null;
  return (
    <div className="tool-checklist-stack">
      {blocks.map((block, blockIndex) => {
        const total = block.total || block.items.length;
        const done = block.done || block.items.filter((item) => item.checked).length;
        const progress = total ? Math.round(done / total * 100) : 0;
        return (
          <section className="tool-checklist" key={`${block.title}-${blockIndex}`}>
            <header><strong>{block.title}</strong><span>{done}/{total}</span></header>
            <div className="tool-progress"><span style={{ width: `${progress}%` }} /></div>
            <ul>
              {block.items.map((entry, index) => (
                <li className={entry.checked ? "checked" : ""} key={`${entry.text}-${index}`}>
                  <i>{entry.checked ? "✓" : ""}</i><span>{entry.text}</span>
                </li>
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

function ToolResultView({ toolName, result, isError = false }: { toolName: string; result: unknown; isError?: boolean }) {
  const checklist = toolName.toLowerCase() === "checklist" ? parseChecklistResult(result) : [];
  if (checklist.length) return <ChecklistResultView result={result} />;
  const text = textFromUnknown(result);
  const isDiff = text.startsWith("---") || text.startsWith("diff --git") || text.includes("\n+++");
  return <CopyablePre text={text} label={isError ? "复制错误" : isDiff ? "复制 diff" : "复制执行结果"} className={`tool-result ${isDiff ? "diff-view" : ""}`}>{isDiff ? diffLines(text) : text}</CopyablePre>;
}

function CopyablePre({ text, label = "复制", className = "", containerClassName = "", children }: { text: string; label?: string; className?: string; containerClassName?: string; children?: ReactNode }) {
  return (
    <div className={`copyable-content ${containerClassName}`.trim()}>
      <pre className={className}>{children ?? text}</pre>
      <CopyButton text={text} label={label} />
    </div>
  );
}

function reactNodeText(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(reactNodeText).join("");
  if (!isValidElement<{ children?: ReactNode }>(node)) return "";
  const type = typeof node.type === "string" ? node.type : "";
  const content = reactNodeText(node.props.children);
  if (type === "tr") return `${content}\n`;
  if (type === "th" || type === "td") return `${content}\t`;
  if (["li", "p", "div"].includes(type)) return `${content}\n`;
  return content;
}

function markdownCodeChild(children: ReactNode): { className: string; source: string } | null {
  if (!isValidElement<{ className?: string; children?: ReactNode }>(children)) return null;
  const className = children.props.className ?? "";
  const language = /(?:^|\s)language-([^\s]+)/.exec(className)?.[1]?.toLowerCase();
  if (!language) return null;
  return {
    className: MESSAGE_CODE_LANGUAGE_ALIASES[language] ?? language,
    source: String(children.props.children ?? "").replace(/\n$/, ""),
  };
}

function MessageBlockCopy({ text, label }: { text: string; label: string }) {
  return (
    <div className="message-block-actions">
      <CopyButton text={text} label={label} />
    </div>
  );
}

function InlineImages({ images }: { images?: Array<{ media_type: string; data: string }> }) {
  if (!images?.length) return null;
  return (
    <div className="message-images" aria-label="图片附件">
      {images.map((image, index) => (
        <img
          key={`${image.media_type}-${index}`}
          src={`data:${image.media_type};base64,${image.data}`}
          alt={`图片 ${index + 1}`}
          loading="lazy"
        />
      ))}
    </div>
  );
}

const MESSAGE_MARKDOWN_COMPONENTS: Components = {
  img: ({ src, alt }) => (
    typeof src === "string" && /^(?:https?:|data:image\/)/i.test(src)
      ? <img src={src} alt={alt ?? "图片"} loading="lazy" />
      : null
  ),
  table: ({ children }) => (
    <div className="message-table-shell">
      <div className="message-table-wrap"><table>{children}</table></div>
      <MessageBlockCopy text={reactNodeText(children).trim()} label="复制表格" />
    </div>
  ),
  pre: ({ children }) => {
    const code = markdownCodeChild(children);
    if (!code || !MESSAGE_CODE_LANGUAGES.has(code.className)) {
      const text = reactNodeText(children).replace(/\n$/, "");
      return (
        <div className="message-code-shell">
          <pre className="message-plain-code">{children}</pre>
          <MessageBlockCopy text={text} label="复制代码" />
        </div>
      );
    }
    return (
      <div className="message-code-shell">
        <SyntaxHighlighter
          className="message-code-block"
          language={code.className}
          PreTag="pre"
          CodeTag="code"
          useInlineStyles={false}
          customStyle={{}}
        >
          {code.source}
        </SyntaxHighlighter>
        <MessageBlockCopy text={code.source} label="复制代码" />
      </div>
    );
  },
};

export function MessageMarkdown({ children }: { children: string }) {
  const normalizedMarkdown = useMemo(() => normalizeMarkdownMathDelimiters(children), [children]);
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeKatex]}
      components={MESSAGE_MARKDOWN_COMPONENTS}
    >
      {normalizedMarkdown}
    </ReactMarkdown>
  );
}

export function ChatItemView({ item, now, showTurnDuration, turnDurationFormat, onPermission, onToggleChoice, onSubmitChoice, onPlan, onCompatibilityRetry, onFork, forkDisabled }: {
  item: ChatItem;
  now: number;
  showTurnDuration: boolean;
  turnDurationFormat: TurnDurationFormat;
  onPermission: (item: ChatItem, allowed: boolean, always?: boolean) => void;
  onToggleChoice: (item: ChatItem, option: string) => void;
  onSubmitChoice: (item: ChatItem) => void;
  onPlan: (action: "execute" | "revise" | "cancel") => void;
  onCompatibilityRetry?: () => void;
  onFork?: (item: ChatItem) => void;
  forkDisabled?: boolean;
}) {
  const [collapsed, setCollapsed] = useState(item.collapsed ?? false);
  const durationMs = item.durationMs ?? (item.startedAt ? Math.max(0, now - item.startedAt) : null);
  const durationLabel = durationMs === null ? null : formatElapsed(durationMs);
  if (item.kind === "turn_duration") {
    if (!showTurnDuration || durationMs === null) return null;
    const label = `已处理：${formatTurnDuration(durationMs, turnDurationFormat)}`;
    return <div className="turn-duration-divider" role="separator" aria-label={label}><span>{label}</span></div>;
  }
  if (item.kind === "user") {
    return (
      <article className="message user-message-shell">
        <div className="user-message"><MessageMarkdown>{item.text ?? ""}</MessageMarkdown></div>
        <InlineImages images={item.images} />
        {item.text && <div className="message-actions">
          <CopyButton text={item.text} label="复制输入" />
        </div>}
      </article>
    );
  }
  if (item.kind === "assistant") {
    return (
      <article className="message assistant-message">
        <MessageMarkdown>{item.text ?? ""}</MessageMarkdown>
        <InlineImages images={item.images} />
        {item.text && <div className="message-actions">
          <CopyButton text={item.text} label="复制回复" />
          {item.status === "complete" && onFork && (
            <button
              className="copy-button fork-button"
              type="button"
              title="从此处分叉"
              aria-label="从此处分叉"
              disabled={forkDisabled}
              onClick={() => onFork(item)}
            >
              <GitBranch />
            </button>
          )}
        </div>}
      </article>
    );
  }
  if (item.kind === "command") {
    return (
      <article className="command-card">
        <header className="command-card-header">
          <Terminal />
          <strong>{item.title ?? "命令结果"}</strong>
          {item.command && <code>{item.command}</code>}
          <span>完成</span>
        </header>
        <CopyablePre text={item.text ?? ""} className="command-card-content" />
      </article>
    );
  }
  if (item.kind === "system") return <div className="system-line">{item.text}</div>;
  if (item.kind === "error") {
    return (
      <div className="error-line copyable-inline">
        <AlertTriangle />
        <span>{item.text}</span>
        {item.text && <CopyButton text={item.text} label="复制错误" />}
      </div>
    );
  }
  if (item.kind === "document_job") {
    const progress = item.total ? Math.min(100, Math.round(((item.current ?? 0) / item.total) * 100)) : 0;
    const statusLabel = item.status === "complete"
      ? "完成"
      : item.status === "failed"
        ? "失败"
        : item.status === "cancelled"
          ? "已取消"
          : item.status === "retrying"
            ? "校验后重试"
            : "运行中";
    return (
      <article className={`activity-card document-job-card is-${item.status ?? "running"}`}>
        <div className="activity-header">
          <i className="tool-glyph" aria-hidden="true">
            {item.status === "running" || item.status === "retrying" ? <LoaderCircle className="spin" /> : item.action === "translate" ? <FileText /> : <Sparkles />}
          </i>
          <span>{item.title}</span>
          {(item.action === "generate_blog" ? item.language : item.locale) && (
            <code>{item.action === "generate_blog" ? item.language : item.locale}</code>
          )}
          <em className="tool-status">{statusLabel}</em>
        </div>
        <div className="document-job-body">
          {(item.total ?? 0) > 1 && <div className="document-job-progress"><i style={{ width: `${progress}%` }} /><span>{item.current ?? 0} / {item.total}</span></div>}
          {item.text && <p>{item.text}</p>}
          {item.status === "failed" && item.engine === "precise" && onCompatibilityRetry && (
            <button className="document-job-retry" type="button" onClick={onCompatibilityRetry}>
              <RefreshCw />使用兼容模式重试
            </button>
          )}
        </div>
      </article>
    );
  }
  if (item.kind === "thinking") {
    return (
      <article className="activity-card thinking-card">
        <button className="activity-header" onClick={() => setCollapsed((value) => !value)}>
          {item.status === "running" ? <LoaderCircle className="spin" /> : <Bot />}
          <span>{item.title}</span>
          {durationLabel && <small className="activity-duration">{durationLabel}</small>}
          {collapsed ? <ChevronRight /> : <ChevronDown />}
        </button>
        {!collapsed && <div className="activity-body prose-muted">{item.text}</div>}
      </article>
    );
  }
  if (item.kind === "tool") {
    const input = item.input ?? (
      item.result === undefined && item.detail && typeof item.detail === "object" && !Array.isArray(item.detail)
        ? item.detail as Record<string, unknown>
        : {}
    );
    const result = item.result ?? (typeof item.detail === "string" ? item.detail : undefined);
    const presentation = getToolPresentation(item.title ?? "Tool", input);
    return (
      <article className={`activity-card tool-card tool-kind-${presentation.kind} ${item.isError ? "is-error" : ""}`}>
        <button className="activity-header" onClick={() => setCollapsed((value) => !value)}>
          <i className="tool-glyph" aria-hidden="true">{item.status === "running" ? <LoaderCircle className="spin" /> : presentation.glyph}</i>
          <span className="tool-card-title">{presentation.label}</span>
          <small className="tool-technical-name">{item.title}</small>
          {presentation.summary && <code className="tool-summary">{presentation.summary}</code>}
          <em className={`tool-status ${item.isError ? "error" : item.status === "running" ? "running" : "complete"}`}>
            {item.isError ? "失败" : item.status === "running" ? "运行中" : "完成"}
          </em>
          {durationLabel && <small className="activity-duration">{durationLabel}</small>}
          {collapsed ? <ChevronRight /> : <ChevronDown />}
        </button>
        {!collapsed && (
          <div className="activity-body tool-card-body">
            {presentation.action && <div className="tool-action-badge">{presentation.action}</div>}
            {presentation.fields.length > 0 && (
              <section className="tool-card-section">
                <h4>调用参数</h4>
                <div className="tool-detail-list">{presentation.fields.map((field) => <ToolFieldView field={field} key={field.key} />)}</div>
              </section>
            )}
            {result !== undefined && (
              <section className="tool-card-section">
                <h4>{item.isError ? "错误" : "执行结果"}</h4>
                <ToolResultView toolName={item.title ?? "Tool"} result={result} isError={item.isError} />
              </section>
            )}
            <InlineImages images={item.images} />
            {presentation.fields.length === 0 && result === undefined && <div className="tool-empty">无需参数</div>}
          </div>
        )}
      </article>
    );
  }
  if (item.kind === "file_change") {
    return (
      <article className="activity-card diff-card">
        <button className="activity-header" onClick={() => setCollapsed((value) => !value)}>
          <FileDiff />
          <span>{item.action} {item.title}</span>
          {collapsed ? <ChevronRight /> : <ChevronDown />}
        </button>
        {!collapsed && (
          <CopyablePre
            text={item.diff ?? "此事件没有附带 diff"}
            className="activity-body diff-view"
          >
            {item.diff ? diffLines(item.diff) : "此事件没有附带 diff"}
          </CopyablePre>
        )}
      </article>
    );
  }
  if (item.kind === "permission") {
    return (
      <article className="request-card">
        <div className="request-title"><ShieldAlert /><strong>{item.title}</strong>{durationLabel && <small className="activity-duration">{durationLabel}</small>}</div>
        {item.text && <p>{item.text}</p>}
        <CopyablePre text={textFromUnknown(item.detail)} />
        {item.status === "pending" ? (
          <div className="request-actions">
            <button onClick={() => onPermission(item, false)}>拒绝</button>
            <button onClick={() => onPermission(item, true, true)}>始终允许</button>
            <button className="primary" onClick={() => onPermission(item, true)}>允许</button>
          </div>
        ) : <span className={`request-result ${item.status}`}>{item.status === "allowed" ? "已允许" : "已拒绝"}</span>}
      </article>
    );
  }
  if (item.kind === "choice") {
    return (
      <article className="request-card">
        <div className="request-title"><MoreHorizontal /><strong>{item.title}</strong>{durationLabel && <small className="activity-duration">{durationLabel}</small>}</div>
        <div className="choice-list">
          {item.options?.map((option) => (
            <label key={option}>
              <input
                type={item.multiple ? "checkbox" : "radio"}
                checked={item.selected?.includes(option) ?? false}
                disabled={item.status !== "pending"}
                onChange={() => onToggleChoice(item, option)}
              />
              <span>{option}</span>
            </label>
          ))}
        </div>
        {item.status === "pending" && (
          <div className="request-actions">
            <button className="primary" disabled={!item.selected?.length} onClick={() => onSubmitChoice(item)}>确认</button>
          </div>
        )}
      </article>
    );
  }
  if (item.kind === "plan") {
    return (
      <article className="request-card plan-card">
        <div className="request-title"><Bot /><strong>{item.title}</strong></div>
        <CopyablePre text={textFromUnknown(item.detail)} />
        <div className="request-actions">
          <button onClick={() => onPlan("cancel")}>取消</button>
          <button onClick={() => onPlan("revise")}>修改</button>
          <button className="primary" onClick={() => onPlan("execute")}>执行计划</button>
        </div>
      </article>
    );
  }
  return null;
}

function ConnectionModal({ settings, activeConnectionId, initialEditingId, onClose, onActivate, onSave }: {
  settings: DesktopSettings;
  activeConnectionId: string;
  initialEditingId?: string | null;
  onClose: () => void;
  onActivate: (id: string) => void;
  onSave: (connection: ConnectionPreset, password: string) => Promise<void>;
}) {
  const initialConnection = settings.connections.find((connection) => connection.id === initialEditingId) ?? null;
  const [name, setName] = useState(initialConnection?.name ?? "");
  const [baseUrl, setBaseUrl] = useState(initialConnection?.base_url ?? "https://");
  const [password, setPassword] = useState("");
  const [allowInsecure, setAllowInsecure] = useState(initialConnection?.allow_insecure_remote ?? false);
  const [editingId, setEditingId] = useState<string | null>(initialConnection?.id ?? null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const editingConnection = settings.connections.find((connection) => connection.id === editingId) ?? null;
  const editConnection = (connection: ConnectionPreset) => {
    setEditingId(connection.id);
    setName(connection.name);
    setBaseUrl(connection.base_url);
    setPassword("");
    setAllowInsecure(connection.allow_insecure_remote);
    setError(null);
  };
  const createConnection = () => {
    setEditingId(null);
    setName("");
    setBaseUrl("https://");
    setPassword("");
    setAllowInsecure(false);
    setError(null);
  };
  return (
    <Modal title="Gateway 连接" onClose={onClose}>
      <div className="saved-connections">
        {settings.connections.map((connection) => (
          <div className="saved-connection-row" key={connection.id}>
            <button
              className={connection.id === activeConnectionId ? "active" : ""}
              onClick={() => onActivate(connection.id)}
            >
              <Server />
              <span><strong>{connection.name}</strong><small>{connection.base_url}</small></span>
              <ChevronRight />
            </button>
            <button className="icon-button" title="编辑连接" onClick={() => editConnection(connection)}>
              <Settings />
            </button>
          </div>
        ))}
      </div>
      <div className="modal-divider">
        <span>{editingConnection ? `编辑 ${editingConnection.name}` : "新建远程连接"}</span>
      </div>
      <form
        className="form-grid"
        onSubmit={(event) => {
          event.preventDefault();
          setBusy(true);
          setError(null);
          let fallbackName: string;
          try {
            fallbackName = new URL(baseUrl).host;
          } catch {
            setError("Gateway 地址无效");
            setBusy(false);
            return;
          }
          const id = editingConnection?.id ?? crypto.randomUUID();
          void onSave({
            id,
            name: name.trim() || fallbackName,
            base_url: baseUrl,
            credential_ref: password
              ? editingConnection?.credential_ref ?? `gateway-${id}`
              : editingConnection?.credential_ref ?? null,
            allow_insecure_remote: allowInsecure,
            last_model_profile: editingConnection?.last_model_profile ?? null,
            projects: editingConnection?.projects ?? [],
            favorite_items: editingConnection?.favorite_items ?? [],
            last_project_path: editingConnection?.last_project_path ?? null,
            last_project_id: editingConnection?.last_project_id ?? null,
            document_workspace_root: editingConnection?.document_workspace_root ?? null,
          }, password).catch((reason) => {
            setError(reason instanceof Error ? reason.message : String(reason));
            setBusy(false);
          });
        }}
      >
        <label><span>名称</span><input value={name} onChange={(event) => setName(event.target.value)} placeholder="构建服务器" /></label>
        <label><span>Gateway 地址</span><input required value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://host:4096" /></label>
        <label>
          <span>{editingConnection?.credential_ref ? "密码（留空则保留；浏览器重开后需重新输入）" : "密码"}</span>
          <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" />
        </label>
        {isInsecureRemoteUrl(baseUrl) && (
          <label className="warning-check">
            <input type="checkbox" checked={allowInsecure} onChange={(event) => setAllowInsecure(event.target.checked)} />
            <ShieldAlert /><span>允许通过未加密连接传输凭据</span>
          </label>
        )}
        {error && <div className="form-error"><AlertTriangle />{error}</div>}
        <div className="modal-actions">
          {editingConnection && <button type="button" onClick={createConnection}>新建连接</button>}
          <button type="button" onClick={onClose}>取消</button>
          <button className="primary" disabled={busy}>{busy ? <LoaderCircle className="spin" /> : editingConnection ? <Settings /> : <Plus />}保存并连接</button>
        </div>
      </form>
    </Modal>
  );
}

export function ProjectTypeModal({ capabilities, onClose, onSelect }: {
  capabilities: DocumentCapabilities | null | undefined;
  onClose: () => void;
  onSelect: (kind: "project" | "document") => void;
}) {
  const [kind, setKind] = useState<"project" | "document">("project");
  const documentDisabled = capabilities === null;
  return (
    <Modal title="创建项目" onClose={onClose} wide>
      <div className="project-type-modal">
        <h3>项目类型</h3>
        <div className="project-type-grid" role="radiogroup" aria-label="项目类型">
          <button
            type="button"
            role="radio"
            aria-checked={kind === "project"}
            className={kind === "project" ? "selected" : ""}
            onClick={() => setKind("project")}
          >
            <span className="project-type-check">{kind === "project" && <Check />}</span>
            <Folder />
            <strong>项目</strong>
            <small>编辑、运行和测试本地或远程工作区中的文件</small>
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={kind === "document"}
            className={kind === "document" ? "selected" : ""}
            disabled={documentDisabled}
            onClick={() => setKind("document")}
          >
            <span className="project-type-check">{kind === "document" && <Check />}</span>
            <FileText />
            <strong>文档</strong>
            <small>{documentDisabled ? "当前 Gateway 版本不支持文档项目" : "阅读、翻译文档并生成 Blog"}</small>
          </button>
        </div>
        {capabilities === undefined && <p className="project-type-status"><LoaderCircle className="spin" />正在检查文档能力…</p>}
      </div>
      <div className="modal-actions">
        <button type="button" onClick={onClose}>取消</button>
        <button className="primary" type="button" onClick={() => onSelect(kind)}>下一步<ChevronRight /></button>
      </div>
    </Modal>
  );
}

export function DocumentProjectModal({ api, capabilities, defaultRoot, onClose, onSave }: {
  api: GatewayApi;
  capabilities: DocumentCapabilities | null | undefined;
  defaultRoot: string;
  onClose: () => void;
  onSave: (project: ProjectPreset) => void;
}) {
  const [projectId] = useState(() => crypto.randomUUID());
  const [mode, setMode] = useState<"file" | "url">("file");
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [workspacePath, setWorkspacePath] = useState(() => defaultProjectDirectory(defaultRoot, "新文档"));
  const [workspaceEdited, setWorkspaceEdited] = useState(false);
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState<"upload" | "convert" | "parse" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const applySuggestedName = (value: string) => {
    const filename = value.split(/[\\/]/).at(-1) || value;
    const suggested = filename.replace(/\.[^.]+$/, "").trim() || "新文档";
    setName((current) => current.trim() ? current : suggested);
    if (!workspaceEdited) setWorkspacePath(defaultProjectDirectory(defaultRoot, suggested));
  };
  const updateName = (value: string) => {
    setName(value);
    if (!workspaceEdited) setWorkspacePath(defaultProjectDirectory(defaultRoot, value));
  };
  const urlExtension = (() => {
    try {
      const filename = new URL(url).pathname.split("/").at(-1) ?? "";
      // Numeric suffixes are common in document routes (for example arXiv's
      // /pdf/2608.19843) and are not file extensions.
      return filename.match(/\.[A-Za-z][A-Za-z0-9]{0,9}$/)?.[0].toLowerCase();
    } catch {
      return undefined;
    }
  })();
  const selectedExtension = mode === "file"
    ? file?.name.match(/\.[^.]+$/)?.[0]?.toLowerCase()
    : urlExtension;
  const knownConversion = Boolean(selectedExtension && selectedExtension !== ".pdf");
  const formatUnavailable = Boolean(
    selectedExtension
    && capabilities
    && capabilities.supported_extensions.includes(selectedExtension)
    && !capabilities.available_extensions.includes(selectedExtension),
  );
  const unsupportedFormat = Boolean(
    mode === "file"
    && capabilities
    && (!selectedExtension || !capabilities.supported_extensions.includes(selectedExtension)),
  );

  const create = async () => {
    if (busy) return;
    const projectName = name.trim() || "新文档";
    if (!workspacePath.trim()) {
      setError("请输入文档工作目录");
      return;
    }
    if (mode === "file" && !file) {
      setError("请选择一个本地文档");
      return;
    }
    if (file && capabilities && file.size > capabilities.max_bytes) {
      setError(`文件不能超过 ${Math.round(capabilities.max_bytes / 1024 / 1024)} MiB`);
      return;
    }
    if (unsupportedFormat) {
      setError("请选择 PDF、Word、ODT、RTF、PPT、TXT、Markdown 或 HTML 文档");
      return;
    }
    if (mode === "url") {
      try {
        const parsed = new URL(url);
        if (!/^https?:$/.test(parsed.protocol)) throw new Error();
      } catch {
        setError("请输入有效的 HTTP 或 HTTPS 文档地址");
        return;
      }
    }
    if (formatUnavailable) {
      setError("此格式需要 Gateway 安装 LibreOffice 后才能导入");
      return;
    }
    setBusy(true);
    setError(null);
    setStage("upload");
    // PDF URLs are downloaded and copied directly by the Gateway. Only show
    // conversion for an explicitly non-PDF source; extensionless direct URLs
    // (such as arXiv PDF links) stay in download state until the response.
    const stageTimer = knownConversion ? window.setTimeout(() => setStage("convert"), 500) : undefined;
    try {
      const manifest = mode === "file"
        ? await api.uploadDocument({
          workspacePath: workspacePath.trim(),
          projectId,
          projectName,
          file: file!,
        })
        : await api.importDocumentUrl({
          workspacePath: workspacePath.trim(),
          projectId,
          projectName,
          url: url.trim(),
        });
      setStage("parse");
      onSave({
        id: projectId,
        kind: "document",
        path: manifest.workspace,
        name: projectName,
        directories: [manifest.workspace],
        last_session_id: null,
        favorite_session_ids: [],
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setBusy(false);
      setStage(null);
    } finally {
      if (stageTimer !== undefined) window.clearTimeout(stageTimer);
    }
  };

  return (
    <Modal title="创建文档项目" onClose={onClose} wide>
      <div className="document-project-form">
        <label className="project-name-field">
          <FileText />
          <input autoFocus value={name} disabled={busy} placeholder="文档项目名称" onChange={(event) => updateName(event.target.value)} />
        </label>
        <div className="document-source-tabs" role="tablist" aria-label="文档来源">
          <button className={mode === "file" ? "active" : ""} onClick={() => setMode("file")}>本地文件</button>
          <button className={mode === "url" ? "active" : ""} onClick={() => setMode("url")}>网络地址</button>
        </div>
        {mode === "file" ? (
          <label className={`document-file-drop ${file ? "selected" : ""}`}>
            <input
              type="file"
              disabled={busy}
              accept={capabilities?.supported_extensions.join(",")}
              onChange={(event) => {
                const selected = event.target.files?.[0] ?? null;
                setFile(selected);
                if (selected) applySuggestedName(selected.name);
                setError(null);
              }}
            />
            <FileText />
            <strong>{file?.name ?? "选择本地文档"}</strong>
            <small>{file ? `${(file.size / 1024 / 1024).toFixed(1)} MiB` : "PDF、Word、演示文稿及常见文本格式"}</small>
          </label>
        ) : (
          <label className="document-url-field">
            <span>直接文档地址</span>
            <input
              type="url"
              disabled={busy}
              value={url}
              placeholder="https://example.com/document.pdf"
              onChange={(event) => {
                setUrl(event.target.value);
                applySuggestedName(event.target.value);
                setError(null);
              }}
            />
            <small>仅下载直接返回文档文件的 HTTP/HTTPS 地址，不抓取普通网页或网盘分享页。</small>
          </label>
        )}
        <label className="document-workspace-field">
          <span>工作目录</span>
          <input
            value={workspacePath}
            disabled={busy}
            onChange={(event) => {
              setWorkspaceEdited(true);
              setWorkspacePath(event.target.value);
            }}
          />
          <small>原文件、翻译数据和 Blog 会存放在这里；不会修改你选择的源文件。</small>
        </label>
        {capabilities && !capabilities.libreoffice.available && (
          <div className="document-capability-warning"><AlertTriangle />当前 Gateway 未安装 LibreOffice，首版只能导入 PDF。</div>
        )}
        {formatUnavailable && <div className="form-error"><AlertTriangle />此格式需要先安装 LibreOffice。</div>}
        {unsupportedFormat && <div className="form-error"><AlertTriangle />不支持这个文件格式。</div>}
        {stage && (
          <div className="document-import-progress">
            {[
              ["upload", mode === "url" ? "下载" : "导入"],
              ...(knownConversion ? [["convert", "转换为 PDF"]] : []),
              ["parse", "准备文档"],
            ].map(([value, label]) => (
              <span className={stage === value ? "active" : ""} key={value}>{stage === value ? <LoaderCircle className="spin" /> : <Check />}{label}</span>
            ))}
          </div>
        )}
        {error && <div className="form-error"><AlertTriangle />{error}</div>}
      </div>
      <div className="modal-actions">
        <button type="button" disabled={busy} onClick={onClose}>取消</button>
        <button className="primary" type="button" disabled={busy || formatUnavailable || unsupportedFormat} onClick={() => void create()}>
          {busy ? <LoaderCircle className="spin" /> : <FileText />}创建文档
        </button>
      </div>
    </Modal>
  );
}

export function ProjectModal({ api, home, roots, project, projects, protectPrimaryDirectory = false, onClose, onSave, onRemove }: {
  api: GatewayApi;
  home: string;
  roots: string[];
  project: ProjectPreset | null;
  projects: ProjectPreset[];
  protectPrimaryDirectory?: boolean;
  onClose: () => void;
  onSave: (project: ProjectPreset) => void;
  onRemove?: () => void;
}) {
  const editing = project !== null;
  const [name, setName] = useState(project?.name ?? "");
  const [directories, setDirectories] = useState(() => (
    project?.directories.length
      ? project.directories
      : project?.path ? [project.path] : []
  ));
  const [choosingDirectory, setChoosingDirectory] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const suggestedName = directories[0] ? basename(directories[0]) : "新项目";
  const documentProject = project?.kind === "document";
  const protectedPrimaryDirectory = protectPrimaryDirectory || documentProject
    ? project?.directories[0] ?? project?.path ?? null
    : null;

  const save = async () => {
    if (busy) return;
    const nextDirectories = [...new Set(directories)];
    if (editing && nextDirectories.length === 0) {
      setError("请为项目选择一个主目录");
      return;
    }
    if (protectedPrimaryDirectory
      && comparablePath(nextDirectories[0] ?? "") !== comparablePath(protectedPrimaryDirectory)) {
      setError(documentProject ? "文档项目的托管工作目录不能修改" : "默认项目的原始主目录不能移除");
      return;
    }
    const shouldCreateDefaultDirectory = !editing && nextDirectories.length === 0;
    const path = shouldCreateDefaultDirectory
      ? defaultProjectDirectory(home, name)
      : nextDirectories[0];
    const duplicate = projects.find((item) => (
      item.id !== project?.id && comparablePath(item.path) === comparablePath(path)
    ));
    if (duplicate) {
      setError(`“${duplicate.name}”已经使用这个主目录，请选择其他目录`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const primaryPath = shouldCreateDefaultDirectory
        ? (await api.createDirectory(path)).path
        : path;
      const savedDirectories = [primaryPath, ...nextDirectories.slice(1)];
      onSave({
        id: project?.id ?? crypto.randomUUID(),
        kind: project?.kind ?? "project",
        path: primaryPath,
        name: name.trim() || suggestedName,
        directories: savedDirectories,
        is_default: project?.is_default === true,
        last_session_id: project
          && projectPathKey(project.path) === projectPathKey(primaryPath)
          ? project.last_session_id
          : null,
        favorite_session_ids: project
          && projectPathKey(project.path) === projectPathKey(primaryPath)
          ? project.favorite_session_ids ?? []
          : [],
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setBusy(false);
    }
  };
  if (choosingDirectory) {
    return (
      <DirectoryModal
        api={api}
        home={directories.at(-1) ?? home}
        roots={roots}
        title="加入项目目录"
        selectLabel="加入此目录"
        onClose={() => setChoosingDirectory(false)}
        onSelect={(path) => {
          setDirectories((current) => current.some(
            (item) => projectPathKey(item) === projectPathKey(path),
          ) ? current : [...current, path]);
          setError(null);
          setChoosingDirectory(false);
        }}
      />
    );
  }
  return (
    <Modal title={documentProject ? "编辑文档项目" : editing ? "编辑项目" : "新建项目"} onClose={onClose}>
        <div className="project-form">
          <label className="project-name-field">
            <Folder />
            <input
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && void save()}
              placeholder="给项目起个名字"
              disabled={busy}
            />
          </label>
          <section className="project-directories">
            <h3>{editing ? "项目目录" : "项目文件夹（可选）"}</h3>
            <div className={`project-directory-box ${directories.length === 0 ? "empty" : ""}`}>
              {directories.map((path, index) => {
                const primaryDirectoryProtected = Boolean(protectedPrimaryDirectory) && index === 0;
                return (
                <div className="project-directory-row" key={path} title={path}>
                  <Folder />
                  <span><strong>{basename(path)}</strong><small>{path}</small></span>
                  <button
                    className="icon-button small"
                    title={primaryDirectoryProtected
                      ? documentProject ? "文档项目的托管工作目录不能移除" : "默认项目的主目录不能移除"
                      : `移除 ${basename(path)}`}
                    aria-label={`移除目录 ${path}`}
                    disabled={busy || primaryDirectoryProtected}
                    onClick={() => setDirectories((current) => current.filter((item) => item !== path))}
                  >
                    {primaryDirectoryProtected ? <Lock /> : <X />}
                  </button>
                </div>
                );
              })}
              <button className="project-add-directory" type="button" disabled={busy} onClick={() => setChoosingDirectory(true)}>
                <FolderInput />
                <span>{directories.length === 0 ? "选择项目文件夹" : "继续加入目录"}</span>
              </button>
            </div>
            {documentProject && <p>主目录由 CrabCode 托管；在这里可以补充供 Agent 参考的目录。</p>}
            {directories.length === 0 && (
              <p>{editing ? "每个项目都需要一个独立的主目录。" : "不选择时，将在用户主目录下自动创建同名文件夹。"}</p>
            )}
          </section>
          {error && <div className="form-error"><AlertTriangle />{error}</div>}
        </div>
        <div className={`modal-actions project-actions ${editing ? "editing" : ""}`}>
          {editing && onRemove && (
            <button className="danger-button" type="button" onClick={onRemove}>移除项目</button>
          )}
          <span className="project-actions-spacer" />
          <button type="button" disabled={busy} onClick={onClose}>取消</button>
          <button className="primary" type="button" disabled={busy || (editing && directories.length === 0)} onClick={() => void save()}>
            {busy && <LoaderCircle className="spin" />}{editing ? "保存更改" : "创建项目"}
          </button>
        </div>
    </Modal>
  );
}

export function DirectoryModal({
  api,
  home,
  roots,
  title = "选择项目目录",
  selectLabel = "使用此目录",
  allowFiles = false,
  allowDirectorySelection = true,
  onClose,
  onSelect,
}: {
  api: GatewayApi;
  home: string;
  roots: string[];
  title?: string;
  selectLabel?: string;
  allowFiles?: boolean;
  allowDirectorySelection?: boolean;
  onClose: () => void;
  onSelect: (path: string, kind: "file" | "folder") => void;
}) {
  const [path, setPath] = useState(home);
  const [listing, setListing] = useState<WorkspaceDirectoryListing | null>(null);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setListing(null);
    setSelectedFile(null);
    setError(null);
    void api.directories(path, showHidden, allowFiles)
      .then(setListing)
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)));
  }, [allowFiles, api, path, showHidden]);
  const selectedFileEntry = listing?.files?.find((file) => selectedFile !== null
    && projectPathKey(file.path) === projectPathKey(selectedFile)) ?? null;
  return (
    <Modal title={title} onClose={onClose} wide>
      <div className="directory-roots">
        {roots.map((root) => <button key={root} onClick={() => setPath(root)}><Folder />{root}</button>)}
      </div>
      <div className="directory-toolbar">
        <button className="icon-button" title="上一级" disabled={!listing?.parent} onClick={() => listing?.parent && setPath(listing.parent)}><ChevronLeft /></button>
        <input value={path} onChange={(event) => setPath(event.target.value)} onKeyDown={(event) => event.key === "Enter" && setPath(event.currentTarget.value)} />
        <label><input type="checkbox" checked={showHidden} onChange={(event) => setShowHidden(event.target.checked)} />显示隐藏项目</label>
      </div>
      <div className="directory-list">
        {!listing && !error && <div className="directory-loading"><LoaderCircle className="spin" /></div>}
        {error && <div className="form-error"><AlertTriangle />{error}</div>}
        {listing?.directories.map((directory) => (
          <button key={directory.path} onDoubleClick={() => setPath(directory.path)} onClick={() => setPath(directory.path)}>
            <Folder />
            <span>{directory.name}</span>
            {directory.is_symlink && <small>链接</small>}
            <ChevronRight />
          </button>
        ))}
        {allowFiles && (listing?.files ?? []).map((file) => (
          <button
            key={file.path}
            className={selectedFile !== null
              && projectPathKey(selectedFile) === projectPathKey(file.path) ? "selected" : ""}
            onClick={() => setSelectedFile(file.path)}
            onDoubleClick={() => onSelect(file.path, "file")}
          >
            <FileText />
            <span>{file.name}</span>
            <small>{formatFileSize(file.size)}{file.is_symlink ? " · 链接" : ""}</small>
          </button>
        ))}
        {listing && listing.directories.length === 0 && (!allowFiles || (listing.files ?? []).length === 0) && (
          <div className="directory-empty">此目录为空</div>
        )}
      </div>
      <div className="modal-actions">
        <button onClick={onClose}>取消</button>
        <button
          type="button"
          disabled={!selectedFileEntry}
          title={selectedFileEntry ? "取消文件选择并改为引用当前文件夹" : "尚未选择文件"}
          onClick={() => setSelectedFile(null)}
        >取消选择</button>
        <button
          className="primary"
          disabled={!listing || (!selectedFileEntry && !allowDirectorySelection)}
          onClick={() => {
            if (selectedFileEntry) onSelect(selectedFileEntry.path, "file");
            else if (listing && allowDirectorySelection) onSelect(listing.path, "folder");
          }}
        >
          {selectedFileEntry ? <FileText /> : <FolderOpen />}
          {selectedFileEntry ? "引用此文件" : selectLabel}
        </button>
      </div>
    </Modal>
  );
}

function GoalModal({ api, sessionId, onClose }: {
  api: GatewayApi;
  sessionId: string;
  onClose: () => void;
}) {
  const [objective, setObjective] = useState("");
  const [hasGoal, setHasGoal] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void api.goal(sessionId)
      .then(({ goal }) => {
        const editable = Boolean(goal && (goal.status === "active" || goal.status === "paused"));
        setObjective(editable ? goal?.objective ?? "" : "");
        setHasGoal(editable);
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
      .finally(() => setLoading(false));
  }, [api, sessionId]);
  const save = async () => {
    if (!objective.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.manageGoal(sessionId, hasGoal ? "edit" : "set", objective.trim());
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setBusy(false);
    }
  };
  const clear = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.manageGoal(sessionId, "clear");
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setBusy(false);
    }
  };
  return (
    <Modal title="目标" onClose={onClose}>
      <div className="goal-form">
        <label htmlFor="goal-objective">持续追踪的目标</label>
        <textarea
          id="goal-objective"
          autoFocus
          value={objective}
          disabled={loading || busy}
          placeholder="输入当前会话要持续追踪的目标"
          onChange={(event) => setObjective(event.target.value)}
        />
        {error && <div className="form-error"><AlertTriangle />{error}</div>}
      </div>
      <div className="modal-actions">
        {hasGoal && <button type="button" disabled={busy} onClick={() => void clear()}>清除目标</button>}
        <button type="button" disabled={busy} onClick={onClose}>取消</button>
        <button className="primary" disabled={loading || busy || !objective.trim()} onClick={() => void save()}>
          {busy ? <LoaderCircle className="spin" /> : <Target />}{hasGoal ? "更新目标" : "设置目标"}
        </button>
      </div>
    </Modal>
  );
}

function CheckpointModal({ api, sessionId, onClose, onRestored }: {
  api: GatewayApi;
  sessionId: string;
  onClose: () => void;
  onRestored: () => void;
}) {
  const [items, setItems] = useState<CheckpointInfo[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void api.checkpoints(sessionId).then(setItems).catch((reason) => setError(String(reason)));
  }, [api, sessionId]);
  const restore = async (id?: string) => {
    if (!window.confirm(id ? "恢复到这个检查点？文件和会话都将回退。" : "撤销最近一次文件和会话改动？")) return;
    setBusy(true);
    try {
      if (id) await api.revert(sessionId, id);
      else await api.undo(sessionId);
      onRestored();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setBusy(false);
    }
  };
  return (
    <Modal title="检查点与恢复" onClose={onClose}>
      <button className="undo-latest" disabled={busy} onClick={() => void restore()}><RotateCcw />撤销最近检查点</button>
      <div className="checkpoint-list">
        {!items && !error && <LoaderCircle className="spin" />}
        {items?.map((item) => (
          <button key={item.id} disabled={busy} onClick={() => void restore(item.id)}>
            <History />
            <span>
              <strong>{item.label || item.id.slice(0, 8)}</strong>
              <small>{item.snapshot_id ? "文件快照已包含" : "仅保存对话"}</small>
            </span>
            <RotateCcw />
          </button>
        ))}
        {items?.length === 0 && <div className="empty-sidebar">暂无检查点</div>}
        {error && <div className="form-error"><AlertTriangle />{error}</div>}
      </div>
    </Modal>
  );
}

export function ScheduleDeleteModal({ job, busy, error, onClose, onConfirm }: {
  job: ScheduleJobInfo;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return (
    <Modal title="永久删除任务" onClose={onClose}>
      <div className="confirm-dialog-copy">
        <AlertTriangle />
        <div>
          <strong>删除“{job.name}”？</strong>
          <p>这个任务及其运行历史会被永久删除，无法恢复。</p>
        </div>
      </div>
      {error && <div className="form-error"><AlertTriangle />{error}</div>}
      <div className="modal-actions">
        <button type="button" disabled={busy} onClick={onClose}>取消</button>
        <button className="confirm-danger" type="button" disabled={busy} onClick={onConfirm}>
          {busy ? <LoaderCircle className="spin" /> : <Trash2 />}
          永久删除
        </button>
      </div>
    </Modal>
  );
}

export function ProjectDeleteModal({ project, onClose, onConfirm }: {
  project: ProjectPreset;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return (
    <Modal title="删除项目" onClose={onClose}>
      <div className="confirm-dialog-copy">
        <Trash2 />
        <div>
          <strong>从列表中删除“{project.name}”？</strong>
          <p>只会移除项目配置，不会删除项目文件夹、文件或已有会话记录。</p>
        </div>
      </div>
      <div className="modal-actions">
        <button type="button" onClick={onClose}>取消</button>
        <button className="confirm-danger" type="button" onClick={onConfirm}>
          <Trash2 />删除项目
        </button>
      </div>
    </Modal>
  );
}

function Modal({ title, onClose, wide = false, children }: {
  title: string;
  onClose: () => void;
  wide?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className={`modal ${wide ? "wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <header><h2>{title}</h2><button className="icon-button" title="关闭" onClick={onClose}><X /></button></header>
        <div className="modal-body">{children}</div>
      </section>
    </div>
  );
}

export default App;
