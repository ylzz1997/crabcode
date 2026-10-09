import { VirtualMachineSettings, type VmSettingsUpdate } from "./VirtualMachineSettings";
import type { LumeInstallerState } from "./lumeInstaller";
import {
  ArrowLeft,
  ArrowRightLeft,
  Bot,
  ChartNoAxesCombined,
  Check,
  Copy,
  Download,
  FileText,
  FolderCog,
  Image as ImageIcon,
  Info,
  LoaderCircle,
  Minus,
  Pencil,
  Paintbrush,
  Plus,
  RotateCcw,
  ScrollText,
  Search,
  Server,
  Settings,
  SlidersHorizontal,
  Terminal,
  Trash2,
  Upload,
  WifiOff,
  Wrench,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { ModelSettingsPanel } from "./ModelSettingsPanel";
import { PromptSettingsPanel } from "./PromptSettingsPanel";
import { UsageSettingsPanel } from "./UsageSettingsPanel";
import { RuntimeSettingsPanel } from "./RuntimeSettingsPanel";
import { composerModifierLabel } from "./ComposerEditor";
import {
  readNotificationPermission,
  requestSessionNotificationPermission,
  type NotificationPermissionState,
} from "./sessionNotifications";
import { ApprovalShortcutSettings } from "./ApprovalShortcutSettings";
import { ThemeRegistry, resolveActiveTheme } from "./theme";
import {
  parseSkinPackage,
  parseThemeDocument,
  safeThemeFilename,
  serializeSkinPackage,
  serializeThemeDocument,
} from "./themePackages";
import {
  isDesktopShell,
  loadCustomDockIcon,
  saveThemeExport,
  type DocumentEngineInstallProgress,
  GATEWAY_INSTALL_FEATURES,
  installedGatewayFeatures,
  type GatewayInstallFeature,
  type GatewaySuiteInstallProgress,
  type SystemTool,
  type SystemToolInstallProgress,
} from "./native";
import type {
  CodeFontFamily,
  ConnectionPreset,
  ComposerSendKey,
  DesktopSettings,
  DiffMarkerStyle,
  DockIconChoice,
  DocumentCapabilities,
  FileUploadMode,
  GatewayViewState,
  ModelSettingsMutation,
  ModelSettingsResponse,
  PromptSettingsMutation,
  PromptSettingsResponse,
  RuntimeSettingsMutation,
  RuntimeSettingsResponse,
  ProjectPreset,
  ThemeMode,
  ThemePreset,
  ThemeProfile,
  TurnDurationFormat,
  UiFontFamily,
} from "./types";
import desktopPackage from "../package.json";

export type SettingsSectionId = "general" | "appearance" | "document" | "runtime" | "prompts" | "connections" | "models" | "usage" | "projects" | "about";

interface SettingsSectionDefinition {
  id: SettingsSectionId;
  title: string;
  description: string;
  searchText: string;
}

export const SETTINGS_SECTIONS: SettingsSectionDefinition[] = [
  {
    id: "general",
    title: "常规",
    description: "运行环境、文件上传、文件查看与会话设置",
    searchText: "常规 运行环境 Python 路径 自动检测 本地启动 CrabCode 套件 安装 Gateway Search Debugger Browser Playwright Chromium 浏览器工具 网页浏览 Ripgrep rg 语义搜索 文本搜索 调试 浏览器模式 文件 上传 内容 路径 引用 查看 浏览 标签 标签页 最大标签数 最大数量 上限 会话 在会话结尾显示编辑卡片 编辑卡片 文件变更摘要 跟进 处理方式 排队 引导 queue steer 反向发送 显示 处理用时 耗时 仅秒数 时分秒 发送快捷键 Enter 回车 Ctrl Cmd Command Option 权限快捷键 全局 审批 允许 始终允许 拒绝 Windows macOS F9 F10 F11 系统通知 通知 执行时 执行完毕 开始执行 右下角 气泡 通知权限",
  },
  {
    id: "appearance",
    title: "外观",
    description: "主题、颜色、字体与应用图标",
    searchText: "外观 主题 皮肤 预设 导入 导出 复制 重命名 删除 恢复默认 system 跟随系统 浅色 深色 强调色 背景色 前景色 界面字体 代码字体 半透明侧栏 对比度 指针光标 字号 Diff 标记 加号 减号 字体平滑 Dock 图标 螃蟹 自定义 上传",
  },
  {
    id: "document",
    title: "文档",
    description: "文档翻译请求与批处理设置",
    searchText: "文档 翻译 原文 显示原文 复制 并行请求 并行 请求 批次 Block 数 单次请求 批大小",
  },
  {
    id: "runtime",
    title: "运行与工具",
    description: "上下文压缩、Computer Use、文件快照与额外工具配置",
    searchText: "运行 上下文 压缩 自动 预留 token compact buffer 阈值 Computer Use 虚拟机 Lume VM 共享目录 电脑 后台应用 前台桌面 background foreground 快照 文件快照 checkpoint 检查点 snapshot 最大大小 启用 额外工具 extra tools import path 工具",
  },
  {
    id: "prompts",
    title: "提示词",
    description: "自定义系统与上下文压缩提示词模版，以及追加到用户输入的提示",
    searchText: "提示词 模版 模板 prompt 自定义 默认 系统提示词 上下文 压缩 compact 用户输入 追加 用户提示 规则 留空 导入 导出 JSON",
  },
  {
    id: "connections",
    title: "Gateway 连接",
    description: "连接地址、凭据与当前 Gateway",
    searchText: "Gateway 连接 地址 密码 凭据 远程 本地 当前连接 新建 编辑 删除",
  },
  {
    id: "models",
    title: "模型",
    description: "查看并编辑模型、配置组与最终生效参数",
    searchText: "模型 Models Group 配置组 Provider Base URL 推理 Thinking Token 上下文 继承 默认模型 查看 编辑 新增 删除 查询 刷新",
  },
  {
    id: "usage",
    title: "使用情况",
    description: "按日期查看 Token 总量和各模型趋势",
    searchText: "使用情况 Token 用量 热力图 折线图 模型 日期 统计",
  },
  {
    id: "projects",
    title: "项目",
    description: "工作目录与项目管理",
    searchText: "项目 项目目录 工作目录 文件夹 当前项目 新建 编辑 移除",
  },
  {
    id: "about",
    title: "关于",
    description: "Crab Desktop 版本与作者信息",
    searchText: "关于 版本 Crab Desktop Gateway 协议 作者 Yuri Head",
  },
];

const GATEWAY_FEATURE_DETAILS: Record<GatewayInstallFeature, { title: string; detail: string }> = {
  search: { title: "Search", detail: "语义代码搜索 · 依赖体积较大" },
  debugger: { title: "Debugger", detail: "DAP 与进程级调试" },
  browser: { title: "Browser", detail: "网页浏览 · 安装 Playwright 与 Chromium" },
};

export function filterSettingsSections(query: string): SettingsSectionDefinition[] {
  const normalized = query.trim().toLocaleLowerCase("zh-CN");
  if (!normalized) return SETTINGS_SECTIONS;
  return SETTINGS_SECTIONS.filter((section) => (
    `${section.title} ${section.description} ${section.searchText}`
      .toLocaleLowerCase("zh-CN")
      .includes(normalized)
  ));
}

const SECTION_ICONS = {
  general: SlidersHorizontal,
  appearance: Paintbrush,
  document: FileText,
  runtime: Wrench,
  prompts: ScrollText,
  connections: Server,
  models: Bot,
  usage: ChartNoAxesCombined,
  projects: FolderCog,
  about: Info,
} satisfies Record<SettingsSectionId, typeof Settings>;

const DARK_DOCK_ICON = new URL("../src-tauri/icons/icon.png", import.meta.url).href;
const LIGHT_DOCK_ICON = new URL("../src-tauri/resources/dock-icon-light.png", import.meta.url).href;
const DOCK_ICON_SIZE = 512;
const DOCK_ICON_CORNER_RADIUS = 112;

function clipDockIconShape(context: CanvasRenderingContext2D): void {
  const size = DOCK_ICON_SIZE;
  const radius = DOCK_ICON_CORNER_RADIUS;
  context.beginPath();
  context.moveTo(radius, 0);
  context.lineTo(size - radius, 0);
  context.quadraticCurveTo(size, 0, size, radius);
  context.lineTo(size, size - radius);
  context.quadraticCurveTo(size, size, size - radius, size);
  context.lineTo(radius, size);
  context.quadraticCurveTo(0, size, 0, size - radius);
  context.lineTo(0, radius);
  context.quadraticCurveTo(0, 0, radius, 0);
  context.closePath();
  context.clip();
}

async function normalizeDockIcon(file: File): Promise<{ bytes: Uint8Array; preview: string }> {
  const sourceUrl = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.src = sourceUrl;
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve();
      image.onerror = () => reject(new Error("无法读取这张图片"));
    });
    const canvas = document.createElement("canvas");
    canvas.width = DOCK_ICON_SIZE;
    canvas.height = DOCK_ICON_SIZE;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("无法处理这张图片");
    context.clearRect(0, 0, DOCK_ICON_SIZE, DOCK_ICON_SIZE);
    context.save();
    clipDockIconShape(context);
    const scale = Math.min(DOCK_ICON_SIZE / image.naturalWidth, DOCK_ICON_SIZE / image.naturalHeight);
    const width = Math.round(image.naturalWidth * scale);
    const height = Math.round(image.naturalHeight * scale);
    context.drawImage(
      image,
      (DOCK_ICON_SIZE - width) / 2,
      (DOCK_ICON_SIZE - height) / 2,
      width,
      height,
    );
    context.restore();
    const blob = await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob((value) => value ? resolve(value) : reject(new Error("无法生成 PNG 图标")), "image/png");
    });
    return {
      bytes: new Uint8Array(await blob.arrayBuffer()),
      preview: canvas.toDataURL("image/png"),
    };
  } finally {
    URL.revokeObjectURL(sourceUrl);
  }
}

