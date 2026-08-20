import {
  ArrowLeft,
  ArrowRightLeft,
  Check,
  FolderCog,
  Image as ImageIcon,
  Minus,
  Pencil,
  Paintbrush,
  Plus,
  RotateCcw,
  Search,
  Server,
  Settings,
  SlidersHorizontal,
  Trash2,
  Upload,
  WifiOff,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { isDesktopShell, loadCustomDockIcon } from "./native";
import type {
  CodeFontFamily,
  ConnectionPreset,
  DesktopSettings,
  DiffMarkerStyle,
  DockIconChoice,
  GatewayViewState,
  ProjectPreset,
  ThemeMode,
  ThemeProfile,
  UiFontFamily,
} from "./types";

export type SettingsSectionId = "general" | "appearance" | "connections" | "projects";

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
    description: "运行环境与本地启动设置",
    searchText: "常规 运行环境 Python 路径 自动检测 本地启动 浏览器模式",
  },
  {
    id: "appearance",
    title: "外观",
    description: "主题、颜色、字体与应用图标",
    searchText: "外观 主题 系统 跟随系统 浅色 深色 强调色 背景色 前景色 界面字体 代码字体 半透明侧栏 对比度 指针光标 字号 Diff 标记 加号 减号 字体平滑 Dock 图标 螃蟹 自定义 上传",
  },
  {
    id: "connections",
    title: "Gateway 连接",
    description: "连接地址、凭据与当前 Gateway",
    searchText: "Gateway 连接 地址 密码 凭据 远程 本地 当前连接 新建 编辑 删除",
  },
  {
    id: "projects",
    title: "项目",
    description: "工作目录与项目管理",
    searchText: "项目 项目目录 工作目录 文件夹 当前项目 新建 编辑 移除",
  },
];

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
  connections: Server,
  projects: FolderCog,
} satisfies Record<SettingsSectionId, typeof Settings>;

const DARK_DOCK_ICON = new URL("../src-tauri/icons/icon.png", import.meta.url).href;
const LIGHT_DOCK_ICON = new URL("../src-tauri/resources/dock-icon-light.png", import.meta.url).href;

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
    canvas.width = 512;
    canvas.height = 512;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("无法处理这张图片");
    context.clearRect(0, 0, 512, 512);
    const scale = Math.min(512 / image.naturalWidth, 512 / image.naturalHeight);
    const width = Math.round(image.naturalWidth * scale);
    const height = Math.round(image.naturalHeight * scale);
    context.drawImage(image, (512 - width) / 2, (512 - height) / 2, width, height);
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
  settings: DesktopSettings;
  gateways: Record<string, GatewayViewState>;
  activeConnection: ConnectionPreset | null;
  activeProject: ProjectPreset | null;
  activeSection: SettingsSectionId;
  onSectionChange: (section: SettingsSectionId) => void;
  onBack: () => void;
  onSavePythonPath: (path: string) => void;
  onThemeModeChange: (mode: ThemeMode) => void;
  onThemeProfileChange: (scheme: "light" | "dark", changes: Partial<ThemeProfile>) => void;
  onAppearanceChange: (changes: AppearanceSettingsUpdate) => void;
  onDockIconChange: (choice: DockIconChoice, pngBytes?: Uint8Array) => Promise<void>;
  onActivateConnection: (id: string) => void;
  onNewConnection: () => void;
  onEditConnection: (id: string) => void;
  onDeleteConnection: (id: string) => void;
  onNewProject: () => void;
  onEditProject: (project: ProjectPreset) => void;
}

export type AppearanceSettingsUpdate = Partial<Pick<DesktopSettings,
  | "pointer_cursor"
  | "ui_font_size"
  | "code_font_size"
  | "diff_marker_style"
  | "font_smoothing"
>>;

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
  onThemeModeChange,
  onThemeProfileChange,
  onAppearanceChange,
  onDockIconChange,
  onActivateConnection,
  onNewConnection,
  onEditConnection,
  onDeleteConnection,
  onNewProject,
  onEditProject,
}: SettingsViewProps) {
  const [query, setQuery] = useState("");
  const [pythonPath, setPythonPath] = useState(settings.python_path ?? "");
  const [customIconPreview, setCustomIconPreview] = useState<string | null>(null);
  const [dockIconBusy, setDockIconBusy] = useState(false);
  const [appearanceError, setAppearanceError] = useState<string | null>(null);
  const customIconInputRef = useRef<HTMLInputElement>(null);
  const matchingSections = useMemo(() => filterSettingsSections(query), [query]);
  const activeGateway = activeConnection ? gateways[activeConnection.id] : null;
  const canManageProjects = activeGateway?.status === "online" && Boolean(activeGateway.workspace);

  useEffect(() => {
    setPythonPath(settings.python_path ?? "");
  }, [settings.python_path]);

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

  const activeDefinition = SETTINGS_SECTIONS.find((section) => section.id === activeSection)!;

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
                <div className="settings-group">
                  {isDesktopShell() ? (
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
              </section>
            )}

            {activeSection === "appearance" && (
              <section className="settings-section appearance-settings" aria-labelledby="appearance-settings-title">
                <h2 id="appearance-settings-title">主题</h2>
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

                <div className="theme-profiles">
                  {(settings.theme_mode === "system" || settings.theme_mode === "light") && (
                    <ThemeProfileEditor
                      scheme="light"
                      profile={settings.light_theme}
                      onChange={(changes) => onThemeProfileChange("light", changes)}
                    />
                  )}
                  {(settings.theme_mode === "system" || settings.theme_mode === "dark") && (
                    <ThemeProfileEditor
                      scheme="dark"
                      profile={settings.dark_theme}
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

                <div className="settings-group settings-entity-list">
                  {activeConnection?.projects.map((project) => (
                    <div className="settings-entity-row" key={project.id}>
                      <span className="settings-entity-icon project"><FolderCog /></span>
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
          </div>
        )}
      </main>
    </div>
  );
}
