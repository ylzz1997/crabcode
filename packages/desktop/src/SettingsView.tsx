import {
  ArrowLeft,
  ArrowRightLeft,
  Check,
  FolderCog,
  Pencil,
  Plus,
  RotateCcw,
  Search,
  Server,
  Settings,
  SlidersHorizontal,
  Trash2,
  WifiOff,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { isDesktopShell } from "./native";
import type {
  ConnectionPreset,
  DesktopSettings,
  GatewayViewState,
  ProjectPreset,
} from "./types";

export type SettingsSectionId = "general" | "connections" | "projects";

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
  connections: Server,
  projects: FolderCog,
} satisfies Record<SettingsSectionId, typeof Settings>;

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
  onActivateConnection: (id: string) => void;
  onNewConnection: () => void;
  onEditConnection: (id: string) => void;
  onDeleteConnection: (id: string) => void;
  onNewProject: () => void;
  onEditProject: (project: ProjectPreset) => void;
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
  onActivateConnection,
  onNewConnection,
  onEditConnection,
  onDeleteConnection,
  onNewProject,
  onEditProject,
}: SettingsViewProps) {
  const [query, setQuery] = useState("");
  const [pythonPath, setPythonPath] = useState(settings.python_path ?? "");
  const matchingSections = useMemo(() => filterSettingsSections(query), [query]);
  const activeGateway = activeConnection ? gateways[activeConnection.id] : null;
  const canManageProjects = activeGateway?.status === "online" && Boolean(activeGateway.workspace);

  useEffect(() => {
    setPythonPath(settings.python_path ?? "");
  }, [settings.python_path]);

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