function imageBytesToDataUrl(bytes: Uint8Array): Promise<string> {
  return new Promise((resolve, reject) => {
    const copy = new Uint8Array(bytes.byteLength);
    copy.set(bytes);
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("无法读取自定义图标"));
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.readAsDataURL(new Blob([copy.buffer], { type: "image/png" }));
  });
}

function connectionStatusLabel(status: GatewayViewState["status"] | undefined): string {
  if (status === "online") return "在线";
  if (status === "connecting") return "正在连接";
  if (status === "error") return "连接异常";
  return "离线";
}

function projectDirectorySummary(project: ProjectPreset): string {
  if (project.directories.length === 0) return "使用用户主目录";
  if (project.directories.length === 1) return project.directories[0];
  return `${project.directories.length} 个目录 · ${project.directories[0]}`;
}

interface SettingsViewProps {
  onVmSettingsChange?: (changes: VmSettingsUpdate) => void;
  onVmRefresh?: () => void;
  computerTaskBusy?: boolean;
  lumeInstaller?: LumeInstallerState;
  settings: DesktopSettings;
  gateways: Record<string, GatewayViewState>;
  activeConnection: ConnectionPreset | null;
  activeProject: ProjectPreset | null;
  activeSection: SettingsSectionId;
  onSectionChange: (section: SettingsSectionId) => void;
  onBack: () => void;
  onSavePythonPath: (path: string) => void;
  gatewaySuiteBusy?: boolean;
  gatewaySuiteProgress?: GatewaySuiteInstallProgress | null;
  gatewaySuiteError?: string | null;
  gatewaySuiteSuccess?: string | null;
  onInstallGatewaySuite?: (features: readonly GatewayInstallFeature[], pythonPath: string | null) => Promise<void>;
  systemToolBusy?: SystemTool | null;
  systemToolProgress?: SystemToolInstallProgress | null;
  systemToolError?: string | null;
  systemToolSuccess?: string | null;
  onInstallSystemTool?: (tool: SystemTool, pythonPath: string | null) => Promise<void>;
  onConversationChange: (changes: ConversationSettingsUpdate) => void;
  approvalShortcutError?: string | null;
  onDocumentChange: (changes: DocumentSettingsUpdate) => void;
  onThemeModeChange: (mode: ThemeMode) => void;
  onThemeProfileChange: (scheme: "light" | "dark", changes: Partial<ThemeProfile>) => void;
  onThemePresetChange: (id: string) => void;
  onThemeDuplicate: (id: string) => void;
  onThemeRename: (id: string, name: string) => void;
  onThemeDelete: (id: string) => void;
  onThemeRestoreDefault: () => void;
  onThemeImport: (theme: ThemePreset) => void;
  onThemeImportFailure: () => void;
  onAppearanceChange: (changes: AppearanceSettingsUpdate) => void;
  onDockIconChange: (choice: DockIconChoice, pngBytes?: Uint8Array) => Promise<void>;
  onActivateConnection: (id: string) => void;
  onNewConnection: () => void;
  onEditConnection: (id: string) => void;
  onDeleteConnection: (id: string) => void;
  modelSettings?: ModelSettingsResponse | null;
  modelSettingsLoading?: boolean;
  modelSettingsError?: string | null;
  onRefreshModelSettings?: () => void;
  onMutateModelSettings?: (mutation: ModelSettingsMutation) => Promise<void>;
  onTestModel?: (name: string) => Promise<{ ok: boolean; message: string; elapsed_ms?: number }>;
  runtimeSettings?: RuntimeSettingsResponse | null;
  runtimeSettingsLoading?: boolean;
  runtimeSettingsError?: string | null;
  onRefreshRuntimeSettings?: () => void;
  onMutateRuntimeSettings?: (mutation: RuntimeSettingsMutation) => Promise<void>;
  promptSettings?: PromptSettingsResponse | null;
  promptSettingsLoading?: boolean;
  promptSettingsError?: string | null;
  onRefreshPromptSettings?: () => void;
  onMutatePromptSettings?: (mutation: PromptSettingsMutation) => Promise<void>;
  onNewProject: () => void;
  onEditProject: (project: ProjectPreset) => void;
  onDocumentWorkspaceRoot?: (connectionId: string, path: string | null) => void;
  documentCapabilities?: DocumentCapabilities | null;
  canManageDocumentEngine?: boolean;
  documentEngineBusy?: "install" | "remove" | null;
  documentEngineProgress?: DocumentEngineInstallProgress | null;
  documentEngineError?: string | null;
  onInstallDocumentEngine?: () => Promise<void>;
  onRemoveDocumentEngine?: () => Promise<void>;
}

export type AppearanceSettingsUpdate = Partial<Pick<DesktopSettings,
  | "pointer_cursor"
  | "ui_font_size"
  | "code_font_size"
  | "diff_marker_style"
  | "font_smoothing"
>>;

export type ConversationSettingsUpdate = Partial<Pick<DesktopSettings,
  | "show_turn_duration"
  | "show_file_edit_summary"
  | "turn_duration_format"
  | "session_notify_on_start"
  | "session_notify_on_complete"
  | "session_notify_on_interaction"
  | "composer_send_key"
  | "follow_up_mode"
  | "approval_shortcuts"
  | "file_upload_mode"
  | "file_upload_max_size_mb"
  | "project_files_max_tabs"
>>;

export type DocumentSettingsUpdate = Partial<Pick<DesktopSettings,
  | "document_show_original_text"
  | "document_translation_concurrency"
  | "document_translation_batch_size"
>>;

function NumberSettingInput({
  label,
  value,
  minimum,
  maximum,
  step,
  onChange,
}: {
  label: string;
  value: number;
  minimum: number;
  maximum: number;
  step: number;
  onChange: (value: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));

  useEffect(() => setDraft(String(value)), [value]);

  const commit = () => {
    const parsed = Number(draft);
    const next = Number.isFinite(parsed)
      ? Math.min(maximum, Math.max(minimum, Math.round(parsed)))
      : value;
    setDraft(String(next));
    if (next !== value) onChange(next);
  };

  return (
    <input
      className="settings-number-input"
      aria-label={label}
      type="number"
      min={minimum}
      max={maximum}
      step={step}
      value={draft}
      onBlur={commit}
      onChange={(event) => setDraft(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter") event.currentTarget.blur();
        if (event.key === "Escape") setDraft(String(value));
      }}
    />
  );
}

interface ThemeColorRowProps {
  schemeLabel: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
}

function ThemeColorRow({ schemeLabel, label, value, onChange }: ThemeColorRowProps) {
  return (
    <div className="theme-profile-row">
      <strong>{label}</strong>
      <div className="settings-color-control">
        <label title={`选择${label}`}>
          <input
            type="color"
            aria-label={`${schemeLabel}${label}`}
            value={value}
            onChange={(event) => onChange(event.target.value)}
          />
          <span className="settings-color-swatch" style={{ backgroundColor: value }} />
          <code>{value.toUpperCase()}</code>
        </label>
      </div>
    </div>
  );
}

function ThemeProfileEditor({
  scheme,
  profile,
  onChange,
}: {
  scheme: "light" | "dark";
  profile: ThemeProfile;
  onChange: (changes: Partial<ThemeProfile>) => void;
}) {
  const schemeLabel = scheme === "light" ? "浅色主题" : "深色主题";
  return (
    <section className={`theme-profile-card ${scheme}`} aria-label={schemeLabel}>
      <header>
        <strong>{schemeLabel}</strong>
      </header>
      <ThemeColorRow
        schemeLabel={schemeLabel}
        label="强调色"
        value={profile.accent_color}
        onChange={(value) => onChange({ accent_color: value })}
      />
      <ThemeColorRow
        schemeLabel={schemeLabel}
        label="背景"
        value={profile.background_color}
        onChange={(value) => onChange({ background_color: value })}
      />
      <ThemeColorRow
        schemeLabel={schemeLabel}
        label="前景"
        value={profile.foreground_color}
        onChange={(value) => onChange({ foreground_color: value })}
      />
      <div className="theme-profile-row">
        <strong>UI 字体</strong>
        <select
          className="settings-select-control"
          aria-label={`${schemeLabel}UI 字体`}
          value={profile.ui_font_family}
          onChange={(event) => onChange({ ui_font_family: event.target.value as UiFontFamily })}
        >
          <option value="system">系统默认</option>
          <option value="inter">Inter</option>
          <option value="serif">衬线字体</option>
        </select>
      </div>
      <div className="theme-profile-row">
        <strong>代码字体</strong>
        <select
          className="settings-select-control"
          aria-label={`${schemeLabel}代码字体`}
          value={profile.code_font_family}
          onChange={(event) => onChange({ code_font_family: event.target.value as CodeFontFamily })}
        >
          <option value="system-mono">系统默认</option>
          <option value="menlo">Menlo</option>
          <option value="monaco">Monaco</option>
        </select>
      </div>
      <div className="theme-profile-row">
        <strong>半透明侧栏</strong>
        <button
          className={`settings-switch ${profile.translucent_sidebar ? "on" : ""}`}
          type="button"
          role="switch"
          aria-checked={profile.translucent_sidebar}
          aria-label={`${schemeLabel}半透明侧栏`}
          onClick={() => onChange({ translucent_sidebar: !profile.translucent_sidebar })}
        ><span /></button>
      </div>
      <div className="theme-profile-row">
        <strong>对比度</strong>
        <div className="settings-range-control">
          <input
            type="range"
            min="0"
            max="100"
            step="1"
            aria-label={`${schemeLabel}对比度`}
            value={profile.contrast}
            onChange={(event) => onChange({ contrast: Number(event.target.value) })}
          />
          <output>{profile.contrast}</output>
        </div>
      </div>
      <div className="theme-profile-row">
        <strong>圆角比例</strong>
        <div className="settings-range-control">
          <input
            type="range"
            min="0.5"
            max="1.75"
            step="0.05"
            aria-label={`${schemeLabel}圆角比例`}
            value={profile.radius_scale}
            onChange={(event) => onChange({ radius_scale: Number(event.target.value) })}
          />
          <output>{Math.round(profile.radius_scale * 100)}%</output>
        </div>
      </div>
      <div className="theme-profile-row">
        <strong>阴影强度</strong>
        <div className="settings-range-control">
          <input
            type="range"
            min="0"
            max="100"
            step="1"
            aria-label={`${schemeLabel}阴影强度`}
            value={profile.shadow_strength}
            onChange={(event) => onChange({ shadow_strength: Number(event.target.value) })}
          />
          <output>{profile.shadow_strength}</output>
        </div>
      </div>
    </section>
  );
}

export function SettingsView({
  settings,
  gateways,
  activeConnection,
  activeProject,
  activeSection,
  onSectionChange,
  onBack,
  onSavePythonPath,
  gatewaySuiteBusy = false,
  gatewaySuiteProgress = null,
  gatewaySuiteError = null,
  gatewaySuiteSuccess = null,
  onInstallGatewaySuite,
  systemToolBusy = null,
  systemToolProgress = null,
  systemToolError = null,
  systemToolSuccess = null,
  onInstallSystemTool,
  onVmSettingsChange,
  onVmRefresh,
  computerTaskBusy,
  lumeInstaller,
  onConversationChange,
  approvalShortcutError,
  onDocumentChange,
  onThemeModeChange,
  onThemeProfileChange,
  onThemePresetChange,
  onThemeDuplicate,
  onThemeRename,
  onThemeDelete,
  onThemeRestoreDefault,
  onThemeImport,
  onThemeImportFailure,
  onAppearanceChange,
  onDockIconChange,
  onActivateConnection,
  onNewConnection,
  onEditConnection,
  onDeleteConnection,
  modelSettings = null,
  modelSettingsLoading = false,
  modelSettingsError = null,
  onRefreshModelSettings = () => {},
  onMutateModelSettings,
  onTestModel,
  runtimeSettings = null,
  runtimeSettingsLoading = false,
  runtimeSettingsError = null,
  onRefreshRuntimeSettings = () => {},
  onMutateRuntimeSettings,
  promptSettings = null,
  promptSettingsLoading = false,
  promptSettingsError = null,
  onRefreshPromptSettings = () => {},
  onMutatePromptSettings,
  onNewProject,
  onEditProject,
  onDocumentWorkspaceRoot,
  documentCapabilities,
  canManageDocumentEngine,
  documentEngineBusy = null,
  documentEngineProgress = null,
  documentEngineError = null,
  onInstallDocumentEngine = async () => undefined,
  onRemoveDocumentEngine = async () => undefined,
}: SettingsViewProps) {
  const [query, setQuery] = useState("");
  const [pythonPath, setPythonPath] = useState(settings.python_path ?? "");
  const [gatewayFeatures, setGatewayFeatures] = useState<GatewayInstallFeature[]>(["search"]);
  const [installedFeatures, setInstalledFeatures] = useState<GatewayInstallFeature[] | null>(null);
  const [gatewayFeatureError, setGatewayFeatureError] = useState<string | null>(null);
  const [gatewayFeatureRetry, setGatewayFeatureRetry] = useState(0);
  const [customIconPreview, setCustomIconPreview] = useState<string | null>(null);
  const [dockIconBusy, setDockIconBusy] = useState(false);
  const [appearanceError, setAppearanceError] = useState<string | null>(null);
  const [themeTransferMessage, setThemeTransferMessage] = useState<string | null>(null);
  const [renamingThemeId, setRenamingThemeId] = useState<string | null>(null);
  const [themeNameDraft, setThemeNameDraft] = useState("");
  const [deletingThemeId, setDeletingThemeId] = useState<string | null>(null);
  const [notificationPermission, setNotificationPermission] = useState<NotificationPermissionState>("unknown");
  const customIconInputRef = useRef<HTMLInputElement>(null);
  const themeImportInputRef = useRef<HTMLInputElement>(null);
  const matchingSections = useMemo(() => filterSettingsSections(query), [query]);
  const themeRegistry = useMemo(() => new ThemeRegistry(settings.custom_theme_presets), [settings.custom_theme_presets]);
  const themePresets = useMemo(() => themeRegistry.list(), [themeRegistry]);
  const activeTheme = useMemo(() => resolveActiveTheme(settings), [settings]);
  const activeThemeIsBuiltin = themeRegistry.isBuiltin(activeTheme.id);
  const activeGateway = activeConnection ? gateways[activeConnection.id] : null;
  const canManageProjects = activeGateway?.status === "online" && Boolean(activeGateway.workspace);
  const gatewayFeaturesLoading = installedFeatures === null && gatewayFeatureError === null;
  const gatewayFeaturesLocked = installedFeatures === null || gatewaySuiteBusy || systemToolBusy !== null;
  const selectedGatewayFeatures = GATEWAY_INSTALL_FEATURES.filter((feature) => (
    installedFeatures?.includes(feature) || gatewayFeatures.includes(feature)
  ));

  useEffect(() => {
    setPythonPath(settings.python_path ?? "");
  }, [settings.python_path]);

  useEffect(() => {
    if (activeSection !== "general" || !isDesktopShell()) return;
    let cancelled = false;
    setInstalledFeatures(null);
    setGatewayFeatureError(null);
    void installedGatewayFeatures(settings.python_path)
      .then((features) => {
        if (!cancelled) setInstalledFeatures(features);
      })
      .catch((error: unknown) => {
        if (!cancelled) setGatewayFeatureError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
    };
  }, [activeSection, settings.python_path, gatewaySuiteSuccess, gatewayFeatureRetry]);

  useEffect(() => {
    if (activeSection !== "general" || !isDesktopShell()) return;
    let cancelled = false;
    void readNotificationPermission().then((state) => {
      if (!cancelled) setNotificationPermission(state);
    });
    return () => {
      cancelled = true;
    };
  }, [activeSection]);

  useEffect(() => {
    if (settings.dock_icon !== "custom" || !isDesktopShell()) return;
    let cancelled = false;
    void loadCustomDockIcon()
      .then(async (bytes) => bytes ? imageBytesToDataUrl(bytes) : null)
      .then((preview) => {
        if (!cancelled && preview) setCustomIconPreview(preview);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [settings.dock_icon]);

  useEffect(() => {
    if (matchingSections.length > 0 && !matchingSections.some((section) => section.id === activeSection)) {
      onSectionChange(matchingSections[0].id);
    }
  }, [activeSection, matchingSections, onSectionChange]);

  const savePythonPath = () => {
    const normalized = pythonPath.trim();
    setPythonPath(normalized);
    if (normalized !== (settings.python_path ?? "")) onSavePythonPath(normalized);
  };

  const installSelectedGatewaySuite = async () => {
    if (!onInstallGatewaySuite || gatewayFeaturesLocked) return;
    const normalizedPythonPath = pythonPath.trim();
    setPythonPath(normalizedPythonPath);
    if (normalizedPythonPath !== (settings.python_path ?? "")) {
      onSavePythonPath(normalizedPythonPath);
    }
    try {
      await onInstallGatewaySuite(selectedGatewayFeatures, normalizedPythonPath || null);
    } catch {
      // The application-level task owner preserves and displays the error.
    }
  };

  const installSelectedSystemTool = async (tool: SystemTool) => {
    if (!onInstallSystemTool) return;
    const normalizedPythonPath = pythonPath.trim();
    setPythonPath(normalizedPythonPath);
    if (normalizedPythonPath !== (settings.python_path ?? "")) {
      onSavePythonPath(normalizedPythonPath);
    }
    try {
      await onInstallSystemTool(tool, normalizedPythonPath || null);
    } catch {
      // The application-level task owner preserves and displays the error.
    }
  };

  const setGatewayFeatureSelected = (feature: GatewayInstallFeature, selected: boolean) => {
    if (gatewayFeaturesLocked || installedFeatures?.includes(feature)) return;
    setGatewayFeatures((current) => GATEWAY_INSTALL_FEATURES.filter((candidate) => (
      candidate === feature ? selected : current.includes(candidate)
    )));
  };

  const activeDefinition = SETTINGS_SECTIONS.find((section) => section.id === activeSection)!;
  const preciseEngine = documentCapabilities?.translation_engines?.precise;
  const documentEngineLoading = documentCapabilities === undefined && activeGateway?.status === "online";
  const documentEngineInstallCommand = preciseEngine?.install_command ?? "crabcode document-engine install";

  const beginThemeRename = (theme: ThemePreset) => {
    setDeletingThemeId(null);
    setRenamingThemeId(theme.id);
    setThemeNameDraft(theme.name);
  };

  const finishThemeRename = (theme: ThemePreset) => {
    const name = themeNameDraft.trim();
    if (name && name !== theme.name) onThemeRename(theme.id, name);
    setRenamingThemeId(null);
    setThemeNameDraft("");
  };

  const beginThemeDelete = (theme: ThemePreset) => {
    setRenamingThemeId(null);
    setDeletingThemeId(theme.id);
  };

  const manageDocumentEngine = async (action: "install" | "remove") => {
    try {
      if (action === "install") await onInstallDocumentEngine();
      else await onRemoveDocumentEngine();
    } catch {
      // The application-level task owner preserves and displays the error.
    }
  };

  const selectDockIcon = async (choice: DockIconChoice, pngBytes?: Uint8Array) => {
    setDockIconBusy(true);
    setAppearanceError(null);
    try {
      await onDockIconChange(choice, pngBytes);
    } catch (error) {
      setAppearanceError(error instanceof Error ? error.message : String(error));
    } finally {
      setDockIconBusy(false);
    }
  };

  const uploadCustomIcon = async (file: File) => {
    if (!file.type.startsWith("image/")) {
      setAppearanceError("请选择 PNG、JPEG 或 WebP 图片");
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      setAppearanceError("自定义图标不能超过 10MB");
      return;
    }
    try {
      const { bytes, preview } = await normalizeDockIcon(file);
      setCustomIconPreview(preview);
      await selectDockIcon("custom", bytes);
    } catch (error) {
      setAppearanceError(error instanceof Error ? error.message : String(error));
    }
  };

  const downloadThemeFile = async (bytes: Uint8Array | string, filename: string, type: string): Promise<string | null> => {
    const payload = typeof bytes === "string" ? new TextEncoder().encode(bytes) : new Uint8Array(bytes);
    const nativePath = await saveThemeExport(filename, payload);
    if (nativePath) return nativePath;
    const blob = new Blob([payload], { type });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
    return null;
  };

  const exportTheme = async () => {
    setAppearanceError(null);
    setThemeTransferMessage(null);
    try {
      const path = await downloadThemeFile(
        serializeThemeDocument(activeTheme),
        `${safeThemeFilename(activeTheme.name)}.crabtheme.json`,
        "application/json",
      );
      setThemeTransferMessage(path ? `已导出到 ${path}` : `已导出 ${activeTheme.name} 的主题数据。`);
    } catch (error) {
      setAppearanceError(error instanceof Error ? error.message : String(error));
    }
  };

  const exportSkin = async () => {
    setAppearanceError(null);
    setThemeTransferMessage(null);
    try {
      const path = await downloadThemeFile(
        serializeSkinPackage(activeTheme),
        `${safeThemeFilename(activeTheme.name)}.crabskin`,
        "application/vnd.crabcode.skin+zip",
      );
      setThemeTransferMessage(path ? `已导出到 ${path}` : `已导出 ${activeTheme.name} 的完整皮肤包。`);
    } catch (error) {
      setAppearanceError(error instanceof Error ? error.message : String(error));
    }
  };

  const importThemeFile = async (file: File) => {
    setAppearanceError(null);
    setThemeTransferMessage(null);
    try {
      const imported = file.name.toLowerCase().endsWith(".crabskin")
        ? parseSkinPackage(new Uint8Array(await file.arrayBuffer()))
        : parseThemeDocument(await file.text());
      onThemeImport(imported);
      setThemeTransferMessage(`已导入并启用 ${imported.name}。`);
    } catch (error) {
      onThemeImportFailure();
      setAppearanceError(`${error instanceof Error ? error.message : String(error)}；已回退到 Crab 默认皮肤。`);
    }
  };

  return (
    <div className="settings-shell">
      <aside className="settings-sidebar" aria-label="设置导航">
        <button className="settings-back" type="button" onClick={onBack}>
          <ArrowLeft />
          <span>返回工作台</span>
        </button>

        <label className="settings-search">
          <Search />
          <input
            aria-label="搜索设置"
            value={query}
            placeholder="搜索设置"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>

        <div className="settings-nav-heading">设置</div>
        <nav className="settings-nav" aria-label="设置分类">
          {matchingSections.map((section) => {
            const Icon = SECTION_ICONS[section.id];
            return (
              <button
                key={section.id}
                className={activeSection === section.id ? "active" : ""}
                type="button"
                aria-current={activeSection === section.id ? "page" : undefined}
                onClick={() => onSectionChange(section.id)}
              >
                <Icon />
                <span>{section.title}</span>
              </button>
            );
          })}
        </nav>

        {matchingSections.length === 0 && (
          <div className="settings-nav-empty">未找到相关设置</div>
        )}

        <div className="settings-sidebar-brand">
          <span><Settings /></span>
          <div><strong>Crab Desktop</strong><small>SETTINGS</small></div>
        </div>
      </aside>

      <main className="settings-main">
        {matchingSections.length === 0 ? (
          <div className="settings-empty-state">
            <Search />
            <h1>未找到设置</h1>
            <p>试试搜索“连接”、“Python”或“项目”。</p>
          </div>
        ) : (
          <div className="settings-content">
            <header className="settings-page-header">
              <h1>{activeDefinition.title}</h1>
              <p>{activeDefinition.description}</p>
            </header>

            {activeSection === "general" && (
              <section className="settings-section" aria-labelledby="runtime-settings-title">
                <h2 id="runtime-settings-title">运行环境</h2>
                <div className="settings-group gateway-environment-group">
                  {isDesktopShell() ? (
                    <>
                      <div className="settings-row">
                        <div className="settings-row-copy">
                          <strong>Python 路径</strong>
                          <span>用于自动安装和启动本地 Gateway，留空时自动检测 python3 或 python。</span>
                        </div>
                        <div className="settings-input-control">
                          <input
                            aria-label="Python 路径"
                            value={pythonPath}
                            placeholder="自动检测 python3 / python"
                            onBlur={savePythonPath}
                            onChange={(event) => setPythonPath(event.target.value)}
                            onKeyDown={(event) => {
                              if (event.key === "Enter") event.currentTarget.blur();
                            }}
                          />
                          {pythonPath && (
                            <button
                              className="icon-button small"
                              type="button"
                              title="恢复自动检测"
                              aria-label="恢复自动检测"
                              onClick={() => {
                                setPythonPath("");
                                onSavePythonPath("");
                              }}
                            >
                              <RotateCcw />
                            </button>
                          )}
                        </div>
                      </div>
                      <div className="settings-row gateway-suite-row">
                        <div className="settings-row-copy">
                          <strong>CrabCode 套件</strong>
                          <span>首次启动会安装 Gateway、Browser 和 Chromium。这里可按需再安装 Search、Debugger 和 Browser；装完后需重启本地 Gateway。</span>
                        </div>
                        <div className="gateway-suite-install">
                          {gatewayFeaturesLoading && (
                            <div className="settings-loading-status" role="status">
                              <LoaderCircle className="spin" aria-hidden="true" />
                              <span>正在检测已安装的组件，请稍候…</span>
                            </div>
                          )}
                          <div className="gateway-feature-list" role="group" aria-label="CrabCode 安装组件" aria-busy={gatewayFeaturesLoading}>
                            <label className="gateway-feature-option is-required">
                              <input type="checkbox" checked disabled readOnly />
                              <span><strong>Gateway</strong><small>必装 · 本地服务与客户端协议</small></span>
                            </label>
                            {GATEWAY_INSTALL_FEATURES.map((feature) => {
                              const installed = installedFeatures?.includes(feature) ?? false;
                              const meta = GATEWAY_FEATURE_DETAILS[feature];
                              return (
                                <label key={feature} className={`gateway-feature-option${installed ? " is-installed" : ""}${gatewayFeaturesLocked ? " is-disabled" : ""}`}>
                                  <input
                                    type="checkbox"
                                    aria-label={meta.title}
                                    checked={selectedGatewayFeatures.includes(feature)}
                                    disabled={installed || gatewayFeaturesLocked}
                                    onChange={(event) => setGatewayFeatureSelected(feature, event.target.checked)}
                                  />
                                  <span>
                                    <strong>{meta.title}</strong>
                                    <small>{installed ? `已安装 · ${meta.detail}` : meta.detail}</small>
                                  </span>
                                </label>
                              );
                            })}
                          </div>
                          {gatewayFeatureError !== null && (
                            <small className="gateway-suite-error" role="alert" title={gatewayFeatureError}>
                              组件检测失败：{gatewayFeatureError}
                            </small>
                          )}
                          <div className="gateway-suite-actions">
                            {gatewayFeatureError !== null && (
                              <button
                                className="settings-command"
                                type="button"
                                disabled={gatewaySuiteBusy || systemToolBusy !== null}
                                onClick={() => setGatewayFeatureRetry((current) => current + 1)}
                              ><RotateCcw /><span>重新检测</span></button>
                            )}
                            <button
                              className="settings-command primary"
                              type="button"
                              disabled={gatewayFeaturesLocked || !onInstallGatewaySuite}
                              onClick={() => void installSelectedGatewaySuite()}
                            >
                              {gatewaySuiteBusy || gatewayFeaturesLoading ? <LoaderCircle className="spin" /> : <Download />}
                              <span>{gatewaySuiteBusy ? "正在安装" : gatewayFeaturesLoading ? "正在检测" : "安装套件"}</span>
                            </button>
                          </div>
                          {gatewaySuiteBusy && gatewaySuiteProgress && (
                            <small className="gateway-suite-progress" role="status" title={gatewaySuiteProgress.detail}>
                              {gatewaySuiteProgress.detail}
                            </small>
                          )}
                          {gatewaySuiteError && <small className="gateway-suite-error" role="alert">{gatewaySuiteError}</small>}
                          {gatewaySuiteSuccess && <small className="gateway-suite-success" role="status">{gatewaySuiteSuccess}</small>}
                        </div>
                      </div>
                    </>
                  ) : (
                    <div className="settings-row">
                      <div className="settings-row-copy">
                        <strong>运行方式</strong>
                        <span>浏览器版连接已经运行的 Gateway，不负责本地安装和启动。</span>
                      </div>
                      <span className="settings-value">浏览器模式</span>
                    </div>
                  )}
                </div>

                {isDesktopShell() && (
                  <>
                    <div className="settings-section-heading general-spaced-heading">
                      <div><h2>系统工具</h2><p>检测并安装本地 Gateway 使用的命令行工具。</p></div>
                    </div>
                    <div className="settings-group">
                      <div className="settings-row">
                        <div className="settings-row-copy">
                          <strong>Ripgrep (rg)</strong>
                          <span>内置 Grep 会优先使用 ripgrep 进行快速文本搜索；已安装的 rg 会直接复用。</span>
                        </div>
                        <div className="system-tool-install">
                          <button
                            className="settings-command primary"
                            type="button"
                            aria-label="安装 Ripgrep"
                            disabled={systemToolBusy !== null || gatewaySuiteBusy || !onInstallSystemTool}
                            onClick={() => void installSelectedSystemTool("ripgrep")}
                          >
                            {systemToolBusy === "ripgrep" ? <LoaderCircle className="spin" /> : <Download />}
                            <span>{systemToolBusy === "ripgrep" ? "正在检测与安装" : "检测并安装"}</span>
                          </button>
                          {systemToolBusy === "ripgrep" && systemToolProgress && (
                            <small className="system-tool-progress" role="status" title={systemToolProgress.detail}>
                              {systemToolProgress.detail}
                            </small>
                          )}
                          {systemToolError && <small className="system-tool-error" role="alert">{systemToolError}</small>}
                          {systemToolSuccess && <small className="system-tool-success" role="status">{systemToolSuccess}</small>}
                        </div>
                      </div>
                    </div>
                  </>
                )}

                <div className="settings-section-heading general-spaced-heading">
                  <div><h2>文件上传</h2><p>控制添加文件时发送完整内容还是仅发送本地路径。</p></div>
                </div>
                <div className="settings-group general-options-group">
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>上传方式</strong>
                      <span>仅传路径时，从当前 Gateway 的工作区选择文件，不读取或上传文件正文。</span>
                    </div>
                    <div className="settings-segmented" aria-label="文件上传方式">
                      {(["content", "path"] as FileUploadMode[]).map((mode) => (
                        <button
                          key={mode}
                          className={settings.file_upload_mode === mode ? "active" : ""}
                          type="button"
                          aria-pressed={settings.file_upload_mode === mode}
                          onClick={() => onConversationChange({ file_upload_mode: mode })}
                        >
                          {mode === "content" ? "上传内容" : "仅传路径"}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>最大文件大小</strong>
                      <span>上传文件内容时的单文件上限，单位 MB，范围 1–100。</span>
                    </div>
                    <NumberSettingInput
                      label="最大文件大小（MB）"
                      value={settings.file_upload_max_size_mb}
                      minimum={1}
                      maximum={100}
                      step={1}
                      onChange={(value) => onConversationChange({ file_upload_max_size_mb: value })}
                    />
                  </div>
                </div>

                <div className="settings-section-heading general-spaced-heading">
                  <div><h2>文件查看</h2><p>控制项目文件查看区的标签行为。</p></div>
                </div>
                <div className="settings-group general-options-group">
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>最大标签数</strong>
                      <span>最多同时打开 1–50 个文件；超过上限时替换最早打开的标签。</span>
                    </div>
                    <NumberSettingInput
                      label="文件查看最大标签数"
                      value={settings.project_files_max_tabs}
                      minimum={1}
                      maximum={50}
                      step={1}
                      onChange={(value) => onConversationChange({ project_files_max_tabs: value })}
                    />
                  </div>
                </div>

                <ApprovalShortcutSettings
                  value={settings.approval_shortcuts}
                  onChange={(value) => onConversationChange({ approval_shortcuts: value })}
                  registrationError={approvalShortcutError}
                />

                <div className="settings-section-heading general-spaced-heading">
                  <div><h2>会话</h2><p>控制发送方式、处理用时和系统通知。</p></div>
                </div>
                <div className="settings-group general-options-group">
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>发送快捷键</strong>
                      <span>空闲时选择按 Enter 发送，或按 {composerModifierLabel()} 发送。</span>
                    </div>
                    <div className="settings-segmented" aria-label="发送快捷键">
                      {(["enter", "mod_enter"] as ComposerSendKey[]).map((key) => (
                        <button
                          key={key}
                          className={settings.composer_send_key === key ? "active" : ""}
                          type="button"
                          aria-pressed={settings.composer_send_key === key}
                          onClick={() => onConversationChange({ composer_send_key: key })}
                        >
                          {key === "enter" ? "Enter 发送" : `${composerModifierLabel()} 发送`}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>跟进处理方式</strong>
                      <span>运行时将后续消息排队，或引导当前运行。按 {composerModifierLabel()} 对单条消息执行相反操作；Shift+Enter 换行。</span>
                    </div>
                    <div className="settings-segmented" aria-label="跟进处理方式">
                      {(["queue", "steer"] as const).map((mode) => (
                        <button
                          key={mode}
                          type="button"
                          className={(settings.follow_up_mode ?? "queue") === mode ? "active" : ""}
                          aria-pressed={(settings.follow_up_mode ?? "queue") === mode}
                          onClick={() => onConversationChange({ follow_up_mode: mode })}
                        >{mode === "queue" ? "排队" : "引导"}</button>
                      ))}
                    </div>
                  </div>
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>在会话结尾显示编辑卡片</strong>
                      <span>每轮编辑结束后显示文件变更摘要。关闭后仍可在文件工作区查看变更。</span>
                    </div>
                    <button
                      className={`settings-switch ${settings.show_file_edit_summary ? "on" : ""}`}
                      type="button"
                      role="switch"
                      aria-checked={settings.show_file_edit_summary}
                      aria-label="在会话结尾显示编辑卡片"
                      onClick={() => onConversationChange({ show_file_edit_summary: !settings.show_file_edit_summary })}
                    ><span /></button>
                  </div>
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>显示处理用时</strong>
                      <span>每轮工作结束后，在对话中显示本轮处理总时间。</span>
                    </div>
                    <button
                      className={`settings-switch ${settings.show_turn_duration ? "on" : ""}`}
                      type="button"
                      role="switch"
                      aria-checked={settings.show_turn_duration}
                      aria-label="显示处理用时"
                      onClick={() => onConversationChange({ show_turn_duration: !settings.show_turn_duration })}
                    ><span /></button>
                  </div>
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>用时格式</strong>
                      <span>仅显示累计秒数，或按时、分、秒拆分显示。</span>
                    </div>
                    <div className="settings-segmented" aria-label="处理用时格式">
                      {(["seconds", "hms"] as TurnDurationFormat[]).map((format) => (
                        <button
                          key={format}
                          className={settings.turn_duration_format === format ? "active" : ""}
                          type="button"
                          aria-pressed={settings.turn_duration_format === format}
                          disabled={!settings.show_turn_duration}
                          onClick={() => onConversationChange({ turn_duration_format: format })}
                        >
                          {format === "seconds" ? "仅秒数" : "时分秒"}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>执行时通知</strong>
                      <span>会话开始执行时弹出系统通知。</span>
                    </div>
                    <button
                      className={`settings-switch ${settings.session_notify_on_start ? "on" : ""}`}
                      type="button"
                      role="switch"
                      aria-checked={settings.session_notify_on_start}
                      aria-label="执行时通知"
                      onClick={() => {
                        const enabled = !settings.session_notify_on_start;
                        onConversationChange({ session_notify_on_start: enabled });
                        if (enabled) {
                          void requestSessionNotificationPermission().then((state) => {
                            if (state !== "unknown") setNotificationPermission(state);
                          });
                        }
                      }}
                    ><span /></button>
                  </div>
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>执行完毕通知</strong>
                      <span>会话这一轮执行结束时弹出系统通知。</span>
                    </div>
                    <button
                      className={`settings-switch ${settings.session_notify_on_complete ? "on" : ""}`}
                      type="button"
                      role="switch"
                      aria-checked={settings.session_notify_on_complete}
                      aria-label="执行完毕通知"
                      onClick={() => {
                        const enabled = !settings.session_notify_on_complete;
                        onConversationChange({ session_notify_on_complete: enabled });
                        if (enabled) {
                          void requestSessionNotificationPermission().then((state) => {
                            if (state !== "unknown") setNotificationPermission(state);
                          });
                        }
                      }}
                    ><span /></button>
                  </div>
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>需要操作时通知</strong>
                      <span>会话请求权限、询问选择或等待确认计划时弹出系统通知。</span>
                    </div>
                    <button
                      className={`settings-switch ${settings.session_notify_on_interaction ? "on" : ""}`}
                      type="button"
                      role="switch"
                      aria-checked={settings.session_notify_on_interaction}
                      aria-label="需要操作时通知"
                      onClick={() => {
                        const enabled = !settings.session_notify_on_interaction;
                        onConversationChange({ session_notify_on_interaction: enabled });
                        if (enabled) {
                          void requestSessionNotificationPermission().then((state) => {
                            if (state !== "unknown") setNotificationPermission(state);
                          });
                        }
                      }}
                    ><span /></button>
                  </div>
                  {notificationPermission === "prompt" || notificationPermission === "denied" ? (
                    <div className="settings-row compact">
                      <div className="settings-row-copy">
                        <strong>通知权限</strong>
                        <span>
                          {notificationPermission === "denied"
                            ? "系统拒绝了通知。macOS 请到「系统设置 → 通知」中允许 Crab Desktop；Windows 请在系统通知设置中允许本应用。"
                            : "尚未获得系统通知权限。macOS 会弹出授权框；若已经拒绝过，请到系统设置的通知中允许 Crab Desktop。"}
                        </span>
                      </div>
                      <button
                        className="settings-command"
                        type="button"
                        onClick={() => {
                          void requestSessionNotificationPermission().then((state) => {
                            if (state !== "unknown") setNotificationPermission(state);
                          });
                        }}
                      >
                        请求通知权限
                      </button>
                    </div>
                  ) : null}
                </div>
              </section>
            )}

            {activeSection === "appearance" && (
              <section className="settings-section appearance-settings" aria-labelledby="appearance-settings-title">
                <h2 id="appearance-settings-title">皮肤预设</h2>
                <p className="settings-section-description">每个预设同时包含浅色和深色外观；导入的主题只使用声明式颜色、排版和图片资源，不执行第三方代码。</p>
                <div className="theme-preset-grid" aria-label="皮肤预设">
                  {themePresets.map((theme) => {
                    const builtin = themeRegistry.isBuiltin(theme.id);
                    const selected = theme.id === activeTheme.id;
                    return (
                      <article className={`theme-preset-card ${selected ? "active" : ""}`} key={theme.id}>
                        <button
                          className="theme-preset-select"
                          type="button"
                          aria-pressed={selected}
                          onClick={() => onThemePresetChange(theme.id)}
                        >
                          <span className="theme-preset-preview">
                            <i
                              className="light"
                              style={{
                                backgroundColor: theme.light.background_color,
                                color: theme.light.foreground_color,
                                ...(theme.preview?.light ? { backgroundImage: `url("${theme.preview.light}")` } : {}),
                              }}
                            ><b style={{ backgroundColor: theme.light.accent_color }} /></i>
                            <i
                              className="dark"
                              style={{
                                backgroundColor: theme.dark.background_color,
                                color: theme.dark.foreground_color,
                                ...(theme.preview?.dark ? { backgroundImage: `url("${theme.preview.dark}")` } : {}),
                              }}
                            ><b style={{ backgroundColor: theme.dark.accent_color }} /></i>
                          </span>
                          <span className="theme-preset-copy">
                            <strong>{theme.name}</strong>
                            <small>{builtin ? "内置" : theme.author} · v{theme.version}</small>
                          </span>
                          <span className="theme-preset-check"><Check /></span>
                        </button>
                        {renamingThemeId === theme.id ? (
                          <form
                            className="theme-preset-inline-action rename"
                            onSubmit={(event) => {
                              event.preventDefault();
                              finishThemeRename(theme);
                            }}
                          >
                            <input
                              autoFocus
                              aria-label={`重命名 ${theme.name}`}
                              maxLength={80}
                              value={themeNameDraft}
                              onChange={(event) => setThemeNameDraft(event.target.value)}
                              onKeyDown={(event) => {
                                if (event.key === "Escape") {
                                  setRenamingThemeId(null);
                                  setThemeNameDraft("");
                                }
                              }}
                            />
                            <button type="submit" title="保存名称" disabled={!themeNameDraft.trim()}><Check />保存</button>
                            <button type="button" title="取消重命名" onClick={() => setRenamingThemeId(null)}><X />取消</button>
                          </form>
                        ) : deletingThemeId === theme.id ? (
                          <div className="theme-preset-inline-action delete" role="alert">
                            <span>确定删除这个预设？</span>
                            <button
                              type="button"
                              className="danger confirm"
                              title={`确认删除 ${theme.name}`}
                              onClick={() => {
                                setDeletingThemeId(null);
                                onThemeDelete(theme.id);
                              }}
                            ><Trash2 />删除</button>
                            <button type="button" title="取消删除" onClick={() => setDeletingThemeId(null)}><X />取消</button>
                          </div>
                        ) : (
                          <div className="theme-preset-actions">
                            <button type="button" title={`复制 ${theme.name}`} onClick={() => onThemeDuplicate(theme.id)}><Copy />复制</button>
                            {!builtin && (
                              <>
                                <button type="button" title={`重命名 ${theme.name}`} onClick={() => beginThemeRename(theme)}><Pencil />重命名</button>
                                <button type="button" className="danger" title={`删除 ${theme.name}`} onClick={() => beginThemeDelete(theme)}><Trash2 />删除</button>
                              </>
                            )}
                          </div>
                        )}
                      </article>
                    );
                  })}
                </div>
                <div className="theme-transfer-toolbar">
                  <button type="button" onClick={() => themeImportInputRef.current?.click()}><Upload />导入主题或皮肤</button>
                  <button type="button" onClick={() => void exportTheme()}><Download />导出主题</button>
                  <button type="button" onClick={() => void exportSkin()} disabled={!activeTheme.visuals && !activeTheme.preview}><Download />导出完整皮肤</button>
                  <button type="button" onClick={onThemeRestoreDefault} disabled={activeTheme.id === "builtin.crab"}><RotateCcw />恢复 Crab 默认</button>
                  <input
                    ref={themeImportInputRef}
                    className="visually-hidden"
                    type="file"
                    accept=".crabtheme.json,.crabskin,application/json,application/zip"
                    aria-label="导入 Crab 主题或皮肤"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) void importThemeFile(file);
                      event.target.value = "";
                    }}
                  />
                </div>
                {themeTransferMessage && <div className="appearance-success">{themeTransferMessage}</div>}

                <div className="appearance-subheading appearance-spaced-heading">
                  <div><h2>外观模式</h2><p>选择跟随系统，或固定使用预设中的浅色或深色版本。</p></div>
                </div>
                <div className="theme-choice-grid">
                  <button
                    className={`theme-choice ${settings.theme_mode === "system" ? "active" : ""}`}
                    type="button"
                    aria-pressed={settings.theme_mode === "system"}
                    onClick={() => onThemeModeChange("system")}
                  >
                    <span className="theme-preview system">
                      <span className="theme-preview-sidebar" />
                      <span className="theme-preview-window"><i /><i /><i /></span>
                    </span>
                    <span className="theme-choice-label"><strong>系统</strong><small>跟随系统外观</small></span>
                    <span className="theme-choice-check"><Check /></span>
                  </button>
                  <button
                    className={`theme-choice ${settings.theme_mode === "light" ? "active" : ""}`}
                    type="button"
                    aria-pressed={settings.theme_mode === "light"}
                    onClick={() => onThemeModeChange("light")}
                  >
                    <span className="theme-preview light">
                      <span className="theme-preview-sidebar" />
                      <span className="theme-preview-window"><i /><i /><i /></span>
                    </span>
                    <span className="theme-choice-label"><strong>浅色</strong><small>始终使用浅色主题</small></span>
                    <span className="theme-choice-check"><Check /></span>
                  </button>
                  <button
                    className={`theme-choice ${settings.theme_mode === "dark" ? "active" : ""}`}
                    type="button"
                    aria-pressed={settings.theme_mode === "dark"}
                    onClick={() => onThemeModeChange("dark")}
                  >
                    <span className="theme-preview dark">
                      <span className="theme-preview-sidebar" />
                      <span className="theme-preview-window"><i /><i /><i /></span>
                    </span>
                    <span className="theme-choice-label"><strong>深色</strong><small>始终使用深色主题</small></span>
                    <span className="theme-choice-check"><Check /></span>
                  </button>
                </div>

                <div className="appearance-subheading appearance-spaced-heading">
                  <div>
                    <h2>当前预设细节</h2>
                    <p>{activeThemeIsBuiltin ? "修改任意字段时会自动创建一个可编辑副本。" : `正在编辑 ${activeTheme.name}。`}</p>
                  </div>
                </div>
                <div className="theme-profiles">
                  {(settings.theme_mode === "system" || settings.theme_mode === "light") && (
                    <ThemeProfileEditor
                      scheme="light"
                      profile={activeTheme.light}
                      onChange={(changes) => onThemeProfileChange("light", changes)}
                    />
                  )}
                  {(settings.theme_mode === "system" || settings.theme_mode === "dark") && (
                    <ThemeProfileEditor
                      scheme="dark"
                      profile={activeTheme.dark}
                      onChange={(changes) => onThemeProfileChange("dark", changes)}
                    />
                  )}
                </div>

                <div className="appearance-subheading appearance-spaced-heading">
                  <div><h2>偏好</h2><p>控制交互指针、字号和文本呈现方式。</p></div>
                </div>
                <div className="settings-group appearance-options-group">
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>指针光标</strong>
                      <span>在按钮、链接和可点击项目上显示手形指针。</span>
                    </div>
                    <button
                      className={`settings-switch ${settings.pointer_cursor ? "on" : ""}`}
                      type="button"
                      role="switch"
                      aria-checked={settings.pointer_cursor}
                      aria-label="指针光标"
                      onClick={() => onAppearanceChange({ pointer_cursor: !settings.pointer_cursor })}
                    ><span /></button>
                  </div>
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>界面字号</strong>
                      <span>调整正文、导航和表单控件的基础字号。</span>
                    </div>
                    <div className="settings-stepper" aria-label="界面字号">
                      <button
                        type="button"
                        title="减小界面字号"
                        aria-label="减小界面字号"
                        disabled={settings.ui_font_size <= 11}
                        onClick={() => onAppearanceChange({ ui_font_size: settings.ui_font_size - 1 })}
                      ><Minus /></button>
                      <output>{settings.ui_font_size}px</output>
                      <button
                        type="button"
                        title="增大界面字号"
                        aria-label="增大界面字号"
                        disabled={settings.ui_font_size >= 18}
                        onClick={() => onAppearanceChange({ ui_font_size: settings.ui_font_size + 1 })}
                      ><Plus /></button>
                    </div>
                  </div>
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>代码字号</strong>
                      <span>调整代码块、工具输出和 Diff 的字号。</span>
                    </div>
                    <div className="settings-stepper" aria-label="代码字号">
                      <button
                        type="button"
                        title="减小代码字号"
                        aria-label="减小代码字号"
                        disabled={settings.code_font_size <= 10}
                        onClick={() => onAppearanceChange({ code_font_size: settings.code_font_size - 1 })}
                      ><Minus /></button>
                      <output>{settings.code_font_size}px</output>
                      <button
                        type="button"
                        title="增大代码字号"
                        aria-label="增大代码字号"
                        disabled={settings.code_font_size >= 18}
                        onClick={() => onAppearanceChange({ code_font_size: settings.code_font_size + 1 })}
                      ><Plus /></button>
                    </div>
                  </div>
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>Diff 标记</strong>
                      <span>使用色条或传统的加减号区分变更。</span>
                    </div>
                    <div className="settings-segmented" aria-label="Diff 标记">
                      {(["color", "symbols"] as DiffMarkerStyle[]).map((style) => (
                        <button
                          key={style}
                          className={settings.diff_marker_style === style ? "active" : ""}
                          type="button"
                          aria-pressed={settings.diff_marker_style === style}
                          onClick={() => onAppearanceChange({ diff_marker_style: style })}
                        >
                          {style === "color" ? "色条" : "+ / -"}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>字体平滑</strong>
                      <span>使用抗锯齿方式绘制界面和代码文字。</span>
                    </div>
                    <button
                      className={`settings-switch ${settings.font_smoothing ? "on" : ""}`}
                      type="button"
                      role="switch"
                      aria-checked={settings.font_smoothing}
                      aria-label="字体平滑"
                      onClick={() => onAppearanceChange({ font_smoothing: !settings.font_smoothing })}
                    ><span /></button>
                  </div>
                </div>

                <div className="appearance-subheading appearance-spaced-heading">
                  <div><h2>Dock 图标</h2><p>选择应用在 Dock 或任务栏中显示的图标。</p></div>
                </div>
                <div className="dock-icon-grid" aria-label="Dock 图标选择">
                  <button
                    className={`dock-icon-choice ${settings.dock_icon === "dark" ? "active" : ""}`}
                    type="button"
                    disabled={dockIconBusy}
                    onClick={() => void selectDockIcon("dark")}
                  >
                    <span className="crab-icon-preview dark"><img src={DARK_DOCK_ICON} alt="" /></span>
                    <span>黑底白蟹</span>
                    <i><Check /></i>
                  </button>
                  <button
                    className={`dock-icon-choice ${settings.dock_icon === "light" ? "active" : ""}`}
                    type="button"
                    disabled={dockIconBusy}
                    onClick={() => void selectDockIcon("light")}
                  >
                    <span className="crab-icon-preview light"><img src={LIGHT_DOCK_ICON} alt="" /></span>
                    <span>白底黑蟹</span>
                    <i><Check /></i>
                  </button>
                  <button
                    className={`dock-icon-choice custom ${settings.dock_icon === "custom" ? "active" : ""}`}
                    type="button"
                    disabled={dockIconBusy || !isDesktopShell()}
                    onClick={() => customIconInputRef.current?.click()}
                  >
                    <span className="crab-icon-preview custom">
                      {customIconPreview ? <img src={customIconPreview} alt="" /> : <ImageIcon />}
                    </span>
                    <span>自定义图标</span>
                    <i>{settings.dock_icon === "custom" ? <Check /> : <Upload />}</i>
                  </button>
                  <input
                    ref={customIconInputRef}
                    className="visually-hidden"
                    type="file"
                    accept="image/png,image/jpeg,image/webp"
                    aria-label="上传自定义 Dock 图标"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) void uploadCustomIcon(file);
                      event.target.value = "";
                    }}
                  />
                </div>
                {!isDesktopShell() && <div className="appearance-hint">Dock 图标仅在桌面应用中可更改。</div>}
                {appearanceError && <div className="appearance-error">{appearanceError}</div>}
              </section>
            )}

            {activeSection === "document" && (
              <section className="settings-section" aria-labelledby="document-settings-title">
                <h2 id="document-settings-title">翻译</h2>
                <p className="settings-section-description">使用当前会话选择的模型翻译文档。请求会并行执行，译文缓存按批次安全保存。</p>
                <div className="settings-group document-translation-group">
                  <div className="settings-row document-engine-row">
                    <div className="settings-row-copy">
                      <strong>高精度 PDF 引擎</strong>
                      <span>
                        {preciseEngine?.status === "ready"
                          ? `BabelDOC ${preciseEngine.version} 已就绪；新的全文翻译会默认生成原生译后 PDF。`
                          : preciseEngine?.detail ?? (documentEngineLoading ? "正在读取当前 Gateway 的能力…" : documentCapabilities === undefined ? "连接 Gateway 后检测引擎状态。" : "当前 Gateway 不支持高精度 PDF 引擎。")}
                      </span>
                      <small>
                        本地解析与排版，不接收模型密钥
                        {preciseEngine?.install_source === "offline_bundle"
                          ? " · 已校验离线资源包"
                          : " · 程序与资源来自 BabelDOC 官方源"}
                        {preciseEngine ? ` · BabelDOC ${preciseEngine.version}` : ""}
                        {preciseEngine?.download_bytes
                          ? ` · ${preciseEngine.download_estimated ? "安装资源约 " : "资源 "}${Math.ceil(preciseEngine.download_bytes / 1024 / 1024)} MiB`
                          : ""}
                      </small>
                      {documentEngineError && <small className="document-engine-error">{documentEngineError}</small>}
                    </div>
                    {documentEngineLoading ? (
                      <div className="settings-loading-status" role="status">
                        <LoaderCircle className="spin" aria-hidden="true" />
                        <span>正在检测文档引擎…</span>
                      </div>
                    ) : canManageDocumentEngine ? (
                      preciseEngine?.status === "ready" ? (
                        <button
                          className="settings-command"
                          type="button"
                          disabled={documentEngineBusy !== null}
                          onClick={() => void manageDocumentEngine("remove")}
                        ><Trash2 />{documentEngineBusy === "remove" ? "正在删除…" : "删除引擎"}</button>
                      ) : (
                        <div className="document-engine-install">
                          <div className="document-engine-command">
                            <code>{documentEngineInstallCommand}</code>
                            <button
                              className="settings-command primary"
                              type="button"
                              title={`执行 ${documentEngineInstallCommand}`}
                              disabled={documentEngineBusy !== null || documentCapabilities === undefined}
                              onClick={() => void manageDocumentEngine("install")}
                            ><Terminal />{documentEngineBusy === "install" ? "正在安装…" : preciseEngine?.status === "upgrade_required" ? "执行升级" : "执行安装"}</button>
                          </div>
                          {documentEngineBusy === "install" && documentEngineProgress && (
                            <div
                              className="document-engine-progress"
                              role="progressbar"
                              aria-label="高精度 PDF 引擎安装进度"
                              aria-valuemin={0}
                              aria-valuemax={100}
                              aria-valuenow={documentEngineProgress.percent}
                              aria-valuetext={documentEngineProgress.detail}
                            >
                              <div className="document-engine-progress-copy">
                                <span>{documentEngineProgress.detail}</span>
                                <strong>{documentEngineProgress.percent}%</strong>
                              </div>
                              <div className="document-engine-progress-track">
                                <i style={{ width: `${documentEngineProgress.percent}%` }} />
                              </div>
                            </div>
                          )}
                        </div>
                      )
                    ) : (
                      <div className="document-engine-command remote">
                        <small>请在 Gateway 主机运行</small>
                        <code>{documentEngineInstallCommand}</code>
                      </div>
                    )}
                  </div>
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>并行请求数</strong>
                      <span>同时发送给模型的翻译请求数量，范围 1–8。</span>
                    </div>
                    <NumberSettingInput
                      label="翻译并行请求数"
                      value={settings.document_translation_concurrency}
                      minimum={1}
                      maximum={8}
                      step={1}
                      onChange={(value) => onDocumentChange({
                        document_translation_concurrency: value,
                      })}
                    />
                  </div>
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>单批 Block 数</strong>
                      <span>仅兼容模式生效。每次请求携带的文本 Block 数量，范围 10–400；过大的文本仍受字符上限约束。</span>
                    </div>
                    <NumberSettingInput
                      label="翻译单批 Block 数"
                      value={settings.document_translation_batch_size}
                      minimum={10}
                      maximum={400}
                      step={10}
                      onChange={(value) => onDocumentChange({
                        document_translation_batch_size: value,
                      })}
                    />
                  </div>
                </div>

                <div className="settings-section-heading general-spaced-heading">
                  <div><h2>选择</h2><p>控制 PDF 页面中的原文选择行为。</p></div>
                </div>
                <div className="settings-group">
                  <div className="settings-row compact">
                    <div className="settings-row-copy">
                      <strong>显示原文</strong>
                      <span>框选 PDF 文字时显示所选原文；关闭后仍可复制。</span>
                    </div>
                    <button
                      className={`settings-switch ${settings.document_show_original_text ? "on" : ""}`}
                      type="button"
                      role="switch"
                      aria-checked={settings.document_show_original_text}
                      aria-label="显示原文"
                      onClick={() => onDocumentChange({
                        document_show_original_text: !settings.document_show_original_text,
                      })}
                    ><span /></button>
                  </div>
                </div>
              </section>
            )}

            {activeSection === "runtime" && (
              <RuntimeSettingsPanel
                localVmSelected={settings.computer_use_environment === "local_vm"}
                computerUseEnvironment={<VirtualMachineSettings settings={settings} taskBusy={computerTaskBusy} onChange={onVmSettingsChange} onRefresh={onVmRefresh} lumeInstaller={lumeInstaller} />}
                activeConnection={activeConnection}
                activeProject={activeProject}
                gateway={activeGateway}
                data={runtimeSettings}
                loading={runtimeSettingsLoading}
                error={runtimeSettingsError}
                onRefresh={onRefreshRuntimeSettings}
                onMutate={onMutateRuntimeSettings}
              />
            )}

            {activeSection === "prompts" && (
              <PromptSettingsPanel
                activeConnection={activeConnection}
                activeProject={activeProject}
                gateway={activeGateway}
                data={promptSettings}
                loading={promptSettingsLoading}
                error={promptSettingsError}
                onRefresh={onRefreshPromptSettings}
                onMutate={onMutatePromptSettings}
              />
            )}

            {activeSection === "connections" && (
              <section className="settings-section" aria-labelledby="connection-settings-title">
                <div className="settings-section-heading">
                  <div>
                    <h2 id="connection-settings-title">已保存连接</h2>
                    <p>切换当前 Gateway，或管理连接地址与凭据。</p>
                  </div>
                  <button className="settings-command primary" type="button" onClick={onNewConnection}>
                    <Plus />
                    <span>添加 Gateway</span>
                  </button>
                </div>
                <div className="settings-group settings-entity-list">
                  {settings.connection_order.map((id) => {
                    const connection = settings.connections.find((item) => item.id === id);
                    if (!connection) return null;
                    const status = gateways[id]?.status;
                    const active = connection.id === activeConnection?.id;
                    return (
                      <div className="settings-entity-row" key={connection.id}>
                        <span className={`settings-entity-icon connection ${status ?? "offline"}`}><Server /></span>
                        <span className="settings-entity-copy">
                          <strong>{connection.name}</strong>
                          <small title={connection.base_url}>{connection.base_url}</small>
                          <span className={`settings-entity-status ${status ?? "offline"}`}>
                            {connectionStatusLabel(status)}
                          </span>
                        </span>
                        <span className="settings-entity-actions">
                          {active ? (
                            <span className="settings-current"><Check />当前</span>
                          ) : (
                            <button
                              className="icon-button small"
                              type="button"
                              title={`切换至 ${connection.name}`}
                              aria-label={`切换至 ${connection.name}`}
                              onClick={() => onActivateConnection(connection.id)}
                            >
                              <ArrowRightLeft />
                            </button>
                          )}
                          <button
                            className="icon-button small"
                            type="button"
                            title={`编辑 ${connection.name}`}
                            aria-label={`编辑 ${connection.name}`}
                            onClick={() => onEditConnection(connection.id)}
                          >
                            <Pencil />
                          </button>
                          {connection.id !== "local" && (
                            <button
                              className="icon-button small danger-icon-button"
                              type="button"
                              title={`删除 ${connection.name}`}
                              aria-label={`删除 ${connection.name}`}
                              onClick={() => onDeleteConnection(connection.id)}
                            >
                              <Trash2 />
                            </button>
                          )}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </section>
            )}

            {activeSection === "models" && (
              <ModelSettingsPanel
                activeConnection={activeConnection}
                activeProject={activeProject}
                gateway={activeGateway}
                data={modelSettings}
                loading={modelSettingsLoading}
                error={modelSettingsError}
                onRefresh={onRefreshModelSettings}
                onMutate={onMutateModelSettings}
                onTest={onTestModel}
              />
            )}

            {activeSection === "usage" && (
              <UsageSettingsPanel
                connection={activeConnection}
                project={activeProject}
                online={activeGateway?.status === "online"}
              />
            )}

            {activeSection === "projects" && (
              <section className="settings-section" aria-labelledby="project-settings-title">
                <div className="settings-section-heading">
                  <div>
                    <h2 id="project-settings-title">项目与目录</h2>
                    <p>管理当前 Gateway 中用于创建会话的工作目录。</p>
                  </div>
                  <button
                    className="settings-command primary"
                    type="button"
                    disabled={!canManageProjects}
                    onClick={onNewProject}
                  >
                    <Plus />
                    <span>添加项目</span>
                  </button>
                </div>

                <div className="settings-context-row">
                  <label htmlFor="settings-project-connection">当前 Gateway</label>
                  <select
                    id="settings-project-connection"
                    value={activeConnection?.id ?? ""}
                    onChange={(event) => onActivateConnection(event.target.value)}
                  >
                    {settings.connection_order.map((id) => {
                      const connection = settings.connections.find((item) => item.id === id);
                      return connection ? <option key={id} value={id}>{connection.name}</option> : null;
                    })}
                  </select>
                </div>

                {!canManageProjects && (
                  <div className="settings-inline-note">
                    <WifiOff />
                    <span>连接 Gateway 后可以新增项目或编辑目录。</span>
                  </div>
                )}

                <div className="settings-context-row document-root-setting">
                  <label htmlFor="settings-document-root">文档项目默认位置</label>
                  <input
                    id="settings-document-root"
                    value={activeConnection?.document_workspace_root
                      ?? activeGateway?.workspace?.documents_dir
                      ?? ""}
                    placeholder={activeGateway?.workspace?.documents_dir ?? "连接 Gateway 后自动检测"}
                    disabled={!activeConnection || !canManageProjects || !onDocumentWorkspaceRoot}
                    onChange={(event) => activeConnection
                      && onDocumentWorkspaceRoot?.(
                        activeConnection.id,
                        event.target.value.trim() ? event.target.value : null,
                      )}
                  />
                  <small>只影响之后创建的文档项目，不移动已有工作区。</small>
                </div>

                <div className="settings-group settings-entity-list">
                  {activeConnection?.projects.map((project) => (
                    <div className="settings-entity-row" key={project.id}>
                      <span className="settings-entity-icon project">{project.kind === "document" ? <FileText /> : <FolderCog />}</span>
                      <span className="settings-entity-copy">
                        <strong>{project.name}</strong>
                        <small title={projectDirectorySummary(project)}>{projectDirectorySummary(project)}</small>
                      </span>
                      <span className="settings-entity-actions">
                        {project.id === activeProject?.id && <span className="settings-current"><Check />当前</span>}
                        <button
                          className="icon-button small"
                          type="button"
                          disabled={!canManageProjects}
                          title={`编辑 ${project.name}`}
                          aria-label={`编辑 ${project.name}`}
                          onClick={() => onEditProject(project)}
                        >
                          <Pencil />
                        </button>
                      </span>
                    </div>
                  ))}
                  {activeConnection?.projects.length === 0 && (
                    <div className="settings-list-empty">当前 Gateway 还没有项目</div>
                  )}
                </div>
              </section>
            )}

            {activeSection === "about" && (
              <section className="settings-section settings-about" aria-labelledby="about-settings-title">
                <div className="settings-about-card">
                  <div className="settings-about-mark"><Info /></div>
                  <div className="settings-about-copy">
                    <h2 id="about-settings-title">Crab Desktop</h2>
                    <p>面向 CrabCode 工作区的桌面客户端。</p>
                    <dl className="settings-about-details">
                      <div><dt>版本</dt><dd>v{desktopPackage.version}</dd></div>
                      <div><dt>Gateway 协议</dt><dd>v1</dd></div>
                      <div><dt>文档布局</dt><dd>paragraph-v1</dd></div>
                    </dl>
                    <p className="settings-about-author">作者 Yuri Head</p>
                  </div>
                </div>
              </section>
            )}
          </div>
        )}
      </main>
    </div>
  );
}
